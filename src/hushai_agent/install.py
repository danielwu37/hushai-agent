"""hushai install units：把服务单元模板渲染到使用者指定的目录。只生成文件，不安装、不启用。"""

from __future__ import annotations

import getpass
import importlib.util
import re
import shutil
import sys
from pathlib import Path

from hushai_agent.config import InstanceConfig
from hushai_agent.paths import get_config_root, get_state_root

TEMPLATES_DIR = Path(__file__).resolve().parent / "templates" / "systemd"
TEMPLATES_BRAIN_DIR = Path(__file__).resolve().parent / "templates" / "systemd-brain"
TEMPLATES_SYNC_DIR = Path(__file__).resolve().parent / "templates" / "systemd-sync"
TIME_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
MEMORY_RE = re.compile(r"^\d+[KMG]$")
DEFAULT_DIGEST_TIME = "21:30"
# 实测（11-14）：一次 agy 或 claude 问答式调用进程树峰值约 260–290 MB，256M 会被杀
DEFAULT_MEMORY_MAX = "512M"
WEEKLY_GAP_MINUTES = 45
DEFAULT_PORT = 8765


def _hushai_command() -> str:
    """命令的绝对路径；没装成命令时退回「解释器 -m 包名」。

    包没装进解释器时，systemd 与钩子里找不到它，要带上 PYTHONPATH。
    """
    found = shutil.which("hushai")
    if found:
        return found
    pkg_parent = Path(__file__).resolve().parent.parent
    try:
        importable = importlib.util.find_spec("hushai_agent", None) is not None and str(pkg_parent) not in sys.path
    except ValueError:
        importable = False
    if importable:
        return f"{sys.executable} -m hushai_agent"
    return f"/usr/bin/env PYTHONPATH={pkg_parent} {sys.executable} -m hushai_agent"


def _weekly_time(digest_time: str) -> str:
    """周重写排在汇总之后，避免同一时刻跑两个重任务。"""
    h, m = map(int, digest_time.split(":"))
    total = (h * 60 + m + WEEKLY_GAP_MINUTES) % (24 * 60)
    return f"{total // 60:02d}:{total % 60:02d}"


def _render_from_dir(tpl_dir: Path, out: Path, values: dict[str, str]) -> list[Path]:
    written: list[Path] = []
    for tpl in sorted(tpl_dir.iterdir()):
        if not tpl.is_file():
            continue
        text = tpl.read_text(encoding="utf-8")
        name = tpl.name
        for key, val in values.items():
            text = text.replace(key, val)
            name = name.replace(key, val)
        # 唯一不经 fsio 路径护栏的写入：输出目录在三个允许根之外，
        # 这里只写使用者指定的目录、只写模板里这几个固定文件名。
        target = out / name
        target.write_text(text, encoding="utf-8")
        written.append(target)
    return written


def detect_existing_digest_time(cfg: InstanceConfig) -> str:
    """从 /etc/systemd/system 或实例 units 目录读取既有 digest.timer 时间，未找到时返回 DEFAULT_DIGEST_TIME。"""
    candidates = [
        Path(f"/etc/systemd/system/hushai-{cfg.name}-digest.timer"),
        get_config_root() / cfg.name / "units" / f"hushai-{cfg.name}-digest.timer",
    ]
    for path in candidates:
        try:
            if not path.is_file():
                continue
            text = path.read_text(encoding="utf-8")
            m = re.search(r"OnCalendar\s*=\s*[^\n]*?\b([01]\d|2[0-3]):([0-5]\d):00\b", text)
            if m:
                return f"{m.group(1)}:{m.group(2)}"
        except OSError:
            continue
    return DEFAULT_DIGEST_TIME


def render_brain_units(cfg: InstanceConfig, out_dir: str | Path, memory_max: str = DEFAULT_MEMORY_MAX) -> list[Path]:
    """渲染第二大脑编译定时器（时间取 [brain] time；只生成文件，不安装、不启用）。"""
    from hushai_agent.brain_cfg import load_brain_config

    out = Path(out_dir).expanduser()
    if not out.is_dir():
        raise ValueError(f"输出目录不存在: {out_dir}")
    if not MEMORY_RE.match(memory_max):
        raise ValueError(f"--memory-max 需形如 256M: {memory_max}")
    values = {
        "{{INSTANCE}}": cfg.name,
        "{{HUSHAI}}": _hushai_command(),
        "{{TZ}}": cfg.timezone,
        "{{USER}}": getpass.getuser(),
        "{{HOME}}": str(Path.home()),
        "{{MEMORY_MAX}}": memory_max,
        "{{BRAIN_TIME}}": load_brain_config(cfg).time,
        "{{CONFIG_ROOT}}": str(get_config_root()),
        "{{STATE_ROOT}}": str(get_state_root()),
    }
    return _render_from_dir(TEMPLATES_BRAIN_DIR, out, values)


