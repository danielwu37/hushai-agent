"""纯文件型 Outbox（<state>/outbox/）：handlers、dedupe 索引维护、enqueue 与 flush 入口。"""

from __future__ import annotations

import datetime
import uuid
from typing import Any, Callable
from zoneinfo import ZoneInfo

from hushai_agent import lock
from hushai_agent.config import InstanceConfig, load_instance_config
from hushai_agent.fsio import atomic_write
from hushai_agent.inboxscan import iter_inbox_lines
from hushai_agent.outbox_ops import (  # noqa: F401
    DEFAULT_ARCHIVE_RETAIN_DAYS,
    DEFAULT_BASE_BACKOFF_SECONDS,
    DEFAULT_MAX_ATTEMPTS,
    DEFAULT_MAX_BACKOFF_SECONDS,
    _now_dt,
    _shift_seconds,
    _utc,
    archive_old,
    flush_outbox as flush,
    list_envelopes,
    mark_acked_by_rel_paths,
    recover_stale_sending,
    retry_failed,
)
from hushai_agent.outbox_store import (
    DELIVERY_SEMANTICS,
    OUTBOX_LOCK_NAME,
    OUTBOX_STATES,
    ensure_layout,
    has_active_envelope,
    iso_to_utc_ts,
    iter_all_envelope_files,
    load_dedupe_status,
    load_envelope,
    parse_iso,
    reconcile_duplicate_files,
    save_dedupe,
    state_subdir,
    write_envelope,
)
from hushai_agent.vault import add_inbox_entry

HandlerFn = Callable[[InstanceConfig, dict[str, Any]], tuple[bool, str]]


def _stage_hook(stage: str) -> None:
    """供测试与故障注入使用的阶段钩子点（生产环境下为空操作）。"""
    _ = stage


def _deliver_exchange_out(cfg: InstanceConfig, env: dict[str, Any]) -> tuple[bool, str]:
    payload = env.get("payload") or {}
    rel = str(payload.get("rel_path") or "").strip()
    content = str(payload.get("content") or "")
    if not rel:
        return False, "missing payload.rel_path"
    atomic_write(cfg.vault / rel, content, [cfg.vault])
    receipt = payload.get("receipt")
    if isinstance(receipt, dict):
        steward_door = str(receipt.get("steward_door") or cfg.steward_door)
        domain = str(receipt.get("domain") or cfg.domains[0])
        did = str(receipt.get("did") or "").strip()
        if did and not any(r.door == steward_door and r.artifact == rel for r in iter_inbox_lines(cfg.vault / "hub" / "inbox")):
            add_inbox_entry(cfg.name, steward_door, domain, did, artifact=rel)
    return True, rel


def _deliver_today_alert(cfg: InstanceConfig, env: dict[str, Any]) -> tuple[bool, str]:
    from hushai_agent import alerts

    payload = env.get("payload") or {}
    alert_line = str(payload.get("alert_line") or "").strip()
    if not alert_line:
        return False, "missing payload.alert_line"
    msg = alert_line[5:].strip() if alert_line.startswith("- 系统：") else alert_line
    alerts.record_alert(
        cfg,
        kind=str(payload.get("kind") or "system_alert"),
        level=str(payload.get("level") or "needs_human"),
        message=msg,
        suggestion=str(payload.get("suggestion") or ""),
        source=str(payload.get("problem_key") or "outbox"),
    )
    return True, alert_line


DEFAULT_HANDLERS: dict[str, HandlerFn] = {
    "exchange_out": _deliver_exchange_out,
    "today_alert": _deliver_today_alert,
}


def has_pending_or_sending(cfg: InstanceConfig | str) -> bool:
    for st in ("pending", "sending"):
        sdir = state_subdir(cfg, st)
        if sdir.is_dir() and any(p.is_file() and p.suffix == ".json" for p in sdir.iterdir()):
            return True
    return False


def _find_latest_by_dedupe_key(cfg: InstanceConfig, dedupe_key: str) -> tuple[str, dict[str, Any]] | None:
    matches = [(st, env) for st, p in iter_all_envelope_files(cfg) if (env := load_envelope(p)) and str(env.get("dedupe_key") or "") == dedupe_key]
    if not matches:
        return None
    return max(matches, key=lambda item: (iso_to_utc_ts(item[1].get("created_at")), int(item[1].get("transition_seq") or 0), str(item[1].get("message_id") or "")))


def _envelope_state_anywhere(cfg: InstanceConfig, message_id: str) -> str | None:
    if not message_id:
        return None
    if (active := has_active_envelope(cfg, message_id)) is not None:
        return active
    for st, p in iter_all_envelope_files(cfg):
        if st == "archive" and p.stem == message_id:
            return "archive"
    return None


def _update_dedupe_entry(dedupe: dict[str, dict[str, Any]], env: dict[str, Any], now_iso: str, *, dir_state: str | None = None) -> None:
    if not (dkey := str(env.get("dedupe_key") or "")):
        return
    prev = dedupe.get(dkey) or {}
    dedupe[dkey] = {
        "message_id": env["message_id"],
        "status": "archive" if dir_state == "archive" else env["status"],
        "transition_seq": int(env.get("transition_seq") or 1),
        "created_at": env.get("created_at", now_iso),
        "updated_at": now_iso,
        "expires_at": env.get("dedupe_expires_at", prev.get("expires_at")),
    }


