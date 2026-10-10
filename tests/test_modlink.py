#!/usr/bin/env python3
"""Проверки modlink без root и без сети: таблица, конфиг sing-box (настоящий check),
экспорт, перенос из modlink-linux, HiLink и триггер на фальшивом модеме.

    python3 tests/test_modlink.py
"""
import base64
import io
import json
import os
import re
import socket
import stat
import sys
import tempfile
import threading
import time
import types
import urllib.error
import urllib.request
from contextlib import redirect_stderr, redirect_stdout
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.join(HERE, os.pardir)
sys.path.insert(0, REPO)
sys.path.insert(0, HERE)
from pcs.core import singbox, util  # noqa: E402
from pcs.core.util import Busy, Fail  # noqa: E402
from pcs.modlink import cli, daemon, hilink, ops, store, system  # noqa: E402
from singbox_check import binary as sb_binary, sb_check  # noqa: E402

passed = failed = 0


def check(name, cond, detail=""):
    global passed, failed
    if cond:
        passed += 1
        print("  ok   %s" % name)
    else:
        failed += 1
        print("  FAIL %s%s" % (name, ("\n       " + str(detail)) if detail else ""))


def fails(fn, *a, **kw):
    """Текст Fail или None, если не упало."""
    try:
        fn(*a, **kw)
    except Fail as e:
        return str(e)
    return None


def fresh_root():
    root = tempfile.mkdtemp(prefix="modlink-test-")
    store.P.__init__(root)          # один объект на все модули — меняем на месте
    return root


