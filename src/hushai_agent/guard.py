"""写入路径护栏：确保所有写入操作严格限制在允许的根目录内。"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Iterable


class GuardError(PermissionError):
    """路径护栏异常：目标路径超出允许的根目录范围。"""


def _resolve_existing_or_parent(target: Path) -> Path:
    """将路径解析为绝对真实路径（跟随符号链接；文件不存在时解析其父目录及符号链接）。"""
    abs_target = Path(os.path.abspath(os.fspath(target)))
    # os.path.realpath 会消解 .. 并跟随沿途及尾部的符号链接（含悬空符号链接）
    real_target = Path(os.path.realpath(os.fspath(abs_target)))
    return real_target


def ensure_writable(path: str | Path, roots: Iterable[str | Path]) -> Path:
    """校验目标路径是否位于任一允许根目录（roots）之内。

    - 跟随符号链接；若目标文件不存在则同时解析其父目录真实路径。
    - 拦截 .. 路径逃逸及指向允许根外部的符号链接。
    - 校验通过返回解析后的绝对真实 Path；否则抛出 GuardError。
    """
    if path is None or str(path).strip() == "":
        raise GuardError("目标路径不能为空")

    raw_path = Path(path)
    abs_path = Path(os.path.abspath(os.fspath(raw_path)))
    real_path = _resolve_existing_or_parent(abs_path)
    real_parent = _resolve_existing_or_parent(abs_path.parent)

    resolved_roots: list[Path] = []
    for root in roots:
        if root is not None and str(root).strip() != "":
            resolved_roots.append(_resolve_existing_or_parent(Path(root)))

    if not resolved_roots:
        raise GuardError(f"未提供允许写入的根目录，拒绝写入: {path}")

    for root in resolved_roots:
        in_root = (real_path == root) or real_path.is_relative_to(root)
        parent_in_root = (real_parent == root) or real_parent.is_relative_to(root)
        if in_root and (real_path == root or parent_in_root):
            return real_path

    raise GuardError(f"路径护栏拦截：目标路径 {path} (真实路径 {real_path}) 不在允许的根目录内")
