"""Перенос с proxyveth-virt 3.4 на proxyveth gw (docs/ARCHITECTURE.md §13). Зовёт gw.setup().

Что было у proxyveth-virt 3.4 (один файл /usr/local/bin/proxyveth.py и симлинк proxyveth):
  • /etc/proxyveth/env — источник (API_URL | SHEET_CSV_URL | SHEET_ID+SHEET_GID) и
    переопределения (VETH_PREFIX, RT_TABLE_BASE); config.json {"modems", "last_sync"};
    singbox/modem_N.json, logs/, restart_counts.json;
  • юниты proxyveth.service (up all), proxyveth-watchdog.service, proxyveth-autosync.{service,timer};
  • модем N: netns ns_N, veth mdmN, ip rule from 192.168.N.100 lookup 1000+N без
    приоритета, nat MASQUERADE -s 192.168.N.0/24 без пометки; sing-box и посредник —
    процессы из-под юнитов, pid в /run/proxyveth/; /etc/sysctl.d/99-proxyveth.conf.

Снимается только узнаваемо своё: имена ns_N/mdmN, правила по его списку модемов.
mp.space, modlink и общий с ним /usr/local/bin/sing-box не трогаем.

Порядок — чтобы модемы не легли разом:
  1. копия старого в /var/lib/proxyveth/proxyveth-virt-3.4 (для отката руками);
  2. источник из env → config.json "source";
  3. старый замок /run/proxyveth.lock: watchdog и autosync старого ждут и ничего не делают;
  4. старые юниты останавливаются без убийства процессов (KillMode=process) — модемы
     работают, как работали;
  5. первый модем пробный: старый снят, новый поднят; если по-старому через него был
     выход в интернет, а по-новому нет — новый снимается, старое запускается как было;
  6. остальные — пачками: снять старый N → сразу поднять новый N. Простой — секунды;
  7. старые юниты, скрипт и файлы — прочь (копия остаётся).
Перенос можно прервать и повторить: setup продолжит с того, что осталось.
PROXYVETH_MIGRATE_LIMIT=K — перенести не больше K модемов за раз (остальные ждут
следующего setup; apply их не трогает, пока жив ns_N).
"""
import fcntl
import glob
import os
import re
import shutil
import signal
import time

from pcs.core import singbox, util
from pcs.core.util import Fail

from . import gw

SCRIPT = "/usr/local/bin/proxyveth.py"
LINK = "/usr/local/bin/proxyveth"
UNITS = ("proxyveth-autosync.timer", "proxyveth-autosync.service", "proxyveth-watchdog.service", "proxyveth.service")
ENABLED = ("proxyveth.service", "proxyveth-watchdog.service", "proxyveth-autosync.timer")
ENV = gw.ETC + "/env"
SBDIR = gw.ETC + "/singbox"
LOGS = gw.ETC + "/logs"
COUNTS = gw.ETC + "/restart_counts.json"
LOCK = "/run/proxyveth.lock"
SYSCTL = "/etc/sysctl.d/99-proxyveth.conf"
RT_TABLES = ("/etc/iproute2/rt_tables", "/usr/share/iproute2/rt_tables")
BACKUP = gw.LIB + "/proxyveth-virt-3.4"
DROPIN = "/run/systemd/system/%s.d/90-pcs-migrate.conf"
OLD_KEYS = ("modems", "last_sync")
LIMIT = "PROXYVETH_MIGRATE_LIMIT"

kill = os.kill
sleep = time.sleep


def P(path):
    return gw.P(path)


def sh(*cmd, **kw):
    return gw.sh(*cmd, **kw)


def read_env(text):
    out = {}
    for line in (text or "").splitlines():
        line = line.strip()
        if line.startswith("export "):
            line = line[7:].strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        v = v.strip()
        if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
            v = v[1:-1]
        out[k.strip()] = v
    return out


def source(env):
    """Источник таблицы — в том же порядке, что выбирал proxyveth-virt."""
    for k in ("API_URL", "SHEET_CSV_URL", "PROXYVETH_SOURCE_URL"):
        if env.get(k):
            return env[k]
    if env.get("SHEET_ID"):
        return "https://docs.google.com/spreadsheets/d/%s/export?format=csv&gid=%s" % (
            env["SHEET_ID"], env.get("SHEET_GID") or "0")
    return ""


