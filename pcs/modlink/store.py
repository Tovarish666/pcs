"""Таблица modlink (/etc/modlink/proxies.json) и всё, что из неё выводится без системы:
проверка строк, свободные порты, конфиг 3proxy, строки для клиентов, перенос из modlink-linux.

Строка — ровно поля из docs/ARCHITECTURE.md §8. Порты у строки постоянные: не зависят от
её места в списке (в modlink-linux зависели — и съезжали при правке). Пересчитываются только
по явной команде — «режим портов» (как в modlink для Windows):
  triples — у каждой строки свои три порта подряд: прокси P, ребут P+1, реконнект P+2;
  shared  — у всех один порт прокси (модем выбирается по логину) и один порт управления
            (ссылки /reconnect/<пароль> и /reboot/<пароль>).
Смешанное тоже законно: строки с одним портом прокси различаются логином.
"""
import csv
import hashlib
import urllib.parse
import io
import ipaddress
import os
import re
import secrets
import socket
import string
import unicodedata

from ..core.util import Fail, jload, jsave

FIELDS = ("id", "enabled", "name", "login", "password", "port", "lan_ip", "modem_ip",
          "reconnect_port", "reboot_port", "interval_min", "protect")
MODES = ("triples", "shared")          # режим портов: свои тройки | один общий (по логину)
TRAFFIC = ("tcp+udp", "tcp")           # что пропускать: TCP и UDP (SOCKS5 UDP ASSOCIATE) | только TCP
BASE_PORT = 10000
FIRST_FREE_ID = 1001          # id не по номеру модема (второй прокси на модем, чужая сеть)
MAX_INTERVAL = 7 * 24 * 60
PW_ABC = "abcdefghjkmnpqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789"   # без 0/O, 1/l/I
SAFE = set(string.ascii_letters + string.digits + string.punctuation)


class Paths:
    """Где лежит всё своё. root — для тестов (MODLINK_ROOT); на машине — пусто."""

    def __init__(self, root=""):
        r = root.rstrip("/")
        self.root = r
        self.etc = r + "/etc/modlink"
        self.lib = r + "/var/lib/modlink"
        self.log = r + "/var/log/modlink"
        self.run = r + "/run/modlink"
        self.units = r + "/etc/systemd/system"
        self.proxies = self.etc + "/proxies.json"
        self.singbox = self.etc + "/singbox.json"          # прежний движок; остаётся до uninstall --purge
        self.cfg3 = self.etc + "/3proxy.cfg"
        self.config = self.etc + "/config.json"
        self.guard = self.etc + "/protect.json"
        self.legacy = self.etc + "/legacy"
        self.applied = self.lib + "/applied.json"
        self.checks = self.lib + "/checks.json"            # внешний IP раз в час, скорость — демон
        self.state = self.run + "/state.json"
        self.lock = self.run + "/lock"

    def rowlog(self, rid):
        return "%s/%d.log" % (self.log, rid)

    def modemlock(self, ip):
        return "%s/hilink-%s.lock" % (self.run, ip)


P = Paths(os.environ.get("MODLINK_ROOT", ""))


# ── проверка значений ──────────────────────────────────────────────────────
def as_port(v, what="порт"):
    if isinstance(v, bool):
        raise Fail("%s: нужно число 1–65535" % what)
    try:
        p = int(str(v).strip())
    except ValueError:
        raise Fail("%s: «%s» — не число (нужно 1–65535 или auto)" % (what, v))
    if not 1 <= p <= 65535:
        raise Fail("%s: %d — вне 1–65535" % (what, p))
    return p


def as_ipv4(v, what, empty_ok=False):
    s = str(v if v is not None else "").strip()
    if not s and empty_ok:
        return ""
    try:
        a = ipaddress.IPv4Address(s)
    except ValueError:
        raise Fail("%s: «%s» — не адрес IPv4" % (what, s))
    if a.is_unspecified or a.is_multicast or s == "255.255.255.255":
        raise Fail("%s: %s не годится" % (what, s))
    return str(a)


def as_secret(v, what, colon_ok):
    s = str(v if v is not None else "")
    if not s:
        raise Fail("%s: пусто" % what)
    bad = sorted({c for c in s if c not in SAFE or (c == ":" and not colon_ok)})
    if bad:
        shown = " ".join(repr(c) for c in bad[:5])
        raise Fail("%s: недопустимые символы %s — только латиница, цифры и знаки, без пробелов%s"
                   % (what, shown, "" if colon_ok else " и «:»"))
    if len(s) > 128:
        raise Fail("%s: длиннее 128 символов" % what)
    return s


