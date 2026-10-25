#!/usr/bin/env python3
"""Роутер хоста — MikroTik по REST (pcs/core/mikrotik.py, pcs/hub/router.py, PCS.md §20).
Подставной RouterOS — настоящий HTTP-сервер с базовой авторизацией поверх mikrotik.Fake.

    python3 tests/test_router.py
"""
import base64
import http.server
import json
import os
import shutil
import socket
import stat
import sys
import tempfile
import threading

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, os.pardir))
from pcs.core import mikrotik  # noqa: E402
from pcs.core.util import Fail  # noqa: E402
from pcs.hub import router, store  # noqa: E402

passed = failed = 0


def check(name, cond, detail=""):
    global passed, failed
    if cond:
        passed += 1
        print("  ok   %s" % name)
    else:
        failed += 1
        print("  FAIL %s%s" % (name, ("\n       " + str(detail)) if detail else ""))


def raises(fn, *a, **kw):
    try:
        fn(*a, **kw)
    except Fail as e:
        return str(e)
    return None


USER, PW = "pcs", "rt-pass-1"
STATE = mikrotik.demo_state()
FAKE = mikrotik.Fake(STATE)
SEEN = []


class RouterOS(http.server.BaseHTTPRequestHandler):
    """/rest/… как у RouterOS 7: JSON, 401 без пароля, 404 на неизвестное, reboot рвёт соединение."""

    def log_message(self, *a):
        pass

    def _go(self, method):
        SEEN.append((method, self.path, self.headers.get("Authorization")))
        want = "Basic " + base64.b64encode(("%s:%s" % (USER, PW)).encode()).decode()
        if self.headers.get("Authorization") != want:
            return self._out(401, {"error": 401, "message": "Unauthorized"})
        n = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(n) or b"null") if n else None
        path = self.path.split("?")[0]
        if not path.startswith("/rest/"):
            return self._out(404, {"error": 404, "message": "Not Found"})
        if STATE.get("_no_rest"):
            return self._out(404, {"error": 404, "message": "Not Found"})
        path = path[len("/rest"):]
        if method == "POST" and path == "/system/reboot":
            self.close_connection = True
            self.connection.shutdown(socket.SHUT_RDWR)
            return
        try:
            out = FAKE.req(method, path, body)
        except mikrotik.RouterFail as e:
            return self._out(400, {"error": 400, "message": "Bad Request", "detail": str(e).replace("роутер: ", "")})
        if method == "DELETE":
            self.send_response(204)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        self._out(200, out)

    def _out(self, code, obj):
        data = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        self._go("GET")

    def do_PUT(self):
        self._go("PUT")

    def do_PATCH(self):
        self._go("PATCH")

    def do_DELETE(self):
        self._go("DELETE")

    def do_POST(self):
        self._go("POST")


srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), RouterOS)
PORT = srv.server_address[1]
threading.Thread(target=srv.serve_forever, daemon=True).start()
c = mikrotik.Client("127.0.0.1", USER, PW, port=PORT, timeout=5)

print("сводка:")
st = mikrotik.status(c)
check("модель, версия, аптайм в секундах", st["board"] == "hAP ax^2" and st["version"].startswith("7.16") and st["uptime"] == 3 * 604800 + 2 * 86400 + 4 * 3600 + 11 * 60 + 5, st)
check("память занято/всего, температура, внешний IP", st["ram"] == [1073741824 - 604000256, 1073741824] and st["temp"] == "47" and st["public_ip"] == "188.134.88.13")
check("выход в интернет: ether1 → шлюз", st["wan"] == {"gateway": "188.134.88.1", "interface": "ether1"}, st["wan"])
check("пароль — заголовком Basic, не в адресе", all("rt-pass" not in p for _, p, _ in SEEN) and SEEN[-1][2].startswith("Basic "))
check("«1d03:04:05» тоже понимается", mikrotik.uptime_s("1d03:04:05") == 86400 + 3 * 3600 + 4 * 60 + 5)

print("доступ:")
e = raises(mikrotik.status, mikrotik.Client("127.0.0.1", USER, "wrong", port=PORT, timeout=5))
check("неверный пароль — «не пустил»", e and "не пустил" in e, e)
STATE["_no_rest"] = True
e = raises(mikrotik.status, c)
check("нет REST (RouterOS 6 / не MikroTik) — понятно и про WebFig", e and "нет REST API" in e and "WebFig" in e, e)
STATE.pop("_no_rest")
s0 = socket.socket()
s0.bind(("127.0.0.1", 0))
dead = s0.getsockname()[1]
s0.close()
e = raises(mikrotik.status, mikrotik.Client("127.0.0.1", USER, PW, port=dead, timeout=3))
check("роутер не отвечает — «недоступен» с адресом", e and "недоступен" in e and str(dead) in e, e)

