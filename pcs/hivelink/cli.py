"""Команда hivelink — драйвер настоящих USB-модемов (docs/ARCHITECTURE.md, §6)."""
import os
import re
import sys
import time

from ..core.util import QUIET, Busy, Fail, jload, jsave, lock, need_root, run_cli
from . import common, driver, hilink, install, status, usb
from .common import log

USAGE = """hivelink — драйвер настоящих USB-модемов Huawei (E3372, E3131, E353, Vodafone K5150/K5160):
Zero-CD → USB-конфигурация с rndis_host → DHCP → маршрут → данные HiLink → фикс приёма.

  hivelink install            поставить или обновить (повторять можно)
  hivelink uninstall          снять всё своё
  hivelink status [--fast] [--json]   модемы: драйвер, USB, связь, данные, сеть, внешний IP
                              (--fast — без внешнего IP: секунды вместо минуты)
  hivelink doctor [--json]    окружение, установка, фикс приёма
  hivelink reconnect N        переподключить модем N (HiLink)
  hivelink reset N            заново подключить USB-устройство модема N
  hivelink recfg N            забыть выученную USB-конфигурацию модема и подобрать заново
  hivelink dataon N|--all     включить мобильные данные
  hivelink off ПОРТ|ethN|N    выключить модем: отцепить от USB (authorized=0), и после перезагрузки
                              тоже — например, второй модем с тем же N; без аргумента — список
  hivelink on ПОРТ            включить выключенный
  hivelink number N [--ussd КОД] [--keep-mode]
                              номер SIM по USSD (код — по оператору: Yota *103#, Билайн *110*10#,
                              МТС *111*0887#, МегаФон *205#, Tele2 *201#); на время USSD режим сети —
                              «авто», потом как был (--keep-mode — оставить «авто»)

  N — номер модема (модем 192.168.N.1, машина 192.168.N.100); у reset и recfg можно
  USB-порт (3-1.4.2) — для модема, который до адреса не дошёл. --json — у всех команд.
  Виртуальные модемы proxyveth (шина dummy_hcd) hivelink не трогает.
"""


def say(text=""):
    if not QUIET[0]:
        print(text)


def held(wait=120):
    return lock(common.LOCK, wait=wait, what="команда hivelink")


def conf():
    return common.load_conf()


def target(arg, c):
    """Настоящий модем по номеру N или USB-порту: {iface, dev, port, driver, addr, n}."""
    mods, _ = driver.modems(c)
    if re.fullmatch(r"\d+", arg or ""):
        n = int(arg)
        if not 1 <= n <= 254:
            raise Fail("номер модема — 1–254")
        for m in mods:
            if m["n"] == n:
                return m
        raise Fail("модема N=%d нет среди настоящих: 192.168.%d.100 нет ни на одном USB-модеме Huawei "
                   "(виртуальные proxyveth hivelink не трогает) — hivelink status" % (n, n))
    if re.fullmatch(r"\d+-[\d.]+", arg or ""):
        d = usb.find(arg)
        if not d:
            raise Fail("на USB-порту %s настоящего модема Huawei нет — hivelink doctor" % arg)
        return next((m for m in mods if m["dev"] == d),
                    {"iface": None, "dev": d, "port": arg, "driver": None, "addr": None, "n": None})
    raise Fail("ждал номер модема 1–254 или USB-порт вида 3-1.4.2")


def with_n(m, c):
    """Модем доступен по web-API: есть адрес 192.168.N.100, и подсеть не делит с чужим."""
    if m["n"] is None:
        raise Fail("у модема на %s нет адреса 192.168.N.100 — web-API недоступен (hivelink status)" % m["port"])
    mods, addrs = driver.modems(c)
    if m["n"] not in driver.want(mods, addrs, c):
        raise Fail("подсеть 192.168.%d.0/24 у модема не одна (сеть машины, виртуальный модем или второй "
                   "модем) — web-API этого модема не выбрать; смени подсеть модема" % m["n"])
    return m


def one_arg(args, name):
    if len(args) != 1:
        raise Fail("нужен один номер модема: hivelink %s N" % name)
    return args[0]


def cmd_install(args):
    need_root()
    with held():
        # HIVELINK_SKIP_RNDIS_FIX=1 — без DKMS-сборки (приём на RNDIS останется ~1.3 Мбит)
        data = install.install(skip_fix=os.environ.get("HIVELINK_SKIP_RNDIS_FIX") == "1")
    say("  hivelink установлен%s: udev + hivelink.timer (15 с), сеть модемов — %s"
        % (" (старый e3372-driver снят)" if data["migrated"] else "",
           status.net_text(data["net"])))
    say("  состояние: hivelink status · разбор: hivelink doctor · журнал: journalctl -t hivelink -f")
    return 0, data


def cmd_uninstall(args):
    need_root()
    with held():
        data = install.uninstall()
    say("  hivelink снят: модемы остались как есть, но без маршрутов и без автоматики")
    return 0, data


