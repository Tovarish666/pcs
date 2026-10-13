"""Демон modlink.service: ссылки реконнекта и ребута, автореконнект по таймеру, проверки, состояние.

Строки берёт из применённого (/var/lib/modlink/applied.json) — того же, из чего собран
конфиг 3proxy: до `modlink apply` ссылки и прокси не расходятся. SIGHUP — перечитать.

Наружу открыты только порты управления (как в modlink для Windows):
  GET http://<ip>:<порт>/reconnect[/<пароль>]   — сменить IP
  GET http://<ip>:<порт>/reboot[/<пароль>]      — перезагрузить модем
Порт свой у строки — пароль не нужен. Общий у нескольких строк (режим портов shared) —
строку выбирает пароль (или логин) в пути, либо HTTP Basic (логин:пароль).
Браузеру — страница («было → стало»), скрипту — JSON {"ok": true, "ip": "…"} / {"ok": false, "error": "…"}.

Пока порт реконнекта строки занят кем-то ещё (старая панель modlink-linux), таймер строки
молчит: строку обслуживает тот, у кого порт, — иначе реконнекты задвоятся.
Проверки: раз в час внешний IP через каждый прокси (wan_check), раз в день в 12:00 —
скорость (speedtest, по умолчанию выкл: это трафик модема). → /var/lib/modlink/checks.json.
"""
import base64
import html
import json
import os
import signal
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from ..core.util import Busy, Fail, jsave, jload
from . import hilink, ops, store
from .store import P

RETRY = 30                    # с, повторная попытка занять порт управления
WAN_EVERY = 3600              # с, проверка внешнего IP
WAN_FIRST = 60                # с после старта — первая
SPEED_HOUR = 12               # час суточного теста скорости (местное время)
ACTIONS = ("reconnect", "reboot")


def journal(msg):
    """В journald (stdout юнита): у него своя ротация; реконнекты — в журналах строк."""
    print(msg, flush=True)


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


PAGE = """<!doctype html><html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>%(title)s</title>
<style>body{margin:0;min-height:100vh;display:flex;align-items:center;justify-content:center;
background:#0b0b0d;color:#f3f4f6;font:15px system-ui,-apple-system,"Segoe UI",sans-serif}
.c{background:#121216;border:1px solid rgba(255,255,255,.08);border-radius:14px;padding:28px 32px;
min-width:280px;max-width:92vw;text-align:center}.t{color:#9a9aa2;font-size:13px;margin-bottom:14px}
.ip{font:600 24px ui-monospace,Menlo,Consolas,monospace;margin:6px 0}.old{color:#62626a;text-decoration:line-through}
.ok{color:#5ee0a0}.warn{color:#f0a94a}.err{color:#fb7185}.m{color:#9a9aa2;font-size:13px;margin-top:12px}</style>
</head><body><div class="c"><div class="t">%(title)s</div>%(body)s</div></body></html>"""


def page(title, body):
    return PAGE % {"title": html.escape(title), "body": body}


def handler(d, port):
    class H(BaseHTTPRequestHandler):
        timeout = 15
        server_version = "modlink"
        sys_version = ""

        def log_message(self, *a):
            pass

        def do_GET(self):
            u = urllib.parse.urlsplit(self.path)
            parts = [x for x in u.path.split("/") if x]
            if not parts or parts[0] not in ACTIONS or len(parts) > 2:
                return self.reply(404, {"ok": False, "error": "есть только GET /reconnect и /reboot"})
            token = urllib.parse.unquote(parts[1]) if len(parts) == 2 else ""
            login = pw = None
            a = self.headers.get("Authorization") or ""
            if a[:6].lower() == "basic ":
                try:
                    login, _, pw = base64.b64decode(a[6:].strip()).decode("utf-8", "replace").partition(":")
                except ValueError:
                    pass
            r, code, why = d.resolve(port, parts[0], token, login, pw)
            if r is None:
                return self.reply(code, {"ok": False, "error": why})
            self.reply(*d.act(r, parts[0], self.wants_html()), row=r, action=parts[0])

        def wants_html(self):
            q = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
            return "json" not in q and "text/html" in (self.headers.get("Accept") or "")

        def do_POST(self):
            self.reply(405, {"ok": False, "error": "только GET /reconnect и /reboot"})

        do_PUT = do_DELETE = do_PATCH = do_POST

        def reply(self, code, body, row=None, action=None):
            if self.wants_html():
                data = render(code, body, row, action).encode()
                ctype = "text/html; charset=utf-8"
            else:
                data = json.dumps({k: v for k, v in body.items() if k != "old"}, ensure_ascii=False).encode()
                ctype = "application/json; charset=utf-8"
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)
    return H


