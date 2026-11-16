"""按归属单向同步（hushai sync）：只调网盘命令行工具，不碰挂载点；保护服务端端文件并在时间预算内触发事件处理。"""

from __future__ import annotations

import os
import shlex
import time
from typing import Any

from hushai_agent import clock, ledger, on_event
from hushai_agent.adapters import proc
from hushai_agent.config import InstanceConfig
from hushai_agent.config_ext import ExtConfig, load_ext

DEFAULT_SYNC_TOTAL_BUDGET = 480.0

PUSH_INCLUDES = (
    "/AGENTS.md",
    "/AGENTS.local.md",
    "/profile.md",
    "/status.md",
    "/status-index.md",
    "/open-items.md",
    "/today.md",
    "/insights.md",
    "/decisions.md",
    "/lessons.md",
    "/candidates.md",
    "/index.md",
    "/weekly.md",
    "/notes/**",
    "/sources/**",
    "/people/**",
    "/projects/**",
    "/daily/**",
    "/reports/**",
    "/exchange/out/**",
)


def _hub_local_only_excludes(cfg: InstanceConfig) -> list[str]:
    """取出落在 hub/ 下的 local_only 与本地隔离领域相关路径，转成 rclone --exclude 参数。"""
    args: list[str] = ["--exclude", ".git/**", "--exclude", "/domains/**"]
    for rel in cfg.local_only:
        norm = rel.strip().strip("/")
        if norm == "hub":
            args += ["--exclude", "/**"]
        elif norm.startswith("hub/"):
            sub = norm[len("hub/") :]
            args += ["--exclude", f"/{sub}", "--exclude", f"/{sub}/**"]
    return args


def _server_doors(cfg: InstanceConfig, ext: ExtConfig) -> list[str]:
    doors = [cfg.steward_door, *ext.sync_server_doors]
    return list(dict.fromkeys(d for d in doors if d))


def _build_push_argv(
    cfg: InstanceConfig,
    tool_argv: list[str],
    remote_hub: str,
    dry_run: bool,
    ext: ExtConfig | None = None,
) -> list[str]:
    """构建 push（服务器 hub -> 远端 hub）命令参数，推送衍生视图及服务端产生收件。"""
    ext_cfg = ext or load_ext(cfg)
    argv = [*tool_argv, "copy", str(cfg.vault / "hub"), remote_hub]
    if dry_run:
        argv.append("--dry-run")
    argv += _hub_local_only_excludes(cfg)
    for inc in PUSH_INCLUDES:
        argv += ["--include", inc]
    for door in _server_doors(cfg, ext_cfg):
        argv += [
            "--include",
            f"/inbox/*-{door}.md",
            "--include",
            f"/inbox/*-{door}-auto.md",
        ]
    return argv


def _build_pull_argv(
    cfg: InstanceConfig,
    tool_argv: list[str],
    remote_hub: str,
    dry_run: bool,
    ext: ExtConfig | None = None,
) -> list[str]:
    """构建 pull（远端 hub -> 服务器 hub）命令参数：排除服务端写入端（或按 pull_doors 白名单拉取），并用 --update 防旧覆新。"""
    ext_cfg = ext or load_ext(cfg)
    argv = [*tool_argv, "copy", remote_hub, str(cfg.vault / "hub"), "--update"]
    if dry_run:
        argv.append("--dry-run")
    argv += _hub_local_only_excludes(cfg)
    for door in _server_doors(cfg, ext_cfg):
        argv += [
            "--exclude",
            f"/inbox/*-{door}.md",
            "--exclude",
            f"/inbox/*-{door}-auto.md",
        ]
    if ext_cfg.sync_pull_doors:
        for door in ext_cfg.sync_pull_doors:
            if door in _server_doors(cfg, ext_cfg):
                continue
            argv += [
                "--include",
                f"/inbox/*-{door}.md",
                "--include",
                f"/inbox/*-{door}-auto.md",
            ]
    else:
        argv += ["--include", "/inbox/*.md"]
    argv += ["--include", "/exchange/in/**"]
    return argv


