"""Веб-панель PCS: HTTP на порту 666 (docs/ARCHITECTURE.md §10).

    pcs web serve            → main([...])          (pcs-web.service)
    PCS_WEB_FAKE=1 python3 -c 'from pcs.web.server import main; main(["--port", "6660"])'

Вход — логин и пароль из /etc/pcs/web.json (auth.set_password), сессия — cookie
HttpOnly; SameSite=Strict, каждый POST — с заголовком X-CSRF из GET /api/session.
CORS нет. Терминал — WebSocket /ws/term?target=<id|host> ↔ pty (api.term_argv).
"""
import argparse
import getpass
import hmac
import http.server
import importlib
import json
import mimetypes
import os
import signal
import socket
import sys
import tempfile
import threading
import time
import traceback
import urllib.parse

from pcs import VERSION
from pcs.core.util import Fail
from pcs.web import auth, ws
from pcs.web.auth import WEB_JSON, set_password  # noqa: F401  (set_password зовёт `pcs web passwd`)
from pcs.web.panel import Bad, Panel, target

STATIC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
MAX_BODY = 4 << 20
MAX_TERMS = 24
LOG_DIR = "/var/log/pcs"
CSP = ("default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
       "connect-src 'self' ws: wss:; font-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
TYPES = {".js": "text/javascript; charset=utf-8", ".css": "text/css; charset=utf-8",
         ".html": "text/html; charset=utf-8", ".svg": "image/svg+xml", ".json": "application/json",
         ".txt": "text/plain; charset=utf-8", ".map": "application/json", "": "text/plain; charset=utf-8"}


class App:
    """Состояние панели: api, сессии, пауза входа, открытые терминалы."""

    def __init__(self, api, config=WEB_JSON, port=666, fake=False, fail_delay=1.0, throttle=None,
                 log_dir=LOG_DIR, quiet=False):
        self.api, self.config, self.port, self.fake = api, config, port, fake
        self.fail_delay, self.log_dir, self.quiet = fail_delay, log_dir, quiet
        self.sessions = auth.Sessions()
        self.throttle = throttle or auth.Throttle()
        self.panel = Panel(api, audit=lambda user, m, p: self.log("%s %s %s" % (user, m, p)))
        self.terms = {}
        self.tlock = threading.Lock()
        self.cookie = "pcs_sid_%d" % port      # куки не различают порты — у фейка и панели свои

    def log(self, msg):
        line = "%s %s" % (time.strftime("%F %T"), msg)
        if not self.quiet:
            print("pcs-web: " + msg, file=sys.stderr, flush=True)
        if self.log_dir:
            try:
                os.makedirs(self.log_dir, exist_ok=True)
                with open(os.path.join(self.log_dir, "web.log"), "a") as f:
                    f.write(line + "\n")
            except OSError:
                pass

    def close_terms(self):
        with self.tlock:
            terms = list(self.terms.values())
        for t in terms:
            t.close()


class Handler(http.server.BaseHTTPRequestHandler):
    server_version = "pcs-web"
    sys_version = ""
    protocol_version = "HTTP/1.1"
    timeout = 300

    @property
    def app(self):
        return self.server.app

    # ── ответы ──────────────────────────────────────────────────────────────

    def _send(self, code, body=b"", ctype="application/json", headers=()):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", CSP)
        for k, v in headers:
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, code, obj, headers=()):
        self._send(code, json.dumps(obj, ensure_ascii=False), "application/json; charset=utf-8",
                   (("Cache-Control", "no-store"),) + tuple(headers))

    def _redirect(self, where):
        self._send(302, b"", "text/plain", (("Location", where), ("Cache-Control", "no-store")))

    def log_request(self, code="-", size="-"):
        try:
            c = int(code)
        except (TypeError, ValueError):
            c = 0
        if c >= 400 and c not in (401, 404):
            self.app.log("%s %s %s → %s" % (self.client_address[0], self.command, self.path.split("?")[0], code))

    def log_error(self, fmt, *args):
        self.app.log("%s %s" % (self.client_address[0], fmt % args))

    # ── сессия и проверки ───────────────────────────────────────────────────

    def _session(self):
        raw = self.headers.get("Cookie") or ""
        sid = None
        for part in raw.split(";"):
            k, _, v = part.strip().partition("=")
            if k == self.app.cookie:
                sid = v
        return self.app.sessions.get(sid, auth.stamp(self.app.config)) if sid else None

    def _origin_ok(self, required=False):
        """Origin, если есть, — ровно наш адрес (Host). Без CORS чужие страницы сюда не ходят."""
        origin = self.headers.get("Origin")
        if not origin:
            return not required
        host = self.headers.get("Host") or ""
        u = urllib.parse.urlsplit(origin)
        return bool(host) and u.scheme in ("http", "https") and u.netloc.lower() == host.lower()

    def _csrf_ok(self, s):
        got = self.headers.get("X-CSRF") or ""
        return bool(got) and hmac.compare_digest(got.encode(), s["csrf"].encode())

    def _body(self):
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            raise Bad("Content-Length")
        if n > MAX_BODY:
            raise Bad("слишком большой запрос", 413)
        raw = self.rfile.read(n) if n else b""
        if not raw:
            return {}
        ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        if ctype != "application/json":
            raise Bad("нужен Content-Type: application/json", 415)
        try:
            obj = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            raise Bad("тело запроса — не JSON")
        if not isinstance(obj, dict):
            raise Bad("тело запроса — объект JSON")
        return obj

    # ── методы ──────────────────────────────────────────────────────────────

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        u = urllib.parse.urlsplit(self.path)
        path = u.path
        try:
            if path == "/ws/term":
                return self._ws_term(u)
            if path.startswith("/api/"):
                return self._api("GET", path, u.query)
            if path == "/login":
                if self._session():
                    return self._redirect("/")
                return self._static("login.html")
            if path in ("/", "/index.html"):
                if not self._session():
                    return self._redirect("/login")
                return self._static("index.html")
            if path.startswith("/static/"):
                return self._static(path[len("/static/"):])
            if path == "/favicon.ico":
                return self._static("favicon.svg")
            return self._send(404, "нет такой страницы", "text/plain; charset=utf-8")
        except (BrokenPipeError, ConnectionResetError):
            pass

    def do_POST(self):
        u = urllib.parse.urlsplit(self.path)
        try:
            if u.path == "/api/login":
                return self._login()
            if u.path.startswith("/api/"):
                return self._api("POST", u.path, u.query)
            return self._json(404, {"ok": False, "error": "нет такого адреса"})
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _api(self, method, path, query):
        s = self._session()
        if s is None:
            return self._json(401, {"ok": False, "error": "нужен вход", "data": {"auth": False}})
        try:
            if method == "POST":
                if not self._origin_ok():
                    return self._json(403, {"ok": False, "error": "чужой Origin"})
                if not self._csrf_ok(s):
                    return self._json(403, {"ok": False, "error": "нет или неверный X-CSRF — обновите страницу"})
                body = self._body()
            else:
                body = None
            if path == "/api/session" and method == "GET":
                return self._json(200, {"ok": True, "data": {
                    "auth": True, "user": s["user"], "csrf": s["csrf"], "version": VERSION,
                    "port": self.app.port, "fake": self.app.fake}})
            if path == "/api/logout" and method == "POST":
                self.app.sessions.drop(s["sid"])
                self.app.log("%s выход" % s["user"])
                return self._json(200, {"ok": True, "data": None}, self._cookie_hdr("", 0))
            q = {k: v[-1] for k, v in urllib.parse.parse_qs(query).items()}
            code, out = self.app.panel.handle(method, path, q, body, user=s["user"])
            return self._json(code, out)
        except Bad as e:
            return self._json(e.code, {"ok": False, "error": str(e)})
        except (BrokenPipeError, ConnectionResetError):
            raise
        except Exception as e:
            self.app.log("сбой %s %s: %s\n%s" % (method, path, e, traceback.format_exc()))
            return self._json(500, {"ok": False, "error": "сбой панели (%s) — подробности: journalctl -u pcs-web"
                                    % e.__class__.__name__})

    def _cookie_hdr(self, sid, max_age):
        return (("Set-Cookie", "%s=%s; Path=/; HttpOnly; SameSite=Strict; Max-Age=%d"
                 % (self.app.cookie, sid, max_age)),)

    def _login(self):
        app, ip = self.app, self.client_address[0]
        if not self._origin_ok() or not self.headers.get("X-CSRF"):
            return self._json(403, {"ok": False, "error": "вход только со страницы панели"})
        wait = app.throttle.wait(ip)
        if wait:
            return self._json(429, {"ok": False, "error": "много неудачных входов — подождите %d с" % wait,
                                    "data": {"wait": wait}}, (("Retry-After", str(wait)),))
        try:
            body = self._body()
        except Bad as e:
            return self._json(e.code, {"ok": False, "error": str(e)})
        user, pw = body.get("user"), body.get("password")
        if not isinstance(user, str) or not isinstance(pw, str) or len(user) > 200 or len(pw) > 1000:
            return self._json(400, {"ok": False, "error": "нужны логин и пароль"})
        if not auth.configured(app.config):
            return self._json(503, {"ok": False, "error": "вход не настроен: задайте пароль командой pcs web passwd"})
        if not auth.check(app.config, user, pw):
            app.throttle.failed(ip)
            app.log("%s неудачный вход «%s»" % (ip, user[:40]))
            time.sleep(app.fail_delay)
            return self._json(401, {"ok": False, "error": "неверный логин или пароль"})
        app.throttle.passed(ip)
        s = app.sessions.new(user, auth.stamp(app.config))
        app.log("%s вход %s" % (ip, user))
        return self._json(200, {"ok": True, "data": {"user": user, "csrf": s["csrf"]}},
                          self._cookie_hdr(s["sid"], int(app.sessions.ttl)))

    # ── статика ─────────────────────────────────────────────────────────────

    def _static(self, rel):
        rel = urllib.parse.unquote(rel)
        parts = rel.replace("\\", "/").split("/")
        if "\0" in rel or rel.startswith("/") or any(p in ("..", "") for p in parts) or parts[0].startswith("."):
            return self._send(404, "нет такого файла", "text/plain; charset=utf-8")
        root = os.path.realpath(STATIC)
        full = os.path.realpath(os.path.join(root, *parts))
        if not full.startswith(root + os.sep) or not os.path.isfile(full):
            return self._send(404, "нет такого файла", "text/plain; charset=utf-8")
        st = os.stat(full)
        etag = '"%x-%x"' % (int(st.st_mtime), st.st_size)
        hdr = [("Cache-Control", "no-cache"), ("ETag", etag)]
        if self.headers.get("If-None-Match") == etag:
            self.send_response(304)
            for k, v in hdr:
                self.send_header(k, v)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        ext = os.path.splitext(full)[1].lower()
        ctype = TYPES.get(ext) or mimetypes.guess_type(full)[0] or "application/octet-stream"
        with open(full, "rb") as f:
            data = f.read()
        self._send(200, data, ctype, hdr)

    # ── терминал ────────────────────────────────────────────────────────────

    def _ws_term(self, u):
        app = self.app
        s = self._session()
        if s is None:
            return self._send(401, "нужен вход", "text/plain; charset=utf-8")
        if not self._origin_ok(required=True):
            return self._send(403, "чужой Origin", "text/plain; charset=utf-8")
        h = self.headers
        if (h.get("Upgrade") or "").lower() != "websocket" or \
                "upgrade" not in [x.strip().lower() for x in (h.get("Connection") or "").split(",")]:
            return self._send(400, "нужен WebSocket", "text/plain; charset=utf-8")
        if (h.get("Sec-WebSocket-Version") or "").strip() != "13":
            return self._send(426, "нужен WebSocket 13", "text/plain; charset=utf-8",
                              (("Sec-WebSocket-Version", "13"),))
        key = h.get("Sec-WebSocket-Key") or ""
        if not ws.key_ok(key):
            return self._send(400, "Sec-WebSocket-Key", "text/plain; charset=utf-8")
        q = {k: v[-1] for k, v in urllib.parse.parse_qs(u.query).items()}
        try:
            t = target(q.get("target", ""))
            argv = app.api.term_argv(t)
            cols, rows = int(q.get("cols", 100)), int(q.get("rows", 30))
        except (Bad, Fail, ValueError) as e:
            return self._send(400, str(e), "text/plain; charset=utf-8")
        with app.tlock:
            busy = len(app.terms) >= MAX_TERMS
        if busy:
            return self._send(503, "открыто слишком много терминалов", "text/plain; charset=utf-8")
        self.send_response(101)
        self.send_header("Upgrade", "websocket")
        self.send_header("Connection", "Upgrade")
        self.send_header("Sec-WebSocket-Accept", ws.accept(key))
        self.end_headers()
        self.wfile.flush()
        self.close_connection = True
        term = ws.Term(argv, cols, rows)
        with app.tlock:
            app.terms[term.pid] = term
        app.log("%s терминал %s открыт (pid %d)" % (s["user"], t, term.pid))
        try:
            why = ws.bridge(self.connection, term)
        finally:
            with app.tlock:
                app.terms.pop(term.pid, None)
        app.log("%s терминал %s закрыт: %s" % (s["user"], t, why))


