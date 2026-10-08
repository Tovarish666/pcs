#!/usr/bin/env python3
"""Проверки hivelink без root, без модемов и без сети.

    python3 tests/test_hivelink.py

sysfs — подставной каталог: настоящий модем на PCI-контроллере, виртуальный
модем proxyveth на dummy_hcd (тот же 12d1:14dc), Zero-CD, хаб, сетевухи хоста.
"""
import fnmatch
import io
import re
import json
import os
import shutil
import sys
import tempfile
import threading
import types
from contextlib import redirect_stdout
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, os.pardir))
from pcs.core import util  # noqa: E402
from pcs.hivelink import cli, common, driver, hilink, install, rndis, route, status, usb  # noqa: E402

passed = failed = 0


def check(name, cond, detail=""):
    global passed, failed
    if cond:
        passed += 1
        print("  ok   %s" % name)
    else:
        failed += 1
        print("  FAIL %s%s" % (name, ("\n       " + str(detail)) if detail else ""))


TMP = tempfile.mkdtemp(prefix="hivelink-test-")
util.QUIET[0] = True                      # журнал прохода — не на экран
common.log.path = os.path.join(TMP, "log", "hivelink.log")
common.RUN = os.path.join(TMP, "run")
common.STATE = os.path.join(TMP, "run", "state.json")
common.CONFMAP = os.path.join(TMP, "var", "confmap.json")
common.MP_SETUP = os.path.join(TMP, "нет-mp.space")


# ── подставной sysfs ────────────────────────────────────────────────────────
S = os.path.join(TMP, "sys")
PCI = "devices/pci0000:00/0000:00:14.0/usb1"
DUMMY = "devices/platform/dummy_hcd.0/usb3"


def mk(rel, **attrs):
    d = os.path.join(S, rel)
    os.makedirs(d, exist_ok=True)
    for k, v in attrs.items():
        path = os.path.join(d, k.replace("__", "/"))
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write(v + "\n")
    return d


def link(target, name):
    os.makedirs(os.path.dirname(os.path.join(S, name)), exist_ok=True)
    if os.path.lexists(os.path.join(S, name)):
        os.unlink(os.path.join(S, name))
    os.symlink(os.path.join(S, target), os.path.join(S, name))


def device(base, port, pid, cfg="1", ncfg="3", drv=None, ifn=None, vendor="12d1", devnum="5", rev="0102"):
    d = mk("%s/%s" % (base, port), idVendor=vendor, idProduct=pid, bcdDevice=rev, bConfigurationValue=cfg,
           bNumConfigurations=ncfg, busnum="1", devnum=devnum, authorized="1", power__control="auto")
    link("%s/%s" % (base, port), "bus/usb/devices/%s" % port)
    intf = mk("%s/%s/%s:%s.0" % (base, port, port, cfg), bInterfaceClass="e0")
    link("%s/%s/%s:%s.0" % (base, port, port, cfg), "bus/usb/devices/%s:%s.0" % (port, cfg))
    if drv:
        mk("bus/usb/drivers/%s" % drv)
        os.symlink(os.path.join(S, "bus/usb/drivers", drv), os.path.join(intf, "driver"))
    if ifn:
        mk("%s/%s/%s:%s.0/net/%s" % (base, port, port, cfg, ifn))
        link("%s/%s/%s:%s.0" % (base, port, port, cfg), "class/net/%s/device" % ifn)
    return d


device(PCI, "1-2", "14dc", drv="rndis_host", ifn="eth3")                 # настоящий, HiLink
device(PCI, "1-3", "1f01", cfg="2", ncfg="2")                            # настоящий, Zero-CD в cfg 2
device(PCI, "1-5", "14dc", cfg="2", drv="huawei_cdc_ncm", ifn="wwan0", devnum="9", rev="0103")   # NCM вместо RNDIS
device(PCI, "1-4", "0101", vendor="1a40", ncfg="1")                      # хаб
device(DUMMY, "3-1", "14dc", drv="cdc_ether", ifn="eth1")                # виртуальный proxyveth
device("devices/platform/dummy_hcd.1/usb4", "4-1", "14dc", cfg="2", drv="huawei_cdc_ncm", ifn="eth2")
mk("devices/pci0000:00/0000:00:1f.6")
link("devices/pci0000:00/0000:00:1f.6", "class/net/enp0s31f6/device")
mk("class/net/vmbr0")
usb.SYS = S

