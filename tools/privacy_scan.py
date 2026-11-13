"""私人信息与敏感特征正则扫描工具。

用法：
    python tools/privacy_scan.py --patterns FILE [PATH]
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path
from typing import Iterable, Pattern, Sequence

SKIP_DIRS = frozenset(
    {
        ".git",
        "__pycache__",
        ".pytest_cache",
        ".mypy_cache",
        ".venv",
        "venv",
        "build",
        "dist",
    }
)


def load_patterns(patterns_file: str | Path) -> list[Pattern[str]]:
    """从规则文件中加载正则表达式列表（忽略空行和 # 开头的注释行）。"""
    pfile = Path(patterns_file)
    if not pfile.is_file():
        raise FileNotFoundError(f"规则文件不存在: {pfile}")

    compiled: list[Pattern[str]] = []
    lines = pfile.read_text(encoding="utf-8").splitlines()
    for lineno, raw in enumerate(lines, start=1):
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            continue
        try:
            compiled.append(re.compile(stripped))
        except re.error as exc:
            raise ValueError(f"规则文件 {pfile}:{lineno} 正则语法错误: {exc}") from exc
    return compiled


def _is_binary_bytes(data: bytes) -> bool:
    """判断字节内容是否为二进制文件。"""
    return b"\x00" in data


def _iter_text_files(
    target_path: Path,
    exclude_realpaths: set[Path],
) -> Iterable[Path]:
    """遍历目标路径下的所有候选文本文件（跳过 .git 等目录）。"""
    if target_path.is_file():
        real = Path(os.path.realpath(os.fspath(target_path)))
        if real not in exclude_realpaths:
            yield target_path
        return

    for root, dirs, files in os.walk(os.fspath(target_path)):
        dirs[:] = [
            d
            for d in sorted(dirs)
            if d not in SKIP_DIRS and not d.endswith(".egg-info")
        ]
        root_path = Path(root)
        for fname in sorted(files):
            fpath = root_path / fname
            real = Path(os.path.realpath(os.fspath(fpath)))
            if real in exclude_realpaths:
                continue
            yield fpath


def scan_path(
    target_path: str | Path,
    patterns: Sequence[Pattern[str]],
    exclude_files: Iterable[str | Path] = (),
) -> list[tuple[str, int, str]]:
    """扫描路径下的所有文本文件，返回命中记录列表 [(文件路径, 行号, 匹配内容), ...]。"""
    tpath = Path(target_path)
    if not tpath.exists():
        raise FileNotFoundError(f"扫描目标路径不存在: {tpath}")

    exclude_realpaths = {
        Path(os.path.realpath(os.fspath(Path(p)))) for p in exclude_files if p
    }

    hits: list[tuple[str, int, str]] = []
    if not patterns:
        return hits

    for fpath in _iter_text_files(tpath, exclude_realpaths):
        try:
            raw_bytes = fpath.read_bytes()
        except OSError:
            continue

        if _is_binary_bytes(raw_bytes):
            continue

        try:
            text = raw_bytes.decode("utf-8")
        except UnicodeDecodeError:
            continue

        if tpath.is_dir():
            display_path = fpath.relative_to(tpath).as_posix()
        else:
            display_path = fpath.as_posix()

        for lineno, line in enumerate(text.splitlines(), start=1):
            for pat in patterns:
                match = pat.search(line)
                if match:
                    hits.append((display_path, lineno, match.group(0)))
                    break

    return hits


def build_parser() -> argparse.ArgumentParser:
    """构建命令行参数解析器。"""
    parser = argparse.ArgumentParser(
        prog="privacy_scan",
        description="扫描目录或文件中的私人信息特征正则",
    )
    parser.add_argument(
        "--patterns",
        required=True,
        help="正则规则文件路径（每行一个正则，# 开头为注释）",
    )
    parser.add_argument(
        "path",
        nargs="?",
        default=".",
        metavar="PATH",
        help="要扫描的目标路径（默认当前目录）",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """命令行入口：命中打印 `文件:行号:匹配内容` 并返回 1，无命中返回 0。"""
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        patterns = load_patterns(args.patterns)
        hits = scan_path(
            target_path=args.path,
            patterns=patterns,
            exclude_files=[args.patterns],
        )
    except (FileNotFoundError, ValueError, OSError) as exc:
        print(f"错误: {exc}", file=sys.stderr)
        return 1

    if hits:
        for file_str, lineno, matched in hits:
            print(f"{file_str}:{lineno}:{matched}")
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
