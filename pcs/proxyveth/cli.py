"""proxyveth — из прокси сетевой интерфейс. Команда на сервере (docs/ARCHITECTURE.md §6).

Режим сервера — "mode" в /etc/proxyveth/config.json: usb или gw. Общее для обоих
режимов делает эта команда: таблица и её локальная копия, проверка, защита от
ошибки в таблице, маршрут к прокси через основной канал, таймер sync, смена
режима, переезд с vmodem 4.x. Модемы — функции модуля режима
(pcs/proxyveth/usb.py, pcs/proxyveth/gw.py) с одним интерфейсом:
setup, apply, up, down, status, diag, teardown, doctor.
"""
import argparse
import importlib
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
import traceback

from pcs import VERSION
from pcs.core import net, table
from pcs.core.util import QUIET, Busy, Fail, Log, jload, jsave, lock, merge, rd, run_cli, sh, wr
from pcs.proxyveth import usb

ETC = "/etc/proxyveth"
CONFIG = ETC + "/config.json"
TABLE = ETC + "/table.csv"
OLD_GW = ETC + "/env"                  # proxyveth-virt 3.4: режим gw, переезд делает gw.setup
RUN = "/run/proxyveth"
LOCK = RUN + "/lock"
UNITD = "/etc/systemd/system"
PY = "/usr/bin/python3"
CMD = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "bin", "proxyveth")
MODES = ("usb", "gw")
IFACE = ("setup", "apply", "up", "down", "status", "diag", "teardown", "doctor")
COMMON = {"source": "", "max_remove_share": 0.5}
ICON = {"ok": "✓", "warn": "⚠", "broken": "✗", "absent": "·", "disabled": "–", "rebooting": "↻"}

HELP = """proxyveth — из прокси сетевой интерфейс (режим сервера: usb или gw)

  proxyveth mode [usb|gw] [--yes]       режим сервера; смена — снять всё своё и поднять в новом
  proxyveth source [URL|local]          источник таблицы; local — главной становится локальная копия
  proxyveth table [show|pull|edit|put]  показать / из Google / копия в $EDITOR / копия со stdin
  proxyveth lint                        проверить таблицу, ничего не трогая
  proxyveth sync [--force]              привести модемы к таблице (таймер делает это сам)
  proxyveth up|down|restart N|all       down и restart всех — с --yes
  proxyveth status [--wan]              состояние (с --wan — внешний IP через каждый модем)
  proxyveth problems                    только проблемные
  proxyveth diag N                      прокси → логин → модем → SIM → интернет
  proxyveth rotate N | reboot N         смена IP / перезагрузка настоящего модема
  proxyveth doctor                      окружение
  proxyveth setup                       подготовить сервер (зовёт хаб при create/update)
  proxyveth version

  --json у любой команды: {"ok": true, "data": …} или {"ok": false, "error": "…"}
"""

log = Log("proxyveth")


def say(msg=""):
    if not QUIET[0]:
        print(msg, flush=True)


def need_root():
    if os.geteuid() != 0:
        raise Fail("нужен root")


def load_cfg():
    c = jload(CONFIG, {})
    return merge(COMMON, c if isinstance(c, dict) else {})


def save_cfg(**changes):
    c = jload(CONFIG, {})
    jsave(CONFIG, merge(c if isinstance(c, dict) else {}, changes))
    os.chmod(os.path.dirname(CONFIG), 0o700)


def held():
    return lock(LOCK, what="команда proxyveth")


# ── режим ──────────────────────────────────────────────────────────────────
def mode_mod(mode):
    """Модуль режима с интерфейсом §6. gw пишется отдельно — пока его нет, честно сказать."""
    if mode not in MODES:
        raise Fail("неизвестный режим %r — usb или gw" % mode)
    try:
        m = importlib.import_module("pcs.proxyveth." + mode)
    except Exception as e:
        raise Fail("режим %s ещё не готов: %s" % (mode, e))
    miss = [f for f in IFACE if not callable(getattr(m, f, None))]
    if miss:
        raise Fail("режим %s ещё не готов: нет %s" % (mode, ", ".join(miss)))
    return m


def cur_mode(cfg):
    return cfg.get("mode") if cfg.get("mode") in MODES else None


