"""proxyveth, режим usb — модем выглядит как настоящий USB Huawei E3372h (бывший vmodem 4.x).

Прокси ген1 (host:port:login:pass) превращается в модем, который сервер видит
воткнутым в USB: Huawei E3372h HiLink (12d1:14dc, cdc_ether), со своим DHCP
(выдаёт ровно 192.168.N.100), своим DNS и веб-мордой настоящего модема на
192.168.N.1. Отдельных ВМ нет: каждый модем — network namespace pvN на этой же
машине (гаджет configfs pvN на dummy_hcd, dnsmasq, sing-box, веб-морда).

Интерфейс режима (docs/ARCHITECTURE.md §6, зовёт pcs/proxyveth/cli.py):
setup, apply, up, down, status, diag, teardown, doctor. Сверх него — rotate,
reboot, running, live_hosts, служебное для юнитов (routes, hostside, replug)
и переезд с vmodem 4.x (vmodem_*).

Правила:
  * своя сторона чинится пересозданием («удали и начни заново»), чужая
    (прокси, SIM) — нет: модем всё равно создаётся и ждёт, а мы предупреждаем;
  * кривая или недоступная таблица не ломает работающее;
  * модем втыкается, когда его DHCP уже слушает, — как настоящий, который
    сперва загрузился;
  * разборка строго сверху вниз: сеть гаджета исчезает раньше гаджета.
"""
import glob
import hashlib
import json
import os
import re
import shlex
import shutil
import socket
import struct
import tarfile
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor

from pcs import VERSION
from pcs.core import net, singbox, table
from pcs.core.util import QUIET, Fail, Log, jload, jsave, lock, merge, nsh, nsx, rd, sh, wr

ETC = "/etc/proxyveth"
CONFIG = ETC + "/config.json"            # общий с cli: "mode", "source" и настройки режима
TABLE = ETC + "/table.csv"               # локальная копия таблицы (pcs.core.table.fetch)
MDIR = ETC + "/usb"                      # модем N: MDIR/N — dnsmasq, sing-box, пароль прокси
RUN = "/run/proxyveth"
LIB = "/var/lib/proxyveth"
LOGD = "/var/log/proxyveth"
GROOT = "/sys/kernel/config/usb_gadget"
HCD_DRV = "/sys/bus/platform/drivers/dummy_hcd"
UNITD = "/etc/systemd/system"
HOOK = "/usr/local/lib/pcs/proxyveth-udhcpc-hook"
HOST_RULE = "/etc/udev/rules.d/80-proxyveth-host.rules"
HOST_LINK = "/etc/systemd/network/10-proxyveth-cdc.link"
MODPROBE = "/etc/modprobe.d/proxyveth.conf"
MODLOAD = "/etc/modules-load.d/proxyveth.conf"
MP_SETUP = "/usr/local/bin/modem-interface-setup.sh"
REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
PY = "/usr/bin/python3"
CMD = os.path.join(REPO, "bin", "proxyveth")
WEB = os.path.join(REPO, "pcs", "proxyveth", "modem_web.py")
TUN = "pvtun0"
MARK = net.comment("proxyveth")
DKMS = "proxyveth-dummy-hcd"

CHECK_URL = ("connectivitycheck.gstatic.com", 80, "/generate_204")
CHECK_URL2 = ("cp.cloudflare.com", 80, "/generate_204")
IP_URL = ("api.ipify.org", 80, "/")
BANDS = "<NetworkBand>3FFFFFFF</NetworkBand><LTEBand>7FFFFFFFFFFFFFFF</LTEBand>"

DEFAULTS = {
    "transport": "dummy",          # dummy | vudc
    "slots": 48,                   # экземпляров dummy_hcd (не больше свободных USB-шин)
    "strategy": "window:5",        # сколько модемов «в пути» одновременно: у mp.space 5 слотов
    "host_timeout": 90,
    "diag_interval": 300,          # как часто перепроверять исправные модемы на прокси
    "max_fail": 3,
    "hostside": "auto",            # auto | on | off — свой DHCP и маршруты на стороне сервера
    # Как модем выглядит для сервера. Значения — как у E3372h в режиме HiLink.
    "usb": {"vendor": "0x12d1", "product_id": "0x14dc", "bcd_device": "0x0102",
            "manufacturer": "HUAWEI_MOBILE", "product": "HUAWEI_MOBILE", "serial": "",
            "host_mac": "0c:5b:8f:27:9a:64", "dev_mac": "00:1e:10:1f:00:00"},
    # Прямые прокси на каждый модем (перешли из vmodem; включаются в config.json).
    "proxy": {"enabled": False, "base_port": 20000, "listen": "0.0.0.0", "user": "", "pass": ""},
    "notify": {"telegram_token": "", "telegram_chat": "", "webhook": ""},
}

UPSTREAM_TEXT = {
    "ok": "в порядке",
    "proxy-down": "прокси не отвечает",
    "proxy-error": "прокси рвёт соединение",
    "proxy-auth": "прокси не пускает (логин/пароль)",
    "modem-blocked": "прокси не пускает в сеть модема",
    "modem-unreachable": "настоящий модем за прокси недоступен",
    "sim": "проблема с SIM",
    "no-data": "у настоящего модема нет связи",
    "captive": "SIM не оплачена / портал оператора",
    "no-internet": "нет интернета через модем",
    "?": "прокси ещё не проверялась",
    "—": "строки в таблице нет",
}

log = Log("proxyveth")


def say(msg=""):
    if not QUIET[0]:
        print(msg, flush=True)


def load_cfg():
    return merge(DEFAULTS, jload(CONFIG, {}))


def save_cfg(**changes):
    jsave(CONFIG, merge(jload(CONFIG, {}), changes))


def state():
    st = jload(os.path.join(RUN, "usb.json"), {})
    st.setdefault("modems", {})
    st.setdefault("bad_slots", [])
    return st


def save_state(st):
    jsave(os.path.join(RUN, "usb.json"), st)


# ── таблица ────────────────────────────────────────────────────────────────
LAYOUT = 4          # как устроен модем внутри; сменилось — sync пересоздаёт модемы сам
RELAY_PORT = 1080   # proxyveth-px: 127.0.0.1:1080 внутри netns модема → SOCKS5 прокси (то же в .socket)
ROW = ("real", "host", "port", "user", "pw")


def spec_hash(p):
    return hashlib.sha1(json.dumps([LAYOUT] + [p[k] for k in ROW]).encode()).hexdigest()[:12]


def same_row(n, p):
    """Строка таблицы та же, что у запущенного модема, — значит, пересоздаём из-за новой схемы."""
    old = jload(os.path.join(mdir(n), "spec.json"), None)
    return isinstance(old, dict) and all(old.get(k) == p[k] for k in ROW)


def table_now():
    """Годные, выключенные и отбракованные строки локальной копии — без сети."""
    text = rd(TABLE)
    if not text:
        return {}, set(), set()
    d, dis, inv, _ = table.lint(text, net.local_octets(), mp_tables=True)
    return d, dis, inv


def live_specs():
    """С какими настройками модемы работают прямо сейчас (не всегда = таблица:
    кривую или подозрительную таблицу мы к модемам не применяем)."""
    out = {}
    for n in running():
        spec = jload(os.path.join(mdir(n), "spec.json"), None)
        if isinstance(spec, dict):
            out[n] = spec
    return out


def live_hosts():
    """Адреса прокси работающих модемов — их тоже закрепляет pin_proxies."""
    return sorted({p["host"] for p in live_specs().values() if p.get("host")})


def specs_now(desired=None):
    specs = dict(table_now()[0] if desired is None else desired)
    specs.update(live_specs())
    return specs


def one(n):
    p = specs_now().get(n)
    if not p:
        raise Fail("модема %d нет среди годных строк таблицы (proxyveth lint)" % n)
    return p


# ── USB ────────────────────────────────────────────────────────────────────
def gdir(n):
    return "%s/pv%d" % (GROOT, n)


def udc_prefix(transport):
    return "dummy_udc." if transport == "dummy" else "usbip-vudc."


def udc_slots(transport):
    try:
        names = os.listdir("/sys/class/udc")
    except OSError:
        return []
    return sorted((s for s in names if s.startswith(udc_prefix(transport))),
                  key=lambda s: int(s.rsplit(".", 1)[1]))


def free_slots(cfg, st):
    used = {rd(u) for u in glob.glob(GROOT + "/*/UDC")}
    return [s for s in udc_slots(cfg["transport"]) if s not in used and s not in st["bad_slots"]]


def usbip_busid(udc):
    for line in sh("usbip", "port", check=False).stdout.splitlines():
        m = re.search(r"(\d+-[\d.]+) -> usbip://127\.0\.0\.1:3240/(\S+)$", line.strip())
        if m and m.group(2) == udc:
            return m.group(1)
    return ""


def host_side(n):
    """Как сервер видит модем: (USB busid, интерфейс). По слоту UDC, не по серийнику:
    серийник — как у настоящих E3372h, одинаковый."""
    udc = rd(gdir(n) + "/UDC")
    busid = ""
    if udc.startswith("dummy_udc."):
        devs = glob.glob("/sys/devices/platform/dummy_hcd.%s/usb*/*-1" % udc.rsplit(".", 1)[1])
        busid = os.path.basename(devs[0]) if devs else ""
    elif udc.startswith("usbip-vudc."):
        busid = usbip_busid(udc)
    if not busid:
        return "", ""
    nets = glob.glob("/sys/bus/usb/devices/%s/%s:*/net/*" % (busid, busid))
    return busid, (os.path.basename(nets[0]) if nets else "")


def addr_of(iface):
    if not iface:
        return ""
    m = re.search(r"inet (\S+)/", sh("ip", "-4", "-o", "addr", "show", iface, check=False).stdout)
    return m.group(1) if m else ""


def host_mode(cfg):
    """Кто на стороне сервера берёт у модема адрес и строит маршруты."""
    if os.path.exists(MP_SETUP):
        return "mpspace"
    return "own" if cfg["hostside"] in ("auto", "on") else "none"


def make_gadget(n, cfg):
    u, g = cfg["usb"], gdir(n)
    for d in ("strings/0x409", "configs/c.1/strings/0x409", "functions/ecm.usb0"):
        os.makedirs(os.path.join(g, d), exist_ok=True)
    serial = "PV%08d" % n if u["serial"] == "auto" else u["serial"]
    for f, v in (("idVendor", u["vendor"]), ("idProduct", u["product_id"]), ("bcdDevice", u["bcd_device"]),
                 ("strings/0x409/manufacturer", u["manufacturer"]), ("strings/0x409/product", u["product"]),
                 ("strings/0x409/serialnumber", serial),
                 ("configs/c.1/strings/0x409/configuration", "CDC ECM"), ("configs/c.1/MaxPower", "500"),
                 ("functions/ecm.usb0/host_addr", u["host_mac"]),
                 ("functions/ecm.usb0/dev_addr", u["dev_mac"])):
        wr(os.path.join(g, f), v)
    # Своё имя сетевухе гаджета: udev mp.space ловит KERNEL=="usb*" и запустил бы
    # DHCP на стороне модема.
    try:
        wr(g + "/functions/ecm.usb0/ifname", "pvg%d")
    except OSError:
        pass
    link = g + "/configs/c.1/ecm.usb0"
    if not os.path.islink(link):
        os.symlink(g + "/functions/ecm.usb0", link)
    return g


