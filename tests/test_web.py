#!/usr/bin/env python3
"""Проверки веб-панели pcs.web без root и без сети: вход, CSRF, маршруты на фейке,
статика, WebSocket и терминал на локальном сокете.

    python3 tests/test_web.py
"""
import base64
import http.client
import json
import os
import socket
import stat
import struct
import sys
import tempfile
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, os.pardir))
from pcs.core.util import Fail  # noqa: E402
from pcs.web import auth, fake_api, ws  # noqa: E402
from pcs.web.panel import Panel  # noqa: E402
from pcs.web.server import make_server  # noqa: E402

passed = failed = 0


def check(name, cond, detail=""):
    global passed, failed
    if cond:
        passed += 1
        print("  ok   %s" % name)
    else:
        failed += 1
        print("  FAIL %s%s" % (name, ("\n       " + str(detail)[:600]) if detail else ""))


fake_api.DELAY, fake_api.STEP = 0, 0.01
tmp = tempfile.mkdtemp(prefix="pcs-web-test-")
CFG = os.path.join(tmp, "etc", "web.json")

# ── пароль панели ────────────────────────────────────────────────────────────
print("пароль панели (web.json):")
auth.set_password(CFG, "admin", "secret-pass", iterations=1000)
raw = open(CFG).read()
check("файл 600, каталог 700", stat.S_IMODE(os.stat(CFG).st_mode) == 0o600
      and stat.S_IMODE(os.stat(os.path.dirname(CFG)).st_mode) == 0o700)
check("в файле нет пароля открытым текстом", "secret-pass" not in raw)
rec = json.loads(raw)["password"]
check("PBKDF2-SHA256 с солью", rec["algo"] == "pbkdf2-sha256" and len(bytes.fromhex(rec["salt"])) == 16, rec)
check("верный пароль — да", auth.check(CFG, "admin", "secret-pass"))
check("неверный пароль — нет", not auth.check(CFG, "admin", "secret-pasS"))
check("чужой логин — нет", not auth.check(CFG, "root", "secret-pass"))
salt1 = rec["salt"]
st1 = auth.stamp(CFG)
auth.set_password(CFG, "admin", "secret-pass", iterations=1000)
check("та же строка пароля — другая соль", json.loads(open(CFG).read())["password"]["salt"] != salt1)
check("смена пароля меняет отпечаток сессий", auth.stamp(CFG) != st1)
try:
    auth.set_password(CFG, "admin", "short", iterations=1000)
    check("короткий пароль — отказ", False)
except Fail:
    check("короткий пароль — отказ", True)
try:
    auth.set_password(CFG, "ad min", "secret-pass", iterations=1000)
    check("логин с пробелом — отказ", False)
except Fail:
    check("логин с пробелом — отказ", True)
check("нет файла — вход не настроен", not auth.configured(os.path.join(tmp, "nope.json"))
      and not auth.check(os.path.join(tmp, "nope.json"), "admin", "x"))
from pcs.web.server import set_password as sp2  # noqa: E402
check("set_password доступна и из pcs.web.server (для pcs web passwd)", sp2 is auth.set_password)

th = auth.Throttle(free=2, base=30)
th.failed("1.2.3.4")
th.failed("1.2.3.4")
check("пауза после неудачных входов", th.wait("1.2.3.4") >= 29 and th.wait("5.6.7.8") == 0)
th.passed("1.2.3.4")
check("удачный вход сбрасывает паузу", th.wait("1.2.3.4") == 0)

# ── кадры WebSocket ──────────────────────────────────────────────────────────
print("кадры WebSocket (RFC 6455):")
check("Sec-WebSocket-Accept по RFC", ws.accept("dGhlIHNhbXBsZSBub25jZQ==") == "s3pPLMBiTxaQ9kYGzzhZRbK+xOo=")
check("ключ: 16 байт base64", ws.key_ok("dGhlIHNhbXBsZSBub25jZQ==") and not ws.key_ok("abc") and not ws.key_ok(""))
M = b"\x01\x02\x03\x04"
p = ws.Parser(need_mask=True)
out = p.feed(ws.frame(ws.TEXT, "привет", mask=M) + ws.frame(ws.BIN, b"\x00\xff", mask=M))
check("маскированные кадры клиента разбираются", out == [(ws.TEXT, "привет"), (ws.BIN, b"\x00\xff")], out)
big = os.urandom(70000)
data = ws.frame(ws.BIN, big, mask=M)
out = []
for i in range(0, len(data), 999):          # по кусочкам — как приходит из сокета
    out += p.feed(data[i:i + 999])