def free_tcp():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def run_cli(*argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        rc = cli.main(list(argv))
    util.QUIET[0] = False
    return rc, out.getvalue(), err.getvalue()


store.PROBE[0] = lambda p: True         # машина тут ни при чём: все порты свободны
hilink.HiLink.pause = staticmethod(lambda s: None)

# ── таблица ────────────────────────────────────────────────────────────────
print("таблица и проверки:")
fresh_root()
doc = store.load()
check("нет файла — пустая таблица", doc["proxies"] == [])
r1 = store.add(doc, "192.168.201.100")
check("id = номер модема, логин modemN, имя «modem N»",
      (r1["id"], r1["login"], r1["name"]) == (201, "modem201", "modem 201"), r1)
check("порты auto — первая свободная пара от 10000", (r1["port"], r1["reconnect_port"]) == (10000, 10001), r1)
check("HiLink по умолчанию — .1 той же сети", r1["modem_ip"] == "192.168.201.1")
check("пароль сгенерирован: 12 знаков без похожих", len(r1["password"]) == 12
      and set(r1["password"]) <= set(store.PW_ABC))
r2 = store.add(doc, "192.168.202.100", interval_min=10)
check("вторая строка — следующая пара", (r2["port"], r2["reconnect_port"]) == (10002, 10003))
r3 = store.add(doc, "192.168.201.100", login="second")
check("второй прокси на тот же модем — id от 1001", r3["id"] == 1001, r3)
r4 = store.add(doc, "10.20.30.40", modem_ip="")
check("чужая сеть: id 1002, логин proxyID, без HiLink",
      (r4["id"], r4["login"], r4["modem_ip"]) == (1002, "proxy1002", ""), r4)
check("явный порт, занятый строкой, — отказ",
      "занят дважды" in (fails(store.add, doc, "192.168.203.100", port=10000) or ""))
check("триггер на порту чужого прокси — отказ",
      "занят дважды" in (fails(store.add, doc, "192.168.203.100", reconnect_port=10002) or ""))
check("порт и триггер строки совпадают — отказ",
      fails(store.add, doc, "192.168.203.100", port=11000, reconnect_port=11000) is not None)
store.PROBE[0] = lambda p: p not in (10006, 10008)
r5 = store.add(doc, "192.168.205.100")
check("auto обходит порты, занятые на машине", (r5["port"], r5["reconnect_port"]) == (10009, 10010), r5)
store.PROBE[0] = lambda p: True
for bad, what in ((dict(lan_ip="192.168.1"), "IPv4"), (dict(lan_ip="0.0.0.0"), "0.0.0.0"),
                  (dict(lan_ip="192.168.9.100", port="0"), "порт 0"), (dict(lan_ip="192.168.9.100", port="70000"), "порт 70000"),
                  (dict(lan_ip="192.168.9.100", port="abc"), "порт-буквы"),
                  (dict(lan_ip="192.168.9.100", login="a b"), "пробел в логине"),
                  (dict(lan_ip="192.168.9.100", login="a:b"), "двоеточие в логине"),
                  (dict(lan_ip="192.168.9.100", login="лог"), "кириллица в логине"),
                  (dict(lan_ip="192.168.9.100", password="p\tw"), "таб в пароле"),
                  (dict(lan_ip="192.168.9.100", password="p\x01w"), "управляющий в пароле"),
                  (dict(lan_ip="192.168.9.100", modem_ip="300.1.1.1"), "modem_ip"),
                  (dict(lan_ip="192.168.9.100", interval_min=-1), "интервал < 0"),
                  (dict(lan_ip="192.168.9.100", name="a\nb"), "перевод строки в имени")):
    n0 = len(doc["proxies"])
    check("отказ: %s" % what, fails(store.add, doc, **bad) is not None and len(doc["proxies"]) == n0)
check("двоеточие в пароле можно (Basic это позволяет)",
      store.norm(dict(r1, password="p:w")) ["password"] == "p:w")
store.PROBE[0] = lambda p: p != 10000
r = store.set_fields(doc, 201, [("port", "auto"), ("name", "мой"), ("interval", "5"), ("password", "gen")])
check("set: port=auto уходит с занятого, псевдоним interval, password=gen",
      r["port"] == 10008 and r["interval_min"] == 5 and r["password"] != r1["password"] and r["name"] == "мой", r)
r = store.set_fields(doc, 201, [("port", "auto")], mine={10000})
check("set: port=auto — свой применённый порт для строки свободен", r["port"] == 10000, r)
store.PROBE[0] = lambda p: True
check("set: id не меняется", "постоянный" in (fails(store.set_fields, doc, 201, [("id", "5")]) or ""))
check("set: неизвестное поле", "нет поля" in (fails(store.set_fields, doc, 201, [("color", "red")]) or ""))
check("set: enabled=0", store.set_fields(doc, 202, [("enabled", "0")])["enabled"] is False)
store.delete(doc, 1002)
check("удалённый id не переиспользуется", store.add(doc, "10.20.30.41")["id"] == 1003)
store.save(doc)
check("proxies.json — 600, каталог — 700",
      stat.S_IMODE(os.stat(store.P.proxies).st_mode) == 0o600 and stat.S_IMODE(os.stat(store.P.etc).st_mode) == 0o700)
saved = json.load(open(store.P.proxies))
check("в файле поля строго по §8", all(list(x) == list(store.FIELDS) for x in saved["proxies"]), saved["proxies"][0])
check("сохранено → прочитано то же", store.load()["proxies"] == sorted(doc["proxies"], key=lambda x: x["id"]))
check("пароль скрыт без явного запроса", store.masked(r1)["password"] is None and store.masked(r1, True)["password"])
open(store.P.proxies, "w").write("{испорчен")
check("испорченный файл — понятная ошибка, а не пустая таблица", "не читается" in (fails(store.load) or ""))

# ── конфиг sing-box ────────────────────────────────────────────────────────
print("конфиг sing-box:")
rows = [store.norm(x) for x in (
    dict(id=201, enabled=True, name="m201", login="modem201", password="pw:1", port=10000, lan_ip="192.168.201.100",
         modem_ip="192.168.201.1", reconnect_port=10001, interval_min=0),
    dict(id=202, enabled=True, name="m202", login="modem202", password="pw2", port=10002, lan_ip="192.168.202.100",
         modem_ip="192.168.202.1", reconnect_port=10003, interval_min=5),
    dict(id=203, enabled=False, name="m203", login="modem203", password="pw3", port=10004, lan_ip="192.168.203.100",
         modem_ip="192.168.203.1", reconnect_port=10005, interval_min=0))]
cfg = store.config(rows)
ins = {i["tag"]: i for i in cfg["inbounds"]}
outs = {o["tag"]: o for o in cfg["outbounds"]}
check("inbound на каждую включённую строку, выключенной нет", sorted(ins) == ["in-201", "in-202"], sorted(ins))
check("mixed 0.0.0.0:port с users", ins["in-201"]["type"] == "mixed" and ins["in-201"]["listen"] == "0.0.0.0"
      and ins["in-201"]["listen_port"] == 10000
      and ins["in-201"]["users"] == [{"username": "modem201", "password": "pw:1"}])
check("direct с inet4_bind_address = lan_ip", outs["out-202"] == {"type": "direct", "tag": "out-202",
                                                                   "inet4_bind_address": "192.168.202.100"})
rules = cfg["route"]["rules"]
check("правило inbound → свой outbound", {"inbound": ["in-201"], "action": "route", "outbound": "out-201"} in rules)
check("остальное — reject последним правилом", rules[-1] == {"action": "reject"})
check("без служебных outbound block/dns", all(o["type"] == "direct" for o in cfg["outbounds"]))
check("частные адреса и IPv6 закрыты до правил строк",
      rules.index({"ip_is_private": True, "action": "reject"}) < rules.index({"inbound": ["in-201"], "action": "route", "outbound": "out-201"})
      and {"ip_cidr": ["::/0"], "action": "reject"} in rules)
txt = json.dumps(cfg)
check("ни sniff, ни domain_strategy, ни address-DNS", not re.search(r"sniff|domain_strategy|\"address\"", txt))
ok, msg = sb_check(cfg)
if ok is not None:
    check("sing-box %s check: конфиг принят" % singbox.VERSION, ok, msg)
    ok, msg = sb_check(store.config([]))
    check("sing-box check: пустая таблица тоже годна", ok, msg)
    ok, msg = sb_check(store.config(rows[2:]))
    check("sing-box check: только выключенные строки", ok, msg)

# ── экспорт ────────────────────────────────────────────────────────────────
print("экспорт:")
lines = store.export_lines(rows, "203.0.113.5")
check("формат IP:PORT:LOGIN:PASS<TAB>http://IP:RPORT/reconnect",
      lines[0] == "203.0.113.5:10000:modem201:pw:1\thttp://203.0.113.5:10001/reconnect", lines)
check("выключенные не экспортируются", len(lines) == 2)

# ── интерфейсы ─────────────────────────────────────────────────────────────
print("from-ifaces:")
ipout = ("1: lo    inet 127.0.0.1/8 scope host lo\\       valid_lft forever preferred_lft forever\n"
         "2: eth0    inet 192.168.88.100/24 brd 192.168.88.255 scope global eth0\\       valid_lft forever\n"
         "7: eth201    inet 192.168.201.100/24 brd 192.168.201.255 scope global eth201\\  valid_lft forever\n"
         "8: eth202    inet 192.168.202.100/24 brd 192.168.202.255 scope global eth202\\  valid_lft forever\n"
         "9: pv204@if2    inet 192.168.204.100/24 brd 192.168.204.255 scope global pv204\\  valid_lft forever\n"
         "10: eth9    inet 192.168.9.5/24 brd 192.168.9.255 scope global eth9\\  valid_lft forever\n")
addrs = store.parse_addrs(ipout)
check("разбор ip -4 -o addr (и veth с @ifN)", ("pv204", "192.168.204.100", 24) in addrs and len(addrs) == 6, addrs)
sug = store.suggest(addrs, rows[:1], skip_devs=("eth0",))
check("предложены .100 без уже занятых и без основного канала",
      [s["lan_ip"] for s in sug] == ["192.168.202.100", "192.168.204.100"], sug)
check("заготовка: modem_ip …1, имя «modem N»", sug[0]["modem_ip"] == "192.168.202.1" and sug[0]["name"] == "modem 202")

# ── HiLink на фальшивом модеме ─────────────────────────────────────────────
print("HiLink (фальшивый модем):")


class Modem:
    def __init__(self):
        self.calls, self.issued, self.used = [], set(), set()
        self.data, self.mode, self.error, self.gate, self.n = 1, "03", None, None, 0
        self.mode_posts, self.fail_modes = 0, set()
        modem = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def send(self, body, code=200, ctype="text/xml"):
                b = body.encode()
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(b)))
                self.end_headers()
                self.wfile.write(b)

            def do_GET(self):
                p = self.path
                if p == "/ip":
                    return self.send("203.0.113.7\n", ctype="text/plain")
                if p == "/api/webserver/SesTokInfo":
                    modem.n += 1
                    tok = "tok%d" % modem.n
                    modem.issued.add(tok)
                    return self.send("<response><SesInfo>SessionID=s%d</SesInfo><TokInfo>%s</TokInfo></response>"
                                     % (modem.n, tok))
                if "SessionID=" not in (self.headers.get("Cookie") or ""):
                    return self.send("<error><code>125002</code><message></message></error>")
                if p == "/api/net/net-mode":
                    return self.send("<response><NetworkMode>%s</NetworkMode><NetworkBand>3FFFFFFF</NetworkBand>"
                                     "<LTEBand>800C5</LTEBand></response>" % modem.mode)
                if p == "/api/monitoring/status":
                    return self.send("<response><ConnectionStatus>%s</ConnectionStatus><CurrentNetworkTypeEx>101"
                                     "</CurrentNetworkTypeEx><SignalIcon>4</SignalIcon></response>"
                                     % ("901" if modem.data else "902"))
                self.send("", 404)

            def do_POST(self):
                body = self.rfile.read(int(self.headers.get("Content-Length") or 0)).decode()
                tok = self.headers.get("__RequestVerificationToken")
                if tok not in modem.issued or tok in modem.used:
                    return self.send("<error><code>125002</code><message></message></error>")
                modem.used.add(tok)
                if modem.error:
                    return self.send("<error><code>%s</code><message></message></error>" % modem.error)
                if self.path == "/api/dialup/mobile-dataswitch":
                    v = int(re.search(r"<dataswitch>(\d)</dataswitch>", body).group(1))
                    if modem.gate:
                        modem.gate.wait(5)
                    modem.data = v
                    modem.calls.append(("data", v))
                elif self.path == "/api/net/net-mode":
                    modem.mode_posts += 1
                    if modem.mode_posts in modem.fail_modes:
                        return self.send("<error><code>112003</code><message></message></error>")
                    modem.calls.append(("mode", re.search(r"<NetworkMode>(\w+)<", body).group(1),
                                        re.search(r"<LTEBand>(\w+)<", body).group(1)))
                elif self.path == "/api/device/control":
                    modem.calls.append(("control", re.search(r"<Control>(\d)<", body).group(1)))
                else:
                    return self.send("", 404)
                self.send("<response>OK</response>")

        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.srv.daemon_threads = True
        self.port = self.srv.server_address[1]
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()


