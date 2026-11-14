"""M3c 单元测试（待办页面 hushai serve）。"""

from __future__ import annotations

import json
import stat
import sys
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent
for p in (str(TESTS_DIR.parent / "src"), str(TESTS_DIR)):
    if p not in sys.path:
        sys.path.insert(0, p)

from hushai_agent import items, ledger, serve  # noqa: E402
from test_m3c import Base, item_line  # noqa: E402


class TestServe(Base):
    def start(self):
        self.server = serve.make_server(self.cfg(), 0)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.cookie = ""

    def req(self, method: str, path: str, body: bytes | None = None, headers: dict | None = None, cookie: bool = True):
        hdrs = dict(headers or {})
        if cookie and self.cookie:
            hdrs["Cookie"] = self.cookie

        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *a, **k):
                return None

        opener = urllib.request.build_opener(NoRedirect)
        request = urllib.request.Request(self.url + path, data=body, method=method, headers=hdrs)
        try:
            with opener.open(request, timeout=10) as resp:
                return resp.status, resp.headers, resp.read()
        except urllib.error.HTTPError as exc:
            return exc.code, exc.headers, exc.read()

    def act(self, **body):
        code, _, raw = self.req("POST", "/api/act", json.dumps(body).encode("utf-8"), {"X-Hushai": "1", "Content-Type": "application/json"})
        return code, (json.loads(raw) if raw else {})

    def test_serve(self) -> None:
        self.write_items(
            item_line("260301-1", "交水费"), item_line("260301-2", "寄合同"), item_line("260301-3", "退订杂志"),
            item_line("260301-4", "选印厂", "user_decision", "｜rec=选甲家"),
            item_line("260301-5", "选封面", "user_decision", "｜rec=用第二版"),
            item_line("260301-6", "定书名", "user_decision", "｜rec=用原名"),
        )
        self.write_today(("260301-1", "交水费"), ("260301-2", "寄合同"), ("260301-3", "退订杂志"), ("260301-4", "选印厂"))
        for text in ("候选甲", "候选乙"):
            self.say(text, domain="候选记忆")
        self.start()
        token = serve.token_path(self.cfg()).read_text(encoding="utf-8").strip()
        self.assertEqual(stat.S_IMODE(serve.token_path(self.cfg()).stat().st_mode), 0o600)

        for path in ("/", "/api/brief", "/etc/passwd", "/?t=wrong"):
            code, _, raw = self.req("GET", path)
            self.assertEqual((code, raw), (403, b""), msg=path)
        self.assertEqual(self.req("POST", "/api/act", b"{}", {"X-Hushai": "1"})[0], 403)
        code, headers, _ = self.req("GET", f"/?t={token}")
        self.assertEqual((code, headers["Location"]), (302, "/"))
        self.assertIn("HttpOnly", headers["Set-Cookie"])
        self.assertIn("SameSite=Strict", headers["Set-Cookie"])
        self.assertNotIn(token, headers["Set-Cookie"])
        self.cookie = headers["Set-Cookie"].split(";")[0]

        code, headers, raw = self.req("GET", "/")
        page = raw.decode("utf-8")
        self.assertEqual(code, 200)
        self.assertIn('data-a="done" data-id="260301-1"', page)
        self.assertIn("default-src 'none'", headers["Content-Security-Policy"])
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertEqual(json.loads(self.req("GET", "/api/brief")[2])["for_date"], "2026-03-03")
        self.assertEqual(self.req("GET", "/etc/passwd")[0], 404)
        self.assertEqual(self.req("PUT", "/", b"x")[0], 405)
        self.assertEqual(self.req("POST", "/api/act", b'{"action":"done","id":"260301-1"}')[0], 403)  # 缺 X-Hushai

        code, data = self.act(action="done", id="260301-1")
        self.assertEqual(code, 200)
        self.assertEqual(data["todos"][0]["state"], "done")
        self.assertIn("_html", data)
        web = (self.hub / "inbox" / "2026-03-02-web.md").read_text(encoding="utf-8")
        self.assertIn("完成 260301-1：交水费", web)
        self.assertIn("- [x] 260301-1", (self.hub / "open-items.md").read_text(encoding="utf-8"))

        self.assertEqual(self.act(action="defer", id="260301-2")[1]["todos"][1]["state"], "deferred")
        self.assertIn("推迟 260301-2 到 2026-03-09", (self.hub / "inbox" / "2026-03-02-web.md").read_text(encoding="utf-8"))
        self.assertEqual(self.act(action="drop", id="260301-3")[1]["todos"][2]["state"], "dropped")
        self.assertEqual(self.act(action="accept", id="260301-4")[1]["decisions"][0]["state"], "done")
        self.assertEqual(self.act(action="approve", id="C-260302-1", to="profile")[0], 200)
        self.assertIn("候选甲", (self.hub / "profile.md").read_text(encoding="utf-8"))
        code, data = self.act(action="discard", id="C-260302-2")
        self.assertEqual((code, data["candidates"]), (200, []))
        self.assertEqual(self.act(action="accept_all")[0], 200)
        status = {it.id: it.status for it in items.load(self.cfg())}
        self.assertEqual((status["260301-5"], status["260301-6"]), ("done", "done"))

        self.assertEqual(self.act(action="done", id="260301-1")[0], 409)   # 已处理
        self.assertEqual(self.act(action="done", id="260399-1")[0], 409)   # 不存在
        self.assertEqual(self.act(action="discard", id="C-260302-2")[0], 409)
        self.assertEqual(self.act(action="accept_all")[0], 409)
        for bad in ({"action": "rm"}, {"action": "done", "id": "../x"}, {"action": "approve", "id": "C-260302-1", "to": "etc"}, {}):
            self.assertEqual(self.act(**bad)[0], 400, msg=bad)
        self.assertEqual(self.req("POST", "/api/act", b"not json", {"X-Hushai": "1"})[0], 400)
        self.assertIn(self.req("POST", "/api/act", b"x" * 5000, {"X-Hushai": "1"})[0], (400, 413))

        log = (self.base / "state" / "demo" / "serve.log").read_text(encoding="utf-8")
        self.assertNotIn(token, log)
        self.assertNotIn(self.cookie.split("=")[1], log)
        self.assertIn("POST /api/act 200", log)
        acts = [r for r in ledger.read(self.cfg()) if r.get("job") == "serve-act"]
        self.assertGreaterEqual(len(acts), 10)

    def test_requires_web_door(self) -> None:
        self.edit_config("known = steward,laptop,web", "known = steward,laptop")
        with self.assertRaises(ValueError):
            serve.make_server(self.cfg(), 0)


if __name__ == "__main__":
    unittest.main()
