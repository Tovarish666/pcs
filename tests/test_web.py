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
code, r = Panel(rc).handle("POST", "/api/targets/2000/modlink/save", {}, {
    "del": [7], "set": [{"id": 8, "fields": {"name": "x y", "enabled": False, "password": "gen"}}],
    "add": [{"lan_ip": "192.168.9.100", "password": "S3cret!x"}]})
args = [c[2] for c in rc.calls]
check("save: del с --yes, disable, set, add, apply", r["ok"] and args[0] == ["del", "7", "--yes"]
      and ["disable", "8"] in args and ["set", "8", "name=x y", "password=gen"] in args and args[-1] == ["apply"], args)
add = next(c for c in rc.calls if c[2][0] == "add")
check("набранный пароль новой строки — через stdin, не в аргументах",
      "S3cret!x" not in " ".join(add[2]) and add[2][add[2].index("--password") + 1] == "-" and add[3] == "S3cret!x\n", add)
check("порты по умолчанию — auto", add[2][add[2].index("--port") + 1] == "auto")
check("новая строка без поля защиты — без флагов (по умолчанию без защиты)",
      "--no-protect" not in add[2] and "--protect" not in add[2])
rc = Rec()
Panel(rc).handle("POST", "/api/targets/2000/modlink/save", {}, {"add": [{"lan_ip": "192.168.9.100", "protect": True}]})
check("новая строка с галочкой защиты — add --protect", any(c[2][0] == "add" and "--protect" in c[2] for c in rc.calls), rc.calls)
rc = Rec()
code, r = Panel(rc).handle("POST", "/api/targets/host/modlink/save", {}, {
    "set": [{"id": 8, "fields": {"protect": False}}], "add": [{"lan_ip": "192.168.3.100", "protect": False}]})
args = [c[2] for c in rc.calls]
check("modlink на хосте: id=None; защита снимается set protect=0 и add --no-protect",
      r["ok"] and all(c[0] is None for c in rc.calls) and ["set", "8", "protect=0"] in args
      and any(x[0] == "add" and "--no-protect" in x for x in args), rc.calls)
code, r = Panel(Rec()).handle("POST", "/api/targets/2000/modlink/save", {}, {"set": [{"id": 8, "fields": {"protect": "может"}}]})
check("защита: не 1/0 — 400", code == 400, r)
code, r = Panel(Rec()).handle("GET", "/api/servers/2000/modlink", {}, None)
check("старый адрес /api/servers/N/modlink — 404 (без алиасов)", code == 404, r)
code, r = Panel(Rec()).handle("GET", "/api/targets/2000/modlink", {}, None)
check("list: pending виден, пароль не уходит в браузер", r["data"]["list"]["data"]["pending"] is True
      and "password" not in r["data"]["list"]["data"]["rows"][0], r)
code, r = Panel(Rec(with_input=False)).handle("POST", "/api/targets/2000/proxyveth/table", {}, {"csv": "n,real,proxy\n"})
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

    def term_argv(self, target, serial=False):
        if target != "host":
            fake_api.server(target)            # нет такой ВМ — Fail
        SERIAL.append(serial)
        return [sys.executable, "-c", TERM_SCRIPT]


SERIAL = []


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
c = http.client.HTTPConnection("127.0.0.1", PORT, timeout=20)
c.request("POST", "/api/login", body=json.dumps({"user": "admin", "password": "x"}),
          headers={"Content-Type": "application/json", "Origin": ORIGIN})
r1 = c.getresponse()
r1.read()
try:
    c.request("GET", "/login")
    r2 = c.getresponse()
    r2.read()
    st2 = r2.status
except (OSError, http.client.HTTPException):
    st2 = "закрыто"          # клиент переподключился бы сам — тоже правильно
c.close()
check("отказ до чтения тела: соединение закрыто, тело не становится «следующим запросом» (не 501)",
      r1.status == 403 and r1.getheader("Connection") == "close" and st2 != 501, (r1.status, st2))
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


