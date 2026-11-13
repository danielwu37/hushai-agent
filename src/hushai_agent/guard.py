"""读写路径护栏：确保写入在允许根目录内，且提示词读取不越界或触碰 local_only。"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Iterable


class GuardError(PermissionError):
    """路径护栏异常：目标路径超出允许范围或命中隐私黑名单。"""


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


def ensure_prompt_allowed(
    path: str | Path,
    vault_root: str | Path,
    local_only: Iterable[str | Path] = (),
) -> Path:
    """校验目标路径是否允许被读取并拼入模型提示词。

    - 将 path 解析为真实路径；若不在 vault_root 内则抛出 GuardError。
    - 若落在任一 local_only 路径之内（含该路径本身，或通过符号链接指向），抛出 GuardError。
    - 校验通过返回解析后的真实 Path。
    """
    if path is None or str(path).strip() == "":
        raise GuardError("目标路径不能为空")
    if vault_root is None or str(vault_root).strip() == "":
        raise GuardError("vault 根目录不能为空")

    real_vault = _resolve_existing_or_parent(Path(vault_root))

    raw_path = Path(path)
    if not raw_path.is_absolute() and not raw_path.exists() and not raw_path.is_symlink():
        abs_path = Path(os.path.abspath(os.fspath(real_vault / raw_path)))
    else:
        abs_path = Path(os.path.abspath(os.fspath(raw_path)))

    real_path = _resolve_existing_or_parent(abs_path)
    real_parent = _resolve_existing_or_parent(abs_path.parent)

    in_vault = (real_path == real_vault) or real_path.is_relative_to(real_vault)
    parent_in_vault = (real_parent == real_vault) or real_parent.is_relative_to(real_vault)
    if not (in_vault and (real_path == real_vault or parent_in_vault)):
        raise GuardError(
            f"路径护栏拦截：读取路径 {path} (真实路径 {real_path}) 不在 vault 目录内"
        )

    if isinstance(local_only, (str, Path)):
        ban_items: Iterable[str | Path] = (local_only,)
    else:
        ban_items = local_only or ()

    for item in ban_items:
        if item is None or str(item).strip() == "":
            continue
        raw_item = Path(str(item).strip())
        ban_target = raw_item if raw_item.is_absolute() else (real_vault / raw_item)
        abs_ban = Path(os.path.abspath(os.fspath(ban_target)))
        real_ban = _resolve_existing_or_parent(abs_ban)

        if (
            real_path == real_ban
            or real_path.is_relative_to(real_ban)
            or real_parent == real_ban
            or real_parent.is_relative_to(real_ban)
            or abs_path == abs_ban
            or abs_path.is_relative_to(abs_ban)
        ):
            raise GuardError(
                f"路径护栏拦截：路径 {path} 落在 local_only 隐私目录 ({item}) 内，禁止读取"
            )

    return real_path