check("длина 64 бита, приём по кусочкам", out == [(ws.BIN, big)])
mid = os.urandom(300)
check("длина 16 бит", p.feed(ws.frame(ws.BIN, mid, mask=M)) == [(ws.BIN, mid)])
out = p.feed(ws.frame(ws.TEXT, "ab", fin=False, mask=M) + ws.frame(ws.PING, b"p", mask=M)
             + ws.frame(ws.CONT, "cd", mask=M))
check("фрагменты склеиваются, пинг посреди — сразу", out == [(ws.PING, b"p"), (ws.TEXT, "abcd")], out)


def proto_err(data, need_mask=True, max_msg=ws.MAX_MSG):
    try:
        ws.Parser(need_mask=need_mask, max_msg=max_msg).feed(data)
    except ws.WSError as e:
        return e.code
    return None


check("кадр клиента без маски — 1002", proto_err(ws.frame(ws.TEXT, "x")) == 1002)
check("слишком большое сообщение — 1009", proto_err(ws.frame(ws.BIN, b"x" * 100, mask=M), max_msg=50) == 1009)
check("текст не UTF-8 — 1007", proto_err(ws.frame(ws.TEXT, b"\xff\xfe", mask=M)) == 1007)
check("продолжение без начала — 1002", proto_err(ws.frame(ws.CONT, "x", mask=M)) == 1002)
check("неизвестный код — 1002", proto_err(bytes([0x83, 0x80]) + M) == 1002)
check("кадры сервера без маски (клиентский разбор)",
      ws.Parser(need_mask=False).feed(ws.close_frame(1000, "пока")) == [(ws.CLOSE, struct.pack("!H", 1000) + "пока".encode())])

# ── слой API (без HTTP) ──────────────────────────────────────────────────────
print("слой API поверх api хаба:")


class Rec:
    """api, который только записывает вызовы run — проверить, что уходит на сервер."""

    def __init__(self, with_input=True):
        self.calls = []
        self.with_input = with_input

    def run(self, sid, part, args, **kw):
        if kw and not self.with_input:
            raise TypeError("run() got an unexpected keyword argument 'input'")
        self.calls.append((sid, part, list(args), kw.get("input")))
        if args[:1] == ["list"]:
            return {"ok": True, "data": {"proxies": [{"id": 7, "login": "u", "password": None, "port": 20007}],
                                         "pending": True}}
        return {"ok": True, "data": {"id": 9}}


rc = Rec()
code, r = Panel(rc).handle("POST", "/api/servers/2000/modlink/save", {}, {
    "del": [7], "set": [{"id": 8, "fields": {"name": "x y", "enabled": False, "password": "gen"}}],
    "add": [{"lan_ip": "192.168.9.100", "password": "S3cret!x"}]})
args = [c[2] for c in rc.calls]
check("save: del с --yes, disable, set, add, apply", r["ok"] and args[0] == ["del", "7", "--yes"]
      and ["disable", "8"] in args and ["set", "8", "name=x y", "password=gen"] in args and args[-1] == ["apply"], args)
add = next(c for c in rc.calls if c[2][0] == "add")
check("набранный пароль новой строки — через stdin, не в аргументах",
      "S3cret!x" not in " ".join(add[2]) and add[2][add[2].index("--password") + 1] == "-" and add[3] == "S3cret!x\n", add)
check("порты по умолчанию — auto", add[2][add[2].index("--port") + 1] == "auto")
code, r = Panel(Rec()).handle("GET", "/api/servers/2000/modlink", {}, None)
check("list: pending виден, пароль не уходит в браузер", r["data"]["list"]["data"]["pending"] is True
      and "password" not in r["data"]["list"]["data"]["rows"][0], r)
