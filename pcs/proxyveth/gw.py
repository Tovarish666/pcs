"""proxyveth, режим gw — шлюз из прокси без USB и без предела ~40 модемов.

Бывший proxyveth-virt 3.4 (в проде на 90 модемах) в порядке PCS 5. Модем N:

  сервер                          netns pvN
  veth pvN 192.168.N.100/24 ───── eth0 192.168.N.254/24   (+ 192.168.N.1/32 при real ≠ n)
                                  sing-box: tun pvtun0 10.0.N.1/30 → SOCKS5 прокси ген1
                                  маршрут к самой прокси — назад через сервер

  ip rule priority 30000+N from 192.168.N.100 lookup 1000+N → default via 192.168.N.254
  nat POSTROUTING -s 192.168.N.254 -o <основной канал> MASQUERADE    (pcs:proxyveth)

mp.space видит модем на 192.168.N.100 и ходит на 192.168.N.1. Если настоящий
модем за прокси с другим октетом (real ≠ n), 192.168.N.1 в netns занимает
посредник proxyveth-hostfix@N (gw_hostfix.py). DNS клиентов уходит в туннель и
резолвится sing-box по TCP через ту же прокси — имена с IP модема; остальной UDP
режется: прокси ген1 его не возит.

Долгоживущее — юниты proxyveth-gw@N (sing-box) и proxyveth-hostfix@N. Сломанное
чинит apply() (её зовёт таймер sync) с нарастающей паузой и не бросает.
Интерфейс режима — docs/ARCHITECTURE.md §6.
"""
import hashlib
import json
import os
import re
import shlex
import shutil
import socket
import struct
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from pcs.core import net, singbox, util
from pcs.core.util import Fail

PART = "proxyveth"
TAG = net.comment(PART)
LAYOUT = 1                         # устройство модема; сменилось — apply пересоздаёт модемы сам
ROOT = ""                          # корень файловой системы (тесты подменяют)
ETC = "/etc/proxyveth"
CONFIG = ETC + "/config.json"
GWD = ETC + "/gw"                  # GWD/N/{singbox.json,spec.json}; путь знает и gw_hostfix
LIB = "/var/lib/proxyveth"
STATE = LIB + "/gw.json"
HEALTH = LIB + "/gw-health.json"
RUN = "/run/proxyveth"
RETRY = RUN + "/gw-retry.json"     # в /run: после перезагрузки — с чистого листа
UNITD = "/etc/systemd/system"
PY = "/usr/bin/python3"
CODE = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
TUN, INNER = "pvtun0", "eth0"
UNIT_SB, UNIT_HF = "proxyveth-gw@%d", "proxyveth-hostfix@%d"
DEFAULTS = {"dns": "1.1.1.1", "workers": 8}
BACKOFF = (0, 120, 300, 600, 1200, 1800)    # пауза перед повтором после k неудач подряд, с
IP_URL = ("api.ipify.org", "/")
LIGHT = {"sb", "tun", "route", "hostfix", "rule", "table", "nat"}   # чинится без пересоздания

sh = util.sh
log = util.Log(PART)
_HOST = threading.Lock()           # ip rule и iptables сервера — из нескольких потоков


def P(path):
    return ROOT + path


def ns(n):
    return "pv%d" % n


def rt(n):
    return net.table("gw", n)


def prio(n):
    return net.prio("gw", n)


def mdir(n):
    return P("%s/%d" % (GWD, n))


def nsh(name, *args, check=True):
    return sh("ip", "-n", name, *args, check=check)


def nsx(name, *cmd, check=True, input=None):
    return sh("ip", "netns", "exec", name, *cmd, check=check, input=input)


def uplink():
    return net.uplink_route()


def local_octets():
    return net.local_octets()


def resolve(host):
    try:
        return socket.gethostbyname(host)
    except OSError as e:
        raise Fail("не резолвится адрес прокси %s: %s" % (host, e))


def opts(cfg):
    o = dict(DEFAULTS)
    o.update((cfg if isinstance(cfg, dict) else {}).get("gw") or {})
    return o


def norm(n, r):
    """Строка таблицы (pcs.core.table.lint) → строка gw. None — «не трогать»."""
    if r is None:
        return None
    return {"n": int(n), "real": int(r.get("real") or n), "host": str(r["host"]), "port": int(r["port"]),
            "user": str(r["user"]), "pw": str(r["pw"]), "enabled": bool(r.get("enabled", True))}


def spec(row):
    return hashlib.sha256(json.dumps([row["real"], row["host"], int(row["port"]), row["user"], row["pw"]])
                          .encode()).hexdigest()[:16]


def par(items, fn, o=None):
    items = list(items)
    if len(items) <= 1:
        return [fn(i) for i in items]
    with ThreadPoolExecutor(max_workers=max(1, min(int((o or DEFAULTS)["workers"]), len(items)))) as ex:
        return list(ex.map(fn, items))


# ── конфиги и юниты ────────────────────────────────────────────────────────
def sb_config(row, proxy_ip, dns="1.1.1.1"):
    """sing-box в netns модема — формат 1.12+ (§5)."""
    return {
        "log": {"level": "warn", "timestamp": False},
        # DNS клиентов перехватывается на tun и резолвится по TCP через ту же прокси:
        # имена — с IP модема, без утечки через сервер; ipv4_only — у 3proxy нет IPv6,
        # AAAA дали бы долгие таймауты на каждом соединении.
        "dns": {"servers": [{"type": "tcp", "tag": "remote", "server": dns, "detour": "proxy"}],
                "final": "remote", "strategy": "ipv4_only"},
        "inbounds": [{"type": "tun", "tag": "tun-in", "interface_name": TUN, "address": ["10.0.%d.1/30" % row["n"]],
                      "mtu": 1420, "auto_route": False, "strict_route": False, "stack": "system"}],
        "outbounds": [{"type": "socks", "tag": "proxy", "version": "5", "server": proxy_ip,
                       "server_port": int(row["port"]), "username": row["user"], "password": row["pw"]}],
        "route": {"rules": [{"port": 53, "action": "hijack-dns"}], "final": "proxy", "auto_detect_interface": False},
    }