def RUN(body):
    """/api/run как в браузере: долгая команда приходит заданием — дождаться и вернуть итог конвертом."""
    r, d = P("/api/run", body)
    if not (d.get("ok") and isinstance(d.get("data"), dict) and set(d["data"]) == {"job"}):
        return r, d
    for _ in range(150):
        _, j = G("/api/jobs/" + d["data"]["job"])
        if j["data"]["state"] != "running":
            break
        time.sleep(0.2)
    j = j["data"]
    return r, ({"ok": True, "data": j["result"]} if j["state"] == "done"
               else {"ok": False, "error": (j.get("result") or {}).get("error") or j.get("error")})


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
check("1000 USB 19/20 (один ждёт сервер), 2000 USB 5/5, 200 шлюз 89/90",
      (srv[1000]["mode"], srv[1000]["modems"]["ok"], srv[1000]["modems"]["total"]) == ("usb", 19, 20)
      and (srv[2000]["modems"]["ok"], srv[2000]["modems"]["total"]) == (5, 5)
      and (srv[200]["mode"], srv[200]["modems"]["ok"], srv[200]["modems"]["total"]) == ("gw", 89, 90))
r, d = G("/api/servers/2000")
check("сервер: версии частей", d["ok"] and "versions" in d["data"])
r, d = G("/api/servers/4242")
check("нет ВМ — ошибка текстом", r.status == 200 and not d["ok"] and "4242" in d["error"])
r, d = G("/api/targets/200/proxyveth")
rows = d["data"]["status"]["data"]
check("proxyveth: строки Row и источник таблицы", len(rows) == 90 and {"n", "real", "proxy", "state", "problems",
                                                                         "ext_ip", "since"} <= set(rows[0])
      and d["data"]["source"]["data"]["source"] == "google")
check("proxyveth: одна проблемная", [x["n"] for x in rows if x["state"] != "ok"] == [57])
r, d = G("/api/targets/2000/proxyveth?only=status")
check("опрос статуса — без источника", "source" not in d["data"] and d["data"]["status"]["ok"])
r, d = G("/api/targets/2000/proxyveth/table")
csv0 = d["data"]["csv"]
check("таблица: CSV с шапкой", csv0.startswith("n,real,proxy"), csv0[:80])
r, d = P("/api/targets/2000/proxyveth/table", {"csv": csv0 + "206,86,188.134.95.184:1186:modem86:pw86,1\n",
                                               "make_main": True})
check("таблица сохранена в локальную копию и стала главной", d["ok"] and d["data"]["saved"]["data"]["rows"] == 6
      and d["data"]["source"]["data"]["source"] == "local", d)
r, d = RUN({"target": 2000, "part": "proxyveth", "args": ["sync"]})
check("синхронизация создала модем 206", d["ok"] and d["data"]["created"] == [206], d)
r, d = P("/api/targets/2000/proxyveth/table", {"csv": "n,real,proxy\n207,87,188.134.95.184:1187:m:p\n"})
r, d = RUN({"target": 2000, "part": "proxyveth", "args": ["sync"]})
check("снести больше половины без --force — отказ", not d["ok"] and "--force" in d["error"], d)
r, d = P("/api/run", {"target": 2000, "part": "proxyveth", "args": ["sync", "--force"]})
check("--force без номера ВМ — 403", r.status == 403)
r, d = RUN({"target": 2000, "part": "proxyveth", "args": ["sync", "--force"], "confirm": "2000"})
check("--force с номером ВМ — выполнено", d["ok"] and d["data"]["created"] == [207], d)
r, d = P("/api/run", {"target": 2000, "part": "proxyveth", "args": ["diag", "207"]})
check("диагностика модема: шаги и итог", d["ok"] and len(d["data"]["steps"]) == 5 and d["data"]["verdict"])