code, r = Panel(Rec(with_input=False)).handle("POST", "/api/servers/2000/proxyveth/table", {}, {"csv": "n,real,proxy\n"})
check("api.run без input= — понятная ошибка, не падение", code == 200 and not r["ok"] and "input" in r["error"], r)
code, r = Panel(Rec()).handle("POST", "/api/targets/host/update", {}, {})
check("нет api.update — понятная ошибка", code == 200 and not r["ok"] and "api.update" in r["error"], r)
rc = Rec()
code, r = Panel(rc).handle("POST", "/api/run", {}, {"target": "host", "part": "hivelink", "args": ["status", "--json"]})
check("run: host → id=None, --json не дублируется", r["ok"] and rc.calls == [(None, "hivelink", ["status"], None)], rc.calls)
code, r = Panel(Rec()).handle("POST", "/api/run", {}, {"target": "2000", "part": "bash", "args": ["-c", "id"]})
check("run: только части proxyveth/modlink/hivelink", code == 400, r)
code, r = Panel(Rec()).handle("POST", "/api/run", {}, {"target": "2000", "part": "proxyveth", "args": ["mode", "gw", "--yes"]})
check("run: --yes без номера ВМ — 403", code == 403, r)
code, r = Panel(Rec()).handle("POST", "/api/run", {}, {"target": "2000", "part": "proxyveth",
                                                      "args": ["mode", "gw", "--yes"], "confirm": "1000"})
check("run: чужой номер в подтверждении — 403", code == 403, r)
code, r = Panel(Rec()).handle("DELETE", "/api/overview", {}, None)
check("чужой метод — 405, неизвестный адрес — 404",
      code == 405 and Panel(Rec()).handle("GET", "/api/nope", {}, None)[0] == 404)

# ── HTTP: вход, CSRF, маршруты на фейке ─────────────────────────────────────
print("HTTP-сервер на фейке:")

TERM_SCRIPT = r'''
import os, sys
print("READY", flush=True)
for line in sys.stdin:
    line = line.strip()
    if line == "size":
        c, r = os.get_terminal_size(0)
        print("SIZE %d %d" % (c, r), flush=True)
    elif line == "quit":
        sys.exit(0)
    else:
        print("ECHO " + line, flush=True)
'''


class TermApi:
    def __getattr__(self, k):
        return getattr(fake_api, k)

    def term_argv(self, target):
        if target != "host":
            fake_api.server(target)            # нет такой ВМ — Fail
        return [sys.executable, "-c", TERM_SCRIPT]


httpd = make_server(0, TermApi(), "127.0.0.1", CFG, fake=True, fail_delay=0,
                    throttle=auth.Throttle(free=3, base=30), log_dir=None, quiet=True)
threading.Thread(target=httpd.serve_forever, daemon=True).start()
PORT = httpd.server_address[1]
ORIGIN = "http://127.0.0.1:%d" % PORT
COOKIE_NAME = httpd.app.cookie


def req(method, path, body=None, headers=None, cookie=None, raw=False):
    c = http.client.HTTPConnection("127.0.0.1", PORT, timeout=20)
    h = {"Host": "127.0.0.1:%d" % PORT}
    if body is not None:
        body = body if isinstance(body, bytes) else json.dumps(body).encode()
        h["Content-Type"] = "application/json"
    if cookie:
        h["Cookie"] = "%s=%s" % (COOKIE_NAME, cookie)
    h.update(headers or {})
    c.request(method, path, body=body, headers=h)
    r = c.getresponse()
    data = r.read()
    c.close()
    if raw:
        return r, data
    try:
        return r, json.loads(data.decode())
    except ValueError:
        return r, data


r, d = req("GET", "/")
check("без сессии / → вход", r.status == 302 and r.getheader("Location") == "/login")
r, d = req("GET", "/api/overview")
check("без сессии API — 401", r.status == 401 and d["ok"] is False)
r, d = req("GET", "/login", raw=True)
check("страница входа отдаётся", r.status == 200 and b"/static/login.js" in d)
r, d = req("POST", "/api/login", {"user": "admin", "password": "secret-pass"}, {"Origin": ORIGIN})
check("вход без X-CSRF — 403", r.status == 403)
r, d = req("POST", "/api/login", {"user": "admin", "password": "secret-pass"},
           {"Origin": "http://evil.example", "X-CSRF": "login"})