def unit_texts():
    env = "Environment=PYTHONPATH=%s" % CODE
    return {
        "proxyveth-gw@.service": "\n".join([
            "[Unit]",
            "Description=proxyveth gw: модем %i — туннель в SOCKS5",
            "StartLimitIntervalSec=0",
            "[Service]",
            "NetworkNamespacePath=/run/netns/pv%i",
            "# Без D-Bus: иначе sing-box прописывает DNS своего tun в resolved на интерфейс",
            "# сервера с тем же номером, и DNS всего сервера уходит в netns модема.",
            "InaccessiblePaths=-/run/dbus/system_bus_socket",
            env,
            "ExecStart=%s run -c %s/%%i/singbox.json" % (singbox.BIN, GWD),
            "# Маршруты через tun ядро стирает вместе с ним — ставятся при каждом старте.",
            "ExecStartPost=%s -m pcs.proxyveth.gw _routes %%i" % PY,
            "Restart=always",
            "RestartSec=3",
            ""]),
        "proxyveth-hostfix@.service": "\n".join([
            "[Unit]",
            "Description=proxyveth gw: модем %i — веб-морда настоящего модема на 192.168.%i.1",
            "StartLimitIntervalSec=0",
            "[Service]",
            "NetworkNamespacePath=/run/netns/pv%i",
            env,
            "ExecStart=%s -m pcs.proxyveth.gw_hostfix %%i" % PY,
            "Restart=always",
            "RestartSec=3",
            ""]),
    }


def ensure_units():
    changed = False
    for name, text in unit_texts().items():
        path = P(UNITD + "/" + name)
        if util.rd(path) != text.strip():
            os.makedirs(os.path.dirname(path), exist_ok=True)
            util.wr(path, text)
            changed = True
    if changed:
        sh("systemctl", "daemon-reload")
    return changed


def ns_rules(n=None, real=None):
    """iptables внутри netns модема — одним iptables-restore."""
    c = "-m comment --comment %s" % TAG
    # Настоящий модем отвечает на DNS по 192.168.N.1. При real != n этот адрес — посредник
    # Host, поэтому DNS к нему отправляем на настоящий модем: дальше туннель и hijack-dns.
    dnat = ["-A PREROUTING -d 192.168.%d.1/32 -p %s -m %s --dport 53 %s -j DNAT --to-destination 192.168.%d.1:53"
            % (n, p, p, c, real) for p in ("udp", "tcp")] if n and real and real != n else []
    return "\n".join([
        "*filter", ":INPUT ACCEPT [0:0]", ":FORWARD ACCEPT [0:0]", ":OUTPUT ACCEPT [0:0]",
        # DNS в туннель пускаем — его перехватит sing-box. Остальной UDP режем: прокси
        # ген1 его не возит, а клиенты mp.space заливают его тысячами пакетов в секунду.
        "-A OUTPUT -o %s -p udp -m udp --dport 53 %s -j ACCEPT" % (TUN, c),
        "-A OUTPUT -o %s -p udp %s -j DROP" % (TUN, c),
        "-A FORWARD -o %s -p udp -m udp --dport 53 %s -j ACCEPT" % (TUN, c),
        "-A FORWARD -o %s -p udp %s -j DROP" % (TUN, c),
        "-A FORWARD -i %s -o %s %s -j ACCEPT" % (INNER, TUN, c),
        "-A FORWARD -i %s -o %s %s -j ACCEPT" % (TUN, INNER, c),
        "COMMIT",
        "*nat", ":PREROUTING ACCEPT [0:0]", ":INPUT ACCEPT [0:0]", ":OUTPUT ACCEPT [0:0]", ":POSTROUTING ACCEPT [0:0]",
        *dnat,
        "-A POSTROUTING -o %s %s -j MASQUERADE" % (TUN, c),
        "COMMIT", ""])


# Через SOCKS5+tun крупные пакеты иначе теряются молча. Первым в FORWARD: ниже ACCEPT
# до него бы не дошло. Модуля TCPMSS нет — работаем без него.
MSS = ["FORWARD", "1", "-o", TUN, "-p", "tcp", "-m", "tcp", "--tcp-flags", "SYN,RST", "SYN",
       "-m", "comment", "--comment", TAG, "-j", "TCPMSS", "--clamp-mss-to-pmtu"]


def nat_rule(n, dev):
    return ["POSTROUTING", "-s", "192.168.%d.254/32" % n, "-o", dev, "-m", "comment", "--comment", TAG,
            "-j", "MASQUERADE"]


def opt(t, key):
    i = t.index(key) if key in t else -1
    return t[i + 1] if 0 <= i < len(t) - 1 else None


def tagged(line, tag=TAG):
    """Строка iptables -S → токены, если правило помечено tag (iptables берёт его в кавычки)."""
    try:
        t = shlex.split(line)
    except ValueError:
        return None
    return t if t[:1] == ["-A"] and opt(t, "--comment") == tag else None


