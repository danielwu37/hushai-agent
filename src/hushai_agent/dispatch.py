"""派活模块（hushai dispatch）：生成任务书、在隔离工作目录执行代理式调用并自动续跑。"""

from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path
from typing import Any

from hushai_agent import adapters, clock, ledger
from hushai_agent.adapters.base import Result
from hushai_agent.config import (
    DEFAULT_DOMAINS_EN,
    DEFAULT_DOMAINS_ZH,
    VALID_TIERS,
    InstanceConfig,
    load_instance_config,
)
from hushai_agent.fsio import atomic_write, ensure_dir, read_for_prompt
from hushai_agent.guard import GuardError
from hushai_agent.paths import get_instance_state_dir

JOB_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,40}$")
RESUME_PROMPT = "连接刚才中断了，请检查已写文件是否完整，继续完成任务并做最终汇报"
REPORT_TAIL_CHARS = 2000

_DOMAIN_ALIASES: dict[str, str] = {
    **dict(zip(DEFAULT_DOMAINS_ZH, DEFAULT_DOMAINS_EN)),
    **dict(zip(DEFAULT_DOMAINS_EN, DEFAULT_DOMAINS_ZH)),
}


def _load_domain_background(cfg: InstanceConfig, domain: str | None) -> str:
    """读取领域库 AGENTS.md；读不到或属于 local_only 时返回「无」。"""
    if not domain or not domain.strip():
        return "无"
    clean = domain.strip()
    if "/" in clean or "\\" in clean or ".." in clean:
        return "无"

    candidates = [clean]
    alias = _DOMAIN_ALIASES.get(clean)
    if alias and alias not in candidates:
        candidates.append(alias)

    for cand in candidates:
        target = cfg.vault / "domains" / cand / "AGENTS.md"
        try:
            text = read_for_prompt(target, cfg.vault, cfg.local_only).strip()
            if text:
                return text
        except GuardError:
            return "无"
        except (OSError, UnicodeDecodeError):
            continue
    return "无"


def _read_task_file(cfg: InstanceConfig, task_path_raw: str | Path) -> str:
    """读取外部或 vault 内的任务书文件；若位于 vault 内则经 read_for_prompt 护栏读取。"""
    tpath = Path(task_path_raw).expanduser()
    if not tpath.is_file():
        raise FileNotFoundError(f"任务书文件不存在: {task_path_raw}")

    real_vault = Path(os.path.realpath(os.fspath(cfg.vault)))
    real_task = Path(os.path.realpath(os.fspath(tpath)))
    abs_vault = Path(os.path.abspath(os.fspath(cfg.vault)))
    abs_task = Path(os.path.abspath(os.fspath(tpath)))
    if (
        real_task == real_vault
        or real_task.is_relative_to(real_vault)
        or abs_task == abs_vault
        or abs_task.is_relative_to(abs_vault)
    ):
        return read_for_prompt(tpath, cfg.vault, cfg.local_only)
    return tpath.read_text(encoding="utf-8")


def _build_goal_task(cfg: InstanceConfig, goal: str, domain: str | None) -> str:
    """不调模型，用内置模板 prompts/dispatch-task.zh.md 生成自足任务书。"""
    clean_goal = (goal or "").strip()
    if not clean_goal:
        raise ValueError("任务目标（--goal）不能为空")
    tpl_path = Path(__file__).resolve().parent / "prompts" / "dispatch-task.zh.md"
    tpl = tpl_path.read_text(encoding="utf-8")
    background = _load_domain_background(cfg, domain)
    return tpl.replace("{{GOAL}}", clean_goal).replace("{{BACKGROUND}}", background)


