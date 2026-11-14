"""底层安全文件读写：原子覆写、O_APPEND 整行追加与受护栏保护的提示词读取。"""

from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path
from typing import Iterable

from hushai_agent.guard import GuardError, ensure_prompt_allowed, ensure_writable


def ensure_dir(path: str | Path, roots: Iterable[str | Path]) -> Path:
    """经路径护栏检查后创建目录（含父目录）。"""
    root_list = list(roots)
    real_dir = ensure_writable(path, root_list)
    real_dir.mkdir(parents=True, exist_ok=True)
    return real_dir


def clear_dir(path: str | Path, roots: Iterable[str | Path]) -> int:
    """经路径护栏检查后清空目录里的全部内容（目录本身保留），返回删除的条目数。

    - 拒绝清空允许根目录本身，避免误把整个状态目录清掉。
    - 符号链接只删链接本身，不跟随到链接目标。
    """
    root_list = list(roots)
    real_dir = ensure_writable(path, root_list)
    for root in root_list:
        if real_dir == Path(os.path.realpath(os.fspath(root))):
            raise GuardError(f"拒绝清空允许根目录本身: {path}")
    if not real_dir.is_dir():
        return 0

    removed = 0
    for child in sorted(real_dir.iterdir()):
        if child.is_symlink() or not child.is_dir():
            child.unlink()
        else:
            ensure_writable(child, root_list)
            shutil.rmtree(child)
        removed += 1
    return removed


def atomic_write(path: str | Path, content: str, roots: Iterable[str | Path]) -> Path:
    """先写同目录临时文件再 os.replace 原子替换，写入前强制检查路径护栏。"""
    root_list = list(roots)
    target = ensure_writable(path, root_list)
    parent = ensure_writable(target.parent, root_list)
    parent.mkdir(parents=True, exist_ok=True)

    tmp_path: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=os.fspath(parent),
            prefix=f".{target.name}.",
            suffix=".tmp",
            delete=False,
        ) as tmp_file:
            tmp_path = tmp_file.name
            tmp_file.write(content)
            tmp_file.flush()
            os.fsync(tmp_file.fileno())
        os.replace(tmp_path, target)
        tmp_path = None
    finally:
        if tmp_path is not None and os.path.exists(tmp_path):
            try:
                os.unlink(tmp_path)
            except OSError:
                pass

    return target


def append_line(path: str | Path, line: str, roots: Iterable[str | Path]) -> Path:
    """使用 O_APPEND 一次性写入整行，写入前强制检查路径护栏。"""
    root_list = list(roots)
    target = ensure_writable(path, root_list)
    parent = ensure_writable(target.parent, root_list)
    parent.mkdir(parents=True, exist_ok=True)

    cleaned = line.rstrip("\r\n")
    if "\n" in cleaned or "\r" in cleaned:
        raise ValueError("append_line 单次写入内容中不能包含换行符")

    payload = (cleaned + "\n").encode("utf-8")
    fd = os.open(os.fspath(target), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
    try:
        os.write(fd, payload)
    finally:
        os.close(fd)

    return target


def append_line_with_header(
    path: str | Path,
    header: str,
    line: str,
    roots: Iterable[str | Path],
) -> Path:
    """首次创建文件时用 O_CREAT|O_EXCL 一次写入表头与首行；若文件已存在则 O_APPEND 仅追加行。"""
    root_list = list(roots)
    target = ensure_writable(path, root_list)
    parent = ensure_writable(target.parent, root_list)
    parent.mkdir(parents=True, exist_ok=True)

    clean_header = header.rstrip("\r\n")
    clean_line = line.rstrip("\r\n")
    if "\n" in clean_header or "\r" in clean_header:
        raise ValueError("append_line_with_header 表头中不能包含换行符")
    if "\n" in clean_line or "\r" in clean_line:
        raise ValueError("append_line_with_header 单次写入内容中不能包含换行符")

    try:
        fd = os.open(
            os.fspath(target),
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o644,
        )
    except FileExistsError:
        payload = (clean_line + "\n").encode("utf-8")
        fd = os.open(
            os.fspath(target),
            os.O_WRONLY | os.O_CREAT | os.O_APPEND,
            0o644,
        )
        try:
            os.write(fd, payload)
        finally:
            os.close(fd)
    else:
        payload = (clean_header + "\n" + clean_line + "\n").encode("utf-8")
        try:
            os.write(fd, payload)
        finally:
            os.close(fd)

    return target


def read_for_prompt(
    path: str | Path,
    vault_root: str | Path,
    local_only: Iterable[str | Path] = (),
) -> str:
    """经 ensure_prompt_allowed 校验后以 UTF-8 读取文件内容，供拼接模型提示词使用。"""
    real_path = ensure_prompt_allowed(path, vault_root, local_only)
    return real_path.read_text(encoding="utf-8")
