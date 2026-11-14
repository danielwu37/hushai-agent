"""晨间简报页面：单文件、自包含，不引用任何外部资源；所有来自数据的文字都做 HTML 转义。"""

from __future__ import annotations

from html import escape
from typing import Any

STATE_LABEL = {"done": "已办", "dropped": "不做了", "deferred": "已推迟"}

CSS = """
:root{--bg:#f6f5f1;--card:#fff;--ink:#1d1d1f;--mute:#6e6e73;--line:#e3e1da;--go:#0b6b4f;--go-ink:#fff;--warn:#b3261e;--soft:#eeece5}
@media (prefers-color-scheme:dark){:root{--bg:#141413;--card:#1f1f1d;--ink:#f2f1ec;--mute:#a3a29b;--line:#33332f;--go:#5fd0a5;--go-ink:#0c1f18;--warn:#ff8a80;--soft:#2a2a27}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);font:17px/1.5 -apple-system,BlinkMacSystemFont,"PingFang SC","Segoe UI",sans-serif;-webkit-text-size-adjust:100%}
main{max-width:640px;margin:0 auto;padding:16px 16px 48px}
h1{font-size:22px;margin:8px 0 2px}
.sub{color:var(--mute);font-size:14px;margin:0 0 12px}
h2{font-size:14px;color:var(--mute);font-weight:600;letter-spacing:.08em;margin:22px 4px 8px}
.card{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:12px 14px;margin:0 0 10px}
.t{font-size:17px;font-weight:600;overflow-wrap:anywhere}
.m{color:var(--mute);font-size:13px;margin-top:2px;overflow-wrap:anywhere}
.rec{background:var(--soft);border-radius:8px;padding:6px 10px;margin-top:8px;font-size:15px}
.late{color:var(--warn);font-weight:600}
.row{display:flex;gap:8px;margin-top:10px;flex-wrap:wrap}
button{font:inherit;font-size:15px;min-height:44px;padding:8px 14px;border-radius:10px;border:1px solid var(--line);background:var(--card);color:var(--ink);cursor:pointer;flex:1 1 auto}
button.go{background:var(--go);border-color:var(--go);color:var(--go-ink);font-weight:600}
button:disabled{opacity:.45;cursor:default}
.all{margin:4px 0 6px}
.all button{width:100%}
.off{opacity:.5}
.off .t{text-decoration:line-through;font-weight:400}
.note{font-size:13px;color:var(--mute);border:1px dashed var(--line);border-radius:10px;padding:8px 10px;margin:0 0 12px}
#msg{position:fixed;left:16px;right:16px;bottom:16px;background:var(--ink);color:var(--bg);padding:10px 14px;border-radius:10px;font-size:14px;display:none}
"""

JS = """
(function(){
var app=document.getElementById('app'),msg=document.getElementById('msg'),busy=false;
function say(t){msg.textContent=t;msg.style.display='block';setTimeout(function(){msg.style.display='none'},3500)}
app.addEventListener('click',function(ev){
  var b=ev.target.closest('button[data-a]');if(!b||busy)return;
  var a=b.getAttribute('data-a');
  if(a==='hide'){b.closest('.card').style.display='none';return}
  busy=true;b.disabled=true;
  var body={action:a};
  if(b.hasAttribute('data-id'))body.id=b.getAttribute('data-id');
  if(b.hasAttribute('data-to'))body.to=b.getAttribute('data-to');
  fetch('/api/act',{method:'POST',credentials:'same-origin',headers:{'Content-Type':'application/json','X-Hushai':'1'},body:JSON.stringify(body)})
  .then(function(r){return r.json().then(function(j){return {ok:r.ok,j:j}})})
  .then(function(x){if(x.j&&x.j._html){app.innerHTML=x.j._html}if(!x.ok){say((x.j&&x.j.error)||'没有成功，请再试一次')}})
  .catch(function(){say('连接不上，请再试一次');b.disabled=false})
  .then(function(){busy=false});
});
})();
"""


