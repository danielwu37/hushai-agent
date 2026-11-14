"""M3a 单元测试用本地桩脚本生成器（绝不调用真实外部 AI 命令行）。"""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
from pathlib import Path
from typing import Any

from hushai_agent.vault import init_instance

STUB_SOURCE = '''import json
import os
import sys
import time
from pathlib import Path

cfg = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
args = sys.argv[2:]
stdin_text = "" if sys.stdin.isatty() else sys.stdin.read()
cwd = os.getcwd()
cwd_entries = sorted(os.listdir(cwd))

schema_content = None
if "--json-schema" in args:
    idx = args.index("--json-schema")
    if idx + 1 < len(args):
        # claude 传的是 JSON 字符串本身，当路径探测会因文件名过长抛 OSError
        try:
            sp = Path(args[idx + 1])
            if sp.is_file():
                schema_content = sp.read_text(encoding="utf-8")
        except OSError:
            schema_content = args[idx + 1]

with Path(cfg["log"]).open("a", encoding="utf-8") as fh:
    fh.write(
        json.dumps(
            {
                "args": args,
                "stdin": stdin_text,
                "cwd": cwd,
                "cwd_entries": cwd_entries,
                "schema_content": schema_content,
            },
            ensure_ascii=False,
        )
        + "\\n"
    )

count_file = Path(cfg["count_file"]) if cfg.get("count_file") else None
call_idx = 0
if count_file is not None:
    call_idx = int(count_file.read_text(encoding="utf-8")) if count_file.exists() else 0
    count_file.write_text(str(call_idx + 1), encoding="utf-8")

steps = cfg.get("steps") or [cfg]
step = steps[min(call_idx, len(steps) - 1)]

if step.get("sleep"):
    time.sleep(float(step["sleep"]))
for rel, text in (step.get("write_files") or {}).items():
    out_p = Path(cwd) / rel
    out_p.parent.mkdir(parents=True, exist_ok=True)
    out_p.write_text(text, encoding="utf-8")
if step.get("stderr"):
    sys.stderr.write(step["stderr"])
if step.get("raw_stdout") is not None:
    sys.stdout.write(step["raw_stdout"])
elif "stdout_json" in step:
    sys.stdout.write(json.dumps(step["stdout_json"], ensure_ascii=False))
sys.exit(int(step.get("rc", 0)))
'''


def ensure_stub_script(base_dir: Path) -> Path:
    """在临时目录中写出桩脚本文件并返回其路径。"""
    stub_path = base_dir / "ai_stub.py"
    if not stub_path.exists():
        stub_path.write_text(STUB_SOURCE, encoding="utf-8")
    return stub_path


def make_stub_command(
    base_dir: Path,
    tag: str,
    spec: dict[str, Any],
) -> tuple[str, Path]:
    """生成单个桩配置并返回 (command 字符串, 调用日志 jsonl 路径)。"""
    stub_path = ensure_stub_script(base_dir)
    log_path = base_dir / f"stub-{tag}.log.jsonl"
    count_path = base_dir / f"stub-{tag}.count"
    cfg_path = base_dir / f"stub-{tag}.cfg.json"
    payload = {
        **spec,
        "log": str(log_path),
        "count_file": str(count_path),
    }
    cfg_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    cmd = f"{shlex.quote(sys.executable)} {shlex.quote(str(stub_path))} {shlex.quote(str(cfg_path))}"
    return cmd, log_path


def read_stub_logs(log_path: Path) -> list[dict[str, Any]]:
    """读取桩脚本记录的调用日志列表。"""
    if not log_path.is_file():
        return []
    return [
        json.loads(line)
        for line in log_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def init_with_adapters(
    base_dir: Path,
    cfg_root: Path,
    instance: str,
    adapter_lines: list[str],
) -> tuple[Path, Path]:
    """初始化测试实例并在 config.ini 的 [adapters] 节插入配置行。"""
    vault = base_dir / f"vault-{instance}"
    init_instance(instance, vault, no_git=True)
    cfg_file = cfg_root / instance / "config.ini"
    lines = cfg_file.read_text(encoding="utf-8").splitlines()
    idx = lines.index("[adapters]")
    lines[idx + 1 : idx + 1] = adapter_lines
    cfg_file.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return vault, cfg_file


def run_hushai_cli(src_dir: Path, *args: str) -> subprocess.CompletedProcess[str]:
    """以子进程运行 python -m hushai_agent。"""
    env = os.environ.copy()
    py_path = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = f"{src_dir}{os.pathsep}{py_path}" if py_path else str(src_dir)
    return subprocess.run(
        [sys.executable, "-m", "hushai_agent", *args],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