def need_mode(cfg):
    mode = cur_mode(cfg)
    if not mode:
        if usb.vmodem_found():
            raise Fail("здесь ещё vmodem 4.x — перенести на proxyveth: proxyveth setup")
        raise Fail("режим сервера не задан: proxyveth mode usb|gw")
    return mode, mode_mod(mode)


def confirm(a, text, word):
    if a.yes:
        return
    if a.json or not sys.stdin.isatty():
        raise Fail("%s — подтверди: --yes" % text)
    say("  ⚠ %s" % text)
    try:
        got = input("  для подтверждения введи «%s»: " % word).strip()
    except EOFError:
        got = ""
    if got != word:
        raise Fail("не подтверждено — ничего не менял")


# ── таблица ────────────────────────────────────────────────────────────────
def src_of(cfg):
    s = cfg.get("source") or ""
    if not s:
        raise Fail("таблица не задана: proxyveth source <ссылка на Google-таблицу> (или proxyveth source local)")
    return TABLE if s == "local" else s


def read_table(cfg, mode, fetch=True, strict=False):
    """fetch — из источника (с локальной копией про запас), иначе только копия.
    Таблица без единой годной строки — сломана: копия остаётся прошлой, модемы
    приводятся к ней. strict (для lint) — разбор именно того, что пришло."""
    taken, mp, note = net.local_octets(), mode == "usb", []
    if not fetch:
        text, stale = rd(TABLE), False
    else:
        src = src_of(cfg)
        prev = rd(TABLE) if src != TABLE else ""
        text, stale = table.fetch(src, TABLE, log=log)
        d, dis, _, probs = table.lint(text, taken, mp_tables=mp)
        if not d and not dis and prev and not stale:
            pd, pdis, _, _ = table.lint(prev, taken, mp_tables=mp)
            if pd or pdis:            # пустая или сломанная таблица — не повод забыть прошлую
                note.append("в таблице ни одной годной строки (%s) — считаю её сломанной, %s"
                            % (probs[0] if probs else "пусто", "копию не трогаю" if strict else "беру прошлую копию"))
                wr(TABLE + ".tmp", prev, 0o600)
                os.replace(TABLE + ".tmp", TABLE)
                if not strict:
                    text, stale = prev, True
    d, dis, inv, probs = table.lint(text, taken, mp_tables=mp)
    return {"text": text, "stale": stale, "desired": d, "disabled": dis, "invalid": inv, "problems": note + probs}


def show_problems(probs):
    for pr in probs:
        say("  " + (pr if pr.startswith("⚠") else "✗ " + pr))


def summary(t):
    return {"ok": sorted(t["desired"]), "disabled": sorted(t["disabled"]), "invalid": sorted(t["invalid"]),
            "problems": t["problems"], "stale": t["stale"]}


def pin(desired, mod):
    """Адреса прокси — через основной канал: и из таблицы, и у работающих модемов."""
    hosts = {p["host"] for p in desired.values()}
    live = getattr(mod, "live_hosts", None)
    if callable(live):
        hosts |= set(live())
    net.pin_proxies(sorted(hosts), log=log)


def running_of(mod):
    f = getattr(mod, "running", None)
    if callable(f):
        return list(f())
    return [r["n"] for r in mod.status() if r["state"] not in ("absent", "disabled")]


def do_sync(cfg, mode, mod, force=False):
    t = read_table(cfg, mode)
    for pr in t["problems"]:
        log(pr if pr.startswith("⚠") else "✗ " + pr)
    desired, invalid = t["desired"], t["invalid"]
    pin(desired, mod)
    now = running_of(mod)
    keep = [n for n in now if n in invalid]
    if keep:
        log("⚠ строки про работающие модемы %s кривые — модемы не трогаю" % keep)
    remove = [n for n in now if n not in desired and n not in invalid]
    hold = []
    share = float(cfg.get("max_remove_share", 0.5))
    if remove and len(remove) > max(2, share * len(now)) and not force:
        log("✗ таблица требует снести %d из %d модемов — похоже на ошибку в таблице; снести: proxyveth sync --force"
            % (len(remove), len(now)))
        hold = remove
    res = mod.apply(desired, dict(cfg, keep=sorted(set(keep) | set(hold))), force=force)
    return dict(res, held=hold, stale=t["stale"], problems=t["problems"])


