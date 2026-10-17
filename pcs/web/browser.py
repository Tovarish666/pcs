"""Браузер на одно окно через прокси — вкладки mp.space и ЮMoney в панели.

Как устроено:
  - Firefox ESR на машине панели, в своём виртуальном экране (Xtigervnc), от пользователя
    pcs-browser без прав; окно видно во вкладке панели через noVNC (WebSocket панели).
  - Один постоянный профиль на (хост или ВМ, вкладка): /var/lib/pcs/browser/<ключ>/profile —
    куки, вход, вкладки и сохранённые пароли переживают закрытие браузера, перезапуск панели
    и перезагрузку машины. Удаляется только вручную.
  - Прокси: Firefox ходит в SOCKS5 на unix-сокете /run/pcs-browser-<ключ>/proxy.sock, за ним
    пересыльщик (forward) — к прокси пользователя: HTTP CONNECT или SOCKS5, с логином. DNS — тоже
    через прокси. Сменили прокси — открытые соединения рвутся, новые идут через новый.
  - Ни одного TCP-порта: VNC и прокси — unix-сокеты, X — без tcp. `ss -tulpn` чистый.
  - Браузер обычный: без автоматизации (никакого webdriver), без подмены отпечатков; WebRTC
    не светит адрес машины (только через прокси), русский язык, московское время.
  - Запуск — только по кнопке; никто не смотрит IDLE секунд — закрывается сам (профиль остаётся).

Запуск сеанса — юнит systemd pcs-browser-<ключ> (systemd-run), внутри — `python3 -m
pcs.web.browser run КЛЮЧ ДИСПЛЕЙ` от pcs-browser: пересыльщик, dbus, Xtigervnc, matchbox, Firefox.
"""
import base64
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
IDLE = 1800                 # с без зрителя — закрыть браузер
MAX_RUNNING = 4
FIREFOX = "firefox-esr"
XVNC = "Xtigervnc"
WM = "matchbox-window-manager"
PACKAGES = ("firefox-esr", "firefox-esr-l10n-ru", "tigervnc-standalone-server", "matchbox-window-manager",
            "xfonts-base", "fonts-dejavu-core", "fonts-liberation", "libgl1-mesa-dri", "dbus-daemon", "wmctrl")
TZ = "Europe/Moscow"
EUROPE = set("AT BE BG HR CY CZ DK EE FI FR DE GR HU IE IT LV LT LU MT NL PL PT RO SK SI ES SE GB CH NO IS LI "
             "MD RS ME MK AL BA AD MC SM VA".split())

# Вкладки: что за сайт, какой нужен адрес, кнопки-помогаторы (свои — добавляются в панели)
KINDS = {
    "mp": {"title": "mp.space", "want": "eu", "want_text": "европейский адрес",
           "links": [["mobileproxy.space", "https://mobileproxy.space/"]]},
    "ym": {"title": "ЮMoney", "want": "ru", "want_text": "российский адрес",
           "links": [["Кошелёк", "https://yoomoney.ru/"]]},
}


try:
    from ..core.util import Fail as _Fail
except ImportError:                     # запуск сеанса от pcs-browser: тот же пакет, но на всякий
    _Fail = Exception


class BrowserFail(_Fail):
    pass


# ── ключ сеанса и файлы ──────────────────────────────────────────────────────

def key(kind, target, node=None):
    """mp|ym + host|номер ВМ (+ присоединённый хост) → имя профиля и юнита: 2000-mp, n-vps1-host-ym."""
    if kind not in KINDS:
        raise BrowserFail("вкладка: mp или ym")
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
    """Внешний адрес через прокси и страна: {ip, cc, country, city, region_ok}."""
    try:
        d = json.loads(_https_get(up, "ipwho.is", "/?lang=ru&fields=success,ip,country,country_code,city"))
        if not d.get("success", True) or not d.get("ip"):
            raise ValueError
        out = {"ip": d["ip"], "cc": d.get("country_code"), "country": d.get("country"), "city": d.get("city")}
    except (BrowserFail, ValueError, KeyError):
        ip = _https_get(up, "api.ipify.org", "/").strip()
        if not re.fullmatch(r"[0-9a-fA-F.:]{3,45}", ip):
            raise BrowserFail("внешний адрес не узнать: ответ «%s»" % ip[:60])
        out = {"ip": ip, "cc": None, "country": None, "city": None}
    want = KINDS.get(kind, {}).get("want")
    cc = (out["cc"] or "").upper()
    out["region_ok"] = None if not cc or not want else (cc == "RU" if want == "ru" else cc in EUROPE)
    out["t"] = int(time.time())
    return out


