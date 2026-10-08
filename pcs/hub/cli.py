"""pcs — хаб PCS на хосте Proxmox: серверы, меню, доступы, панель (docs/ARCHITECTURE.md, §6).

Здесь только разбор командной строки и печать человеку; дело делают
servers, ops, remote, web, jobs. Панель зовёт те же функции через api.
"""
import argparse
import json
import os
import shlex
import subprocess
import sys
import traceback

from .. import VERSION
from ..core import dns as dnsfix
from ..core import util
from ..core.util import Fail, sh
from . import jobs, ops, pve, remote, servers, store, ui, web

HELP = """pcs %s — хаб фермы мобильных прокси на хосте Proxmox

  pcs                                   меню
  pcs status [--json]                   хост и все серверы сразу
  pcs server list | info [ID] [--password] | use ID|host
  pcs server create [--mode usb|gw] [--sheet URL] [--mpspace] [--id N] [--name …]
                    [--ip A.B.C.D/M --gw …|--dhcp] [--cores N] [--ram ГБ] [--disk ГБ]
  pcs server adopt ID [--ip IP] | delete ID [--yes]
  pcs proxyveth …  [--server ID]        команда proxyveth на сервере (аргументы — как есть)
  pcs modlink …    [--server ID]
  pcs hivelink …   [--server ID|--host]
  pcs dns     [--server ID|--host] [--dns "1.1.1.1 8.8.8.8"]
  pcs passwd  [--server ID|--host]      пароль root (Enter — сгенерировать)
  pcs key     [--server ID|--host] [--add PUBKEY | --rotate]
  pcs ssh [ID] | pcs exec [ID] 'cmd'    консоль / команда на сервере
  pcs ssh-port PORT [--server ID]       порт SSH сервера
  pcs mpspace install|auth|check [--server ID]
  pcs update [--host | --server ID | --all]   без флагов — хост из GitHub и все серверы
  pcs web on|off|status|passwd          веб-панель :666
  pcs doctor [--server ID|--host] | pcs job [ID] | pcs log | pcs version

  Без --server — выбранный сервер (pcs server use ID). --json — ответ для программ.
  Секреты не в командной строке: пароль root — PCS_PASSWORD или вопрос, auth.mp —
  PCS_MP_AUTH или stdin, HTTP-прокси для mp.space — PCS_MP_PROXY.""" % VERSION

PASSTHRU = ("proxyveth", "modlink", "hivelink")


class Parser(argparse.ArgumentParser):
    def error(self, message):
        raise Fail("не понял аргументы (%s) — справка: pcs help" % message)


