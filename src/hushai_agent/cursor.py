"""已对账收件游标（digested.jsonl）的读取与追加。"""

from __future__ import annotations

import datetime
import json
from pathlib import Path
from typing import Iterable

from hushai_agent import clock
from hushai_agent.config import InstanceConfig
from hushai_agent.fsio import append_line
from hushai_agent.paths import get_instance_state_dir


def _resolve_state_dir(cfg: InstanceConfig | str | Path) -> Path:
    """获取实例对应的状态目录。"""
    if isinstance(cfg, Path):
        return cfg
    if isinstance(cfg, str):
        return get_instance_state_dir(cfg)
    return get_instance_state_dir(cfg.name)


def _get_cursor_path(cfg: InstanceConfig | str | Path) -> Path:
    """获取实例游标文件路径 <状态目录>/digested.jsonl。"""
    return _resolve_state_dir(cfg) / "digested.jsonl"


def load_ids(cfg: InstanceConfig | str | Path) -> set[str]:
    """读取 digested.jsonl 中所有已对账的 line_id 集合，自动跳过损坏行。"""
    cursor_file = _get_cursor_path(cfg)
    if not cursor_file.is_file():
        return set()

    try:
        lines = cursor_file.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError):
        return set()

    ids: set[str] = set()
    for raw in lines:
        stripped = raw.strip()
        if not stripped:
            continue
        try:
            obj = json.loads(stripped)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            lid = obj.get("line_id")
            if isinstance(lid, str) and lid.strip():
                ids.add(lid.strip())
    return ids


def append(
    cfg: InstanceConfig,
    line_ids: Iterable[str],
    date: datetime.date | str,
) -> int:
    """将一批已对账的 line_id 追加写入 digested.jsonl，返回写入行数。"""
    state_dir = _resolve_state_dir(cfg)
    cursor_file = _get_cursor_path(cfg)
    date_str = date.isoformat() if isinstance(date, datetime.date) else str(date)
    ts = clock.now(cfg).isoformat()

    written = 0
    for lid in line_ids:
        clean_lid = str(lid).strip()
        if not clean_lid:
            continue
        payload = json.dumps(
            {"line_id": clean_lid, "date": date_str, "ts": ts},
            ensure_ascii=False,
        )
        append_line(cursor_file, payload, [state_dir])
        written += 1
    return written
