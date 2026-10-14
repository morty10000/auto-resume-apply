"""Create the portable release ZIP with standards-compliant UTF-8 names.

Windows bsdtar writes Chinese filenames without the ZIP UTF-8 flag on some
systems. The archive then looks correct on the build machine but can extract
as mojibake on another machine. Python's zipfile always marks Unicode names
with the UTF-8 flag, so use it as the single packaging path.
"""
from __future__ import annotations

import argparse
import os
import stat
import zipfile
from pathlib import Path

TOP_EXCLUDES = {".git", ".venv", "venv", "data", "dist", "build_tmp"}
PROJECT_FILE_EXCLUDES = {"项目计划书.md", "UI设计规范.md"}
SCRIPT_EXCLUDES = {"collect_sample.py", "diag_boss.py", "screenshot.py"}


def should_exclude(rel: Path) -> bool:
    parts = rel.parts
    if not parts:
        return False
    if parts[0] in TOP_EXCLUDES:
        return True
    if parts[:2] == ("runtime", "Scripts"):
        return True
    if ".local-browsers" in parts:
        return True
    if any(p == "__pycache__" for p in parts):
        return True
    name = parts[-1]
    if name.endswith((".pyc", ".pyo")):
        return True
    if parts[0] == "scripts":
        if name.startswith("_") or name.startswith("verify_block"):
            return True
        if name in SCRIPT_EXCLUDES:
            return True
    if len(parts) == 1 and name in PROJECT_FILE_EXCLUDES:
        return True
    return False


def directory_entry(arcname: str) -> zipfile.ZipInfo:
    info = zipfile.ZipInfo(arcname.rstrip("/") + "/")
    info.external_attr = (stat.S_IFDIR | 0o755) << 16
    info.compress_type = zipfile.ZIP_STORED
    return info


def build(root: Path, out: Path) -> tuple[int, int]:
    root = root.resolve()
    out.parent.mkdir(parents=True, exist_ok=True)
    out.unlink(missing_ok=True)
    folder = root.name
    count = 0
    total = 0
    seen: set[str] = set()
    with zipfile.ZipFile(out, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6, allowZip64=True) as zf:
        zf.writestr(directory_entry(folder + "/"), b"")
        for current, dirs, files in os.walk(root, followlinks=False):
            current_path = Path(current)
            rel_dir = current_path.relative_to(root)
            dirs[:] = sorted(
                d for d in dirs
                if not should_exclude(rel_dir / d if rel_dir.parts else Path(d))
            )
            for name in sorted(files):
                rel = rel_dir / name if rel_dir.parts else Path(name)
                if should_exclude(rel):
                    continue
                key = rel.as_posix().casefold()
                if key in seen:
                    continue
                seen.add(key)
                src = root / rel
                arc = f"{folder}/{rel.as_posix()}"
                zf.write(src, arc, compress_type=zipfile.ZIP_DEFLATED, compresslevel=6)
                total += src.stat().st_size
                count += 1
    return count, total


def verify(out: Path, folder: str) -> None:
    with zipfile.ZipFile(out) as zf:
        infos = zf.infolist()
        names = {info.filename for info in infos}
        required = {
            f"{folder}/start.bat",
            f"{folder}/runtime/python.exe",
            f"{folder}/scripts/package_portable.py",
            f"{folder}/scripts/server_host.py",
            f"{folder}/frontend/favicon.svg",
            f"{folder}/frontend/index.html",
            f"{folder}/frontend/app.js",
        }
        missing = sorted(required - names)
        personal = sorted(
            n for n in names
            if n.startswith(f"{folder}/data/")
            or n.endswith("user_config.json")
            or n.endswith((".db", ".db-wal", ".db-shm"))
        )
        without_utf8 = [i.filename for i in infos if not (i.flag_bits & 0x800)]
    if missing or personal or without_utf8:
        raise SystemExit(
            "zip verification failed: "
            f"missing={missing[:5]} personal={personal[:5]} non_utf8={without_utf8[:5]}"
        )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", required=True)
    parser.add_argument("--zip", dest="zip_path", required=True)
    args = parser.parse_args()
    root = Path(args.root)
    out = Path(args.zip_path)
    count, total = build(root, out)
    verify(out, root.name)
    size = Path(args.zip_path).stat().st_size
    print(f"portable zip: {count} files, source={total / 1024 / 1024:.1f} MB, zip={size / 1024 / 1024:.1f} MB, utf8=ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
