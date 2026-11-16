"""实例配置文件（config.ini）的读写与严格校验。"""

from __future__ import annotations

import configparser
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from hushai_agent.config_loop import (
    DEFAULT_MAX_DAILY_CALLS,
    DEFAULT_STALE_DAYS,
    DEFAULT_STATUS_ARCHIVE_DAYS,
    DEFAULT_WINDOW_DAYS,
    VALID_TIERS,
    ConfigError,
    _parse_int_option,
    _parse_optional_sections,
    parse_chain,
    parse_dispatch_adapter,
    parse_fallback_chain,
    parse_job_chain,
)
from hushai_agent.config_schema import (
    DEFAULT_AGENTS_LINES,
    DEFAULT_DOMAINS,
    DEFAULT_DOMAINS_EN,
    DEFAULT_DOMAINS_ZH,
    DEFAULT_KNOWN_DOORS,
    DEFAULT_LANG,
    DEFAULT_LOCAL_ONLY,
    DEFAULT_LOCAL_ONLY_DOMAINS,
    DEFAULT_PROFILE_LINES,
    DEFAULT_STATUS_INDEX_LINES,
    DEFAULT_STEWARD_DOOR,
    DEFAULT_TIMEZONE,
    DEFAULT_TODAY_ITEMS,
    InstanceConfig,
    is_local_only_domain,
    render_default_config,
)
from hushai_agent.fsio import atomic_write
from hushai_agent.inboxfmt import is_valid_door
from hushai_agent.paths import (
    get_instance_config_dir,
    get_instance_config_file,
    is_valid_instance_name,
)

__all__ = [
    "ConfigError",
    "DEFAULT_AGENTS_LINES",
    "DEFAULT_DOMAINS",
    "DEFAULT_DOMAINS_EN",
    "DEFAULT_DOMAINS_ZH",
    "DEFAULT_KNOWN_DOORS",
    "DEFAULT_LANG",
    "DEFAULT_LOCAL_ONLY",
    "DEFAULT_LOCAL_ONLY_DOMAINS",
    "DEFAULT_MAX_DAILY_CALLS",
    "DEFAULT_PROFILE_LINES",
    "DEFAULT_STALE_DAYS",
    "DEFAULT_STATUS_ARCHIVE_DAYS",
    "DEFAULT_STATUS_INDEX_LINES",
    "DEFAULT_STEWARD_DOOR",
    "DEFAULT_TIMEZONE",
    "DEFAULT_TODAY_ITEMS",
    "DEFAULT_WINDOW_DAYS",
    "InstanceConfig",
    "VALID_TIERS",
    "is_local_only_domain",
    "load_instance_config",
    "parse_chain",
    "parse_fallback_chain",
    "parse_job_chain",
    "render_default_config",
    "validate_timezone",
    "write_instance_config",
]


def validate_timezone(tz_name: str) -> None:
    """使用 zoneinfo 校验时区名称是否合法。"""
    if not tz_name or not tz_name.strip():
        raise ConfigError("时区不能为空")
    try:
        ZoneInfo(tz_name.strip())
    except (ZoneInfoNotFoundError, KeyError, ValueError) as exc:
        raise ConfigError(f"非法时区: {tz_name}") from exc


def write_instance_config(
    instance: str,
    vault_path: Path,
    timezone: str = DEFAULT_TIMEZONE,
    lang: str = DEFAULT_LANG,
    roots: list[Path] | None = None,
) -> Path:
    """将实例配置写入 <config-root>/<instance>/config.ini。"""
    cfg_dir = get_instance_config_dir(instance)
    cfg_file = get_instance_config_file(instance)
    allowed_roots = roots if roots is not None else [cfg_dir]
    content = render_default_config(
        name=instance,
        vault_path=vault_path,
        timezone=timezone,
        lang=lang,
    )
    return atomic_write(cfg_file, content, allowed_roots)


