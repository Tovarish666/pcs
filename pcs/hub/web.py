"""Веб-панель со стороны хаба: юнит pcs-web.service, вход (/etc/pcs/web.json), запуск.

web.json (600): {"user": "admin", "algo": "pbkdf2_sha256", "iterations": 600000,
                 "salt": "<hex>", "hash": "<hex>", "updated": "…"}
Проверка входа — verify(user, password); саму панель пишет pcs.web.
"""
import hashlib
import hmac
import os
import re
import secrets
import time

from ..core import util
from ..core.util import Fail, jload, jsave, sh
from . import remote, store

PORT = 666
UNIT = "/etc/systemd/system/pcs-web.service"
ITER = 600000
UNIT_TEXT = """[Unit]
Description=PCS: веб-панель хаба (порт %d)
Wants=network-online.target
After=network-online.target

[Service]
ExecStart=%s web serve
Restart=on-failure
RestartSec=3

[Install]
WantedBy=multi-user.target
"""


def creds_path():
    return os.path.join(store.ETC, "web.json")


def hash_pw(password, salt=None, iterations=ITER):
    salt = salt or secrets.token_hex(16)
    h = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), iterations).hex()
    return {"algo": "pbkdf2_sha256", "iterations": iterations, "salt": salt, "hash": h}


def set_login(user, password):
    if not re.fullmatch(r"[A-Za-z0-9_.@-]{1,64}", user or ""):
        raise Fail("логин — латиница, цифры, _.@- (до 64 символов)")
    if len(password or "") < 8:
        raise Fail("пароль панели — не короче 8 символов")
    if re.search(r"[\r\n\x00]", password):
        raise Fail("в пароле перевод строки — так нельзя")
    d = dict(hash_pw(password), user=user, updated=time.strftime("%F %T"))
    os.makedirs(store.ETC, mode=0o700, exist_ok=True)
    jsave(creds_path(), d, 0o600)
    return {"user": user}


def verify(user, password):
    d = jload(creds_path(), {})
    if not d.get("hash") or d.get("algo") != "pbkdf2_sha256":
        return False
    h = hash_pw(password or "", d["salt"], int(d.get("iterations") or ITER))["hash"]
    return hmac.compare_digest(user or "", d.get("user", "")) & hmac.compare_digest(h, d["hash"])


def status():
    act = sh("systemctl", "is-active", "pcs-web.service", check=False).stdout.strip() or "unknown"
    en = sh("systemctl", "is-enabled", "pcs-web.service", check=False).stdout.strip() or "disabled"
    d = jload(creds_path(), {})
    return {"active": act == "active", "state": act, "enabled": en == "enabled", "port": PORT,
            "user": d.get("user"), "login_set": bool(d.get("hash"))}


def ready():
    import importlib.util
    try:
        return importlib.util.find_spec("pcs.web.server") is not None
    except ImportError:
        return False


def on():
    util.need_root()
    if not jload(creds_path(), {}).get("hash"):
        raise Fail("сначала логин и пароль панели: pcs web passwd")
    if not ready():
        raise Fail("веб-панели в этой версии PCS ещё нет (pcs.web.server) — включится после pcs update --host")
    text = UNIT_TEXT % (PORT, os.path.join(remote.ROOT, "bin", "pcs"))
    if util.rd(UNIT) != text.strip():
        util.wr(UNIT, text)
        sh("systemctl", "daemon-reload")
    sh("systemctl", "enable", "pcs-web.service", check=False)
    sh("systemctl", "restart", "pcs-web.service")
    return status()


def off():
    util.need_root()
    sh("systemctl", "disable", "--now", "pcs-web.service", check=False)
    return status()


def serve(argv):
    if not ready():
        raise Fail("веб-панели в этой версии ещё нет (pcs.web.server) — обнови PCS: pcs update --host")
    try:
        from ..web import server
    except ImportError as e:
        raise Fail("веб-панель не загрузилась: %s" % e)
    if not hasattr(server, "main"):
        raise Fail("pcs.web.server без main() — веб-панель не готова")
    return server.main(argv)
