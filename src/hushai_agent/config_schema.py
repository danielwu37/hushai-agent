"""实例配置数据模型、默认值、隐私领域过滤与默认 config.ini 渲染。"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from hushai_agent.config_loop import (
    DEFAULT_MAX_DAILY_CALLS,
    DEFAULT_STALE_DAYS,
    DEFAULT_STATUS_ARCHIVE_DAYS,
    DEFAULT_WINDOW_DAYS,
)
from hushai_agent.inboxfmt import ALL_RESERVED_DOMAINS

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
DEFAULT_LOCAL_ONLY_DOMAINS = ("health", "健康")


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
    local_only_domains: tuple[str, ...] = DEFAULT_LOCAL_ONLY_DOMAINS
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


def is_local_only_domain(domain: str, cfg: InstanceConfig) -> bool:
    """判断给定领域名是否属于 [privacy] local_only_domains（禁止发送给外部模型）。"""
    norm = (domain or "").strip().casefold()
    if not norm:
        return False
    blocked = getattr(cfg, "local_only_domains", DEFAULT_LOCAL_ONLY_DOMAINS)
    return norm in {d.strip().casefold() for d in blocked if d and d.strip()}


def render_default_config(
    name: str,
    vault_path: Path,
    timezone: str = DEFAULT_TIMEZONE,
    lang: str = DEFAULT_LANG,
    local_only: str = DEFAULT_LOCAL_ONLY,
    local_only_domains: str = ",".join(DEFAULT_LOCAL_ONLY_DOMAINS),
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
        f"local_only_domains = {local_only_domains}",
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
        "[brain]",
        "enabled = false",
        "time = 22:30",
        "since =",
        "max_daily_calls = 12",
        "max_calls_per_run = 6",
        "index_max_lines = 200",
        "stale_days = 90",
        "",
    ]
    return "\n".join(lines)