def sync_units():
    return {
        "proxyveth-sync.service": """[Unit]
Description=proxyveth: привести модемы к таблице
After=network-online.target
Wants=network-online.target
[Service]
Type=oneshot
ExecStart=%s %s sync --quiet
""" % (PY, CMD),
        "proxyveth-sync.timer": """[Unit]
Description=proxyveth: модемы — при загрузке и каждые 2 минуты
[Timer]
OnBootSec=15
OnUnitInactiveSec=120
[Install]
WantedBy=timers.target
""",
    }


def ensure_timer():
    for name, body in sync_units().items():
        wr(os.path.join(UNITD, name), body)
    sh("systemctl", "daemon-reload")
    sh("systemctl", "enable", "--now", "proxyveth-sync.timer")


def migrate_vmodem():
    """vmodem 4.x → proxyveth usb (§13): настройки и таблица переезжают, старые модемы
    снимаются. Файлы vmodem удаляются потом — когда новое уже поставлено."""
    if not usb.vmodem_found():
        return False
    cfg = jload(CONFIG, {})
    cfg = cfg if isinstance(cfg, dict) else {}
    if cfg.get("mode") not in (None, "", "usb"):
        raise Fail("здесь vmodem 4.x, а режим proxyveth — %s: сначала proxyveth mode usb" % cfg["mode"])
    old = usb.vmodem_settings()
    jsave(CONFIG, dict(merge(old, cfg), mode="usb"))
    os.chmod(ETC, 0o700)
    usb.vmodem_carry()
    log("переезд с vmodem 4.x: настройки и таблица перенесены (источник: %s)" % (old.get("source") or "не задан"))
    usb.vmodem_down()
    return True


# ── команды ────────────────────────────────────────────────────────────────
def cmd_setup(a):
    need_root()
    with held():
        migrated = migrate_vmodem()
        cfg = load_cfg()
        mode = cur_mode(cfg)
        if not mode and os.path.exists(OLD_GW):
            mode = "gw"
        if not mode:
            # Нечего готовить — это не ошибка: так pcs update проходит по серверам без модемов
            log("режим сервера не задан — готовить нечего; выбрать: proxyveth mode usb|gw")
            return 0, {"mode": None, "migrated": False, "sync": None}
        mod = mode_mod(mode)
        save_cfg(mode=mode)
        mod.setup(load_cfg())
        if migrated:
            usb.vmodem_purge()
        ensure_timer()
        res = None
        if migrated and cfg.get("source"):
            log("поднимаю модемы по-новому")
            res = do_sync(load_cfg(), mode, mod)
        if migrated:
            usb.vmodem_dkms()
        log("proxyveth %s: режим %s, таймер proxyveth-sync включён" % (VERSION, mode))
        return 0, {"mode": mode, "migrated": migrated, "sync": res}


def cmd_mode(a):
    cfg = load_cfg()
    cur = cur_mode(cfg)
    if not a.mode:
        say("  режим: %s" % (cur or "не задан (proxyveth mode usb|gw)"))
        return 0, {"mode": cur}
    new = a.mode
    newmod = mode_mod(new)                # нового режима нет — старый не трогаем
    need_root()
    old_vm = usb.vmodem_found()
    if not cur:
        cur = "usb" if old_vm else ("gw" if os.path.exists(OLD_GW) else None)
    with held():
        if cur and cur != new:
            confirm(a, "режим %s → %s: все модемы сервера будут сняты и подняты заново" % (cur, new), new)
        migrated = migrate_vmodem() if old_vm else False
        if cur and cur != new:
            mode_mod(cur).teardown()
            log("режим %s снят" % cur)
        save_cfg(mode=new)
        newmod.setup(load_cfg())
        if migrated:
            usb.vmodem_purge()
        ensure_timer()
        cfg = load_cfg()
        res = do_sync(cfg, new, newmod) if cfg.get("source") else None
        if migrated:
            usb.vmodem_dkms()
        log("режим сервера: %s" % new)
        return 0, {"mode": new, "previous": cur, "migrated": migrated, "sync": res}


