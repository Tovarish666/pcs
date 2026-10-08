"""Серверы хаба: создать ВМ под ключ, взять существующую, удалить, обновить код, сводка."""
import ipaddress
import json
import os
import re
import secrets
import shlex
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor

from .. import VERSION
from ..core import dns as dnsfix
from ..core import util
from ..core.util import Fail, sh
from . import node, pve, remote, store, ui

MODES = ("usb", "gw")
DEFAULTS = {"id": None, "name": "", "cores": 4, "ram_gb": 4, "disk_gb": 40, "storage": "", "bridge": "vmbr0",
            "ip": "", "gw": "", "dns": None, "password": "", "mode": "usb", "sheet": "", "mpspace": False,
            "auth": "", "proxy": "", "replace": False}
EMPTY = {"ok": 0, "warn": 0, "broken": 0, "total": 0}


# ── сводка ─────────────────────────────────────────────────────────────────
def overview(s, timeout=20, wan=False):
    """Одна ВМ для дерева панели, меню и pcs status.
    state: running | stopped | paused | absent (нет в Proxmox) | offline (нет SSH) | old (нет pcs-node)."""
    vm = pve.qm_status(s["id"]) if pve.is_pve() else "running"
    d = {"id": s["id"], "name": s.get("name", ""), "ip": s.get("ip", ""), "port": s.get("port") or 22,
         "state": vm if vm in ("stopped", "paused", "absent") else "running", "mode": s.get("mode"),
         "modems": dict(EMPTY), "mpspace": None, "version": None, "error": None}
    if d["state"] != "running":
        return d
    try:
        info = remote.node(s, "info", *(["--wan"] if wan else []), timeout=timeout)
    except Fail as e:
        d["state"] = "old" if "нет pcs-node" in str(e) else "offline"
        d["error"] = str(e)
        return d
    pv = info.get("proxyveth") or {}
    d.update(mode=pv.get("mode"), modems=pv.get("modems") or dict(EMPTY), mpspace=info.get("mpspace"),
             version=info.get("version"), info=info)
    if pv.get("error"):
        d["error"] = "proxyveth: %s" % pv["error"]
    if d["mode"] and d["mode"] != s.get("mode"):
        s["mode"] = d["mode"]
        store.save(s)
    return d


def overview_all(timeout=20):
    srv = store.all_servers()
    if not srv:
        return []
    with ThreadPoolExecutor(max_workers=min(8, len(srv))) as ex:
        return list(ex.map(lambda s: overview(s, timeout), srv))


def host_ip():
    m = re.search(r"src (\S+)", sh("ip", "-4", "route", "get", "1.1.1.1", check=False).stdout)
    return m.group(1) if m else ""


def host_overview():
    hl = remote.local_part("hivelink", ["status"], timeout=30)
    return {"name": socket.gethostname(), "kind": "proxmox" if pve.is_pve() else "other", "version": VERSION,
            "ip": host_ip(), "cpu": {"cores": os.cpu_count() or 0, "load": list(os.getloadavg())},
            "ram": node.mem(), "disk": node.disk("/var/lib/vz" if os.path.isdir("/var/lib/vz") else "/"),
            "hivelink": hl.get("data") if hl.get("ok") else {"installed": False, "error": hl.get("error")},
            "dns": dnsfix.current()}