def _rebuild_dedupe_from_disk(cfg: InstanceConfig, now_iso: str) -> dict[str, dict[str, Any]]:
    best_by_key: dict[str, tuple[tuple[float, int, str], str, dict[str, Any]]] = {}
    for st, p in iter_all_envelope_files(cfg):
        if (env := load_envelope(p)) is None:
            continue
        if not (dkey := str(env.get("dedupe_key") or "").strip()):
            continue
        rank = (iso_to_utc_ts(env.get("created_at")), int(env.get("transition_seq") or 0), str(env.get("message_id") or ""))
        if dkey not in best_by_key or rank > best_by_key[dkey][0]:
            best_by_key[dkey] = (rank, st, env)
    rebuilt: dict[str, dict[str, Any]] = {}
    for dkey in sorted(best_by_key):
        _, st, env = best_by_key[dkey]
        _update_dedupe_entry(rebuilt, env, now_iso, dir_state=st)
    return rebuilt


def _load_dedupe_with_heal_locked(cfg: InstanceConfig, now_iso: str) -> tuple[dict[str, dict[str, Any]], str, bool]:
    dedupe, status = load_dedupe_status(cfg)
    if status == "ok":
        return dedupe, "ok", False
    files = iter_all_envelope_files(cfg)
    if status == "missing" and not files:
        return {}, "uninitialized", False
    rebuilt = _rebuild_dedupe_from_disk(cfg, now_iso)
    save_dedupe(cfg, rebuilt)
    return rebuilt, ("corrupt" if status == "corrupt" else "missing_with_envelopes"), True


def enqueue(
    cfg: InstanceConfig | str,
    channel: str,
    target: str,
    payload: dict[str, Any],
    dedupe_key: str,
    *,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    base_backoff_seconds: int = DEFAULT_BASE_BACKOFF_SECONDS,
    max_backoff_seconds: int = DEFAULT_MAX_BACKOFF_SECONDS,
    cooldown_seconds: int | None = None,
    immediate_sent: bool = False,
    now: datetime.datetime | str | None = None,
) -> dict[str, Any]:
    """按幂等键去重后入队到 <state>/outbox/pending/（或 immediate_sent 记入 sent/）。"""
    inst_cfg = cfg if isinstance(cfg, InstanceConfig) else load_instance_config(cfg)
    if not (dkey := str(dedupe_key or "").strip()):
        raise ValueError("outbox.enqueue 需要非空 dedupe_key")
    tz = ZoneInfo(inst_cfg.timezone)
    now_dt = _now_dt(inst_cfg, now)
    now_iso = now_dt.isoformat()
    expires_at = _shift_seconds(now_dt, cooldown_seconds, tz).isoformat() if cooldown_seconds else None

    with lock.runner_lock(inst_cfg, name=OUTBOX_LOCK_NAME, wait_seconds=5.0):
        ensure_layout(inst_cfg)
        reconcile_duplicate_files(inst_cfg)
        dedupe, dedupe_health, dedupe_rebuilt = _load_dedupe_with_heal_locked(inst_cfg, now_iso)
        if existing := dedupe.get(dkey):
            exp_dt = parse_iso(existing.get("expires_at"), tz)
            loc_state = _envelope_state_anywhere(inst_cfg, str(existing.get("message_id") or ""))
            in_cooldown = exp_dt is not None and _utc(now_dt) < _utc(exp_dt)
            known_state = loc_state or (existing.get("status") if existing.get("status") in (*OUTBOX_STATES, "archive") else None)
            if in_cooldown or (existing.get("expires_at") is None and known_state is not None):
                return {"queued": False, "deduplicated": True, "message_id": existing.get("message_id"), "status": loc_state or existing.get("status"), "delivery_semantics": DELIVERY_SEMANTICS, "dedupe_health": dedupe_health, "dedupe_rebuilt": dedupe_rebuilt}
        if (disk_hit := _find_latest_by_dedupe_key(inst_cfg, dkey)) is not None:
            st, env = disk_hit
            exp_dt = parse_iso(env.get("dedupe_expires_at"), tz)
            if exp_dt is None or _utc(now_dt) < _utc(exp_dt):
                _update_dedupe_entry(dedupe, env, now_iso, dir_state=st)
                save_dedupe(inst_cfg, dedupe)
                return {"queued": False, "deduplicated": True, "message_id": env["message_id"], "status": st, "delivery_semantics": DELIVERY_SEMANTICS, "dedupe_health": dedupe_health, "dedupe_rebuilt": dedupe_rebuilt}

        msg_id = f"OB-{now_dt.strftime('%Y%m%dT%H%M%S')}-{uuid.uuid4().hex[:8]}"
        status = "sent" if immediate_sent else "pending"
        env = {
            "version": 1, "transition_seq": 1, "message_id": msg_id, "dedupe_key": dkey, "dedupe_expires_at": expires_at,
            "channel": str(channel), "target": str(target), "payload": dict(payload), "delivery_semantics": DELIVERY_SEMANTICS,
            "status": status, "created_at": now_iso, "updated_at": now_iso, "attempts": 1 if immediate_sent else 0,
            "max_attempts": max(1, int(max_attempts)), "base_backoff_seconds": max(1, int(base_backoff_seconds)),
            "max_backoff_seconds": max(1, int(max_backoff_seconds)), "next_retry_at": now_iso, "sending_started_at": None,
            "sent_at": now_iso if immediate_sent else None, "last_error": "",
        }
        write_envelope(inst_cfg, status, env)
        _update_dedupe_entry(dedupe, env, now_iso)
        save_dedupe(inst_cfg, dedupe)
        return {"queued": True, "deduplicated": False, "message_id": msg_id, "status": status, "delivery_semantics": DELIVERY_SEMANTICS, "dedupe_health": dedupe_health, "dedupe_rebuilt": dedupe_rebuilt}