def cmd_status(args):
    if [a for a in args if a != "--fast"]:
        raise Fail("лишние аргументы: %s — hivelink help" % " ".join(a for a in args if a != "--fast"))
    c = conf()
    data = status.collect(c, ext="--fast" not in args)
    if not QUIET[0]:
        status.show(data, c)
    return 0, data


def cmd_doctor(args):
    checks = status.doctor()
    if not QUIET[0]:
        status.show_doctor(checks)
    return 0, checks


def cmd_reconnect(args):
    need_root()
    c = conf()
    m = with_n(target(one_arg(args, "reconnect"), c), c)
    with held():
        api = hilink.Api(m["n"], src=m["addr"])
        try:
            api.dial(False)
            time.sleep(3)
            api.dial(True)
        except hilink.HiFail as e:
            raise Fail("N=%d: %s" % (m["n"], e))
        st = common.load_state()
        st["heal"].pop(str(m["n"]), None)         # лестница починки — с начала
        common.save_state(st)
    log("N=%d: переподключён (HiLink)" % m["n"])
    return 0, {"n": m["n"], "done": True}


def cmd_reset(args):
    need_root()
    m = target(one_arg(args, "reset"), conf())
    with held():
        if not usb.reenumerate(m["dev"]):
            raise Fail("USB %s: не вышло переподключить (нет authorized)" % m["port"])
    log("%s: USB-устройство переподключено%s" % (m["port"], " (N=%d)" % m["n"] if m["n"] else ""))
    return 0, {"n": m["n"], "usb_port": m["port"], "done": True}


def cmd_recfg(args):
    need_root()
    c = conf()
    m = target(one_arg(args, "recfg"), c)
    d, p = m["dev"], m["port"]
    key = "%s:%s" % (usb.attr(d, "idProduct").lower(), usb.attr(d, "bcdDevice") or "0")
    with held():
        cmap = jload(common.CONFMAP, {})
        if isinstance(cmap, dict) and cmap.pop(key, None) is not None:
            jsave(common.CONFMAP, cmap, mode=0o644)
        st = common.load_state()
        for k in ("cfgtry.", "nodrv.", "gaveup.", "zerocd."):
            st["cnt"].pop(k + p, None)
        st["probe"].pop(p, None)
        common.save_state(st)
        log("%s (%s): выученная USB-конфигурация забыта — подбираю заново" % (p, key))
        driver.run_pass(c)
    return 0, {"n": m["n"], "usb_port": p, "model": key, "done": True}


def cmd_dataon(args):
    need_root()
    c = conf()
    if args == ["--all"]:
        mods, addrs = driver.modems(c)
        w = driver.want(mods, addrs, c)
        todo = [m for m in mods if m["n"] in w and w[m["n"]][0] == m["iface"]]
        if not todo:
            raise Fail("настоящих модемов с адресом нет — hivelink status")
    else:
        todo = [with_n(target(one_arg(args, "dataon"), c), c)]
    done, failed = [], {}
    with held():
        for m in sorted(todo, key=lambda m: m["n"]):
            try:
                hilink.Api(m["n"], src=m["addr"]).set_data(True)
                done.append(m["n"])
                say("  ✓ N=%d: мобильные данные включены" % m["n"])
            except hilink.HiFail as e:
                failed[str(m["n"])] = str(e)
                say("  ✗ N=%d: %s" % (m["n"], e))
    if not done:
        raise Fail("; ".join("N=%s: %s" % kv for kv in failed.items()))
    return 0, {"done": done, "failed": failed}


def off_target(arg, c):
    """Модем для off: USB-порт, интерфейс или N. Два модема с одним N — только по порту."""
    mods, _ = driver.modems(c)
    if re.fullmatch(r"\d+", arg or ""):
        same = [m for m in mods if m["n"] == int(arg)]
        if len(same) > 1:
            raise Fail("у N=%s модемов %d: %s — укажи порт: hivelink off ПОРТ"
                       % (arg, len(same), ", ".join("%s (%s)" % (m["port"], m["iface"]) for m in same)))
    if re.fullmatch(r"(eth|enx|wwan|usb)[\w-]*", arg or ""):
        m = next((m for m in mods if m["iface"] == arg), None)
        if not m:
            raise Fail("%s — не интерфейс настоящего модема (hivelink status)" % arg)
        return m
    return target(arg, c)


def cmd_off(args):
    need_root()
    off = common.load_off()
    if not args:
        say("\n".join("  %s%s" % (p, " — был %s" % v.get("was") if v.get("was") else "") for p, v in sorted(off.items()))
            or "  выключенных модемов нет")
        return 0, {"off": off}
    m = off_target(one_arg(args, "off"), conf())
    p = m["port"]
    was = ", ".join(x for x in ("N=%d" % m["n"] if m["n"] else "", m["iface"] or "") if x)
    with held():
        off[p] = {"t": int(time.time()), "was": was}
        common.save_off(off)
        usb.set_authorized(m["dev"], False)
    log("%s: модем выключен%s — отцеплен от USB; включить: hivelink on %s" % (p, " (%s)" % was if was else "", p))
    return 0, {"usb_port": p, "was": was, "off": True}


