"""Веб-панель PCS: HTTP и HTTPS на одном порту 666 (docs/ARCHITECTURE.md §10).

    pcs web serve            → main([...])          (pcs-web.service)
    PCS_WEB_FAKE=1 python3 -c 'from pcs.web.server import main; main(["--port", "6660"])'

Вход — логин и пароль из /etc/pcs/web.json (auth.set_password), сессия — cookie
HttpOnly; SameSite=Strict, каждый POST — с заголовком X-CSRF из GET /api/session.
CORS нет. Терминал — WebSocket /ws/term?target=<id|host> ↔ pty (api.term_argv).

Связь хостов (PCS.md §19): тот же порт отвечает по TLS (свой сертификат, pcs/web/tls.py).
  POST /api/v1/join {user, password, name}   — логин и пароль панели → токен (только HTTPS)
  /api/v1/<путь> с Authorization: Bearer     — те же адреса, что /api/<путь> у браузера
  POST /api/v1/login-link                     — одноразовая ссылка входа (хаб открывает панель)
  POST /api/v1/leave                          — отозвать свой токен (хаб отсоединяется)

Телепорт (PCS.md §19.4): /t/<имя>/… — панель присоединённого хоста через хаб, как есть
(страницы, API, терминал, окна браузера). Выполняет всё тот хост; хаб — только окно (teleport.py).
"""
import argparse
import getpass
import hmac
import http.server
import importlib
import json
import mimetypes
import re
import os
import secrets
import signal
import socket
import ssl
import sys
import tempfile
import threading
import time
import traceback
import urllib.parse

from pcs import VERSION
from pcs.core.util import Fail
from pcs.web import auth, teleport, tls, tokens, ws
from pcs.web.auth import WEB_JSON, set_password  # noqa: F401  (set_password зовёт `pcs web passwd`)
from pcs.web.panel import Bad, Panel, target

