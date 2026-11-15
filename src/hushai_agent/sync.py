"""按归属单向同步（hushai sync）：只调网盘命令行工具，不碰挂载点；拉取后触发事件处理。"""

from __future__ import annotations

import shlex
from typing import Any

from hushai_agent import clock, ledger, on_event
from hushai_agent.adapters import proc
from hushai_agent.config import InstanceConfig
from hushai_agent.config_ext import load_ext

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
    """取出落在 hub/ 下的 local_only 路径，转成 rclone --exclude 参数。"""
    args: list[str] = ["--exclude", ".git/**"]
    for rel in cfg.local_only:
        norm = rel.strip().strip("/")
        if norm == "hub":
            args += ["--exclude", "/**"]
        elif norm.startswith("hub/"):
            sub = norm[len("hub/") :]
            args += ["--exclude", f"/{sub}", "--exclude", f"/{sub}/**"]
    return args


def _build_push_argv(cfg: InstanceConfig, tool_argv: list[str], remote_hub: str, dry_run: bool) -> list[str]:
    """构建 push（服务器 hub -> 远端 hub）命令参数。"""
    steward = cfg.steward_door
    argv = [*tool_argv, "copy", str(cfg.vault / "hub"), remote_hub]
    if dry_run:
        argv.append("--dry-run")
    argv += _hub_local_only_excludes(cfg)
    for inc in PUSH_INCLUDES:
        argv += ["--include", inc]
    argv += [
        "--include",
        f"/inbox/*-{steward}.md",
        "--include",
        f"/inbox/*-{steward}-auto.md",
    ]
    return argv


def _build_pull_argv(cfg: InstanceConfig, tool_argv: list[str], remote_hub: str, dry_run: bool) -> list[str]:
    """构建 pull（远端 hub -> 服务器 hub）命令参数。"""
    steward = cfg.steward_door
    argv = [*tool_argv, "copy", remote_hub, str(cfg.vault / "hub")]
    if dry_run:
        argv.append("--dry-run")
    argv += _hub_local_only_excludes(cfg)
    argv += [
        "--exclude",
        f"/inbox/*-{steward}.md",
        "--exclude",
        f"/inbox/*-{steward}-auto.md",
        "--include",
        "/inbox/*.md",
        "--include",
        "/exchange/in/**",
    ]
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


def run_sync(
    cfg: InstanceConfig,
    direction: str = "both",
    dry_run: bool = False,
) -> tuple[dict[str, Any], int]:
    """按文件归属执行单向同步，记账本并返回 (摘要字典, 退出码)。"""
    if direction not in ("push", "pull", "both"):
        raise ValueError(f"非法同步方向: {direction}")

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
    summary: dict[str, Any] = {"ok": True, "direction": direction, "dry_run": dry_run}
    notes: list[str] = []

    if direction in ("push", "both"):
        push_res = _run_with_retry(
            cfg, _build_push_argv(cfg, tool_argv, remote_hub, dry_run), ext.sync_timeout
        )
        summary["push"] = push_res
        if not push_res["ok"]:
            summary["ok"] = False
            notes.append(f"push:{push_res.get('error', 'failed')}")

    if direction in ("pull", "both"):
        pull_res = _run_with_retry(
            cfg, _build_pull_argv(cfg, tool_argv, remote_hub, dry_run), ext.sync_timeout
        )
        summary["pull"] = pull_res
        if not pull_res["ok"]:
            summary["ok"] = False
            notes.append(f"pull:{pull_res.get('error', 'failed')}")
        elif not dry_run:
            try:
                summary["on_event"] = on_event.handle(cfg)
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
