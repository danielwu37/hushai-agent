"""Second-brain 配置（[brain] 节）与状态文件（<state>/brain-state.json）管理。

所有 routine 默认禁用（false），须由用户显式执行 enable 才开启。
"""

from __future__ import annotations

import configparser
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from hushai_agent.config import ConfigError, InstanceConfig
from hushai_agent.config_edit import update_ini_text
from hushai_agent.fsio import atomic_write
from hushai_agent.paths import get_instance_config_dir, get_instance_config_file, get_instance_state_dir

ROUTINE_NAMES = ("daily", "weekly", "resurface", "monthly")
DEFAULT_DAILY_TIME = "21:00"
DEFAULT_WEEKLY_DAY = "Sun"
DEFAULT_WEEKLY_TIME = "18:00"
DEFAULT_RESURFACE_TIME = "09:00"
DEFAULT_MIN_LINKS = 2
DEFAULT_STALE_MONTHS = 6
DEFAULT_INDEX_MAX_LINES = 200
DEFAULT_RESURFACE_MIN_AGE_DAYS = 3

DEFAULT_BRAIN_INI_BLOCK = (
    "[brain]\n"
    "enabled = false\n"
    "daily_enabled = false\n"
    "weekly_enabled = false\n"
    "resurface_enabled = false\n"
    "monthly_enabled = false\n"
    f"daily_time = {DEFAULT_DAILY_TIME}\n"
    f"weekly_day = {DEFAULT_WEEKLY_DAY}\n"
    f"weekly_time = {DEFAULT_WEEKLY_TIME}\n"
    f"resurface_time = {DEFAULT_RESURFACE_TIME}\n"
    f"min_links = {DEFAULT_MIN_LINKS}\n"
    f"stale_months = {DEFAULT_STALE_MONTHS}\n"
    f"index_max_lines = {DEFAULT_INDEX_MAX_LINES}\n"
)


@dataclass(frozen=True)
class BrainConfig:
    """解析后的 [brain] 配置。所有 routine 默认禁用。"""

    enabled: bool = False
    daily_enabled: bool = False
    weekly_enabled: bool = False
    resurface_enabled: bool = False
    monthly_enabled: bool = False
    daily_time: str = DEFAULT_DAILY_TIME
    weekly_day: str = DEFAULT_WEEKLY_DAY
    weekly_time: str = DEFAULT_WEEKLY_TIME
    resurface_time: str = DEFAULT_RESURFACE_TIME
    min_links: int = DEFAULT_MIN_LINKS
    stale_months: int = DEFAULT_STALE_MONTHS
    index_max_lines: int = DEFAULT_INDEX_MAX_LINES
    resurface_min_age_days: int = DEFAULT_RESURFACE_MIN_AGE_DAYS

    def is_routine_enabled(self, routine: str) -> bool:
        """判断指定 routine 是否已启用（支持 'daily' | 'weekly' | 'resurface' | 'monthly'）。"""
        mapping = {
            "daily": self.daily_enabled,
            "weekly": self.weekly_enabled,
            "resurface": self.resurface_enabled,
            "monthly": self.monthly_enabled,
        }
        if routine not in mapping:
            raise ValueError(f"未知 routine: {routine}")
        return bool(mapping[routine])


def _parse_bool(raw: str, default: bool = False) -> bool:
    val = (raw or "").strip().lower()
    if not val:
        return default
    return val in ("true", "1", "yes", "on")


def _parse_pos_int(parser: configparser.ConfigParser, key: str, default: int, allow_zero: bool = False) -> int:
    raw = parser.get("brain", key, fallback="").strip()
    if not raw:
        return default
    try:
        num = int(raw)
    except ValueError as exc:
        raise ConfigError(f"配置 [brain].{key} 必须为整数: {raw}") from exc
    if (allow_zero and num < 0) or (not allow_zero and num <= 0):
        raise ConfigError(f"配置 [brain].{key} 取值不合法: {num}")
    return num


