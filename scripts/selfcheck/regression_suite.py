"""Offline regression checks for the auto-resume application.

The suite uses a temporary data directory and never talks to a recruitment
platform. It exercises the API surface, configuration persistence, database
constraints, task control, collection scheduling, matching, application
scheduling, platform registration, and the Edge login-state guard.

Run from the project root:

    runtime\\python.exe -X utf8 scripts\\selfcheck\\regression_suite.py
"""
from __future__ import annotations

import asyncio
import inspect
import json
import os
import tempfile
import time
import traceback
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

# Must be set before importing backend modules: paths and database paths are
# intentionally resolved once at import time in the application.
_TMP = tempfile.TemporaryDirectory(prefix="auto_resume_regression_", ignore_cleanup_errors=True)
os.environ["AUTO_RESUME_DATA_DIR"] = _TMP.name
os.environ["AUTO_RESUME_API_PORT"] = "18080"

ROOT = Path(__file__).resolve().parents[2]
import sys

sys.path.insert(0, str(ROOT))

from fastapi import HTTPException
from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError

from backend.api import apply as apply_api
from backend.api import collect as collect_api
from backend.api import match as match_api
from backend.api import pipeline as pipeline_api
from backend.api import userconfig as userconfig_api
from backend.core import task_control
from backend.core.job_filter import (
    company_block_reason,
    keyword_in_list_fields,
    parse_active_days,
    parse_salary_k,
    passes_filters,
)
from backend.core.paths import DATA_DIR, RESUMES_DIR
from backend.core.task_locks import apply_lock, collect_lock, match_lock
from backend.db.database import checkpoint_wal, engine, init_db, session_scope
from backend.db.models import AppConfig, Application as AppRow
from backend.db.models import Base, DailyStat, Job as JobRow, JobStatus
from backend.platforms import registry
from backend.platforms.base import ApplyResult, BasePlatform, Job, JobQuery
from backend.services import edge_login, task_hub
from backend.services.matcher import score_job
from backend.services.verify import VerifyRequired


TESTS: list[tuple[str, callable]] = []


def test(name: str):
    def deco(fn):
        TESTS.append((name, fn))
        return fn
    return deco


def reset_runtime() -> None:
    task_control.clear()
    task_hub.hub.state = None
    task_hub.hub._logs = []
    edge_login._status_cache.clear()
    edge_login._checked_at.clear()
    try:
        task_hub.RUNNING_MARKER.unlink(missing_ok=True)
    except OSError:
        pass


def reset_db() -> None:
    reset_runtime()
    init_db()
    Base.metadata.drop_all(engine)
    init_db()


def add_job(
    platform: str = "boss",
    job_id: str = "j1",
    status: str = JobStatus.COLLECTED,
    *,
    title: str = "嵌入式软件工程师",
    company: str = "示例科技",
    salary: str | None = "6-8K",
    city: str | None = "深圳",
    description: str | None = "熟悉 STM32 与嵌入式软件开发",
    extra: dict | None = None,
    score: float | None = None,
) -> int:
    with session_scope() as s:
        row = JobRow(
            platform=platform,
            platform_job_id=job_id,
            title=title,
            company=company,
            salary=salary,
            city=city,
            url=f"https://example.invalid/{platform}/{job_id}",
            description=description,
            extra=json.dumps(extra or {}, ensure_ascii=False),
            status=status,
            match_score=score,
        )
        s.add(row)
        s.flush()
        return int(row.id)


def insert_job(**kwargs) -> int:
    return add_job(**kwargs)


