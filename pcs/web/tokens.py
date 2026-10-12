"""Токены для программ (хаб другого хоста): Bearer к /api/v1/*, только по HTTPS.

Токен выдаётся при присоединении (POST /api/v1/join, логин и пароль панели) и хранится
у хаба; здесь — только SHA-256 токена. Отозвать: `pcs token revoke ID` или отсоединение.

    /etc/pcs/tokens.json  {"tokens": [{"id", "name", "hash", "created", "last", "from"}]}  (600)
"""
import hashlib
import hmac
import os
import secrets
import threading
import time

from pcs.core.util import Fail, jload, jsave

PATH = os.environ.get("PCS_TOKENS", "/etc/pcs/tokens.json")
PREFIX = "pcs_"
_lock = threading.Lock()
_touch = {}                       # id → когда последний раз писали «last» (не чаще раза в минуту)


def _load(path):
    d = jload(path, {})
    return d if isinstance(d, dict) and isinstance(d.get("tokens"), list) else {"tokens": []}


def _hash(token):
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def issue(name, ip="", path=None):
    """Новый токен. → (id, токен) — токен показывается один раз, здесь хранится только хэш."""
    path = path or PATH
    token = PREFIX + secrets.token_urlsafe(32)
    tid = "t_" + secrets.token_hex(3)
    with _lock:
        d = _load(path)
        d["tokens"].append({"id": tid, "name": str(name)[:80], "hash": _hash(token), "created": int(time.time()),
                            "last": None, "from": ip})
        jsave(path, d, 0o600)
    return tid, token


def check(token, ip="", path=None):
    """Запись токена или None. Сравнение — по хэшу, без утечки по времени."""
    path = path or PATH
    if not isinstance(token, str) or not token.startswith(PREFIX) or len(token) > 200:
        return None
    h = _hash(token)
    found = None
    for rec in _load(path)["tokens"]:
        if hmac.compare_digest(str(rec.get("hash", "")), h):
            found = rec
    if found and time.time() - _touch.get(found["id"], 0) > 60:
        _touch[found["id"]] = time.time()
        with _lock:
            d = _load(path)
            for rec in d["tokens"]:
                if rec.get("id") == found["id"]:
                    rec["last"], rec["last_from"] = int(time.time()), ip
            jsave(path, d, 0o600)
    return found


def listing(path=None):
    return [{k: v for k, v in r.items() if k != "hash"} for r in _load(path or PATH)["tokens"]]


def revoke(tid, path=None):
    path = path or PATH
    with _lock:
        d = _load(path)
        left = [r for r in d["tokens"] if r.get("id") != tid]
        if len(left) == len(d["tokens"]):
            raise Fail("токена %s нет — список: pcs token list" % tid)
        d["tokens"] = left
        jsave(path, d, 0o600)
    return {"revoked": tid}