def _btn(label: str, action: str, live: bool, item_id: str = "", to: str = "", primary: bool = False) -> str:
    attrs = f' data-a="{action}"'
    if item_id:
        attrs += f' data-id="{escape(item_id, quote=True)}"'
    if to:
        attrs += f' data-to="{to}"'
    cls = ' class="go"' if primary else ""
    return f"<button{cls}{attrs}{'' if live else ' disabled'}>{escape(label)}</button>"


def _card(title: str, meta: str, extra: str, buttons: list[str], state: str = "open") -> str:
    closed = state in STATE_LABEL
    meta_html = f'<div class="m">{meta}</div>' if meta else ""
    row = "" if closed or not buttons else f'<div class="row">{"".join(buttons)}</div>'
    tag = f'<div class="m">{STATE_LABEL[state]}</div>' if closed else ""
    cls = "card off" if closed else "card"
    return f'<div class="{cls}"><div class="t">{escape(title)}</div>{meta_html}{"" if closed else extra}{tag}{row}</div>'


def render_body(brief: dict[str, Any], live: bool) -> str:
    """四个区的 HTML 片段；空的区整个不出现。"""
    out: list[str] = []
    if not live:
        out.append('<p class="note">这是只读快照，点选请打开待办页面，或按文末写法回写。</p>')
    if brief.get("degraded"):
        out.append('<p class="note">今日模型不可用，仅机械对账。</p>')
    if any(d["rec"] and d["state"] == "open" for d in brief["decisions"]):
        out.append(f'<div class="all">{_btn("全部按推荐确认", "accept_all", live, primary=True)}</div>')

    if brief["todos"]:
        out.append("<h2>要我办</h2>")
        for t in brief["todos"]:
            bits = [escape(t["id"])]
            if t["overdue"]:
                bits.append('<span class="late">逾期</span>')
            elif t["due"]:
                bits.append(f"到期 {escape(t['due'])}")
            if t["defers"]:
                bits.append(f"已推迟 {int(t['defers'])} 次")
            btns = [_btn("已办", "done", live, t["id"], primary=True), _btn("先不动", "defer", live, t["id"]), _btn("不做", "drop", live, t["id"])]
            out.append(_card(t["title"], " · ".join(bits), "", btns, t["state"]))
    if brief["decisions"]:
        out.append("<h2>要我定</h2>")
        for d in brief["decisions"]:
            rec = f'<div class="rec">推荐：{escape(d["rec"])}</div>' if d["rec"] else ""
            btns = [_btn("按推荐", "accept", live, d["id"], primary=True)] if d["rec"] else []
            btns += [_btn("先不动", "defer", live, d["id"]), _btn("不做", "drop", live, d["id"])]
            out.append(_card(d["title"], escape(d["id"]), rec, btns, d["state"]))
    if brief["candidates"]:
        out.append("<h2>候选记忆</h2>")
        for c in brief["candidates"]:
            btns = [_btn("记入画像", "approve", live, c["id"], "profile", primary=True), _btn("记入规则", "approve", live, c["id"], "rules"), _btn("丢弃", "discard", live, c["id"])]
            out.append(_card(c["text"], escape(c["id"]), "", btns))
    if brief["suspects"]:
        out.append("<h2>疑似已办</h2>")
        for s in brief["suspects"]:
            btns = [_btn("确认已办", "done", live, s["id"], primary=True), _btn("还没办", "hide", live)]
            out.append(_card(s["title"], f"{escape(s['id'])} · {escape(s['reason'])}", "", btns))
    if not (brief["todos"] or brief["decisions"] or brief["candidates"] or brief["suspects"]):
        out.append('<div class="card"><div class="t">今天没有要你处理的事</div></div>')
    return "".join(out)


def render_html(brief: dict[str, Any], live: bool) -> str:
    """完整页面。live=False 是只读快照（按钮全部禁用、不带脚本）。"""
    script = f"<script>{JS}</script>" if live else ""
    return (
        '<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">'
        '<meta name="color-scheme" content="light dark"><meta name="robots" content="noindex">'
        f"<title>晨间简报</title><style>{CSS}</style></head><body><main>"
        f'<h1>晨间简报</h1><p class="sub">{escape(str(brief["for_date"]))}</p>'
        f'<div id="app">{render_body(brief, live)}</div></main><div id="msg" role="status"></div>{script}</body></html>'
    )