def render(code, body, row, action):
    name = (row or {}).get("name") or ("строка %s" % (row or {}).get("id", "?"))
    if action == "reboot":
        title = "Ребут · " + name
        if body.get("ok"):
            return page(title, '<div class="ip ok">перезагрузка отправлена</div><div class="m">модем вернётся минуты через 1–2</div>')
    else:
        title = "Реконнект · " + name if row else "modlink"
        if body.get("ok"):
            old, new = body.get("old"), body.get("ip")
            if new and old and old != new:
                return page(title, '<div class="ip old">%s</div><div class="ip ok">%s</div><div class="m">IP сменился</div>'
                            % (html.escape(old), html.escape(new)))
            if new and old == new:
                return page(title, '<div class="ip warn">%s</div><div class="m">IP не сменился — оператор выдал тот же</div>'
                            % html.escape(new))
            return page(title, '<div class="ip ok">%s</div><div class="m">модем переподключён</div>'
                        % html.escape(new or "готово"))
    return page(title, '<div class="ip err">не вышло</div><div class="m">%s</div>' % html.escape(body.get("error") or str(code)))


class Listener:
    def __init__(self, port):
        self.port, self.srv, self.error, self.tried = port, None, None, 0.0

    def start(self, d):
        self.tried = time.time()
        try:
            self.srv = Server((d.host, self.port), handler(d, self.port))
        except OSError as e:
            self.error = "порт %d: %s" % (self.port, e.strerror or e)
            return False
        self.error = None
        threading.Thread(target=self.srv.serve_forever, kwargs={"poll_interval": 0.5},
                         name="ctl-%d" % self.port, daemon=True).start()
        return True

    def stop(self):
        if self.srv:
            self.srv.shutdown()
            self.srv.server_close()
            self.srv = None


Trigger = Listener              # прежнее имя


