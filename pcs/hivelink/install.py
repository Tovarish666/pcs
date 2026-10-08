"""hivelink: установка, снятие и миграция со старого e3372-driver (§13 D)."""
import os
import re
import shutil

from ..core import net
from ..core.util import Fail, jload, jsave, need_root, rd, sh
from . import common, driver, rndis, route, usb
from .common import log

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))   # /opt/pcs
BIN = os.path.join(ROOT, "bin", "hivelink")
CMD_LINK = "/usr/local/bin/hivelink"
SERVICE = "/etc/systemd/system/hivelink.service"
TIMER = "/etc/systemd/system/hivelink.timer"
UDEV = "/etc/udev/rules.d/70-hivelink.rules"
UDEV_PORTS = "/etc/udev/rules.d/71-hivelink-ports.rules"
# После штатного 40-usb_modeswitch.rules: правила идут по имени файла, 41 > 40
UDEV_MS = "/etc/udev/rules.d/41-hivelink-modeswitch.rules"
NETWORK = "/etc/systemd/network/25-hivelink.network"
PACKAGES = ("usb-modeswitch", "usb-modeswitch-data", "usbutils")

UDEV_TEXT = """# hivelink: реакция на события настоящих USB-модемов и питание шины.
# Виртуальные модемы proxyveth (шина dummy_hcd, usbip — vhci_hcd) — не наши.
ACTION=="remove", GOTO="hivelink_end"
SUBSYSTEM!="usb|net", GOTO="hivelink_end"
DEVPATH=="%(virtual)s", GOTO="hivelink_end"

# Автосуспенд выключен: уснувший хаб роняет всю ветку, уснувший модем — сотовую сессию
SUBSYSTEM=="usb", ENV{DEVTYPE}=="usb_device", ATTR{idVendor}=="12d1", TEST=="power/control", ATTR{power/control}="on"
SUBSYSTEM=="usb", ENV{DEVTYPE}=="usb_device", ATTR{idVendor}=="%(hubs)s", TEST=="power/control", ATTR{power/control}="on"

# Мгновенный проход драйвера. --no-block обязателен: проход по сорока модемам дольше
# бюджета udev; шторм событий схлопывается в один запуск (oneshot + блокировка).
SUBSYSTEM=="usb", ENV{DEVTYPE}=="usb_device", ATTR{idVendor}=="12d1", RUN+="/usr/bin/systemctl --no-block start hivelink.service"
SUBSYSTEM=="net", ACTION=="add", ATTRS{idVendor}=="12d1", RUN+="/usr/bin/systemctl --no-block start hivelink.service"

LABEL="hivelink_end"
"""

UDEV_PORTS_TEXT = """# hivelink: стабильные имена AT-портов Huawei по функции интерфейса, а не по номеру
# ttyUSB (номера разъезжаются при добавлении модема). Карта Prot → функция — из
# ew_cdcacm.inf оригинального пакета HUAWEI Drivers 5.05.01.158. HiLink ttyUSB не отдаёт —
# это для stick-режима и диагностики. Ключ — физический USB-путь: серийники совпадают.
# Результат: /dev/hivelink/<usb-путь>-pcui и т.д.
ACTION!="add", GOTO="hivelink_ports_end"
SUBSYSTEM!="tty", GOTO="hivelink_ports_end"
DEVPATH=="%(virtual)s", GOTO="hivelink_ports_end"
ATTRS{idVendor}!="12d1", GOTO="hivelink_ports_end"

IMPORT{builtin}="path_id"

ATTRS{bInterfaceProtocol}=="02|12|72",    ENV{HIVELINK_FN}="pcui"
ATTRS{bInterfaceProtocol}=="03|13|63|73", ENV{HIVELINK_FN}="diag"
ATTRS{bInterfaceProtocol}=="05|14|65|74", ENV{HIVELINK_FN}="gps"
ATTRS{bInterfaceProtocol}=="06|66",       ENV{HIVELINK_FN}="ctrl"
ATTRS{bInterfaceProtocol}=="0e|15|75",    ENV{HIVELINK_FN}="pcvoice"
ATTRS{bInterfaceProtocol}=="18",          ENV{HIVELINK_FN}="shella"
ATTRS{bInterfaceProtocol}=="19",          ENV{HIVELINK_FN}="shellb"
ATTRS{bInterfaceProtocol}=="1a",          ENV{HIVELINK_FN}="seriala"
ATTRS{bInterfaceProtocol}=="1b",          ENV{HIVELINK_FN}="serialb"
ATTRS{bInterfaceProtocol}=="1c",          ENV{HIVELINK_FN}="serialc"

ENV{HIVELINK_FN}=="", GOTO="hivelink_ports_end"
ENV{ID_PATH}=="", GOTO="hivelink_ports_end"
SYMLINK+="hivelink/$env{ID_PATH}-$env{HIVELINK_FN}"

LABEL="hivelink_ports_end"
"""

