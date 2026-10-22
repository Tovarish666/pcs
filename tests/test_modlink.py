#!/usr/bin/env python3
"""Проверки modlink без root и без сети: таблица и режимы портов, конфиг 3proxy, строки
для клиента, перенос из modlink-linux, HiLink и ссылки реконнекта/ребута на фальшивом модеме.

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
import urllib.parse
import urllib.request
from contextlib import redirect_stderr, redirect_stdout
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.join(HERE, os.pardir)
sys.path.insert(0, REPO)
sys.path.insert(0, HERE)
from pcs.core import threeproxy, util  # noqa: E402
from pcs.core.util import Busy, Fail  # noqa: E402
from pcs.modlink import cli, daemon, hilink, ops, store, system  # noqa: E402

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
check("порты auto — первая свободная тройка от 10000: прокси, ребут, реконнект",
      (r1["port"], r1["reboot_port"], r1["reconnect_port"]) == (10000, 10001, 10002), r1)
check("HiLink по умолчанию — .1 той же сети", r1["modem_ip"] == "192.168.201.1")
check("пароль сгенерирован: 12 знаков без похожих", len(r1["password"]) == 12
      and set(r1["password"]) <= set(store.PW_ABC))
r2 = store.add(doc, "192.168.202.100", interval_min=10)
check("вторая строка — следующая тройка", (r2["port"], r2["reboot_port"], r2["reconnect_port"]) == (10003, 10004, 10005))
r3 = store.add(doc, "192.168.201.100", login="second")
check("второй прокси на тот же модем — id от 1001", r3["id"] == 1001, r3)
r4 = store.add(doc, "10.20.30.40", modem_ip="")
check("чужая сеть: id 1002, логин proxyID, без HiLink",
      (r4["id"], r4["login"], r4["modem_ip"]) == (1002, "proxy1002", ""), r4)
check("явный порт прокси чужой строки — можно (общий порт, модем по логину)",
      store.add(dict(doc, proxies=list(doc["proxies"])), "192.168.203.100", port=10000)["port"] == 10000)
check("порт управления на порту чужого прокси — отказ",
      "и прокси" in (fails(store.add, doc, "192.168.203.100", reconnect_port=10003) or ""))
check("порт прокси строки = её же порт управления — отказ",
      fails(store.add, doc, "192.168.203.100", port=11000, reconnect_port=11000) is not None)
check("логин повторяется — отказ (у 3proxy один список пользователей)",
      "логин" in (fails(store.add, doc, "192.168.203.100", login="modem201") or ""))
check("ребут и реконнект на одном порту строки — можно (один слушатель, два пути)",
      store.add(dict(doc, proxies=list(doc["proxies"])), "192.168.209.100", port=12000, reboot_port=12001,
                reconnect_port=12001)["reconnect_port"] == 12001)
check("порт управления 0 — нет ссылки (можно)",
      store.add(dict(doc, proxies=list(doc["proxies"])), "192.168.208.100", reboot_port="0")["reboot_port"] == 0)
store.PROBE[0] = lambda p: p not in (10012, 10014)
r5 = store.add(doc, "192.168.205.100")
check("auto обходит порты, занятые на машине (тройка целиком свободна)",
      (r5["port"], r5["reboot_port"], r5["reconnect_port"]) == (10015, 10016, 10017), r5)
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
      r["port"] == 10012 and r["interval_min"] == 5 and r["password"] != r1["password"] and r["name"] == "мой", r)
r = store.set_fields(doc, 201, [("port", "auto")], mine={10000})
check("set: port=auto — свой применённый порт для строки свободен", r["port"] == 10000, r)
store.PROBE[0] = lambda p: True
check("set: id не меняется", "постоянный" in (fails(store.set_fields, doc, 201, [("id", "5")]) or ""))
check("set: неизвестное поле", "нет поля" in (fails(store.set_fields, doc, 201, [("color", "red")]) or ""))
check("set: enabled=0", store.set_fields(doc, 202, [("enabled", "0")])["enabled"] is False)
store.delete(doc, 1002)
check("удалённый id не переиспользуется", store.add(doc, "10.20.30.41")["id"] == 1003)

print("режим портов (как в modlink для Windows):")
d2 = {"next_id": 1001, "proxies": [store.norm(dict(x, port=0 + x["port"])) for x in doc["proxies"][:3]]}
store.assign_ports(d2, 15000, "triples")
pp = [(x["id"], x["port"], x["reboot_port"], x["reconnect_port"]) for x in sorted(d2["proxies"], key=lambda x: x["id"])]
check("triples: подряд по id — прокси N+3i, ребут +1, реконнект +2",
      [x[1:] for x in pp] == [(15000, 15001, 15002), (15003, 15004, 15005), (15006, 15007, 15008)], pp)
store.assign_ports(d2, 16000, "shared")
check("shared: прокси у всех 16000, ребут и реконнект у всех 16001",
      {(x["port"], x["reboot_port"], x["reconnect_port"]) for x in d2["proxies"]} == {(16000, 16001, 16001)}, d2["proxies"])
d3 = {"next_id": 1001, "proxies": [dict(x) for x in d2["proxies"]]}
d3["proxies"][1]["password"] = d3["proxies"][0]["password"]
check("общий порт управления и одинаковые пароли — отказ (ссылку не различить)",
      "пароли одинаковые" in (fails(store.check_all, d3["proxies"]) or ""))
store.PROBE[0] = lambda p: p != 16001
check("режим портов: занятый на машине порт — отказ", "16001" in (fails(store.assign_ports, d2, 16000, "shared") or ""))
check("…а свой применённый — не помеха", store.assign_ports(d2, 16000, "shared", mine={16001}) is not None)
store.PROBE[0] = lambda p: True
check("режим портов — только triples или shared", fails(store.assign_ports, d2, 16000, "pairs") is not None)
store.save(doc)
check("proxies.json — 600, каталог — 700",
      stat.S_IMODE(os.stat(store.P.proxies).st_mode) == 0o600 and stat.S_IMODE(os.stat(store.P.etc).st_mode) == 0o700)
saved = json.load(open(store.P.proxies))
check("в файле поля строго по §8", all(list(x) == list(store.FIELDS) for x in saved["proxies"]), saved["proxies"][0])
check("сохранено → прочитано то же", store.load()["proxies"] == sorted(doc["proxies"], key=lambda x: x["id"]))
check("пароль скрыт без явного запроса", store.masked(r1)["password"] is None and store.masked(r1, True)["password"])
open(store.P.proxies, "w").write("{испорчен")
check("испорченный файл — понятная ошибка, а не пустая таблица", "не читается" in (fails(store.load) or ""))

# ── конфиг 3proxy ──────────────────────────────────────────────────────────
print("конфиг 3proxy:")
rows = [store.norm(x) for x in (
    dict(id=201, enabled=True, name="m201", login="modem201", password="pw:1", port=10000, lan_ip="192.168.201.100",
         modem_ip="192.168.201.1", reboot_port=10001, reconnect_port=10002, interval_min=0),
    dict(id=202, enabled=True, name="m202", login="modem202", password="pw2", port=10003, lan_ip="192.168.202.100",
         modem_ip="192.168.202.1", reboot_port=10004, reconnect_port=10005, interval_min=5),
    dict(id=203, enabled=False, name="m203", login="modem203", password="pw3", port=10006, lan_ip="192.168.203.100",
         modem_ip="192.168.203.1", reboot_port=10007, reconnect_port=10008, interval_min=0))]
S0 = dict(store.SETTINGS)
cfg = store.config3(rows, S0)
lines3 = cfg.splitlines()


def block(text, port):
    """Правила сервиса порта: от flush до строки auto -pPORT."""
    ls = text.splitlines()
    i = next(k for k, ln in enumerate(ls) if ln.startswith("auto -p%d" % port))
    j = max(k for k in range(i) if ls[k] == "flush")
    return ls[j:i + 1]


check("сервис auto на каждый порт включённых, выключенной нет",
      [ln.split()[1] for ln in lines3 if ln.startswith("auto ")] == ["-p10000", "-p10003"], lines3)
check("свой порт — выход -e lan_ip", "auto -p10000 -e192.168.201.100" in lines3)
check("auth strong и пользователи только включённых, в кавычках",
      "auth strong" in lines3 and 'users "modem201:CL:pw:1" "modem202:CL:pw2"' in lines3, [x for x in lines3 if x.startswith("users")])
check("flush перед каждым сервисом: правила не перетекают", lines3.count("flush") == 2)
b = block(cfg, 10000)
check("у строки закрыты частные сети, CGNAT и сам сервер — до allow",
      any(x.startswith("deny modem201 * ") and "192.168.0.0/16" in x and "100.64.0.0/10" in x and "127.0.0.0/8" in x for x in b)
      and b[-2] == "allow modem201", b)
check("maxconn поднят (по умолчанию у 3proxy 100 на сервис)", "maxconn 5000" in lines3)
check("журнала соединений по умолчанию нет", not any(x.startswith("log ") for x in lines3))
check("access_log — журнал с ротацией", any(x.startswith("log ") for x in store.config3(rows, dict(S0, access_log=True)).splitlines()))
check("пустая таблица — конфиг без сервисов (годен)", "auto" not in store.config3([], S0) and "users" not in store.config3([], S0))
check("кавычки и $ в пароле — внутри кавычек, экранированы", '"x:CL:p\\"w$1"' in store.config3(
    [store.norm(dict(rows[0], login="x", password='p"w$1'))], S0))

print("защита строки (protect):")
check("по умолчанию защита включена (и у старых строк без поля)", all(r["protect"] for r in rows))
dom = next((x for x in b if "mts.ru" in x), "")
check("ЛК операторов закрыты — по имени и поддоменам, только TCP (UDP 3proxy по имени рубит весь)",
      dom.startswith("deny modem201 * ") and "*.mts.ru" in dom and ",mts.ru," in dom and "megafon.ru" in dom
      and dom.endswith(" * CONNECT,HTTP,HTTPS"), dom)
prt = next((x for x in b if x.startswith("deny modem201 * * ")), "")
check("опасные порты закрыты (25, 445, торренты)", ",25," in prt and "445" in prt and "6881-6889" in prt, prt)
open3 = store.config3([dict(rows[0], protect=False), rows[1]], S0)
bo = block(open3, 10000)
check("без защиты: ни ЛК, ни портов для этой строки", not any("mts.ru" in x or x.startswith("deny modem201 * * ") for x in bo), bo)
check("без защиты: веб своего модема открыт — раньше запрета частных",
      bo.index("allow modem201 * 192.168.201.1") < next(k for k, x in enumerate(bo) if x.startswith("deny modem201 * 0.0.0.0")), bo)
check("без защиты: частные и CGNAT всё равно закрыты", any("100.64.0.0/10" in x for x in bo))
check("protect принимает on/off/да/нет", store.norm(dict(rows[0], protect="off"))["protect"] is False
      and store.norm(dict(rows[0], protect="да"))["protect"] is True)
check("protect: мусор — понятная ошибка", "protect" in (fails(store.norm, dict(rows[0], protect="может")) or ""))
gx = os.path.join(store.P.etc, "protect.json")
check("protect.json нет — дополнений нет", store.guard_extra(gx) == {"domains": [], "cidrs": [], "ports": []})
os.makedirs(store.P.etc, exist_ok=True)
json.dump({"domains": [".LK.Example.RU"], "cidrs": ["10.200.0.0/16"], "ports": ["8291"]}, open(gx, "w"))
ext = store.guard_extra(gx)
check("protect.json: домены, сети и порты читаются", ext == {"domains": ["lk.example.ru"], "cidrs": ["10.200.0.0/16"],
                                                               "ports": [8291]}, ext)
bx = block(store.config3(rows, S0, ext), 10000)
check("дополнения попадают в правила", any("*.lk.example.ru" in x for x in bx) and "deny modem201 * 10.200.0.0/16" in bx
      and any(x.startswith("deny modem201 * * ") and "8291" in x for x in bx), bx)
json.dump({"domains": ["не домен"]}, open(gx, "w"))
check("protect.json с мусором — Fail", "не годится" in (fails(store.guard_extra, gx) or ""))
os.unlink(gx)

print("трафик и общий порт:")
check("tcp+udp и задан WAN IP — 3proxy сообщает его клиенту для UDP (-Ni)",
      "auto -p10000 -e192.168.201.100 -Ni203.0.113.5" in store.config3(rows, dict(S0, wan_ip="203.0.113.5")).splitlines())
tcp = store.config3(rows, dict(S0, traffic="tcp", wan_ip="203.0.113.5"))
check("только TCP — UDP ASSOCIATE запрещён, -Ni не нужен",
      "deny modem201 * * * UDPASSOC" in block(tcp, 10000) and "-Ni" not in tcp)
sh_rows = [store.norm(dict(r, port=16000, reboot_port=16001, reconnect_port=16001)) for r in rows[:2]]
sb = block(store.config3(sh_rows, S0), 16000)
check("общий порт: один сервис без -e, у каждого логина свой выход (parent extip)",
      sb[-1] == "auto -p16000" and sb.index("allow modem201") + 1 == sb.index("parent 1000 extip 192.168.201.100 0")
      and "parent 1000 extip 192.168.202.100 0" in sb, sb)
check("общий порт: запреты — у каждого логина свои и до его allow",
      sb.index("allow modem202") > max(k for k, x in enumerate(sb) if x.startswith("deny modem202")), sb)

# ── экспорт ────────────────────────────────────────────────────────────────
print("экспорт:")
lines = store.export_lines(rows, "203.0.113.5")
check("всё по колонкам: прокси, ребут, реконнект (табуляция — в таблицу по ячейкам)",
      lines[0] == "203.0.113.5:10000:modem201:pw:1\thttp://203.0.113.5:10001/reboot\thttp://203.0.113.5:10002/reconnect", lines)
check("выключенные не экспортируются", len(lines) == 2)
ex = {w: store.export(rows, "203.0.113.5", w) for w in ("proxy", "reboot", "reconnect", "all")}
check("копировать: только прокси / только ребут / только реконнект — по одной колонке",
      ex["proxy"]["rows"][1] == ["203.0.113.5:10003:modem202:pw2"] and ex["reboot"]["rows"][0] == ["http://203.0.113.5:10001/reboot"]
      and ex["reconnect"]["rows"][0] == ["http://203.0.113.5:10002/reconnect"] and ex["all"]["columns"] == ["Ip:port:log:pass", "Reboot", "ReConnect"])
shx = store.export(sh_rows + [store.norm(dict(rows[1], id=209, login="m209", password="a/b?c", port=16000,
                                                    reboot_port=16001, reconnect_port=16001))], "203.0.113.5")["rows"]
check("общий порт управления — пароль строки в пути ссылки (с кодированием)",
      shx[0][2] == "http://203.0.113.5:16001/reconnect/pw%3A1" and shx[2][1] == "http://203.0.113.5:16001/reboot/a%2Fb%3Fc", shx)
check("порта ребута нет — ячейка пустая", store.export([dict(rows[0], reboot_port=0)], "1.2.3.4")["rows"][0][1] == "")
check("что копировать — только известное", fails(store.export, rows, "1.2.3.4", "csv") is not None)
from pcs.core import table as ptable  # noqa: E402
feed = [dict(rows[0], protect=False, password="p,w:1"), rows[1], rows[2],
        store.norm(dict(rows[0], id=1001, lan_ip="10.0.0.5", port=10010, reconnect_port=10011, protect=False))]
tl, tw = store.table_lines(feed, "192.168.88.254")
good, _, bad, probs = ptable.lint("\n".join(tl) + "\n")
check("export --table: строки таблицы proxyveth, proxyveth их принимает (и пароль с «,» и «:»)",
      tl[0] == "n,real,proxy,enabled" and sorted(good) == [201, 202] and not bad
      and good[201]["host"] == "192.168.88.254" and good[201]["pw"] == "p,w:1" and good[201]["real"] == 201, (tl, probs))
check("export --table: предупреждения — защита у 202, не 192.168.N.x у 1001",
      any("строка 202" in w and "protect 202 off" in w for w in tw) and any("строка 1001" in w for w in tw)
      and not any("строка 201" in w for w in tw), tw)

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

# ── ссылки реконнекта и ребута в демоне ───────────────────────────────────
print("демон: ссылки реконнекта и ребута:")
fresh_root()
rport, busy_port, sport, bport = free_tcp(), free_tcp(), free_tcp(), free_tcp()
arow = dict(id=201, enabled=True, name="modem 201", login="modem201", password="pw", port=free_tcp(),
            lan_ip="127.0.0.1", modem_ip="127.0.0.1", reconnect_port=rport, reboot_port=bport, interval_min=5)
brow = dict(arow, id=202, name="modem 202", login="modem202", port=free_tcp(), reconnect_port=busy_port, reboot_port=0)
crow = dict(arow, id=203, name="modem 203", login="modem203", password="c/3", port=free_tcp(), reconnect_port=sport,
            reboot_port=sport, interval_min=0)
drow = dict(crow, id=204, name="modem 204", login="modem204", password="d4")
util.jsave(store.P.applied, {"proxies": [arow, brow, crow, drow]})
blocker = socket.socket()
blocker.bind(("127.0.0.1", busy_port))
blocker.listen(1)
d = daemon.Daemon(host="127.0.0.1", log=lambda msg: None)
d.next_wan = time.time() + 10 ** 6             # проверки внешнего IP здесь не нужны
th = threading.Thread(target=d.run, kwargs={"signals": False}, daemon=True)
th.start()
for _ in range(50):
    if os.path.exists(store.P.state):
        break
    time.sleep(0.05)


def get(path, port=rport, timeout=10, headers=None, raw=False):
    req = urllib.request.Request("http://127.0.0.1:%d%s" % (port, path), headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = r.read()
            return r.status, (body.decode() if raw else json.loads(body))
    except urllib.error.HTTPError as e:
        body = e.read() or b"{}"
        return e.code, (body.decode() if raw else json.loads(body))


m.calls.clear()
code, body = get("/reconnect")
check("GET /reconnect → 200 {ok, ip}", (code, body) == (200, {"ok": True, "ip": "203.0.113.7"}), (code, body))
check("модему ушёл реконнект", ("data", 0) in m.calls and m.calls[-1] == ("data", 1), m.calls)
last = ops.last(201)
check("журнал строки: link, ok, IP", last and last["how"] == "link" and last["ok"] and last["text"] == "203.0.113.7", last)
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
check("POST — 405, реконнекта нет", code == 405 and ops.last(201)["how"] == "link")
code, page_ = get("/reconnect", headers={"Accept": "text/html,application/xhtml+xml"}, raw=True)
check("браузеру — страница «было → стало» (а не JSON)", code == 200 and page_.startswith("<!doctype html>")
      and "203.0.113.7" in page_ and "Реконнект · modem 201" in page_, page_[:200])
code, body = get("/reconnect?json=1", headers={"Accept": "text/html"})
check("?json — JSON и браузеру", code == 200 and body["ok"] is True, body)
m.calls.clear()
code, body = get("/reboot", port=bport)
check("GET /reboot на порту ребута → модему ушла перезагрузка", code == 200 and body == {"ok": True}
      and m.calls == [("control", "1")], (code, body, m.calls))
code, body = get("/reboot")
check("ребута на порту реконнекта нет — 404", code == 404, (code, body))
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

print("общий порт управления:")
code, body = get("/reconnect", port=sport)
check("без пароля — 401 и как надо", code == 401 and "/reconnect/<пароль>" in body["error"], (code, body))
code, body = get("/reconnect/" + urllib.parse.quote("c/3", safe=""), port=sport)
check("/reconnect/<пароль строки> — реконнект именно её", code == 200 and ops.last(203)["ok"] and ops.last(204) is None, (code, body))
code, body = get("/reconnect/modem204", port=sport)
check("логин в пути тоже узнаётся", code == 200 and ops.last(204)["ok"], (code, body))
code, body = get("/reconnect/" + urllib.parse.quote("чужой"), port=sport)
check("не тот пароль — 403", code == 403, (code, body))
auth = {"Authorization": "Basic " + base64.b64encode(b"modem203:c/3").decode()}
code, body = get("/reboot", port=sport, headers=auth)
check("HTTP Basic (логин:пароль) — тоже можно", code == 200, (code, body))
code, body = get("/reboot", port=sport, headers={"Authorization": "Basic " + base64.b64encode(b"modem203:x").decode()})
check("Basic с неверным паролем — 403", code == 403, (code, body))

st = json.load(open(store.P.state))
check("состояние: порт реконнекта 201 поднят, у 202 — занят",
      st["triggers"][str(rport)]["ok"] and not st["triggers"][str(busy_port)]["ok"] and st["triggers"][str(busy_port)]["error"], st)
check("общий порт управления — один слушатель на две строки", sport in d.ctl and len(d.ctl) == 4, list(d.ctl))
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
check("таймер строки, чей порт реконнекта занят чужим, молчит", ops.last(202) is None)
blocker.close()
with d.lock:
    d.ctl[busy_port].tried = 0
d.retry()
check("порт освободился — слушатель поднят повтором", d.ctl[busy_port].srv is not None)
util.jsave(store.P.applied, {"proxies": [dict(arow, reconnect_port=busy_port, reboot_port=0, interval_min=0)]})
d.reload_ev.set()
for _ in range(100):
    if list(d.ctl) == [busy_port] and not d.next:
        break
    time.sleep(0.05)
time.sleep(0.6)                                  # старые слушатели закрываются вне блокировки
check("SIGHUP: перечитал — строки ушли, порты закрыты, таймер снят",
      list(d.ctl) == [busy_port] and not d.next, (list(d.ctl), d.next))
try:
    socket.create_connection(("127.0.0.1", rport), timeout=1).close()
    check("старый порт реконнекта закрыт", False)
except OSError:
    check("старый порт реконнекта закрыт", True)

print("проверки (внешний IP раз в час):")
ipx = {"n": 0}
real_ext = hilink.ext_ip
hilink.ext_ip = lambda **k: "198.51.100.%d" % (ipx.update(n=ipx["n"] + 1) or ipx["n"]) if k.get("proxy") else real_ext(**k)
try:
    d._checks(True, False)
finally:
    hilink.ext_ip = real_ext
chk = json.load(open(store.P.checks))
check("внешний IP через прокси строки — в checks.json", chk["201"]["wan_ip"] == "198.51.100.1" and chk["201"]["wan_t"], chk)
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


SVCST = {}                                         # is-active по юнитам; нет в словаре — active


def fake_sh(*c, **k):
    ran.append(c)
    out = SVCST.get(c[2], "active") + "\n" if c[:2] == ("systemctl", "is-active") else ""
    return types.SimpleNamespace(stdout=out, stderr="", returncode=0)


real = (system.sh, system.net.ensure_base, system.threeproxy.install, system.threeproxy.installed,
        system.time.sleep, system.listening)
UP = {"ports": set(range(1, 65536))}               # что «слушает» на машине
system.sh = fake_sh
system.net.ensure_base = lambda log=None: None
system.threeproxy.install = lambda path=None, a=None: False
system.threeproxy.installed = lambda path=None: True
system.time.sleep = lambda s: None
system.listening = lambda: UP["ports"]
sbb = None
try:
    res = system.migrate(lambda m: None, dry_run=True, binary=sbb)
    check("--dry-run: строки показаны, ничего не записано и не запущено",
          len(res["rows"]) == 4 and not os.path.exists(store.P.proxies) and not ran and res["rows"][0]["password"] is None)
    msg = fails(system.setup, lambda m: None, binary=sbb)
    check("setup без migrate: старый modlink.service не тронут",
          msg is None and open(os.path.join(store.P.units, "modlink.service")).read() == legacy_unit
          and ("systemctl", "disable", "--now", "modlink.service") not in ran, (msg, ran))
    store.save({"next_id": 1001, "proxies": [store.norm(dict(r, password="new%d" % r["id"])) for r in rows[:2]]})
    msg = fails(system.apply, lambda m: None, binary=sbb)
    check("apply при старом modlink-linux — отказ с подсказкой про migrate, ничего не запущено",
          msg and "modlink migrate --dry-run" in msg and "--force --stop-panel" in msg
          and json.load(open(store.P.singbox)) == old_cfg and not any("restart" in c or "reload" in c for c in ran), (msg, ran))
    os.unlink(store.P.proxies)
    ran.clear()
    if True:
        res = system.migrate(lambda m: None, binary=sbb)
        doc = store.load()
        check("migrate — явное распоряжение: modlink включён", store.is_on())
        check("migrate: таблица перенесена, порты и пароли те же",
              [(r["port"], r["password"]) for r in doc["proxies"]][:2] == [(20000, "secret81"), (20002, "fromrunning")])
        check("migrate: старый sing-box остановлен, его юнит — в legacy/",
              ("systemctl", "disable", "--now", "modlink.service") in ran
              and open(os.path.join(store.P.legacy, "modlink.service")).read() == legacy_unit
              and system.owner("modlink.service") == "ours")
        check("migrate: старый singbox.json сохранён в legacy/",
              json.load(open(os.path.join(store.P.legacy, "singbox.json"))) == old_cfg)
        newcfg = open(store.P.cfg3).read().splitlines()
        check("migrate: конфиг 3proxy — порты как были (20000, 20002, …), выход с адреса модема",
              [x.split()[1] for x in newcfg if x.startswith("auto ")] == ["-p20000", "-p20002", "-p20004", "-p20006"]
              and "auto -p20000 -e192.168.81.100 -Ni198.51.100.20" in newcfg, newcfg)
        check("migrate: ссылки реконнекта как были (порт+1), ребута не было — нет",
              [(r["reconnect_port"], r["reboot_port"]) for r in store.load()["proxies"]][:2] == [(20001, 0), (20003, 0)])
        check("migrate: старая панель без флага не остановлена (и об этом сказано)",
              not any("modlink-panel.service" in c and "disable" in c for c in ran)
              and any("modlink-panel" in w for w in res["warnings"]), res["warnings"])
        check("migrate: /usr/local/bin/sing-box не тронут — его нет ни в одной команде",
              not any("/usr/local/bin/sing-box" in " ".join(c) for c in ran))
        check("ext_ip старой панели сохранён для export", system.ext_ip_saved() == "198.51.100.20")
        check("базовый порт старой панели — в настройках", store.settings()["base_port"] == 20000, store.settings())
        ran.clear()
        res = system.migrate(lambda m: None, stop_panel=True, binary=sbb)
        check("migrate --stop-panel: повтор безопасен, панель выключена",
              ("systemctl", "disable", "--now", "modlink-panel.service") in ran and len(store.load()["proxies"]) == 4)
        store.save(dict(store.load(), proxies=store.load()["proxies"][:1]))
        check("таблица уже другая — migrate без --force отказывает",
              "--force" in (fails(system.migrate, lambda m: None, binary=sbb) or ""))
        pv = system.migrate(lambda m: None, dry_run=True, binary=sbb)
        check("--dry-run при непустой таблице — всё равно показывает, что перенесётся, и предупреждает",
              len(pv["rows"]) == 4 and pv["replaces"] == 1 and any("заменит" in w for w in pv["warnings"])
              and len(store.load()["proxies"]) == 1, pv)
        said = []
        system.migrate(said.append, force=True, binary=sbb)
        dup = [m for m in said if m.startswith("⚠") and said.count(m) > 1]
        check("migrate: предупреждения не печатаются дважды", not dup, dup)
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
          "10002" in (fails(system.apply, lambda m: None, binary=sbb) or "") and not os.path.exists(store.P.cfg3))
    store.PROBE[0] = lambda p: True
    check("по умолчанию modlink выключен", not store.is_on())
    ran.clear()
    res = system.apply(lambda m: None, binary=sbb)
    check("apply у выключенного: таблица применена, но 3proxy и демон не запущены и не перечитаны",
          res["on"] is False and len(system.applied_rows()) == 2 and os.path.exists(store.P.cfg3)
          and not any(c[1:2] in (("restart",), ("reload",), ("enable",), ("reload-or-restart",)) for c in ran), ran)
    store.set_on(True)
    if True:
        ran.clear()
        res = system.apply(lambda m: None, binary=sbb)
        check("apply: конфиг записан (600), применённое сохранено, 3proxy перечитал конфиг (reload — соединения живут)",
              stat.S_IMODE(os.stat(store.P.cfg3).st_mode) == 0o600
              and len(system.applied_rows()) == 2 and ("systemctl", "reload", "modlink-3proxy.service") in ran
              and ("systemctl", "restart", "modlink-3proxy.service") not in ran, ran)
        check("apply: нет своего демона — честное предупреждение", any("modlink setup" in w for w in res["warnings"]))
        check("после apply правок нет", not system.pending(store.load()["proxies"]))
        doc = store.load()
        store.set_fields(doc, 201, [("enabled", "0")])
        store.set_fields(doc, 202, [("enabled", "0")])
        store.save(doc)
        check("правка без apply — видна как неприменённая", system.pending(store.load()["proxies"]))
        ran.clear()
        system.apply(lambda m: None, binary=sbb)
        check("все строки выключены — 3proxy остановлен", ("systemctl", "disable", "--now", "modlink-3proxy.service") in ran)

        print("откат и переход с sing-box:")
        doc = store.load()
        store.set_fields(doc, 201, [("enabled", "1")])
        store.save(doc)
        system.apply(lambda m: None, binary=sbb)
        good = open(store.P.cfg3).read()
        doc = store.load()
        store.set_fields(doc, 202, [("enabled", "1")])
        store.save(doc)
        UP["ports"] = {10000}                       # новый порт 10003 так и не поднялся
        ran.clear()
        msg = fails(system.apply, lambda m: None, binary=sbb)
        check("3proxy не поднял порт — отказ, прежний конфиг вернулся и перезапущен",
              msg and "10003" in msg and "прежний конфиг" in msg and open(store.P.cfg3).read() == good
              and ("systemctl", "restart", "modlink-3proxy.service") in ran, (msg, ran))
        UP["ports"] = set(range(1, 65536))
        os.makedirs(store.P.units, exist_ok=True)
        open(os.path.join(store.P.units, "modlink-sb.service"), "w").write(system.MARK + "\n[Service]\nExecStart=x\n")
        ran.clear()
        system.setup(lambda m: None, binary=sbb)
        check("setup: прежний sing-box modlink (modlink-sb) остановлен и снят, 3proxy-юнит на месте",
              ("systemctl", "disable", "--now", "modlink-sb.service") in ran
              and not os.path.exists(os.path.join(store.P.units, "modlink-sb.service"))
              and system.owner("modlink-3proxy.service") == "ours", ran)

        print("выключатель: modlink on / off:")
        ran.clear()
        res = system.turn_off(lambda m: None)
        check("off: 3proxy и демон остановлены и выключены, флаг снят",
              not store.is_on() and res["on"] is False and ("systemctl", "disable", "--now", "modlink-3proxy.service") in ran
              and ("systemctl", "disable", "--now", "modlink.service") in ran, ran)
        ran.clear()
        res = system.setup(lambda m: None, binary=sbb)
        check("setup у выключенного (как после pcs update): юниты на месте, но ничего не запущено",
              res["on"] is False and system.owner("modlink.service") == "ours"
              and ("systemctl", "disable", "--now", "modlink.service") in ran
              and not any(c[1:2] in (("restart",), ("enable",), ("reload",)) for c in ran), ran)
        SVCST.update({"modlink-3proxy.service": "inactive", "modlink.service": "inactive"})
        ran.clear()
        res = system.turn_on(lambda m: None, binary=sbb)
        SVCST.clear()
        check("on: флаг стоит, демон включён и запущен, 3proxy запущен по применённой таблице",
              store.is_on() and res["on"] is True and ("systemctl", "enable", "modlink.service") in ran
              and ("systemctl", "restart", "modlink.service") in ran
              and any(c[:2] == ("systemctl", "enable") and "modlink-3proxy.service" in c for c in ran), ran)
        store.set_on(False)
        rc, out, err = run_cli("daemon")
        check("демон у выключенного сразу выходит с 0 (юнит его не перезапускает)", rc == 0 and "выключен" in out
              and "Restart=on-failure" in system.unit_daemon(), (rc, out, err))
        store.set_on(True)
finally:
    (system.sh, system.net.ensure_base, system.threeproxy.install, system.threeproxy.installed,
     system.time.sleep, system.listening) = real

print("порты modlink — не исходящим соединениям (ip_local_reserved_ports):")
check("диапазоны: туда и обратно", system.ranges({10000, 10001, 10002, 10005}) == "10000-10002,10005"
      and system.parse_ranges("10000-10002,10005\n") == {10000, 10001, 10002, 10005})
fresh_root()
rp = store.P.root + system.RESERVED
os.makedirs(os.path.dirname(rp))
open(rp, "w").write("8080\n")
check("резерв: чужой порт остаётся, свои добавлены, файл sysctl для загрузки",
      system.reserve_ports({10000, 10001, 10002}) and open(rp).read().strip() == "8080,10000-10002"
      and "ip_local_reserved_ports = 8080,10000-10002" in open(store.P.root + system.RESERVED_CONF).read())
system.reserve_ports({20000})
check("порты сменились — прежние свои сняты", open(rp).read().strip() == "8080,20000")
system.reserve_ports(set())
check("строк нет — свои сняты, чужой на месте", open(rp).read().strip() == "8080")
check("нет /proc (не Linux) — молча ничего", (os.unlink(rp) or True) and system.reserve_ports({1}) is False)

print("юниты:")
u = system.unit_proxy()
check("modlink-3proxy: свой 3proxy из /usr/local/lib/pcs со своим конфигом, reload — SIGUSR1",
      "ExecStart=%s %s" % (threeproxy.BIN, store.P.cfg3) in u and "kill -USR1" in u and system.MARK in u
      and "LimitNOFILE=1048576" in u, u)
check("modlink.service: демон этой же копии кода", " daemon\n" in system.unit_daemon()
      and os.path.join(system.ROOT, "bin", "modlink") in system.unit_daemon())

# ── команда ────────────────────────────────────────────────────────────────
print("конфиг по таблице (обратное «Копировать»):")
SHEET = ("\tIp:port:log:pass\tReBoot\tReConnect\n"
         "\t\t\t\n"
         "Иванова Анна Александровна\t203.0.113.13:16000:modem201:6av42s3nyg\thttp://203.0.113.13:16001/reboot\t"
         "http://203.0.113.13:16002/reconnect\n"
         "Иванова Анна Александровна\t203.0.113.13:16003:modem202:aecjeptr7e\thttp://203.0.113.13:16004/reboot\t"
         "http://203.0.113.13:16005/reconnect\n"
         "Петров Владимир\t203.0.113.13:16006:modem7:egw3vd2qgb\thttp://203.0.113.13:16007/reboot\t"
         "http://203.0.113.13:16008/reconnect\n")
specs, w = store.parse_sheet(SHEET)
check("лист как у клиента: шапка, пустые, имена — мимо; прокси, ребут, реконнект — из ячеек",
      [(x["login"], x["port"], x["reboot_port"], x["reconnect_port"]) for x in specs]
      == [("modem201", 16000, 16001, 16002), ("modem202", 16003, 16004, 16005), ("modem7", 16006, 16007, 16008)]
      and specs[0]["password"] == "6av42s3nyg" and specs[0]["ip"] == "203.0.113.13" and not w, (specs, w))
two = store.parse_sheet("203.0.113.13:16000:modem1:pw1\thttp://203.0.113.13:16001/reconnect\n")[0][0]
check("вариант без ReBoot: ребута нет, реконнект есть", two["reboot_port"] == 0 and two["reconnect_port"] == 16001, two)
csvx = store.parse_sheet('"203.0.113.13:16000:modem1:p,w1",http://203.0.113.13:16001/reboot/p%2Cw1\n'
                         "203.0.113.13:16000:modem2:pw2 http://203.0.113.13:16001/reconnect/pw2\n")
check("CSV из ссылки (пароль с запятой — в кавычках) и строка через пробелы; общий порт с паролем в ссылке",
      [(x["password"], x["reboot_port"], x["reconnect_port"]) for x in csvx[0]] == [("p,w1", 16001, 0), ("pw2", 0, 16001)]
      and not csvx[1], csvx)
check("ссылка без прокси — предупреждение, не молча",
      any("строка 2" in m for m in store.parse_sheet("203.0.113.13:1:a:b\nhttp://203.0.113.13:2/reboot\n")[1]))
check("пусто, кривой порт, повтор логина — понятный отказ",
      "ни одной строки" in fails(store.parse_sheet, "Ip:port:log:pass\n")
      and "строка 1" in fails(store.parse_sheet, "203.0.113.13:99999:m:p\n")
      and "логин m" in fails(store.parse_sheet, "1.2.3.4:1:m:p\n1.2.3.4:4:m:q\n"))
cur = {"next_id": 1001, "proxies": [
    store.norm({"id": 201, "name": "modem 201", "login": "modem201", "password": "old", "port": 10000, "lan_ip": "192.168.201.100",
                "modem_ip": "192.168.201.1", "reconnect_port": 10002, "reboot_port": 10001, "interval_min": 30}),
    store.norm({"id": 202, "name": "modem 202", "login": "modem202", "password": "aecjeptr7e", "port": 16003,
                "lan_ip": "192.168.202.100", "modem_ip": "192.168.202.1", "reconnect_port": 16005, "reboot_port": 16004}),
    store.norm({"id": 204, "name": "modem 204", "login": "modem204", "password": "x", "port": 10009, "lan_ip": "192.168.204.100",
                "modem_ip": "192.168.204.1", "reconnect_port": 10011, "reboot_port": 10010})]}
pl = store.import_plan(cur, specs, addrs)
got = {x["login"]: (x["action"], x["id"], x["lan_ip"], x["dev"]) for x in pl["rows"]}
check("модем — по логину modemN: 201 поправить (порты, пароль), 202 как есть, 7 — добавить (модема 7 нет)",
      got == {"modem201": ("change", 201, "192.168.201.100", "eth201"), "modem202": ("same", 202, "192.168.202.100", "eth202"),
              "modem7": ("add", 7, "192.168.7.100", None)}
      and set(pl["rows"][0]["changed"]) == {"password", "port", "reboot_port", "reconnect_port"}, pl["rows"])
r201 = store.find(pl["doc"], 201)
check("правка строки не трогает её интервал, имя и защиту", r201["interval_min"] == 30 and r201["protect"]
      and r201["port"] == 16000 and r201["password"] == "6av42s3nyg")
check("строка вне таблицы (204) остаётся; модема 7 нет — предупреждение",
      store.find(pl["doc"], 204) and not pl["removed"] and any("модема 7" in m for m in pl["warnings"]), pl["warnings"])
check("настройки: внешний адрес из таблицы, режим тройками, базовый порт — первый порт таблицы",
      pl["settings"] == {"wan_ip": "203.0.113.13", "mode": "triples", "base_port": 16000}, pl["settings"])
exp = store.export_lines([r for r in pl["doc"]["proxies"] if r["login"] in ("modem201", "modem202", "modem7")], "203.0.113.13")
check("главное: «Копировать» после импорта даёт ровно строки таблицы",
      sorted(exp) == sorted(ln.split("\t", 1)[1] for ln in SHEET.splitlines()[2:]), exp)
pl = store.import_plan(cur, specs, addrs, replace=True)
check("--replace: строки вне таблицы убираются", [r["id"] for r in pl["removed"]] == [204]
      and sorted(r["id"] for r in pl["doc"]["proxies"]) == [7, 201, 202], pl["removed"])
clash = [dict(specs[0], login="client1", port=10010, reboot_port=0, reconnect_port=0)]
msg = fails(store.import_plan, cur, clash, addrs, how="order", skip_devs=("eth0",))
check("порт строки вне таблицы занят — отказ с подсказкой «заменить всё»", msg and "--replace" in msg, msg)
odd = store.import_plan({"next_id": 1001, "proxies": []}, [dict(specs[0], login="alice"), dict(specs[1], login="bob")], addrs,
                        skip_devs=("eth0",))
check("логин не modemN — на свободные модемы машины по порядку (основной канал машины — не модем)",
      [x["lan_ip"] for x in odd["rows"]] == ["192.168.201.100", "192.168.202.100"], odd["rows"])
check("модемов не хватило — понятный отказ", "не хватило модема" in fails(
    store.import_plan, {"next_id": 1001, "proxies": []}, [dict(specs[0], login=c) for c in "abcd"], addrs[:3]))
sh_specs = store.parse_sheet("203.0.113.13:16000:modem201:a\thttp://203.0.113.13:16001/reconnect/a\n"
                             "203.0.113.13:16000:modem202:b\thttp://203.0.113.13:16001/reconnect/b\n")[0]
shp = store.import_plan({"next_id": 1001, "proxies": []}, sh_specs, addrs)
check("общий порт на всех — режим shared, ссылки с паролем строки",
      shp["settings"]["mode"] == "shared" and sorted(store.export_lines(shp["doc"]["proxies"], "203.0.113.13"))
      == ["203.0.113.13:16000:modem201:a\t\thttp://203.0.113.13:16001/reconnect/a",
          "203.0.113.13:16000:modem202:b\t\thttp://203.0.113.13:16001/reconnect/b"], shp)
fresh_root()
real_sh = util.sh
util.sh = lambda *c, **k: types.SimpleNamespace(stdout=ipout, stderr="", returncode=0) if c[:2] == ("ip", "-4") \
    else real_sh(*c, **k)
try:
    src = os.path.join(store.P.root, "sheet.tsv")
    with open(src, "w") as f:
        f.write(SHEET)
    rc, out, err = run_cli("import", src, "--dry-run", "--json")
    d = json.loads(out)["data"] if rc == 0 else {}
    check("modlink import --dry-run: план, ничего не записано", rc == 0 and len(d.get("rows", [])) == 3
          and d["dry_run"] and not os.path.exists(store.P.proxies), out + err)
    applied = []
    real_apply, real_on = system.apply, system.turn_on
    system.apply = lambda log, binary=None: applied.append("apply") or {"on": store.is_on()}
    system.turn_on = lambda log, binary=None: applied.append("on") or store.set_on(True) or {"on": True}
    try:
        rc, out, err = run_cli("import", src, "--on")
    finally:
        system.apply, system.turn_on = real_apply, real_on
    check("modlink import --on: записал, применил, включил; внешний адрес сохранён",
          rc == 0 and applied == ["apply", "on"] and len(store.load()["proxies"]) == 3
          and store.settings()["wan_ip"] == "203.0.113.13" and "добавить 3" in out, out + err)
    store.PROBE[0] = lambda p: p != 16006
    rc, out, err = run_cli("import", src)
    check("порт занят другой программой — отказ до записи", rc == 1 and "16006" in err, err)
finally:
    util.sh = real_sh
    store.PROBE[0] = lambda p: True

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
check("export печатает строки для клиента (прокси, ребут, реконнект)", rc == 0 and out.startswith("203.0.113.1:10000:modem201:")
      and "\thttp://203.0.113.1:10001/reboot\thttp://203.0.113.1:10002/reconnect" in out, out)
rc, out, err = run_cli("status", "--json")
check("status --json у выключенного: on=false, включённые строки — off", rc == 0 and json.loads(out)["data"]["on"] is False
      and json.loads(out)["data"]["rows"][0]["state"] == "off", out)
store.set_on(True)
rc, out, err = run_cli("status", "--json")
check("status --json: строки и состояния служб", rc == 0 and json.loads(out)["data"]["rows"][0]["state"] == "pending", out)
rc, out, err = run_cli("log", "201", "--json")
check("log --json", rc == 0 and json.loads(out)["data"] == {"id": 201, "lines": []}, out)
rc, out, err = run_cli("add", "--lan-ip", "192.168.202.100", "--no-protect", "--json")
check("add --no-protect — строка без защиты", rc == 0 and json.loads(out)["data"]["row"]["protect"] is False, out)
rc, out, err = run_cli("protect", "--json")
d = json.loads(out)["data"] if rc == 0 else {}
check("protect без аргументов: списки и строки без защиты", d.get("off") == [202] and "mts.ru" in d.get("domains", [])
      and 25 in d.get("ports", []), out)
rc, out, err = run_cli("protect", "all", "on", "--json")
check("protect all on — включает только тем, у кого снята", rc == 0 and json.loads(out)["data"]["changed"] == [202], out)
rc, out, err = run_cli("protect", "201", "off")
check("protect ID off — снята и предупреждение", rc == 0 and not store.find(store.load(), 201)["protect"]
      and "только для своих" in out and "modlink apply" in out, out)
rc, out, err = run_cli("set", "201", "protect=on")
check("set protect=on", rc == 0 and store.find(store.load(), 201)["protect"], err)
rc, out, err = run_cli("export", "--table", "--ip", "192.168.88.254", "--json")
d = json.loads(out)["data"] if rc == 0 else {}
check("export --table --json: строки и предупреждения", d.get("lines", [""])[0] == "n,real,proxy,enabled"
      and len(d["lines"]) == 3 and d["warnings"], out)
rc, out, err = run_cli("settings", "--json")
d = json.loads(out)["data"] if rc == 0 else {}
check("settings: по умолчанию базовый 10000, адреса — авто", d.get("base_port") == 10000 and d.get("lan_ip") == ""
      and d.get("wan_ip") == "", out)
rc, out, err = run_cli("settings", "base_port=20000", "lan_ip=192.168.88.254", "wan_ip=203.0.113.9")
rows_now = sorted(store.load()["proxies"], key=lambda r: r["id"])
check("settings base_port=20000 — порты ВСЕХ строк заново тройками: прокси, ребут, реконнект",
      rc == 0 and [(r["port"], r["reboot_port"], r["reconnect_port"]) for r in rows_now] == [(20000, 20001, 20002), (20003, 20004, 20005)]
      and "modlink apply" in out, (out, err, [(r["id"], r["port"]) for r in rows_now]))
rc, out, err = run_cli("export")
check("WAN IP настроек — в экспорте для клиентов", rc == 0 and out.startswith("203.0.113.9:20000:"), out)
rc, out, err = run_cli("export", "--table", "--json")
check("LAN IP настроек — в строках для proxyveth", rc == 0 and "192.168.88.254:20000:" in json.loads(out)["data"]["lines"][1], out)
rc, out, err = run_cli("add", "--lan-ip", "192.168.203.100", "--json")
check("новая строка — следующая тройка от базового порта", rc == 0 and json.loads(out)["data"]["row"]["port"] == 20006, out)
store.PROBE[0] = lambda p: p != 30003
rc, out, err = run_cli("settings", "base_port=30000")
check("base_port, где порт занят чужим — отказ, ничего не поменялось",
      rc == 1 and "30003" in err and store.settings()["base_port"] == 20000
      and sorted(r["port"] for r in store.load()["proxies"]) == [20000, 20003, 20006], err)
store.PROBE[0] = lambda p: True
rc, out, err = run_cli("settings", "mode=shared", "--json")
rows_now = store.load()["proxies"]
check("mode=shared — у всех один порт прокси и один порт управления",
      rc == 0 and {(r["port"], r["reboot_port"], r["reconnect_port"]) for r in rows_now} == {(20000, 20001, 20001)}
      and json.loads(out)["data"]["mode"] == "shared", out)
rc, out, err = run_cli("export", "--what", "reconnect")
check("export --what reconnect: на общем порту — ссылки с паролем строки",
      rc == 0 and all(re.match(r"http://203\.0\.113\.9:20001/reconnect/\S+$", x) for x in out.split()), out)
rc, out, err = run_cli("add", "--lan-ip", "192.168.204.100", "--json")
check("в режиме shared новая строка — на общих портах",
      rc == 0 and (json.loads(out)["data"]["row"]["port"], json.loads(out)["data"]["row"]["reconnect_port"]) == (20000, 20001), out)
rc, out, err = run_cli("settings", "mode=triples", "traffic=tcp", "access_log=on", "wan_check=off", "speedtest=on", "--json")
dd = json.loads(out)["data"] if rc == 0 else {}
check("обратно тройки; трафик, журнал, проверки — сохранены",
      rc == 0 and dd["mode"] == "triples" and dd["traffic"] == "tcp" and dd["access_log"] is True
      and dd["wan_check"] is False and dd["speedtest"] is True
      and sorted(r["port"] for r in store.load()["proxies"]) == [20000, 20003, 20006, 20009], out)
rc, out, err = run_cli("settings", "traffic=quic")
check("неизвестный трафик — отказ", rc == 1 and "traffic" in err, err)
rc, out, err = run_cli("settings", "lan_ip=auto", "wan_ip=не-адрес")
check("кривой адрес — отказ", rc == 1 and "wan_ip" in err, err)
rc, out, err = run_cli("settings", "lan_ip=auto")
check("lan_ip=auto — снова определять самим", rc == 0 and store.settings()["lan_ip"] == "", err)
rc, out, err = run_cli("protect", "201")
check("protect ID без on/off — подсказка", rc == 1 and "on|off" in err, err)
rc, out, err = run_cli("list")
check("list: колонка «защита»", rc == 0 and "защита" in out, out)
rc, out, err = run_cli()
check("без аргументов — справка", rc == 0 and "modlink list" in out and "modlink protect" in out)

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