# ── разбор ─────────────────────────────────────────────────────────────────
def parser():
    ap = Parser(prog="pcs", description="хаб PCS на хосте Proxmox", add_help=True)
    sub = ap.add_subparsers(dest="cmd", parser_class=Parser)
    sub.add_parser("status")
    srv = sub.add_parser("server").add_subparsers(dest="scmd", parser_class=Parser)
    srv.add_parser("list")
    c = srv.add_parser("info")
    c.add_argument("id", nargs="?")
    c.add_argument("--password", action="store_true", help="показать пароль root (для ЛК mp.space)")
    srv.add_parser("use").add_argument("id")
    c = srv.add_parser("create", help="ВМ Ubuntu 24.04 под ключ")
    c.add_argument("--id", type=int, help="VM ID (по умолчанию — свободная тысяча)")
    c.add_argument("--name")
    c.add_argument("--cores", type=int)
    c.add_argument("--ram", type=int, help="ГБ (числа от 512 — мегабайты, как в PCS 4)")
    c.add_argument("--disk", type=int, help="ГБ")
    c.add_argument("--storage")
    c.add_argument("--bridge")
    c.add_argument("--ip", help="статический адрес с маской, напр. 192.168.1.60/24")
    c.add_argument("--gw")
    c.add_argument("--dhcp", action="store_true")
    c.add_argument("--dns", help='"1.1.1.1 8.8.8.8"')
    c.add_argument("--mode", choices=servers.MODES, help="режим proxyveth: usb (как USB-модемы) или gw")
    c.add_argument("--sheet", help="ссылка на Google-таблицу модемов — сразу создать модемы")
    c.add_argument("--mpspace", action="store_true", help="сразу поставить mobileproxy.space (auth.mp — PCS_MP_AUTH)")
    c.add_argument("--replace", action="store_true", help="ВМ с этим ID уже есть — удалить и создать заново")
    c.add_argument("--yes", action="store_true")
    c = srv.add_parser("adopt")
    c.add_argument("id")
    c.add_argument("--ip")
    c = srv.add_parser("delete")
    c.add_argument("id")
    c.add_argument("--yes", action="store_true")
    for name in ("dns", "passwd", "key", "doctor"):
        c = sub.add_parser(name)
        c.add_argument("--server")
        c.add_argument("--host", action="store_true")
    sub.choices["dns"].add_argument("--dns")
    g = sub.choices["key"].add_mutually_exclusive_group()
    g.add_argument("--add", metavar="PUBKEY")
    g.add_argument("--rotate", action="store_true")
    c = sub.add_parser("ssh-port")
    c.add_argument("port", type=int)
    c.add_argument("--server")
    c = sub.add_parser("mpspace")
    c.add_argument("action", choices=("install", "auth", "check"))
    c.add_argument("--server")
    c.add_argument("--no-reboot", action="store_true")
    c = sub.add_parser("update")
    g = c.add_mutually_exclusive_group()
    g.add_argument("--host", action="store_true", help="только хост из GitHub")
    g.add_argument("--server", action="append", help="код хоста — на этот сервер")
    g.add_argument("--all", action="store_true", help="код хоста — на все серверы")
    c = sub.add_parser("web")
    c.add_argument("action", choices=("on", "off", "status", "passwd", "serve"))
    c.add_argument("--user")
    c.add_argument("rest", nargs=argparse.REMAINDER)
    c = sub.add_parser("job")
    c.add_argument("id", nargs="?")
    c.add_argument("run_id", nargs="?")
    sub.add_parser("log")
    for p in list(sub.choices.values()) + list(srv.choices.values()):
        p.add_argument("--json", action="store_true")
    return ap


def split_target(args, allow_host=False):
    """--server ID / --server=ID / --host — откуда угодно в аргументах части; остальное — как есть."""
    rest, server, host = [], None, False
    i = 0
    while i < len(args):
        a = args[i]
        if a == "--server" and i + 1 < len(args):
            server = args[i + 1]
            i += 2
            continue
        if a.startswith("--server="):
            server = a.split("=", 1)[1]
        elif a == "--host" and allow_host:
            host = True
        else:
            rest.append(a)
        i += 1
    return rest, server, host


# ── сквозные команды частей ────────────────────────────────────────────────
def passthru(part, args):
    rest, server, host = split_target(args, allow_host=(part == "hivelink"))
    as_json = "--json" in rest
    try:
        t = store.target(server, host)
        if not rest:
            rest = ["help"] if not as_json else ["status", "--json"]
        if t == store.HOST:
            if part != "hivelink":
                raise Fail("на хосте нет %s — он на серверах: --server ID" % part)
            return subprocess.call([os.path.join(remote.ROOT, "bin", part)] + rest)
        if not as_json and sys.stdout.isatty():
            ui.info("%s на %s" % (part, store.label(t)))
        return remote.stream(t, " ".join(shlex.quote(x) for x in [part] + rest),
                             tty=sys.stdout.isatty() and sys.stdin.isatty() and not as_json)
    except Fail as e:
        say_fail(e, as_json)
        return 1


def say_fail(e, as_json):
    if as_json:
        print(json.dumps({"ok": False, "error": str(e)}, ensure_ascii=False))
    else:
        print("  %s✗%s %s" % (ui.C["r"], ui.C["x"], e), file=sys.stderr)
    ui.log("FAIL " + str(e))