class Daemon:
    def __init__(self, host="0.0.0.0", log=None):
        self.host = host
        self.rows, self.ctl, self.next, self.running = {}, {}, {}, set()
        self.lock = threading.Lock()
        self.stop_ev, self.reload_ev = threading.Event(), threading.Event()
        self.log = log or journal
        self._state = None
        c = jload(P.checks, {})
        self.checks = c if isinstance(c, dict) else {}
        self.next_wan = time.time() + WAN_FIRST
        self.speed_day = None
        self.checking = False

    def load(self):
        doc = jload(P.applied, {})
        rows = {}
        for r in doc.get("proxies", []) if isinstance(doc, dict) else []:
            try:
                r = store.norm(r)
            except Fail as e:
                self.log("пропускаю строку: %s" % e)
                continue
            if r["enabled"]:
                rows[r["id"]] = r
        return rows

    def sync(self):
        rows = self.load()
        now = time.time()
        ports = {}
        for r in rows.values():
            for p in {r["reconnect_port"], r["reboot_port"]} - {0}:
                ports.setdefault(p, []).append(r["id"])
        gone = []
        with self.lock:
            for p in list(self.ctl):
                if p not in ports:
                    gone.append(self.ctl.pop(p))
            for rid in list(self.next):
                if rid not in rows or not rows[rid]["interval_min"]:
                    del self.next[rid]
            for rid, r in rows.items():
                old = self.rows.get(rid)
                if r["interval_min"] and (rid not in self.next or not old or old["interval_min"] != r["interval_min"]):
                    self.next[rid] = now + r["interval_min"] * 60
            self.rows = rows
            for p, ids in sorted(ports.items()):
                t = self.ctl.get(p)
                if t is None:
                    t = self.ctl[p] = Listener(p)
                elif t.srv:
                    continue
                who = "строка %d" % ids[0] if len(ids) == 1 else "строки %s" % ", ".join(map(str, sorted(ids)))
                if t.start(self):
                    self.log("%s: управление :%d (/reconnect, /reboot)" % (who, p))
                else:
                    self.log("%s: порт управления не поднят — %s; повторю через %d с" % (who, t.error, RETRY))
        for t in gone:                 # закрывать вне блокировки: shutdown ждёт цикл сервера до 0,5 с
            t.stop()
        self.save_state()

    def retry(self):
        now = time.time()
        with self.lock:
            for p, t in self.ctl.items():
                if t.srv is None and now - t.tried >= RETRY and t.start(self):
                    self.log("управление :%d (порт освободился)" % p)

    def resolve(self, port, action, token, login=None, pw=None):
        """Строка запроса: пароль/логин в пути → Basic → единственная строка порта. → (строка, код, ошибка)."""
        key = "reconnect_port" if action == "reconnect" else "reboot_port"
        with self.lock:
            cand = [r for r in self.rows.values() if r[key] == port]
        if not cand:
            return None, 404, "на этом порту нет %s" % ("реконнекта" if action == "reconnect" else "ребута")
        if token:
            hit = [r for r in cand if token in (r["password"], r["login"])]
            if len(hit) == 1:
                return hit[0], 200, None
            return None, 403, "в ссылке не тот пароль строки"
        if login is not None:
            hit = [r for r in cand if r["login"] == login and r["password"] == pw]
            if hit:
                return hit[0], 200, None
            return None, 403, "логин или пароль не те"
        if len(cand) == 1:
            return cand[0], 200, None
        return None, 401, "порт общий у %d строк — нужен пароль строки в ссылке: /%s/<пароль>" % (len(cand), action)

    def trigger(self, rid):
        """Прежний вход (реконнект строки по id) — для совместимости."""
        with self.lock:
            r = self.rows.get(rid)
        if not r:
            return 404, {"ok": False, "error": "строки нет"}
        return self.act(r, "reconnect", False)

    def act(self, r, action, want_old=False, how="link"):
        old = None
        if action == "reconnect" and want_old:
            try:                       # «было» для страницы — тем же путём, что прокси
                old = hilink.ext_ip(source=r["lan_ip"], timeout=4)
            except Fail:
                pass
        try:
            res = ops.reconnect(r, how) if action == "reconnect" else ops.reboot(r, how)
        except Busy as e:
            return 409, {"ok": False, "error": str(e)}
        except Fail as e:
            self.log("строка %d: %s (%s) не вышел — %s" % (r["id"], "реконнект" if action == "reconnect" else "ребут", how, e))
            return 502, {"ok": False, "error": str(e)}
        except Exception as e:       # демон не должен падать из-за одного модема
            self.log("строка %d: сбой — %r" % (r["id"], e))
            return 500, {"ok": False, "error": "сбой: %s" % e}
        finally:
            if action == "reconnect":
                with self.lock:
                    if r["id"] in self.next:
                        self.next[r["id"]] = time.time() + r["interval_min"] * 60
        out = {"ok": True}
        if res.get("ip"):
            out["ip"] = res["ip"]
            self.remember(r["id"], wan_ip=res["ip"], wan_t=int(time.time()), wan_error=None)
        if old:
            out["old"] = old
        return 200, out

    do = (lambda self, r, how: self.act(r, "reconnect", False, how))     # прежний вход

    def tick(self):
        now = time.time()
        with self.lock:
            for rid, at in list(self.next.items()):
                if at > now or rid in self.running:
                    continue
                r = self.rows.get(rid)
                self.next[rid] = now + r["interval_min"] * 60
                t = self.ctl.get(r["reconnect_port"]) if r["reconnect_port"] else None
                if r["reconnect_port"] and (not t or t.srv is None):
                    continue           # порт реконнекта у другого (старая панель) — он и обслуживает
                self.running.add(rid)
                threading.Thread(target=self._timer, args=(r,), name="timer-%d" % rid, daemon=True).start()
        if not self.checking:
            s = store.settings()
            lt = time.localtime(now)
            day = time.strftime("%Y-%m-%d", lt)
            wan = s["wan_check"] and now >= self.next_wan
            speed = s["speedtest"] and lt.tm_hour == SPEED_HOUR and self.speed_day != day
            if wan:
                self.next_wan = now + WAN_EVERY
            if speed:
                self.speed_day = day
            if wan or speed:
                self.checking = True
                threading.Thread(target=self._checks, args=(wan, speed), name="checks", daemon=True).start()

    def _timer(self, r):
        try:
            self.act(r, "reconnect", False, "timer")
        finally:
            with self.lock:
                self.running.discard(r["id"])

    def remember(self, rid, **kw):
        with self.lock:
            c = self.checks.setdefault(str(rid), {})
            c.update(kw)
            data = dict(self.checks)
        try:
            jsave(P.checks, data, 0o600)
        except OSError:
            pass

    def _checks(self, wan, speed):
        """Внешний IP через каждый прокси (тем путём, что клиент) и/или скорость — по одной строке."""
        try:
            with self.lock:
                rows = sorted(self.rows.values(), key=lambda r: r["id"])
            for r in rows:
                if self.stop_ev.is_set():
                    break
                if wan:
                    try:
                        ip = hilink.ext_ip(proxy=("127.0.0.1", r["port"], r["login"], r["password"]), timeout=10)
                        self.remember(r["id"], wan_ip=ip, wan_t=int(time.time()), wan_error=None)
                    except Fail as e:
                        self.remember(r["id"], wan_t=int(time.time()), wan_error=str(e))
                if speed:
                    try:
                        res = ops.speedtest(r, "daily")
                        self.remember(r["id"], speed=dict(res, t=int(time.time())))
                    except Fail as e:
                        self.remember(r["id"], speed={"error": str(e), "t": int(time.time())})
        finally:
            self.checking = False

    def save_state(self):
        with self.lock:
            st = {"pid": os.getpid(),
                  "triggers": {str(p): {"port": p, "ok": t.srv is not None, "error": t.error}
                               for p, t in self.ctl.items()},
                  "next": {str(rid): int(at) for rid, at in self.next.items()}}
        if st != self._state:
            try:
                jsave(P.state, st, 0o600)
                self._state = st
            except OSError:
                pass

    def run(self, signals=True):
        os.makedirs(P.run, mode=0o700, exist_ok=True)
        if signals:
            signal.signal(signal.SIGHUP, lambda *a: self.reload_ev.set())
            signal.signal(signal.SIGTERM, lambda *a: self.stop_ev.set())
            signal.signal(signal.SIGINT, lambda *a: self.stop_ev.set())
        self.log("демон modlink: старт")
        self.sync()
        while not self.stop_ev.is_set():
            if self.reload_ev.is_set():
                self.reload_ev.clear()
                self.log("перечитываю применённое")
                self.sync()
            self.retry()
            self.tick()
            self.save_state()
            self.stop_ev.wait(0.5)
        with self.lock:
            for t in self.ctl.values():
                t.stop()
            self.ctl.clear()
        try:
            os.unlink(P.state)
        except OSError:
            pass
        self.log("демон modlink: стоп")
        return 0
