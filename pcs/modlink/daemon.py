"""Демон modlink.service: триггеры реконнекта, автореконнект по таймеру, состояние для status.

Строки берёт из применённого (/var/lib/modlink/applied.json) — того же, из чего собран
конфиг sing-box: до `modlink apply` триггеры и прокси не расходятся. SIGHUP — перечитать.

Наружу открыты только триггеры: `GET http://<ip>:<reconnect_port>/reconnect` → JSON
{"ok": true, "ip": "…"} или {"ok": false, "error": "…"}. Больше ни одного пути и порта.
Пока порт триггера занят кем-то ещё (старая панель modlink-linux), таймер строки
молчит: строку обслуживает тот, у кого порт, — иначе реконнекты задвоятся.
"""
import json
import os
import signal
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from ..core.util import Busy, Fail, Log, jsave, jload
from . import ops, store
from .store import P

RETRY = 30                    # с, повторная попытка занять порт триггера


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def handler(d, rid):
    class H(BaseHTTPRequestHandler):
        timeout = 15
        server_version = "modlink"
        sys_version = ""

        def log_message(self, *a):
            pass

        def do_GET(self):
            if urllib.parse.urlsplit(self.path).path.rstrip("/") != "/reconnect":
                return self.reply(404, {"ok": False, "error": "есть только GET /reconnect"})
            self.reply(*d.trigger(rid))

        def reply(self, code, body):
            data = json.dumps(body, ensure_ascii=False).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)
    return H


class Trigger:
    def __init__(self, port):
        self.port, self.srv, self.error, self.tried = port, None, None, 0.0

    def start(self, d, rid):
        self.tried = time.time()
        try:
            self.srv = Server((d.host, self.port), handler(d, rid))
        except OSError as e:
            self.error = "порт %d: %s" % (self.port, e.strerror or e)
            return False
        self.error = None
        threading.Thread(target=self.srv.serve_forever, kwargs={"poll_interval": 0.5},
                         name="trigger-%d" % rid, daemon=True).start()
        return True

    def stop(self):
        if self.srv:
            self.srv.shutdown()
            self.srv.server_close()
            self.srv = None


class Daemon:
    def __init__(self, host="0.0.0.0", log=None):
        self.host = host
        self.rows, self.trig, self.next, self.running = {}, {}, {}, set()
        self.lock = threading.Lock()
        self.stop_ev, self.reload_ev = threading.Event(), threading.Event()
        self.log = log or Log("modlink", P.log)
        self._state = None

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
        with self.lock:
            for rid in list(self.trig):
                if rid not in rows or rows[rid]["reconnect_port"] != self.trig[rid].port:
                    self.trig.pop(rid).stop()
            for rid in list(self.next):
                if rid not in rows or not rows[rid]["interval_min"]:
                    del self.next[rid]
            for rid, r in rows.items():
                old = self.rows.get(rid)
                if r["interval_min"] and (rid not in self.next or not old or old["interval_min"] != r["interval_min"]):
                    self.next[rid] = now + r["interval_min"] * 60
            self.rows = rows
            for rid, r in sorted(rows.items()):
                t = self.trig.get(rid)
                if t is None:
                    t = self.trig[rid] = Trigger(r["reconnect_port"])
                elif t.srv:
                    continue
                if t.start(self, rid):
                    self.log("строка %d: триггер :%d/reconnect" % (rid, t.port))
                else:
                    self.log("строка %d: триггер не поднят — %s; повторю через %d с" % (rid, t.error, RETRY))
        self.save_state()

    def retry(self):
        now = time.time()
        with self.lock:
            for rid, t in self.trig.items():
                if t.srv is None and now - t.tried >= RETRY and t.start(self, rid):
                    self.log("строка %d: триггер :%d/reconnect (порт освободился)" % (rid, t.port))

    def trigger(self, rid):
        with self.lock:
            r = self.rows.get(rid)
        if not r:
            return 404, {"ok": False, "error": "строки нет"}
        return self.do(r, "trigger")

    def do(self, r, how):
        try:
            res = ops.reconnect(r, how)
        except Busy as e:
            return 409, {"ok": False, "error": str(e)}
        except Fail as e:
            self.log("строка %d: реконнект (%s) не вышел — %s" % (r["id"], how, e))
            return 502, {"ok": False, "error": str(e)}
        except Exception as e:       # демон не должен падать из-за одного модема
            self.log("строка %d: сбой реконнекта — %r" % (r["id"], e))
            return 500, {"ok": False, "error": "сбой: %s" % e}
        finally:
            with self.lock:
                if r["id"] in self.next:
                    self.next[r["id"]] = time.time() + r["interval_min"] * 60
        out = {"ok": True}
        if res.get("ip"):
            out["ip"] = res["ip"]
        return 200, out

    def tick(self):
        now = time.time()
        with self.lock:
            for rid, at in list(self.next.items()):
                if at > now or rid in self.running:
                    continue
                r, t = self.rows.get(rid), self.trig.get(rid)
                self.next[rid] = now + r["interval_min"] * 60
                if not t or t.srv is None:
                    continue
                self.running.add(rid)
                threading.Thread(target=self._timer, args=(r,), name="timer-%d" % rid, daemon=True).start()

    def _timer(self, r):
        try:
            self.do(r, "timer")
        finally:
            with self.lock:
                self.running.discard(r["id"])

    def save_state(self):
        with self.lock:
            st = {"pid": os.getpid(),
                  "triggers": {str(rid): {"port": t.port, "ok": t.srv is not None, "error": t.error}
                               for rid, t in self.trig.items()},
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
            for t in self.trig.values():
                t.stop()
            self.trig.clear()
        try:
            os.unlink(P.state)
        except OSError:
            pass
        self.log("демон modlink: стоп")
        return 0