m = Modem()
ops.HILINK_PORT[0] = m.port
hilink.IP_URLS = ["http://127.0.0.1:%d/ip" % m.port]
h = hilink.HiLink("127.0.0.1", m.port, timeout=3)
check("статус: подключён", h.connected())
h.reconnect(wait=3)
check("реконнект: данные выкл → режим сети туда-обратно → данные вкл",
      m.calls == [("data", 0), ("mode", "02", "800C5"), ("mode", "03", "800C5"), ("data", 1)], m.calls)
check("каждый POST — со своим свежим токеном", len(m.used) == 4)
m.calls.clear()
m.mode_posts, m.fail_modes = 0, {2, 3, 4}
e = fails(h.reconnect, wait=3)
check("режим сети не вернулся — данные всё равно включены, и об этом сказано",
      e and "режим сети не вернулся к 03" in e and m.calls[-1] == ("data", 1) and m.data == 1, (e, m.calls))
m.fail_modes = set()
m.calls.clear()
h.reboot()
check("ребут — device/control Control=1", m.calls == [("control", "1")], m.calls)
m.error = "100003"
e = fails(h.reboot)
check("ошибка модема — по-русски и с кодом", e and "нет прав" in e and "100003" in e, e)
m.error = None
check("модема нет — понятная ошибка",
      "не отвечает" in (fails(hilink.HiLink("127.0.0.1", free_tcp(), timeout=1).status) or ""))
