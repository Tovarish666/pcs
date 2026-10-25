"""Роутер MikroTik (RouterOS 7) через REST API: /rest/… на его веб-сервере (PCS.md §20).

REST есть с RouterOS 7.1 и работает там, где работает WebFig (служба www, порт 80; или
www-ssl, 443). Хост ходит к роутеру по своей локальной сети — это его шлюз (192.168.88.1).
RouterOS 6 REST не умеет — тогда только WebFig в окне.

Соответствие REST: GET — список/запись, PUT — добавить, PATCH /id — изменить, DELETE /id —
удалить, POST /команда — выполнить (reboot, make-static). Значения — строки ("true", "12").

Здесь — клиент и разбор ответов в простой вид для панели; с чем и как соединяться — pcs.hub.router.
Защита от «отрезать себя»: интерфейс, через который хост видит роутер, не выключается;
правила выхода в интернет (srcnat/masquerade) из панели не трогаются.
"""
import base64
import http.client
import ipaddress
import json
import re
import socket
import ssl
import urllib.parse

from pcs.core.util import Fail

PORT_RE = re.compile(r"\d{1,5}(-\d{1,5})?")
ID_RE = re.compile(r"\*[0-9A-Fa-f]{1,8}")
TIMEOUT = 8


class RouterFail(Fail):
    pass


def yes(v):
    return str(v).lower() in ("true", "yes", "1")


def num(v, default=0):
    try:
        return int(float(str(v)))
    except (TypeError, ValueError):
        return default


def uptime_s(text):
    """«2w1d5h32m11s» или «1d03:04:05» → секунды."""
    t, total = str(text or ""), 0
    for n, u in re.findall(r"(\d+)([wdhms])", t):
        total += int(n) * {"w": 604800, "d": 86400, "h": 3600, "m": 60, "s": 1}[u]
    m = re.search(r"(\d+):(\d\d):(\d\d)$", t)
    if m:
        total += int(m.group(1)) * 3600 + int(m.group(2)) * 60 + int(m.group(3))
    return total


class Client:
    """REST RouterOS по HTTP(S) с базовой авторизацией. req() → разобранный JSON."""

    def __init__(self, host, user, password, https=False, port=None, timeout=TIMEOUT):
        self.host, self.https = host, bool(https)
        self.port = int(port or (443 if https else 80))
        self.timeout = timeout
        self.auth = "Basic " + base64.b64encode(("%s:%s" % (user, password)).encode()).decode()

    def where(self):
        return "%s://%s%s" % ("https" if self.https else "http", self.host,
                              "" if self.port in (80, 443) else ":%d" % self.port)

    def req(self, method, path, body=None, query=None, quiet_drop=False):
        url = "/rest" + path + ("?" + urllib.parse.urlencode(query) if query else "")
        if self.https:
            ctx = ssl.create_default_context()
            ctx.check_hostname, ctx.verify_mode = False, ssl.CERT_NONE      # у роутера свой сертификат
            c = http.client.HTTPSConnection(self.host, self.port, timeout=self.timeout, context=ctx)
        else:
            c = http.client.HTTPConnection(self.host, self.port, timeout=self.timeout)
        h = {"Authorization": self.auth, "Accept": "application/json"}
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            h["Content-Type"] = "application/json"
        try:
            c.request(method, url, body=data, headers=h)
            r = c.getresponse()
            raw = r.read()
        except (socket.timeout, TimeoutError):
            if quiet_drop:
                return None
            raise RouterFail("роутер %s не ответил за %d с" % (self.where(), self.timeout))
        except (OSError, http.client.HTTPException, ssl.SSLError) as e:
            if quiet_drop:                  # reboot: роутер рвёт соединение — это и есть ответ
                return None
            raise RouterFail("роутер %s недоступен: %s" % (self.where(), getattr(e, "strerror", None) or e))
        finally:
            c.close()
        try:
            obj = json.loads(raw.decode("utf-8", "replace")) if raw.strip() else None
        except ValueError:
            obj = None
        if r.status == 401:
            raise RouterFail("роутер не пустил: неверный логин или пароль (или у пользователя нет прав на api/rest)")
        if r.status == 404 and path == "/system/resource":
            raise RouterFail("на %s нет REST API: это не MikroTik или RouterOS старше 7.1 — тогда только WebFig" % self.where())
        if r.status >= 400:
            msg = obj.get("detail") or obj.get("message") if isinstance(obj, dict) else None
            raise RouterFail("роутер: %s" % (msg or "HTTP %d" % r.status))
        return obj