check("вход с чужого Origin — 403", r.status == 403)
r, d = req("POST", "/api/login", {"user": "admin", "password": "wrong-pass"}, {"Origin": ORIGIN, "X-CSRF": "login"})
check("неверный пароль — 401 и без cookie", r.status == 401 and not r.getheader("Set-Cookie") and not d["ok"])
r, d = req("POST", "/api/login", {"user": "admin", "password": "secret-pass"}, {"Origin": ORIGIN, "X-CSRF": "login"})
cookie = r.getheader("Set-Cookie") or ""
check("верный пароль — 200 и cookie HttpOnly; SameSite=Strict",
      r.status == 200 and d["ok"] and "HttpOnly" in cookie and "SameSite=Strict" in cookie, cookie)
SID = cookie.split(";")[0].split("=", 1)[1]
check("пароля в ответе нет", "secret-pass" not in json.dumps(d))
r, d = req("GET", "/api/session", cookie=SID)
CSRF = d["data"]["csrf"]
check("GET /api/session — пользователь и CSRF", r.status == 200 and d["data"]["user"] == "admin" and len(CSRF) > 20)
check("заголовки: CSP, nosniff, без CORS", "frame-ancestors 'none'" in (r.getheader("Content-Security-Policy") or "")
      and r.getheader("X-Content-Type-Options") == "nosniff" and r.getheader("Access-Control-Allow-Origin") is None)
r, d = req("OPTIONS", "/api/overview", headers={"Origin": "http://evil.example",
                                                "Access-Control-Request-Method": "POST"})
check("на preflight CORS ответа нет", r.getheader("Access-Control-Allow-Origin") is None and r.status >= 400)
r, d = req("GET", "/", cookie=SID, raw=True)
check("с сессией / → панель", r.status == 200 and b"/static/app.js" in d)


def P(path, body=None, csrf=None, origin=ORIGIN, cookie=None):
    h = {"X-CSRF": CSRF if csrf is None else csrf}
    if origin:
        h["Origin"] = origin
    return req("POST", path, body if body is not None else {}, h, cookie=cookie or SID)


def G(path):
    return req("GET", path, cookie=SID)


r, d = P("/api/targets/host/dns", csrf="")
check("POST без X-CSRF — 403", r.status == 403)
r, d = P("/api/targets/host/dns", csrf="bad")
check("POST с чужим X-CSRF — 403", r.status == 403)
r, d = P("/api/targets/host/dns", origin="http://evil.example")
check("POST с чужого Origin — 403", r.status == 403)
r, d = req("POST", "/api/targets/host/dns", b"[1]", {"X-CSRF": CSRF, "Origin": ORIGIN}, cookie=SID)
r2, d2 = req("POST", "/api/targets/host/dns", b"{x", {"X-CSRF": CSRF, "Origin": ORIGIN}, cookie=SID)
check("тело не объект JSON — 400", r.status == 400 and r2.status == 400)
r, d = P("/api/targets/host/dns", {"dns": "1.1.1.1 8.8.8.8"})
check("DNS хоста — применено", r.status == 200 and d["ok"] and d["data"]["dns"] == ["1.1.1.1", "8.8.8.8"], d)
r, d = P("/api/targets/2000/dns", {"dns": "1.1.1.1; rm -rf /"})
check("DNS: мусор в адресах — 400", r.status == 400)

r, d = G("/api/overview")
ov = d["data"]
srv = {s["id"]: s for s in ov["servers"]}
check("обзор: хост servervolga и три ВМ", ov["host"]["name"] == "servervolga" and sorted(srv) == [200, 1000, 2000], ov)
check("1000 USB 20/20, 2000 USB 5/5, 200 шлюз 89/90",
      (srv[1000]["mode"], srv[1000]["modems"]["ok"], srv[1000]["modems"]["total"]) == ("usb", 20, 20)
      and (srv[2000]["modems"]["ok"], srv[2000]["modems"]["total"]) == (5, 5)
      and (srv[200]["mode"], srv[200]["modems"]["ok"], srv[200]["modems"]["total"]) == ("gw", 89, 90))
r, d = G("/api/servers/2000")
check("сервер: версии частей", d["ok"] and "versions" in d["data"])
r, d = G("/api/servers/4242")
check("нет ВМ — ошибка текстом", r.status == 200 and not d["ok"] and "4242" in d["error"])
r, d = G("/api/servers/200/proxyveth")
rows = d["data"]["status"]["data"]
check("proxyveth: строки Row и источник таблицы", len(rows) == 90 and {"n", "real", "proxy", "state", "problems",
                                                                         "ext_ip", "since"} <= set(rows[0])
      and d["data"]["source"]["data"]["source"] == "google")