check("внешний IP с привязкой к адресу", hilink.ext_ip(source="127.0.0.1") == "203.0.113.7")


class FakeProxy(BaseHTTPRequestHandler):
    """CONNECT-прокси с логином u/p: туннель «отвечает» фиксированным IP."""

    def log_message(self, *a):
        pass

    def do_CONNECT(self):
        if self.headers.get("Proxy-Authorization") != "Basic " + base64.b64encode(b"u:p").decode():
            self.send_response(407)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        self.send_response(200, "Connection established")
        self.end_headers()
        while self.rfile.readline() not in (b"\r\n", b"\n", b""):
            pass
        body = b"198.51.100.9"
        self.wfile.write(b"HTTP/1.1 200 OK\r\nContent-Length: %d\r\nConnection: close\r\n\r\n%s" % (len(body), body))
        self.close_connection = True


px = ThreadingHTTPServer(("127.0.0.1", 0), FakeProxy)
threading.Thread(target=px.serve_forever, daemon=True).start()
pport = px.server_address[1]
check("внешний IP через прокси (CONNECT + логин)", hilink.ext_ip(proxy=("127.0.0.1", pport, "u", "p")) == "198.51.100.9")
check("неверный пароль прокси — так и сказано",
      "407" in (fails(hilink.ext_ip, proxy=("127.0.0.1", pport, "u", "bad")) or ""))
