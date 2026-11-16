"""hushai heal 的第 5–7 项检查：记忆写入、模型可用（不调模型）与远端同步。"""

from __future__ import annotations

import datetime
import json
import shlex
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo

from hushai_agent import clock, cursor, doorkit, ledger, reconcile
from hushai_agent.adapters import proc
from hushai_agent.config import InstanceConfig
from hushai_agent.config_ext import load_ext
from hushai_agent.fsio import atomic_write
from hushai_agent.inboxscan import iter_inbox_lines
from hushai_agent.items import FlexDate
from hushai_agent.paths import get_instance_config_dir, get_instance_state_dir
from hushai_agent.runner import _get_job_chain, _is_adapter_skipped


def _parse_iso(ts_raw: Any, tz: ZoneInfo) -> datetime.datetime | None:
    if not isinstance(ts_raw, str) or not ts_raw.strip():
        return None
    text = ts_raw.strip()
    norm = text[:-1] + "+00:00" if text.endswith("Z") else text
    try:
        dt = datetime.datetime.fromisoformat(norm)
    except ValueError:
        return None
    return dt.replace(tzinfo=tz) if dt.tzinfo is None else dt.astimezone(tz)


def _default_cmd_runner(argv: list[str], timeout: int = 15) -> tuple[int, str]:
    rc, out, err, _ = proc.run(argv, None, Path("/tmp"), timeout)
    return rc, (out or err or "").strip()


def check_memory_write(
    cfg: InstanceConfig, before: str | None, dry_run: bool, hooks: dict[str, Any]
) -> dict[str, Any]:
    """5. 记忆写入：游标两天未前进但收件有未消化行，或钩子缺失/当天有会话无收件。"""
    today_d = clock.today(cfg)
    cutoff_d = today_d - datetime.timedelta(days=2)
    digested_ids = cursor.load_ids(cfg)
    inbox_dir = cfg.vault / "hub" / "inbox"
    recs = list(iter_inbox_lines(inbox_dir)) if inbox_dir.is_dir() else []

    stale_undigested = [
        r for r in recs
        if (d := FlexDate.from_any(r.date)) is not None and d < cutoff_d and r.line_id not in digested_ids
    ]
    cfg_dir = get_instance_config_dir(cfg.name)
    doorkit_dir = cfg_dir / "doorkit"
    claude_kit = doorkit_dir / "claude-settings.json"
    hook_missing = False
    if doorkit_dir.is_dir():
        if not claude_kit.is_file() or "capture" not in claude_kit.read_text(encoding="utf-8", errors="ignore"):
            hook_missing = True

    if not stale_undigested and not hook_missing:
        return {"status": "ok", "result": "游标与端接入钩子正常"}

    actions: list[str] = []
    problems: list[str] = []
    if hook_missing:
        problems.append("端接入包 Stop 钩子缺失 capture")
        if not dry_run:
            doorkit_dir.mkdir(parents=True, exist_ok=True)
            doorkit.render_doorkit(cfg, doorkit_dir)
        actions.append("重装端接入包 doorkit")

    if stale_undigested:
        problems.append(f"存在 {len(stale_undigested)} 条超 2 天未对账收件")
        if not dry_run:
            reconcile.run_mechanical_only(cfg)
        actions.append("执行即时机械对账 reconcile")

    return {
        "status": "healed",
        "problem": "；".join(problems),
        "action": "；".join(actions),
        "result": "钩子与机械对账已补跑",
        "tries": 1,
    }


