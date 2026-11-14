"""agy 适配项：问答式（提示词作为最后一个参数 -p=<提示词>）与代理式（派活用）。"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from hushai_agent.adapters import proc
from hushai_agent.adapters.base import Adapter, Result
from hushai_agent.config import InstanceConfig
from hushai_agent.fsio import atomic_write
from hushai_agent.paths import get_instance_state_dir

# Linux 单个命令行参数上限约 128 KB，留出余量
MAX_PROMPT_BYTES = 100_000
PROMPT_TOO_LARGE = "prompt-too-large"
# 子进程超时比 --print-timeout 多留几秒，让 agy 自己先超时并给出错误信息
KILL_GRACE_SECONDS = 2
CONVERSATION_KEYS = ("conversation_id", "conversationId", "conversation")


def prompt_too_large(prompt: str) -> bool:
    """提示词 UTF-8 字节数是否超过上限。"""
    return len(prompt.encode("utf-8")) > MAX_PROMPT_BYTES


def _conversation_id(payload: dict[str, Any]) -> str:
    """取会话编号（兼容几种可能的字段名）。"""
    for key in CONVERSATION_KEYS:
        val = payload.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip()
    return ""


def parse_result(rc: int, stdout: str, stderr: str, duration: float, model: str) -> Result:
    """解析 agy 的 JSON 输出：status == SUCCESS 且返回码为 0 才算成功。"""
    payload = proc.parse_json(stdout)
    if payload is None:
        reason = proc.snippet(stderr) or f"标准输出不是 JSON: {proc.snippet(stdout, 120)}"
        if rc != 0:
            reason = f"返回码 {rc}: {reason}"
        return Result(ok=False, error=reason, model=model, duration=duration, raw=stdout)

    status = str(payload.get("status") or "")
    response = payload.get("response")
    text = response if isinstance(response, str) else ("" if response is None else str(response))
    used_model = str(payload.get("model") or model)
    common: dict[str, Any] = {
        "text": text,
        "usage": proc.usage_of(payload),
        "model": used_model,
        "duration": duration,
        "conversation_id": _conversation_id(payload),
        "raw": stdout,
    }
    if status == "SUCCESS" and rc == 0:
        return Result(ok=True, data=proc.data_of(payload), **common)

    error = proc.snippet(payload.get("error")) or proc.snippet(stderr)
    if not error:
        error = f"status={status or '缺失'}，返回码 {rc}"
    interrupted = status == "ERROR" and "interrupt" in error.lower()
    return Result(ok=False, error=error, interrupted=interrupted, **common)


def _schema_file_name(meta: dict[str, Any] | None) -> str:
    """结构文件名取任务名，只保留小写字母、数字与连字符。"""
    job = str((meta or {}).get("job") or "adhoc").lower()
    clean = re.sub(r"[^a-z0-9-]+", "-", job).strip("-")
    return f"{clean or 'adhoc'}.schema.json"


class AgyAdapter(Adapter):
    """agy 适配项：问答式以空目录为工作目录；代理式在指定工作目录带工具干活。"""

    def __init__(self, cfg: InstanceConfig, settings: proc.CommandSettings) -> None:
        self.name = settings.name
        self.cfg = cfg
        self.settings = settings

    @property
    def timeout(self) -> int:
        """配置的超时秒数（<名字>.timeout，默认 180）。"""
        return self.settings.timeout

    def capabilities(self) -> set[str]:
        """agy 同时支持问答式与代理式。"""
        return {"complete", "agent"}

    def write_schema(self, schema: dict[str, Any], meta: dict[str, Any] | None) -> Path:
        """把结构写到 <状态目录>/schemas/<job>.schema.json 并返回路径。"""
        state_dir = get_instance_state_dir(self.cfg.name)
        path = state_dir / "schemas" / _schema_file_name(meta)
        content = json.dumps(schema, ensure_ascii=False, indent=2) + "\n"
        return atomic_write(path, content, [state_dir])

    def _effective_timeout(self, timeout: int | None) -> int:
        return int(timeout) if timeout and timeout > 0 else self.settings.timeout

    def complete(
        self,
        prompt: str,
        tier: str,
        schema: dict[str, Any] | None = None,
        timeout: int | None = None,
        meta: dict[str, Any] | None = None,
    ) -> Result:
        """问答式调用；模型未配置或提示词超过 100,000 字节时不调用，直接失败。"""
        model = self.settings.model_for(tier)
        if not model:
            return Result(ok=False, error=self.settings.missing_model_error(tier))
        if prompt_too_large(prompt):
            return Result(ok=False, error=PROMPT_TOO_LARGE, model=model)

        seconds = self._effective_timeout(timeout)
        argv = [
            *self.settings.command,
            "--output-format", "json",
            "--model", model,
            "--print-timeout", f"{seconds}s",
            "--disable-slash-commands",
        ]
        if schema is not None:
            argv += ["--json-schema", str(self.write_schema(schema, meta))]
        # 提示词必须是最后一个参数，且写成 -p=<提示词> 一个整体
        argv.append(f"-p={prompt}")

        cwd = proc.empty_cwd(self.cfg)
        rc, out, err, duration = proc.run(argv, None, cwd, seconds + KILL_GRACE_SECONDS)
        return parse_result(rc, out, err, duration, model)

    def agent_run(
        self,
        task_text: str,
        tier: str,
        workdir: str | Path,
        timeout: int | None = None,
        resume_id: str | None = None,
    ) -> Result:
        """代理式调用（派活用）：在 workdir 里以 accept-edits 模式干活。

        不使用任何跳过权限确认的参数。
        """
        model = self.settings.model_for(tier)
        if not model:
            return Result(ok=False, error=self.settings.missing_model_error(tier))
        if prompt_too_large(task_text):
            return Result(ok=False, error=PROMPT_TOO_LARGE, model=model)

        seconds = self._effective_timeout(timeout)
        argv = [
            *self.settings.command,
            "--mode", "accept-edits",
            "--output-format", "json",
            "--model", model,
            "--print-timeout", f"{seconds}s",
        ]
        if resume_id:
            argv += ["--conversation", resume_id]
        argv.append(f"-p={task_text}")

        rc, out, err, duration = proc.run(argv, None, workdir, seconds + KILL_GRACE_SECONDS)
        return parse_result(rc, out, err, duration, model)
