#!/usr/bin/env python3
"""Связь хостов (PCS.md §19): HTTPS на порту панели, присоединение, токены, вызовы через хаб.

Хост-«VPS» — настоящий сервер панели с фейковыми данными и настоящим TLS, в этом процессе;
хаб — настоящий pcs.hub.nodes. Без root и без сети.

    python3 tests/test_nodes.py
"""
import http.client
import json
import os
import socket
import ssl
import sys
import tempfile
import threading
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, os.pardir))
TMP = tempfile.mkdtemp(prefix="pcs-nodes-test-")
os.environ["PCS_ETC"] = os.path.join(TMP, "hub-etc")
from pcs.core.util import Fail  # noqa: E402
from pcs.hub import api, nodes  # noqa: E402
from pcs.web import auth, fake_api, server, tls, tokens  # noqa: E402

passed = failed = 0


def check(name, cond, detail=""):
    global passed, failed
    if cond:
        passed += 1
        print("  ok   %s" % name)
    else:
        failed += 1
        print("  FAIL %s%s" % (name, ("\n       " + str(detail)) if detail else ""))


def fails(fn, *a, **kw):
    try:
        fn(*a, **kw)
    except Fail as e:
        return str(e)
    return None


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def start(tls_dir, tokens_path, config):
    port = free_port()
    srv = server.make_server(port, fake_api, "127.0.0.1", config, tls_dir=tls_dir, fail_delay=0, log_dir=None,
                             quiet=True, tokens_path=tokens_path)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, port


if not __import__("shutil").which("openssl"):
    print("  skip: нет openssl — сертификат не создать")
    sys.exit(0)

print("HTTPS на порту панели:")
cfg = os.path.join(TMP, "vps-web.json")
auth.set_password(cfg, "admin", "vps-pass-1", iterations=1000)
srv, port = start(os.path.join(TMP, "vps-tls"), os.path.join(TMP, "vps-tokens.json"), cfg)
fp = tls.fingerprint_file(tls.paths(os.path.join(TMP, "vps-tls"))[0])
check("сертификат создан сам, ключ — только root (600)", len(fp) == 95
      and oct(os.stat(tls.paths(os.path.join(TMP, "vps-tls"))[1]).st_mode & 0o777) == "0o600", fp)
r = urllib.request.urlopen("http://127.0.0.1:%d/login" % port, timeout=5)
check("тот же порт по HTTP — панель для браузера, как раньше", r.status == 200 and b"<html" in r.read().lower())
ctx = tls.client_context()
r = urllib.request.urlopen("https://127.0.0.1:%d/login" % port, timeout=5, context=ctx)
check("и по HTTPS — та же панель", r.status == 200)
c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
c.request("POST", "/api/v1/join", body=json.dumps({"user": "admin", "password": "vps-pass-1"}),
          headers={"Content-Type": "application/json"})
resp = c.getresponse()
check("присоединение по голому HTTP — отказ (пароль открытым текстом не пойдёт)", resp.status == 403
      and "HTTPS" in json.loads(resp.read())["error"])

print("присоединение:")
check("неверный пароль — понятный отказ", "неверный логин или пароль" in (fails(nodes.join, "127.0.0.1:%d" % port, "admin", "bad") or ""))
msg = fails(nodes.join, "127.0.0.1:%d" % port, "admin", "vps-pass-1", fingerprint="AB" * 32) or ""
check("чужой отпечаток (подмена посредине) — отказ ДО отправки пароля, и какой там на самом деле",
      "не совпал с указанным" in msg and fp in msg, msg)
check("отпечаток кривой длины — понятная ошибка", "64" in (fails(nodes.join, "127.0.0.1:%d" % port, "admin", "x", fingerprint="AB:CD") or ""))
d = nodes.join("https://127.0.0.1:%d/" % port, "admin", "vps-pass-1", name="VPS Msk 1", fingerprint=fp.lower())
check("присоединён: имя, адрес, отпечаток запомнены; пароль — нет", d["name"] == "vps-msk-1" and d["port"] == port
      and d["fingerprint"] == fp and "token" not in d and "vps-pass-1" not in open(nodes.path("vps-msk-1")).read(), d)
check("запись хоста — 600, токен у хаба есть", oct(os.stat(nodes.path("vps-msk-1")).st_mode & 0o777) == "0o600"
      and json.load(open(nodes.path("vps-msk-1")))["token"].startswith("pcs_"))
check("на хосте хранится только хэш токена", "pcs_" not in open(os.path.join(TMP, "vps-tokens.json")).read()
      and len(tokens.listing(os.path.join(TMP, "vps-tokens.json"))) == 1)
check("список хостов — без токенов", [x["name"] for x in nodes.listing()] == ["vps-msk-1"]
      and all("token" not in x for x in nodes.listing()))