STATIC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
MAX_BODY = 4 << 20
MAX_TERMS = 24
LOG_DIR = "/var/log/pcs"
CSP = ("default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
       "connect-src 'self' ws: wss:; font-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
OV_CACHE = "/var/lib/pcs/web-overview.json"     # последний обзор: панель после перезапуска показывает его сразу
TYPES = {".woff2": "font/woff2", ".js": "text/javascript; charset=utf-8", ".css": "text/css; charset=utf-8",
         ".html": "text/html; charset=utf-8", ".svg": "image/svg+xml", ".json": "application/json",
         ".txt": "text/plain; charset=utf-8", ".map": "application/json", "": "text/plain; charset=utf-8"}


class App:
    """Состояние панели: api, сессии, пауза входа, открытые терминалы."""

    def __init__(self, api, config=WEB_JSON, port=666, fake=False, fail_delay=1.0, throttle=None,
                 log_dir=LOG_DIR, quiet=False, tokens_path=None):
        self.api, self.config, self.port, self.fake = api, config, port, fake
        self.fail_delay, self.log_dir, self.quiet = fail_delay, log_dir, quiet
        self.sessions = auth.Sessions()
        self.throttle = throttle or auth.Throttle()
        if fake:
            from . import fake_api
            br = fake_api.FakeBrowser()
        elif callable(getattr(api, "browser_install", None)):
            from . import browser
            br = browser.Manager(jobs_start=api.browser_install)
        else:
            br = None
        self.panel = Panel(api, audit=lambda user, m, p: self.log("%s %s %s" % (user, m, p)),
                           ov_path=None if fake else OV_CACHE, browser=br)
        self.terms = {}
        self.tlock = threading.Lock()
        self.cookie = "pcs_sid_%d" % port      # куки не различают порты — у фейка и панели свои
        self.ott = {}                          # одноразовые ссылки входа: токен → (до, логин)
        self.fingerprint = ""                  # отпечаток своего сертификата (если HTTPS включён)
        self.tokens_path = tokens_path or tokens.PATH
        self.teleport = teleport.Teleporter(_node_rec)

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

    def parse_request(self):
        self._body_done = False
        return super().parse_request()

    # ── ответы ──────────────────────────────────────────────────────────────

    def _send(self, code, body=b"", ctype="application/json", headers=(), frame=False):
        """frame — можно показать в рамке своей же страницы (страница хоста внутри хаба)."""
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        if not getattr(self, "_body_done", True) and (self.headers.get("Content-Length") or "0").strip() not in ("", "0"):
            # Отказ до чтения тела (403, 413): тело осталось в сокете и стало бы «следующим запросом» (501) —
            # закрываем. Но сначала дочитываем (если не огромное): закрыть сокет с непрочитанным — это сброс
            # (RST), и по TLS клиент не успевает прочесть сам ответ (так терялся 403 в телепорте).
            try:
                n = int(self.headers.get("Content-Length") or 0)
                if 0 < n <= MAX_BODY:
                    self.rfile.read(n)
            except (ValueError, OSError):
                pass
            self._body_done = True
            self.close_connection = True
            self.send_header("Connection", "close")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "SAMEORIGIN" if frame else "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", CSP.replace("frame-ancestors 'none'", "frame-ancestors 'self'") if frame else CSP)
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

    def _tls(self):
        return isinstance(self.connection, ssl.SSLSocket)

    def _bearer(self):
        h = self.headers.get("Authorization") or ""
        return h[7:].strip() if h[:7].lower() == "bearer " else ""

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
        self._body_done = True
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
            m = TELE.fullmatch(path)
            if m:
                return self._teleport(self.command, m.group(1), m.group(2) or "", u.query)
            if path == "/ws/term":
                return self._ws_term(u)
            if path == "/ws/browser":
                return self._ws_browser(u)
            if path == "/api/v1/ws/term":
                return self._v1_ws(u)
            m = re.fullmatch(r"/ws/node/([a-z0-9-]{1,40})/term", path)
            if m:
                return self._ws_node(u, m.group(1))
            if path.startswith("/api/v1/"):
                return self._v1("GET", path, u.query)
            if path.startswith("/api/"):
                return self._api("GET", path, u.query)
            if path == "/login":
                ott = urllib.parse.parse_qs(u.query).get("ott", [""])[-1]
                if ott:
                    return self._login_ott(ott)
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
            m = TELE.fullmatch(u.path)
            if m:
                return self._teleport("POST", m.group(1), m.group(2) or "", u.query)
            if u.path == "/api/login":
                return self._login()
            if u.path.startswith("/api/v1/"):
                return self._v1("POST", u.path, u.query)
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

    # ── телепорт: панель присоединённого хоста через хаб ─────────────────────

    def _teleport(self, method, name, rest, query):
        """/t/<имя>/… → тот хост, как есть. Своя сессия хаба нужна; сессию на том хосте держит хаб."""
        app = self.app
        s = self._session()
        if s is None:
            if method != "POST" and not rest.startswith("/api/"):
                return self._redirect("/login")
            return self._json(401, {"ok": False, "error": "нужен вход", "data": {"auth": False}})
        if not rest:
            return self._redirect("/t/%s/" % name)
        if method == "POST" and not self._origin_ok():
            return self._json(403, {"ok": False, "error": "чужой Origin"})
        path = rest + ("?" + query if query else "")
        app.teleport.forget(lambda sid: app.sessions.get(sid, auth.stamp(app.config)) is not None)
        if (self.headers.get("Upgrade") or "").lower() == "websocket":
            return self._teleport_ws(s, name, path)
        body = b""
        if method == "POST":
            try:
                n = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                return self._json(400, {"ok": False, "error": "Content-Length"})
            if n > MAX_BODY:
                return self._json(413, {"ok": False, "error": "слишком большой запрос"})
            body = self.rfile.read(n) if n else b""
            self._body_done = True
        try:
            code, headers, data = app.teleport.request(s["sid"], name, method, path, list(self.headers.items()), body)
        except Fail as e:
            if rest.startswith("/api/") or method == "POST":
                return self._json(502, {"ok": False, "error": str(e)})
            return self._send(502, TELE_FAIL % (name, e), "text/html; charset=utf-8", frame=True)
        self.send_response(code)
        for k, v in headers:
            kl = k.lower()
            if kl in ("x-frame-options", "content-security-policy"):
                continue                        # свои — ниже: его панель показывается в рамке хаба
            if kl == "location" and v.startswith("/"):
                v = "/t/%s%s" % (name, v)       # его адреса — внутри телепорта
            self.send_header(k, v)
        # и на 304 тоже: браузер хранит заголовки закэшированной страницы и обновляет их из 304 —
        # без этого прежний запрет рамки (DENY) остался бы у него навсегда
        self.send_header("X-Frame-Options", "SAMEORIGIN")
        self.send_header("Content-Security-Policy", CSP.replace("frame-ancestors 'none'", "frame-ancestors 'self'"))
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        if method != "HEAD":
            self.wfile.write(data)

    def _teleport_ws(self, s, name, path):
        h = self.headers
        key = h.get("Sec-WebSocket-Key") or ""
        if not self._origin_ok(required=True):
            return self._send(403, "чужой Origin", "text/plain; charset=utf-8")
        if not ws.key_ok(key):
            return self._send(400, "нужен WebSocket 13", "text/plain; charset=utf-8")
        try:
            far, head = self.app.teleport.websocket(s["sid"], name, path, key, h.get("Sec-WebSocket-Version") or "13",
                                                    h.get("Sec-WebSocket-Protocol"))
        except Fail as e:
            return self._send(502, str(e), "text/plain; charset=utf-8")
        self.close_connection = True
        try:
            self.wfile.write(head)               # его «101 Switching Protocols» и что пришло следом
            self.wfile.flush()
            _relay(self.connection, far)
        finally:
            far.close()

    # ── связь хостов: /api/v1 по токену, только по HTTPS ────────────────────

    def _v1(self, method, path, query):
        app, ip = self.app, self.client_address[0]
        if not self._tls():
            return self._json(403, {"ok": False, "error": "связь хостов — только по HTTPS: https://<адрес>:%d" % app.port})
        if self.headers.get("Origin"):
            return self._json(403, {"ok": False, "error": "/api/v1 — для программ, не для страниц"})
        try:
            if path == "/api/v1/join" and method == "POST":
                return self._join()
            wait = app.throttle.wait(ip)
            if wait:
                return self._json(429, {"ok": False, "error": "много неудачных попыток — подождите %d с" % wait})
            rec = tokens.check(self._bearer(), ip, app.tokens_path)
            if rec is None:
                app.throttle.failed(ip)
                return self._json(401, {"ok": False, "error": "нет или неверный токен — присоедините хост заново"})
            body = self._body() if method == "POST" else None
            who = "api:%s" % rec.get("name", rec.get("id"))
            if path == "/api/v1/leave" and method == "POST":
                tokens.revoke(rec["id"], app.tokens_path)
                app.log("%s %s отсоединился (токен %s отозван)" % (ip, who, rec["id"]))
                return self._json(200, {"ok": True, "data": {"revoked": rec["id"]}})
            if path == "/api/v1/login-link" and method == "POST":
                user = auth.load(app.config).get("user")
                if not user:
                    return self._json(200, {"ok": False, "error": "вход в панель не настроен: pcs web passwd"})
                ott = secrets.token_urlsafe(24)
                now = time.time()
                app.ott = {k: v for k, v in app.ott.items() if v[0] > now}
                app.ott[ott] = (now + 60, user)
                app.log("%s %s: одноразовая ссылка входа" % (ip, who))
                return self._json(200, {"ok": True, "data": {"path": "/login?ott=" + ott, "ttl": 60}})
            q = {k: v[-1] for k, v in urllib.parse.parse_qs(query).items()}
            code, out = app.panel.handle(method, "/api/" + path[len("/api/v1/"):], q, body, user=who)
            return self._json(code, out)
        except Bad as e:
            return self._json(e.code, {"ok": False, "error": str(e)})
        except (BrokenPipeError, ConnectionResetError):
            raise
        except Exception as e:
            self.app.log("сбой %s %s: %s\n%s" % (method, path, e, traceback.format_exc()))
            return self._json(500, {"ok": False, "error": "сбой панели (%s)" % e.__class__.__name__})

    def _join(self):
        """Хаб другого хоста присоединяет этот: логин и пароль панели → токен (пароль не хранится)."""
        app, ip = self.app, self.client_address[0]
        wait = app.throttle.wait(ip)
        if wait:
            return self._json(429, {"ok": False, "error": "много неудачных попыток — подождите %d с" % wait})
        body = self._body()
        user, pw, name = body.get("user"), body.get("password"), str(body.get("name") or "хаб")[:60]
        if not isinstance(user, str) or not isinstance(pw, str) or len(user) > 200 or len(pw) > 1000:
            return self._json(400, {"ok": False, "error": "нужны логин и пароль панели этого хоста"})
        if not auth.configured(app.config):
            return self._json(503, {"ok": False, "error": "вход в панель здесь не настроен: pcs web passwd"})
        if not auth.check(app.config, user, pw):
            app.throttle.failed(ip)
            app.log("%s присоединение «%s»: неверный логин или пароль" % (ip, name))
            time.sleep(app.fail_delay)
            return self._json(401, {"ok": False, "error": "неверный логин или пароль панели"})
        app.throttle.passed(ip)
        tid, token = tokens.issue("хаб " + name, ip, app.tokens_path)
        app.log("%s присоединён к хабу «%s» (токен %s)" % (ip, name, tid))
        try:
            info = app.api.host_info()
        except Fail as e:
            info = {"error": str(e)}
        return self._json(200, {"ok": True, "data": {"token": token, "token_id": tid, "version": VERSION,
                                                     "fingerprint": app.fingerprint, "host": info}})

    def _login_ott(self, ott):
        """Вход по одноразовой ссылке от хаба: 60 с, один раз."""
        app = self.app
        rec = app.ott.pop(ott, None)
        if not rec or rec[0] < time.time():
            return self._redirect("/login")
        s = app.sessions.new(rec[1], auth.stamp(app.config))
        app.log("%s вход по ссылке хаба (%s)" % (self.client_address[0], rec[1]))
        return self._send(302, b"", "text/plain", (("Location", "/"), ("Cache-Control", "no-store"))
                          + self._cookie_hdr(s["sid"], int(app.sessions.ttl)))

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

    def _v1_ws(self, u):
        """Терминал по токену — для хаба другого хоста (только по HTTPS)."""
        app, ip = self.app, self.client_address[0]
        if not self._tls():
            return self._send(403, "только по HTTPS", "text/plain; charset=utf-8")
        rec = tokens.check(self._bearer(), ip, app.tokens_path)
        if rec is None:
            app.throttle.failed(ip)
            return self._send(401, "нет или неверный токен", "text/plain; charset=utf-8")
        return self._ws_term(u, user="api:%s" % rec.get("name", rec.get("id")))

    def _ws_node(self, u, name):
        """Терминал присоединённого хоста в браузере хаба: хаб — посредник, кадры не трогает."""
        app = self.app
        s = self._session()
        if s is None:
            return self._send(401, "нужен вход", "text/plain; charset=utf-8")
        if not self._origin_ok(required=True):
            return self._send(403, "чужой Origin", "text/plain; charset=utf-8")
        h = self.headers
        key = h.get("Sec-WebSocket-Key") or ""
        if (h.get("Upgrade") or "").lower() != "websocket" or not ws.key_ok(key) \
                or (h.get("Sec-WebSocket-Version") or "").strip() != "13":
            return self._send(400, "нужен WebSocket 13", "text/plain; charset=utf-8")
        fn = getattr(app.api, "node_ws", None)
        if fn is None:
            return self._send(501, "этот хаб не умеет терминал присоединённых хостов", "text/plain; charset=utf-8")
        try:
            far, rest = fn(name, u.query)
        except Fail as e:
            return self._send(502, str(e), "text/plain; charset=utf-8")
        self.send_response(101)
        self.send_header("Upgrade", "websocket")
        self.send_header("Connection", "Upgrade")
        self.send_header("Sec-WebSocket-Accept", ws.accept(key))
        self.end_headers()
        self.wfile.flush()
        self.close_connection = True
        near = self.connection
        near.settimeout(None)
        app.log("%s терминал хоста %s открыт" % (s["user"], name))
        try:
            if rest:
                near.sendall(rest)
            _relay(near, far)
        finally:
            for x in (far,):
                try:
                    x.close()
                except OSError:
                    pass
        app.log("%s терминал хоста %s закрыт" % (s["user"], name))

    def _ws_term(self, u, user=None):
        app = self.app
        s = {"user": user} if user else self._session()
        if s is None:
            return self._send(401, "нужен вход", "text/plain; charset=utf-8")
        if not user and not self._origin_ok(required=True):
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
            if q.get("console") == "serial":
                if t == "host":
                    raise Bad("консоль через Proxmox — только у ВМ")
                argv = app.api.term_argv(t, serial=True)
            else:
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


    def _ws_browser(self, u):
        """Окно браузера вкладок mp.space и ЮMoney: noVNC ↔ unix-сокет Xtigervnc этого сеанса."""
        app = self.app
        s = self._session()
        if s is None:
            return self._send(401, "нужен вход", "text/plain; charset=utf-8")
        if not self._origin_ok(required=True):
            return self._send(403, "чужой Origin", "text/plain; charset=utf-8")
        h = self.headers
        key = h.get("Sec-WebSocket-Key") or ""
        if (h.get("Upgrade") or "").lower() != "websocket" or not ws.key_ok(key) \
                or (h.get("Sec-WebSocket-Version") or "").strip() != "13":
            return self._send(400, "нужен WebSocket 13", "text/plain; charset=utf-8")
        br = app.panel.browser
        q = {k: v[-1] for k, v in urllib.parse.parse_qs(u.query).items()}
        try:
            if br is None:
                raise Fail("браузер здесь недоступен")
            k = br.key(q.get("kind", ""), q.get("target", ""), q.get("node") or None)
            path = br.vnc_socket(k)
            if not path:
                raise Fail("браузер не запущен")
            if isinstance(path, tuple):         # фейк: проброшенный VNC по TCP
                far = socket.create_connection(path, timeout=10)
                far.settimeout(None)
            else:
                far = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                far.connect(path)
        except (Fail, OSError) as e:
            return self._send(409, str(e), "text/plain; charset=utf-8")
        self.send_response(101)
        self.send_header("Upgrade", "websocket")
        self.send_header("Connection", "Upgrade")
        self.send_header("Sec-WebSocket-Accept", ws.accept(key))
        self.end_headers()
        self.wfile.flush()
        self.close_connection = True
        br.seen(k, +1)
        app.log("%s браузер %s: окно открыто" % (s["user"], k))
        try:
            why = ws.bridge_socket(self.connection, far)
        finally:
            br.seen(k, -1)
            far.close()
        app.log("%s браузер %s: окно закрыто (%s)" % (s["user"], k, why))