def cmd_source(a):
    cfg = load_cfg()
    if not a.url:
        have = os.path.exists(TABLE)
        data = {"source": cfg.get("source") or None, "sheet": cfg.get("sheet") or None,
                "copy": TABLE if have else None, "copy_time": int(os.path.getmtime(TABLE)) if have else None}
        say("  таблица: %s" % ({"local": "локальная копия %s" % TABLE}.get(cfg.get("source"), cfg.get("source"))
                               or "не задана"))
        if have:
            say("  копия: %s (%s)" % (TABLE, time.strftime("%F %T", time.localtime(data["copy_time"]))))
        return 0, data
    need_root()
    mode = cur_mode(cfg) or "usb"
    with held():
        if a.url == "local":
            if not os.path.exists(TABLE):
                os.makedirs(ETC, mode=0o700, exist_ok=True)
                wr(TABLE, "n,real,proxy,enabled", 0o600)
                say("  копии не было — положил пустую с шапкой: proxyveth table edit")
            save_cfg(source="local")
            say("  главная — локальная копия %s; Google больше не читается (вернуть: proxyveth source <ссылка>)" % TABLE)
            return 0, {"source": "local"}
        url = a.url
        sheet = {"sheet": url} if re.match(r"https?://", url) else {}
        tmp = TABLE + ".new"
        if os.path.exists(tmp):
            os.unlink(tmp)
        try:
            text, _ = table.fetch(url, tmp, log=log)
        except Fail as e:
            if not a.force:
                raise Fail("таблица по ссылке не читается (%s) — не запоминаю; запомнить всё равно: --force" % e)
            save_cfg(source=url, **sheet)
            say("  ⚠ запомнил %s, хотя сейчас она не читается (%s)\n"
                "    модемы создадутся сами, когда таблица откроется: таймер пробует раз в 2 минуты" % (url, e))
            return 0, {"source": url, "readable": False}
        if os.path.exists(tmp):
            os.replace(tmp, TABLE)
        d, dis, inv, probs = table.lint(text, net.local_octets(), mp_tables=(mode == "usb"))
        save_cfg(source=url, **sheet)
        say("  запомнил: %s\n  годных строк %d, выключено %d, отбраковано %d" % (url, len(d), len(dis), len(inv)))
        show_problems(probs)
        say("  создать модемы: proxyveth sync")
        return 0, {"source": url, "readable": True, "ok": sorted(d), "disabled": sorted(dis),
                   "invalid": sorted(inv), "problems": probs}


