"""Outbox 运维恢复、ack 标记、重试、归档与清单查询（从 outbox.py 拆出）。"""

from __future__ import annotations

import datetime
import sys
from typing import Any
from zoneinfo import ZoneInfo

from hushai_agent import clock, ledger, lock
from hushai_agent.config import InstanceConfig, load_instance_config
from hushai_agent.outbox_store import (
    DELIVERY_SEMANTICS,
    OUTBOX_LOCK_NAME,
    OUTBOX_STATES,
    ensure_layout,
    iso_to_utc_ts,
    load_dedupe,
    load_dedupe_status,
    load_envelope,
    outbox_dir,
    parse_iso,
    quarantine_corrupt,
    reconcile_duplicate_files,
    save_dedupe,
    state_subdir,
    transition_envelope,
    write_envelope,
)

DEFAULT_MAX_ATTEMPTS = 5
DEFAULT_BASE_BACKOFF_SECONDS = 30
DEFAULT_MAX_BACKOFF_SECONDS = 3600
DEFAULT_ARCHIVE_RETAIN_DAYS = 30


def _hook(stage: str) -> None:
    mod = sys.modules.get("hushai_agent.outbox")
    fn = getattr(mod, "_stage_hook", None) if mod is not None else None
    if fn is not None:
        fn(stage)


def _utc(dt: datetime.datetime) -> datetime.datetime:
    return dt.astimezone(datetime.timezone.utc)


def _shift_seconds(dt: datetime.datetime, seconds: int | float, tz: ZoneInfo) -> datetime.datetime:
    return (_utc(dt) + datetime.timedelta(seconds=seconds)).astimezone(tz)


def _now_dt(cfg: InstanceConfig, now: datetime.datetime | str | None = None) -> datetime.datetime:
    tz = ZoneInfo(cfg.timezone)
    if now is None:
        return clock.now(cfg)
    if isinstance(now, str):
        parsed = parse_iso(now, tz)
        if parsed is None:
            raise ValueError(f"非法时间格式: {now!r}")
        return parsed
    return now.replace(tzinfo=tz) if now.tzinfo is None else now.astimezone(tz)


def mark_acked_by_rel_paths(cfg: InstanceConfig | str, door: str, rel_paths: list[str]) -> int:
    """配合 gate check-inbox --ack，将已发送至对应端的 exchange_out 信封标注 acked_at。"""
    if not rel_paths:
        return 0
    inst_cfg = cfg if isinstance(cfg, InstanceConfig) else load_instance_config(cfg)
    targets, acked, now_iso = set(rel_paths), 0, clock.now(inst_cfg).isoformat()
    with lock.runner_lock(inst_cfg, name=OUTBOX_LOCK_NAME, wait_seconds=5.0):
        reconcile_duplicate_files(inst_cfg)
        sent_dir = state_subdir(inst_cfg, "sent")
        if not sent_dir.is_dir():
            return 0
        for p in sorted(sent_dir.glob("*.json")):
            if p.name.startswith("corrupt-"):
                continue
            env = load_envelope(p)
            if not env or env.get("channel") != "exchange_out" or env.get("target") != door:
                continue
            rel = str((env.get("payload") or {}).get("rel_path") or "")
            if rel in targets and not env.get("acked_at"):
                env["acked_at"] = now_iso
                env["updated_at"] = now_iso
                env["transition_seq"] = max(1, int(env.get("transition_seq") or 0)) + 1
                write_envelope(inst_cfg, "sent", env)
                acked += 1
    return acked