class Server(http.server.ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, addr, app):
        if ":" in addr[0]:
            self.address_family = socket.AF_INET6
        self.app = app
        super().__init__(addr, Handler)


def make_server(port=666, api=None, bind="0.0.0.0", config=WEB_JSON, **kw):
    if api is None:
        api = importlib.import_module("pcs.hub.api")
    return Server((bind, port), App(api, config=config, port=port, **kw))


def serve(port=666, api=None, bind="0.0.0.0", config=WEB_JSON, **kw):
    httpd = make_server(port, api, bind, config, **kw)

    def stop(*_):
        raise SystemExit(0)
    signal.signal(signal.SIGTERM, stop)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.app.close_terms()
        httpd.server_close()


def _passwd(argv, config):
    p = argparse.ArgumentParser(prog="pcs-web passwd")
    p.add_argument("--user", default=None)
    a = p.parse_args(argv)
    user = a.user or input("  логин панели [admin]: ").strip() or "admin"
    pw = getpass.getpass("  пароль: ")
    if pw != getpass.getpass("  ещё раз: "):
        raise Fail("пароли не совпали")
    set_password(config, user, pw)
    print("  ✓ сохранено в %s" % config)


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    p = argparse.ArgumentParser(prog="pcs-web", description="веб-панель PCS (порт 666)")
    p.add_argument("cmd", nargs="?", choices=("serve", "passwd"), default="serve")
    p.add_argument("--port", type=int, default=int(os.environ.get("PCS_WEB_PORT") or 666))
    p.add_argument("--bind", default=None, help="адрес (по умолчанию 0.0.0.0; у фейка 127.0.0.1)")
    p.add_argument("--config", default=os.environ.get("PCS_WEB_CONFIG"))
    p.add_argument("--fake", action="store_true", default=os.environ.get("PCS_WEB_FAKE") == "1",
                   help="фейковые данные вместо хаба (для разработки)")
    p.add_argument("--user", default=None)
    a = p.parse_args(argv)
    try:
        if a.cmd == "passwd":
            _passwd(["--user", a.user] if a.user else [], a.config or WEB_JSON)
            return 0
        if a.fake:
            from pcs.web import fake_api as api
            config = a.config or os.path.join(tempfile.gettempdir(), "pcs-web-fake.json")
            if not auth.configured(config):
                set_password(config, "admin", "pcs-fake")
            bind = a.bind or "127.0.0.1"
            print("  панель с фейковыми данными: http://%s:%d   вход: %s / pcs-fake"
                  % (bind if bind not in ("0.0.0.0", "::") else "127.0.0.1", a.port, auth.load(config).get("user")),
                  flush=True)
            log_dir = None
        else:
            try:
                api = importlib.import_module("pcs.hub.api")
            except ImportError as e:
                raise Fail("нет pcs.hub.api (%s) — хаб не установлен; для пробы: PCS_WEB_FAKE=1" % e)
            config, bind, log_dir = a.config or WEB_JSON, a.bind or "0.0.0.0", LOG_DIR
            if not auth.configured(config):
                print("  ⚠ пароль панели не задан — вход закрыт: pcs web passwd", file=sys.stderr, flush=True)
        try:
            serve(a.port, api, bind, config, fake=a.fake, log_dir=log_dir)
        except OSError as e:
            raise Fail("не открыть порт %s:%d: %s" % (bind, a.port, e.strerror or e))
        return 0
    except Fail as e:
        print("  ✗ %s" % e, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
