"""2026-11-17 任务书 v5 落实：额度不自动切换、告警只进 alerts 并主动报、capture 用模型判断、北极星统计。"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

TESTS_DIR = Path(__file__).resolve().parent
SRC_DIR = TESTS_DIR.parent / "src"
for p in (str(SRC_DIR), str(TESTS_DIR)):
    if p not in sys.path:
        sys.path.insert(0, p)

from hushai_agent import alerts, brief, capture, capture_model, ledger, northstar, steward  # noqa: E402
from hushai_agent.config import load_instance_config  # noqa: E402
from hushai_agent.config_edit import apply_config_updates  # noqa: E402
from hushai_agent.paths import get_instance_state_dir  # noqa: E402
from hushai_agent.vault import add_inbox_entry, init_instance  # noqa: E402
from m3a_stubs import run_hushai_cli  # noqa: E402


class TestV5Landing(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.base = Path(self._tmp.name)
        self._env = os.environ.copy()
        os.environ["HUSHAI_CONFIG_ROOT"] = str(self.base / "config")
        os.environ["HUSHAI_STATE_ROOT"] = str(self.base / "state")
        os.environ["HUSHAI_LOCK_WAIT"] = "0"
        os.environ["HUSHAI_MODEL_LOCK_WAIT"] = "0"
        os.environ["HUSHAI_NOW"] = "2026-11-17T10:00:00"
        self.vault = self.base / "vault"
        init_instance("demo", self.vault, timezone="Asia/Shanghai", no_git=True)
        apply_config_updates("demo", {"domains": {"names": "工作,生活,健康"}})
        self.cfg = load_instance_config("demo")
        self.state = get_instance_state_dir("demo")

    def tearDown(self) -> None:
        os.environ.clear()
        os.environ.update(self._env)
        self._tmp.cleanup()

    # ---- 告警 ----
    def test_resolved_heal_alert_drops_out_and_cooldown_clears(self) -> None:
        alerts.record_alert(self.cfg, kind="heal_needs_human", level="needs_human",
                            message="管家离线", source="steward_online:管家离线")
        (self.state / "heal-alerts.json").write_text(json.dumps({"steward_online:管家离线": "x"}), encoding="utf-8")
        self.assertEqual(len(alerts.active_alerts(self.cfg)), 1)
        self.assertIn("主动告诉使用者", alerts.render_active(self.cfg))
        self.assertEqual(alerts.resolve_heal_check(self.cfg, "steward_online"), 1)
        self.assertEqual(alerts.active_alerts(self.cfg), [])
        self.assertEqual(alerts.render_active(self.cfg), "")
        self.assertEqual(json.loads((self.state / "heal-alerts.json").read_text(encoding="utf-8")), {})
        self.assertEqual(alerts.resolve_heal_check(self.cfg, "steward_online"), 0)

    def test_brief_shows_only_active_severe_alerts(self) -> None:
        alerts.record_alert(self.cfg, kind="steward_switch", level="info", message="管家切换（手动切换）")
        alerts.record_alert(self.cfg, kind="ext", level="warning", message="中国要闻源异常", source="ext:china-news")
        msgs = [a["message"] for a in brief.build_brief(self.cfg)["alerts"]]
        self.assertEqual(msgs, ["中国要闻源异常"])
        alerts.resolve_alerts(self.cfg, "ext:china-news")
        self.assertEqual(brief.build_brief(self.cfg)["alerts"], [])

    def test_alert_cli_never_touches_inbox(self) -> None:
        r = run_hushai_cli(SRC_DIR, "alert", "add", "--instance", "demo", "--source", "ext:webfare",
                           "--message", "Webfare 今日未出新数据")
        self.assertEqual(r.returncode, 0, r.stderr)
        r = run_hushai_cli(SRC_DIR, "alert", "active", "--instance", "demo")
        self.assertIn("Webfare 今日未出新数据", r.stdout)
        self.assertEqual(list((self.vault / "hub" / "inbox").glob("*.md")), [])
        r = run_hushai_cli(SRC_DIR, "alert", "resolve", "--instance", "demo", "--source", "ext:webfare")
        self.assertEqual(json.loads(r.stdout)["resolved"], 1)

    def test_steward_session_start_reports_alerts(self) -> None:
        out = self.base / "kit"
        out.mkdir()
        steward.render_steward_kit(self.cfg, out)
        hooks = json.loads((self.vault / ".agents" / "hooks.json").read_text(encoding="utf-8"))
        cmds = [h["command"] for h in hooks["hushai-steward"]["SessionStart"]]
        self.assertTrue(any(c.endswith("alert active --instance demo") for c in cmds))

    # ---- capture ----
    def _transcript(self, human: str, reply: str) -> Path:
        t = self.base / "t.jsonl"
        rows = [{"type": "user", "message": {"role": "user", "content": human}},
                {"type": "assistant", "message": {"role": "assistant", "content": [{"type": "text", "text": reply}]}}]
        t.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n", encoding="utf-8")
        return t

    def _payload(self, t: Path) -> str:
        return json.dumps({"session_id": "s1", "transcript_path": str(t)})

    def test_extract_last_exchange_keeps_full_text(self) -> None:
        long_text = "合同" * 200
        human, reply = capture_model.extract_last_exchange(self._transcript(long_text, "好的"))
        self.assertEqual(human, long_text)
        self.assertEqual(reply, "好的")

    def test_model_skip_writes_nothing(self) -> None:
        t = self._transcript("这些简单的活，你全部自己改吧", "好的，开始改")
        with mock.patch.object(capture_model, "classify", return_value={"record": False, "domain": "生活", "did": "", "todo": "无"}):
            res = capture.run_capture(self.cfg, stdin_text=self._payload(t))
        self.assertEqual(res["reason"], "model-skip")
        self.assertEqual(list((self.vault / "hub" / "inbox").glob("*.md")), [])

    def test_model_record_uses_model_sentence(self) -> None:
        t = self._transcript("周三前把合同寄给出版社，记一下" + "细节" * 100, "已记下")
        dec = {"record": True, "domain": "工作", "did": "定周三前寄合同给出版社", "todo": "周三前寄出"}
        with mock.patch.object(capture_model, "classify", return_value=dec):
            res = capture.run_capture(self.cfg, stdin_text=self._payload(t))
        self.assertTrue(res["written"])
        text = Path(res["inbox_file"]).read_text(encoding="utf-8")
        self.assertIn("｜工作｜定周三前寄合同给出版社｜产物：无｜待办：周三前寄出", text)

    def test_model_unavailable_falls_back(self) -> None:
        t = self._transcript("周三前把合同寄给出版社", "已记下")
        with mock.patch.object(capture_model, "classify", return_value=None):
            res = capture.run_capture(self.cfg, stdin_text=self._payload(t))
        self.assertTrue(res["written"])
        self.assertNotIn("mode", res)

    def test_capture_validator(self) -> None:
        v = capture_model.make_validator(self.cfg)
        self.assertIsNotNone(v({"record": True, "domain": "火星", "did": "x", "todo": "无"})[1])
        clean, err = v({"record": True, "domain": "工作", "did": "a｜b\nc" + "长" * 100, "todo": ""})
        self.assertIsNone(err)
        self.assertNotIn("｜", clean["did"])
        self.assertLessEqual(len(clean["did"]), capture_model.DID_MAX)
        self.assertEqual(clean["todo"], "无")

    def test_capture_budget_separate_from_main(self) -> None:
        ledger.record(self.cfg, "call", job="capture", adapter="claude", tier="light", model="m", ok=True, duration=1, usage={}, error="")
        ledger.record(self.cfg, "call", job="digest", adapter="agy", tier="light", model="m", ok=True, duration=1, usage={}, error="")
        self.assertEqual(ledger.calls_today(self.cfg), 2)
        self.assertEqual(ledger.calls_today(self.cfg, exclude_job="capture"), 1)
        self.assertEqual(ledger.calls_today(self.cfg, only_job="capture"), 1)

    # ---- 北极星 ----
    def test_northstar_stats(self) -> None:
        (self.vault / "hub" / "today.md").write_text(
            "# 今日待办 2026-11-17\n- [ ] 1. 寄合同（261117-1｜到期 2026-11-18）\n- [ ] 2. 交报告（261117-2）\n", encoding="utf-8")
        add_inbox_entry("demo", "web", "工作", "完成 261117-1")
        add_inbox_entry("demo", "steward", "工作", "复审 hushai 整改")
        add_inbox_entry("demo", "steward", "生活", "周末带孩子去公园")
        ledger.record(self.cfg, "job", job="heal", date="2026-11-17", result="needs_human", note="")
        st = northstar.compute(self.cfg, days=7)
        self.assertEqual((st["today_shown"], st["today_acted"]), (2, 1))
        self.assertEqual(st["today_useful_rate"], 0.5)
        self.assertEqual((st["inbox_lines"], st["system_lines"]), (3, 1))
        self.assertEqual(st["needs_human_total"], 1)
        self.assertIn("## 北极星", northstar.render_section(st))


if __name__ == "__main__":
    unittest.main()