def hcd_power(udc, on):
    """Своя шина USB (root hub в lsusb) — только у слота с модемом. У пустого слота
    dummy_hcd отвязан от драйвера: шины нет, а слот UDC остаётся и ждёт модема."""
    if not udc.startswith("dummy_udc."):
        return
    hcd = "dummy_hcd." + udc.rsplit(".", 1)[1]
    if os.path.exists(os.path.join(HCD_DRV, hcd)) != on:
        wr(os.path.join(HCD_DRV, "bind" if on else "unbind"), hcd)


def park_idle_hcds(st):
    """Убрать шины пустых слотов. Слот модема, который сейчас перезагружается
    (гаджет отвязан), тоже занят — по состоянию."""
    busy = {rd(u) for u in glob.glob(GROOT + "/*/UDC")} | {m.get("slot") for m in st["modems"].values()}
    for path in glob.glob(HCD_DRV + "/dummy_hcd.*"):
        udc = "dummy_udc." + path.rsplit(".", 1)[1]
        if udc not in busy:
            try:
                hcd_power(udc, False)
            except OSError as e:
                log("⚠ не убрать пустую шину %s: %s" % (udc, e.strerror))


def bind(n, udc):
    hcd_power(udc, True)
    wr(gdir(n) + "/UDC", udc)
    if udc.startswith("usbip-vudc."):
        sh("usbip", "attach", "-r", "127.0.0.1", "-d", udc)


def unbind_dir(g):
    udc = rd(g + "/UDC")
    if udc.startswith("usbip-vudc."):
        for line in sh("usbip", "port", check=False).stdout.split("Port ")[1:]:
            if line.rstrip().endswith("/" + udc):
                sh("usbip", "detach", "-p", line.split(":")[0], check=False)
    if udc:
        try:
            wr(g + "/UDC", "")
        except OSError:
            pass
    return udc


def unbind(n):
    return unbind_dir(gdir(n))


def drop_gadget(g):
    """Гаджет configfs — вынуть из слота и разобрать в обратном порядке."""
    if not os.path.isdir(g):
        return
    unbind_dir(g)
    if os.path.islink(g + "/configs/c.1/ecm.usb0"):
        os.unlink(g + "/configs/c.1/ecm.usb0")
    for d in ("functions/ecm.usb0", "configs/c.1/strings/0x409", "configs/c.1", "strings/0x409", ""):
        try:
            os.rmdir(os.path.join(g, d) if d else g)
        except OSError:
            pass


# ── один модем ─────────────────────────────────────────────────────────────
def mdir(n):
    return os.path.join(MDIR, str(n))


def singbox_conf(p):
    """Туннель модема: всё из usb0 — в прокси. Формат sing-box 1.12+ (§5)."""
    n = p["n"]
    return {
        "log": {"level": "warn", "output": "%s/sb-%d.log" % (LOGD, n), "timestamp": True},
        # DNS настоящего модема — по TCP через прокси: UDP у прокси ген1 не ходит.
        "dns": {"servers": [{"type": "tcp", "tag": "modem", "server": "192.168.%d.1" % p["real"], "detour": "proxy"}],
                "strategy": "ipv4_only"},
        "inbounds": [
            {"type": "tun", "tag": "tun-in", "interface_name": TUN, "address": ["172.20.0.1/30"],
             "mtu": 1400, "auto_route": False, "strict_route": False, "stack": "system"},
            {"type": "direct", "tag": "dns-in", "listen": "127.0.0.1", "listen_port": 5353},
        ],
        # Прокси — через proxyveth-px: сокет внутри netns, соединение наружу — с самого
        # сервера. Своей сети наружу у netns нет, поэтому и veth в корне не нужен.
        "outbounds": [
            {"type": "socks", "tag": "proxy", "version": "5", "server": "127.0.0.1", "server_port": RELAY_PORT,
             "username": p["user"], "password": p["pw"]},
        ],
        # Любой DNS через модем (и dnsmasq модема, и клиент на 8.8.8.8) — к DNS
        # настоящего модема по TCP: по UDP через прокси ген1 он бы просто не дошёл.
        "route": {"rules": [{"inbound": ["dns-in"], "action": "hijack-dns"}, {"port": 53, "action": "hijack-dns"}],
                  "final": "proxy", "auto_detect_interface": False},
    }


def dnsmasq_conf(p):
    # Как у HiLink: шлюз и DNS — сам модем, аренда на сутки.
    n = p["n"]
    return "\n".join([
        "interface=usb0", "bind-dynamic", "listen-address=192.168.%d.1" % n,
        "no-resolv", "server=127.0.0.1#5353", "cache-size=1000",
        "dhcp-range=192.168.%d.100,192.168.%d.100,255.255.255.0,24h" % (n, n),
        "dhcp-option=3,192.168.%d.1" % n, "dhcp-option=6,192.168.%d.1" % n,
        "dhcp-authoritative", "dhcp-leasefile=%s/leases" % mdir(n),
        "log-facility=%s/dnsmasq-%d.log" % (LOGD, n), ""])


def write_configs(p):
    n, d = p["n"], mdir(p["n"])
    os.makedirs(d, exist_ok=True)
    os.chmod(d, 0o700)
    os.makedirs(LOGD, exist_ok=True)
    files = {
        "singbox.json": json.dumps(singbox_conf(p), indent=1),
        "dnsmasq.conf": dnsmasq_conf(p),
        "env": "REAL=%d\nSOCKS=%s:%d\nSOCKS_USER=%s\n" % (p["real"], p["host"], p["port"], p["user"]),
        "proxy.pass": p["pw"] + "\n",
        "spec.json": json.dumps(dict(p, hash=spec_hash(p))),
    }
    for name, body in files.items():
        wr(os.path.join(d, name), body, 0o600)


def tun_dns_links():
    """Интерфейсы сервера, на которых висит DNS чужого tun 172.20.0.2."""
    try:
        out = sh("resolvectl", "dns", check=False).stdout
    except Fail:                          # нет systemd-resolved — нет и беды
        return []
    return [m.group(1) for m in re.finditer(r"^Link \d+ \((\S+)\): (.*)$", out, re.M)
            if "172.20.0.2" in m.group(2).split()]


def drop_tun_dns():
    """sing-box до запрета D-Bus вешал DNS своего tun (172.20.0.2, «весь DNS — сюда»)
    на интерфейс сервера с тем же номером. Снять и вернуть DNS хозяину интерфейса."""
    for link in tun_dns_links():
        sh("resolvectl", "revert", link, check=False)
        sh("networkctl", "renew", link, check=False)       # networkd вернёт DNS из аренды
        log("DNS интерфейса %s: снят чужой 172.20.0.2" % link)


def tun_routes(n, real):
    """Маршруты через pvtun0. Ядро стирает их вместе с tun, когда sing-box падает, —
    поэтому их же ставит ExecStartPost юнита при каждом перезапуске."""
    ns = "pv%d" % n
    for _ in range(50):
        if "172.20.0.1" in nsh(ns, "-4", "addr", "show", TUN, check=False).stdout:
            break
        time.sleep(0.2)
    else:
        raise Fail("%s не появился (%s/sb-%d.log)" % (TUN, LOGD, n))
    # В туннель — только трафик клиента модема (таблица 100). В основной таблице
    # маршрута по умолчанию нет: свой трафик sing-box идёт на 127.0.0.1 (proxyveth-px),
    # а всё остальное из netns наружу не попадёт и в свой же tun петлёй не уйдёт.
    nsh(ns, "route", "replace", "default", "dev", TUN, "table", "100")
    nsh(ns, "route", "replace", "192.168.%d.1/32" % real, "dev", TUN)


def dns_bound(ns, n):
    return ("192.168.%d.1:53" % n) in nsx(ns, "ss", "-Hlnu", check=False).stdout


def ensure_dns(ns, n):
    """dnsmasq стартует раньше usb0 и изредка пропускает появление адреса — тогда
    он молчит на DHCP, а mp.space повторять не умеет. Проверяем и перезапускаем."""
    for _ in range(15):
        if dns_bound(ns, n):
            return
        time.sleep(0.1)
    log("⚠ модем %d: dnsmasq не подхватил usb0 — перезапускаю" % n)
    sh("systemctl", "restart", "proxyveth-dns@%d" % n)
    for _ in range(30):
        if dns_bound(ns, n):
            return
        time.sleep(0.1)
    raise Fail("dnsmasq не слушает 192.168.%d.1" % n)


def destroy(n, quiet=False):
    """Разборка сверху вниз: сетевуха гаджета исчезает раньше гаджета."""
    for u in ("web", "sb", "px", "dns"):
        part = ["proxyveth-%s@%d.service" % (u, n)] + (["proxyveth-px@%d.socket" % n] if u == "px" else [])
        sh("systemctl", "stop", *part, check=False)
        sh("systemctl", "reset-failed", *part, check=False)
    drop_gadget(gdir(n))
    sh("ip", "netns", "del", "pv%d" % n, check=False)
    try:
        os.unlink(os.path.join(RUN, "rebooting-%d" % n))
    except OSError:
        pass
    if not quiet:
        log("модем %d снят" % n)