def as_cport(v, what):
    """Порт управления (реконнект, ребут): 1–65535 или 0/пусто — нет."""
    if v in (None, "", 0, "0"):
        return 0
    return as_port(v, what)


def as_name(v):
    s = str(v if v is not None else "").strip()
    if any(unicodedata.category(c) == "Cc" for c in s):
        raise Fail("name: управляющие символы недопустимы")
    if len(s) > 64:
        raise Fail("name: длиннее 64 символов")
    return s


def as_bool(v, what="enabled"):
    if isinstance(v, bool):
        return v
    s = str(v).strip().lower()
    if s in ("1", "true", "yes", "on", "да", "вкл"):
        return True
    if s in ("0", "false", "no", "off", "нет", "выкл"):
        return False
    raise Fail("%s: «%s» — нужно 1/0" % (what, v))


def as_interval(v):
    if isinstance(v, bool):
        raise Fail("interval_min: нужно число минут")
    try:
        m = int(str(v).strip() or 0)
    except ValueError:
        raise Fail("interval_min: «%s» — не число минут" % v)
    if not 0 <= m <= MAX_INTERVAL:
        raise Fail("interval_min: %d — нужно 0 (выкл) … %d" % (m, MAX_INTERVAL))
    return m


def norm(r):
    """Строка с проверенными значениями всех полей §8 — или Fail с понятным текстом."""
    try:
        rid = int(r["id"])
    except (KeyError, TypeError, ValueError):
        raise Fail("строка без id")
    if rid < 1:
        raise Fail("id %s: нужно положительное число" % r.get("id"))
    try:
        return {
            "id": rid,
            "enabled": as_bool(r.get("enabled", True)),
            "name": as_name(r.get("name", "")),
            "login": as_secret(r.get("login"), "login", colon_ok=False),
            "password": as_secret(r.get("password"), "password", colon_ok=True),
            "port": as_port(r.get("port"), "port"),
            "lan_ip": as_ipv4(r.get("lan_ip"), "lan_ip"),
            "modem_ip": as_ipv4(r.get("modem_ip", ""), "modem_ip", empty_ok=True),
            "reconnect_port": as_cport(r.get("reconnect_port"), "reconnect_port"),
            "reboot_port": as_cport(r.get("reboot_port", 0), "reboot_port"),
            "interval_min": as_interval(r.get("interval_min", 0)),
            "protect": as_bool(r.get("protect", True), "protect"),
        }
    except Fail as e:
        raise Fail("строка %d: %s" % (rid, e))


def check_all(rows):
    """Межстрочные проверки. id и логины не повторяются (у 3proxy один список пользователей:
    вход пускает по логину и паролю на любой порт, где этот логин разрешён). Порт прокси
    может быть общим у нескольких строк — модем выбирается по логину. Порт управления —
    тоже, тогда строку выбирает пароль в ссылке: у строк такого порта пароли разные.
    Порт прокси не бывает портом управления."""
    ids, logins, proxy, ctl = set(), {}, {}, {}
    for r in rows:
        if r["id"] in ids:
            raise Fail("id %d повторяется" % r["id"])
        ids.add(r["id"])
        if r["login"] in logins:
            raise Fail("логин «%s» у строк %d и %d — логины не повторяются" % (r["login"], logins[r["login"]], r["id"]))
        logins[r["login"]] = r["id"]
        proxy.setdefault(r["port"], []).append(r)
        for p in {r["reconnect_port"], r["reboot_port"]} - {0}:
            ctl.setdefault(p, []).append(r)
    for p, rs in ctl.items():
        if p in proxy:
            raise Fail("порт %d — и прокси (строка %d), и управление (строка %d)" % (p, proxy[p][0]["id"], rs[0]["id"]))
        pw = {}
        for r in rs:
            if len(rs) > 1 and r["password"] in pw:
                raise Fail("порт управления %d общий у строк %d и %d, а пароли одинаковые — ссылку не различить"
                           % (p, pw[r["password"]], r["id"]))
            pw[r["password"]] = r["id"]
    return rows


# ── файл таблицы ───────────────────────────────────────────────────────────
def load(path=None):
    """{"next_id": …, "proxies": [строки]} — проверенное; файла нет — пустая таблица."""
    path = path or P.proxies
    doc = jload(path, None)
    if doc is None:
        if os.path.exists(path):
            raise Fail("%s не читается (испорчен?) — исправь или восстанови из .tmp" % path)
        doc = {}
    if not isinstance(doc, dict) or not isinstance(doc.get("proxies", []), list):
        raise Fail("%s: неверный формат" % path)
    rows = check_all([norm(r) for r in doc.get("proxies", [])])
    return {"next_id": max(int(doc.get("next_id") or FIRST_FREE_ID), FIRST_FREE_ID),
            "proxies": sorted(rows, key=lambda r: r["id"]),
            **({"migrated": doc["migrated"]} if doc.get("migrated") else {})}


