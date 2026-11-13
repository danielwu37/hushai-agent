"""实例配置文件（config.ini）的读写、生成与校验。"""

from __future__ import annotations

import configparser
from dataclasses import dataclass
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

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
DEFAULT_DOMAINS = ("工作", "家庭", "生活", "学习", "健康")
DEFAULT_AGENTS_LINES = 200
DEFAULT_PROFILE_LINES = 120
DEFAULT_STATUS_INDEX_LINES = 80
DEFAULT_TODAY_ITEMS = 7


class ConfigError(ValueError):
    """配置文件缺失、格式损坏或字段不合法。"""


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
    local_only: str = "",
) -> str:
    """生成标准 config.ini 文本内容。"""
    known_str = ",".join(DEFAULT_KNOWN_DOORS)
    domains_str = ",".join(DEFAULT_DOMAINS)
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
    """读取并严格校验指定实例的 config.ini。"""
    if not is_valid_instance_name(instance):
        raise ConfigError(f"非法实例名: {instance}")

    cfg_file = get_instance_config_file(instance)
    if not cfg_file.is_file():
        raise ConfigError(f"实例配置文件不存在: {cfg_file}")

    parser = configparser.ConfigParser(interpolation=None)
    try:
        content = cfg_file.read_text(encoding="utf-8")
        parser.read_string(content)
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
    if not is_valid_instance_name(name):
        raise ConfigError(f"配置中 instance.name 非法: {name}")
    if name != instance:
        raise ConfigError(f"配置中 instance.name ({name}) 与实例名 ({instance}) 不一致")

    vault_raw = parser.get("instance", "vault").strip()
    if not vault_raw:
        raise ConfigError("配置中 instance.vault 不能为空")
    vault_path = Path(vault_raw)
    if not vault_path.is_absolute():
        raise ConfigError(f"配置中 instance.vault 必须为绝对路径: {vault_raw}")

    timezone = parser.get("instance", "timezone").strip()
    validate_timezone(timezone)

    lang = parser.get("instance", "lang").strip()
    if lang not in ("zh", "en"):
        raise ConfigError(f"配置中 instance.lang 必须为 zh 或 en，当前为: {lang}")

    steward_door = parser.get("doors", "steward").strip()
    if not is_valid_door(steward_door):
        raise ConfigError(f"配置中 doors.steward 编号非法: {steward_door}")

    known_raw = parser.get("doors", "known").strip()
    known_doors = tuple(d.strip() for d in known_raw.split(",") if d.strip())
    if not known_doors:
        raise ConfigError("配置中 doors.known 不能为空")
    for door in known_doors:
        if not is_valid_door(door):
            raise ConfigError(f"配置中 doors.known 含非法端编号: {door}")
    if steward_door not in known_doors:
        raise ConfigError(f"配置中 doors.steward ({steward_door}) 未包含在 doors.known 中")

    domains_raw = parser.get("domains", "names").strip()
    domains = tuple(d.strip() for d in domains_raw.split(",") if d.strip())
    if not domains:
        raise ConfigError("配置中 domains.names 不能为空")

    def _get_positive_int(key: str) -> int:
        val_str = parser.get("limits", key).strip()
        try:
            val = int(val_str)
        except ValueError as exc:
            raise ConfigError(f"配置中 limits.{key} 必须为整数: {val_str}") from exc
        if val <= 0:
            raise ConfigError(f"配置中 limits.{key} 必须大于 0: {val}")
        return val

    agents_lines = _get_positive_int("agents_lines")
    profile_lines = _get_positive_int("profile_lines")
    status_index_lines = _get_positive_int("status_index_lines")
    today_items = _get_positive_int("today_items")

    local_only_raw = parser.get("privacy", "local_only").strip()
    local_only = tuple(p.strip() for p in local_only_raw.split(",") if p.strip())

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
    )