def create(p, cfg, st):
    """С нуля. Упало на любом шаге — снести всё обратно, полуживого не оставлять."""
    n, ns = p["n"], "pv%d" % p["n"]
    destroy(n, quiet=True)
    write_configs(p)
    slots = free_slots(cfg, st)
    if not slots:
        raise Fail("нет свободного слота UDC (%s*) — больше слотов: \"slots\" в %s и proxyveth setup"
                   % (udc_prefix(cfg["transport"]), CONFIG))
    step, udc = "netns", None
    try:
        # В netns модема — только lo, usb0 и tun sing-box. Своей сети наружу у него нет:
        # к прокси sing-box ходит через сокет proxyveth-px, веб-морда живёт на сервере.
        sh("ip", "netns", "add", ns)
        nsh(ns, "link", "set", "lo", "up")
        nsx(ns, "sysctl", "-qw", "net.ipv4.ip_forward=1", "net.ipv4.conf.all.rp_filter=0",
            "net.ipv4.conf.default.rp_filter=0")

        step = "dhcp"                    # bind-dynamic: подхватит usb0, когда тот появится
        sh("systemctl", "start", "proxyveth-dns@%d" % n)

        step = "tunnel"                  # ExecStartPost сам поставит маршруты через tun
        sh("systemctl", "start", "proxyveth-px@%d.socket" % n)
        sh("systemctl", "start", "proxyveth-sb@%d" % n)
        tun_routes(n, p["real"])
        nsh(ns, "rule", "add", "from", "192.168.%d.0/24" % n, "lookup", "100")
        nsh(ns, "rule", "add", "from", "172.20.0.1", "lookup", "100")
        nsx(ns, "iptables", "-t", "nat", "-A", "POSTROUTING", "-o", TUN, "-m", "comment", "--comment", MARK,
            "-j", "MASQUERADE")
        nsx(ns, "iptables", "-A", "FORWARD", "-o", TUN, "-p", "tcp", "--tcp-flags", "SYN,RST", "SYN",
            "-m", "comment", "--comment", MARK, "-j", "TCPMSS", "--clamp-mss-to-pmtu")

        step = "usb"                     # привязка к UDC = модем воткнут; дальше — быстро
        g = make_gadget(n, cfg)
        for cand in slots[:3]:
            try:
                hcd_power(cand, True)
                wr(g + "/UDC", cand)
                udc = cand
                break
            except OSError as e:
                log("⚠ модем %d: слот %s не привязался (%s) — помечаю плохим" % (n, cand, e.strerror))
                st["bad_slots"].append(cand)
        if not udc:
            raise Fail("ни один слот UDC не привязался — возможно, нужна перезагрузка сервера")
        ifn = rd(g + "/functions/ecm.usb0/ifname")
        sh("ip", "link", "set", ifn, "netns", ns)
        nsh(ns, "link", "set", ifn, "name", "usb0")
        nsh(ns, "addr", "add", "192.168.%d.1/24" % n, "dev", "usb0")
        nsh(ns, "link", "set", "usb0", "up")
        nsh(ns, "route", "replace", "192.168.%d.0/24" % n, "dev", "usb0", "table", "100")
        ensure_dns(ns, n)

        step = "web"
        sh("systemctl", "start", "proxyveth-web@%d" % n)
        if udc.startswith("usbip-vudc."):
            sh("usbip", "attach", "-r", "127.0.0.1", "-d", udc)
    except (Fail, OSError) as e:
        destroy(n, quiet=True)
        raise Fail("шаг %s: %s" % (step, e))
    m = st["modems"].setdefault(str(n), {})
    m.update(slot=udc, hash=spec_hash(p), plugged=time.time(), replugged=0)


def rebooting(n):
    try:
        return float(rd(os.path.join(RUN, "rebooting-%d" % n), "0")) > time.time()
    except ValueError:
        return False


def check_local(n, cfg):
    """Своя сторона: (класс, [проблемы]). ok | wait-host | rebooting | broken | absent."""
    ns, g = "pv%d" % n, gdir(n)
    if not os.path.exists("/run/netns/" + ns):
        return "absent", ["модема нет"]
    if rebooting(n):
        return "rebooting", ["перезагружается (как настоящий: USB вынут)"]
    probs = []
    if not os.path.isdir(g) or not rd(g + "/UDC"):
        probs.append("USB-гаджет не воткнут")
    if "192.168.%d.1/" % n not in nsh(ns, "-4", "-o", "addr", "show", "usb0", check=False).stdout:
        probs.append("usb0 без адреса")
    for unit in ("proxyveth-dns@%d" % n, "proxyveth-px@%d.socket" % n, "proxyveth-sb@%d" % n, "proxyveth-web@%d" % n):
        if sh("systemctl", "is-active", unit, check=False).stdout.strip() != "active":
            probs.append("%s не работает" % unit)
    if TUN not in nsh(ns, "route", "show", "default", "table", "100", check=False).stdout:
        probs.append("нет маршрута в туннель")
    if not dns_bound(ns, n):
        probs.append("dnsmasq не слушает 192.168.%d.1" % n)
    if probs:
        return "broken", probs
    busid, ifn = host_side(n)
    if not busid:
        return "wait-host", ["сервер не видит USB-устройство"]
    if host_mode(cfg) != "none":
        ip = "192.168.%d.100" % n
        if addr_of(ifn) != ip:
            return "wait-host", ["у %s нет %s" % (ifn or "?", ip)]
        if "from %s " % ip not in sh("ip", "rule", check=False).stdout.replace("\t", " ") + " ":
            return "wait-host", ["у сервера пропало правило маршрутизации для %s" % ip]
    return "ok", []


def replug(n, st=None):
    """Как вынуть модем и вставить обратно. Если сервер его видит — со стороны
    сервера (так же делает USB reset mp.space), иначе — перепривязка гаджета."""
    busid, _ = host_side(n)
    if busid:
        try:
            wr("/sys/bus/usb/drivers/usb/unbind", busid)
        except OSError:
            pass
        time.sleep(2)
        wr("/sys/bus/usb/drivers/usb/bind", busid)
        return "USB переподключён со стороны сервера"
    udc = unbind(n) or (st or state())["modems"].get(str(n), {}).get("slot", "")
    time.sleep(1)
    bind(n, udc)
    return "гаджет перепривязан к %s" % udc


# ── прокси и настоящий модем ───────────────────────────────────────────────
def socks_connect(p, dst, dport, timeout=8):
    """TCP через SOCKS5. Исключение несёт класс поломки первым словом."""
    try:
        s = socket.create_connection((p["host"], p["port"]), timeout=timeout)
    except OSError as e:
        raise Fail("proxy-down прокси %s:%d не отвечает: %s" % (p["host"], p["port"], e))
    s.settimeout(timeout)
    stage = "proxy-error"
    try:
        s.sendall(b"\x05\x01\x02")
        r = s.recv(64)
        if r[:4] == b"HTTP":
            raise Fail("proxy-down на %s:%d отвечает HTTP, а не SOCKS5 — не тот порт?" % (p["host"], p["port"]))
        if len(r) < 2 or r[0] != 5:
            raise Fail("proxy-down на %s:%d отвечает не SOCKS5" % (p["host"], p["port"]))
        if r[1] == 0xFF:
            raise Fail("proxy-auth прокси не принимает вход по логину/паролю")
        if r[1] == 2:
            u, pw = p["user"].encode(), p["pw"].encode()
            s.sendall(b"\x01" + bytes([len(u)]) + u + bytes([len(pw)]) + pw)
            r = s.recv(2)
            if len(r) < 2 or r[1] != 0:
                raise Fail("proxy-auth логин/пароль не приняты")
        try:
            req = b"\x05\x01\x00\x01" + socket.inet_aton(dst)
        except OSError:
            req = b"\x05\x01\x00\x03" + bytes([len(dst)]) + dst.encode()
        stage = "connect-fail"
        s.sendall(req + struct.pack(">H", dport))
        r = s.recv(10)
        if len(r) >= 2 and r[1] == 2:
            raise Fail("connect-denied прокси запретил соединение с %s:%d (код 2)" % (dst, dport))
        if len(r) < 2 or r[1] != 0:
            raise Fail("connect-fail прокси не соединил с %s:%d (код %s)" % (dst, dport, r[1] if len(r) > 1 else "?"))
        return s
    except Fail:
        s.close()
        raise
    except OSError as e:
        s.close()
        if stage == "connect-fail":
            raise Fail("connect-fail %s при соединении с %s:%d" % (e.__class__.__name__, dst, dport))
        raise Fail("proxy-error прокси оборвал рукопожатие (%s) — лимит соединений?" % e.__class__.__name__)


def http(s, host, path, cookie="", token="", body=None):
    if body is None:
        req = "GET %s HTTP/1.1\r\nHost: %s\r\n" % (path, host)
    else:
        req = ("POST %s HTTP/1.1\r\nHost: %s\r\nContent-Type: application/x-www-form-urlencoded; charset=UTF-8\r\n"
               "X-Requested-With: XMLHttpRequest\r\nContent-Length: %d\r\n" % (path, host, len(body.encode())))
        if token:
            req += "__RequestVerificationToken: %s\r\n" % token
    if cookie:
        req += "Cookie: %s\r\n" % cookie
    s.sendall((req + "Connection: close\r\n\r\n" + (body or "")).encode())
    data = b""
    while True:
        try:
            c = s.recv(65536)
        except OSError:
            break
        if not c:
            break
        data += c
    s.close()
    head, _, b = data.partition(b"\r\n\r\n")
    code = int(head.split(b" ")[1]) if head.startswith(b"HTTP/") else 0
    return code, head.decode("latin1"), b.decode("utf-8", "replace")


def xml(tag, text):
    m = re.search(r"<%s>([^<]*)</%s>" % (tag, tag), text)
    return m.group(1) if m else ""


def hilink(p, path, body=None, timeout=25):
    """Запрос к веб-морде настоящего модема через его прокси (сессия + токен HiLink).
    Смена режима сети у модема идёт секунды — ждём ответа дольше, чем диагностика."""
    real = "192.168.%d.1" % p["real"]
    _, _, b = http(socks_connect(p, real, 80), real, "/api/webserver/SesTokInfo")
    ses, tok = xml("SesInfo", b), xml("TokInfo", b)
    if not ses:
        raise Fail("modem-unreachable веб-морда %s не отдала сессию" % real)
    xmlbody = None if body is None else '<?xml version="1.0" encoding="UTF-8"?><request>%s</request>' % body
    return http(socks_connect(p, real, 80, timeout=timeout), real, path, ses, tok, xmlbody)


def public_ip(p):
    try:
        ip = http(socks_connect(p, IP_URL[0], IP_URL[1], timeout=12), IP_URL[0], IP_URL[2])[2].strip()
        return ip if re.fullmatch(r"\d+\.\d+\.\d+\.\d+", ip) else ""
    except Fail:
        return ""


