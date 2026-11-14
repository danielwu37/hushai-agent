"""实例配置文件（config.ini）的读写、生成与校验。"""

from __future__ import annotations

import configparser
from dataclasses import dataclass, field
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
from hushai_agent.fsio import atomic_write
from hushai_agent.inboxfmt import ALL_RESERVED_DOMAINS, is_valid_door
from hushai_agent.paths import (
    get_instance_config_dir,
    get_instance_config_file,
    is_valid_instance_name,
)

DEFAULT_TIMEZONE = "Asia/Shanghai"
DEFAULT_LANG = "zh"
DEFAULT_STEWARD_DOOR = "steward"
DEFAULT_KNOWN_DOORS = ("steward", "laptop", "web")
DEFAULT_DOMAINS_ZH = ("工作", "家庭", "生活", "学习", "健康")
DEFAULT_DOMAINS_EN = ("work", "family", "life", "study", "health")
DEFAULT_DOMAINS = DEFAULT_DOMAINS_ZH
DEFAULT_AGENTS_LINES = 200
DEFAULT_PROFILE_LINES = 120
DEFAULT_STATUS_INDEX_LINES = 80
DEFAULT_TODAY_ITEMS = 7
DEFAULT_LOCAL_ONLY = "domains/health"

__all__ = [
    "ConfigError",
    "DEFAULT_AGENTS_LINES",
    "DEFAULT_DOMAINS",
    "DEFAULT_DOMAINS_EN",
    "DEFAULT_DOMAINS_ZH",
    "DEFAULT_KNOWN_DOORS",
    "DEFAULT_LANG",
    "DEFAULT_LOCAL_ONLY",
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
    "load_instance_config",
    "parse_chain",
    "parse_fallback_chain",
    "parse_job_chain",
    "render_default_config",
    "validate_timezone",
    "write_instance_config",
]


@dataclass(frozen=True)
class InstanceConfig:
    """解析后的实例配置对象。"""

    name: str
    vault: Path
    timezone: str
    lang: str
    steward_door: str
    known_doors: tuple[str, ...]
    domains: tuple[str, ...]
    agents_lines: int
    profile_lines: int
    status_index_lines: int
    today_items: int
    local_only: tuple[str, ...]
    adapters: dict[str, dict[str, str]] = field(default_factory=dict)
    digest: list[tuple[str, str]] = field(default_factory=list)
    weekly: list[tuple[str, str]] = field(default_factory=list)
    window_days: int = DEFAULT_WINDOW_DAYS
    stale_days: int = DEFAULT_STALE_DAYS
    status_archive_days: int = DEFAULT_STATUS_ARCHIVE_DAYS
    max_daily_calls: int = DEFAULT_MAX_DAILY_CALLS
    jobs: dict[str, list[tuple[str, str]]] = field(default_factory=dict)
    dispatch_adapter: str = ""

    @property
    def allowed_domains(self) -> frozenset[str]:
        """返回该实例允许的所有领域（配置领域 + 保留领域）。"""
        return frozenset((*self.domains, *ALL_RESERVED_DOMAINS))


def validate_timezone(tz_name: str) -> None:
    """使用 zoneinfo 校验时区名称是否合法。"""
    if not tz_name or not tz_name.strip():
        raise ConfigError("时区不能为空")
    try:
        ZoneInfo(tz_name.strip())
    except (ZoneInfoNotFoundError, KeyError, ValueError) as exc:
        raise ConfigError(f"非法时区: {tz_name}") from exc


def render_default_config(
    name: str,
    vault_path: Path,
    timezone: str = DEFAULT_TIMEZONE,
    lang: str = DEFAULT_LANG,
    local_only: str = DEFAULT_LOCAL_ONLY,
) -> str:
    """生成标准 config.ini 文本内容。"""
    known_str = ",".join(DEFAULT_KNOWN_DOORS)
    domains = DEFAULT_DOMAINS_EN if lang == "en" else DEFAULT_DOMAINS_ZH
    domains_str = ",".join(domains)
    lines = [
        "[instance]",
        f"name = {name}",
        f"vault = {vault_path}",
        f"timezone = {timezone}",
        f"lang = {lang}",
        "",
        "[doors]",
        f"steward = {DEFAULT_STEWARD_DOOR}",
        f"known = {known_str}",
        "",
        "[domains]",
        f"names = {domains_str}",
        "",
        "[limits]",
        f"agents_lines = {DEFAULT_AGENTS_LINES}",
        f"profile_lines = {DEFAULT_PROFILE_LINES}",
        f"status_index_lines = {DEFAULT_STATUS_INDEX_LINES}",
        f"today_items = {DEFAULT_TODAY_ITEMS}",
        "",
        "[privacy]",
        f"local_only = {local_only}",
        "",
        "[adapters]",
        "# <名字>.type = fake | claude | agy（省略时 type 等于名字）",
        "# fake 另需 <名字>.fixtures = 样例目录；<名字>.light / <名字>.heavy = 模型名",
        "# claude / agy 可选 <名字>.command（默认等于类型名）、<名字>.timeout（秒，默认 180）",
        "",
        "[jobs]",
        "digest =",
        "weekly =",
        "dispatch =",
        f"window_days = {DEFAULT_WINDOW_DAYS}",
        f"stale_days = {DEFAULT_STALE_DAYS}",
        f"status_archive_days = {DEFAULT_STATUS_ARCHIVE_DAYS}",
        "",
        "[budget]",
        f"max_daily_calls = {DEFAULT_MAX_DAILY_CALLS}",
        "",
        "[routing]",
        "enabled = false",
        "trusted_doors =",
        "max_daily = 10",
        "",
    ]
    return "\n".join(lines)


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
