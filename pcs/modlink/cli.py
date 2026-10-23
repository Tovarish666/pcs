"""modlink — из сетевого интерфейса прокси (PCS). Движок — 3proxy: HTTP и SOCKS5 (с UDP) на одном порту.

  modlink on | off                        включить / выключить modlink целиком. По умолчанию выключен:
                                          3proxy и демон не запущены, портов modlink в ss -tulpn нет.
                                          on — запуск по применённой таблице; off — остановить всё
  modlink list [--show-pass]              таблица прокси
  modlink add --lan-ip IP [--name …] [--login …] [--password …|gen|-] [--port N|auto]
              [--modem-ip IP] [--reboot-port N|auto|0] [--reconnect-port N|auto|0]
              [--interval MIN] [--disabled] [--protect]   (по умолчанию без защиты)
  modlink set ID поле=значение …          поля: enabled name login password port lan_ip
                                          modem_ip reboot_port reconnect_port interval_min protect
  modlink del ID --yes | enable ID | disable ID
  modlink settings [ключ=значение …]      общие настройки:
      base_port=N        от него порты (новые строки и смена режима портов)
      mode=triples|shared  режим портов — порты ВСЕХ строк заново: triples — у строки свои
                         прокси N+3i, ребут N+3i+1, реконнект N+3i+2; shared — у всех один порт
                         прокси N (модем — по логину) и один порт управления N+1
      traffic=tcp+udp|tcp  пропускать UDP (SOCKS5 UDP ASSOCIATE) или только TCP
      lan_ip=IP|auto     адрес машины в LAN — строки для proxyveth ВМ этого хоста
      wan_ip=IP|auto     внешний адрес для клиентов: строки, ссылки, UDP
      access_log=on|off  журнал каждого соединения 3proxy (/var/log/modlink/3proxy.log)
      wan_check=on|off   раз в час внешний IP через каждый прокси (по умолчанию вкл)
      speedtest=on|off   раз в день в 12:00 скорость каждого модема (трафик! по умолчанию выкл)
  modlink protect [ID|all on|off]         защита строки: ЛК операторов, опасные порты, веб модема
                                          (без аргументов — что закрыто и у каких строк)
  modlink apply                           конфиг 3proxy → перечитать (соединения живут) → проверка портов
  modlink status
  modlink test ID                         внешний IP через этот прокси + HiLink
  modlink reconnect ID | reboot ID | speedtest ID | log ID [-n N]
  modlink logs [3proxy|modlink] [-n N]    журнал 3proxy (и соединений, если access_log) или демона
  modlink export [--what proxy|reboot|reconnect|all] [--ip IP|auto] [--save]
                                          строки для клиента: прокси IP:PORT:LOGIN:PASS, ссылки
                                          ребута и реконнекта; all — всё по колонкам (табуляция)
  modlink export --table [--ip IP]        таблица proxyveth: виртуальные модемы из этих настоящих
                                          (IP по умолчанию — свой основной адрес: ВМ этого хоста)
  modlink from-ifaces [--add]             строки по интерфейсам 192.168.N.100 сервера
  modlink import [ФАЙЛ|ССЫЛКА|-] [--replace] [--map number|order] [--dry-run] [--on]
                                          конфиг по таблице: строки «Ip:port:log:pass  ReBoot
                                          ReConnect» (как «Копировать»; колонки — любые, ReBoot
                                          может не быть) → таблица modlink, где каждая работает
                                          ровно так; модем — по логину modemN (--map order — по
                                          порядку); --replace — убрать строки, которых нет в
                                          таблице; --dry-run — только план; --on — и включить
  modlink migrate [--from DIR] [--dry-run] [--force] [--stop-panel]   из modlink-linux
  modlink setup | uninstall --yes [--purge] | daemon | version

  Правки таблицы вступают в силу после `modlink apply` (и только у включённого modlink).
  --json у любой команды: {"ok": true, "data": …} или {"ok": false, "error": "…"}.
  Пароль — `-`: прочитать из stdin (не светить в командной строке).
"""
import argparse
import re
import sys