class FakePlatform(BasePlatform):
    def __init__(self, name: str, *, fail: bool = False, verify: bool = False,
                 delay: float = 0.0, result: ApplyResult | None = None):
        self.name = name
        self.display_name = name
        self.fail = fail
        self.verify = verify
        self.delay = delay
        self.result = result or ApplyResult(success=True, message="ok")
        self.calls: list[str] = []
        self.last_partial_jobs: list[Job] = []
        self.last_done_keywords: list[str] = []
        self.last_scanned = 0
        self.last_filtered = 0
        self.last_body_dropped = 0
        self.last_body_rescued = 0
        self._progress_handler = None

    async def check_login(self) -> bool:
        self.calls.append("check_login")
        return True

    async def search_jobs(self, query: JobQuery) -> list[Job]:
        self.calls.append("search_jobs")
        if self.delay:
            # Match production adapters: long waits are split into short
            # cancellable slices instead of one uninterruptible sleep.
            remaining = float(self.delay)
            while remaining > 0:
                chunk = min(0.05, remaining)
                await asyncio.sleep(chunk)
                task_control.raise_if_cancelled()
                remaining -= chunk
        if self.fail:
            self.last_partial_jobs = [
                Job(platform=self.name, platform_job_id="partial", title="部分岗位", company="C", url="u")
            ]
            raise RuntimeError("simulated adapter failure")
        if self.verify:
            self.last_partial_jobs = [
                Job(platform=self.name, platform_job_id="verify-partial", title="验证前岗位", company="C", url="u")
            ]
            raise VerifyRequired("simulated verification")
        job = Job(
            platform=self.name,
            platform_job_id=f"{self.name}-job",
            title=f"{self.name} 嵌入式工程师",
            company=f"{self.name} 公司",
            salary="6-9K",
            city="深圳",
            url=f"https://example.invalid/{self.name}",
            description="熟悉 STM32",
            extra={"degree": "本科", "experience": "应届"},
        )
        self.last_scanned = 3
        self.last_filtered = 1
        return [job]

    async def apply(self, job: Job, greeting: str | None) -> ApplyResult:
        self.calls.append(f"apply:{job.platform_job_id}")
        if self.delay:
            await asyncio.sleep(self.delay)
        return self.result.model_copy(deep=True)


@test("数据库：建表、唯一键、外键与 WAL 检查点")
def test_database_contract() -> None:
    reset_db()
    with session_scope() as s:
        s.add(JobRow(platform="boss", platform_job_id="same", title="a", company="c", url="u"))
    try:
        with session_scope() as s:
            s.add(JobRow(platform="boss", platform_job_id="same", title="b", company="c", url="u"))
    except IntegrityError:
        pass
    else:
        raise AssertionError("jobs unique constraint is not enforced")
    with engine.connect() as conn:
        assert conn.exec_driver_sql("PRAGMA foreign_keys").scalar() == 1
    checkpoint_wal()


@test("配置：中文 JSON 原子保存与乱码修复")
def test_userconfig_roundtrip() -> None:
    reset_runtime()
    userconfig_api.CONFIG_PATH.unlink(missing_ok=True)
    cfg = {
        "keywords": ["单片机", "嵌入式"],
        "company_blacklist": ["软通动力|iSoftStone"],
        "nested": {"city": "深圳"},
    }
    userconfig_api.save_userconfig(userconfig_api.UserConfigIn(config=cfg))
    raw = userconfig_api.CONFIG_PATH.read_text(encoding="utf-8")
    assert "单片机" in raw and "软通动力" in raw
    out = userconfig_api.get_userconfig()
    assert out["ok"] is True and out["config"] == cfg
    # One and two layers of UTF-8/Latin-1 double decoding.
    assert userconfig_api._repair_mojibake_text("åå") == "商品"
    assert userconfig_api._repair_mojibake_text("Ã¥ÂÂÃ¥ÂÂ") == "商品"


@test("任务控制：暂停/继续/停止不超过一个检查周期")
async def test_task_control() -> None:
    task_control.clear()
    assert not task_control.is_cancelled()
    task_control.request_pause()
    started = time.monotonic()
    waiter = asyncio.create_task(task_control.wait_if_paused(0.01))
    await asyncio.sleep(0.05)
    assert not waiter.done()
    task_control.clear_pause()
    await asyncio.wait_for(waiter, timeout=0.3)
    assert time.monotonic() - started < 1.0
    task_control.request()
    await task_control.cancellable_sleep(10, step=0.01)
    assert time.monotonic() - started < 1.0
    task_control.clear()


@test("任务状态中心：运行标记、快照与中断追溯")
def test_task_hub() -> None:
    reset_runtime()
    hub = task_hub.hub
    hub.begin("all", ["collect", "match", "apply"], "测试")
    snap = hub.snapshot()
    assert snap["active"] is True and snap["kind"] == "all"
    assert task_hub.RUNNING_MARKER.exists()
    hub.end(ok=True, summary="完成")
    snap = hub.snapshot()
    assert snap["active"] is False and snap["status"] == "done" and snap["percent"] == 100
    assert not task_hub.RUNNING_MARKER.exists()
    task_hub.RUNNING_MARKER.write_text('{"kind":"all","stages":["collect"]}', encoding="utf-8")
    msg = task_hub.note_interrupted_run()
    assert msg and "上次运行被中断" in msg


