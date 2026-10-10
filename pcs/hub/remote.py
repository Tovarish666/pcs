"""Хаб ↔ сервер по SSH (§9): ключ хаба, команды частей с --json, доставка кода.

Все вызовы ssh с захватом вывода идут через call() — его и подменяют тесты.
"""
import glob
import io
import json
import os
import re
import shlex
import shutil
import subprocess
import tarfile
import tempfile
import time

from .. import VERSION
from ..core.util import Fail, sh
from . import store, ui

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.realpath(__file__))))
RUN = os.environ.get("PCS_RUN", "/run/pcs")
PARTS = ("proxyveth", "modlink", "hivelink")
SERVER_BIN = ("proxyveth", "modlink", "hivelink", "pcs-node")
SKIP = {".git", "tests", "__pycache__", ".source", ".DS_Store", ".idea", ".vscode"}


def ensure_key():
    k = store.key_path()
    if not os.path.exists(k):
        os.makedirs(os.path.dirname(k), mode=0o700, exist_ok=True)
        sh("ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", "pcs@%s" % os.uname().nodename, "-f", k)
    with open(k + ".pub") as f:
        return f.read().strip()


def key_of(s):
    return s.get("key") or store.key_path()


def base(s, tty=False, key=None):
    """argv ssh до сервера. С явным key — без общего мастер-соединения: вход проверяется честно."""
    os.makedirs(RUN, mode=0o700, exist_ok=True)
    master = ["-o", "ControlMaster=no", "-o", "ControlPath=none"] if key else \
        ["-o", "ControlMaster=auto", "-o", "ControlPath=%s/cm-%%C" % RUN, "-o", "ControlPersist=120"]
    return ["ssh", "-i", key or key_of(s), "-p", str(s.get("port") or 22),
            "-o", "BatchMode=yes", "-o", "IdentitiesOnly=yes", "-o", "StrictHostKeyChecking=accept-new",
            "-o", "UserKnownHostsFile=" + store.known_hosts(), "-o", "ConnectTimeout=10",
            "-o", "ServerAliveInterval=15"] + master + \
        ["-o", "LogLevel=ERROR"] + (["-tt"] if tty else []) + ["root@" + s["ip"]]