UDEV_MS_TEXT = """# hivelink: Zero-CD настоящих модемов переключает hivelink сам — по одному, под
# блокировкой и со счётчиками попыток. Штатный usb_modeswitch (40-usb_modeswitch.rules)
# для них отменяется: на двух десятках модемов это шторм, а его диспетчер после
# переключения уводит модем в MBIM-конфигурацию, и Zero-CD не дожимается никогда.
# Только Zero-CD PID Huawei и только не на виртуальной шине; остальное — как было.
ACTION!="add|change", GOTO="hivelink_ms_end"
SUBSYSTEM!="usb", GOTO="hivelink_ms_end"
DEVPATH=="%(virtual)s", GOTO="hivelink_ms_end"
ATTRS{idVendor}!="12d1", GOTO="hivelink_ms_end"
# RUN= (не +=) сбрасывает накопленное до этого места, то есть вызов usb_modeswitch из 40-го;
# правила с номерами больше 41 (загрузка драйверов и наши 70-е) добавятся после.
ATTRS{idProduct}=="%(zerocd)s", RUN="/bin/true"
LABEL="hivelink_ms_end"
"""

NETWORK_TEXT = """# hivelink: настоящие USB-модемы Huawei — DHCP от самого модема, полная изоляция
# от резолвинга и от основной таблицы маршрутов машины.
#
# Матч по драйверу и вендору: у всех E3372 один MAC, имена ethN не стабильны.
# Path=! исключает виртуальные модемы proxyveth (шина dummy_hcd, usbip — vhci_hcd):
# их адрес и маршруты делает сам proxyveth или mp.space.
# huawei_cdc_ncm/cdc_mbim не перечислены: это raw-IP без DHCP — такие модемы hivelink
# переводит на другую USB-конфигурацию.
[Match]
Driver=cdc_ether rndis_host
Property=ID_VENDOR_ID=12d1
Path=!%(paths)s

[Link]
# Не держать загрузку машины: часть модемов поднимается через минуту
RequiredForOnline=no

[Network]
DHCP=ipv4
LinkLocalAddressing=no
IPv6AcceptRA=no
LLDP=no
EmitLLDP=no
# DNS машины модемы не трогают ни при каких условиях (DNS — забота pcs.core.dns)
DNSDefaultRoute=no

[DHCPv4]
UseDNS=no
UseDomains=no
UseNTP=no
UseHostname=no
UseTimezone=no
# Шлюз и маршруты из DHCP не берём: сорок маршрутов по умолчанию подрались бы с
# основным каналом. Маршрутизация модема — в его таблице 2000+N (hivelink).
UseGateway=no
UseRoutes=no
RouteMetric=1000
# У всех модемов один MAC — идентификатор клиента не из него
ClientIdentifier=duid
"""

SERVICE_TEXT = """[Unit]
Description=hivelink: настоящие USB-модемы — проход приведения к рабочему состоянию
After=systemd-networkd.service
# udev стартует юнит на каждое событие модема; при включении сорока модемов это сотни
# стартов подряд — обычный лимит выбил бы юнит в failed. От наложения — блокировка.
StartLimitIntervalSec=0

[Service]
Type=oneshot
ExecStart=%(bin)s run
TimeoutStartSec=600
Nice=5
IOSchedulingClass=idle
SyslogIdentifier=hivelink
"""