r, d = G("/api/targets/2000/modlink")
ml = d["data"]["list"]["data"]["rows"]
check("modlink: строки §8 без паролей", len(ml) == 5 and all("password" not in x for x in ml)
      and {"id", "enabled", "name", "login", "port", "lan_ip", "modem_ip", "reconnect_port", "interval_min"} <= set(ml[0]))
check("modlink: состояние строк из status", d["data"]["status"]["ok"] and d["data"]["status"]["data"]["rows"][0]["state"] == "ok")
check("modlink: выключатель в status", d["data"]["status"]["data"].get("on") is True)
r, d = P("/api/run", {"target": 2000, "part": "modlink", "args": ["off"]})
r2, d2 = G("/api/targets/2000/modlink?only=status")
st = d2["data"]["status"]["data"]
check("modlink off: 3proxy и демон остановлены, включённые строки — off", d["ok"] and st["on"] is False
      and st["proxy"] == "inactive" and st["daemon"] == "inactive" and st["rows"][0]["state"] == "off", (d, st))
r, d = P("/api/run", {"target": 2000, "part": "modlink", "args": ["on"]})
check("modlink on: снова работает", d["ok"] and d["data"]["on"] is True
      and G("/api/targets/2000/modlink?only=status")[1]["data"]["status"]["data"]["rows"][0]["state"] == "ok", d)
r, d = P("/api/targets/2000/modlink/reveal", {"id": 201})
check("пароль — только по явному запросу", d["ok"] and d["data"]["password"] == fake_api.S[2000]["modlink"][0]["password"])
r, d = P("/api/targets/2000/modlink/export")
check("экспорт для клиента — по колонкам (прокси | ребут | реконнект) для вставки в таблицу",
      d["ok"] and d["data"]["columns"] == ["Ip:port:log:pass", "Reboot", "ReConnect"]
      and d["data"]["rows"][0][0].startswith("192.168.88.7:") and d["data"]["rows"][0][1].endswith("/reboot")
      and d["data"]["rows"][0][2].endswith("/reconnect"), d)
r3, d3 = P("/api/targets/2000/modlink/export", {"what": "reconnect"})
check("копировать только ссылки реконнекта — одна колонка", d3["ok"] and d3["data"]["columns"] == ["ReConnect"]
      and all(len(x) == 1 and "/reconnect" in x[0] for x in d3["data"]["rows"]), d3)
r3, d3 = P("/api/targets/2000/modlink/export", {"what": "csv"})
check("что копировать — только известное (400)", r3.status == 400, d3)
r2, d2 = P("/api/targets/2000/modlink/export", {"table": True})
check("для proxyveth — колонки n | real | proxy, без шапки в строках",
      d2["ok"] and d2["data"]["columns"] == ["n", "real", "proxy"] and d2["data"]["rows"][0][0] == "201"
      and d2["data"]["rows"][0][2].startswith("192.168.88.7:"), d2)
r, d = P("/api/targets/2000/modlink/export")
check("экспорт для клиента: IP:PORT:LOGIN:PASS<TAB>ребут<TAB>реконнект", d["ok"] and d["data"]["text"].startswith("192.168.88.7:10000:modem201:")
      and "\thttp://192.168.88.7:10001/reboot\thttp://192.168.88.7:10002/reconnect" in d["data"]["text"], d)
r, d = P("/api/targets/2000/modlink/save", {"set": [{"id": 202, "fields": {"name": "новое имя", "password": "gen"}}],
                                            "add": [{"lan_ip": "192.168.206.100", "modem_ip": "192.168.206.1",
                                                     "password": "Typed-pw1"}],
                                            "del": [205]})
ids = [x["id"] for x in fake_api.S[2000]["modlink"]]
new = next(x for x in fake_api.S[2000]["modlink"] if x["id"] == 206)
check("modlink: правки, удаление, добавление (id = N) и apply", d["ok"] and d["data"]["applied"] and 205 not in ids
      and new["password"] == "Typed-pw1" and fake_api.S[2000]["ml_pending"] is False, d)
