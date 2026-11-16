"""事件处理（hushai on-event）：收件或交换区有变化时跑一遍，先即时机械对账，再在预算内做跨端路由。"""

from __future__ import annotations

import sys
import time
from typing import Any

from hushai_agent import clock, digest, ledger, lock, outbox, routing
from hushai_agent.config import InstanceConfig
from hushai_agent.config_ext import load_ext

MIN_ROUTING_BUDGET_SEC = 10.0


def _note_failure(cfg: InstanceConfig, step: str, exc: Exception) -> dict[str, Any]:
    """一步出错记账本，不向上抛。"""
    try:
        ledger.record(
            cfg, "job", job=f"on-event-{step}", date=clock.today(cfg).isoformat(),
            result="failed", note=str(exc)[:200],
        )
    except Exception as log_exc:
        sys.stderr.write(f"hushai on-event ledger failure ({step}): {log_exc}\n")
    return {"error": str(exc)}


def handle(
    cfg: InstanceConfig,
    *,
    budget_seconds: float | None = None,
) -> dict[str, Any]:
    """分步兜底执行：可选 budget_seconds 限制整体时长，超时前将未完成的路由留给 dirty 标记。"""
    deadline = (time.monotonic() + float(budget_seconds)) if budget_seconds is not None else None
    out: dict[str, Any] = {}
    try:
        out["mechanical"] = digest.run_mechanical_only(cfg)
    except Exception as exc:
        out["mechanical"] = _note_failure(cfg, "mechanical", exc)

    try:
        ext = load_ext(cfg)
        if not ext.routing_enabled:
            out["routing"] = {"skipped": "disabled"}
        elif deadline is not None and (deadline - time.monotonic()) < MIN_ROUTING_BUDGET_SEC:
            lock.mark_dirty(cfg, "routing")
            out["routing"] = {"skipped": "budget-exhausted", "dirty_marked": True}
        else:
            out["routing"] = routing.run(cfg, ext, deadline=deadline)
    except Exception as exc:
        out["routing"] = _note_failure(cfg, "routing", exc)

    try:
        if deadline is not None and (deadline - time.monotonic()) <= 0:
            out["outbox"] = {"skipped": "budget-exhausted"}
        elif outbox.has_pending_or_sending(cfg):
            out["outbox"] = outbox.flush(cfg)
    except Exception as exc:
        out["outbox"] = _note_failure(cfg, "outbox", exc)
    return out
