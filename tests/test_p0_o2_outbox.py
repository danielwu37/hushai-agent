"""P0 / O2 测试：<state>/outbox/ 纯文件状态机、崩溃恢复、退避重试、去重、归档、at-least-once 与静默/24h 告警集成。"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

TESTS_DIR = Path(__file__).resolve().parent
REPO_ROOT = TESTS_DIR.parent
SRC_DIR = REPO_ROOT / "src"
for p in (str(SRC_DIR), str(TESTS_DIR)):
    if p not in sys.path:
        sys.path.insert(0, p)

from hushai_agent import (  # noqa: E402
    gate,
    heal,
    ledger,
    on_event,
    outbox,
    outbox_store,
    routing,
)
from hushai_agent.config import load_instance_config  # noqa: E402
from hushai_agent.config_ext import load_ext  # noqa: E402
from hushai_agent.vault import add_inbox_entry, init_instance  # noqa: E402


class TestP0O2Outbox(unittest.TestCase):
    """覆盖 <state>/outbox/ 状态流转、故障恢复、指数退避、去重、归档、CLI 与现有防打扰闭环。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.base = Path(self._tmp.name)
        self._orig_env = os.environ.copy()
        os.environ["HUSHAI_CONFIG_ROOT"] = str(self.base / "config")
        os.environ["HUSHAI_STATE_ROOT"] = str(self.base / "state")
        os.environ["HUSHAI_LOCK_WAIT"] = "0"
        os.environ["HUSHAI_NOW"] = "2026-03-02T10:00:00"
        self.vault = self.base / "vault"
        self.fixtures = self.base / "fixtures"
        self.fixtures.mkdir()
        init_instance("demo", self.vault, no_git=True)
        self.cfg_file = self.base / "config" / "demo" / "config.ini"
        self.state_dir = self.base / "state" / "demo"
        self.ob_dir = self.state_dir / "outbox"
        self._configure_fake_routing()

    def tearDown(self) -> None:
        os.environ.clear()
        os.environ.update(self._orig_env)
        self._tmp.cleanup()

    def cfg(self):
        return load_instance_config("demo")

    def _configure_fake_routing(self) -> None:
        txt = self.cfg_file.read_text(encoding="utf-8")
        txt = txt.replace(
            "[adapters]",
            f"[adapters]\nfk.type = fake\nfk.fixtures = {self.fixtures}\nfk.heavy = fake-heavy",
        ).replace(
            "[routing]\nenabled = false\ntrusted_doors =\nmax_daily = 10",
            "[routing]\nenabled = true\ntrusted_doors = laptop\nmax_daily = 10\nsteward = fk:heavy\nagy =",
        )
        self.cfg_file.write_text(txt, encoding="utf-8")

    def _run_cli(self, *args: str) -> subprocess.CompletedProcess[str]:
        env = os.environ.copy()
        py_path = env.get("PYTHONPATH", "")
        env["PYTHONPATH"] = f"{SRC_DIR}{os.pathsep}{py_path}" if py_path else str(SRC_DIR)
        return subprocess.run(
            [sys.executable, "-m", "hushai_agent", *args],
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )

    def test_01_lifecycle_exponential_backoff_and_manual_retry(self) -> None:
        """1. <state>/outbox/ 落盘布局、pending->sending->pending 指数退避、max_attempts 入 failed 与 retry 重排。"""
        cfg = self.cfg()
        enq = outbox.enqueue(
            cfg,
            channel="webhook_stub",
            target="phone",
            payload={"msg": "早间提醒"},
            dedupe_key="reminder-1",
            max_attempts=3,
            base_backoff_seconds=30,
            max_backoff_seconds=120,
        )
        self.assertTrue(enq["queued"])
        self.assertEqual(enq["delivery_semantics"], "at-least-once")
        msg_id = enq["message_id"]
        for sub in ("pending", "sending", "sent", "failed", "archive"):
            self.assertTrue((self.ob_dir / sub).is_dir(), msg=f"缺失 {sub}")
        self.assertTrue((self.ob_dir / "pending" / f"{msg_id}.json").is_file())

        attempts_seen: list[int] = []

        def fail_twice_then_ok(_cfg, env):
            attempts_seen.append(int(env["attempts"]))
            return False, "temporary 503"

        # attempt=1 @ 10:00:00 -> 失败，退避 30s（到 10:00:30）
        r1 = outbox.flush(cfg, handlers={"webhook_stub": fail_twice_then_ok})
        self.assertEqual((r1["retried"], r1["failed"], r1["sent"], r1["delivery_semantics"]), (1, 0, 0, "at-least-once"))
        self.assertTrue((self.ob_dir / "pending" / f"{msg_id}.json").is_file())

        # 10:00:15 尚未到退避窗 -> deferred_backoff=1，不调用 handler
        os.environ["HUSHAI_NOW"] = "2026-03-02T10:00:15"
        r_wait = outbox.flush(cfg, handlers={"webhook_stub": fail_twice_then_ok})
        self.assertEqual((r_wait["deferred_backoff"], len(attempts_seen)), (1, 1))

        # 10:00:31 attempt=2 -> 失败，退避 60s（到 10:01:31）
        os.environ["HUSHAI_NOW"] = "2026-03-02T10:00:31"
        r2 = outbox.flush(cfg, handlers={"webhook_stub": fail_twice_then_ok})
        self.assertEqual((r2["retried"], r2["failed"]), (1, 0))

        # 10:01:35 attempt=3 == max_attempts -> 落入 failed/
        os.environ["HUSHAI_NOW"] = "2026-03-02T10:01:35"
        r3 = outbox.flush(cfg, handlers={"webhook_stub": fail_twice_then_ok})
        self.assertEqual((r3["retried"], r3["failed"], len(attempts_seen)), (0, 1, 3))
        failed_env = json.loads((self.ob_dir / "failed" / f"{msg_id}.json").read_text(encoding="utf-8"))
        self.assertEqual((failed_env["status"], failed_env["last_error"]), ("failed", "temporary 503"))

        # retry_failed 人工重排 -> 下次 flush 送至 sent/
        os.environ["HUSHAI_NOW"] = "2026-03-02T10:05:00"
        re_res = outbox.retry_failed(cfg, message_id=msg_id, extra_attempts=2)
        self.assertEqual(re_res["requeued"], 1)
        r4 = outbox.flush(cfg, handlers={"webhook_stub": lambda _c, _e: (True, "delivered")})
        self.assertEqual(r4["sent"], 1)
        self.assertTrue((self.ob_dir / "sent" / f"{msg_id}.json").is_file())
        self.assertFalse((self.ob_dir / "failed" / f"{msg_id}.json").is_file())

    def test_02_dedupe_on_enqueue_and_missing_dedupe_index_fallback(self) -> None:
        """2. 幂等键去重：正常防重；即使 dedupe.json 丢失仍按活跃目录信封兜底去重。"""
        cfg = self.cfg()
        e1 = outbox.enqueue(cfg, "exchange_out", "laptop", {"rel_path": "hub/exchange/out/to-laptop/a.md", "content": "hi"}, dedupe_key="k-dup")
        e2 = outbox.enqueue(cfg, "exchange_out", "laptop", {"rel_path": "hub/exchange/out/to-laptop/a.md", "content": "hi"}, dedupe_key="k-dup")
        self.assertTrue(e1["queued"])
        self.assertFalse(e2["queued"])
        self.assertTrue(e2["deduplicated"])
        self.assertEqual(e1["message_id"], e2["message_id"])

        # 模拟 dedupe.json 丢失后再次入队同一幂等键，仍按磁盘信封判重
        (self.ob_dir / "dedupe.json").unlink()
        e3 = outbox.enqueue(cfg, "exchange_out", "laptop", {"rel_path": "hub/exchange/out/to-laptop/a.md", "content": "hi"}, dedupe_key="k-dup")
        self.assertFalse(e3["queued"])
        self.assertTrue(e3["deduplicated"])
        self.assertEqual(len(list((self.ob_dir / "pending").glob("*.json"))), 1)

    def test_03_crash_mid_sending_recovers_with_explicit_at_least_once_redelivery(self) -> None:
        """3. sending 阶段（外部投递后、移入 sent 前）崩溃恢复：明确 at-least-once 重投且无限次死循环。"""
        cfg = self.cfg()
        enq = outbox.enqueue(cfg, "ext_notify", "phone", {"text": "notify"}, dedupe_key="notify-1", max_attempts=3)
        msg_id = enq["message_id"]
        external_deliveries: list[str] = []

        def fake_send(_cfg, env):
            external_deliveries.append(env["message_id"])
            return True, "ok"

        def crash_before_sent(stage: str) -> None:
            if stage == "after_external_send_before_sent":
                raise RuntimeError("SIGKILL simulated after external send")

        with mock.patch.object(outbox, "_stage_hook", side_effect=crash_before_sent):
            with self.assertRaises(RuntimeError):
                outbox.flush(cfg, handlers={"ext_notify": fake_send})

        # 外部实际已被送出 1 次，但信封遗留在 sending/，sent/ 为空
        self.assertEqual(len(external_deliveries), 1)
        self.assertTrue((self.ob_dir / "sending" / f"{msg_id}.json").is_file())
        self.assertFalse((self.ob_dir / "sent" / f"{msg_id}.json").is_file())

        # stale_seconds=60 在 t+10s 不抢收；t+0s（默认 None，持有独占 outbox.lock）立即回收；或 t+65s 回收
        rec_skip = outbox.recover_stale_sending(cfg, stale_seconds=60, now="2026-03-02T10:00:10")
        self.assertEqual(rec_skip["recovered"], 0)

        # 下一次 flush 在同一独占锁下立即回收并重投 -> 外部共投递 2 次（at-least-once）
        fl = outbox.flush(cfg, handlers={"ext_notify": fake_send})
        self.assertEqual((fl["recovered"], fl["sent"], fl["delivery_semantics"]), (1, 1, "at-least-once"))
        self.assertEqual(len(external_deliveries), 2)
        self.assertTrue((self.ob_dir / "sent" / f"{msg_id}.json").is_file())
        self.assertFalse((self.ob_dir / "sending" / f"{msg_id}.json").is_file())

        # max_attempts=1 时若在 sending 中途崩溃，恢复后应直接转入 failed/ 防止无限崩循环
        enq_one = outbox.enqueue(cfg, "ext_notify", "phone", {"text": "once"}, dedupe_key="notify-one", max_attempts=1)
        one_id = enq_one["message_id"]
        with mock.patch.object(outbox, "_stage_hook", side_effect=crash_before_sent):
            with self.assertRaises(RuntimeError):
                outbox.flush(cfg, handlers={"ext_notify": fake_send})
        rec_max = outbox.recover_stale_sending(cfg)
        self.assertEqual((rec_max["failed"], rec_max["recovered"]), (1, 0))
        self.assertTrue((self.ob_dir / "failed" / f"{one_id}.json").is_file())

        # 跨目录双份残留清理（模拟 transition 写入目标后尚未删除源文件时崩溃）
        env_sent = json.loads((self.ob_dir / "sent" / f"{msg_id}.json").read_text(encoding="utf-8"))
        outbox_store.write_envelope(cfg, "pending", {**env_sent, "status": "pending", "attempts": 1})
        self.assertEqual(outbox_store.reconcile_duplicate_files(cfg), 1)
        self.assertFalse((self.ob_dir / "pending" / f"{msg_id}.json").is_file())
        self.assertTrue((self.ob_dir / "sent" / f"{msg_id}.json").is_file())

    def test_04_corrupt_envelopes_quarantined_and_archiving_preserves_dedupe(self) -> None:
        """4. 损坏信封隔离不阻塞队列；archive_old 归档历史终态并清理过期冷却键但保留永久幂等键。"""
        cfg = self.cfg()
        outbox_store.ensure_layout(cfg)
        (self.ob_dir / "pending" / "bad1.json").write_text("{broken json", encoding="utf-8")
        (self.ob_dir / "sending" / "bad2.json").write_text('{"only": "partial"}', encoding="utf-8")

        outbox.enqueue(
            cfg,
            channel="exchange_out",
            target="laptop",
            payload={"rel_path": "hub/exchange/out/to-laptop/msg.md", "content": "正文"},
            dedupe_key="perm-msg",
        )
        outbox.enqueue(
            cfg,
            channel="today_alert",
            target="steward",
            payload={"alert_line": "- 系统：临时告警"},
            dedupe_key="temp-cooldown",
            cooldown_seconds=3600,
        )
        res = outbox.flush(cfg)
        self.assertEqual((res["corrupted"], res["sent"]), (2, 2))
        self.assertFalse((self.ob_dir / "pending" / "bad1.json").is_file())
        self.assertFalse((self.ob_dir / "sending" / "bad2.json").is_file())
        corrupt_files = list((self.ob_dir / "failed").glob("corrupt-*.json"))
        self.assertEqual(len(corrupt_files), 2)
        self.assertEqual((self.vault / "hub" / "exchange" / "out" / "to-laptop" / "msg.md").read_text(encoding="utf-8"), "正文")

        # 35 天后归档 retain_days=30；最近（t+32 天）刚发的不应归档
        os.environ["HUSHAI_NOW"] = "2026-04-04T10:00:00"
        recent = outbox.enqueue(cfg, "exchange_out", "laptop", {"rel_path": "hub/exchange/out/to-laptop/r.md", "content": "r"}, dedupe_key="recent-k", immediate_sent=True)
        os.environ["HUSHAI_NOW"] = "2026-04-06T10:00:00"
        arch = outbox.archive_old(cfg, retain_days=30)
        self.assertEqual((arch["archived"], arch["pruned_dedupe"]), (2, 1))
        self.assertTrue((self.ob_dir / "sent" / f"{recent['message_id']}.json").is_file())
        self.assertEqual(len(list((self.ob_dir / "archive" / "2026-03").glob("*.json"))), 2)

        # 永久幂等键在归档后仍防重；过期 cooldown 键已被清理可重新入队
        dup_perm = outbox.enqueue(cfg, "exchange_out", "laptop", {"rel_path": "x", "content": "x"}, dedupe_key="perm-msg")
        re_cool = outbox.enqueue(cfg, "today_alert", "steward", {"alert_line": "- 系统：新告警"}, dedupe_key="temp-cooldown", cooldown_seconds=3600)
        self.assertTrue(dup_perm["deduplicated"])
        self.assertTrue(re_cool["queued"])

    def test_05_integration_routing_gate_ack_heal_24h_dedupe_silence_and_cli(self) -> None:
        """5. 联动 routing/gate --ack、heal 24h 去重与静默策略、空轮静默以及 hushai outbox CLI。"""
        cfg = self.cfg()
        # (A) routing + gate check-inbox --ack
        (self.fixtures / "route-steward-2026-03-02.json").write_text(json.dumps({"__raw__": "交付内容"}), encoding="utf-8")
        add_inbox_entry("demo", "laptop", "工作", "请求审阅", todo="@steward 请回复")
        out = on_event.handle(self.cfg())
        self.assertEqual(out["routing"]["routed"], 1)
        listed = outbox.list_envelopes(self.cfg(), status="sent")
        self.assertEqual(listed["counts"]["sent"], 1)
        self.assertEqual(listed["envelopes"][0]["channel"], "exchange_out")
        self.assertNotIn("acked_at", listed["envelopes"][0])

        gate.check_inbox(self.cfg(), "laptop", ack=True)
        listed_after_ack = outbox.list_envelopes(self.cfg(), status="sent")
        self.assertIn("acked_at", listed_after_ack["envelopes"][0])

        # (B) 空 outbox 下运行 on_event / heal 保持绝对静默（不增 outbox 账本行，不重复投递）
        before_ob_ledger = [r for r in ledger.read(self.cfg()) if r.get("job") == "outbox"]
        self.assertEqual(before_ob_ledger, [])
        on_event.handle(self.cfg())
        self.assertEqual([r for r in ledger.read(self.cfg()) if r.get("job") == "outbox"], [])

        # (C) heal 经 outbox 发送 today_alert：保留 24 小时去重与静默策略
        def always_down(argv: list[str], timeout: int = 15) -> tuple[int, str]:
            if argv[:2] == ["systemctl", "is-active"]:
                return (3, "inactive")
            return (0, "")

        hooks = {"installed_units": {"hushai-demo-watch.path"}, "cmd_runner": always_down}
        h1 = heal.run_heal(self.cfg(), hooks=hooks)
        self.assertEqual(len(h1["alerts_written"]), 1)
        self.assertEqual((self.vault / "hub" / "today.md").read_text(encoding="utf-8").count("- 系统："), 1)

        # 3 小时后同一问题：24h 去重静默，不新增 outbox 信封也不重复写 today.md
        os.environ["HUSHAI_NOW"] = "2026-03-02T13:00:00"
        sent_before = outbox.list_envelopes(self.cfg())["counts"]["sent"]
        h2 = heal.run_heal(self.cfg(), hooks=hooks)
        self.assertEqual(h2["alerts_written"], [])
        self.assertEqual(outbox.list_envelopes(self.cfg())["counts"]["sent"], sent_before)

        # 25 小时后冷却期过：重新触发新告警
        os.environ["HUSHAI_NOW"] = "2026-03-03T11:05:00"
        h3 = heal.run_heal(self.cfg(), hooks=hooks)
        self.assertEqual(len(h3["alerts_written"]), 1)

        # (D) hushai outbox CLI 全流程 + on_event / heal 自动接管 pending/sending
        outbox.enqueue(self.cfg(), "exchange_out", "laptop", {"rel_path": "hub/exchange/out/to-laptop/on_ev.md", "content": "ev"}, dedupe_key="ev-1")
        ev_res = on_event.handle(self.cfg())
        self.assertEqual(ev_res.get("outbox", {}).get("sent"), 1)
        self.assertEqual((self.vault / "hub" / "exchange" / "out" / "to-laptop" / "on_ev.md").read_text(encoding="utf-8"), "ev")

        # 模拟 sending/ 遗留孤儿由 heal.run_heal 自动回收重投
        enq_h = outbox.enqueue(self.cfg(), "exchange_out", "laptop", {"rel_path": "hub/exchange/out/to-laptop/heal.md", "content": "h"}, dedupe_key="heal-rec-1")
        h_id = enq_h["message_id"]
        h_env = json.loads((self.ob_dir / "pending" / f"{h_id}.json").read_text(encoding="utf-8"))
        h_env.update(status="sending", attempts=1, sending_started_at="2026-03-03T11:00:00", updated_at="2026-03-03T11:00:00")
        outbox_store.transition_envelope(self.cfg(), "pending", "sending", h_env)
        h_rec = heal.run_heal(self.cfg(), hooks={"installed_units": set()})
        self.assertEqual((h_rec.get("outbox_recovered"), h_rec.get("outbox_sent")), (1, 1))
        self.assertTrue((self.ob_dir / "sent" / f"{h_id}.json").is_file())

        outbox.enqueue(self.cfg(), "exchange_out", "laptop", {"rel_path": "hub/exchange/out/to-laptop/cli.md", "content": "cli"}, dedupe_key="cli-1")
        p_st = self._run_cli("outbox", "status", "--instance", "demo", "--json")
        self.assertEqual(p_st.returncode, 0, msg=p_st.stderr)
        self.assertEqual(json.loads(p_st.stdout)["counts"]["pending"], 1)
        p_fl = self._run_cli("outbox", "flush", "--instance", "demo", "--json")
        self.assertEqual(p_fl.returncode, 0, msg=p_fl.stderr)
        fl_json = json.loads(p_fl.stdout)
        self.assertEqual((fl_json["sent"], fl_json["delivery_semantics"]), (1, "at-least-once"))
        p_ls = self._run_cli("outbox", "list", "--instance", "demo", "--status", "sent")
        self.assertEqual(p_ls.returncode, 0, msg=p_ls.stderr)
        self.assertIn("at-least-once", p_ls.stdout)
        p_rc = self._run_cli("outbox", "recover", "--instance", "demo", "--json")
        self.assertEqual(p_rc.returncode, 0, msg=p_rc.stderr)
        p_rt = self._run_cli("outbox", "retry", "--instance", "demo", "--json")
        self.assertEqual(p_rt.returncode, 0, msg=p_rt.stderr)
        p_ar = self._run_cli("outbox", "archive", "--instance", "demo", "--days", "30", "--json")
        self.assertEqual(p_ar.returncode, 0, msg=p_ar.stderr)


if __name__ == "__main__":
    unittest.main()
