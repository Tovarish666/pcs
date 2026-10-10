#!/usr/bin/env python3
"""pcs-node (служебная команда хаба на сервере) на временном корне, без root.

    python3 tests/test_hub_node.py
"""
import base64
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import types

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.join(HERE, os.pardir)
sys.path.insert(0, ROOT)
from pcs import VERSION  # noqa: E402
from pcs.core.util import Fail  # noqa: E402
from pcs.hub import node  # noqa: E402

passed = failed = 0


def check(name, cond, detail=""):
    global passed, failed
    if cond:
        passed += 1
        print("  ok   %s" % name)
    else:
        failed += 1
        print("  FAIL %s%s" % (name, ("\n       " + str(detail)) if detail else ""))


def put(root, rel, text=""):
    p = os.path.join(root, rel)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w") as f:
        f.write(text)


ROWS = [{"n": 201, "state": "ok"}, {"n": 202, "state": "ok"}, {"n": 203, "state": "warn"},
        {"n": 204, "state": "broken"}, {"n": 205, "state": "absent"}, {"n": 206, "state": "disabled"},
        {"n": 207, "state": "rebooting"}]


class World:
    def __init__(self, status=None):
        self.calls = []
        self.status = status if status is not None else json.dumps({"ok": True, "data": ROWS})

    def run(self, *cmd, check=True, timeout=None, input=None):
        self.calls.append((cmd, input))
        out = ""
        if cmd[:2] == ("proxyveth", "status"):
            out = self.status
        elif cmd[:2] == ("systemctl", "is-active"):
            out = "\n".join("active" if u != "modlink-sb" else "failed" for u in cmd[2:])
        elif cmd[:2] == ("systemctl", "is-enabled"):
            out = "enabled\n"
        elif cmd[:2] == ("ip", "-4"):
            out = "1.1.1.1 via 192.168.88.1 dev eth0 src 192.168.88.7 uid 0\n"
        elif cmd[0] == "curl":
            out = "94.25.229.40"
        return types.SimpleNamespace(returncode=0, stdout=out, stderr="")


print("сводка модемов:")
check("Row → ok/warn/broken/total, выключенные не в счёт", node.summarize(ROWS)
      == {"ok": 2, "warn": 2, "broken": 2, "total": 6}, node.summarize(ROWS))
check("данные списком или {rows: …}", node.rows_of({"rows": ROWS}) == ROWS and node.rows_of(ROWS) == ROWS
      and node.rows_of(None) == [])

print("pcs-node info:")
root = tempfile.mkdtemp()
put(root, "usr/local/bin/proxyveth")
put(root, "usr/local/sbin/vmodem")
put(root, "etc/proxyveth/config.json", '{"mode": "usb", "source": "https://x"}')
put(root, "usr/local/bin/modem-interface-setup.sh", "#!/bin/sh\n")
put(root, "home/nodejs/work/auth.mp", '{"auth": "AAA:BBB", "port": 1800}')
put(root, "etc/modlink/proxies.json", "[]")
put(root, "proc/meminfo", "MemTotal:        4000000 kB\nMemAvailable:    3000000 kB\n")
put(root, "proc/uptime", "12345.6 100.0\n")
put(root, "etc/systemd/resolved.conf.d/99-pcs.conf", "[Resolve]\nDNS=9.9.9.9 1.1.1.1\n")
w = World()
d = node.info(wan=True, root=root, run=w.run)
check("версия кода", d["version"] == VERSION)
check("режим proxyveth из /etc/proxyveth/config.json", d["proxyveth"]["mode"] == "usb" and d["proxyveth"]["installed"])
check("модемы из proxyveth status --json", d["proxyveth"]["modems"] == {"ok": 2, "warn": 2, "broken": 2, "total": 6}, d)
check("след vmodem виден (нужна миграция)", d["proxyveth"]["legacy"] == "vmodem")
check("mp.space: стоит, службы, порт, без ключа auth", d["mpspace"]["installed"] and d["mpspace"]["ok"]
      and d["mpspace"]["port"] == 1800 and d["mpspace"]["units"] == {"mproxy": "active", "nodejs-server": "active"}
      and "AAA" not in json.dumps(d), d["mpspace"])
