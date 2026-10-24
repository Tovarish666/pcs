"""Телепорт к присоединённому хосту (PCS.md §19.4): /t/<имя>/… на хабе — это панель того хоста.

Хосты не влияют друг на друга: каждый живёт сам по себе, со своим кодом и своей версией.
Хаб — только окно: страницы, команды, терминал и окна браузера того хоста идут к нему как есть,
по HTTPS с его отпечатком сертификата. Выполняет всё сам тот хост — хаб ничего за него не делает
и не понимает его запросов.

Сессия на том хосте — одноразовой ссылкой входа (/api/v1/login-link по токену хаба, как
«Его панель в новой вкладке»): своя на каждую сессию хаба, живёт у хаба в памяти; кончилась —
берётся новая, незаметно. Куки того хоста в браузер не уходят.
"""
import http.client
import json
import socket
import ssl
import threading

from pcs.core.util import Fail
from pcs.web import tls

# что из запроса браузера идёт тому хосту; Cookie, Host и Origin — свои (того хоста)
FWD = ("content-type", "x-csrf", "accept", "accept-language", "user-agent", "if-none-match",
       "if-modified-since", "cache-control", "pragma")
# что из ответа того хоста не отдаём: его куки, служебное соединения (длину посчитаем сами)
DROP = ("set-cookie", "connection", "keep-alive", "transfer-encoding", "content-length", "content-encoding")


def hostport(rec):
    host = "[%s]" % rec["host"] if ":" in rec["host"] else rec["host"]
    return "%s:%d" % (host, rec["port"])