def save(doc, path=None):
    path = path or P.proxies
    rows = check_all([norm(r) for r in doc["proxies"]])
    os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
    out = {"next_id": doc.get("next_id", FIRST_FREE_ID),
           "proxies": [{k: r[k] for k in FIELDS} for r in sorted(rows, key=lambda r: r["id"])]}
    if doc.get("migrated"):
        out["migrated"] = doc["migrated"]
    jsave(path, out, 0o600)


def find(doc, rid):
    try:
        rid = int(rid)
    except (TypeError, ValueError):
        raise Fail("ID — число: «%s»" % rid)
    for r in doc["proxies"]:
        if r["id"] == rid:
            return r
    raise Fail("строки %d нет — список: modlink list" % rid)


def masked(r, show=False):
    out = {k: r[k] for k in FIELDS}
    if not show:
        out["password"] = None
    return out


# ── общие настройки (/etc/modlink/config.json) ────────────────────────────
SETTINGS = {"base_port": BASE_PORT, "lan_ip": "", "wan_ip": "", "mode": "triples", "traffic": "tcp+udp",
            "access_log": False, "wan_check": True, "speedtest": False}


def settings(path=None):
    """Общие настройки; пустой адрес — определить самим.
    base_port — откуда считаются порты (новые строки и «режим портов»);
    lan_ip — адрес машины в локальной сети: строки для proxyveth ВМ этого хоста;
    wan_ip — внешний адрес для клиентов: строки прокси и ссылки; его же 3proxy сообщает
             клиенту для UDP (SOCKS5 UDP ASSOCIATE). ext_ip — прежнее имя wan_ip;
    mode — режим портов (MODES); traffic — TCP и UDP или только TCP;
    access_log — журнал каждого соединения 3proxy (/var/log/modlink/3proxy.log, 3 дня);
    wan_check — раз в час внешний IP через каждый прокси; speedtest — раз в день в 12:00
                скорость каждого модема (yaspeed, это трафик модема)."""
    c = jload(path or P.config, {})
    c = c if isinstance(c, dict) else {}
    out = dict(SETTINGS)
    try:
        out["base_port"] = as_port(c.get("base_port") or BASE_PORT, "base_port")
    except Fail:
        pass
    out["lan_ip"] = str(c.get("lan_ip") or "")
    out["wan_ip"] = str(c.get("wan_ip") or c.get("ext_ip") or "")
    if c.get("mode") in MODES:
        out["mode"] = c["mode"]
    if c.get("traffic") in TRAFFIC:
        out["traffic"] = c["traffic"]
    for k in ("access_log", "wan_check", "speedtest"):
        if isinstance(c.get(k), bool):
            out[k] = c[k]
    return out


def save_settings(path=None, **kw):
    """kw: base_port, lan_ip, wan_ip ("" или "auto" — определять самим), mode, traffic,
    access_log, wan_check, speedtest. → новые настройки."""
    path = path or P.config
    c = jload(path, {})
    c = c if isinstance(c, dict) else {}
    for k, v in kw.items():
        if k == "base_port":
            c[k] = as_port(v, "base_port")
            if c[k] > 65533:
                raise Fail("base_port: %d — нужна тройка портов, не выше 65533" % c[k])
        elif k in ("lan_ip", "wan_ip"):
            v = str(v or "").strip()
            c[k] = "" if v.lower() in ("", "auto", "авто") else as_ipv4(v, k)
        elif k == "mode":
            if v not in MODES:
                raise Fail("mode: triples (у строки свои порты) или shared (один порт на всех)")
            c[k] = v
        elif k == "traffic":
            v = {"udp": "tcp+udp", "tcp,udp": "tcp+udp", "tcp+udp": "tcp+udp", "tcp": "tcp"}.get(str(v).lower())
            if not v:
                raise Fail("traffic: tcp+udp или tcp")
            c[k] = v
        elif k in ("access_log", "wan_check", "speedtest"):
            c[k] = as_bool(v, k)
        else:
            raise Fail("нет настройки «%s»; есть: %s" % (k, ", ".join(SETTINGS)))
    c.pop("ext_ip", None)                 # прежнее имя wan_ip
    os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
    jsave(path, c, 0o600)
    return settings(path)


