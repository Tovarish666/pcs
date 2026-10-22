#!/usr/bin/env python3
"""Проверки браузера вкладок mp.space и ЮMoney (pcs.web.browser) без браузера, без root и без сети:
разбор прокси, соединение через HTTP CONNECT и SOCKS5 с паролем, пересыльщик на unix-сокете,
смена прокси на ходу, настройки профиля.

    python3 tests/test_browser.py
"""
import base64
import json
import os
import shutil
import socket
import struct
import sys
import tempfile
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, os.pardir))
from pcs.web import browser as B  # noqa: E402

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
    except B.BrowserFail as e:
        return str(e)
    return None


def serve(handler):
    """TCP-сервер на 127.0.0.1:случайный порт, каждое соединение — handler(conn) в потоке."""
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    s.listen(16)

    def loop():
        while True:
            c, _ = s.accept()
            threading.Thread(target=handler, args=(c,), daemon=True).start()
    threading.Thread(target=loop, daemon=True).start()
    return s.getsockname()[1]


def echo(c):
    try:
        while True:
            d = c.recv(4096)
            if not d:
                break
            c.sendall(b"echo:" + d)
    except OSError:
        pass
    c.close()


ECHO = serve(echo)
SEEN = []       # куда просили соединить прокси: (тип, хост, порт)


def pipe(a, b):
    try:
        B.relay(a, b)
    except OSError:
        pass
    a.close()
    b.close()


def http_proxy(c):
    head = b""
    while b"\r\n\r\n" not in head:
        d = c.recv(4096)
        if not d:
            return c.close()
        head += d
    lines = head.decode().split("\r\n")
    target = lines[0].split(" ")[1]
    auth = [ln for ln in lines if ln.lower().startswith("proxy-authorization:")]
    good = "Basic " + base64.b64encode(b"user:secret").decode()
    if not auth or auth[0].split(":", 1)[1].strip() != good:
        c.sendall(b"HTTP/1.1 407 Proxy Authentication Required\r\n\r\n")
        return c.close()
    host, port = target.rsplit(":", 1)
    SEEN.append(("http", host, int(port)))
    up = socket.create_connection(("127.0.0.1", int(port)))
    c.sendall(b"HTTP/1.1 200 Connection established\r\n\r\n")
    pipe(c, up)


def socks_proxy(c):
    def rx(n):
        return B._recv_exact(c, n)
    try:
        ver, n = rx(2)
        methods = rx(n)
        if 2 not in methods:
            c.sendall(b"\x05\xff")
            return c.close()
        c.sendall(b"\x05\x02")
        rx(1)
        u = rx(rx(1)[0])
        p = rx(rx(1)[0])
        if (u, p) != (b"user", b"secret"):
            c.sendall(b"\x01\x01")
            return c.close()
        c.sendall(b"\x01\x00")
        ver, cmd, _, atyp = rx(4)
        host = socket.inet_ntoa(rx(4)) if atyp == 1 else rx(rx(1)[0]).decode()
        port = struct.unpack("!H", rx(2))[0]
        SEEN.append(("socks5", host, port))
        up = socket.create_connection(("127.0.0.1", port))
        c.sendall(b"\x05\x00\x00\x01\x7f\x00\x00\x01" + struct.pack("!H", port))
        pipe(c, up)
    except (OSError, B.BrowserFail):
        c.close()


HTTP = serve(http_proxy)
SOCKS = serve(socks_proxy)

print("разбор прокси:")
check("host:port:логин:пароль — HTTP по умолчанию",
      B.parse_proxy("1.2.3.4:8080:user:pass")
      == {"scheme": "http", "host": "1.2.3.4", "port": 8080, "user": "user", "pw": "pass"})
check("тип из списка — SOCKS5", B.parse_proxy("proxy.example.net:1080", "socks5")["scheme"] == "socks5")
check("socks5://логин:пароль@host:port — тип из строки",
      B.parse_proxy("socks5://u:p@h.example:9050", "http") == {"scheme": "socks5", "host": "h.example", "port": 9050,
                                                              "user": "u", "pw": "p"})