check("proxyveth: одна проблемная", [x["n"] for x in rows if x["state"] != "ok"] == [57])
r, d = G("/api/servers/2000/proxyveth?only=status")
check("опрос статуса — без источника", "source" not in d["data"] and d["data"]["status"]["ok"])
r, d = G("/api/servers/2000/proxyveth/table")
csv0 = d["data"]["csv"]
check("таблица: CSV с шапкой", csv0.startswith("n,real,proxy"), csv0[:80])
r, d = P("/api/servers/2000/proxyveth/table", {"csv": csv0 + "206,86,188.134.95.184:1186:modem86:pw86,1\n",
                                               "make_main": True})
check("таблица сохранена в локальную копию и стала главной", d["ok"] and d["data"]["saved"]["data"]["rows"] == 6
      and d["data"]["source"]["data"]["source"] == "local", d)
r, d = P("/api/run", {"target": 2000, "part": "proxyveth", "args": ["sync"]})
check("синхронизация создала модем 206", d["ok"] and d["data"]["created"] == [206], d)
r, d = P("/api/servers/2000/proxyveth/table", {"csv": "n,real,proxy\n207,87,188.134.95.184:1187:m:p\n"})
r, d = P("/api/run", {"target": 2000, "part": "proxyveth", "args": ["sync"]})
check("снести больше половины без --force — отказ", not d["ok"] and "--force" in d["error"], d)
r, d = P("/api/run", {"target": 2000, "part": "proxyveth", "args": ["sync", "--force"]})
check("--force без номера ВМ — 403", r.status == 403)
r, d = P("/api/run", {"target": 2000, "part": "proxyveth", "args": ["sync", "--force"], "confirm": "2000"})
check("--force с номером ВМ — выполнено", d["ok"] and d["data"]["created"] == [207], d)
r, d = P("/api/run", {"target": 2000, "part": "proxyveth", "args": ["diag", "207"]})
check("диагностика модема: шаги и итог", d["ok"] and len(d["data"]["steps"]) == 5 and d["data"]["verdict"])

r, d = G("/api/servers/2000/modlink")
ml = d["data"]["list"]["data"]["rows"]
check("modlink: строки §8 без паролей", len(ml) == 5 and all("password" not in x for x in ml)
      and {"id", "enabled", "name", "login", "port", "lan_ip", "modem_ip", "reconnect_port", "interval_min"} <= set(ml[0]))
check("modlink: состояние строк из status", d["data"]["status"]["ok"] and d["data"]["status"]["data"]["rows"][0]["state"] == "ok")
r, d = P("/api/servers/2000/modlink/reveal", {"id": 201})
check("пароль — только по явному запросу", d["ok"] and d["data"]["password"] == fake_api.S[2000]["modlink"][0]["password"])
r, d = P("/api/servers/2000/modlink/export")
check("экспорт для клиента: IP:PORT:LOGIN:PASS<TAB>ссылка", d["ok"] and d["data"]["text"].startswith("192.168.88.7:20201:u201:")
      and "\thttp://192.168.88.7:21201/reconnect" in d["data"]["text"], d)
r, d = P("/api/servers/2000/modlink/save", {"set": [{"id": 202, "fields": {"name": "новое имя", "password": "gen"}}],
                                            "add": [{"lan_ip": "192.168.206.100", "modem_ip": "192.168.206.1",
                                                     "password": "Typed-pw1"}],
                                            "del": [205]})
ids = [x["id"] for x in fake_api.S[2000]["modlink"]]
new = next(x for x in fake_api.S[2000]["modlink"] if x["id"] == 206)
check("modlink: правки, удаление, добавление (id = N) и apply", d["ok"] and d["data"]["applied"] and 205 not in ids
      and new["password"] == "Typed-pw1" and fake_api.S[2000]["ml_pending"] is False, d)
r, d = P("/api/servers/2000/modlink/save", {"set": [{"id": 202, "fields": {"port": "70000"}}]})
check("modlink: кривой порт — 400 до сервера", r.status == 400)
r, d = P("/api/run", {"target": 2000, "part": "modlink", "args": ["test", "201"]})
check("modlink test: прокси и HiLink", d["ok"] and d["data"]["proxy"]["ok"] and d["data"]["hilink"]["ok"])

