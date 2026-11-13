"""运行账本（runs.jsonl）的追加记录、读取与当日调用计数。"""

from __future__ import annotations

import datetime
import json
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from hushai_agent import clock
from hushai_agent.config import DEFAULT_TIMEZONE, InstanceConfig
from hushai_agent.fsio import append_line
from hushai_agent.paths import get_instance_state_dir

VALID_KINDS = ("call", "job")
VALID_JOB_RESULTS = ("ok", "degraded", "failed", "needs_human")


def _get_state_dir(cfg: InstanceConfig | str | Path) -> Path:
    """获取实例对应的状态目录。"""
    if isinstance(cfg, Path):
        return cfg
    if isinstance(cfg, str):
        return get_instance_state_dir(cfg)
    return get_instance_state_dir(cfg.name)


def _get_ledger_path(cfg: InstanceConfig | str | Path) -> Path:
    """获取实例账本文件路径 <状态目录>/runs.jsonl。"""
    return _get_state_dir(cfg) / "runs.jsonl"


def record(cfg: InstanceConfig, kind: str, **fields: Any) -> dict[str, Any]:
    """向 runs.jsonl 追加一条运行账本记录（自动写入 ts 与 kind）。"""
    if kind not in VALID_KINDS:
        raise ValueError(f"未知的账本记录类型 '{kind}'（只能是 call 或 job）")

    ts = clock.now(cfg).isoformat()
    if kind == "call":
        entry: dict[str, Any] = {
            "ts": ts,
            "kind": "call",
            "job": fields.get("job", ""),
            "adapter": fields.get("adapter", ""),
            "tier": fields.get("tier", ""),
            "model": fields.get("model", ""),
            "ok": bool(fields.get("ok", False)),
            "duration": float(fields.get("duration", 0.0)),
            "usage": fields.get("usage") if fields.get("usage") is not None else {},
            "error": fields.get("error", ""),
        }
    else:
        res_val = fields.get("result", "ok")
        if res_val not in VALID_JOB_RESULTS:
            raise ValueError(
                f"非法任务结果 '{res_val}'（只能是 {VALID_JOB_RESULTS} 之一）"
            )
        date_val = fields.get("date", "")
        date_str = (
            date_val.isoformat()
            if isinstance(date_val, datetime.date)
            else str(date_val)
        )
        entry = {
            "ts": ts,
            "kind": "job",
            "job": fields.get("job", ""),
            "date": date_str,
            "dry_run": bool(fields.get("dry_run", False)),
            "result": res_val,
            "note": fields.get("note", ""),
        }

    state_dir = _get_state_dir(cfg)
    ledger_file = _get_ledger_path(cfg)
    line = json.dumps(entry, ensure_ascii=False)
    append_line(ledger_file, line, [state_dir])
    return entry


def read(
    cfg: InstanceConfig | str | Path,
    limit: int | None = None,
) -> list[dict[str, Any]]:
    """读取 runs.jsonl 中的账本记录，自动跳过空行或损坏的 JSON 行。"""
    ledger_file = _get_ledger_path(cfg)
    if not ledger_file.is_file():
        return []

    try:
        lines = ledger_file.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError):
        return []

    rows: list[dict[str, Any]] = []
    for raw in lines:
        stripped = raw.strip()
        if not stripped:
            continue
        try:
            obj = json.loads(stripped)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            rows.append(obj)

    if limit is not None:
        if limit <= 0:
            return []
        return rows[-limit:]
    return rows


def calls_today(cfg: InstanceConfig) -> int:
    """统计实例时区当天（按 clock.today）的模型调用（kind == 'call'）行数。"""
    target_date = clock.today(cfg)
    tz_name = getattr(cfg, "timezone", DEFAULT_TIMEZONE)
    tz = ZoneInfo(tz_name)

    count = 0
    for row in read(cfg):
        if row.get("kind") != "call":
            continue
        ts_raw = row.get("ts")
        if not isinstance(ts_raw, str) or not ts_raw.strip():
            continue
        normalized = (
            ts_raw.strip()[:-1] + "+00:00"
            if ts_raw.strip().endswith("Z")
            else ts_raw.strip()
        )
        try:
            dt = datetime.datetime.fromisoformat(normalized)
        except ValueError:
            continue
        if dt.tzinfo is not None:
            dt = dt.astimezone(tz)
        if dt.date() == target_date:
            count += 1

    return count