r, d = P("/api/targets/2000/modlink/save", {"set": [{"id": 202, "fields": {"port": "70000"}}]})
check("modlink: кривой порт — 400 до сервера", r.status == 400)
check("modlink: у строк есть защита (protect)", all("protect" in x for x in ml))
r, d = P("/api/targets/host/modlink/export", {"table": True})
check("modlink хоста → строки для proxyveth и предупреждения", d["ok"] and d["data"]["text"].startswith("n,real,proxy,enabled\n")
      and isinstance(d["data"]["warnings"], list), d)
r, d = G("/api/targets/host/modlink")
check("modlink на хосте: строки настоящих модемов хоста", d["ok"] and d["data"]["list"]["ok"]
      and [x["id"] for x in d["data"]["list"]["data"]["rows"]] == [1, 2], d)
r, d = G("/api/targets/2000/modlink")
check("modlink: общие настройки приходят вместе с таблицей", d["data"]["settings"]["ok"]
      and d["data"]["settings"]["data"]["base_port"] == 10000, d["data"].get("settings"))
r, d = P("/api/targets/2000/modlink/settings", {"base_port": 30000, "lan_ip": "", "wan_ip": "203.0.113.7"})
ports = sorted((x["id"], x["port"], x["reconnect_port"]) for x in fake_api.S[2000]["modlink"])
check("настройки: базовый порт — порты всех строк заново, WAN IP сохранён", d["ok"] and d["data"]["renumbered"]
      and ports[0][1:] == (30000, 30002) and ports[1][1:] == (30003, 30005)
      and fake_api.S[2000]["ml_settings"]["wan_ip"] == "203.0.113.7", (d, ports))
r, d = P("/api/targets/2000/modlink/settings", {"wan_ip": "не адрес"})
check("настройки: кривой адрес — 400", r.status == 400, d)
r, d = P("/api/targets/2000/modlink/settings", {"base_port": 70000})
check("настройки: порт вне 1–65534 — 400", r.status == 400, d)
r, d = G("/api/targets/host/modlink")
check("старый modlink-linux на хосте виден панели (unit legacy)", d["data"]["status"]["data"]["unit"] == "legacy", d)
r, d = P("/api/targets/host/modlink/save", {"set": [{"id": 1, "fields": {"protect": False}}]})
check("при старом modlink-linux «Применить» — понятный отказ", not d["ok"] and "migrate" in d["error"], d)
r, d = P("/api/run", {"target": "host", "part": "modlink", "args": ["migrate", "--force", "--stop-panel"]})
check("перенос с --force без подтверждения — 403", r.status == 403, d)
r, d = P("/api/run", {"target": "host", "part": "modlink", "args": ["migrate", "--force", "--stop-panel"], "confirm": "host"})
check("долгая команда (migrate) — сразу задание, запрос не висит", d["ok"] and set(d["data"]) == {"job"}, d)
jid = d["data"]["job"]
for _ in range(100):
    r, j = G("/api/jobs/" + jid)
    if j["data"]["state"] != "running":
        break
    time.sleep(0.2)
check("задание доработало: итог — данные команды", j["data"]["state"] == "done" and not fake_api.HML["legacy"]
      and isinstance(j["data"]["result"], dict) and "rows" in j["data"]["result"], j)
r, d = G("/api/jobs")
check("список заданий для страницы хоста", d["ok"] and any(x["id"] == jid for x in d["data"]), d)
r, d = P("/api/run", {"target": "host", "part": "modlink", "args": ["migrate", "--dry-run"]})
check("предпросмотр (--dry-run) — сразу ответ, без задания", d["ok"] and "rows" in d["data"], d)
r, d = P("/api/targets/host/modlink/save", {"set": [{"id": 1, "fields": {"protect": False}}]})
check("modlink на хосте: снять защиту и применить", d["ok"] and d["data"]["applied"]
      and fake_api.HML["modlink"][0]["protect"] is False, d)
