"""Браузер на одно окно через прокси — вкладки mp.space и ЮMoney в панели.

Задача — самый обычный браузер, а не «невидимка»: ничего не подделывается, браузер не знает
ни о каком прокси. Он живёт в своей сети, где весь интернет — прокси пользователя, как
обычный компьютер за роутером провайдера.

  - Браузер — Яндекс Браузер для Linux (официальный пакет из репозитория Яндекса), обычный
    профиль, обычные настройки. Экран — Xtigervnc 1920×1080, окно видно во вкладке панели
    через noVNC (WebSocket панели); от пользователя pcs-browser без прав.
  - Сеть — свой network namespace pcsbr<N>: в нём только lo и tun0 sing-box («роутер»),
    маршрут по умолчанию — в tun0. sing-box отдаёт всё на 127.0.0.1:1080 этого же namespace —
    там слушает наш пересыльщик, а соединения к прокси пользователя он открывает уже из сети
    машины (поток переключается между namespace). DNS перехватывается на tun и идёт через
    прокси по TCP. Сеть машины не трогается: ни NAT, ни forwarding, ни правил маршрутизации.
  - Утечек нет по устройству: у namespace нет другого выхода, кроме прокси. WebRTC, DNS,
    QUIC — как у любого компьютера за таким провайдером (UDP прокси не возит — браузер сам
    переходит на TCP).
  - Часовой пояс — страны внешнего адреса прокси, язык — русский.
  - Профиль на (хост или ВМ, вкладка): /var/lib/pcs/browser/<ключ>/profile — куки, вход,
    вкладки переживают закрытие браузера, перезапуск панели и машины.
  - Ни одного TCP-порта на машине: VNC — unix-сокет, пересыльщик — внутри namespace.
  - Запуск — только по кнопке; никто не смотрит IDLE секунд — закрывается сам.

Сеанс — юнит systemd pcs-browser-<ключ> (systemd-run) с `python3 -m pcs.web.browser run …`
(root: namespace, sing-box, пересыльщик), внутри — `… session …` от pcs-browser (dbus,
Xtigervnc, matchbox, браузер).

Веб модемов (вид md) — тот же браузер, но вместо прокси пользователя пересыльщик ходит к модемам
машины, где они воткнуты: на хабе — прямо, на ВМ — через SSH хаба (`ssh -W`, свой мастер в
каталоге сеанса). Пускает только к частным адресам на портах веба (MODEM_NETS, MODEM_PORTS):
больше этому браузеру ходить некуда — ни в интернет, ни к самой машине.
"""
import base64
import ctypes
import ipaddress
import json
import os
import pwd
import re
import select
import shutil
import signal
import socket
import ssl
import struct
import subprocess
import sys
import threading
import time

ROOT = "/var/lib/pcs/browser"
USER = "pcs-browser"
UNIT = "pcs-browser-%s"
RUNDIR = "/run/pcs-browser-%s"
NS = "pcsbr%d"
IDLE = 1800                 # с без зрителя — закрыть браузер
MAX_RUNNING = 4
SCREEN = "1920x1080"        # обычный экран, а не размер вкладки панели
BROWSER = "yandex-browser-stable"
XVNC = "Xtigervnc"
WM = "matchbox-window-manager"
SINGBOX = "/usr/local/lib/pcs/sing-box"
FWD_PORT = 1080             # пересыльщик — внутри namespace браузера, снаружи его нет
TUN_ADDR = "172.19.0.1/30"
YANDEX_REPO = "https://repo.yandex.ru/yandex-browser/deb"
YANDEX_KEY = "https://repo.yandex.ru/yandex-browser/YANDEX-BROWSER-KEY.GPG"
YANDEX_FPR = "224974DB7FED6DECB7D855EF60B9CD3A083A7A9A"     # Yandex Browser Repository Key
YANDEX_KEYRING = "/etc/apt/keyrings/yandex-browser.asc"
YANDEX_LIST = "/etc/apt/sources.list.d/yandex-browser.list"
PACKAGES = ("tigervnc-standalone-server", "matchbox-window-manager", "xfonts-base", "fonts-dejavu-core",
            "fonts-liberation", "fonts-noto-core", "fonts-noto-color-emoji", "libgl1-mesa-dri", "dbus-daemon",
            "wmctrl", "gnupg")
TZ_DEFAULT = "Europe/Moscow"
EUROPE = set("AT BE BG HR CY CZ DK EE FI FR DE GR HU IE IT LV LT LU MT NL PL PT RO SK SI ES SE GB CH NO IS LI "
             "MD RS ME MK AL BA AD MC SM VA".split())

# Вкладки: что за сайт, какой нужен адрес, кнопки-помогаторы (свои — добавляются в панели)
KINDS = {
    "mp": {"title": "mp.space", "want": "eu", "want_text": "европейский адрес",
           "links": [["mobileproxy.space", "https://mobileproxy.space/"]]},
    "ym": {"title": "ЮMoney", "want": "ru", "want_text": "российский адрес",
           "links": [["Кошелёк", "https://yoomoney.ru/"]]},
    "md": {"title": "веб модемов", "want": None, "want_text": "", "links": []},
}
MODEM_NETS = tuple(ipaddress.ip_network(n) for n in ("192.168.0.0/16", "10.0.0.0/8", "172.16.0.0/12"))
MODEM_PORTS = (80, 443, 8080, 8443)


try:
    from ..core.util import Fail as _Fail
except ImportError:
    _Fail = Exception


class BrowserFail(_Fail):
    pass


# ── ключ сеанса и файлы ──────────────────────────────────────────────────────