print("вызовы через хаб:")
o = nodes.overview_one(nodes.load("vps-msk-1"))
check("сводка хоста: живой, вид, серверы", o["ok"] and o["host_info"]["name"] and isinstance(o["servers"], list), o)
e = nodes.call("vps-msk-1", "GET", "targets/2000/modlink")
check("любой адрес панели через /api/v1 (modlink ВМ 2000)", e["ok"] and e["data"]["list"]["ok"], e)
e = nodes.call("vps-msk-1", "POST", "run", {"target": "2000", "part": "proxyveth", "args": ["status"]})
check("команда части (proxyveth status) — конверт", e["ok"] and isinstance(e["data"], list), str(e)[:200])
e = nodes.call("vps-msk-1", "POST", "run", {"target": "2000", "part": "proxyveth", "args": ["sync"]})
check("долгая команда — задание у хоста", e["ok"] and set(e["data"]) == {"job"}, e)
for _ in range(60):
    j = nodes.call("vps-msk-1", "GET", "jobs/" + e["data"]["job"])
    if j["data"]["state"] != "running":
        break
    time.sleep(0.2)
check("задание доработало, итог виден через хаб", j["data"]["state"] == "done", j)
e = nodes.call("vps-msk-1", "POST", "run", {"target": "2000", "part": "proxyveth", "args": ["sync", "--force"]})
check("опасное (--force) без подтверждения — 403 и у хоста", not e["ok"] and "подтвердите" in e["error"], e)
u = nodes.login_url("vps-msk-1")
check("одноразовая ссылка входа: http, тот же порт, 60 с", u["url"].startswith("http://127.0.0.1:%d/login?ott=" % port)
      and u["ttl"] == 60, u)
op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor())
r = op.open(u["url"], timeout=5)
body = r.read()
check("по ссылке — сразу в панель (сессия выдана)", r.status == 200 and r.geturl().endswith("/")
      and b"login" not in r.geturl().encode(), r.geturl())
r = urllib.request.urlopen(u["url"], timeout=5)
check("второй раз та же ссылка не пускает", r.geturl().endswith("/login"), r.geturl())

print("терминал хоста через хаб:")
from pcs.web import ws  # noqa: E402
sock, rest = nodes.ws_open("vps-msk-1", "target=host&cols=80&rows=24")
buf = bytearray(rest)
sock.settimeout(10)
deadline = time.time() + 10
while b"root@" not in buf and time.time() < deadline:
    chunk = sock.recv(4096)
    if not chunk:
        break
    buf += chunk
check("WebSocket к терминалу хоста: рукопожатие по токену, приглашение пришло", b"root@" in buf, bytes(buf[:200]))
sock.sendall(ws.frame(0x2, b"proxyveth mode\n", mask=b"abcd"))
deadline, out = time.time() + 10, bytearray()
while b"mode" not in out and time.time() < deadline:
    chunk = sock.recv(4096)
    if not chunk:
        break
    out += chunk
check("ввод доходит, вывод возвращается", b"mode" in out, bytes(out[:300]))
sock.close()
rec = json.load(open(nodes.path("vps-msk-1")))
c = http.client.HTTPSConnection("127.0.0.1", port, context=ctx, timeout=5)
c.request("GET", "/api/v1/ws/term?target=host", headers={"Upgrade": "websocket", "Connection": "Upgrade",
                                                          "Sec-WebSocket-Key": "dGhlIHNhbXBsZSBub25jZQ==",
                                                          "Sec-WebSocket-Version": "13", "Authorization": "Bearer pcs_xx"})
check("терминал по чужому токену — 401", c.getresponse().status == 401)
c.close()

print("терминал присоединённого хоста в браузере хаба (ретранслятор):")
import pcs.hub.api as hub_api  # noqa: E402
hub_cfg = os.path.join(TMP, "hub-web.json")
auth.set_password(hub_cfg, "admin", "hub-pass-1", iterations=1000)
hub_port = free_port()
hub = server.make_server(hub_port, hub_api, "127.0.0.1", hub_cfg, fail_delay=0, log_dir=None, quiet=True)
threading.Thread(target=hub.serve_forever, daemon=True).start()
op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor())
origin = "http://127.0.0.1:%d" % hub_port
req = urllib.request.Request(origin + "/api/login", data=json.dumps({"user": "admin", "password": "hub-pass-1"}).encode(),
                             headers={"Content-Type": "application/json", "X-CSRF": "1", "Origin": origin})
op.open(req, timeout=5)
cookie = "; ".join("%s=%s" % (c.name, c.value) for c in op.handlers[-1].cookiejar) if hasattr(op.handlers[-1], "cookiejar") else ""
if not cookie:
    for h in op.handlers:
        if hasattr(h, "cookiejar"):
            cookie = "; ".join("%s=%s" % (c.name, c.value) for c in h.cookiejar)