def load_instance_config(instance: str) -> InstanceConfig:
    """读取并严格校验指定实例的 config.ini（兼容 M1 旧配置）。"""
    if not is_valid_instance_name(instance):
        raise ConfigError(f"非法实例名: {instance}")

    cfg_file = get_instance_config_file(instance)
    if not cfg_file.is_file():
        raise ConfigError(f"实例配置文件不存在: {cfg_file}")

    parser = configparser.ConfigParser(interpolation=None)
    try:
        parser.read_string(cfg_file.read_text(encoding="utf-8"))
    except (OSError, configparser.Error, UnicodeDecodeError) as exc:
        raise ConfigError(f"读取配置文件失败: {exc}") from exc

    required_sections = {
        "instance": ("name", "vault", "timezone", "lang"),
        "doors": ("steward", "known"),
        "domains": ("names",),
        "limits": ("agents_lines", "profile_lines", "status_index_lines", "today_items"),
        "privacy": ("local_only",),
    }
    for sec, keys in required_sections.items():
        if not parser.has_section(sec):
            raise ConfigError(f"配置缺少 [{sec}] 节")
        for key in keys:
            if not parser.has_option(sec, key):
                raise ConfigError(f"配置 [{sec}] 节缺少 {key} 字段")

    name = parser.get("instance", "name").strip()
    if not is_valid_instance_name(name) or name != instance:
        raise ConfigError(f"配置中 instance.name ({name}) 非法或与实例名 ({instance}) 不一致")

    vault_raw = parser.get("instance", "vault").strip()
    vault_path = Path(vault_raw) if vault_raw else Path("")
    if not vault_raw or not vault_path.is_absolute():
        raise ConfigError(f"配置中 instance.vault 必须为非空绝对路径: {vault_raw}")

    timezone = parser.get("instance", "timezone").strip()
    validate_timezone(timezone)

    lang = parser.get("instance", "lang").strip()
    if lang not in ("zh", "en"):
        raise ConfigError(f"配置中 instance.lang 必须为 zh 或 en，当前为: {lang}")

    steward_door = parser.get("doors", "steward").strip()
    if not is_valid_door(steward_door):
        raise ConfigError(f"配置中 doors.steward 编号非法: {steward_door}")

    known_doors = tuple(d.strip() for d in parser.get("doors", "known").split(",") if d.strip())
    if not known_doors or any(not is_valid_door(d) for d in known_doors):
        raise ConfigError("配置中 doors.known 不能为空或含非法端编号")
    if steward_door not in known_doors:
        raise ConfigError(f"配置中 doors.steward ({steward_door}) 未包含在 doors.known 中")

    domains = tuple(d.strip() for d in parser.get("domains", "names").split(",") if d.strip())
    if not domains:
        raise ConfigError("配置中 domains.names 不能为空")

    agents_lines = _parse_int_option(parser, "limits", "agents_lines", DEFAULT_AGENTS_LINES)
    profile_lines = _parse_int_option(parser, "limits", "profile_lines", DEFAULT_PROFILE_LINES)
    status_index_lines = _parse_int_option(
        parser, "limits", "status_index_lines", DEFAULT_STATUS_INDEX_LINES
    )
    today_items = _parse_int_option(parser, "limits", "today_items", DEFAULT_TODAY_ITEMS)
    local_only = tuple(
        p.strip() for p in parser.get("privacy", "local_only").split(",") if p.strip()
    )
    if parser.has_option("privacy", "local_only_domains"):
        local_only_domains = tuple(
            d.strip() for d in parser.get("privacy", "local_only_domains").split(",") if d.strip()
        )
    else:
        local_only_domains = DEFAULT_LOCAL_ONLY_DOMAINS

    (
        adapters,
        jobs,
        window_days,
        stale_days,
        status_archive_days,
        max_daily_calls,
    ) = _parse_optional_sections(parser)

    return InstanceConfig(
        name=name,
        vault=vault_path,
        timezone=timezone,
        lang=lang,
        steward_door=steward_door,
        known_doors=known_doors,
        domains=domains,
        agents_lines=agents_lines,
        profile_lines=profile_lines,
        status_index_lines=status_index_lines,
        today_items=today_items,
        local_only=local_only,
        local_only_domains=local_only_domains,
        adapters=adapters,
        digest=jobs.get("digest", []),
        weekly=jobs.get("weekly", []),
        window_days=window_days,
        stale_days=stale_days,
        status_archive_days=status_archive_days,
        max_daily_calls=max_daily_calls,
        jobs=jobs,
        dispatch_adapter=parse_dispatch_adapter(parser),
    )
