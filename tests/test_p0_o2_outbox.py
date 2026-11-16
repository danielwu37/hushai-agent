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
    alerts,
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
        self.assertEqual((self.vault / "hub" / "today.md").read_text(encoding="utf-8").count("- 系统："), 0)
        self.assertEqual(len(alerts.load_recent_alerts(self.cfg(), limit=50)), 1)

        # 3 小时后同一问题：24h 去重静默，不新增 outbox 信封也不重复记告警
        os.environ["HUSHAI_NOW"] = "2026-03-02T13:00:00"
        sent_before = outbox.list_envelopes(self.cfg())["counts"]["sent"]
        h2 = heal.run_heal(self.cfg(), hooks=hooks)
        self.assertEqual(h2["alerts_written"], [])
        self.assertEqual(outbox.list_envelopes(self.cfg())["counts"]["sent"], sent_before)
        self.assertEqual(len(alerts.load_recent_alerts(self.cfg(), limit=50)), 1)

        # 25 小时后冷却期过：重新触发新告警（alerts.jsonl 记 2 条，load_recent_alerts 去重展示最新 1 条）
        os.environ["HUSHAI_NOW"] = "2026-03-03T11:05:00"
        h3 = heal.run_heal(self.cfg(), hooks=hooks)
        self.assertEqual(len(h3["alerts_written"]), 1)
        self.assertEqual(len(alerts.alerts_path(self.cfg()).read_text(encoding="utf-8").splitlines()), 2)
        self.assertEqual(len(alerts.load_recent_alerts(self.cfg(), limit=50)), 1)

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


