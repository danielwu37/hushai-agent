"""Vault 初始化（init）与收件追加（inbox add）业务逻辑。"""

from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

from hushai_agent import clock
from hushai_agent.config import (
    DEFAULT_LANG,
    DEFAULT_TIMEZONE,
    load_instance_config,
    validate_timezone,
    write_instance_config,
)
from hushai_agent.fsio import append_line_with_header, atomic_write, ensure_dir
from hushai_agent.inboxfmt import (
    format_inbox_filename,
    format_inbox_line,
    is_valid_door,
)
from hushai_agent.paths import (
    get_instance_config_dir,
    get_instance_config_file,
    get_instance_state_dir,
    get_templates_vault_dir,
    is_valid_instance_name,
)


@dataclass(frozen=True)
class InitResult:
    """实例初始化结果。"""

    instance: str
    vault: Path
    config_file: Path
    state_dir: Path
    created_files: tuple[str, ...]
    config_created: bool
    git_initialized: bool
    git_notice: str | None = None


def _init_git_repo(
    vault_path: Path,
    created_files: Sequence[str] = (),
) -> tuple[bool, str | None]:
    """在 vault 目录中初始化 Git 仓库；若已有提交但本次补齐了缺失文件，则仅提交补齐的文件。"""
    if shutil.which("git") is None:
        return False, "未检测到 git 命令，已跳过 Git 仓库初始化"

    git_dir = vault_path / ".git"
    already_git = git_dir.exists()

    try:
        if not already_git:
            res_init = subprocess.run(
                ["git", "init"],
                cwd=os.fspath(vault_path),
                capture_output=True,
                text=True,
                check=False,
            )
            if res_init.returncode != 0:
                return False, f"git init 失败: {res_init.stderr.strip()}"

        # 检查是否已有提交
        res_head = subprocess.run(
            ["git", "rev-parse", "--verify", "HEAD"],
            cwd=os.fspath(vault_path),
            capture_output=True,
            text=True,
            check=False,
        )
        has_commit = res_head.returncode == 0

        if not has_commit:
            subprocess.run(
                ["git", "add", "-A"],
                cwd=os.fspath(vault_path),
                capture_output=True,
                text=True,
                check=False,
            )
            res_commit = subprocess.run(
                [
                    "git",
                    "-c",
                    "user.name=hushai",
                    "-c",
                    "user.email=hushai@localhost",
                    "commit",
                    "-m",
                    "Initialize hushAi-agent vault",
                ],
                cwd=os.fspath(vault_path),
                capture_output=True,
                text=True,
                check=False,
            )
            if res_commit.returncode != 0:
                return True, f"git commit 提示: {res_commit.stderr.strip()}"
        elif created_files:
            res_add = subprocess.run(
                ["git", "add", "--", *created_files],
                cwd=os.fspath(vault_path),
                capture_output=True,
                text=True,
                check=False,
            )
            if res_add.returncode != 0:
                return True, f"git add 提示: {res_add.stderr.strip()}"
            res_commit = subprocess.run(
                [
                    "git",
                    "-c",
                    "user.name=hushai",
                    "-c",
                    "user.email=hushai@localhost",
                    "commit",
                    "--allow-empty",
                    "-m",
                    "hushai init: 补齐缺失文件",
                ],
                cwd=os.fspath(vault_path),
                capture_output=True,
                text=True,
                check=False,
            )
            if res_commit.returncode != 0:
                return True, f"git commit 提示: {res_commit.stderr.strip()}"
        return True, None
    except OSError as exc:
        return False, f"调用 git 异常: {exc}"