r, d = P("/api/targets/2000/passwd", {"password": "rootpass1"})
check("пароль root ВМ — сохранено", d["ok"])
r, d = P("/api/targets/host/key", {"pubkey": "not a key"})
check("ключ SSH: не ключ — 400", r.status == 400)
r, d = P("/api/targets/2000/key", {"pubkey": "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIOMqqnkVzrm0SdG6UOoqKLsabgH5C9okWi0dh2l9GKJl me@pc"})
check("ключ SSH — добавлен", d["ok"])
r, d = P("/api/targets/2000/mpspace", {"action": "check"})
check("mp.space: проверка", d["ok"] and d["data"]["result"]["running"] is True)
r, d = P("/api/targets/2000/mpspace", {"action": "auth", "auth": ""})
check("mp.space: пустой auth.mp — 400", r.status == 400)


def wait_job(jid, t=10):
    end = time.time() + t
    while time.time() < end:
        r, d = G("/api/jobs/" + jid)
        if d["ok"] and d["data"]["state"] != "running":
            return d["data"]
        time.sleep(0.05)
    return d.get("data")


r, d = P("/api/targets/host/update")
j = wait_job(d["data"]["job"]) if d["ok"] else None
check("обновить PCS — задание с журналом", j and j["state"] == "done" and len(j["log"]) >= 3, j)
r, d = P("/api/servers", {"name": "bad name!", "mode": "usb"})
check("создать ВМ: кривое имя — 400", r.status == 400)
r, d = P("/api/servers", {"name": "pcs3000", "cores": 4, "ram_gb": 8, "disk_gb": 40, "mode": "gw",
                          "sheet": "https://docs.google.com/spreadsheets/d/x/edit", "mpspace": True})
j = wait_job(d["data"]["job"]) if d["ok"] else None
check("создать ВМ — задание: журнал и итог", j and j["state"] == "done" and j["result"]["id"] == 3000
      and any("mp.space" in x for x in j["log"]), j)
r, d = G("/api/overview")
check("новая ВМ в обзоре (кэш сброшен)", any(s["id"] == 3000 and s["mode"] == "gw" for s in d["data"]["servers"]))
r, d = P("/api/servers", {"name": "fail-vm", "mode": "usb"})
j = wait_job(d["data"]["job"])
check("упавшее задание — состояние failed и причина", j["state"] == "failed" and j["result"]["error"], j)
r, d = P("/api/servers/3000/delete", {"confirm": "300"})
check("удалить ВМ: неверный номер — 403", r.status == 403 and 3000 in fake_api.S)
r, d = P("/api/servers/3000/delete", {"confirm": "3000"})
check("удалить ВМ: номер совпал — удалена", d["ok"] and 3000 not in fake_api.S)
r, d = G("/api/jobs/nope")
check("нет задания — ошибка текстом", not d["ok"])

# ── статика ──────────────────────────────────────────────────────────────────
print("статика:")
r, d = req("GET", "/static/app.js", raw=True)
check("app.js отдаётся как JS", r.status == 200 and "javascript" in r.getheader("Content-Type") and b"jobView" in d)
etag = r.getheader("ETag")
r, d = req("GET", "/static/app.js", headers={"If-None-Match": etag}, raw=True)
check("ETag → 304", r.status == 304 and d == b"")
r, d = req("GET", "/static/vendor/xterm/xterm.js", raw=True)
check("xterm.js лежит в репозитории", r.status == 200 and len(d) > 100000)
check("лицензии xterm (MIT) рядом", all(os.path.isfile(os.path.join(HERE, os.pardir, "pcs/web/static/vendor/xterm", f))
                                        for f in ("LICENSE", "LICENSE.addon-fit", "addon-fit.js", "xterm.css")))
for bad in ("/static/../server.py", "/static/%2e%2e/server.py", "/static/..%2fserver.py", "/static/vendor/../../auth.py",
            "/static/%2e%2e%2f%2e%2e%2fcore/util.py", "/static/..\\server.py", "/static//etc/passwd",
            "/static/.hidden", "/static/%00app.js"):
    r, d = req("GET", bad, raw=True)
    check("не выйти из static: %s" % bad, r.status == 404 and b"def " not in d and b"root:" not in d, (r.status, d[:80]))