print("настоящие и виртуальные модемы:")
real = [usb.port(d) for d in usb.devices()]
check("на dummy_hcd — не наши", real == ["1-2", "1-3", "1-5"], real)
check("виртуальные посчитаны отдельно", usb.virtual_count() == 2)
check("readlink -f виртуального содержит /dummy_hcd.", usb.is_virtual(os.path.join(S, "bus/usb/devices/3-1"))
      and not usb.is_virtual(os.path.join(S, "bus/usb/devices/1-2")))
check("интерфейс настоящего модема — наш", usb.real_iface("eth3"))
check("интерфейс виртуального модема — не наш", not usb.real_iface("eth1") and not usb.real_iface("eth2"))
check("сетевухи хоста — не модемы", not usb.real_iface("vmbr0") and not usb.real_iface("enp0s31f6"))
check("хаб найден для питания", [usb.port(d) for d in usb.hubs()] == ["1-4"])
check("драйвер интерфейса", usb.iface_driver("eth3") == "rndis_host")
check("порт → устройство только среди настоящих", usb.find("1-2") and usb.find("3-1") is None)
check("режимы PID", [usb.pid_mode(p) for p in ("1f01", "1F14", "14dc", "1506", "14fe", "155e", "1c25")]
      == ["cdrom", "cdrom", "hilink", "stick", "stick-serial", "other", "stick"])
check("поздние PID — other, обслуживаются по драйверу", usb.pid_mode("1f1e") == "other")

ADDR = """1: lo    inet 127.0.0.1/8 scope host lo\\       valid_lft forever preferred_lft forever
2: vmbr0    inet 192.168.88.100/24 scope global vmbr0\\       valid_lft forever preferred_lft forever
5: eth3    inet 192.168.101.100/24 metric 1000 brd 192.168.101.255 scope global dynamic eth3\\       valid_lft 85000sec
6: eth1    inet 192.168.5.100/24 brd 192.168.5.255 scope global eth1\\       valid_lft forever preferred_lft forever
"""
conf = dict(common.DEFAULTS)
mods, addrs = driver.modems(conf, ADDR)
check("модемы — только интерфейсы настоящих устройств", sorted(m["iface"] for m in mods) == ["eth3", "wwan0"], mods)
check("номер модема из адреса 192.168.N.100", [m["n"] for m in mods if m["iface"] == "eth3"] == [101])
check("адрес .100 на vmbr0 — не модем", "vmbr0" not in {m["iface"] for m in mods})
w = driver.want(mods, addrs, conf)
check("маршрутизация нужна только eth3 (NCM без адреса — нет)", w == {101: ("eth3", "192.168.101.100")}, w)
mods2, addrs2 = driver.modems(conf, ADDR.replace("192.168.101.", "192.168.5."))
check("подсеть виртуального модема занята — настоящий пропущен", driver.want(mods2, addrs2, conf) == {})
mods3, addrs3 = driver.modems(conf, ADDR.replace("192.168.101.", "192.168.88."))
check("подсеть сети хоста занята — модем пропущен", driver.want(mods3, addrs3, conf) == {})

print("таблицы и приоритеты:")
check("таблица 2000+N, приоритет 31000+N (pcs.core.net)",
      route.table(5) == 2005 and route.prio(5) == 31005 and route.table(254) == 2254)
check("диапазоны свои", (route.PRIO_MIN, route.PRIO_MAX, route.TABLE_MIN, route.TABLE_MAX) == (31001, 31254, 2001, 2254))

