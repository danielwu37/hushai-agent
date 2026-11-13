"""模型适配层基类与统一返回结果数据结构。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class Result:
    """单次模型问答调用的返回结果。"""

    ok: bool
    data: dict[str, Any] | None = None
    text: str = ""
    usage: dict[str, Any] = field(default_factory=dict)
    model: str = ""
    duration: float = 0.0
    error: str = ""


class Adapter:
    """AI 模型适配项抽象基类。"""

    name: str = ""

    def complete(
        self,
        prompt: str,
        tier: str,
        schema: dict[str, Any] | None = None,
        timeout: int = 60,
        meta: dict[str, Any] | None = None,
    ) -> Result:
        """执行无工具问答式调用。"""
        raise NotImplementedError

    def capabilities(self) -> set[str]:
        """返回适配项支持的能力集合。"""
        return {"complete"}