@test("岗位过滤：公司别名、精确匹配、HR 活跃度与正文关键词")
def test_job_filter() -> None:
    assert parse_salary_k("6-9K·14薪") == (6.0, 9.0)
    assert parse_salary_k("8000-15000元") == (8.0, 15.0)
    assert parse_active_days("2天前在线") == 2
    assert parse_active_days("刚刚活跃") == 0
    job = Job(
        platform="boss", platform_job_id="1", title="嵌入式软件", company="软通动力信息技术有限公司",
        salary="6-9K", city="深圳", url="u", extra={"company_short_name": "软通动力", "degree": "本科"},
    )
    assert company_block_reason(job.company, job.platform, job.extra, ["软通动力"]) is not None
    assert company_block_reason("软通动力信息技术有限公司", "boss", {}, ["=软通动力信息技术有限公司"]) is not None
    assert company_block_reason("软通动力信息技术有限公司", "boss", {}, ["=软通动力"]) is None
    assert keyword_in_list_fields(job, "嵌入式") is True
    query = JobQuery(
        keywords=["嵌入式"], cities=["深圳"], company_blacklist=["外包"],
        company_blacklist_by_platform={"boss": ["劳务"]}, hr_active_days=0,
    )
    job.extra["hr_active_days"] = 0
    assert passes_filters(job, "嵌入式", query) is True
    job.extra["hr_active_days"] = 4
    assert passes_filters(job, "嵌入式", query) is False


@test("匹配引擎：正文参与技能识别，标题/正文黑名单直接淘汰")
def test_matcher() -> None:
    resume = {
        "skills": ["STM32", "嵌入式", "C语言"], "expected_position": "嵌入式软件工程师",
        "years_experience": 0, "education": "本科",
    }
    good = {
        "title": "嵌入式软件工程师", "company": "A", "salary": "6-9K", "city": "深圳",
        "description": "岗位要求熟悉 STM32 和 FreeRTOS", "extra": {"degree": "本科", "experience": "应届"},
    }
    result = score_job(good, resume, {"threshold": 50, "salary_min": 4, "salary_max": 10, "cities": ["深圳"]})
    assert result["status"] == "matched", result
    blocked = dict(good, title="技术支持", description="需要出差")
    result = score_job(blocked, resume, {"threshold": 0, "salary_min": 4, "salary_max": 10, "blacklist": ["出差"]})
    assert result["status"] == "rejected"
    assert "正文含" in result["detail"]["reason"]


@test("采集调度：多平台并行、单平台失败隔离、验证跳过与取消保存")
async def test_collect_scheduler() -> None:
    reset_db()
    ok = FakePlatform("boss")
    bad = FakePlatform("zhilian", fail=True)
    fake = {"boss": ok, "zhilian": bad}
    with mock.patch.object(collect_api, "get_platform", lambda name: fake[name]):
        result = await collect_api._collect_impl(collect_api.CollectIn(
            platforms=["boss", "zhilian"], keywords=["嵌入式"], cities=["深圳"],
            max_jobs_by_platform={"boss": 5, "zhilian": 5},
        ))
    assert result["collected"] == 2  # one successful + one preserved partial
    assert result["by_platform"]["boss"]["ok"] is True
    assert result["by_platform"]["zhilian"]["ok"] is False
    assert task_hub.hub.snapshot()["active"] is False

    reset_db()
    verify = FakePlatform("liepin", verify=True)
    with mock.patch.object(collect_api, "get_platform", lambda name: verify):
        result = await collect_api._collect_impl(collect_api.CollectIn(
            platforms=["liepin"], keywords=["嵌入式", "单片机"], cities=["深圳"],
            max_jobs_by_platform={"liepin": 5},
        ))
    assert result["resume"] and result["resume"][0]["platform"] == "liepin"
    assert result["collected"] == 1

    reset_db()
    slow = FakePlatform("job51", delay=5)
    with mock.patch.object(collect_api, "get_platform", lambda name: slow):
        task = asyncio.create_task(collect_api._collect_impl(collect_api.CollectIn(
            platforms=["job51"], keywords=["嵌入式"], cities=["深圳"],
        )))
        await asyncio.sleep(0.05)
        task_control.request()
        stopped = await asyncio.wait_for(task, timeout=2)
    assert stopped["stopped"] is True
    assert task_hub.hub.snapshot()["active"] is False


