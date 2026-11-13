"""带回退链、单次校验重试与每日调用预算控制的模型调用执行器。"""

from __future__ import annotations

import time
from typing import Any, Callable

from hushai_agent import adapters, ledger
from hushai_agent.adapters.base import Result
from hushai_agent.config import InstanceConfig, parse_fallback_chain

ValidateFn = Callable[[dict[str, Any] | None], tuple[dict[str, Any] | None, str | None]]


def _get_job_chain(cfg: InstanceConfig, job: str) -> list[tuple[str, str]]:
    """从配置中获取指定任务的回退链。"""
    attr_val = getattr(cfg, job, None)
    if attr_val:
        if isinstance(attr_val, str):
            return parse_fallback_chain(attr_val)
        return list(attr_val)
    job_map = getattr(cfg, "jobs", None) or {}
    if job in job_map and job_map[job]:
        val = job_map[job]
        if isinstance(val, str):
            return parse_fallback_chain(val)
        return list(val)
    return []


def run_model(
    cfg: InstanceConfig,
    job: str,
    prompt: str,
    schema: dict[str, Any] | None,
    meta: dict[str, Any] | None,
    validate: ValidateFn,
    timeout: int = 60,
) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    """按任务配置的回退链调用模型，包含每日预算拦截与结构校验失败单次重试。"""
    chain = _get_job_chain(cfg, job)
    if not chain:
        return None, {
            "degraded": "no-adapter",
            "adapter": "",
            "tier": "",
            "model": "",
            "calls": 0,
            "errors": [],
        }

    call_meta = dict(meta or {})
    call_meta.setdefault("job", job)

    errors: list[str] = []
    calls_made = 0
    last_adapter = ""
    last_tier = ""
    last_model = ""

    for adapter_name, tier in chain:
        last_adapter = adapter_name
        last_tier = tier

        if ledger.calls_today(cfg) >= cfg.max_daily_calls:
            return None, {
                "degraded": "budget",
                "adapter": last_adapter,
                "tier": last_tier,
                "model": last_model,
                "calls": calls_made,
                "errors": errors,
            }

        try:
            adapter = adapters.build(cfg, adapter_name)
        except Exception as exc:
            errors.append(f"{adapter_name}:{tier}: {exc}")
            continue

        for attempt in range(2):
            if attempt > 0 and ledger.calls_today(cfg) >= cfg.max_daily_calls:
                return None, {
                    "degraded": "budget",
                    "adapter": last_adapter,
                    "tier": last_tier,
                    "model": last_model,
                    "calls": calls_made,
                    "errors": errors,
                }

            t0 = time.monotonic()
            try:
                res = adapter.complete(
                    prompt=prompt,
                    tier=tier,
                    schema=schema,
                    timeout=timeout,
                    meta=call_meta,
                )
            except Exception as exc:
                res = Result(
                    ok=False,
                    error=str(exc),
                    duration=round(time.monotonic() - t0, 4),
                )

            duration = res.duration if res.duration > 0 else round(time.monotonic() - t0, 4)
            last_model = res.model
            calls_made += 1

            ledger.record(
                cfg,
                "call",
                job=job,
                adapter=adapter_name,
                tier=tier,
                model=res.model,
                ok=res.ok,
                duration=duration,
                usage=res.usage,
                error=res.error,
            )

            if not res.ok:
                errors.append(f"{adapter_name}:{tier}: {res.error}")
                break

            clean_data, fatal_error = validate(res.data)
            if fatal_error is None:
                return clean_data, {
                    "adapter": adapter_name,
                    "tier": tier,
                    "model": res.model,
                    "calls": calls_made,
                }

            errors.append(f"{adapter_name}:{tier}: {fatal_error}")

    return None, {
        "degraded": "all-failed",
        "errors": errors,
        "adapter": last_adapter,
        "tier": last_tier,
        "model": last_model,
        "calls": calls_made,
    }
