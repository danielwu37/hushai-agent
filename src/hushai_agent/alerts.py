"""系统与自愈告警账本（<state>/alerts.jsonl）：隔离系统消息，不再污染 hub/inbox/ 或 hub/today.md。"""

from __future__ import annotations

import datetime
import json
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from hushai_agent import clock, lock
from hushai_agent.fsio import ensure_dir
from hushai_agent.guard import ensure_writable
from hushai_agent.paths import get_instance_state_dir

ALERTS_FILENAME = "alerts.jsonl"
DEFAULT_ALERT_HOURS = 48
DEFAULT_ALERT_LIMIT = 20


def alerts_path(cfg: Any) -> Path:
    """返回实例 alerts.jsonl 路径。"""
    name = cfg.name if hasattr(cfg, "name") else str(cfg)
    return get_instance_state_dir(name) / ALERTS_FILENAME


def _parse_iso(ts_raw: Any, tz: ZoneInfo) -> datetime.datetime | None:
    if not isinstance(ts_raw, str) or not ts_raw.strip():
        return None
    text = ts_raw.strip()
    norm = text[:-1] + "+00:00" if text.endswith("Z") else text
    try:
        dt = datetime.datetime.fromisoformat(norm)
    except ValueError:
        return None
    return dt.replace(tzinfo=tz) if dt.tzinfo is None else dt.astimezone(tz)


def record_alert(
    cfg: Any,
    *,
    kind: str,
    level: str,
    message: str,
    suggestion: str = "",
    source: str = "",
    detail: str = "",
    ts: str | None = None,
) -> dict[str, Any]:
    """追加一条结构化系统告警到 <state>/alerts.jsonl（持 alerts.lock 保证原子追加）。"""
    state_dir = get_instance_state_dir(cfg.name if hasattr(cfg, "name") else str(cfg))
    now_dt = clock.now(cfg)
    entry: dict[str, Any] = {
        "ts": ts or now_dt.isoformat(timespec="seconds"),
        "date": now_dt.date().isoformat(),
        "kind": str(kind).strip() or "system",
        "level": str(level).strip() or "info",
        "message": str(message).strip(),
    }
    if suggestion:
        entry["suggestion"] = str(suggestion).strip()
    if source:
        entry["source"] = str(source).strip()
    if detail:
        entry["detail"] = str(detail).strip()

    with lock.runner_lock(cfg, name="alerts.lock", wait_seconds=5.0):
        ensure_dir(state_dir, [state_dir])
        target = ensure_writable(state_dir / ALERTS_FILENAME, [state_dir])
        with open(target, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    return entry


def load_recent_alerts(
    cfg: Any,
    *,
    hours: int = DEFAULT_ALERT_HOURS,
    limit: int = DEFAULT_ALERT_LIMIT,
) -> list[dict[str, Any]]:
    """读取最近 hours 小时内的系统告警（按时间倒序、同类内容保留最新一条）。"""
    path = alerts_path(cfg)
    if not path.is_file():
        return []

    now_dt = clock.now(cfg)
    tz = ZoneInfo(getattr(cfg, "timezone", "Asia/Shanghai"))
    cutoff = now_dt - datetime.timedelta(hours=max(1, hours))

    rows: list[dict[str, Any]] = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []

    for raw in lines:
        s = raw.strip()
        if not s:
            continue
        try:
            obj = json.loads(s)
        except json.JSONDecodeError:
            continue
        if not isinstance(obj, dict) or not obj.get("message"):
            continue
        dt = _parse_iso(obj.get("ts"), tz)
        if dt is not None and dt < cutoff:
            continue
        rows.append(obj)

    deduped: list[dict[str, Any]] = []
    seen_keys: set[tuple[str, str]] = set()
    for row in reversed(rows):
        key = (str(row.get("kind") or ""), str(row.get("message") or ""))
        if key in seen_keys:
            continue
        seen_keys.add(key)
        deduped.append(row)
        if len(deduped) >= limit:
            break
    return deduped