class TestP0O2OutboxCodexFixes(unittest.TestCase):
    """P0/O2 针对 Codex NEEDS_FIX 审查阻塞项与 N4/N5/N7/N8 的专项回归与故障注入测试。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.cfg_root = self.root / "cfg"
        self.state_root = self.root / "state"
        self.vault_dir = self.root / "vault"
        self._env_patch = mock.patch.dict(
            os.environ,
            {
                "HUSHAI_CONFIG_ROOT": str(self.cfg_root),
                "HUSHAI_STATE_ROOT": str(self.state_root),
            },
        )
        self._env_patch.start()
        from hushai_agent.vault import init_instance

        init_instance("demo", self.vault_dir, timezone="Asia/Shanghai", no_git=True)

    def tearDown(self) -> None:
        self._env_patch.stop()
        self._tmp.cleanup()

    def cfg(self, name: str = "demo"):
        from hushai_agent.config import load_instance_config

        return load_instance_config(name)

    def _spawn_helper_and_sigkill(self, code: str, flag_path: Path, env_extra: dict[str, str] | None = None) -> None:
        import signal
        import subprocess
        import sys
        import time

        env = os.environ.copy()
        env["PYTHONPATH"] = f"/root/hushai-agent/src:{env.get('PYTHONPATH', '')}".rstrip(":")
        if env_extra:
            env.update(env_extra)
        proc = subprocess.Popen(
            [sys.executable, "-c", code],
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            deadline = time.monotonic() + 10.0
            while time.monotonic() < deadline:
                if flag_path.is_file():
                    break
                if proc.poll() is not None:
                    break
                time.sleep(0.02)
            if not flag_path.is_file():
                out, err = proc.communicate(timeout=2)
                self.fail(f"子进程未到达挂起点: rc={proc.returncode} stdout={out!r} stderr={err!r}")
            os.kill(proc.pid, signal.SIGKILL)
            rc = proc.wait(timeout=5)
            self.assertEqual(rc, -signal.SIGKILL)
        finally:
            if proc.stdout is not None:
                proc.stdout.close()
            if proc.stderr is not None:
                proc.stderr.close()

    def test_06_versioned_transition_identical_timestamp_and_sigkill_all_transitions(self) -> None:
        """B1/N6/N7/N8: 同时间戳全状态迁移崩溃按 transition_seq 稳定胜出，且真实 SIGKILL 后可恢复。"""
        from hushai_agent import outbox, outbox_store

        cfg = self.cfg()
        fixed_now = "2026-03-02T10:00:00"

        # 1) failed -> pending 同时间戳、相同 attempts，写目标后删除源文件前崩溃：retry 不得被 _STATUS_TIE_ORDER 静默撤销
        e1 = outbox.enqueue(cfg, "webhook_stub", "ep", {"x": 1}, dedupe_key="b1-failed-pending", max_attempts=1, now=fixed_now)
        mid1 = e1["message_id"]
        fl1 = outbox.flush(cfg, handlers={"webhook_stub": lambda _c, _e: (False, "fail-once")}, now=fixed_now)
        self.assertEqual(fl1["failed"], 1)

        def fail_on(stage_target: str):
            def _hook(stage: str) -> None:
                if stage == stage_target:
                    raise RuntimeError(f"crash-at:{stage}")
            return _hook

        with mock.patch.object(outbox, "_stage_hook", side_effect=fail_on("after_transition_write:failed->pending")):
            with self.assertRaises(RuntimeError):
                outbox.retry_failed(cfg, message_id=mid1, extra_attempts=2, now=fixed_now)

        failed_file = outbox_store.state_subdir(cfg, "failed") / f"{mid1}.json"
        pending_file = outbox_store.state_subdir(cfg, "pending") / f"{mid1}.json"
        self.assertTrue(failed_file.is_file())
        self.assertTrue(pending_file.is_file())
        f_env = outbox_store.load_envelope(failed_file)
        p_env = outbox_store.load_envelope(pending_file)
        assert f_env and p_env
        self.assertEqual(f_env["updated_at"], p_env["updated_at"])
        self.assertEqual(f_env["attempts"], p_env["attempts"])
        self.assertGreater(p_env["transition_seq"], f_env["transition_seq"])

        cleaned = outbox_store.reconcile_duplicate_files(cfg)
        self.assertEqual(cleaned, 1)
        self.assertFalse(failed_file.exists())
        self.assertTrue(pending_file.is_file())
        fl_done = outbox.flush(cfg, handlers={"webhook_stub": lambda _c, _e: (True, "ok")}, now=fixed_now)
        self.assertEqual(fl_done["sent"], 1)

        # 2) pending->sending / sending->pending / sending->failed / sending->sent / sent->archive 同时间戳全覆盖
        transitions_ok: list[str] = []

        # 2a) pending -> sending 写后未删崩溃
        e2 = outbox.enqueue(cfg, "webhook_stub", "ep", {"x": 2}, dedupe_key="b1-p2s", max_attempts=3, now=fixed_now)
        mid2 = e2["message_id"]
        with mock.patch.object(outbox, "_stage_hook", side_effect=fail_on("after_transition_write:pending->sending")):
            with self.assertRaises(RuntimeError):
                outbox.flush(cfg, handlers={"webhook_stub": lambda _c, _e: (True, "ok")}, now=fixed_now)
        self.assertEqual(outbox_store.reconcile_duplicate_files(cfg), 1)
        self.assertTrue((outbox_store.state_subdir(cfg, "sending") / f"{mid2}.json").is_file())
        transitions_ok.append("pending->sending")

        # 2b) sending -> pending（由 recover_stale_sending 或 backoff 写入 pending 后未删 sending）同时间戳崩溃
        with mock.patch.object(outbox, "_stage_hook", side_effect=fail_on("after_transition_write:sending->pending")):
            with self.assertRaises(RuntimeError):
                outbox.recover_stale_sending(cfg, now=fixed_now)
        self.assertEqual(outbox_store.reconcile_duplicate_files(cfg), 1)
        self.assertTrue((outbox_store.state_subdir(cfg, "pending") / f"{mid2}.json").is_file())
        self.assertFalse((outbox_store.state_subdir(cfg, "sending") / f"{mid2}.json").exists())
        transitions_ok.append("sending->pending")

        # 2c) sending -> pending via flush failure backoff（同时间戳写 pending 后未删 sending）
        with mock.patch.object(outbox, "_stage_hook", side_effect=fail_on("after_transition_write:sending->pending")):
            with self.assertRaises(RuntimeError):
                outbox.flush(cfg, handlers={"webhook_stub": lambda _c, _e: (False, "retry-me")}, now=fixed_now)
        self.assertEqual(outbox_store.reconcile_duplicate_files(cfg), 1)
        self.assertTrue((outbox_store.state_subdir(cfg, "pending") / f"{mid2}.json").is_file())

        # 2d) sending -> failed（达到 max_attempts=1，同时间戳写 failed 后未删 sending）
        e2b = outbox.enqueue(cfg, "webhook_stub", "ep", {"x": 20}, dedupe_key="b1-s2f", max_attempts=1, now=fixed_now)
        mid2b = e2b["message_id"]
        with mock.patch.object(outbox, "_stage_hook", side_effect=fail_on("after_transition_write:sending->failed")):
            with self.assertRaises(RuntimeError):
                outbox.flush(cfg, handlers={"webhook_stub": lambda _c, _e: (False, "final-fail")}, now=fixed_now)
        self.assertEqual(outbox_store.reconcile_duplicate_files(cfg), 1)
        self.assertTrue((outbox_store.state_subdir(cfg, "failed") / f"{mid2b}.json").is_file())
        self.assertFalse((outbox_store.state_subdir(cfg, "sending") / f"{mid2b}.json").exists())
        transitions_ok.append("sending->failed")

        # 2e) sending -> sent（同时间戳写 sent 后未删 sending；先归档清理之前的 mid1 保证仅剩 mid3）
        outbox.archive_old(cfg, retain_days=0, now=fixed_now)
        e3 = outbox.enqueue(cfg, "webhook_stub", "ep", {"x": 3}, dedupe_key="b1-s2sent", max_attempts=2, now=fixed_now)
        mid3 = e3["message_id"]
        with mock.patch.object(outbox, "_stage_hook", side_effect=fail_on("after_transition_write:sending->sent")):
            with self.assertRaises(RuntimeError):
                outbox.flush(cfg, handlers={"webhook_stub": lambda _c, _e: (True, "ok")}, now=fixed_now)
        self.assertEqual(outbox_store.reconcile_duplicate_files(cfg), 1)
        self.assertTrue((outbox_store.state_subdir(cfg, "sent") / f"{mid3}.json").is_file())
        transitions_ok.append("sending->sent")

        # 2f) sent -> archive（同时间戳写 archive 后未删 sent）
        with mock.patch.object(outbox, "_stage_hook", side_effect=fail_on("after_transition_write:sent->archive")):
            with self.assertRaises(RuntimeError):
                outbox.archive_old(cfg, retain_days=0, now=fixed_now)
        self.assertEqual(outbox_store.reconcile_duplicate_files(cfg), 1)
        self.assertFalse((outbox_store.state_subdir(cfg, "sent") / f"{mid3}.json").exists())
        self.assertEqual(len(list((outbox_store.state_subdir(cfg, "archive")).rglob(f"{mid3}.json"))), 1)
        transitions_ok.append("sent->archive")
        self.assertEqual(len(transitions_ok), 5)

        # 3) 真实子进程 SIGKILL 故障注入：failed -> pending 写后删前 + flush sending 外部投递后转 sent 前
        ek1 = outbox.enqueue(cfg, "webhook_stub", "ep", {"k": 1}, dedupe_key="sigkill-r", max_attempts=1, now=fixed_now)
        mid_k1 = ek1["message_id"]
        outbox.flush(cfg, handlers={"webhook_stub": lambda _c, _e: (False, "fail")}, now=fixed_now)

        flag1 = self.root / "sigkill_retry.ready"
        child_retry = f"""