def key(kind, target, node=None):
    """mp|ym + host|номер ВМ (+ присоединённый хост) → имя профиля и юнита: 2000-mp, n-vps1-host-ym."""
    if kind not in KINDS:
        raise BrowserFail("вкладка: mp, ym или md")
    t = str(target)
    if t != "host" and not re.fullmatch(r"\d{1,9}", t):
        raise BrowserFail("цель: host или номер ВМ")
    if node is not None and not re.fullmatch(r"[a-z0-9-]{1,40}", str(node)):
        raise BrowserFail("имя хоста")
    return ("n-%s-" % node if node else "") + "%s-%s" % (t, kind)


def kind_of(k):
    return k.rsplit("-", 1)[-1]


def home(k):
    return os.path.join(ROOT, k)


def conf_path(k):
    return os.path.join(home(k), "session.json")


def load_conf(k):
    try:
        with open(conf_path(k)) as f:
            c = json.load(f)
        return c if isinstance(c, dict) else {}
    except (OSError, ValueError):
        return {}


# ── прокси пользователя ──────────────────────────────────────────────────────

def parse_proxy(text, ptype="http"):
    """host:port | host:port:login:pass | login:pass@host:port | http://… | socks5://… →
    {scheme, host, port, user, pw}. Тип из строки важнее ptype."""
    t = (text or "").strip()
    if not t:
        raise BrowserFail("прокси не задан")
    scheme = ptype if ptype in ("http", "socks5") else "http"
    m = re.match(r"^(https?|socks5h?|socks)://(.*)$", t, re.I)
    if m:
        scheme = "socks5" if m.group(1).lower().startswith("socks") else "http"
        t = m.group(2).rstrip("/")
    user = pw = None
    if "@" in t:
        cred, _, t = t.rpartition("@")
        user, _, pw = cred.partition(":")
    parts = t.split(":")
    if len(parts) == 4 and user is None:
        host, port, user, pw = parts
    elif len(parts) == 2:
        host, port = parts
    else:
        raise BrowserFail("прокси: host:port, host:port:логин:пароль или socks5://логин:пароль@host:port")
    try:
        port = int(port)
    except ValueError:
        raise BrowserFail("прокси: порт — число")
    if not (0 < port < 65536) or not re.fullmatch(r"[A-Za-z0-9.-]{1,253}", host):
        raise BrowserFail("прокси: адрес или порт")
    return {"scheme": scheme, "host": host, "port": port, "user": user or None, "pw": pw or None}


def masked(up):
    if not up:
        return None
    return "%s://%s%s:%d" % (up["scheme"], (up["user"] + ":•••@") if up.get("user") else "", up["host"], up["port"])


def _recv_exact(s, n):
    buf = b""
    while len(buf) < n:
        d = s.recv(n - len(buf))
        if not d:
            raise BrowserFail("прокси закрыл соединение")
        buf += d
    return buf


def dial(up, host, port, timeout=15):
    """Соединение с host:port через прокси пользователя → сокет."""
    try:
        s = socket.create_connection((up["host"], up["port"]), timeout=timeout)
    except OSError as e:
        raise BrowserFail("прокси %s:%d не отвечает (%s)" % (up["host"], up["port"], e.strerror or e))
    try:
        if up["scheme"] == "http":
            req = "CONNECT %s:%d HTTP/1.1\r\nHost: %s:%d\r\n" % (host, port, host, port)
            if up.get("user"):
                tok = base64.b64encode(("%s:%s" % (up["user"], up.get("pw") or "")).encode()).decode()
                req += "Proxy-Authorization: Basic %s\r\n" % tok
            s.sendall((req + "\r\n").encode())
            head = b""
            while b"\r\n\r\n" not in head:
                d = s.recv(4096)
                if not d:
                    raise BrowserFail("прокси закрыл соединение")
                head += d
                if len(head) > 16384:
                    raise BrowserFail("прокси: слишком длинный ответ")
            line = head.split(b"\r\n", 1)[0].decode("latin-1")
            code = line.split(" ")[1] if len(line.split(" ")) > 1 else "?"
            if code != "200":
                raise BrowserFail("прокси отказал: %s" % ("нужен логин и пароль" if code == "407" else line.strip()))
            return s
        # SOCKS5
        methods = b"\x00\x02" if up.get("user") else b"\x00"
        s.sendall(b"\x05" + bytes([len(methods)]) + methods)
        ver, meth = _recv_exact(s, 2)
        if ver != 5 or meth == 0xFF:
            raise BrowserFail("SOCKS5: прокси не принял способ входа" + ("" if up.get("user") else " — нужен логин и пароль"))
        if meth == 2:
            u, p = (up.get("user") or "").encode(), (up.get("pw") or "").encode()
            s.sendall(b"\x01" + bytes([len(u)]) + u + bytes([len(p)]) + p)
            if _recv_exact(s, 2)[1] != 0:
                raise BrowserFail("SOCKS5: логин или пароль не подошли")
        try:
            ip = ipaddress.ip_address(host)
            addr = (b"\x01" + ip.packed) if ip.version == 4 else (b"\x04" + ip.packed)
        except ValueError:
            h = host.encode("idna")
            addr = b"\x03" + bytes([len(h)]) + h
        s.sendall(b"\x05\x01\x00" + addr + struct.pack("!H", port))
        rep = _recv_exact(s, 4)
        if rep[1] != 0:
            raise BrowserFail("SOCKS5: прокси не соединил (код %d)" % rep[1])
        atyp = rep[3]
        _recv_exact(s, 4 if atyp == 1 else 16 if atyp == 4 else _recv_exact(s, 1)[0])
        _recv_exact(s, 2)
        return s
    except BrowserFail:
        s.close()
        raise
    except OSError as e:
        s.close()
        raise BrowserFail("прокси: %s" % (e.strerror or e))


def modem_addr(host, port):
    """Пускать ли браузер веба модемов: частный IPv4 и порт веба."""
    try:
        a = ipaddress.IPv4Address(host)
    except ValueError:
        return False
    return port in MODEM_PORTS and any(a in n for n in MODEM_NETS)


