"""pcs-node — служебная команда хаба на сервере (§9). Человеку не нужна: её зовёт хаб.

    pcs-node info [--wan] [--json]            версия кода, режим proxyveth, модемы, mp.space
    pcs-node dns-fix [--dns "1.1.1.1 8.8.8.8"] [--boot] [--json]
    pcs-node passwd [--json]                  пароль root — строкой со stdin
    pcs-node key [--add|--drop] [--json]      ключи root; ключ — строкой со stdin
    pcs-node ssh-port PORT [--json]           порт SSH (на Ubuntu 24.04 — через ssh.socket)
    pcs-node mpspace install --params FILE [--no-reboot]
    pcs-node mpspace auth|check [--json]      auth.mp — со stdin
    pcs-node version

Работает и на хосте: так хаб чинит DNS у себя (юнит pcs-dns.service).
"""
import argparse
import base64
import hashlib
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time

from .. import VERSION
from ..core import dns as dnsfix
from ..core import util
from ..core.util import Fail, jload, rd, sh

MP_SETUP = "usr/local/bin/modem-interface-setup.sh"
MP_WORK = "home/nodejs/work"
MP_UNITS = ("mproxy", "nodejs-server")
MP_HOSTS = ("mobileproxy.rent", "mobileproxy.space", "proxeon.net")
PV_CONFIG = "etc/proxyveth/config.json"
LOGD = "/var/log/pcs"
KEY_TYPES = ("ssh-ed25519", "ssh-rsa", "ssh-dss", "ecdsa-sha2-nistp256", "ecdsa-sha2-nistp384",
             "ecdsa-sha2-nistp521", "sk-ssh-ed25519@openssh.com", "sk-ecdsa-sha2-nistp256@openssh.com")


def at(root, rel):
    return os.path.join(root, rel)


def say(msg):
    if not util.QUIET[0]:
        print("  " + msg, flush=True)


# ── info ───────────────────────────────────────────────────────────────────
def summarize(rows):
    """Row proxyveth (§6) → {"ok","warn","broken","total"}; выключенные в таблице не считаются."""
    c = {"ok": 0, "warn": 0, "broken": 0, "total": 0}
    for r in rows or []:
        st = (r or {}).get("state") if isinstance(r, dict) else None
        if st == "disabled":
            continue
        c["total"] += 1
        if st == "ok":
            c["ok"] += 1
        elif st in ("warn", "rebooting"):
            c["warn"] += 1
        else:
            c["broken"] += 1
    return c


def rows_of(data):
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for k in ("rows", "modems"):
            if isinstance(data.get(k), list):
                return data[k]
    return []


def proxyveth_info(root="/", run=sh):
    cfg = jload(at(root, PV_CONFIG), {})
    mode = cfg.get("mode") if isinstance(cfg, dict) and cfg.get("mode") in ("usb", "gw") else None
    d = {"installed": os.path.exists(at(root, "usr/local/bin/proxyveth")), "mode": mode,
         "modems": summarize([]), "legacy": "vmodem" if os.path.exists(at(root, "usr/local/sbin/vmodem")) else None}
    if not d["installed"]:
        return d
    r = run("proxyveth", "status", "--json", check=False, timeout=60)
    env = envelope(r.stdout)
    if env.get("ok"):
        d["modems"] = summarize(rows_of(env.get("data")))
    else:
        d["error"] = env.get("error") or (r.stderr or "").strip()[-200:] or "proxyveth status: код %d" % r.returncode
    return d


def envelope(out):
    for cand in [(out or "").strip()] + [ln for ln in reversed((out or "").splitlines()) if ln.startswith("{")]:
        try:
            d = json.loads(cand)
            if isinstance(d, dict) and "ok" in d:
                return d
        except ValueError:
            pass
    return {"ok": False, "error": "ответ не JSON"}


def units_state(names, run=sh):
    out = run("systemctl", "is-active", *names, check=False).stdout.split()
    return {n: (out[i] if i < len(out) else "unknown") for i, n in enumerate(names)}


