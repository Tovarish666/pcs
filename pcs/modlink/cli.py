"""modlink — из сетевого интерфейса прокси (PCS).

  modlink list [--show-pass]              таблица прокси
  modlink add --lan-ip IP [--name …] [--login …] [--password …|gen|-] [--port N|auto]
              [--modem-ip IP] [--reconnect-port N|auto] [--interval MIN] [--disabled]
  modlink set ID поле=значение …          поля: enabled name login password port lan_ip
                                          modem_ip reconnect_port interval_min
  modlink del ID --yes | enable ID | disable ID
  modlink apply                           конфиг sing-box → sing-box check → перезапуск
  modlink status
  modlink test ID                         внешний IP через этот прокси + HiLink
  modlink reconnect ID | reboot ID | log ID [-n N]
  modlink export [--ip IP|auto] [--save]  IP:PORT:LOGIN:PASS<TAB>http://IP:RPORT/reconnect
  modlink from-ifaces [--add]             строки по интерфейсам 192.168.N.100 сервера
  modlink migrate [--from DIR] [--dry-run] [--force] [--stop-panel]   из modlink-linux
  modlink setup | uninstall --yes [--purge] | daemon | version

  Правки таблицы вступают в силу после `modlink apply`.
  --json у любой команды: {"ok": true, "data": …} или {"ok": false, "error": "…"}.
  Пароль — `-`: прочитать из stdin (не светить в командной строке).
"""
import argparse
import re
import sys

from .. import VERSION
from ..core import util
from ..core.util import Fail, lock, need_root
from . import hilink, ops, store, system
from .store import P


def say(s=""):
    if not util.QUIET[0]:
        print(s, flush=True)


log = (lambda m: say("  " + m))


class Parser(argparse.ArgumentParser):
    def error(self, message):
        m = re.search(r"invalid choice: '?([^' ,)]+)", message)
        if m:
            message = "нет команды «%s»" % m.group(1)
        else:
            message = re.sub(r"the following arguments are required: ", "не хватает: ", message)
            message = re.sub(r"unrecognized arguments: ", "лишнее: ", message)
            message = re.sub(r"argument (\S+): expected one argument", r"\1: нужно значение", message)
            message = re.sub(r"argument (\S+): invalid int value: (\S+)", r"\1: \2 — не число", message)
        raise Fail("%s — справка: modlink help" % message)


def parser():
    ap = Parser(prog="modlink", add_help=False)
    sub = ap.add_subparsers(dest="cmd", parser_class=Parser)
    sub.required = True
    c = sub.add_parser("list")
    c.add_argument("--show-pass", action="store_true")
    c = sub.add_parser("add")
    c.add_argument("--lan-ip", required=True)
    c.add_argument("--name")
    c.add_argument("--login")
    c.add_argument("--password")
    c.add_argument("--port", default="auto")
    c.add_argument("--modem-ip")
    c.add_argument("--reconnect-port", default="auto")
    c.add_argument("--interval", default="0")
    c.add_argument("--disabled", action="store_true")
    c = sub.add_parser("set")
    c.add_argument("id")
    c.add_argument("pairs", nargs="+")
    c = sub.add_parser("del")
    c.add_argument("id")
    c.add_argument("--yes", action="store_true")
    for name in ("enable", "disable", "test", "reconnect", "reboot"):
        sub.add_parser(name).add_argument("id")
    c = sub.add_parser("log")
    c.add_argument("id")
    c.add_argument("-n", type=int, default=50)
    for name in ("apply", "status", "setup", "daemon", "version"):
        sub.add_parser(name)
    c = sub.add_parser("export")
    c.add_argument("--ip")
    c.add_argument("--save", action="store_true")
    c = sub.add_parser("from-ifaces")
    c.add_argument("--add", action="store_true")
    c = sub.add_parser("migrate")
    c.add_argument("--from", dest="src")
    c.add_argument("--dry-run", action="store_true")
    c.add_argument("--force", action="store_true")
    c.add_argument("--stop-panel", action="store_true")
    c = sub.add_parser("uninstall")
    c.add_argument("--yes", action="store_true")
    c.add_argument("--purge", action="store_true")
    return ap


