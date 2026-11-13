"""M2 闭环相关的配置解析辅助函数（回退链、可选节与整数配置项）。"""

from __future__ import annotations

import configparser

DEFAULT_WINDOW_DAYS = 14
DEFAULT_STALE_DAYS = 14
DEFAULT_MAX_DAILY_CALLS = 20
VALID_TIERS = ("light", "heavy")


class ConfigError(ValueError):
    """配置文件缺失、格式损坏或字段不合法。"""


def parse_fallback_chain(raw: str) -> list[tuple[str, str]]:
    """解析任务回退链（如 'agy:light, claude:light'），返回 [(adapter_name, tier), ...]。"""
    if not raw or not raw.strip():
        return []

    chain: list[tuple[str, str]] = []
    for part in raw.split(","):
        item = part.strip()
        if not item:
            continue
        if ":" not in item:
            raise ConfigError(f"回退链项格式错误（需为 <adapter>:<tier>）: {item}")
        adapter_name, tier = (seg.strip() for seg in item.split(":", 1))
        if not adapter_name:
            raise ConfigError(f"回退链适配项名称不能为空: {item}")
        if tier not in VALID_TIERS:
            raise ConfigError(f"回退链档位非法 '{tier}'（只能是 light 或 heavy）: {item}")
        chain.append((adapter_name, tier))
    return chain


parse_chain = parse_fallback_chain
parse_job_chain = parse_fallback_chain


def _parse_int_option(
    parser: configparser.ConfigParser,
    section: str,
    key: str,
    default: int,
    allow_zero: bool = False,
) -> int:
    """解析可选整数配置项。"""
    if not parser.has_section(section) or not parser.has_option(section, key):
        return default
    val_str = parser.get(section, key).strip()
    if not val_str:
        return default
    try:
        val = int(val_str)
    except ValueError as exc:
        raise ConfigError(f"配置中 {section}.{key} 必须为整数: {val_str}") from exc
    if allow_zero and val < 0:
        raise ConfigError(f"配置中 {section}.{key} 不能小于 0: {val}")
    if not allow_zero and val <= 0:
        raise ConfigError(f"配置中 {section}.{key} 必须大于 0: {val}")
    return val


def _parse_optional_sections(
    parser: configparser.ConfigParser,
) -> tuple[
    dict[str, dict[str, str]],
    dict[str, list[tuple[str, str]]],
    int,
    int,
    int,
]:
    """解析 M2 新增可选配置节 [adapters]、[jobs]、[budget]。"""
    adapters: dict[str, dict[str, str]] = {}
    if parser.has_section("adapters"):
        for opt, raw_val in parser.items("adapters"):
            if "." not in opt:
                raise ConfigError(f"[adapters] 配置键必须形如 <名字>.<字段>: {opt}")
            adapter_name, field_name = (p.strip() for p in opt.split(".", 1))
            if not adapter_name or not field_name:
                raise ConfigError(f"[adapters] 配置键非法: {opt}")
            adapters.setdefault(adapter_name, {})[field_name] = raw_val.strip()
        for adapter_name, props in adapters.items():
            if not props.get("type"):
                props["type"] = adapter_name

    jobs: dict[str, list[tuple[str, str]]] = {"digest": [], "weekly": []}
    if parser.has_section("jobs"):
        for opt, raw_val in parser.items("jobs"):
            if opt in ("window_days", "stale_days"):
                continue
            jobs[opt.strip()] = parse_fallback_chain(raw_val)

    window_days = _parse_int_option(parser, "jobs", "window_days", DEFAULT_WINDOW_DAYS)
    stale_days = _parse_int_option(parser, "jobs", "stale_days", DEFAULT_STALE_DAYS)
    max_daily_calls = _parse_int_option(
        parser, "budget", "max_daily_calls", DEFAULT_MAX_DAILY_CALLS, allow_zero=True
    )
    return adapters, jobs, window_days, stale_days, max_daily_calls
