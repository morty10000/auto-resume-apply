"""前端 ↔ 后端双向接线审计（离线静态检查，无需启动服务）。

四张核对表：
1. app.js 里 fetch('/api/...') 的每个调用 → 后端是否存在对应路由（方法 + 路径）
2. 后端每个 /api 路由 → 前端是否有调用（未被使用 = 提示信息，不算错误）
3. index.html 每个控件 id（input/select/textarea/button）→ app.js 是否引用
4. app.js 引用的每个 #id → index.html 是否存在（防止断链）；tab 按钮 ↔ 面板 id
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

HTML = (ROOT / "frontend" / "index.html").read_text(encoding="utf-8")
JS = (ROOT / "frontend" / "app.js").read_text(encoding="utf-8")


# ---------------- 1/2. API 路由双向核对 ----------------
def backend_routes() -> dict[str, set[str]]:
    """返回 {路径模板: {方法}}。模板统一成 {param} 形式。"""
    from backend.main import app

    routes: dict[str, set[str]] = {}

    def collect(route) -> None:
        path = getattr(route, "path", None)
        methods = getattr(route, "methods", None)
        if path and methods and path.startswith("/api"):
            tmpl = re.sub(r"\{[^}]+\}", "{}", path)
            routes.setdefault(tmpl, set()).update(m.upper() for m in methods if m != "HEAD")
            return
        # 部分 FastAPI 版本：include_router 会包成 _IncludedRouter，需展开原 APIRouter
        original = getattr(route, "original_router", None)
        if original is not None:
            for sub in getattr(original, "routes", []):
                collect(sub)
            return
        for sub in getattr(route, "routes", []) or []:
            collect(sub)

    for r in app.routes:
        collect(r)
    return routes


def frontend_calls() -> list[tuple[str, str, int]]:
    """返回 [(模板, 方法, 行号)]。"""
    calls = []
    for m in re.finditer(r"fetch\(\s*([`'\"])(.+?)\1", JS):
        raw, line = m.group(2), JS[: m.start()].count("\n") + 1
        if "/api" not in raw:
            continue
        tmpl = re.sub(r"\$\{[^}]+\}", "{}", raw.split("?")[0])
        tail = JS[m.end(): m.end() + 220]
        method = "POST" if re.search(r"method:\s*['\"]POST", tail) else "GET"
        calls.append((tmpl, method, line))
    return calls


def match_route(tmpl: str, routes: dict[str, set[str]], method: str) -> bool:
    for route_tmpl, methods in routes.items():
        if route_tmpl == tmpl and method in methods:
            return True
    return False


def route_reachable(route_tmpl: str, calls: list[tuple[str, str, int]]) -> bool:
    return any(c[0] == route_tmpl for c in calls)


# ---------------- 3/4. 控件 id 双向核对 ----------------
CONTROL_TAG = re.compile(
    r"<(input|select|textarea|button)\b[^>]*\bid=\"([^\"]+)\"", re.IGNORECASE
)
ID_ANY = re.compile(r"\bid=\"([^\"]+)\"")
JS_ID_REF = re.compile(r"#([A-Za-z][\w-]*)")

# 纯展示节点（非控件）允许没有 JS 引用
ALLOW_UNREF = {
    "resumeBar", "resumeResult", "resumeBusy", "logBox", "toastBox",
}


def audit() -> int:
    problems = 0

    print("=========== 1. 前端 fetch → 后端路由 ===========")
    routes = backend_routes()
    calls = frontend_calls()
    for tmpl, method, line in sorted(set(calls)):
        ok = match_route(tmpl, routes, method)
        print(f"  [{'OK ' if ok else 'FAIL'}] {method:4} {tmpl}   (app.js:{line})")
        if not ok:
            problems += 1

    print()
    print("=========== 2. 后端路由 → 前端调用（未被使用仅提示） ===========")
    for route_tmpl in sorted(routes):
        used = route_reachable(route_tmpl, calls)
        mark = "OK " if used else "note"
        print(f"  [{mark}] {'/'.join(sorted(routes[route_tmpl]))} {route_tmpl}")

    print()
    print("=========== 3. HTML 控件 id → app.js 引用 ===========")
    controls = CONTROL_TAG.findall(HTML)
    all_ids = set(ID_ANY.findall(HTML))
    js_refs = set(JS_ID_REF.findall(JS))
    missing = []
    for _tag, cid in controls:
        if cid not in js_refs and cid not in ALLOW_UNREF:
            missing.append(cid)
            problems += 1
    print(f"  控件总数: {len(controls)} ｜ 未被 JS 引用的控件: {missing or '无'}")

    print()
    print("=========== 4. app.js 引用的 id → HTML 是否存在 ===========")
    css_color = re.compile(r"^[0-9a-fA-F]{3,8}$")
    broken = []
    for ref in sorted(set(JS_ID_REF.findall(JS))):
        if css_color.match(ref) or ref in all_ids:
            continue
        if ref[0].islower() and ref.startswith(("btn", "tab", "ms", "fs", "than", "typo")):
            broken.append(ref)
    print(f"  app.js 引用但 HTML 不存在的关键 id: {broken or '无'}")

    tabs = re.findall(r'data-tab="([^"]+)"', HTML)
    panels = set(re.findall(r'id="tab-([^"]+)"', HTML))
    bad_tabs = [t for t in tabs if t not in panels]
    print(f"  tab 按钮 {len(tabs)} 个 ｜ 无对应面板的: {bad_tabs or '无'}")
    if bad_tabs:
        problems += len(bad_tabs)

    print()
    if problems:
        print(f"发现 {problems} 个问题需要处理 ✗")
    else:
        print("接线审计全部通过 ✓")
    return problems


if __name__ == "__main__":
    raise SystemExit(1 if audit() else 0)
