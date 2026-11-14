"""实例时钟抽象：支持通过环境变量 HUSHAI_NOW 注入测试与回放时间。"""

from __future__ import annotations

import datetime
import os
from typing import Any
from zoneinfo import ZoneInfo

from hushai_agent.config import DEFAULT_TIMEZONE


def _resolve_tz(cfg: Any) -> ZoneInfo:
    """从配置对象或时区字符串解析 ZoneInfo。"""
    if cfg is None:
        tz_name = DEFAULT_TIMEZONE
    elif isinstance(cfg, str):
        tz_name = cfg
    else:
        tz_name = getattr(cfg, "timezone", DEFAULT_TIMEZONE)
    return ZoneInfo(tz_name)


def now(cfg: Any = None) -> datetime.datetime:
    """返回实例时区的当前时间；若设置了 HUSHAI_NOW 则按实例本地时间解析返回。"""
    tz = _resolve_tz(cfg)
    env_now = os.environ.get("HUSHAI_NOW", "").strip()
    if env_now:
        normalized = env_now[:-1] + "+00:00" if env_now.endswith("Z") else env_now
        parsed = datetime.datetime.fromisoformat(normalized)
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=tz)
        return parsed.astimezone(tz)
    return datetime.datetime.now(tz)


def today(cfg: Any = None) -> datetime.date:
    """返回实例时区的当前日期。"""
    return now(cfg).date()


def is_real_date(text: str) -> bool:
    """YYYY-MM-DD 且是日历上真实存在的日子（2026-02-31 不算）。"""
    try:
        datetime.date.fromisoformat(text)
    except (TypeError, ValueError):
        return False
    return True
