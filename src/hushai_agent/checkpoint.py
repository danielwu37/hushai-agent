"""运行检查点与崩溃恢复调度：执行主提交阶段、支持中途重入与目标文件校验恢复。"""

from __future__ import annotations

import json
import os
import time
import uuid
from pathlib import Path
from typing import Any

from hushai_agent import lock
from hushai_agent.checkpoint_verify import (
    sha256_text,
    unlink_if_exists,
    verify_append_targets,
    verify_atomic_targets,
    verify_closing_steps,
    verify_daily_state_and_cursor,
    verify_open_items_and_archive,
    verify_routing_targets,
)
from hushai_agent.config import InstanceConfig
from hushai_agent.fsio import atomic_write, ensure_dir
from hushai_agent.paths import get_instance_state_dir


def checkpoints_dir(cfg: InstanceConfig | str) -> Path:
    """返回实例检查点目录 <state>/checkpoints/。"""
    name = cfg.name if isinstance(cfg, InstanceConfig) else str(cfg)
    return get_instance_state_dir(name) / "checkpoints"


def _save_ckpt(cfg: InstanceConfig, filename: str, payload: dict[str, Any]) -> Path:
    state_dir = get_instance_state_dir(cfg.name)
    cdir = ensure_dir(checkpoints_dir(cfg), [state_dir])
    cpath = cdir / filename
    atomic_write(cpath, json.dumps(payload, ensure_ascii=False, indent=2) + "\n", [state_dir])
    return cpath


def new_run_id(kind: str, as_of_str: str) -> str:
    return f"{kind}-{as_of_str}-{int(time.time() * 1000)}-{uuid.uuid4().hex[:8]}"


def _stage_hook(stage: str) -> None:
    """供测试与诊断使用的阶段钩子点（生产环境下为空操作）。"""
    _ = stage


def execute_commit_plan(cfg: InstanceConfig, plan: dict[str, Any]) -> dict[str, Any]:
    """持久化检查点后按阶段落盘并通过目标文件核验全部副作用，最后清理检查点。"""
    state_dir = get_instance_state_dir(cfg.name)
    kind = str(plan["kind"])
    run_id = str(plan["run_id"])
    filename = f"{int(plan.get('seq') or time.time_ns()):020d}-{kind}-{run_id}.json"

    plan["stage"] = "planned"
    cpath = _save_ckpt(cfg, filename, plan)
    _stage_hook("after_ckpt_planned")

    # 1. 核心目标写入与追加（目标侧幂等 + 字节摘要可控）
    verify_open_items_and_archive(cfg.vault, plan.get("open_items") or {})
    verify_atomic_targets(cfg.vault, plan.get("atomic_targets") or {})
    verify_append_targets(cfg, plan)
    _stage_hook("after_vault_writes")

    # 2. 日报累积态、日报文件与游标推进
    ds = plan.get("daily_state") or {}
    from hushai_agent import report
    from hushai_agent.daily_state import update_daily_state

    if ds.get("cur_facts"):
        acc = update_daily_state(
            state_dir,
            dict(ds["cur_facts"]),
            dry_run=False,
            is_mechanical=bool(ds.get("is_mechanical")),
            run_id=run_id,
        )
        daily_text = str(ds.get("daily_report_text") or report.render_daily(acc))
        daily_rel = str(ds.get("daily_report_rel") or f"hub/reports/daily-{plan['as_of']}.md")
        atomic_write(cfg.vault / daily_rel, daily_text, [cfg.vault])

    plan["stage"] = "before_cursor"
    _save_ckpt(cfg, filename, plan)
    _stage_hook("before_cursor")

    verify_daily_state_and_cursor(cfg, plan)
    plan["stage"] = "cursor_done"
    _save_ckpt(cfg, filename, plan)
    _stage_hook("after_cursor")

    # 3. 收尾步（晨间简报、第二大脑、唤醒、Git 提交）
    closing_res = verify_closing_steps(cfg, plan)
    plan.setdefault("closing", {})["brain_done"] = True
    plan["stage"] = "closing_done"
    _save_ckpt(cfg, filename, plan)
    _stage_hook("after_closing")

    # 4. 终态全量目标文件二次核验：绝不信任 JSON stage 本身，核验磁盘真实一致才移除检查点
    verify_open_items_and_archive(cfg.vault, plan.get("open_items") or {})
    verify_atomic_targets(cfg.vault, plan.get("atomic_targets") or {})
    verify_append_targets(cfg, plan)
    verify_daily_state_and_cursor(cfg, plan)
    closing_final = verify_closing_steps(cfg, plan)
    if closing_final.get("git_committed"):
        closing_res["git_committed"] = True

    unlink_if_exists(cpath, [state_dir])
    return closing_res