# ── создание ───────────────────────────────────────────────────────────────
def normalize(p):
    """Параметры создания (CLI, панель) → проверенный dict. Ошибка — Fail до любых действий."""
    q = dict(DEFAULTS)
    q.update({k: v for k, v in (p or {}).items() if v not in (None, "")})
    if q["id"] not in (None, ""):
        q["id"] = int(store.norm_id(q["id"]))
        if not 100 <= q["id"] <= 999999999:
            raise Fail("VM ID — от 100")
    for k, lo, hi in (("cores", 1, 128), ("ram_gb", 1, 1024), ("disk_gb", 10, 10000)):
        try:
            q[k] = int(q[k])
        except (TypeError, ValueError):
            raise Fail("%s — число" % k)
        if not lo <= q[k] <= hi:
            raise Fail("%s: от %d до %d" % (k, lo, hi))
    if q["name"] and not re.fullmatch(r"[a-zA-Z0-9]([a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?", q["name"]):
        raise Fail("имя ВМ — латиница, цифры и «-»: %s" % q["name"])
    if q["mode"] not in MODES:
        raise Fail("режим — usb или gw, а не %r" % q["mode"])
    if q["ip"]:
        try:
            iface = ipaddress.ip_interface(q["ip"])
        except ValueError:
            raise Fail("адрес — в виде IP/маска, например 192.168.1.60/24: %s" % q["ip"])
        if "/" not in q["ip"] or iface.version != 4:
            raise Fail("адрес — IPv4 с маской, например 192.168.1.60/24")
        if q["gw"]:
            try:
                ipaddress.IPv4Address(q["gw"])
            except ValueError:
                raise Fail("шлюз — адрес IPv4: %s" % q["gw"])
    q["dns"] = dnsfix.parse_dns(" ".join(q["dns"]) if isinstance(q["dns"], list) else q["dns"]) \
        if q["dns"] else list(dnsfix.DEFAULT)
    if q["password"]:
        node.check_password(q["password"])
    if q["sheet"] and not re.match(r"^(https?://|/)", q["sheet"]):
        raise Fail("таблица — ссылка https://… (или путь к файлу на сервере)")
    q["mpspace"] = q["mpspace"] in (True, 1, "1", "true", "yes", "on", "да")
    q["replace"] = q["replace"] in (True, 1, "1", "true", "yes")
    return q