def nat_rules():
    """Свои правила nat POSTROUTING сервера: [(n или None, интерфейс, токены)]."""
    out = []
    for line in sh("iptables", "-w", "5", "-t", "nat", "-S", "POSTROUTING", check=False).stdout.splitlines():
        t = tagged(line)
        if t is None:
            continue
        m = re.fullmatch(r"192\.168\.(\d+)\.254(?:/32)?", opt(t, "-s") or "")
        out.append((int(m.group(1)) if m else None, opt(t, "-o"), t))
    return out


def ipt_del(t, table="nat"):
    sh("iptables", "-w", "5", "-t", table, "-D", *t[1:], check=False)


# ── снимок системы ─────────────────────────────────────────────────────────
def units_state():
    out = {}
    r = sh("systemctl", "list-units", "--all", "--plain", "--no-legend", "--full",
           "proxyveth-gw@*", "proxyveth-hostfix@*", check=False)
    for line in r.stdout.splitlines():
        f = [x for x in line.split() if x != "●"]
        if len(f) >= 3:
            out[f[0]] = f[2]
    return out


def active(s, unit):
    return s["units"].get(unit + ".service") == "active"


def snap(full=True):
    """Сторона сервера — несколькими командами на всех модемов сразу."""
    s = {"netns": {ln.split()[0] for ln in sh("ip", "netns", "list", check=False).stdout.splitlines() if ln.strip()},
         "veth": {ln.split(":")[1].strip().split("@")[0]
                  for ln in sh("ip", "-o", "link", "show", "type", "veth", check=False).stdout.splitlines()
                  if ln.count(":") >= 2}}
    if not full:
        return s
    addr = {}
    for ln in sh("ip", "-4", "-o", "addr", "show", check=False).stdout.splitlines():
        f = ln.split()
        if len(f) >= 4 and f[2] == "inet":
            addr.setdefault(f[1].split("@")[0], []).append(f[3])
    s.update(addr=addr, rules=sh("ip", "-4", "rule", "show", check=False).stdout,
             routes=sh("ip", "-4", "route", "show", "table", "all", check=False).stdout,
             nat=nat_rules(), units=units_state(), uplink=uplink())
    return s


def running():
    """Номера своих модемов — для общей команды proxyveth (интерфейс режима)."""
    return sorted(own_modems(snap(full=False), load_state()))


def own_modems(s, st=None):
    """Свои модемы: netns pvN, у которых на сервере veth pvN или которые создавали мы.
    netns pvN без того и другого — не наш (у режима usb те же имена)."""
    known = set((st or {}).get("modems", {}))
    out = set()
    for name in s["netns"] | s["veth"]:
        m = re.fullmatch(r"pv(\d+)", name)
        if m and (name in s["veth"] or m.group(1) in known):
            out.add(int(m.group(1)))
    return out


def legacy_ns(s):
    """Модемы, которые ещё держит proxyveth-virt 3.4 (netns ns_N)."""
    return {int(m.group(1)) for m in (re.fullmatch(r"ns_(\d+)", x) for x in s["netns"]) if m}


def inner_routes(n):
    return nsh(ns(n), "-4", "route", "show", check=False).stdout


def local(n, row, s, inner=None):
    """Поломки своей стороны модема n: [(код, текст)]. Пусто — цела."""
    name = ns(n)
    if name not in s["netns"]:
        return [("absent", "модема нет")]
    if inner is None:
        inner = inner_routes(n)
    bad = []
    if "192.168.%d.100/24" % n not in s["addr"].get(name, []):
        bad.append(("veth", "у %s нет 192.168.%d.100" % (name, n)))
    if not re.search(r"^192\.168\.%d\.0/24 dev %s\b" % (n, INNER), inner, re.M):
        bad.append(("veth", "в netns у %s нет 192.168.%d.254" % (INNER, n)))
    if not active(s, UNIT_SB % n):
        bad.append(("sb", "sing-box не работает (journalctl -u %s)" % (UNIT_SB % n)))
    elif not re.search(r"\bdev %s\b.*\bsrc 10\.0\.%d\.1\b" % (TUN, n), inner):
        bad.append(("tun", "нет туннеля %s" % TUN))
    if not re.search(r"^default dev %s\b" % TUN, inner, re.M):
        bad.append(("route", "нет маршрута в туннель"))
    if row and row["real"] != n and not active(s, UNIT_HF % n):
        bad.append(("hostfix", "посредник 192.168.%d.1 → 192.168.%d.1 не работает" % (n, row["real"])))
    if not re.search(r"^%d:\s+from 192\.168\.%d\.100 lookup \S+" % (prio(n), n), s["rules"], re.M):
        bad.append(("rule", "нет правила маршрутизации для 192.168.%d.100" % n))
    if not re.search(r"^default via 192\.168\.%d\.254 dev %s table \S+" % (n, name), s["routes"], re.M):
        bad.append(("table", "пуста таблица маршрутов %d" % rt(n)))
    up = s["uplink"]
    if up and not any(k == n and o == up[1] for k, o, _ in s["nat"]):
        bad.append(("nat", "нет NAT к прокси через %s" % up[1]))
    return bad