check("modlink: юниты", d["modlink"]["installed"] and d["modlink"]["state"]["modlink-sb"] == "failed")
check("адрес, внешний IP, память, uptime, DNS", d["ip"] == "192.168.88.7" and d["wan"] == "94.25.229.40"
      and d["ram"] == {"total": 4096000000, "used": 1024000000} and d["uptime"] == 12345
      and d["dns"] == ["9.9.9.9", "1.1.1.1"], d)
check("всё сериализуется в JSON", json.loads(json.dumps(d))["version"] == VERSION)
w = World(status='{"ok": false, "error": "таблица не читается"}')
d = node.info(root=root, run=w.run)
check("proxyveth с ошибкой — ошибка в сводке, остальное есть", d["proxyveth"]["error"] == "таблица не читается"
      and d["mpspace"]["ok"] and "wan" not in d, d["proxyveth"])
os.unlink(os.path.join(root, "home/nodejs/work/auth.mp"))
d = node.info(root=root, run=World().run)
check("auth.mp нет — mp.space не в порядке", d["mpspace"]["installed"] and not d["mpspace"]["ok"])
shutil.rmtree(root)
root = tempfile.mkdtemp()
d = node.info(root=root, run=World().run)
check("чистый сервер: ничего не стоит, proxyveth не зовётся", not d["proxyveth"]["installed"]
      and d["proxyveth"]["mode"] is None and not d["mpspace"]["installed"] and d["dns"] == ["1.1.1.1", "8.8.8.8"])
shutil.rmtree(root)

print("ключи root:")
root = tempfile.mkdtemp()
blob = base64.b64encode(b"\x00\x00\x00\x0bssh-ed25519\x00\x00\x00\x20" + bytes(range(32))).decode()
pub = "ssh-ed25519 %s me@laptop" % blob
r = node.key_add(pub, root)
ak = os.path.join(root, "root/.ssh/authorized_keys")
check("ключ добавлен", r["added"] and r["fp"].startswith("SHA256:") and pub in open(ak).read(), r)
check("права: .ssh 700, authorized_keys 600", stat.S_IMODE(os.stat(ak).st_mode) == 0o600
      and stat.S_IMODE(os.stat(os.path.dirname(ak)).st_mode) == 0o700)
check("тот же ключ второй раз не дублируется", not node.key_add(pub + "\n", root)["added"]
      and open(ak).read().count(blob) == 1)
with open(ak, "a") as f:
    f.write('command="/bin/true",no-pty ssh-rsa %s other\n# комментарий\n' % base64.b64encode(b"x" * 40).decode())
ks = node.keys(root)
check("список: тип, отпечаток, комментарий; опции не мешают", [k["type"] for k in ks] == ["ssh-ed25519", "ssh-rsa"]
      and ks[0]["comment"] == "me@laptop", ks)
check("убрать по отпечатку", node.key_drop(ks[1]["fp"], root)["removed"] == 1 and len(node.keys(root)) == 1
      and "# комментарий" in open(ak).read())
check("убрать по открытому ключу", node.key_drop(pub, root)["removed"] == 1 and node.keys(root) == [])
for bad in ("", "ssh-ed25519", "ssh-ed25519 !!!не-base64", "%s\n%s" % (pub, pub), "привет"):
    try:
        node.key_add(bad, root)
        check("не ключ — отказ: %r" % bad[:20], False)
    except Fail:
        check("не ключ — отказ: %r" % bad[:20], True)
kg = shutil.which("ssh-keygen")
if kg:
    k = os.path.join(root, "k")
    subprocess.run([kg, "-q", "-t", "ed25519", "-N", "", "-C", "t", "-f", k], check=True)
    want = subprocess.run([kg, "-lf", k + ".pub"], capture_output=True, text=True).stdout.split()[1]
    got = node.fingerprint(node.parse_key(open(k + ".pub").read())[1])
    check("отпечаток совпадает с ssh-keygen -l", got == want, (got, want))
shutil.rmtree(root)