trow = store.norm(dict(id=7, enabled=True, name="t", login="u", password="p", port=pport, lan_ip="127.0.0.1",
                       modem_ip="127.0.0.1", reconnect_port=1, interval_min=0))
res = ops.test(trow)
check("modlink test: IP через прокси и HiLink «подключён, 4G»",
      res["proxy"] == {"ok": True, "ip": "198.51.100.9"} and res["hilink"]["status"] == "подключён"
      and res["hilink"]["net"] == "4G", res)

# ── триггер реконнекта в демоне ────────────────────────────────────────────
print("демон и триггер:")
fresh_root()
rport, busy_port = free_tcp(), free_tcp()
arow = dict(id=201, enabled=True, name="modem 201", login="modem201", password="pw", port=free_tcp(),
            lan_ip="127.0.0.1", modem_ip="127.0.0.1", reconnect_port=rport, interval_min=5)
brow = dict(arow, id=202, name="modem 202", login="modem202", port=free_tcp(), reconnect_port=busy_port)
util.jsave(store.P.applied, {"proxies": [arow, brow]})
blocker = socket.socket()
blocker.bind(("127.0.0.1", busy_port))
blocker.listen(1)
d = daemon.Daemon(host="127.0.0.1", log=lambda msg: None)
th = threading.Thread(target=d.run, kwargs={"signals": False}, daemon=True)
th.start()
for _ in range(50):
    if os.path.exists(store.P.state):
        break
    time.sleep(0.05)


def get(path, port=rport, timeout=10):
    try:
        with urllib.request.urlopen("http://127.0.0.1:%d%s" % (port, path), timeout=timeout) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


m.calls.clear()
code, body = get("/reconnect")
check("GET /reconnect → 200 {ok, ip}", (code, body) == (200, {"ok": True, "ip": "203.0.113.7"}), (code, body))
check("модему ушёл реконнект", ("data", 0) in m.calls and m.calls[-1] == ("data", 1), m.calls)
last = ops.last(201)
check("журнал строки: trigger, ok, IP", last and last["how"] == "trigger" and last["ok"] and last["text"] == "203.0.113.7", last)
code, body = get("/reconnect")
check("тот же IP второй раз — отмечено в журнале", "тот же" in ops.last(201)["text"])
code, body = get("/status")
check("другие пути — 404", code == 404 and body["ok"] is False, (code, body))
req = urllib.request.Request("http://127.0.0.1:%d/reconnect" % rport, data=b"x", method="POST")
try:
    urllib.request.urlopen(req, timeout=5)
    code = 200
except urllib.error.HTTPError as e:
    code = e.code
check("POST — 405, реконнекта нет", code == 405 and ops.last(201)["how"] == "trigger")
m.gate = threading.Event()
slow = threading.Thread(target=get, args=("/reconnect",))
slow.start()
time.sleep(0.4)
code, body = get("/reconnect")
check("пока идёт реконнект — 409 «уже идёт»", code == 409 and "уже идёт" in body["error"], (code, body))
try:
    ops.reconnect(store.norm(arow), "cli")
    check("CLI в занятый модем — Busy", False)
except Busy:
    check("CLI в занятый модем — Busy", True)
