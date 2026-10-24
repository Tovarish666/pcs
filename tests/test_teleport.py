#!/usr/bin/env python3
"""Телепорт (pcs/web/teleport.py, PCS.md §19.4): две настоящие панели — хаб и присоединённый хост
(HTTPS со своим сертификатом), присоединение штатное (nodes.join), дальше всё — через /t/<имя>/….

    python3 tests/test_teleport.py
"""
import base64
import http.client
import json
import os
import secrets
import shutil
import socket
import sys
import tempfile
import threading

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, os.pardir))
from pcs.hub import nodes, store  # noqa: E402
from pcs.web import fake_api, server  # noqa: E402
from pcs.web.auth import set_password  # noqa: E402

passed = failed = 0


def check(name, cond, detail=""):
    global passed, failed
    if cond:
        passed += 1
        print("  ok   %s" % name)
    else:
        failed += 1
        print("  FAIL %s%s" % (name, ("\n       " + str(detail)) if detail else ""))


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


TMP = tempfile.mkdtemp(prefix="pcs-teleport-")
store.ETC = os.path.join(TMP, "hub-etc")
NODE_PORT, HUB_PORT = free_port(), free_port()
node_cfg, hub_cfg = os.path.join(TMP, "node.json"), os.path.join(TMP, "hub.json")
set_password(node_cfg, "admin", "node-pass-1")
set_password(hub_cfg, "admin", "hub-pass-1")
node = server.make_server(NODE_PORT, fake_api, "127.0.0.1", node_cfg, tls_dir=os.path.join(TMP, "node-tls"),
                          fake=True, quiet=True, log_dir=None, fail_delay=0,
                          tokens_path=os.path.join(TMP, "node-tokens.json"))
hub = server.make_server(HUB_PORT, fake_api, "127.0.0.1", hub_cfg, tls_dir=None, fake=True, quiet=True, log_dir=None,
                         fail_delay=0)
for srv in (node, hub):
    threading.Thread(target=srv.serve_forever, daemon=True).start()

ORIGIN = "http://127.0.0.1:%d" % HUB_PORT


def req(method, path, body=None, headers=None, cookie=None):
    c = http.client.HTTPConnection("127.0.0.1", HUB_PORT, timeout=20)
    h = dict(headers or {})
    if cookie:
        h["Cookie"] = cookie
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        h["Content-Type"] = "application/json"
    c.request(method, path, body=data, headers=h)
    r = c.getresponse()
    out = r.read()
    c.close()
    return r, out


print("присоединение (штатно, по HTTPS):")
rec = nodes.join("127.0.0.1:%d" % NODE_PORT, "admin", "node-pass-1", name="vps1")
check("хост присоединён, отпечаток запомнен", rec["name"] == "vps1" and nodes.load("vps1").get("fingerprint"), rec)

r, b = req("POST", "/api/login", {"user": "admin", "password": "hub-pass-1"}, {"Origin": ORIGIN, "X-CSRF": "x"})
hub_cookie = r.getheader("Set-Cookie").split(";")[0]
check("вход в хаб", r.status == 200, b)

print("телепорт — страницы:")
r, b = req("GET", "/t/vps1/")
check("без входа в хаб — на вход хаба", r.status == 302 and r.getheader("Location") == "/login", r.status)
r, b = req("GET", "/t/vps1", cookie=hub_cookie)
check("/t/<имя> без слэша — на /t/<имя>/", r.status == 302 and r.getheader("Location") == "/t/vps1/")
r, b = req("GET", "/t/vps1/", cookie=hub_cookie)
check("страница — панель того хоста (статика относительная)", r.status == 200 and b'src="static/app.js"' in b, (r.status, b[:200]))
check("куки того хоста в браузер не уходят", not r.getheader("Set-Cookie"))
r, b = req("GET", "/t/vps1/static/app.js", cookie=hub_cookie)
check("его статика — через хаб", r.status == 200 and b"TELE" in b and r.getheader("Content-Type", "").startswith(("application/javascript", "text/javascript")),
      (r.status, r.getheader("Content-Type")))