def cmd_table(a):
    act = a.action or "show"
    cfg = load_cfg()
    mode = cur_mode(cfg) or "usb"
    if act == "show":
        if not os.path.exists(TABLE):
            raise Fail("локальной копии таблицы нет: proxyveth table pull (или proxyveth source <ссылка>)")
        t = read_table(cfg, mode, fetch=False)
        say(t["text"])
        say("\n  годных %d, выключено %d, отбраковано %d — %s" % (
            len(t["desired"]), len(t["disabled"]), len(t["invalid"]), TABLE))
        show_problems(t["problems"])
        return 0, dict(summary(t), source=cfg.get("source") or None, path=TABLE, text=t["text"])
    need_root()
    if act == "pull":
        src = cfg.get("source") or ""
        if src == "local":
            src = cfg.get("sheet") or ""
            if not src:
                raise Fail("ссылки на Google-таблицу нет — главная и так локальная копия")
            confirm(a, "главная — локальная копия: её правки заменятся таблицей из Google", "да")
        if not src:
            raise Fail("таблица не задана: proxyveth source <ссылка на Google-таблицу>")
        with held():
            text, stale = table.fetch(src, TABLE, log=lambda m: None)
            if stale:
                raise Fail("таблица недоступна — локальная копия не тронута")
            t = read_table(cfg, mode, fetch=False)
            say("  копия обновлена из %s: годных %d, выключено %d, отбраковано %d" % (
                src, len(t["desired"]), len(t["disabled"]), len(t["invalid"])))
            show_problems(t["problems"])
            return 0, summary(t)
    if act == "put":
        # Таблица со stdin в локальную копию — так её сохраняет панель
        text = sys.stdin.read()
        if not text.strip() or "\0" in text:
            raise Fail("таблица пустая — не сохраняю")
        d, dis, inv, probs = table.lint(text, net.local_octets(), mp_tables=(mode == "usb"))
        if not d and not dis:
            raise Fail("в таблице ни одной годной строки (%s) — копию не трогаю" % (probs[0] if probs else "пусто"))
        with held():
            os.makedirs(ETC, mode=0o700, exist_ok=True)
            wr(TABLE + ".tmp", text, 0o600)
            os.replace(TABLE + ".tmp", TABLE)
        show_problems(probs)
        say("  копия сохранена: годных %d, выключено %d, отбраковано %d" % (len(d), len(dis), len(inv)))
        return 0, {"changed": True, "ok": sorted(d), "disabled": sorted(dis), "invalid": sorted(inv),
                   "problems": probs, "source": cfg.get("source") or None}
    # edit
    if a.json or not sys.stdin.isatty():
        raise Fail("table edit — только в терминале")
    with held():
        os.makedirs(RUN, exist_ok=True)
        tmp = os.path.join(RUN, "table-edit.csv")
        before = (open(TABLE).read() if os.path.exists(TABLE) else "n,real,proxy,enabled\n")
        wr(tmp, before, 0o600)
        editor = os.environ.get("VISUAL") or os.environ.get("EDITOR") or ("nano" if shutil.which("nano") else "vi")
        subprocess.call(shlex.split(editor) + [tmp])
        after = open(tmp).read()
        os.unlink(tmp)
        if after == before:
            say("  без изменений")
            return 0, {"changed": False}
        d, dis, inv, probs = table.lint(after, net.local_octets(), mp_tables=(mode == "usb"))
        show_problems(probs)
        say("  годных %d, выключено %d, отбраковано %d" % (len(d), len(dis), len(inv)))
        if inv:
            confirm(a, "в таблице есть отбракованные строки", "да")
        os.makedirs(ETC, mode=0o700, exist_ok=True)
        wr(TABLE + ".tmp", after, 0o600)
        os.replace(TABLE + ".tmp", TABLE)
        if cfg.get("source") != "local":
            say("  ⚠ источник — Google-таблица: при следующем sync копия перезапишется.\n"
                "    Сделать главной копию: proxyveth source local")
        else:
            say("  сохранено; применит таймер, сразу — proxyveth sync")
        return 0, {"changed": True, "ok": sorted(d), "disabled": sorted(dis), "invalid": sorted(inv)}


def cmd_lint(a):
    cfg = load_cfg()
    t = read_table(cfg, cur_mode(cfg) or "usb", strict=True)
    show_problems(t["problems"])
    say("  годных %d (выключено %d), отбраковано n: %s%s" % (
        len(t["desired"]), len(t["disabled"]), ",".join(map(str, sorted(t["invalid"]))) or "—",
        "   [таблица из локальной копии]" if t["stale"] else ""))
    return (1 if t["invalid"] else 0), summary(t)


def cmd_sync(a):
    need_root()
    try:
        lk = lock(LOCK, wait=0 if a.quiet else 300, what="команда proxyveth")
    except Busy:
        if a.quiet:              # таймер: идёт ручная команда — пропустить этот заход
            return 0, {"skipped": True}
        raise
    with lk:
        cfg = load_cfg()
        mode, mod = need_mode(cfg)
        res = do_sync(cfg, mode, mod, force=a.force)
        if not a.quiet:
            say("\n  создано %s | пересоздано %s | снесено %s | не вышло %s%s" % (
                res["created"] or "—", res["recreated"] or "—", res["removed"] or "—",
                ",".join(map(str, res["failed"])) or "—", " | не снесено (таблица подозрительная) %s" % res["held"]
                if res["held"] else ""))
            print_rows(mod.status())
        return (1 if res["failed"] else 0), res


def parse_n(s, allow_all=False):
    if allow_all and s == "all":
        return None
    if not s.isdigit() or not 1 <= int(s) <= 254:
        raise Fail("номер модема — 1..254%s, а не %r" % (" или all" if allow_all else "", s))
    return int(s)


