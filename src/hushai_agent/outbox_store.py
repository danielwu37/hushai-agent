"""纯文件型 Outbox 存储原语：<state>/outbox/ 目录结构、去重索引、跨态原子迁移与坏信封隔离。"""

from __future__ import annotations

import datetime
import json
import os
import uuid
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from hushai_agent.config import InstanceConfig
from hushai_agent.fsio import atomic_write, ensure_dir
from hushai_agent.guard import ensure_writable
from hushai_agent.paths import get_instance_state_dir

OUTBOX_STATES = ("pending", "sending", "sent", "failed")
OUTBOX_LOCK_NAME = "outbox.lock"
DELIVERY_SEMANTICS = "at-least-once"

_STATUS_TIE_ORDER = {"pending": 1, "sending": 2, "failed": 3, "sent": 4, "archive": 5}


def _stage_hook(stage: str) -> None:
    """供测试与故障注入使用的存储层阶段钩子点（生产环境下为空操作）。"""
    _ = stage


def outbox_dir(cfg: InstanceConfig | str) -> Path:
    """返回实例 Outbox 根目录 <state>/outbox/。"""
    name = cfg.name if isinstance(cfg, InstanceConfig) else str(cfg)
    return get_instance_state_dir(name) / "outbox"


def state_subdir(cfg: InstanceConfig | str, state: str) -> Path:
    if state not in OUTBOX_STATES and state != "archive":
        raise ValueError(f"非法 outbox 状态目录: {state}")
    return outbox_dir(cfg) / state


def ensure_layout(cfg: InstanceConfig | str) -> Path:
    """确保 <state>/outbox/{pending,sending,sent,failed,archive} 目录均已存在。"""
    state_dir = get_instance_state_dir(cfg.name if isinstance(cfg, InstanceConfig) else str(cfg))
    root = ensure_dir(outbox_dir(cfg), [state_dir])
    for sub in (*OUTBOX_STATES, "archive"):
        ensure_dir(root / sub, [state_dir])
    return root


def parse_iso(raw: str | None, tz: ZoneInfo) -> datetime.datetime | None:
    """宽容解析 ISO 时间字符串并统一到实例时区。"""
    if not raw or not isinstance(raw, str):
        return None
    try:
        dt = datetime.datetime.fromisoformat(raw.strip())
    except ValueError:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=tz)
    return dt.astimezone(tz)


def iso_to_utc_ts(raw: str | None) -> float:
    """将 ISO 时间转为可跨时区比较的 UTC epoch 秒数（无时区按 UTC 兜底）。"""
    if not raw or not isinstance(raw, str):
        return 0.0
    try:
        dt = datetime.datetime.fromisoformat(raw.strip())
    except ValueError:
        return 0.0
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.timezone.utc)
    else:
        dt = dt.astimezone(datetime.timezone.utc)
    return dt.timestamp()


def load_dedupe_status(cfg: InstanceConfig | str) -> tuple[dict[str, dict[str, Any]], str]:
    """读取 <state>/outbox/dedupe.json 并返回 (表, 健康态: ok|missing|corrupt)。"""
    path = outbox_dir(cfg) / "dedupe.json"
    if not path.is_file():
        return {}, "missing"
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValueError):
        return {}, "corrupt"
    if not isinstance(raw, dict):
        return {}, "corrupt"
    cleaned = {str(k): v for k, v in raw.items() if isinstance(v, dict)}
    if len(cleaned) != len(raw):
        return cleaned, "corrupt"
    return cleaned, "ok"


def load_dedupe(cfg: InstanceConfig | str) -> dict[str, dict[str, Any]]:
    """读取 <state>/outbox/dedupe.json；文件损坏时安全返回空表。"""
    return load_dedupe_status(cfg)[0]


def save_dedupe(cfg: InstanceConfig | str, table: dict[str, dict[str, Any]]) -> Path:
    """原子持久化 <state>/outbox/dedupe.json。"""
    state_dir = get_instance_state_dir(cfg.name if isinstance(cfg, InstanceConfig) else str(cfg))
    ensure_layout(cfg)
    path = outbox_dir(cfg) / "dedupe.json"
    atomic_write(path, json.dumps(table, ensure_ascii=False, indent=2) + "\n", [state_dir])
    return path


def unlink_safe(path: Path, state_dir: Path) -> None:
    """按路径护栏安全删除状态目录下文件。"""
    try:
        os.unlink(ensure_writable(path, [state_dir]))
    except FileNotFoundError:
        pass


def write_envelope(cfg: InstanceConfig | str, state: str, env: dict[str, Any]) -> Path:
    """将信封原子写入指定状态子目录（<state>/outbox/<state>/<message_id>.json）。"""
    state_dir = get_instance_state_dir(cfg.name if isinstance(cfg, InstanceConfig) else str(cfg))
    ensure_layout(cfg)
    msg_id = str(env["message_id"])
    path = state_subdir(cfg, state) / f"{msg_id}.json"
    atomic_write(path, json.dumps(env, ensure_ascii=False, indent=2) + "\n", [state_dir])
    return path