def old_rows(data):
    """config.json proxyveth-virt → ({n: строка gw}, [что не годится])."""
    rows, bad = {}, []
    mods = data.get("modems") if isinstance(data, dict) else None
    for k, m in (mods if isinstance(mods, dict) else {}).items():
        try:
            n = int(k)
            row = {"n": n, "real": int(m.get("real") or n), "host": str(m["proxy_host"]).strip(),
                   "port": int(m["proxy_port"]), "user": str(m["login"]).strip(), "pw": str(m["password"]),
                   "enabled": bool(m.get("enabled", True))}
            if not 1 <= n <= 254 or not 1 <= row["real"] <= 254:
                raise ValueError("номер вне 1..254")
            if not re.fullmatch(r"[A-Za-z0-9.-]+", row["host"]):
                raise ValueError("адрес прокси %r" % row["host"])
            if not 0 < row["port"] < 65536:
                raise ValueError("порт %d" % row["port"])
            if not row["user"] or not row["pw"] or re.search(r"\s", row["user"] + row["pw"]):
                raise ValueError("пустой логин или пароль, или пробел в них")
            rows[n] = row
        except (KeyError, TypeError, ValueError, AttributeError) as e:
            bad.append("n=%s: %s" % (k, e))
    return rows, bad


def old_rt(n, base):
    t = base + n
    return t if t not in (0, 253, 254, 255) else 20000 + n


def table_ids():
    """Имена таблиц маршрутов → номера (ip rule показывает имя, если оно заведено)."""
    out = {}
    for path in [P(p) for p in RT_TABLES] + sorted(glob.glob(P("/etc/iproute2/rt_tables.d/*.conf"))):
        for line in util.rd(path).splitlines():
            f = line.split("#", 1)[0].split()
            if len(f) == 2 and f[0].isdigit():
                out[f[1]] = f[0]
    return out


def veths():
    return {ln.split(":")[1].strip().split("@")[0]
            for ln in sh("ip", "-o", "link", "show", "type", "veth", check=False).stdout.splitlines() if ln.count(":") >= 2}


def old_link():
    p = P(LINK)
    return os.path.islink(p) and os.readlink(p).endswith("proxyveth.py")


def find():
    """Что осталось от proxyveth-virt 3.4 → словарь; None — ничего."""
    env = read_env(util.rd(P(ENV)))
    live = util.jload(P(gw.CONFIG), {})
    config = isinstance(live, dict) and isinstance(live.get("modems"), dict)
    rows, bad = old_rows(live if config else util.jload(P(BACKUP + "/config.json"), {}))
    units = [u for u in UNITS if "proxyveth.py" in util.rd(P(gw.UNITD + "/" + u))]
    prefix = env.get("VETH_PREFIX") or "mdm"
    try:
        base = int(env.get("RT_TABLE_BASE") or 1000)
    except ValueError:
        base = 1000
    links = veths()
    ns = set()
    for ln in sh("ip", "netns", "list", check=False).stdout.splitlines():
        m = re.fullmatch(r"ns_(\d+)", (ln.split() or [""])[0])
        if not m:
            continue
        n = int(m.group(1))
        if (n in rows or os.path.isdir(P("/etc/netns/ns_%d" % n))
                or os.path.exists(P("%s/modem_%d.json" % (SBDIR, n))) or prefix + str(n) in links):
            ns.add(n)
    files = [p for p in (ENV, SYSCTL, LOGS, COUNTS) if os.path.exists(P(p))]
    if glob.glob(P(SBDIR + "/modem_*.json")):
        files.append(SBDIR)
    baks = glob.glob(P(SCRIPT) + ".bak.*")
    script = os.path.exists(P(SCRIPT))
    if not (units or script or ns or files or config or baks or old_link()):
        return None
    return {"env": env, "source": source(env), "rows": rows, "bad": bad, "units": units, "script": script,
            "prefix": prefix, "base": base, "ns": sorted(ns), "veths": links, "files": files,
            "config": config, "baks": baks}


