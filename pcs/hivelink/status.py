"""hivelink: status и doctor — только чтение, ничего не меняют."""
import os
import platform
import shutil
import sys
from concurrent.futures import ThreadPoolExecutor

from .. import VERSION
from ..core.util import Fail, jload, rd, sh
from . import common, driver, hilink, install, rndis, route, usb

ROW_KEYS = ("n", "iface", "driver", "usb_port", "usb_id", "mode", "cfg", "addr", "conn", "conn_text",
            "dataswitch", "net", "signal", "ext_ip", "errors")


def local_rows(conf, addr_text=None):
    """Строки по настоящим USB-модемам из sysfs и адресов, без похода в сеть.
    Вторым — номера модемов, к которым можно идти в web-API (их подсеть не чужая)."""
    ok, bad = driver.ok_drivers(conf), conf["bad_drivers"]
    mods, addrs = driver.modems(conf, addr_text)
    busy = route.busy_octets(addrs, {m["iface"] for m in mods})
    w = driver.want(mods, addrs, conf)
    by_dev = {}
    for m in mods:
        by_dev.setdefault(m["dev"], []).append(m)
    rows, reach = [], set()
    for d in usb.devices():
        pid = usb.attr(d, "idProduct").lower()
        mode = usb.pid_mode(pid)
        cur, tot = usb.config(d)
        ms = by_dev.get(d, [])
        m = next((x for x in ms if x["driver"] in ok + bad), ms[0] if ms else None)
        drv = usb.net_driver(d, ok, bad) or None
        row = dict.fromkeys(ROW_KEYS)
        row.update(n=m["n"] if m else None, iface=m["iface"] if m else None, driver=drv,
                   usb_port=usb.port(d), usb_id="%s:%s" % (usb.attr(d, "idVendor").lower(), pid), mode=mode,
                   cfg="%s/%d" % ("?" if cur is None else cur, tot), addr=m["addr"] if m else None, errors=[])
        e = row["errors"]
        n = row["n"]
        if usb.claimed(d):
            e.append("отдан в ВМ или usbip — здесь не обслуживается")
        elif mode == "cdrom":
            e.append("Zero-CD: ждёт переключения (usb_modeswitch)")
        elif mode in ("stick", "stick-serial"):
            e.append("stick-прошивка — HiLink'ом не станет, нужна перепрошивка")
        elif drv in bad:
            e.append("драйвер %s (raw-IP, без DHCP) — подбираю USB-конфигурацию" % drv)
        elif not drv:
            e.append("сетевой драйвер не привязан")
        elif not row["addr"]:
            e.append("нет адреса (DHCP от модема)")
        elif n is None:
            e.append("адрес %s — не 192.168.N.100: модем настроен иначе" % row["addr"])
        elif n in busy:
            e.append("подсеть 192.168.%d.0/24 занята интерфейсом %s — модем пропущен" % (n, busy[n]))
        elif n not in w:
            e.append("в подсети 192.168.%d.0/24 два модема — этот пропущен" % n)
        else:
            reach.add(n)
        rows.append(row)
    return rows, reach


def probe_row(row, ext=True):
    p = hilink.probe(row["n"], row["addr"])
    row.update(conn=p["conn"], dataswitch=p["dataswitch"], net=p["net"], signal=p["signal"])
    row["conn_text"] = hilink.conn_text(p["conn"])
    row["errors"] += p["errors"]
    if p["dataswitch"] == 0:
        row["errors"].append("мобильные данные выключены (hivelink dataon %d)" % row["n"])
    if p["conn"] is not None and p["conn"] != 901:
        row["errors"].append("связь: %s" % row["conn_text"])
    if ext:
        row["ext_ip"] = hilink.ext_ip(row["addr"])
        if not row["ext_ip"]:
            row["errors"].append("в интернет через модем не выйти")
    return row


def summary(rows):
    s = {"total": len(rows), "hilink": 0, "zerocd": 0, "stick": 0, "other": 0, "with_addr": 0, "ok": 0}
    for r in rows:
        k = {"hilink": "hilink", "cdrom": "zerocd", "stick": "stick", "stick-serial": "stick"}.get(r["mode"], "other")
        s[k] += 1
        s["with_addr"] += bool(r["addr"])
        s["ok"] += not r["errors"]
    return s