RULES = """0:	from all lookup local
1101:	from 192.168.101.100 lookup 101
30005:	from 192.168.5.100 lookup 1005
31007:	from 192.168.7.100 lookup 2007
31101:	from 192.168.101.100 lookup 2101
31101:	from 192.168.101.100 lookup 2101
31150:	from all fwmark 0x1 lookup 2150
32764:	from 192.168.1.100 lookup 101
32764:	from 192.168.105.100 lookup modem5
32765:	from all to 188.134.95.184 lookup 90
32766:	from all lookup main
32767:	from all lookup default
"""
ROUTES = """default via 192.168.1.1 dev eth5 table 101
default via 192.168.5.1 dev eth1 table 1005
default via 192.168.7.1 dev eth9 table 2007
192.168.101.0/24 dev eth3 table 2101 scope link src 192.168.101.100
default via 192.168.101.1 dev eth3 table 2101
default via 192.168.88.1 dev vmbr0 table 90
default via 192.168.88.1 dev vmbr0 proto kernel
local 127.0.0.1 dev lo table local proto kernel scope host src 127.0.0.1
"""
cmds = route.plan({101: ("eth3", "192.168.101.100")}, RULES, ROUTES)
txt = [" ".join(c) for c in cmds]
check("снято своё лишнее: правило ушедшего модема 7", "ip rule del priority 31007 from 192.168.7.100 lookup 2007" in txt, txt)
check("снят дубль своего правила", txt.count("ip rule del priority 31101 from 192.168.101.100 lookup 2101") == 1, txt)
check("чужое в нашем диапазоне тоже не остаётся", "ip rule del priority 31150 from all fwmark 0x1 lookup 2150" in txt)
check("очищена только своя таблица ушедшего модема", [c for c in txt if "flush" in c] == ["ip route flush table 2007"], txt)
touched = [c for c in cmds if c[:3] == ["ip", "rule", "del"] and not route.ours(int(c[4]))]
check("правила mp.space, proxyveth, старые и основные не тронуты", not touched, touched)
check("целый модем — без лишних команд",
      not any("2101" in c and ("replace" in c or "add" in c) for c in txt), txt)
fresh = [" ".join(c) for c in route.plan({102: ("eth4", "192.168.102.100")}, "", "")]
check("новый модем: маршруты и правило в своей таблице", fresh == [
    "ip route replace 192.168.102.0/24 dev eth4 src 192.168.102.100 table 2102",
    "ip route replace default via 192.168.102.1 dev eth4 table 2102",
    "ip rule add priority 31102 from 192.168.102.100 lookup 2102"], fresh)
moved = [" ".join(c) for c in route.plan({101: ("eth7", "192.168.101.100")}, RULES, ROUTES)]
check("модем сменил интерфейс — таблица переложена",
      "ip route flush table 2101" in moved and "ip route replace default via 192.168.101.1 dev eth7 table 2101" in moved)
gone = route.plan({}, RULES, ROUTES)
check("снятие: только свои правила и таблицы",
      all(route.ours(int(c[4])) for c in gone if c[1] == "rule")
      and sorted(c[4] for c in gone if c[2] == "flush") == ["2007", "2101"], gone)
leg = [" ".join(c) for c in route.legacy_plan(RULES + "1007:\tfrom 192.168.7.100 lookup 7\n1050:\tfrom 192.168.50.100 lookup 99\n")]
check("старый e3372: его правила сняты", "ip rule del priority 1101 from 192.168.101.100 lookup 101" in leg
      and "ip rule del priority 1007 from 192.168.7.100 lookup 7" in leg, leg)
check("старый e3372: таблица 101 живёт — на неё ссылается proxyveth/mp.space",
      "ip route flush table 101" not in leg and "ip route flush table 7" in leg, leg)
check("старый e3372: непохожее правило не тронуто", not any("1050" in c for c in leg))
check("адреса: интерфейс с @", route.parse_addrs("7: pv5@if2    inet 10.0.0.1/30 scope global pv5\n") == {"pv5": ["10.0.0.1/30"]})

print("udev, networkd, systemd:")
u, ms, ports, nw = install.udev_text(), install.udev_ms_text(), install.udev_ports_text(), install.network_text()
first_run = u.index("RUN+=")
check("udev: dummy_hcd уходит в конец раньше любого действия",
      'DEVPATH=="*/dummy_hcd.*|*/vhci_hcd.*", GOTO="hivelink_end"' in u
      and u.index("dummy_hcd") < u.index('ATTR{power/control}="on"') < first_run)
check("udev: net-событие по ATTRS (ID_VENDOR_ID на 70-м ещё не задан)",
      'SUBSYSTEM=="net", ACTION=="add", ATTRS{idVendor}=="12d1"' in u and "ID_VENDOR_ID" not in u)
check("udev: юнит hivelink.service, не e3372", "start hivelink.service" in u and "e3372" not in u + ms + ports)
check("modeswitch: только Zero-CD PID и не на dummy_hcd",
      'DEVPATH=="*/dummy_hcd.*|*/vhci_hcd.*", GOTO="hivelink_ms_end"' in ms
      and 'ATTRS{idProduct}=="1f01|1f02|1f10|1f11|1f12|1f13|1f14|1440", RUN="/bin/true"' in ms)