def edit():
    return lock(P.lock, what="правка modlink")


def secret(v):
    """`-` — пароль из stdin: так он не виден в ps и истории."""
    if v == "-":
        v = sys.stdin.readline().rstrip("\r\n")
        if not v:
            raise Fail("пароль из stdin пустой")
    return v


def hint_apply():
    say("  → применить: modlink apply")


def row_line(r, show=False):
    iv = "%dм" % r["interval_min"] if r["interval_min"] else "—"
    return "  %-5d %-4s %-14s %-6d %-7d %-16s %-15s %-12s %-14s %s" % (
        r["id"], "да" if r["enabled"] else "нет", r["name"][:14], r["port"], r["reconnect_port"],
        r["lan_ip"], r["modem_ip"] or "—", r["login"][:12], r["password"] if show else "••••", iv)


HEAD = "  %-5s %-4s %-14s %-6s %-7s %-16s %-15s %-12s %-14s %s" % (
    "ID", "вкл", "имя", "порт", "триггер", "lan_ip", "modem_ip", "логин", "пароль", "авто")


# ── команды ────────────────────────────────────────────────────────────────
def cmd_list(a):
    doc = store.load()
    pend = system.pending(doc["proxies"])
    say(HEAD)
    for r in doc["proxies"]:
        say(row_line(r, a.show_pass))
    if not doc["proxies"]:
        say("  (пусто) — добавить: modlink add --lan-ip 192.168.N.100 или modlink from-ifaces")
    elif pend:
        say("  есть неприменённые правки → modlink apply")
    return 0, {"proxies": [store.masked(r, a.show_pass) for r in doc["proxies"]], "pending": pend}


def cmd_add(a):
    with edit():
        doc = store.load()
        r = store.add(doc, a.lan_ip, name=a.name, login=a.login, password=secret(a.password),
                      port=a.port, modem_ip=a.modem_ip, reconnect_port=a.reconnect_port,
                      interval_min=store.as_interval(a.interval), enabled=not a.disabled)
        dup = [x["id"] for x in doc["proxies"] if x["lan_ip"] == r["lan_ip"] and x["id"] != r["id"]]
        store.save(doc)
    say("  + строка %d: %s, порт %d, триггер %d, логин %s" % (r["id"], r["name"], r["port"], r["reconnect_port"], r["login"]))
    if dup:
        say("  ⚠ тот же lan_ip у строк %s — реконнект меняет IP всем им" % ", ".join(map(str, dup)))
    hint_apply()
    return 0, {"row": store.masked(r)}


def cmd_set(a):
    pairs = []
    for p in a.pairs:
        if "=" not in p:
            raise Fail("«%s»: нужно поле=значение" % p)
        k, v = p.split("=", 1)
        pairs.append((k.strip(), secret(v) if k.strip() == "password" else v))
    with edit():
        doc = store.load()
        rid = store.find(doc, a.id)["id"]
        mine = {p for x in system.applied_rows() if x["id"] == rid for p in (x["port"], x["reconnect_port"])}
        r = store.set_fields(doc, rid, pairs, mine=mine)
        store.save(doc)
    say("  строка %d: %s" % (r["id"], ", ".join(k for k, _ in pairs)))
    hint_apply()
    return 0, {"row": store.masked(r)}


def cmd_del(a):
    with edit():
        doc = store.load()
        r = store.find(doc, a.id)
        if not a.yes:
            if util.QUIET[0] or not sys.stdin.isatty():
                raise Fail("удаление строки %d — подтверди: modlink del %d --yes" % (r["id"], r["id"]))
            ans = input("  удалить строку %d (%s, порт %d)? [y/N] " % (r["id"], r["name"], r["port"]))
            if ans.strip().lower() not in ("y", "yes", "д", "да"):
                raise Fail("отменено")
        store.delete(doc, r["id"])
        store.save(doc)
    say("  − строка %d (%s)" % (r["id"], r["name"]))
    hint_apply()
    return 0, {"deleted": r["id"]}