def _run_with_retry(cfg: InstanceConfig, argv: list[str], timeout: int) -> dict[str, Any]:
    """执行一次外部同步命令，失败时自动重试 1 次。"""
    cwd = proc.empty_cwd(cfg)
    tries = 0
    last_rc = 1
    last_err = ""
    total_dur = 0.0
    for attempt in range(2):
        tries = attempt + 1
        rc, out, err, dur = proc.run(argv, None, cwd, timeout)
        total_dur += dur
        last_rc = rc
        if rc == 0:
            return {"ok": True, "tries": tries, "duration": round(total_dur, 3), "argv": argv}
        last_err = (err or out or f"rc={rc}").strip()[:300]
    return {
        "ok": False,
        "tries": tries,
        "duration": round(total_dur, 3),
        "returncode": last_rc,
        "error": last_err,
        "argv": argv,
    }


def _total_budget_seconds(ext: ExtConfig) -> float:
    raw = os.environ.get("HUSHAI_SYNC_BUDGET", "").strip()
    if raw:
        try:
            return max(1.0, float(raw))
        except ValueError:
            pass
    return max(DEFAULT_SYNC_TOTAL_BUDGET, float(ext.sync_timeout) * 4 + 60.0)


def run_sync(
    cfg: InstanceConfig,
    direction: str = "both",
    dry_run: bool = False,
) -> tuple[dict[str, Any], int]:
    """按文件归属执行单向同步，控制总时间预算内记账本并返回 (摘要字典, 退出码)。"""
    if direction not in ("push", "pull", "both"):
        raise ValueError(f"非法同步方向: {direction}")

    t_start = time.monotonic()
    ext = load_ext(cfg)
    today_str = clock.today(cfg).isoformat()
    if not ext.sync_remote:
        ledger.record(
            cfg, "job", job="sync", date=today_str, dry_run=dry_run,
            result="failed", note="missing [sync] remote",
        )
        return {"ok": False, "error": "未配置 [sync] remote"}, 1

    tool_argv = shlex.split(ext.sync_tool)
    if not tool_argv:
        return {"ok": False, "error": "[sync] tool 为空"}, 1

    remote_hub = f"{ext.sync_remote.rstrip('/')}/hub"
    total_budget = _total_budget_seconds(ext)
    summary: dict[str, Any] = {"ok": True, "direction": direction, "dry_run": dry_run}
    notes: list[str] = []

    if direction in ("push", "both"):
        push_res = _run_with_retry(
            cfg, _build_push_argv(cfg, tool_argv, remote_hub, dry_run, ext), ext.sync_timeout
        )
        summary["push"] = push_res
        if not push_res["ok"]:
            summary["ok"] = False
            notes.append(f"push:{push_res.get('error', 'failed')}")

    if direction in ("pull", "both"):
        pull_res = _run_with_retry(
            cfg, _build_pull_argv(cfg, tool_argv, remote_hub, dry_run, ext), ext.sync_timeout
        )
        summary["pull"] = pull_res
        if not pull_res["ok"]:
            summary["ok"] = False
            notes.append(f"pull:{pull_res.get('error', 'failed')}")
        elif not dry_run:
            remaining = max(0.0, total_budget - (time.monotonic() - t_start))
            try:
                summary["on_event"] = on_event.handle(cfg, budget_seconds=remaining)
            except Exception as exc:
                summary["on_event"] = {"error": str(exc)}

    ledger.record(
        cfg,
        "job",
        job="sync",
        date=today_str,
        dry_run=dry_run,
        result="ok" if summary["ok"] else "failed",
        note="; ".join(notes)[:200] if notes else f"direction={direction}",
    )
    return summary, (0 if summary["ok"] else 1)