def check_up(p, udp=False):
    """→ (класс, [подробности], внешний IP). Классы по цепочке: proxy-down, proxy-error,
    proxy-auth, modem-blocked, modem-unreachable, sim, no-data, captive, no-internet, ok."""
    real = "192.168.%d.1" % p["real"]
    info = []
    try:
        s = socks_connect(p, real, 80)
    except Fail as e:
        cls, msg = str(e).split(" ", 1)
        if cls == "connect-denied":
            # Код 2 бывает и от неверного пароля (прокси пускает на рукопожатии и режет
            # на соединении), и от запрета частных адресов. Различаем по интернету.
            try:
                socks_connect(p, CHECK_URL[0], CHECK_URL[1]).close()
                return "modem-blocked", ["прокси пускает в интернет, но не в сеть модема %s — это не ген1?" % real], ""
            except Fail:
                return "proxy-auth", ["прокси принимает вход, но ничего не пускает (код 2) — неверный логин/пароль?"], ""
        if cls == "connect-fail":
            return "modem-unreachable", ["прокси работает, но до %s не достучаться: %s" % (real, msg)], ""
        return cls, [msg], ""
    code, _, body = http(s, real, "/api/webserver/SesTokInfo")
    if code != 200 or "<SesInfo>" not in body:
        return "modem-unreachable", ["%s отвечает, но это не HiLink (HTTP %s)" % (real, code)], ""
    try:
        _, _, st = http(socks_connect(p, real, 80), real, "/api/monitoring/status", xml("SesInfo", body))
        conn, sim = xml("ConnectionStatus", st), xml("SimStatus", st)
        info.append("модем: связь %s, SIM %s, сеть %s, сигнал %s" % (
            {"901": "есть", "902": "нет", "900": "подключается", "903": "рвётся"}.get(conn, conn or "?"),
            {"1": "ок", "0": "ошибка", "255": "нет SIM"}.get(sim, sim or "?"),
            xml("CurrentNetworkType", st) or "?", xml("SignalIcon", st) or "?"))
        if sim and sim != "1":
            return "sim", info, ""
        if conn and conn != "901":
            return "no-data", info, ""
    except Fail as e:
        info.append("статус модема не прочитался: %s" % e)
    # Интернет: одна медленная проверка через слабую прокси — ещё не поломка.
    # Две разные цели и повтор первой при таймауте; портал оператора — сразу.
    why = ""
    for host, port, path in (CHECK_URL, CHECK_URL, CHECK_URL2):
        try:
            code, head, _ = http(socks_connect(p, host, port, timeout=12), host, path)
        except Fail as e:
            why = "модем на связи, но в интернет не выходит: %s" % str(e).split(" ", 1)[1]
            continue
        if code == 204:
            break
        if code == 0:
            why = "соединение открывается, но ответа нет — трафик уходит в никуда"
            continue
        loc = re.search(r"(?im)^location:\s*(\S+)", head)
        return "captive", info + ["вместо 204 пришёл HTTP %s%s — похоже, SIM не оплачена"
                                  % (code, (" → " + loc.group(1)) if loc else "")], ""
    else:
        return "no-internet", info + [why], ""
    ip = public_ip(p)
    if udp:
        info.append("UDP: %s" % udp_probe(p))
    return "ok", info, ip


def check_up_safe(p, udp=False):
    try:
        return check_up(p, udp)
    except OSError as e:          # оборвалось посреди обмена — для прокси ген1 бывает
        return "proxy-error", ["прокси оборвал соединение (%s)" % e.__class__.__name__], ""


def udp_probe(p):
    try:
        s = socket.create_connection((p["host"], p["port"]), timeout=8)
        s.sendall(b"\x05\x01\x02")
        s.recv(2)
        u, pw = p["user"].encode(), p["pw"].encode()
        s.sendall(b"\x01" + bytes([len(u)]) + u + bytes([len(pw)]) + pw)
        s.recv(2)
        s.sendall(b"\x05\x03\x00\x01\x00\x00\x00\x00\x00\x00")
        r = s.recv(10)
        if len(r) < 10 or r[1] != 0:
            return "UDP ASSOCIATE отклонён"
        relay = (socket.inet_ntoa(r[4:8]), struct.unpack(">H", r[8:10])[0])
        q = struct.pack(">HHHHHH", 7, 0x0100, 1, 0, 0, 0) + b"\x07example\x03com\x00\x00\x01\x00\x01"
        us = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        us.settimeout(4)
        us.connect(relay)
        us.send(b"\x00\x00\x00\x01" + socket.inet_aton("8.8.8.8") + struct.pack(">H", 53) + q)
        try:
            us.recv(2048)
            return "работает (ретранслятор %s:%d)" % relay
        except ConnectionRefusedError:
            return "не работает: ретранслятор %s:%d закрыт (ICMP port unreachable) — чинится на стороне прокси" % relay
        except socket.timeout:
            return "не работает: ретранслятор %s:%d молчит" % relay
    except Exception as e:
        return "проверка не удалась: %s" % e


def wait_real_modem(p, limit=180, least=10):
    """Ждём, пока настоящий модем после перезагрузки снова отвечает через прокси."""
    t0 = time.time()
    time.sleep(least)
    while time.time() - t0 < limit:
        try:
            code, _, b = http(socks_connect(p, "192.168.%d.1" % p["real"], 80, timeout=5),
                              "192.168.%d.1" % p["real"], "/api/webserver/SesTokInfo")
            if code == 200 and "<SesInfo>" in b:
                return time.time() - t0
        except Fail:
            pass
        time.sleep(3)
    return None


# ── состояние, предупреждения, оповещения ──────────────────────────────────
def health_path():
    return os.path.join(LIB, "usb-health.json")


def evaluate(desired, cfg, fresh=None, udp=False, upstream=True):
    """Своя сторона всегда свежая. Прокси: fresh=None — проверить все; иначе —
    fresh, неисправные и давно не проверенные, остальным — из запаса.
    upstream=False — прокси не трогать вовсе."""
    hp = jload(health_path(), {})
    now = time.time()
    ns = sorted(set(running()) | set(desired))
    need = [n for n in ns if upstream and n in desired and (
        fresh is None or n in fresh or hp.get(str(n), {}).get("up") != "ok"
        or now - hp.get(str(n), {}).get("t", 0) > cfg["diag_interval"])]
    with ThreadPoolExecutor(6) as ex:                 # прокси не любят толпу с одного IP
        res = dict(zip(need, ex.map(lambda n: check_up_safe(desired[n], udp), need)))
    rows = []
    for n in ns:
        old = hp.get(str(n), {})
        cls, probs = check_local(n, cfg)
        busid, ifn = host_side(n) if cls != "absent" else ("", "")
        if n in res:
            ucls, uinfo, ip = res[n]
            t = now
        elif n in desired:
            ucls, uinfo, ip, t = old.get("up", "?"), old.get("up_info", []), old.get("ip", ""), old.get("t", 0)
        else:
            ucls, uinfo, ip, t = "—", ["нет в таблице"], "", 0
        rows.append({"n": n, "real": desired[n]["real"] if n in desired else None, "local": cls,
                     "local_problems": probs, "usb": busid, "iface": ifn, "host_ip": addr_of(ifn),
                     "up": ucls, "up_info": uinfo, "ip": ip, "t": t})
    return rows


def verdict(r):
    """Для оповещений: ok | warn | broken (как было у vmodem)."""
    if r["local"] == "ok" and r["up"] == "ok":
        return "ok"
    if r["local"] in ("broken", "absent"):
        return "broken"
    return "warn"


def state_of(r):
    """Для Row (§6): ok | warn | broken | absent | rebooting."""
    if r["local"] in ("absent", "rebooting", "broken"):
        return r["local"]
    return "ok" if r["local"] == "ok" and r["up"] == "ok" else "warn"


def up_reason(r):
    return "%s: %s" % (UPSTREAM_TEXT.get(r["up"], r["up"]), (r["up_info"] or [""])[-1])


def reason(r):
    if r["local"] != "ok":
        return "; ".join(r["local_problems"])
    return up_reason(r)


def remember(rows, cfg, announce=True):
    """Запомнить состояние и сообщить только об изменениях. О поломке — когда она
    подтвердилась двумя проверками подряд (слабая прокси разок не ответить может),
    о восстановлении — сразу."""
    hp = jload(health_path(), {})
    changes = []
    for r in rows:
        key, old = str(r["n"]), hp.get(str(r["n"]), {})
        v = verdict(r)
        streak = old.get("streak", 0) + 1 if v == old.get("verdict") else 1
        told = old.get("told", "ok")
        if v != told and (v == "ok" or streak >= 2):
            changes.append((r["n"], v, reason(r) if v != "ok" else "снова в порядке" + (" (IP %s)" % r["ip"] if r["ip"] else "")))
            told = v
        hp[key] = {"verdict": v, "streak": streak, "told": told,
                   "since": old.get("since", time.time()) if old.get("verdict") == v else time.time(),
                   "up": r["up"], "up_info": r["up_info"], "ip": r["ip"] or old.get("ip", ""), "t": r["t"] or old.get("t", 0)}
    for key in list(hp):
        if int(key) not in {r["n"] for r in rows}:
            del hp[key]
    jsave(health_path(), hp, mode=0o644)
    if changes and announce:
        icon = {"ok": "✅", "warn": "⚠️", "broken": "❌"}
        text = "%s (%s)\n" % (socket.gethostname(), server_ip()) + "\n".join(
            "%s модем %d: %s" % (icon[v], n, why) for n, v, why in changes)
        notify(cfg, text)
    return changes


def server_ip():
    m = re.search(r"src (\S+)", sh("ip", "route", "get", "1.1.1.1", check=False).stdout)
    return m.group(1) if m else "?"


def notify(cfg, text):
    """Оповещения отложены (§1): код остался, настраиваются только в config.json."""
    nc = cfg["notify"]
    sent = []
    try:
        if nc.get("telegram_token") and nc.get("telegram_chat"):
            req = urllib.request.Request(
                "https://api.telegram.org/bot%s/sendMessage" % nc["telegram_token"],
                data=json.dumps({"chat_id": nc["telegram_chat"], "text": text}).encode(),
                headers={"Content-Type": "application/json"})
            urllib.request.urlopen(req, timeout=15).read()
            sent.append("telegram")
        if nc.get("webhook"):
            req = urllib.request.Request(nc["webhook"], headers={"Content-Type": "application/json"},
                                         data=json.dumps({"server": socket.gethostname(), "ip": server_ip(),
                                                          "text": text, "time": int(time.time())},
                                                         ensure_ascii=False).encode())
            urllib.request.urlopen(req, timeout=15).read()
            sent.append("webhook")
    except Exception as e:
        log("⚠ оповещение не ушло: %s" % e)
    return sent


def running():
    try:
        return sorted(int(x[2:]) for x in os.listdir("/run/netns") if re.fullmatch(r"pv\d+", x))
    except OSError:
        return []


def to_row(r, p, h, off=False):
    """Строка состояния для панели и меню — одинаковая для usb и gw (§6)."""
    probs = list(r["local_problems"]) if r["local"] != "ok" else []
    if r["local"] in ("ok", "wait-host") and r["up"] != "ok":
        probs.append(up_reason(r))
    if off:
        probs.append("выключен в таблице — снимется при следующем sync")
    return {"n": r["n"], "real": p["real"] if p else r.get("real"),
            "proxy": "%s:%d" % (p["host"], p["port"]) if p else None,
            "state": state_of(r), "problems": probs, "iface": r["iface"] or None,
            "ext_ip": r["ip"] or None, "since": int(h.get("since") or time.time())}