def ssh_tunnel_argv(argv, host, port, ctl_dir):
    """argv ssh хаба до ВМ (последний — root@IP) → тот же, но `-W host:port` и свой мастер в ctl_dir."""
    if not argv or not str(argv[-1]).startswith("root@"):
        raise BrowserFail("путь до ВМ: argv ssh хаба")
    head = [a for a in argv[:-1] if a != "-tt"]
    opts = ["-F", "/dev/null", "-o", "ControlMaster=auto", "-o", "ControlPath=%s/cm-%%C" % ctl_dir,
            "-o", "ControlPersist=300"]
    return [head[0]] + opts + head[1:] + ["-W", "%s:%d" % (host, port), argv[-1]]


def dial_via(via, host, port, ctl_dir="/run", timeout=10):
    """Соединение к веб модема с машины, где он воткнут. via: {"kind": "local"} — отсюда;
    {"kind": "ssh", "argv": [...]} — с ВМ через SSH хаба (поток ssh -W)."""
    if not modem_addr(host, port):
        raise BrowserFail("к %s:%s браузеру модемов нельзя" % (host, port))
    if via.get("kind") == "local":
        try:
            return socket.create_connection((host, port), timeout=timeout)
        except OSError as e:
            raise BrowserFail("модем %s не отвечает: %s" % (host, e.strerror or e))
    if via.get("kind") == "ssh":
        a, b = socket.socketpair()
        try:
            p = subprocess.Popen(ssh_tunnel_argv(via.get("argv") or [], host, port, ctl_dir), stdin=b, stdout=b,
                                 stderr=subprocess.DEVNULL, close_fds=True)
        except OSError as e:
            a.close()
            raise BrowserFail("ssh: %s" % e)
        finally:
            b.close()
        threading.Thread(target=p.wait, daemon=True).start()        # без зомби: ssh кончится с потоком
        return a
    raise BrowserFail("путь к модемам: local или ssh")


def _https_get(up, host, path, timeout=15):
    s = dial(up, host, 443, timeout)
    try:
        s.settimeout(timeout)
        t = ssl.create_default_context().wrap_socket(s, server_hostname=host)
        t.sendall(("GET %s HTTP/1.1\r\nHost: %s\r\nUser-Agent: curl/8\r\nAccept: */*\r\nConnection: close\r\n\r\n"
                   % (path, host)).encode())
        data = b""
        while len(data) < 65536:
            d = t.recv(16384)
            if not d:
                break
            data += d
        t.close()
    except (OSError, ssl.SSLError) as e:
        s.close()
        raise BrowserFail("%s через прокси: %s" % (host, getattr(e, "strerror", None) or e))
    head, _, body = data.partition(b"\r\n\r\n")
    if b"chunked" in head.lower():
        out, rest = b"", body
        while rest:
            n, _, rest = rest.partition(b"\r\n")
            try:
                n = int(n.split(b";")[0], 16)
            except ValueError:
                break
            if not n:
                break
            out, rest = out + rest[:n], rest[n + 2:]
        body = out
    return body.decode("utf-8", "replace")


def check(up, kind=None):
    """Внешний адрес через прокси, страна и часовой пояс: {ip, cc, country, city, tz, region_ok}."""
    try:
        d = json.loads(_https_get(up, "ipwho.is", "/?lang=ru&fields=success,ip,country,country_code,city,timezone"))
        if not d.get("success", True) or not d.get("ip"):
            raise ValueError
        tz = (d.get("timezone") or {}).get("id") if isinstance(d.get("timezone"), dict) else None
        out = {"ip": d["ip"], "cc": d.get("country_code"), "country": d.get("country"), "city": d.get("city"),
               "tz": tz if tz and re.fullmatch(r"[A-Za-z_]+(/[A-Za-z0-9_+-]+){0,2}", tz) else None}
    except (BrowserFail, ValueError, KeyError):
        ip = _https_get(up, "api.ipify.org", "/").strip()
        if not re.fullmatch(r"[0-9a-fA-F.:]{3,45}", ip):
            raise BrowserFail("внешний адрес не узнать: ответ «%s»" % ip[:60])
        out = {"ip": ip, "cc": None, "country": None, "city": None, "tz": None}
    want = KINDS.get(kind, {}).get("want")
    cc = (out["cc"] or "").upper()
    out["region_ok"] = None if not cc or not want else (cc == "RU" if want == "ru" else cc in EUROPE)
    out["t"] = int(time.time())
    return out


# ── пересыльщик: SOCKS5 внутри namespace браузера → прокси пользователя ──────

def setns(fd):
    """Перевести ТЕКУЩИЙ поток в network namespace (fd). Сокеты, созданные потоком после этого,
    живут там; уже открытые остаются, где были."""
    if hasattr(os, "setns"):
        os.setns(fd, getattr(os, "CLONE_NEWNET", 0x40000000))
        return
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.setns(fd, 0x40000000) != 0:
        e = ctypes.get_errno()
        raise OSError(e, os.strerror(e))


