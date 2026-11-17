"""汇总与周报的日期边界、演练模型门槛与检查点计划构建辅助。"""

from __future__ import annotations

import datetime
import hashlib
from typing import Any

from hushai_agent import checkpoint, clock, items, ledger, runner
from hushai_agent.config import InstanceConfig
from hushai_agent.config_ext import ExtConfig
from hushai_agent.digest_apply import build_append_targets_manifest, build_closing_plan
from hushai_agent.inboxscan import InboxRecord
from hushai_agent.items import FlexDate


def sha256_text(text: str) -> str:
    """计算 UTF-8 文本的 SHA-256 摘要。"""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def should_call_model_in_run(
    cfg: InstanceConfig,
    job: str,
    *,
    dry_run: bool,
    with_model: bool,
) -> bool:
    """判断本轮是否调用大模型：正常模式调用；演练模式仅在 --with-model 或全链路为 fake 时调用。"""
    if not dry_run or with_model:
        return True
    chain = runner._get_job_chain(cfg, job)
    if not chain:
        return False
    return all(
        (cfg.adapters.get(name, {}).get("type") or name).strip() == "fake"
        for name, _ in chain
    )


def resolve_default_digest_dates(
    cfg: InstanceConfig,
    date_arg: str | None = None,
    for_date_arg: str | None = None,
) -> tuple[datetime.date | str, datetime.date | str | None]:
    """解析 CLI 默认日期：到了 digest_time 才把今日待办翻到明天；之前跑一律写当天。

    凌晨 (<06:00) 且昨日还没有成功的 digest 时，基准日回指昨日（补昨天那份）。
    """
    if date_arg and date_arg.strip():
        for_d = for_date_arg.strip() if for_date_arg and for_date_arg.strip() else None
        return date_arg.strip(), for_d

    now_dt = clock.now(cfg)
    today_d = now_dt.date()
    if now_dt.hour < 6:
        yesterday = today_d - datetime.timedelta(days=1)
        rows = ledger.read(cfg)
        y_str = yesterday.isoformat()
        ran_yesterday = any(
            r.get("kind") == "job"
            and r.get("job") == "digest"
            and r.get("date") == y_str
            and r.get("result") in ("ok", "degraded")
            and not r.get("dry_run")
            for r in rows
        )
        if not ran_yesterday and not (cfg.vault / "hub" / "reports" / f"daily-{y_str}.md").is_file():
            for_d = for_date_arg.strip() if for_date_arg and for_date_arg.strip() else today_d
            return yesterday, for_d

    if for_date_arg and for_date_arg.strip():
        return today_d, for_date_arg.strip()
    from hushai_agent.install import detect_existing_digest_time

    dh, dm = (int(x) for x in detect_existing_digest_time(cfg).split(":", 1))
    if now_dt < now_dt.replace(hour=dh, minute=dm, second=0, microsecond=0):
        return today_d, today_d
    return today_d, None


def execute_digest_commit_plan(
    inst_cfg: InstanceConfig,
    *,
    run_id: str,
    as_of_date: FlexDate,
    target_for_date: FlexDate,
    all_items: list[items.OpenItem],
    records: list[InboxRecord],
    ai_rest: list[InboxRecord],
    degraded: str | None,
    today_text: str,
    status_text: str,
    index_text: str,
    summary: dict[str, Any],
    daily_text: str,
    cand_lines: list[str],
    mech_cand_count: int,
    ext: ExtConfig,
    expected_cand_pairs: list[tuple[str, str]],
    corr_lines: list[str],
    cand_manifest: list[dict[str, str]],
    cortex_res: dict[str, Any],
    archived_status: list[Any],
) -> dict[str, Any]:
    """构造并执行 digest 的检查点提交计划。"""
    as_of_str = as_of_date.isoformat()
    keep_items, archive_items = items.split_archive(all_items, as_of_date)
    open_items_text = items.render_open_items(keep_items, as_of_date)
    rest_id_set = {r.line_id for r in ai_rest}
    advance_ids = (
        [r.line_id for r in records if r.line_id not in rest_id_set]
        if degraded
        else [r.line_id for r in records]
    )
    plan: dict[str, Any] = {
        "version": 1,
        "kind": "digest",
        "run_id": run_id,
        "as_of": as_of_str,
        "for_date": target_for_date.isoformat(),
        "open_items": {
            "content": open_items_text,
            "sha256": sha256_text(open_items_text),
            "archived_lines": [
                {"id": it.id, "line": items.format_item_line(it)} for it in archive_items
            ],
        },
        "atomic_targets": {
            "hub/today.md": {"content": today_text, "sha256": sha256_text(today_text)},
            "hub/status.md": {"content": status_text, "sha256": sha256_text(status_text)},
            "hub/status-index.md": {"content": index_text, "sha256": sha256_text(index_text)},
        },
        "daily_state": {
            "run_id": run_id,
            "as_of": as_of_str,
            "cur_facts": summary,
            "is_mechanical": False,
            "daily_report_rel": f"hub/reports/daily-{as_of_str}.md",
            "daily_report_text": daily_text,
            "daily_report_sha256": sha256_text(daily_text),
        },
        "candidates_add": {
            "lines": cand_lines,
            "n_user": mech_cand_count,
            "limit": ext.candidates_pending,
            "expected_pairs": expected_cand_pairs,
        },
        "corrections": corr_lines,
        "append_targets": build_append_targets_manifest(cand_manifest, cortex_res),
        "status_archive": [
            {
                "domain": r.domain,
                "topic": r.topic,
                "summary": r.summary,
                "verified": r.verified.isoformat() if r.verified else None,
                "src": list(r.src),
            }
            for r in archived_status
        ],
        "advance_ids": advance_ids,
        "closing": build_closing_plan(
            inst_cfg,
            as_of_date,
            ai_rest,
            commit_msg=f"digest {as_of_str}",
            ledger_job="digest",
        ),
    }
    return checkpoint.execute_commit_plan(inst_cfg, plan)