def shell(cmd, args):
    """pcs ssh [ID] [аргументы ssh] / pcs exec [ID] 'команда'."""
    rest, server, host = split_target(args, allow_host=True)
    if rest and (rest[0].isdigit() or rest[0] in ("host", "хост")) and (cmd == "ssh" or len(rest) > 1):
        server, rest = rest[0], rest[1:]
    try:
        t = store.target(server, host)
        if t == store.HOST:
            if cmd == "ssh":
                raise Fail("это и есть хост — консоль уже открыта")
            return subprocess.call(["bash", "-lc", " ".join(rest)])
        if cmd == "ssh":
            os.execvp("ssh", remote.base(t, tty=True) + rest)
        if not rest:
            raise Fail("какую команду выполнить: pcs exec [ID] 'команда'")
        return remote.stream(t, " ".join(rest))
    except Fail as e:
        say_fail(e, False)
        return 1


# ── команды ────────────────────────────────────────────────────────────────
def c_status(a):
    host = servers.host_overview()
    rows = servers.overview_all()
    if a.json:
        return 0, {"host": host, "servers": [{k: v for k, v in d.items() if k != "info"} for d in rows]}
    C = ui.C
    hl = host.get("hivelink") or {}
    print("\n  %sХост %s%s (%s) · PCS %s · hivelink: %s" % (
        C["b"], host["name"], C["x"], host.get("ip") or "?", VERSION,
        "не готов" if hl.get("error") else ("стоит" if hl else "—")))
    print_rows(rows)
    return 0, None


STATE = {"running": ("g", "работает"), "stopped": ("d", "стоп"), "paused": ("y", "пауза"), "absent": ("r", "нет ВМ"),
         "offline": ("r", "нет SSH"), "old": ("y", "старый код")}


def print_rows(rows):
    C = ui.C
    act = store.active()
    if not rows:
        print("  серверов нет: pcs server create или pcs server adopt ID")
        return
    print("\n  %-2s %-6s %-14s %-15s %-11s %-5s %-14s %-10s %s" % ("", "ID", "имя", "адрес", "ВМ", "режим", "модемы",
                                                                 "mp.space", "PCS"))
    for d in rows:
        col, st = STATE.get(d["state"], ("y", d["state"]))
        m = d["modems"]
        mod = ("%d/%d" % (m["ok"], m["total"]) + (" ⚠%d" % m["warn"] if m["warn"] else "")
               + (" ✗%d" % m["broken"] if m["broken"] else "")) if m.get("total") else "—"
        mp = d.get("mpspace") or {}
        mps = ("работает" if mp.get("ok") else "сбой") if mp.get("installed") else ("—" if d["mpspace"] is None else "нет")
        print("  %-2s %-6s %-14s %-15s %s%-11s%s %-5s %-14s %-10s %s" % (
            "●" if str(d["id"]) == act else "", d["id"], (d.get("name") or "")[:14], d.get("ip") or "?",
            C[col], st, C["x"], d.get("mode") or "—", mod, mps, d.get("version") or "?"))
        if d.get("error") and d["state"] != "running":
            print("  %s       %s%s" % (C["d"], d["error"][:100], C["x"]))
    print("\n  ● — выбранный. Выбрать: pcs server use ID")


def c_server_list(a):
    known = store.all_servers()
    vms = pve.qm_list() if pve.is_pve() else {}
    rows = [{"id": s["id"], "name": s.get("name"), "ip": s.get("ip"), "mode": s.get("mode"),
             "vm": (vms.get(str(s["id"])) or (None, "absent"))[1] if vms or pve.is_pve() else None} for s in known]
    other = [{"id": int(k), "name": v[0], "vm": v[1]} for k, v in sorted(vms.items(), key=lambda x: int(x[0]))
             if k not in {str(s["id"]) for s in known}]
    if a.json:
        return 0, {"servers": rows, "other_vms": other, "active": store.active() or None}
    act = store.active()
    print("\n  %-2s %-7s %-14s %-16s %-6s %s" % ("", "ID", "имя", "адрес", "режим", "ВМ"))
    for r in rows:
        print("  %-2s %-7s %-14s %-16s %-6s %s" % ("●" if str(r["id"]) == act else "", r["id"], (r["name"] or "")[:14],
                                              r["ip"] or "?", r["mode"] or "—", r["vm"] or "?"))
    for r in other:
        print("  %s%-2s %-7s %-14s %-16s %-6s %s%s" % (ui.C["d"], "", r["id"], r["name"][:14], "—", "", r["vm"], ui.C["x"]))
    if act == store.HOST:
        print("\n  выбран хост")
    print("\n  ● — выбранный (pcs server use ID). Серые — ВМ не под PCS: pcs server adopt ID")
    return 0, None


