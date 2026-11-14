"""适配项自检命令（hushai adapter test）：用极小提示词与结构验证问答式调用并记账。"""

from __future__ import annotations

import time
from typing import Any

from hushai_agent import adapters, clock, ledger
from hushai_agent.adapters.base import Result
from hushai_agent.config import VALID_TIERS, InstanceConfig, load_instance_config

TEST_PROMPT = '请只返回 JSON 对象：{"ok": true}'
TEST_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["ok"],
    "additionalProperties": False,
    "properties": {
        "ok": {"type": "boolean"},
    },
}


def run_adapter_test(
    cfg: InstanceConfig | str,
    adapter_name: str,
    tier: str = "light",
) -> dict[str, Any]:
    """用极小提示词与结构走一遍 complete，记一条 call 账本行并受每日预算限制。"""
    if tier not in VALID_TIERS:
        raise ValueError(f"非法档位 '{tier}'（只能是 light 或 heavy）")

    inst_cfg = cfg if isinstance(cfg, InstanceConfig) else load_instance_config(cfg)
    clean_name = (adapter_name or "").strip()
    if not clean_name:
        raise ValueError("适配项名称不能为空")

    if ledger.calls_today(inst_cfg) >= inst_cfg.max_daily_calls:
        return {
            "ok": False,
            "adapter": clean_name,
            "tier": tier,
            "model": "",
            "duration": 0.0,
            "data": None,
            "error": "超出每日调用预算（max_daily_calls 已用尽）",
        }

    try:
        adapter = adapters.build(inst_cfg, clean_name)
    except ValueError as exc:
        return {
            "ok": False,
            "adapter": clean_name,
            "tier": tier,
            "model": "",
            "duration": 0.0,
            "data": None,
            "error": str(exc),
        }

    meta = {
        "job": "adapter-test",
        "date": clock.today(inst_cfg).isoformat(),
        "empty": {"ok": True},
    }
    t0 = time.monotonic()
    try:
        res = adapter.complete(prompt=TEST_PROMPT, tier=tier, schema=TEST_SCHEMA, meta=meta)
    except Exception as exc:
        res = Result(ok=False, error=str(exc), duration=round(time.monotonic() - t0, 4))

    duration = res.duration if res.duration > 0 else round(time.monotonic() - t0, 4)
    ledger.record(
        inst_cfg,
        "call",
        job="adapter-test",
        adapter=clean_name,
        tier=tier,
        model=res.model,
        ok=res.ok,
        duration=duration,
        usage=res.usage,
        error=res.error,
    )
    return {
        "ok": bool(res.ok),
        "adapter": clean_name,
        "tier": tier,
        "model": res.model,
        "duration": duration,
        "data": res.data,
        "error": res.error,
    }