def assign_ports(doc, base, mode, mine=()):
    """Порты всех строк заново от base (в порядке id), как в modlink для Windows:
    triples — прокси base+3i, ребут base+3i+1, реконнект base+3i+2;
    shared  — прокси у всех base, ребут и реконнект у всех base+1.
    mine — порты, которые держит своё применённое: для нас они свободны."""
    rows = sorted(doc["proxies"], key=lambda r: r["id"])
    if mode not in MODES:
        raise Fail("режим портов: triples или shared")
    want = range(base, base + (3 * len(rows) if mode == "triples" else 2)) if rows else ()
    if want and want[-1] > 65535:
        raise Fail("от %d не хватит портов на %d строк" % (base, len(rows)))
    busy = [p for p in want if p not in mine and not PROBE[0](p)]
    if busy:
        raise Fail("порт %s уже слушает кто-то другой (ss -ltnp) — возьми другой базовый порт"
                   % ", ".join(map(str, busy[:5])))
    for i, r in enumerate(rows):
        if mode == "triples":
            r["port"], r["reboot_port"], r["reconnect_port"] = base + 3 * i, base + 3 * i + 1, base + 3 * i + 2
        else:
            r["port"], r["reboot_port"], r["reconnect_port"] = base, base + 1, base + 1
    doc["proxies"] = check_all(rows)
    return rows


def renumber(doc, base, mine=()):
    """Прежнее имя: порты всех строк заново — в текущем режиме портов."""
    return assign_ports(doc, base, settings()["mode"], mine)


# ── новые строки ───────────────────────────────────────────────────────────
def gen_password(n=12):
    return "".join(secrets.choice(PW_ABC) for _ in range(n))


def modem_n(ip):
    """192.168.N.x → N (1–254), иначе None."""
    m = re.fullmatch(r"192\.168\.(\d+)\.\d+", ip or "")
    return int(m.group(1)) if m and 1 <= int(m.group(1)) <= 254 else None