check("modeswitch: штатный не отключён для всех 12d1",
      not any(ln.startswith('ATTRS{idVendor}=="12d1"') and "GOTO" in ln for ln in ms.splitlines()))
check("modeswitch: файл после штатного 40-usb_modeswitch.rules",
      os.path.basename(install.UDEV_MS) > "40-usb_modeswitch.rules" and os.path.basename(install.UDEV_MS).startswith("41-hivelink"))
check("AT-порты: тоже без виртуальных, имена hivelink",
      "dummy_hcd" in ports and 'SYMLINK+="hivelink/' in ports)
for name in (install.UDEV, install.UDEV_PORTS, install.UDEV_MS, install.NETWORK, install.SERVICE, install.TIMER):
    check("имя с префиксом hivelink: %s" % os.path.basename(name), "hivelink" in os.path.basename(name))
sect, cur = {}, None
for ln in nw.splitlines():
    ln = ln.strip()
    if ln.startswith("["):
        cur = ln
    elif ln and not ln.startswith("#"):
        k, v = ln.split("=", 1)
        sect.setdefault(cur, {})[k] = v
check(".network: матч по драйверу, вендору и не dummy_hcd", sect["[Match]"] == {
    "Driver": "cdc_ether rndis_host", "Property": "ID_VENDOR_ID=12d1",
    "Path": "!platform-dummy_hcd.* platform-vhci_hcd.*"}, sect.get("[Match]"))
pats = sect["[Match]"]["Path"][1:].split()
check(".network: Path= ловит ID_PATH виртуального и не ловит настоящий",
      any(fnmatch.fnmatchcase("platform-dummy_hcd.0-usb-0:1:1.0", p) for p in pats)
      and not any(fnmatch.fnmatchcase("pci-0000:00:14.0-usb-0:2:1.0", p) for p in pats))
check(".network: DNS от модема не берётся", sect["[DHCPv4]"]["UseDNS"] == "no"
      and sect["[Network]"]["DNSDefaultRoute"] == "no" and "DNS" not in sect["[Network]"])
check(".network: шлюз и маршруты из DHCP не берутся, загрузку не держит",
      sect["[DHCPv4]"]["UseGateway"] == "no" and sect["[DHCPv4]"]["UseRoutes"] == "no"
      and sect["[Link]"]["RequiredForOnline"] == "no")
check("udev DEVPATH-шаблон ловит виртуальный путь", fnmatch.fnmatchcase(
    "/devices/platform/dummy_hcd.0/usb3/3-1/3-1:1.0/net/eth1", usb.UDEV_VIRTUAL.split("|")[0])
    and not fnmatch.fnmatchcase("/devices/pci0000:00/0000:00:14.0/usb1/1-2", usb.UDEV_VIRTUAL.split("|")[0]))
svc = install.service_text("/opt/pcs/bin/hivelink")
check("юнит зовёт hivelink run", "ExecStart=/opt/pcs/bin/hivelink run" in svc and "StartLimitIntervalSec=0" in svc)
code = "".join(open(m.__file__).read() for m in (install, driver, rndis, common))
check("глобальные rp_filter/arp_* не трогаются, networkd не рестартуется",
      not re.search(r"conf[./]all|conf[./]default|rp_filter\s*=\s*1|\"restart\"", code))
check("свои sysctl — только на интерфейсах модемов",
      [k for k, _ in driver.IFACE_SYSCTL] == ["rp_filter", "arp_ignore", "arp_announce"])