print("пробросы портов:")
rules = mikrotik.nat(c)
check("проброс и выход в интернет различаются", [r["forward"] for r in rules] == [False, True, True, True], rules)
r = mikrotik.nat_add(c, "tcp", "16100", "192.168.88.9", "", "прокси pcs3000", mikrotik.lan_nets(c))
new = [x for x in STATE["/ip/firewall/nat"] if x[".id"] in r["ids"]]
check("добавлен: dstnat на 192.168.88.9, снаружи — только список WAN", len(new) == 1 and new[0]["chain"] == "dstnat"
      and new[0]["in-interface-list"] == "WAN" and new[0]["to-addresses"] == "192.168.88.9" and "to-ports" not in new[0], new)
r = mikrotik.nat_add(c, "tcp+udp", "5000-5009", "192.168.88.7", "15000-15009", "", mikrotik.lan_nets(c))
check("tcp+udp — два правила, диапазон в диапазон", len(r["ids"]) == 2 and r["to_port"] == "15000-15009")
e = raises(mikrotik.nat_add, c, "tcp", "16025", "192.168.88.9")
check("пересечение с проброшенным — отказ с тем, куда он ведёт", e and "уже проброшен" in e and "192.168.88.7" in e, e)
e = raises(mikrotik.nat_add, c, "udp", "16025", "192.168.88.9")
check("тот же порт по udp — можно (там tcp)", e is None, e)
for args, want in ((("tcp", "70000", "192.168.88.9"), "1–65535"), (("tcp", "80; drop", "192.168.88.9"), "диапазон"),
                   (("icmp", "80", "192.168.88.9"), "протокол"), (("tcp", "81", "8.8.8.8", "", "", mikrotik.lan_nets(c)), "не из сетей"),
                   (("tcp", "82-90", "192.168.88.9", "100-101"), "одной длины"), (("tcp", "83", "192.168.88.9", "", 'a"b'), "кавычек")):
    e = raises(mikrotik.nat_add, c, *args)
    check("проверка: %s" % want, e and want in e, e)
mas = STATE["/ip/firewall/nat"][0][".id"]
e = raises(mikrotik.nat_del, c, mas)
check("выход в интернет (masquerade) из панели не удаляется", e and "не проброс" in e and any(x[".id"] == mas for x in STATE["/ip/firewall/nat"]), e)
e = raises(mikrotik.nat_set, c, mas, True)
check("…и не выключается", e and "не проброс" in e and STATE["/ip/firewall/nat"][0]["disabled"] == "false")
pid = next(x[".id"] for x in STATE["/ip/firewall/nat"] if x.get("dst-port") == "16100")
mikrotik.nat_set(c, pid, True)
check("проброс выключается", next(x for x in STATE["/ip/firewall/nat"] if x[".id"] == pid)["disabled"] == "true")
mikrotik.nat_del(c, pid)
check("…и удаляется", not any(x[".id"] == pid for x in STATE["/ip/firewall/nat"]))
e = raises(mikrotik.nat_del, c, "*1; /system reset")
check("номер правила — только *N", e and "*1" in e, e)
del STATE["/interface/list/member"][:]
r = mikrotik.nat_add(c, "tcp", "17000", "192.168.88.9")
check("нет списка WAN — интерфейс маршрута по умолчанию", next(x for x in STATE["/ip/firewall/nat"] if x[".id"] == r["ids"][0]).get("in-interface") == "ether1")

print("интерфейсы:")
mac = "bc:24:11:00:00:fe"
rows = mikrotik.interfaces(c, "127.0.0.1", mac)
check("связь хоста с роутером: порт моста по MAC хоста", [x["name"] for x in rows if x["ours"]] == ["ether2", "bridge"], [(x["name"], x["ours"]) for x in rows])
rows = mikrotik.interfaces(c, "192.168.88.1", None)
check("…и интерфейс с адресом роутера в нашей сети", [x["name"] for x in rows if x["ours"]] == ["bridge"])
e = raises(mikrotik.iface_set, c, "*2", True, "192.168.88.1", mac)
check("выключить свою связь — отказ", e and "отрезать панель" in e and STATE["/interface"][1]["disabled"] == "false", e)
mikrotik.iface_set(c, "*3", True, "192.168.88.1", mac)
check("другой порт — выключается", STATE["/interface"][2]["disabled"] == "true")
mikrotik.iface_set(c, "*3", False, "192.168.88.1", mac)
check("…и включается", STATE["/interface"][2]["disabled"] == "false")