m.gate.set()
slow.join(10)
m.gate = None
m.error = "113018"
code, body = get("/reconnect")
check("модем отказал — 502 и текст ошибки", code == 502 and body["ok"] is False and "113018" in body["error"], (code, body))
check("неудача — в журнале строки", ops.last(201)["ok"] is False)
m.error = None
st = json.load(open(store.P.state))
check("состояние: триггер 201 поднят, 202 — порт занят",
      st["triggers"]["201"]["ok"] and not st["triggers"]["202"]["ok"] and st["triggers"]["202"]["error"], st)
check("таймер 201 взведён на interval_min", abs(st["next"]["201"] - (time.time() + 300)) < 30, st["next"])
m.calls.clear()
with d.lock:
    d.next[201] = 0
    d.next[202] = 0
d.tick()
for _ in range(100):
    if ops.last(201)["how"] == "timer":
        break
    time.sleep(0.05)
check("таймер: реконнект строки 201", ops.last(201)["how"] == "timer" and ("data", 1) in m.calls)
check("таймер строки с чужим портом триггера молчит", ops.last(202) is None)
blocker.close()
with d.lock:
    d.trig[202].tried = 0
d.retry()
check("порт освободился — триггер поднят повтором", d.trig[202].srv is not None)
util.jsave(store.P.applied, {"proxies": [dict(arow, reconnect_port=busy_port, interval_min=0)]})
d.reload_ev.set()
for _ in range(60):
    if 202 not in d.trig and d.trig.get(201) and d.trig[201].port == busy_port:
        break
    time.sleep(0.05)
check("SIGHUP: перечитал — строка ушла, триггер переехал, таймер снят",
      list(d.trig) == [201] and d.trig[201].port == busy_port and not d.next, (list(d.trig), d.next))
try:
    socket.create_connection(("127.0.0.1", rport), timeout=1).close()
    check("старый порт триггера закрыт", False)
except OSError:
    check("старый порт триггера закрыт", True)
d.stop_ev.set()
th.join(5)
check("демон останавливается и убирает состояние", not th.is_alive() and not os.path.exists(store.P.state))

# ── перенос из modlink-linux ───────────────────────────────────────────────
print("перенос из modlink-linux:")
root = fresh_root()
os.makedirs(store.P.etc)
old_conf = ("# modlink — список модемов\n81  secret81\n\n83   secret83   15\n82\n84 x 7m\nabc\n300 zz\n81 dup\n")
open(os.path.join(store.P.etc, "modems.conf"), "w").write(old_conf)
json.dump({"ext_ip": "198.51.100.20", "base_port": 20000}, open(os.path.join(store.P.etc, "server.json"), "w"))
old_cfg = {"inbounds": [{"type": "mixed", "tag": "in-82", "listen": "0.0.0.0", "listen_port": 20002,
                         "users": [{"username": "modem82", "password": "fromrunning"}]}],
           "outbounds": [{"type": "direct", "tag": "direct"}], "route": {"rules": [], "final": "direct"}}
json.dump(old_cfg, open(store.P.singbox, "w"))
rows, ext, warn = store.legacy(store.P.etc)
by = {r["id"]: r for r in rows}
check("модемы по N, без брака и повторов", sorted(by) == [81, 82, 83, 84], sorted(by))
check("порты base+2i и триггеры base+2i+1 в порядке N",
      [(by[n]["port"], by[n]["reconnect_port"]) for n in (81, 82, 83, 84)]
      == [(20000, 20001), (20002, 20003), (20004, 20005), (20006, 20007)])
check("пароли из modems.conf", by[81]["password"] == "secret81" and by[83]["password"] == "secret83")
check("нет пароля — взят из работающего singbox.json", by[82]["password"] == "fromrunning")
check("логин modemN, lan .100, HiLink .1, интервал", by[83]["login"] == "modem83"
      and by[83]["lan_ip"] == "192.168.83.100" and by[83]["modem_ip"] == "192.168.83.1" and by[83]["interval_min"] == 15)
check("ext_ip из server.json", ext == "198.51.100.20")
check("брак назван в предупреждениях", len([w for w in warn if "modems.conf:" in w]) == 4, warn)
check("без пароля и без работающего конфига — как придумывал modlink-server",
      store.legacy(store.P.etc, old_sb={})[0][1]["password"] == store.legacy_password(82))

legacy_unit = ("[Unit]\nDescription=modlink — sing-box proxy for modems\n[Service]\n"
               "ExecStart=/usr/local/bin/sing-box run -c /etc/modlink/singbox.json\nRestart=always\n")