print("исходник ядра для DKMS:")
K = [("6.8.0-45-generic", "", "", "6.8.0", "https://cdn.kernel.org/pub/linux/kernel/v6.x/linux-6.8.tar.xz"),
     ("6.8.0-45-generic", "", "Ubuntu 6.8.0-45.45-generic 6.8.12", "6.8.12",
      "https://cdn.kernel.org/pub/linux/kernel/v6.x/linux-6.8.12.tar.xz"),
     ("6.1.0-25-amd64", "Linux version 6.1.0-25-amd64 (debian-kernel@lists.debian.org) (gcc-12 (Debian 12.2.0-14) "
      "12.2.0, GNU ld (GNU Binutils for Debian) 2.40) #1 SMP PREEMPT_DYNAMIC Debian 6.1.106-3 (2024-08-26)", "",
      "6.1.106", "https://cdn.kernel.org/pub/linux/kernel/v6.x/linux-6.1.106.tar.xz"),
     ("6.1.0-25-amd64", "", "", "6.1.0", "https://cdn.kernel.org/pub/linux/kernel/v6.x/linux-6.1.tar.xz"),
     ("6.12.48+deb13-amd64", "", "", "6.12.48", "https://cdn.kernel.org/pub/linux/kernel/v6.x/linux-6.12.48.tar.xz"),
     ("6.8.12-4-pve", "Linux version 6.8.12-4-pve (build@proxmox) (gcc (Debian 12.2.0-14) 12.2.0, GNU ld (GNU "
      "Binutils for Debian) 2.40) #1 SMP PREEMPT_DYNAMIC PMX 6.8.12-4 (2024-11-06T15:04Z)", "",
      "6.8.12", "https://cdn.kernel.org/pub/linux/kernel/v6.x/linux-6.8.12.tar.xz"),
     ("6.14.0-1-pve", "", "", "6.14.0", "https://cdn.kernel.org/pub/linux/kernel/v6.x/linux-6.14.tar.xz"),
     ("5.15.0-100-generic", "", "", "5.15.0", "https://cdn.kernel.org/pub/linux/kernel/v5.x/linux-5.15.tar.xz"),
     ("6.12.48+deb13-amd64", "Linux version 6.12.48+deb13-amd64 (gcc-14 (Debian 14.2.0-19) 14.2.0) #1 SMP "
      "PREEMPT_DYNAMIC Debian 6.12.48-1 (2025-09-20)", "", "6.12.48",
      "https://cdn.kernel.org/pub/linux/kernel/v6.x/linux-6.12.48.tar.xz")]
for rel, ver, sig, want_v, url in K:
    v = rndis.upstream(rel, ver, sig)
    check("%s%s → %s, %s" % (rel, " (" + (sig or ver).split()[-1 if sig else -2] + ")" if sig or ver else "",
                             want_v, url.rsplit("/", 1)[1]), v == want_v and rndis.tar_url(v) == url,
          (v, rndis.tar_url(v)))
check("x.y.0 у kernel.org — без .0 и в теге", rndis.file_urls("6.8.0")[0].endswith("?h=v6.8")
      and "6.8.0" not in " ".join(rndis.file_urls("6.8.0")))
check("заголовки ядра Proxmox и Debian/Ubuntu",
      rndis.header_pkgs("6.8.12-4-pve")[0] == "proxmox-headers-6.8.12-4-pve"
      and rndis.header_pkgs("6.8.0-45-generic") == ["linux-headers-6.8.0-45-generic"])
SRC_C = """#include <linux/module.h>
#include <linux/usb/usbnet.h>

int generic_rndis_bind(struct usbnet *dev, struct usb_interface *intf, int flags)
{
\tdev->rx_urb_size &= ~(dev->maxpacket - 1);
\tu.init->max_transfer_size = cpu_to_le32(dev->rx_urb_size);
}
"""
pt = rndis.patch(SRC_C)
check("патч: параметр и подмена перед max_transfer_size", "module_param(rx_urb_size_override, uint, 0644);" in pt
      and "\tif (rx_urb_size_override)\n\t\tdev->rx_urb_size = rx_urb_size_override;\n\tu.init->max_transfer_size" in pt)
check("патч идемпотентен", rndis.patch(pt) == pt)
try:
    rndis.patch(SRC_C.replace("max_transfer_size", "max_xfer"))
    check("апстрим переписал функцию — честный отказ", False)
except util.Fail:
    check("апстрим переписал функцию — честный отказ", True)

print("HiLink:")
SES = '<?xml version="1.0" encoding="UTF-8"?>\n<response>\n<SesInfo>SessionID=abc123</SesInfo>\n<TokInfo>tok9</TokInfo>\n</response>'
check("сессия", hilink.parse_session(SES) == ("SessionID=abc123", "tok9"))
check("сессия без префикса", hilink.parse_session("<SesInfo>xyz</SesInfo><TokInfo>t</TokInfo>")[0] == "SessionID=xyz")
check("сессии не дали", hilink.parse_session("<error><code>125002</code></error>") == (None, None))
STATUS = ("<response><ConnectionStatus>901</ConnectionStatus><SignalIcon>4</SignalIcon>"
          "<CurrentNetworkType>19</CurrentNetworkType><CurrentNetworkTypeEx>101</CurrentNetworkTypeEx></response>")