from .. import VERSION
from ..core import bg, util
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
    c.add_argument("--reboot-port", default="auto")
    c.add_argument("--interval", default="0")
    c.add_argument("--disabled", action="store_true")
    g = c.add_mutually_exclusive_group()
    g.add_argument("--protect", action="store_true")
    g.add_argument("--no-protect", action="store_true")     # по умолчанию и так; для старых вызовов
    c = sub.add_parser("set")
    c.add_argument("id")
    c.add_argument("pairs", nargs="+")
    c = sub.add_parser("del")
    c.add_argument("id")
    c.add_argument("--yes", action="store_true")
    c = sub.add_parser("settings")
    c.add_argument("pairs", nargs="*")
    c = sub.add_parser("protect")
    c.add_argument("id", nargs="?")
    c.add_argument("state", nargs="?", choices=("on", "off"))
    for name in ("enable", "disable", "test", "reconnect", "reboot", "speedtest"):
        sub.add_parser(name).add_argument("id")
    c = sub.add_parser("log")
    c.add_argument("id")
    c.add_argument("-n", type=int, default=50)
    c = sub.add_parser("logs")
    c.add_argument("which", nargs="?", choices=("3proxy", "modlink"), default="3proxy")
    c.add_argument("-n", type=int, default=200)
    for name in ("apply", "status", "setup", "daemon", "version", "on", "off"):
        sub.add_parser(name)
    c = sub.add_parser("export")
    c.add_argument("--ip")
    c.add_argument("--what", choices=tuple(store.COPY), default="all")
    c.add_argument("--save", action="store_true")
    c.add_argument("--table", action="store_true")
    c = sub.add_parser("from-ifaces")
    c.add_argument("--add", action="store_true")
    c = sub.add_parser("import")
    c.add_argument("src", nargs="?", default="-")
    c.add_argument("--replace", action="store_true")
    c.add_argument("--map", choices=store.MAP, default="number")
    c.add_argument("--dry-run", action="store_true")
    c.add_argument("--on", action="store_true")
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
    return "  %-5d %-4s %-14s %-6d %-6s %-7s %-16s %-15s %-12s %-14s %-5s %s" % (
        r["id"], "да" if r["enabled"] else "нет", r["name"][:14], r["port"], r.get("reboot_port") or "—",
        r["reconnect_port"] or "—", r["lan_ip"], r["modem_ip"] or "—", r["login"][:12],
        r["password"] if show else "••••", iv, "да" if r.get("protect") else "нет")


HEAD = "  %-5s %-4s %-14s %-6s %-6s %-7s %-16s %-15s %-12s %-14s %-5s %s" % (
    "ID", "вкл", "имя", "порт", "ребут", "реконн", "lan_ip", "modem_ip", "логин", "пароль", "авто", "защита")


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
                      port=a.port, modem_ip=a.modem_ip, reconnect_port=a.reconnect_port, reboot_port=a.reboot_port,
                      interval_min=store.as_interval(a.interval), enabled=not a.disabled,
                      protect=a.protect)
        dup = [x["id"] for x in doc["proxies"] if x["lan_ip"] == r["lan_ip"] and x["id"] != r["id"]]
        store.save(doc)
    say("  + строка %d: %s, порт %d, ребут %s, реконнект %s, логин %s" % (
        r["id"], r["name"], r["port"], r["reboot_port"] or "—", r["reconnect_port"] or "—", r["login"]))
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
        mine = {p for x in system.applied_rows() if x["id"] == rid for p in system.ports_of(x)}
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


