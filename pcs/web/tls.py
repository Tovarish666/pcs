"""Свой сертификат панели: HTTPS на том же порту 666, что и HTTP.

Браузер по-прежнему открывает http://IP:666. Хосты PCS между собой ходят только по
https://IP:666 и сверяют отпечаток сертификата, запомненный при присоединении (TOFU):
подменить хост посредине нельзя, а пароль и токены не идут открытым текстом.

    cert, key = ensure()             # создать при первом запуске (openssl, RSA 2048, 10 лет)
    fingerprint_file(cert) -> "AB:CD:…"   (SHA-256 сертификата в DER)
"""
import hashlib
import os
import socket
import ssl
import subprocess

from pcs.core.util import Fail

DIR = os.environ.get("PCS_TLS_DIR", "/etc/pcs/tls")


def paths(d=None):
    d = d or DIR
    return os.path.join(d, "cert.pem"), os.path.join(d, "key.pem")


def ensure(d=None):
    """Сертификат и ключ — есть или создать. → (cert, key)."""
    cert, key = paths(d)
    if os.path.exists(cert) and os.path.exists(key):
        return cert, key
    os.makedirs(os.path.dirname(cert), mode=0o700, exist_ok=True)
    cn = "pcs-" + (socket.gethostname() or "host")
    old = os.umask(0o077)
    try:
        r = subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048",     # RSA — понимают все TLS
                            "-nodes", "-days", "3650", "-subj", "/CN=" + cn, "-keyout", key + ".tmp", "-out", cert + ".tmp"],
                           capture_output=True, text=True, timeout=60)
    finally:
        os.umask(old)
    if r.returncode != 0:
        raise Fail("сертификат панели не создался (openssl): %s" % (r.stderr.strip()[-200:] or r.returncode))
    os.replace(key + ".tmp", key)
    os.replace(cert + ".tmp", cert)
    return cert, key


def fingerprint_der(der):
    h = hashlib.sha256(der).hexdigest().upper()
    return ":".join(h[i:i + 2] for i in range(0, len(h), 2))


def fingerprint_file(cert):
    with open(cert) as f:
        return fingerprint_der(ssl.PEM_cert_to_DER_cert(f.read()))


def norm_fp(fp):
    """Отпечаток в любом виде (с двоеточиями, пробелами, строчными) → «AB:CD:…»; пусто → ""."""
    h = "".join(c for c in str(fp or "").upper() if c in "0123456789ABCDEF")
    if not h:
        return ""
    if len(h) != 64:
        raise Fail("отпечаток — 64 шестнадцатеричных знака (SHA-256), а тут %d" % len(h))
    return ":".join(h[i:i + 2] for i in range(0, 64, 2))


def server_context(d=None):
    cert, key = ensure(d)
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    ctx.load_cert_chain(cert, key)
    return ctx


def client_context():
    """Без проверки цепочки — проверяем сами отпечаток (свой сертификат, без CA)."""
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    return ctx