def blank_row(n, st, problem, p=None):
    return {"n": n, "real": p["real"] if p else None, "proxy": "%s:%d" % (p["host"], p["port"]) if p else None,
            "state": st, "problems": [problem], "iface": None, "ext_ip": None, "since": int(time.time())}


# ── сторона сервера: свой DHCP и маршруты, когда агрегатора нет ────────────
HOOK_SH = r"""#!/bin/sh
# proxyveth: udhcpc на стороне сервера — адрес от модема и маршрутизация по
# источнику. Таблицы — как у mobileproxy.space: 100 + третий октет % 200.
# Маршрута по умолчанию в main не ставим: интернет сервера модем не перехватит.
addrs() { ip -4 -o addr show dev "$interface" | awk '{split($4, a, "/"); print a[1]}'; }
case "$1" in
deconfig)
    for a in $(addrs); do ip rule del from "$a" 2>/dev/null; done
    ip -4 addr flush dev "$interface"
    ip link set "$interface" up
    ;;
bound|renew)
    for a in $(addrs); do
        [ "$a" = "$ip" ] && continue
        ip rule del from "$a" 2>/dev/null
        ip -4 addr flush dev "$interface"
    done
    ip addr replace "$ip/${mask:-24}" dev "$interface"
    t=$((100 + $(echo "$ip" | cut -d. -f3) % 200))
    gw="${router%% *}"; [ -n "$gw" ] || gw="${ip%.*}.1"
    net=$(ip -4 route show dev "$interface" proto kernel | awk '{print $1; exit}')
    ip route flush table "$t" 2>/dev/null
    [ -n "$net" ] && ip route replace "$net" dev "$interface" src "$ip" table "$t"
    ip route replace default via "$gw" dev "$interface" table "$t"
    ip rule del from "$ip" 2>/dev/null
    ip rule add from "$ip" table "$t"
    sysctl -qw "net.ipv4.conf.$interface.rp_filter=0"
    logger -t proxyveth-host "$interface: $ip, шлюз $gw, таблица $t"
    ;;
esac
exit 0
"""

# Только свои модемы (шины dummy_hcd и vhci_hcd для vudc): настоящие USB-модемы
# на том же сервере — забота hivelink.
HOST_RULE_TEXT = """# proxyveth: модем режима usb — DHCP и маршруты на стороне сервера
ACTION=="add", SUBSYSTEM=="net", DEVPATH=="/devices/platform/dummy_hcd.*|/devices/platform/vhci_hcd.*", DRIVERS=="cdc_ether|rndis_host|cdc_ncm|huawei_cdc_ncm", TAG+="systemd", ENV{SYSTEMD_WANTS}+="proxyveth-host@%k.service"
"""

HOST_LINK_TEXT = """# proxyveth: у всех E3372h один MAC. Имя по MAC (enx…) достаётся только первому
# модему, на втором udev падает с «File exists» и бросает устройство
# недонастроенным. Оставляем ядерные имена ethN — так же делает mobileproxy.space.
[Match]
Driver=cdc_ether rndis_host cdc_ncm huawei_cdc_ncm
[Link]
NamePolicy=
MACAddressPolicy=none
"""

MODEM_DRIVERS = ("cdc_ether", "rndis_host", "cdc_ncm", "huawei_cdc_ncm")


def modem_ifaces():
    """Сетевухи своих модемов на сервере (на шинах dummy_hcd / vhci_hcd)."""
    out = []
    for dev in glob.glob("/sys/class/net/*"):
        drv = os.path.basename(os.path.realpath(dev + "/device/driver")) if os.path.exists(dev + "/device/driver") else ""
        path = os.path.realpath(dev + "/device") if drv else ""
        if drv in MODEM_DRIVERS and ("/dummy_hcd." in path or "/vhci_hcd." in path):
            out.append(os.path.basename(dev))
    return sorted(out)


def apply_hostside(cfg):
    """Своя сторона сервера нужна, только когда адрес у модема больше некому взять."""
    want = host_mode(cfg) == "own"
    have = rd(HOST_RULE) == HOST_RULE_TEXT.strip() and os.path.exists(HOST_LINK)
    if want and not have:
        os.makedirs(os.path.dirname(HOST_LINK), exist_ok=True)
        os.makedirs(os.path.dirname(HOST_RULE), exist_ok=True)
        wr(HOST_LINK, HOST_LINK_TEXT)
        wr(HOST_RULE, HOST_RULE_TEXT)
        sh("udevadm", "control", "--reload", check=False)
        for ifn in modem_ifaces():       # уже воткнутые — прогнать через udev заново
            sh("udevadm", "trigger", "--action=add", "/sys/class/net/%s" % ifn, check=False)
            sh("systemctl", "start", "--no-block", "proxyveth-host@%s" % ifn, check=False)
        log("сторона сервера: свой DHCP и маршруты включены")
    elif not want and (os.path.exists(HOST_RULE) or os.path.exists(HOST_LINK)):
        for f in (HOST_RULE, HOST_LINK):
            if os.path.exists(f):
                os.unlink(f)
        sh("udevadm", "control", "--reload", check=False)
        sh("systemctl", "stop", "proxyveth-host@*", check=False)
        log("сторона сервера: отдаю модемы %s" % ("mobileproxy.space" if host_mode(cfg) == "mpspace" else "никому (hostside=off)"))


def hostside(iface):
    """Служебное для proxyveth-host@IFACE: udhcpc с нашим хуком."""
    os.execvp("udhcpc", ["udhcpc", "-f", "-i", iface, "-s", HOOK, "-t", "5", "-T", "2", "-A", "3", "-S"])


# ── прямые прокси на каждый модем ──────────────────────────────────────────
def proxy_path():
    return os.path.join(MDIR, "proxy.json")


def proxy_conf(cfg, ns):
    pc = cfg["proxy"]
    inb, outb, rules, dsrv = [], [], [], []
    for n in sorted(ns):
        inb.append({"type": "mixed", "tag": "in-%d" % n, "listen": pc["listen"], "listen_port": pc["base_port"] + n,
                    "users": [{"username": pc["user"], "password": pc["pass"]}]})
        # Имена — DNS самого модема, как у клиента за настоящим модемом.
        dsrv.append({"type": "tcp", "tag": "dns-%d" % n, "server": "192.168.%d.1" % n, "detour": "out-%d" % n})
        # Выход — с адреса модема: дальше маршрутизация по источнику ведёт в его USB.
        outb.append({"type": "direct", "tag": "out-%d" % n, "inet4_bind_address": "192.168.%d.100" % n,
                     "domain_resolver": {"server": "dns-%d" % n, "strategy": "ipv4_only"}})
        rules.append({"inbound": ["in-%d" % n], "outbound": "out-%d" % n})
    return {"log": {"level": "warn", "output": LOGD + "/proxy.log", "timestamp": True},
            "dns": {"servers": dsrv, "strategy": "ipv4_only"},
            "inbounds": inb, "outbounds": outb,
            "route": {"rules": rules + [{"action": "reject"}], "auto_detect_interface": False}}


def apply_proxy(cfg, ns):
    path = proxy_path()
    if not cfg["proxy"]["enabled"] or not ns:
        if os.path.exists(path):
            sh("systemctl", "disable", "--now", "proxyveth-proxy", check=False)
            os.unlink(path)
        return
    body = json.dumps(proxy_conf(cfg, ns), indent=1)
    if rd(path) == body.strip() and sh("systemctl", "is-active", "proxyveth-proxy", check=False).stdout.strip() == "active":
        return
    wr(path, body, 0o600)
    sh("systemctl", "enable", "proxyveth-proxy", check=False)
    sh("systemctl", "restart", "proxyveth-proxy")
    log("прямые прокси: %d портов (%d–%d)" % (len(ns), cfg["proxy"]["base_port"] + min(ns),
                                              cfg["proxy"]["base_port"] + max(ns)))


# ── установка ──────────────────────────────────────────────────────────────
def units():
    cmd = "%s %s" % (PY, CMD)
    return {
        "proxyveth-dns@.service": f"""[Unit]
Description=proxyveth: DHCP и DNS модема %i
[Service]
NetworkNamespacePath=/run/netns/pv%i
ExecStart=/usr/sbin/dnsmasq --keep-in-foreground --conf-file={MDIR}/%i/dnsmasq.conf
Restart=always
RestartSec=2
""",
        "proxyveth-px@.socket": f"""[Unit]
Description=proxyveth: выход модема %i к прокси (сокет внутри netns модема)
[Socket]
NetworkNamespacePath=/run/netns/pv%i
ListenStream=127.0.0.1:{RELAY_PORT}
""",
        # Сам ретранслятор — на сервере: принимает внутри netns, соединяется отсюда.
        "proxyveth-px@.service": f"""[Unit]
Description=proxyveth: выход модема %i к прокси
Requires=proxyveth-px@%i.socket
After=proxyveth-px@%i.socket
[Service]
EnvironmentFile={MDIR}/%i/env
ExecStart=/usr/lib/systemd/systemd-socket-proxyd --connections-max=4096 ${{SOCKS}}
""",
        # Без D-Bus: sing-box прописывает в systemd-resolved DNS своего tun по номеру
        # интерфейса, а номер — из netns модема. На сервере под тем же номером eth0 или
        # модем — и весь DNS сервера уходил на 172.20.0.2 внутри чужого netns.
        "proxyveth-sb@.service": f"""[Unit]
Description=proxyveth: туннель модема %i в SOCKS5
[Service]
NetworkNamespacePath=/run/netns/pv%i
InaccessiblePaths=-/run/dbus/system_bus_socket
ExecStart={singbox.BIN} run -c {MDIR}/%i/singbox.json
ExecStartPost={cmd} routes %i
Restart=always
RestartSec=2
""",
        "proxyveth-web@.service": f"""[Unit]
Description=proxyveth: веб-морда модема %i
[Service]
EnvironmentFile={MDIR}/%i/env
ExecStart={PY} {WEB} --netns /run/netns/pv%i --virt %i --real ${{REAL}} --socks ${{SOCKS}} --socks-user ${{SOCKS_USER}} --socks-pass-file {MDIR}/%i/proxy.pass --on-reboot "systemd-run --no-block --collect --unit=proxyveth-reboot-%i {cmd} replug %i --reboot"
Restart=always
RestartSec=2
""",
        "proxyveth-host@.service": f"""[Unit]
Description=proxyveth: DHCP и маршруты модема на %i (сторона сервера)
BindsTo=sys-subsystem-net-devices-%i.device
After=sys-subsystem-net-devices-%i.device
[Service]
ExecStart={cmd} hostside %i
Restart=on-failure
RestartSec=3
""",
        "proxyveth-proxy.service": f"""[Unit]
Description=proxyveth: прямые прокси на каждый модем
After=network-online.target
[Service]
ExecStart={singbox.BIN} run -c {proxy_path()}
Restart=always
RestartSec=3
[Install]
WantedBy=multi-user.target
""",
        "proxyveth-usbipd.service": """[Unit]
Description=proxyveth: usbipd для транспорта vudc (только 127.0.0.1)
[Service]
ExecStart=/usr/sbin/usbipd --device
Restart=always
""",
    }