def cmd_updown(a):
    need_root()
    n = parse_n(a.n, allow_all=True)
    cfg = load_cfg()
    mode, mod = need_mode(cfg)
    if n is None and a.cmd in ("down", "restart"):
        confirm(a, "%s все модемы сервера" % ("снять" if a.cmd == "down" else "пересоздать"), "да")
    with held():
        if a.cmd in ("up", "restart"):
            pin(read_table(cfg, mode, fetch=False)["desired"], mod)
        if a.cmd in ("down", "restart"):
            mod.down(n)
        if a.cmd in ("up", "restart"):
            mod.up(n)
        if a.cmd == "down":
            say("  таймер поднимет %s снова, если есть в таблице; убрать насовсем — enabled=0 в таблице"
                % ("модем" if n else "модемы"))
        return 0, {"n": n, "action": a.cmd}


def print_rows(rows):
    say("\n  %-4s %-5s %-22s %-2s %-9s %-16s %s" % ("n", "real", "прокси", "", "у сервера", "внешний IP", "состояние"))
    for r in rows:
        say("  %-4d %-5s %-22s %-2s %-9s %-16s %s" % (
            r["n"], r["real"] or "—", r["proxy"] or "—", ICON.get(r["state"], "?"), r["iface"] or "—",
            r["ext_ip"] or "—", "в порядке" if r["state"] == "ok" else "; ".join(r["problems"]) or r["state"]))
    c = {s: sum(1 for r in rows if r["state"] == s) for s in ICON}
    say("\n  модемов %d: в порядке %d, с предупреждением %d, сломано %d, нет %d, выключено %d%s" % (
        len(rows), c["ok"], c["warn"], c["broken"], c["absent"], c["disabled"],
        ", перезагружается %d" % c["rebooting"] if c["rebooting"] else ""))


def cmd_status(a):
    cfg = load_cfg()
    _, mod = need_mode(cfg)
    rows = mod.status(wan=a.wan)
    print_rows(rows)
    return 0, rows


def cmd_problems(a):
    cfg = load_cfg()
    _, mod = need_mode(cfg)
    rows = [r for r in mod.status() if r["state"] not in ("ok", "disabled")]
    if rows:
        print_rows(rows)
    else:
        say("  проблем нет")
    return (1 if rows else 0), rows


def cmd_diag(a):
    n = parse_n(a.n)
    cfg = load_cfg()
    _, mod = need_mode(cfg)
    d = mod.diag(n, udp=True) if a.udp else mod.diag(n)
    say("  модем %d%s" % (n, (" (real %s, прокси %s)" % (d.get("real"), d.get("proxy"))) if d.get("proxy") else ""))
    for s in d["steps"]:
        say("    %s %-13s %s" % ("✓" if s["ok"] else "✗", s["name"], s["text"]))
    say("  итог: %s%s" % (d["verdict"], (" — " + d["text"]) if d.get("text") else ""))
    return (0 if d["verdict"] == "ok" else 1), d


def cmd_rotate(a):
    n = parse_n(a.n)
    cfg = load_cfg()
    mode, mod = need_mode(cfg)
    fn = getattr(mod, a.cmd, None)
    if not callable(fn):
        raise Fail("в режиме %s команды %s нет" % (mode, a.cmd))
    res = fn(n)
    if a.cmd == "reboot":
        say("  ✓ модем %d перезагружен" % n)
    return 0, res


def cmd_doctor(a):
    cfg = load_cfg()
    mode = cur_mode(cfg)
    out = [{"name": "режим", "ok": bool(mode), "text": mode or "не задан: proxyveth mode usb|gw"},
           {"name": "источник таблицы", "ok": bool(cfg.get("source")), "text": cfg.get("source") or "не задан"},
           {"name": "локальная копия", "ok": os.path.exists(TABLE),
            "text": time.strftime("%F %T", time.localtime(os.path.getmtime(TABLE))) if os.path.exists(TABLE) else "нет"}]
    timer = sh("systemctl", "is-active", "proxyveth-sync.timer", check=False).stdout.strip()
    out.append({"name": "таймер proxyveth-sync", "ok": timer == "active", "text": timer or "нет"})
    if mode:
        try:
            out += mode_mod(mode).doctor()
        except Fail as e:
            out.append({"name": "режим %s" % mode, "ok": False, "text": str(e)})
    for c in out:
        say("  %s %s%s" % ("✓" if c["ok"] else "✗", c["name"], (" — " + c["text"]) if c["text"] else ""))
    return (0 if all(c["ok"] for c in out) else 1), out


def cmd_version(a):
    mode = cur_mode(load_cfg())
    say(VERSION)
    return 0, {"version": VERSION, "mode": mode}


