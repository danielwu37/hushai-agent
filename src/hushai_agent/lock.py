"""实例任务互斥锁：基于 fcntl.flock(LOCK_EX | LOCK_NB) 实现，不写 pid、不删锁文件。"""

from __future__ import annotations

import fcntl
import os
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from hushai_agent.config import InstanceConfig
from hushai_agent.fsio import ensure_dir
from hushai_agent.guard import ensure_writable
from hushai_agent.paths import get_instance_state_dir


class LockBusy(RuntimeError):
    """实例已有运行中的任务持有 runner.lock。"""


def _resolve_state_dir(cfg: InstanceConfig | str | Path) -> Path:
    """从配置对象、实例名或路径获取状态目录。"""
    if isinstance(cfg, Path):
        return cfg
    if isinstance(cfg, str):
        return get_instance_state_dir(cfg)
    return get_instance_state_dir(cfg.name)


def job_wait_seconds() -> float:
    """定时汇总、周重写等锁的秒数；HUSHAI_LOCK_WAIT 可改（测试里设 0）。"""
    try:
        return max(0.0, float(os.environ.get("HUSHAI_LOCK_WAIT", "60")))
    except ValueError:
        return 60.0


@contextmanager
def runner_lock(cfg: InstanceConfig | str | Path, name: str = "runner.lock", wait_seconds: float = 0.0) -> Iterator[Path]:
    """对 <状态目录>/<name>（默认 runner.lock）获取排他锁；等 wait_seconds 秒仍拿不到就抛出 LockBusy。

    定时汇总传入等待时间：会话结束钩子触发的一次几秒的对账，不该让当晚汇总整个跳过。
    """
    state_dir = _resolve_state_dir(cfg)
    ensure_dir(state_dir, [state_dir])
    lock_file = ensure_writable(state_dir / name, [state_dir])

    fd = os.open(os.fspath(lock_file), os.O_CREAT | os.O_RDWR, 0o644)
    try:
        deadline = time.monotonic() + max(0.0, wait_seconds)
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except (BlockingIOError, OSError) as exc:
                if time.monotonic() >= deadline:
                    os.close(fd)
                    raise LockBusy(f"无法获取实例锁（已有任务正在运行）: {lock_file}") from exc
                time.sleep(1.0)

        try:
            yield lock_file
        finally:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                os.close(fd)
    except LockBusy:
        raise