r, d = P("/api/run", {"target": 2000, "part": "modlink", "args": ["test", "201"]})
check("modlink test: прокси и HiLink", d["ok"] and d["data"]["proxy"]["ok"] and d["data"]["hilink"]["ok"])

r, d = G("/api/targets/host")
check("сводка хоста: вид, proxyveth, mp.space (можно ли)", d["ok"] and d["data"]["kind"] == "proxmox"
      and d["data"]["vms"] is True and d["data"]["mpspace_ok"] is False and "modems" in d["data"], d)
r, d = P("/api/targets/host/mpspace", {"action": "install"})
check("mp.space на хост Proxmox — понятный отказ (GRUB, все ВМ)", not d["ok"] and "GRUB" in d["error"], d)
r, d = G("/api/targets/host/proxyveth")
check("proxyveth на хосте Proxmox без режима — честная ошибка, а не падение", d["ok"]
      and not d["data"]["status"]["ok"] and "режим" in d["data"]["status"]["error"], d)
r, d = P("/api/run", {"target": "host", "part": "proxyveth", "args": ["mode"]})
check("proxyveth на хосте — команды идут (mode)", d["ok"], d)
r, d = G("/api/vms")
check("ВМ Proxmox не под PCS", d["ok"] and {v["id"] for v in d["data"]} >= {100}, d)
r, d = P("/api/servers/2000/power", {"action": "explode"})
check("питание: неизвестное действие — 400", r.status == 400, d)
r, d = P("/api/servers/2000/power", {"action": "shutdown"})
check("питание: выключить", d["ok"] and d["data"]["state"] == "stopped", d)
r, d = P("/api/servers/2000/power", {"action": "start"})
check("питание: включить", d["ok"] and d["data"]["state"] == "running", d)
r, d = P("/api/servers/2000/color", {"color": "violet"})
r2, d2 = G("/api/overview")
check("цвет ВМ — в сводке для дерева", d["ok"] and [s for s in d2["data"]["servers"] if s["id"] == 2000][0]["color"] == "violet", d)
r, d = P("/api/vms/100/adopt", {"ip": "не ip"})
check("добавить в PCS: кривой IP — 400", r.status == 400, d)
r, d = P("/api/vms/100/adopt", {"ip": "192.168.88.30", "password": "pw"})
check("добавить в PCS — задание", d["ok"] and "job" in d["data"], d)
for _ in range(60):
    r, j = G("/api/jobs/" + str(d["data"]["job"]))
    if j["data"]["state"] != "running":
        break
    time.sleep(0.2)
r, d = G("/api/overview")
check("после задания ВМ 100 — в PCS", any(s["id"] == 100 for s in d["data"]["servers"]), d)
r, d = P("/api/servers/100/forget", {})
check("убрать из PCS без номера — 403", r.status == 403, d)
r, d = P("/api/servers/100/forget", {"confirm": "100"})
r2, d2 = G("/api/overview")
check("убрать из PCS — записи нет, ВМ снова «не под PCS»", d["ok"] and not any(s["id"] == 100 for s in d2["data"]["servers"]), d)
r, d = G("/api/self")
check("свой отпечаток сертификата — для присоединения", d["ok"] and len(d["data"]["fingerprint"]) == 95, d)
r, d = P("/api/nodes/join", {"addr": "95.165.83.236", "user": "admin", "password": "wrong-pass"})
check("присоединить хост: неверный пароль — понятный отказ", not d["ok"] and "неверный" in d["error"], d)
r, d = P("/api/nodes/join", {"addr": "95.165.83.236", "user": "admin", "password": "pcs-fake", "name": "vps-msk1"})
check("присоединить хост VPS", d["ok"] and d["data"]["name"] == "vps-msk1", d)
r, d = G("/api/nodes")
check("присоединённые хосты со сводкой", d["ok"] and d["data"][0]["name"] == "vps-msk1" and d["data"][0]["host_info"]["kind"] == "ubuntu", d)
r, d = P("/api/nodes/vps-msk1/targets/host/update", {})
check("действие на присоединённом хосте через хаб (обновить PCS → задание там)", d["ok"] and "job" in d["data"], d)
r, d = G("/api/nodes/vps-msk1/jobs/" + str(d["data"]["job"]))
check("его задание — через хаб", d["ok"] and d["data"]["title"], d)
r, d = P("/api/nodes/vps-msk1/login", {})
check("одноразовая ссылка входа в его панель", d["ok"] and "/login" in d["data"]["url"], d)
r, d = G("/api/nodes/vps-msk1/v1/join")
check("через хаб к /api/v1 напрямую — нельзя", r.status == 400, d)
r, d = P("/api/nodes/vps-msk1/remove", {})
check("отсоединить без подтверждения именем — 403", r.status == 403, d)
r, d = P("/api/nodes/vps-msk1/remove", {"confirm": "vps-msk1"})
check("отсоединить с подтверждением", d["ok"] and d["data"]["revoked"], d)
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
      and any("mobileproxy.space" in x for x in j["log"]) and any("── Образ ──" in x for x in j["log"]), j)
