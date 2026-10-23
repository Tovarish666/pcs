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
import time
import traceback

from .. import VERSION
from ..core import bg
from ..core import dns as dnsfix
from ..core import util
from ..core.util import Fail, sh
from . import jobs, node, ops, pve, remote, servers, store, ui, web

HELP = """pcs %s — хаб фермы мобильных прокси на хосте Proxmox

  pcs                                   меню
  pcs status [--json]                   хост и все серверы сразу
  pcs server list | info [ID] [--password] | use ID|host
  pcs server create [--mode usb|gw] [--sheet URL] [--mpspace] [--id N] [--name …]
                    [--ip A.B.C.D/M --gw …|--dhcp] [--cores N] [--ram ГБ] [--disk ГБ]
  pcs server adopt ID [--ip IP] | delete ID [--yes]   добавить ВМ в PCS / удалить ВМ с дисками
  pcs server start|shutdown|reboot|stop ID           питание ВМ (shutdown — штатно, stop — жёстко)
  pcs server forget ID [--yes]          убрать из PCS (ВМ не трогается) — так чистят удалённые мимо PCS
  pcs server color ID [красный|оранжевый|жёлтый|зелёный|голубой|синий|фиолетовый]   цвет в дереве
  pcs server trust-key ID [--accept SHA256:…]   ключ SSH сервера: сверить; сменился — принять
  pcs proxyveth …  [--server ID|--host] команда proxyveth на сервере или самом хосте (как есть)
  pcs modlink …    [--server ID|--host]   без выбранного сервера — на хосте
  pcs hivelink …   [--server ID|--host]
  pcs dns     [--server ID|--host] [--dns "1.1.1.1 8.8.8.8"]
  pcs passwd  [--server ID|--host]      пароль root (Enter — сгенерировать)
  pcs key     [--server ID|--host] [--add PUBKEY | --rotate]
  pcs ssh [ID] | pcs exec [ID] 'cmd'    консоль / команда на сервере
  pcs ssh-port PORT [--server ID]       порт SSH сервера
  pcs mpspace install|auth|check [--server ID|--host]   на хост — только Ubuntu (VPS)
  pcs update [--host | --server ID | --all]   без флагов — хост из GitHub и все серверы
  pcs web on|off|status|passwd          веб-панель :666
  pcs doctor [--server ID|--host] | pcs log | pcs version
  pcs node [list|add АДРЕС|remove ИМЯ|open ИМЯ]   присоединённые хосты (VPS, другой Proxmox): pcs node help
  pcs node ИМЯ proxyveth|modlink|hivelink …       команда части на том хосте
  pcs token [list | revoke ID]          кто присоединил этот хост (токены его хабов)
  pcs job [ID] [--server ID]            долгое (update, установка, перенос…) идёт в фоне: обрыв
                                        связи ему не мешает; здесь — список, журнал, «смотреть дальше»
  pcs job ID --retry                    повторить упавшее (создание ВМ — продолжит ту же ВМ)
  pcs host-setup                        хост после установки/обновления: команды, hivelink, modlink

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
    for name in ("start", "shutdown", "reboot", "stop"):
        srv.add_parser(name).add_argument("id")
    c = srv.add_parser("forget")
    c.add_argument("id")
    c.add_argument("--yes", action="store_true")
    c = srv.add_parser("color")
    c.add_argument("id")
    c.add_argument("color", nargs="?", default="")
    c = srv.add_parser("trust-key")
    c.add_argument("id")
    c.add_argument("--accept", metavar="SHA256:…")
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
    c.add_argument("--host", action="store_true")
    c.add_argument("--no-reboot", action="store_true")
    c = sub.add_parser("host-setup")
    c.add_argument("--mode", choices=servers.MODES)
    c = sub.add_parser("update")
    g = c.add_mutually_exclusive_group()
    g.add_argument("--host", action="store_true", help="только хост из GitHub")
    g.add_argument("--server", action="append", help="код хоста — на этот сервер")
    g.add_argument("--all", action="store_true", help="код хоста — на все серверы")
    c = sub.add_parser("web")
    c.add_argument("action", choices=("on", "off", "status", "passwd", "serve"))
    c.add_argument("--user")
    c.add_argument("rest", nargs=argparse.REMAINDER)
    c = sub.add_parser("token")
    c.add_argument("action", nargs="?", choices=("list", "revoke"), default="list")
    c.add_argument("id", nargs="?")
    c = sub.add_parser("job")
    c.add_argument("id", nargs="?")
    c.add_argument("run_id", nargs="?")
    c.add_argument("--server")
    c.add_argument("--retry", action="store_true")
    sub.add_parser("log")
    for p in list(sub.choices.values()) + list(srv.choices.values()):
        p.add_argument("--json", action="store_true")
    return ap


def split_target(args, allow_host=True):
    """--server ID / --server=ID / --host — откуда угодно в аргументах части; остальное — как есть.
    --host — всегда флаг хаба: для proxyveth это ошибка, а не «молча на выбранный сервер»."""
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
    rest, server, host = split_target(args)
    as_json = "--json" in rest
    try:
        # modlink и hivelink есть и на хосте: без выбранного сервера — туда
        t = store.target(server, host, default_host=part in remote.HOST_PARTS
                         and (part != "proxyveth" or not pve.is_pve()))
        if not rest:
            rest = ["help"] if not as_json else ["status", "--json"]
        if t == store.HOST:
            if part not in remote.HOST_PARTS:
                raise Fail("на хосте нет %s — он на серверах: --server ID" % part)
            if not as_json and sys.stdout.isatty():
                ui.info("%s на хосте" % part)
            return subprocess.call([os.path.join(remote.ROOT, "bin", part)] + rest)
        if not as_json and sys.stdout.isatty():
            ui.info("%s на %s" % (part, store.label(t)))
        # номер сервера — частям на сервере: их подсказка «смотреть снова» будет для хоста
        return remote.stream(t, "%s=%s " % (bg.HUB_ID, t["id"]) + " ".join(shlex.quote(x) for x in [part] + rest),
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
    rest, server, host = split_target(args)
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
    for s in known:
        servers.sync_name(s, (vms.get(str(s["id"])) or (None, None))[0])
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


def as_job(a, kind, params, title, target=None):
    """Долгое из терминала — заданием хаба (юнит systemd) и показывать его журнал: обрыв SSH
    не убивает работу. → (код, данные) или None — делать здесь (не терминал, --json, уже в фоне)."""
    if getattr(a, "json", False) or not bg.wanted():
        return None
    from . import api                     # noqa: F401 — виды заданий
    jid = jobs.start(kind, params, title=title, target=target)
    rc = jobs.follow(jid)
    d = jobs.get(jid, lines=0)
    return rc, d.get("result")


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
    r = as_job(a, "create_server", p, "Создание ВМ %s" % (p.get("name") or p.get("id") or ""), target=p.get("id") or "new")
    return r if r else (0, servers.create(p))


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


def c_server_power(a):
    return 0, servers.power(store.load(a.id)["id"], a.scmd)


def c_server_forget(a):
    s = store.load(a.id)
    if not a.yes and not ui.confirm_id("Убрать ВМ %s (%s) из PCS? Сама ВМ, её диски и модемы останутся"
                                       % (s["id"], s.get("name")), s["id"]):
        raise Fail("отменено (в скриптах — --yes)")
    return 0, servers.forget(s["id"])


def c_server_color(a):
    d = servers.set_color(store.load(a.id)["id"], a.color)
    ui.ok("цвет ВМ %s: %s" % (d["id"], servers.COLOR_RU.get(d.get("color"), "по номеру")))
    return 0, d


def c_server_trust_key(a):
    """Ключ SSH сервера. Без --accept: сверить и, если сменился, а guest agent его подтвердил, — принять."""
    s = store.load(a.id)
    if a.accept:
        d = remote.rekey(s, accept=a.accept.strip())
        ui.ok("сервер %s: принят ключ %s (был: %s)" % (s["id"], ", ".join(d["new"]), ", ".join(d["old"]) or "—"))
        return 0, d
    d = remote.host_key(s)
    ui.info("знает хаб:    %s" % (", ".join(d["known"]) or "—"))
    ui.info("показывает:   %s" % (", ".join(d["shown"]) or "— (сервер не ответил)"))
    ui.info("guest agent:  %s" % (", ".join(d["agent"]) if d["agent"] is not None else "сверить нечем"))
    if d["same"]:
        ui.ok("ключ сервера %s не менялся" % s["id"])
        return 0, d
    if not d["shown"]:
        raise Fail("сервер %s не отдал ключ — он включён?" % s["id"])
    if d["agent_ok"]:
        d.update(remote.rekey(s), accepted=True)
        ui.ok("ключ сменился, guest agent его подтвердил — принят: %s" % ", ".join(d["new"]))
        return 0, d
    ui.warn("ключ сменился, подтвердить нечем; если сервер переустанавливали — сверь отпечаток в консоли ВМ "
            "(ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub) и прими: pcs server trust-key %s --accept ОТПЕЧАТОК" % s["id"])
    return 1, d


def c_dns(a):
    t = store.target(a.server, a.host)
    dns = dnsfix.parse_dns(a.dns) if a.dns else None
    ui.step("DNS: %s" % store.label(t))
    d = ops.dns_fix(t, dns)
    for f in d.get("changed") or []:
        ui.info("изменён %s" % f)
    for f in d.get("foreign") or []:
        ui.info("снят чужой DNS %s" % f)
    if d.get("want") and d["want"] != d["dns"]:
        ui.warn("заданные %s теряют запросы — стоят %s; таймер pcs-dns-check вернёт их, когда заработают"
                % (" ".join(d["want"]), " ".join(d["dns"])))
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
    s = store.target(a.server, getattr(a, "host", False), default_host=not pve.is_pve())
    if s == store.HOST:
        return mpspace_here(a)
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
    r = as_job(a, "mpspace", {"id": s["id"], "auth": auth, "proxy": os.environ.get("PCS_MP_PROXY", ""),
                              "no_reboot": a.no_reboot}, "mp.space на сервер %s" % s["id"], target=s["id"])
    if r:
        return r
    lk = util.lock(os.path.join(remote.RUN, "srv-%s.lock" % s["id"]), wait=10, what="работа с сервером %s" % s["id"])
    try:
        return 0, servers.mpspace_install(s, auth, os.environ.get("PCS_MP_PROXY", ""), a.no_reboot)
    finally:
        lk.close()


def c_update(a):
    if a.host:
        params, key = {"host": True}, "host"
    elif a.server or a.all:
        params = {"servers": [int(store.norm_id(x)) for x in a.server] if a.server else "all"}
        key = "all" if a.all else ",".join(str(x) for x in params["servers"])
    else:
        params, key = {"host": True, "servers": "all" if store.all_servers() else None}, "all"
    r = as_job(a, "update", params, "Обновление PCS (%s)" % key, target=key)
    if r:
        return r
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


def c_host_setup(a):
    r = as_job(a, "host_setup", {"mode": a.mode}, "Хост: команды и части PCS", target="host")
    if r:
        return r
    d = servers.host_setup(mode=a.mode)
    return (1 if d["failed"] else 0), d


def mpspace_here(a):
    """mp.space прямо на этом хосте (Ubuntu, VPS)."""
    from . import api
    if a.action == "check":
        d = api.mpspace("host", "check")
        if not a.json:
            (ui.ok if d.get("installed") else ui.warn)("mobileproxy.space на этом хосте: %s"
                                                       % ("стоит" if d.get("installed") else "не стоит"))
        return 0, d
    auth = os.environ.get("PCS_MP_AUTH", "")
    if a.action == "auth":
        auth = auth or (ui.ask("auth.mp из ЛК ({\"auth\": \"…\", \"port\": …})") if ui.interactive()
                        else sys.stdin.readline().strip())
        return 0, api.mpspace("host", "auth", auth=auth)
    ok_, why = servers.mpspace_allowed()
    if not ok_:
        raise Fail(why)
    # PCS_MP_AUTH задан (пусть и пустым — «позже») — не спрашивать второй раз (так зовёт install.sh)
    if not auth and "PCS_MP_AUTH" not in os.environ and ui.interactive() and not a.json:
        print("  auth.mp из ЛК: Мой прокси-бизнес → Сервера → иконка ↓ (Enter — задать позже: pcs mpspace auth)")
        auth = ui.ask("auth.mp", "")
    params = {"auth": auth, "proxy": os.environ.get("PCS_MP_PROXY", ""), "no_reboot": a.no_reboot}
    r = as_job(a, "mpspace_host", params, "mp.space на этот хост", target="host")
    return r if r else (0, servers.mpspace_host(params))


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
    from . import api
    d["fingerprint"] = api.self_fingerprint()
    if not a.json:
        print("  панель: %s, автозапуск: %s, порт %d, логин: %s" % (
            "работает" if d["active"] else d["state"], "да" if d["enabled"] else "нет", d["port"], d["user"] or "не задан"))
        if d["fingerprint"]:
            print("  HTTPS на том же порту; отпечаток (сверить при присоединении): %s" % d["fingerprint"])
    return 0, d


# ── присоединённые хосты: pcs node … ──────────────────────────────────────
NODE_HELP = """  pcs node [list]                              присоединённые хосты и их состояние
  pcs node add АДРЕС [--user admin] [--name ИМЯ] [--fingerprint FP]
                                               присоединить хост (VPS или другой Proxmox): логин и
                                               пароль его панели; пароль — PCS_NODE_PASSWORD или вопрос
  pcs node remove ИМЯ [--yes]                  отсоединить (хост отзовёт наш токен)
  pcs node open ИМЯ                            одноразовая ссылка входа в его панель (60 с)
  pcs node ИМЯ status                          сводка хоста
  pcs node ИМЯ proxyveth|modlink|hivelink …    команда части на том хосте (долгая — заданием)
  pcs node ИМЯ job [ID]                        его фоновые задания"""


def node_cmd(argv):
    """pcs node … — свой разбор: после имени хоста идёт команда части как есть."""
    as_json = "--json" in argv
    argv = [x for x in argv if x != "--json"]

    def opt(name, default=None):
        if name in argv:
            i = argv.index(name)
            if i + 1 >= len(argv):
                raise Fail("%s: нужно значение" % name)
            v = argv[i + 1]
            del argv[i:i + 2]
            return v
        return default

    def fn(_):
        from . import api, nodes
        if not argv or argv[0] == "list":
            lst = api.nodes_overview()
            if not as_json:
                if not lst:
                    print("  присоединённых хостов нет — присоединить: pcs node add АДРЕС")
                for d in lst:
                    h = d.get("host_info") or {}
                    m = h.get("modems") or {}
                    print("  %s %-14s %-22s %-8s %s" % (
                        "✓" if d["ok"] else "✗", d["name"], "%s:%s" % (d["host"], d["port"]), h.get("kind") or d.get("kind") or "?",
                        ("модемы %s/%s · ВМ %d · PCS %s" % (m.get("ok", 0), m.get("total", 0), len(d.get("servers") or []),
                                                          h.get("version") or "?")) if d["ok"] else d.get("error")))
            return 0, lst
        if argv[0] in ("help", "-h", "--help"):
            print(NODE_HELP)
            return 0, None
        if argv[0] == "add":
            fp, user, name = opt("--fingerprint"), opt("--user", "admin"), opt("--name")
            if len(argv) != 2:
                raise Fail("pcs node add АДРЕС [--user admin] [--name ИМЯ] [--fingerprint FP]")
            pw = os.environ.get("PCS_NODE_PASSWORD") or (ui.ask("Пароль панели того хоста (логин %s)" % user, secret=True)
                                                         if ui.interactive() else sys.stdin.readline().rstrip("\n"))
            d = api.node_join(argv[1], user, pw, name=name, fingerprint=fp)
            if not as_json:
                info = d.get("info") or {}
                ui.ok("присоединён: %s (%s:%s, %s, PCS %s)" % (d["name"], d["host"], d["port"], info.get("kind") or "?",
                                                              d.get("version") or "?"))
                print("  отпечаток сертификата: %s — дальше хаб ходит только к нему" % d["fingerprint"])
            return 0, d
        if argv[0] == "remove":
            yes = "--yes" in argv
            argv[:] = [x for x in argv if x != "--yes"]
            if len(argv) != 2:
                raise Fail("pcs node remove ИМЯ [--yes]")
            if not yes and not (ui.interactive() and ui.confirm("Отсоединить хост %s?" % argv[1], False)):
                raise Fail("отсоединение хоста %s — подтверди: pcs node remove %s --yes" % (argv[1], argv[1]))
            d = api.node_remove(argv[1])
            if not as_json:
                ui.ok("отсоединён: %s%s" % (d["name"], "" if d["revoked"] else " (хост не ответил — токен на нём отзови: pcs token)"))
            return 0, d
        if argv[0] == "open":
            if len(argv) != 2:
                raise Fail("pcs node open ИМЯ")
            d = api.node_login(argv[1])
            if not as_json:
                print("  %s   (одноразовая, %d с)" % (d["url"], d["ttl"]))
            return 0, d
        name, rest = argv[0], argv[1:]
        nodes.load(name)
        if not rest or rest[0] == "status":
            d = nodes.overview_one(nodes.load(name))
            if not as_json:
                print(json.dumps(d, ensure_ascii=False, indent=1))
            return (0 if d["ok"] else 1), d
        if rest[0] == "job":
            e = api.node_call(name, "GET", "jobs/" + rest[1] if len(rest) > 1 else "jobs")
            if not e.get("ok"):
                raise Fail(e.get("error"))
            if not as_json:
                print(json.dumps(e["data"], ensure_ascii=False, indent=1))
            return 0, e["data"]
        if rest[0] not in PASSTHRU:
            raise Fail("на хосте %s — части %s; справка: pcs node help" % (name, ", ".join(PASSTHRU)))
        e = api.node_call(name, "POST", "run", {"target": "host", "part": rest[0], "args": rest[1:],
                                                "confirm": "host" if any(x in ("--yes", "--force") for x in rest) else ""},
                          timeout=600)
        if not e.get("ok"):
            raise Fail(e.get("error"))
        d = e.get("data")
        if isinstance(d, dict) and set(d) == {"job"}:
            d = node_follow(name, d["job"], quiet=as_json)
        elif not as_json:
            print(json.dumps(d, ensure_ascii=False, indent=1))
        return 0, d
    return util.run_cli(fn, None, as_json)


def node_follow(name, jid, quiet=False):
    """Долгая команда на присоединённом хосте: печатать её журнал, пока не кончится. → итог."""
    import time
    from . import api
    seen = 0
    if not quiet:
        print("  ▸ задание %s на хосте %s — связь оборвётся, задание доработает там" % (jid, name))
    while True:
        e = api.node_call(name, "GET", "jobs/" + jid, timeout=30)
        if not e.get("ok"):
            raise Fail(e.get("error"))
        j = e["data"]
        log = j.get("log") or []
        if not quiet:
            for ln in log[seen:]:
                print("  │ " + ln)
        seen = len(log)
        if j.get("state") not in ("running", "queued"):
            if j.get("state") != "done":
                raise Fail(j.get("error") or "задание на хосте %s не удалось" % name)
            return j.get("result")
        time.sleep(1.5)


def c_token(a):
    from ..web import tokens
    if a.action == "revoke":
        if not a.id:
            raise Fail("pcs token revoke ID — ID из pcs token list")
        d = tokens.revoke(a.id)
        ui.ok("токен %s отозван — тот хаб больше не управляет этим хостом" % a.id)
        return 0, d
    lst = tokens.listing()
    if not a.json:
        if not lst:
            print("  токенов нет — этот хост никто не присоединял")
        for t in lst:
            print("  %s  %-24s выдан %s с %s, последний раз %s" % (
                t["id"], t.get("name", ""), time.strftime("%F %T", time.localtime(t["created"])), t.get("from") or "?",
                time.strftime("%F %T", time.localtime(t["last"])) if t.get("last") else "—"))
    return 0, lst


def c_job(a):
    if a.id == "run":
        if not a.run_id:
            raise Fail("pcs job run ID")
        from .api import KINDS
        return jobs.run(a.run_id, KINDS), None
    if a.server:                           # фоновые запуски на сервере (proxyveth, modlink там)
        s = store.target(a.server)
        if a.json:
            return 0, remote.node(s, "bg", *([a.id] if a.id else []), timeout=60)
        cmd = "%s=%s pcs-node bg" % (bg.HUB_ID, s["id"]) + (" " + shlex.quote(a.id) if a.id else "")
        return remote.stream(s, cmd, tty=sys.stdout.isatty()), None
    if a.id and a.retry:
        new = jobs.retry(a.id)
        if a.json:
            return 0, {"job": new}
        return jobs.follow(new), {"job": new}
    if a.id:
        if bg.is_id(a.id):
            d = bg.get(a.id)
        else:
            d = jobs.get(a.id)
        if not a.json and d["state"] == "running" and sys.stdout.isatty():
            return (bg.follow(a.id) if d.get("kind") == "bg" else jobs.follow(a.id)), None
        if not a.json:
            print("\n  %s — %s (%s)" % (d["id"], d["title"], STATE.get(d["state"], d["state"])))
            for ln in d["log"][-80:]:
                print("  │ " + ln)
            if d.get("error"):
                ui.bad(d["error"])
            if d.get("retry"):
                ui.info("повторить: pcs job %s --retry" % d["id"])
        return (0 if d["state"] in ("done", "running") else 1), d
    lst = sorted(jobs.recent() + bg.recent(), key=lambda d: d.get("started") or 0, reverse=True)[:30]
    if not a.json:
        if not lst:
            print("  фоновых заданий нет")
        for d in lst:
            print("  %s  %-10s %s%s" % (d["id"], STATE.get(d["state"], d["state"]), d["title"],
                                         ("  ✗ " + d["error"]) if d.get("error") else ""))
        run = [d for d in lst if d["state"] == "running"]
        if run:
            print("\n  идёт: %s — смотреть: pcs job %s" % (run[0]["title"], run[0]["id"]))
        print("  на сервере: pcs job --server ID")
    return 0, lst


STATE = {"running": "идёт", "done": "готово", "failed": "ошибка"}


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
    ml = node.modlink_info()
    chk(ml["installed"], "modlink на хосте", "pcs update --host (или modlink setup)")
    if ml["installed"]:
        ui.info("modlink: демон %(modlink)s, 3proxy %(modlink-3proxy)s" % ml["state"])
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
    ("server", "delete"): c_server_delete, ("server", "start"): c_server_power, ("server", "shutdown"): c_server_power,
    ("server", "reboot"): c_server_power, ("server", "stop"): c_server_power, ("server", "forget"): c_server_forget,
    ("server", "color"): c_server_color, ("server", "trust-key"): c_server_trust_key, ("dns", None): c_dns, ("passwd", None): c_passwd, ("key", None): c_key,
    ("ssh-port", None): c_ssh_port, ("mpspace", None): c_mpspace, ("update", None): c_update, ("web", None): c_web,
    ("host-setup", None): c_host_setup,
    ("job", None): c_job, ("log", None): c_log, ("doctor", None): c_doctor, ("token", None): c_token,
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
        if cmd == "node":
            util.need_root()
            return node_cmd(argv[1:])
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
