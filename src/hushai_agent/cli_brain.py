"""brain 子命令：第二大脑的编译、检索、体检、投放、核实与开关。"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from hushai_agent import brain_ask, brain_cfg, brain_lint, clock, install
from hushai_agent.brain_ingest import run_ingest
from hushai_agent.brain_page import DROP_DIR, STATUS_VERIFIED, clean_title, load_pages, name_map, norm, write_page
from hushai_agent.brain_raw import FILE_MAX_BYTES, FILE_SUFFIXES
from hushai_agent.config import load_instance_config
from hushai_agent.fsio import atomic_write


def register_brain_commands(subparsers: Any) -> None:
    p = subparsers.add_parser("brain", help="第二大脑：把显式交给它的材料编译成互相链接的页面")
    sub = p.add_subparsers(dest="brain_command", required=True)

    def add(name: str, help_text: str) -> argparse.ArgumentParser:
        sp = sub.add_parser(name, help=help_text)
        sp.add_argument("--instance", required=True, help="实例名称")
        sp.add_argument("--json", action="store_true", help="以 JSON 输出")
        return sp

    add("status", "开关、上次编译、页数与待编译积压")
    sp = add("ingest", "编译待处理原料（定时器每晚一次；未开启时需 --force）")
    sp.add_argument("--force", action="store_true", help="未开启也跑一次")
    sp.add_argument("--max-calls", type=int, default=None, help="本次最多调几次模型（默认 [brain] max_calls_per_run）")
    sp.add_argument("--since", default=None, help="只收此日起的收件行（默认 [brain] since）")
    sp.add_argument("--dry-run", action="store_true", help="只看会送几批、多长，不调模型不写盘")
    sp = add("ask", "本地检索相关页面（不调模型）")
    sp.add_argument("question", help="问题")
    sp.add_argument("--limit", type=int, default=5, help="最多返回几页")
    add("lint", "体检：断链、重名、孤页、缺来源、隔离领域、待复核")
    sp = add("add", "把一个 .md/.txt 文件放进投放文件夹 hub/exchange/in/入脑/，下次编译时入脑")
    sp.add_argument("path", help="文件路径；- 表示从标准输入读（需 --name）")
    sp.add_argument("--name", default=None, help="存成的文件名（不含扩展名）")
    sp = add("verify", "使用者核实过某页后标「已核实」（只在使用者明确说过时用）")
    sp.add_argument("title", help="页面标题或别名")
    sp = add("enable", "打开第二大脑；默认只收今天起的新材料")
    sp.add_argument("--since", default=None, help="从哪天的收件行开始收（YYYY-MM-DD）")
    add("disable", "关闭第二大脑（页面保留）")
    sp = add("units", "生成第二大脑定时器单元到 --out（只生成，不安装）")
    sp.add_argument("--out", required=True, help="输出目录（需已存在）")


def _emit(args: argparse.Namespace, data: Any, text: str) -> int:
    print(json.dumps(data, ensure_ascii=False, indent=2) if args.json else text)
    return 0


def _add_file(cfg: Any, src: str, name: str | None) -> str:
    if src == "-":
        if not name:
            raise ValueError("从标准输入读时必须给 --name")
        text, suffix = sys.stdin.read(), ".md"
    else:
        path = Path(src).expanduser()
        if path.suffix.lower() not in FILE_SUFFIXES:
            raise ValueError(f"只收 {'/'.join(FILE_SUFFIXES)} 文件: {path.name}")
        text, suffix = path.read_text(encoding="utf-8"), path.suffix.lower()
        name = name or path.stem
    if len(text.encode("utf-8")) > FILE_MAX_BYTES:
        raise ValueError("文件超过 64 KB，请拆分后再放")
    base = clean_title(name) or "未命名"
    folder = cfg.vault / "hub" / DROP_DIR
    target = folder / f"{base}{suffix}"
    n = 2
    while target.exists():
        target, n = folder / f"{base}-{n}{suffix}", n + 1
    atomic_write(target, text, [cfg.vault])
    return f"hub/{DROP_DIR}/{target.name}"


def _verify(cfg: Any, title: str) -> str:
    page = name_map(load_pages(cfg.vault / "hub")).get(norm(title))
    if page is None:
        raise ValueError(f"没有找到页面: {title}")
    page.meta["status"] = STATUS_VERIFIED
    page.meta["verified"] = clock.today(cfg).isoformat()
    write_page(cfg.vault, page)
    return f"hub/{page.rel}"


def dispatch_brain_command(args: argparse.Namespace) -> int | None:
    if args.command != "brain":
        return None
    cfg = load_instance_config(args.instance)
    cmd = args.brain_command
    if cmd == "ingest":
        res = run_ingest(cfg, force=args.force, max_calls=args.max_calls, since=args.since, dry_run=args.dry_run)
        text = (f"跳过：{res['skipped']}" if res.get("skipped") else
                f"编译 {res['done']}/{res['pending']} 批｜新建 {len(res['created'])}｜更新 {len(res['updated'])}"
                + (f"｜中止：{res['degraded']}" if res["degraded"] else ""))
        _emit(args, res, text)
        return 1 if res.get("degraded") else 0
    if cmd == "ask":
        res = brain_ask.ask(cfg, args.question, limit=args.limit)
        return _emit(args, res, brain_ask.render_text(res))
    if cmd in ("lint", "status"):
        bcfg = brain_cfg.load_brain_config(cfg)
        rep = brain_lint.lint(cfg, bcfg)
        if cmd == "lint":
            return _emit(args, rep, brain_lint.render_text(rep))
        last = brain_cfg.load_state(cfg).get("last_run") or {}
        data = {"enabled": bcfg.enabled, "since": bcfg.since, "time": bcfg.time, "last_run": last,
                "pages": rep["pages"], "by_type": rep["by_type"], "level": rep["level"],
                "pending_lines": rep["pending_lines"], "pending_files": rep["pending_files"]}
        head = f"第二大脑：{'已开启' if bcfg.enabled else '已关闭'}｜收件起点 {bcfg.since or '-'}｜每晚 {bcfg.time}"
        tail = f"上次编译：{last.get('at', '无')}（{last.get('done', 0)}/{last.get('pending', 0)} 批）"
        return _emit(args, data, "\n".join([head, *brain_lint.render_text(rep).splitlines()[:2], tail]))
    if cmd == "add":
        rel = _add_file(cfg, args.path, args.name)
        return _emit(args, {"added": rel}, f"已放入 {rel}，下次编译时入脑")
    if cmd == "verify":
        rel = _verify(cfg, args.title)
        return _emit(args, {"verified": rel}, f"已标「已核实」：{rel}")
    if cmd in ("enable", "disable"):
        new = brain_cfg.set_enabled(cfg, cmd == "enable", since=getattr(args, "since", None))
        data: dict[str, Any] = {"enabled": new.enabled, "since": new.since}
        hint = f"｜收件起点 {new.since}｜定时器：hushai brain units --out <目录> 后按提示安装"
        if cmd == "disable":
            data["units_removed"], data["system_residue"] = install.remove_brain_units(cfg)
            hint = "".join(f"\n请在 SSH 里删除系统单元：sudo rm /etc/systemd/system/{n}" for n in data["system_residue"])
        return _emit(args, data, f"第二大脑已{'开启' if new.enabled else '关闭'}{hint}")
    written = install.render_brain_units(cfg, args.out)
    return _emit(args, {"written": [str(p) for p in written]},
                 "\n".join([*(f"✓ {p}" for p in written), *install.next_steps(cfg, written)]))