import re  # noqa: E402
SD = os.path.join(HERE, os.pardir, "pcs/web/static")
ext = []
for f in ("index.html", "login.html", "app.js", "login.js", "theme.js"):
    t = open(os.path.join(SD, f)).read()
    ext += re.findall(r'(?:src|href)\s*[=:]\s*["\'](?:https?:)?//[^"\']+', t)
check("никаких внешних CDN: скрипты и стили — только свои", not ext, ext)

# ── WebSocket и терминал ─────────────────────────────────────────────────────
print("терминал: WebSocket ↔ pty:")


def ws_open(path, origin=ORIGIN, cookie=SID, version="13", key=None):
    s = socket.create_connection(("127.0.0.1", PORT), timeout=10)
    key = key or base64.b64encode(os.urandom(16)).decode()
    lines = ["GET %s HTTP/1.1" % path, "Host: 127.0.0.1:%d" % PORT, "Upgrade: websocket",
             "Connection: keep-alive, Upgrade", "Sec-WebSocket-Key: " + key, "Sec-WebSocket-Version: " + version]
    if origin:
        lines.append("Origin: " + origin)
    if cookie:
        lines.append("Cookie: other=1; %s=%s" % (COOKIE_NAME, cookie))
    s.sendall(("\r\n".join(lines) + "\r\n\r\n").encode())
    buf = b""
    while b"\r\n\r\n" not in buf:
        chunk = s.recv(4096)
        if not chunk:
            break
        buf += chunk
    head, _, rest = buf.partition(b"\r\n\r\n")
    status = int(head.split(b" ")[1]) if head else 0
    hdrs = {}
    for ln in head.decode("latin-1").split("\r\n")[1:]:
        k, _, v = ln.partition(":")
        hdrs[k.strip().lower()] = v.strip()
    return s, status, hdrs, key, rest


class Client:
    def __init__(self, s, rest):
        self.s, self.p, self.q = s, ws.Parser(need_mask=False), []
        if rest:
            self.q += self.p.feed(rest)

    def send(self, op, data):
        self.s.sendall(ws.frame(op, data, mask=os.urandom(4)))

    def until(self, pred, t=10):
        """Читать кадры, пока pred(накопленный вывод, кадры) не станет правдой."""
        end, text = time.time() + t, b""
        frames = []
        while time.time() < end:
            while self.q:
                f = self.q.pop(0)
                frames.append(f)
                if f[0] == ws.BIN:
                    text += f[1]
                if pred(text, frames):
                    return text, frames
            self.s.settimeout(max(0.05, end - time.time()))
            try:
                chunk = self.s.recv(65536)
            except socket.timeout:
                break
            if not chunk:
                break
            self.q += self.p.feed(chunk)
        return text, frames


s, st, h, key, rest = ws_open("/ws/term?target=2000", cookie=None)
check("WS без сессии — 401", st == 401)
s.close()
s, st, h, key, rest = ws_open("/ws/term?target=2000", origin="http://evil.example")
check("WS с чужого Origin — 403", st == 403)
s.close()
s, st, h, key, rest = ws_open("/ws/term?target=2000", origin=None)
check("WS без Origin — 403", st == 403)
s.close()
s, st, h, key, rest = ws_open("/ws/term?target=2000", version="8")
check("WS не 13-й версии — 426", st == 426 and h.get("sec-websocket-version") == "13")
s.close()
s, st, h, key, rest = ws_open("/ws/term?target=4242")
check("WS к несуществующей ВМ — 400", st == 400)
s.close()

s, st, h, key, rest = ws_open("/ws/term?target=2000&cols=90&rows=20")
check("рукопожатие: 101 и верный Accept", st == 101 and h.get("sec-websocket-accept") == ws.accept(key)
      and h.get("upgrade", "").lower() == "websocket", (st, h))
