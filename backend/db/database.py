"""数据库引擎与会话管理（SQLite + WAL）。

- 数据库文件：data/app.db（路径统一由 backend.core.paths 推导）
- WAL 模式：读写并发，调度器与 Web 请求可同时访问
- get_db()：FastAPI 依赖注入用
- session_scope()：脚本 / 调度器用的事务上下文
"""
from __future__ import annotations

import logging
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


def checkpoint_wal() -> None:
    """把 WAL 落盘并截断（启动时调用一次）。

    SQLite 的 WAL 只做「回绕」不做「收缩」，长期运行后 app.db-wal 会超过
    数据库本体（2026-10-12 实测 6.3MB vs 4.6MB）。启动时 TRUNCATE 一次即可
    把体积降回 0，同时保证关闭/重启时数据全部在主库文件里（失败不影响启动）。
    """
    try:
        with engine.begin() as conn:
            conn.exec_driver_sql("PRAGMA wal_checkpoint(TRUNCATE)")
    except Exception:  # noqa: BLE001  维护动作，失败不影响服务启动
        logging.getLogger(__name__).warning("WAL 检查点失败（已忽略）", exc_info=True)


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