check("логин:пароль@host:port", B.parse_proxy("u:p@10.0.0.1:3128")["user"] == "u")
check("пусто, мусор, порт не число — понятный отказ",
      all(fails(B.parse_proxy, x) for x in ("", "abc", "h:port", "h:99999", "h:1:2:3:4:5")))
check("в панели пароль скрыт", B.masked(B.parse_proxy("h:1:user:secret")) == "http://user:•••@h:1"
      and "secret" not in B.masked(B.parse_proxy("socks5://user:secret@h:2")))

print("соединение через прокси пользователя:")
for kind, port in (("http", HTTP), ("socks5", SOCKS)):
    up = {"scheme": kind, "host": "127.0.0.1", "port": port, "user": "user", "pw": "secret"}
    s = B.dial(up, "127.0.0.1", ECHO, timeout=5)
    s.sendall(b"hi")
    check("%s с логином: соединяет, данные ходят" % kind, s.recv(100) == b"echo:hi")
    s.close()
    bad = dict(up, pw="wrong")
    msg = fails(B.dial, bad, "127.0.0.1", ECHO, timeout=5)
    check("%s: неверный пароль — понятная ошибка" % kind, msg and ("логин" in msg or "пароль" in msg), msg)
check("прокси не отвечает — понятная ошибка",
      "не отвечает" in (fails(B.dial, {"scheme": "http", "host": "127.0.0.1", "port": 1, "user": None, "pw": None},
                              "x", 1, timeout=2) or ""))

print("пересыльщик: sing-box → SOCKS5 → прокси пользователя (здесь — на unix-сокете):")
TMP = tempfile.mkdtemp(prefix="pcs-br-")
conf = os.path.join(TMP, "session.json")
json.dump({"proxy": {"scheme": "http", "host": "127.0.0.1", "port": HTTP, "user": "user", "pw": "secret"}},
          open(conf, "w"))
sock = os.path.join(TMP, "p.sock")
fw = B.Forward(conf, unix=sock)
threading.Thread(target=fw.serve, daemon=True).start()
for _ in range(50):
    if os.path.exists(sock):
        break
    time.sleep(0.05)


def via_forward(host, port):
    c = socket.socket(socket.AF_UNIX)
    c.settimeout(5)
    c.connect(sock)
    c.sendall(b"\x05\x01\x00")
    assert c.recv(2) == b"\x05\x00"
    h = host.encode()
    c.sendall(b"\x05\x01\x00\x03" + bytes([len(h)]) + h + struct.pack("!H", port))
    rep = B._recv_exact(c, 10)
    return c, rep[1]


c, code = via_forward("localhost", ECHO)
c.sendall(b"ff")
check("SOCKS5 на unix-сокете → HTTP-прокси → цель; имя уходит прокси (DNS не мимо)",
      code == 0 and c.recv(100) == b"echo:ff" and SEEN[-1] == ("http", "localhost", ECHO), (code, SEEN[-1:]))
check("сокет пересыльщика — только владельцу (600)", oct(os.stat(sock).st_mode & 0o777) == "0o600")
time.sleep(1.1)                 # mtime файла — с точностью файловой системы
json.dump({"proxy": {"scheme": "socks5", "host": "127.0.0.1", "port": SOCKS, "user": "user", "pw": "secret"}},
          open(conf, "w"))
time.sleep(2.5)                 # пересыльщик замечает смену раз в 2 с
try:
    left = c.recv(100)
except OSError:
    left = b""
check("сменили прокси — открытые соединения рвутся (браузер переподключится через новый)", left == b"", left)
c.close()
c, code = via_forward("localhost", ECHO)
c.sendall(b"s5")
check("новые соединения — через новый прокси (SOCKS5)", code == 0 and c.recv(100) == b"echo:s5"
      and SEEN[-1] == ("socks5", "localhost", ECHO), SEEN[-1:])
c.close()
json.dump({}, open(conf, "w"))
time.sleep(1.1)
os.utime(conf)
c, code = via_forward("localhost", ECHO)
check("прокси нет — никуда напрямую (отказ SOCKS)", code != 0, code)
c.close()