def cmd_enable(a, on=True):
    with edit():
        doc = store.load()
        r = store.set_fields(doc, a.id, [("enabled", on)])
        store.save(doc)
    say("  строка %d: %s" % (r["id"], "включена" if on else "выключена"))
    hint_apply()
    return 0, {"row": store.masked(r)}


def cmd_apply(a):
    with edit():
        return 0, system.apply(log)


def cmd_status(a):
    s = system.status()
    say("  sing-box (modlink-sb): %s    демон (modlink): %s%s" % (
        s["sb"], s["daemon"], "" if s["unit"] != "legacy" else "  — юнит от старого modlink-linux: modlink migrate"))
    if "panel" in s:
        say("  старая панель modlink-panel: %s" % s["panel"])
    mark = {True: "✓", False: "✗", None: "·"}
    for e in s["rows"]:
        last = e["last"]
        lt = ("%s %s %s" % (last["t"][5:16], "ok" if last["ok"] else "✗", last["text"][:40])) if last else "—"
        say("  %-5d %-9s %-14s :%-5d %s  %-15s %-8s :%-5d %s  %s" % (
            e["id"], e["state"], e["name"][:14], e["port"], mark[e["port_up"]], e["lan_ip"],
            e["iface"] or "нет", e["reconnect_port"], mark[e["trigger_up"]], lt))
    if not s["rows"]:
        say("  строк нет")
    if s["pending"]:
        say("  есть неприменённые правки → modlink apply")
    return 0, s


def cmd_test(a):
    r = store.find(store.load(), a.id)
    res = ops.test(r)
    res["iface"] = system.local_addrs().get(r["lan_ip"])
    px, hl = res["proxy"], res["hilink"]
    say("  строка %d (%s)%s" % (r["id"], r["name"], "" if r["enabled"] else " — выключена"))
    say("  %s прокси :%d → %s" % ("✓" if px["ok"] else "✗", r["port"],
                                  "внешний IP %s" % px["ip"] if px["ok"] else px["error"]))
    if hl["ok"] or "status" in hl:
        extra = ", ".join(x for x in (hl.get("net"), "сигнал %s/5" % hl["signal"] if hl.get("signal") else "") if x)
        say("  %s HiLink %s: %s%s" % ("✓" if hl["ok"] else "⚠", r["modem_ip"], hl["status"], ", " + extra if extra else ""))
    else:
        say("  ⚠ HiLink %s: %s" % (r["modem_ip"] or "—", hl["error"]))
    say("  %s интерфейс: %s" % ("✓" if res["iface"] else "⚠", "%s (%s)" % (res["iface"], r["lan_ip"])
                                if res["iface"] else "адреса %s на машине нет" % r["lan_ip"]))
    if not px["ok"]:
        raise Fail("строка %d: прокси не работает — %s" % (r["id"], px["error"]))
    return 0, res


def cmd_reconnect(a):
    r = store.find(store.load(), a.id)
    say("  строка %d: реконнект через HiLink %s…" % (r["id"], r["modem_ip"] or "—"))
    res = ops.reconnect(r, "cli")
    say("  ✓ %s (%.1f с)" % ("новый IP %s%s" % (res["ip"], " — тот же, что был" if res.get("same") else "")
                             if res.get("ip") else "модем подключён, внешний IP не узнал", res["dt"]))
    return 0, res


def cmd_reboot(a):
    r = store.find(store.load(), a.id)
    res = ops.reboot(r, "cli")
    say("  ✓ строка %d: модем %s перезагружается (~1 мин)" % (r["id"], r["modem_ip"]))
    return 0, res


def cmd_log(a):
    rid = store.find(store.load(), a.id)["id"]
    lines = ops.tail(rid, a.n)
    for ln in lines:
        say("  " + ln)
    if not lines:
        say("  журнал строки %d пуст" % rid)
    return 0, {"id": rid, "lines": lines}


