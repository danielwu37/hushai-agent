"""各根目录、实例目录与内置模板路径解析。"""

from __future__ import annotations

import os
import re
from pathlib import Path

INSTANCE_NAME_RE = re.compile(r"^[a-z][a-z0-9-]{0,30}$")


def is_valid_instance_name(name: str) -> bool:
    """校验实例名是否符合规范正则 [a-z][a-z0-9-]{0,30}。"""
    return bool(name and INSTANCE_NAME_RE.match(name))


def get_config_root() -> Path:
    """获取配置根目录，支持环境变量 HUSHAI_CONFIG_ROOT 覆盖。"""
    env_val = os.environ.get("HUSHAI_CONFIG_ROOT", "").strip()
    if env_val:
        return Path(env_val).expanduser()
    return Path.home() / ".config" / "hushai"


def get_state_root() -> Path:
    """获取状态根目录，支持环境变量 HUSHAI_STATE_ROOT 覆盖。"""
    env_val = os.environ.get("HUSHAI_STATE_ROOT", "").strip()
    if env_val:
        return Path(env_val).expanduser()
    return Path.home() / ".local" / "state" / "hushai"


def get_instance_config_dir(instance: str) -> Path:
    """获取指定实例的配置目录 <config-root>/<instance>。"""
    return get_config_root() / instance


def get_instance_config_file(instance: str) -> Path:
    """获取指定实例的配置文件路径 <config-root>/<instance>/config.ini。"""
    return get_instance_config_dir(instance) / "config.ini"


def get_instance_state_dir(instance: str) -> Path:
    """获取指定实例的状态目录 <state-root>/<instance>。"""
    return get_state_root() / instance


def get_templates_vault_dir() -> Path:
    """获取包内内置的 templates/vault 模板目录。"""
    return Path(__file__).resolve().parent / "templates" / "vault"
