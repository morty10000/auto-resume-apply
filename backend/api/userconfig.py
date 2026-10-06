"""用户配置的文本文件持久化（data/user_config.json）。

前端表单的任何改动都会自动同步到这里；关闭 / 重启项目后，界面从该文件恢复配置。
- GET  /api/userconfig  读取当前配置（不存在返回 null）
- POST /api/userconfig  覆盖保存当前配置
"""
from __future__ import annotations

import json
import os
import threading
import time

from fastapi import APIRouter
from pydantic import BaseModel

from backend.core.paths import DATA_DIR

router = APIRouter(tags=["userconfig"])

CONFIG_PATH = DATA_DIR / "user_config.json"
_save_lock = threading.Lock()   # 保存串行化：杜绝并发写竞争（连点/自动保存重叠）


class UserConfigIn(BaseModel):
    config: dict


@router.get("/api/userconfig")
def get_userconfig() -> dict:
    """读取当前配置；文件不存在时返回 None。"""
    if not CONFIG_PATH.exists():
        return {"ok": True, "config": None, "updated_at": None}
    try:
        data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"ok": True, "config": None, "updated_at": None}
    return {"ok": True, "config": data.get("config"), "updated_at": data.get("updated_at")}


@router.post("/api/userconfig")
def save_userconfig(item: UserConfigIn) -> dict:
    """覆盖保存当前配置到文本文件。

    原子写：先写「本线程专属」临时文件再整体替换（杜绝读到写一半的内容）；
    保存全程串行化 + 失败重试——Windows 上 os.replace 偶发被文件扫描/句柄瞬时
    占用（WinError 5），唯一临时名 + 重试可彻底消除这类 500。
    """
    payload = {
        "app": "全自动投递简历系统",
        "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "config": item.config,
    }
    text = json.dumps(payload, ensure_ascii=False, indent=2)
    with _save_lock:
        last_err: Exception | None = None
        for attempt in range(5):
            tmp_path = CONFIG_PATH.with_name(
                f"{CONFIG_PATH.name}.{os.getpid()}.{threading.get_ident()}.tmp"
            )
            try:
                tmp_path.write_text(text, encoding="utf-8")
                os.replace(tmp_path, CONFIG_PATH)   # 同目录 rename：原子替换
                last_err = None
                break
            except PermissionError as e:            # 被扫描/句柄瞬时占用 → 退避重试
                last_err = e
                try:
                    tmp_path.unlink(missing_ok=True)
                except OSError:
                    pass
                time.sleep(0.15 * (attempt + 1))
        if last_err is not None:
            raise last_err
    return {"ok": True, "updated_at": payload["updated_at"]}