def cmd_on(args):
    need_root()
    arg = one_arg(args, "on")
    off = common.load_off()
    d = next((x for x in usb.devices() if usb.port(x) == arg), None)
    if arg not in off and not (d and not usb.authorized(d)):
        raise Fail("модем на порту %s не выключен — выключенные: %s" % (arg, ", ".join(sorted(off)) or "нет"))
    with held():
        off.pop(arg, None)
        common.save_off(off)
        if d:
            usb.set_authorized(d, True)
    log("%s: модем включён%s" % (arg, "" if d else " (сейчас его на USB нет — подключится, когда появится)"))
    return 0, {"usb_port": arg, "off": False, "present": bool(d)}


def cmd_number(args):
    """Номер SIM модема N по USSD (pcs.core.simnum)."""
    from ..core import simnum
    need_root()
    ussd, keep = None, False
    rest = list(args)
    if "--keep-mode" in rest:
        rest.remove("--keep-mode")
        keep = True
    if "--ussd" in rest:
        i = rest.index("--ussd")
        if i + 1 >= len(rest):
            raise Fail("--ussd КОД: например *103#")
        ussd = rest[i + 1]
        del rest[i:i + 2]
    c = conf()
    m = with_n(target(one_arg(rest, "number"), c), c)
    api = hilink.Api(m["n"], src=m["addr"], timeout=10)
    try:
        r = simnum.find(api.get, api.post_raw, ussd=ussd, keep_mode=keep, log=lambda t: say("  " + t))
    except simnum.SimFail as e:
        raise Fail("N=%d: %s" % (m["n"], e))
    except hilink.HiFail as e:
        raise Fail("N=%d: %s" % (m["n"], e))
    r.update(n=m["n"], usb_port=m["port"], t=int(time.time()))
    if r["number"]:
        nums = jload(common.NUMBERS, {}) or {}
        nums[str(m["n"])] = {k: r[k] for k in ("number", "operator", "plmn", "ussd", "via", "t", "usb_port")}
        os.makedirs(os.path.dirname(common.NUMBERS), exist_ok=True)
        jsave(common.NUMBERS, nums, mode=0o644)
        say("  ✓ N=%d: %s (%s, %s)" % (m["n"], r["number"], r["operator"], "из SMS" if r["via"] == "sms" else r["ussd"]))
    else:
        say("  ответ %s: %s\n  номера в ответе нет — попробуй другой код (--ussd)" % (r["ussd"], r["text"] or "—"))
    return 0, r


def cmd_run(args):
    """Служебное: один проход драйвера (зовут hivelink.service, udev и таймер)."""
    need_root()
    try:
        lk = lock(common.LOCK, wait=0)
    except Busy:
        return 0, None              # идёт другой проход или команда — этот не нужен
    with lk:
        driver.run_pass()
    return 0, None


def cmd_eth0(args):
    """Служебное (hivelink-eth0.service при загрузке): eth0 — основной сетевой карте, а не модему."""
    need_root()
    return 0, install.fix_eth0()


CMDS = {"install": cmd_install, "uninstall": cmd_uninstall, "status": cmd_status, "doctor": cmd_doctor,
        "reconnect": cmd_reconnect, "reset": cmd_reset, "recfg": cmd_recfg, "dataon": cmd_dataon,
        "run": cmd_run, "eth0": cmd_eth0, "off": cmd_off, "on": cmd_on, "number": cmd_number}


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    as_json = "--json" in argv
    argv = [a for a in argv if a != "--json"]
    if not argv or argv[0] in ("help", "-h", "--help"):
        if as_json:
            return run_cli(lambda a: (0, {"usage": USAGE}), [], True)
        print(USAGE)
        return 0
    cmd, rest = argv[0], argv[1:]
    if cmd in ("install", "uninstall") and not as_json:
        from ..core import bg
        rc = bg.handoff("hivelink-" + cmd, argv, title="hivelink " + " ".join(argv))
        if rc is not None:
            return rc
    fn = CMDS.get(cmd)
    if fn is None:
        def fn(a):
            raise Fail("нет команды «%s» — hivelink help" % cmd)
    elif cmd in ("install", "uninstall", "doctor", "run", "eth0") and rest:
        def fn(a, cmd=cmd):
            raise Fail("лишние аргументы: %s — hivelink help" % " ".join(a))
    return run_cli(guarded(fn), rest, as_json)


def guarded(fn):
    """Без трасс Python: неожиданное — тоже одна строка «✗ …»."""
    def wrap(a):
        try:
            return fn(a)
        except (Fail, KeyboardInterrupt):
            raise
        except OSError as e:
            raise Fail("%s%s — hivelink doctor" % (e.strerror or e, ": %s" % e.filename if e.filename else ""))
        except Exception as e:
            raise Fail("сбой hivelink (%s: %s) — hivelink doctor" % (e.__class__.__name__, e))
    return wrap
