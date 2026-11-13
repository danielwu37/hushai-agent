"""本地离线测试用 fake 适配项：读取样例 JSON 并解析来源行定位符。"""

from __future__ import annotations

import copy
import datetime
import json
import re
from pathlib import Path
from typing import Any

from hushai_agent.adapters.base import Adapter, Result
from hushai_agent.inboxscan import iter_inbox_lines

LOCATOR_RE = re.compile(
    r"^(?P<stem>\d{4}-\d{2}-\d{2}-[a-z][a-z0-9]{1,15}(?:-auto)?)#(?P<idx>\d+)$"
)


def _build_stem_index(inbox_dir: str | Path | None) -> dict[str, list[str]]:
    """扫描收件目录，按收件文件名 stem 建立合法行 line_id 列表索引。"""
    if not inbox_dir:
        return {}
    by_stem: dict[str, list[str]] = {}
    for rec in iter_inbox_lines(inbox_dir):
        by_stem.setdefault(rec.path.stem, []).append(rec.line_id)
    return by_stem


def _resolve_locator(token: str, by_stem: dict[str, list[str]]) -> str:
    """将形如 '2026-03-02-laptop#3' 的定位符解析为对应第 3 条合法行的 line_id；失败则原样保留。"""
    match = LOCATOR_RE.match(token)
    if not match:
        return token
    stem = match.group("stem")
    idx = int(match.group("idx"))
    lines = by_stem.get(stem)
    if not lines or idx < 1 or idx > len(lines):
        return token
    return lines[idx - 1]


def resolve_source_line_ids(node: Any, by_stem: dict[str, list[str]]) -> Any:
    """递归遍历 JSON 结构，解析所有键名为 source_line_ids 的数组中的来源定位符。"""
    if isinstance(node, dict):
        out: dict[str, Any] = {}
        for k, v in node.items():
            if k == "source_line_ids" and isinstance(v, list):
                out[k] = [
                    _resolve_locator(item, by_stem) if isinstance(item, str) else item
                    for item in v
                ]
            else:
                out[k] = resolve_source_line_ids(v, by_stem)
        return out
    if isinstance(node, list):
        return [resolve_source_line_ids(item, by_stem) for item in node]
    return node


class FakeAdapter(Adapter):
    """不联网的测试适配项，按 <fixtures>/<job>-<date>.json 返回预设结果。"""

    def __init__(
        self,
        name: str,
        fixtures_dir: str | Path,
        models: dict[str, str] | None = None,
        default_inbox_dir: str | Path | None = None,
    ) -> None:
        self.name = name
        self.fixtures_dir = Path(fixtures_dir)
        self.models = models or {"light": "fake-light", "heavy": "fake-heavy"}
        self.default_inbox_dir = Path(default_inbox_dir) if default_inbox_dir else None

    def complete(
        self,
        prompt: str,
        tier: str,
        schema: dict[str, Any] | None = None,
        timeout: int = 60,
        meta: dict[str, Any] | None = None,
    ) -> Result:
        """从样例目录读取对应 job-date.json 并返回 Result。"""
        actual_meta = meta or {}
        model_name = self.models.get(tier, f"fake-{tier}")
        job = str(actual_meta.get("job", ""))
        raw_date = actual_meta.get("date", "")
        date_str = (
            raw_date.isoformat()
            if isinstance(raw_date, datetime.date)
            else str(raw_date)
        )

        fixture_path = self.fixtures_dir / f"{job}-{date_str}.json"
        if not fixture_path.is_file():
            empty_data = copy.deepcopy(actual_meta.get("empty", {}))
            return Result(ok=True, data=empty_data, model=model_name)

        try:
            payload = json.loads(fixture_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            return Result(ok=False, error=f"读取样例文件失败: {exc}", model=model_name)

        if isinstance(payload, dict) and "__fail__" in payload:
            return Result(
                ok=False,
                data=None,
                error=str(payload["__fail__"]),
                model=model_name,
            )

        if isinstance(payload, dict) and "__raw__" in payload:
            return Result(
                ok=True,
                data=None,
                text=str(payload["__raw__"]),
                model=model_name,
            )

        inbox_dir = (
            actual_meta["inbox_dir"]
            if "inbox_dir" in actual_meta
            else self.default_inbox_dir
        )
        by_stem = _build_stem_index(inbox_dir)
        resolved = resolve_source_line_ids(payload, by_stem)
        return Result(ok=True, data=resolved, model=model_name)
