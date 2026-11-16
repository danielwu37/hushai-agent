"""交互式配置菜单基础 IO、校验回滚与通用提示原语。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from hushai_agent import install
from hushai_agent.brain_cfg import load_brain_config
from hushai_agent.config import ConfigError, load_instance_config
from hushai_agent.config_edit import apply_config_updates
from hushai_agent.paths import get_instance_config_dir


@dataclass
class MenuIO:
    """可替换的终端读写通道，便于单元测试驱动菜单交互。"""

    read_line: Callable[[str], str] = input
    write: Callable[[str], None] = print


def _prompt(io: MenuIO, text: str, default: str | None = None) -> str:
    suffix = f" [回车保留: {default}]" if default not in (None, "") else ""
    raw = io.read_line(f"{text}{suffix}: ").strip()
    if not raw and default is not None:
        return default
    return raw


def _prompt_non_empty(io: MenuIO, text: str, default: str = "") -> str:
    val = _prompt(io, text, default=default if default else None).strip()
    if not val:
        raise ValueError(f"{text} 不能为空")
    return val


def _prompt_bool(io: MenuIO, label: str, current: bool) -> bool:
    cur_str = "开启(true)" if current else "关闭(false)"
    raw = io.read_line(f"{label}（当前: {cur_str}，输入 y 开启 / n 关闭 / 回车保持）: ").strip().lower()
    if not raw:
        return current
    if raw in ("y", "yes", "1", "true", "on", "是", "开"):
        return True
    if raw in ("n", "no", "0", "false", "off", "否", "关"):
        return False
    raise ValueError(f"无法识别的开关输入: {raw}")


def _offer_regen_units(io: MenuIO, instance: str) -> None:
    cfg = load_instance_config(instance)
    bcfg = load_brain_config(cfg)
    units_dir = get_instance_config_dir(instance) / "units"
    ans = io.read_line(f"是否立即重新生成 systemd 单元到 {units_dir}？[y/N]: ").strip().lower()
    if ans not in ("y", "yes", "1", "是"):
        return
    units_dir.mkdir(parents=True, exist_ok=True)
    dtime = install.detect_existing_digest_time(cfg)
    written = install.render_units(cfg, units_dir, digest_time=dtime, with_brain=bcfg.enabled)
    io.write(f"✓ 已重新生成 {len(written)} 个单元文件至: {units_dir}")
    for line in install.next_steps(cfg, written):
        io.write(f"  {line}")


def _safe_apply(
    io: MenuIO,
    instance: str,
    updates: dict[str, dict[str, str | None]],
    ok_msg: str = "设置已保存并校验通过。",
    effect_hint: str = "立即生效（下次命令或网页请求自动读取新配置）",
    prompt_units: bool = False,
) -> bool:
    try:
        apply_config_updates(instance, updates)
        io.write(f"✓ {ok_msg}")
        if effect_hint:
            io.write(f"  [生效方式] {effect_hint}")
        if prompt_units:
            _offer_regen_units(io, instance)
        return True
    except (ConfigError, ValueError, OSError) as exc:
        io.write(f"✗ 修改未保存（已自动回滚原配置）: {exc}")
        return False


def _run_guarded(io: MenuIO, action: Callable[[], None]) -> None:
    try:
        action()
    except (ConfigError, ValueError, OSError, RuntimeError) as exc:
        io.write(f"✗ 操作失败: {exc}")


def _menu_loop(
    io: MenuIO,
    title: str,
    render_summary: Callable[[], list[str]],
    items: list[tuple[str, Callable[[], None]]],
    stop_when: Callable[[], bool] | None = None,
) -> None:
    while True:
        io.write(f"\n--- {title} ---")
        for line in render_summary():
            io.write(f"  {line}")
        for idx, (label, _) in enumerate(items, start=1):
            io.write(f"  {idx}. {label}")
        io.write("  0. 返回上级菜单")
        raw = io.read_line("请选择编号: ").strip()
        if raw in ("0", "q", "back"):
            return
        if raw.isdigit() and 1 <= int(raw) <= len(items):
            _run_guarded(io, items[int(raw) - 1][1])
            if stop_when and stop_when():
                return
        else:
            io.write("请输入有效编号。")
