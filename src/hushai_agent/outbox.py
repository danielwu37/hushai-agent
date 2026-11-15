"""纯文件型 Outbox（<state>/outbox/）：pending/sending/sent/failed 状态机、恢复、指数退避、去重与归档。

投递语义（明确声明）：本模块提供 **at-least-once（至少一次）** 投递保证——若进程在外部投递已生效后、
`sending -> sent` 原子迁移前崩溃，恢复器会回收 `sending` 信封并在下次 flush 时重新投递；绝不宣称
外部消息恰好送达一次（exactly-once）。
"""

from __future__ import annotations

import datetime
import json
import uuid
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo

from hushai_agent import clock, ledger, lock
from hushai_agent.config import InstanceConfig, load_instance_config
from hushai_agent.fsio import atomic_write
from hushai_agent.inboxscan import iter_inbox_lines
from hushai_agent.outbox_store import (
    DELIVERY_SEMANTICS,
    OUTBOX_LOCK_NAME,
    OUTBOX_STATES,
    ensure_layout,
    has_active_envelope,
    load_dedupe,
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
from hushai_agent.paths import get_instance_state_dir
from hushai_agent.vault import add_inbox_entry

DEFAULT_MAX_ATTEMPTS = 5
DEFAULT_BASE_BACKOFF_SECONDS = 30
DEFAULT_MAX_BACKOFF_SECONDS = 3600
DEFAULT_ARCHIVE_RETAIN_DAYS = 30

HandlerFn = Callable[[InstanceConfig, dict[str, Any]], tuple[bool, str]]


def _stage_hook(stage: str) -> None:
    """供测试与故障注入使用的阶段钩子点（生产环境下为空操作）。"""
    _ = stage


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
    from hushai_agent.heal import _prepend_today_alert

    payload = env.get("payload") or {}
    alert_line = str(payload.get("alert_line") or "").strip()
    if not alert_line:
        return False, "missing payload.alert_line"
    _prepend_today_alert(cfg, alert_line)
    return True, alert_line


DEFAULT_HANDLERS: dict[str, HandlerFn] = {
    "exchange_out": _deliver_exchange_out,
    "today_alert": _deliver_today_alert,
}


def has_pending_or_sending(cfg: InstanceConfig | str) -> bool:
    """快速判断 outbox 是否存在待投递或待恢复信封（无锁只读检查）。"""
    for st in ("pending", "sending"):
        sdir = state_subdir(cfg, st)
        if sdir.is_dir() and any(p.is_file() and p.suffix == ".json" for p in sdir.iterdir()):
            return True
    return False


def _find_active_by_dedupe_key(cfg: InstanceConfig, dedupe_key: str) -> tuple[str, dict[str, Any]] | None:
    matches: list[tuple[str, dict[str, Any]]] = []
    for st in ("pending", "sending", "sent", "failed"):
        sdir = state_subdir(cfg, st)
        if not sdir.is_dir():
            continue
        for p in sorted(sdir.glob("*.json")):
            if p.name.startswith("corrupt-"):
                continue
            env = load_envelope(p)
            if env and str(env.get("dedupe_key") or "") == dedupe_key:
                matches.append((st, env))
    if not matches:
        return None
    return max(matches, key=lambda item: (str(item[1].get("created_at") or ""), str(item[1].get("message_id") or "")))


def _update_dedupe_entry(
    dedupe: dict[str, dict[str, Any]],
    env: dict[str, Any],
    now_iso: str,
) -> None:
    dkey = str(env.get("dedupe_key") or "")
    if not dkey:
        return
    prev = dedupe.get(dkey) or {}
    dedupe[dkey] = {
        "message_id": env["message_id"],
        "status": env["status"],
        "created_at": env.get("created_at", now_iso),
        "updated_at": now_iso,
        "expires_at": env.get("dedupe_expires_at", prev.get("expires_at")),
    }


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
    dkey = str(dedupe_key or "").strip()
    if not dkey:
        raise ValueError("outbox.enqueue 需要非空 dedupe_key")

    tz = ZoneInfo(inst_cfg.timezone)
    now_dt = _now_dt(inst_cfg, now)
    now_iso = now_dt.isoformat()
    expires_at = (now_dt + datetime.timedelta(seconds=cooldown_seconds)).isoformat() if cooldown_seconds else None

    with lock.runner_lock(inst_cfg, name=OUTBOX_LOCK_NAME, wait_seconds=5.0):
        ensure_layout(inst_cfg)
        dedupe = load_dedupe(inst_cfg)
        existing = dedupe.get(dkey)
        if existing:
            exp_dt = parse_iso(existing.get("expires_at"), tz)
            active_state = has_active_envelope(inst_cfg, str(existing.get("message_id") or ""))
            # 若设了冷却窗口且已过期，则允许重新入队；否则在有效或永久去重下跳过
            in_cooldown = exp_dt is not None and now_dt < exp_dt
            permanent_dup = existing.get("expires_at") is None and active_state is not None
            if in_cooldown or permanent_dup or (existing.get("expires_at") is None and existing.get("status") in OUTBOX_STATES):
                return {
                    "queued": False,
                    "deduplicated": True,
                    "message_id": existing.get("message_id"),
                    "status": active_state or existing.get("status"),
                    "delivery_semantics": DELIVERY_SEMANTICS,
                }
        disk_hit = _find_active_by_dedupe_key(inst_cfg, dkey)
        if disk_hit is not None:
            st, env = disk_hit
            exp_dt = parse_iso(env.get("dedupe_expires_at"), tz)
            if exp_dt is None or now_dt < exp_dt:
                _update_dedupe_entry(dedupe, env, now_iso)
                save_dedupe(inst_cfg, dedupe)
                return {
                    "queued": False,
                    "deduplicated": True,
                    "message_id": env["message_id"],
                    "status": st,
                    "delivery_semantics": DELIVERY_SEMANTICS,
                }

        msg_id = f"OB-{now_dt.strftime('%Y%m%dT%H%M%S')}-{uuid.uuid4().hex[:8]}"
        status = "sent" if immediate_sent else "pending"
        env: dict[str, Any] = {
            "version": 1,
            "message_id": msg_id,
            "dedupe_key": dkey,
            "dedupe_expires_at": expires_at,
            "channel": str(channel),
            "target": str(target),
            "payload": dict(payload),
            "delivery_semantics": DELIVERY_SEMANTICS,
            "status": status,
            "created_at": now_iso,
            "updated_at": now_iso,
            "attempts": 1 if immediate_sent else 0,
            "max_attempts": max(1, int(max_attempts)),
            "base_backoff_seconds": max(1, int(base_backoff_seconds)),
            "max_backoff_seconds": max(1, int(max_backoff_seconds)),
            "next_retry_at": now_iso,
            "sending_started_at": None,
            "sent_at": now_iso if immediate_sent else None,
            "last_error": "",
        }
        write_envelope(inst_cfg, status, env)
        _update_dedupe_entry(dedupe, env, now_iso)
        save_dedupe(inst_cfg, dedupe)
        return {
            "queued": True,
            "deduplicated": False,
            "message_id": msg_id,
            "status": status,
            "delivery_semantics": DELIVERY_SEMANTICS,
        }


def mark_acked_by_rel_paths(cfg: InstanceConfig | str, door: str, rel_paths: list[str]) -> int:
    """配合 gate check-inbox --ack，将已发送至对应端的 exchange_out 信封标注 acked_at。"""
    if not rel_paths:
        return 0
    inst_cfg = cfg if isinstance(cfg, InstanceConfig) else load_instance_config(cfg)
    targets = set(rel_paths)
    acked = 0
    now_iso = clock.now(inst_cfg).isoformat()
    with lock.runner_lock(inst_cfg, name=OUTBOX_LOCK_NAME, wait_seconds=5.0):
        sent_dir = state_subdir(inst_cfg, "sent")
        if not sent_dir.is_dir():
            return 0
        for p in sorted(sent_dir.glob("*.json")):
            env = load_envelope(p)
            if not env or env.get("channel") != "exchange_out" or env.get("target") != door:
                continue
            rel = str((env.get("payload") or {}).get("rel_path") or "")
            if rel in targets and not env.get("acked_at"):
                env["acked_at"] = now_iso
                env["updated_at"] = now_iso
                write_envelope(inst_cfg, "sent", env)
                acked += 1
    return acked


def _recover_stale_sending_locked(
    cfg: InstanceConfig,
    now_dt: datetime.datetime,
    stale_seconds: int | None = None,
) -> dict[str, int]:
    tz = ZoneInfo(cfg.timezone)
    now_iso = now_dt.isoformat()
    dedupe_cleaned = reconcile_duplicate_files(cfg)
    sdir = state_subdir(cfg, "sending")
    recovered = failed = corrupted = 0
    if not sdir.is_dir():
        return {"recovered": 0, "failed": 0, "corrupted": 0, "dedupe_cleaned": dedupe_cleaned}

    dedupe = load_dedupe(cfg)
    changed_dedupe = False
    for p in sorted(sdir.glob("*.json")):
        env = load_envelope(p)
        if env is None:
            quarantine_corrupt(cfg, p, "invalid sending envelope json")
            corrupted += 1
            continue
        if stale_seconds is not None:
            started_dt = parse_iso(env.get("sending_started_at") or env.get("updated_at"), tz)
            if started_dt is not None and (now_dt - started_dt).total_seconds() < stale_seconds:
                continue
        attempts = int(env.get("attempts") or 0)
        max_attempts = int(env.get("max_attempts") or DEFAULT_MAX_ATTEMPTS)
        env["sending_started_at"] = None
        env["updated_at"] = now_iso
        if attempts >= max_attempts:
            env["status"] = "failed"
            env["last_error"] = "crash-recovery-max-attempts"
            transition_envelope(cfg, "sending", "failed", env)
            failed += 1
        else:
            env["status"] = "pending"
            env["next_retry_at"] = now_iso
            env["last_error"] = "reclaimed-after-sending-crash"
            transition_envelope(cfg, "sending", "pending", env)
            recovered += 1
        _update_dedupe_entry(dedupe, env, now_iso)
        changed_dedupe = True

    if changed_dedupe:
        save_dedupe(cfg, dedupe)
    return {"recovered": recovered, "failed": failed, "corrupted": corrupted, "dedupe_cleaned": dedupe_cleaned}


def recover_stale_sending(
    cfg: InstanceConfig | str,
    stale_seconds: int | None = None,
    now: datetime.datetime | str | None = None,
) -> dict[str, Any]:
    """在持有 outbox.lock 下回收中断遗留在 sending/ 的信封（明确 at-least-once 重投）。"""
    inst_cfg = cfg if isinstance(cfg, InstanceConfig) else load_instance_config(cfg)
    now_dt = _now_dt(inst_cfg, now)
    with lock.runner_lock(inst_cfg, name=OUTBOX_LOCK_NAME, wait_seconds=5.0):
        ensure_layout(inst_cfg)
        res = _recover_stale_sending_locked(inst_cfg, now_dt, stale_seconds=stale_seconds)
    res["delivery_semantics"] = DELIVERY_SEMANTICS
    return res


def flush(
    cfg: InstanceConfig | str,
    *,
    handlers: dict[str, HandlerFn] | None = None,
    now: datetime.datetime | str | None = None,
    channel_filter: str | None = None,
    stale_seconds: int | None = None,
    limit: int = 50,
) -> dict[str, Any]:
    """执行一轮无常驻拉起投递：先回收孤儿 sending，再按退避窗口逐封投递到期 pending 信封。"""
    inst_cfg = cfg if isinstance(cfg, InstanceConfig) else load_instance_config(cfg)
    now_dt = _now_dt(inst_cfg, now)
    tz = ZoneInfo(inst_cfg.timezone)
    reg: dict[str, HandlerFn] = dict(DEFAULT_HANDLERS)
    if handlers:
        reg.update(handlers)

    sent = failed = retried = deferred_backoff = corrupted = 0
    rec_info = {"recovered": 0, "failed": 0, "corrupted": 0, "dedupe_cleaned": 0}

    with lock.runner_lock(inst_cfg, name=OUTBOX_LOCK_NAME, wait_seconds=5.0):
        ensure_layout(inst_cfg)
        rec_info = _recover_stale_sending_locked(inst_cfg, now_dt, stale_seconds=stale_seconds)
        corrupted += rec_info["corrupted"]
        failed += rec_info["failed"]

        pending_dir = state_subdir(inst_cfg, "pending")
        candidates_list: list[tuple[str, str, Path, dict[str, Any]]] = []
        for p in sorted(pending_dir.glob("*.json")):
            env = load_envelope(p)
            if env is None:
                quarantine_corrupt(inst_cfg, p, "invalid pending envelope json")
                corrupted += 1
                continue
            candidates_list.append((str(env.get("created_at") or ""), str(env["message_id"]), p, env))
        candidates_list.sort(key=lambda x: (x[0], x[1]))

        dedupe = load_dedupe(inst_cfg)
        processed = 0
        for _, _, _, env in candidates_list:
            if processed >= limit:
                break
            if channel_filter and env.get("channel") != channel_filter:
                continue
            next_dt = parse_iso(env.get("next_retry_at"), tz)
            if next_dt is not None and next_dt > now_dt:
                deferred_backoff += 1
                continue

            processed += 1
            now_iso = _now_dt(inst_cfg, now).isoformat()
            attempts = int(env.get("attempts") or 0) + 1
            env["attempts"] = attempts
            env["status"] = "sending"
            env["sending_started_at"] = now_iso
            env["updated_at"] = now_iso
            transition_envelope(inst_cfg, "pending", "sending", env)
            _update_dedupe_entry(dedupe, env, now_iso)
            save_dedupe(inst_cfg, dedupe)
            _stage_hook("after_write_sending")

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
            env["sending_started_at"] = None
            env["updated_at"] = end_iso
            if ok:
                _stage_hook("after_external_send_before_sent")
                env["status"] = "sent"
                env["sent_at"] = end_iso
                env["last_error"] = ""
                transition_envelope(inst_cfg, "sending", "sent", env)
                sent += 1
            else:
                env["last_error"] = str(detail)[:500]
                max_attempts = int(env.get("max_attempts") or DEFAULT_MAX_ATTEMPTS)
                if attempts >= max_attempts:
                    env["status"] = "failed"
                    transition_envelope(inst_cfg, "sending", "failed", env)
                    failed += 1
                else:
                    base_s = int(env.get("base_backoff_seconds") or DEFAULT_BASE_BACKOFF_SECONDS)
                    max_s = int(env.get("max_backoff_seconds") or DEFAULT_MAX_BACKOFF_SECONDS)
                    delay = min(max_s, base_s * (2 ** max(0, attempts - 1)))
                    env["status"] = "pending"
                    env["next_retry_at"] = (end_dt + datetime.timedelta(seconds=delay)).isoformat()
                    transition_envelope(inst_cfg, "sending", "pending", env)
                    retried += 1
            _update_dedupe_entry(dedupe, env, end_iso)
            save_dedupe(inst_cfg, dedupe)

    if rec_info["recovered"] or sent or failed or retried or corrupted:
        ledger.record(
            inst_cfg,
            "job",
            job="outbox",
            date=now_dt.date().isoformat(),
            result="failed" if failed else "ok",
            note=f"sent={sent}; retried={retried}; failed={failed}; recovered={rec_info['recovered']}",
        )

    return {
        "delivery_semantics": DELIVERY_SEMANTICS,
        "recovered": rec_info["recovered"],
        "sent": sent,
        "retried": retried,
        "failed": failed,
        "deferred_backoff": deferred_backoff,
        "corrupted": corrupted,
    }


def retry_failed(
    cfg: InstanceConfig | str,
    message_id: str | None = None,
    extra_attempts: int = 3,
    now: datetime.datetime | str | None = None,
) -> dict[str, Any]:
    """将 failed/ 下的有效信封重新放回 pending/ 排队重试（跳过 corrupt-* 隔离文件）。"""
    inst_cfg = cfg if isinstance(cfg, InstanceConfig) else load_instance_config(cfg)
    now_iso = _now_dt(inst_cfg, now).isoformat()
    requeued = 0
    with lock.runner_lock(inst_cfg, name=OUTBOX_LOCK_NAME, wait_seconds=5.0):
        ensure_layout(inst_cfg)
        fdir = state_subdir(inst_cfg, "failed")
        dedupe = load_dedupe(inst_cfg)
        for p in sorted(fdir.glob("*.json")):
            if p.name.startswith("corrupt-"):
                continue
            env = load_envelope(p)
            if env is None:
                continue
            if message_id and env["message_id"] != message_id:
                continue
            env["status"] = "pending"
            env["max_attempts"] = max(int(env.get("max_attempts") or DEFAULT_MAX_ATTEMPTS), int(env.get("attempts") or 0) + max(1, extra_attempts))
            env["next_retry_at"] = now_iso
            env["updated_at"] = now_iso
            env["last_error"] = "manual-requeue"
            transition_envelope(inst_cfg, "failed", "pending", env)
            _update_dedupe_entry(dedupe, env, now_iso)
            requeued += 1
        if requeued:
            save_dedupe(inst_cfg, dedupe)
    return {"requeued": requeued, "delivery_semantics": DELIVERY_SEMANTICS}


def archive_old(
    cfg: InstanceConfig | str,
    retain_days: int = DEFAULT_ARCHIVE_RETAIN_DAYS,
    include_failed: bool = False,
    now: datetime.datetime | str | None = None,
) -> dict[str, Any]:
    """将超过保留天数的 sent/（及可选 failed/）信封归档至 <state>/outbox/archive/YYYY-MM/ 并清理过期冷却键。"""
    inst_cfg = cfg if isinstance(cfg, InstanceConfig) else load_instance_config(cfg)
    now_dt = _now_dt(inst_cfg, now)
    tz = ZoneInfo(inst_cfg.timezone)
    cutoff = now_dt - datetime.timedelta(days=max(0, int(retain_days)))
    archived = pruned_dedupe = 0

    with lock.runner_lock(inst_cfg, name=OUTBOX_LOCK_NAME, wait_seconds=5.0):
        ensure_layout(inst_cfg)
        targets = ("sent", "failed") if include_failed else ("sent",)
        for st in targets:
            sdir = state_subdir(inst_cfg, st)
            for p in sorted(sdir.glob("*.json")):
                if p.name.startswith("corrupt-"):
                    continue
                env = load_envelope(p)
                if env is None:
                    continue
                ts_dt = parse_iso(env.get("sent_at") or env.get("updated_at") or env.get("created_at"), tz)
                if ts_dt is None or ts_dt > cutoff:
                    continue
                ym = ts_dt.strftime("%Y-%m")
                env["archived_at"] = now_dt.isoformat()
                transition_envelope(inst_cfg, st, "archive", env, archive_ym=ym)
                archived += 1

        dedupe = load_dedupe(inst_cfg)
        keep_dedupe: dict[str, dict[str, Any]] = {}
        for k, entry in dedupe.items():
            exp_dt = parse_iso(entry.get("expires_at"), tz)
            if exp_dt is not None and exp_dt <= now_dt:
                pruned_dedupe += 1
                continue
            keep_dedupe[k] = entry
        if pruned_dedupe:
            save_dedupe(inst_cfg, keep_dedupe)

    return {
        "archived": archived,
        "pruned_dedupe": pruned_dedupe,
        "delivery_semantics": DELIVERY_SEMANTICS,
    }


def list_envelopes(cfg: InstanceConfig | str, status: str | None = None) -> dict[str, Any]:
    """统计并列出 <state>/outbox/ 各状态信封。"""
    inst_cfg = cfg if isinstance(cfg, InstanceConfig) else load_instance_config(cfg)
    counts: dict[str, int] = {st: 0 for st in (*OUTBOX_STATES, "archive")}
    items_out: list[dict[str, Any]] = []
    root = outbox_dir(inst_cfg)
    if root.is_dir():
        for st in OUTBOX_STATES:
            sdir = state_subdir(inst_cfg, st)
            if not sdir.is_dir():
                continue
            files = sorted(sdir.glob("*.json"))
            counts[st] = len(files)
            if status in (None, st):
                for p in files:
                    env = load_envelope(p)
                    items_out.append(env if env is not None else {"file": p.name, "status": st, "corrupted": True})
        adir = state_subdir(inst_cfg, "archive")
        if adir.is_dir():
            afiles = sorted(adir.rglob("*.json"))
            counts["archive"] = len(afiles)
            if status == "archive":
                for p in afiles:
                    env = load_envelope(p)
                    if env is not None:
                        items_out.append(env)
    return {
        "delivery_semantics": DELIVERY_SEMANTICS,
        "counts": counts,
        "envelopes": items_out,
    }
