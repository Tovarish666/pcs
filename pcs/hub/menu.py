"""Меню хаба (§11): `pcs` без аргументов.

Главное — не перепутать сервер. Поэтому:
  • наверху каждого экрана рамка выбранного сервера: номер, имя, IP, хост или ВМ,
    режим proxyveth, модемы ok/total, mp.space; у каждого сервера свой цвет рамки;
  • сервер есть и в строке ввода: [2000 pcs2000 192.168.88.7] »;
  • опасное — только после ввода номера сервера;
  • «0 — назад» на каждом экране; неверный ввод меню не роняет.
Пункты выполняются отдельным процессом `pcs …` — сбой команды не роняет меню.
"""
import os
import re
import subprocess
import sys
import time
import traceback

from .. import VERSION
from ..core.util import Fail
from . import remote, store, ui

WIDTH = 66
FRAME_COLORS = ("c", "m", "bl", "y")
ANSI = re.compile(r"\x1b\[[0-9;]*m")


class Quit(Exception):
    pass


def vlen(s):
    return len(ANSI.sub("", s))


class Menu:
    def __init__(self, inp=None, out=None, call=None, fetch=None, color=None, clear=None, ttl=20):
        self.inp = inp or (lambda prompt: ui.INPUT[0](prompt))
        self.out = out or sys.stdout
        self.call = call or self._call
        self.fetch = fetch or self._fetch
        self.C = ui.C if color is None else ui.palette(color)
        self.clear = sys.stdout.isatty() if clear is None else clear
        self.ttl = ttl
        self.cache = {}
        self.sel = store.active()

    # ── вывод ──────────────────────────────────────────────────────────────
    def p(self, text=""):
        print(text, file=self.out, flush=True)

    def read(self, prompt):
        try:
            return self.inp(prompt).strip()
        except EOFError:
            raise Quit()

    def _call(self, argv):
        return subprocess.call([sys.executable, os.path.join(remote.ROOT, "bin", "pcs")] + argv)

    def _fetch(self, sel):
        from . import servers
        if sel == store.HOST:
            import socket
            return {"kind": "host", "id": "хост", "name": socket.gethostname(), "ip": servers.host_ip(),
                    "state": "running", "version": VERSION}
        s = store.load(sel)
        d = servers.overview(s, timeout=8)
        d["kind"] = "vm"
        return d

    def info(self, force=False):
        if not self.sel:
            return None
        hit = self.cache.get(self.sel)
        if hit and not force and time.time() - hit[0] < self.ttl:
            return hit[1]
        try:
            d = self.fetch(self.sel)
        except Fail as e:
            d = {"kind": "vm", "id": self.sel, "name": "?", "ip": "?", "state": "unknown", "error": str(e)}
        self.cache[self.sel] = (time.time(), d)
        return d

    def color(self):
        if self.sel == store.HOST:
            return "y"
        ids = [str(s["id"]) for s in store.all_servers()]
        i = ids.index(self.sel) if self.sel in ids else 0
        return FRAME_COLORS[i % len(FRAME_COLORS)] if self.sel != store.HOST else "y"

    def frame(self):
        """Рамка выбранного сервера — несколько строк, цветная, всегда наверху."""
        C = self.C
        d = self.info()
        col = C["b"] + C[self.color()]
        lines = []
        if not d:
            lines = ["%sСЕРВЕР НЕ ВЫБРАН%s" % (C["inv"], C["x"] + col), "выбрать — клавиша v"]
        else:
            st = {"running": (C["g"], "работает"), "stopped": (C["r"], "остановлена"), "paused": (C["y"], "на паузе"),
                  "absent": (C["r"], "нет в Proxmox"), "offline": (C["r"], "нет связи по SSH"),
                  "old": (C["y"], "старый код — pcs update"), "unknown": (C["r"], "неизвестно")}.get(
                d.get("state"), (C["y"], d.get("state") or "?"))
            if d.get("kind") == "host":
                head = "%s ХОСТ %s  %s · %s" % (C["inv"], C["x"] + col, d.get("name"), d.get("ip") or "?")
                lines = [head, "Proxmox · PCS %s · proxyveth и modlink — на ВМ (клавиша v)" % VERSION]
            else:
                head = "%s ВМ %s %s  %s · %s" % (C["inv"], d.get("id"), C["x"] + col, d.get("name"), d.get("ip") or "?")
                m = d.get("modems") or {}
                mod = "модемы %s/%s" % (m.get("ok", 0), m.get("total", 0)) if m.get("total") else "модемов нет"
                if m.get("warn"):
                    mod += "  ⚠ %d" % m["warn"]
                if m.get("broken"):
                    mod += "  ✗ %d" % m["broken"]
                mp = d.get("mpspace") or {}
                mps = ("работает" if mp.get("ok") else ("стоит, есть сбои" if mp.get("installed") else "не стоит")) \
                    if d.get("mpspace") is not None else "?"
                lines = [head + "  %s●%s %s" % (st[0], C["x"] + col, st[1]),
                         "ВМ Proxmox · proxyveth %s · %s" % (d.get("mode") or "—", mod),
                         "mp.space: %s · PCS %s" % (mps, d.get("version") or "?")]
                if d.get("error"):
                    lines.append("⚠ " + d["error"][:WIDTH - 6])
        self.p()
        self.p("  %s╔%s╗%s" % (col, "═" * (WIDTH - 2), C["x"]))
        for ln in lines:
            pad = WIDTH - 4 - vlen(ln)
            self.p("  %s║ %s%s ║%s" % (col, ln, " " * max(pad, 0), C["x"]))
        self.p("  %s╚%s╝%s" % (col, "═" * (WIDTH - 2), C["x"]))

    def prompt(self):
        C = self.C
        d = self.info()
        if not d:
            tag = "не выбран"
        elif d.get("kind") == "host":
            tag = "хост %s %s" % (d.get("name"), d.get("ip") or "")
        else:
            tag = "%s %s %s" % (d.get("id"), d.get("name"), d.get("ip") or "")
        return "  %s[%s]%s » " % (C["b"] + C[self.color()], tag.strip(), C["x"])

    def screen(self, title, items):
        if self.clear:
            self.out.write("\033[H\033[2J")
        self.frame()
        C = self.C
        self.p("\n  %s%s%s" % (C["b"], title, C["x"]))
        for key, label, _ in items:
            if key == "-":
                self.p()
                continue
            self.p("  %s%3s%s  %s" % (C["b"], key, C["x"], label))
        self.p("\n  %s  v%s  сменить сервер      %s  0%s  %s" % (C["b"], C["x"], C["b"], C["x"],
                                                                "выход" if title == "PCS" else "назад"))

    def note(self, msg, icon="⚠"):
        self.p("  %s%s%s %s" % (self.C["y"], icon, self.C["x"], msg))

    def pause(self):
        self.read("\n  %sEnter — назад в меню%s " % (self.C["d"], self.C["x"]))

    # ── выбор сервера ──────────────────────────────────────────────────────
    def choose(self):
        srv = store.all_servers()
        self.p("\n  %sКакой сервер?%s" % (self.C["b"], self.C["x"]))
        self.p("  %8s  хост Proxmox (hivelink, DNS, пароль, ключ)" % "хост")
        for s in srv:
            self.p("  %8s  %-14s %-16s %s" % (s["id"], s.get("name", "")[:14], s.get("ip") or "?", s.get("mode") or ""))
        a = self.read("  Номер ВМ или «хост» (Enter — оставить %s): " % (self.sel or "как есть"))
        if not a:
            return
        try:
            sid = store.norm_id(a)
            store.set_active(sid)
        except Fail as e:
            self.note(str(e))
            self.pause()
            return
        self.sel = sid
        self.cache.pop(sid, None)

    # ── ввод значений ──────────────────────────────────────────────────────
    def ask(self, label, pattern=None, default="", hint=""):
        """Значение или None (Enter без значения по умолчанию — отмена)."""
        d = (" [%s]" % default) if default else ""
        a = self.read("  ? %s%s%s: " % (label, (" (%s)" % hint) if hint else "", d)) or default
        if not a:
            return None
        if pattern and not re.fullmatch(pattern, a):
            raise Fail("не подходит: %r — %s" % (a, hint or label))
        return a

    def confirm_server(self, what):
        """Опасное: подтвердить вводом номера сервера (у хоста — его имени)."""
        d = self.info() or {}
        expect = d.get("name") if self.sel == store.HOST else self.sel
        self.p("\n  %s%s⚠ %s%s" % (self.C["b"], self.C["r"], what, self.C["x"]))
        a = self.read("  Введи номер сервера %s%s%s, чтобы подтвердить (Enter — отмена): "
                      % (self.C["b"], expect, self.C["x"]) if self.sel != store.HOST else
                      "  Введи имя хоста %s%s%s, чтобы подтвердить (Enter — отмена): " % (self.C["b"], expect, self.C["x"]))
        if a != str(expect):
            self.note("не подтверждено — ничего не делаю")
            return False
        return True

    def need_server(self):
        if not self.sel:
            raise Fail("сервер не выбран — клавиша v")
        if self.sel == store.HOST:
            raise Fail("выбран хост, а это — для ВМ: выбери сервер клавишей v")
        return self.sel

    def tgt(self):
        """--server ID или --host для команды хаба."""
        if not self.sel:
            raise Fail("сервер не выбран — клавиша v")
        return ["--host"] if self.sel == store.HOST else ["--server", self.sel]

    def run(self, argv, pause=True):
        self.p()
        rc = self.call(argv)
        self.cache.clear()
        if rc not in (0, None):
            self.note("команда завершилась с кодом %s" % rc, "✗")
        if pause:
            self.pause()
        return rc

    # ── экраны ─────────────────────────────────────────────────────────────
    def loop(self, title, items):
        while True:
            self.screen(title, items)
            ch = self.read(self.prompt()).lower()
            if ch in ("0", "q", "й", "выход", "назад"):
                return
            if ch in ("v", "м"):
                self.choose()
                continue
            act = {k: fn for k, _, fn in items if k != "-"}.get(ch)
            if not act:
                if ch:
                    self.note("нет такого пункта: %s" % ch)
                    self.pause()
                continue
            try:
                act()
            except Fail as e:
                self.note(str(e), "✗")
                self.pause()
            except KeyboardInterrupt:
                self.p()
                self.note("прервано")
            except Quit:
                raise
            except Exception as e:                       # noqa: BLE001 — меню не падает
                ui.log("menu: %s" % traceback.format_exc())
                self.note("внутренняя ошибка: %s: %s (подробности — /var/log/pcs/pcs.log)" % (type(e).__name__, e), "✗")
                self.pause()

    def main(self):
        items = [
            ("1", "Серверы            список, создать, взять, удалить, обновить", self.m_servers),
            ("2", "Модемы             proxyveth на выбранном сервере", self.m_modems),
            ("3", "Прокси             modlink на выбранном сервере", self.m_proxies),
            ("4", "Софт               mp.space, hivelink", self.m_soft),
            ("5", "Сеть и доступ      DNS, пароль root, ключ SSH, порт SSH, консоль", self.m_access),
            ("6", "Диагностика        сводка, doctor, журнал", self.m_diag),
        ]
        try:
            self.loop("PCS", items)
        except (Quit, KeyboardInterrupt):
            self.p()
        return 0

    # Серверы
    def m_servers(self):
        def delete():
            sid = self.need_server()
            if self.confirm_server("Удалить ВМ %s со всеми дисками? Модемы на ней пропадут." % sid):
                self.run(["server", "delete", sid, "--yes"])
                self.sel = store.active()

        def update_one():
            sid = self.need_server()
            if self.confirm_server("Обновить код PCS на сервере %s? proxyveth setup пересоберёт модемы, если нужно." % sid):
                self.run(["update", "--server", sid])

        def update_all():
            self.p("\n  %s⚠ Обновит PCS на хосте (из GitHub) и на ВСЕХ серверах.%s" % (self.C["r"], self.C["x"]))
            if self.read("  Введи слово «все», чтобы подтвердить (Enter — отмена): ") != "все":
                self.note("не подтверждено — ничего не делаю")
                return
            self.run(["update"])

        def adopt():
            sid = self.ask("Номер ВМ (VM ID)", r"\d+", hint="число")
            if sid:
                self.run(["server", "adopt", sid])
                self.sel = store.active()

        self.loop("Серверы", [
            ("1", "Хост и все серверы (сводка)", lambda: self.run(["status"])),
            ("2", "Сведения о выбранном и сводка для ЛК mp.space", lambda: self.run(["server", "info", self.need_server()])),
            ("3", "Создать сервер — ВМ Ubuntu 24.04 под ключ", lambda: self.run(["server", "create"])),
            ("4", "Взять существующую ВМ под управление", adopt),
            ("5", "Обновить код PCS на выбранном сервере", update_one),
            ("6", "Обновить PCS везде: хост из GitHub и все серверы", update_all),
            ("7", "Удалить выбранную ВМ", delete),
        ])

    # Модемы
    def m_modems(self):
        def pv(*args):
            return lambda: self.run(["proxyveth"] + list(args) + ["--server", self.need_server()])

        def source():
            self.need_server()
            u = self.ask("Ссылка на Google-таблицу или local", hint="local — главной станет локальная копия")
            if u:
                self.run(["proxyveth", "source", u, "--server", self.sel])

        def diag():
            self.need_server()
            n = self.ask("Номер модема", r"\d{1,3}", hint="1–254")
            if n:
                self.run(["proxyveth", "diag", n, "--server", self.sel])

        def updown(verb):
            def go():
                self.need_server()
                n = self.ask("Номер модема или all", r"\d{1,3}|all", hint="1–254 или all")
                if not n:
                    return
                if (n == "all" or verb == "down") and not self.confirm_server(
                        "proxyveth %s %s на сервере %s — модемы пропадут у клиентов." % (verb, n, self.sel)):
                    return
                self.run(["proxyveth", verb, n, "--server", self.sel])
            return go

        def mode():
            self.need_server()
            m = self.ask("Режим", r"usb|gw", hint="usb или gw")
            if m and self.confirm_server("Сменить режим сервера %s на %s? Все модемы снимутся и поднимутся заново."
                                         % (self.sel, m)):
                self.run(["proxyveth", "mode", m, "--yes", "--server", self.sel])

        self.loop("Модемы — proxyveth", [
            ("1", "Состояние", pv("status")),
            ("2", "Состояние с внешними IP (дольше)", pv("status", "--wan")),
            ("3", "Только проблемные", pv("problems")),
            ("4", "Привести к таблице (sync)", pv("sync")),
            ("5", "Таблица: показать", pv("table", "show")),
            ("6", "Таблица: проверить, ничего не трогая (lint)", pv("lint")),
            ("7", "Источник таблицы", source),
            ("8", "Диагностика модема", diag),
            ("9", "Поднять модем", updown("up")),
            ("10", "Перезапустить модем", updown("restart")),
            ("11", "Опустить модем", updown("down")),
            ("12", "Режим сервера usb / gw", mode),
            ("13", "Окружение (doctor)", pv("doctor")),
        ])

    # Прокси
    def m_proxies(self):
        def ml(*args):
            return lambda: self.run(["modlink"] + list(args) + ["--server", self.need_server()])

        def with_id(verb, danger=False):
            def go():
                self.need_server()
                i = self.ask("ID прокси (из списка)", r"\d+", hint="число")
                if not i:
                    return
                if danger and not self.confirm_server("modlink %s %s на сервере %s" % (verb, i, self.sel)):
                    return
                self.run(["modlink", verb, i, "--server", self.sel])
            return go

        def add():
            self.need_server()
            ip = self.ask("Адрес интерфейса модема на сервере (lan-ip)", r"\d+\.\d+\.\d+\.\d+", hint="192.168.N.100")
            if ip:
                self.run(["modlink", "add", "--lan-ip", ip, "--password", "gen", "--port", "auto",
                          "--server", self.sel])

        self.loop("Прокси — modlink", [
            ("1", "Список", ml("list")),
            ("2", "Состояние", ml("status")),
            ("3", "Предложить по интерфейсам сервера (from-ifaces)", ml("from-ifaces")),
            ("4", "Добавить прокси", add),
            ("5", "Проверить прокси", with_id("test")),
            ("6", "Включить", with_id("enable")),
            ("7", "Выключить", with_id("disable", danger=True)),
            ("8", "Удалить", with_id("del", danger=True)),
            ("9", "Применить (apply)", ml("apply")),
            ("10", "Строки для клиента (export, с паролями)", ml("export")),
        ])

    # Софт
    def m_soft(self):
        def mp(action):
            return lambda: self.run(["mpspace", action, "--server", self.need_server()])

        def hl(*args):
            return lambda: self.run(["hivelink"] + list(args) + self.tgt())

        def hl_install():
            where = "хост" if self.sel == store.HOST else "сервер %s" % self.need_server()
            self.p("  hivelink — драйвер настоящих модемов. Ставлю на %s." % where)
            self.run(["hivelink", "install"] + self.tgt())

        self.loop("Софт — mp.space и hivelink", [
            ("1", "mp.space: поставить на выбранный сервер", mp("install")),
            ("2", "mp.space: задать auth.mp", mp("auth")),
            ("3", "mp.space: проверить службы", mp("check")),
            ("4", "Сводка для ЛК mp.space (с паролем root)",
             lambda: self.run(["server", "info", self.need_server(), "--password"])),
            ("-", "", None),
            ("5", "hivelink: состояние", hl("status")),
            ("6", "hivelink: поставить на выбранный (хост или ВМ)", hl_install),
            ("7", "hivelink: диагностика (doctor)", hl("doctor")),
        ])

    # Сеть и доступ
    def m_access(self):
        def dns():
            cur = self.ask("DNS через пробел", r"[0-9a-fA-F:., ]+", default="1.1.1.1 8.8.8.8")
            if cur:
                self.run(["dns", "--dns", cur] + self.tgt())

        def passwd():
            if self.sel == store.HOST and not self.confirm_server(
                    "Пароль root хоста — это и вход в веб-интерфейс Proxmox."):
                return
            self.run(["passwd"] + self.tgt())

        def key_add():
            k = self.ask("Открытый ключ (ssh-ed25519 AAAA… комментарий)")
            if k:
                self.run(["key", "--add", k] + self.tgt())

        def rotate():
            sid = self.need_server()
            if self.confirm_server("Новый ключ хаба для сервера %s: старый перестанет подходить." % sid):
                self.run(["key", "--rotate", "--server", sid])

        def port():
            sid = self.need_server()
            n = self.ask("Новый порт SSH", r"\d{1,5}", hint="1–65535")
            if n and self.confirm_server("Сменить порт SSH сервера %s на %s?" % (sid, n)):
                self.run(["ssh-port", n, "--server", sid])

        def console():
            if self.sel == store.HOST:
                raise Fail("это и есть хост — консоль уже открыта")
            self.run(["ssh", self.need_server()], pause=False)

        def execute():
            c = self.ask("Команда")
            if c:
                self.run(["exec", "host" if self.sel == store.HOST else self.need_server(), c])

        self.loop("Сеть и доступ", [
            ("1", "Починить DNS", dns),
            ("2", "Пароль root", passwd),
            ("3", "Ключи SSH: показать", lambda: self.run(["key"] + self.tgt())),
            ("4", "Ключи SSH: добавить свой", key_add),
            ("5", "Ключ хаба для сервера: сменить (rotate)", rotate),
            ("6", "Порт SSH сервера", port),
            ("7", "Консоль сервера (ssh)", console),
            ("8", "Выполнить команду", execute),
        ])

    # Диагностика
    def m_diag(self):
        self.loop("Диагностика", [
            ("1", "Хост и все серверы (сводка)", lambda: self.run(["status"])),
            ("2", "Проверить хост и выбранный сервер (doctor)", lambda: self.run(["doctor"] + self.tgt())),
            ("3", "proxyveth doctor", lambda: self.run(["proxyveth", "doctor", "--server", self.need_server()])),
            ("4", "hivelink doctor", lambda: self.run(["hivelink", "doctor"] + self.tgt())),
            ("5", "Журнал PCS (последние 40 строк)", lambda: self.run(["log"])),
            ("6", "Фоновые задания", lambda: self.run(["job"])),
        ])