def describe(f):
    parts = []
    if f["ns"]:
        parts.append("модемы ns_%s" % ",".join(map(str, f["ns"])))
    if f["units"]:
        parts.append("юниты %s" % ", ".join(f["units"]))
    if f["script"]:
        parts.append(SCRIPT)
    parts += f["files"]
    if f["config"]:
        parts.append("старые ключи в config.json")
    return "; ".join(parts) + " — proxyveth setup перенесёт"


# ── старый модем ───────────────────────────────────────────────────────────
def cmdline(pid):
    try:
        with open(P("/proc/%s/cmdline" % pid), "rb") as fh:
            return [a.decode("utf-8", "replace") for a in fh.read().split(b"\0") if a]
    except OSError:
        return []


def is_old(args, n):
    """Процесс старого модема N: его sing-box (по конфигу) или посредник (_hostfix N)."""
    if args and os.path.basename(args[0]) == "sing-box":
        return any(a.endswith("/singbox/modem_%d.json" % n) for a in args)
    if "_hostfix" in args:
        i = args.index("_hostfix")
        return args[i + 1:i + 2] == [str(n)] and any(a.endswith("proxyveth.py") for a in args[:i])
    return False


def procs(n):
    try:
        pids = [d for d in os.listdir(P("/proc")) if d.isdigit()]
    except OSError:
        return []
    return [int(d) for d in pids if is_old(cmdline(d), n)]


def stop(pid):
    for sig, ticks in ((signal.SIGTERM, 30), (signal.SIGKILL, 20)):
        try:
            kill(pid, sig)
        except OSError:
            return
        for _ in range(ticks):
            if not cmdline(pid):
                return
            sleep(0.1)


def old_down(n, f):
    """Снять старый модем N — как его ns_down, но только узнаваемое."""
    for pid in procs(n):
        stop(pid)
    sh("ip", "netns", "del", "ns_%d" % n, check=False)
    for name in (f["prefix"] + str(n), "veth%dh" % n, "veth%dn" % n, "veth_ext%d_host" % n, "pvtmp%d" % n):
        if name in f["veths"]:
            sh("ip", "link", "del", name, check=False)
    t, ids = str(old_rt(n, f["base"])), table_ids()
    for line in sh("ip", "-4", "rule", "show", check=False).stdout.splitlines():
        m = re.match(r"(\d+):\s+from 192\.168\.%d\.100 lookup (\S+)\s*$" % n, line)
        if m and t in (m.group(2), ids.get(m.group(2))) and int(m.group(1)) != gw.prio(n):
            sh("ip", "rule", "del", "priority", m.group(1), "from", "192.168.%d.100" % n, "lookup", m.group(2),
               check=False)
    # Маршрут в таблице старого модема ядро убрало вместе с его veth; таблицу не трогаем —
    # номер 1000+N теперь наш, а при другом RT_TABLE_BASE мог быть чужим.
    for line in sh("iptables", "-w", "5", "-t", "nat", "-S", "POSTROUTING", check=False).stdout.splitlines():
        if re.fullmatch(r"-A POSTROUTING -s 192\.168\.%d\.0/24 -o \S+ -j MASQUERADE" % n, line.strip()):
            sh("iptables", "-w", "5", "-t", "nat", "-D", *line.split()[1:], check=False)
    shutil.rmtree(P("/etc/netns/ns_%d" % n), ignore_errors=True)
    for p in ("%s/modem_%d.json" % (SBDIR, n), "/run/proxyveth/ns_%d.pid" % n, "/run/proxyveth/hostfix_%d.pid" % n):
        try:
            os.unlink(P(p))
        except OSError:
            pass


# ── юниты, замок, файлы ────────────────────────────────────────────────────
def take_lock(wait=300):
    path = P(LOCK)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fh = open(path, "a")
    t0 = time.time()
    while True:
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return fh
        except OSError:
            if time.time() - t0 >= wait:
                fh.close()
                raise Fail("старый proxyveth занят дольше %d с (watchdog чинит модемы?) — повтори позже" % wait)
            sleep(1)