r, d = G("/api/overview")
check("новая ВМ в обзоре (кэш сброшен)", any(s["id"] == 3000 and s["mode"] == "gw" for s in d["data"]["servers"]))
r, d = P("/api/servers", {"name": "fail-vm", "mode": "usb"})
j = wait_job(d["data"]["job"])
check("упавшее задание — состояние failed и причина", j["state"] == "failed" and j["result"]["error"], j)
check("упавшее задание можно повторить", j["retry"] is True, j)
r, d = P("/api/jobs/%s/retry" % j["id"])
j2 = wait_job(d["data"]["job"]) if d["ok"] else None
check("«Повторить» — новое задание, ВМ доделана", j2 and j2["state"] == "done" and "(повтор)" in j2["title"], (d, j2))
r, d = P("/api/jobs/%s/retry" % j["id"])
check("повторить второй раз то же — отказ; старое знает, кем повторено", not d["ok"]
      and G("/api/jobs/" + j["id"])[1]["data"]["retried"] == j2["id"], d)

print("браузер вкладок mp.space и ЮMoney (фейк):")
r, d = G("/api/browser/mp/2000")
check("состояние: не установлен, прокси нет, кнопки-помогаторы есть", d["ok"] and d["data"]["installed"] is False
      and d["data"]["proxy"] is None and d["data"]["links"] and d["data"]["title"] == "mp.space", d)
r, d = P("/api/browser/mp/2000/start", {})
check("открыть без установки — понятный отказ", not d["ok"] and "не установлен" in d["error"], d)
r, d = P("/api/browser/install", {})
j = wait_job(d["data"]["job"]) if d["ok"] else None
check("установка — заданием", j and j["state"] == "done", j)
r, d = P("/api/browser/mp/2000/start", {})
check("без прокси не открывается (иначе — с адреса сервера)", not d["ok"] and "прокси" in d["error"], d)
r, d = P("/api/browser/mp/2000/proxy", {"proxy": "eu.example.net:8080:user:secret", "type": "http"})
check("прокси: проверен, адрес и страна, пароль скрыт", d["ok"] and d["data"]["check"]["cc"] == "DE"
      and d["data"]["check"]["region_ok"] is True and "secret" not in json.dumps(d), d)