# ── подъём и снос одного модема ────────────────────────────────────────────
def server_side(n, up):
    """Маршрутизация и NAT модема на сервере. Идемпотентно: что есть — не трогаем."""
    name, t, p = ns(n), str(rt(n)), str(prio(n))
    with _HOST:
        # Приоритет сверяем по выводу: у старого proxyveth-virt правило с тем же from и
        # lookup, но другим приоритетом — его за своё не считать.
        have = sh("ip", "-4", "rule", "show", "priority", p, check=False).stdout
        if not re.search(r"^%s:\s+from 192\.168\.%d\.100 " % (p, n), have, re.M):
            for _ in range(10):
                if sh("ip", "rule", "del", "priority", p, check=False).returncode != 0:
                    break
            sh("ip", "rule", "add", "priority", p, "from", "192.168.%d.100" % n, "lookup", t)
        sh("ip", "route", "replace", "default", "via", "192.168.%d.254" % n, "dev", name, "table", t)
        for k, o, tok in nat_rules():
            if k == n and o != up[1]:
                ipt_del(tok)                           # основной канал сменился
        want = nat_rule(n, up[1])
        if sh("iptables", "-w", "5", "-t", "nat", "-C", *want, check=False).returncode != 0:
            sh("iptables", "-w", "5", "-t", "nat", "-A", *want)


def write_files(row, ip, o):
    n = row["n"]
    d = mdir(n)
    os.makedirs(d, exist_ok=True)
    os.chmod(d, 0o700)
    util.wr(d + "/singbox.json", json.dumps(sb_config(row, ip, o["dns"]), indent=1), 0o600)
    util.wr(d + "/spec.json", json.dumps({"n": n, "real": row["real"], "proxy_ip": ip}), 0o600)
    nd = P("/etc/netns/" + ns(n))
    os.makedirs(nd, exist_ok=True)
    # Для `ip netns exec pvN …` руками: DNS уходит в туннель, отвечает sing-box.
    util.wr(nd + "/resolv.conf", "# proxyveth gw: резолвит sing-box через прокси модема\n"
                                 "nameserver %s\noptions timeout:3 attempts:2\n" % o["dns"])


def journal(unit, lines=3):
    r = sh("journalctl", "-u", unit, "-n", str(lines), "--no-pager", "-o", "cat", check=False)
    return " | ".join(x.strip() for x in r.stdout.splitlines() if x.strip())[-300:]


def create(row, up, o):
    """Модем с нуля. Упало на любом шаге — снести обратно, полуживого не оставлять. → адрес прокси."""
    n, real = row["n"], row["real"]
    name = ns(n)
    ip = resolve(row["host"])
    remove(n, quiet=True)
    write_files(row, ip, o)
    step = "netns"
    try:
        sh("ip", "netns", "add", name)
        nsh(name, "link", "set", "lo", "up")
        step = "veth"
        sh("ip", "link", "add", name, "type", "veth", "peer", "name", INNER, "netns", name)
        # rp_filter при маршрутизации по источнику тихо режет обратный трафик —
        # только на своих интерфейсах; netns модема целиком наш.
        sh("sysctl", "-qw", "net.ipv4.conf.%s.rp_filter=0" % name)
        sh("ip", "addr", "add", "192.168.%d.100/24" % n, "dev", name)
        sh("ip", "link", "set", name, "up")
        nsx(name, "sysctl", "-qw", "net.ipv4.ip_forward=1", "net.ipv4.conf.all.rp_filter=0",
            "net.ipv4.conf.default.rp_filter=0", "net.ipv4.conf.%s.rp_filter=0" % INNER)
        nsh(name, "addr", "add", "192.168.%d.254/24" % n, "dev", INNER)
        if real != n:
            nsh(name, "addr", "add", "192.168.%d.1/32" % n, "dev", INNER)
        nsh(name, "link", "set", INNER, "up")
        # Единственное исключение из туннеля — сама прокси, иначе sing-box завернёт
        # собственный аплинк сам в себя. Через сервер, с явным адресом для NAT.
        nsh(name, "route", "add", "%s/32" % ip, "via", "192.168.%d.100" % n, "dev", INNER,
            "src", "192.168.%d.254" % n)
        step = "iptables"
        nsx(name, "iptables-restore", input=ns_rules(n, real))
        nsx(name, "iptables", "-w", "5", "-I", *MSS, check=False)
        step = "маршрутизация сервера"
        server_side(n, up)
        step = "sing-box"
        sh("systemctl", "start", UNIT_SB % n, timeout=90)
        if real != n:
            step = "посредник"
            sh("systemctl", "start", UNIT_HF % n, timeout=30)
        step = "проверка"
        r = inner_routes(n)
        if not re.search(r"^default dev %s\b" % TUN, r, re.M) or "src 10.0.%d.1" % n not in r:
            raise Fail("туннель %s без маршрута" % TUN)
    except Fail as e:
        why = str(e)
        if step in ("sing-box", "проверка"):
            tail = journal(UNIT_SB % n)
            why += (" — sing-box: " + tail) if tail else ""
        remove(n, quiet=True)
        raise Fail("модем %d, шаг «%s»: %s" % (n, step, why))
    return ip


def remove(n, quiet=False):
    name = ns(n)
    units = (UNIT_HF % n, UNIT_SB % n)
    sh("systemctl", "stop", *units, check=False, timeout=60)
    sh("systemctl", "reset-failed", *units, check=False)
    sh("ip", "netns", "del", name, check=False)
    sh("ip", "link", "del", name, check=False)          # veth без netns — хвост
    with _HOST:
        for _ in range(10):
            if sh("ip", "rule", "del", "priority", str(prio(n)), check=False).returncode != 0:
                break
        sh("ip", "route", "flush", "table", str(rt(n)), check=False)
        for k, _o, t in nat_rules():
            if k == n:
                ipt_del(t)
    shutil.rmtree(P("/etc/netns/" + name), ignore_errors=True)
    shutil.rmtree(mdir(n), ignore_errors=True)
    if not quiet:
        log("модем %d снят" % n)