print("веб модемов: пересыльщик ходит только к модемам машины:")
check("адреса модемов: частные IPv4 и порты веба; остальное — нет",
      B.modem_addr("192.168.101.1", 80) and B.modem_addr("10.5.0.1", 443) and not B.modem_addr("192.168.101.1", 22)
      and not B.modem_addr("8.8.8.8", 80) and not B.modem_addr("1.1.1.1", 53) and not B.modem_addr("modem.local", 80))
hub = ["ssh", "-i", "/etc/pcs/ssh/id_ed25519", "-p", "22", "-o", "BatchMode=yes", "-tt", "root@192.168.88.20"]
tun = B.ssh_tunnel_argv(hub, "192.168.101.1", 80, "/run/pcs-browser-2000-md")
check("ssh хаба → туннель: -W модем, свой мастер в каталоге сеанса, без -tt, адрес ВМ — последним",
      tun[0] == "ssh" and tun[-1] == "root@192.168.88.20" and tun[-3:-1] == ["-W", "192.168.101.1:80"] and "-tt" not in tun
      and "ControlPath=/run/pcs-browser-2000-md/cm-%C" in tun and "-F" in tun, tun)
FAKE_SSH = os.path.join(TMP, "fake-ssh")
with open(FAKE_SSH, "w") as f:
    f.write("#!%s\nimport os, socket, sys, threading\n"
            "hp = sys.argv[sys.argv.index('-W') + 1]\n"
            "s = socket.create_connection(('127.0.0.1', int(hp.rsplit(':', 1)[1])))\n"
            "def up():\n    while True:\n        d = os.read(0, 4096)\n        if not d:\n            break\n        s.sendall(d)\n"
            "threading.Thread(target=up, daemon=True).start()\n"
            "while True:\n    d = s.recv(4096)\n    if not d:\n        break\n    os.write(1, d)\n" % sys.executable)
os.chmod(FAKE_SSH, 0o755)
real_nets, real_ports = B.MODEM_NETS, B.MODEM_PORTS
B.MODEM_NETS, B.MODEM_PORTS = B.MODEM_NETS + (B.ipaddress.ip_network("127.0.0.0/8"),), B.MODEM_PORTS + (ECHO,)
try:
    for via, what in (({"kind": "local"}, "на хабе — прямо"), ({"kind": "ssh", "argv": [FAKE_SSH, "root@vm"]}, "на ВМ — через ssh -W")):
        json.dump({"via": via}, open(conf, "w"))
        time.sleep(1.1)
        os.utime(conf)
        c, code = via_forward("127.0.0.1", ECHO)
        c.sendall(b"md")
        got = c.recv(100) if code == 0 else b""
        c.close()
        check("веб модема %s: соединение доходит до модема" % what, code == 0 and got == b"echo:md", (code, got))
    c, code = via_forward("1.1.1.1", 53)
    check("веб модемов: DNS и интернет — отказ (браузеру модемов больше никуда)", code != 0, code)
    c.close()
    c, code = via_forward("127.0.0.1", 22)
    check("веб модемов: не порт веба — отказ", code != 0, code)
    c.close()
finally:
    B.MODEM_NETS, B.MODEM_PORTS = real_nets, real_ports

md = B.singbox_config(1080, modems=True)
check("«роутер» браузера модемов: в пересыльщик — только TCP к модемам на портах веба, остальное (DNS тоже) — отказ сразу",
      md["route"]["rules"][0]["outbound"] == "proxy" and "192.168.0.0/16" in md["route"]["rules"][0]["ip_cidr"]
      and md["route"]["rules"][0]["port"] == [80, 443, 8080, 8443] and md["route"]["rules"][-1]["action"] == "reject"
      and "dns" not in md and not any(r.get("action") == "hijack-dns" for r in md["route"]["rules"]), md)