class Teleporter:
    def __init__(self, resolve, timeout=120):
        self.resolve = resolve          # имя → запись хоста {name, host, port, fingerprint, token}
        self.timeout = timeout
        self.cookies = {}               # (сессия хаба, имя) → «pcs_sid_666=…» того хоста
        self.lock = threading.Lock()

    def _conn(self, rec, timeout):
        c = http.client.HTTPSConnection(rec["host"], rec["port"], context=tls.client_context(), timeout=timeout)
        try:
            c.connect()
        except (OSError, ssl.SSLError) as e:
            raise Fail("хост %s (%s) не отвечает по HTTPS: %s" % (rec["name"], hostport(rec), getattr(e, "strerror", None) or e))
        if tls.fingerprint_der(c.sock.getpeercert(binary_form=True)) != rec.get("fingerprint"):
            c.close()
            raise Fail("сертификат хоста %s не тот, что при присоединении — это может быть подмена" % rec["name"])
        return c

    def _login(self, rec):
        """Одноразовая ссылка входа по токену хаба → куки сессии того хоста."""
        c = self._conn(rec, 20)
        try:
            c.request("POST", "/api/v1/login-link", body=b"{}", headers={
                "Authorization": "Bearer " + (rec.get("token") or ""), "Content-Type": "application/json"})
            r = c.getresponse()
            try:
                obj = json.loads(r.read().decode("utf-8") or "{}")
            except ValueError:
                obj = {}
        except (OSError, http.client.HTTPException) as e:
            raise Fail("хост %s: связь оборвалась (%s)" % (rec["name"], e))
        finally:
            c.close()
        if not obj.get("ok"):
            raise Fail("хост %s не впустил: %s" % (rec["name"], obj.get("error") or "HTTP %d" % r.status))
        c = self._conn(rec, 20)
        try:
            c.request("GET", obj["data"]["path"], headers={"Host": hostport(rec)})
            r = c.getresponse()
            r.read()
            for k, v in r.getheaders():
                if k.lower() == "set-cookie":
                    pair = v.split(";", 1)[0].strip()
                    if "=" in pair and pair.split("=", 1)[1]:
                        return pair
        except (OSError, http.client.HTTPException) as e:
            raise Fail("хост %s: связь оборвалась (%s)" % (rec["name"], e))
        finally:
            c.close()
        raise Fail("хост %s не дал сессию — обнови на нём PCS" % rec["name"])

    def cookie(self, sid, rec, fresh=False):
        key = (sid, rec["name"])
        with self.lock:
            have = self.cookies.get(key)
        if have and not fresh:
            return have
        pair = self._login(rec)
        with self.lock:
            self.cookies[key] = pair
        return pair

    def forget(self, alive):
        """Сессии того хоста, чьи сессии хаба кончились, — забыть. alive(sid) → bool."""
        with self.lock:
            for k in [k for k in self.cookies if not alive(k[0])]:
                self.cookies.pop(k, None)

    def request(self, sid, name, method, path, headers, body=b""):
        """Запрос браузера → тот хост. → (код, [(заголовок, значение)], тело)."""
        rec = self.resolve(name)
        sent_origin = any(k.lower() == "origin" for k, _ in headers)
        for attempt in (1, 2):
            pair = self.cookie(sid, rec, fresh=attempt == 2)
            h = {k: v for k, v in headers if k.lower() in FWD}
            h.update({"Host": hostport(rec), "Cookie": pair})
            if sent_origin:
                h["Origin"] = "https://" + hostport(rec)
            c = self._conn(rec, self.timeout)
            try:
                c.request(method, path, body=body or None, headers=h)
                r = c.getresponse()
                data = r.read()
            except (OSError, http.client.HTTPException) as e:
                raise Fail("хост %s: связь оборвалась (%s)" % (name, e))
            finally:
                c.close()
            loc = (r.getheader("Location") or "").split("?")[0]
            if attempt == 1 and (r.status == 401 or (r.status in (302, 303) and loc.endswith("/login"))):
                continue                # сессия того хоста кончилась — новая, и ещё раз
            return r.status, [(k, v) for k, v in r.getheaders() if k.lower() not in DROP], data
        return r.status, [(k, v) for k, v in r.getheaders() if k.lower() not in DROP], data

    def websocket(self, sid, name, path, key, version="13", protocol=None):
        """WebSocket (терминал, окно браузера) того хоста. → (сокет, заголовки ответа 101 + хвост).
        Не 101 — Fail с текстом ответа."""
        rec = self.resolve(name)
        for attempt in (1, 2):
            pair = self.cookie(sid, rec, fresh=attempt == 2)
            try:
                raw = socket.create_connection((rec["host"], rec["port"]), timeout=20)
                sock = tls.client_context().wrap_socket(raw, server_hostname=None)
            except (OSError, ssl.SSLError) as e:
                raise Fail("хост %s не отвечает по HTTPS: %s" % (name, e))
            if tls.fingerprint_der(sock.getpeercert(binary_form=True)) != rec.get("fingerprint"):
                sock.close()
                raise Fail("сертификат хоста %s не тот, что при присоединении — это может быть подмена" % name)
            req = ("GET %s HTTP/1.1\r\nHost: %s\r\nOrigin: https://%s\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                   "Sec-WebSocket-Key: %s\r\nSec-WebSocket-Version: %s\r\nCookie: %s\r\n"
                   % (path, hostport(rec), hostport(rec), key, version, pair))
            if protocol:
                req += "Sec-WebSocket-Protocol: %s\r\n" % protocol
            sock.sendall((req + "\r\n").encode())
            buf = b""
            while b"\r\n\r\n" not in buf:
                chunk = sock.recv(4096)
                if not chunk:
                    break
                buf += chunk
                if len(buf) > 65536:
                    break
            head = buf.split(b"\r\n\r\n", 1)[0]
            status = head.split(b"\r\n", 1)[0].decode("latin-1")
            if " 101 " in status + " ":
                sock.settimeout(None)
                return sock, buf
            sock.close()
            if attempt == 1 and " 401 " in status + " ":
                continue
            raise Fail("хост %s не открыл соединение: %s" % (name, status or "нет ответа"))
