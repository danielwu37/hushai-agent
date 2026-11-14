"""模型适配层基类与统一返回结果数据结构。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class Result:
    """单次模型调用（问答式或代理式）的返回结果。

    conversation_id、interrupted 只有代理式调用会填；raw 是外部命令的原始标准输出，
    供派活落盘 out.json 使用。
    """

    ok: bool
    data: dict[str, Any] | None = None
    text: str = ""
    usage: dict[str, Any] = field(default_factory=dict)
    model: str = ""
    duration: float = 0.0
    error: str = ""
    conversation_id: str = ""
    interrupted: bool = False
    raw: str = ""


class Adapter:
    """AI 模型适配项抽象基类。"""

    name: str = ""

    def complete(
        self,
        prompt: str,
        tier: str,
        schema: dict[str, Any] | None = None,
        timeout: int | None = None,
        meta: dict[str, Any] | None = None,
    ) -> Result:
        """执行无工具问答式调用；timeout 为 None 时用适配项自己的配置。"""
        raise NotImplementedError

    def capabilities(self) -> set[str]:
        """返回适配项支持的能力集合。"""
        return {"complete"}