class Forward:
    """SOCKS5 без пароля (для sing-box) → прокси пользователя. Слушает там, куда указывает
    listen_in (fd namespace браузера) или в своём; к прокси ходит из сети машины. Прокси —
    из session.json при каждом соединении; сменился — открытые соединения рвутся."""

    def __init__(self, conf_file, port=FWD_PORT, listen_in=None, unix=None, ctl_dir="/run"):
        self.conf_file, self.port, self.listen_in, self.unix = conf_file, port, listen_in, unix
        self.ctl_dir = ctl_dir
        self.conns = set()
        self.lock = threading.Lock()
        self.ready = threading.Event()
        self._up, self._mt = None, None

    def upstream(self):
        try:
            mt = os.stat(self.conf_file).st_mtime_ns
        except OSError:
            return None
        if mt != self._mt:
            c = {}
            try:
                with open(self.conf_file) as f:
                    c = json.load(f)
            except (OSError, ValueError):
                pass
            new = c.get("proxy") if isinstance(c.get("proxy"), dict) else None
            if new is None and isinstance(c.get("via"), dict):
                new = dict(c["via"], via=True)          # веб модемов: не прокси, а машина с модемами
            if self._mt is not None and new != self._up:
                self.drop_all()
            self._up, self._mt = new, mt
        return self._up

    def drop_all(self):
        with self.lock:
            conns, self.conns = list(self.conns), set()
        for s in conns:
            try:
                s.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass

    def listener(self):
        if self.unix:                                   # проверки: unix-сокет
            try:
                os.unlink(self.unix)
            except OSError:
                pass
            srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            srv.bind(self.unix)
            os.chmod(self.unix, 0o600)
            return srv
        own = None
        if self.listen_in is not None:
            own = os.open("/proc/thread-self/ns/net", os.O_RDONLY)
            setns(self.listen_in)
        try:
            srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            srv.bind(("127.0.0.1", self.port))
        finally:
            if own is not None:
                setns(own)                              # назад: к прокси — из сети машины
                os.close(own)
        return srv

    def serve(self):
        srv = self.listener()
        srv.listen(256)
        self.ready.set()
        threading.Thread(target=self._watch, daemon=True).start()
        while True:
            c, _ = srv.accept()
            threading.Thread(target=self._one, args=(c,), daemon=True).start()

    def _watch(self):
        while True:
            time.sleep(2)
            self.upstream()

    def _one(self, c):
        far = None
        try:
            c.settimeout(30)
            ver, n = _recv_exact(c, 2)
            _recv_exact(c, n)
            c.sendall(b"\x05\x00")
            ver, cmd, _, atyp = _recv_exact(c, 4)
            if atyp == 1:
                host = socket.inet_ntoa(_recv_exact(c, 4))
            elif atyp == 4:
                host = socket.inet_ntop(socket.AF_INET6, _recv_exact(c, 16))
            elif atyp == 3:
                host = _recv_exact(c, _recv_exact(c, 1)[0]).decode("idna")
            else:
                return
            port = struct.unpack("!H", _recv_exact(c, 2))[0]
            if cmd != 1:                    # только TCP: UDP прокси не возит, браузер перейдёт на TCP
                c.sendall(b"\x05\x07\x00\x01\0\0\0\0\0\0")
                return
            up = self.upstream()
            if not up:
                c.sendall(b"\x05\x02\x00\x01\0\0\0\0\0\0")      # прокси не задан — никуда напрямую
                return
            if up.get("via") and not modem_addr(host, port):
                c.sendall(b"\x05\x02\x00\x01\0\0\0\0\0\0")      # веб модемов: никуда, кроме модемов
                return
            try:
                far = dial_via(up, host, port, self.ctl_dir) if up.get("via") else dial(up, host, port)
            except BrowserFail:
                c.sendall(b"\x05\x05\x00\x01\0\0\0\0\0\0")
                return
            c.sendall(b"\x05\x00\x00\x01\0\0\0\0\0\0")
            c.settimeout(None)
            far.settimeout(None)
            with self.lock:
                self.conns.update((c, far))
            relay(c, far)
        except (OSError, BrowserFail, ValueError, UnicodeError):
            pass
        finally:
            with self.lock:
                self.conns.discard(c)
                self.conns.discard(far)
            for s in (c, far):
                if s is not None:
                    try:
                        s.close()
                    except OSError:
                        pass


def relay(a, b, chunk=65536):
    socks = [a, b]
    while True:
        r, _, _ = select.select(socks, [], [], 300)
        if not r:
            continue
        for s in r:
            try:
                d = s.recv(chunk)
            except OSError:
                return
            if not d:
                return
            (b if s is a else a).sendall(d)


# ── «роутер»: sing-box в namespace браузера ──────────────────────────────────

def singbox_config(port=FWD_PORT, modems=False):
    """tun0 — маршрут по умолчанию namespace; всё — в пересыльщик на 127.0.0.1:port.
    DNS перехватывается и идёт через прокси по TCP (ipv4_only: IPv6 у прокси обычно нет).
    modems — браузер веба модемов: в пересыльщик только адреса и порты модемов, остальное (DNS,
    службы браузера) отклоняется прямо здесь — сразу и без строки в журнале на каждый запрос."""
    if modems:
        return {
            "log": {"level": "warn", "timestamp": False},
            "inbounds": [{"type": "tun", "tag": "tun-in", "interface_name": "tun0", "address": [TUN_ADDR],
                          "mtu": 1420, "auto_route": False, "strict_route": False, "stack": "system"}],
            "outbounds": [{"type": "socks", "tag": "proxy", "version": "5", "server": "127.0.0.1", "server_port": port}],
            "route": {"rules": [{"network": "tcp", "ip_cidr": [str(n) for n in MODEM_NETS], "port": list(MODEM_PORTS),
                                 "action": "route", "outbound": "proxy"},
                                {"ip_cidr": ["0.0.0.0/0", "::/0"], "action": "reject"}],
                      "final": "proxy", "auto_detect_interface": False},
        }
    return {
        "log": {"level": "warn", "timestamp": False},
        "dns": {"servers": [{"type": "tcp", "tag": "remote", "server": "1.1.1.1", "detour": "proxy"}],
                "final": "remote", "strategy": "ipv4_only"},
        "inbounds": [{"type": "tun", "tag": "tun-in", "interface_name": "tun0", "address": [TUN_ADDR],
                      "mtu": 1420, "auto_route": False, "strict_route": False, "stack": "system"}],
        "outbounds": [{"type": "socks", "tag": "proxy", "version": "5", "server": "127.0.0.1", "server_port": port}],
        # UDP прокси не возит: отклоняем сразу (ICMP «порт недоступен»), как в сети с закрытым UDP —
        # браузер тут же переходит с QUIC на TCP, а не ждёт таймаутов на каждой странице.
        "route": {"rules": [{"port": 53, "action": "hijack-dns"}, {"network": "udp", "action": "reject"}],
                  "final": "proxy", "auto_detect_interface": False},
    }