def collect(conf, ext=True):
    rows, reach = local_rows(conf)
    todo = [r for r in rows if r["n"] in reach]
    if todo:
        with ThreadPoolExecutor(max_workers=min(16, len(todo))) as ex:
            list(ex.map(lambda r: probe_row(r, ext), todo))
    rows.sort(key=lambda r: (r["n"] is None, r["n"] or 0, r["usb_port"]))
    try:
        fix = rndis.state()
    except Fail:
        fix = None
    return {"version": VERSION, "installed": install.installed(), "net": common.net_mode(conf),
            "virtual": usb.virtual_count(), "usb": summary(rows), "fix": fix, "modems": rows}


def fix_text(fix, size):
    if size <= 0:
        return "выключен (rx_urb_size = 0)"
    if not fix:
        return "не проверить"
    if fix["active"]:
        return "активен, rx_urb_size = %d" % fix["size"]
    if fix["needed"]:
        return "НЕ активен: в памяти штатный rndis_host — приём ~1.3 Мбит (hivelink install)"
    return "собран, модуль не загружен (модемов с rndis_host нет)" if fix["dkms"] else "не собран (hivelink install)"


def show(data, conf):
    s = data["usb"]
    netw = {"on": "своя (DHCP через networkd, таблицы 2000+N)", "off": "у mp.space (hivelink её не трогает)"}
    print("  hivelink %s%s · USB-модемов %d: HiLink %d, Zero-CD %d, stick %d, прочих %d · с адресом %d · сеть: %s"
          % (data["version"], "" if data["installed"] else " (не установлен: hivelink install)", s["total"],
             s["hilink"], s["zerocd"], s["stick"], s["other"], s["with_addr"], netw[data["net"]]))
    if data["virtual"]:
        print("  виртуальных модемов proxyveth (dummy_hcd): %d — не трогаю" % data["virtual"])
    print("  фикс приёма rndis_host: %s" % fix_text(data["fix"], conf["rx_urb_size"]))
    if not data["modems"]:
        print("\n  настоящих USB-модемов Huawei нет")
        return
    fmt = "  %-4s %-10s %-11s %-10s %-4s %-14s %-6s %-4s %s"
    print()
    print(fmt % ("N", "IFACE", "DRIVER", "USB", "CFG", "СВЯЗЬ", "ДАННЫЕ", "СЕТЬ", "ВНЕШНИЙ IP"))
    dash = lambda v: "—" if v is None else str(v)       # noqa: E731
    for r in data["modems"]:
        print(fmt % (dash(r["n"]), dash(r["iface"]), dash(r["driver"]), r["usb_port"], r["cfg"],
                     dash(r["conn_text"]), dash(r["dataswitch"]), dash(r["net"]), dash(r["ext_ip"])))
        for e in r["errors"]:
            print("       ⚠ %s" % e)
    bad = sum(1 for r in data["modems"] if r["errors"])
    if bad:
        print("\n  с проблемами: %d — подробности: hivelink doctor, journalctl -t hivelink -n 50" % bad)


# ── doctor ──────────────────────────────────────────────────────────────────

def check(out, name, ok, text):
    out.append({"name": name, "ok": bool(ok), "text": text})


def foreign_in_range(rules_text):
    """Правила в диапазоне приоритетов hivelink, не похожие на свои."""
    out = []
    for p, sel, tbl in route.rules(rules_text):
        if route.ours(p):
            n = p - route.PRIO_MIN + route.N_MIN
            if sel != ["from", "192.168.%d.100" % n] or tbl != str(route.table(n)):
                out.append("%d: %s lookup %s" % (p, " ".join(sel), tbl))
    return out