def mpspace_info(root="/", run=sh):
    installed = os.path.exists(at(root, MP_SETUP)) or os.path.isdir(at(root, MP_WORK))
    d = {"installed": installed, "units": {}, "port": None, "ok": False}
    if installed:
        d["units"] = units_state(MP_UNITS, run)
        auth = jload(at(root, MP_WORK + "/auth.mp"), {})
        d["port"] = auth.get("port") if isinstance(auth, dict) else None
        d["auth"] = bool(isinstance(auth, dict) and auth.get("auth"))
        d["ok"] = all(v == "active" for v in d["units"].values()) and d["auth"]
    return d


def primary_ip(run=sh):
    m = re.search(r"src (\S+)", run("ip", "-4", "route", "get", "1.1.1.1", check=False).stdout)
    return m.group(1) if m else ""


def wan_ip(run=sh):
    for url in ("https://api.ipify.org", "http://ip-api.com/line/?fields=query"):
        ip = run("curl", "-s", "-4", "-m", "8", url, check=False).stdout.strip()
        if re.fullmatch(r"\d+\.\d+\.\d+\.\d+", ip):
            return ip
    return ""


def mem(root="/"):
    m = {}
    for ln in (rd(at(root, "proc/meminfo")) or "").splitlines():
        k, _, v = ln.partition(":")
        if v.strip().split() and v.strip().split()[0].isdigit():
            m[k] = int(v.split()[0]) * 1024
    return {"total": m.get("MemTotal", 0), "used": m.get("MemTotal", 0) - m.get("MemAvailable", 0)}


def disk(path="/"):
    try:
        u = shutil.disk_usage(path)
        return {"total": u.total, "used": u.used}
    except OSError:
        return {"total": 0, "used": 0}


def info(wan=False, root="/", run=sh):
    up = rd(at(root, "proc/uptime")).split()
    d = {"version": VERSION, "hostname": socket.gethostname(), "ip": primary_ip(run),
         "uptime": int(float(up[0])) if up else 0, "kernel": os.uname().release,
         "cpu": {"cores": os.cpu_count() or 0, "load": list(os.getloadavg()) if hasattr(os, "getloadavg") else []},
         "ram": mem(root), "disk": disk(root),
         "proxyveth": proxyveth_info(root, run), "mpspace": mpspace_info(root, run),
         "modlink": {"installed": os.path.exists(at(root, "etc/modlink/proxies.json"))},
         "hivelink": {"installed": os.path.isdir(at(root, "etc/hivelink"))},
         "dns": dnsfix.current(root)}
    if d["modlink"]["installed"]:
        d["modlink"]["state"] = units_state(("modlink", "modlink-sb"), run)
    if wan:
        d["wan"] = wan_ip(run)
    return d


# ── пароль и ключи ─────────────────────────────────────────────────────────
def check_password(pw):
    if not pw:
        raise Fail("пустой пароль")
    if re.search(r"[\r\n\x00]", pw):
        raise Fail("в пароле перевод строки — так нельзя")
    if len(pw) > 256:
        raise Fail("пароль длиннее 256 символов")
    return pw


def passwd(pw, run=sh):
    check_password(pw)
    run("chpasswd", input="root:%s\n" % pw)
    return {"changed": True}


def parse_key(line):
    """Строка authorized_keys или .pub → (тип, тело, комментарий) или None."""
    f = (line or "").strip().split()
    for i, tok in enumerate(f):
        if tok in KEY_TYPES and i + 1 < len(f):
            blob = f[i + 1]
            try:
                raw = base64.b64decode(blob, validate=True)
            except ValueError:
                return None
            if len(raw) < 20:
                return None
            return tok, blob, " ".join(f[i + 2:])
    return None


def fingerprint(blob):
    return "SHA256:" + base64.b64encode(hashlib.sha256(base64.b64decode(blob)).digest()).decode().rstrip("=")


def auth_path(root="/"):
    return at(root, "root/.ssh/authorized_keys")