os.makedirs(store.P.units)
open(os.path.join(store.P.units, "modlink.service"), "w").write(legacy_unit)
open(os.path.join(store.P.units, "modlink-panel.service"), "w").write("[Service]\nExecStart=/usr/local/bin/modlink-panel\n")
ran = []


def fake_sh(*c, **k):
    ran.append(c)
    out = "active\n" if c[:2] == ("systemctl", "is-active") else ""
    return types.SimpleNamespace(stdout=out, stderr="", returncode=0)


real = (system.sh, system.net.ensure_base, system.singbox.install, system.time.sleep)
system.sh = fake_sh
system.net.ensure_base = lambda log=None: None
system.singbox.install = lambda path=None, a=None: False
system.time.sleep = lambda s: None
sbb = sb_binary() or "/nonexistent/sing-box"
try:
    res = system.migrate(lambda m: None, dry_run=True, binary=sbb)
    check("--dry-run: строки показаны, ничего не записано и не запущено",
          len(res["rows"]) == 4 and not os.path.exists(store.P.proxies) and not ran and res["rows"][0]["password"] is None)
    msg = fails(system.setup, lambda m: None, binary=sbb)
    check("setup без migrate: старый modlink.service не тронут",
          msg is None and open(os.path.join(store.P.units, "modlink.service")).read() == legacy_unit
          and ("systemctl", "disable", "--now", "modlink.service") not in ran, (msg, ran))
    ran.clear()
    if sb_binary():
        res = system.migrate(lambda m: None, binary=sbb)
        doc = store.load()
        check("migrate: таблица перенесена, порты и пароли те же",
              [(r["port"], r["password"]) for r in doc["proxies"]][:2] == [(20000, "secret81"), (20002, "fromrunning")])
        check("migrate: старый sing-box остановлен, его юнит — в legacy/",
              ("systemctl", "disable", "--now", "modlink.service") in ran
              and open(os.path.join(store.P.legacy, "modlink.service")).read() == legacy_unit
              and system.owner("modlink.service") == "ours")
        check("migrate: старый singbox.json сохранён в legacy/",
              json.load(open(os.path.join(store.P.legacy, "singbox.json"))) == old_cfg)
        newcfg = json.load(open(store.P.singbox))
        check("migrate: новый конфиг — наш формат и порты 20000…",
              [i["listen_port"] for i in newcfg["inbounds"]] == [20000, 20002, 20004, 20006])
        check("migrate: старая панель без флага не остановлена (и об этом сказано)",
              not any("modlink-panel.service" in c and "disable" in c for c in ran)
              and any("modlink-panel" in w for w in res["warnings"]), res["warnings"])
        check("migrate: /usr/local/bin/sing-box не тронут — его нет ни в одной команде",
              not any("/usr/local/bin/sing-box" in " ".join(c) for c in ran))
        check("ext_ip старой панели сохранён для export", system.ext_ip_saved() == "198.51.100.20")
        ran.clear()
        res = system.migrate(lambda m: None, stop_panel=True, binary=sbb)
        check("migrate --stop-panel: повтор безопасен, панель выключена",
              ("systemctl", "disable", "--now", "modlink-panel.service") in ran and len(store.load()["proxies"]) == 4)
        store.save(dict(store.load(), proxies=store.load()["proxies"][:1]))
        check("таблица уже другая — migrate без --force отказывает",
              "--force" in (fails(system.migrate, lambda m: None, binary=sbb) or ""))
        ran.clear()
        out = system.uninstall(lambda m: None, purge=True)
        check("uninstall: свои юниты сняты, старый modlink-linux возвращён на место",
              open(os.path.join(store.P.units, "modlink.service")).read() == legacy_unit
              and not os.path.exists(os.path.join(store.P.units, "modlink-sb.service"))
              and json.load(open(store.P.singbox)) == old_cfg, out)
        check("uninstall --purge: чужие modems.conf и server.json на месте, своих файлов нет",
              os.path.exists(os.path.join(store.P.etc, "modems.conf")) and not os.path.exists(store.P.proxies)
              and not os.path.exists(store.P.lib))

    print("apply:")
    fresh_root()
    doc = store.load()
    store.add(doc, "192.168.201.100")
    store.add(doc, "192.168.202.100")
    store.save(doc)
    store.PROBE[0] = lambda p: p != 10002
    check("apply: порт занят кем-то другим — отказ до записи конфига",
          "10002" in (fails(system.apply, lambda m: None, binary=sbb) or "") and not os.path.exists(store.P.singbox))
    store.PROBE[0] = lambda p: True
    if sb_binary():
        ran.clear()
        res = system.apply(lambda m: None, binary=sbb)
        check("apply: конфиг записан (600), применённое сохранено, sing-box перезапущен",
              stat.S_IMODE(os.stat(store.P.singbox).st_mode) == 0o600
              and len(system.applied_rows()) == 2 and ("systemctl", "restart", "modlink-sb.service") in ran)
        check("apply: нет своего демона — честное предупреждение", any("modlink setup" in w for w in res["warnings"]))
        check("после apply правок нет", not system.pending(store.load()["proxies"]))
        doc = store.load()
        store.set_fields(doc, 201, [("enabled", "0")])
        store.set_fields(doc, 202, [("enabled", "0")])
        store.save(doc)
        check("правка без apply — видна как неприменённая", system.pending(store.load()["proxies"]))
        ran.clear()
        system.apply(lambda m: None, binary=sbb)
        check("все строки выключены — sing-box остановлен", ("systemctl", "disable", "--now", "modlink-sb.service") in ran)