print("устройства, журнал, перезагрузка:")
ls = mikrotik.leases(c)
check("аренды: адрес, имя, закреплён ли", [(x["address"], x["dynamic"]) for x in ls][:2] == [("192.168.88.254", False), ("192.168.88.7", True)])
mikrotik.lease_static(c, "*2")
check("закрепить адрес — make-static", STATE["/ip/dhcp-server/lease"][1]["dynamic"] == "false")
check("журнал — время, темы, текст", mikrotik.log(c)[-1]["topics"].startswith("system,error"))
check("перезагрузка: роутер рвёт соединение — это успех", mikrotik.reboot(c) == {"rebooting": True})

print("доступ хранится на хосте:")
TMP = tempfile.mkdtemp(prefix="pcs-router-")
store.ETC = TMP


def make(host, user, password, https=False, port=None):
    return mikrotik.Client(host, user, password, https=https, port=PORT if port is None else port, timeout=5)


check("не подключён — сводка без сети, «подключить»", router.brief() == {"configured": False, "hidden": False, "host": "", "name": "", "model": ""})
e = raises(router.call, "status", {}, make)
check("действие без доступа — «не подключён»", e and "не подключён" in e, e)
e = raises(router.call, "save", {"host": "127.0.0.1", "user": USER, "password": "wrong"}, make)
check("неверный пароль — не сохраняется", e and "не пустил" in e and not os.path.exists(router.path()), e)
r = router.call("save", {"host": "127.0.0.1", "user": USER, "password": PW}, make)
check("проверен и сохранён", r["configured"] and r["status"]["identity"] == "MikroTik" and "password" not in r and r["password_set"])
check("файл — только root (0600)", stat.S_IMODE(os.stat(router.path()).st_mode) == 0o600)
r = router.call("save", {"host": "127.0.0.1", "user": USER, "password": ""}, make)
check("пустой пароль — прежний", router.load()["password"] == PW and r["configured"])
check("сводка для дерева", router.brief()["name"] == "MikroTik" and router.brief()["model"] == "hAP ax^2")
check("status — с адресом WebFig", router.call("status", {}, make)["webfig"] == "http://127.0.0.1:%d/" % PORT)
e = raises(router.call, "save", {"host": "1.2.3.4; reboot", "user": USER}, make)
check("адрес — только IP или имя", e and "адрес" in e, e)
router.call("save", {"hidden": True}, make)
check("спрятать из дерева — без проверки, доступ цел", router.brief()["hidden"] and router.load()["password"] == PW)
router.call("forget", {}, make)
check("забыть — логина и пароля нет", "password" not in router.load() and not router.brief()["configured"])

print("панель (фейковый API):")
from pcs.web import fake_api, panel  # noqa: E402
fake_api.DELAY = 0
fake_api.reset()
P = panel.Panel(fake_api)
code, r = P.handle("GET", "/api/router")
check("GET /api/router — доступ без пароля и сводка", code == 200 and r["ok"] and r["data"]["status"]["identity"] == "MikroTik"
      and "password" not in r["data"]["conf"] and "fake" not in json.dumps(r["data"]), r)
code, r = P.handle("POST", "/api/router/nat_add", body={"proto": "tcp", "port": "18000", "to": "192.168.88.9", "comment": "тест"})
check("проброс через панель", r["ok"] and r["data"]["port"] == "18000", r)
code, r = P.handle("POST", "/api/router/reboot", body={})
check("перезагрузка без подтверждения — 400", code == 400 and not r["ok"])
code, r = P.handle("POST", "/api/router/reboot", body={"confirm": "reboot"})
check("с подтверждением — ушёл", r["ok"] and fake_api.RT.get("_rebooted") == 1, r)
code, r = P.handle("POST", "/api/router/save", body={"host": "192.168.88.1", "user": "admin", "password": "wrong"})
check("неверный пароль — ошибка в ответе, доступ прежний", not r["ok"] and "не пустил" in r["error"] and fake_api.RT_CONF.c["password"] == "fake", r)
code, r = P.handle("GET", "/api/router/leases")
check("устройства", r["ok"] and len(r["data"]) == 3)
check("в обзоре хоста — сводка роутера", fake_api.host_info()["router"]["configured"])

srv.shutdown()
shutil.rmtree(TMP, ignore_errors=True)
print("\nитого: ok %d, fail %d" % (passed, failed))
sys.exit(1 if failed else 0)