def port_free(p):
    """Никто на машине не слушает TCP-порт p (свой sing-box и триггеры — тоже «кто-то»)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind(("0.0.0.0", p))
        return True
    except OSError:
        return False
    finally:
        s.close()


PROBE = [port_free]           # тесты подменяют: машина тут ни при чём


def taken_ports(doc, skip_id=None):
    out = set()
    for r in doc["proxies"]:
        if r["id"] != skip_id:
            out.update(p for p in (r["port"], r["reconnect_port"], r["reboot_port"]) if p)
    return out


def free_port(taken, start=BASE_PORT, pair=False, mine=(), n=None):
    """Первый свободный порт от start, за ним ещё свободных подряд: n (pair — это n=2).
    mine — порты, которые держит своё же (применённая строка): для неё они свободны."""
    n = n or (2 if pair else 1)
    ok = (lambda p: p <= 65535 and p not in taken and (p in mine or PROBE[0](p)))
    p = start
    while p <= 65535:
        if all(ok(p + k) for k in range(n)):
            return p
        p += 1
    raise Fail("не нашёл свободного порта от %d" % start)


def new_id(doc, lan_ip):
    """id = номер модема, если адрес 192.168.N.x и N свободен; иначе следующий от 1001."""
    used = {r["id"] for r in doc["proxies"]}
    n = modem_n(lan_ip)
    if n and n not in used:
        return n
    i = max(doc.get("next_id", FIRST_FREE_ID), FIRST_FREE_ID)
    while i in used:
        i += 1
    doc["next_id"] = i + 1
    return i


def default_modem_ip(lan_ip):
    a = lan_ip.split(".")
    return "" if a[3] == "1" else ".".join(a[:3] + ["1"])


def resolve_ports(doc, row, port, rport, bport="auto", skip_id=None, mine=()):
    """Проставить порты прокси, реконнекта и ребута: число — как есть, 0 — нет (управление),
    auto — по режиму портов: shared — общие base и base+1; triples — свободная тройка подряд
    (прокси, ребут, реконнект), а если часть задана — свободные рядом."""
    s = settings()
    base, auto = s["base_port"], (lambda v: str(v).lower() == "auto")
    if s["mode"] == "shared":
        row["port"] = base if auto(port) else as_port(port, "port")
        row["reboot_port"] = base + 1 if auto(bport) else as_cport(bport, "reboot_port")
        row["reconnect_port"] = base + 1 if auto(rport) else as_cport(rport, "reconnect_port")
        return row
    taken = taken_ports(doc, skip_id)
    if not auto(port):
        row["port"] = as_port(port, "port")
        taken.add(row["port"])
    for k, v in (("reboot_port", bport), ("reconnect_port", rport)):
        if not auto(v):
            row[k] = as_cport(v, k)
            taken.add(row[k])
    free = (lambda p: p <= 65535 and p not in taken and (p in mine or PROBE[0](p)))
    if auto(port):
        need = 1 + auto(bport) + auto(rport)
        row["port"] = free_port(taken, start=base, mine=mine, n=need)
        taken.add(row["port"])
    for k, v, off in (("reboot_port", bport, 1), ("reconnect_port", rport, 2)):
        if auto(v):
            p = row["port"] + off
            row[k] = p if free(p) else free_port(taken, start=base, mine=mine)
            taken.add(row[k])
    return row


def add(doc, lan_ip, name=None, login=None, password=None, port="auto", modem_ip=None,
        reconnect_port="auto", interval_min=0, enabled=True, protect=True, reboot_port="auto"):
    lan_ip = as_ipv4(lan_ip, "lan_ip")
    rid = new_id(doc, lan_ip)
    n = modem_n(lan_ip)
    row = {"id": rid, "enabled": enabled,
           "name": name if name is not None else ("modem %d" % n if n else lan_ip),
           "login": login or ("modem%d" % n if n else "proxy%d" % rid),
           "password": gen_password() if password in (None, "", "gen") else password,
           "lan_ip": lan_ip,
           "modem_ip": default_modem_ip(lan_ip) if modem_ip is None else modem_ip,
           "interval_min": interval_min, "protect": protect}
    resolve_ports(doc, row, port, reconnect_port, reboot_port)
    row = norm(row)
    check_all(doc["proxies"] + [row])
    doc["proxies"].append(row)
    return row


SET_ALIASES = {"interval": "interval_min", "rport": "reconnect_port", "lan": "lan_ip", "modem": "modem_ip",
               "guard": "protect", "bport": "reboot_port", "reboot": "reboot_port", "reconnect": "reconnect_port"}


def set_fields(doc, rid, pairs, mine=()):
    """pairs — [("поле", "значение")]. Возвращает исправленную строку (в doc уже заменена).
    mine — порты, которые сейчас держит применённая версия этой строки."""
    r = dict(find(doc, rid))
    ports = {}
    for k, v in pairs:
        k = SET_ALIASES.get(k.replace("-", "_"), k.replace("-", "_"))
        if k == "id":
            raise Fail("id постоянный — его не меняют")
        if k not in FIELDS:
            raise Fail("нет поля «%s»; поля: %s" % (k, ", ".join(FIELDS[1:])))
        if k in ("port", "reconnect_port", "reboot_port"):
            ports[k] = v
        elif k == "password" and v == "gen":
            r[k] = gen_password()
        else:
            r[k] = v
    if ports:
        resolve_ports(doc, r, ports.get("port", r["port"]), ports.get("reconnect_port", r["reconnect_port"]),
                      ports.get("reboot_port", r["reboot_port"]), skip_id=r["id"], mine=mine)
    r = norm(r)
    rows = [r if x["id"] == r["id"] else x for x in doc["proxies"]]
    check_all(rows)
    doc["proxies"] = rows
    return r


def delete(doc, rid):
    r = find(doc, rid)
    doc["proxies"] = [x for x in doc["proxies"] if x["id"] != r["id"]]
    return r


# ── защита (protect=1 у строки, так по умолчанию) ─────────────────────────
# Мобильная сеть узнаёт абонента по IP: личный кабинет оператора, оплата с баланса,
# подписки и веб модема открываются без пароля. Клиент прокси получил бы всё это.
GUARD_DOMAINS = (
    "mts.ru", "megafon.ru", "beeline.ru", "tele2.ru", "t2.ru", "yota.ru",      # большая четвёрка
    "sbermobile.ru", "vtbmobile.ru", "gpbmobile.ru", "motivtelecom.ru",       # виртуальные и местные
    "danycom.ru", "letai.ru", "volnamobile.ru", "win-mobile.ru",
)
GUARD_PORTS = (23, 25, 135, 137, 138, 139, 445, 465, 587, 1900, 2525)   # telnet, почта, SMB/NetBIOS, SSDP
GUARD_RANGES = ("6881:6889",)                                           # BitTorrent
ALWAYS_CIDRS = ("100.64.0.0/10",)   # CGNAT: внутренняя сеть оператора и Tailscale; частные — ip_is_private


def guard_extra(path=None):
    """/etc/modlink/protect.json — что закрыть сверх списка: {"domains": [], "cidrs": [], "ports": []}.
    Нет файла — пусто; испорчен — Fail (apply не пройдёт, пока не исправят)."""
    path = path or P.guard
    doc = jload(path, None)
    if doc is None:
        if os.path.exists(path):
            raise Fail("%s не читается — исправь или удали" % path)
        return {"domains": [], "cidrs": [], "ports": []}
    if not isinstance(doc, dict):
        raise Fail("%s: нужен объект {domains, cidrs, ports}" % path)
    out = {"domains": [], "cidrs": [], "ports": []}
    for d in doc.get("domains") or []:
        d = str(d).strip().lower().lstrip(".")
        if not re.fullmatch(r"[a-z0-9-]+(\.[a-z0-9-]+)+", d):
            raise Fail("%s: домен «%s» — не годится" % (path, d))
        out["domains"].append(d)
    for c in doc.get("cidrs") or []:
        try:
            out["cidrs"].append(str(ipaddress.ip_network(str(c).strip(), strict=False)))
        except ValueError:
            raise Fail("%s: «%s» — не адрес или сеть" % (path, c))
    for p in doc.get("ports") or []:
        out["ports"].append(as_port(p, "%s: порт" % path))
    return out


def guard_lists(extra=None):
    """Что закрывает защита: встроенное + protect.json."""
    extra = extra or {"domains": [], "cidrs": [], "ports": []}
    return {"domains": sorted(set(GUARD_DOMAINS) | set(extra["domains"])),
            "cidrs": sorted(set(extra["cidrs"])),
            "ports": sorted(set(GUARD_PORTS) | set(extra["ports"])),
            "port_ranges": list(GUARD_RANGES),
            "always": ["частные адреса (10/8, 172.16/12, 192.168/16, 127/8, 169.254/16)"] + list(ALWAYS_CIDRS)
            + ["IPv6"]}


# ── конфиг 3proxy ──────────────────────────────────────────────────────────
ALWAYS_DENY = ("0.0.0.0/8", "10.0.0.0/8", "100.64.0.0/10", "127.0.0.0/8", "169.254.0.0/16",
               "172.16.0.0/12", "192.168.0.0/16", "224.0.0.0/4", "240.0.0.0/4")
TCP_OPS = "CONNECT,HTTP,HTTPS"     # запрет по имени — только TCP: на UDP 3proxy сверяет имя с IP и рубит всё


def q3(s):
    """Токен для 3proxy — в кавычках: он режет по пробелам, а `$` в начале значит «вставить файл»."""
    return '"%s"' % str(s).replace("\\", "\\\\").replace('"', '\\"')


def acl(r, g, shared, traffic):
    """Правила одной строки (первое совпавшее решает): своё разрешённое → запреты → allow."""
    L, out = r["login"], []
    if not r["protect"] and r["modem_ip"]:
        out.append("allow %s * %s" % (L, r["modem_ip"]))            # веб своего модема — для своих (ген1)
    out.append("deny %s * %s" % (L, ",".join(ALWAYS_DENY)))         # сеть и службы самого сервера
    if traffic == "tcp":
        out.append("deny %s * * * UDPASSOC" % L)
    if r["protect"]:
        names = ",".join(n for d in g["domains"] for n in (d, "*." + d))
        out.append("deny %s * %s * %s" % (L, names, TCP_OPS))
        if g["cidrs"]:
            out.append("deny %s * %s" % (L, ",".join(g["cidrs"])))
        ports = ",".join([str(p) for p in g["ports"]] + [x.replace(":", "-") for x in g["port_ranges"]])
        out.append("deny %s * * %s" % (L, ports))
    out.append("allow %s" % L)
    if shared:
        out.append("parent 1000 extip %s 0" % r["lan_ip"])         # общий порт: выход — по логину
    return out


def config3(rows, s=None, extra=None):
    """Конфиг 3proxy. На каждый порт прокси — сервис `auto` (HTTP CONNECT + SOCKS5 на одном порту).
    Порт одной строки: выход `-e lan_ip` (адрес интерфейса модема; дальше — маршрут по источнику).
    Общий порт нескольких строк: `parent 1000 extip lan_ip 0` у правила логина — модем по логину.
    `flush` перед сервисом: правила не перетекают между портами.

    У любой строки закрыты частные сети, CGNAT, сам сервер (IPv6 не выйдет: выход — IPv4).
    Защита (protect, по умолчанию вкл): сверх того сайты и ЛК операторов (по имени, которое
    прислал клиент), опасные порты и веб модема. Без защиты веб своего модема открыт.
    traffic=tcp — UDP ASSOCIATE запрещён. wan_ip задан — его 3proxy сообщает клиенту для UDP."""
    s = s or settings()
    g = guard_lists(extra)
    en = [r for r in sorted(rows, key=lambda x: (x["port"], x["id"])) if r["enabled"]]
    out = ["# modlink (PCS) — пишет `modlink apply`, руками не править", "",
           "nserver 8.8.8.8", "nserver 1.1.1.1", "nscache 65536",
           "timeouts 1 5 30 60 180 1800 15 60", "maxconn 5000"]
    if s.get("access_log"):
        out += ['log "%s/3proxy.log" D' % P.log, "rotate 3",
                'logformat "L%Y-%m-%d %H:%M:%S %N.%p %E %U %C:%c %R:%r %O %I %T"']
    out += ["", "auth strong"]
    if en:
        out.append("users " + " ".join(q3("%s:CL:%s" % (r["login"], r["password"])) for r in en))
    groups = {}
    for r in en:
        groups.setdefault(r["port"], []).append(r)
    for port, rs in sorted(groups.items()):
        shared = len(rs) > 1
        out += ["", "# порт %d: %s" % (port, ", ".join("строка %d (%s)" % (r["id"], r["name"]) for r in rs)), "flush"]
        for r in rs:
            out += acl(r, g, shared, s.get("traffic", "tcp+udp"))
        svc = "auto -p%d" % port
        if not shared:
            svc += " -e%s" % rs[0]["lan_ip"]
        if s.get("traffic", "tcp+udp") != "tcp" and s.get("wan_ip"):
            svc += " -Ni%s" % s["wan_ip"]
        out.append(svc)
    return "\n".join(out) + "\n"


# ── строки для клиентов ────────────────────────────────────────────────────
def shared_ctl(rows):
    """Порты управления, общие у нескольких включённых строк: там строку выбирает пароль в ссылке."""
    cnt = {}
    for r in rows:
        if r["enabled"]:
            for p in {r["reconnect_port"], r["reboot_port"]} - {0}:
                cnt[p] = cnt.get(p, 0) + 1
    return {p for p, n in cnt.items() if n > 1}


def link(r, ip, what, shared=()):
    """http://IP:порт/reconnect или /reboot; на общем порту — с паролем строки в пути
    (браузер не считает это прокси-авторизацией, в отличие от http://login:pass@)."""
    port = r["reconnect_port"] if what == "reconnect" else r["reboot_port"]
    if not port:
        return ""
    tail = "/" + urllib.parse.quote(r["password"], safe="") if port in shared else ""
    return "http://%s:%d/%s%s" % (ip, port, what, tail)


COPY = {"proxy": ["Ip:port:log:pass"], "reboot": ["Reboot"], "reconnect": ["ReConnect"],
        "all": ["Ip:port:log:pass", "Reboot", "ReConnect"]}


def export(rows, ip, what="all"):
    """Строки для клиента — как «Копировать» в modlink для Windows: только прокси, только ссылки
    ребута, только ссылки реконнекта или всё (по колонкам: вставляется в таблицу по ячейкам).
    → {"columns", "rows"} — строки только включённых, по порту."""
    if what not in COPY:
        raise Fail("что копировать: proxy, reboot, reconnect или all")
    shared = shared_ctl(rows)
    out = []
    for r in sorted((x for x in rows if x["enabled"]), key=lambda x: (x["port"], x["id"])):
        cells = {"proxy": "%s:%d:%s:%s" % (ip, r["port"], r["login"], r["password"]),
                 "reboot": link(r, ip, "reboot", shared), "reconnect": link(r, ip, "reconnect", shared)}
        out.append([cells[k] for k in (["proxy", "reboot", "reconnect"] if what == "all" else [what])])
    return {"columns": COPY[what], "rows": out}


def export_lines(rows, ip, what="all"):
    """Те же строки текстом: колонки через табуляцию."""
    return ["\t".join(c for c in row) for row in export(rows, ip, what)["rows"]]


def table_lines(rows, ip):
    """Таблица proxyveth (n,real,proxy,enabled) из строк modlink: настоящий модем N → виртуальный N.
    → (строки CSV с шапкой, предупреждения). proxyveth ходит через прокси к 192.168.real.1
    (DNS и HiLink) — у строки-поставщика защита снята, а modem_ip — ровно этот адрес."""
    buf, warn = io.StringIO(), []
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(["n", "real", "proxy", "enabled"])
    for r in sorted(rows, key=lambda x: x["id"]):
        if not r["enabled"]:
            continue
        n = modem_n(r["lan_ip"])
        if not n:
            warn.append("строка %d: lan_ip %s — не 192.168.N.x, номер модема не узнать; пропустил" % (r["id"], r["lan_ip"]))
            continue
        if r["protect"]:
            warn.append("строка %d: защита включена — DNS и HiLink виртуального модема (192.168.%d.1 через прокси) "
                        "не пройдут: modlink protect %d off" % (r["id"], n, r["id"]))
        elif r["modem_ip"] != "192.168.%d.1" % n:
            warn.append("строка %d: modem_ip %s, а виртуальному модему нужен 192.168.%d.1: modlink set %d modem_ip=192.168.%d.1"
                        % (r["id"], r["modem_ip"] or "пуст", n, r["id"], n))
        w.writerow([n, n, "%s:%d:%s:%s" % (ip, r["port"], r["login"], r["password"]), 1])
    return buf.getvalue().splitlines(), warn


# ── интерфейсы сервера ─────────────────────────────────────────────────────
def parse_addrs(text):
    """Вывод `ip -4 -o addr` → [(dev, ip, prefix)]."""
    out = []
    for ln in text.splitlines():
        m = re.match(r"\s*\d+:\s+(\S+)\s+inet\s+(\d+\.\d+\.\d+\.\d+)/(\d+)", ln)
        if m:
            out.append((m.group(1).split("@")[0], m.group(2), int(m.group(3))))
    return out


def suggest(addrs, rows, skip_devs=()):
    """Интерфейсы с 192.168.N.100, которых ещё нет в таблице → заготовки строк."""
    have = {r["lan_ip"] for r in rows}
    out = []
    for dev, ip, _ in addrs:
        n = modem_n(ip)
        if n and ip.endswith(".100") and ip not in have and dev not in skip_devs:
            out.append({"dev": dev, "lan_ip": ip, "modem_ip": "192.168.%d.1" % n, "name": "modem %d" % n})
            have.add(ip)
    return sorted(out, key=lambda s: modem_n(s["lan_ip"]))


# ── перенос из modlink-linux ───────────────────────────────────────────────
def legacy_password(n):
    """Пароль, который modlink-server придумывал строке без пароля."""
    return hashlib.sha256(("proxyveth-modem-%d" % n).encode()).hexdigest()[:16]


def legacy(src, old_sb=None):
    """modems.conf + server.json старого modlink-linux → (строки, ext_ip, предупреждения).

    Порты как были: прокси base+2i, триггер base+2i+1 (i — место в списке по N),
    логин modemN, пароли — из modems.conf, а где его нет — из работающего singbox.json.
    """
    conf = os.path.join(src, "modems.conf")
    try:
        with open(conf, encoding="utf-8") as f:
            text = f.read()
    except OSError:
        raise Fail("нет %s — старого modlink-linux тут нет" % conf)
    server = jload(os.path.join(src, "server.json"), {})
    try:
        base = int(server.get("base_port") or BASE_PORT)
    except (TypeError, ValueError):
        raise Fail("server.json: base_port «%s» — не число" % server.get("base_port"))
    sb = old_sb if old_sb is not None else jload(os.path.join(src, "singbox.json"), {})
    running = {}
    for i in sb.get("inbounds", []) if isinstance(sb, dict) else []:
        m = re.fullmatch(r"in-(\d+)", str(i.get("tag", "")))
        if m and i.get("users"):
            running[int(m.group(1))] = (i.get("listen_port"), i["users"][0].get("password"))
    warn, entries, seen = [], [], set()
    for ln, raw in enumerate(text.splitlines(), 1):
        parts = raw.split("#", 1)[0].split()
        if not parts:
            continue
        try:
            n = int(parts[0])
        except ValueError:
            warn.append("modems.conf:%d: «%s» — не номер модема, пропустил" % (ln, raw.strip()))
            continue
        if not 1 <= n <= 254 or n in seen:
            warn.append("modems.conf:%d: модем %d %s — пропустил" % (ln, n, "повторяется" if n in seen else "вне 1–254"))
            continue
        seen.add(n)
        iv = 0
        if len(parts) > 2:
            try:
                iv = int(parts[2])
            except ValueError:
                warn.append("modems.conf:%d: интервал «%s» — не число, поставил 0" % (ln, parts[2]))
        entries.append((n, parts[1] if len(parts) > 1 else None, iv))
    if not entries:
        raise Fail("%s: модемов нет — переносить нечего" % conf)
    rows = []
    for i, (n, pw, iv) in enumerate(sorted(entries)):
        port = base + 2 * i
        run_port, run_pw = running.get(n, (None, None))
        if pw is None:
            pw = run_pw or legacy_password(n)
        if run_port is not None and run_port != port:
            warn.append("модем %d: в работающем конфиге порт %s, по modems.conf — %d; беру %d"
                        % (n, run_port, port, port))
        if run_pw is not None and run_pw != pw:
            warn.append("модем %d: пароль в работающем конфиге другой; беру из modems.conf" % n)
        rows.append(norm({"id": n, "enabled": True, "name": "modem %d" % n, "login": "modem%d" % n,
                          "password": pw, "port": port, "lan_ip": "192.168.%d.100" % n,
                          "modem_ip": "192.168.%d.1" % n, "reconnect_port": port + 1, "reboot_port": 0,
                          "interval_min": min(max(iv, 0), MAX_INTERVAL)}))
    check_all(rows)
    return rows, str(server.get("ext_ip") or "").strip(), warn