TELE = re.compile(r"/t/([a-z0-9-]{1,40})(/.*)?")
TELE_FAIL = ("<!doctype html><meta charset=utf-8><title>Хост не отвечает</title><body style='font:15px system-ui;padding:40px;"
             "background:#0b0d11;color:#e6e8ee'><h2>Панель хоста «%s» не открылась</h2><p>%s</p>"
             "<p style='color:#8a92a2'>Хост выключен, нет сети до него или на нём старый PCS. Другие хосты это не задевает.</p>")


def _node_rec(name):
    from pcs.hub import nodes
    return nodes.load(name)


def _relay(a, b, chunk=65536):
    """Перекладывать байты a↔b, пока одна сторона не закроет (кадры WebSocket — как есть)."""
    def pump(src, dst):
        try:
            while True:
                data = src.recv(chunk)
                if not data:
                    break
                dst.sendall(data)
        except OSError:
            pass
        for x in (src, dst):
            try:
                x.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
    t = threading.Thread(target=pump, args=(b, a), daemon=True)
    t.start()
    pump(a, b)
    t.join(timeout=5)


class Server(http.server.ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, addr, app, ssl_ctx=None):
        if ":" in addr[0]:
            self.address_family = socket.AF_INET6
        self.app = app
        self.ssl_ctx = ssl_ctx
        super().__init__(addr, Handler)

    def finish_request(self, request, client_address):
        """HTTP и HTTPS на одном порту: первый байт TLS-приветствия — 0x16."""
        if self.ssl_ctx is not None:
            try:
                request.settimeout(15)
                first = request.recv(1, socket.MSG_PEEK)
            except OSError:
                return
            if first == b"\x16":
                try:
                    tls_sock = self.ssl_ctx.wrap_socket(request, server_side=True)
                except (ssl.SSLError, OSError):
                    return
                try:
                    return super().finish_request(tls_sock, client_address)
                finally:
                    try:
                        tls_sock.close()
                    except OSError:
                        pass
            request.settimeout(None)
        return super().finish_request(request, client_address)

    def handle_error(self, request, client_address):
        """Клиент оборвал соединение (закрыл вкладку, моргнула сеть) — не ошибка, без трассы в журнале."""
        if isinstance(sys.exc_info()[1], (ConnectionResetError, BrokenPipeError, TimeoutError)):
            return
        super().handle_error(request, client_address)


