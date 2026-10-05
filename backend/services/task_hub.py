"""任务状态中心：全系统同一时间只有一个任务在跑，这里记录它的实时状态。

用途：
- 供 /api/task/status 轮询 —— 前端刷新 / 重开页面后也能恢复
  「现在正在做什么、做到哪一步、已经有什么结果」，不再出现
  「日志没反应、刷新后一点就提示有任务在运行」的困惑。
- 供采集 / 匹配 / 投递三个 API 与各平台适配器埋点：
  阶段、当前动作、统计数字、进度百分比、事件日志。

全部为内存态；服务重启即归零（与服务端任务锁同一生命周期）。
"""
from __future__ import annotations

import time

from backend.core.paths import DATA_DIR

TASK_LOG_DIR = DATA_DIR / "task_logs"   # 任务事件日志落盘目录（按天一个文件）
MAX_LOGS = 300          # 事件日志最多保留条数（滚动丢弃最旧的）
STALE_SECONDS = 1800    # 超过多久没有进度更新，视为「疑似中断」自动收敛


class TaskHub:
    """单任务状态记录器（线程/协程安全的简单实现：单事件循环内使用）。"""

    def __init__(self) -> None:
        self._seq = 0                 # 日志序号：全局单调递增，永不回绕
        self._logs: list[dict] = []
        self.state: dict | None = None

    # ------------------------------------------------------------ 生命周期

    def begin(self, kind: str, stages: list[str], label: str = "") -> None:
        """开始一个新任务（清空上一任务的日志与状态）。"""
        self._logs = []
        self.state = {
            "active": True,
            "status": "running",                      # running | done | error
            "kind": kind,                             # all | collect | match | apply
            "stages": list(stages),                   # 本次任务的阶段列表
            "label": label,                           # 来源（配置名 / 当前表单）
            "stage": None,                            # 当前阶段名
            "stage_label": None,
            "detail": "正在启动…",                     # 当前动作（人话）
            "percent": 0,
            "stats": {"collected": 0, "matched": 0, "applied": 0, "failed": 0},
            "started_at": time.time(),
            "updated_at": time.time(),
            "finished_at": None,
            "ok": None,
            "summary": "",
        }

    def end(self, ok: bool = True, summary: str = "") -> None:
        """标记任务结束（正常完成或失败）。"""
        if not self.state:
            return
        self.state.update({
            "active": False,
            "status": "done" if ok else "error",
            "finished_at": time.time(),
            "ok": ok,
            "updated_at": time.time(),
        })
        if ok:
            self.state["percent"] = 100   # 正常结束收尾：进度不会再停在 97%
        if summary:
            self.state["summary"] = summary
            self.state["detail"] = summary
            self.log("OK" if ok else "ERR", summary)

    def enter_stage(self, stage: str, stage_label: str, detail: str = "") -> None:
        """进入任务的一个阶段（登录检查 / 采集 / 匹配 / 投递）。"""
        if not self.state:
            return
        self.state["stage"] = stage
        self.state["stage_label"] = stage_label
        if detail:
            self.state["detail"] = detail
        self.state["updated_at"] = time.time()

    # ------------------------------------------------------------ 验证等待

    def set_verify_wait(self, label: str) -> None:
        """标记「等待人工完成安全验证」（前端据此显示横幅/倒计时）。"""
        if not self.state:
            return
        self.state["verify_wait"] = {"platform": label, "since": time.time()}
        self.state["detail"] = f"【{label}】检测到安全验证 —— 请在浏览器完成验证（或点「继续运行」）"
        self.state["updated_at"] = time.time()

    def clear_verify_wait(self) -> None:
        """清除验证等待标记（验证通过 / 超时 / 人工确认后）。"""
        if self.state and "verify_wait" in self.state:
            self.state.pop("verify_wait", None)
            self.state["updated_at"] = time.time()

    def add_resume(self, info: dict) -> None:
        """登记「验证跳过、可续跑」的平台信息（前端显示续跑按钮用）。"""
        if not self.state:
            return
        self.state.setdefault("resume_pending", []).append(info)
        self.state["updated_at"] = time.time()

    # ------------------------------------------------------------ 进度更新

    def update(
        self,
        *,
        detail: str | None = None,
        percent: int | float | None = None,
        stats: dict | None = None,
        status: str | None = None,
    ) -> None:
        """更新当前动作 / 进度 / 统计（None 字段保持不变）。"""
        if not self.state:
            return
        if detail is not None:
            self.state["detail"] = detail
        if percent is not None:
            self.state["percent"] = max(0, min(100, round(float(percent))))
        if stats:
            self.state["stats"].update({k: v for k, v in stats.items() if v is not None})
        if status:
            self.state["status"] = status
        self.state["updated_at"] = time.time()

    def log(self, level: str, message: str) -> None:
        """追加一条事件日志（level: INFO / OK / WARN / ERR），并同步为当前动作。"""
        self._seq += 1
        self._logs.append({
            "seq": self._seq,
            "t": time.strftime("%H:%M:%S"),
            "level": level,
            "message": message,
        })
        if len(self._logs) > MAX_LOGS:
            self._logs = self._logs[-MAX_LOGS:]
        if self.state:
            self.state["updated_at"] = time.time()
        # 事件日志落盘（按天文件；任何失败都不影响主流程）
        try:
            TASK_LOG_DIR.mkdir(parents=True, exist_ok=True)
            f = TASK_LOG_DIR / f"{time.strftime('%Y-%m-%d')}.log"
            with f.open("a", encoding="utf-8") as fh:
                fh.write(f"[{time.strftime('%H:%M:%S')}] [{level}] {message}\n")
        except OSError:
            pass

    # ------------------------------------------------------------ 快照

    def snapshot(self) -> dict:
        """当前状态快照（供 API 返回；含日志与新鲜度信息）。"""
        now = time.time()
        if not self.state:
            return {
                "active": False,
                "status": "idle",
                "server_time": now,
                "logs": list(self._logs),   # idle 时也保留最近事件（停止请求 / 人工确认等）
            }
        s = dict(self.state)
        s["server_time"] = now
        s["elapsed"] = round((s.get("finished_at") or now) - s["started_at"], 1)
        s["fresh_seconds"] = round(now - s["updated_at"], 1)      # 最后进度更新距今
        s["logs"] = list(self._logs)
        return s

    def sweep_stale(self) -> None:
        """懒清理：长时间无进度更新的 active 任务 → 判定疑似中断并收敛。"""
        if (
            self.state
            and self.state.get("active")
            and time.time() - self.state.get("updated_at", 0) > STALE_SECONDS
        ):
            self.end(ok=False, summary=f"任务疑似中断（超过 {STALE_SECONDS // 60} 分钟无进度更新）")


# 全局单例
hub = TaskHub()