class Fake:
    """Роутер в памяти — тот же REST (для фейковой панели и тестов)."""

    def __init__(self, state=None):
        self.s = state if state is not None else demo_state()
        self.n = 100
        self.calls = []

    def where(self):
        return "http://192.168.88.1"

    def _table(self, path):
        if path not in self.s or not isinstance(self.s[path], list):
            raise RouterFail("роутер: no such command")
        return self.s[path]

    def req(self, method, path, body=None, query=None, quiet_drop=False):
        self.calls.append((method, path, body))
        if self.s.get("_down"):
            raise RouterFail("роутер %s недоступен: Connection refused" % self.where())
        if method == "GET":
            if path in self.s:
                v = self.s[path]
                if isinstance(v, list) and query:
                    v = [x for x in v if all(x.get(k) == q for k, q in query.items())]
                return json.loads(json.dumps(v))
            base, _, rid = path.rpartition("/")
            for x in self._table(base):
                if x.get(".id") == rid:
                    return dict(x)
            raise RouterFail("роутер: no such item")
        if method == "PUT":
            self.n += 1
            row = dict(body or {}, **{".id": "*%X" % self.n})
            row.setdefault("disabled", "false")
            self._table(path).append(row)
            return row
        if method in ("PATCH", "DELETE"):
            base, _, rid = path.rpartition("/")
            rows = self._table(base)
            for i, x in enumerate(rows):
                if x.get(".id") == rid:
                    if method == "DELETE":
                        rows.pop(i)
                        return None
                    x.update(body or {})
                    return dict(x)
            raise RouterFail("роутер: no such item")
        if method == "POST":
            if path == "/system/reboot":
                self.s["_rebooted"] = self.s.get("_rebooted", 0) + 1
                return []
            if path == "/ip/dhcp-server/lease/make-static":
                for x in self.s["/ip/dhcp-server/lease"]:
                    if x[".id"] == (body or {}).get(".id"):
                        x["dynamic"] = "false"
                        return []
                raise RouterFail("роутер: no such item")
        raise RouterFail("роутер: no such command")


