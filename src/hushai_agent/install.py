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


def render_units(
    cfg: InstanceConfig,
    out_dir: str | Path,
    digest_time: str = DEFAULT_DIGEST_TIME,
    memory_max: str = DEFAULT_MEMORY_MAX,
    port: int = DEFAULT_PORT,
) -> list[Path]:
    """渲染全部单元模板，返回写出的文件。"""
    out = Path(out_dir).expanduser()
    if not out.is_dir():
        raise ValueError(f"输出目录不存在: {out_dir}")
    if not TIME_RE.match(digest_time):
        raise ValueError(f"--digest-time 需为 HH:MM: {digest_time}")
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
        "{{DIGEST_TIME}}": digest_time,
        "{{WEEKLY_TIME}}": _weekly_time(digest_time),
        "{{PORT}}": str(int(port)),
        "{{CONFIG_ROOT}}": str(get_config_root()),
        "{{STATE_ROOT}}": str(get_state_root()),
    }
    written: list[Path] = []
    for tpl in sorted(TEMPLATES_DIR.iterdir()):
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
