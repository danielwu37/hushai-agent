"""端接入包（doorkit）与一站式安装向导（install wizard）。"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from hushai_agent import doctor, install
from hushai_agent.config import InstanceConfig, load_instance_config
from hushai_agent.fsio import atomic_write
from hushai_agent.paths import get_instance_config_dir, get_instance_config_file
from hushai_agent.vault import init_instance

DENY_RULES = [
    "Bash(rm -rf /*)",
    "Bash(rm -rf ~*)",
    "Bash(git push --force*)",
    "Bash(mkfs*)",
    "Bash(dd if=*)",
]


def _render_claude_kit(cfg: InstanceConfig, out_dir: Path, door: str, hushai_cmd: str) -> list[Path]:
    """生成 Claude 端接入包（采用 --setting-sources project,local --settings 同账号零冲突方案）。"""
    settings = {
        "permissions": {
            "deny": DENY_RULES,
        },
        "hooks": {
            "SessionStart": [
                {
                    "matcher": "",
                    "hooks": [
                        {
                            "type": "command",
                            "command": f"{hushai_cmd} gate check-inbox --instance {cfg.name} --door {door}",
                        }
                    ],
                }
            ],
            "Stop": [
                {
                    "matcher": "",
                    "hooks": [
                        {
                            "type": "command",
                            "command": f"{hushai_cmd} reconcile --instance {cfg.name}",
                        }
                    ],
                }
            ],
        },
    }
    settings_path = out_dir / "claude-settings.json"
    settings_path.write_text(json.dumps(settings, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    script_path = out_dir / "start-claude-steward.sh"
    script_text = (
        "#!/bin/sh\n"
        "# 启动本实例的 Claude 管家会话（同账号零冲突隔离：跳过 ~/.claude 全局规则与钩子，共用登录凭据）\n"
        f'cd "{cfg.vault}" || exit 1\n'
        f'exec claude --setting-sources project,local --settings "{settings_path}" "$@"\n'
    )
    script_path.write_text(script_text, encoding="utf-8")
    os.chmod(script_path, 0o755)
    return [settings_path, script_path]


def _render_agy_kit(cfg: InstanceConfig, out_dir: Path, door: str, hushai_cmd: str) -> list[Path]:
    """生成 Antigravity CLI（agy）端接入包。"""
    hooks = {
        "hooks": {
            "SessionStart": [
                {
                    "command": f"{hushai_cmd} gate check-inbox --instance {cfg.name} --door {door}",
                }
            ],
            "Stop": [
                {
                    "command": f"{hushai_cmd} reconcile --instance {cfg.name}",
                }
            ],
            "PreToolUse": [
                {
                    "matcher": "run_command",
                    "policy": "force_ask",
                    "description": "高风险命令（删除、外发、付款、改账号）必须经使用者确认",
                }
            ],
        }
    }
    hooks_path = out_dir / "agy-hooks.json"
    hooks_path.write_text(json.dumps(hooks, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    script_path = out_dir / "start-agy-steward.sh"
    script_text = (
        "#!/bin/sh\n"
        "# 启动本实例的 Antigravity CLI 管家会话\n"
        f'cd "{cfg.vault}" || exit 1\n'
        'exec agy "$@"\n'
    )
    script_path.write_text(script_text, encoding="utf-8")
    os.chmod(script_path, 0o755)
    return [hooks_path, script_path]


def render_doorkit(
    cfg: InstanceConfig,
    out_dir: str | Path,
    adapter: str = "all",
    door: str | None = None,
) -> list[Path]:
    """把端接入包渲染到使用者指定的已存在目录。"""
    if adapter not in ("claude", "agy", "all"):
        raise ValueError(f"不支持的端接入包类型: {adapter}")
    out = Path(out_dir).expanduser().resolve()
    if not out.is_dir():
        raise ValueError(f"输出目录不存在: {out_dir}")
    target_door = (door or cfg.steward_door).strip()
    if target_door not in cfg.known_doors:
        raise ValueError(f"未知的端: {target_door}")

    hushai_cmd = install._hushai_command()
    written: list[Path] = []
    if adapter in ("claude", "all"):
        written.extend(_render_claude_kit(cfg, out, target_door, hushai_cmd))
    if adapter in ("agy", "all"):
        written.extend(_render_agy_kit(cfg, out, target_door, hushai_cmd))
    return written


def run_wizard(
    instance: str,
    vault: str | Path,
    timezone: str = "Asia/Shanghai",
    lang: str = "zh",
    steward: str = "claude",
    agy_light: str = "gemini-3.8-flash-low",
    agy_heavy: str = "gemini-4-argon-high",
    claude_light: str = "haiku",
    claude_heavy: str = "sonnet",
    remote: str = "",
    no_git: bool = False,
) -> dict[str, Any]:
    """一站式安装向导：初始化实例、写入双适配项与回退链配置、生成单元与接入包、执行体检。"""
    init_res = init_instance(
        instance=instance,
        vault=vault,
        timezone=timezone,
        lang=lang,
        no_git=no_git,
    )
    cfg_dir = get_instance_config_dir(instance)
    cfg_file = get_instance_config_file(instance)
    text = cfg_file.read_text(encoding="utf-8")

    adapters_block = (
        "[adapters]\n"
        "agy.type = agy\n"
        "agy.command = agy\n"
        f"agy.light = {agy_light}\n"
        f"agy.heavy = {agy_heavy}\n"
        "claude.type = claude\n"
        "claude.command = claude\n"
        f"claude.light = {claude_light}\n"
        f"claude.heavy = {claude_heavy}"
    )
    jobs_block = (
        "[jobs]\n"
        f"digest = {'agy:light, claude:light' if steward == 'agy' else 'agy:light, claude:light'}\n"
        "weekly = agy:heavy, claude:heavy\n"
        "dispatch = agy"
    )
    text = text.replace("[adapters]", adapters_block, 1)
    text = text.replace("[jobs]\ndigest =\nweekly =\ndispatch =", jobs_block, 1)
    if "[sync]" not in text:
        text = text.rstrip("\n") + f"\n\n[sync]\ntool = rclone\nremote = {remote}\ntimeout = 60\n"
    atomic_write(cfg_file, text, [cfg_dir])

    cfg = load_instance_config(instance)
    units_dir = cfg_dir / "units"
    doorkit_dir = cfg_dir / "doorkit"
    units_dir.mkdir(parents=True, exist_ok=True)
    doorkit_dir.mkdir(parents=True, exist_ok=True)

    units = install.render_units(cfg, units_dir)
    kit = render_doorkit(cfg, doorkit_dir, adapter="all", door=cfg.steward_door)
    doc = doctor.run_doctor(instance)

    return {
        "ok": doc["ok"],
        "instance": instance,
        "vault": str(cfg.vault),
        "created_files": init_res.created_files,
        "units_dir": str(units_dir),
        "units": [str(p) for p in units],
        "doorkit_dir": str(doorkit_dir),
        "doorkit": [str(p) for p in kit],
        "doctor": doc,
        "next_steps": install.next_steps(cfg, units),
    }