@test("匹配落库：读取简历、更新状态与匹配记录")
def test_match_body() -> None:
    reset_db()
    (RESUMES_DIR / "profile.json").write_text(json.dumps({
        "parsed": {
            "skills": ["STM32", "嵌入式"], "expected_position": "嵌入式软件工程师",
            "years_experience": 0, "education": "本科",
        }
    }, ensure_ascii=False), encoding="utf-8")
    good_id = insert_job(title="嵌入式软件工程师", description="熟悉 STM32", extra={"degree": "本科", "experience": "应届"})
    blocked_id = insert_job(job_id="j2", title="技术支持", description="需要出差")
    applied_id = insert_job(job_id="j3", status=JobStatus.APPLIED, title="已投岗位")
    result = match_api._match_body(match_api.MatchIn(
        threshold=50, salary_min=4, salary_max=10, cities=["深圳"], blacklist=["出差"], flow="match",
    ))
    assert result["scanned"] == 2 and result["matched"] == 1
    with session_scope() as s:
        assert s.get(JobRow, good_id).status == JobStatus.MATCHED
        blocked = s.get(JobRow, blocked_id)
        assert blocked.status == JobStatus.REJECTED and "正文含" in json.loads(blocked.match_detail)["reason"]
        assert s.get(JobRow, applied_id).status == JobStatus.APPLIED
    rows = match_api.list_matches(limit=10)["rows"]
    assert any(r["id"] == good_id for r in rows)


@test("投递调度：公司屏蔽、平台每日上限、重复沟通与成功落库")
async def test_apply_scheduler() -> None:
    reset_db()
    blocked_id = insert_job(platform="boss", job_id="blocked", status=JobStatus.MATCHED, company="软通动力", title="嵌入式")
    b1 = insert_job(platform="boss", job_id="b1", status=JobStatus.MATCHED, title="嵌入式一", score=90)
    b2 = insert_job(platform="boss", job_id="b2", status=JobStatus.MATCHED, title="嵌入式二", score=80)
    z1 = insert_job(platform="zhilian", job_id="z1", status=JobStatus.MATCHED, title="嵌入式三", score=70)
    boss = FakePlatform("boss", result=ApplyResult(success=True, message="已沟通"))
    zhilian = FakePlatform("zhilian", result=ApplyResult(success=True, duplicate=True, message="此前已沟通"))
    fake = {"boss": boss, "zhilian": zhilian}
    with mock.patch.object(apply_api, "get_platform", lambda name: fake[name]), \
         mock.patch.object(apply_api, "load_resume", lambda: {"skills": ["STM32"]}), \
         mock.patch.object(task_control, "cancellable_sleep", lambda *a, **k: asyncio.sleep(0)):
        result = await apply_api._apply_impl(apply_api.ApplyIn(
            platforms=["boss", "zhilian"], daily_limit_by_platform={"boss": 1, "zhilian": 1},
            company_blacklist=["软通动力"], delay_min=1, delay_max=1,
        ))
    assert result["company_blocked"] == 1
    assert result["applied"] == 1 and result["duplicate"] == 1
    assert result["by_platform"]["boss"]["applied"] == 1
    assert result["by_platform"]["boss"]["duplicate"] == 0
    with session_scope() as s:
        assert s.get(JobRow, blocked_id).status == JobStatus.MATCHED
        assert s.get(JobRow, b1).status in (JobStatus.APPLIED, JobStatus.MATCHED)
        assert s.get(JobRow, z1).status == JobStatus.APPLIED
        assert s.execute(select(func.count()).select_from(AppRow)).scalar() == 2
        assert s.execute(select(func.count()).select_from(DailyStat)).scalar() == 1


@test("投递验证：触发验证后跳过平台并记录状态")
async def test_apply_verify_guard() -> None:
    reset_db()
    insert_job(platform="liepin", job_id="v1", status=JobStatus.MATCHED)
    fake = {"liepin": FakePlatform("liepin", result=ApplyResult(success=False, need_verify=True, message="安全验证"))}
    with mock.patch.object(apply_api, "get_platform", lambda name: fake[name]), \
         mock.patch.object(apply_api, "load_resume", lambda: {"skills": []}), \
         mock.patch.object(task_control, "cancellable_sleep", lambda *a, **k: asyncio.sleep(0)):
        result = await apply_api._apply_impl(apply_api.ApplyIn(platforms=["liepin"], delay_min=1, delay_max=1))
    assert result["need_verify"] is True
    assert edge_login.get_login_status("liepin")["status"] == "verify_required"