def demo_state():
    """Правдоподобный hAP ax² у фермы: LAN 192.168.88.0/24, WAN на ether1, пробросы к прокси."""
    return {
        "/system/resource": {"uptime": "3w2d4h11m5s", "version": "7.16.1 (stable)", "free-memory": "604000256",
                             "total-memory": "1073741824", "cpu-load": "7", "cpu-count": "4", "free-hdd-space": "92315648",
                             "total-hdd-space": "134217728", "architecture-name": "arm64", "board-name": "hAP ax^2",
                             "platform": "MikroTik"},
        "/system/identity": {"name": "MikroTik"},
        "/system/routerboard": {"routerboard": "true", "model": "C52iG-5HaxD2HaxD", "serial-number": "HE20A0XXXXX",
                                "current-firmware": "7.16.1", "upgrade-firmware": "7.16.1"},
        "/system/health": [{".id": "*1", "name": "cpu-temperature", "value": "47", "type": "C"}],
        "/ip/cloud": {"ddns-enabled": "false", "public-address": "188.134.88.13"},
        "/ip/address": [
            {".id": "*1", "address": "192.168.88.1/24", "network": "192.168.88.0", "interface": "bridge", "dynamic": "false", "disabled": "false"},
            {".id": "*2", "address": "188.134.88.13/24", "network": "188.134.88.0", "interface": "ether1", "dynamic": "true", "disabled": "false"}],
        "/ip/route": [{".id": "*1", "dst-address": "0.0.0.0/0", "gateway": "188.134.88.1", "immediate-gw": "188.134.88.1%ether1",
                       "active": "true", "dynamic": "true", "distance": "1"}],
        "/interface": [
            {".id": "*1", "name": "ether1", "type": "ether", "running": "true", "disabled": "false", "rx-byte": "981734121233",
             "tx-byte": "1203993312321", "mac-address": "48:A9:8A:00:00:01", "comment": "WAN"},
            {".id": "*2", "name": "ether2", "type": "ether", "running": "true", "disabled": "false", "rx-byte": "1203003312321",
             "tx-byte": "981034121233", "mac-address": "48:A9:8A:00:00:02"},
            {".id": "*3", "name": "ether3", "type": "ether", "running": "false", "disabled": "false", "rx-byte": "0", "tx-byte": "0",
             "mac-address": "48:A9:8A:00:00:03"},
            {".id": "*4", "name": "wifi1", "type": "wifi", "running": "false", "disabled": "true", "rx-byte": "0", "tx-byte": "0",
             "mac-address": "48:A9:8A:00:00:05"},
            {".id": "*9", "name": "bridge", "type": "bridge", "running": "true", "disabled": "false", "rx-byte": "1202003312321",
             "tx-byte": "980034121233", "mac-address": "48:A9:8A:00:00:02"}],
        "/interface/list": [{".id": "*1", "name": "WAN"}, {".id": "*2", "name": "LAN"}],
        "/interface/list/member": [{".id": "*1", "list": "WAN", "interface": "ether1"}, {".id": "*2", "list": "LAN", "interface": "bridge"}],
        "/interface/bridge/host": [{".id": "*1", "mac-address": "BC:24:11:00:00:FE", "on-interface": "ether2", "interface": "bridge"}],
        "/ip/firewall/nat": [
            {".id": "*1", "chain": "srcnat", "action": "masquerade", "out-interface-list": "WAN", "disabled": "false",
             "comment": "defconf: masquerade", "bytes": "1200000", "packets": "9000"},
            {".id": "*2", "chain": "dstnat", "action": "dst-nat", "protocol": "tcp", "dst-port": "666", "in-interface-list": "WAN",
             "to-addresses": "192.168.88.254", "to-ports": "666", "disabled": "false", "comment": "панель PCS", "bytes": "88000", "packets": "640"},
            {".id": "*3", "chain": "dstnat", "action": "dst-nat", "protocol": "tcp", "dst-port": "16021-16030", "in-interface-list": "WAN",
             "to-addresses": "192.168.88.7", "disabled": "false", "comment": "прокси pcs2000", "bytes": "5400000", "packets": "41000"},
            {".id": "*4", "chain": "dstnat", "action": "dst-nat", "protocol": "tcp", "dst-port": "2221", "in-interface-list": "WAN",
             "to-addresses": "192.168.88.254", "to-ports": "22", "disabled": "true", "comment": "ssh хоста"}],
        "/ip/dhcp-server/lease": [
            {".id": "*1", "address": "192.168.88.254", "mac-address": "BC:24:11:00:00:FE", "host-name": "servervolga", "status": "bound",
             "dynamic": "false", "server": "defconf", "comment": "хост", "last-seen": "12s"},
            {".id": "*2", "address": "192.168.88.7", "mac-address": "BC:24:11:00:07:01", "host-name": "pcs2000", "status": "bound",
             "dynamic": "true", "server": "defconf", "last-seen": "40s"},
            {".id": "*3", "address": "192.168.88.31", "mac-address": "AA:BB:CC:00:00:31", "host-name": "", "status": "waiting",
             "dynamic": "true", "server": "defconf", "last-seen": "2d3h"}],
        "/log": [{".id": "*%X" % i, "time": t, "topics": tp, "message": m} for i, (t, tp, m) in enumerate([
            ("2026-10-24 09:12:01", "system,info,account", "user admin logged in from 192.168.88.254 via rest"),
            ("2026-10-24 09:40:17", "dhcp,info", "defconf assigned 192.168.88.7 for BC:24:11:00:07:01 pcs2000"),
            ("2026-10-24 10:02:44", "interface,info", "ether3 link down"),
            ("2026-10-24 10:05:12", "system,error,critical", "login failure for user admin from 45.141.84.10 via winbox")], 1)],
    }