# ── пересыльщик: SOCKS5 на unix-сокете → прокси пользователя ─────────────────

class Forward:
    """Firefox → unix-сокет (SOCKS5 без пароля) → прокси пользователя. Прокси берётся из
    session.json при каждом соединении; сменился — открытые соединения рвутся."""

    def __init__(self, path, conf_file):
        self.path, self.conf_file = path, conf_file
        self.conns = set()
        self.lock = threading.Lock()
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

    def serve(self):
        try:
            os.unlink(self.path)
        except OSError:
            pass
        srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        srv.bind(self.path)
        os.chmod(self.path, 0o600)
        srv.listen(128)
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
            if cmd != 1:                    # только CONNECT: UDP через прокси браузер не шлёт
                c.sendall(b"\x05\x07\x00\x01\0\0\0\0\0\0")
                return
            up = self.upstream()
            if not up:
                c.sendall(b"\x05\x02\x00\x01\0\0\0\0\0\0")      # прокси не задан — никуда напрямую
                return
            try:
                far = dial(up, host, port)
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


# ── профиль Firefox ──────────────────────────────────────────────────────────

def prefs(k):
    """Обычный Firefox: только прокси, сохранение сеанса и тишина первого запуска."""
    sock = os.path.join(RUNDIR % k, "proxy.sock")
    p = {
        "network.proxy.type": 1,
        "network.proxy.socks": "file://" + sock,
        "network.proxy.socks_port": 0,
        "network.proxy.socks_version": 5,
        "network.proxy.socks_remote_dns": True,         # DNS — тоже через прокси
        "network.proxy.no_proxies_on": "",
        "network.proxy.allow_hijacking_localhost": True,
        "network.trr.mode": 5,                          # без DoH мимо прокси
        "media.peerconnection.ice.proxy_only": True,    # WebRTC не светит адрес машины
        "media.peerconnection.ice.default_address_only": True,
        "browser.startup.page": 3,                      # вкладки — как были
        "browser.sessionstore.resume_from_crash": True,
        "browser.shell.checkDefaultBrowser": False,
        "browser.aboutwelcome.enabled": False,
        "trailhead.firstrun.didSeeAboutWelcome": True,
        "browser.startup.homepage_override.mstone": "ignore",
        "datareporting.policy.dataSubmissionPolicyBypassNotification": True,
        "toolkit.telemetry.reportingpolicy.firstRun": False,
        "browser.tabs.warnOnClose": False,
        "browser.warnOnQuit": False,
        "browser.sessionstore.max_resumed_crashes": 100,  # обрыв (свет, перезагрузка) — тоже вкладки как были
        "intl.accept_languages": "ru-RU, ru, en-US, en",
        "intl.locale.requested": "ru",
        "app.update.auto": False,
    }
    lines = ["// PCS: пишется при каждом запуске браузера из панели; свои настройки — в about:config"]
    for name, v in p.items():
        lines.append("user_pref(%s, %s);" % (json.dumps(name), json.dumps(v)))
    return "\n".join(lines) + "\n"


# ── установка и сеансы (root, процесс панели) ────────────────────────────────

def installed():
    return bool(shutil.which(FIREFOX) and shutil.which(XVNC) and shutil.which(WM))


def install(log=print):
    """Пакеты браузера (≈300 МБ) и пользователь pcs-browser. Повторять можно."""
    from ..hivelink.common import apt_install
    osr = ""
    try:
        with open("/etc/os-release") as f:
            osr = f.read()
    except OSError:
        pass
    if re.search(r"^ID=\"?ubuntu", osr, re.M):
        raise BrowserFail("на Ubuntu Firefox ESR в apt нет (только snap) — браузер ставится на хост Proxmox/Debian")
    if not os.path.exists("/etc/debian_version"):
        raise BrowserFail("браузер ставится на Debian/Proxmox")
    if not shutil.which("apt-get"):
        raise BrowserFail("нет apt-get")
    got = apt_install(list(PACKAGES), log=log)
    log("пакеты на месте" + (": поставлены %s" % " ".join(got) if got else ""))
    ensure_user()
    if not installed():
        raise BrowserFail("после установки нет %s — см. журнал apt" % ", ".join(
            x for x in (FIREFOX, XVNC, WM) if not shutil.which(x)))
    log("браузер готов: %s" % subprocess.run([FIREFOX, "--version"], capture_output=True, text=True).stdout.strip())
    return {"installed": True}


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
    _own(tmp, 0o600)
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
    """Свой номер X-дисплея на ключ (60–259): абстрактные сокеты X общие на всю машину."""
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