# ── установка и сеансы (root, процесс панели) ────────────────────────────────

def browser_bin():
    return shutil.which(BROWSER)


def installed():
    return bool(browser_bin() and shutil.which(XVNC) and shutil.which(WM) and os.path.exists(SINGBOX))


def _yandex_repo(log):
    """Репозиторий Яндекс Браузера: ключ — только с закреплённым отпечатком."""
    import urllib.request
    key = urllib.request.urlopen(YANDEX_KEY, timeout=60).read()
    r = subprocess.run(["gpg", "--show-keys", "--with-colons"], input=key, capture_output=True)
    fprs = [ln.split(b":")[9].decode() for ln in r.stdout.splitlines() if ln.startswith(b"fpr:")]
    if not fprs or fprs[0] != YANDEX_FPR:
        raise BrowserFail("ключ репозитория Яндекс Браузера не тот (%s) — не ставлю" % (fprs[:1] or "не прочитан"))
    os.makedirs(os.path.dirname(YANDEX_KEYRING), exist_ok=True)
    with open(YANDEX_KEYRING, "wb") as f:
        f.write(key)
    os.chmod(YANDEX_KEYRING, 0o644)
    line = "deb [arch=amd64 signed-by=%s] %s stable main\n" % (YANDEX_KEYRING, YANDEX_REPO)
    with open(YANDEX_LIST, "w") as f:
        f.write(line)
    log("репозиторий Яндекс Браузера: %s (ключ %s…)" % (YANDEX_REPO, YANDEX_FPR[:16]))


def install(log=print):
    """Яндекс Браузер, экран, шрифты, sing-box и пользователь pcs-browser. Повторять можно."""
    from ..hivelink.common import apt_install
    from ..core import singbox
    if not os.path.exists("/etc/debian_version") or not shutil.which("apt-get"):
        raise BrowserFail("браузер ставится на Debian/Proxmox/Ubuntu (apt)")
    if os.uname().machine not in ("x86_64", "amd64"):
        raise BrowserFail("Яндекс Браузер для Linux — только amd64")
    got = apt_install(["gnupg", "curl"], log=log)
    _yandex_repo(log)
    env = dict(os.environ, DEBIAN_FRONTEND="noninteractive")
    subprocess.run(["apt-get", "-o", "DPkg::Lock::Timeout=600", "update"], env=env, capture_output=True, timeout=600)
    got += apt_install(list(PACKAGES) + [BROWSER], log=log)
    log("пакеты на месте" + (": поставлены %s" % " ".join(got) if got else ""))
    if not os.path.exists(SINGBOX):
        singbox.install()
        log("sing-box %s поставлен" % singbox.VERSION)
    ensure_user()
    if not installed():
        raise BrowserFail("после установки нет %s — см. журнал apt" % ", ".join(
            x for x in (BROWSER, XVNC, WM, SINGBOX) if not (shutil.which(x) or os.path.exists(x))))
    v = subprocess.run([browser_bin(), "--version"], capture_output=True, text=True).stdout.strip()
    log("браузер готов: %s" % (v or BROWSER))
    return {"installed": True, "version": v}


def ensure_user():
    try:
        pwd.getpwnam(USER)
    except KeyError:
        subprocess.run(["useradd", "--system", "--user-group", "--home-dir", ROOT, "--no-create-home",
                        "--shell", "/usr/sbin/nologin", USER], check=True)
    os.makedirs(ROOT, mode=0o755, exist_ok=True)
    os.chmod(ROOT, 0o755)


def _own(path, mode):
    pw = pwd.getpwnam(USER)
    os.chown(path, pw.pw_uid, pw.pw_gid)
    os.chmod(path, mode)


def save_conf(k, c):
    ensure_user()
    h = home(k)
    os.makedirs(h, exist_ok=True)
    _own(h, 0o700)
    tmp = conf_path(k) + ".tmp"
    with open(tmp, "w") as f:
        json.dump(c, f, ensure_ascii=False, indent=1)
    os.chmod(tmp, 0o600)                    # прокси с паролем — только root (пересыльщик — root)
    os.replace(tmp, conf_path(k))


def systemctl(*a):
    try:
        return subprocess.run(("systemctl",) + a, capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired) as e:
        return subprocess.CompletedProcess(a, 1, "", str(e))


def running(k):
    return systemctl("is-active", UNIT % k + ".service").stdout.strip() in ("active", "activating")


def all_running():
    out = systemctl("list-units", "--plain", "--no-legend", "pcs-browser-*.service").stdout
    return [m.group(1) for m in re.finditer(r"^pcs-browser-(\S+)\.service\s+\S+\s+active", out, re.M)]


def _display(k):
    """Свой номер X-дисплея и namespace на ключ (60–259)."""
    path = os.path.join(ROOT, "displays.json")
    try:
        with open(path) as f:
            d = json.load(f)
    except (OSError, ValueError):
        d = {}
    if k not in d:
        used = set(d.values())
        d[k] = next(n for n in range(60, 260) if n not in used)
        with open(path, "w") as f:
            json.dump(d, f)
    return d[k]