TIMER_TEXT = """[Unit]
Description=hivelink каждые 15 с (страховка поверх udev)

[Timer]
# udev даёт мгновенную реакцию на появление устройства; таймер — для того, что без
# события: модем не дозвонился, DHCP не доехал, конфигурацию перебирают шагами.
OnBootSec=20
OnUnitActiveSec=15
AccuracySec=2s
Persistent=false

[Install]
WantedBy=timers.target
"""


def udev_text():
    return UDEV_TEXT % {"virtual": usb.UDEV_VIRTUAL, "hubs": "|".join(usb.HUBS)}


def udev_ports_text():
    return UDEV_PORTS_TEXT % {"virtual": usb.UDEV_VIRTUAL}


def udev_ms_text():
    return UDEV_MS_TEXT % {"virtual": usb.UDEV_VIRTUAL, "zerocd": "|".join(usb.ZEROCD)}


def network_text():
    return NETWORK_TEXT % {"paths": usb.NETWORKD_VIRTUAL}


def service_text(bin_path=BIN):
    return SERVICE_TEXT % {"bin": bin_path}


def installed():
    return os.path.exists(SERVICE) and os.path.exists(TIMER)


def active(unit):
    return sh("systemctl", "is-active", unit, check=False).stdout.strip() == "active"


# ── networkd ────────────────────────────────────────────────────────────────

def networkd_present():
    return any(os.path.exists(p) for p in ("/lib/systemd/systemd-networkd", "/usr/lib/systemd/systemd-networkd"))


def ensure_networkd():
    """DHCP модемов делает systemd-networkd. На Ubuntu он уже основной — не трогаем вовсе.
    На хосте Proxmox (ifupdown2) он выключен: запускаем — не перезапускаем; своих .network
    у него там нет, кроме нашего, поэтому основной канал хоста он не трогает."""
    if active("systemd-networkd"):
        return False
    if not networkd_present():
        common.apt_install(["systemd-networkd"])         # Debian 13: отдельный пакет
    wait = sh("systemctl", "is-enabled", "systemd-networkd-wait-online.service", check=False).stdout.strip()
    sh("systemctl", "enable", "--now", "systemd-networkd.service")
    common.set_flag("networkd_started", True)
    if wait != "enabled":
        # enable тянет за собой wait-online; без управляемых им линков он держал бы
        # загрузку машины две минуты
        sh("systemctl", "disable", "systemd-networkd-wait-online.service", check=False)
        common.set_flag("wait_online_disabled", True)
    log("systemd-networkd запущен для DHCP модемов (основной канал машины ему не отдан)")
    return True


def real_ifaces():
    return [ifn for d in usb.devices() for ifn in usb.ifaces(d)]


def sync_network(conf):
    """.network модемов есть, только когда сеть модемов наша (нет mp.space или net=on)."""
    on = common.net_mode(conf) == "on"
    changed = False
    if on:
        changed = common.put(NETWORK, network_text())
        ensure_networkd()
    elif os.path.exists(NETWORK):
        os.unlink(NETWORK)
        changed = True
        log("адрес и маршруты модемов делает mp.space — .network hivelink снят")
    if changed and active("systemd-networkd"):
        sh("networkctl", "reload", check=False)
        for ifn in real_ifaces():                       # только настоящие модемы
            sh("networkctl", "reconfigure", ifn, check=False)
    return changed


# ── миграция со старого e3372-driver ────────────────────────────────────────

LEGACY_UNITS = ("e3372-reconcile.timer", "e3372-reconcile.service", "e3372-watchdog.timer",
                "e3372-watchdog.service")