def apt(*pkgs):
    missing = [p for p in pkgs if sh("dpkg", "-s", p, check=False).returncode != 0]
    if not missing:
        return
    log("ставлю: %s" % " ".join(missing))
    env = dict(os.environ, DEBIAN_FRONTEND="noninteractive")
    r = sh("apt-get", "-o", "DPkg::Lock::Timeout=600", "-y", "-qq", "install", *missing, check=False, env=env, timeout=1800)
    if r.returncode != 0:
        sh("apt-get", "-o", "DPkg::Lock::Timeout=600", "-qq", "update", check=False, env=env, timeout=600)
        sh("apt-get", "-o", "DPkg::Lock::Timeout=600", "-y", "-qq", "install", *missing, env=env, timeout=1800)


def ensure_module(cfg):
    # Первым: udc_core, нужный dummy_hcd, лежит там же, где libcomposite
    if sh("modprobe", "libcomposite", check=False).returncode != 0:
        # обновилось ядро, а модули USB-гаджетов для него не поставлены
        log("модулей USB-гаджетов для ядра %s нет — ставлю" % os.uname().release)
        apt("linux-modules-extra-" + os.uname().release)
        sh("modprobe", "libcomposite")
    if cfg["transport"] == "dummy" and not os.path.isdir("/sys/module/dummy_hcd"):
        if sh("modprobe", "dummy_hcd", check=False).returncode != 0:
            log("dummy_hcd для ядра %s нет — собираю" % os.uname().release)
            build_dummy_hcd()
            sh("modprobe", "dummy_hcd")
    if cfg["transport"] == "vudc" and not os.path.isdir("/sys/module/usbip_vudc"):
        sh("modprobe", "usbip-vudc")


def build_dummy_hcd():
    """dummy_hcd в Ubuntu не собран. Исходник — уже поправленный от прошлой сборки
    (свой или vmodem), иначе linux-source того же ядра, иначе апстрим той же серии;
    собираем через dkms — переживёт обновления ядра."""
    kver = os.uname().release
    series = ".".join(kver.split("-")[0].split(".")[:2])
    code = None
    for f in sorted(glob.glob("/usr/src/*-dummy-hcd-%s/dummy_hcd.c" % series)):
        try:
            with open(f) as fh:
                code = fh.read()
            break
        except OSError:
            pass
    for tar in glob.glob("/usr/src/linux-source-%s*.tar.*" % series):
        if code:
            break
        try:
            with tarfile.open(tar) as t:
                m = next(x for x in t.getmembers() if x.name.endswith("drivers/usb/gadget/udc/dummy_hcd.c"))
                code = t.extractfile(m).read().decode()
        except (StopIteration, OSError, tarfile.TarError):
            pass
    for url in ("https://raw.githubusercontent.com/torvalds/linux/v%s/drivers/usb/gadget/udc/dummy_hcd.c" % series,
                "https://git.kernel.org/pub/scm/linux/kernel/git/torvalds/linux.git/plain/drivers/usb/gadget/udc/dummy_hcd.c?h=v%s" % series):
        if code:
            break
        try:
            code = urllib.request.urlopen(url, timeout=60).read().decode()
        except Exception:
            pass
    if not code or "MAX_NUM_UDC" not in code:
        raise Fail("не нашёл исходник dummy_hcd для ядра %s" % kver)
    # Шин USB в ядре не больше 63, так что 64 экземпляров хватает с запасом.
    code = re.sub(r"#define MAX_NUM_UDC\s+\d+", "#define MAX_NUM_UDC\t64", code)
    apt("dkms", "linux-headers-" + kver)
    old = [(p, os.path.basename(p).rsplit("-", 1)) for p in
           glob.glob("/usr/src/vmns-dummy-hcd-*") + glob.glob("/usr/src/vmodem-dummy-hcd-*")]   # прототип v2, vmodem 4.x
    for _, (name, ver) in old:
        sh("dkms", "remove", "--all", "%s/%s" % (name, ver), check=False)
    src = "/usr/src/%s-%s" % (DKMS, series)
    os.makedirs(src, exist_ok=True)
    wr(src + "/dummy_hcd.c", code)
    wr(src + "/Makefile", "obj-m := dummy_hcd.o")
    wr(src + "/dkms.conf", 'PACKAGE_NAME="%s"\nPACKAGE_VERSION="%s"\nBUILT_MODULE_NAME[0]="dummy_hcd"\n'
                           'DEST_MODULE_LOCATION[0]="/updates"\nAUTOINSTALL="yes"' % (DKMS, series))
    try:
        sh("dkms", "install", "--force", "%s/%s" % (DKMS, series), "-k", kver, timeout=900)
    except Fail:
        for _, (name, ver) in old:            # не собралось — вернуть, что было
            sh("dkms", "install", "%s/%s" % (name, ver), "-k", kver, check=False, timeout=900)
        raise
    for path, _ in old:
        shutil.rmtree(path, ignore_errors=True)


def gadgets_bound():
    return [u for u in glob.glob(GROOT + "/*/UDC") if rd(u)]


def setup(cfg):
    """Подготовить сервер под режим usb. Повторять можно."""
    cfg = merge(DEFAULTS, cfg or {})
    for d in (ETC, MDIR, LIB, LOGD, RUN, os.path.dirname(HOOK)):
        os.makedirs(d, exist_ok=True)
    os.chmod(ETC, 0o700)
    os.chmod(MDIR, 0o700)
    if not os.path.exists(WEB):
        raise Fail("нет %s — код PCS неполный, обнови его (pcs update)" % WEB)
    kver = os.uname().release
    apt("dnsmasq-base", "udhcpc", "iptables", "iproute2")
    if sh("modinfo", "libcomposite", check=False).returncode != 0:
        apt("linux-modules-extra-" + kver)
    if cfg["transport"] == "vudc":
        apt("linux-tools-generic", "linux-tools-" + kver)
    try:
        if singbox.install():
            log("поставил sing-box %s (%s)" % (singbox.VERSION, singbox.BIN))
    except Fail:
        raise
    except Exception as e:                 # сеть, GitHub — повторить setup позже
        raise Fail("sing-box %s не скачался (%s) — проверь интернет сервера и повтори proxyveth setup" % (singbox.VERSION, e))
    if cfg["transport"] == "dummy" and sh("modinfo", "dummy_hcd", check=False).returncode != 0:
        log("собираю dummy_hcd под ядро %s (dkms, пару минут)" % kver)
        build_dummy_hcd()
    # Сколько модемов влезет: у каждого своя USB-шина, а шин в ядре максимум 63.
    slots = cfg["slots"]
    buses = len([b for b in glob.glob("/sys/bus/usb/devices/usb*") if "dummy_hcd" not in os.path.realpath(b)])
    if slots > 63 - buses:
        log("⚠ шин USB уже занято %d, а их в ядре не больше 63 — модемов влезет %d" % (buses, 63 - buses))
        slots = 63 - buses
        save_cfg(slots=slots)
    wr(MODPROBE, "options dummy_hcd num=%d\noptions usbip-vudc num=%d" % (slots, slots))
    wr(MODLOAD, "libcomposite\n" + ("dummy_hcd" if cfg["transport"] == "dummy" else "usbip-vudc"))
    # Упало ядро — перезагрузиться через 10 с, а не висеть: модемы после загрузки
    # поднимаются сами. И networkd не стирает маршрутизацию модемов (любой netplan apply).
    net.ensure_base(log=log)
    for name, body in units().items():
        wr(os.path.join(UNITD, name), body)
    wr(HOOK, HOOK_SH, 0o755)
    sh("systemctl", "daemon-reload")
    drop_tun_dns()
    sh("modprobe", "libcomposite")
    loaded = os.path.isdir("/sys/module/dummy_hcd")
    if cfg["transport"] == "dummy" and loaded and len(udc_slots("dummy")) != slots and not gadgets_bound():
        sh("modprobe", "-r", "dummy_hcd", check=False)       # выгружать — только без живых гаджетов
    ensure_module(cfg)
    if cfg["transport"] == "vudc":
        sh("systemctl", "enable", "--now", "proxyveth-usbipd", check=False)
    apply_hostside(cfg)
    have = len(udc_slots(cfg["transport"]))
    log("режим usb готов: слотов под модемы %d (%s), сторона сервера — %s" % (
        have, cfg["transport"], {"mpspace": "mobileproxy.space", "own": "своя (DHCP + маршруты)",
                                 "none": "никто"}[host_mode(cfg)]))
    if have and have < slots:
        log("⚠ модуль загружен со старым числом слотов (%d) — новое применится после перезагрузки" % have)


def create_all(queue, desired, cfg, st, strategy):
    """Втыкаем модемы по стратегии и ждём, пока сервер возьмёт адрес.
    window:W — не больше W модемов «в пути» (у mp.space ровно 5 слотов настройки);
    fixed:B:G:P — пачки по B, между модемами G с, между пачками P с; all — все сразу."""
    kind, *nums = strategy.split(":")
    nums = [float(x) for x in nums]
    t0, plugged, ready, failed = time.time(), {}, {}, {}
    need_ip = host_mode(cfg) != "none"

    def poll():
        have = set(re.findall(r"inet (192\.168\.\d+\.100)/", sh("ip", "-4", "-o", "addr", check=False).stdout))
        for n, tp in list(plugged.items()):
            if n in ready or n in failed:
                continue
            if ("192.168.%d.100" % n in have) if need_ip else host_side(n)[0]:
                ready[n] = time.time() - tp
            elif time.time() - tp > cfg["host_timeout"]:
                failed[n] = "сервер не взял адрес за %dс" % cfg["host_timeout"]

    def wait(sec):
        end = time.time() + sec
        while time.time() < end:
            poll()
            time.sleep(0.5)

    def start(n):
        try:
            create(desired[n], cfg, st)
            plugged[n] = time.time()
        except Fail as e:
            failed[n] = str(e)
            log("✗ модем %d: %s" % (n, e))

    if kind == "fixed":
        b, gap, pause = int(nums[0]), nums[1], nums[2]
        for i in range(0, len(queue), b):
            for n in queue[i:i + b]:
                start(n)
                wait(gap)
            if i + b < len(queue):
                wait(pause)
    else:
        w = 10 ** 6 if kind == "all" else int(nums[0])
        q = list(queue)
        while q:
            poll()
            inflight = [n for n in plugged if n not in ready and n not in failed]
            while q and len(inflight) < w:
                n = q.pop(0)
                start(n)
                inflight.append(n)
            time.sleep(0.5)
    while any(n not in ready and n not in failed for n in plugged):
        poll()
        time.sleep(0.5)
    return ready, failed, time.time() - t0


