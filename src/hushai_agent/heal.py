"""静默自愈健康检查（hushai heal）：白名单护栏、检查项注册表、收件单行自愈记录与 24h 去重上报。"""

from __future__ import annotations

import datetime
import json
import re
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo

from hushai_agent import clock, ledger
from hushai_agent.config import InstanceConfig, load_instance_config
from hushai_agent.fsio import append_line_with_header, atomic_write
from hushai_agent.heal_checks import (
    _default_cmd_runner,
    _parse_iso,
    check_memory_write,
    check_model_available,
    check_state_and_disk,
    check_sync_ok,
    check_timers_on_time,
    check_vault_rw,
    check_watch_alive,
)
from hushai_agent.inboxfmt import format_inbox_line
from hushai_agent.paths import get_instance_state_dir
from hushai_agent.steward import check_steward_online
from hushai_agent.todaysel import DEGRADED_QUOTE, TODAY_HEADER_RE, TODAY_QUOTE

ALERT_COOLDOWN_SECONDS = 24 * 3600
ALLOWED_UNIT_OPS = frozenset({"start", "restart", "reset-failed"})

CheckFn = Callable[[InstanceConfig, str | None, bool, dict[str, Any]], dict[str, Any]]

# 检查项注册表：每项一个函数，以后新增检查项只需在此表加一行
CHECKS: tuple[tuple[str, str, CheckFn], ...] = (
    ("vault_rw", "vault 可读写", check_vault_rw),
    ("state_and_disk", "状态目录与磁盘", check_state_and_disk),
    ("timers_on_time", "定时任务按时跑", check_timers_on_time),
    ("watch_alive", "监听单元活着", check_watch_alive),
    ("memory_write", "记忆写入", check_memory_write),
    ("model_available", "模型可用", check_model_available),
    ("sync_ok", "同步", check_sync_ok),
    ("steward_online", "管家在线", check_steward_online),
)


def is_whitelisted_unit(instance: str, unit_name: str) -> bool:
    """校验单元名是否在自愈白名单内：仅限 hushai-<实例>-* 以及 rclone-*.service / mnt-onedrive-*.mount。"""
    name = (unit_name or "").strip()
    if not name or "/" in name or "\\" in name or ".." in name:
        return False
    hushai_pat = rf"^hushai-{re.escape(instance)}-[a-z0-9-]+(\.(service|timer|path|mount))?$"
    if re.match(hushai_pat, name):
        return True
    if re.match(r"^rclone-[a-z0-9-]+(\.service)?$", name):
        return True
    if re.match(r"^mnt-onedrive(-[a-zA-Z0-9_\-\.\\]+)?\.mount$", name):
        return True
    return False


def operate_systemd_unit(
    cfg: InstanceConfig,
    op: str,
    unit_name: str,
    dry_run: bool = False,
    hooks: dict[str, Any] | None = None,
) -> tuple[bool, str]:
    """对白名单内的 systemd 单元执行确定性恢复操作；白名单外一律拒绝。"""
    if not is_whitelisted_unit(cfg.name, unit_name):
        return False, f"单元不在自愈白名单内，拒绝操作: {unit_name}"
    if op not in ALLOWED_UNIT_OPS:
        return False, f"不允许的单元操作: {op}"
    if dry_run:
        return True, f"dry-run: systemctl {op} {unit_name}"

    run_cmd = (hooks or {}).get("cmd_runner") or _default_cmd_runner
    rc, out = run_cmd(["systemctl", op, unit_name], timeout=15)
    if rc == 0:
        return True, f"systemctl {op} {unit_name} 成功"
    return False, f"systemctl {op} {unit_name} 失败 (rc={rc}): {out[:100]}"