# ── разбор ──────────────────────────────────────────────────────────────────

def _opt(c, path, default):
    try:
        v = c.req("GET", path)
        return v if v is not None else default
    except RouterFail:
        return default


def status(c):
    """Сводка роутера. Первым — /system/resource: нет доступа/REST — ошибка сразу и понятная."""
    res = c.req("GET", "/system/resource") or {}
    ident = _opt(c, "/system/identity", {})
    rb = _opt(c, "/system/routerboard", {})
    health = _opt(c, "/system/health", [])
    cloud = _opt(c, "/ip/cloud", {})
    addrs = _opt(c, "/ip/address", [])
    routes = _opt(c, "/ip/route", [])
    temp = volt = None
    for h in health if isinstance(health, list) else []:
        if h.get("name") in ("temperature", "cpu-temperature", "board-temperature1") and temp is None:
            temp = h.get("value")
        elif h.get("name") == "voltage":
            volt = h.get("value")
    wan = None
    for r in routes if isinstance(routes, list) else []:
        if r.get("dst-address") == "0.0.0.0/0" and yes(r.get("active", "true")):
            gw = r.get("gateway") or ""
            wan = {"gateway": gw.split("%")[0], "interface": (r.get("immediate-gw") or gw).partition("%")[2] or r.get("vrf-interface") or ""}
            break
    total_m, free_m = num(res.get("total-memory")), num(res.get("free-memory"))
    total_d, free_d = num(res.get("total-hdd-space")), num(res.get("free-hdd-space"))
    fw_new = rb.get("upgrade-firmware") if rb.get("upgrade-firmware") and rb.get("upgrade-firmware") != rb.get("current-firmware") else None
    return {"identity": ident.get("name") or "", "board": res.get("board-name") or "", "model": rb.get("model") or "",
            "version": res.get("version") or "", "firmware": rb.get("current-firmware") or "", "firmware_new": fw_new,
            "uptime": uptime_s(res.get("uptime")), "cpu": num(res.get("cpu-load")), "cores": num(res.get("cpu-count"), 1),
            "arch": res.get("architecture-name") or "", "ram": [total_m - free_m, total_m], "disk": [total_d - free_d, total_d],
            "temp": temp, "voltage": volt, "public_ip": cloud.get("public-address") or "",
            "addresses": [{"address": a.get("address"), "interface": a.get("interface"), "dynamic": yes(a.get("dynamic")),
                           "disabled": yes(a.get("disabled"))} for a in (addrs if isinstance(addrs, list) else [])],
            "wan": wan}


def wan_lists(c):
    """Интерфейсы WAN: из списка «WAN» (так в заводской настройке), иначе — интерфейс маршрута по умолчанию."""
    members = _opt(c, "/interface/list/member", [])
    out = {"list": None, "interfaces": []}
    if any(m.get("list") == "WAN" for m in members):
        out["list"] = "WAN"
        out["interfaces"] = [m.get("interface") for m in members if m.get("list") == "WAN"]
        return out
    for r in _opt(c, "/ip/route", []):
        if r.get("dst-address") == "0.0.0.0/0":
            i = (r.get("immediate-gw") or "").partition("%")[2]
            if i:
                out["interfaces"] = [i]
                break
    return out


def ours(c, router_ip=None, my_mac=None):
    """Интерфейсы, через которые хост видит роутер: с адресом роутера в нашей сети и порт моста,
    за которым MAC хоста. Их выключить — отрезать панель от роутера."""
    out = set()
    try:
        ip = ipaddress.IPv4Address(router_ip) if router_ip else None
    except ValueError:
        ip = None
    for a in _opt(c, "/ip/address", []):
        try:
            if ip and ip in ipaddress.IPv4Interface(a.get("address")).network:
                out.add(a.get("interface"))
        except ValueError:
            pass
    if my_mac:
        for h in _opt(c, "/interface/bridge/host", []):
            if (h.get("mac-address") or "").lower() == my_mac.lower():
                out.add(h.get("on-interface"))
                out.add(h.get("interface") or h.get("bridge"))
    out.discard(None)
    return out