def doctor():
    out = []
    osr = dict(ln.split("=", 1) for ln in rd("/etc/os-release").splitlines() if "=" in ln)
    check(out, "система", sys.version_info >= (3, 11),
          "%s, ядро %s, Python %s" % (osr.get("PRETTY_NAME", "?").strip('"'), platform.release(),
                                     platform.python_version()))
    check(out, "root", os.geteuid() == 0, "да" if os.geteuid() == 0 else "нет — часть проверок неполная")
    try:
        conf = common.load_conf()
        check(out, "настройки", True, common.CONF if os.path.exists(common.CONF) else "умолчания")
    except Fail as e:
        conf = dict(common.DEFAULTS)
        check(out, "настройки", False, str(e))
    files = [install.SERVICE, install.TIMER, install.UDEV, install.UDEV_PORTS, install.UDEV_MS]
    miss = [f for f in files if not os.path.exists(f)]
    check(out, "установка", not miss, "на месте" if not miss else "нет %s — hivelink install" % ", ".join(miss))
    check(out, "таймер", install.active("hivelink.timer"),
          "hivelink.timer работает" if install.active("hivelink.timer") else "hivelink.timer не запущен")
    legacy = install.legacy_found()
    check(out, "старый e3372-driver", not legacy,
          "нет" if not legacy else "остался (%s) — hivelink install снимет" % ", ".join(legacy[:3]))
    check(out, "usb_modeswitch", shutil.which("usb_modeswitch"),
          shutil.which("usb_modeswitch") or "нет — Zero-CD не переключится: apt-get install usb-modeswitch")
    mode = common.net_mode(conf)
    if mode == "on":
        nd = install.active("systemd-networkd")
        have = os.path.exists(install.NETWORK)
        check(out, "сеть модемов", nd and have, "своя: networkd %s, %s %s" % (
            "работает" if nd else "НЕ запущен", install.NETWORK, "есть" if have else "НЕТ"))
    else:
        check(out, "сеть модемов", not os.path.exists(install.NETWORK),
              "у mp.space (%s) — hivelink только USB и HiLink" % common.MP_SETUP)
    rules_text = sh("ip", "-4", "rule", "show", check=False).stdout
    foreign = foreign_in_range(rules_text)
    check(out, "правила 31001–31254", not foreign, "только свои" if not foreign else "чужие: %s" % "; ".join(foreign[:4]))
    rows, _ = local_rows(conf)
    s = summary(rows)
    check(out, "модемы", True, "настоящих %d (HiLink %d, Zero-CD %d, stick %d), с адресом %d; виртуальных %d — не трогаю"
          % (s["total"], s["hilink"], s["zerocd"], s["stick"], s["with_addr"], usb.virtual_count()))
    for r in rows:
        if r["errors"]:
            check(out, "USB %s" % r["usb_port"], False, "; ".join(r["errors"]))
    try:
        fix = rndis.state()
    except Fail:
        fix = None
    good = conf["rx_urb_size"] <= 0 or (fix and (fix["active"] and fix["size"] == conf["rx_urb_size"]
                                                 or not fix["needed"] and fix["dkms"]))
    check(out, "фикс приёма rndis_host", good, fix_text(fix, conf["rx_urb_size"]) +
          (" (файл %s)" % fix["file"] if fix and fix["file"] else ""))
    img = "/boot/initrd.img-" + platform.release()
    if conf["rx_urb_size"] > 0 and shutil.which("lsinitramfs") and os.path.exists(img):
        lines = [ln for ln in sh("lsinitramfs", img, check=False, timeout=300).stdout.splitlines() if "rndis_host" in ln]
        stock = lines and not any("/updates/" in ln for ln in lines)
        check(out, "initramfs", not stock, "в образе штатный rndis_host — после перезагрузки фикс не встанет "
              "(hivelink install)" if stock else "без штатного rndis_host")
    cmap = jload(common.CONFMAP, {})
    check(out, "выученные USB-конфигурации", True, ", ".join("%s→%s" % kv for kv in sorted(cmap.items())) or "пока нет")
    return out


def show_doctor(checks):
    for c in checks:
        print("  %s %-28s %s" % ("✓" if c["ok"] else "✗", c["name"], c["text"]))
    print("\n  USB-устройства Huawei:")
    for d in usb.devices():
        info = []
        for i in usb.interfaces(d):
            drv = os.path.basename(os.path.realpath(i + "/driver")) if os.path.exists(i + "/driver") else "-"
            nets = os.listdir(i + "/net") if os.path.isdir(i + "/net") else []
            info.append("%s:%s%s" % (i.rsplit(":", 1)[-1], drv, ("=" + ",".join(nets)) if nets else ""))
        cur, tot = usb.config(d)
        print("    %-12s %s:%-5s rev %-5s cfg %s/%d %-12s %s" % (
            usb.port(d), usb.attr(d, "idVendor"), usb.attr(d, "idProduct"), usb.attr(d, "bcdDevice") or "?",
            "?" if cur is None else cur, tot, usb.pid_mode(usb.attr(d, "idProduct")), " ".join(info)))
    print("\n  журнал: journalctl -t hivelink -n 80   ·   %s" % common.log.path)
