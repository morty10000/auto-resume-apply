"""FastAPI 应用入口。

当前范围：数据层自举 + 健康检查 + 前端静态页面。
后续 Block 逐步挂载 api/ 路由与 WebSocket。
"""
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from backend.api.platforms import router as platforms_api
from backend.api.collect import router as collect_api
from backend.api.resume import router as resume_api
from backend.api.match import router as match_api
from backend.api.apply import router as apply_api
from backend.api.pipeline import router as pipeline_api
from backend.api.stats import router as stats_api
from backend.api.userconfig import router as userconfig_api
from backend.api.task import router as task_api
from backend.core.paths import PROJECT_ROOT
from backend.db.database import DB_PATH, checkpoint_wal, init_db
from backend.services.edge_login import resume_pending_logins

# 服务端日志：输出到控制台（start.bat 会重定向落盘到 data/server_console.log）
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)

FRONTEND_DIR = PROJECT_ROOT / "frontend"


@asynccontextmanager
async def lifespan(_app: FastAPI):
    init_db()
    checkpoint_wal()   # 启动时收缩 WAL（长期运行后 WAL 会长过数据库本体）
    await resume_pending_logins()
    yield


app = FastAPI(title="全自动投递简历系统", version="0.1.0", lifespan=lifespan)


@app.middleware("http")
async def _no_cache_frontend(request, call_next):
    """前端页面/脚本强制回源校验。

    StaticFiles 只给 ETag/Last-Modified，不写 Cache-Control，Chrome 会做启发式缓存：
    端口不变时刷新可能一直用旧 index.html，导致「代码更新了界面还是老样子」
    （2026-10-12 实测）。这里给非 /api 响应加 no-cache，浏览器每次校验、内容变了才取新的。
    """
    resp = await call_next(request)
    if not request.url.path.startswith("/api"):
        resp.headers["Cache-Control"] = "no-cache, must-revalidate"
    return resp


@app.get("/api/health")
def health() -> dict:
    """健康检查：服务在线 + 数据库路径。"""
    return {"status": "ok", "db_path": str(DB_PATH)}


# API 路由（必须注册在静态挂载之前）
app.include_router(platforms_api)
app.include_router(collect_api)
app.include_router(resume_api)
app.include_router(match_api)
app.include_router(apply_api)
app.include_router(pipeline_api)
app.include_router(stats_api)
app.include_router(userconfig_api)
app.include_router(task_api)


# 前端静态页面（注意：必须在所有 /api 路由注册之后挂载）
if FRONTEND_DIR.is_dir():
    app.mount("/", StaticFiles(directory=str(FRONTEND_DIR), html=True), name="frontend")