finally:
    system.sh, system.net.ensure_base, system.singbox.install, system.time.sleep = real

print("юниты:")
u = system.unit_sb()
check("modlink-sb: свой sing-box из /usr/local/lib/pcs, не /usr/local/bin",
      "ExecStart=%s run -c " % singbox.BIN in u and "/usr/local/bin/sing-box" not in u and system.MARK in u)
check("modlink.service: демон этой же копии кода", " daemon\n" in system.unit_daemon()
      and os.path.join(system.ROOT, "bin", "modlink") in system.unit_daemon())

# ── команда ────────────────────────────────────────────────────────────────
print("команда modlink:")
fresh_root()
rc, out, err = run_cli("add", "--lan-ip", "192.168.201.100", "--json")
env = json.loads(out)
check("--json: конверт ok/data, пароль скрыт", rc == 0 and env["ok"] and env["data"]["row"]["password"] is None, out)
rc, out, err = run_cli("list", "--json", "--show-pass")
check("list --show-pass — пароль по явной просьбе", json.loads(out)["data"]["proxies"][0]["password"], out)
rc, out, err = run_cli("del", "201", "--json")
check("del без --yes — отказ конвертом", rc == 1 and json.loads(out) == {"ok": False, "error": "удаление строки 201 — подтверди: modlink del 201 --yes"}, out)
rc, out, err = run_cli("frobnicate")
check("неизвестная команда — одна строка ✗ в stderr, код 1", rc == 1 and err.count("\n") == 1
      and "✗" in err and "frobnicate" in err and not out, err)
rc, out, err = run_cli("set", "201", "port")
check("set без «=» — понятная ошибка", rc == 1 and "поле=значение" in err, err)
rc, out, err = run_cli("export", "--ip", "203.0.113.1")
check("export печатает строки для клиента", rc == 0 and out.startswith("203.0.113.1:10000:modem201:"), out)
rc, out, err = run_cli("status", "--json")
check("status --json: строки и состояния служб", rc == 0 and json.loads(out)["data"]["rows"][0]["state"] == "pending", out)
rc, out, err = run_cli("log", "201", "--json")
check("log --json", rc == 0 and json.loads(out)["data"] == {"id": 201, "lines": []}, out)
rc, out, err = run_cli()
check("без аргументов — справка", rc == 0 and "modlink list" in out)

print("без shell:")
src = ""
for dp, _, fs in os.walk(os.path.join(REPO, "pcs", "modlink")):
    for f in fs:
        if f.endswith(".py"):
            src += open(os.path.join(dp, f), encoding="utf-8").read()
check("нет shell=True, os.system, os.popen", not re.search(r"shell\s*=\s*True|os\.system\(|os\.popen\(", src))
check("subprocess только через util.sh (списком аргументов)", "import subprocess" not in src and "Popen" not in src)

print("\nитого: ok %d, fail %d" % (passed, failed))
sys.exit(1 if failed else 0)