import time
from pathlib import Path
from unittest import mock
from hushai_agent import outbox

def _hook(stage):
    if stage == "after_transition_write:failed->pending":
        Path({str(flag1)!r}).write_text("ready", encoding="utf-8")
        time.sleep(30)

with mock.patch.object(outbox, "_stage_hook", side_effect=_hook):
    outbox.retry_failed("demo", message_id={mid_k1!r}, extra_attempts=2, now={fixed_now!r})
"""
        self._spawn_helper_and_sigkill(child_retry, flag1)
        # SIGKILL 后两份同时间戳文件共存，next flush 必须恢复 pending 并投递成功
        self.assertTrue((outbox_store.state_subdir(cfg, "failed") / f"{mid_k1}.json").is_file())
        self.assertTrue((outbox_store.state_subdir(cfg, "pending") / f"{mid_k1}.json").is_file())
        res_k1 = outbox.flush(cfg, handlers={"webhook_stub": lambda _c, _e: (True, "ok")}, now=fixed_now)
        self.assertEqual(res_k1["sent"], 1)
        self.assertFalse((outbox_store.state_subdir(cfg, "failed") / f"{mid_k1}.json").exists())
        self.assertTrue((outbox_store.state_subdir(cfg, "sent") / f"{mid_k1}.json").is_file())

        # 3b) 真实 SIGKILL on flush after_external_send_before_sent（至少一次外部发送后被杀，父进程恢复重投并收口）
        ek2 = outbox.enqueue(
            cfg,
            "exchange_out",
            "laptop",
            {"rel_path": "hub/exchange/out/to-laptop/sigkill.md", "content": "sigkill-test"},
            dedupe_key="sigkill-flush",
            max_attempts=3,
            now=fixed_now,
        )
        mid_k2 = ek2["message_id"]
        flag2 = self.root / "sigkill_flush.ready"
        child_flush = f"""
