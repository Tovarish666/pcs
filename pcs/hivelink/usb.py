"""hivelink: настоящие USB-модемы в sysfs.

Настоящий модем — USB-устройство Huawei, которое НЕ сидит на виртуальной шине.
Виртуальные модемы proxyveth usb выглядят точь-в-точь как E3372h (12d1:14dc,
cdc_ether, тот же MAC), но их шина — dummy_hcd (или vhci_hcd у транспорта usbip):
`readlink -f` устройства содержит `/dummy_hcd.`. Их hivelink не трогает никогда.
"""
import fnmatch
import glob
import os
import re
import time

from ..core.util import rd, sh, wr

SYS = "/sys"                                    # подменяется в тестах
VIRTUAL = ("/dummy_hcd.", "/vhci_hcd.")         # шины виртуальных модемов proxyveth
VENDORS = ("12d1",)                             # Huawei; Vodafone K5150/K5160 — тоже 12d1
HUBS = ("1a40", "05e3", "2109", "0424")         # типовые хабы: автосуспенд роняет всю ветку
MODELS = {"Vodafone (Huawei)": "K5160", "HUAWEI_MOBILE": "E3372"}
ZEROCD = ("1f01", "1f02", "1f10", "1f11", "1f12", "1f13", "1f14", "1440")
UDEV_VIRTUAL = "*/dummy_hcd.*|*/vhci_hcd.*"     # то же для DEVPATH в udev
NETWORKD_VIRTUAL = "platform-dummy_hcd.* platform-vhci_hcd.*"   # то же для ID_PATH в .network

# Режим по PID — по INF оригинального пакета HUAWEI Drivers 5.05.01.158:
#   cdrom         FcSwitch.inf          Zero-CD, ждёт переключения (1f10–1f14 — Vodafone K5150/K5160)
#   hilink        ewqsnet.inf           RNDIS/ECM — рабочий режим
#   stick         ew_usbenumfilter.inf  composite stick, не HiLink
#   stick-serial  ew_hwusbdev*.inf      стик в serial-режиме, не HiLink
# PID поздних ревизий (E3372h-320) туда не входят — other, обслуживается по драйверу.
PID_MODES = (
    ("cdrom", "1f01 1f02 1f10 1f11 1f12 1f13 1f14 1440"),
    ("hilink", "1400 1441 14bb 14bc 14bd 14be 14d[7-9a-f] 14e[0-9a-f] 14f[6-9a] 14fd 1565 1566 "
               "157[5-9ab] 157f 159[0-2]"),
    ("stick", "150[6-9a-f] 14c[6-9a-f] 151[b-f] 156[b-f] 158[5-9] 159[3-68ace] 15a[02468a] 15b[0-9a] "
              "15d[1-9a-f] 15e[0-3] 15fd 1c2[5-9a]"),
    ("stick-serial", "1446 1449 14fe 14ff 1505 151a 156a 15ab 15ac 15ad 15ae 15af 15[2-4][0-9a-f] "
                     "155[0-9ab] 15c[a-f] 1c0b 1c1b 1c24"),
)


def pid_mode(pid):
    pid = (pid or "").lower()
    for mode, pats in PID_MODES:
        if any(fnmatch.fnmatchcase(pid, p) for p in pats.split()):
            return mode
    return "other"


def _p(*a):
    return os.path.join(SYS, *a)


def is_virtual(path):
    real = os.path.realpath(path)
    return any(v in real for v in VIRTUAL)


def attr(dev, name):
    return rd(os.path.join(dev, name))


def port(dev):
    return os.path.basename(dev)


def _usb_devs(vendors):
    for d in sorted(glob.glob(_p("bus", "usb", "devices", "*"))):
        if ":" in os.path.basename(d):                 # интерфейс, а не устройство
            continue
        if attr(d, "idVendor").lower() in vendors:
            yield d


def devices(vendors=VENDORS):
    """Настоящие USB-устройства модемов (каталоги /sys/bus/usb/devices/<порт>)."""
    return [d for d in _usb_devs(vendors) if not is_virtual(d)]


def virtual_count(vendors=VENDORS):
    return sum(1 for d in _usb_devs(vendors) if is_virtual(d))


def hubs():
    return [d for d in _usb_devs(HUBS) if not is_virtual(d)]


def models(devs):
    """{модель: сколько} по строке USB product («Vodafone (Huawei)» — K5160, «HUAWEI_MOBILE» — E3372)."""
    out = {}
    for d in devs:
        k = MODELS.get(attr(d, "product"), attr(d, "product") or attr(d, "idProduct"))
        out[k] = out.get(k, 0) + 1
    return dict(sorted(out.items(), key=lambda kv: -kv[1]))


def find(name):
    """Настоящее устройство по USB-порту (3-1.4.2) или None."""
    for d in devices():
        if port(d) == name:
            return d
    return None


def interfaces(dev):
    return sorted(glob.glob(os.path.join(dev, port(dev) + ":*")))


def _drv(path):
    link = os.path.join(path, "driver")
    return os.path.basename(os.path.realpath(link)) if os.path.exists(link) else ""