def c_server_info(a):
    s = store.target(a.id)
    if s == store.HOST:
        raise Fail("это хост: pcs status")
    if a.json:
        d = servers.overview(s, timeout=25, wan=True)
        if a.password:
            d["password"] = s.get("password")
        return 0, d
    servers.print_info(s, show_password=a.password)
    return 0, None


def c_server_use(a):
    sid = store.set_active(a.id)
    ui.ok("выбран %s" % ("хост" if sid == store.HOST else "сервер %s" % sid))
    return 0, {"active": sid}


def ram_gb(v):
    if v is None:
        return None
    return max(1, v // 1024) if v >= 512 else v


def c_server_create(a):
    p = {"id": a.id, "name": a.name, "cores": a.cores, "ram_gb": ram_gb(a.ram), "disk_gb": a.disk,
         "storage": a.storage, "bridge": a.bridge, "ip": a.ip, "gw": a.gw, "dns": a.dns, "mode": a.mode,
         "sheet": a.sheet, "mpspace": a.mpspace, "replace": a.replace,
         "password": os.environ.get("PCS_PASSWORD", ""), "auth": os.environ.get("PCS_MP_AUTH", ""),
         "proxy": os.environ.get("PCS_MP_PROXY", "")}
    if ui.interactive() and not a.json:
        p = ask_create(p, a)
    p = servers.normalize(p)
    if p["replace"] and p["id"] and not a.yes:
        if not ui.confirm_id("ВМ %s будет удалена со всеми дисками и создана заново" % p["id"], p["id"]):
            raise Fail("отменено")
    pve.need_pve()
    return 0, servers.create(p)


def ask_create(p, a):
    """Вопросы человеку — только о том, что не задано флагами."""
    ui.hdr("Новый сервер: ВМ Ubuntu 24.04 под модемы")
    p["id"] = p["id"] or int(ui.ask("VM ID", str(pve.next_id())))
    p["name"] = p["name"] or ui.ask("Имя", "pcs%s" % p["id"])
    if not p["mode"]:
        p["mode"] = ui.ask("Режим proxyveth: usb (как USB-модемы, до ~40) или gw (шлюз, без предела)", "usb")
    gw_def, src, mask = pve.host_net()
    if not p["ip"] and not a.dhcp:
        p["ip"] = ui.ask("IP сервера с маской (Enter — DHCP; статика надёжнее для ЛК), напр. %s/%s"
                         % (".".join((src or "192.168.1.1").split(".")[:3] + ["60"]), mask), "")
    if p["ip"] and not p["gw"]:
        p["gw"] = ui.ask("Шлюз", gw_def)
    if not p["password"]:
        p["password"] = ui.ask("Пароль root (Enter — сгенерировать)", secret=True)
    if not p["sheet"]:
        p["sheet"] = ui.ask("Ссылка на Google-таблицу модемов (Enter — позже)", "")
    if not a.mpspace:
        p["mpspace"] = ui.confirm("Сразу поставить mobileproxy.space?", False)
    if p["mpspace"] and not p["auth"]:
        print("  auth.mp из ЛК: Мой прокси-бизнес → Сервера → иконка ↓ (Enter — задать позже: pcs mpspace auth)")
        p["auth"] = ui.ask("auth.mp", "")
    return p


def c_server_adopt(a):
    return 0, servers.adopt(a.id, ip=a.ip or "", password=os.environ.get("PCS_PASSWORD", ""))


def c_server_delete(a):
    s = store.load(a.id)
    if not a.yes and not ui.confirm_id("Удалить ВМ %s (%s) со всеми дисками? Модемы на ней пропадут"
                                       % (s["id"], s.get("name")), s["id"]):
        raise Fail("отменено (в скриптах — --yes)")
    return 0, servers.delete(s["id"])


def c_dns(a):
    t = store.target(a.server, a.host)
    dns = dnsfix.parse_dns(a.dns) if a.dns else None
    ui.step("DNS: %s" % store.label(t))
    d = ops.dns_fix(t, dns)
    for f in d.get("changed") or []:
        ui.info("изменён %s" % f)
    for f in d.get("foreign") or []:
        ui.info("снят чужой DNS %s" % f)
    ui.ok("DNS в порядке: %s (%s)" % (" ".join(d["dns"]), d.get("mode")))
    return 0, d


def c_passwd(a):
    t = store.target(a.server, a.host)
    pw = os.environ.get("PCS_PASSWORD") or ui.secret_twice("Новый пароль root, %s (Enter — сгенерировать)" % store.label(t))
    d = ops.set_password(t, pw)
    where = "/etc/pcs/servers/%s.json" % t["id"] if t != store.HOST else "нигде (это хост)"
    ui.ok("пароль root сменён (%s); сохранён: %s%s" % (store.label(t), where,
                                                      ("; новый: %s" % d["password"]) if d.get("password") else ""))
    return 0, d


def c_key(a):
    t = store.target(a.server, a.host)
    if a.rotate:
        d = ops.rotate(t)
        ui.ok("ключ хаба для сервера %s сменён: %s, старый снят" % (d["target"], d["fp"]))
        return 0, d
    if a.add:
        d = ops.key_add(t, a.add)
        (ui.ok if d.get("added") else ui.info)("%s %s: %s" % ("ключ добавлен" if d.get("added") else "ключ уже есть",
                                                             d.get("fp"), store.label(t)))
        return 0, d
    keys, hub = ops.key_list(t), ops.hub_key()
    if not a.json:
        print("\n  Ключи root, %s:" % store.label(t))
        for k in keys:
            mark = " (хаб)" if k["fp"] == hub["fp"] or "pcs" in (k.get("comment") or "") else ""
            print("    %s %s %s%s" % (k["type"], k["fp"], k.get("comment") or "", mark))
        print("\n  Ключ хаба: %s\n  %s" % (hub["fp"], hub["pub"]))
    return 0, {"keys": keys, "hub": hub}


def c_ssh_port(a):
    t = store.target(a.server)
    d = ops.ssh_port(t, a.port)
    ui.ok("SSH сервера %s — на порту %d (%s)" % (t["id"], a.port, d.get("via")))
    return 0, d


def c_mpspace(a):
    s = store.target(a.server)
    if s == store.HOST:
        raise Fail("mp.space ставится на сервер, а не на хост: --server ID")
    if a.action == "check":
        d = remote.node(s, "mpspace", "check", timeout=60)
        if not a.json:
            if not d.get("installed"):
                ui.warn("mobileproxy.space на сервере %s не стоит: pcs mpspace install" % s["id"])
            for u, st in (d.get("units") or {}).items():
                (ui.ok if st == "active" else ui.bad)("служба %s: %s" % (u, st))
            if d.get("installed"):
                (ui.ok if d.get("auth") else ui.bad)("auth.mp: %s" % ("порт %s" % d.get("port") if d.get("auth") else "не задан"))
        return 0, d
    if a.action == "auth":
        raw = os.environ.get("PCS_MP_AUTH") or (ui.ask("auth.mp из ЛК ({\"auth\": \"…\", \"port\": …})")
                                               if ui.interactive() else sys.stdin.readline().strip())
        if not raw:
            raise Fail("auth.mp пуст: вставь содержимое файла из ЛК (Мой прокси-бизнес → Сервера → ↓)")
        d = remote.node(s, "mpspace", "auth", input=raw + "\n", timeout=60)
        ui.ok("auth.mp записан на сервер %s (порт %s)" % (s["id"], d.get("port")))
        return 0, d
    auth = os.environ.get("PCS_MP_AUTH", "")
    if not auth and ui.interactive() and not a.json:
        print("  auth.mp из ЛК: Мой прокси-бизнес → Сервера → иконка ↓ (Enter — задать позже: pcs mpspace auth)")
        auth = ui.ask("auth.mp", "")
    lk = util.lock(os.path.join(remote.RUN, "srv-%s.lock" % s["id"]), wait=10, what="работа с сервером %s" % s["id"])
    try:
        return 0, servers.mpspace_install(s, auth, os.environ.get("PCS_MP_PROXY", ""), a.no_reboot)
    finally:
        lk.close()


def c_update(a):
    if a.host:
        return 0, {"host": servers.update_host()}
    if a.server or a.all:
        r = servers.update_servers([store.norm_id(x) for x in a.server] if a.server else None)
        if r["failed"]:
            for k, v in r["failed"].items():
                ui.bad("сервер %s: %s" % (k, v))
            return 1, r
        return 0, r
    return 0, servers.update({"host": True, "servers": "all" if store.all_servers() else None})


def c_web(a):
    if a.action == "serve":
        rc = web.serve(a.rest)
        return (rc or 0), None
    if a.action == "on":
        d = web.on()
        ui.ok("панель включена: http://%s:%d (порт открывает и закрывает пользователь)" % (servers.host_ip() or "хост", web.PORT))
        return 0, d
    if a.action == "off":
        d = web.off()
        ui.ok("панель выключена")
        return 0, d
    if a.action == "passwd":
        util.need_root()
        cur = (web.status().get("user") if os.path.exists(web.creds_path()) else None) or "admin"
        user = a.user or os.environ.get("PCS_WEB_USER") or (ui.ask("Логин панели", cur) if ui.interactive() else cur)
        pw = os.environ.get("PCS_WEB_PASS") or ui.secret_twice("Пароль панели (не короче 8)")
        d = web.set_login(user, pw)
        if web.status()["active"]:
            sh("systemctl", "try-restart", "pcs-web.service", check=False)
        ui.ok("вход в панель: %s, пароль сохранён хэшем в %s" % (user, web.creds_path()))
        return 0, d
    d = web.status()
    if not a.json:
        print("  панель: %s, автозапуск: %s, порт %d, логин: %s" % (
            "работает" if d["active"] else d["state"], "да" if d["enabled"] else "нет", d["port"], d["user"] or "не задан"))
    return 0, d


def c_job(a):
    if a.id == "run":
        if not a.run_id:
            raise Fail("pcs job run ID")
        from .api import KINDS
        return jobs.run(a.run_id, KINDS), None
    if a.id:
        d = jobs.get(a.id)
        if not a.json:
            print("\n  %s — %s (%s)" % (d["id"], d["title"], d["state"]))
            for ln in d["log"][-60:]:
                print("  │ " + ln)
            if d.get("error"):
                ui.bad(d["error"])
        return 0, d
    lst = jobs.recent()
    if not a.json:
        if not lst:
            print("  фоновых заданий нет")
        for d in lst:
            print("  %s  %-8s %s%s" % (d["id"], d["state"], d["title"], ("  ✗ " + d["error"]) if d.get("error") else ""))
    return 0, lst


def c_log(a):
    lines = []
    try:
        with open(ui.LOG, errors="replace") as f:
            lines = f.readlines()[-40:]
    except OSError:
        pass
    if not a.json:
        print("".join(lines) if lines else "  журнал пуст: %s" % ui.LOG)
    return 0, [ln.rstrip("\n") for ln in lines]


def c_doctor(a):
    """Проверка хоста и (если выбран) сервера — что мешает работать."""
    bad = []

    def chk(cond, text, fix=""):
        if cond:
            ui.ok(text)
        else:
            ui.bad(text + (" — " + fix if fix else ""))
            bad.append(text)
    ui.hdr("Хост")
    for prog in ("qm", "pvesm", "wget", "ssh", "ssh-keygen", "openssl"):
        chk(bool(__import__("shutil").which(prog)), "есть %s" % prog)
    for h in ("cloud-images.ubuntu.com", "codeload.github.com"):
        chk(sh("getent", "ahostsv4", h, check=False, timeout=15).returncode == 0, "DNS хоста: %s" % h, "pcs dns --host")
    chk("local" in sh("pvesm", "status", "--content", "snippets", check=False).stdout,
        "хранилище local умеет snippets", "поправит pcs server create")
    chk(os.path.exists(store.key_path()), "ключ хаба %s" % store.key_path(), "создастся сам при create/adopt")
    w = web.status()
    chk(w["login_set"], "логин панели задан", "pcs web passwd")
    ui.info("панель: %s" % ("работает" if w["active"] else w["state"]))
    hl = remote.local_part("hivelink", ["status"], timeout=30)
    (ui.ok if hl.get("ok") else ui.warn)("hivelink на хосте: %s" % ("в порядке" if hl.get("ok") else hl.get("error")))
    t = store.target(a.server, a.host, default_host=True)
    if t != store.HOST:
        ui.hdr("Сервер %s (%s)" % (t["id"], t.get("ip")))
        d = servers.overview(t, timeout=25)
        chk(d["state"] == "running", "ВМ работает, SSH отвечает, pcs-node есть", d.get("error") or d["state"])
        if d["state"] == "running":
            chk(d.get("version") == VERSION, "код PCS той же версии, что на хосте (%s)" % (d.get("version")),
                "pcs update --server %s" % t["id"])
            r = remote.ssh(t, "for h in github.com docs.google.com mobileproxy.space; do getent ahostsv4 $h >/dev/null "
                              "&& echo ok $h || echo FAIL $h; done; cloud-init status 2>/dev/null | head -1", check=False)
            for ln in r.stdout.splitlines():
                if ln.startswith(("ok ", "FAIL ")):
                    chk(ln.startswith("ok"), "DNS сервера: %s" % ln.split()[1], "pcs dns --server %s" % t["id"])
                elif ln.strip():
                    ui.info(ln.strip())
            if (d.get("info") or {}).get("proxyveth", {}).get("installed"):
                ui.hdr("proxyveth doctor")
                remote.stream(t, "proxyveth doctor")
    ui.say()
    (ui.ok if not bad else ui.bad)("проблем не найдено" if not bad else "проблем: %d" % len(bad))
    return (1 if bad else 0), {"problems": bad}


HANDLERS = {
    ("status", None): c_status, ("server", "list"): c_server_list, ("server", "info"): c_server_info,
    ("server", "use"): c_server_use, ("server", "create"): c_server_create, ("server", "adopt"): c_server_adopt,
    ("server", "delete"): c_server_delete, ("dns", None): c_dns, ("passwd", None): c_passwd, ("key", None): c_key,
    ("ssh-port", None): c_ssh_port, ("mpspace", None): c_mpspace, ("update", None): c_update, ("web", None): c_web,
    ("job", None): c_job, ("log", None): c_log, ("doctor", None): c_doctor,
}


def handler(a):
    fn = HANDLERS.get((a.cmd, getattr(a, "scmd", None)))
    if not fn:
        raise Fail("какая команда? справка: pcs help")
    return fn


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    as_json = "--json" in argv
    try:
        if not argv:
            if ui.interactive():
                util.need_root()
                from .menu import Menu
                return Menu().main()
            print(HELP)
            return 0
        cmd = argv[0]
        if cmd in ("help", "-h", "--help"):
            print(HELP)
            return 0
        if cmd in ("version", "--version"):
            print(json.dumps({"ok": True, "data": VERSION}) if as_json else VERSION)
            return 0
        if cmd in PASSTHRU:
            util.need_root()
            return passthru(cmd, argv[1:])
        if cmd in ("ssh", "exec"):
            util.need_root()
            return shell(cmd, argv[1:])
        a = parser().parse_args(argv)
        fn = handler(a)
        util.need_root()
        ui.log("pcs %s" % " ".join(x if not x.startswith("ssh-") else "<ключ>" for x in argv))
        return util.run_cli(fn, a, as_json=a.json)
    except Fail as e:
        say_fail(e, as_json)
        return 1
    except KeyboardInterrupt:
        return 130
    except Exception as e:                           # noqa: BLE001 — без трасс человеку
        ui.log("CRASH %s" % traceback.format_exc())
        say_fail(Fail("внутренняя ошибка: %s: %s (подробности — %s)" % (type(e).__name__, e, ui.LOG)), as_json)
        return 1
