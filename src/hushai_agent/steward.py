"""常驻管家会话（hushai steward）：模型切换（claude / agy）、状态查询与在线自愈检查。"""

from __future__ import annotations

import configparser
import json
import os
from pathlib import Path
from typing import Any, Callable

from hushai_agent import clock, install
from hushai_agent.config import ConfigError, InstanceConfig
from hushai_agent.fsio import atomic_write
from hushai_agent.heal_checks_ext import _default_cmd_runner
from hushai_agent.paths import (
    get_instance_config_dir,
    get_instance_config_file,
    get_instance_state_dir,
)

VALID_BACKENDS = ("claude", "agy")
DEFAULT_BACKEND = "agy"

def get_backend(cfg: InstanceConfig) -> str:
    """读取 config.ini 中的 [steward] backend（缺省为 agy）。"""
    cfg_file = get_instance_config_file(cfg.name)
    parser = configparser.ConfigParser(interpolation=None)
    if cfg_file.is_file():
        parser.read_string(cfg_file.read_text(encoding="utf-8"))
    val = parser.get("steward", "backend", fallback=DEFAULT_BACKEND).strip().lower()
    return val if val in VALID_BACKENDS else DEFAULT_BACKEND


def set_backend(cfg: InstanceConfig, backend: str) -> str:
    """更新 config.ini 中的 [steward] backend 并记录切换时间，返回切换前的值。"""
    if backend not in VALID_BACKENDS:
        raise ConfigError(f"不支持的管家后端: {backend}（仅支持 claude 或 agy）")
    from hushai_agent.config_edit import update_ini_sections

    prev = get_backend(cfg)
    cfg_dir = get_instance_config_dir(cfg.name)
    cfg_file = get_instance_config_file(cfg.name)
    update_ini_sections(cfg_file, cfg_dir, {"steward": {"backend": backend}})

    state_dir = get_instance_state_dir(cfg.name)
    state_payload = {"backend": backend, "previous_backend": prev, "switched_at": clock.now(cfg).isoformat()}
    atomic_write(state_dir / "steward-state.json", json.dumps(state_payload, ensure_ascii=False) + "\n", [state_dir])
    return prev