def _validate_workdir(cfg: InstanceConfig, workdir: str | Path) -> Path:
    """校验 --workdir 已存在、为目录且不得位于 vault 之内。"""
    wd = Path(workdir).expanduser()
    if not wd.exists() or not wd.is_dir():
        raise ValueError(f"工作目录不存在或不是目录: {workdir}")

    real_vault = Path(os.path.realpath(os.fspath(cfg.vault)))
    real_wd = Path(os.path.realpath(os.fspath(wd)))
    abs_vault = Path(os.path.abspath(os.fspath(cfg.vault)))
    abs_wd = Path(os.path.abspath(os.fspath(wd)))
    if (
        real_wd == real_vault
        or real_wd.is_relative_to(real_vault)
        or abs_wd == abs_vault
        or abs_wd.is_relative_to(abs_vault)
    ):
        raise GuardError(f"派活工作目录不得位于 vault 之内: {workdir}")
    return real_wd


def _list_workdir_files(work_dir: Path) -> list[str]:
    """列出工作目录内的全部文件（相对 POSIX 路径，跳过 .git 内部目录）。"""
    files: list[str] = []
    for root, dirs, names in os.walk(os.fspath(work_dir)):
        dirs[:] = [d for d in sorted(dirs) if d != ".git"]
        root_path = Path(root)
        for fname in sorted(names):
            files.append((root_path / fname).relative_to(work_dir).as_posix())
    return files


def _save_raw_output(path: Path, res: Result, state_dir: Path) -> None:
    """将单次调用原始输出写入 out.json / out.pre.json。"""
    if res.raw and res.raw.strip():
        content = res.raw if res.raw.endswith("\n") else res.raw + "\n"
    else:
        payload = {
            "ok": res.ok,
            "model": res.model,
            "duration": res.duration,
            "text": res.text,
            "error": res.error,
            "conversation_id": res.conversation_id,
        }
        content = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    atomic_write(path, content, [state_dir])


def _invoke_and_record(
    cfg: InstanceConfig,
    adapter: Any,
    adapter_name: str,
    task_text: str,
    tier: str,
    work_dir: Path,
    timeout: int | None,
    resume_id: str | None = None,
) -> tuple[Result, float]:
    """执行一次 agent_run 并记录 call 账本行。"""
    t0 = time.monotonic()
    try:
        res = adapter.agent_run(
            task_text=task_text,
            tier=tier,
            workdir=work_dir,
            timeout=timeout,
            resume_id=resume_id,
        )
    except Exception as exc:
        res = Result(ok=False, error=str(exc), duration=round(time.monotonic() - t0, 4))
    duration = res.duration if res.duration > 0 else round(time.monotonic() - t0, 4)
    ledger.record(
        cfg,
        "call",
        job="dispatch",
        adapter=adapter_name,
        tier=tier,
        model=res.model,
        ok=res.ok,
        duration=duration,
        usage=res.usage,
        error=res.error,
    )
    return res, duration