LEGACY_FILES = (
    "/etc/systemd/system/e3372-reconcile.service", "/etc/systemd/system/e3372-reconcile.timer",
    "/etc/systemd/system/e3372-watchdog.service", "/etc/systemd/system/e3372-watchdog.timer",
    "/etc/systemd/network/25-e3372.network", "/etc/systemd/network/25-hilink.network",
    "/etc/udev/rules.d/70-e3372.rules", "/etc/udev/rules.d/71-e3372-ports.rules",
    "/etc/sysctl.d/99-e3372.conf",
    "/usr/local/sbin/e3372-reconcile", "/usr/local/sbin/e3372-watchdog.sh",
    "/usr/local/bin/e3372-status", "/usr/local/bin/e3372-ctl", "/usr/local/bin/e3372-doctor",
    "/etc/networkd-dispatcher/routable.d/50-e3372",
)
LEGACY_DIRS = ("/usr/local/lib/e3372", "/run/e3372", "/var/lib/e3372", "/var/cache/e3372", "/etc/e3372")
LEGACY_CONF = "/etc/e3372/e3372.conf"
LEGACY_CONFMAP = "/var/lib/e3372/confmap"
LEGACY_MS_RULES = "/etc/udev/rules.d/40-usb_modeswitch.rules"
LEGACY_MS_PIDS = ("1f01", "1f02", "1f10", "1f11", "1f12", "1f13", "1f14", "1440", "14fe", "1505", "155a", "1c0b")
LEGACY_KEYS = {
    "PREFER_DRIVER": ("prefer_driver", str), "FALLBACK_DRIVERS": ("fallback_drivers", list),
    "BAD_DRIVERS": ("bad_drivers", list), "CFG_MAX_TRIES": ("cfg_max_tries", int),
    "ZEROCD_MAX_TRIES": ("zerocd_max_tries", int), "NODRV_GRACE": ("nodrv_grace", int),
    "DHCP_MAX_TRIES": ("dhcp_max_tries", int), "HILINK_EVERY": ("hilink_every", int),
    "HILINK_CONCURRENCY": ("hilink_concurrency", int), "AUTO_DATASWITCH": ("auto_dataswitch", bool),
    "AUTO_RECONNECT": ("auto_reconnect", bool), "RECONNECT_COOLDOWN": ("reconnect_cooldown", int),
    "USB_AUTOSUSPEND_OFF": ("usb_autosuspend_off", bool), "QUIET_REPEAT": ("quiet_repeat", int),
    "RX_URB_SIZE": ("rx_urb_size", int),
}


def legacy_found():
    return [p for p in LEGACY_FILES + LEGACY_DIRS if os.path.exists(p)]


def shell_vars(text):
    """KEY=VALUE из shell-конфига — разбором, без source."""
    out = {}
    for ln in text.splitlines():
        m = re.match(r"^\s*(?:export\s+)?([A-Z_][A-Z0-9_]*)=(.*)$", ln)
        if not m:
            continue
        v = m.group(2).strip()
        if v[:1] in "\"'":
            q = v[0]
            v = v[1:v.index(q, 1)] if q in v[1:] else v[1:]
        else:
            v = re.split(r"\s+#", v, 1)[0].strip()
        out[m.group(1)] = v
    return out


def legacy_conf(text):
    """Старый /etc/e3372/e3372.conf → ключи нового config.json (что удалось понять)."""
    out = {}
    for k, v in shell_vars(text).items():
        if k not in LEGACY_KEYS:
            continue
        key, kind = LEGACY_KEYS[k]
        try:
            out[key] = v.split() if kind is list else (v == "1") if kind is bool else int(v) if kind is int else v
        except ValueError:
            pass
    return out


def legacy_confmap(text):
    out = {}
    for ln in text.splitlines():
        f = ln.split()
        if len(f) == 2 and re.fullmatch(r"[0-9a-fA-F]{4}:[0-9a-fA-F]+", f[0]) and f[1].isdigit():
            out[f[0].lower()] = int(f[1])
    return out