s_ = socket.create_connection(("127.0.0.1", hub_port), timeout=10)
s_.sendall(("GET /ws/node/vps-msk-1/term?target=host&cols=80&rows=24 HTTP/1.1\r\nHost: 127.0.0.1:%d\r\nUpgrade: websocket\r\n"
            "Connection: Upgrade\r\nSec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\nSec-WebSocket-Version: 13\r\n"
            "Origin: %s\r\nCookie: %s\r\n\r\n" % (hub_port, origin, cookie)).encode())
buf, t0 = bytearray(), time.time()
while time.time() - t0 < 10 and b"root@" not in buf:
    chunk = s_.recv(4096)
    if not chunk:
        break
    buf += chunk
check("браузер → хаб → хост: 101 и приглашение консоли того хоста", buf.startswith(b"HTTP/1.1 101") and b"root@" in buf,
      bytes(buf[:200]))
s_.sendall(ws.frame(0x2, b"proxyveth mode\n", mask=b"q1w2"))
t0, out = time.time(), bytearray()
while time.time() - t0 < 10 and b"mode" not in out:
    chunk = s_.recv(4096)
    if not chunk:
        break
    out += chunk
check("ввод через хаб доходит до хоста, ответ возвращается", b"mode" in out, bytes(out[:200]))
s_.close()
s2 = socket.create_connection(("127.0.0.1", hub_port), timeout=10)
s2.sendall(("GET /ws/node/vps-msk-1/term?target=host HTTP/1.1\r\nHost: 127.0.0.1:%d\r\nUpgrade: websocket\r\n"
            "Connection: Upgrade\r\nSec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\nSec-WebSocket-Version: 13\r\n"
            "Origin: %s\r\n\r\n" % (hub_port, origin)).encode())
check("без входа в хаб — 401", s2.recv(200).startswith(b"HTTP/1.1 401"))
s2.close()
hub.shutdown()

print("защита API:")
c = http.client.HTTPSConnection("127.0.0.1", port, context=ctx, timeout=5)
c.request("GET", "/api/v1/overview", headers={"Authorization": "Bearer pcs_someone-else-token"})
resp = c.getresponse()
check("чужой токен — 401", resp.status == 401)
resp.read()
c.request("GET", "/api/v1/overview", headers={"Authorization": "Bearer " + json.load(open(nodes.path("vps-msk-1")))["token"],
                                              "Origin": "http://evil.example"})
resp = c.getresponse()
check("с Origin (страница в браузере) — отказ: /api/v1 только для программ", resp.status == 403)
resp.read()
c.close()

print("подмена хоста:")
srv.shutdown()
srv.server_close()
srv2 = server.make_server(port, fake_api, "127.0.0.1", cfg, tls_dir=os.path.join(TMP, "other-tls"), fail_delay=0,
                          log_dir=None, quiet=True, tokens_path=os.path.join(TMP, "vps-tokens.json"))
threading.Thread(target=srv2.serve_forever, daemon=True).start()
check("на том же адресе другой сертификат — хаб не отдаёт токен, честная ошибка",
      "подмена" in (fails(nodes.call, "vps-msk-1", "GET", "overview") or ""))
o = nodes.overview_one(nodes.load("vps-msk-1"))
check("в сводке — недоступен с объяснением, а не падение", not o["ok"] and "подмена" in o["error"], o)
srv2.shutdown()
srv2.server_close()
srv, port2 = start(os.path.join(TMP, "vps-tls"), os.path.join(TMP, "vps-tokens.json"), cfg)
rec = json.load(open(nodes.path("vps-msk-1")))
rec["port"] = port2
__import__("pcs.core.util", fromlist=["x"]).jsave(nodes.path("vps-msk-1"), rec, 0o600)

print("отсоединение:")
d = nodes.remove("vps-msk-1")
check("отсоединён: хост отозвал токен, записи у хаба нет", d["revoked"] and not os.path.exists(nodes.path("vps-msk-1"))
      and tokens.listing(os.path.join(TMP, "vps-tokens.json")) == [], d)
check("неизвестный хост — понятная ошибка", "pcs node list" in (fails(nodes.load, "nope") or ""))
check("адрес: мусор — понятная ошибка", "не IP" in (fails(nodes.parse_addr, "a b c") or ""))
check("адрес: порт по умолчанию 666", nodes.parse_addr("95.165.83.236") == ("95.165.83.236", 666))
srv.shutdown()

print("API хаба:")
check("api: nodes, nodes_overview, node_join, node_call, node_login, node_remove — есть",
      all(hasattr(api, f) for f in ("nodes", "nodes_overview", "node_join", "node_call", "node_login", "node_remove")))

print("\nитого: ok %d, fail %d" % (passed, failed))
sys.exit(1 if failed else 0)