def load_brain_config(cfg: InstanceConfig) -> BrainConfig:
    """从实例 config.ini 读取 [brain] 配置；缺失时全部默认为禁用。"""
    cfg_file = get_instance_config_file(cfg.name)
    parser = configparser.ConfigParser(interpolation=None)
    try:
        parser.read_string(cfg_file.read_text(encoding="utf-8"))
    except (OSError, configparser.Error, UnicodeDecodeError) as exc:
        raise ConfigError(f"读取配置文件失败: {exc}") from exc

    if not parser.has_section("brain"):
        return BrainConfig()

    return BrainConfig(
        enabled=_parse_bool(parser.get("brain", "enabled", fallback="false")),
        daily_enabled=_parse_bool(parser.get("brain", "daily_enabled", fallback="false")),
        weekly_enabled=_parse_bool(parser.get("brain", "weekly_enabled", fallback="false")),
        resurface_enabled=_parse_bool(parser.get("brain", "resurface_enabled", fallback="false")),
        monthly_enabled=_parse_bool(parser.get("brain", "monthly_enabled", fallback="false")),
        daily_time=parser.get("brain", "daily_time", fallback=DEFAULT_DAILY_TIME).strip() or DEFAULT_DAILY_TIME,
        weekly_day=parser.get("brain", "weekly_day", fallback=DEFAULT_WEEKLY_DAY).strip() or DEFAULT_WEEKLY_DAY,
        weekly_time=parser.get("brain", "weekly_time", fallback=DEFAULT_WEEKLY_TIME).strip() or DEFAULT_WEEKLY_TIME,
        resurface_time=parser.get("brain", "resurface_time", fallback=DEFAULT_RESURFACE_TIME).strip() or DEFAULT_RESURFACE_TIME,
        min_links=_parse_pos_int(parser, "min_links", DEFAULT_MIN_LINKS),
        stale_months=_parse_pos_int(parser, "stale_months", DEFAULT_STALE_MONTHS),
        index_max_lines=_parse_pos_int(parser, "index_max_lines", DEFAULT_INDEX_MAX_LINES),
        resurface_min_age_days=_parse_pos_int(
            parser, "resurface_min_age_days", DEFAULT_RESURFACE_MIN_AGE_DAYS, allow_zero=True
        ),
    )


def _update_ini_section(raw_text: str, section: str, updates: dict[str, str]) -> str:
    """在尽量保留原有注释和格式的前提下，原子更新 INI 指定节的键值。"""
    return update_ini_text(raw_text, {section: dict(updates)})


def set_brain_routine_enabled(cfg: InstanceConfig, target: str, enabled: bool) -> BrainConfig:
    """启用或禁用指定的 second-brain routine（daily | weekly | resurface | monthly | all）。"""
    if target != "all" and target not in ROUTINE_NAMES:
        raise ValueError(f"不支持的 routine 名称: {target}（可选 daily|weekly|resurface|monthly|all）")

    cfg_file = get_instance_config_file(cfg.name)
    cfg_dir = get_instance_config_dir(cfg.name)
    raw_text = cfg_file.read_text(encoding="utf-8")
    current = load_brain_config(cfg)

    targets = ROUTINE_NAMES if target == "all" else (target,)
    state_map = {
        "daily": current.daily_enabled,
        "weekly": current.weekly_enabled,
        "resurface": current.resurface_enabled,
        "monthly": current.monthly_enabled,
    }
    for name in targets:
        state_map[name] = enabled

    overall_enabled = enabled or any(state_map.values())
    updates = {
        "enabled": "true" if overall_enabled else "false",
        "daily_enabled": "true" if state_map["daily"] else "false",
        "weekly_enabled": "true" if state_map["weekly"] else "false",
        "resurface_enabled": "true" if state_map["resurface"] else "false",
        "monthly_enabled": "true" if state_map["monthly"] else "false",
    }
    new_text = _update_ini_section(raw_text, "brain", updates)
    atomic_write(cfg_file, new_text, [cfg_dir])
    return load_brain_config(cfg)


def _state_file(cfg: InstanceConfig) -> Path:
    return get_instance_state_dir(cfg.name) / "brain-state.json"


def default_brain_state() -> dict[str, Any]:
    """返回默认的 second-brain 状态结构。"""
    return {
        "onboarded": False,
        "onboarded_at": "",
        "preferences": {},
        "initial_setup_shown": False,
        "processed_inbox_ids": [],
        "processed_exchange_files": [],
        "resurface_history": [],
        "monthly_last_run": "",
    }


def load_brain_state(cfg: InstanceConfig) -> dict[str, Any]:
    """读取 <state>/brain-state.json；不存在或损坏时返回默认状态。"""
    path = _state_file(cfg)
    base = default_brain_state()
    if not path.is_file():
        return base
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return base
    if not isinstance(data, dict):
        return base
    for key, val in base.items():
        if key not in data or not isinstance(data[key], type(val)):
            data[key] = val
    return data


def save_brain_state(cfg: InstanceConfig, state: dict[str, Any]) -> Path:
    """原子保存 <state>/brain-state.json。"""
    state_dir = get_instance_state_dir(cfg.name)
    path = _state_file(cfg)
    payload = json.dumps(state, ensure_ascii=False, indent=2) + "\n"
    return atomic_write(path, payload, [state_dir])