check("статус: онлайн, LTE, сигнал", hilink.parse_status(STATUS) == {"conn": 901, "net": "LTE", "signal": 4, "error": None})
check("статус: ошибка API", hilink.parse_status("<error><code>125002</code><message></message></error>")["error"] == 125002)
check("3G и 2G", hilink.net_text(45) == "3G" and hilink.net_text(3) == "2G" and hilink.net_text(None) is None)
check("тексты связи", [hilink.conn_text(c) for c in (901, 902, 113, None)] == ["онлайн", "отключён", "нет регистрации", "нет ответа"])
check("ответ OK", hilink.is_ok("<?xml?>\n<response>OK</response>") and not hilink.is_ok("<error><code>1</code></error>"))
check("нет регистрации", hilink.no_registration(907) and hilink.no_registration(112) and not hilink.no_registration(902))

seen = []


class Fake(BaseHTTPRequestHandler):
    data = {"dataswitch": "0"}

    def log_message(self, *a):
        pass

    def reply(self, body):
        self.send_response(200)
        self.send_header("Content-Type", "text/xml")
        self.end_headers()
        self.wfile.write(body.encode())

    def authed(self):
        return self.headers.get("Cookie") == "SessionID=abc123" and self.headers.get("__RequestVerificationToken") == "tok9"

    def do_GET(self):
        if self.path == "/api/webserver/SesTokInfo":
            return self.reply(SES)
        if not self.authed():
            return self.reply("<error><code>125002</code></error>")
        if self.path == "/api/monitoring/status":
            return self.reply(STATUS)
        if self.path == "/api/dialup/mobile-dataswitch":
            return self.reply("<response><dataswitch>%s</dataswitch></response>" % self.data["dataswitch"])
        self.reply("<error><code>100002</code></error>")

    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("Content-Length", 0))).decode()
        seen.append((self.path, body))
        if not self.authed():
            return self.reply("<error><code>125003</code></error>")
        if self.path == "/api/dialup/mobile-dataswitch":
            self.data["dataswitch"] = "1" if "<dataswitch>1</dataswitch>" in body else "0"
        self.reply("<response>OK</response>")


srv = ThreadingHTTPServer(("127.0.0.1", 0), Fake)
threading.Thread(target=srv.serve_forever, daemon=True).start()
api = hilink.Api(101, timeout=3)
api.host, api.port = "127.0.0.1", srv.server_address[1]
try:
    check("без сессии — сначала вход, потом статус", api.status()["conn"] == 901)
    check("данные выключены", api.dataswitch() == 0)
    api.set_data(True)
    check("включить данные: POST с сессией и токеном", api.dataswitch() == 1
          and seen[-1] == ("/api/dialup/mobile-dataswitch",
                           '<?xml version="1.0" encoding="UTF-8"?><request><dataswitch>1</dataswitch></request>'))
    api.dial(False)
    api.dial(True)
    check("реконнект: дозвон 0, потом 1", [b for p, b in seen if p == "/api/dialup/dial"] ==
          ['<?xml version="1.0" encoding="UTF-8"?><request><Action>0</Action></request>',
           '<?xml version="1.0" encoding="UTF-8"?><request><Action>1</Action></request>'])
finally:
    srv.shutdown()
dead = hilink.Api(101, timeout=1)
dead.host, dead.port = "127.0.0.1", srv.server_address[1]
try:
    dead.login()
    check("модем молчит — HiFail, без трассы", False)
except hilink.HiFail as e:
    check("модем молчит — HiFail, без трассы", "не отвечает" in str(e), e)

print("проход драйвера на подставном sysfs:")
cfgs = []
real_set, real_which, real_sh = usb.set_config, shutil.which, driver.sh
usb.set_config = lambda d, v: cfgs.append((usb.port(d), v))
st = common.load_state()
c = dict(common.DEFAULTS, nodrv_grace=0)
driver.step_usbcfg(c, st, usb.devices())
check("NCM на настоящем — следующая конфигурация; виртуальный NCM не тронут", cfgs == [("1-5", 3)], cfgs)
cfgs.clear()
check("рабочая конфигурация модели выучена", util.jload(common.CONFMAP, {}) == {"14dc:0102": 1})
util.jsave(common.CONFMAP, {"14dc:0103": 1})
driver.step_usbcfg(c, common.load_state(), usb.devices())
check("модель уже изучена — сразу на выученную cfg", cfgs == [("1-5", 1)], cfgs)
cfgs.clear()
shutil.which = lambda n: "/usr/sbin/" + n
try:
    driver.step_zerocd(c, common.load_state(), usb.devices())