def start(k, url=None):
    """Запустить сеанс; url — открыть сразу (первый запуск браузера сам открывает адрес: второй
    запуск, пока первый ещё поднимается, мог бы стать отдельным браузером)."""
    if url is not None and not re.fullmatch(r"https?://[^\s\"'<>]{1,2000}", url):
        raise BrowserFail("адрес: http(s)://…")
    if not installed():
        raise BrowserFail("браузер не установлен — «Установить браузер»")
    c = load_conf(k)
    if not c.get("proxy") and not c.get("via"):
        raise BrowserFail("сначала прокси: без него браузеру некуда ходить")
    if running(k):
        return {"running": True, "started": False}
    if len(all_running()) >= MAX_RUNNING:
        raise BrowserFail("открыто уже %d браузера — закрой ненужный" % MAX_RUNNING)
    ensure_user()
    h = home(k)
    for d in (h, os.path.join(h, "profile"), os.path.join(h, "home")):
        os.makedirs(d, exist_ok=True)
        _own(d, 0o700)
    tz = (c.get("check") or {}).get("tz") or TZ_DEFAULT
    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    cmd = ["systemd-run", "--unit", UNIT % k, "--collect", "--quiet",
           "--description", "PCS: браузер %s (%s)" % (KINDS[kind_of(k)]["title"], k),
           # mixed: SIGTERM — только нашему процессу; он закрывает браузер по-хорошему (вкладки и
           # куки сохраняются), потом экран, «роутер» и namespace.
           "-p", "KillMode=mixed", "-p", "TimeoutStopSec=45", "-p", "PrivateTmp=yes",
           "-p", "ProtectHome=yes", "-p", "MemoryMax=4G", "-p", "Nice=5",
           "--setenv", "PYTHONPATH=" + root, "--setenv", "PCS_BROWSER_TZ=" + tz] + \
        (["--setenv", "PCS_BROWSER_URL=" + url] if url else []) + \
        [sys.executable, "-m", "pcs.web.browser", "run", k, str(_display(k))]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        raise BrowserFail("не запустился: %s" % (r.stderr or r.stdout).strip()[-300:])
    sock = os.path.join(RUNDIR % k, "vnc.sock")
    for _ in range(150):
        if os.path.exists(sock):
            return {"running": True, "started": True}
        if not running(k):
            break
        time.sleep(0.15)
    why = subprocess.run(["journalctl", "-u", UNIT % k, "-n", "10", "--no-pager", "-o", "cat"],
                         capture_output=True, text=True).stdout.strip()
    stop(k)
    raise BrowserFail("экран браузера не поднялся%s" % (": " + why.replace("\n", " | ")[-500:] if why else ""))


def stop(k):
    systemctl("stop", UNIT % k + ".service")
    return {"running": False}


def vnc_socket(k):
    p = os.path.join(RUNDIR % k, "vnc.sock")
    return p if os.path.exists(p) else None


def open_url(k, url):
    if not re.fullmatch(r"https?://[^\s\"'<>]{1,2000}", url or ""):
        raise BrowserFail("адрес: http(s)://…")
    p = os.path.join(RUNDIR % k, "ctl.sock")
    if not os.path.exists(p):
        raise BrowserFail("браузер не запущен")
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(20)
    try:
        s.connect(p)
        s.sendall(json.dumps({"open": url}).encode() + b"\n")
        ans = s.recv(4096).decode("utf-8", "replace").strip()
    except OSError as e:
        raise BrowserFail("браузер не ответил: %s" % (e.strerror or e))
    finally:
        s.close()
    if ans != "ok":
        raise BrowserFail(ans or "браузер не открыл адрес")
    return {"opened": url}


# ── сеанс: root — сеть и «роутер»; pcs-browser — экран и браузер ─────────────

def _sh(*a, **kw):
    return subprocess.run(a, capture_output=True, text=True, **kw)


def run(k, display):
    """root, юнит pcs-browser-<ключ>: namespace, пересыльщик, sing-box, потом сеанс от pcs-browser.
    Кончается вместе с сеансом; namespace снимается всегда."""
    d = int(display)
    ns = NS % d
    rd = RUNDIR % k
    os.makedirs(rd, exist_ok=True)
    _own(rd, 0o750)
    pw = pwd.getpwnam(USER)
    _sh("ip", "netns", "del", ns)
    r = _sh("ip", "netns", "add", ns)
    if r.returncode != 0:
        print("✗ namespace %s: %s" % (ns, r.stderr.strip()), file=sys.stderr)
        return 1
    kids = []
    try:
        _sh("ip", "netns", "exec", ns, "ip", "link", "set", "lo", "up")
        _sh("ip", "netns", "exec", ns, "sysctl", "-qw", "net.ipv6.conf.all.disable_ipv6=1",
            "net.ipv6.conf.default.disable_ipv6=1")
        # DNS браузера: любой адрес на :53 уходит в tun и перехватывается sing-box
        etc = "/etc/netns/" + ns
        os.makedirs(etc, exist_ok=True)
        with open(os.path.join(etc, "resolv.conf"), "w") as f:
            f.write("nameserver 1.1.1.1\noptions edns0\n")
        nsfd = os.open("/run/netns/" + ns, os.O_RDONLY)
        fw = Forward(conf_path(k), FWD_PORT, listen_in=nsfd, ctl_dir=rd)
        threading.Thread(target=fw.serve, daemon=True).start()
        if not fw.ready.wait(10):
            raise RuntimeError("пересыльщик не поднялся")
        cfg = os.path.join(rd, "singbox.json")
        with open(cfg, "w") as f:
            json.dump(singbox_config(FWD_PORT, modems=kind_of(k) == "md"), f)
        os.chmod(cfg, 0o600)
        sb = subprocess.Popen(["ip", "netns", "exec", ns, SINGBOX, "run", "-c", cfg], stdin=subprocess.DEVNULL)
        kids.append(sb)
        for _ in range(100):
            if "inet 172.19.0.1/" in _sh("ip", "netns", "exec", ns, "ip", "-4", "-o", "addr", "show", "dev",
                                         "tun0").stdout or sb.poll() is not None:
                break
            time.sleep(0.1)
        r = _sh("ip", "netns", "exec", ns, "ip", "route", "replace", "default", "dev", "tun0")
        if r.returncode != 0:
            raise RuntimeError("маршрут через tun0: %s" % r.stderr.strip())
        env = {"PATH": "/usr/local/bin:/usr/bin:/bin", "PYTHONPATH": os.environ.get("PYTHONPATH", ""),
               "PCS_BROWSER_TZ": os.environ.get("PCS_BROWSER_TZ", TZ_DEFAULT),
               "PCS_BROWSER_URL": os.environ.get("PCS_BROWSER_URL", "")}
        sess = subprocess.Popen(["ip", "netns", "exec", ns, "setpriv", "--reuid=%d" % pw.pw_uid,
                                 "--regid=%d" % pw.pw_gid, "--init-groups", "--",
                                 sys.executable, "-m", "pcs.web.browser", "session", k, str(d)],
                                env=env, stdin=subprocess.DEVNULL)
        kids.append(sess)
        stopping = threading.Event()
        signal.signal(signal.SIGTERM, lambda *_: stopping.set())
        signal.signal(signal.SIGINT, lambda *_: stopping.set())
        while sess.poll() is None and not stopping.is_set():
            time.sleep(0.5)
        if sess.poll() is None:
            sess.send_signal(signal.SIGTERM)        # сеанс сам закрывает браузер по-хорошему
            try:
                sess.wait(timeout=35)
            except subprocess.TimeoutExpired:
                sess.kill()
        return 0
    except (OSError, RuntimeError) as e:
        print("✗ %s" % e, file=sys.stderr)
        return 1
    finally:
        for p in kids[::-1]:
            if p.poll() is None:
                p.terminate()
                try:
                    p.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    p.kill()
        _sh("ip", "netns", "del", ns)
        shutil.rmtree("/etc/netns/" + ns, ignore_errors=True)


