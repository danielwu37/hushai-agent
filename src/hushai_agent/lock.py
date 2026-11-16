"""实例任务互斥锁：基于 fcntl.flock(LOCK_EX | LOCK_NB) 实现，不写 pid、不删锁文件。"""

from __future__ import annotations

import fcntl
import os
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Iterator

from hushai_agent.config import InstanceConfig
from hushai_agent.fsio import atomic_write, ensure_dir
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


def model_wait_seconds() -> float:
    """外部模型/CLI 调用锁（model.lock）的等待秒数。"""
    raw = os.environ.get("HUSHAI_MODEL_LOCK_WAIT")
    if raw is None or not raw.strip():
        raw = os.environ.get("HUSHAI_LOCK_WAIT", "300")
    try:
        return max(0.0, float(raw))
    except ValueError:
        return 300.0


@contextmanager
def model_lock(cfg: InstanceConfig | str | Path, wait_seconds: float | None = None) -> Iterator[Path]:
    """独立模型调用锁（model.lock），与 runner.lock 分离防止重型 CLI 并发及嵌套死锁。"""
    wait = model_wait_seconds() if wait_seconds is None else max(0.0, wait_seconds)
    with runner_lock(cfg, name="model.lock", wait_seconds=wait) as path:
        yield path


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


def _dirty_flag_path(cfg: InstanceConfig | str | Path, key: str) -> Path:
    state_dir = _resolve_state_dir(cfg)
    safe_key = "".join(ch if (ch.isalnum() or ch in "-_") else "_" for ch in key)
    return state_dir / f"dirty-{safe_key}.flag"


def mark_dirty(cfg: InstanceConfig | str | Path, key: str) -> Path:
    """在锁冲突时落盘待补跑标记，避免实时事件被静默丢弃。"""
    state_dir = _resolve_state_dir(cfg)
    flag_path = _dirty_flag_path(cfg, key)
    atomic_write(flag_path, "1\n", [state_dir])
    return flag_path


def has_dirty(cfg: InstanceConfig | str | Path, key: str) -> bool:
    """判断是否存在待补跑标记。"""
    return _dirty_flag_path(cfg, key).is_file()


def clear_dirty(cfg: InstanceConfig | str | Path, key: str) -> bool:
    """原子清除待补跑标记，若原先存在返回 True。"""
    flag_path = _dirty_flag_path(cfg, key)
    try:
        os.unlink(ensure_writable(flag_path, [_resolve_state_dir(cfg)]))
        return True
    except FileNotFoundError:
        return False


def acquire_or_mark_dirty(
    cfg: InstanceConfig | str | Path,
    key: str,
    lock_name: str = "runner.lock",
) -> Any | None:
    """非阻塞拿锁；锁忙时落盘 dirty 标记并立刻复试一次（消除释锁竞态窗口）。"""
    try:
        ctx = runner_lock(cfg, name=lock_name, wait_seconds=0.0)
        ctx.__enter__()
        return ctx
    except LockBusy:
        mark_dirty(cfg, key)
        try:
            ctx = runner_lock(cfg, name=lock_name, wait_seconds=0.0)
            ctx.__enter__()
            clear_dirty(cfg, key)
            return ctx
        except LockBusy:
            return None


def drain_after_unlock(
    cfg: InstanceConfig | str | Path,
    key: str,
    lock_name: str,
    callback: Callable[[], Any],
    max_rounds: int = 5,
) -> int:
    """持锁者释放锁后调用：若存在 dirty 标记则重拿锁排空新到达事件，最多循环 max_rounds 轮。"""
    drained = 0
    for _ in range(max_rounds):
        if not has_dirty(cfg, key):
            break
        try:
            ctx = runner_lock(cfg, name=lock_name, wait_seconds=0.0)
            ctx.__enter__()
        except LockBusy:
            break
        try:
            clear_dirty(cfg, key)
            callback()
            drained += 1
        finally:
            ctx.__exit__(None, None, None)
    return drained