def fix(n, row, bad, up):
    """Починить без пересоздания (интерфейс модема для mp.space не мигает). → удалось ли."""
    codes = {c for c, _ in bad}
    if codes - LIGHT:
        return False
    try:
        if codes & {"sb", "tun", "route"}:
            sh("systemctl", "restart", UNIT_SB % n, timeout=90)
        if "hostfix" in codes:
            sh("systemctl", "restart", UNIT_HF % n, timeout=30)
        if codes & {"rule", "table", "nat"}:
            server_side(n, up)
    except Fail:
        return False
    if local(n, row, snap()):
        return False
    log("модем %d: починил — %s" % (n, "; ".join(t for _, t in bad)))
    return True


def refresh(n, row, m, o):
    """Конфиг sing-box поменялся (новая версия кода, другой DNS) — переписать и перезапустить."""
    ip = m.get("proxy_ip") or resolve(row["host"])
    if util.rd(mdir(n) + "/singbox.json") != json.dumps(sb_config(row, ip, o["dns"]), indent=1):
        write_files(row, ip, o)
        sh("systemctl", "restart", UNIT_SB % n, check=False, timeout=90)
        log("модем %d: конфиг sing-box обновлён" % n)


def sweep(alive):
    """Хвосты модемов, которых больше нет: правила, NAT, юниты. Только своё — по пометке и номерам."""
    lo, hi = prio(1), prio(254)
    with _HOST:
        for k, _o, t in nat_rules():
            if k not in alive:
                ipt_del(t)
        for line in sh("ip", "-4", "rule", "show", check=False).stdout.splitlines():
            m = re.match(r"(\d+):", line)
            if m and lo <= int(m.group(1)) <= hi and int(m.group(1)) - prio(0) not in alive:
                sh("ip", "rule", "del", "priority", m.group(1), check=False)
    for unit, state in units_state().items():
        m = re.fullmatch(r"proxyveth-(?:gw|hostfix)@(\d+)\.service", unit)
        if m and int(m.group(1)) not in alive and state != "inactive":
            sh("systemctl", "stop", unit, check=False, timeout=60)
            sh("systemctl", "reset-failed", unit, check=False)


# ── состояние ──────────────────────────────────────────────────────────────
def load_state():
    st = util.jload(P(STATE), {})
    st = st if isinstance(st, dict) else {}
    for k, v in (("desired", {}), ("modems", {}), ("down", [])):
        st.setdefault(k, v)
    return st


def save_state(st):
    util.jsave(P(STATE), st)


def remember(st, row, ip):
    st["modems"][str(row["n"])] = {"spec": spec(row), "layout": LAYOUT, "proxy_ip": ip, "created": int(time.time())}


def rebooting(n):
    try:
        return float(util.rd(P("%s/rebooting-%d" % (RUN, n)), "0")) > time.time()
    except ValueError:
        return False


def legacy():
    """Есть ли на машине proxyveth-virt 3.4 (для cli: какой режим предложить)."""
    from . import gw_legacy
    return gw_legacy.find() is not None


# ── интерфейс режима (§6) ──────────────────────────────────────────────────
def setup(cfg):
    """Подготовить сервер под gw; если стоит proxyveth-virt 3.4 — перенести его модемы.
    → None или отчёт переноса {"source", "moved", "removed", "failed", "left", "backup"}."""
    cfg = cfg if cfg is not None else {}
    if not os.path.exists(P("/dev/net/tun")):
        raise Fail("нет /dev/net/tun — sing-box не создаст туннель (modprobe tun)")
    for d in (ETC, GWD, LIB, RUN):
        os.makedirs(P(d), exist_ok=True)
        os.chmod(P(d), 0o700)
    net.ensure_base(log)
    missing = [c for c in ("ip", "iptables", "iptables-restore") if not shutil.which(c)]
    if missing:
        log("ставлю iproute2 и iptables")
        sh("apt-get", "-o", "DPkg::Lock::Timeout=600", "-y", "-qq", "install", "iproute2", "iptables",
           timeout=1800, env=dict(os.environ, DEBIAN_FRONTEND="noninteractive"))
    if singbox.install():
        log("sing-box %s поставлен в %s" % (singbox.VERSION, singbox.BIN))
    ensure_units()
    from . import gw_legacy
    return gw_legacy.migrate(cfg, log)