def transition_envelope(
    cfg: InstanceConfig | str,
    src_state: str,
    dst_state: str,
    env: dict[str, Any],
    archive_ym: str | None = None,
    on_after_write: Any = None,
) -> Path:
    """递增单调迁移版本号 transition_seq 后先原子写入目标目录再删除源目录文件。"""
    state_dir = get_instance_state_dir(cfg.name if isinstance(cfg, InstanceConfig) else str(cfg))
    ensure_layout(cfg)
    msg_id = str(env["message_id"])
    env["transition_seq"] = max(1, int(env.get("transition_seq") or 0)) + 1
    if dst_state == "archive":
        ym = archive_ym or str(env.get("updated_at") or env.get("created_at") or "")[:7] or "unknown"
        dst_dir = ensure_dir(state_subdir(cfg, "archive") / ym, [state_dir])
        dst_path = dst_dir / f"{msg_id}.json"
        atomic_write(dst_path, json.dumps(env, ensure_ascii=False, indent=2) + "\n", [state_dir])
    else:
        dst_path = write_envelope(cfg, dst_state, env)

    hook_name = f"after_transition_write:{src_state}->{dst_state}"
    _stage_hook(hook_name)
    _stage_hook("after_transition_write")
    if on_after_write is not None:
        on_after_write(hook_name)
        on_after_write("after_transition_write")

    unlink_safe(state_subdir(cfg, src_state) / f"{msg_id}.json", state_dir)
    return dst_path


def load_envelope(path: Path) -> dict[str, Any] | None:
    """读取并校验单份信封 JSON；无效返回 None。"""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValueError):
        return None
    if not isinstance(raw, dict) or not raw.get("message_id") or not raw.get("channel"):
        return None
    return raw


def quarantine_corrupt(cfg: InstanceConfig | str, src_path: Path, reason: str) -> Path:
    """将损坏的信封移入 failed/corrupt-* 隔离保存，避免阻塞后续投递。"""
    state_dir = get_instance_state_dir(cfg.name if isinstance(cfg, InstanceConfig) else str(cfg))
    ensure_layout(cfg)
    failed_dir = state_subdir(cfg, "failed")
    dst = failed_dir / f"corrupt-{src_path.stem}-{uuid.uuid4().hex[:6]}.json"
    try:
        raw_preview = src_path.read_text(encoding="utf-8", errors="replace")[:2048]
    except OSError:
        raw_preview = ""
    payload = {
        "corrupted": True,
        "source": src_path.name,
        "reason": reason,
        "raw_preview": raw_preview,
    }
    atomic_write(dst, json.dumps(payload, ensure_ascii=False, indent=2) + "\n", [state_dir])
    unlink_safe(src_path, state_dir)
    return dst


def has_active_envelope(cfg: InstanceConfig | str, message_id: str) -> str | None:
    """查询指定 message_id 当前所在的活跃状态名；未找到返回 None。"""
    for st in OUTBOX_STATES:
        if (state_subdir(cfg, st) / f"{message_id}.json").is_file():
            return st
    return None


def iter_all_envelope_files(cfg: InstanceConfig | str) -> list[tuple[str, Path]]:
    """按状态枚举 pending/sending/sent/failed 与 archive/YYYY-MM/ 中的全部非隔离信封文件。"""
    out: list[tuple[str, Path]] = []
    for st in OUTBOX_STATES:
        sdir = state_subdir(cfg, st)
        if not sdir.is_dir():
            continue
        for p in sorted(sdir.glob("*.json")):
            if not p.name.startswith("corrupt-"):
                out.append((st, p))
    adir = state_subdir(cfg, "archive")
    if adir.is_dir():
        for p in sorted(adir.rglob("*.json")):
            if p.is_file() and not p.name.startswith("corrupt-"):
                out.append(("archive", p))
    return out


def reconcile_duplicate_files(cfg: InstanceConfig | str) -> int:
    """清理原子迁移中途崩溃（含归档迁移）造成的跨目录同 ID 双份残留，优先按 transition_seq 裁决。"""
    state_dir = get_instance_state_dir(cfg.name if isinstance(cfg, InstanceConfig) else str(cfg))
    by_id: dict[str, list[tuple[str, Path, dict[str, Any]]]] = {}
    removed = 0
    for st, p in iter_all_envelope_files(cfg):
        env = load_envelope(p)
        if env is None:
            continue
        msg_id = str(env["message_id"])
        by_id.setdefault(msg_id, []).append((st, p, env))

    for _, entries in by_id.items():
        if len(entries) <= 1:
            continue

        def _winner_key(item: tuple[str, Path, dict[str, Any]]) -> tuple[int, int, int, float, int]:
            dir_state, _, env = item
            env_status = str(env.get("status") or "")
            status_matches_dir = (
                int(env_status in ("sent", "failed"))
                if dir_state == "archive"
                else int(env_status == dir_state)
            )
            return (
                int(env.get("transition_seq") or 0),
                status_matches_dir,
                int(env.get("attempts") or 0),
                iso_to_utc_ts(env.get("updated_at")),
                _STATUS_TIE_ORDER.get(dir_state, 0),
            )

        winner = max(entries, key=_winner_key)
        for _, p, _ in entries:
            if p != winner[1]:
                unlink_safe(p, state_dir)
                removed += 1
    return removed
