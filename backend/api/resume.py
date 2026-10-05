"""简历上传与解析 API。

- 上传的文件保存在 data/resumes/uploads/ 下（原始文件，不出本机）
- 解析结果写入 data/resumes/profile.json（结构化档案 + 全文）
- 图片版 PDF（扫描件）自动走 OCR，首次识别约 10~40 秒
"""
from __future__ import annotations

import asyncio
import json
import re
import time

from fastapi import APIRouter, File, HTTPException, UploadFile

from backend.core.paths import RESUMES_DIR
from backend.services.resume_parser import extract_text
from backend.services.resume_profile import parse_text

router = APIRouter(tags=["resume"])

UPLOAD_DIR = RESUMES_DIR / "uploads"
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
PROFILE_PATH = RESUMES_DIR / "profile.json"
RAW_TEXT_PATH = RESUMES_DIR / "raw_text.txt"

MAX_BYTES = 10 * 1024 * 1024
_BAD_CHARS = re.compile(r'[\\/:*?"<>|\r\n\t]+')
_lock = asyncio.Lock()


def _safe_name(name: str) -> str:
    """文件名去掉非法字符（保留中文）。"""
    cleaned = _BAD_CHARS.sub("-", (name or "").strip()).strip(" .-")
    return cleaned[:80] or "resume"


def _run_pipeline(path) -> tuple[dict, object]:
    """在线程池里执行的阻塞解析管线：文件 → 全文 → 结构化档案。"""
    result = extract_text(path)
    if not result.text.strip():
        raise ValueError("未能从文件中识别出任何文字，请确认文件清晰度或改用文本版 PDF / Word")
    profile = parse_text(result.text)
    return profile, result


def _tidy_uploads(keep: int = 5) -> None:
    """只保留最近 keep 份上传副本（仅管理 uploads 目录内的 pdf/docx 文件）。"""
    try:
        base = UPLOAD_DIR.resolve()
        files = [
            f for f in UPLOAD_DIR.iterdir()
            if f.is_file() and f.suffix.lower() in (".pdf", ".docx")
        ]
    except OSError:
        return
    files.sort(key=lambda f: f.stat().st_mtime, reverse=True)
    for stale in files[keep:]:
        try:
            if stale.parent.resolve() == base:
                stale.unlink()
        except OSError:
            pass


@router.post("/api/resume/upload")
async def upload_resume(file: UploadFile = File(...)) -> dict:
    """上传并解析简历（PDF / DOCX；图片版 PDF 自动 OCR）。"""
    name = _safe_name(file.filename or "resume.pdf")
    suffix = name.lower().rsplit(".", 1)[-1] if "." in name else ""
    if suffix not in ("pdf", "docx"):
        raise HTTPException(status_code=400, detail="仅支持 .pdf / .docx 文件")

    data = await file.read()
    if len(data) > MAX_BYTES:
        raise HTTPException(status_code=400, detail="文件不能超过 10MB")
    if not data:
        raise HTTPException(status_code=400, detail="文件为空")

    head = bytes(data[:8])
    if suffix == "pdf" and not head.startswith(b"%PDF"):
        raise HTTPException(status_code=400, detail="文件头校验失败：不是有效的 PDF 文件")
    if suffix == "docx" and not head.startswith(b"PK"):
        raise HTTPException(status_code=400, detail="文件头校验失败：不是有效的 DOCX 文件")

    if _lock.locked():
        raise HTTPException(status_code=409, detail="已有简历正在解析，请稍候")
    async with _lock:
        path = UPLOAD_DIR / name
        path.write_bytes(data)
        _tidy_uploads()

        try:
            profile, meta = await asyncio.to_thread(_run_pipeline, path)
        except ValueError as e:
            raise HTTPException(status_code=422, detail=str(e)) from e
        except Exception as e:  # OCR 引擎异常等
            raise HTTPException(status_code=500, detail=f"解析失败：{e}") from e

        payload = {
            "app": "全自动投递简历系统",
            "filename": name,
            "file": f"uploads/{name}",
            "uploaded_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "engine": meta.engine,
            "pages": meta.pages,
            "ocr_seconds": meta.ocr_seconds,
            "text_chars": len(meta.text),
            "warnings": meta.warnings,
            "parsed": profile,
        }
        PROFILE_PATH.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        RAW_TEXT_PATH.write_text(meta.text, encoding="utf-8")

    return {
        "ok": True,
        "filename": name,
        "engine": meta.engine,
        "pages": meta.pages,
        "ocr_seconds": meta.ocr_seconds,
        "text_chars": len(meta.text),
        "warnings": meta.warnings,
        "uploaded_at": payload["uploaded_at"],
        "parsed": profile,
    }


@router.get("/api/resume/current")
def current_resume() -> dict:
    """当前生效的简历档案（未上传过则 resume=null）。"""
    if not PROFILE_PATH.exists():
        return {"ok": True, "resume": None}
    try:
        payload = json.loads(PROFILE_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"ok": True, "resume": None}
    return {"ok": True, "resume": payload}
