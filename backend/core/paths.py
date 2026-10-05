"""项目路径常量（唯一来源）。

所有路径都相对项目根目录推导，与 runtime/ 便携环境的位置无关：
    <项目根>/data                 运行时数据总目录
    <项目根>/data/app.db          SQLite 数据库
    <项目根>/data/browser_data/   每平台一个浏览器持久化目录（登录态）
    <项目根>/data/resumes/        上传的简历文件
    <项目根>/data/presets/        搜索配置文件（保存配置时写入的文本文件）
"""
from __future__ import annotations

from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]   # backend/core/paths.py → 项目根
DATA_DIR = PROJECT_ROOT / "data"
DB_PATH = DATA_DIR / "app.db"
BROWSER_DATA_DIR = DATA_DIR / "browser_data"
RESUMES_DIR = DATA_DIR / "resumes"
PRESETS_DIR = DATA_DIR / "presets"

for _d in (DATA_DIR, BROWSER_DATA_DIR, RESUMES_DIR, PRESETS_DIR):
    _d.mkdir(parents=True, exist_ok=True)