def cmd_export(a):
    rows = store.load()["proxies"]
    ip = a.ip if a.ip and a.ip != "auto" else (None if a.ip == "auto" else system.ext_ip_saved())
    if not ip:
        ip = hilink.ext_ip()
    ip = store.as_ipv4(ip, "IP сервера")
    if a.save:
        with edit():
            system.save_ext_ip(ip)
    lines = store.export_lines(rows, ip)
    if not util.QUIET[0]:
        for ln in lines:
            print(ln)
        if not lines:
            print("  включённых строк нет", file=sys.stderr)
    return 0, {"ip": ip, "lines": lines}


def cmd_from_ifaces(a):
    from ..core import net
    try:
        up = net.uplink_route()
    except Fail:
        up = None
    text = util.sh("ip", "-4", "-o", "addr", check=False).stdout
    doc = store.load()
    sug = store.suggest(store.parse_addrs(text), doc["proxies"], skip_devs=(up[1],) if up else ())
    added = []
    if a.add and sug:
        with edit():
            doc = store.load()
            for s in store.suggest(store.parse_addrs(text), doc["proxies"], skip_devs=(up[1],) if up else ()):
                added.append(store.add(doc, s["lan_ip"], name=s["name"], modem_ip=s["modem_ip"]))
            store.save(doc)
    for s in sug:
        say("  %s %-10s lan %-16s (%s)  HiLink %s" % ("+" if a.add else "·", s["name"], s["lan_ip"], s["dev"], s["modem_ip"]))
    if not sug:
        say("  новых интерфейсов 192.168.N.100 нет")
    elif added:
        say("  добавлено строк: %d" % len(added))
        hint_apply()
    else:
        say("  добавить все: modlink from-ifaces --add")
    return 0, {"suggest": sug, "added": [store.masked(r) for r in added]}


def cmd_migrate(a):
    with edit():
        res = system.migrate(log, src=a.src, stop_panel=a.stop_panel, force=a.force, dry_run=a.dry_run)
    if a.dry_run:
        say(HEAD)
        for r in res["rows"]:
            say(row_line(dict(r, password="")))
        for w in res["warnings"]:
            say("  ⚠ " + w)
        say("  это проверка; перенести: modlink migrate%s" % (" --from " + a.src if a.src else ""))
    else:
        say("  перенос готов: строк %d, sing-box %s, демон %s; клиентам — modlink export"
            % (len(res["rows"]), res["sb"], res["daemon"]))
    return 0, res


def cmd_setup(a):
    with edit():
        res = system.setup(log)
    say("  sing-box: %s, демон: %s" % (res["sb"], res["daemon"]))
    return 0, res


def cmd_uninstall(a):
    if not a.yes:
        raise Fail("снять modlink — подтверди: modlink uninstall --yes%s" % (" --purge" if a.purge else ""))
    with edit():
        return 0, system.uninstall(log, purge=a.purge)


def cmd_daemon(a):
    from .daemon import Daemon
    return Daemon().run(), None


CMDS = {"list": cmd_list, "add": cmd_add, "set": cmd_set, "del": cmd_del, "enable": cmd_enable,
        "disable": lambda a: cmd_enable(a, False), "apply": cmd_apply, "status": cmd_status,
        "test": cmd_test, "reconnect": cmd_reconnect, "reboot": cmd_reboot, "log": cmd_log,
        "export": cmd_export, "from-ifaces": cmd_from_ifaces, "migrate": cmd_migrate,
        "setup": cmd_setup, "uninstall": cmd_uninstall, "daemon": cmd_daemon}


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    as_json = "--json" in argv
    argv = [x for x in argv if x != "--json"]
    if not argv or argv[0] in ("help", "-h", "--help"):
        print(__doc__.strip())
        return 0

    def fn(_):
        a = parser().parse_args(argv)
        if a.cmd == "version":
            say(VERSION)
            return 0, {"version": VERSION}
        if not P.root:
            need_root()
        return CMDS[a.cmd](a)
    return util.run_cli(fn, None, as_json)
