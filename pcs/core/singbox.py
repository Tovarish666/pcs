"""Единственный sing-box PCS — одна версия на proxyveth и modlink.

Лежит в /usr/local/lib/pcs/sing-box. /usr/local/bin/sing-box принадлежит кому
угодно (modlink-linux, proxyveth-virt) — его не читаем, не ставим, не удаляем.

Конфиги — только в формате 1.12+ (sing-box 1.14 старый не принимает вовсе):
  • DNS-серверы: {"type": "tcp"|"udp"|"local"…, "server": …}, не "address": "tcp://…";
  • никаких служебных outbound dns/block — вместо них route-действия
    {"action": "hijack-dns"}, {"action": "reject"};
  • никаких sniff* на inbound — {"action": "sniff"} в route.rules, если нужно.
Каждый генератор конфига проверяется в тестах настоящим `sing-box check`
(tests/singbox_check.py) — версия та же, что ставится на серверы.
"""
import hashlib
import io
import json
import os
import platform
import tarfile
import tempfile
import urllib.request

from .util import Fail, sh

VERSION = "1.14.2"
SHA256 = {                       # контрольные суммы архивов релиза (совпадают с digest на GitHub)
    "linux-amd64": "a684484d7477d1437282ee411f4d131d0340aaad60a7868841ebd5d87dd8a0c6",
    "darwin-arm64": "925c5382eca8492b0150f868a6db20b18290a38700e621724b3703fd453e032d",   # только для тестов
}
BIN = "/usr/local/lib/pcs/sing-box"


def arch():
    m = platform.machine().lower()
    s = platform.system().lower()
    return "%s-%s" % (s, {"x86_64": "amd64", "aarch64": "arm64", "arm64": "arm64"}.get(m, m))


def url(a=None):
    a = a or arch()
    return "https://github.com/SagerNet/sing-box/releases/download/v%s/sing-box-%s-%s.tar.gz" % (VERSION, VERSION, a)


def installed(path=BIN):
    """Стоит ли ровно наша версия (а не просто «какой-то sing-box»)."""
    if not os.path.exists(path):
        return False
    r = sh(path, "version", check=False)
    return r.returncode == 0 and r.stdout.startswith("sing-box version %s\n" % VERSION)


def install(path=BIN, a=None):
    """Скачать, сверить сумму, положить атомарно. Повторный вызов — ничего не делает."""
    a = a or arch()
    if installed(path):
        return False
    if a not in SHA256:
        raise Fail("sing-box %s: нет контрольной суммы под %s" % (VERSION, a))
    data = urllib.request.urlopen(url(a), timeout=120).read()
    if hashlib.sha256(data).hexdigest() != SHA256[a]:
        raise Fail("sing-box %s: контрольная сумма не совпала — не ставлю" % VERSION)
    with tarfile.open(fileobj=io.BytesIO(data)) as t:
        m = next(x for x in t.getmembers() if x.name.endswith("/sing-box"))
        body = t.extractfile(m).read()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path + ".tmp", "wb") as f:
        f.write(body)
    os.chmod(path + ".tmp", 0o755)
    os.replace(path + ".tmp", path)
    return True


def check(config, binary=BIN):
    """`sing-box check` на конфиге-словаре → (True, "") или (False, текст ошибки)."""
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
        json.dump(config, f)
    try:
        r = sh(binary, "check", "-c", f.name, check=False)
    finally:
        os.unlink(f.name)
    out = (r.stderr + r.stdout).strip()
    bad = r.returncode != 0 or "deprecated" in out.lower() or "FATAL" in out or "ERROR" in out
    return (not bad), out