print("пароль root:")
w = World()
node.passwd("p@ss:wo'rd\"", run=w.run)
check("через chpasswd со stdin, не в командной строке", w.calls == [(("chpasswd",), "root:p@ss:wo'rd\"\n")], w.calls)
for bad in ("", "a\nb", "x" * 300):
    try:
        node.passwd(bad, run=w.run)
        check("брак пароля — отказ: %r" % bad[:8], False)
    except Fail:
        check("брак пароля — отказ: %r" % bad[:8], True)

print("порт SSH:")
root = tempfile.mkdtemp()
w = World()
d = node.ssh_port(2222, root=root, run=w.run)
sock = open(os.path.join(root, "etc/systemd/system/ssh.socket.d/10-pcs-port.conf")).read()
check("Ubuntu 24.04: ListenStream у ssh.socket (со сбросом)", "ListenStream=\nListenStream=2222" in sock
      and d["via"] == "ssh.socket", sock)
check("и Port в sshd_config.d — генератор не спорит",
      "Port 2222" in open(os.path.join(root, "etc/ssh/sshd_config.d/11-pcs-port.conf")).read())
check("перезапуск сокета отложен (не рвёт SSH, по которому пришла команда)",
      any(c[0][:2] == ("systemd-run", "--on-active=2") and "ssh.socket" in c[0] for c in w.calls), w.calls)
try:
    node.ssh_port(70000, root=root, run=w.run)
    check("порт вне 1–65535 — отказ", False)
except Fail:
    check("порт вне 1–65535 — отказ", True)
shutil.rmtree(root)

print("mp.space: auth.mp:")
root = tempfile.mkdtemp()
try:
    node.mp_auth('{"auth": "K:K", "port": 1800}', root=root, run=World().run)
    check("софта нет — понятная ошибка", False)
except Fail as e:
    check("софта нет — понятная ошибка", "pcs mpspace install" in str(e), e)
os.makedirs(os.path.join(root, "home/nodejs/work"))
w = World()
d = node.mp_auth('{"auth": "K1:K2", "port": "1800"}', root=root, run=w.run)
p = os.path.join(root, "home/nodejs/work/auth.mp")
check("auth.mp записан 600, порт числом", json.load(open(p)) == {"auth": "K1:K2", "port": 1800}
      and stat.S_IMODE(os.stat(p).st_mode) == 0o600 and d == {"port": 1800})
check("nodejs-server перезапущен", any(c[0] == ("systemctl", "restart", "nodejs-server") for c in w.calls))
d = node.mp_auth("K3:K4", root=root, run=w.run)
check("голый ключ — порт берётся из прежнего файла, прежний — в .bak", d["port"] == 1800
      and json.load(open(p))["auth"] == "K3:K4"
      and any(f.startswith("auth.mp.bak.") for f in os.listdir(os.path.dirname(p))))
for bad in ('{"auth": "a b", "port": 1}', '{"port": 1800}', "{не json", '{"auth": "K", "port": 99999}'):
    try:
        node.mp_auth(bad, root=root, run=w.run)
        check("брак auth.mp — отказ: %s" % bad, False)
    except Fail:
        check("брак auth.mp — отказ: %s" % bad, True)
shutil.rmtree(root)

print("командная строка pcs-node:")
pn = os.path.join(ROOT, "bin", "pcs-node")
r = subprocess.run([sys.executable, pn], capture_output=True, text=True)
check("без аргументов — справка", r.returncode == 0 and "pcs-node info" in r.stdout)
r = subprocess.run([sys.executable, pn, "version", "--json"], capture_output=True, text=True)
check("version --json — конверт", json.loads(r.stdout) == {"ok": True, "data": VERSION}, r.stdout)
if os.geteuid() != 0:
    r = subprocess.run([sys.executable, pn, "info", "--json"], capture_output=True, text=True)
    check("info --json без root — конверт с ошибкой", json.loads(r.stdout) == {"ok": False, "error": "нужен root"},
          r.stdout + r.stderr)

print("\nитого: ok %d, fail %d" % (passed, failed))
sys.exit(1 if failed else 0)
