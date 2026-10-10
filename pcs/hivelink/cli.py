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
  hivelink status [--json]    модемы: драйвер, USB, связь, данные, сеть, внешний IP
  hivelink doctor [--json]    окружение, установка, фикс приёма
  hivelink reconnect N        переподключить модем N (HiLink)
  hivelink reset N            заново подключить USB-устройство модема N
  hivelink recfg N            забыть выученную USB-конфигурацию модема и подобрать заново
  hivelink dataon N|--all     включить мобильные данные

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
    c = conf()
    data = status.collect(c)
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
        st["dial"].pop(str(m["n"]), None)
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


CMDS = {"install": cmd_install, "uninstall": cmd_uninstall, "status": cmd_status, "doctor": cmd_doctor,
        "reconnect": cmd_reconnect, "reset": cmd_reset, "recfg": cmd_recfg, "dataon": cmd_dataon,
        "run": cmd_run}


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
    fn = CMDS.get(cmd)
    if fn is None:
        def fn(a):
            raise Fail("нет команды «%s» — hivelink help" % cmd)
    elif cmd in ("install", "uninstall", "status", "doctor", "run") and rest:
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
