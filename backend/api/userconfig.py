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

# 前端、脚本或 Windows 工具链若把 UTF-8 中文按 Latin-1 再编码一次，会出现
# “Ã¥Â.../å.../ç...” 形式的乱码。这里在配置边界统一贴现，旧文件会自动恢复，
# 后续即使旧缓存再次提交乱码也不会污染 data/user_config.json。
_MOJIBAKE_MARKERS = (
    "Ã", "Â", "â", "å", "ç", "æ", "é", "ä", "¤", "¦", "§", "¨",
    "©", "ª", "«", "¬", "®", "°", "±", "²", "³", "´", "µ", "¶",
    "·", "¸", "¹", "º", "»", "¼", "½", "¾", "¿", "�",
)


def _mojibake_score(text: str) -> int:
    return sum(text.count(marker) for marker in _MOJIBAKE_MARKERS)


def _repair_mojibake_text(value: str) -> str:
    """修复一层或多层 UTF-8 -> Latin-1 误解码；正常中文保持不变。"""
    text = value
    for _ in range(3):
        if _mojibake_score(text) == 0:
            break
        try:
            candidate = text.encode("latin-1").decode("utf-8")
        except (UnicodeEncodeError, UnicodeDecodeError):
            break
        if candidate == text or _mojibake_score(candidate) >= _mojibake_score(text):
            break
        text = candidate
    return text


def _repair_mojibake(value):
    """递归修复配置中的字符串、列表和字典。"""
    if isinstance(value, str):
        return _repair_mojibake_text(value)
    if isinstance(value, list):
        return [_repair_mojibake(item) for item in value]
    if isinstance(value, dict):
        return {
            _repair_mojibake(key): _repair_mojibake(val)
            for key, val in value.items()
        }
    return value


def _write_config(config: dict) -> dict:
    """原子写入配置，所有入口共用同一套锁与重试逻辑。"""
    payload = {
        "app": "全自动投递简历系统",
        "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "config": config,
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
    return payload


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
    raw_config = data.get("config") or {}
    config = _repair_mojibake(raw_config)
    if config != raw_config:
        # 打开旧版乱码文件时顺手修复落盘；GET 失败不阻断页面读取。
        try:
            data = _write_config(config)
        except OSError:
            pass
    return {"ok": True, "config": config, "updated_at": data.get("updated_at")}


@router.post("/api/userconfig")
def save_userconfig(item: UserConfigIn) -> dict:
    """覆盖保存当前配置到文本文件。

    原子写：先写「本线程专属」临时文件再整体替换（杜绝读到写一半的内容）；
    保存全程串行化 + 失败重试——Windows 上 os.replace 偶发被文件扫描/句柄瞬时
    占用（WinError 5），唯一临时名 + 重试可彻底消除这类 500。
    """
    payload = _write_config(_repair_mojibake(item.config))
    return {"ok": True, "updated_at": payload["updated_at"]}