finally:
    shutil.which = real_which
check("Zero-CD не в cfg 1 — сначала cfg 1 (там есть mass-storage)", cfgs == [("1-3", 1)], cfgs)
os.unlink(common.CONFMAP)
st = {"cnt": {}, "probe": {"1-5": {"1": "none", "2": "huawei_cdc_ncm", "3": "huawei_cdc_ncm"}}, "msg": {}, "dial": {}}
st["cnt"]["cfgtry.1-5"] = c["cfg_max_tries"]
cfgs.clear()
driver.step_usbcfg(c, st, usb.devices())
check("ни одна конфигурация не годна — бросаем, а не крутим вечно", st["cnt"].get("gaveup.1-5") == "9" and not cfgs, st)
driver.step_usbcfg(c, st, usb.devices())
check("брошенный не перебирается до пере-enumerate", not cfgs)
ran = []
WORLD = {("ip", "-4", "-o", "addr", "show"): ADDR, ("ip", "-4", "rule", "show"): RULES,
         ("ip", "-4", "route", "show", "table", "all"): ROUTES}
fake_sh = lambda *a, **k: (ran.append(a), types.SimpleNamespace(stdout=WORLD.get(a, ""), stderr="", returncode=0))[1]  # noqa: E731
driver.sh = fake_sh
try:
    common.save_state({"cycle": 0})
    cfgs.clear()
    driver.run_pass(dict(common.DEFAULTS, hilink_every=1000))
finally:
    driver.sh, usb.set_config = real_sh, real_set
changes = [" ".join(a) for a in ran if a[:2] in (("ip", "rule"), ("ip", "route")) and "show" not in a]
check("проход: свои лишние сняты, чужие правила и таблицы целы",
      "ip rule del priority 31007 from 192.168.7.100 lookup 2007" in changes
      and not any(x in c for c in changes for x in (" 1005", " 101 ", "lookup 101", "modem5", " 90", "32764", "30005")),
      changes)
check("проход: виртуальные модемы не тронуты ни одной командой",
      not any("eth1" in " ".join(a) or "eth2" in " ".join(a) or "3-1" in " ".join(a) for a in ran), ran)
check("проход: питание выключено у настоящих и хаба, у виртуальных — нет",
      usb.attr(usb.find("1-2"), "power/control") == "on" and usb.attr(os.path.join(S, "bus/usb/devices/1-4"), "power/control") == "on"
      and usb.attr(os.path.join(S, "bus/usb/devices/3-1"), "power/control") == "auto")
check("проход: состояние сохранено", common.load_state()["cycle"] == 1)

print("status:")
rows, reach = status.local_rows(conf, ADDR)
check("строки — только настоящие устройства", sorted(r["usb_port"] for r in rows) == ["1-2", "1-3", "1-5"], rows)
need = {"n", "iface", "driver", "usb_port", "cfg", "conn", "dataswitch", "net", "ext_ip", "errors"}
check("поля строки status --json", all(need <= set(r) for r in rows))
r12 = next(r for r in rows if r["usb_port"] == "1-2")
check("рабочий модем: N, интерфейс, драйвер, cfg", (r12["n"], r12["iface"], r12["driver"], r12["cfg"], r12["errors"])
      == (101, "eth3", "rndis_host", "1/3", []), r12)
check("в web-API идём только к своим", reach == {101})
r13 = next(r for r in rows if r["usb_port"] == "1-3")
check("Zero-CD назван ошибкой, а не прочерком", r13["n"] is None and r13["errors"] and "Zero-CD" in r13["errors"][0])
check("сводка USB", status.summary(rows) == {"total": 3, "hilink": 2, "zerocd": 1, "stick": 0, "other": 0,
                                             "with_addr": 1, "ok": 1}, status.summary(rows))
check("doctor: чужие правила в нашем диапазоне видны",
      status.foreign_in_range(RULES) == ["31150: from all fwmark 0x1 lookup 2150"], status.foreign_in_range(RULES))
rows2, reach2 = status.local_rows(conf, ADDR.replace("192.168.101.", "192.168.5."))
check("подсеть у виртуального модема — в web-API не идём", not reach2
      and any("занята интерфейсом eth1" in e for r in rows2 for e in r["errors"]))