def keys(root="/"):
    out = []
    for ln in (rd(auth_path(root)) or "").splitlines():
        k = parse_key(ln)
        if k and not ln.lstrip().startswith("#"):
            out.append({"type": k[0], "fp": fingerprint(k[1]), "comment": k[2]})
    return out


def _write_keys(root, lines):
    p = auth_path(root)
    os.makedirs(os.path.dirname(p), mode=0o700, exist_ok=True)
    os.chmod(os.path.dirname(p), 0o700)
    tmp = p + ".pcs-tmp"
    with open(tmp, "w") as f:
        f.write("".join(ln + "\n" for ln in lines))
    os.chmod(tmp, 0o600)
    os.replace(tmp, p)


def key_add(pub, root="/"):
    lines = [ln for ln in (pub or "").strip().splitlines() if ln.strip()]
    if len(lines) != 1 or not parse_key(lines[0]):
        raise Fail("это не открытый ключ SSH (ожидается одна строка вида «ssh-ed25519 AAAA… комментарий»)")
    typ, blob, comment = parse_key(lines[0])
    have = (rd(auth_path(root)) or "").splitlines()
    if any((parse_key(ln) or (0, None))[1] == blob for ln in have):
        return {"added": False, "fp": fingerprint(blob)}
    _write_keys(root, have + ["%s %s%s" % (typ, blob, (" " + comment) if comment else "")])
    return {"added": True, "fp": fingerprint(blob)}


def key_drop(what, root="/"):
    """Убрать ключ: по открытому ключу или отпечатку SHA256:…"""
    what = (what or "").strip()
    k = parse_key(what)
    match_blob = k[1] if k else None
    if not match_blob and not what.startswith("SHA256:"):
        raise Fail("нужен открытый ключ или отпечаток SHA256:…")
    have = (rd(auth_path(root)) or "").splitlines()
    keep = []
    for ln in have:
        pk = parse_key(ln)
        if pk and (pk[1] == match_blob or fingerprint(pk[1]) == what):
            continue
        keep.append(ln)
    if len(keep) != len(have):
        _write_keys(root, keep)
    return {"removed": len(have) - len(keep)}


# ── порт SSH ───────────────────────────────────────────────────────────────
def ssh_port(port, root="/", run=sh):
    """На Ubuntu 24.04 sshd поднят сокетом: Port в sshd_config сам по себе не меняет порт —
    нужен ListenStream у ssh.socket. Пишем и то, и другое, чтобы генератор не спорил."""
    if not (isinstance(port, int) and 1 <= port <= 65535):
        raise Fail("порт — число 1–65535")
    os.makedirs(at(root, "etc/ssh/sshd_config.d"), exist_ok=True)
    util.wr(at(root, "etc/ssh/sshd_config.d/11-pcs-port.conf"), "# PCS: порт SSH (pcs ssh-port)\nPort %d" % port)
    socket_on = run("systemctl", "is-enabled", "ssh.socket", check=False).stdout.strip() == "enabled"
    if socket_on:
        d = at(root, "etc/systemd/system/ssh.socket.d")
        os.makedirs(d, exist_ok=True)
        util.wr(os.path.join(d, "10-pcs-port.conf"), "[Socket]\nListenStream=\nListenStream=%d" % port)
        run("systemctl", "daemon-reload", check=False)
        # сокет перезапускается отложенно: иначе рвётся SSH, через который пришла команда
        run("systemd-run", "--on-active=2", "--collect", "systemctl", "restart", "ssh.socket", check=False)
    else:
        run("systemd-run", "--on-active=2", "--collect", "systemctl", "restart", "ssh", check=False)
    return {"port": port, "via": "ssh.socket" if socket_on else "sshd_config"}


# ── mobileproxy.space ──────────────────────────────────────────────────────
def mp_mirror(proxy="", run=sh):
    for h in MP_HOSTS:
        cmd = ["curl", "-s", "-o", "/dev/null", "-m", "20", "-w", "%{http_code}"] + (["--proxy", proxy] if proxy else [])
        if run(*cmd, "https://%s/downloads/sp/install.sh" % h, check=False).stdout.strip() == "200":
            return h
    return ""