def cmd_settings(a):
    kv = {}
    for p in a.pairs:
        if "=" not in p:
            raise Fail("«%s»: нужно ключ=значение (%s)" % (p, ", ".join(store.SETTINGS)))
        k, v = p.split("=", 1)
        kv[k.strip().replace("-", "_")] = v.strip()
    renumbered = False
    if kv:
        with edit():
            old = store.settings()
            if "base_port" in kv or ("mode" in kv and kv["mode"] != old["mode"]):
                base = store.as_port(kv.get("base_port", old["base_port"]), "base_port")
                mode = kv.get("mode", old["mode"])
                doc = store.load()
                mine = {p for r in system.applied_rows() for p in system.ports_of(r)}
                store.assign_ports(doc, base, mode, mine=mine)
                store.save_settings(**kv)        # сначала проверка портов, потом запись
                store.save(doc)
                renumbered = bool(doc["proxies"])
            else:
                store.save_settings(**kv)
    s = store.settings()
    lan_now = s["lan_ip"] or (primary_ip_or_none() or "")
    onoff = (lambda v: "вкл" if v else "выкл")
    say("  базовый порт: %d    режим портов: %s    трафик: %s" % (
        s["base_port"], "свои тройки" if s["mode"] == "triples" else "один общий (по логину)",
        "TCP и UDP" if s["traffic"] == "tcp+udp" else "только TCP"))
    say("  LAN IP: %s    WAN IP: %s" % (s["lan_ip"] or "авто (%s)" % (lan_now or "?"), s["wan_ip"] or "авто (при экспорте)"))
    say("  журнал соединений: %s    внешний IP раз в час: %s    скорость в 12:00: %s" % (
        onoff(s["access_log"]), onoff(s["wan_check"]), onoff(s["speedtest"])))
    if renumbered:
        b = s["base_port"]
        say("  порты всех строк — заново от %d: %s" % (b, "прокси %d, ребут %d, реконнект %d, следующая строка %d, …"
                                                         % (b, b + 1, b + 2, b + 3) if s["mode"] == "triples"
                                                         else "прокси у всех %d, ребут и реконнект — %d" % (b, b + 1)))
        hint_apply()
    elif kv and set(kv) & {"traffic", "access_log", "wan_ip"}:
        hint_apply()
    return 0, dict(s, lan_ip_now=lan_now, renumbered=renumbered)


def primary_ip_or_none():
    try:
        return primary_ip()
    except Fail:
        return None


def cmd_protect(a):
    """Без аргументов — что закрывает защита и у каких строк она снята; ID|all on|off — переключить."""
    if a.id is not None and a.state is None:
        raise Fail("modlink protect %s on|off" % a.id)
    if a.id is None:
        doc = store.load()
        g = store.guard_lists(store.guard_extra())
        off = [r["id"] for r in doc["proxies"] if not r["protect"]]
        say("  защита строки (по умолчанию выключена, включается modlink protect ID on) закрывает:")
        say("    сайты и ЛК операторов: %s" % ", ".join(g["domains"]))
        say("    порты: %s, %s" % (", ".join(map(str, g["ports"])), ", ".join(x.replace(":", "–") for x in g["port_ranges"])))
        if g["cidrs"]:
            say("    сети: %s" % ", ".join(g["cidrs"]))
        say("    веб своего модема (modem_ip)")
        say("  у всех строк закрыты: %s" % ", ".join(g["always"]))
        say("  свои дополнения: %s {\"domains\": [], \"cidrs\": [], \"ports\": []}" % P.guard)
        say("  без защиты: %s" % (", ".join(map(str, off)) if off else "нет таких строк"))
        return 0, dict(g, off=off)
    on = a.state == "on"
    with edit():
        doc = store.load()
        ids = [r["id"] for r in doc["proxies"]] if a.id == "all" else [store.find(doc, a.id)["id"]]
        changed = [rid for rid in ids if store.find(doc, rid)["protect"] != on]
        for rid in changed:
            store.set_fields(doc, rid, [("protect", on)])
        store.save(doc)
    say("  защита %s: %s" % ("включена" if on else "СНЯТА", ", ".join(map(str, changed)) if changed else "уже так"))
    if not on and changed:
        say("  ⚠ без защиты клиенту открыты ЛК оператора и веб модема — только для своих (ген1)")
    if changed:
        hint_apply()
    return 0, {"protect": on, "changed": changed}