def _windows(env):
    out = subprocess.run(["wmctrl", "-l"], env=env, capture_output=True, text=True, timeout=5).stdout
    return [ln.split()[0] for ln in out.splitlines() if ln.strip()]


def session(k, display):
    """pcs-browser, внутри namespace браузера: dbus, экран, оконный менеджер, браузер, управление."""
    rd = RUNDIR % k
    h = home(k)
    disp = ":%d" % int(display)
    env = {"PATH": "/usr/local/bin:/usr/bin:/bin", "DISPLAY": disp, "HOME": os.path.join(h, "home"),
           "TZ": os.environ.get("PCS_BROWSER_TZ") or TZ_DEFAULT, "LANG": "ru_RU.UTF-8", "LANGUAGE": "ru",
           "LC_ALL": "C.UTF-8", "DBUS_SESSION_BUS_ADDRESS": "unix:path=%s/bus" % rd, "XDG_RUNTIME_DIR": rd,
           "USER": USER, "LOGNAME": USER}
    kids = []

    def spawn(argv):
        p = subprocess.Popen(argv, env=env, stdin=subprocess.DEVNULL)
        kids.append(p)
        return p

    spawn(["dbus-daemon", "--session", "--nofork", "--nopidfile", "--address=unix:path=%s/bus" % rd])
    x = spawn([XVNC, disp, "-geometry", SCREEN, "-depth", "24", "-SecurityTypes", "None", "-rfbport", "-1",
               "-rfbunixpath", os.path.join(rd, "vnc.sock"), "-rfbunixmode", "0600", "-nolisten", "tcp",
               "-AlwaysShared", "-AcceptSetDesktopSize=0", "-desktop", "PCS " + KINDS[kind_of(k)]["title"]])
    for _ in range(100):
        if os.path.exists(os.path.join(rd, "vnc.sock")) or x.poll() is not None:
            break
        time.sleep(0.1)
    spawn([WM, "-use_titlebar", "no"])
    profile = os.path.join(h, "profile")
    first = not os.path.exists(os.path.join(profile, "Local State"))
    # Флаги — только то, чего сайты не видят: профиль, без мастера первого запуска, хранилище паролей
    # без связки ключей. --enable-unsafe-swiftshader — штатный программный WebGL Chromium, как у
    # любого Chrome без видеокарты (ВМ, удалённый стол): без него WebGL нет вовсе — это заметнее.
    base = [browser_bin() or BROWSER, "--user-data-dir=" + profile, "--no-first-run", "--no-default-browser-check",
            "--password-store=basic", "--enable-unsafe-swiftshader"]
    links, url = KINDS[kind_of(k)]["links"], os.environ.get("PCS_BROWSER_URL") or ""
    br = spawn(base + ([url] if url else [links[0][1]] if first and links else []))
    done = threading.Event()

    def ctl():
        p = os.path.join(rd, "ctl.sock")
        try:
            os.unlink(p)
        except OSError:
            pass
        srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        srv.bind(p)
        os.chmod(p, 0o600)
        srv.listen(8)
        while not done.is_set():
            c, _ = srv.accept()
            try:
                c.settimeout(20)
                msg = json.loads(c.recv(8192).decode("utf-8"))
                url = msg.get("open", "")
                if not re.fullmatch(r"https?://[^\s\"'<>]{1,2000}", url):
                    c.sendall(b"bad url\n")
                    continue
                # второй запуск с тем же профилем отдаёт адрес уже открытому браузеру
                r = subprocess.run(base + [url], env=env, capture_output=True, text=True, timeout=20)
                c.sendall(b"ok\n" if r.returncode == 0 else ("браузер: %s\n" % r.stderr.strip()[-200:]).encode())
            except (OSError, ValueError, subprocess.TimeoutExpired) as e:
                try:
                    c.sendall(("%s\n" % e).encode())
                except OSError:
                    pass
            finally:
                c.close()

    threading.Thread(target=ctl, daemon=True).start()
    stopping = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stopping.set())
    signal.signal(signal.SIGINT, lambda *_: stopping.set())
    while br.poll() is None and x.poll() is None and not stopping.is_set():
        try:
            br.wait(timeout=1)
        except subprocess.TimeoutExpired:
            pass
    # Закрыть по-хорошему: окна — как крестиком; браузер пишет вкладки и куки. Не вышел — SIGTERM
    # (Chromium и на него закрывается чисто).
    end = time.monotonic() + 15
    while br.poll() is None and x.poll() is None and time.monotonic() < end:
        try:
            for w in _windows(env):
                subprocess.run(["wmctrl", "-i", "-c", w], env=env, capture_output=True, timeout=5)
        except (OSError, subprocess.TimeoutExpired):
            pass
        try:
            br.wait(timeout=2)
        except subprocess.TimeoutExpired:
            pass
    if br.poll() is None:
        br.send_signal(signal.SIGTERM)
        try:
            br.wait(timeout=15)
        except subprocess.TimeoutExpired:
            br.kill()
    done.set()
    for p in kids:
        if p.poll() is None:
            p.terminate()
    for p in kids:
        try:
            p.wait(timeout=5)
        except subprocess.TimeoutExpired:
            p.kill()
    return 0