def mp_auth(raw, root="/", run=sh):
    """auth.mp из ЛК ({"auth": "KEY:KEY", "port": 1800}) → /home/nodejs/work/auth.mp (600)."""
    raw = (raw or "").strip()
    try:
        d = json.loads(raw) if raw.startswith("{") else {"auth": raw}
    except ValueError as e:
        raise Fail("auth.mp — это не JSON: %s" % e)
    work = at(root, MP_WORK)
    cur = jload(os.path.join(work, "auth.mp"), {})
    auth = str(d.get("auth", "")).strip()
    if not auth or re.search(r'[\s"\\]', auth):
        raise Fail("в auth.mp нет поля auth или в нём пробелы/кавычки — скопируй файл из ЛК целиком")
    port = d.get("port") or (cur.get("port") if isinstance(cur, dict) else None)
    if not str(port).isdigit() or not 0 < int(port) < 65536:
        raise Fail("в auth.mp нет порта: нужен \"port\" в JSON")
    d.update(auth=auth, port=int(port))
    if not os.path.isdir(work):
        raise Fail("софта mobileproxy.space на сервере нет: сначала pcs mpspace install")
    path = os.path.join(work, "auth.mp")
    if os.path.exists(path):
        shutil.copy2(path, "%s.bak.%s" % (path, time.strftime("%Y%m%d-%H%M%S")))
    fd, tmp = tempfile.mkstemp(dir=work)
    with os.fdopen(fd, "w") as f:
        json.dump(d, f)
    os.chmod(tmp, 0o600)
    try:
        shutil.chown(tmp, "nodejs", "nodejs")
    except (LookupError, OSError):
        pass
    os.replace(tmp, path)
    run("systemctl", "restart", "nodejs-server", check=False)
    say("auth.mp записан (порт %d), nodejs-server перезапущен" % d["port"])
    return {"port": d["port"]}


def run_logged(cmd, env=None, logname="mpspace"):
    """Долгая установка: вывод и на экран (журнал юнита), и в /var/log/pcs/<имя>.log."""
    os.makedirs(LOGD, exist_ok=True)
    with open(os.path.join(LOGD, logname + ".log"), "a") as lf:
        p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, env=env)
        for line in p.stdout:
            line = re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", line)
            print("    " + line.rstrip(), flush=True)
            lf.write(line)
        return p.wait()


def mp_install(params, no_reboot=False, run=sh):
    """Софт mobileproxy.space: install.sh, auth.mp, setup-modem-management.sh, перезагрузка.
    Секреты (auth.mp, прокси с паролем) приходят файлом 600 и сразу удаляются."""
    proxy = (params or {}).get("proxy") or ""
    host = mp_mirror(run=run) or (mp_mirror(proxy, run) if proxy else "")
    if not host:
        raise Fail("ни одно зеркало mobileproxy.space не открывается%s — нужен HTTP-прокси"
                   % (" даже через прокси" if proxy else ""))
    say("зеркало %s отвечает" % host)
    env = dict(os.environ, DEBIAN_FRONTEND="noninteractive")
    if proxy:
        env.update(http_proxy=proxy, https_proxy=proxy, no_proxy="localhost,127.0.0.1,192.168.0.0/16,10.0.0.0/8")
    run("systemctl", "disable", "--now", "unattended-upgrades", "apt-daily.timer", "apt-daily-upgrade.timer", check=False)
    work = "/root/pcs-soft"
    os.makedirs(work, exist_ok=True)
    for script in ("install.sh", "setup-modem-management.sh"):
        path = os.path.join(work, script)
        run("curl", "-fsSL", "-m", "120", *(["--proxy", proxy] if proxy else []),
            "-o", path, "https://%s/downloads/sp/%s" % (host, script), timeout=180)
        if not rd(path).startswith("#!"):
            raise Fail("%s скачался, но это не скрипт" % script)
        if script == "setup-modem-management.sh" and params.get("auth"):
            mp_auth(params["auth"], run=run)
        say("── %s (mobileproxy.space)" % script)
        rc = run_logged(["bash", path], env=env)
        if rc != 0:
            raise Fail("%s завершился с кодом %d (лог: %s/mpspace.log)" % (script, rc, LOGD))
    if no_reboot:
        dnsfix.fix(log=say)                 # софт агрегатора трогает сеть — DNS перепроверить
    else:
        say("перезагрузка: mobileproxy.space меняет GRUB и initramfs (DNS проверит pcs-dns.service)")
        run("systemd-run", "--on-active=5", "--collect", "systemctl", "reboot", check=False)
    return {"mirror": host, "reboot": not no_reboot}


