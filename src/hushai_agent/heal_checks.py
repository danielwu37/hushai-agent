"""hushai heal 的第 1–4 项检查：vault 探针、状态目录与磁盘、定时任务按时跑、监听单元存活。"""

from __future__ import annotations

import datetime
import os
import shutil
import sys
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo

from hushai_agent import clock, digest, ledger, weekly
from hushai_agent.adapters import proc
from hushai_agent.config import InstanceConfig
from hushai_agent.heal_checks_ext import (
    _default_cmd_runner,
    _parse_iso,
    check_memory_write,
    check_model_available,
    check_sync_ok,
)
from hushai_agent.paths import get_instance_state_dir

__all__ = [
    "_default_cmd_runner",
    "_parse_iso",
    "_probe_vault_subprocess",
    "check_vault_rw",
    "check_state_and_disk",
    "check_timers_on_time",
    "check_watch_alive",
    "check_memory_write",
    "check_model_available",
    "check_sync_ok",
]

DISK_MIN_FREE_BYTES = 500 * 1024 * 1024
PROBE_TIMEOUT_SEC = 10


def _probe_vault_subprocess(vault: Path, hooks: dict[str, Any]) -> tuple[bool, str]:
    """在带 10 秒超时的子进程中写读删 <vault>/.heal-probe。"""
    if "probe_fn" in hooks:
        return hooks["probe_fn"](vault)
    probe_file = vault / ".heal-probe"
    code = (
        "import pathlib, sys; p = pathlib.Path(sys.argv[1]); "
        "p.write_text('ok', encoding='utf-8'); "
        "assert p.read_text(encoding='utf-8') == 'ok'; "
        "p.unlink()"
    )
    rc, _, err, _ = proc.run([sys.executable, "-c", code, str(probe_file)], None, vault.parent, PROBE_TIMEOUT_SEC)
    if rc == 0:
        return True, "探针写读删正常"
    return False, (err or f"探针子进程失败 (rc={rc})").strip()[:120]


def _find_rclone_mount(vault: Path, hooks: dict[str, Any]) -> tuple[str, str] | None:
    """检查 vault 是否位于 rclone/fuse 挂载点下，返回 (挂载点路径, 挂载单元名) 或 None。"""
    if "rclone_mount" in hooks:
        return hooks["rclone_mount"]
    mounts_file = Path("/proc/mounts")
    if not mounts_file.is_file():
        return None
    vault_str = os.path.realpath(os.fspath(vault))
    best_mp = ""
    for line in mounts_file.read_text(encoding="utf-8", errors="ignore").splitlines():
        parts = line.split()
        if len(parts) < 3:
            continue
        mp, fstype = parts[1], parts[2]
        if ("rclone" in fstype or "fuse" in fstype or mp.startswith("/mnt/onedrive")) and (
            vault_str == mp or vault_str.startswith(mp.rstrip("/") + "/")
        ):
            if len(mp) > len(best_mp):
                best_mp = mp
    if not best_mp:
        return None
    slug = best_mp.strip("/").replace("/", "-")
    return best_mp, f"{slug}.mount"


def check_vault_rw(cfg: InstanceConfig, before: str | None, dry_run: bool, hooks: dict[str, Any]) -> dict[str, Any]:
    """1. vault 可读写：10 秒子进程探针；挂载卡死时懒卸载并重启挂载单元（最多 2 次）。"""
    from hushai_agent.heal import operate_systemd_unit

    ok, detail = _probe_vault_subprocess(cfg.vault, hooks)
    if ok:
        return {"status": "ok", "result": detail}

    mount_info = _find_rclone_mount(cfg.vault, hooks)
    if mount_info is None:
        return {
            "status": "failed",
            "problem": f"本地目录不可读写（{detail}）",
            "suggestion": f"检查 {cfg.vault} 目录权限与文件系统状态",
        }

    mountpoint, mount_unit = mount_info
    run_cmd: Callable[..., tuple[int, str]] = hooks.get("cmd_runner") or _default_cmd_runner
    actions: list[str] = []
    for attempt in range(1, 3):
        if not dry_run:
            run_cmd(["fusermount", "-uz", mountpoint], timeout=10)
        u_ok, u_msg = operate_systemd_unit(cfg, "restart", mount_unit, dry_run=dry_run, hooks=hooks)
        actions.append(f"fusermount -uz {mountpoint} && systemctl restart {mount_unit}")
        if not u_ok:
            return {
                "status": "failed",
                "problem": f"挂载点 {mountpoint} 卡死且拒绝操作单元（{u_msg}）",
                "action": "；".join(actions),
                "suggestion": f"人工核查挂载单元 {mount_unit}",
                "tries": attempt,
            }
        ok_now, _ = _probe_vault_subprocess(cfg.vault, hooks)
        if ok_now or dry_run:
            return {
                "status": "healed",
                "problem": f"vault 探针失败（{detail}）",
                "action": f"懒卸载 {mountpoint} 并重启 {mount_unit}（第 {attempt} 次）",
                "result": "探针恢复可读写" if not dry_run else "演练跳过实际重挂",
                "tries": attempt,
            }
    return {
        "status": "failed",
        "problem": f"挂载点 {mountpoint} 连续 2 次重挂后仍不可读写",
        "action": "；".join(actions),
        "suggestion": f"人工检查 rclone 凭据与挂载服务 {mount_unit}",
        "tries": 2,
    }


