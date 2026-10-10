"""Маршрутизация на сервере — одна схема на все части (docs/ARCHITECTURE.md, «Сеть»).

Таблицы и приоритеты ip rule у каждой части свои и не пересекаются:

  часть                         таблицы          приоритет ip rule     пометка iptables
  proxyveth usb, своя сторона   100 + N % 200    авто (как mp.space)   —
  proxyveth gw                  1000 + N         30000 + N             pcs:proxyveth
  hivelink                      2000 + N         31000 + N             pcs:hivelink
  core: адреса прокси           90               32765                 —

mp.space нумерует свои таблицы так же, как proxyveth usb (100 + N % 200, имена
modemK), — поэтому на сервере с USB-модемами N и N+200 не уживаются.
Правила iptables — только с -m comment --comment pcs:<часть>; уборка снимает
только помеченное своей частью.
"""
import os
import re
import socket

from .util import rd, sh, wr

SYSCTL = "/etc/sysctl.d/90-pcs.conf"
SYSCTL_TEXT = ("# PCS: упало ядро — перезагрузка через 10 с, а не висеть до ручного reset\n"
               "kernel.panic = 10\nkernel.panic_on_oops = 1\nnet.ipv4.ip_forward = 1\n")
NETWORKD = "/etc/systemd/networkd.conf.d/10-pcs.conf"
NETWORKD_TEXT = ("# PCS: networkd при перезапуске (любой netplan apply) не стирает чужие\n"
                 "# правила и маршруты — то есть маршрутизацию модемов и mp.space\n"
                 "[Network]\nManageForeignRoutingPolicyRules=no\nManageForeignRoutes=no\n")


def ensure_base(log=print):
    """Общая база любой машины с модемами: sysctl PCS и networkd без уборки чужого.
    Идемпотентно; networkd перезапускается, только если drop-in поменялся."""
    if rd(SYSCTL) != SYSCTL_TEXT.strip():
        os.makedirs(os.path.dirname(SYSCTL), exist_ok=True)
        wr(SYSCTL, SYSCTL_TEXT)
        sh("sysctl", "-q", "-p", SYSCTL, check=False)
    if rd(NETWORKD) != NETWORKD_TEXT.strip():
        os.makedirs(os.path.dirname(NETWORKD), exist_ok=True)
        wr(NETWORKD, NETWORKD_TEXT)
        sh("systemctl", "try-restart", "systemd-networkd", check=False)
        log("systemd-networkd больше не стирает маршрутизацию модемов")

PIN_TABLE, PIN_PRIO = 90, 32765
RANGES = {"gw": (1000, 30000), "hivelink": (2000, 31000)}


def table(part, n):
    """Номер таблицы маршрутов модема N для части part ('usb', 'gw', 'hivelink')."""
    if part == "usb":
        return 100 + n % 200
    return RANGES[part][0] + n


def prio(part, n):
    """Приоритет ip rule модема N. Для 'usb' — None: ядро ставит само, как у mp.space."""
    if part == "usb":
        return None
    return RANGES[part][1] + n


def comment(part):
    """Пометка своих правил iptables: -m comment --comment <это>."""
    return "pcs:%s" % part


def local_octets():
    """Третьи октеты 192.168.x.0/24, занятые сетью самой машины (не модемами .100)."""
    out = set()
    for line in sh("ip", "-4", "-o", "addr", check=False).stdout.splitlines():
        m = re.search(r"inet (192\.168\.(\d+)\.(\d+))/(\d+)", line)
        if m and m.group(3) != "100":
            out.add(int(m.group(2)))
    return out


def uplink_route(modem_mac="0c:5b:8f:27:9a:64"):
    """(шлюз, интерфейс) лучшего маршрута по умолчанию не через модем.

    udhcpc из скрипта mp.space на пару секунд ставит маршрут по умолчанию через
    модем с метрикой 0 — лучше основного; поэтому «лучший» ищем среди немодемных.
    """
    mac = "link/ether %s " % modem_mac.lower()
    modems = {ln.split(":")[1].strip().split("@")[0]
              for ln in sh("ip", "-o", "link", check=False).stdout.splitlines() if mac in ln + " "}
    best = None
    for ln in sh("ip", "-4", "route", "show", "default", "table", "main", check=False).stdout.splitlines():
        f = ln.split()
        dev = f[f.index("dev") + 1] if "dev" in f else ""
        metric = int(f[f.index("metric") + 1]) if "metric" in f else 0
        if dev and dev not in modems and (best is None or metric < best[0]):
            best = (metric, f[f.index("via") + 1] if "via" in f else None, dev)
    return best[1:] if best else None


def pin_proxies(hosts, modem_mac="0c:5b:8f:27:9a:64", log=print):
    """Адреса прокси — всегда через основной канал (таблица 90, правило 32765).

    Иначе соединение к прокси, открытое в окно udhcpc, уходит в свой же туннель
    и размножается петлёй: тысячи соединений, прокси начинает отказывать.
    Правило стоит после правил «from 192.168.N.100» и перед main.
    """
    up = uplink_route(modem_mac)
    if not up:
        log("⚠ не нашёл основной канал сервера — маршрут к прокси не закреплён")
        return False
    gw, dev = up
    t = str(PIN_TABLE)
    if any("dev %s " % dev not in ln + " " for ln in sh("ip", "-4", "route", "show", "table", t, check=False).stdout.splitlines()):
        sh("ip", "route", "flush", "table", t, check=False)        # основной канал сменился
    for ln in sh("ip", "-4", "route", "show", "dev", dev, "scope", "link", "table", "main", check=False).stdout.splitlines():
        sh("ip", "route", "replace", ln.split()[0], "dev", dev, "table", t, check=False)
    sh("ip", "route", "replace", "default", *(["via", gw] if gw else []), "dev", dev, "table", t)
    want = set()
    for h in hosts:
        try:
            want.update(socket.gethostbyname_ex(h)[2])
        except OSError:
            pass
    have = set(re.findall(r"to (\S+) lookup %s\b" % t, sh("ip", "-4", "rule", check=False).stdout))
    for ip in want - have:
        sh("ip", "rule", "add", "priority", str(PIN_PRIO), "to", ip, "lookup", t)
    for ip in have - want:
        sh("ip", "rule", "del", "priority", str(PIN_PRIO), "to", ip, "lookup", t, check=False)
    return True