def create(p):
    """ВМ Ubuntu 24.04 под ключ: образ → ВМ → cloud-init → код PCS → proxyveth → (таблица) → (mp.space)."""
    p = normalize(p)
    pve.need_pve()
    pub = remote.ensure_key()
    t0 = time.time()
    gw_def, _, _ = pve.host_net()
    lk = util.lock(os.path.join(remote.RUN, "create.lock"), wait=3600, what="создание ВМ")
    try:
        vmid = p["id"] or pve.next_id()
        if vmid in pve.used_ids():
            if not p["replace"]:
                raise Fail("ВМ %d уже есть (пересоздать — --replace)" % vmid)
            ui.warn("удаляю ВМ %d со всеми дисками (--replace)" % vmid)
            sh("qm", "stop", str(vmid), check=False, timeout=120)
            sh("qm", "destroy", str(vmid), "--destroy-unreferenced-disks", "1", "--purge", "1", timeout=300)
            store.drop(vmid)
        name = p["name"] or "pcs%d" % vmid
        st = pve.storages()
        storage = p["storage"] or ("local-lvm" if "local-lvm" in st else (st or ["local"])[0])
        password = p["password"] or secrets.token_urlsafe(12)
        ui.hdr("Образ")
        pve.ensure_image()
        ui.hdr("ВМ %d (%s): %d ядер, %d ГБ, диск %d ГБ, %s, proxyveth %s"
               % (vmid, name, p["cores"], p["ram_gb"], p["disk_gb"], p["ip"] or "DHCP", p["mode"]))
        sh("qm", "create", str(vmid), "--name", name, "--memory", str(p["ram_gb"] * 1024), "--cores", str(p["cores"]),
           "--cpu", "host", "--balloon", "0", "--net0", "virtio,bridge=%s" % p["bridge"], "--ostype", "l26",
           "--machine", "q35", "--scsihw", "virtio-scsi-single", "--agent", "enabled=1", "--serial0", "socket",
           # без «QEMU Tablet» в lsusb: серверу мышь не нужна, а по ней видно виртуалку
           "--tablet", "0", "--onboot", "1",
           "--description", "pcs %s: сервер под модемы (proxyveth %s)" % (VERSION, p["mode"]))
        sh("qm", "importdisk", str(vmid), pve.IMG_PATH, storage, timeout=900)
        disk = re.search(r"^unused\d+: (\S+)", sh("qm", "config", str(vmid)).stdout, re.M).group(1)
        sh("qm", "set", str(vmid), "--scsi0", "%s,discard=on,ssd=1,iothread=1" % disk, "--boot", "order=scsi0")
        sh("qm", "resize", str(vmid), "scsi0", "%dG" % p["disk_gb"])
        pve.ensure_snippets()
        snip = os.path.join(pve.SNIPPETS, "pcs-%d.yaml" % vmid)
        fd = os.open(snip, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(pve.user_data(name, password, pub, p["dns"], p["mode"]))
        if sh("qm", "set", str(vmid), "--ide2", "%s:cloudinit" % storage, check=False).returncode != 0:
            sh("qm", "set", str(vmid), "--ide2", "local:cloudinit")
        gw = p["gw"] or gw_def
        sh("qm", "set", str(vmid), "--cicustom", "user=local:snippets/pcs-%d.yaml" % vmid,
           "--nameserver", " ".join(p["dns"]), "--ipconfig0", ("ip=%s,gw=%s" % (p["ip"], gw)) if p["ip"] else "ip=dhcp")
        sh("qm", "start", str(vmid))
    finally:
        lk.close()
    ui.ok("ВМ %d создана и запущена" % vmid)

    s = {"id": vmid, "name": name, "ip": p["ip"].split("/")[0] if p["ip"] else "", "password": password,
         "net": "static" if p["ip"] else "dhcp", "created": time.strftime("%F %T"), "pcs": VERSION, "mode": p["mode"]}
    ui.hdr("Первая загрузка: пакеты, обновление системы%s" % (", ядро с USB-модулями" if p["mode"] == "usb" else ""))
    while not s["ip"]:
        s["ip"] = pve.vm_ip(vmid, p["bridge"])
        if time.time() - t0 > 900:
            raise Fail("адрес ВМ не определился за 15 минут (qm terminal %d)" % vmid)
        if not s["ip"]:
            time.sleep(5)
    remote.forget(s["ip"])
    store.save(s)
    ui.ok("адрес %s" % s["ip"])
    remote.wait(s, 900)
    ui.step("жду cloud-init (apt upgrade идёт на ВМ, это несколько минут)")
    remote.ssh(s, "cloud-init status --wait >/dev/null 2>&1; test -f /var/lib/pcs-cloud-init-done", timeout=3600)
    ui.ok("cloud-init отработал за %d с" % (time.time() - t0))
    need = remote.ssh(s, "test -f /var/run/reboot-required && echo reboot; modinfo libcomposite >/dev/null 2>&1 || echo reboot",
                      check=False).stdout
    if "reboot" in need:
        ui.step("перезагрузка на новое ядро")
        boot = remote.boot_id(s)
        remote.ssh(s, "systemd-run --on-active=2 --collect systemctl reboot", check=False)
        remote.wait_reboot(s, boot)
        ui.ok("ядро %s" % remote.ssh(s, "uname -r").stdout.strip())
    store.set_active(vmid)
    setup(s, mode=p["mode"], sheet=p["sheet"], fresh=True)
    if p["mpspace"]:
        mpspace_install(s, p["auth"], p["proxy"])
    ui.hdr("Готово за %d мин" % ((time.time() - t0) // 60 + 1))
    print_info(s, show_password=not p["password"] and ui.interactive())
    return {"id": vmid, "name": name, "ip": s["ip"], "mode": p["mode"],
            "password": password if not p["password"] else None}


def setup(s, mode=None, sheet="", fresh=False):
    """Код PCS на сервер, DNS (у новой ВМ), proxyveth setup (+ режим, таблица), modlink setup.
    Падение любой из частей — Fail: обновление сервера не должно казаться удачным."""
    ui.hdr("Код PCS на сервер %s" % s["id"])
    had = fresh or remote.ssh(s, "for p in /usr/local/bin/proxyveth /usr/local/sbin/vmodem /etc/vmodem; do "
                                 "test -e $p && echo yes && break; done", check=False).stdout.strip() == "yes"
    ver = remote.deliver(s)
    s["version"] = ver
    store.save(s)
    ui.ok("PCS %s в /opt/pcs, команды proxyveth, modlink, hivelink, pcs-node" % ver)
    if fresh:
        try:
            remote.node(s, "dns-fix", timeout=300)
            ui.ok("DNS сервера: %s" % " ".join(dnsfix.DEFAULT))
        except Fail as e:
            ui.warn(str(e))
    if had:
        ui.hdr("proxyveth setup")
        if remote.stream(s, "proxyveth setup") != 0:
            raise Fail("proxyveth setup на сервере %s не прошёл — см. выше" % s["id"])
        if mode:
            if remote.stream(s, "proxyveth mode %s --yes" % shlex.quote(mode)) != 0:
                raise Fail("proxyveth mode %s не прошёл — см. выше" % mode)
            s["mode"] = mode
            store.save(s)
    else:
        ui.info("proxyveth на сервере не было — его setup не запускаю (поставить: pcs proxyveth setup --server %s)"
                % s["id"])
    ui.hdr("modlink setup")
    if remote.stream(s, "modlink setup") != 0:
        raise Fail("modlink setup на сервере %s не прошёл — см. выше" % s["id"])
    if sheet and had:
        ui.hdr("Модемы по таблице")
        if remote.stream(s, "proxyveth source %s" % shlex.quote(sheet)) == 0:
            if remote.stream(s, "proxyveth sync") != 0:
                ui.warn("proxyveth sync с ошибками — модемы досоздаст таймер (pcs proxyveth status)")
        else:
            ui.warn("таблица не принята — задай позже: pcs proxyveth source <ссылка>")


# ── взять существующую ВМ ──────────────────────────────────────────────────
def adopt(sid, ip="", password=""):
    pve.need_pve()
    pub = remote.ensure_key()
    sid = store.norm_id(sid)
    old = {}
    try:                                    # состояние PCS 3 (bash): /etc/pcs/mp/<id>.conf
        with open(os.path.join(store.ETC, "mp", "%s.conf" % sid)) as f:
            for line in f:
                if "=" in line and not line.startswith("#"):
                    k, v = line.rstrip("\n").split("=", 1)
                    old[k] = (shlex.split(v) or [""])[0]
    except (OSError, ValueError):
        pass
    prev = {}
    try:
        prev = store.load(sid)
    except Fail:
        pass
    ip = ip or prev.get("ip") or old.get("VM_IP") or pve.vm_ip(sid) or ui.ask("IP ВМ %s" % sid)
    if not ip:
        raise Fail("адрес ВМ неизвестен: --ip")
    s = dict(prev, id=int(sid), name=pve.vm_name(sid), ip=ip,
             password=password or prev.get("password") or old.get("VM_PASSWORD", ""),
             net=prev.get("net") or old.get("VM_NET_MODE", "?"),
             created=prev.get("created") or "adopted %s" % time.strftime("%F %T"))
    if old.get("VM_SSH_PORT", "22") != "22" and not prev.get("port"):
        s["port"] = int(old["VM_SSH_PORT"])
    if not remote.alive(s):
        pw = s["password"] or ui.ask("Пароль root ВМ %s" % sid, secret=True)
        if not pw:
            raise Fail("ключ хаба на ВМ не подходит, а пароля нет: pcs server adopt %s (спросит пароль)" % sid)
        r = remote.with_password(ip, pw, "mkdir -p /root/.ssh && chmod 700 /root/.ssh && "
                                         "grep -qxF %s /root/.ssh/authorized_keys 2>/dev/null || "
                                         "echo %s >> /root/.ssh/authorized_keys" % (shlex.quote(pub), shlex.quote(pub)),
                                 port=s.get("port") or 22)
        if r.returncode != 0 or not remote.alive(s):
            raise Fail("не зашёл по паролю: %s" % (r.stderr.strip() or "?"))
        s["password"] = pw
        ui.ok("ключ хаба положен на ВМ")
    store.save(s)
    setup(s)
    store.set_active(sid)
    ui.ok("сервер %s (%s) под управлением PCS и выбран" % (sid, ip))
    return {"id": int(sid), "ip": ip}


# ── удалить ────────────────────────────────────────────────────────────────
def delete(sid):
    pve.need_pve()
    s = store.load(sid)
    sh("qm", "stop", str(s["id"]), check=False, timeout=120)
    if pve.qm_status(s["id"]) != "absent":
        sh("qm", "destroy", str(s["id"]), "--destroy-unreferenced-disks", "1", "--purge", "1", timeout=300)
    store.drop(s["id"])
    for f in (os.path.join(pve.SNIPPETS, "pcs-%s.yaml" % s["id"]),):
        if os.path.exists(f):
            os.unlink(f)
    if s.get("key") and os.path.exists(s["key"]):
        for f in (s["key"], s["key"] + ".pub"):
            try:
                os.unlink(f)
            except OSError:
                pass
    if s.get("ip"):
        remote.forget(s["ip"])
    ui.ok("ВМ %s (%s) удалена" % (s["id"], s.get("name")))
    return {"id": s["id"], "deleted": True}


# ── обновление ─────────────────────────────────────────────────────────────
def source():
    """Откуда PCS поставлен (install.sh пишет /opt/pcs/.source): обновляться оттуда же."""
    src = {"PCS_REPO": "Tovarish666/pcs", "PCS_BRANCH": "main"}
    try:
        with open(os.path.join(remote.ROOT, ".source")) as f:
            for line in f:
                if "=" in line:
                    k, v = line.strip().split("=", 1)
                    src[k] = v
    except OSError:
        pass
    return os.environ.get("PCS_REPO", src["PCS_REPO"]), os.environ.get("PCS_BRANCH", src["PCS_BRANCH"])


def update_host():
    """Хост — из GitHub (та же ветка, что при установке). → новая версия."""
    dest = remote.ROOT
    if os.path.isdir(os.path.join(dest, ".git")):
        raise Fail("%s — рабочая копия git: обнови её git pull, а не pcs update" % dest)
    repo, branch = source()
    url = "https://codeload.github.com/%s/tar.gz/refs/heads/%s" % (repo, branch)
    ui.hdr("PCS на хосте: из %s (%s)" % (repo, branch))
    d = tempfile.mkdtemp()
    try:
        try:
            data = urllib.request.urlopen(url, timeout=120).read()
        except OSError as e:
            raise Fail("не скачался %s@%s: %s" % (repo, branch, e))
        with open(os.path.join(d, "pcs.tgz"), "wb") as f:
            f.write(data)
        new = os.path.join(d, "src")
        os.makedirs(new)
        sh("tar", "xzf", os.path.join(d, "pcs.tgz"), "-C", new, "--strip-components=1")
        if not os.path.exists(os.path.join(new, "bin", "pcs")):
            raise Fail("в архиве %s@%s нет bin/pcs — это не PCS 5" % (repo, branch))
        r = sh(sys.executable, "-m", "compileall", "-q", os.path.join(new, "pcs"), check=False)
        if r.returncode != 0:
            raise Fail("новая версия не компилируется — оставляю прежнюю: %s" % (r.stdout + r.stderr).strip()[-300:])
        ver = sh(sys.executable, os.path.join(new, "bin", "pcs"), "version").stdout.strip()
        with open(os.path.join(new, ".source"), "w") as f:
            f.write("PCS_REPO=%s\nPCS_BRANCH=%s\n" % (repo, branch))
        for f in os.listdir(os.path.join(new, "bin")):
            os.chmod(os.path.join(new, "bin", f), 0o755)
        shutil.rmtree(dest + ".new", ignore_errors=True)
        shutil.copytree(new, dest + ".new", symlinks=True)
    finally:
        shutil.rmtree(d, ignore_errors=True)
    shutil.rmtree(dest + ".prev", ignore_errors=True)
    os.rename(dest, dest + ".prev")
    os.rename(dest + ".new", dest)
    for c in ("pcs", "hivelink"):
        link("/usr/local/bin/" + c, os.path.join(dest, "bin", c))
    ui.ok("PCS на хосте: %s → %s (прежний — в %s.prev)" % (VERSION, ver, dest))
    hl = subprocess.run([os.path.join(dest, "bin", "hivelink"), "install"], capture_output=True, text=True)
    if hl.returncode != 0:
        ui.warn("hivelink install на хосте: %s" % ((hl.stderr or hl.stdout).strip().splitlines() or ["?"])[-1].lstrip(" ✗"))
    sh("systemctl", "try-restart", "pcs-web.service", check=False)
    return ver


def link(path, target):
    tmp = path + ".pcs-new"
    if os.path.lexists(tmp):
        os.unlink(tmp)
    os.symlink(target, tmp)
    os.replace(tmp, path)


def update_servers(ids=None):
    """Код хоста — на серверы (ids=None — все). Остановленные пропускаются с предупреждением."""
    targets = [store.load(i) for i in ids] if ids else store.all_servers()
    done, failed = [], {}
    for s in targets:
        ui.hdr("Сервер %s (%s)" % (s["id"], s.get("ip") or "?"))
        if pve.is_pve() and pve.qm_status(s["id"]) != "running":
            failed[str(s["id"])] = "ВМ не запущена"
            ui.warn("ВМ %s не запущена — пропускаю" % s["id"])
            continue
        lk = util.lock(os.path.join(remote.RUN, "srv-%s.lock" % s["id"]), wait=600, what="работа с сервером %s" % s["id"])
        try:
            setup(s)
            done.append(s["id"])
        except Fail as e:
            failed[str(s["id"])] = str(e)
            ui.bad(str(e))
        finally:
            lk.close()
    return {"version": VERSION, "updated": done, "failed": failed}


def update(params):
    """Задание «update»: {"host": bool, "servers": [ID…] | "all"}."""
    res = {}
    if params.get("host"):
        res["host"] = update_host()
        srv = params.get("servers")
        if srv:
            # серверам — уже новый код хоста: он только что лёг в /opt/pcs
            argv = [sys.executable, os.path.join(remote.ROOT, "bin", "pcs"), "update"] + \
                (["--all"] if srv == "all" else sum((["--server", str(i)] for i in srv), []))
            rc = subprocess.call(argv)
            res["servers"] = "ok" if rc == 0 else "с ошибками (код %d)" % rc
            if rc != 0:
                raise Fail("хост обновлён, на части серверов — ошибки (см. журнал)")
        return res
    srv = params.get("servers")
    r = update_servers(None if srv in (None, "all") else srv)
    if r["failed"]:
        raise Fail("не обновлены: %s" % ", ".join("%s (%s)" % kv for kv in r["failed"].items()))
    return r


# ── mp.space ───────────────────────────────────────────────────────────────
def mpspace_install(s, auth="", proxy="", no_reboot=False):
    ui.hdr("mobileproxy.space на сервере %s (%s)" % (s["id"], s["ip"]))
    params = {"auth": auth or "", "proxy": proxy or ""}
    remote.ssh(s, "umask 077; mkdir -p /run/pcs-node && cat > /run/pcs-node/mpspace.json",
               input=json.dumps(params))
    rc = remote.detached(s, "pcs-mpspace", "pcs-node mpspace install --params /run/pcs-node/mpspace.json"
                         + (" --no-reboot" if no_reboot else ""))
    if rc not in (0, None):
        raise Fail("установка mobileproxy.space завершилась с кодом %s (лог на сервере: /var/log/pcs/mpspace.log)" % rc)
    if not no_reboot:
        ui.step("сервер перезагружается")
        remote.wait(s, 900, "SSH после перезагрузки")
        time.sleep(10)
    try:
        d = remote.node(s, "dns-fix", timeout=300)
        ui.ok("DNS после mp.space: %s" % " ".join(d.get("dns") or []))
    except Fail as e:
        ui.warn(str(e))
    if remote.ssh(s, "test -e /usr/local/bin/proxyveth", check=False).returncode == 0:
        ui.step("proxyveth setup: сторона сервера теперь у mp.space")
        if remote.stream(s, "proxyveth setup") != 0:
            ui.warn("proxyveth setup после mp.space с ошибками — см. выше")
    chk = remote.node(s, "mpspace", "check", timeout=60)
    for u, st in (chk.get("units") or {}).items():
        (ui.ok if st == "active" else ui.warn)("служба %s: %s" % (u, st))
    if not chk.get("auth"):
        ui.warn("auth.mp не задан: pcs mpspace auth --server %s" % s["id"])
    return chk


# ── человеку ───────────────────────────────────────────────────────────────
def print_info(s, show_password=False, wan=True):
    d = overview(s, timeout=25, wan=wan)
    info = d.get("info") or {}
    mp = d.get("mpspace") or {}
    m = d["modems"]
    pw = s.get("password") or "(не сохранён)"
    ui.say("""
  Сервер %(id)s (%(name)s) — %(state)s
    адрес           : %(ip)s, SSH %(port)s (pcs ssh %(id)s)
    код PCS         : %(ver)s%(old)s
    proxyveth       : %(mode)s, модемы %(ok)s/%(total)s (⚠ %(warn)s, ✗ %(broken)s)
    mp.space        : %(mp)s%(err)s

  Для ЛК mobileproxy.space (Мой прокси-бизнес → Сервера → ✏):
    Статический IP  : %(wan)s
    LocalIP         : %(ip)s
    Root login      : root
    Root password   : %(pw)s
    SSH порт        : %(port)s
    OS              : Unix
""" % dict(id=s["id"], name=s.get("name"), state=d["state"], ip=s.get("ip"), port=s.get("port") or 22,
           ver=d.get("version") or "?", old="" if not d.get("version") or d["version"] == VERSION
           else " (на хосте %s — pcs update --server %s)" % (VERSION, s["id"]),
           mode=d.get("mode") or "—", ok=m["ok"], total=m["total"], warn=m["warn"], broken=m["broken"],
           mp=("работает, порт %s" % mp.get("port")) if mp.get("ok") else
           ("стоит, но: %s" % ", ".join("%s %s" % kv for kv in (mp.get("units") or {}).items()) if mp.get("installed")
            else "не стоит"),
           err=("\n    ⚠ %s" % d["error"]) if d.get("error") else "",
           wan=info.get("wan") or "—", pw=pw if show_password else "скрыт (pcs server info %s --password)" % s["id"]))
    return d