def render_steward_kit(cfg: InstanceConfig, out_dir: Path, door: str | None = None) -> list[Path]:
    """生成 start-steward.sh、agy-settings.json 并在 vault 内同步本地钩子配置。"""
    target_door = (door or cfg.steward_door).strip()
    hushai_cmd = install._hushai_command()
    backend = get_backend(cfg)
    log_file = get_instance_state_dir(cfg.name) / "steward.log"

    hooks_obj = {
        "hushai-steward": {
            "SessionStart": [
                {"type": "command", "command": f"{hushai_cmd} gate check-inbox --instance {cfg.name} --door {target_door}"},
                {"type": "command", "command": f"{hushai_cmd} alert active --instance {cfg.name}"},
            ],
            "Stop": [
                {"type": "command", "command": f"{hushai_cmd} capture --instance {cfg.name} --door {target_door}"},
                {"type": "command", "command": f"{hushai_cmd} reconcile --instance {cfg.name}"},
            ],
        }
    }
    from hushai_agent.doorkit import AGY_ASK_RULES, AGY_DENY_RULES

    agy_settings = {
        "trustedWorkspaces": [str(cfg.vault)],
        "permissions": {
            "deny": AGY_DENY_RULES,
            "ask": AGY_ASK_RULES,
        },
    }
    agy_settings_path = out_dir / "agy-settings.json"
    agy_settings_path.write_text(json.dumps(agy_settings, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    claude_settings_path = out_dir / "claude-settings.json"
    if claude_settings_path.is_file():
        local_claude_dir = cfg.vault / ".claude"
        local_claude_dir.mkdir(parents=True, exist_ok=True)
        atomic_write(local_claude_dir / "settings.local.json", claude_settings_path.read_text(encoding="utf-8"), [cfg.vault])

    local_agents_dir = cfg.vault / ".agents"
    local_agents_dir.mkdir(parents=True, exist_ok=True)
    atomic_write(local_agents_dir / "hooks.json", json.dumps(hooks_obj, ensure_ascii=False, indent=2) + "\n", [cfg.vault])

    claude_cmd = str(cfg.adapters.get("claude", {}).get("command") or "claude")
    agy_cmd = str(cfg.adapters.get("agy", {}).get("command") or "agy")
    start_path = out_dir / "start-steward.sh"
    start_text = (
        "#!/bin/sh\n"
        f'cd "{cfg.vault}" || exit 1\n'
        "export HUB_CAPTURE_CHILD=1\n"
        f'BACKEND="{backend}"\n'
        f'echo "[hushai-steward start backend=$BACKEND ts=$(date -Is)]" >> "{log_file}" 2>/dev/null || true\n'
        'if [ "$BACKEND" = "claude" ]; then\n'
        f'  exec {claude_cmd} remote-control --name "hushAi 管家" --spawn same-dir --permission-mode acceptEdits "$@"\n'
        "else\n"
        f'  exec {agy_cmd} --remote-control --mode accept-edits --settings "{agy_settings_path}" "$@"\n'
        "fi\n"
    )
    start_path.write_text(start_text, encoding="utf-8")
    os.chmod(start_path, 0o755)
    return [agy_settings_path, start_path]


def status(cfg: InstanceConfig, hooks: dict[str, Any] | None = None) -> dict[str, Any]:
    """查询管家当前 backend、systemd 单元状态与 tmux 会话存活情况。"""
    run_cmd: Callable[..., tuple[int, str]] = (hooks or {}).get("cmd_runner") or _default_cmd_runner
    unit = f"hushai-{cfg.name}-steward.service"
    session = f"hushai-{cfg.name}"
    backend = get_backend(cfg)
    doorkit_dir = get_instance_config_dir(cfg.name) / "doorkit"

    rc_svc, out_svc = run_cmd(["systemctl", "is-active", unit], timeout=10)
    first_ln = (out_svc.splitlines()[0].strip() if out_svc else "")
    svc_state = "no-systemd" if "System has not been booted" in out_svc else (first_ln or ("active" if rc_svc == 0 else "inactive"))
    if "steward_tmux_alive" in (hooks or {}):
        tmux_alive = bool((hooks or {})["steward_tmux_alive"])
    else:
        rc_tmux, _ = run_cmd(["tmux", "-L", session, "has-session", "-t", f"={session}"], timeout=10)
        tmux_alive = rc_tmux == 0

    return {
        "ok": True,
        "instance": cfg.name,
        "backend": backend,
        "service": unit,
        "service_active": rc_svc == 0 and svc_state == "active",
        "service_state": svc_state,
        "tmux_session": session,
        "tmux_alive": tmux_alive,
        "vault": str(cfg.vault),
        "start_script": str(doorkit_dir / "start-steward.sh"),
    }


def _record_steward_alert(cfg: InstanceConfig, prev: str, curr: str, reason: str) -> str:
    from hushai_agent import alerts

    alerts.record_alert(
        cfg,
        kind="steward_switch",
        level="info",
        message=f"管家切换（{reason}）：切前 {prev}，切后 {curr}",
        source=f"steward:{curr}",
    )
    return str(alerts.alerts_path(cfg))


def use_backend(
    cfg: InstanceConfig,
    backend: str,
    restart: bool = True,
    dry_run: bool = False,
    reason: str = "手动切换",
    hooks: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """切换管家后端（claude / agy）、更新启动脚本、记录运维告警并重启服务。"""
    from hushai_agent import doorkit
    from hushai_agent.heal import operate_systemd_unit

    if backend not in VALID_BACKENDS:
        raise ConfigError(f"不支持的管家后端: {backend}")
    prev = get_backend(cfg)
    alert_file = ""
    if not dry_run:
        prev = set_backend(cfg, backend)
        doorkit_dir = get_instance_config_dir(cfg.name) / "doorkit"
        doorkit_dir.mkdir(parents=True, exist_ok=True)
        doorkit.render_doorkit(cfg, doorkit_dir, adapter="all", door=cfg.steward_door)
        alert_file = _record_steward_alert(cfg, prev, backend, reason)

    unit = f"hushai-{cfg.name}-steward.service"
    restarted, restart_msg = False, "已跳过重启 (--no-restart)"
    if restart:
        operate_systemd_unit(cfg, "reset-failed", unit, dry_run=dry_run, hooks=hooks)
        restarted, restart_msg = operate_systemd_unit(cfg, "restart", unit, dry_run=dry_run, hooks=hooks)

    return {
        "ok": True if not restart else restarted,
        "instance": cfg.name,
        "previous_backend": prev,
        "backend": backend,
        "reason": reason,
        "alert_file": alert_file,
        "service": unit,
        "restarted": restarted,
        "restart_detail": restart_msg,
    }


def restart_steward(cfg: InstanceConfig, dry_run: bool = False, hooks: dict[str, Any] | None = None) -> dict[str, Any]:
    """确保端接入包存在并重启管家服务。"""
    from hushai_agent import doorkit
    from hushai_agent.heal import operate_systemd_unit

    if not dry_run:
        doorkit_dir = get_instance_config_dir(cfg.name) / "doorkit"
        doorkit_dir.mkdir(parents=True, exist_ok=True)
        doorkit.render_doorkit(cfg, doorkit_dir, adapter="all", door=cfg.steward_door)
    unit = f"hushai-{cfg.name}-steward.service"
    operate_systemd_unit(cfg, "reset-failed", unit, dry_run=dry_run, hooks=hooks)
    ok, msg = operate_systemd_unit(cfg, "restart", unit, dry_run=dry_run, hooks=hooks)
    return {"ok": ok, "instance": cfg.name, "backend": get_backend(cfg), "service": unit, "detail": msg}


def check_steward_online(cfg: InstanceConfig, before: str | None, dry_run: bool, hooks: dict[str, Any]) -> dict[str, Any]:
    """8. 管家在线：检查 tmux 会话与单元状态，离线时重启。额度用尽不自动切换（2026-11-17 起彻底关掉）。"""
    from hushai_agent.heal import operate_systemd_unit
    from hushai_agent.heal_checks import _unit_installed

    unit = hooks.get("steward_unit") or f"hushai-{cfg.name}-steward.service"
    has_hook = any(k in hooks for k in ("steward_tmux_alive", "steward_unit"))
    if not has_hook and not _unit_installed(unit, hooks):
        return {"status": "ok", "result": f"{unit} 尚未安装，跳过检查"}

    backend = get_backend(cfg)
    st = status(cfg, hooks=hooks)
    if st["service_active"] and st["tmux_alive"]:
        return {"status": "ok", "result": f"{unit} active，tmux 会话 {st['tmux_session']} 在线（backend={backend}）"}

    prob = f"管家离线（service={st['service_state']}, tmux={'alive' if st['tmux_alive'] else 'dead'}）"
    for attempt in range(1, 3):
        operate_systemd_unit(cfg, "reset-failed", unit, dry_run=dry_run, hooks=hooks)
        ok, msg = operate_systemd_unit(cfg, "restart", unit, dry_run=dry_run, hooks=hooks)
        if not ok:
            return {"status": "failed", "problem": f"{prob} 且重启失败（{msg}）", "action": f"systemctl restart {unit}", "suggestion": f"人工检查 {unit}", "tries": attempt}
        if dry_run:
            return {"status": "healed", "problem": prob, "action": f"systemctl restart {unit}", "result": "演练模式已模拟恢复管家会话", "tries": attempt}
        st_now = status(cfg, hooks=hooks)
        if st_now["service_active"] and st_now["tmux_alive"]:
            return {"status": "healed", "problem": prob, "action": f"systemctl reset-failed & restart {unit}（第 {attempt} 次）", "result": f"tmux 会话 {st_now['tmux_session']} 已重新拉起", "tries": attempt}

    return {"status": "failed", "problem": f"{prob}，连续 2 次重启后仍未恢复", "action": f"systemctl restart {unit}（2 次）", "suggestion": f"人工运行 hushai steward restart --instance {cfg.name}", "tries": 2}