def cmd_apply(a):
    with edit():
        return 0, system.apply(log)


def cmd_on(a):
    with edit():
        return 0, system.turn_on(log)


def cmd_off(a):
    with edit():
        return 0, system.turn_off(log)


def cmd_status(a):
    s = system.status()
    say("  modlink: %s" % ("включён" if s["on"] else "ВЫКЛЮЧЕН — 3proxy и демон не запущены (включить: modlink on)"))
    say("  %s (modlink-3proxy): %s    демон (modlink): %s%s" % (
        s["engine"], s["proxy"], s["daemon"], "" if s["unit"] != "legacy" else "  — юнит от старого modlink-linux: modlink migrate"))
    if "panel" in s:
        say("  старая панель modlink-panel: %s" % s["panel"])
    mark = {True: "✓", False: "✗", None: "·"}
    for e in s["rows"]:
        last = e["last"]
        lt = ("%s %s %s" % (last["t"][5:16], "ok" if last["ok"] else "✗", last["text"][:40])) if last else "—"
        say("  %-5d %-9s %-14s :%-5d %s  %-15s %-8s упр %s  внешний %-15s %s" % (
            e["id"], e["state"], e["name"][:14], e["port"], mark[e["port_up"]], e["lan_ip"],
            e["iface"] or "нет", mark[e["trigger_up"]], e.get("wan_ip") or "—", lt))
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


def cmd_speedtest(a):
    r = store.find(store.load(), a.id)
    say("  строка %d: тест скорости с %s (yaspeed, ~30 с)…" % (r["id"], r["lan_ip"]))
    res = ops.speedtest(r, "cli")
    say("  ✓ ↓ %.1f Мбит/с · ↑ %.1f Мбит/с · %.0f мс%s" % (res["down"], res["up"], res["ping"],
                                                          "  (IP %s)" % res["ip"] if res.get("ip") else ""))
    return 0, res


def cmd_log(a):
    rid = store.find(store.load(), a.id)["id"]
    lines = ops.tail(rid, a.n)
    for ln in lines:
        say("  " + ln)
    if not lines:
        say("  журнал строки %d пуст" % rid)
    return 0, {"id": rid, "lines": lines}


def cmd_logs(a):
    """Журнал 3proxy (journald юнита + журнал соединений, если включён) или демона modlink."""
    n = max(1, min(a.n, 2000))
    unit = system.PROXY if a.which == "3proxy" else system.SVC
    lines = util.sh("journalctl", "-u", unit, "-n", str(n), "--no-pager", "-o", "short-iso",
                    check=False).stdout.splitlines()
    if a.which == "3proxy" and store.settings()["access_log"]:
        try:
            with open(P.log + "/3proxy.log", encoding="utf-8", errors="replace") as f:
                lines += ["— соединения —"] + f.read().splitlines()[-n:]
        except OSError:
            pass
    for ln in lines:
        say("  " + ln)
    return 0, {"which": a.which, "lines": lines[-n * 2:]}


def primary_ip():
    m = re.search(r"src (\d+\.\d+\.\d+\.\d+)", util.sh("ip", "-4", "route", "get", "1.1.1.1", check=False).stdout)
    if not m:
        raise Fail("свой основной адрес не узнал — укажи: modlink export --table --ip IP")
    return m.group(1)


