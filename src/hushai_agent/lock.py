"""实例任务互斥锁：基于 fcntl.flock(LOCK_EX | LOCK_NB) 实现，不写 pid、不删锁文件。"""

from __future__ import annotations

import fcntl
import os
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


@contextmanager
def runner_lock(cfg: InstanceConfig | str | Path) -> Iterator[Path]:
    """对 <状态目录>/runner.lock 获取非阻塞排他锁；失败抛出 LockBusy。"""
    state_dir = _resolve_state_dir(cfg)
    ensure_dir(state_dir, [state_dir])
    lock_file = ensure_writable(state_dir / "runner.lock", [state_dir])

    fd = os.open(os.fspath(lock_file), os.O_CREAT | os.O_RDWR, 0o644)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (BlockingIOError, OSError) as exc:
            os.close(fd)
            raise LockBusy(f"无法获取实例锁（已有任务正在运行）: {lock_file}") from exc

        try:
            yield lock_file
        finally:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                os.close(fd)
    except LockBusy:
        raise