def migrate():
    """Старый e3372-driver: снять своё по его путям, перенести настройки и выученное."""
    found = legacy_found()
    if not found:
        return None
    log("найден старый e3372-driver — снимаю его (настройки и выученные конфигурации переношу)")
    old = shell_vars(rd(LEGACY_CONF))
    conf = legacy_conf(rd(LEGACY_CONF))
    cmap = legacy_confmap(rd(LEGACY_CONFMAP))
    if old.get("HOST_PREFIX", "192.168") != "192.168" or old.get("HOST_OCTET", "100") != "100" \
            or old.get("GW_OCTET", "1") != "1":
        log("⚠ в старом конфиге своя адресация (HOST_PREFIX/HOST_OCTET/GW_OCTET) — в PCS она одна: "
            "модем 192.168.N.1, машина 192.168.N.100")
    for u in LEGACY_UNITS:
        sh("systemctl", "disable", "--now", u, check=False)
    try:
        base = int(old.get("RULE_PRIO_BASE", "1000"))
    except ValueError:
        base = 1000
    for c in route.legacy_plan(sh("ip", "-4", "rule", "show", check=False).stdout, base):
        sh(*c, check=False)
    if os.path.exists(LEGACY_MS_RULES) and rd(LEGACY_MS_RULES).startswith("# e3372-driver:"):
        os.unlink(LEGACY_MS_RULES)
    for pid in LEGACY_MS_PIDS:
        cfg = "/etc/usb_modeswitch.d/12d1:" + pid
        if os.path.exists(cfg + ".e3372.bak"):
            os.replace(cfg + ".e3372.bak", cfg)
        elif os.path.exists(cfg) and re.search(r"^HuaweiNewMode=1", rd(cfg), re.M):
            os.unlink(cfg)
    had_network = os.path.exists("/etc/systemd/network/25-e3372.network")
    for p in LEGACY_FILES:
        if os.path.lexists(p):
            os.unlink(p)
    for p in LEGACY_DIRS:
        shutil.rmtree(p, ignore_errors=True)
    sh("systemctl", "daemon-reload", check=False)
    sh("udevadm", "control", "--reload", check=False)
    if had_network and active("systemd-networkd"):
        sh("networkctl", "reload", check=False)
    # Старый DKMS-пакет снимается в rndis.ensure — только когда новый собран.
    # Значения sysctl старого 99-e3372.conf не откатываем: rp_filter=2 безопасен, а
    # свои настройки на интерфейсах модемов hivelink ставит сам.
    return {"conf": conf, "confmap": cmap, "removed": found}


# ── установка и снятие ──────────────────────────────────────────────────────

def write_conf(over=None):
    """Конфиг пользователя не затираем: только дописываем недостающие ключи умолчаниями.
    Перенесённое из старого e3372.conf берётся, только когда конфига ещё нет."""
    os.makedirs(common.ETC, mode=0o700, exist_ok=True)
    os.chmod(common.ETC, 0o700)
    if os.path.exists(common.CONF):
        common.load_conf()                      # битый — Fail, install его не трогает
        raw = jload(common.CONF, {})
        if not set(common.DEFAULTS) <= set(raw):
            jsave(common.CONF, dict(common.DEFAULTS, **raw))
    else:
        conf = dict(common.DEFAULTS)
        conf.update({k: v for k, v in (over or {}).items()
                     if k in conf and type(v) is type(conf[k])})
        jsave(common.CONF, conf)
    return common.load_conf()