def check_state_and_disk(cfg: InstanceConfig, before: str | None, dry_run: bool, hooks: dict[str, Any]) -> dict[str, Any]:
    """2. 状态目录与磁盘：状态目录可写，且 vault 所在分区剩余空间 ≥ 500 MB。"""
    state_dir = get_instance_state_dir(cfg.name)
    if not state_dir.is_dir() or not os.access(os.fspath(state_dir), os.W_OK):
        return {"status": "failed", "problem": f"状态目录不可写（{state_dir}）", "suggestion": f"修复目录权限 {state_dir}"}
    free_bytes = hooks.get("disk_free_bytes")
    if free_bytes is None:
        try:
            free_bytes = shutil.disk_usage(os.fspath(cfg.vault)).free
        except OSError as exc:
            return {"status": "failed", "problem": f"无法读取磁盘空间（{exc}）", "suggestion": "检查磁盘挂载"}
    free_mb = int(free_bytes) // (1024 * 1024)
    if int(free_bytes) < DISK_MIN_FREE_BYTES:
        return {"status": "failed", "problem": f"磁盘剩余空间仅 {free_mb} MB（低于 500 MB）", "suggestion": "释放磁盘空间"}
    return {"status": "ok", "result": f"状态目录可写，磁盘剩余 {free_mb} MB"}


def _unit_installed(unit_name: str, hooks: dict[str, Any]) -> bool:
    if "installed_units" in hooks:
        return unit_name in hooks["installed_units"]
    if "cmd_runner" in hooks:
        return True
    return (Path("/etc/systemd/system") / unit_name).exists()