def _write_heal_inbox(cfg: InstanceConfig, item: dict[str, Any]) -> str:
    """已自愈时向 hub/inbox/<日期>-heal.md 追加一行：自愈：X，动作 Y，结果 Z。"""
    now_dt = clock.now(cfg)
    date_str = now_dt.strftime("%Y-%m-%d")
    time_str = now_dt.strftime("%H:%M")
    domain = "AI" if "AI" in cfg.domains else cfg.domains[0]
    did = f"自愈：{item.get('problem', item['name'])}，动作 {item.get('action', '-')}，结果 {item.get('result', '已恢复')}"
    line = format_inbox_line(
        time_str=time_str,
        domain=domain,
        did=did,
        lang=cfg.lang,
        allowed_domains=cfg.allowed_domains,
    )
    inbox_file = cfg.vault / "hub" / "inbox" / f"{date_str}-heal.md"
    header = f"# {date_str} heal"
    append_line_with_header(inbox_file, header, line, [cfg.vault])
    return str(inbox_file)


def _prepend_today_alert(cfg: InstanceConfig, alert_line: str) -> None:
    """在 hub/today.md 顶部（表头引用之后、待办列表之前）插入一条系统告警。"""
    today_file = cfg.vault / "hub" / "today.md"
    today_str = clock.today(cfg).isoformat()
    if not today_file.is_file():
        content = f"# 今日待办 {today_str}\n{TODAY_QUOTE}\n{alert_line}\n"
        atomic_write(today_file, content, [cfg.vault])
        return

    lines = today_file.read_text(encoding="utf-8").splitlines()
    if alert_line in [ln.strip() for ln in lines]:
        return

    insert_idx = 0
    for idx, raw in enumerate(lines):
        s = raw.strip()
        if TODAY_HEADER_RE.match(s) or s in (TODAY_QUOTE, DEGRADED_QUOTE) or s.startswith(">"):
            insert_idx = idx + 1
        else:
            break
    new_lines = lines[:insert_idx] + [alert_line] + lines[insert_idx:]
    atomic_write(today_file, "\n".join(new_lines) + "\n", [cfg.vault])


