"""3proxy — движок прокси modlink (с 13.10.2026; раньше modlink был на sing-box).

Почему 3proxy: сервис `auto` — HTTP CONNECT и SOCKS5 (с UDP) на одном порту, процесс ~9 МБ
против ~55 МБ у sing-box, бинарник 330 КБ. Так же сделан modlink для Windows (ветка app-v2).

Лежит в /usr/local/lib/pcs/3proxy. Ставится из официального .deb релиза (сверка SHA-256);
из пакета берём только бинарник — его юнит, /etc/3proxy и init.d не нужны: у modlink свой
юнит и свой конфиг. Нужны libssl3, libpcre2, zlib, zstd — есть в Debian 13 и Ubuntu 24.04.
Версию 3proxy не печатает — какую поставили, помнит файл рядом (<BIN>.version).
"""
import hashlib
import os
import platform
import shutil
import tempfile
import urllib.request

from .util import Fail, rd, sh

VERSION = "1.0.0"
SHA256 = {                        # из SHA256SUMS-x86_64 / SHA256SUMS-arm64 релиза
    "amd64": "a27525c24a7240895d5de8d5fa6b7489638a2b98aa1cee81cf1030163ab3d92b",
    "arm64": "dc2f217251d1f25514b69aa8310cebd89a9070950745326b05ac7908f4fd7a41",
}
DEB = {"amd64": "x86_64", "arm64": "arm64"}
BIN = "/usr/local/lib/pcs/3proxy"


def arch():
    m = platform.machine().lower()
    return {"x86_64": "amd64", "amd64": "amd64", "aarch64": "arm64", "arm64": "arm64"}.get(m, m)


def url(a=None):
    a = a or arch()
    return "https://github.com/3proxy/3proxy/releases/download/%s/3proxy-%s.%s.deb" % (VERSION, VERSION, DEB[a])


def installed(path=BIN):
    return os.path.exists(path) and rd(path + ".version").strip() == VERSION


def install(path=BIN, a=None):
    """Скачать .deb, сверить сумму, вынуть bin/3proxy, положить атомарно. Уже стоит — ничего."""
    a = a or arch()
    if installed(path):
        return False
    if a not in SHA256:
        raise Fail("3proxy %s: нет сборки под %s" % (VERSION, a))
    try:
        data = urllib.request.urlopen(url(a), timeout=120).read()
    except OSError as e:
        raise Fail("3proxy %s не скачался (%s) — проверь доступ к github.com" % (VERSION, e))
    if hashlib.sha256(data).hexdigest() != SHA256[a]:
        raise Fail("3proxy %s: контрольная сумма не совпала — не ставлю" % VERSION)
    d = tempfile.mkdtemp()
    try:
        deb = os.path.join(d, "3proxy.deb")
        with open(deb, "wb") as f:
            f.write(data)
        r = sh("dpkg-deb", "-x", deb, os.path.join(d, "x"), check=False)
        src = os.path.join(d, "x", "bin", "3proxy")
        if r.returncode != 0 or not os.path.exists(src):
            raise Fail("3proxy %s: в пакете нет bin/3proxy (%s)" % (VERSION, (r.stderr or "").strip()[-200:]))
        os.makedirs(os.path.dirname(path), exist_ok=True)
        shutil.copyfile(src, path + ".tmp")
        os.chmod(path + ".tmp", 0o755)
        os.replace(path + ".tmp", path)
        with open(path + ".version", "w") as f:
            f.write(VERSION + "\n")
    finally:
        shutil.rmtree(d, ignore_errors=True)
    miss = [ln.split()[0] for ln in sh("ldd", path, check=False).stdout.splitlines() if "not found" in ln]
    if miss:
        raise Fail("3proxy поставлен, но не хватает библиотек: %s — apt install libssl3 libpcre2-8-0 zlib1g libzstd1"
                   % ", ".join(miss))
    return True
