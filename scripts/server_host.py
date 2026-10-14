"""后台服务入口：由 start.bat 通过 pythonw 启动，不显示控制台窗口。"""
from __future__ import annotations

import ctypes
import os
import sys
import time
import traceback
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from backend.core.paths import DATA_DIR  # noqa: E402

LOG_PATH = DATA_DIR / "server_console.log"


def _init_log() -> None:
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    log_file = open(LOG_PATH, "a", encoding="utf-8", buffering=1)
    sys.stdout = log_file
    sys.stderr = log_file


def _message_box(text: str, title: str = "全自动投递简历系统") -> None:
    try:
        ctypes.windll.user32.MessageBoxW(0, text, title, 0x10)
    except Exception:
        pass


def main() -> None:
    _init_log()
    print(f"\n[{time.strftime('%Y-%m-%d %H:%M:%S')}] 后台服务启动中 …", flush=True)
    try:
        port = int((os.environ.get("AUTO_RESUME_API_PORT", "8000") or "8000").strip())
    except ValueError:
        port = 8000
    port = min(max(port, 1), 65535)
    try:
        import uvicorn

        uvicorn.run(
            "backend.main:app",
            host="127.0.0.1",
            port=port,
            log_level="info",
            access_log=True,
        )
    except KeyboardInterrupt:
        print("服务已停止。", flush=True)
    except Exception:
        traceback.print_exc()
        _message_box(
            "后台服务启动失败。\n\n"
            "请查看 data\\server_console.log 中的错误信息。\n"
            "常见原因：8000 端口被占用、运行环境缺失或被安全软件拦截。"
        )
        raise SystemExit(1)


if __name__ == "__main__":
    main()
