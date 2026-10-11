"""Таблица modlink (/etc/modlink/proxies.json) и всё, что из неё выводится без системы:
проверка строк, свободные порты, конфиг sing-box, строки экспорта, перенос из modlink-linux.

Строка — ровно поля из docs/ARCHITECTURE.md §8. Порт и триггер у строки постоянные:
не зависят от её места в списке (в modlink-linux зависели — и съезжали при правке).
"""
import csv
import hashlib
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
          "reconnect_port", "interval_min", "protect")
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
        self.singbox = self.etc + "/singbox.json"
        self.config = self.etc + "/config.json"
        self.guard = self.etc + "/protect.json"
        self.legacy = self.etc + "/legacy"
        self.applied = self.lib + "/applied.json"
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
            "reconnect_port": as_port(r.get("reconnect_port"), "reconnect_port"),
            "interval_min": as_interval(r.get("interval_min", 0)),
            "protect": as_bool(r.get("protect", True), "protect"),
        }
    except Fail as e:
        raise Fail("строка %d: %s" % (rid, e))


def check_all(rows):
    """Межстрочные проверки: id и порты (прокси и триггеры вместе) не повторяются."""
    ids, seen = set(), {}
    for r in rows:
        if r["id"] in ids:
            raise Fail("id %d повторяется" % r["id"])
        ids.add(r["id"])
        for kind, p in (("прокси", r["port"]), ("триггер", r["reconnect_port"])):
            if p in seen:
                raise Fail("порт %d занят дважды: %s и строка %d (%s)" % (p, seen[p], r["id"], kind))
            seen[p] = "строка %d (%s)" % (r["id"], kind)
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
def settings(path=None):
    """{"base_port", "lan_ip", "wan_ip"}; пустой адрес — определить самим.
    base_port — откуда считаются порты (новые строки и «все строки заново»);
    lan_ip — адрес машины в локальной сети: строки для proxyveth ВМ этого хоста;
    wan_ip — внешний адрес для клиентов: экспорт. ext_ip — прежнее имя wan_ip."""
    c = jload(path or P.config, {})
    c = c if isinstance(c, dict) else {}
    try:
        base = as_port(c.get("base_port") or BASE_PORT, "base_port")
    except Fail:
        base = BASE_PORT
    return {"base_port": base, "lan_ip": str(c.get("lan_ip") or ""), "wan_ip": str(c.get("wan_ip") or c.get("ext_ip") or "")}


def save_settings(path=None, **kw):
    """kw: base_port, lan_ip, wan_ip ("" или "auto" — определять самим). → новые настройки."""
    path = path or P.config
    c = jload(path, {})
    c = c if isinstance(c, dict) else {}
    for k, v in kw.items():
        if k == "base_port":
            c[k] = as_port(v, "base_port")
            if c[k] > 65534:
                raise Fail("base_port: %d — нужна пара портов, не выше 65534" % c[k])
        elif k in ("lan_ip", "wan_ip"):
            v = str(v or "").strip()
            c[k] = "" if v.lower() in ("", "auto", "авто") else as_ipv4(v, k)
        else:
            raise Fail("нет настройки «%s»; есть: base_port, lan_ip, wan_ip" % k)
    c.pop("ext_ip", None)                 # прежнее имя wan_ip
    os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
    jsave(path, c, 0o600)
    return settings(path)


def renumber(doc, base, mine=()):
    """Порты всех строк заново, подряд от base в порядке id: прокси base+2i, триггер base+2i+1
    (как в modlink-linux). mine — порты, которые держит своё применённое: для нас они свободны."""
    rows = sorted(doc["proxies"], key=lambda r: r["id"])
    if base + 2 * len(rows) - 1 > 65535:
        raise Fail("от %d не хватит портов на %d строк" % (base, len(rows)))
    busy = [p for p in range(base, base + 2 * len(rows)) if p not in mine and not PROBE[0](p)]
    if busy:
        raise Fail("порт %s уже слушает кто-то другой (ss -ltnp) — возьми другой базовый порт"
                   % ", ".join(map(str, busy[:5])))
    for i, r in enumerate(rows):
        r["port"], r["reconnect_port"] = base + 2 * i, base + 2 * i + 1
    doc["proxies"] = check_all(rows)
    return rows


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
            out.update((r["port"], r["reconnect_port"]))
    return out


def free_port(taken, start=BASE_PORT, pair=False, mine=()):
    """Первый свободный порт от start; pair — чтобы и следующий был свободен (прокси + триггер).
    mine — порты, которые держит своё же (применённая строка): для неё они свободны."""
    ok = (lambda p: p <= 65535 and p not in taken and (p in mine or PROBE[0](p)))
    p = start
    while p <= 65535:
        if ok(p) and (not pair or ok(p + 1)):
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