def report_created(strategy, ready, failed, total):
    tt = sorted(ready.values())
    log("создание (%s): готовы %d, нет %d, всего %.0f с; модему до адреса: медиана %.1f с, макс %.1f с" % (
        strategy, len(ready), len(failed), total, tt[len(tt) // 2] if tt else 0, tt[-1] if tt else 0))
    for n, w in sorted(failed.items()):
        log("✗ модем %d: %s" % (n, w))


# ── интерфейс режима (§6) ──────────────────────────────────────────────────
def apply(desired, cfg, force=False):
    """Привести модемы к таблице. desired — годные строки {n: строка}.
    cfg["keep"] — номера, которые не сносить, хоть их и нет в desired (кривая строка
    про работающий модем, подозрительная таблица — это решает cli).
    force — сломанные пересоздать сразу, без паузы после череды неудач."""
    cfg = merge(DEFAULTS, cfg or {})
    keep = set(cfg.get("keep") or ())
    st = state()
    ensure_module(cfg)
    apply_hostside(cfg)
    now = running()
    remove = [n for n in now if n not in desired and n not in keep]
    recreate, why = [], {}
    for n in now:
        if n not in desired:
            continue
        m = st["modems"].get(str(n), {})
        if m.get("hash") != spec_hash(desired[n]):
            recreate.append(n)
            why[n] = ("модем без учёта — пересоздаю" if not m.get("hash") else
                      "новая схема модема (proxyveth %s)" % VERSION if same_row(n, desired[n]) else
                      "поменялась строка таблицы")
            continue
        cls, probs = check_local(n, cfg)
        if cls == "broken" and (force or time.time() >= m.get("next_try", 0)):
            recreate.append(n)
            why[n] = "сломано: " + "; ".join(probs)
        elif cls == "wait-host" and time.time() - m.get("plugged", 0) > cfg["host_timeout"]:
            if not m.get("replugged"):
                log("модем %d: сервер не взял адрес — %s" % (n, replug(n, st)))
                m.update(replugged=1, plugged=time.time())
            else:
                recreate.append(n)
                why[n] = "сервер так и не взял адрес после переподключения"

    create_ = sorted(set(desired) - set(now))
    log("план: создать %s | пересоздать %s | снести %s | без изменений %d" % (
        create_ or "—", recreate or "—", remove or "—", len([n for n in now if n in desired and n not in recreate])))
    for n in recreate:
        log("  %d: %s" % (n, why[n]))

    for n in remove:
        destroy(n)
        st["modems"].pop(str(n), None)
    for n in recreate:
        m = st["modems"].setdefault(str(n), {})
        m["fails"] = m.get("fails", 0) + 1
        if m["fails"] > cfg["max_fail"]:
            m["next_try"] = time.time() + 600
            log("✗ модем %d пересоздаётся %d-й раз подряд — пауза 10 минут" % (n, m["fails"]))
    save_state(st)
    todo = create_ + sorted(recreate)
    ready, failed = {}, {}
    if todo:
        ready, failed, total = create_all(todo, desired, cfg, st, cfg["strategy"])
        for n in ready:
            st["modems"][str(n)]["fails"] = 0
        report_created(cfg["strategy"], ready, failed, total)
    save_state(st)
    park_idle_hcds(st)
    apply_proxy(cfg, [n for n in running() if n in desired or n in keep])
    rows = evaluate(specs_now(desired), cfg, fresh=set(todo))
    remember(rows, cfg)
    return {"created": [n for n in create_ if n in ready], "recreated": [n for n in sorted(recreate) if n in ready],
            "removed": remove, "failed": {n: failed[n] for n in sorted(failed)}}


def up(n=None):
    """Поднять модем N заново (или все из таблицы, которых нет)."""
    cfg, st = load_cfg(), state()
    ensure_module(cfg)
    try:
        if n is not None:
            p = one(n)
            create(p, cfg, st)
            log("модем %d поднят (%s)" % (n, st["modems"][str(n)]["slot"]))
            return
        desired = table_now()[0]
        todo = [k for k in sorted(desired) if k not in running()]
        if not todo:
            log("все модемы из таблицы уже есть")
            return
        ready, failed, total = create_all(todo, desired, cfg, st, cfg["strategy"])
        report_created(cfg["strategy"], ready, failed, total)
        if failed:
            raise Fail("не поднялись: %s" % ", ".join(map(str, sorted(failed))))
    finally:
        save_state(st)
        park_idle_hcds(st)


def down(n=None):
    """Снять модем N (или все). Таймер поднимет его снова, если он есть в таблице."""
    st = state()
    for k in ([n] if n is not None else running()):
        destroy(k)
        st["modems"].pop(str(k), None)
    save_state(st)
    park_idle_hcds(st)


def status(wan=False):
    """[Row] — все модемы: работающие, из таблицы, выключенные, отбракованные."""
    cfg = load_cfg()
    desired, disabled, invalid = table_now()
    specs = dict(desired)
    specs.update(live_specs())
    if wan:
        rows = evaluate(specs, cfg, fresh=None)
        remember(rows, cfg)
    else:
        rows = evaluate(specs, cfg, upstream=False)
    hp = jload(health_path(), {})
    out = [to_row(r, specs.get(r["n"]), hp.get(str(r["n"]), {}), r["n"] in disabled) for r in rows]
    seen = {r["n"] for r in rows}
    out += [blank_row(n, "disabled", "выключен в таблице") for n in sorted(disabled - seen)]
    out += [blank_row(n, "absent", "строка таблицы отбракована — proxyveth lint") for n in sorted(invalid - seen)]
    return sorted(out, key=lambda r: r["n"])


STEPS = ("прокси", "логин", "модем", "SIM", "интернет")
FAIL_AT = {"proxy-down": 0, "proxy-error": 0, "proxy-auth": 1, "modem-blocked": 2, "modem-unreachable": 2,
           "sim": 3, "no-data": 3, "captive": 4, "no-internet": 4, "ok": 5}


def diag(n, udp=False):
    """Прокси → логин → модем → SIM → интернет, потом своя сторона."""
    cfg = load_cfg()
    p = one(n)
    cls, info, ip = check_up_safe(p, udp)
    at = FAIL_AT.get(cls, 0)
    good = ["%s:%d отвечает по SOCKS5" % (p["host"], p["port"]),
            "логин %s принят" % p["user"],
            "веб-морда настоящего модема 192.168.%d.1 отвечает" % p["real"],
            info[0] if info and info[0].startswith("модем:") else "SIM и связь — без замечаний",
            "интернет есть" + (", внешний IP %s" % ip if ip else "")]
    steps = [{"name": STEPS[i], "ok": True, "text": good[i]} for i in range(min(at, len(STEPS)))]
    if at < len(STEPS):
        steps.append({"name": STEPS[at], "ok": False, "text": "%s: %s" % (UPSTREAM_TEXT.get(cls, cls), "; ".join(info))})
    elif len(info) > 1:
        steps[-1]["text"] += "; " + "; ".join(info[1:])
    lc, probs = check_local(n, cfg)
    busid, ifn = host_side(n)
    steps.append({"name": "своя сторона", "ok": lc == "ok",
                  "text": "%s; USB %s, у сервера %s %s" % ("в порядке" if lc == "ok" else "; ".join(probs),
                                                           busid or "—", ifn or "—", addr_of(ifn) or "")})
    r = {"local": lc, "up": cls}
    return {"n": n, "real": p["real"], "proxy": "%s:%d" % (p["host"], p["port"]), "steps": steps,
            "verdict": state_of(r), "ext_ip": ip or None,
            "text": "в порядке" if state_of(r) == "ok" else (UPSTREAM_TEXT.get(cls, cls) if cls != "ok" else "; ".join(probs))}


def rotate(n):
    """Сменить IP, как mp.space для типа 3: данные выкл → режим 02 → 03 → данные вкл."""
    p = one(n)
    before = public_ip(p)
    say("  модем %d: сейчас %s — меняю IP (данные выкл → режим 02 → 03 → данные вкл)" % (n, before or "?"))
    for i, body in enumerate(("<dataswitch>0</dataswitch>", "<NetworkMode>02</NetworkMode>" + BANDS,
                              "<NetworkMode>03</NetworkMode>" + BANDS, "<dataswitch>1</dataswitch>")):
        path = "/api/dialup/mobile-dataswitch" if "dataswitch" in body else "/api/net/net-mode"
        try:
            code, _, resp = hilink(p, path, body)
        except Fail as e:
            code, resp = 0, str(e)
        if code != 200 or "<error>" in resp:
            # Первый шаг не прошёл — до модема не достучались, дальше смысла нет.
            # Остальные модем иногда молча переваривает: итог решает смена IP.
            if i == 0:
                raise Fail("модем не принял команду %s: HTTP %s %s" % (path, code, xml("code", resp)))
            say("  ⚠ %s: модем ответил HTTP %s — продолжаю" % (path, code))
        time.sleep(3)
    t0 = time.time()
    while time.time() - t0 < 120:
        after = public_ip(p)
        if after and after != before:
            took = time.time() - t0 + 9
            say("  ✓ модем %d: IP сменился %s → %s (%.0f с)" % (n, before or "?", after, took))
            return {"n": n, "before": before or None, "after": after, "seconds": round(took)}
        time.sleep(5)
    raise Fail("IP не сменился за 2 минуты (был %s)" % (before or "?"))


def reboot_cycle(n, p, send=True):
    """Перезагрузка как у настоящего модема: USB пропадает и возвращается, когда
    модем снова отвечает. Сам модем перезагружаем, только если send."""
    flag = os.path.join(RUN, "rebooting-%d" % n)
    os.makedirs(RUN, exist_ok=True)
    wr(flag, str(time.time() + 240))
    try:
        if send:
            code, _, resp = hilink(p, "/api/device/control", "<Control>1</Control>")
            if code != 200 or "<error>" in resp:
                raise Fail("модем не принял перезагрузку: HTTP %s %s" % (code, xml("code", resp)))
        udc = unbind(n)
        log("модем %d: перезагружается — USB вынут" % n)
        took = wait_real_modem(p)
        bind(n, udc)
        log("модем %d: %s — USB вставлен" % (
            n, ("настоящий модем вернулся за %.0f с" % took) if took else "настоящий модем за 3 минуты не ответил"))
        return took
    finally:
        try:
            os.unlink(flag)
        except OSError:
            pass


def reboot(n):
    took = reboot_cycle(n, one(n), send=True)
    if not took:
        raise Fail("настоящий модем %d за 3 минуты не ответил — USB вставлен обратно, проверь: proxyveth diag %d" % (n, n))
    return {"n": n, "seconds": round(took)}


def teardown():
    """Снять всё своё: модемы, службы, сторону сервера. Настройки и таблица остаются."""
    st = state()
    ns = set(running()) | {int(g.rsplit("pv", 1)[1]) for g in glob.glob(GROOT + "/pv*")
                           if re.fullmatch(r"pv\d+", os.path.basename(g))}
    for n in sorted(ns):
        destroy(n)
        st["modems"].pop(str(n), None)
    sh("systemctl", "disable", "--now", "proxyveth-proxy", "proxyveth-usbipd", check=False)
    sh("systemctl", "stop", "proxyveth-host@*", check=False)
    for f in (HOST_RULE, HOST_LINK, MODPROBE, MODLOAD, HOOK, os.path.join(RUN, "usb.json"), health_path()) + \
            tuple(os.path.join(UNITD, u) for u in units()):
        try:
            os.unlink(f)
        except OSError:
            pass
    shutil.rmtree(MDIR, ignore_errors=True)
    sh("udevadm", "control", "--reload", check=False)
    sh("systemctl", "daemon-reload", check=False)
    park_idle_hcds(state())
    log("режим usb снят: модемов снято %d" % len(ns))


def doctor():
    cfg = load_cfg()
    out = []

    def chk(name, ok, text=""):
        out.append({"name": name, "ok": bool(ok), "text": text})

    chk("модули USB-гаджетов", os.path.isdir("/sys/module/libcomposite"), "libcomposite")
    slots = udc_slots(cfg["transport"])
    chk("слоты под модемы", slots, "%d (%s), занято %d" % (len(slots), cfg["transport"], len(gadgets_bound())))
    buses = len([b for b in glob.glob("/sys/bus/usb/devices/usb*") if "dummy_hcd" not in os.path.realpath(b)])
    chk("шины USB", buses + len(slots) <= 63, "своих шин %d, под модемы %d, предел ядра 63" % (buses, len(slots)))
    chk("sing-box %s" % singbox.VERSION, singbox.installed(), singbox.BIN)
    miss = [u for u in units() if not os.path.exists(os.path.join(UNITD, u))]
    chk("службы модемов", not miss, ("нет: %s — proxyveth setup" % ", ".join(miss)) if miss else "на месте")
    for prog in ("/usr/sbin/dnsmasq", "/usr/lib/systemd/systemd-socket-proxyd", WEB):
        chk(os.path.basename(prog), os.path.exists(prog), prog)
    chk("udhcpc", shutil.which("udhcpc") or host_mode(cfg) != "own", "нужен своей стороне сервера")
    hm = host_mode(cfg)
    chk("сторона сервера", hm != "own" or os.path.exists(HOST_RULE),
        {"mpspace": "mobileproxy.space", "own": "своя (DHCP + маршруты)", "none": "никто (hostside=off)"}[hm])
    foreign = tun_dns_links()
    chk("DNS сервера", not foreign, ("чужой 172.20.0.2 на %s — proxyveth setup" % ", ".join(foreign)) if foreign else "без следов sing-box")
    chk("база PCS", os.path.exists(net.SYSCTL) and os.path.exists(net.NETWORKD), "%s, %s" % (net.SYSCTL, net.NETWORKD))
    chk("vmodem 4.x", not vmodem_found(), "следов нет" if not vmodem_found() else "стоит старый vmodem — proxyveth setup перенесёт")
    return out


# ── служебное для юнитов ───────────────────────────────────────────────────
def routes(n):
    """ExecStartPost proxyveth-sb@N: маршруты через tun после каждого (пере)запуска."""
    spec = jload(os.path.join(mdir(n), "spec.json"), None)
    if not isinstance(spec, dict):
        raise Fail("у модема %d нет spec.json" % n)
    tun_routes(n, spec["real"])


def replug_cmd(n, reboot_=False):
    """proxyveth replug N [--reboot]. --reboot зовёт веб-морда, когда модему уже ушла перезагрузка."""
    if reboot_:
        reboot_cycle(n, one(n), send=False)
        return "перезагрузка отработана"
    return replug(n)


# ── переезд с vmodem 4.x (§13) ─────────────────────────────────────────────
OLD_FILES = ("/etc/udev/rules.d/80-vmodem-host.rules", "/etc/systemd/network/10-vmodem-cdc.link",
             "/etc/systemd/networkd.conf.d/10-vmodem.conf", "/etc/sysctl.d/90-vmodem.conf",
             "/etc/modprobe.d/vmodem.conf", "/etc/modules-load.d/vmodem.conf",
             "/usr/local/sbin/vmodem", "/usr/local/sbin/vmodem-api")
OLD_DIRS = ("/etc/vmodem", "/usr/local/lib/vmodem", "/run/vmodem", "/var/lib/vmodem")
OLD_KEYS = ("source", "transport", "slots", "strategy", "host_timeout", "diag_interval", "max_fail",
            "max_remove_share", "hostside", "usb", "proxy", "notify")


ROOT = "/"                    # корень, где искать vmodem (в тестах — временный каталог)


def at(path):
    return os.path.join(ROOT, path.lstrip("/"))


def vmodem_found():
    return (os.path.isdir(at("/etc/vmodem")) or os.path.exists(at("/usr/local/sbin/vmodem"))
            or bool(glob.glob(at("/etc/systemd/system/vmodem-*"))))


def vmodem_settings():
    """Настройки vmodem, которые переезжают в /etc/proxyveth/config.json."""
    old = jload(at("/etc/vmodem/config.json"), {})
    out = {k: old[k] for k in OLD_KEYS if k in old}
    if out.get("source", "") == "":
        out.pop("source", None)
    elif re.match(r"https?://", out["source"]):
        out["sheet"] = out["source"]
    return out


def vmodem_carry():
    """Таблица, состояние слотов и здоровье модемов — чтобы не начинать с нуля."""
    last = at("/var/lib/vmodem/last-good.csv")
    if os.path.exists(last) and not os.path.exists(TABLE):
        os.makedirs(os.path.dirname(TABLE), exist_ok=True)
        shutil.copyfile(last, TABLE)
        os.chmod(TABLE, 0o600)
    old_hp = at("/var/lib/vmodem/health.json")
    if os.path.exists(old_hp) and not os.path.exists(health_path()):
        jsave(health_path(), jload(old_hp, {}), mode=0o644)
    bad = jload(at("/run/vmodem/state.json"), {}).get("bad_slots") or []
    if bad:
        st = state()
        st["bad_slots"] = sorted(set(st["bad_slots"]) | set(bad))
        save_state(st)


def vmodem_down():
    """Снять модемы vmodem по-старому: таймер, юниты, гаджеты vmN, netns vmN.
    → номера снятых модемов."""
    sh("systemctl", "disable", "--now", "vmodem-sync.timer", check=False)    # чтобы не поднял обратно
    lk = None
    if os.path.isdir(at("/run/vmodem")):
        lk = lock(at("/run/vmodem/lock"), wait=600, what="команда vmodem")   # идущий sync — дождаться, не рвать
    try:
        sh("systemctl", "stop", "vmodem-sync.service", "vmodem-reboot-*", check=False)
        groot = at(GROOT)
        try:
            ns = {int(x[2:]) for x in os.listdir(at("/run/netns")) if re.fullmatch(r"vm\d+", x)}
        except OSError:
            ns = set()
        ns |= {int(os.path.basename(g)[2:]) for g in glob.glob(groot + "/vm*") if re.fullmatch(r"vm\d+", os.path.basename(g))}
        for n in sorted(ns):
            for u in ("api", "sb", "px", "dns"):
                part = ["vmodem-%s@%d.service" % (u, n)] + (["vmodem-px@%d.socket" % n] if u == "px" else [])
                sh("systemctl", "stop", *part, check=False)
                sh("systemctl", "reset-failed", *part, check=False)
            drop_gadget("%s/vm%d" % (groot, n))
            if os.path.exists(at("/sys/class/net/vx%d" % n)):      # veth модема до 4.1
                sh("ip", "link", "del", "vx%d" % n, check=False)
            sh("ip", "netns", "del", "vm%d" % n, check=False)
        sh("systemctl", "disable", "--now", "vmodem-proxy.service", "vmodem-usbipd.service", check=False)
        sh("systemctl", "stop", "vmodem-host@*", check=False)
        drop_old_root_net()
        if ns:
            log("vmodem: сняты модемы %s" % ",".join(map(str, sorted(ns))))
        return sorted(ns)
    finally:
        if lk:
            lk.close()


def drop_old_root_net():
    """До vmodem 4.1 у каждого модема была veth-пара vxN в корне и NAT 10.250/16 — убрать следы."""
    for t, chain, match in (("nat", "POSTROUTING", "-s 10.250.0.0/16"),
                            ("filter", "FORWARD", "-i vx+"), ("filter", "FORWARD", "-o vx+")):
        for line in sh("iptables", "-t", t, "-S", chain, check=False).stdout.splitlines():
            if match in line:
                sh("iptables", "-t", t, *shlex.split(line.replace("-A ", "-D ", 1)), check=False)


def vmodem_purge():
    """Удалить файлы vmodem (после того как поставлено новое). Журнал — в vmodem-4/."""
    sysd = at(UNITD)
    for f in glob.glob(sysd + "/vmodem-*") + glob.glob(sysd + "/*.wants/vmodem-*"):
        try:
            os.unlink(f)
        except OSError:
            pass
    for f in OLD_FILES:
        try:
            os.unlink(at(f))
        except OSError:
            pass
    for d in OLD_DIRS:
        shutil.rmtree(at(d), ignore_errors=True)
    oldlog = at("/var/log/vmodem")
    if os.path.isdir(oldlog):
        dst = at(LOGD + "/vmodem-4")
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        if os.path.exists(dst):
            shutil.rmtree(dst, ignore_errors=True)
        shutil.move(oldlog, dst)
    sh("systemctl", "daemon-reload", check=False)
    sh("udevadm", "control", "--reload", check=False)
    log("vmodem 4.x удалён: юниты, файлы, свой sing-box 1.10")


def vmodem_dkms():
    """Тот же dummy_hcd под своим именем dkms. Загруженный модуль не трогается, поэтому
    можно и при живых модемах — переезд зовёт это последним, чтобы модемы не ждали сборку."""
    if not glob.glob(at("/usr/src/vmodem-dummy-hcd-*")):
        return
    try:
        build_dummy_hcd()
        log("dummy_hcd: пакет dkms теперь %s" % DKMS)
    except Fail as e:
        log("⚠ dummy_hcd не пересобран под именем %s (%s) — работает старый пакет dkms vmodem-dummy-hcd" % (DKMS, e))