def check_model_available(
    cfg: InstanceConfig, before: str | None, dry_run: bool, hooks: dict[str, Any]
) -> dict[str, Any]:
    """6. 模型可用（不调模型）：读账本近 24 小时记录；首家连续失败 3 次则写 adapter-skip.json 跳过 6 小时。"""
    job_key = before if before in ("digest", "weekly") else "digest"
    chain = _get_job_chain(cfg, job_key)
    if not chain:
        return {"status": "ok", "result": "未配置模型回退链"}

    first_adapter = chain[0][0]
    existing_skip = _is_adapter_skipped(cfg, first_adapter)
    if existing_skip is not None:
        return {"status": "ok", "result": f"首家 {first_adapter} 已在跳过窗口内（至 {existing_skip}）"}

    now_dt = clock.now(cfg)
    tz = ZoneInfo(cfg.timezone)
    cutoff = now_dt - datetime.timedelta(hours=24)
    calls_24h = [
        r for r in ledger.read(cfg)
        if r.get("kind") == "call" and (dt := _parse_iso(r.get("ts"), tz)) is not None and dt >= cutoff
    ]
    first_calls = [r for r in calls_24h if r.get("adapter") == first_adapter]
    if len(first_calls) < 3 or any(bool(r.get("ok")) for r in first_calls[-3:]):
        return {"status": "ok", "result": f"首家 {first_adapter} 近 24 小时无连续 3 次失败"}

    until_dt = now_dt + datetime.timedelta(hours=6)
    state_dir = get_instance_state_dir(cfg.name)
    skip_file = state_dir / "adapter-skip.json"
    if not dry_run:
        data: dict[str, Any] = {}
        if skip_file.is_file():
            try:
                loaded = json.loads(skip_file.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    data = loaded
            except (OSError, json.JSONDecodeError):
                pass
        data[first_adapter] = {
            "until": until_dt.isoformat(),
            "written_at": now_dt.isoformat(),
            "reason": "consecutive-3-failures",
        }
        atomic_write(skip_file, json.dumps(data, ensure_ascii=False, indent=2) + "\n", [state_dir])

    return {
        "status": "healed",
        "problem": f"回退链首家 {first_adapter} 最近连续 3 次调用失败",
        "action": f"写入 adapter-skip.json 跳过 {first_adapter} 6 小时",
        "result": f"调用器将直接从下一家开始直至 {until_dt.isoformat()}",
        "tries": 1,
    }


def check_sync_ok(
    cfg: InstanceConfig, before: str | None, dry_run: bool, hooks: dict[str, Any]
) -> dict[str, Any]:
    """7. 同步：配了 [sync] remote 时跑 rclone lsd <remote> --timeout 15s，失败重试 1 次。"""
    ext = load_ext(cfg)
    if not ext.sync_remote:
        return {"status": "ok", "result": "未配置 [sync] remote，跳过"}

    tool_argv = shlex.split(ext.sync_tool)
    argv = [*tool_argv, "lsd", ext.sync_remote, "--timeout", "15s"]
    run_cmd: Callable[..., tuple[int, str]] = hooks.get("cmd_runner") or _default_cmd_runner

    rc1, err1 = run_cmd(argv, timeout=20)
    if rc1 == 0:
        return {"status": "ok", "result": f"远端 {ext.sync_remote} 可达"}

    rc2, err2 = run_cmd(argv, timeout=20)
    if rc2 == 0:
        return {
            "status": "healed",
            "problem": f"首次连接远端 {ext.sync_remote} 失败（{err1[:80]}）",
            "action": "重试 1 次 rclone lsd",
            "result": "第 2 次连接成功",
            "tries": 1,
        }
    return {
        "status": "failed",
        "problem": f"远端同步连接 {ext.sync_remote} 连续 2 次失败（{err2[:80]}）",
        "action": "重试 1 次 rclone lsd",
        "suggestion": f"人工检查 rclone 配置与远端 {ext.sync_remote} 授权",
        "tries": 2,
    }


def digest_catchup_dates(
    cfg: InstanceConfig,
    now_dt: datetime.datetime,
) -> tuple[datetime.date, datetime.date]:
    """根据配置的 digest_time 判断补跑应针对昨天还是今天，返回 (as_of_date, for_date)。"""
    from hushai_agent.install import detect_existing_digest_time

    d_time = detect_existing_digest_time(cfg)
    dh, dm = (int(x) for x in d_time.split(":", 1))
    today_sched = now_dt.replace(hour=dh, minute=dm, second=0, microsecond=0)
    today_d = now_dt.date()
    if now_dt < today_sched:
        return today_d - datetime.timedelta(days=1), today_d
    return today_d, today_d + datetime.timedelta(days=1)


def last_weekly_schedule_date(cfg: InstanceConfig, now_dt: datetime.datetime) -> datetime.date:
    """返回截至 now_dt 最近一次应跑周报的预定日期（默认周日 digest_time+45m）。"""
    from hushai_agent.install import _weekly_time, detect_existing_digest_time

    w_time = _weekly_time(detect_existing_digest_time(cfg))
    wh, wm = (int(x) for x in w_time.split(":", 1))
    target_weekday = 6  # Sunday
    days_back = (now_dt.weekday() - target_weekday) % 7
    cand_date = now_dt.date() - datetime.timedelta(days=days_back)
    cand_dt = now_dt.replace(year=cand_date.year, month=cand_date.month, day=cand_date.day, hour=wh, minute=wm, second=0, microsecond=0)
    if cand_dt > now_dt:
        cand_date -= datetime.timedelta(days=7)
    return cand_date


def scheduled_weekly_date(cfg: InstanceConfig, now_dt: datetime.datetime) -> datetime.date | None:
    """定时器触发的周报该用哪天作基准：预定日当天照常跑；晚到的补跑若预定日之后已有周报则返回 None（跳过）。"""
    sched_d = last_weekly_schedule_date(cfg, now_dt)
    if sched_d == now_dt.date():
        return sched_d

    def _late(raw: str) -> bool:
        try:
            return sched_d <= datetime.date.fromisoformat(raw) <= now_dt.date()
        except ValueError:
            return False

    reports_dir = cfg.vault / "hub" / "reports"
    if reports_dir.is_dir() and any(_late(p.stem[len("weekly-") :]) for p in reports_dir.glob("weekly-*.md")):
        return None
    for r in ledger.read(cfg):
        if r.get("kind") == "job" and r.get("job") == "weekly" and r.get("result") in ("ok", "degraded") and not r.get("dry_run"):
            if _late(str(r.get("date") or "").strip()):
                return None
    return sched_d


def weekly_ran_in_cycle(
    cfg: InstanceConfig,
    sched_date: datetime.date,
    parsed_rows: list[tuple[dict[str, Any], datetime.datetime | None]],
    now_date: datetime.date,
) -> bool:
    """判断预定周日周期（同 ISO 周或已在预定日后运行）是否已有成功/降级周报，防止跨周补跑重出。"""
    target_iso = sched_date.isocalendar()[:2]
    reports_dir = cfg.vault / "hub" / "reports"
    if reports_dir.is_dir():
        for p in reports_dir.glob("weekly-*.md"):
            stem_d = p.stem[len("weekly-") :]
            try:
                rep_d = datetime.date.fromisoformat(stem_d)
            except ValueError:
                continue
            if rep_d.isocalendar()[:2] == target_iso or sched_date <= rep_d <= now_date:
                return True
    for r, _ in parsed_rows:
        if r.get("kind") == "job" and r.get("job") == "weekly" and r.get("result") in ("ok", "degraded") and not r.get("dry_run"):
            raw_d = str(r.get("date") or "").strip()
            try:
                rd = datetime.date.fromisoformat(raw_d)
            except ValueError:
                continue
            if rd.isocalendar()[:2] == target_iso or sched_date <= rd <= now_date:
                return True
    return False
