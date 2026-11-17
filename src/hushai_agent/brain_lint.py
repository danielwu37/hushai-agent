"""第二大脑体检（卡帕西 LLM Wiki 的 lint）：纯本地、一遍扫完（O(页数)），不调模型。

查：断链、孤页、重名、缺来源、头信息不全、隔离领域漏进来、久未更新待复核、目录超行、待编译积压。
"""

from __future__ import annotations

import datetime
from collections import Counter
from typing import Any

from hushai_agent import clock
from hushai_agent.brain_cfg import BrainConfig, load_brain_config, load_state
from hushai_agent.brain_page import STATUS_VERIFIED, TYPE_DIRS, load_pages, norm, wikilinks
from hushai_agent.brain_raw import pending_files, pending_lines
from hushai_agent.config import InstanceConfig, is_local_only_domain


def lint(cfg: InstanceConfig, bcfg: BrainConfig | None = None) -> dict[str, Any]:
    bcfg = bcfg or load_brain_config(cfg)
    hub = cfg.vault / "hub"
    pages = load_pages(hub)
    owners: dict[str, set[str]] = {}
    for page in pages:
        for name in page.names():
            if norm(name):
                owners.setdefault(norm(name), set()).add(page.rel)
    inbound: Counter[str] = Counter()
    broken: list[str] = []
    for page in pages:
        text = "\n".join([page.body, *page.refs, page.user])
        for target in wikilinks(text):
            rels = owners.get(norm(target))
            if not rels:
                broken.append(f"{page.title} -> [[{target}]]")
            else:
                inbound.update(r for r in rels if r != page.rel)
    today = clock.today(cfg)
    cutoff = (today - datetime.timedelta(days=bcfg.stale_days)).isoformat()
    report: dict[str, Any] = {
        "pages": len(pages),
        "by_type": dict(Counter(p.kind for p in pages)),
        "broken_links": broken,
        "orphans": [p.title for p in pages if not inbound[p.rel] and p.kind != "source"],
        "duplicates": sorted({" / ".join(sorted(r)) for r in owners.values() if len(r) > 1}),
        "no_refs": [p.title for p in pages if not p.refs],
        "bad_meta": [p.rel for p in pages if p.kind not in TYPE_DIRS or not p.meta.get("title")],
        "private": [p.rel for p in pages if is_local_only_domain(str(p.meta.get("domain") or ""), cfg)],
        "stale": [p.title for p in pages
                  if str(p.meta.get("updated") or "") < cutoff and p.meta.get("status") != STATUS_VERIFIED],
    }
    idx = hub / "index.md"
    report["index_lines"] = len(idx.read_text(encoding="utf-8").splitlines()) if idx.is_file() else 0
    state = load_state(cfg)
    files, too_big = pending_files(cfg, state)
    report["pending_lines"] = len(pending_lines(cfg, state, bcfg.since))
    report["pending_files"] = len(files)
    report["too_big"] = too_big
    report["level"] = level_of(report, bcfg.index_max_lines)
    return report


def level_of(report: dict[str, Any], index_max: int) -> str:
    if report["private"]:
        return "FAIL"
    if report["broken_links"] or report["duplicates"] or report["bad_meta"] or report["index_lines"] > index_max:
        return "WARN"
    return "OK"


def render_text(report: dict[str, Any]) -> str:
    by_type = "、".join(f"{k} {v}" for k, v in sorted(report["by_type"].items())) or "无"
    lines = [
        f"第二大脑体检：{report['level']}｜共 {report['pages']} 页（{by_type}）｜目录 {report['index_lines']} 行",
        f"待编译：收件 {report['pending_lines']} 行、投放文件 {report['pending_files']} 个",
    ]
    labels = (("private", "隔离领域漏进来"), ("broken_links", "断链"), ("duplicates", "重名"),
              ("bad_meta", "头信息不全"), ("no_refs", "缺来源"), ("stale", "久未更新待复核"),
              ("orphans", "孤页（没有别的页链接它）"), ("too_big", "投放文件过大"))
    for key, label in labels:
        items = report.get(key) or []
        if items:
            shown = "；".join(items[:8]) + (f"；…共 {len(items)} 项" if len(items) > 8 else "")
            lines.append(f"- {label}：{shown}")
    return "\n".join(lines)


def check_item(cfg: InstanceConfig) -> Any:
    """给 doctor 用：一行结论。隔离领域漏进来为 FAIL；断链、重名、目录超行为 WARN。"""
    from hushai_agent.validate import CheckItem

    bcfg = load_brain_config(cfg)
    rep = lint(cfg, bcfg)
    state = "已开启" if bcfg.enabled else "已关闭"
    if rep["level"] == "OK":
        msg = f"第二大脑{state}，{rep['pages']} 页，待编译收件 {rep['pending_lines']} 行、文件 {rep['pending_files']} 个"
    else:
        msg = brain_lint_summary(rep)
    return CheckItem(id="second-brain", level=rep["level"], message=msg)


def brain_lint_summary(rep: dict[str, Any]) -> str:
    parts = [f"{label} {len(rep[key])}" for key, label in
             (("private", "隔离领域漏进"), ("broken_links", "断链"), ("duplicates", "重名"), ("bad_meta", "头信息不全"))
             if rep[key]]
    return "第二大脑：" + "、".join(parts) + f"｜目录 {rep['index_lines']} 行｜详见 hushai brain lint"
