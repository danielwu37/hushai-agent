"""claude 适配项：问答式调用 claude 命令行，提示词走标准输入。"""

from __future__ import annotations

import json
from typing import Any

from hushai_agent.adapters import proc
from hushai_agent.adapters.base import Adapter, Result
from hushai_agent.config import InstanceConfig

# 固定参数，不可配置掉：
# --safe-mode 不加载任何全局规则、钩子、插件，后台任务不会把使用者的全局配置带进来，也不触发会话钩子；
# --no-session-persistence 不在磁盘留会话记录。
FIXED_FLAGS = ("--tools", "", "--safe-mode", "--no-session-persistence")


def build_argv(command: tuple[str, ...], model: str, schema: dict[str, Any] | None) -> list[str]:
    """拼出问答式命令行参数（不含提示词，提示词走标准输入）。"""
    argv = [*command, "-p", "--model", model, "--output-format", "json", *FIXED_FLAGS]
    if schema is not None:
        argv += ["--json-schema", json.dumps(schema, ensure_ascii=False)]
    return argv


def parse_result(rc: int, stdout: str, stderr: str, duration: float, model: str) -> Result:
    """解析 claude 的 JSON 输出为统一的 Result。"""
    payload = proc.parse_json(stdout)
    if payload is None:
        reason = proc.snippet(stderr) or "标准输出不是 JSON"
        if rc != 0:
            reason = f"返回码 {rc}: {reason}"
        else:
            reason = f"标准输出不是 JSON: {proc.snippet(stdout, 120)}"
        return Result(ok=False, error=reason, model=model, duration=duration, raw=stdout)

    model_usage = payload.get("modelUsage")
    used_model = model
    if isinstance(model_usage, dict) and model_usage:
        used_model = str(next(iter(model_usage)))

    text = payload.get("result")
    text_str = text if isinstance(text, str) else ("" if text is None else str(text))
    usage = proc.usage_of(payload)

    if rc != 0 or bool(payload.get("is_error")):
        error = proc.snippet(text_str) or proc.snippet(stderr) or f"返回码 {rc}"
        return Result(
            ok=False,
            text=text_str,
            usage=usage,
            model=used_model,
            duration=duration,
            error=error,
            raw=stdout,
        )

    return Result(
        ok=True,
        data=proc.data_of(payload),
        text=text_str,
        usage=usage,
        model=used_model,
        duration=duration,
        raw=stdout,
    )


class ClaudeAdapter(Adapter):
    """以空目录为工作目录、不带任何工具的 claude 问答式适配项。"""

    def __init__(self, cfg: InstanceConfig, settings: proc.CommandSettings) -> None:
        self.name = settings.name
        self.cfg = cfg
        self.settings = settings

    @property
    def timeout(self) -> int:
        """配置的超时秒数（<名字>.timeout，默认 180）。"""
        return self.settings.timeout

    def capabilities(self) -> set[str]:
        """v1 只提供问答式。"""
        return {"complete"}

    def complete(
        self,
        prompt: str,
        tier: str,
        schema: dict[str, Any] | None = None,
        timeout: int | None = None,
        meta: dict[str, Any] | None = None,
    ) -> Result:
        """执行一次问答式调用；档位模型未配置时不调用，直接失败。"""
        model = self.settings.model_for(tier)
        if not model:
            return Result(ok=False, error=self.settings.missing_model_error(tier))

        effective_timeout = timeout if timeout and timeout > 0 else self.settings.timeout
        argv = build_argv(self.settings.command, model, schema)
        cwd = proc.empty_cwd(self.cfg)
        rc, out, err, duration = proc.run(argv, prompt, cwd, effective_timeout)
        return parse_result(rc, out, err, duration, model)