import time
from pathlib import Path
from unittest import mock
from hushai_agent import outbox

def _hook(stage):
    if stage == "after_external_send_before_sent":
        Path({str(flag2)!r}).write_text("ready", encoding="utf-8")
        time.sleep(30)

with mock.patch.object(outbox, "_stage_hook", side_effect=_hook):
    outbox.flush("demo", now={fixed_now!r})
"""
        self._spawn_helper_and_sigkill(child_flush, flag2)
        self.assertTrue((cfg.vault / "hub/exchange/out/to-laptop/sigkill.md").is_file())
        self.assertTrue((outbox_store.state_subdir(cfg, "sending") / f"{mid_k2}.json").is_file())
        res_k2 = outbox.flush(cfg, now=fixed_now)
        self.assertEqual(res_k2["recovered"], 1)
        self.assertEqual(res_k2["sent"], 1)
        sent_k2 = outbox_store.load_envelope(outbox_store.state_subdir(cfg, "sent") / f"{mid_k2}.json")
        assert sent_k2 is not None
        self.assertEqual(sent_k2["attempts"], 2)

    def test_07_archive_permanent_dedupe_and_corrupt_dedupe_rebuild_diagnostics(self) -> None:
        """B2/N4: 永久幂等键归档后删/毁 dedupe.json 仍防重；冷却键过期可重入；损坏索引诊断暴露且空箱静默。"""
        from hushai_agent import cli, outbox, outbox_store

        # 未初始化空实例：不误报 corrupt、无 rebuilt 噪声
        cfg_empty_vault = self.root / "vault_empty"
        from hushai_agent.vault import init_instance

        init_instance("emptybox", cfg_empty_vault, timezone="Asia/Shanghai", no_git=True)
        empty_list = outbox.list_envelopes("emptybox")
        self.assertEqual(empty_list["dedupe_health"], "uninitialized")
        self.assertFalse(empty_list["corrupted_dedupe_index"])
        empty_flush = outbox.flush("emptybox")
        self.assertEqual(empty_flush["dedupe_health"], "uninitialized")
        self.assertFalse(empty_flush["dedupe_rebuilt"])

        cfg = self.cfg()
        t0 = "2026-01-01T10:00:00"
        e_perm = outbox.enqueue(cfg, "exchange_out", "laptop", {"rel_path": "hub/exchange/out/to-laptop/p.md", "content": "p"}, dedupe_key="perm-arch", immediate_sent=True, now=t0)
        e_exp = outbox.enqueue(cfg, "today_alert", "steward", {"alert_line": "- 系统：旧冷却"}, dedupe_key="cool-exp", cooldown_seconds=3600, immediate_sent=True, now=t0)
        e_act = outbox.enqueue(cfg, "today_alert", "steward", {"alert_line": "- 系统：长冷却"}, dedupe_key="cool-act", cooldown_seconds=86400 * 90, immediate_sent=True, now=t0)
        self.assertTrue(e_perm["queued"] and e_exp["queued"] and e_act["queued"])

        arch = outbox.archive_old(cfg, retain_days=30, now="2026-02-15T10:00:00")
        self.assertEqual(arch["archived"], 3)
        self.assertGreaterEqual(arch["pruned_dedupe"], 1)

        dedupe_file = outbox_store.outbox_dir(cfg) / "dedupe.json"

        # 1) 删除 dedupe.json：归档区中的永久幂等键 & 未过期冷却键必须仍去重，过期冷却键可重建再入
        dedupe_file.unlink()
        diag_missing = outbox.list_envelopes(cfg)
        self.assertEqual(diag_missing["dedupe_health"], "missing_with_envelopes")
        self.assertFalse(diag_missing["corrupted_dedupe_index"])

        dup_perm = outbox.enqueue(cfg, "exchange_out", "laptop", {"rel_path": "x", "content": "x"}, dedupe_key="perm-arch", now="2026-02-15T10:01:00")
        self.assertFalse(dup_perm["queued"])
        self.assertTrue(dup_perm["deduplicated"])
        self.assertEqual(dup_perm["status"], "archive")
        self.assertEqual(dup_perm["dedupe_health"], "missing_with_envelopes")
        self.assertTrue(dup_perm["dedupe_rebuilt"])

        dedupe_file.unlink()
        dup_cool = outbox.enqueue(cfg, "today_alert", "steward", {"alert_line": "- 系统：重复"}, dedupe_key="cool-act", now="2026-02-15T10:02:00")
        self.assertFalse(dup_cool["queued"])
        self.assertTrue(dup_cool["deduplicated"])

        re_exp = outbox.enqueue(cfg, "today_alert", "steward", {"alert_line": "- 系统：过期重入"}, dedupe_key="cool-exp", cooldown_seconds=3600, now="2026-02-15T10:03:00")
        self.assertTrue(re_exp["queued"])

        # 2) 损坏 dedupe.json（"{corrupt" 与 非字典 "[]"）：诊断可见，且自动重建后永久键继续去重
        dedupe_file.write_text("{corrupt", encoding="utf-8")
        diag_bad1 = outbox.list_envelopes(cfg)
        self.assertEqual(diag_bad1["dedupe_health"], "corrupt")
        self.assertTrue(diag_bad1["corrupted_dedupe_index"])

        import contextlib
        import io
        import json as _json

        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = cli.main(["outbox", "status", "--instance", "demo", "--json"])
        self.assertEqual(rc, 0)
        cli_json = _json.loads(buf.getvalue())
        self.assertEqual(cli_json["dedupe_health"], "corrupt")
        self.assertTrue(cli_json["corrupted_dedupe_index"])

        healed_enq = outbox.enqueue(cfg, "exchange_out", "laptop", {"rel_path": "x", "content": "x"}, dedupe_key="perm-arch", now="2026-02-15T10:04:00")
        self.assertFalse(healed_enq["queued"])
        self.assertTrue(healed_enq["deduplicated"])
        self.assertEqual(healed_enq["dedupe_health"], "corrupt")
        self.assertTrue(healed_enq["dedupe_rebuilt"])
        self.assertEqual(outbox.list_envelopes(cfg)["dedupe_health"], "ok")

        dedupe_file.write_text("[]", encoding="utf-8")
        self.assertTrue(outbox.list_envelopes(cfg)["corrupted_dedupe_index"])
        healed_flush = outbox.flush(cfg, now="2026-02-15T10:05:00")
        self.assertEqual(healed_flush["dedupe_health"], "corrupt")
        self.assertTrue(healed_flush["dedupe_rebuilt"])
        self.assertFalse(outbox.list_envelopes(cfg)["corrupted_dedupe_index"])

    def test_08_clock_skew_tz_dst_cooldown_boundary_and_heal_crash_24h(self) -> None:
        """N5 + heal 崩后 24h 去重：冷却边界精确判定、DST 春进跨界、跨时区排序与时钟回拨、heal 崩后不重复写入。"""
        import datetime
        from zoneinfo import ZoneInfo
        from hushai_agent import clock, heal, outbox, outbox_store
        from hushai_agent.vault import init_instance

        cfg = self.cfg()

        # 1) 冷却精确边界：T0+3599 去重，T0+3600 允许重入
        c0 = "2026-03-02T10:00:00"
        c_first = outbox.enqueue(cfg, "today_alert", "steward", {"alert_line": "- 系统：边界1"}, dedupe_key="bound-k", cooldown_seconds=3600, immediate_sent=True, now=c0)
        self.assertTrue(c_first["queued"])
        c_before = outbox.enqueue(cfg, "today_alert", "steward", {"alert_line": "- 系统：边界2"}, dedupe_key="bound-k", cooldown_seconds=3600, now="2026-03-02T10:59:59")
        self.assertFalse(c_before["queued"])
        c_exact = outbox.enqueue(cfg, "today_alert", "steward", {"alert_line": "- 系统：边界3"}, dedupe_key="bound-k", cooldown_seconds=3600, now="2026-03-02T11:00:00")
        self.assertTrue(c_exact["queued"])

        # 2) DST 春进跨界（America/New_York 2026-03-08 02:00 -> 03:00）：3600 秒真实耗时应到 03:30-04:00 而非 02:30-05:00
        ny_vault = self.root / "vault_ny"
        init_instance("nybox", ny_vault, timezone="America/New_York", no_git=True)
        ny_cfg = self.cfg("nybox")
        dst_start = "2026-03-08T01:30:00-05:00"
        ny_enq = outbox.enqueue(
            ny_cfg,
            "webhook_stub",
            "ep",
            {"dst": True},
            dedupe_key="dst-k",
            cooldown_seconds=3600,
            base_backoff_seconds=3600,
            max_backoff_seconds=3600,
            max_attempts=3,
            now=dst_start,
        )
        self.assertTrue(ny_enq["queued"])
        # 触发一次失败 -> next_retry_at 必须精确落在真实 +3600s 的 03:30-04:00
        ny_fail = outbox.flush(ny_cfg, handlers={"webhook_stub": lambda _c, _e: (False, "retry")}, now=dst_start)
        self.assertEqual(ny_fail["retried"], 1)
        p_env = outbox_store.load_envelope(outbox_store.state_subdir(ny_cfg, "pending") / f"{ny_enq['message_id']}.json")
        assert p_env is not None
        next_utc = outbox_store.parse_iso(p_env["next_retry_at"], ZoneInfo("America/New_York")).astimezone(datetime.timezone.utc)
        start_utc = outbox_store.parse_iso(dst_start, ZoneInfo("America/New_York")).astimezone(datetime.timezone.utc)
        self.assertEqual(int((next_utc - start_utc).total_seconds()), 3600)

        # +30 分钟（03:00-04:00）：冷却与退避均未到期
        self.assertFalse(outbox.enqueue(ny_cfg, "webhook_stub", "ep", {}, dedupe_key="dst-k", cooldown_seconds=3600, now="2026-03-08T03:00:00-04:00")["queued"])
        self.assertEqual(outbox.flush(ny_cfg, handlers={"webhook_stub": lambda _c, _e: (True, "ok")}, now="2026-03-08T03:00:00-04:00")["deferred_backoff"], 1)
        # +60 分钟（03:30-04:00）：退避与冷却均精确到期
        self.assertEqual(outbox.flush(ny_cfg, handlers={"webhook_stub": lambda _c, _e: (True, "ok")}, now="2026-03-08T03:30:00-04:00")["sent"], 1)
        self.assertTrue(outbox.enqueue(ny_cfg, "webhook_stub", "ep", {}, dedupe_key="dst-k", cooldown_seconds=3600, now="2026-03-08T03:30:00-04:00")["queued"])

        # 3) 跨时区 UTC instant 排序 + 时钟回拨：03:00Z(+08 11:00) 必须先于 10:00Z(+00 10:00) 投递
        order_cfg_vault = self.root / "vault_tz"
        init_instance("tzbox", order_cfg_vault, timezone="Asia/Shanghai", no_git=True)
        tz_cfg = self.cfg("tzbox")
        delivered_order: list[str] = []
        e_later_utc = outbox.enqueue(tz_cfg, "stub", "ep", {"tag": "later-10Z"}, dedupe_key="tz-1", now="2026-03-02T10:00:00+00:00")
        e_early_sh = outbox.enqueue(tz_cfg, "stub", "ep", {"tag": "early-03Z"}, dedupe_key="tz-2", now="2026-03-02T11:00:00+08:00")
        outbox.flush(tz_cfg, handlers={"stub": lambda _c, env: (delivered_order.append(env["payload"]["tag"]) is None, "ok")}, now="2026-03-02T19:00:00+08:00")
        self.assertEqual(delivered_order, ["early-03Z", "later-10Z"])
        self.assertTrue(e_later_utc["queued"] and e_early_sh["queued"])

        # 时钟回拨下 retry_failed + 写后崩：transition_seq 依然击败回拨时间
        ef_skew = outbox.enqueue(tz_cfg, "stub", "ep", {}, dedupe_key="skew-k", max_attempts=1, now="2026-03-02T12:00:00+08:00")
        outbox.flush(tz_cfg, handlers={"stub": lambda _c, _e: (False, "f")}, now="2026-03-02T12:00:00+08:00")
        with mock.patch.object(
            outbox,
            "_stage_hook",
            side_effect=lambda st: (_ for _ in ()).throw(RuntimeError("skew-crash")) if st == "after_transition_write:failed->pending" else None,
        ):
            with self.assertRaises(RuntimeError):
                outbox.retry_failed(tz_cfg, message_id=ef_skew["message_id"], extra_attempts=2, now="2026-03-02T08:00:00+08:00")
        self.assertEqual(outbox_store.reconcile_duplicate_files(tz_cfg), 1)
        self.assertTrue((outbox_store.state_subdir(tz_cfg, "pending") / f"{ef_skew['message_id']}.json").is_file())

        # 4) heal._report_needs_human 写完 today.md、在 atomic_write(heal-alerts.json) 前崩溃 -> 归档+删/损 dedupe.json 后 24h 内不重发，超 24h 后重发
        heal_vault = self.root / "vault_heal"
        init_instance("healbox", heal_vault, timezone="Asia/Shanghai", no_git=True)
        h_cfg = self.cfg("healbox")
        item = {"id": "svc-x", "name": "网关", "problem": "连接超时", "suggestion": "检查端口"}
        t_alert0 = outbox_store.parse_iso("2026-03-05T10:00:00+08:00", ZoneInfo("Asia/Shanghai"))
        orig_atomic = heal.atomic_write

        def crash_on_alerts_json(path: Path, content: str, allowed_roots: list[Path]) -> None:
            if Path(path).name == "heal-alerts.json":
                raise RuntimeError("crash-before-heal-alerts-json")
            orig_atomic(path, content, allowed_roots)

        with mock.patch.object(clock, "now", return_value=t_alert0), mock.patch.object(heal, "atomic_write", side_effect=crash_on_alerts_json):
            with self.assertRaises(RuntimeError):
                heal._report_needs_human(h_cfg, item)

        from hushai_agent.paths import get_instance_state_dir

        self.assertFalse((get_instance_state_dir("healbox") / "heal-alerts.json").exists())
        today_path = h_cfg.vault / "hub" / "today.md"
        self.assertEqual((today_path.read_text(encoding="utf-8") if today_path.is_file() else "").count("- 系统："), 0)
        alert_file = alerts.alerts_path(h_cfg)
        self.assertEqual(
            sum(1 for ln in alert_file.read_text(encoding="utf-8").splitlines() if "网关（连接超时）坏了" in ln),
            1,
        )

        # 即使归档并损坏 dedupe.json，24h 内仍由 outbox 重建索引抑制重发
        outbox.archive_old(h_cfg, retain_days=0, now="2026-03-05T10:05:00+08:00")
        (outbox_store.outbox_dir(h_cfg) / "dedupe.json").write_text("{corrupt", encoding="utf-8")
        t_within_24h = outbox_store.parse_iso("2026-03-06T09:59:59+08:00", ZoneInfo("Asia/Shanghai"))
        with mock.patch.object(clock, "now", return_value=t_within_24h):
            res_dup = heal._report_needs_human(h_cfg, item)
        self.assertIsNone(res_dup)
        self.assertEqual(
            sum(1 for ln in alert_file.read_text(encoding="utf-8").splitlines() if "网关（连接超时）坏了" in ln),
            1,
        )

        # 超过 24h 后允许再次告警并写入 alerts.jsonl（仍不污染 today.md）
        t_after_24h = outbox_store.parse_iso("2026-03-06T10:00:01+08:00", ZoneInfo("Asia/Shanghai"))
        with mock.patch.object(clock, "now", return_value=t_after_24h):
            res_new = heal._report_needs_human(h_cfg, item)
            recent_view = alerts.load_recent_alerts(h_cfg, limit=50)
        self.assertIsNotNone(res_new)
        self.assertEqual(
            sum(1 for ln in alert_file.read_text(encoding="utf-8").splitlines() if "网关（连接超时）坏了" in ln),
            2,
        )
        self.assertEqual(len(recent_view), 1)


if __name__ == "__main__":
    unittest.main()
