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
from backend.api.stats import router as stats_api
from backend.api.userconfig import router as userconfig_api
from backend.api.task import router as task_api
from backend.core.paths import PROJECT_ROOT
from backend.db.database import DB_PATH, init_db
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
    await resume_pending_logins()
    yield


app = FastAPI(title="全自动投递简历系统", version="0.1.0", lifespan=lifespan)


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
app.include_router(stats_api)
app.include_router(userconfig_api)
app.include_router(task_api)


# 前端静态页面（注意：必须在所有 /api 路由注册之后挂载）
if FRONTEND_DIR.is_dir():
    app.mount("/", StaticFiles(directory=str(FRONTEND_DIR), html=True), name="frontend")
