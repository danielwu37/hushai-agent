"""事件处理（hushai on-event）：收件或交换区有变化时跑一遍，先即时机械对账，再做跨端路由。"""

from __future__ import annotations

from typing import Any

from hushai_agent import clock, digest, ledger, outbox, routing
from hushai_agent.config import InstanceConfig
from hushai_agent.config_ext import load_ext


def _note_failure(cfg: InstanceConfig, step: str, exc: Exception) -> dict[str, Any]:
    """一步出错只记账本，不向上抛。"""
    try:
        ledger.record(
            cfg, "job", job=f"on-event-{step}", date=clock.today(cfg).isoformat(),
            result="failed", note=str(exc)[:200],
        )
    except Exception:
        pass
    return {"error": str(exc)}


def handle(cfg: InstanceConfig) -> dict[str, Any]:
    """两步各自兜底：任何一步失败都继续下一步。"""
    out: dict[str, Any] = {}
    try:
        out["mechanical"] = digest.run_mechanical_only(cfg)
    except Exception as exc:
        out["mechanical"] = _note_failure(cfg, "mechanical", exc)
    try:
        ext = load_ext(cfg)
        out["routing"] = routing.run(cfg, ext) if ext.routing_enabled else {"skipped": "disabled"}
    except Exception as exc:
        out["routing"] = _note_failure(cfg, "routing", exc)
    try:
        if outbox.has_pending_or_sending(cfg):
            out["outbox"] = outbox.flush(cfg)
    except Exception as exc:
        out["outbox"] = _note_failure(cfg, "outbox", exc)
    return out