def ensure(rows, st, o, force=False, only=None):
    """Поднять и починить модемы rows (только enabled; only — подмножество). → отчёт как у apply."""
    res = {"created": [], "recreated": [], "removed": [], "failed": {}}
    s = snap()
    up = s["uplink"]
    if not up:
        raise Fail("не нашёл основной канал сервера (маршрут по умолчанию не через модем) — модемам не к чему подключиться")
    now, old, taken = own_modems(s, st), legacy_ns(s), local_octets()
    retry = util.jload(P(RETRY), {})
    todo = {}
    for n in sorted(rows):
        row = rows[n]
        if not row["enabled"] or (only is not None and n not in only) or n in st["down"]:
            continue
        if n in old:
            res["failed"][n] = "ещё работает proxyveth-virt (ns_%d) — перенесёт proxyveth setup" % n
            continue
        if n in taken:
            res["failed"][n] = "192.168.%d.0/24 занята сетью самого сервера" % n
            continue
        m = st["modems"].get(str(n), {})
        if n not in now:
            todo[n] = ("created", "")
        elif m.get("spec") != spec(row) or m.get("layout") != LAYOUT:
            todo[n] = ("recreated", "поменялась строка таблицы" if m.get("layout") == LAYOUT else "новая схема модема")
        else:
            try:
                ip = resolve(row["host"])
            except Fail:
                ip = m.get("proxy_ip")
            bad = local(n, row, s)
            if ip != m.get("proxy_ip"):
                todo[n] = ("recreated", "адрес прокси сменился: %s → %s" % (m.get("proxy_ip"), ip))
            elif bad and not fix(n, row, bad, up):
                todo[n] = ("recreated", "; ".join(t for _, t in bad))
            elif not bad:
                refresh(n, row, m, o)
    t = time.time()
    for n in list(todo):
        r = retry.get(str(n))
        if r and r.get("next", 0) > t and not force:
            res["failed"][n] = "%s; повтор после %d неудач подряд — в %s" % (
                r.get("why", "?"), r["fails"], time.strftime("%H:%M", time.localtime(r["next"])))
            del todo[n]
    for n, (kind, why) in sorted(todo.items()):
        if why:
            log("модем %d: %s — пересоздаю" % (n, why))

    def one(n):
        try:
            return create(rows[n], up, o), None
        except Fail as e:
            return None, str(e)

    order = sorted(todo)
    for n, (ip, err) in zip(order, par(order, one, o)):
        if err:
            r = retry.get(str(n)) or {"fails": 0}
            r["fails"] += 1
            r.update(next=t + BACKOFF[min(r["fails"], len(BACKOFF) - 1)], why=err)
            retry[str(n)] = r
            res["failed"][n] = err
            st["modems"].pop(str(n), None)
            log("✗ %s" % err)
        else:
            retry.pop(str(n), None)
            remember(st, rows[n], ip)
            res[todo[n][0]].append(n)
    try:
        util.jsave(P(RETRY), retry)
    except OSError:
        pass
    return res


def apply(desired, cfg, force=False):
    """Привести модемы к таблице. desired — {n: строка lint | None («не трогать»)}."""
    o = opts(cfg)
    ensure_units()
    st = load_state()
    rows, keep = {}, set()
    for k, r in (desired or {}).items():
        if r is None:
            keep.add(int(k))
        else:
            rows[int(k)] = norm(k, r)
    s = snap(full=False)
    now = own_modems(s, st)
    want = {n for n, r in rows.items() if r["enabled"]}
    gone = sorted(n for n in now if n not in want and n not in keep)
    held = []
    if len(gone) > 1 and len(gone) * 2 > len(now) and not force:
        log("✗ таблица требует снести %d из %d модемов — похоже на ошибку в таблице; снести: --force"
            % (len(gone), len(now)))
        held, gone = gone, []
    par(gone, remove, o)
    for n in gone:
        st["modems"].pop(str(n), None)
    res = ensure(rows, st, o, force=force)
    res["removed"] = gone
    if held:
        res["held"] = held
    sweep(own_modems(snap(full=False), st))
    prev = st["desired"]
    st["desired"] = {str(n): r for n, r in rows.items()}
    for n in keep:
        if str(n) in prev:
            st["desired"][str(n)] = prev[str(n)]
    save_state(st)
    return res


def up(n=None):
    """Поднять модем n (None — все) по последней таблице. Ручное «down» снимается."""
    st = load_state()
    rows = {int(k): r for k, r in st["desired"].items()}
    if n is not None:
        if n not in rows:
            raise Fail("модема %d нет в таблице — proxyveth sync" % n)
        if not rows[n]["enabled"]:
            raise Fail("модем %d выключен в таблице (колонка enabled)" % n)
    st["down"] = [] if n is None else [x for x in st["down"] if x != n]
    res = ensure(rows, st, opts(util.jload(P(CONFIG), {})), force=True, only=None if n is None else {n})
    save_state(st)
    if res["failed"]:
        raise Fail("не поднялись: %s" % "; ".join("%d — %s" % kv for kv in sorted(res["failed"].items())))


def down(n=None):
    """Снять модем n (None — все). Остаётся снятым, пока не up: таймер его не вернёт."""
    st = load_state()
    mine = own_modems(snap(full=False), st)
    targets = sorted(mine) if n is None else [n]
    marks = set(st["down"]) | set(targets)
    if n is None:
        marks |= {int(k) for k in st["desired"]}
    st["down"] = sorted(marks)
    save_state(st)
    par(targets, lambda k: remove(k, quiet=True))
    for k in targets:
        st["modems"].pop(str(k), None)
    save_state(st)
    log("снято модемов: %d (вернуть: proxyveth up %s)" % (len([k for k in targets if k in mine]),
                                                           "all" if n is None else n))


def ext_ip(n, timeout=10):
    """Внешний IP модема тем же путём, что у клиентов: с 192.168.N.100 через netns и туннель."""
    host, path = IP_URL
    try:
        addr = socket.gethostbyname(host)
    except OSError:
        return ""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    data = b""
    try:
        s.settimeout(timeout)
        s.bind(("192.168.%d.100" % n, 0))
        s.connect((addr, 80))
        s.sendall(("GET %s HTTP/1.1\r\nHost: %s\r\nUser-Agent: pcs\r\nConnection: close\r\n\r\n"
                   % (path, host)).encode())
        while len(data) < 65536:
            c = s.recv(4096)
            if not c:
                break
            data += c
    except OSError:
        return ""
    finally:
        s.close()
    from .gw_probe import ipv4
    return ipv4(data.partition(b"\r\n\r\n")[2].decode("latin1"))