def hold(units):
    """Остановить старые юниты, не убивая их процессы: модемы работают, пока их не перенесли."""
    for u in units:
        if u.endswith(".service"):
            path = P(DROPIN % u)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            util.wr(path, "# PCS: перенос на proxyveth gw — модемы не гасить вместе с юнитом\n"
                          "[Service]\nKillMode=process\nExecStop=\n")
    sh("systemctl", "daemon-reload")
    sh("systemctl", "disable", "--now", *units, check=False, timeout=300)


def unhold(units):
    for u in units:
        shutil.rmtree(os.path.dirname(P(DROPIN % u)), ignore_errors=True)
    sh("systemctl", "daemon-reload", check=False)


def backup(f):
    d = P(BACKUP)
    os.makedirs(d + "/units", exist_ok=True)
    os.chmod(d, 0o700)
    items = [(ENV, "env"), (SYSCTL, "99-proxyveth.conf"), (SCRIPT, "proxyveth.py")]
    items += [(gw.UNITD + "/" + u, "units/" + u) for u in f["units"]]
    if f["config"]:
        items.append((gw.CONFIG, "config.json"))
    for src, name in items:
        s, t = P(src), d + "/" + name
        if os.path.isfile(s) and not os.path.exists(t):
            shutil.copy2(s, t)
            os.chmod(t, 0o600)


def set_source(src, cfg):
    if not cfg.get("source"):
        cfg["source"] = src
    data = util.jload(P(gw.CONFIG), {})
    data = data if isinstance(data, dict) else {}
    if not data.get("source"):
        data["source"] = src
        util.jsave(P(gw.CONFIG), data)


def strip_config(cfg):
    data = util.jload(P(gw.CONFIG), None)
    if isinstance(data, dict) and any(k in data for k in OLD_KEYS):
        for k in OLD_KEYS:
            data.pop(k, None)
        util.jsave(P(gw.CONFIG), data)
    for k in OLD_KEYS:
        cfg.pop(k, None)


def unlink(path):
    try:
        os.unlink(P(path))
    except OSError:
        pass


def tidy(f):
    """Старые юниты, скрипт и файлы — прочь (копия уже в BACKUP)."""
    d = P(BACKUP)
    for u in f["units"]:
        unlink(gw.UNITD + "/" + u)
    unhold(f["units"])
    if f["units"]:
        sh("systemctl", "reset-failed", *f["units"], check=False)
    for p in (SCRIPT, ENV, SYSCTL, COUNTS, LOCK):
        unlink(p)
    for p in glob.glob(P(SCRIPT) + ".bak.*"):
        shutil.move(p, d + "/" + os.path.basename(p))
    if old_link():
        new = os.path.join(gw.CODE, "bin", "proxyveth")
        os.unlink(P(LINK))
        if os.path.exists(new):
            os.symlink(new, P(LINK))
    for p in glob.glob(P(SBDIR + "/modem_*.json")):
        os.unlink(p)
    try:
        os.rmdir(P(SBDIR))
    except OSError:
        pass
    if os.path.isdir(P(LOGS)):
        t = d + "/logs"
        shutil.move(P(LOGS), t if not os.path.exists(t) else "%s-%d" % (t, time.time()))
    for p in glob.glob(P("/run/proxyveth/ns_*.pid")) + glob.glob(P("/run/proxyveth/hostfix_*.pid")):
        os.unlink(p)


def gate(n, before, tries=2):
    """Пробный модем: был выход по-старому — должен быть и по-новому (слабой прокси — второй шанс)."""
    if not before:
        return True
    for i in range(tries):
        if gw.ext_ip(n):
            return True
        if i + 1 < tries:
            sleep(3)
    return False


