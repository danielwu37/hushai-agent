"""模型适配层工厂与导出接口。"""

from __future__ import annotations

from pathlib import Path

from hushai_agent.adapters.base import Adapter, Result
from hushai_agent.adapters.fake import FakeAdapter
from hushai_agent.config import InstanceConfig

__all__ = ["Adapter", "FakeAdapter", "Result", "build"]


def build(cfg: InstanceConfig, name: str) -> Adapter:
    """按实例配置 [adapters] 节构建对应类型的 Adapter 实例。"""
    if not name or not name.strip():
        raise ValueError("适配项名称不能为空")
    clean_name = name.strip()

    props = cfg.adapters.get(clean_name)
    if props is not None:
        adapter_type = (props.get("type") or clean_name).strip()
    elif clean_name in ("claude", "agy"):
        adapter_type = clean_name
        props = {}
    else:
        raise ValueError(f"未知适配项名称: {clean_name}")

    if adapter_type == "fake":
        fixtures_raw = props.get("fixtures", "").strip()
        if not fixtures_raw:
            raise ValueError(f"fake 适配项 '{clean_name}' 缺少 fixtures 目录配置")
        fixtures_path = Path(fixtures_raw).expanduser()
        if not fixtures_path.is_absolute():
            fixtures_path = cfg.vault / fixtures_path
        models = {
            "light": props.get("light", "").strip() or "fake-light",
            "heavy": props.get("heavy", "").strip() or "fake-heavy",
        }
        return FakeAdapter(
            name=clean_name,
            fixtures_dir=fixtures_path,
            models=models,
            default_inbox_dir=cfg.vault / "hub" / "inbox",
        )

    if adapter_type in ("claude", "agy"):
        raise NotImplementedError(f"适配项类型 '{adapter_type}' 将在 M3 实现")

    raise ValueError(f"未知适配项类型: {adapter_type}")