def init_instance(
    instance: str,
    vault: str | Path,
    timezone: str = DEFAULT_TIMEZONE,
    lang: str = DEFAULT_LANG,
    no_git: bool = False,
) -> InitResult:
    """从内置模板初始化实例 vault、配置文件与状态目录（幂等）。"""
    if not is_valid_instance_name(instance):
        raise ValueError(f"非法实例名（需匹配 [a-z][a-z0-9-]{{0,30}}）: {instance}")
    validate_timezone(timezone)
    if lang not in ("zh", "en"):
        raise ValueError(f"不支持的语言选项（仅支持 zh 或 en）: {lang}")

    vault_path = Path(os.path.realpath(os.path.abspath(os.fspath(Path(vault).expanduser()))))
    config_dir = get_instance_config_dir(instance)
    config_file = get_instance_config_file(instance)
    state_dir = get_instance_state_dir(instance)
    roots = [vault_path, config_dir, state_dir]

    # 若配置文件已存在，校验其绑定的 vault 路径是否一致
    if config_file.exists():
        existing_cfg = load_instance_config(instance)
        existing_vault = Path(
            os.path.realpath(os.path.abspath(os.fspath(existing_cfg.vault.expanduser())))
        )
        if existing_vault != vault_path:
            raise ValueError(
                f"实例 '{instance}' 配置已存在，且绑定的 vault 路径不同: "
                f"已有 {existing_vault} != 请求 {vault_path}"
            )

    ensure_dir(vault_path, roots)
    ensure_dir(config_dir, roots)
    ensure_dir(state_dir, roots)

    templates_dir = get_templates_vault_dir()
    if not templates_dir.is_dir():
        raise FileNotFoundError(f"内置模板目录不存在: {templates_dir}")

    created_files: list[str] = []
    template_items = sorted(templates_dir.rglob("*"))
    for item in template_items:
        rel_path = item.relative_to(templates_dir)
        dest_path = vault_path / rel_path
        if item.is_dir():
            ensure_dir(dest_path, roots)
        elif item.is_file():
            ensure_dir(dest_path.parent, roots)
            if not dest_path.exists():
                content = item.read_text(encoding="utf-8")
                atomic_write(dest_path, content, roots)
                created_files.append(rel_path.as_posix())

    config_created = False
    if not config_file.exists():
        write_instance_config(
            instance=instance,
            vault_path=vault_path,
            timezone=timezone,
            lang=lang,
            roots=roots,
        )
        config_created = True

    git_initialized = False
    git_notice: str | None = None
    if not no_git:
        git_initialized, git_notice = _init_git_repo(vault_path, created_files)

    return InitResult(
        instance=instance,
        vault=vault_path,
        config_file=config_file,
        state_dir=state_dir,
        created_files=tuple(created_files),
        config_created=config_created,
        git_initialized=git_initialized,
        git_notice=git_notice,
    )


def add_inbox_entry(
    instance: str,
    door: str,
    domain: str,
    did: str,
    artifact: str | None = None,
    todo: str | None = None,
    time_str: str | None = None,
    is_auto: bool = False,
) -> Path:
    """向实例当天对应端的收件文件追加一条记录。"""
    cfg = load_instance_config(instance)

    if not is_valid_door(door):
        raise ValueError(f"非法端编号: {door}")
    if door not in cfg.known_doors:
        raise ValueError(f"未知的端编号 '{door}'（不在配置的 known 列表 {cfg.known_doors} 中）")

    clean_domain = domain.strip()
    if clean_domain not in cfg.allowed_domains:
        raise ValueError(f"非法领域 '{clean_domain}'（不在实例允许的领域列表中）")

    current_dt = clock.now(cfg)
    date_str = current_dt.strftime("%Y-%m-%d")
    actual_time = time_str.strip() if time_str else current_dt.strftime("%H:%M")

    formatted_line = format_inbox_line(
        time_str=actual_time,
        domain=clean_domain,
        did=did,
        artifact=artifact,
        todo=todo,
        lang=cfg.lang,
        allowed_domains=cfg.allowed_domains,
    )

    filename = format_inbox_filename(date_str=date_str, door=door, is_auto=is_auto)
    inbox_file = cfg.vault / "hub" / "inbox" / filename
    roots = [
        cfg.vault,
        get_instance_config_dir(instance),
        get_instance_state_dir(instance),
    ]

    header = f"# {date_str} {door}"
    append_line_with_header(inbox_file, header, formatted_line, roots)
    return inbox_file