def _recover_stale_sending_locked(
    cfg: InstanceConfig,
    now_dt: datetime.datetime,
    stale_seconds: int | None = None,
) -> dict[str, Any]:
    from hushai_agent.outbox import _load_dedupe_with_heal_locked, _update_dedupe_entry

    tz, now_iso = ZoneInfo(cfg.timezone), now_dt.isoformat()
    dedupe_cleaned = reconcile_duplicate_files(cfg)
    dedupe, dedupe_health, dedupe_rebuilt = _load_dedupe_with_heal_locked(cfg, now_iso)
    sdir = state_subdir(cfg, "sending")
    recovered = failed = corrupted = 0
    if not sdir.is_dir():
        return {"recovered": 0, "failed": 0, "corrupted": 0, "dedupe_cleaned": dedupe_cleaned, "dedupe_health": dedupe_health, "dedupe_rebuilt": dedupe_rebuilt}

    changed_dedupe = False
    for p in sorted(sdir.glob("*.json")):
        if p.name.startswith("corrupt-"):
            continue
        env = load_envelope(p)
        if env is None:
            quarantine_corrupt(cfg, p, "invalid sending envelope json")
            corrupted += 1
            continue
        if stale_seconds is not None:
            started_dt = parse_iso(env.get("sending_started_at") or env.get("updated_at"), tz)
            if started_dt is not None and (_utc(now_dt) - _utc(started_dt)).total_seconds() < stale_seconds:
                continue
        attempts = int(env.get("attempts") or 0)
        max_attempts = int(env.get("max_attempts") or DEFAULT_MAX_ATTEMPTS)
        env["sending_started_at"], env["updated_at"] = None, now_iso
        if attempts >= max_attempts:
            env["status"], env["last_error"] = "failed", "crash-recovery-max-attempts"
            transition_envelope(cfg, "sending", "failed", env, on_after_write=_hook)
            failed += 1
        else:
            env["status"], env["next_retry_at"], env["last_error"] = "pending", now_iso, "reclaimed-after-sending-crash"
            transition_envelope(cfg, "sending", "pending", env, on_after_write=_hook)
            recovered += 1
        _update_dedupe_entry(dedupe, env, now_iso)
        changed_dedupe = True
    if changed_dedupe:
        save_dedupe(cfg, dedupe)
    return {"recovered": recovered, "failed": failed, "corrupted": corrupted, "dedupe_cleaned": dedupe_cleaned, "dedupe_health": dedupe_health, "dedupe_rebuilt": dedupe_rebuilt}


def recover_stale_sending(cfg: InstanceConfig | str, stale_seconds: int | None = None, now: datetime.datetime | str | None = None) -> dict[str, Any]:
    """在持有 outbox.lock 下回收中断遗留在 sending/ 的信封（明确 at-least-once 重投）。"""
    inst_cfg = cfg if isinstance(cfg, InstanceConfig) else load_instance_config(cfg)
    now_dt = _now_dt(inst_cfg, now)
    with lock.runner_lock(inst_cfg, name=OUTBOX_LOCK_NAME, wait_seconds=5.0):
        ensure_layout(inst_cfg)
        res = _recover_stale_sending_locked(inst_cfg, now_dt, stale_seconds=stale_seconds)
    res["delivery_semantics"] = DELIVERY_SEMANTICS
    return res