def run_dispatch(
    cfg: InstanceConfig | str,
    name: str,
    task_file: str | Path | None = None,
    goal: str | None = None,
    domain: str | None = None,
    adapter_name: str | None = None,
    tier: str = "heavy",
    workdir: str | Path | None = None,
    timeout: int | None = None,
) -> dict[str, Any]:
    """执行一次派活任务：生成 task.md、调代理式适配项、中断自动续跑一次并落盘记账。"""
    if not name or not JOB_NAME_RE.match(name):
        raise ValueError(f"非法任务名 '{name}'（需匹配 [a-z0-9][a-z0-9-]{{0,40}}）")
    if tier not in VALID_TIERS:
        raise ValueError(f"非法档位 '{tier}'（只能是 light 或 heavy）")
    if (task_file is None and goal is None) or (task_file is not None and goal is not None):
        raise ValueError("必须且只能指定 --task 或 --goal 之一")

    inst_cfg = cfg if isinstance(cfg, InstanceConfig) else load_instance_config(cfg)
    state_dir = get_instance_state_dir(inst_cfg.name)
    job_dir = state_dir / "jobs" / name
    if job_dir.exists():
        raise ValueError(f"任务目录已存在，拒绝覆盖历史: {job_dir}")

    chosen_adapter = (adapter_name or inst_cfg.dispatch_adapter or "").strip()
    if not chosen_adapter:
        raise ValueError("未指定派活适配项：请传 --adapter 或在配置 [jobs] 节设置 dispatch")
    adapter = adapters.build(inst_cfg, chosen_adapter)
    if "agent" not in adapter.capabilities() or not hasattr(adapter, "agent_run"):
        raise ValueError(f"适配项 '{chosen_adapter}' 不支持代理式派活（v1 仅 agy 类型支持）")

    validated_workdir = _validate_workdir(inst_cfg, workdir) if workdir is not None else None
    if task_file is not None:
        task_text = _read_task_file(inst_cfg, task_file)
        if not task_text.strip():
            raise ValueError(f"任务书文件为空: {task_file}")
    else:
        task_text = _build_goal_task(inst_cfg, goal or "", domain)

    ensure_dir(job_dir, [state_dir])
    atomic_write(job_dir / "task.md", task_text, [state_dir])
    work_dir = validated_workdir or ensure_dir(job_dir / "work", [state_dir])

    today_str = clock.today(inst_cfg).isoformat()
    if ledger.calls_today(inst_cfg) >= inst_cfg.max_daily_calls:
        err_msg = "超出每日调用预算（max_daily_calls 已用尽）"
        ledger.record(
            inst_cfg, "job", job="dispatch", date=today_str, dry_run=False,
            result="failed", note=f"name={name}; reason=budget",
        )
        return {
            "ok": False, "name": name, "adapter": chosen_adapter, "tier": tier,
            "model": "", "duration": 0.0, "resumed": False, "conversation_id": "",
            "job_dir": str(job_dir), "workdir": str(work_dir),
            "files": _list_workdir_files(work_dir), "report_tail": "", "error": err_msg,
        }

    res1, dur1 = _invoke_and_record(
        inst_cfg, adapter, chosen_adapter, task_text, tier, work_dir, timeout, resume_id=None,
    )
    resumed = False
    final_res = res1
    total_duration = dur1

    if res1.interrupted and res1.conversation_id:
        _save_raw_output(job_dir / "out.pre.json", res1, state_dir)
        if ledger.calls_today(inst_cfg) >= inst_cfg.max_daily_calls:
            err_msg = "中断后准备续跑时每日调用预算已用尽"
            ledger.record(
                inst_cfg, "job", job="dispatch", date=today_str, dry_run=False,
                result="failed", note=f"name={name}; resumed=False; reason=budget-on-resume",
            )
            return {
                "ok": False, "name": name, "adapter": chosen_adapter, "tier": tier,
                "model": res1.model, "duration": total_duration, "resumed": False,
                "conversation_id": res1.conversation_id, "job_dir": str(job_dir),
                "workdir": str(work_dir), "files": _list_workdir_files(work_dir),
                "report_tail": (res1.text or res1.error or "")[-REPORT_TAIL_CHARS:],
                "error": err_msg,
            }
        res2, dur2 = _invoke_and_record(
            inst_cfg, adapter, chosen_adapter, RESUME_PROMPT, tier, work_dir, timeout,
            resume_id=res1.conversation_id,
        )
        _save_raw_output(job_dir / "out.json", res2, state_dir)
        resumed = True
        final_res = res2
        total_duration = round(dur1 + dur2, 4)
    else:
        _save_raw_output(job_dir / "out.json", res1, state_dir)

    note = f"name={name}; adapter={chosen_adapter}; tier={tier}; resumed={resumed}"
    if final_res.error:
        note = f"{note}; error={final_res.error[:120]}"
    ledger.record(
        inst_cfg, "job", job="dispatch", date=today_str, dry_run=False,
        result="ok" if final_res.ok else "failed", note=note,
    )
    report_src = final_res.text or final_res.error or ""
    return {
        "ok": bool(final_res.ok), "name": name, "adapter": chosen_adapter, "tier": tier,
        "model": final_res.model, "duration": total_duration, "resumed": resumed,
        "conversation_id": final_res.conversation_id, "job_dir": str(job_dir),
        "workdir": str(work_dir), "files": _list_workdir_files(work_dir),
        "report_tail": report_src[-REPORT_TAIL_CHARS:], "error": final_res.error,
    }