def install(skip_fix=False):
    need_root()
    warnings = []
    mig = migrate()
    net.ensure_base(log=log)
    try:
        common.apt_install(PACKAGES)
    except Fail as e:
        warnings.append("пакеты не поставились: %s" % e)
    if not shutil.which("usb_modeswitch"):
        warnings.append("нет usb_modeswitch — модемы в Zero-CD (12d1:1f01) не переключатся; "
                        "поставь: apt-get install usb-modeswitch usb-modeswitch-data")
    conf = write_conf(mig["conf"] if mig else None)
    for d in (common.VAR, common.RUN, common.LOGDIR):
        os.makedirs(d, exist_ok=True)
    if mig and mig["confmap"] and not os.path.exists(common.CONFMAP):
        jsave(common.CONFMAP, mig["confmap"], mode=0o644)
    if os.path.realpath(CMD_LINK) != os.path.realpath(BIN):
        if os.path.lexists(CMD_LINK):
            os.unlink(CMD_LINK)
        os.symlink(BIN, CMD_LINK)
    rules = [common.put(UDEV, udev_text()), common.put(UDEV_PORTS, udev_ports_text()),
             common.put(UDEV_MS, udev_ms_text())]
    units = [common.put(SERVICE, service_text()), common.put(TIMER, TIMER_TEXT)]
    if any(rules):
        sh("udevadm", "control", "--reload", check=False)
    if any(units):
        sh("systemctl", "daemon-reload", check=False)
    try:
        sync_network(conf)
    except Fail as e:
        warnings.append("сеть модемов: %s" % e)
    fix = None
    if skip_fix:
        warnings.append("фикс приёма rndis_host пропущен")
    else:
        try:
            fix = rndis.ensure(conf["rx_urb_size"], log)
            if fix["active"]:
                log("фикс приёма rndis_host активен: rx_urb_size = %d" % fix["size"])
        except Fail as e:
            warnings.append("фикс приёма rndis_host не встал: %s — модемы работают, но приём на "
                            "RNDIS ~1.3 Мбит; повторить: hivelink install" % e)
    r = sh("systemctl", "enable", "--now", "hivelink.timer", check=False)
    if r.returncode != 0:
        raise Fail("не включился hivelink.timer: %s" % (r.stderr or r.stdout).strip()[:300])
    if any(rules):
        # Прогнать через новые правила уже воткнутые настоящие модемы — и только их
        for d in usb.devices():
            sh("udevadm", "trigger", "--action=change", d, check=False)
    driver.run_pass(conf)
    for w in warnings:
        log("⚠ " + w)
    return {"installed": True, "migrated": bool(mig), "net": common.net_mode(conf), "fix": fix,
            "warnings": warnings}


def uninstall():
    need_root()
    fl = common.flags()
    sh("systemctl", "disable", "--now", "hivelink.timer", check=False)
    sh("systemctl", "stop", "hivelink.service", check=False)
    for p in (SERVICE, TIMER, UDEV, UDEV_PORTS, UDEV_MS):
        if os.path.exists(p):
            os.unlink(p)
    sh("systemctl", "daemon-reload", check=False)
    sh("udevadm", "control", "--reload", check=False)
    if os.path.exists(NETWORK):
        os.unlink(NETWORK)
        if active("systemd-networkd"):
            sh("networkctl", "reload", check=False)
    for c in route.plan({}, sh("ip", "-4", "rule", "show", check=False).stdout,
                        sh("ip", "-4", "route", "show", "table", "all", check=False).stdout):
        sh(*c, check=False)
    warnings = []
    try:
        rndis.remove(log)
    except Fail as e:
        warnings.append("фикс rndis_host: %s" % e)
    if fl.get("networkd_started"):
        # networkd запускал hivelink (хост Proxmox) — других .network у него нет
        others = [f for f in os.listdir("/etc/systemd/network") if f.endswith(".network")] \
            if os.path.isdir("/etc/systemd/network") else []
        if others:
            warnings.append("systemd-networkd оставлен: в /etc/systemd/network есть %s" % ", ".join(others))
        else:
            sh("systemctl", "disable", "--now", "systemd-networkd.service", "systemd-networkd.socket", check=False)
            log("systemd-networkd остановлен (его запускал hivelink)")
    if fl.get("wait_online_disabled") and not fl.get("networkd_started"):
        sh("systemctl", "enable", "systemd-networkd-wait-online.service", check=False)
    for p in (common.ETC, common.VAR, common.CACHE, common.RUN):
        shutil.rmtree(p, ignore_errors=True)
    for w in warnings:
        log("⚠ " + w)
    return {"installed": False, "warnings": warnings}