def flush_outbox(
    cfg: InstanceConfig | str,
    *,
    handlers: dict[str, Any] | None = None,
    now: datetime.datetime | str | None = None,
    channel_filter: str | None = None,
    stale_seconds: int | None = None,
    limit: int = 50,
) -> dict[str, Any]:
    from hushai_agent.outbox import DEFAULT_HANDLERS, _update_dedupe_entry

    inst_cfg = cfg if isinstance(cfg, InstanceConfig) else load_instance_config(cfg)
    now_dt = _now_dt(inst_cfg, now)
    tz = ZoneInfo(inst_cfg.timezone)
    reg = dict(DEFAULT_HANDLERS)
    if handlers:
        reg.update(handlers)

    sent = failed = retried = deferred_backoff = corrupted = 0
    rec_info: dict[str, Any] = {"recovered": 0, "failed": 0, "corrupted": 0, "dedupe_cleaned": 0, "dedupe_health": "uninitialized", "dedupe_rebuilt": False}
    with lock.runner_lock(inst_cfg, name=OUTBOX_LOCK_NAME, wait_seconds=5.0):
        ensure_layout(inst_cfg)
        rec_info = _recover_stale_sending_locked(inst_cfg, now_dt, stale_seconds=stale_seconds)
        corrupted += rec_info["corrupted"]
        failed += rec_info["failed"]
        pending_dir = state_subdir(inst_cfg, "pending")
        candidates_list: list[tuple[float, str, Any, dict[str, Any]]] = []
        for p in sorted(pending_dir.glob("*.json")):
            if p.name.startswith("corrupt-"):
                continue
            env = load_envelope(p)
            if env is None:
                quarantine_corrupt(inst_cfg, p, "invalid pending envelope json")
                corrupted += 1
                continue
            candidates_list.append((iso_to_utc_ts(env.get("created_at")), str(env["message_id"]), p, env))
        candidates_list.sort(key=lambda x: (x[0], x[1]))

        dedupe, processed = load_dedupe(inst_cfg), 0
        for _, _, _, env in candidates_list:
            if processed >= limit:
                break
            if channel_filter and env.get("channel") != channel_filter:
                continue
            next_dt = parse_iso(env.get("next_retry_at"), tz)
            if next_dt is not None and _utc(next_dt) > _utc(now_dt):
                deferred_backoff += 1
                continue
            processed += 1
            now_iso = _now_dt(inst_cfg, now).isoformat()
            attempts = int(env.get("attempts") or 0) + 1
            env["attempts"], env["status"], env["sending_started_at"], env["updated_at"] = attempts, "sending", now_iso, now_iso
            transition_envelope(inst_cfg, "pending", "sending", env, on_after_write=_hook)
            _update_dedupe_entry(dedupe, env, now_iso)
            save_dedupe(inst_cfg, dedupe)
            _hook("after_write_sending")

            handler = reg.get(str(env["channel"]))
            if handler is None:
                ok, detail = False, f"unsupported channel: {env['channel']}"
            else:
                try:
                    ok, detail = handler(inst_cfg, env)
                except Exception as exc:  # noqa: BLE001
                    ok, detail = False, f"exception: {exc}"

            end_dt = _now_dt(inst_cfg, now)
            end_iso = end_dt.isoformat()
            env["sending_started_at"], env["updated_at"] = None, end_iso
            if ok:
                _hook("after_external_send_before_sent")
                env["status"], env["sent_at"], env["last_error"] = "sent", end_iso, ""
                transition_envelope(inst_cfg, "sending", "sent", env, on_after_write=_hook)
                sent += 1
            else:
                env["last_error"] = str(detail)[:500]
                max_attempts = int(env.get("max_attempts") or DEFAULT_MAX_ATTEMPTS)
                if attempts >= max_attempts:
                    env["status"] = "failed"
                    transition_envelope(inst_cfg, "sending", "failed", env, on_after_write=_hook)
                    failed += 1
                else:
                    base_s = int(env.get("base_backoff_seconds") or DEFAULT_BASE_BACKOFF_SECONDS)
                    max_s = int(env.get("max_backoff_seconds") or DEFAULT_MAX_BACKOFF_SECONDS)
                    delay = min(max_s, base_s * (2 ** max(0, attempts - 1)))
                    env["status"], env["next_retry_at"] = "pending", _shift_seconds(end_dt, delay, tz).isoformat()
                    transition_envelope(inst_cfg, "sending", "pending", env, on_after_write=_hook)
                    retried += 1
            _update_dedupe_entry(dedupe, env, end_iso)
            save_dedupe(inst_cfg, dedupe)

    if rec_info["recovered"] or sent or failed or retried or corrupted:
        ledger.record(inst_cfg, "job", job="outbox", date=now_dt.date().isoformat(), result="failed" if failed else "ok",
                      note=f"sent={sent}; retried={retried}; failed={failed}; recovered={rec_info['recovered']}")
    return {
        "delivery_semantics": DELIVERY_SEMANTICS, "recovered": rec_info["recovered"], "sent": sent, "retried": retried,
        "failed": failed, "deferred_backoff": deferred_backoff, "corrupted": corrupted,
        "dedupe_health": rec_info["dedupe_health"], "dedupe_rebuilt": rec_info["dedupe_rebuilt"],
    }


from hushai_agent.outbox_maint import archive_old, list_envelopes, retry_failed  # noqa: F401