def _report_needs_human(cfg: InstanceConfig, item: dict[str, Any]) -> str | None:
    """修 2 次仍失败或不在白名单：在 today.md 顶上加一条「系统：X 坏了，建议 Y」，同一问题 24 小时内只写一次。"""
    state_dir = get_instance_state_dir(cfg.name)
    alerts_file = state_dir / "heal-alerts.json"
    now_dt = clock.now(cfg)
    tz = ZoneInfo(cfg.timezone)

    alerts: dict[str, str] = {}
    if alerts_file.is_file():
        try:
            loaded = json.loads(alerts_file.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                alerts = {str(k): str(v) for k, v in loaded.items()}
        except (OSError, json.JSONDecodeError):
            pass

    key = f"{item['id']}:{item.get('problem', '')}"
    prev_dt = _parse_iso(alerts.get(key), tz)
    if prev_dt is not None and 0 <= (now_dt - prev_dt).total_seconds() < ALERT_COOLDOWN_SECONDS:
        return None

    x_part = f"{item['name']}（{item.get('problem', '异常')}）"
    y_part = item.get("suggestion") or "人工排查日志与服务状态"
    alert_line = f"- 系统：{x_part}坏了，建议{y_part}"
    _prepend_today_alert(cfg, alert_line)

    alerts[key] = now_dt.isoformat()
    atomic_write(alerts_file, json.dumps(alerts, ensure_ascii=False, indent=2) + "\n", [state_dir])
    return alert_line


def run_heal(
    cfg: InstanceConfig | str,
    before: str | None = None,
    dry_run: bool = False,
    hooks: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """执行静默自愈健康检查主流程；同一实例同时只跑一个（heal.lock，与 runner.lock 分开，免得补跑汇总时自锁）。"""
    from hushai_agent import lock

    inst_cfg = cfg if isinstance(cfg, InstanceConfig) else load_instance_config(cfg)
    if before is not None and before not in ("digest", "weekly"):
        raise ValueError(f"--before 仅支持 digest 或 weekly: {before}")
    try:
        with lock.runner_lock(inst_cfg, name="heal.lock"):
            res = _run_heal_locked(inst_cfg, before, dry_run, hooks)
    except lock.LockBusy:
        return {"ok": True, "instance": inst_cfg.name, "skipped": "另一次自检正在运行", "checks": []}

    if not dry_run:
        rec_info = _recover_abandoned_state(inst_cfg)
        res.update(rec_info)
    return res


def _recover_abandoned_state(inst_cfg: InstanceConfig) -> dict[str, int]:
    """无阻塞尝试接管遗留的半程崩溃检查点或锁冲突期间积压的事件。"""
    from hushai_agent import checkpoint, lock
    from hushai_agent.config_ext import load_ext

    summary = {"checkpoints_recovered": 0, "routes_recovered": 0}
    if not checkpoint.has_pending(inst_cfg):
        return summary
    try:
        with lock.runner_lock(inst_cfg, name="runner.lock", wait_seconds=0.0):
            rec = checkpoint.recover_pending(inst_cfg)
            summary["checkpoints_recovered"] = int(rec.get("recovered") or 0)
            dirty = lock.clear_dirty(inst_cfg, "mechanical")
            if rec.get("recovered") or dirty:
                from hushai_agent.reconcile import _run_mechanical_locked

                _run_mechanical_locked(inst_cfg)
    except (lock.LockBusy, Exception):
        pass

    ext = load_ext(inst_cfg)
    try:
        with lock.runner_lock(inst_cfg, name="route.lock", wait_seconds=0.0):
            rec_r = checkpoint.recover_pending_routes(inst_cfg)
            summary["routes_recovered"] = int(rec_r or 0)
            dirty_r = lock.clear_dirty(inst_cfg, "routing")
            if ext.routing_enabled and (rec_r or dirty_r):
                from hushai_agent.routing import _run_routing_locked

                _run_routing_locked(inst_cfg, ext)
    except (lock.LockBusy, Exception):
        pass
    return summary


def _run_heal_locked(
    inst_cfg: InstanceConfig,
    before: str | None,
    dry_run: bool,
    hooks: dict[str, Any] | None,
) -> dict[str, Any]:

    env_hooks = dict(hooks or {})
    results: list[dict[str, Any]] = []
    healed_files: list[str] = []
    alerts_written: list[str] = []

    for check_id, check_name, check_fn in CHECKS:
        try:
            raw_res = check_fn(inst_cfg, before, dry_run, env_hooks)
        except Exception as exc:
            raw_res = {
                "status": "failed",
                "problem": f"检查执行异常: {exc}",
                "suggestion": f"人工核查 {check_id}",
            }
        entry = {
            "id": check_id,
            "name": check_name,
            "status": raw_res.get("status", "ok"),
            "problem": raw_res.get("problem", ""),
            "action": raw_res.get("action", ""),
            "result": raw_res.get("result", ""),
            "suggestion": raw_res.get("suggestion", ""),
            "tries": int(raw_res.get("tries", 0)),
        }
        results.append(entry)

        if entry["status"] == "healed" and not dry_run:
            healed_files.append(_write_heal_inbox(inst_cfg, entry))
        elif entry["status"] == "failed" and not dry_run:
            alert_txt = _report_needs_human(inst_cfg, entry)
            if alert_txt:
                alerts_written.append(alert_txt)

    has_failed = any(r["status"] == "failed" for r in results)
    has_healed = any(r["status"] == "healed" for r in results)
    job_result = "needs_human" if has_failed else "ok"

    notes: list[str] = []
    if before:
        notes.append(f"before={before}")
    if has_healed:
        healed_ids = ",".join(r["id"] for r in results if r["status"] == "healed")
        notes.append(f"healed={healed_ids}")
    if has_failed:
        failed_ids = ",".join(r["id"] for r in results if r["status"] == "failed")
        notes.append(f"failed={failed_ids}")

    today_str = clock.today(inst_cfg).isoformat()
    ledger.record(
        inst_cfg,
        "job",
        job="heal",
        date=today_str,
        dry_run=dry_run,
        result=job_result,
        note="; ".join(notes),
    )

    return {
        "ok": not has_failed,
        "result": job_result,
        "instance": inst_cfg.name,
        "date": today_str,
        "before": before,
        "dry_run": dry_run,
        "checks": results,
        "healed_inbox_files": healed_files,
        "alerts_written": alerts_written,
    }
