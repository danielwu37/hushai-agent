"""带回退链、单次校验重试与每日调用预算控制的模型调用执行器。"""

from __future__ import annotations

import datetime
import json
import time
from typing import Any, Callable
from zoneinfo import ZoneInfo

from hushai_agent import adapters, clock, ledger
from hushai_agent.adapters.base import Result
from hushai_agent.config import DEFAULT_TIMEZONE, InstanceConfig, parse_fallback_chain
from hushai_agent.paths import get_instance_state_dir

ValidateFn = Callable[[dict[str, Any] | None], tuple[dict[str, Any] | None, str | None]]


def _is_adapter_skipped(cfg: InstanceConfig, adapter_name: str) -> str | None:
    """检查 <状态目录>/adapter-skip.json 中该适配项是否处于未过期的跳过窗口内，未过期返回到期时间字符串。"""
    try:
        skip_file = get_instance_state_dir(cfg.name) / "adapter-skip.json"
    except Exception:
        return None
    if not skip_file.is_file():
        return None
    try:
        data = json.loads(skip_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    raw = data.get(adapter_name)
    until_str = raw.get("until", "") if isinstance(raw, dict) else (raw if isinstance(raw, str) else "")
    if not until_str or not str(until_str).strip():
        return None
    text = str(until_str).strip()
    norm = text[:-1] + "+00:00" if text.endswith("Z") else text
    try:
        until_dt = datetime.datetime.fromisoformat(norm)
    except ValueError:
        return None
    tz = ZoneInfo(getattr(cfg, "timezone", DEFAULT_TIMEZONE))
    until_dt = until_dt.replace(tzinfo=tz) if until_dt.tzinfo is None else until_dt.astimezone(tz)
    if clock.now(cfg) < until_dt:
        return until_dt.isoformat()
    return None


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
    timeout: int | None = None,
    chain: list[tuple[str, str]] | None = None,
    text_mode: bool = False,
) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    """按任务配置的回退链调用模型，包含每日预算拦截与结构校验失败单次重试。

    timeout 为 None 时各适配项用自己的配置（[adapters] <名字>.timeout，默认 180 秒）。
    chain 给出时用它而不是 [jobs] 里的链（路由用）；text_mode 为真时不要结构，
    交给 validate 的是 {"text": 回答文本}。
    """
    chain = list(chain) if chain else _get_job_chain(cfg, job)
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
        skip_until = _is_adapter_skipped(cfg, adapter_name)
        if skip_until is not None:
            errors.append(f"{adapter_name}:{tier}: skipped until {skip_until}")
            continue

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

            clean_data, fatal_error = validate({"text": res.text} if text_mode else res.data)
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