@test("全流程编排：阶段顺序、锁释放与阶段间停止")
async def test_pipeline_worker() -> None:
    reset_db()
    calls: list[str] = []
    async def fake_collect(_item):
        calls.append("collect")
        return {"ok": True}
    def fake_match(_item):
        calls.append("match")
        task_control.request()
        return {"ok": True}
    async def fake_apply(_item):
        calls.append("apply")
        return {"ok": True}
    item = pipeline_api.PipelineIn(
        stages=["collect", "match", "apply"],
        collect=collect_api.CollectIn(keywords=["嵌入式"], cities=["深圳"]),
        match=match_api.MatchIn(),
        apply=apply_api.ApplyIn(),
    )
    with mock.patch.object(pipeline_api, "_collect_impl", fake_collect), \
         mock.patch.object(pipeline_api, "_match_body", fake_match), \
         mock.patch.object(pipeline_api, "_apply_impl", fake_apply):
        response = await pipeline_api.run_pipeline(item)
        assert response["started"] is True
        await asyncio.sleep(0.05)
        assert calls == ["collect", "match"]
        assert collect_lock.locked() is False and apply_lock.locked() is False and match_lock.locked() is False
    task_control.clear()


@test("平台注册与适配器契约")
def test_platform_registry() -> None:
    assert registry.all_names() == ["boss", "job51", "liepin", "zhilian"]
    for name in registry.all_names():
        adapter = registry.get(name)
        assert isinstance(adapter, BasePlatform)
        assert inspect.iscoroutinefunction(adapter.check_login)
        assert inspect.iscoroutinefunction(adapter.search_jobs)
        assert inspect.iscoroutinefunction(adapter.apply)


@test("Edge 登录状态：运行期验证/失效证据不会被旧 cookie 翻绿")
async def test_edge_login_guard() -> None:
    reset_db()
    cookies = [{"name": "wt2", "value": "x", "domain": ".zhipin.com", "expires": time.time() + 3600}]
    async def fake_read_cookies(*_args, **_kwargs):
        return cookies

    with mock.patch.object(edge_login.browser, "is_running", lambda _key: True), \
         mock.patch.object(edge_login.browser, "read_cookies", fake_read_cookies):
        edge_login.set_login_status("boss", "verify_required", "采集触发验证", source="run")
        await edge_login.probe_login_states()
        assert edge_login.get_login_status("boss")["status"] == "verify_required"
        edge_login.set_login_status("boss", "not_logged", "登录失效", source="run")
        await edge_login.probe_login_states()
        assert edge_login.get_login_status("boss")["status"] == "not_logged"
        with session_scope() as s:
            row = s.get(AppConfig, "login_boss")
            if row:
                s.delete(row)
        edge_login._status_cache.pop("boss", None)
        await edge_login.probe_login_states()
        assert edge_login.get_login_status("boss")["status"] == "logged_in"


@test("API 路由契约：核心端点、静态页面与健康检查")
def test_api_surface() -> None:
    from backend.main import app

    routes: set[tuple[str, str]] = set()

    def collect(route) -> None:
        path = getattr(route, "path", "")
        methods = getattr(route, "methods", set()) or set()
        if path.startswith("/api") and methods:
            routes.update((path, method) for method in methods if method != "HEAD")
            return
        original = getattr(route, "original_router", None)
        if original is not None:
            for sub in getattr(original, "routes", []):
                collect(sub)
            return
        for sub in getattr(route, "routes", []) or []:
            collect(sub)

    for route in app.routes:
        collect(route)
    required = {
        ("/api/health", "GET"), ("/api/platforms", "GET"),
        ("/api/platforms/{name}/login", "POST"), ("/api/collect/run", "POST"),
        ("/api/match/run", "POST"), ("/api/apply/run", "POST"),
        ("/api/pipeline/run", "POST"), ("/api/task/status", "GET"),
        ("/api/task/stop", "POST"), ("/api/userconfig", "GET"),
        ("/api/userconfig", "POST"),
    }
    assert required <= routes, required - routes


async def run_async(fn) -> None:
    await fn()


def main() -> int:
    passed = 0
    failed: list[tuple[str, str]] = []
    print(f"regression suite: {len(TESTS)} checks")
    for name, fn in TESTS:
        try:
            if inspect.iscoroutinefunction(fn):
                asyncio.run(run_async(fn))
            else:
                fn()
            passed += 1
            print(f"  [PASS] {name}")
        except Exception as exc:  # noqa: BLE001
            failed.append((name, traceback.format_exc()))
            print(f"  [FAIL] {name}: {type(exc).__name__}: {exc}")
    if failed:
        print("\nfailures:")
        for name, detail in failed:
            print(f"\n--- {name} ---\n{detail}")
    print(f"\nsummary: pass={passed} fail={len(failed)}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