def web(n, timeout=8):
    """Веб-морда по адресу, который знает mp.space: 192.168.N.1 с 192.168.N.100. → (код, тело)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    data = b""
    try:
        s.settimeout(timeout)
        s.bind(("192.168.%d.100" % n, 0))
        s.connect(("192.168.%d.1" % n, 80))
        s.sendall(("GET /api/webserver/SesTokInfo HTTP/1.1\r\nHost: 192.168.%d.1\r\nConnection: close\r\n\r\n"
                   % n).encode())
        while len(data) < 65536:
            c = s.recv(4096)
            if not c:
                break
            data += c
    except OSError as e:
        return 0, str(e)
    finally:
        s.close()
    head, _, body = data.partition(b"\r\n\r\n")
    m = re.match(rb"HTTP/\d\.\d (\d+)", head)
    return (int(m.group(1)) if m else 0), body.decode("utf-8", "replace")


def dns_probe(n, name="api.ipify.org", server="1.1.1.1", timeout=5):
    """DNS клиента модема: UDP с 192.168.N.100 уходит в туннель, отвечает sing-box."""
    q = struct.pack(">HHHHHH", 0x5043, 0x0100, 1, 0, 0, 0) + b"".join(
        bytes([len(p)]) + p.encode() for p in name.split(".")) + b"\x00\x00\x01\x00\x01"
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.settimeout(timeout)
        s.bind(("192.168.%d.100" % n, 0))
        s.sendto(q, (server, 53))
        r = s.recv(1500)
    except OSError:
        return False
    finally:
        s.close()
    return len(r) >= 12 and r[:2] == q[:2] and r[3] & 0x0F == 0 and struct.unpack(">H", r[6:8])[0] > 0


def judge(n, row, mine, old, s, inner, st):
    """Состояние модема без сети → (state, problems)."""
    if row and not row["enabled"]:
        return "disabled", (["выключен в таблице, но ещё работает — снимет sync"] if n in mine else [])
    if n not in mine:
        if n in st["down"]:
            return "absent", ["снят вручную — вернуть: proxyveth up %d" % n]
        if n in old:
            return "absent", ["ещё работает proxyveth-virt (ns_%d) — перенесёт proxyveth setup" % n]
        return "absent", ["модема нет"]
    if rebooting(n):
        return "rebooting", ["настоящий модем перезагружается"]
    bad = local(n, row, s, inner)
    if bad:
        return "broken", [t for _, t in bad]
    return "ok", ([] if row else ["нет в таблице"])


def status(wan=False):
    """→ [Row] (§6). wan — внешний IP через каждый модем; без него апстрим — из последней проверки."""
    from . import gw_probe
    st = load_state()
    rows = {int(k): r for k, r in st["desired"].items()}
    s = snap()
    mine, old = own_modems(s, st), legacy_ns(s)
    inner = dict(zip(sorted(mine), par(sorted(mine), inner_routes, {"workers": 16})))
    found = {n: judge(n, rows.get(n), mine, old, s, inner.get(n), st) for n in sorted(set(rows) | mine)}
    hp = util.jload(P(HEALTH), {})
    now = int(time.time())
    if wan:
        cand = [n for n, (state, _) in found.items() if state == "ok" and n in rows]
        ips = dict(zip(cand, par(cand, ext_ip, {"workers": 16})))
        dead = [n for n in cand if not ips[n]]
        why = dict(zip(dead, par(dead, lambda n: gw_probe.quick(rows[n]), {"workers": 8})))
        for n in cand:
            up_ = ("ok", []) if ips[n] else ("warn", [why[n]]) if why[n] else (
                "broken", ["через туннель нет выхода, а прокси напрямую работает — дело в нашей стороне "
                           "(маршрут к прокси, NAT)"])
            hp.setdefault(str(n), {}).update(t=now, up=up_[0], up_problems=up_[1], ext_ip=ips[n])
    out = []
    for n, (state, probs) in found.items():
        row, h = rows.get(n), hp.setdefault(str(n), {})
        made = st["modems"].get(str(n), {}).get("created", 0)
        ip = ""
        # Апстрим — из последней проверки --wan, если она была после создания модема;
        # «сломан» из неё помним полчаса, дальше — неизвестно, то есть ok по своей стороне.
        if state == "ok" and h.get("t", 0) >= made:
            ip = h.get("ext_ip", "")
            if h.get("up") in ("warn", "broken") and (wan or h["t"] > now - 1800):
                state = h["up"]
                probs = h.get("up_problems", []) + ([] if wan else [
                    "проверено в %s (status --wan)" % time.strftime("%H:%M", time.localtime(h["t"]))])
        if h.get("state") != state:
            h.update(state=state, since=now)
        out.append({"n": n, "real": row["real"] if row else None,
                    "proxy": "%s:%s" % (row["host"], row["port"]) if row else "",
                    "state": state, "problems": probs, "iface": ns(n), "ext_ip": ip, "since": h["since"]})
    try:
        util.jsave(P(HEALTH), {str(n): hp[str(n)] for n in found})
    except OSError:
        pass
    return out


def diag(n):
    """Своя сторона → прокси → логин → модем → SIM → интернет → путь клиента через туннель."""
    from . import gw_probe
    st = load_state()
    raw = st["desired"].get(str(n))
    if not raw:
        raise Fail("модема %d нет в таблице — proxyveth sync, proxyveth table show" % n)
    row = norm(n, raw)
    steps = []

    def step(name, ok, text):
        steps.append({"name": name, "ok": bool(ok), "text": text})
        return ok

    s = snap()
    mine = own_modems(s, st)
    if not row["enabled"] or n not in mine:
        state, probs = judge(n, row, mine, legacy_ns(s), s, None, st)
        step("своя сторона", False, "; ".join(probs) or "выключен в таблице")
        return {"n": n, "steps": steps, "verdict": state}
    bad = local(n, row, s)
    step("своя сторона", not bad, "; ".join(t for _, t in bad) if bad else
         "netns %s, veth, sing-box, маршруты, NAT — в порядке" % ns(n))
    chain, where, ip_direct = gw_probe.chain(row)
    for name, ok, text in chain:
        step(name, ok, text)
    verdict = "broken" if bad else "warn" if where else "ok"
    if not bad:
        ip = ext_ip(n)
        if step("через туннель", ip, ("внешний IP %s" % ip + ("" if not ip_direct or ip == ip_direct else
                                       " — напрямую через прокси %s: трафик идёт мимо?" % ip_direct))
                if ip else "с 192.168.%d.100 наружу не выходит" % n):
            step("DNS через туннель", dns_probe(n), "резолвит sing-box через прокси")
        elif not where:
            verdict = "broken"
        code, body = web(n)
        step("веб-морда 192.168.%d.1" % n, code == 200 and "<SesInfo>" in body,
             "HTTP %s" % code + ("" if row["real"] == n else " (через посредника на 192.168.%d.1)" % row["real"])
             + (" — 307: посредник не в цепи" if code == 307 else ""))
    return {"n": n, "steps": steps, "verdict": verdict}


def teardown():
    """Снять всё своё: модемы, правила, юниты, конфиги. config.json и копию таблицы не трогаем (они cli)."""
    st = load_state()
    mine = own_modems(snap(full=False), st) | {
        int(m.group(1)) for m in (re.fullmatch(r"proxyveth-(?:gw|hostfix)@(\d+)\.service", u) for u in units_state()) if m}
    par(sorted(mine), lambda k: remove(k, quiet=True))
    sweep(set())
    for name in unit_texts():
        try:
            os.unlink(P(UNITD + "/" + name))
        except OSError:
            pass
    sh("systemctl", "daemon-reload", check=False)
    shutil.rmtree(P(GWD), ignore_errors=True)
    for f in (STATE, HEALTH, RETRY):
        try:
            os.unlink(P(f))
        except OSError:
            pass
    log("режим gw снят, модемов было: %d" % len(mine))


def doctor():
    from . import gw_legacy
    out = []

    def chk(name, ok, text=""):
        out.append({"name": name, "ok": bool(ok), "text": text})

    chk("/dev/net/tun", os.path.exists(P("/dev/net/tun")), "" if os.path.exists(P("/dev/net/tun")) else "modprobe tun")
    sb = singbox.installed()
    chk("sing-box %s" % singbox.VERSION, sb, singbox.BIN if sb else "нет — proxyveth setup")
    chk("ip_forward", util.rd(P("/proc/sys/net/ipv4/ip_forward")) == "1", "net.ipv4.ip_forward (90-pcs.conf)")
    chk("networkd не стирает правила", os.path.exists(P(net.NETWORKD)), net.NETWORKD)
    chk("iptables nat", sh("iptables", "-w", "5", "-t", "nat", "-S", "POSTROUTING", check=False).returncode == 0)
    pol = sh("iptables", "-w", "5", "-S", "FORWARD", check=False).stdout.splitlines()[:1]
    chk("FORWARD пропускает", pol in ([], ["-P FORWARD ACCEPT"]),
        "" if pol in ([], ["-P FORWARD ACCEPT"]) else "%s — sing-box модемов не дойдёт до прокси (ufw? docker?)" % pol[0])
    up_ = uplink()
    chk("основной канал", up_, ("via %s dev %s" % up_) if up_ else "нет маршрута по умолчанию не через модем")
    bad_units = [k for k, v in unit_texts().items() if util.rd(P(UNITD + "/" + k)) != v.strip()]
    chk("юниты proxyveth-gw@, proxyveth-hostfix@", not bad_units, ", ".join(bad_units) and
        "не те или нет: %s — proxyveth setup" % ", ".join(bad_units))
    timer = sh("systemctl", "is-enabled", "proxyveth-sync.timer", check=False).stdout.strip()
    chk("таймер sync", timer == "enabled", timer or "нет")
    old = gw_legacy.find()
    chk("следов proxyveth-virt 3.4 нет", not old, gw_legacy.describe(old) if old else "")
    taken = set(int(k) for k, r in load_state()["desired"].items() if r.get("enabled", True)) & local_octets()
    chk("номера модемов не заняты сетью сервера", not taken, ", ".join(map(str, sorted(taken))))
    return out


# ── внутреннее: юниты зовут модуль сами ────────────────────────────────────
def routes(n):
    """ExecStartPost proxyveth-gw@N (внутри netns): дождаться tun и поставить маршруты."""
    real = int(util.jload(P("%s/%d/spec.json" % (GWD, n)), {}).get("real") or n)
    for _ in range(200):
        if "inet 10.0.%d.1/" % n in sh("ip", "-4", "-o", "addr", "show", "dev", TUN, check=False).stdout:
            break
        time.sleep(0.1)
    else:
        raise Fail("модем %d: %s не появился за 20 с" % (n, TUN))
    sh("ip", "route", "replace", "default", "dev", TUN)
    # Веб-морда модема — реальный адрес: именно он уйдёт в SOCKS5 CONNECT.
    sh("ip", "route", "replace", "192.168.%d.1/32" % real, "dev", TUN)


def main(argv):
    try:
        if len(argv) == 2 and argv[0] == "_routes" and argv[1].isdigit():
            routes(int(argv[1]))
            return 0
    except Fail as e:
        print("✗ %s" % e, file=sys.stderr)
        return 1
    print("✗ это модуль режима gw — команда: proxyveth", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
