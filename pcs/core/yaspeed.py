"""yaspeed — тест скорости через Яндекс.Интернетометр (github.com/Tovarish666/yaspeed).

Для modlink: скорость модема с его же адреса (--source-ip lan_ip — тот же путь, что у прокси).
Ставится по первому запросу в /usr/local/lib/pcs/yaspeed (сверка SHA-256), ~25 МБ.
"""
import hashlib
import json
import os
import urllib.request

from .util import Fail, rd, sh

VERSION = "1.0.1"
SHA256 = {"amd64": "44ae2098585f41cc84ac03ffb291bc318bda4b4f854059a4c1c06e0c1a8a2df0"}
BIN = "/usr/local/lib/pcs/yaspeed"
URL = "https://github.com/Tovarish666/yaspeed/releases/download/v%s/yaspeed-linux" % VERSION


def installed(path=BIN):
    return os.path.exists(path) and rd(path + ".version").strip() == VERSION


def install(path=BIN, a="amd64"):
    if installed(path):
        return False
    if a not in SHA256:
        raise Fail("yaspeed %s: сборка есть только под amd64" % VERSION)
    try:
        data = urllib.request.urlopen(URL, timeout=180).read()
    except OSError as e:
        raise Fail("yaspeed не скачался (%s) — проверь доступ к github.com" % e)
    if hashlib.sha256(data).hexdigest() != SHA256[a]:
        raise Fail("yaspeed %s: контрольная сумма не совпала — не ставлю" % VERSION)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path + ".tmp", "wb") as f:
        f.write(data)
    os.chmod(path + ".tmp", 0o755)
    os.replace(path + ".tmp", path)
    with open(path + ".version", "w") as f:
        f.write(VERSION + "\n")
    return True


def run(source_ip, duration=8, threads=4, path=BIN):
    """→ {"down", "up", "ping", "ip", "server"} — Мбит/с и мс. Через адрес source_ip."""
    from .threeproxy import arch
    install(path, arch())
    r = sh(path, "--source-ip", source_ip, "--json", "--duration", str(duration), "--threads", str(threads),
           check=False, timeout=duration * 2 + 90)
    try:
        d = json.loads(r.stdout[r.stdout.index("{"):])
    except ValueError:
        raise Fail("тест скорости не вышел: %s" % ((r.stderr or r.stdout).strip()[-200:] or "код %d" % r.returncode))
    return {"down": round(float(d.get("download_mbps") or 0), 2), "up": round(float(d.get("upload_mbps") or 0), 2),
            "ping": round(float(d.get("ping_ms") or 0), 1), "ip": d.get("ip") or "", "server": d.get("server") or ""}