c = Client(s, rest)
out, _ = c.until(lambda t, f: b"READY" in t)
check("вывод процесса приходит двоичными кадрами", b"READY" in out, out)
c.send(ws.BIN, "size\n")
out, _ = c.until(lambda t, f: b"SIZE" in t)
check("размер окна из запроса: 90×20", b"SIZE 90 20" in out, out)
c.send(ws.TEXT, json.dumps({"type": "resize", "cols": 132, "rows": 41}))
c.send(ws.BIN, "size\n")
out, _ = c.until(lambda t, f: b"SIZE 132" in t)
check("изменение размера окна доходит до pty", b"SIZE 132 41" in out, out)
c.send(ws.BIN, "привет\n".encode())
out, _ = c.until(lambda t, f: "ECHO привет".encode() in t)
check("ввод (UTF-8) доходит до процесса", "ECHO привет".encode() in out, out)
c.send(ws.PING, b"pp")
out, fr = c.until(lambda t, f: any(x[0] == ws.PONG for x in f))
check("ping → pong с тем же телом", (ws.PONG, b"pp") in fr, fr)
with httpd.app.tlock:
    pids = list(httpd.app.terms)
check("терминал учтён панелью", len(pids) == 1)
c.send(ws.CLOSE, struct.pack("!H", 1000))
out, fr = c.until(lambda t, f: any(x[0] == ws.CLOSE for x in f))
check("закрытие вкладки: ответный CLOSE", any(x[0] == ws.CLOSE for x in fr), fr)
s.close()


def gone(pid, t=6):
    end = time.time() + t
    while time.time() < end:
        with httpd.app.tlock:
            known = pid in httpd.app.terms
        try:
            os.kill(pid, 0)
            alive = True
        except OSError:
            alive = False
        if not alive and not known:
            return True
        time.sleep(0.05)
    return False


check("процесс снят после закрытия вкладки", pids and gone(pids[0]))

s, st, h, key, rest = ws_open("/ws/term?target=host")
c = Client(s, rest)
c.until(lambda t, f: b"READY" in t)
with httpd.app.tlock:
    pid = list(httpd.app.terms)[0]
s.close()                                       # вкладку закрыли без CLOSE — просто оборвалось
check("оборванное соединение — процесс тоже снят", gone(pid))

s, st, h, key, rest = ws_open("/ws/term?target=2000")
c = Client(s, rest)
c.until(lambda t, f: b"READY" in t)
c.send(ws.BIN, "quit\n")
out, fr = c.until(lambda t, f: any(x[0] == ws.CLOSE for x in f))
cl = [x for x in fr if x[0] == ws.CLOSE]
check("процесс завершился — CLOSE 1000 с причиной", cl and struct.unpack("!H", cl[0][1][:2])[0] == 1000
      and "процесс" in cl[0][1][2:].decode(), fr)
s.close()

s, st, h, key, rest = ws_open("/ws/term?target=2000")
c = Client(s, rest)
c.until(lambda t, f: b"READY" in t)
s.sendall(ws.frame(ws.BIN, b"size\n"))         # без маски — нарушение протокола
out, fr = c.until(lambda t, f: any(x[0] == ws.CLOSE for x in f))
cl = [x for x in fr if x[0] == ws.CLOSE]
check("кадр без маски — CLOSE 1002", cl and struct.unpack("!H", cl[0][1][:2])[0] == 1002, fr)
s.close()

# ── выход, пауза входа, смена пароля ────────────────────────────────────────
print("выход и сессии:")
r, d = P("/api/logout")
check("выход — cookie стирается", d["ok"] and "Max-Age=0" in (r.getheader("Set-Cookie") or ""))
r, d = G("/api/overview")
check("после выхода — 401", r.status == 401)

r, d = req("POST", "/api/login", {"user": "admin", "password": "secret-pass"}, {"Origin": ORIGIN, "X-CSRF": "login"})
sid2 = r.getheader("Set-Cookie").split(";")[0].split("=", 1)[1]
auth.set_password(CFG, "admin", "secret-pass2", iterations=1000)
r, d = req("GET", "/api/overview", cookie=sid2)
check("смена пароля закрывает старые сессии", r.status == 401)

for _ in range(3):
    req("POST", "/api/login", {"user": "admin", "password": "nope-nope"}, {"Origin": ORIGIN, "X-CSRF": "login"})
r, d = req("POST", "/api/login", {"user": "admin", "password": "secret-pass2"}, {"Origin": ORIGIN, "X-CSRF": "login"})
check("после неудач — пауза 429 даже с верным паролем", r.status == 429 and r.getheader("Retry-After")
      and not r.getheader("Set-Cookie"), (r.status, d))

httpd.shutdown()
httpd.app.close_terms()
httpd.server_close()

print("\nитого: ok %d, fail %d" % (passed, failed))
sys.exit(1 if failed else 0)
