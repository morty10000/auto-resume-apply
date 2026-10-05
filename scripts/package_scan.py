"""便携包内容扫描（打包前调用）。

确认「实际会打进包的每个文件」都不内嵌本机路径——含二进制文件内嵌路径
（如 pip 生成的启动器 exe 会写入安装时的 python.exe 绝对路径）。
按 UTF-8 与 UTF-16-LE 双编码做字节级搜索，中文路径也能可靠匹配。

跳过清单与 scripts/package.ps1 的 tar 排除规则保持一致。
发现命中：打印文件名并返回非零退出码；干净：返回 0。
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

SKIP_TOP_DIRS = {"data", "dist", "build_tmp", ".git", ".venv", "venv"}
SKIP_FILE_NAMES = {
    "collect_sample.py",
    "diag_boss.py",
    "screenshot.py",
    "项目计划书.md",
    "UI设计规范.md",
}


def should_skip(rel: str) -> bool:
    """rel 为相对项目根的 posix 风格路径。"""
    parts = rel.split("/")
    if parts[0] in SKIP_TOP_DIRS:
        return True
    name = parts[-1]
    if name in SKIP_FILE_NAMES or name.startswith("verify_block"):
        return True
    if "__pycache__" in parts or name.endswith((".pyc", ".pyo")):
        return True
    # 与 package.ps1 的 tar 排除一致：pip 生成的启动器脚本（内嵌安装时绝对路径）/ playwright 浏览器登记目录
    if len(parts) >= 2 and parts[0] == "runtime" and parts[1] == "Scripts":
        return True
    if ".local-browsers" in parts:
        return True
    if rel.startswith("scripts/_"):
        return True
    return False


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", required=True, help="项目根目录")
    ap.add_argument("--needle", action="append", default=[], help="要搜索的本机路径（可多次）")
    args = ap.parse_args()

    root = Path(args.root).resolve()
    needles = [n for n in args.needle if n]
    patterns: list[bytes] = []
    for n in needles:
        patterns.append(n.encode("utf-8"))
        patterns.append(n.encode("utf-16-le"))
    patterns = [p for p in patterns if len(p) >= 6]
    if not patterns:
        print("no needle given; skip content scan")
        return 0

    hits: list[str] = []
    scanned = 0
    for p in root.rglob("*"):
        if not p.is_file():
            continue
        rel = p.relative_to(root).as_posix()
        if should_skip(rel):
            continue
        try:
            data = p.read_bytes()
        except OSError:
            continue
        scanned += 1
        for pat in patterns:
            if pat in data:
                hits.append(rel)
                break

    print(f"content scan: {scanned} files scanned, {len(hits)} hit(s)")
    for h in hits[:50]:
        print("  x", h)
    return 1 if hits else 0


if __name__ == "__main__":
    sys.exit(main())
