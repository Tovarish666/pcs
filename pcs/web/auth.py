"""Вход в панель: логин и пароль в /etc/pcs/web.json (PBKDF2-SHA256 с солью),
сессии в памяти, пауза после неудачных входов.

    set_password(path, user, password)    — зовёт `pcs web passwd` и install.sh
    check(path, user, password) -> bool
"""
import hashlib
import hmac
import json
import os
import secrets
import threading
import time

from pcs.core.util import Fail

WEB_JSON = "/etc/pcs/web.json"
ITERATIONS = 600000          # OWASP 2023 для PBKDF2-SHA256: вход ≈0,3 с — перебор дорог
MIN_PASSWORD = 8


def hash_password(password, salt=None, iterations=ITERATIONS):
    salt = salt if salt is not None else secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return {"algo": "pbkdf2-sha256", "iterations": iterations, "salt": salt.hex(), "hash": dk.hex()}


def verify(rec, password):
    try:
        if rec.get("algo") != "pbkdf2-sha256":
            return False
        salt, want, n = bytes.fromhex(rec["salt"]), bytes.fromhex(rec["hash"]), int(rec["iterations"])
    except (AttributeError, KeyError, TypeError, ValueError):
        return False
    got = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, n)
    return hmac.compare_digest(got, want)


def load(path):
    try:
        with open(path) as f:
            cfg = json.load(f)
        return cfg if isinstance(cfg, dict) else {}
    except (OSError, ValueError):
        return {}


def _save(path, cfg):
    """Файл 600 с самого создания (а не chmod после записи), каталог 700, атомарно."""
    d = os.path.dirname(os.path.abspath(path))
    os.makedirs(d, mode=0o700, exist_ok=True)
    tmp = "%s.tmp%d" % (path, os.getpid())
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(cfg, f, indent=1, ensure_ascii=False)
            f.write("\n")
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def set_password(path, user, password, iterations=ITERATIONS):
    """Задать логин и пароль панели. Прочие ключи web.json сохраняются."""
    user = (user or "").strip()
    if not user or len(user) > 64 or any(c.isspace() or c in ":/\\" for c in user):
        raise Fail("логин — одно слово без пробелов, до 64 знаков")
    if len(password or "") < MIN_PASSWORD:
        raise Fail("пароль короче %d знаков — придумай длиннее" % MIN_PASSWORD)
    cfg = load(path)
    cfg.update(user=user, password=hash_password(password, iterations=iterations), changed=int(time.time()))
    _save(path, cfg)
    return {"user": user, "path": path}


_DUMMY = {"algo": "pbkdf2-sha256", "iterations": ITERATIONS, "salt": "00" * 16, "hash": "00" * 32}


def check(path, user, password):
    cfg = load(path)
    rec, want = cfg.get("password"), cfg.get("user")
    if not isinstance(rec, dict) or not isinstance(want, str):
        verify(_DUMMY, password or "")       # то же время ответа, что и при живой учётке
        return False
    ok_user = hmac.compare_digest((user or "").encode("utf-8"), want.encode("utf-8"))
    ok_pw = verify(rec, password or "")
    return ok_user and ok_pw


def configured(path):
    cfg = load(path)
    return isinstance(cfg.get("password"), dict) and isinstance(cfg.get("user"), str)


def stamp(path):
    """Отпечаток пароля: сменили пароль — старые сессии больше не годятся."""
    rec = load(path).get("password")
    return (rec or {}).get("hash", "")[:16] if isinstance(rec, dict) else ""


class Sessions:
    """Сессии в памяти: перезапуск панели = вход заново (это нормально)."""

    def __init__(self, ttl=7 * 86400, idle=24 * 3600):
        self.ttl, self.idle = ttl, idle
        self._s = {}
        self._lock = threading.Lock()

    def new(self, user, stamp=""):
        now = time.time()
        s = {"sid": secrets.token_urlsafe(32), "csrf": secrets.token_urlsafe(24), "user": user,
             "t0": now, "seen": now, "stamp": stamp}
        with self._lock:
            self._gc(now)
            self._s[s["sid"]] = s
        return s

    def get(self, sid, stamp=None):
        if not sid:
            return None
        now = time.time()
        with self._lock:
            s = self._s.get(sid)
            if s is None:
                return None
            if now - s["t0"] > self.ttl or now - s["seen"] > self.idle or (stamp is not None and s["stamp"] != stamp):
                self._s.pop(sid, None)
                return None
            s["seen"] = now
            return s

    def drop(self, sid):
        with self._lock:
            self._s.pop(sid, None)

    def _gc(self, now):
        for k in [k for k, s in self._s.items() if now - s["t0"] > self.ttl or now - s["seen"] > self.idle]:
            del self._s[k]


class Throttle:
    """Пауза после неудачных входов: первые `free` попыток с адреса — без паузы,
    дальше блок на base·2^k секунд (не больше cap). Удачный вход сбрасывает счёт."""

    def __init__(self, free=5, base=30, cap=900, window=3600):
        self.free, self.base, self.cap, self.window = free, base, cap, window
        self._f = {}               # ip -> [число неудач, время последней]
        self._lock = threading.Lock()

    def wait(self, ip):
        now = time.time()
        with self._lock:
            n, last = self._f.get(ip, (0, 0))
            if now - last > self.window:
                self._f.pop(ip, None)
                return 0
            if n < self.free:
                return 0
            block = min(self.base * 2 ** (n - self.free), self.cap)
            return max(0, int(last + block - now + 0.999))

    def failed(self, ip):
        now = time.time()
        with self._lock:
            n, last = self._f.get(ip, (0, 0))
            if now - last > self.window:
                n = 0
            self._f[ip] = (n + 1, now)
            if len(self._f) > 10000:            # не копить память от перебора с тысяч адресов
                for k in sorted(self._f, key=lambda k: self._f[k][1])[:5000]:
                    del self._f[k]

    def passed(self, ip):
        with self._lock:
            self._f.pop(ip, None)