def check_timers_on_time(
    cfg: InstanceConfig, before: str | None, dry_run: bool, hooks: dict[str, Any]
) -> dict[str, Any]:
    """3. 定时任务按时跑：检查 digest(>26h)/weekly(>8d) 漏跑与单元 failed 状态。"""
    from hushai_agent.heal import operate_systemd_unit

    run_cmd: Callable[..., tuple[int, str]] = hooks.get("cmd_runner") or _default_cmd_runner
    healed_msgs: list[str] = []
    problems: list[str] = []

    for job_name in ("digest", "weekly"):
        svc = f"hushai-{cfg.name}-{job_name}.service"
        tmr = f"hushai-{cfg.name}-{job_name}.timer"
        if _unit_installed(svc, hooks):
            rc_s, out_s = run_cmd(["systemctl", "is-failed", svc], timeout=10)
            if rc_s == 0 or out_s.strip() == "failed":
                problems.append(f"{svc} 处于 failed")
                operate_systemd_unit(cfg, "reset-failed", svc, dry_run=dry_run, hooks=hooks)
                ok_t, msg_t = operate_systemd_unit(cfg, "restart", tmr, dry_run=dry_run, hooks=hooks)
                if not ok_t:
                    return {"status": "failed", "problem": msg_t, "suggestion": f"人工检查 {svc}"}
                healed_msgs.append(f"reset-failed {svc} 并重启 {tmr}")

    rows = ledger.read(cfg)
    now_dt = clock.now(cfg)
    tz = ZoneInfo(cfg.timezone)
    parsed = [(r, _parse_iso(r.get("ts"), tz)) for r in rows]
    first_dt = next((dt for _, dt in parsed if dt is not None), None)

    for job_name, max_age in (("digest", datetime.timedelta(hours=26)), ("weekly", datetime.timedelta(days=8))):
        if before == job_name:
            continue
        job_dts = [
            dt for r, dt in parsed
            if r.get("kind") == "job" and r.get("job") == job_name and r.get("result") in ("ok", "degraded") and dt
        ]
        last_ok = max(job_dts) if job_dts else None
        overdue = (last_ok is not None and (now_dt - last_ok) > max_age) or (
            last_ok is None and first_dt is not None and (now_dt - first_dt) > max_age
        )
        if overdue:
            problems.append(f"{job_name} 超过周期未成功运行")
            if not dry_run:
                try:
                    today_d = clock.today(cfg)
                    (digest.run_digest if job_name == "digest" else weekly.run_weekly)(cfg, today_d)
                    healed_msgs.append(f"补跑 {job_name} 成功")
                except Exception as exc:
                    return {
                        "status": "failed",
                        "problem": f"{job_name} 漏跑且补跑失败（{exc}）",
                        "action": f"尝试补跑 {job_name}",
                        "suggestion": f"人工运行 hushai {job_name} --instance {cfg.name}",
                    }
            else:
                healed_msgs.append(f"演练补跑 {job_name}")

    if healed_msgs:
        return {
            "status": "healed",
            "problem": "；".join(problems),
            "action": "；".join(healed_msgs),
            "result": "定时任务状态已恢复",
            "tries": 1,
        }
    return {"status": "ok", "result": "定时任务按时运行且无 failed 单元"}


def check_watch_alive(
    cfg: InstanceConfig, before: str | None, dry_run: bool, hooks: dict[str, Any]
) -> dict[str, Any]:
    """4. 监听单元活着：检查 hushai-<实例>-watch.path 是否 active，否则 reset-failed 并 start（最多 2 次）。"""
    from hushai_agent.heal import operate_systemd_unit

    unit = hooks.get("watch_unit") or f"hushai-{cfg.name}-watch.path"
    if not _unit_installed(unit, hooks):
        return {"status": "ok", "result": f"{unit} 尚未安装，跳过检查"}

    run_cmd: Callable[..., tuple[int, str]] = hooks.get("cmd_runner") or _default_cmd_runner
    rc, state = run_cmd(["systemctl", "is-active", unit], timeout=10)
    if rc == 0 and state.strip() == "active":
        return {"status": "ok", "result": f"{unit} active"}

    orig_state = state.strip() or "inactive"
    for attempt in range(1, 3):
        ok1, msg1 = operate_systemd_unit(cfg, "reset-failed", unit, dry_run=dry_run, hooks=hooks)
        ok2, msg2 = operate_systemd_unit(cfg, "start", unit, dry_run=dry_run, hooks=hooks)
        if not ok1 or not ok2:
            reason = msg1 if not ok1 else msg2
            return {
                "status": "failed",
                "problem": f"{unit} 状态为 {orig_state} 且无法启动（{reason}）",
                "action": f"尝试启动 {unit}",
                "suggestion": f"人工核查 systemctl status {unit}",
                "tries": attempt,
            }
        if dry_run:
            return {
                "status": "healed",
                "problem": f"{unit} 处于 {orig_state}",
                "action": f"systemctl reset-failed & start {unit}",
                "result": "演练模式已模拟恢复",
                "tries": attempt,
            }
        rc_now, state_now = run_cmd(["systemctl", "is-active", unit], timeout=10)
        if rc_now == 0 and state_now.strip() == "active":
            return {
                "status": "healed",
                "problem": f"{unit} 停止（状态 {orig_state}）",
                "action": f"systemctl reset-failed 并 start {unit}（第 {attempt} 次）",
                "result": f"{unit} 已恢复 active",
                "tries": attempt,
            }

    return {
        "status": "failed",
        "problem": f"{unit} 连续 2 次启动后仍处于非 active 状态",
        "action": f"systemctl reset-failed & start {unit}（2 次）",
        "suggestion": f"人工运行 systemctl status {unit} 排查",
        "tries": 2,
    }