probed = []
real_probe, real_ext = hilink.probe, hilink.ext_ip
hilink.probe = lambda n, src, timeout=5: (probed.append((n, src)), {"conn": 902, "dataswitch": 0, "net": "LTE",
                                                                   "signal": 3, "errors": []})[1]
hilink.ext_ip = lambda src, timeout=6: probed.append(("ext", src))
real_ssh, real_dsh, real_ish = status.sh, driver.sh, install.sh
status.sh = driver.sh = install.sh = fake_sh
try:
    data = status.collect(conf)
    buf = io.StringIO()
    with redirect_stdout(buf):
        status.show(data, conf)
    checks = status.doctor()
    with redirect_stdout(buf):
        status.show_doctor(checks)
finally:
    hilink.probe, hilink.ext_ip = real_probe, real_ext
    status.sh, driver.sh, install.sh = real_ssh, real_dsh, real_ish
check("status --json сериализуется, в web-API — только свой модем со своего адреса",
      json.loads(json.dumps(data))["modems"] and probed == [(101, "192.168.101.100")], probed)
r = next(r for r in data["modems"] if r["n"] == 101)
check("status: связь кодом, данные, внешний IP — честно null", (r["conn"], r["conn_text"], r["dataswitch"], r["ext_ip"])
      == (902, "отключён", 0, None) and any("dataon 101" in e for e in r["errors"]), r)
check("status: модем не в сети — внешний IP не ждём", ("ext", "192.168.101.100") not in probed)
check("status: модемы с номером — первыми", [r["n"] for r in data["modems"]][0] == 101)
check("status: виртуальные посчитаны, но не показаны", data["virtual"] == 2 and len(data["modems"]) == 3)
out = buf.getvalue()
check("человеку: таблица и проблемы", "N    IFACE" in out and "⚠ Zero-CD" in out and "виртуальных модемов proxyveth" in out, out)
check("doctor: проверки в общем виде", checks and all(set(c) == {"name", "ok", "text"} for c in checks))
check("doctor видит чужое правило в диапазоне hivelink",
      any(c["name"] == "правила 31001–31254" and not c["ok"] for c in checks))

print("миграция со старого e3372-driver:")
OLD = """# конфиг
PREFER_DRIVER=cdc_ether
FALLBACK_DRIVERS="rndis_host"   # запасной
AUTO_RECONNECT=0
RX_URB_SIZE=32768  # больше
HILINK_EVERY='2'
HOST_PREFIX=192.168
#CFG_MAX_TRIES=99
$(rm -rf /)
"""
lc = install.legacy_conf(OLD)
check("старый конфиг разобран без source", lc == {"prefer_driver": "cdc_ether", "fallback_drivers": ["rndis_host"],
                                                   "auto_reconnect": False, "rx_urb_size": 32768, "hilink_every": 2}, lc)
check("выученные конфигурации перенесены", install.legacy_confmap("14dc:0102 1\n1506:0000 2\nмусор\n")
      == {"14dc:0102": 1, "1506:0000": 2})
check("пути старого установщика — все e3372", all("e3372" in p or "hilink" in p for p in install.LEGACY_FILES + install.LEGACY_DIRS))

print("команда:")


def run(*argv):
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = cli.main(list(argv))
    util.QUIET[0] = True
    return rc, buf.getvalue()


rc, out = run("status", "лишнее", "--json")
check("лишние аргументы — конверт ok:false", rc == 1 and json.loads(out) == {"ok": False, "error": "лишние аргументы: лишнее — hivelink help"}, out)
rc, out = run("ребут", "--json")
check("неизвестная команда — конверт ok:false", rc == 1 and json.loads(out)["ok"] is False)
rc, out = run("help")
check("справка: ровно команды §6", rc == 0 and all("hivelink %s" % c in out for c in
      ("install", "uninstall", "status [--json]", "doctor", "reconnect N", "reset N", "recfg N", "dataon N|--all")))
if os.geteuid() != 0:
    rc, out = run("reconnect", "5", "--json")
    check("действие без root — одна строка ошибки", rc == 1 and json.loads(out) == {"ok": False, "error": "нужен root"})

shutil.rmtree(TMP, ignore_errors=True)
print("\nитого: ok %d, fail %d" % (passed, failed))
sys.exit(1 if failed else 0)