def call(s, cmd, input=None, timeout=None, key=None):
    """Выполнить cmd на сервере. → CompletedProcess (text). Сеть/ключ — Fail."""
    if not s.get("ip"):
        raise Fail("у сервера %s нет адреса: pcs server adopt %s --ip IP" % (s.get("id"), s.get("id")))
    argv = base(s, key=key) + [cmd]
    try:
        r = subprocess.run(argv, capture_output=True, text=True, input=input, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise Fail("сервер %s (%s): «%s» не уложилось в %s с" % (s["id"], s["ip"], cmd[:60], timeout))
    except FileNotFoundError:
        raise Fail("нет программы ssh")
    if r.returncode == 255 and not r.stdout:
        raise Fail("нет SSH до сервера %s (%s): %s" % (s["id"], s["ip"], (r.stderr.strip().splitlines() or ["?"])[-1]))
    return r


def ssh(s, cmd, check=True, timeout=None, input=None):
    r = call(s, cmd, input=input, timeout=timeout)
    if check and r.returncode != 0:
        raise Fail("сервер %s: %s: %s" % (s["id"], cmd.split()[0] if cmd.split() else cmd,
                                          (r.stderr or r.stdout).strip()[-300:] or "код %d" % r.returncode))
    return r


def stream(s, cmd, tty=False):
    """С живым выводом (человеку или в журнал задания). → код возврата."""
    if not s.get("ip"):
        raise Fail("у сервера %s нет адреса" % s.get("id"))
    return subprocess.call(base(s, tty=tty) + [cmd])


def alive(s, timeout=30, key=None):
    try:
        return call(s, "true", timeout=timeout, key=key).returncode == 0
    except Fail:
        return False


def forget(ip):
    if os.path.exists(store.known_hosts()):
        sh("ssh-keygen", "-f", store.known_hosts(), "-R", ip, check=False)
    drop_masters()


def drop_masters():
    for f in glob.glob(os.path.join(RUN, "cm-*")):
        try:
            os.unlink(f)
        except OSError:
            pass


def wait(s, limit=600, what="SSH"):
    t0, said = time.time(), 0
    while time.time() - t0 < limit:
        if alive(s, timeout=20):
            return time.time() - t0
        if int(time.time() - t0) // 60 > said:
            said = int(time.time() - t0) // 60
            ui.step("…жду %s на %s (%d мин)" % (what, s["ip"], said))
        time.sleep(5)
    raise Fail("%s на %s не поднялся за %d с" % (what, s["ip"], limit))


def boot_id(s):
    try:
        return call(s, "cat /proc/sys/kernel/random/boot_id", timeout=20).stdout.strip()
    except Fail:
        return ""


def wait_reboot(s, old_boot, limit=600):
    t0 = time.time()
    while time.time() - t0 < limit:
        time.sleep(5)
        b = boot_id(s)
        if b and b != old_boot:
            return True
    raise Fail("сервер %s не вернулся после перезагрузки за %d мин (qm terminal %s)" % (s["id"], limit // 60, s["id"]))


def with_password(ip, password, cmd, port=22):
    """Один раз зайти по паролю (положить ключ) — через SSH_ASKPASS, без sshpass.
    Пароль идёт через окружение askpass-скрипта, не через командную строку."""
    d = tempfile.mkdtemp()
    ap = os.path.join(d, "askpass")
    with open(ap, "w") as f:
        f.write('#!/bin/sh\nprintf "%s\\n" "$PCS_PASS"\n')
    os.chmod(ap, 0o700)
    env = dict(os.environ, SSH_ASKPASS=ap, SSH_ASKPASS_REQUIRE="force", DISPLAY=":0", PCS_PASS=password)
    try:
        return subprocess.run(["ssh", "-p", str(port), "-o", "StrictHostKeyChecking=accept-new",
                               "-o", "UserKnownHostsFile=" + store.known_hosts(),
                               "-o", "PreferredAuthentications=password,keyboard-interactive",
                               "-o", "PubkeyAuthentication=no", "-o", "ConnectTimeout=10",
                               "-o", "NumberOfPasswordPrompts=1", "root@" + ip, cmd],
                              env=env, capture_output=True, text=True, timeout=60)
    finally:
        shutil.rmtree(d, ignore_errors=True)


# ── конверт --json ─────────────────────────────────────────────────────────
def envelope(out, what="команда"):
    """Вывод `… --json` → {"ok", "data"|"error"}. Лишние строки до конверта не мешают."""
    text = (out or "").strip()
    for cand in [text] + [ln for ln in reversed(text.splitlines()) if ln.startswith("{")]:
        try:
            d = json.loads(cand)
        except ValueError:
            continue
        if isinstance(d, dict) and "ok" in d:
            return d
    return {"ok": False, "error": "%s ответила не JSON: %s" % (what, (text.splitlines() or ["пусто"])[-1][:200])}


def part(s, name, args, timeout=300, input=None):
    """`<часть> <args> --json` на сервере. → конверт как есть."""
    args = [str(a) for a in args]
    if "--json" not in args:
        args.append("--json")
    cmd = " ".join(shlex.quote(x) for x in [name] + args)
    r = call(s, cmd, timeout=timeout, input=input)
    if r.returncode == 127 or "command not found" in r.stderr:
        return {"ok": False, "error": "на сервере %s нет %s — обнови код: pcs update --server %s" % (s["id"], name, s["id"])}
    env = envelope(r.stdout, name)
    if not env.get("ok") and "не JSON" in env.get("error", "") and r.stderr.strip():
        env["error"] = r.stderr.strip().splitlines()[-1][:300]
    return env


def node(s, *args, input=None, timeout=60):
    """pcs-node на сервере. → data; ошибка — Fail."""
    env = part(s, "pcs-node", list(args), timeout=timeout, input=input)
    if not env.get("ok"):
        raise Fail("сервер %s: %s" % (s["id"], env.get("error") or "pcs-node: ошибка"))
    return env.get("data")


def local_part(name, args, timeout=300, input=None):
    """Часть на самом хосте (/opt/pcs/bin/<часть>). → конверт."""
    args = [str(a) for a in args]
    if "--json" not in args:
        args.append("--json")
    exe = os.path.join(ROOT, "bin", name)
    try:
        r = subprocess.run([exe] + args, capture_output=True, text=True, timeout=timeout, input=input)
    except (OSError, subprocess.TimeoutExpired) as e:
        return {"ok": False, "error": "%s: %s" % (name, e)}
    env = envelope(r.stdout, name)
    if not env.get("ok") and "не JSON" in env.get("error", "") and r.stderr.strip():
        env["error"] = r.stderr.strip().splitlines()[-1].lstrip(" ✗")[:300]
    return env


# ── доставка кода ──────────────────────────────────────────────────────────
def tarball(root=None):
    """Дерево /opt/pcs без .git и тестов — tar.gz в памяти."""
    root = root or ROOT
    buf = io.BytesIO()

    def skip(ti):
        name = os.path.basename(ti.name)
        if name in SKIP or name.endswith((".pyc", ".orig", ".rej", "~")):
            return None
        ti.uid = ti.gid = 0
        ti.uname = ti.gname = "root"
        return ti
    with tarfile.open(fileobj=buf, mode="w:gz") as t:
        for item in sorted(os.listdir(root)):
            if item in SKIP or item.endswith((".prev", ".new")):
                continue
            t.add(os.path.join(root, item), arcname=item, filter=skip)
    return buf.getvalue()


INSTALL = r"""set -e
rm -rf /opt/pcs.new && mkdir -p /opt/pcs.new
tar -xzf - -C /opt/pcs.new
python3 -m compileall -q /opt/pcs.new/pcs >/dev/null || { echo "код не компилируется — оставляю прежний" >&2; exit 3; }
chmod 755 /opt/pcs.new/bin/*
rm -rf /opt/pcs.prev
if [ -d /opt/pcs ]; then mv /opt/pcs /opt/pcs.prev; fi
mv /opt/pcs.new /opt/pcs
for c in %(bins)s; do ln -sfn /opt/pcs/bin/$c /usr/local/bin/$c; done
python3 -c 'import sys; sys.path.insert(0, "/opt/pcs"); import pcs; print(pcs.VERSION)'
""" % {"bins": " ".join(SERVER_BIN)}


def deliver(s, root=None):
    """Положить /opt/pcs на сервер и симлинки команд. → версия на сервере."""
    data = tarball(root)
    if not s.get("ip"):
        raise Fail("у сервера %s нет адреса" % s.get("id"))
    try:
        r = subprocess.run(base(s) + [INSTALL], input=data, capture_output=True, timeout=300)
    except subprocess.TimeoutExpired:
        raise Fail("сервер %s: код не доставился за 5 мин" % s["id"])
    out, err = r.stdout.decode(errors="replace").strip(), r.stderr.decode(errors="replace").strip()
    if r.returncode != 0:
        raise Fail("сервер %s: код не доставлен: %s" % (s["id"], (err or out or "код %d" % r.returncode)[-300:]))
    ver = out.splitlines()[-1] if out else "?"
    if ver != VERSION:
        ui.warn("на сервере %s версия %s, на хосте %s" % (s["id"], ver, VERSION))
    return ver


def detached(s, unit, cmd, limit=3 * 3600):
    """Долгое на сервере — отвязанно от SSH (сеть может моргнуть), вывод — вживую.
    → код возврата; None — сервер перезагрузился сам (значит, дошло до конца)."""
    boot = boot_id(s)
    rc_file = "/var/lib/pcs/%s.rc" % unit
    ssh(s, "mkdir -p /var/lib/pcs; systemctl reset-failed %s 2>/dev/null; rm -f %s; "
           "systemd-run --unit=%s --collect sh -c %s"
        % (unit, rc_file, unit, shlex.quote("%s; echo $? > %s" % (cmd, rc_file))))
    follow = subprocess.Popen(base(s) + ["journalctl -u %s -f -o cat -n all" % unit])
    rc, t0 = None, time.time()
    try:
        while time.time() - t0 < limit:
            time.sleep(5)
            try:
                r = call(s, "cat /proc/sys/kernel/random/boot_id; cat %s 2>/dev/null || echo -; systemctl is-active %s"
                         % (rc_file, unit), timeout=20)
            except Fail:
                continue                     # сеть моргнула или сервер перезагружается
            lines = [x.strip() for x in r.stdout.split("\n")]
            if lines[0] and lines[0] != boot:
                break
            if len(lines) > 1 and re.fullmatch(r"-?\d+", lines[1]):
                rc = int(lines[1])
                break
            if len(lines) > 2 and lines[2] in ("inactive", "failed"):
                time.sleep(3)                # код мог дописаться чуть позже
                last = call(s, "cat %s 2>/dev/null" % rc_file, timeout=20).stdout.strip()
                if re.fullmatch(r"-?\d+", last):
                    rc = int(last)
                    break
                raise Fail("сервер %s: %s оборвалось без кода — смотри journalctl -u %s" % (s["id"], unit, unit))
        else:
            raise Fail("сервер %s: %s идёт дольше %d ч — смотри journalctl -u %s" % (s["id"], unit, limit // 3600, unit))
    finally:
        time.sleep(1)
        follow.terminate()
    return rc
