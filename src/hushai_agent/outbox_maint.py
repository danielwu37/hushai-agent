"""Outbox 维护操作：失败重排、旧信封归档与清单查询（从 outbox_ops.py 拆出）。"""

from __future__ import annotations

import datetime
import sys
from typing import Any
from zoneinfo import ZoneInfo

from hushai_agent import clock, lock
from hushai_agent.config import InstanceConfig, load_instance_config
from hushai_agent.outbox_store import (
    DELIVERY_SEMANTICS,
    OUTBOX_LOCK_NAME,
    OUTBOX_STATES,
    ensure_layout,
    load_dedupe_status,
    load_envelope,
    outbox_dir,
    parse_iso,
    reconcile_duplicate_files,
    save_dedupe,
    state_subdir,
    transition_envelope,
)

DEFAULT_MAX_ATTEMPTS = 5
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


def retry_failed(
    cfg: InstanceConfig | str,
    message_id: str | None = None,
    extra_attempts: int = 3,
    now: datetime.datetime | str | None = None,
) -> dict[str, Any]:
    """将 failed/ 信封重新排入 pending/ 等待投递。"""
    from hushai_agent.outbox import _load_dedupe_with_heal_locked, _update_dedupe_entry

    inst_cfg = cfg if isinstance(cfg, InstanceConfig) else load_instance_config(cfg)
    now_iso = _now_dt(inst_cfg, now).isoformat()
    requeued, dedupe_health, dedupe_rebuilt = 0, "uninitialized", False
    with lock.runner_lock(inst_cfg, name=OUTBOX_LOCK_NAME, wait_seconds=5.0):
        ensure_layout(inst_cfg)
        reconcile_duplicate_files(inst_cfg)
        dedupe, dedupe_health, dedupe_rebuilt = _load_dedupe_with_heal_locked(inst_cfg, now_iso)
        for p in sorted(state_subdir(inst_cfg, "failed").glob("*.json")):
            if p.name.startswith("corrupt-") or (env := load_envelope(p)) is None:
                continue
            if message_id and env["message_id"] != message_id:
                continue
            env["status"], env["next_retry_at"], env["updated_at"], env["last_error"] = "pending", now_iso, now_iso, "manual-requeue"
            env["max_attempts"] = max(int(env.get("max_attempts") or DEFAULT_MAX_ATTEMPTS), int(env.get("attempts") or 0) + max(1, extra_attempts))
            transition_envelope(inst_cfg, "failed", "pending", env, on_after_write=_hook)
            _update_dedupe_entry(dedupe, env, now_iso)
            requeued += 1
        if requeued:
            save_dedupe(inst_cfg, dedupe)
    return {"requeued": requeued, "delivery_semantics": DELIVERY_SEMANTICS, "dedupe_health": dedupe_health, "dedupe_rebuilt": dedupe_rebuilt}


def archive_old(
    cfg: InstanceConfig | str,
    retain_days: int = DEFAULT_ARCHIVE_RETAIN_DAYS,
    include_failed: bool = False,
    now: datetime.datetime | str | None = None,
) -> dict[str, Any]:
    """归档已送达或超过保留期的旧信封，并清理过期去重键。"""
    from hushai_agent.outbox import _load_dedupe_with_heal_locked, _update_dedupe_entry

    inst_cfg = cfg if isinstance(cfg, InstanceConfig) else load_instance_config(cfg)
    now_dt = _now_dt(inst_cfg, now)
    now_iso, tz = now_dt.isoformat(), ZoneInfo(inst_cfg.timezone)
    cutoff = _shift_seconds(now_dt, -86400 * max(0, int(retain_days)), tz)
    archived = pruned_dedupe = 0
    dedupe_health, dedupe_rebuilt = "uninitialized", False
    with lock.runner_lock(inst_cfg, name=OUTBOX_LOCK_NAME, wait_seconds=5.0):
        ensure_layout(inst_cfg)
        reconcile_duplicate_files(inst_cfg)
        dedupe, dedupe_health, dedupe_rebuilt = _load_dedupe_with_heal_locked(inst_cfg, now_iso)
        for st in (("sent", "failed") if include_failed else ("sent",)):
            for p in sorted(state_subdir(inst_cfg, st).glob("*.json")):
                if p.name.startswith("corrupt-") or (env := load_envelope(p)) is None:
                    continue
                ts_dt = parse_iso(env.get("sent_at") or env.get("updated_at") or env.get("created_at"), tz)
                if ts_dt is None or _utc(ts_dt) > _utc(cutoff):
                    continue
                env["archived_at"] = now_iso
                transition_envelope(inst_cfg, st, "archive", env, archive_ym=ts_dt.strftime("%Y-%m"), on_after_write=_hook)
                dkey = str(env.get("dedupe_key") or "")
                if dkey and dedupe.get(dkey, {}).get("message_id") == env.get("message_id"):
                    _update_dedupe_entry(dedupe, env, now_iso, dir_state="archive")
                archived += 1
        keep_dedupe: dict[str, dict[str, Any]] = {}
        for k, entry in dedupe.items():
            exp_dt = parse_iso(entry.get("expires_at"), tz)
            if exp_dt is not None and _utc(exp_dt) <= _utc(now_dt):
                pruned_dedupe += 1
            else:
                keep_dedupe[k] = entry
        if pruned_dedupe or archived:
            save_dedupe(inst_cfg, keep_dedupe)
    return {"archived": archived, "pruned_dedupe": pruned_dedupe, "delivery_semantics": DELIVERY_SEMANTICS, "dedupe_health": dedupe_health, "dedupe_rebuilt": dedupe_rebuilt}


def list_envelopes(cfg: InstanceConfig | str, status: str | None = None) -> dict[str, Any]:
    """统计并列出 outbox 各状态信封及去重索引健康度。"""
    inst_cfg = cfg if isinstance(cfg, InstanceConfig) else load_instance_config(cfg)
    counts: dict[str, int] = {st: 0 for st in (*OUTBOX_STATES, "archive")}
    items_out: list[dict[str, Any]] = []
    root = outbox_dir(inst_cfg)
    dedupe_health, corrupted_dedupe_index = "uninitialized", False
    if root.is_dir():
        for st in OUTBOX_STATES:
            sdir = state_subdir(inst_cfg, st)
            files = sorted(sdir.glob("*.json")) if sdir.is_dir() else []
            counts[st] = len(files)
            if status in (None, st):
                for p in files:
                    items_out.append(load_envelope(p) or {"file": p.name, "status": st, "corrupted": True})
        adir = state_subdir(inst_cfg, "archive")
        afiles = sorted(adir.rglob("*.json")) if adir.is_dir() else []
        counts["archive"] = len(afiles)
        if status == "archive":
            for p in afiles:
                items_out.append(load_envelope(p) or {"file": p.name, "status": "archive", "corrupted": True})
        _, dstatus = load_dedupe_status(inst_cfg)
        corrupted_dedupe_index = dstatus == "corrupt"
        dedupe_health = "corrupt" if dstatus == "corrupt" else (("missing_with_envelopes" if sum(counts.values()) > 0 else "uninitialized") if dstatus == "missing" else "ok")
    return {"delivery_semantics": DELIVERY_SEMANTICS, "dedupe_health": dedupe_health, "corrupted_dedupe_index": corrupted_dedupe_index, "counts": counts, "envelopes": items_out}