def interfaces(c, router_ip=None, my_mac=None):
    rows = c.req("GET", "/interface") or []
    mine, wan = ours(c, router_ip, my_mac), set(wan_lists(c)["interfaces"])
    return [{"id": x.get(".id"), "name": x.get("name") or "", "type": x.get("type") or "", "running": yes(x.get("running")),
             "disabled": yes(x.get("disabled")), "rx": num(x.get("rx-byte")), "tx": num(x.get("tx-byte")),
             "mac": x.get("mac-address") or "", "comment": x.get("comment") or "",
             "ours": x.get("name") in mine, "wan": x.get("name") in wan} for x in rows]


def iface_set(c, rid, disabled, router_ip=None, my_mac=None):
    rid = _rid(rid)
    row = next((x for x in interfaces(c, router_ip, my_mac) if x["id"] == rid), None)
    if row is None:
        raise RouterFail("нет такого интерфейса")
    if disabled and row["ours"]:
        raise RouterFail("%s — через него этот хост видит роутер: выключить его — отрезать панель. Только в WebFig/Winbox" % row["name"])
    c.req("PATCH", "/interface/" + rid, {"disabled": "true" if disabled else "false"})
    return {"name": row["name"], "disabled": bool(disabled)}


def nat(c):
    rows = c.req("GET", "/ip/firewall/nat") or []
    out = []
    for x in rows:
        fwd = x.get("chain") == "dstnat" and x.get("action") in ("dst-nat", "netmap")
        out.append({"id": x.get(".id"), "chain": x.get("chain") or "", "action": x.get("action") or "", "forward": fwd,
                    "proto": x.get("protocol") or "", "port": x.get("dst-port") or "", "dst": x.get("dst-address") or "",
                    "in": x.get("in-interface") or x.get("in-interface-list") or "", "to": x.get("to-addresses") or "",
                    "to_port": x.get("to-ports") or "", "comment": x.get("comment") or "", "disabled": yes(x.get("disabled")),
                    "dynamic": yes(x.get("dynamic")), "invalid": yes(x.get("invalid")), "bytes": num(x.get("bytes")),
                    "packets": num(x.get("packets"))})
    return out


def _ports(p):
    a, _, b = p.partition("-")
    return int(a), int(b or a)


def _port(v, what, empty=False):
    v = str(v if v is not None else "").strip().replace(" ", "")
    if not v and empty:
        return ""
    if not PORT_RE.fullmatch(v):
        raise RouterFail("%s: число 1–65535 или диапазон 16000-16100" % what)
    a, b = _ports(v)
    if not (1 <= a <= b <= 65535):
        raise RouterFail("%s: 1–65535, начало не больше конца" % what)
    return v


def _rid(v):
    v = str(v or "")
    if not ID_RE.fullmatch(v):
        raise RouterFail("номер правила RouterOS: *1, *2A…")
    return v


def _text(v, what, n=120):
    v = str(v or "").strip()
    if len(v) > n or re.search(r"[\x00-\x1f\x7f\"\\]", v):
        raise RouterFail("%s: до %d знаков, без кавычек и управляющих" % (what, n))
    return v