def cmd_export_table(a):
    rows = store.load()["proxies"]
    ip = store.as_ipv4(a.ip if a.ip and a.ip != "auto" else (store.settings()["lan_ip"] or primary_ip()), "IP")
    lines, warn = store.table_lines(rows, ip)
    if not util.QUIET[0]:
        for ln in lines:
            print(ln)
        for w in warn:
            print("  ⚠ " + w, file=sys.stderr)
        if len(lines) < 2:
            print("  строк для proxyveth нет (нужны включённые с lan_ip 192.168.N.x)", file=sys.stderr)
    return 0, {"ip": ip, "lines": lines, "warnings": warn}


def cmd_export(a):
    if a.table:
        return cmd_export_table(a)
    rows = store.load()["proxies"]
    ip = a.ip if a.ip and a.ip != "auto" else (None if a.ip == "auto" else system.ext_ip_saved())
    if not ip:
        ip = hilink.ext_ip()
    ip = store.as_ipv4(ip, "IP сервера")
    if a.save:
        with edit():
            system.save_ext_ip(ip)
    exp = store.export(rows, ip, a.what)
    lines = ["\t".join(r) for r in exp["rows"]]
    if not util.QUIET[0]:
        for ln in lines:
            print(ln)
        if not lines:
            print("  включённых строк нет", file=sys.stderr)
    return 0, {"ip": ip, "what": a.what, "lines": lines, "columns": exp["columns"], "rows": exp["rows"]}


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


def read_source(src):
    """- — stdin; http(s) — ссылка (Google-таблица — её лист в CSV); иначе файл."""
    if src == "-":
        return sys.stdin.read()
    if re.match(r"https?://", src):
        import urllib.request
        from ..core.table import normalize_url
        try:
            req = urllib.request.Request(normalize_url(src), headers={"User-Agent": "pcs"})
            text = urllib.request.urlopen(req, timeout=30).read().decode("utf-8-sig")
        except Exception as e:
            raise Fail("таблица по ссылке не открылась: %s" % e)
        if text.lstrip().startswith("<"):
            raise Fail("вместо таблицы пришла страница — у листа нет доступа по ссылке?")
        return text
    try:
        with open(src, encoding="utf-8-sig") as f:
            return f.read()
    except OSError as e:
        raise Fail("%s: %s" % (src, e.strerror))


IMPORT_ACT = {"add": "+", "change": "~", "same": "="}


def cmd_import(a):
    specs, warn = store.parse_sheet(read_source(a.src))
    addrs = store.parse_addrs(util.sh("ip", "-4", "-o", "addr", check=False).stdout)
    from ..core import net
    try:
        up = net.uplink_route()
    except Fail:
        up = None
    with edit():
        doc = store.load()
        pl = store.import_plan(doc, specs, addrs, replace=a.replace, how=a.map, skip_devs=(up[1],) if up else ())
        mine = {p for r in system.applied_rows() for p in system.ports_of(r)}
        busy = sorted({p for x in specs for p in (x["port"], x["reboot_port"], x["reconnect_port"])
                       if p and p not in mine and not store.PROBE[0](p)})
        if busy and not a.dry_run:
            raise Fail("порт %s слушает другая программа (ss -ltnp) — сначала освободи его: ничего не менял"
                       % ", ".join(map(str, busy[:10])))
        if busy:
            warn.append("порт %s слушает другая программа (ss -ltnp) — там прокси не заработает"
                        % ", ".join(map(str, busy[:10])))
        warn += pl["warnings"]
        cur = store.settings()
        sets = {k: v for k, v in pl["settings"].items() if cur.get(k) != v}
        res = {"rows": pl["rows"], "removed": pl["removed"], "settings": sets, "warnings": warn,
               "on": store.is_on(), "dry_run": a.dry_run, "applied": False}
        for x in pl["rows"]:
            say("  %s строка %-5s %-12s порт %-5d ребут %-5s реконнект %-5s модем %-15s %s" % (
                IMPORT_ACT[x["action"]], x["id"], x["login"][:12], x["port"], x["reboot_port"] or "—",
                x["reconnect_port"] or "—", x["lan_ip"], x["dev"] or "(нет на машине)"))
        for r in pl["removed"]:
            say("  − строка %-5d %-12s порт %d (нет в таблице)" % (r["id"], r["login"][:12], r["port"]))
        for k, v in sets.items():
            say("  настройка %s: %s → %s" % (k, cur.get(k) or "авто", v))
        for w in warn:
            say("  ⚠ " + w)
        n = {k: len([x for x in pl["rows"] if x["action"] == k]) for k in IMPORT_ACT}
        say("  итого: добавить %d, поправить %d, как есть %d, убрать %d" % (n["add"], n["change"], n["same"], len(pl["removed"])))
        if a.dry_run:
            say("  это план; применить: modlink import … без --dry-run%s" % ("" if res["on"] or a.on else " (и --on, чтобы включить)"))
            return 0, res
        store.save(pl["doc"])
        if sets:
            store.save_settings(**sets)
        res["apply"] = system.apply(log)
        res["applied"] = True
        if a.on and not store.is_on():
            res["turned_on"] = system.turn_on(log)
            res["on"] = True
    if not res["on"]:
        say("  modlink выключен — строки записаны и применятся при включении: modlink on")
    return 0, res