def start(k, size=None):
    if not installed():
        raise BrowserFail("браузер не установлен — «Установить браузер»")
    c = load_conf(k)
    if not c.get("proxy"):
        raise BrowserFail("сначала прокси: без него браузер вышел бы в сеть с адреса сервера")
    if running(k):
        return {"running": True, "started": False}
    if len(all_running()) >= MAX_RUNNING:
        raise BrowserFail("открыто уже %d браузера — закрой ненужный" % MAX_RUNNING)
    ensure_user()
    h = home(k)
    for d in (h, os.path.join(h, "profile"), os.path.join(h, "home")):
        os.makedirs(d, exist_ok=True)
        _own(d, 0o700)
    uj = os.path.join(h, "profile", "user.js")
    with open(uj, "w") as f:
        f.write(prefs(k))
    _own(uj, 0o600)
    w, hh = size or (1600, 900)
    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    cmd = ["systemd-run", "--unit", UNIT % k, "--collect", "--quiet",
           "--description", "PCS: браузер %s (%s)" % (KINDS[kind_of(k)]["title"], k),
           "-p", "User=" + USER, "-p", "Group=" + USER,
           "-p", "RuntimeDirectory=" + (RUNDIR % k)[5:], "-p", "RuntimeDirectoryMode=0700",
           "-p", "PrivateTmp=yes", "-p", "ProtectHome=yes", "-p", "ProtectSystem=strict",
           # mixed: SIGTERM — только нашему процессу; он сам закрывает Firefox (тот сохраняет вкладки
           # и куки), потом экран. control-group уронил бы экран одновременно с Firefox.
           "-p", "ReadWritePaths=" + h, "-p", "TimeoutStopSec=30", "-p", "KillMode=mixed",
           "-p", "MemoryMax=3G", "-p", "Nice=5",
           "--setenv", "PYTHONPATH=" + root,
           sys.executable, "-m", "pcs.web.browser", "run", k, str(_display(k)), "%dx%d" % (w, hh)]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        raise BrowserFail("не запустился: %s" % (r.stderr or r.stdout).strip()[-300:])
    sock = os.path.join(RUNDIR % k, "vnc.sock")
    for _ in range(100):
        if os.path.exists(sock):
            return {"running": True, "started": True}
        if not running(k):
            break
        time.sleep(0.15)
    why = subprocess.run(["journalctl", "-u", UNIT % k, "-n", "8", "--no-pager", "-o", "cat"],
                         capture_output=True, text=True).stdout.strip()
    stop(k)
    raise BrowserFail("экран браузера не поднялся%s" % (": " + why.replace("\n", " | ")[-400:] if why else ""))


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
    s.settimeout(10)
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


# ── сеанс (pcs-browser, внутри юнита) ────────────────────────────────────────

