"""第二大脑配置（[brain] 节）与状态文件（<state>/brain-state.json）。

默认关闭；`hushai brain enable` 打开并把 since 设为当天，免得一开就把积压全量编译。
模型链用 [jobs] brain，没配时借 [jobs] digest；调用计入单独预算 max_daily_calls，不占主预算。
"""

from __future__ import annotations

import configparser
import datetime
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from hushai_agent.config import ConfigError, InstanceConfig
from hushai_agent.fsio import atomic_write
from hushai_agent.paths import get_instance_config_file, get_instance_state_dir

STATE_VERSION = 2
DEFAULT_TIME = "22:30"
DEFAULT_MAX_DAILY_CALLS = 12
DEFAULT_MAX_CALLS_PER_RUN = 6
DEFAULT_INDEX_MAX_LINES = 200
DEFAULT_STALE_DAYS = 90
TIME_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")

DEFAULT_BRAIN_INI_LINES = (
    "[brain]",
    "enabled = false",
    f"time = {DEFAULT_TIME}",
    "since =",
    f"max_daily_calls = {DEFAULT_MAX_DAILY_CALLS}",
    f"max_calls_per_run = {DEFAULT_MAX_CALLS_PER_RUN}",
    f"index_max_lines = {DEFAULT_INDEX_MAX_LINES}",
    f"stale_days = {DEFAULT_STALE_DAYS}",
)


@dataclass(frozen=True)
class BrainConfig:
    """解析后的 [brain] 配置。"""

    enabled: bool = False
    time: str = DEFAULT_TIME
    since: str = ""
    max_daily_calls: int = DEFAULT_MAX_DAILY_CALLS
    max_calls_per_run: int = DEFAULT_MAX_CALLS_PER_RUN
    index_max_lines: int = DEFAULT_INDEX_MAX_LINES
    stale_days: int = DEFAULT_STALE_DAYS


def _pos_int(parser: configparser.ConfigParser, key: str, default: int) -> int:
    raw = parser.get("brain", key, fallback="").strip()
    if not raw:
        return default
    try:
        num = int(raw)
    except ValueError as exc:
        raise ConfigError(f"配置 [brain].{key} 必须为整数: {raw}") from exc
    if num <= 0:
        raise ConfigError(f"配置 [brain].{key} 必须大于 0: {num}")
    return num


def load_brain_config(cfg: InstanceConfig) -> BrainConfig:
    """从实例 config.ini 读取 [brain]；缺节时返回默认（关闭）。旧版键（daily_enabled 等）忽略。"""
    parser = configparser.ConfigParser(interpolation=None)
    try:
        parser.read_string(get_instance_config_file(cfg.name).read_text(encoding="utf-8"))
    except (OSError, configparser.Error, UnicodeDecodeError) as exc:
        raise ConfigError(f"读取配置文件失败: {exc}") from exc
    if not parser.has_section("brain"):
        return BrainConfig()
    enabled = parser.get("brain", "enabled", fallback="false").strip().lower() in ("true", "1", "yes", "on")
    time_raw = parser.get("brain", "time", fallback="").strip() or DEFAULT_TIME
    if not TIME_RE.match(time_raw):
        raise ConfigError(f"配置 [brain].time 需为 HH:MM: {time_raw}")
    since = parser.get("brain", "since", fallback="").strip()
    if since:
        try:
            datetime.date.fromisoformat(since)
        except ValueError as exc:
            raise ConfigError(f"配置 [brain].since 需为 YYYY-MM-DD: {since}") from exc
    return BrainConfig(
        enabled=enabled,
        time=time_raw,
        since=since,
        max_daily_calls=_pos_int(parser, "max_daily_calls", DEFAULT_MAX_DAILY_CALLS),
        max_calls_per_run=_pos_int(parser, "max_calls_per_run", DEFAULT_MAX_CALLS_PER_RUN),
        index_max_lines=_pos_int(parser, "index_max_lines", DEFAULT_INDEX_MAX_LINES),
        stale_days=_pos_int(parser, "stale_days", DEFAULT_STALE_DAYS),
    )


def set_enabled(cfg: InstanceConfig, enabled: bool, since: str | None = None) -> BrainConfig:
    """打开或关闭第二大脑；打开时 since 默认为当天（只编译从今天起的新材料）。"""
    from hushai_agent import clock
    from hushai_agent.config_edit import apply_config_updates

    updates: dict[str, str | None] = {"enabled": "true" if enabled else "false"}
    if enabled:
        updates["since"] = since or clock.today(cfg).isoformat()
    _, _, new_brain = apply_config_updates(cfg.name, {"brain": updates})
    return new_brain


def _state_file(cfg: InstanceConfig) -> Path:
    return get_instance_state_dir(cfg.name) / "brain-state.json"


def default_state() -> dict[str, Any]:
    """done_lines：已编译的收件行编号；done_files：投放文件相对路径 -> sha256。"""
    return {"version": STATE_VERSION, "done_lines": [], "done_files": {}, "last_run": {}}


def load_state(cfg: InstanceConfig) -> dict[str, Any]:
    """读取状态；不存在、损坏或是旧版（v1）结构时返回空状态。"""
    base = default_state()
    try:
        data = json.loads(_state_file(cfg).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return base
    if not isinstance(data, dict) or data.get("version") != STATE_VERSION:
        return base
    for key, val in base.items():
        if not isinstance(data.get(key), type(val)):
            data[key] = val
    return data


def save_state(cfg: InstanceConfig, state: dict[str, Any]) -> Path:
    """原子保存状态文件。"""
    state_dir = get_instance_state_dir(cfg.name)
    payload = json.dumps(state, ensure_ascii=False, indent=2) + "\n"
    return atomic_write(_state_file(cfg), payload, [state_dir])