def nat_add(c, proto, port, to, to_port="", comment="", lan=None):
    """Проброс порта снаружи (WAN) на адрес в своей сети. proto: tcp | udp | tcp+udp."""
    if proto not in ("tcp", "udp", "tcp+udp"):
        raise RouterFail("протокол: tcp, udp или tcp+udp")
    port, to_port = _port(port, "внешний порт"), _port(to_port, "порт внутри", empty=True)
    try:
        to_ip = ipaddress.IPv4Address(str(to).strip())
    except ValueError:
        raise RouterFail("адрес внутри: IPv4, например 192.168.88.7")
    if lan is not None and not any(to_ip in n for n in lan):
        raise RouterFail("%s — не из сетей роутера (%s)" % (to_ip, ", ".join(str(n) for n in lan) or "—"))
    if to_port and (_ports(to_port)[1] - _ports(to_port)[0]) != (_ports(port)[1] - _ports(port)[0]):
        raise RouterFail("диапазоны снаружи и внутри — одной длины (или порт внутри пустой — тот же)")
    comment = _text(comment, "подпись")
    lo, hi = _ports(port)
    protos = ("tcp", "udp") if proto == "tcp+udp" else (proto,)
    for r in nat(c):
        if r["forward"] and not r["disabled"] and r["proto"] in protos and r["port"] and not r["dst"]:
            a, b = _ports(r["port"].split(",")[0]) if PORT_RE.fullmatch(r["port"].split(",")[0]) else (0, -1)
            if a <= hi and lo <= b:
                raise RouterFail("%s %s уже проброшен на %s%s (правило %s%s)" % (r["proto"], r["port"], r["to"],
                                 ":" + r["to_port"] if r["to_port"] else "", r["id"], " «%s»" % r["comment"] if r["comment"] else ""))
    w = wan_lists(c)
    made = []
    for p in protos:
        row = {"chain": "dstnat", "action": "dst-nat", "protocol": p, "dst-port": port, "to-addresses": str(to_ip)}
        if to_port:
            row["to-ports"] = to_port
        if comment:
            row["comment"] = comment
        if w["list"]:
            row["in-interface-list"] = w["list"]
        elif w["interfaces"]:
            row["in-interface"] = w["interfaces"][0]
        else:
            raise RouterFail("не нашёл, какой интерфейс смотрит в интернет (нет списка WAN и маршрута по умолчанию) — проброс без него перехватил бы и исходящие соединения")
        made.append((c.req("PUT", "/ip/firewall/nat", row) or {}).get(".id"))
    return {"ids": made, "port": port, "to": str(to_ip), "to_port": to_port or port, "proto": proto}


def _nat_row(c, rid):
    rid = _rid(rid)
    row = next((r for r in nat(c) if r["id"] == rid), None)
    if row is None:
        raise RouterFail("нет такого правила")
    if not row["forward"]:
        raise RouterFail("правило %s — не проброс порта (%s %s): из панели его не трогаем, только WebFig" % (rid, row["chain"], row["action"]))
    if row["dynamic"]:
        raise RouterFail("правило %s — динамическое (его ставит сам роутер)" % rid)
    return rid, row


def nat_set(c, rid, disabled):
    rid, row = _nat_row(c, rid)
    c.req("PATCH", "/ip/firewall/nat/" + rid, {"disabled": "true" if disabled else "false"})
    return {"id": rid, "disabled": bool(disabled)}


def nat_del(c, rid):
    rid, row = _nat_row(c, rid)
    c.req("DELETE", "/ip/firewall/nat/" + rid)
    return {"id": rid, "port": row["port"], "to": row["to"]}


def leases(c):
    rows = c.req("GET", "/ip/dhcp-server/lease") or []
    return [{"id": x.get(".id"), "address": x.get("address") or x.get("active-address") or "", "mac": x.get("mac-address") or "",
             "host": x.get("host-name") or "", "status": x.get("status") or "", "dynamic": yes(x.get("dynamic")),
             "comment": x.get("comment") or "", "server": x.get("server") or "", "last_seen": x.get("last-seen") or "",
             "disabled": yes(x.get("disabled"))} for x in rows]


def lease_static(c, rid):
    rid = _rid(rid)
    c.req("POST", "/ip/dhcp-server/lease/make-static", {".id": rid})
    return {"id": rid}


def log(c, n=150):
    rows = c.req("GET", "/log") or []
    return [{"time": x.get("time") or "", "topics": x.get("topics") or "", "message": x.get("message") or ""} for x in rows[-n:]]


def reboot(c):
    c.req("POST", "/system/reboot", {}, quiet_drop=True)
    return {"rebooting": True}


def lan_nets(c):
    out = []
    w = set(wan_lists(c)["interfaces"])
    for a in _opt(c, "/ip/address", []):
        try:
            n = ipaddress.IPv4Interface(a.get("address")).network
        except ValueError:
            continue
        if a.get("interface") not in w and n.is_private:
            out.append(n)
    return out