r, d = P("/api/browser/mp/2000/proxy", {"proxy": "bad.example:1", "type": "http"})
check("нерабочий прокси не сохраняется", not d["ok"] and G("/api/browser/mp/2000")[1]["data"]["proxy"].startswith("http://user"), d)
r, d = P("/api/browser/mp/2000/start", {})
check("открыть", d["ok"] and G("/api/browser/mp/2000")[1]["data"]["running"] is True, d)
r, d = P("/api/browser/mp/2000/open", {"url": "https://mobileproxy.space/"})
check("кнопка-помогатор открывает адрес", d["ok"], d)
r, d = P("/api/browser/mp/2000/links", {"links": [["ЛК", "https://mobileproxy.space/user.html"]]})
check("свои кнопки сохраняются", d["ok"] and G("/api/browser/mp/2000")[1]["data"]["links"] == [["ЛК", "https://mobileproxy.space/user.html"]], d)
r, d = G("/api/browser/ym/2000")
check("у ЮMoney той же ВМ — свой профиль и прокси", d["ok"] and d["data"]["proxy"] is None and d["data"]["running"] is False, d)
r, d = P("/api/browser/mp/2000/stop", {})
check("закрыть", d["ok"] and G("/api/browser/mp/2000")[1]["data"]["running"] is False, d)
r, d = G("/api/browser/xx/2000")
check("чужая вкладка — 404", r.status == 404, (r.status, d))
print("веб модемов в отдельном окне (фейк):")
r, d = P("/api/browser/md/2000/modem", {"ip": "192.168.201.1"})
check("веб модема ВМ: браузер модемов этой ВМ запущен, адрес открыт; путь — SSH хаба",
      d["ok"] and d["data"] == {"opened": "http://192.168.201.1/", "started": True}
      and fake_api.FakeBrowser and G("/api/browser/md/2000")[1]["data"]["running"] is True, d)
r, d = P("/api/browser/md/2000/modem", {"ip": "192.168.202.1"})
check("второй модем той же ВМ — новая вкладка в том же браузере", d["ok"] and d["data"]["started"] is False, d)
for bad in ("8.8.8.8", "localhost", "192.168.1.1:22", ""):
    r, d = P("/api/browser/md/2000/modem", {"ip": bad})
    check("веб модема: «%s» — отказ (только частные адреса модемов)" % bad, r.status == 400 and not d["ok"], d)
r, d = P("/api/browser/md/2000/modem", {"ip": "192.168.1.1", "port": 22})
check("веб модема: порт не веба — отказ", r.status == 400, d)
r, d = P("/api/browser/md/host/modem", {"ip": "192.168.31.1"})
check("веб модема хоста — отсюда (без SSH)", d["ok"], d)
r, d = P("/api/browser/md/2000/stop", {})
check("закрыть браузер модемов", d["ok"] and G("/api/browser/md/2000")[1]["data"]["running"] is False, d)
r, d = P("/api/browser/md/2000/open", {"url": "https://example.com/"})
check("браузер модемов не открывает произвольные адреса", r.status == 404, (r.status, d))
print("конфиг modlink по таблице (фейк):")
SHEET = ("\tIp:port:log:pass\tReBoot\tReConnect\n"
         "Клиент\t203.0.113.13:16000:modem201:aaa1\thttp://203.0.113.13:16001/reboot\thttp://203.0.113.13:16002/reconnect\n"
         "Клиент\t203.0.113.13:16003:modem77:bbb2\thttp://203.0.113.13:16004/reboot\thttp://203.0.113.13:16005/reconnect\n")
r, d = P("/api/targets/2000/modlink/import", {"text": SHEET})
check("по умолчанию — только план (dry): что поправить, что добавить",
      d["ok"] and d["data"]["dry_run"] and [x["action"] for x in d["data"]["rows"]] == ["change", "add"], d)
r, d = P("/api/targets/2000/modlink/import", {"text": SHEET, "dry": False})
rows = {x["login"]: x for x in G("/api/targets/2000/modlink")[1]["data"]["list"]["data"]["rows"]}
check("применить: строки как в таблице", d["ok"] and d["data"]["applied"] and rows["modem201"]["port"] == 16000
      and rows["modem77"]["reconnect_port"] == 16005, (d, sorted(rows)))