def recover_pending(cfg: InstanceConfig) -> dict[str, int]:
    """在持有 runner.lock 时扫描并恢复所有未完成的 digest / mechanical 检查点。"""
    cdir = checkpoints_dir(cfg)
    if not cdir.is_dir():
        return {"recovered": 0, "skipped_invalid": 0}
    state_dir = get_instance_state_dir(cfg.name)
    files = sorted(
        p for p in cdir.iterdir()
        if p.is_file() and p.suffix == ".json" and not p.name.startswith("route-")
    )
    recovered = skipped_invalid = 0
    for cpath in files:
        try:
            plan = json.loads(cpath.read_text(encoding="utf-8"))
            if not isinstance(plan, dict) or plan.get("kind") not in ("digest", "mechanical"):
                continue
        except (OSError, UnicodeDecodeError, ValueError):
            skipped_invalid += 1
            continue

        # 无视 JSON 中的 stage 提示，直接逐一核验目标文件并修补缺漏项
        verify_open_items_and_archive(cfg.vault, plan.get("open_items") or {})
        verify_atomic_targets(cfg.vault, plan.get("atomic_targets") or {})
        verify_append_targets(cfg, plan)
        verify_daily_state_and_cursor(cfg, plan)
        verify_closing_steps(cfg, plan)

        # 再过一遍全目标核验确保 Git 与简报在补齐后完全自洽
        if plan.get("closing"):
            plan["closing"]["brain_done"] = True
            verify_closing_steps(cfg, plan)
        ledger_job = str((plan.get("closing") or {}).get("ledger_job") or "")
        if ledger_job:
            from hushai_agent import ledger

            ledger.record(
                cfg,
                "job",
                job=ledger_job,
                date=str(plan.get("as_of") or ""),
                dry_run=False,
                result="ok",
                note=f"recovered_run={plan.get('run_id')}",
            )
        unlink_if_exists(cpath, [state_dir])
        recovered += 1
    return {"recovered": recovered, "skipped_invalid": skipped_invalid}


def deliver_route_with_checkpoint(cfg: InstanceConfig, spec: dict[str, Any]) -> None:
    """持久化路由交付检查点，经目标文件核验后写入回复、回执与 routed.jsonl。"""
    state_dir = get_instance_state_dir(cfg.name)
    spec = dict(spec)
    spec["reply_sha256"] = sha256_text(str(spec["reply_content"]))
    cpath = _save_ckpt(cfg, f"route-{spec['line_id']}.json", spec)
    _stage_hook("after_route_ckpt")
    verify_routing_targets(cfg, spec)
    unlink_if_exists(cpath, [state_dir])


def recover_pending_routes(cfg: InstanceConfig) -> int:
    """在持有 route.lock 时恢复所有未完成的路由交付检查点。"""
    cdir = checkpoints_dir(cfg)
    if not cdir.is_dir():
        return 0
    state_dir = get_instance_state_dir(cfg.name)
    recovered = 0
    for cpath in sorted(p for p in cdir.iterdir() if p.is_file() and p.name.startswith("route-") and p.suffix == ".json"):
        try:
            spec = json.loads(cpath.read_text(encoding="utf-8"))
            if not isinstance(spec, dict) or "line_id" not in spec:
                continue
        except (OSError, UnicodeDecodeError, ValueError):
            continue
        verify_routing_targets(cfg, spec)
        unlink_if_exists(cpath, [state_dir])
        recovered += 1
    return recovered


def has_pending(cfg: InstanceConfig) -> bool:
    """检查是否存在未完成的崩溃检查点或待补跑 dirty 标记。"""
    cdir = checkpoints_dir(cfg)
    if cdir.is_dir() and any(p.is_file() and p.suffix == ".json" for p in cdir.iterdir()):
        return True
    return lock.has_dirty(cfg, "mechanical") or lock.has_dirty(cfg, "routing")