def run(k, display, size="1600x900"):
    """Пересыльщик, dbus, экран, оконный менеджер, Firefox. Кончается вместе с Firefox."""
    rd = RUNDIR % k
    h = home(k)
    disp = ":%d" % int(display)
    # C.UTF-8 есть везде (ru_RU в системе может быть не собрана); язык Firefox — из его настроек
    env = dict(os.environ, DISPLAY=disp, HOME=os.path.join(h, "home"), TZ=TZ, LANG="C.UTF-8",
               LANGUAGE="ru", MOZ_CRASHREPORTER_DISABLE="1", MOZ_DBUS_REMOTE="1", MOZ_ENABLE_WAYLAND="0",
               DBUS_SESSION_BUS_ADDRESS="unix:path=%s/bus" % rd, XDG_RUNTIME_DIR=rd)
    fw = Forward(os.path.join(rd, "proxy.sock"), conf_path(k))
    threading.Thread(target=fw.serve, daemon=True).start()
    kids = []

    def spawn(argv, **kw):
        p = subprocess.Popen(argv, env=env, stdin=subprocess.DEVNULL, **kw)
        kids.append(p)
        return p

    spawn(["dbus-daemon", "--session", "--nofork", "--nopidfile", "--address=unix:path=%s/bus" % rd])
    x = spawn([XVNC, disp, "-geometry", size, "-depth", "24", "-SecurityTypes", "None", "-rfbport", "-1",
               "-rfbunixpath", os.path.join(rd, "vnc.sock"), "-rfbunixmode", "0600", "-nolisten", "tcp",
               "-AlwaysShared", "-desktop", "PCS " + KINDS[kind_of(k)]["title"]])
    for _ in range(100):                # экран готов, когда VNC открыл свой сокет
        if os.path.exists(os.path.join(rd, "vnc.sock")) or x.poll() is not None:
            break
        time.sleep(0.1)
    spawn([WM, "-use_titlebar", "no"])
    profile = os.path.join(h, "profile")
    first = not os.path.exists(os.path.join(profile, "prefs.js"))      # профиль ещё не запускали
    start_url = [KINDS[kind_of(k)]["links"][0][1]] if first else []
    ff = spawn([FIREFOX, "--profile", profile, "--class", "pcs-browser"] + start_url)
    done = threading.Event()

    def ctl():
        p = os.path.join(rd, "ctl.sock")
        srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        srv.bind(p)
        os.chmod(p, 0o600)
        srv.listen(8)
        while not done.is_set():
            c, _ = srv.accept()
            try:
                c.settimeout(10)
                msg = json.loads(c.recv(8192).decode("utf-8"))
                url = msg.get("open", "")
                if not re.fullmatch(r"https?://[^\s\"'<>]{1,2000}", url):
                    c.sendall(b"bad url\n")
                    continue
                r = subprocess.run([FIREFOX, "--profile", profile, "--new-tab", url], env=env,
                                   capture_output=True, text=True, timeout=20)
                c.sendall(b"ok\n" if r.returncode == 0 else ("firefox: %s\n" % r.stderr.strip()[-200:]).encode())
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
    while ff.poll() is None and x.poll() is None and not stopping.is_set():
        try:
            ff.wait(timeout=1)
        except subprocess.TimeoutExpired:
            pass
    # Закрыть по-хорошему: окно — через оконный менеджер, как крестиком. Так Firefox пишет
    # сеанс (вкладки) и сбрасывает куки на диск; на SIGTERM он выходит наспех.
    end = time.monotonic() + 20
    while ff.poll() is None and x.poll() is None and time.monotonic() < end:
        subprocess.run(["wmctrl", "-x", "-c", "pcs-browser"], env=env, capture_output=True, timeout=5)
        try:
            ff.wait(timeout=2)
        except subprocess.TimeoutExpired:
            pass
    if ff.poll() is None:
        ff.send_signal(signal.SIGTERM)
        try:
            ff.wait(timeout=10)
        except subprocess.TimeoutExpired:
            ff.kill()
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
                "installed": installed(), "install_job": self._install_job,
                "running": running(k), "proxy": masked(c.get("proxy")), "proxy_type": (c.get("proxy") or {}).get("scheme"),
                "check": c.get("check"), "links": c.get("links") or KINDS[kind]["links"], "idle_min": IDLE // 60,
                "profile": os.path.join(home(k), "profile"), "used": os.path.isdir(os.path.join(home(k), "profile"))}

    def set_proxy(self, k, text, ptype):
        up = parse_proxy(text, ptype)
        res = check(up, kind_of(k))               # не работает — не сохраняем
        c = load_conf(k)
        c.update(proxy=up, check=res)
        save_conf(k, c)
        return {"check": res, "proxy": masked(up), "running": running(k)}

    def recheck(self, k):
        c = load_conf(k)
        if not c.get("proxy"):
            raise BrowserFail("прокси не задан")
        res = check(c["proxy"], kind_of(k))
        c["check"] = res
        save_conf(k, c)
        return {"check": res}

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
    if len(argv) >= 3 and argv[0] == "run":
        return run(argv[1], argv[2], argv[3] if len(argv) > 3 else "1600x900")
    print("python3 -m pcs.web.browser run КЛЮЧ ДИСПЛЕЙ [ШxВ] — сеанс браузера (зовёт systemd-run)", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