sys.path.insert(0, HERE)
from singbox_check import sb_check  # noqa: E402
for name, conf_ in (("браузера вкладок", B.singbox_config(1080)), ("браузера модемов", md)):
    ok_, why = sb_check(conf_)
    if ok_ is not None:
        check("«роутер» %s: конфиг принимает настоящий sing-box" % name, ok_, why)

print("профиль и ключи:")
check("ключи: ВМ, хост, присоединённый хост", B.key("mp", 2000) == "2000-mp" and B.key("ym", "host") == "host-ym"
      and B.key("ym", "host", "vps1") == "n-vps1-host-ym" and B.kind_of("n-vps1-host-ym") == "ym")
check("чужое в ключе — отказ", all(fails(B.key, *a) for a in (("xx", 1), ("mp", "../x"), ("mp", 1, "Bad Name"))))
sb = B.singbox_config(1080)
tun = sb["inbounds"][0]
check("«роутер» sing-box: tun — без auto_route (маршрут ставим сами в своей сети), всё — в пересыльщик 127.0.0.1",
      tun["type"] == "tun" and tun["auto_route"] is False and sb["outbounds"][0]["server"] == "127.0.0.1"
      and sb["outbounds"][0]["server_port"] == 1080 and sb["route"]["final"] == "proxy")
check("UDP (кроме DNS) отклоняется сразу — браузер без задержек переходит с QUIC на TCP",
      sb["route"]["rules"][1] == {"network": "udp", "action": "reject"})
check("DNS браузера перехватывается и идёт через прокси по TCP, только IPv4",
      {"port": 53, "action": "hijack-dns"} in sb["route"]["rules"] and sb["dns"]["servers"][0]["type"] == "tcp"
      and sb["dns"]["servers"][0]["detour"] == "proxy" and sb["dns"]["strategy"] == "ipv4_only")
json.dump({"proxy": {"scheme": "socks5", "host": "127.0.0.1", "port": SOCKS, "user": "user", "pw": "secret"}},
          open(conf, "w"))
c = socket.socket(socket.AF_UNIX)
c.settimeout(5)
c.connect(sock)
c.sendall(b"\x05\x01\x00")
c.recv(2)
c.sendall(b"\x05\x03\x00\x01\x00\x00\x00\x00\x00\x00")       # UDP ASSOCIATE
check("пересыльщик: UDP не возит — на UDP ASSOCIATE честный отказ (браузер перейдёт на TCP)",
      B._recv_exact(c, 10)[1] == 7)
c.close()
import inspect  # noqa: E402
src = inspect.getsource(B.session)
check("браузер без настроек прокси и без флагов, которые видят сайты (только профиль, первый запуск, хранилище паролей)",
      "--proxy" not in src and "--disable-web-security" not in src and "--no-sandbox" not in src
      and "--user-agent" not in src and "--disable-blink-features" not in src)
check("экран — обычный 1920×1080, панель только масштабирует", B.SCREEN == "1920x1080"
      and "-AcceptSetDesktopSize=0" in src)

print("страна адреса:")
real = B._https_get
B._https_get = lambda up, host, path, timeout=15: json.dumps(
    {"success": True, "ip": "185.1.2.3", "country": "Германия", "country_code": "DE", "city": "Франкфурт",
     "timezone": {"id": "Europe/Berlin"}})
check("mp.space: адрес в Европе — годится", B.check({}, "mp")["region_ok"] is True)
check("часовой пояс — страны адреса (для браузера)", B.check({}, "mp")["tz"] == "Europe/Berlin")
check("ЮMoney: не Россия — не годится", B.check({}, "ym")["region_ok"] is False)
B._https_get = lambda up, host, path, timeout=15: (_ for _ in ()).throw(B.BrowserFail("нет")) if host == "ipwho.is" else "185.1.2.3\n"
c = B.check({}, "ym")
check("страну не узнать — только адрес, без оценки", c["ip"] == "185.1.2.3" and c["region_ok"] is None, c)
B._https_get = real

shutil.rmtree(TMP, ignore_errors=True)
print("\nитого: ok %d, fail %d" % (passed, failed))
sys.exit(1 if failed else 0)