# ── команда ────────────────────────────────────────────────────────────────
def stdin_line():
    return sys.stdin.readline().rstrip("\r\n") if not sys.stdin.isatty() else ""


def cmd(a):
    if a.cmd == "version":
        if not a.json:
            print(VERSION)
        return 0, VERSION
    util.need_root()
    if a.cmd == "info":
        d = info(wan=a.wan)
        if not a.json:
            print(json.dumps(d, ensure_ascii=False, indent=1))
        return 0, d
    if a.cmd == "dns-fix":
        lock = util.lock("/run/pcs/dns.lock", what="правка DNS")
        try:
            d = dnsfix.fix(dnsfix.parse_dns(a.dns) if a.dns else None, boot=a.boot, log=say)
        finally:
            lock.close()
        say("✓ DNS в порядке: %s" % " ".join(d["dns"]))
        return 0, d
    if a.cmd == "passwd":
        return 0, passwd(stdin_line())
    if a.cmd == "key":
        if a.add:
            return 0, key_add(stdin_line())
        if a.drop:
            return 0, key_drop(stdin_line())
        d = keys()
        if not a.json:
            for k in d:
                print("  %s %s %s" % (k["type"], k["fp"], k["comment"]))
        return 0, d
    if a.cmd == "ssh-port":
        return 0, ssh_port(a.port)
    if a.cmd == "mpspace":
        if a.action == "auth":
            return 0, mp_auth(stdin_line())
        if a.action == "check":
            d = mpspace_info()
            if not a.json:
                for u, st in d["units"].items():
                    print("  %s служба %s: %s" % ("✓" if st == "active" else "✗", u, st))
                print("  %s auth.mp: порт %s" % ("✓" if d.get("auth") else "✗", d.get("port") or "—"))
            return 0, d
        params = {}
        if a.params:
            params = jload(a.params, {})
            try:
                os.unlink(a.params)
            except OSError:
                pass
        return 0, mp_install(params, no_reboot=a.no_reboot)
    raise Fail("неизвестная команда")


def parser():
    ap = argparse.ArgumentParser(prog="pcs-node", description="служебная команда хаба PCS на сервере")
    sub = ap.add_subparsers(dest="cmd")
    p = sub.add_parser("info")
    p.add_argument("--wan", action="store_true")
    p = sub.add_parser("dns-fix")
    p.add_argument("--dns")
    p.add_argument("--boot", action="store_true")
    sub.add_parser("passwd")
    p = sub.add_parser("key")
    g = p.add_mutually_exclusive_group()
    g.add_argument("--add", action="store_true")
    g.add_argument("--drop", action="store_true")
    p = sub.add_parser("ssh-port")
    p.add_argument("port", type=int)
    p = sub.add_parser("mpspace")
    p.add_argument("action", choices=("install", "auth", "check"))
    p.add_argument("--params")
    p.add_argument("--no-reboot", action="store_true")
    sub.add_parser("version")
    for p in sub.choices.values():
        p.add_argument("--json", action="store_true")
    return ap


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if not argv or argv[0] in ("help", "-h", "--help"):
        print(__doc__.strip())
        return 0
    a = parser().parse_args(argv)
    if not a.cmd:
        print(__doc__.strip())
        return 0
    return util.run_cli(cmd, a, as_json=a.json)