# служебное для юнитов режима usb — в справке нет
def cmd_routes(a):
    usb.routes(parse_n(a.n))
    return 0, None


def cmd_hostside(a):
    usb.hostside(a.iface)
    return 0, None


def cmd_replug(a):
    msg = usb.replug_cmd(parse_n(a.n), a.reboot)
    say("  " + msg)
    return 0, {"n": int(a.n), "text": msg}


COMMANDS = {"setup": cmd_setup, "mode": cmd_mode, "source": cmd_source, "table": cmd_table, "lint": cmd_lint,
            "sync": cmd_sync, "up": cmd_updown, "down": cmd_updown, "restart": cmd_updown, "status": cmd_status,
            "problems": cmd_problems, "diag": cmd_diag, "rotate": cmd_rotate, "reboot": cmd_rotate,
            "doctor": cmd_doctor, "version": cmd_version, "routes": cmd_routes, "hostside": cmd_hostside,
            "replug": cmd_replug}


class Parser(argparse.ArgumentParser):
    def error(self, message):
        m = re.match(r"argument (\w+): invalid choice: '([^']*)'", message)
        if m:
            message = ("нет такой команды: %s" if m.group(1) == "cmd" else "не то значение: %s") % m.group(2)
        elif message.startswith("the following arguments are required"):
            message = "не хватает аргумента: %s" % message.split(":", 1)[1].strip()
        elif message.startswith("unrecognized arguments"):
            message = "лишнее: %s" % message.split(":", 1)[1].strip()
        raise Fail("%s — справка: proxyveth help" % message)


def parser():
    ap = Parser(prog="proxyveth", add_help=False)
    sub = ap.add_subparsers(dest="cmd", parser_class=Parser)
    sub.required = True
    for c in ("setup", "lint", "problems", "doctor", "version"):
        sub.add_parser(c, add_help=False)
    p = sub.add_parser("mode", add_help=False)
    p.add_argument("mode", nargs="?", choices=MODES)
    p.add_argument("--yes", action="store_true")
    p = sub.add_parser("source", add_help=False)
    p.add_argument("url", nargs="?")
    p.add_argument("--force", action="store_true")
    p = sub.add_parser("table", add_help=False)
    p.add_argument("action", nargs="?", choices=("show", "pull", "edit", "put"))
    p.add_argument("--yes", action="store_true")
    p = sub.add_parser("sync", add_help=False)
    p.add_argument("--force", action="store_true")
    p.add_argument("--quiet", action="store_true")
    for c in ("up", "down", "restart"):
        p = sub.add_parser(c, add_help=False)
        p.add_argument("n")
        p.add_argument("--yes", action="store_true")
    p = sub.add_parser("status", add_help=False)
    p.add_argument("--wan", action="store_true")
    p = sub.add_parser("diag", add_help=False)
    p.add_argument("n")
    p.add_argument("--udp", action="store_true")
    for c in ("rotate", "reboot", "routes"):
        sub.add_parser(c, add_help=False).add_argument("n")
    sub.add_parser("hostside", add_help=False).add_argument("iface")
    p = sub.add_parser("replug", add_help=False)
    p.add_argument("n")
    p.add_argument("--reboot", action="store_true")
    return ap


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    as_json = "--json" in argv
    argv = [x for x in argv if x != "--json"]
    if not argv or argv[0] in ("help", "-h", "--help"):
        print(HELP)
        return 0

    def go(_):
        a = parser().parse_args(argv)
        a.json = as_json
        a.yes = getattr(a, "yes", False)
        try:
            rc, data = COMMANDS[a.cmd](a)
        except (Fail, KeyboardInterrupt):
            raise
        except OSError as e:
            raise Fail("%s%s" % (e.strerror or e, (": " + e.filename) if getattr(e, "filename", None) else ""))
        except Exception as e:        # ошибка в коде: человеку — строка, трасса — в журнал
            try:
                with open(log.path, "a") as f:
                    f.write("%s %s" % (time.strftime("%F %T"), traceback.format_exc()))
            except OSError:
                pass
            raise Fail("внутренняя ошибка (%s: %s) — подробности в %s" % (e.__class__.__name__, e, log.path))
        return (0 if as_json else rc), data

    return run_cli(go, None, as_json)
