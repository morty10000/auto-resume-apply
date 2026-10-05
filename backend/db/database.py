"""数据库引擎与会话管理（SQLite + WAL）。

- 数据库文件：data/app.db（路径统一由 backend.core.paths 推导）
- WAL 模式：读写并发，调度器与 Web 请求可同时访问
- get_db()：FastAPI 依赖注入用
- session_scope()：脚本 / 调度器用的事务上下文
"""
from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator

from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session, sessionmaker

from ..core.paths import DATA_DIR, DB_PATH   # noqa: F401  DB_PATH 由此 re-export
from .models import Base

engine = create_engine(
    f"sqlite:///{DB_PATH.as_posix()}",
    connect_args={"check_same_thread": False},
)


@event.listens_for(engine, "connect")
def _configure_sqlite(dbapi_conn, _record) -> None:
    """每个新连接生效的 SQLite 参数。"""
    cur = dbapi_conn.cursor()
    cur.execute("PRAGMA journal_mode=WAL;")
    cur.execute("PRAGMA foreign_keys=ON;")
    cur.execute("PRAGMA busy_timeout=5000;")
    cur.execute("PRAGMA synchronous=NORMAL;")
    cur.close()


SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def init_db() -> None:
    """建表（已存在则跳过）+ 轻量迁移。应用启动时调用。"""
    Base.metadata.create_all(engine)
    _migrate()


def _migrate() -> None:
    """对旧库做幂等的字段补充（SQLite 无 ALTER 库迁移工具，手工检查）。"""
    with engine.begin() as conn:
        cols = {row[1] for row in conn.exec_driver_sql("PRAGMA table_info(jobs)")}
        if "matched_at" not in cols:
            conn.exec_driver_sql("ALTER TABLE jobs ADD COLUMN matched_at TEXT")


def get_db() -> Iterator[Session]:
    """FastAPI 依赖：每个请求一个会话，请求结束自动关闭。"""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


@contextmanager
def session_scope() -> Iterator[Session]:
    """事务上下文：正常提交，异常回滚。"""
    db = SessionLocal()
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()
