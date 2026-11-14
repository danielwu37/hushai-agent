"""待办页面（hushai serve）：只监听 127.0.0.1 的晨间简报页面，点选即写标准收件行。

对外怎么暴露由安装者用自己的反向代理决定，框架不提供改监听地址的选项。
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
import threading
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

from hushai_agent import brief, candidates, clock, items, ledger
from hushai_agent.brief_html import render_body, render_html
from hushai_agent.config import InstanceConfig
from hushai_agent.fsio import append_line, atomic_write
from hushai_agent.paths import get_instance_config_dir, get_instance_state_dir
from hushai_agent.todo import run_todo_action

DOOR = "web"
HOST = "127.0.0.1"
COOKIE = "hushai_s"
MAX_BODY = 4096
TODO_ACTIONS = ("done", "defer", "drop", "accept")
CAND_ACTIONS = ("approve", "discard")
ITEM_ID_RE = re.compile(r"^\d{6}-\d+$")
CAND_ID_RE = re.compile(r"^C-\d{6}-\d+$")
SECURITY_HEADERS = {
    "Cache-Control": "no-store",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; connect-src 'self'",
}


class BadRequest(ValueError):
    """请求参数不合法（400）。其余 ValueError 视为目标不存在或已处理（409）。"""


def load_token(cfg: InstanceConfig) -> str:
    """读取令牌；首次启动时生成，文件权限 600。"""
    cfg_dir = get_instance_config_dir(cfg.name)
    path = cfg_dir / "serve.token"
    if path.is_file() and path.read_text(encoding="utf-8").strip():
        os.chmod(path, 0o600)
        return path.read_text(encoding="utf-8").strip()
    token = secrets.token_urlsafe(32)
    atomic_write(path, token + "\n", [cfg_dir])
    os.chmod(path, 0o600)
    return token


def session_value(token: str) -> str:
    """cookie 里放的是令牌的派生值，不是令牌本身。"""
    return hmac.new(token.encode("utf-8"), b"hushai-serve-session", hashlib.sha256).hexdigest()


def brief_payload(cfg: InstanceConfig) -> dict[str, Any]:
    data = brief.build_brief(cfg)
    data["_html"] = render_body(data, live=True)
    return data


def act(cfg: InstanceConfig, body: Any) -> None:
    """执行一个点选动作：等价于以 web 端名义执行对应的 todo / candidate 命令。"""
    if not isinstance(body, dict):
        raise BadRequest("请求体必须是对象")
    action, target, to = body.get("action"), body.get("id"), body.get("to")
    if action == "accept_all":
        todo = [it for it in items.load(cfg) if it.status == "open" and it.owner == "user_decision" and it.rec]
        if not todo:
            raise ValueError("没有带推荐的待定事项")
        for it in todo:
            run_todo_action(cfg.name, "accept", it.id, door=DOOR)
    elif action in TODO_ACTIONS:
        if not isinstance(target, str) or not ITEM_ID_RE.match(target):
            raise BadRequest("编号不合法")
        run_todo_action(cfg.name, action, target, door=DOOR)
    elif action in CAND_ACTIONS:
        if not isinstance(target, str) or not CAND_ID_RE.match(target):
            raise BadRequest("编号不合法")
        if action == "approve" and to not in ("profile", "rules"):
            raise BadRequest("去向只能是 profile 或 rules")
        candidates.command(cfg, action, target, to or "", DOOR)
    else:
        raise BadRequest("不支持的动作")


class Handler(BaseHTTPRequestHandler):
    """请求处理。没有有效令牌或 cookie 的任何请求一律 403，响应体为空。"""

    server_version = "hushai"
    sys_version = ""

    def _send(self, code: int, body: bytes = b"", ctype: str = "application/json; charset=utf-8", extra: dict[str, str] | None = None) -> None:
        self.send_response(code)
        for key, val in {**SECURITY_HEADERS, **(extra or {})}.items():
            self.send_header(key, val)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, code: int, obj: Any) -> None:
        self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"))

    def _authed(self) -> bool:
        jar = SimpleCookie()
        try:
            jar.load(self.headers.get("Cookie", ""))
        except Exception:
            return False
        got = jar[COOKIE].value if COOKIE in jar else ""
        return hmac.compare_digest(got.encode("utf-8"), self.server.session.encode("utf-8"))

    def _route(self) -> None:
        url = urlsplit(self.path)
        if self.command == "GET" and url.path == "/" and "t" in parse_qs(url.query):
            given = parse_qs(url.query)["t"][0]
            if not hmac.compare_digest(given.encode("utf-8"), self.server.token.encode("utf-8")):
                return self._send(403)
            secure = "; Secure" if self.headers.get("X-Forwarded-Proto", "") == "https" else ""
            cookie = f"{COOKIE}={self.server.session}; Path=/; HttpOnly; SameSite=Strict; Max-Age=31536000{secure}"
            return self._send(302, extra={"Location": "/", "Set-Cookie": cookie})
        if not self._authed():
            return self._send(403)
        cfg = self.server.cfg
        if url.path not in ("/", "/api/brief", "/api/act"):
            return self._send(404)
        if self.command == "GET" and url.path == "/":
            page = render_html(brief.build_brief(cfg), live=True).encode("utf-8")
            return self._send(200, page, "text/html; charset=utf-8")
        if self.command == "GET" and url.path == "/api/brief":
            return self._json(200, brief_payload(cfg))
        if self.command == "POST" and url.path == "/api/act":
            return self._act(cfg)
        return self._send(405)

    def _act(self, cfg: InstanceConfig) -> None:
        if self.headers.get("X-Hushai") != "1":
            return self._send(403)
        try:
            length = int(self.headers.get("Content-Length", ""))
        except ValueError:
            return self._json(400, {"error": "缺少长度"})
        if length < 0 or length > MAX_BODY:
            self.close_connection = True
            return self._json(413, {"error": "请求体过大"})
        code, note = 200, ""
        try:
            body = json.loads(self.rfile.read(length).decode("utf-8"))
            with self.server.act_lock:
                act(cfg, body)
        except (BadRequest, UnicodeDecodeError, json.JSONDecodeError) as exc:
            code, note = 400, str(exc)
        except Exception as exc:  # 目标不存在、已处理、无权等
            code, note = 409, str(exc)
        action = body.get("action") if code != 400 and isinstance(body, dict) else "?"
        ledger.record(
            cfg, "job", job="serve-act", date=clock.today(cfg).isoformat(),
            result="ok" if code == 200 else "failed", note=f"action={action}; code={code}",
        )
        payload = brief_payload(cfg)
        if code != 200:
            payload["error"] = "参数不合法" if code == 400 else (note or "没有成功")
        self._json(code, payload)

    def _safe(self) -> None:
        try:
            self._route()
        except Exception:
            try:
                self._send(500)
            except Exception:
                pass

    do_GET = do_POST = do_PUT = do_DELETE = do_PATCH = do_HEAD = do_OPTIONS = _safe

    def log_message(self, fmt: str, *args: Any) -> None:
        """访问日志写 <状态目录>/serve.log；只记方法、路径（不带查询串）和状态码，不记令牌和 cookie。"""
        try:
            state_dir = get_instance_state_dir(self.server.cfg.name)
            path = urlsplit(getattr(self, "path", "") or "").path[:200]
            status = args[1] if len(args) > 1 else ""
            line = f"{clock.now(self.server.cfg).isoformat(timespec='seconds')} {self.command} {path} {status}"
            append_line(state_dir / "serve.log", line.replace("\n", " ").replace("\r", " "), [state_dir])
        except Exception:
            pass


def make_server(cfg: InstanceConfig, port: int) -> ThreadingHTTPServer:
    """建好但不启动的服务对象；web 不在已知端里时报错。"""
    if DOOR not in cfg.known_doors:
        raise ValueError("待办页面以 web 端的名义写收件：请先把 web 加进配置 [doors] known")
    server = ThreadingHTTPServer((HOST, int(port)), Handler)
    server.daemon_threads = True
    server.cfg = cfg
    server.token = load_token(cfg)
    server.session = session_value(server.token)
    server.act_lock = threading.Lock()
    return server


def token_path(cfg: InstanceConfig) -> Path:
    return get_instance_config_dir(cfg.name) / "serve.token"
