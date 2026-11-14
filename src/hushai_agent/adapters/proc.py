"""真适配项（claude、agy）的公共部分：适配项配置、空工作目录、不经 shell 的子进程调用。"""

from __future__ import annotations

import json
import shlex
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Sequence

from hushai_agent.config import InstanceConfig
from hushai_agent.fsio import clear_dir, ensure_dir
from hushai_agent.paths import get_instance_state_dir

DEFAULT_TIMEOUT = 180
TIMEOUT_RC = -9
NOT_FOUND_RC = 127
CANNOT_RUN_RC = 126
ERROR_SNIPPET = 300
TIERS = ("light", "heavy")


@dataclass(frozen=True)
class CommandSettings:
    """一个真适配项在 [adapters] 节里的配置。"""

    name: str
    adapter_type: str
    command: tuple[str, ...]
    timeout: int = DEFAULT_TIMEOUT
    models: dict[str, str] = field(default_factory=dict)

    def model_for(self, tier: str) -> str:
        """返回该档位的模型名；未配置时返回空串。"""
        return self.models.get(tier, "") if tier in TIERS else ""

    def missing_model_error(self, tier: str) -> str:
        """档位模型未配置时给出的错误说明。"""
        if tier not in TIERS:
            return f"适配项 '{self.name}' 不认识档位 '{tier}'（只能是 light 或 heavy）"
        return (
            f"适配项 '{self.name}' 未配置 {tier} 档模型名，"
            f"请在配置 [adapters] 节填写 {self.name}.{tier} = <模型名>"
        )


def load_settings(name: str, adapter_type: str, props: dict[str, str]) -> CommandSettings:
    """从 [adapters] 的 <名字>.command / .timeout / .light / .heavy 读出配置。"""
    raw_cmd = (props.get("command") or "").strip() or adapter_type
    try:
        argv = tuple(shlex.split(raw_cmd))
    except ValueError as exc:
        raise ValueError(f"[adapters] {name}.command 无法解析: {exc}") from exc
    if not argv:
        raise ValueError(f"[adapters] {name}.command 不能为空")

    timeout = DEFAULT_TIMEOUT
    raw_timeout = (props.get("timeout") or "").strip()
    if raw_timeout:
        try:
            timeout = int(raw_timeout)
        except ValueError as exc:
            raise ValueError(f"[adapters] {name}.timeout 必须为整数秒: {raw_timeout}") from exc
        if timeout <= 0:
            raise ValueError(f"[adapters] {name}.timeout 必须大于 0: {timeout}")

    models = {tier: (props.get(tier) or "").strip() for tier in TIERS}
    return CommandSettings(
        name=name,
        adapter_type=adapter_type,
        command=argv,
        timeout=timeout,
        models=models,
    )


def empty_cwd(cfg: InstanceConfig) -> Path:
    """返回 <状态目录>/empty-cwd：不存在则建，里面有东西则清空（只清这个目录，经路径护栏）。

    所有问答式调用都以它为工作目录，避免命令行工具扫描目录、加载目录里的规则文件。
    """
    state_dir = get_instance_state_dir(cfg.name)
    target = state_dir / "empty-cwd"
    real = ensure_dir(target, [state_dir])
    if any(real.iterdir()):
        clear_dir(real, [state_dir])
    return real


def _to_text(value: Any) -> str:
    """把超时异常里可能是字节串的输出转成文本。"""
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


def run(
    argv: Sequence[str],
    stdin_text: str | None,
    cwd: str | Path,
    timeout: int | float | None,
) -> tuple[int, str, str, float]:
    """不经 shell 执行外部命令，返回 (返回码, 标准输出, 标准错误, 耗时秒)。

    - 超时：返回码记 -9，标准错误末尾附说明。
    - 命令不存在：返回码 127。
    - stdin_text 为 None 时标准输入接空设备，避免外部命令等待输入。
    """
    args = [str(a) for a in argv]
    t0 = time.monotonic()
    if not Path(cwd).is_dir():
        return CANNOT_RUN_RC, "", f"工作目录不存在: {cwd}", 0.0

    kwargs: dict[str, Any] = {
        "cwd": str(cwd),
        "capture_output": True,
        "text": True,
        "encoding": "utf-8",
        "errors": "replace",
        "timeout": timeout if timeout and timeout > 0 else None,
        "check": False,
        "shell": False,
    }
    if stdin_text is None:
        kwargs["stdin"] = subprocess.DEVNULL
    else:
        kwargs["input"] = stdin_text

    try:
        completed = subprocess.run(args, **kwargs)
    except subprocess.TimeoutExpired as exc:
        duration = round(time.monotonic() - t0, 4)
        err = _to_text(exc.stderr).strip()
        note = f"超时：{timeout} 秒内未结束，已终止"
        return TIMEOUT_RC, _to_text(exc.stdout), f"{err}\n{note}".strip(), duration
    except FileNotFoundError:
        duration = round(time.monotonic() - t0, 4)
        return NOT_FOUND_RC, "", f"命令不存在: {args[0] if args else ''}", duration
    except OSError as exc:
        duration = round(time.monotonic() - t0, 4)
        return CANNOT_RUN_RC, "", f"命令无法执行: {exc}", duration

    duration = round(time.monotonic() - t0, 4)
    return completed.returncode, completed.stdout or "", completed.stderr or "", duration


def parse_json(text: str) -> dict[str, Any] | None:
    """把标准输出整体解析为 JSON 对象；不是 JSON 对象时返回 None。"""
    stripped = (text or "").strip()
    if not stripped:
        return None
    try:
        obj = json.loads(stripped)
    except json.JSONDecodeError:
        return None
    return obj if isinstance(obj, dict) else None


def snippet(text: Any, limit: int = ERROR_SNIPPET) -> str:
    """取错误信息的前若干字，非字符串先转成 JSON 文本。"""
    if text is None:
        return ""
    if not isinstance(text, str):
        text = json.dumps(text, ensure_ascii=False)
    return text.strip()[:limit]


def usage_of(payload: dict[str, Any]) -> dict[str, Any]:
    """原样取 usage 字段；不是对象时返回空字典。"""
    usage = payload.get("usage")
    return usage if isinstance(usage, dict) else {}


def data_of(payload: dict[str, Any]) -> dict[str, Any] | None:
    """取 structured_output 字段；不是对象时返回 None。"""
    data = payload.get("structured_output")
    return data if isinstance(data, dict) else None