def drivers(dev):
    return [d for d in (_drv(i) for i in interfaces(dev)) if d]


def net_driver(dev, ok, bad):
    """Первый известный сетевой драйвер устройства (из ok или bad), иначе ''."""
    for d in drivers(dev):
        if d in ok or d in bad:
            return d
    return ""


def claimed(dev):
    """Устройство отдано в ВМ (QEMU держит его через usbfs) или в usbip — его не трогаем."""
    return _drv(dev) == "usbip-host" or "usbfs" in drivers(dev)


def ms_iface(dev):
    """Номер mass-storage интерфейса (CD модема) в текущей конфигурации или None. У Zero-CD
    Huawei он есть и в cfg 1, и в MBIM-конфигурации 2 (там — третьим интерфейсом)."""
    for i in interfaces(dev):
        if attr(i, "bInterfaceClass").lower() == "08":
            try:
                return int(attr(i, "bInterfaceNumber") or "0", 16)
            except ValueError:
                return None
    return None


def config(dev):
    """(текущая, всего) USB-конфигураций; текущая None — не читается."""
    cur = attr(dev, "bConfigurationValue")
    tot = attr(dev, "bNumConfigurations")
    try:
        tot = max(1, int(tot))
    except ValueError:
        tot = 1
    try:
        return int(cur), tot
    except ValueError:
        return None, tot


def set_config(dev, value):
    """Прямая запись обычно проходит (ядро само отвязывает драйверы); если занято —
    сначала расконфигурировать (-1), потом поставить."""
    path = os.path.join(dev, "bConfigurationValue")
    try:
        wr(path, str(value))
        return
    except OSError:
        pass
    try:
        wr(path, "-1")
    except OSError:
        pass
    time.sleep(1)
    try:
        wr(path, str(value))
    except OSError:
        pass


def parent_port(dev):
    """(каталог хаба, номер порта) для устройства «3-1.8.4.1.2» → (…/3-1.8.4.1, 2); у корня — None."""
    name = os.path.basename(dev)
    if "." not in name:
        return None
    hub, num = name.rsplit(".", 1)
    return os.path.join(os.path.dirname(dev), hub), num


def per_port_power(hub, run=sh):
    """Хаб умеет снимать питание с отдельного порта (а не со всех разом — ganged): по дескриптору
    хаба (lsusb -v). Дешёвые хабы 1a40:0101 — ganged: «отключить порт» там питание не снимает."""
    bus, num = attr(hub, "busnum"), attr(hub, "devnum")
    if not (bus and num):
        return False
    out = run("lsusb", "-v", "-s", "%s:%s" % (bus, num), check=False, timeout=20).stdout or ""
    return "Per-port power switching" in out


def power_cycle(dev, off=5, run=sh, sleep=time.sleep):
    """Снять питание с порта модема на off секунд (хаб с per-port power switching). → True — снято."""
    pp = parent_port(dev)
    if not pp or not per_port_power(pp[0], run):
        return False
    hub = os.path.basename(pp[0])
    ctl = os.path.join(pp[0], "%s:1.0" % hub, "%s-port%s" % (hub, pp[1]), "disable")
    try:
        wr(ctl, "1")
        sleep(off)
        wr(ctl, "0")
    except OSError:
        return False
    return True


def authorized(dev):
    """Устройство подключено логически (authorized=1). Нет файла — считаем подключённым."""
    return attr(dev, "authorized") != "0"


def set_authorized(dev, on):
    try:
        wr(os.path.join(dev, "authorized"), "1" if on else "0")
    except OSError:
        return False
    return True


def reenumerate(dev):
    """Логически отцепить и вернуть устройство (как CM_Reenumerate_DevNode в Windows).
    Питание при этом не снимается: зависшую радиочасть так не оживить."""
    path = os.path.join(dev, "authorized")
    if not os.path.exists(path):
        return False
    try:
        wr(path, "0")
        time.sleep(2)
        wr(path, "1")
    except OSError:
        return False
    return True


def ifaces(dev):
    """Сетевые интерфейсы устройства."""
    return sorted(os.path.basename(n) for n in glob.glob(os.path.join(dev, port(dev) + ":*", "net", "*")))


def iface_driver(ifn):
    return _drv(_p("class", "net", ifn, "device"))


def iface_dev(ifn):
    """USB-устройство сетевого интерфейса (каталог в /sys/bus/usb/devices) или ''."""
    link = _p("class", "net", ifn, "device")
    if not os.path.exists(link):
        return ""
    intf = os.path.realpath(link)
    name = os.path.basename(os.path.dirname(intf))
    if not re.match(r"^\d+-[\d.]+$", name) or ":" not in os.path.basename(intf):
        return ""
    dev = _p("bus", "usb", "devices", name)
    return dev if os.path.isdir(dev) else ""


def real_iface(ifn, vendors=VENDORS):
    """Интерфейс принадлежит настоящему модему (а не виртуальному и не сетевухе хоста)."""
    dev = iface_dev(ifn)
    return bool(dev) and not is_virtual(dev) and attr(dev, "idVendor").lower() in vendors
