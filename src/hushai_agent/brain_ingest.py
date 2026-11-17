"""第二大脑编译（卡帕西 LLM Wiki 的 ingest）：原料 -> 模型 -> 校验 -> 增改页面 -> 目录与日志 -> 提交。

不在对账、汇总路径上跑：由 `hushai brain ingest`（定时器每晚一次，或管家手动）触发。
模型调用不持 runner.lock；只有落盘与提交时短暂持锁，免得挡住会话结束时的对账；
释锁后排空期间被挡下的对账事件。
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from hushai_agent import alerts, brain_write, clock, ledger, lock, reconcile, runner
from hushai_agent.brain_cfg import load_brain_config, load_state, save_state
from hushai_agent.brain_page import TYPE_LABELS, Page, clean_title, load_pages, norm
from hushai_agent.brain_raw import RawBatch, build_batches
from hushai_agent.config import InstanceConfig, is_local_only_domain
from hushai_agent.gitutil import commit_hub_if_git

JOB = "brain"
PAGE_TYPES = ("project", "topic", "person")
MAX_PAGES = 8
BODY_MAX = 4000
RELATED_MAX = 6
RELATED_CHARS = 2500
CATALOG_MAX = 200
_STRIP_SECTIONS = re.compile(r"^##\s*(来源|我的笔记)\s*$.*", re.S | re.M)

SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "pages": {"type": "array", "items": {"type": "object", "properties": {
            "title": {"type": "string"}, "type": {"type": "string"}, "domain": {"type": "string"},
            "aliases": {"type": "array", "items": {"type": "string"}},
            "tags": {"type": "array", "items": {"type": "string"}},
            "summary": {"type": "string"}, "body": {"type": "string"},
            "refs": {"type": "array", "items": {"type": "integer"}},
        }, "required": ["title", "type", "domain", "summary", "body"]}},
        "source": {"type": "object", "properties": {
            "title": {"type": "string"}, "summary": {"type": "string"}, "body": {"type": "string"},
        }},
        "log": {"type": "string"},
    },
    "required": ["pages", "log"],
}


def _one_line(text: Any, limit: int) -> str:
    s = re.sub(r"\s+", " ", str(text or "")).replace("｜", "|").strip()
    return s if len(s) <= limit else s[:limit - 1] + "…"


def related_pages(pages: list[Page], text: str) -> list[Page]:
    """按标题、别名在新材料里出现的次数挑最多 6 页旧页，给模型融合用。"""
    hay = norm(text)
    scored = []
    for page in pages:
        score = sum(3 + len(k) for k in {norm(n) for n in page.names()} if len(k) >= 2 and k in hay)
        if score:
            scored.append((score, page.title, page))
    return [p for _, _, p in sorted(scored, key=lambda t: (-t[0], t[1]))[:RELATED_MAX]]


def build_prompt(cfg: InstanceConfig, batch: RawBatch, pages: list[Page], today: str) -> str:
    catalog = [f"{TYPE_LABELS.get(p.kind, p.kind)}｜{p.title}｜{p.meta.get('summary') or ''}" for p in pages]
    related = related_pages(pages, batch.text)
    rel_text = "\n\n".join(
        f"### {p.title}（{TYPE_LABELS.get(p.kind, p.kind)}）\n{p.body[:RELATED_CHARS]}" for p in related
    )
    tpl = (Path(__file__).resolve().parent / "prompts" / "brain-ingest.zh.md").read_text(encoding="utf-8")
    domains = [d for d in cfg.domains if not is_local_only_domain(d, cfg)]
    return (
        tpl.replace("{{DOMAINS}}", "、".join(domains))
        .replace("{{TODAY}}", today)
        .replace("{{CATALOG}}", "\n".join(catalog[:CATALOG_MAX]) or "（还没有页面）")
        .replace("{{RELATED}}", rel_text or "（无）")
        .replace("{{KIND}}", "收件行" if batch.kind == "lines" else f"原件 {batch.file_rel}")
        .replace("{{MATERIAL}}", batch.text)
    )


def make_validator(cfg: InstanceConfig, batch: RawBatch):
    allowed = {d for d in cfg.domains if not is_local_only_domain(d, cfg)}

    def _validate(data: dict[str, Any] | None) -> tuple[dict[str, Any] | None, str | None]:
        if not isinstance(data, dict) or not isinstance(data.get("pages"), list):
            return None, "缺少 pages 数组"
        pages = []
        for raw in data["pages"][:MAX_PAGES]:
            if not isinstance(raw, dict) or raw.get("type") not in PAGE_TYPES:
                continue
            title, domain = clean_title(raw.get("title")), str(raw.get("domain") or "").strip()
            if not title or domain not in allowed:
                continue
            body = _STRIP_SECTIONS.sub("", str(raw.get("body") or "")).replace("\n---", "\n").strip()[:BODY_MAX]
            refs = sorted({i for i in raw.get("refs") or [] if isinstance(i, int) and 1 <= i <= len(batch.refs)})
            if not body or (batch.kind == "lines" and not refs):
                continue
            pages.append({
                "title": title, "type": raw["type"], "domain": domain, "body": body, "refs": refs,
                "summary": _one_line(raw.get("summary"), 60),
                "aliases": [a for a in map(clean_title, raw.get("aliases") or []) if a][:5],
                "tags": [t for t in map(clean_title, raw.get("tags") or []) if t][:4],
            })
        src = data.get("source") if isinstance(data.get("source"), dict) else {}
        source = {"title": clean_title(src.get("title")), "summary": _one_line(src.get("summary"), 60),
                  "body": _STRIP_SECTIONS.sub("", str(src.get("body") or "")).strip()[:BODY_MAX]}
        if batch.kind == "file" and not (source["summary"] and source["body"]):
            return None, "原件材料缺少 source 摘要"
        return {"pages": pages, "source": source, "log": _one_line(data.get("log"), 60)}, None

    return _validate


def _mark_done(state: dict[str, Any], batch: RawBatch) -> None:
    if batch.kind == "lines":
        state["done_lines"] = list(state.get("done_lines") or []) + batch.line_ids
    else:
        state.setdefault("done_files", {})[batch.file_rel] = batch.file_sha


def run_ingest(cfg: InstanceConfig, *, force: bool = False, max_calls: int | None = None,
               since: str | None = None, dry_run: bool = False) -> dict[str, Any]:
    """编译待处理原料；未开启时跳过（--force 例外）。同一时刻只跑一个编译。"""
    bcfg = load_brain_config(cfg)
    if not bcfg.enabled and not force:
        return {"skipped": "disabled"}
    try:
        with lock.runner_lock(cfg, name="brain.lock", wait_seconds=0):
            return _ingest_locked(cfg, bcfg, max_calls or bcfg.max_calls_per_run, since, dry_run)
    except lock.LockBusy:
        return {"skipped": "lock-busy"}


def _ingest_locked(cfg: InstanceConfig, bcfg: Any, limit: int, since: str | None, dry_run: bool) -> dict[str, Any]:
    vault, hub = cfg.vault, cfg.vault / "hub"
    state = load_state(cfg)
    batches, skipped = build_batches(cfg, state, bcfg.since if since is None else since)
    chain = runner._get_job_chain(cfg, JOB) or runner._get_job_chain(cfg, "digest")
    out: dict[str, Any] = {"pending": len(batches), "done": 0, "created": [], "updated": [],
                           "locked": [], "skipped_files": skipped, "degraded": ""}
    for batch in batches[:limit]:
        now = clock.now(cfg)
        prompt = build_prompt(cfg, batch, load_pages(hub), now.date().isoformat())
        if dry_run:
            out.setdefault("prompts", []).append({"kind": batch.kind, "chars": len(prompt), "refs": len(batch.refs)})
            continue
        meta = {"job": JOB, "date": now.date().isoformat(), "kind": batch.kind}
        data, info = runner.run_model(cfg, JOB, prompt, SCHEMA, meta, make_validator(cfg, batch),
                                      chain=chain, budget=bcfg.max_daily_calls)
        if data is None:
            out["degraded"] = str(info.get("degraded") or "failed")
            out["errors"] = list(info.get("errors") or [])[-3:]
            break
        with lock.runner_lock(cfg, wait_seconds=lock.job_wait_seconds()):
            res = brain_write.apply_result(vault, batch, data, now)
            brain_write.append_log(vault, now, batch, data["log"], res)
            _mark_done(state, batch)
            save_state(cfg, state)
        reconcile.drain_pending_mechanical(cfg)
        out["done"] += 1
        for key in ("created", "updated", "locked"):
            out[key] += res[key]
    return _finish(cfg, bcfg, state, out, dry_run)


def _finish(cfg: InstanceConfig, bcfg: Any, state: dict[str, Any], out: dict[str, Any], dry_run: bool) -> dict[str, Any]:
    now = clock.now(cfg)
    stamp = now.strftime("%Y-%m-%d %H:%M")
    if out["done"] and not dry_run:
        with lock.runner_lock(cfg, wait_seconds=lock.job_wait_seconds()):
            brain_write.write_index(cfg.vault, bcfg.index_max_lines, stamp)
            out["git_committed"] = commit_hub_if_git(cfg.vault, f"brain-ingest {stamp}")
        reconcile.drain_pending_mechanical(cfg)
    if not dry_run:
        state["last_run"] = {"at": stamp, **{k: out[k] for k in ("pending", "done", "degraded")}}
        save_state(cfg, state)
        ledger.record(cfg, "job", job=JOB, date=now.date().isoformat(), dry_run=False,
                      result="degraded" if out["degraded"] else "ok",
                      note=f"done={out['done']}/{out['pending']} {out['degraded']}".strip())
        if out["degraded"] == "all-failed":
            alerts.record_alert(cfg, kind="brain", level="warning", source="brain:ingest",
                                message="第二大脑编译连续失败，材料原样保留待下次", suggestion="hushai brain ingest 手动重试；看 doctor 里模型自检")
        elif out["done"]:
            alerts.resolve_alerts(cfg, "brain:")
    return out