def cmd_migrate(a):
    with edit():
        res = system.migrate(log, src=a.src, stop_panel=a.stop_panel, force=a.force, dry_run=a.dry_run)
    if a.dry_run:
        say(HEAD)
        for r in res["rows"]:
            say(row_line(dict(r, password="")))
        for w in res["warnings"]:
            say("  ⚠ " + w)
        say("  это проверка; перенести: modlink migrate%s%s --stop-panel" % (
            " --from " + a.src if a.src else "", " --force" if res.get("replaces") else ""))
    else:
        say("  перенос готов: строк %d, 3proxy %s, демон %s; клиентам — modlink export"
            % (len(res["rows"]), res["proxy"], res["daemon"]))
    return 0, res


def cmd_setup(a):
    with edit():
        res = system.setup(log)
    say("  modlink %s; 3proxy: %s, демон: %s" % ("включён" if res["on"] else "выключен", res["proxy"], res["daemon"]))
    return 0, res


def cmd_uninstall(a):
    if not a.yes:
        raise Fail("снять modlink — подтверди: modlink uninstall --yes%s" % (" --purge" if a.purge else ""))
    with edit():
        return 0, system.uninstall(log, purge=a.purge)


def cmd_daemon(a):
    from .daemon import Daemon
    if not store.is_on():           # выключен — демону делать нечего (юнит его и не запускает)
        say("  modlink выключен — демон не нужен (включить: modlink on)")
        return 0, None
    return Daemon().run(), None


CMDS = {"list": cmd_list, "add": cmd_add, "set": cmd_set, "del": cmd_del, "enable": cmd_enable, "protect": cmd_protect,
        "settings": cmd_settings,
        "disable": lambda a: cmd_enable(a, False), "apply": cmd_apply, "status": cmd_status,
        "test": cmd_test, "reconnect": cmd_reconnect, "reboot": cmd_reboot, "log": cmd_log, "speedtest": cmd_speedtest, "logs": cmd_logs,
        "export": cmd_export, "from-ifaces": cmd_from_ifaces, "import": cmd_import, "migrate": cmd_migrate,
        "setup": cmd_setup, "uninstall": cmd_uninstall, "daemon": cmd_daemon, "on": cmd_on, "off": cmd_off}


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
        if a.cmd in ("setup", "migrate", "uninstall") and not getattr(a, "dry_run", False) \
                and (a.cmd != "uninstall" or a.yes):
            rc = bg.handoff("modlink-" + a.cmd, argv, title="modlink " + " ".join(argv))
            if rc is not None:
                return rc, None
        return CMDS[a.cmd](a)
    return util.run_cli(fn, None, as_json)