# ── панель: состояние, зрители, закрытие без зрителя ─────────────────────────

class Manager:
    """Всё, что панель делает с браузерами. Зрители — открытые окна noVNC: никого нет IDLE
    секунд — браузер закрывается (профиль и вход остаются)."""

    def __init__(self, jobs_start=None):
        self.viewers = {}           # ключ → [число зрителей, когда ушёл последний]
        self.lock = threading.Lock()
        self.jobs_start = jobs_start
        self._install_job = None
        now = time.time()
        for k in all_running() if os.path.isdir("/run/systemd/system") else []:
            self.viewers[k] = [0, now]
        threading.Thread(target=self._idle, daemon=True).start()

    def _idle(self):
        while True:
            time.sleep(60)
            now = time.time()
            for k in all_running():
                with self.lock:
                    v = self.viewers.setdefault(k, [0, now])
                if v[0] <= 0 and now - v[1] > IDLE:
                    stop(k)

    def seen(self, k, delta):
        with self.lock:
            v = self.viewers.setdefault(k, [0, time.time()])
            v[0] = max(0, v[0] + delta)
            if v[0] == 0:
                v[1] = time.time()

    def status(self, k):
        c = load_conf(k)
        kind = kind_of(k)
        return {"key": k, "kind": kind, "title": KINDS[kind]["title"], "want": KINDS[kind]["want_text"],
                "installed": installed(), "install_job": self._install_job, "browser": "Яндекс Браузер",
                "running": running(k), "proxy": masked(c.get("proxy")), "proxy_type": (c.get("proxy") or {}).get("scheme"),
                "check": c.get("check"), "links": c.get("links") or KINDS[kind]["links"], "idle_min": IDLE // 60,
                "tz": (c.get("check") or {}).get("tz") or TZ_DEFAULT,
                "profile": os.path.join(home(k), "profile"), "used": os.path.isdir(os.path.join(home(k), "profile"))}

    def set_proxy(self, k, text, ptype):
        up = parse_proxy(text, ptype)
        res = check(up, kind_of(k))               # не работает — не сохраняем
        c = load_conf(k)
        old_tz = (c.get("check") or {}).get("tz")
        c.update(proxy=up, check=res)
        save_conf(k, c)
        run_ = running(k)
        return {"check": res, "proxy": masked(up), "running": run_,
                "tz_changed": bool(run_ and res.get("tz") and res.get("tz") != old_tz)}

    def recheck(self, k):
        c = load_conf(k)
        if not c.get("proxy"):
            raise BrowserFail("прокси не задан")
        res = check(c["proxy"], kind_of(k))
        c["check"] = res
        save_conf(k, c)
        return {"check": res}

    def open_modem(self, k, via, url):
        """Веб модема: сеанс вида md этой машины (путь к модемам — via), в нём — вкладка url."""
        if kind_of(k) != "md":
            raise BrowserFail("веб модема — в браузере модемов (md)")
        c = load_conf(k)
        if c.get("via") != via:
            c["via"] = via
            save_conf(k, c)
        if running(k):
            return dict(open_url(k, url), started=False)
        start(k, url=url)
        return {"opened": url, "started": True}

    def set_links(self, k, links):
        if not isinstance(links, list) or len(links) > 20:
            raise BrowserFail("кнопки: список до 20")
        out = []
        for x in links:
            if not (isinstance(x, list) and len(x) == 2):
                raise BrowserFail("кнопка: [название, адрес]")
            name, url = str(x[0]).strip()[:40], str(x[1]).strip()
            if not name or not re.fullmatch(r"https?://[^\s\"'<>]{1,2000}", url):
                raise BrowserFail("кнопка «%s»: адрес http(s)://…" % name)
            out.append([name, url])
        c = load_conf(k)
        c["links"] = out
        save_conf(k, c)
        return {"links": out}

    # то, что панель зовёт напрямую (у фейка — те же имена)
    key = staticmethod(key)
    start = staticmethod(start)
    stop = staticmethod(stop)
    open_url = staticmethod(open_url)
    vnc_socket = staticmethod(vnc_socket)

    def install_job(self):
        if installed():
            return {"installed": True}
        if self.jobs_start is None:
            raise BrowserFail("установка — только на хабе")
        self._install_job = self.jobs_start()
        return {"job": self._install_job}


def main(argv):
    if len(argv) == 3 and argv[0] == "run":
        return run(argv[1], argv[2])
    if len(argv) == 3 and argv[0] == "session":
        return session(argv[1], argv[2])
    print("python3 -m pcs.web.browser run|session КЛЮЧ ДИСПЛЕЙ — сеанс браузера (зовёт systemd-run)", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