def make_server(port=666, api=None, bind="0.0.0.0", config=WEB_JSON, tls_dir=None, **kw):
    """tls_dir — где свой сертификат (None — без HTTPS, как в старых тестах; "" — по умолчанию /etc/pcs/tls)."""
    if api is None:
        api = importlib.import_module("pcs.hub.api")
    app = App(api, config=config, port=port, **kw)
    ctx = None
    if tls_dir is not None:
        try:
            ctx = tls.server_context(tls_dir or None)
            app.fingerprint = tls.fingerprint_file(tls.paths(tls_dir or None)[0])
        except (Fail, OSError, ssl.SSLError) as e:
            app.log("HTTPS выключен: %s" % e)
    return Server((bind, port), app, ctx)


def serve(port=666, api=None, bind="0.0.0.0", config=WEB_JSON, **kw):
    kw.setdefault("tls_dir", "")
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
            extra = {}
            if a.fake:            # у фейка свой сертификат и токены — во временном каталоге
                extra = {"tls_dir": os.path.join(tempfile.gettempdir(), "pcs-web-fake-tls"),
                         "tokens_path": os.path.join(tempfile.gettempdir(), "pcs-web-fake-tokens-%d.json" % a.port)}
            serve(a.port, api, bind, config, fake=a.fake, log_dir=log_dir, **extra)
        except OSError as e:
            raise Fail("не открыть порт %s:%d: %s" % (bind, a.port, e.strerror or e))
        return 0
    except Fail as e:
        print("  ✗ %s" % e, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