def render_units(
    cfg: InstanceConfig,
    out_dir: str | Path,
    digest_time: str | None = None,
    memory_max: str = DEFAULT_MEMORY_MAX,
    port: int = DEFAULT_PORT,
    with_brain: bool = False,
) -> list[Path]:
    """渲染全部单元模板，返回写出的文件（默认 10 个核心单元；配了 [sync] remote 时追加同步定时器；with_brain=True 时追加 second-brain 单元）。"""
    out = Path(out_dir).expanduser()
    if not out.is_dir():
        raise ValueError(f"输出目录不存在: {out_dir}")
    d_time = digest_time if digest_time is not None else detect_existing_digest_time(cfg)
    if not TIME_RE.match(d_time):
        raise ValueError(f"--digest-time 需为 HH:MM: {d_time}")
    if not MEMORY_RE.match(memory_max):
        raise ValueError(f"--memory-max 需形如 256M: {memory_max}")
    values = {
        "{{INSTANCE}}": cfg.name,
        "{{HUSHAI}}": _hushai_command(),
        "{{VAULT}}": str(cfg.vault),
        "{{TZ}}": cfg.timezone,
        "{{USER}}": getpass.getuser(),
        "{{HOME}}": str(Path.home()),
        "{{MEMORY_MAX}}": memory_max,
        "{{DIGEST_TIME}}": d_time,
        "{{WEEKLY_TIME}}": _weekly_time(d_time),
        "{{PORT}}": str(int(port)),
        "{{CONFIG_ROOT}}": str(get_config_root()),
        "{{STATE_ROOT}}": str(get_state_root()),
    }
    written = _render_from_dir(TEMPLATES_DIR, out, values)
    # 配了 [sync] remote 才渲染同步定时器（每 15 分钟一轮）；没配就不出，免得账本里每轮记一次失败
    from hushai_agent.config_ext import load_ext

    if load_ext(cfg).sync_remote:
        written.extend(_render_from_dir(TEMPLATES_SYNC_DIR, out, values))
    if with_brain:
        written.extend(render_brain_units(cfg, out, memory_max=memory_max))
    return written


def next_steps(cfg: InstanceConfig, written: list[Path]) -> list[str]:
    """打印给安装者自己执行的命令（本程序不执行）。"""
    names = sorted(p.name for p in written)
    enable = [n for n in names if n.endswith((".path", ".timer")) or n.endswith("-steward.service")]
    return [
        "以下命令由安装者自己核对后执行，本程序没有安装或启用任何单元：",
        f"  sudo cp {written[0].parent}/hushai-{cfg.name}-*.* /etc/systemd/system/",
        "  sudo systemctl daemon-reload",
        f"  sudo systemctl enable --now {' '.join(enable)}",
        f"  # 需要待办页面时再启用：sudo systemctl enable --now hushai-{cfg.name}-serve.service",
    ]


# 2026-11-16 以前的旧版第二大脑单元（8 个），升级时提示从系统目录删掉
LEGACY_BRAIN_UNITS = tuple(
    f"hushai-{{name}}-{unit}.{ext}"
    for unit in ("brain-daily", "brain-weekly", "resurface", "monthly")
    for ext in ("service", "timer")
)


def remove_brain_units(cfg: InstanceConfig, system_dir: str | Path = "/etc/systemd/system") -> tuple[list[str], list[str]]:
    """第二大脑关闭后清掉 <config>/units 里的 brain 单元（含旧版 8 个），免得跟着 cp 装进系统；返回 (已清理, 系统目录残留)。"""
    current = [p.name.replace("{{INSTANCE}}", cfg.name) for p in sorted(TEMPLATES_BRAIN_DIR.iterdir())]
    names = current + [n.replace("{name}", cfg.name) for n in LEGACY_BRAIN_UNITS]
    units_dir = get_config_root() / cfg.name / "units"
    removed = []
    for name in names:
        if (units_dir / name).is_file():
            (units_dir / name).unlink()
            removed.append(name)
    return removed, [n for n in names if (Path(system_dir) / n).exists()]
