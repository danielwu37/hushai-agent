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
# 需要使用者知道的级别；info（已自愈、手动切换）只记账，不主动报
SEVERE_LEVELS = frozenset({"warning", "error", "critical", "needs_human"})
RESOLVED_KIND = "resolved"


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
        if not isinstance(obj, dict) or not obj.get("message") or obj.get("kind") == RESOLVED_KIND:
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


def _read_rows(cfg: Any) -> list[dict[str, Any]]:
    path = alerts_path(cfg)
    if not path.is_file():
        return []
    rows: list[dict[str, Any]] = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    for raw in lines:
        try:
            obj = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            rows.append(obj)
    return rows


def active_alerts(cfg: Any, *, hours: int = 72, limit: int = DEFAULT_ALERT_LIMIT) -> list[dict[str, Any]]:
    """未恢复的严重告警（warning 及以上）：之后出现同来源前缀的 resolved 行即视为已恢复。"""
    tz = ZoneInfo(getattr(cfg, "timezone", "Asia/Shanghai"))
    cutoff = clock.now(cfg) - datetime.timedelta(hours=max(1, hours))
    open_rows: list[dict[str, Any]] = []
    for row in _read_rows(cfg):
        if row.get("kind") == RESOLVED_KIND:
            prefix = str(row.get("resolves") or "")
            if prefix:
                open_rows = [r for r in open_rows if not str(r.get("source") or "").startswith(prefix)]
            continue
        if row.get("level") not in SEVERE_LEVELS or not row.get("message"):
            continue
        dt = _parse_iso(row.get("ts"), tz)
        if dt is not None and dt < cutoff:
            continue
        open_rows.append(row)
    out: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for row in reversed(open_rows):
        key = (str(row.get("source") or row.get("kind") or ""), str(row.get("message") or ""))
        if key not in seen:
            seen.add(key)
            out.append(row)
    return out[:limit]


def resolve_alerts(cfg: Any, source_prefix: str, note: str = "") -> int:
    """把来源以 source_prefix 开头的未恢复告警标为已恢复；没有未恢复的就不写，返回撤下条数。"""
    prefix = str(source_prefix or "").strip()
    if not prefix:
        return 0
    hits = [a for a in active_alerts(cfg, hours=24 * 30, limit=1000) if str(a.get("source") or "").startswith(prefix)]
    if not hits:
        return 0
    state_dir = get_instance_state_dir(cfg.name if hasattr(cfg, "name") else str(cfg))
    entry = {
        "ts": clock.now(cfg).isoformat(timespec="seconds"),
        "kind": RESOLVED_KIND,
        "level": "info",
        "message": note or f"已恢复：{hits[0].get('message', '')}"[:200],
        "resolves": prefix,
    }
    with lock.runner_lock(cfg, name="alerts.lock", wait_seconds=5.0):
        target = ensure_writable(state_dir / ALERTS_FILENAME, [state_dir])
        with open(target, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    return len(hits)


def resolve_heal_check(cfg: Any, check_id: str) -> int:
    """heal 某项检查恢复正常：撤下该项的需人工告警，并清掉 24h 冷却，复发时能再报。"""
    n = resolve_alerts(cfg, f"{check_id}:")
    if n:
        cool = get_instance_state_dir(cfg.name) / "heal-alerts.json"
        try:
            data = json.loads(cool.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = {}
        if isinstance(data, dict) and any(str(k).startswith(f"{check_id}:") for k in data):
            kept = {k: v for k, v in data.items() if not str(k).startswith(f"{check_id}:")}
            from hushai_agent.fsio import atomic_write

            atomic_write(cool, json.dumps(kept, ensure_ascii=False, indent=2) + "\n", [cool.parent])
    return n


def render_active(cfg: Any) -> str:
    """给管家会话开场看的主动报告；没有未恢复告警时返回空串。"""
    rows = active_alerts(cfg)
    if not rows:
        return ""
    lines = [f"【系统主动报告】有 {len(rows)} 条系统问题未恢复，管家须在本次对话开头主动告诉使用者，并说明建议："]
    for a in rows:
        sug = f"（建议：{a['suggestion']}）" if a.get("suggestion") else ""
        lines.append(f"- {str(a.get('ts', ''))[:16]} [{a.get('level')}] {a.get('message')}{sug}")
    return "\n".join(lines)
