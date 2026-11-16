"""M3 新增的可选配置节：[gate]、[routing]、[approval]、[limits] candidates_pending。

InstanceConfig 不动，这里按需再读一次 config.ini；全部有缺省值，旧配置不用改。
"""

from __future__ import annotations

import configparser
from dataclasses import dataclass, field

from hushai_agent.config import ConfigError, InstanceConfig, parse_fallback_chain
from hushai_agent.inboxfmt import is_valid_door
from hushai_agent.paths import get_instance_config_file

DEFAULT_EXCHANGE_EXT = ("md", "txt", "json", "csv", "html", "pdf", "png", "jpg")
DEFAULT_EXCHANGE_MAX_KB = 5120
DEFAULT_ROUTING_MAX_DAILY = 10
DEFAULT_CANDIDATES_PENDING = 20
DEFAULT_SYNC_TOOL = "rclone"
DEFAULT_SYNC_TIMEOUT = 60
DEFAULT_SYNC_SERVER_DOORS = ("web", "muse", "heal")
ROUTING_RESERVED = frozenset({"enabled", "trusted_doors", "max_daily"})


@dataclass(frozen=True)
class ExtConfig:
    """M3 可选配置。"""

    exchange_ext: tuple[str, ...] = DEFAULT_EXCHANGE_EXT
    exchange_max_kb: int = DEFAULT_EXCHANGE_MAX_KB
    routing_enabled: bool = False
    trusted_doors: tuple[str, ...] = ()
    routing_max_daily: int = DEFAULT_ROUTING_MAX_DAILY
    routing_chains: dict[str, list[tuple[str, str]]] = field(default_factory=dict)
    approval_doors: tuple[str, ...] = ()
    candidates_pending: int = DEFAULT_CANDIDATES_PENDING
    sync_tool: str = DEFAULT_SYNC_TOOL
    sync_remote: str = ""
    sync_timeout: int = DEFAULT_SYNC_TIMEOUT
    sync_pull_doors: tuple[str, ...] = ()
    sync_server_doors: tuple[str, ...] = DEFAULT_SYNC_SERVER_DOORS
    capture_default_domain: str = ""
    capture_system_markers: tuple[str, ...] = ()


def _csv(raw: str) -> tuple[str, ...]:
    return tuple(p.strip() for p in raw.split(",") if p.strip())


def _int(parser: configparser.ConfigParser, sec: str, key: str, default: int, allow_zero: bool = True) -> int:
    raw = parser.get(sec, key, fallback="").strip()
    if not raw:
        return default
    try:
        val = int(raw)
    except ValueError as exc:
        raise ConfigError(f"配置中 {sec}.{key} 必须为整数: {raw}") from exc
    if allow_zero and val < 0:
        raise ConfigError(f"配置中 {sec}.{key} 不能小于 0: {val}")
    if not allow_zero and val <= 0:
        raise ConfigError(f"配置中 {sec}.{key} 必须大于 0: {val}")
    return val


def load_ext(cfg: InstanceConfig) -> ExtConfig:
    """读取 M3 可选配置节；节或键缺失时用缺省值。"""
    parser = configparser.ConfigParser(interpolation=None)
    try:
        parser.read_string(get_instance_config_file(cfg.name).read_text(encoding="utf-8"))
    except (OSError, configparser.Error, UnicodeDecodeError) as exc:
        raise ConfigError(f"读取配置文件失败: {exc}") from exc

    ext_raw = parser.get("gate", "exchange_ext", fallback="").strip()
    exchange_ext = tuple(e.lower().lstrip(".") for e in _csv(ext_raw)) or DEFAULT_EXCHANGE_EXT

    chains: dict[str, list[tuple[str, str]]] = {}
    if parser.has_section("routing"):
        for key, raw in parser.items("routing"):
            if key in ROUTING_RESERVED:
                continue
            if not is_valid_door(key):
                raise ConfigError(f"[routing] 目标名非法: {key}")
            chain = parse_fallback_chain(raw)
            if chain:
                chains[key] = chain
    enabled_raw = parser.get("routing", "enabled", fallback="false").strip().lower()

    approval = _csv(parser.get("approval", "doors", fallback=""))
    if not approval:
        approval = tuple(dict.fromkeys((cfg.steward_door, "web")))

    sync_tool = parser.get("sync", "tool", fallback="").strip() or DEFAULT_SYNC_TOOL
    sync_remote = parser.get("sync", "remote", fallback="").strip()
    sync_timeout = _int(parser, "sync", "timeout", DEFAULT_SYNC_TIMEOUT, allow_zero=False)
    sync_pull_doors = _csv(parser.get("sync", "pull_doors", fallback=""))
    srv_raw = parser.get("sync", "server_doors", fallback="").strip()
    sync_server_doors = _csv(srv_raw) if srv_raw else DEFAULT_SYNC_SERVER_DOORS
    capture_default_domain = parser.get("capture", "default_domain", fallback="").strip()

    return ExtConfig(
        exchange_ext=exchange_ext,
        exchange_max_kb=_int(parser, "gate", "exchange_max_kb", DEFAULT_EXCHANGE_MAX_KB, allow_zero=False),
        routing_enabled=enabled_raw in ("true", "1", "yes", "on"),
        trusted_doors=_csv(parser.get("routing", "trusted_doors", fallback="")),
        routing_max_daily=_int(parser, "routing", "max_daily", DEFAULT_ROUTING_MAX_DAILY),
        routing_chains=chains,
        approval_doors=approval,
        candidates_pending=_int(parser, "limits", "candidates_pending", DEFAULT_CANDIDATES_PENDING, allow_zero=False),
        sync_tool=sync_tool,
        sync_remote=sync_remote,
        sync_timeout=sync_timeout,
        sync_pull_doors=sync_pull_doors,
        sync_server_doors=sync_server_doors,
        capture_default_domain=capture_default_domain,
        capture_system_markers=_csv(parser.get("capture", "system_markers", fallback="")),
    )