for body, what in (({}, "пусто"), ({"text": "a\0b"}, "мусор"), ({"url": "ftp://x"}, "не http"), ({"text": SHEET, "map": "x"}, "map")):
    r, d = P("/api/targets/2000/modlink/import", body)
    check("конфиг по таблице: %s — отказ 400" % what, r.status == 400, (r.status, d))
print("«Связь» с ВМ (фейк):")
r, d = P("/api/servers/2000/reconnect", {})
check("«Связь»: шаги — ВМ, агент, адрес, порт SSH, вход", d["ok"] and d["data"]["ok"]
      and [x["what"] for x in d["data"]["steps"]][-1] == "вход по SSH", d)
print("ключ SSH сервера (фейк):")
r, d = P("/api/servers/2000/hostkey", {})
check("сверить: хаб знает старый, сервер показывает новый, агент подтверждает", d["ok"] and d["data"]["same"] is False
      and d["data"]["agent_ok"] is True, d)
r, d = P("/api/servers/2000/hostkey", {"accept": "agent"})
check("принять подтверждённый агентом", d["ok"] and d["data"]["accepted"] and P("/api/servers/2000/hostkey", {})[1]["data"]["same"], d)
r, d = P("/api/servers/2000/hostkey", {"accept": "что-угодно"})
check("принять: только agent или отпечаток SHA256:…", r.status == 400, d)

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
s, st, h, key, rest = ws_open("/ws/term?target=host&console=serial")
check("консоль ВМ (qm terminal) для хоста — 400: она только у ВМ", st == 400, st)
s.close()
del SERIAL[:]
s, st, h, key, rest = ws_open("/ws/term?target=2000&console=serial")
check("консоль ВМ: терминал открыт в режиме serial (qm terminal)", st == 101 and SERIAL == [True], (st, SERIAL))
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

print("обзор: сначала запас, свежий — в фоне (stale-while-revalidate):")
from pcs.web.panel import Panel  # noqa: E402


class SlowApi:
    def __init__(self):
        self.n, self.delay = 0, 0.0

    def host_info(self):
        time.sleep(self.delay)
        self.n += 1
        return {"name": "h", "n": self.n}

    def servers(self):
        time.sleep(self.delay)
        return [{"id": 1000, "name": "a"}]


sa = SlowApi()
ovp = os.path.join(tempfile.mkdtemp(), "ov.json")
pn = Panel(sa, ov_path=ovp)
pn.OV_FRESH = 0.3
d1 = pn.overview({}, {})["data"]
check("первый раз — собирается сразу и ложится на диск", d1["host"]["n"] == 1 and not d1.get("stale") and os.path.exists(ovp), d1)
d2 = pn.overview({}, {})["data"]
check("свежий — без пересборки", d2["host"]["n"] == 1 and not d2.get("stale"))
time.sleep(0.35)
sa.delay = 0.5
t0 = time.time()
d3 = pn.overview({}, {})["data"]
check("устарел — сразу прежний с stale, без ожидания опроса ВМ", d3.get("stale") and d3["host"]["n"] == 1 and time.time() - t0 < 0.2, (d3, time.time() - t0))
pn.OV_FRESH = 5.0
time.sleep(1.0)
d4 = pn.overview({}, {})["data"]
check("фон собрал свежий — следующий запрос его отдаёт", d4["host"]["n"] == 2 and not d4.get("stale"), d4)
pn2 = Panel(sa, ov_path=ovp)
sa.delay = 0.5
t0 = time.time()
d5 = pn2.overview({}, {})["data"]
check("после перезапуска панели — сразу обзор с диска (stale)", d5.get("stale") and d5["host"]["n"] == 2 and time.time() - t0 < 0.2, d5)
time.sleep(1.3)
sa.delay = 0
pn2.forget()
d6 = pn2.overview({}, {})["data"]
check("после действия (forget) — свежий сразу, не запас", not d6.get("stale") and d6["host"]["n"] >= 4, d6)

print("\nитого: ok %d, fail %d" % (passed, failed))
sys.exit(1 if failed else 0)