def migrate(cfg, log=print):
    """→ None (переносить нечего) или отчёт {"source", "moved", "removed", "failed", "left", "backup"}."""
    f = find()
    if not f:
        return None
    rep = {"source": f["source"], "moved": [], "removed": [], "failed": {}, "left": [], "backup": BACKUP}
    if f["prefix"] == "pv":
        raise Fail("в %s VETH_PREFIX=pv — совпадает с новыми именами; перенос только руками" % ENV)
    work = list(f["ns"])
    if work and not f["rows"]:
        raise Fail("работают модемы proxyveth-virt ns_%s, а их списка нет (%s → modems) — вслепую не переношу"
                   % (",".join(map(str, work)), gw.CONFIG))
    log("найден proxyveth-virt 3.4: %s" % describe(f))
    backup(f)
    if f["source"]:
        set_source(f["source"], cfg)
        log("источник таблицы — из %s: %s" % (ENV, f["source"]))
    for b in f["bad"]:
        log("⚠ строка старого config.json не годится (%s) — этот модем не переношу, его поднимет sync" % b)
    o = gw.opts(cfg)
    swap = [n for n in work if n in f["rows"] and f["rows"][n]["enabled"]]
    up = gw.uplink() if swap else None
    if swap and not up:
        raise Fail("не нашёл основной канал сервера — новым модемам не к чему подключиться; старое не тронуто")
    if swap:
        ok, msg = singbox.check(gw.sb_config(f["rows"][swap[0]], "192.0.2.1", o["dns"]))
        if not ok:
            raise Fail("sing-box %s не принял конфиг модема (%s) — старое не тронуто" % (singbox.VERSION, msg[:200]))
    try:
        limit = int(os.environ.get(LIMIT) or 0)
    except ValueError:
        limit = 0
    lock = take_lock() if (f["units"] or work) else None
    try:
        if f["units"]:
            hold(f["units"])
            log("старые юниты остановлены, модемы пока работают по-старому")
        st = gw.load_state()
        if not st["desired"]:
            st["desired"] = {str(n): r for n, r in f["rows"].items()}

        def one(n):
            old_down(n, f)
            try:
                gw.remember(st, f["rows"][n], gw.create(f["rows"][n], up, o))
                return None
            except Fail as e:
                return str(e)

        todo = swap[:limit] if limit > 0 else swap
        if todo:
            n0 = todo[0]
            before = gw.ext_ip(n0)
            err = one(n0)
            if not err and not gate(n0, before):
                err = "по-старому через модем был выход в интернет (%s), а по-новому нет" % before
            if err:
                gw.remove(n0, quiet=True)
                st["modems"].pop(str(n0), None)
                gw.save_state(st)
                unhold(f["units"])
                lock.close()
                lock = None
                back = [u for u in ENABLED if u in f["units"]]
                if back:
                    sh("systemctl", "enable", "--now", "--no-block", *back, check=False, timeout=120)
                raise Fail("перенос остановлен на пробном модеме %d: %s. Старый proxyveth-virt %s; копия — %s"
                           % (n0, err, "запущен как был (модем %d он поднимет сам)" % n0 if back else
                              "без юнитов — подними руками: python3 %s up %d" % (SCRIPT, n0), BACKUP))
            rep["moved"].append(n0)
            log("модем %d перенесён (пробный)%s" % (n0, ", выход в интернет есть" if before else ""))
        rest, w, stopped = todo[1:], max(1, int(o["workers"])), False
        for i in range(0, len(rest), w):
            chunk = rest[i:i + w]
            for n, err in zip(chunk, gw.par(chunk, one, o)):
                if err:
                    rep["failed"][n] = err
                    log("✗ %s" % err)
                else:
                    rep["moved"].append(n)
            if len(rep["failed"]) >= 3 and len(rep["failed"]) > len(rep["moved"]):
                stopped = True
                log("✗ слишком много неудач подряд — остальные модемы оставляю по-старому")
                break
        gw.save_state(st)
        for n in work:
            if n not in swap:                      # нет строки или выключен — только снять
                old_down(n, f)
                rep["removed"].append(n)
        rep["left"] = [n for n in swap if n not in rep["moved"] and n not in rep["failed"]]
        if not rep["left"]:
            tidy(f)
            strip_config(cfg)
    finally:
        if lock:
            lock.close()
    log("перенос: %d модемов по-новому, %d не поднялись%s; копия старого — %s" % (
        len(rep["moved"]), len(rep["failed"]),
        (", %d ждут (proxyveth setup — продолжить)" % len(rep["left"])) if rep["left"] else "", BACKUP))
    if stopped:
        raise Fail("перенос прерван: не поднялись %s — разбор: proxyveth diag N; продолжить: proxyveth setup"
                   % ", ".join(map(str, sorted(rep["failed"]))))
    return rep