r, b = req("GET", "/t/vps1/login", cookie=hub_cookie)
check("его переадресации — внутри телепорта (Location /t/vps1/…)", r.status == 302 and r.getheader("Location") == "/t/vps1/", r.getheader("Location"))

print("телепорт — команды выполняет тот хост:")
r, b = req("GET", "/t/vps1/api/session", cookie=hub_cookie)
sess = json.loads(b)
check("сессия — того хоста (его порт, его CSRF)", sess["ok"] and sess["data"]["port"] == NODE_PORT and sess["data"]["csrf"], sess)
csrf = sess["data"]["csrf"]
r, b = req("POST", "/t/vps1/api/targets/host/dns", {"dns": "1.1.1.1 8.8.8.8"},
           {"Origin": ORIGIN, "X-CSRF": csrf}, cookie=hub_cookie)
d = json.loads(b)
check("POST с его CSRF — выполнено там", r.status == 200 and d["ok"], d)
r, b = req("POST", "/t/vps1/api/targets/host/dns", {"dns": "1.1.1.1"}, {"Origin": "http://evil.example", "X-CSRF": csrf}, cookie=hub_cookie)
check("чужой Origin — отказ ещё на хабе", r.status == 403, b)
r, b = req("POST", "/t/vps1/api/targets/host/dns", {"dns": "1.1.1.1"}, {"Origin": ORIGIN, "X-CSRF": "wrong-token"}, cookie=hub_cookie)
check("неверный CSRF — отказ того хоста", r.status == 403, b)

print("сессия того хоста кончилась:")
node.app.sessions._s.clear()                 # тот хост перезапустил панель — все сессии пропали
r, b = req("GET", "/t/vps1/api/session", cookie=hub_cookie)
d = json.loads(b)
check("хаб незаметно входит заново", r.status == 200 and d["ok"] and d["data"]["csrf"] != csrf, d)

print("WebSocket — терминал того хоста:")
c = socket.create_connection(("127.0.0.1", HUB_PORT), timeout=10)
key = base64.b64encode(secrets.token_bytes(16)).decode()
c.sendall(("GET /t/vps1/ws/term?target=host&cols=80&rows=24 HTTP/1.1\r\nHost: 127.0.0.1:%d\r\nOrigin: %s\r\n"
           "Upgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Key: %s\r\nSec-WebSocket-Version: 13\r\n"
           "Cookie: %s\r\n\r\n" % (HUB_PORT, ORIGIN, key, hub_cookie)).encode())
head = b""
while b"\r\n\r\n" not in head:
    x = c.recv(4096)
    if not x:
        break
    head += x
c.close()
check("терминал открылся там (101 через хаб)", head.startswith(b"HTTP/1.1 101"), head[:120])

print("ошибки:")
r, b = req("GET", "/t/nope/", cookie=hub_cookie)
check("нет такого хоста — понятная страница 502", r.status == 502 and "nope".encode() in b, (r.status, b[:200]))
r, b = req("GET", "/t/nope/api/overview", cookie=hub_cookie)
check("…и JSON для API", r.status == 502 and not json.loads(b)["ok"])
rec2 = nodes.load("vps1")
rec2["fingerprint"] = "AA:" * 31 + "AA"
from pcs.core.util import jsave  # noqa: E402
jsave(nodes.path("vps1"), rec2, 0o600)
hub.app.teleport.cookies.clear()
r, b = req("GET", "/t/vps1/api/session", cookie=hub_cookie)
check("сертификат не тот — отказ, ничего не отправлено", r.status == 502 and "подмена" in json.loads(b)["error"], b)

node.shutdown()
hub.shutdown()
shutil.rmtree(TMP, ignore_errors=True)
print("\nитого: ok %d, fail %d" % (passed, failed))
sys.exit(1 if failed else 0)