def resolve_ports(doc, row, port, rport, skip_id=None, mine=()):
    """Проставить порт и триггер: число — как есть, auto — первый свободный (парой, если оба auto)."""
    taken = taken_ports(doc, skip_id)
    pa, ra = str(port).lower() == "auto", str(rport).lower() == "auto"
    if not pa:
        row["port"] = as_port(port, "port")
        taken.add(row["port"])
    if not ra:
        row["reconnect_port"] = as_port(rport, "reconnect_port")
        taken.add(row["reconnect_port"])
    if pa:
        row["port"] = free_port(taken, start=settings()["base_port"], pair=ra, mine=mine)
        taken.add(row["port"])
    if ra:
        nxt = row["port"] + 1
        row["reconnect_port"] = nxt if nxt <= 65535 and nxt not in taken and (nxt in mine or PROBE[0](nxt)) \
            else free_port(taken, start=settings()["base_port"], mine=mine)
    return row


def add(doc, lan_ip, name=None, login=None, password=None, port="auto", modem_ip=None,
        reconnect_port="auto", interval_min=0, enabled=True, protect=True):
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
    resolve_ports(doc, row, port, reconnect_port)
    row = norm(row)
    check_all(doc["proxies"] + [row])
    doc["proxies"].append(row)
    return row


SET_ALIASES = {"interval": "interval_min", "rport": "reconnect_port", "lan": "lan_ip", "modem": "modem_ip",
               "guard": "protect"}


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
        if k in ("port", "reconnect_port"):
            ports[k] = v
        elif k == "password" and v == "gen":
            r[k] = gen_password()
        else:
            r[k] = v
    if ports:
        resolve_ports(doc, r, ports.get("port", r["port"]), ports.get("reconnect_port", r["reconnect_port"]),
                      skip_id=r["id"], mine=mine)
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


# ── конфиг sing-box (формат 1.12+, проверяется sing-box check 1.14) ─────────
def config(rows, extra=None):
    """mixed-inbound на каждую включённую строку, direct-outbound с inet4_bind_address = lan_ip,
    правило «inbound → свой outbound», остальное — reject.

    Всегда, у любой строки: имена резолвятся только в IPv4 (bind — IPv4, а IPv6 ушёл бы мимо
    модема с адресом сервера); IPv6, частные адреса и CGNAT закрыты — иначе через прокси видны
    службы самого сервера и его сеть.
    Защита строки (protect, по умолчанию вкл): сверх того закрыты ЛК и сайты операторов
    (домен узнаётся и по SNI/Host, когда клиент пришёл с голым IP), опасные порты и веб модема.
    Без защиты веб своего модема (modem_ip) открыт — для себя, ген1.
    """
    g = guard_lists(extra)
    en = sorted((x for x in rows if x["enabled"]), key=lambda x: x["port"])
    tag = (lambda r, k: "%s-%d" % (k, r["id"]))
    guarded = [tag(r, "in") for r in en if r["protect"]]
    rules = []
    if guarded:
        rules.append({"inbound": guarded, "action": "sniff"})
    rules += [{"action": "resolve", "strategy": "ipv4_only"},
              {"ip_cidr": ["::/0"], "action": "reject"}]
    for r in en:
        if not r["protect"] and r["modem_ip"]:
            rules.append({"inbound": [tag(r, "in")], "ip_cidr": [r["modem_ip"] + "/32"],
                          "action": "route", "outbound": tag(r, "out")})
    rules += [{"ip_is_private": True, "action": "reject"},
              {"ip_cidr": list(ALWAYS_CIDRS), "action": "reject"}]
    if guarded:
        rules.append(dict({"inbound": guarded, "domain_suffix": g["domains"]},
                          **({"ip_cidr": g["cidrs"]} if g["cidrs"] else {}), action="reject"))
        rules.append({"inbound": guarded, "port": g["ports"], "port_range": g["port_ranges"], "action": "reject"})
    ins, outs = [], []
    for r in en:
        i, o = tag(r, "in"), tag(r, "out")
        ins.append({"type": "mixed", "tag": i, "listen": "0.0.0.0", "listen_port": r["port"],
                    "users": [{"username": r["login"], "password": r["password"]}]})
        outs.append({"type": "direct", "tag": o, "inet4_bind_address": r["lan_ip"]})
        rules.append({"inbound": [i], "action": "route", "outbound": o})
    rules.append({"action": "reject"})
    return {"log": {"level": "warn"},
            "dns": {"servers": [{"type": "local", "tag": "local"}], "strategy": "ipv4_only"},
            "inbounds": ins, "outbounds": outs, "route": {"rules": rules}}


# ── экспорт ────────────────────────────────────────────────────────────────
def export_lines(rows, ip):
    return ["%s:%d:%s:%s\thttp://%s:%d/reconnect" % (ip, r["port"], r["login"], r["password"], ip, r["reconnect_port"])
            for r in sorted(rows, key=lambda x: x["port"]) if r["enabled"]]


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
                          "modem_ip": "192.168.%d.1" % n, "reconnect_port": port + 1,
                          "interval_min": min(max(iv, 0), MAX_INTERVAL)}))
    check_all(rows)
    return rows, str(server.get("ext_ip") or "").strip(), warn
