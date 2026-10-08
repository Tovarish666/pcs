"""Фейковый API хаба (те же функции, что pcs.hub.api, §9) — для разработки панели и тестов.

Хост servervolga; ВМ 1000 server1 (USB 20/20), 2000 pcs2000 (USB 5/5),
200 LTEspace (шлюз 89/90). Состояние живёт в памяти процесса; reset() — с нуля.
Включается PCS_WEB_FAKE=1 или `--fake`.
"""
import copy
import hashlib
import itertools
import sys
import threading
import time

from pcs import VERSION
from pcs.core import table as ptable
from pcs.core.util import Fail

DELAY = 0.15          # «сеть»: пауза на каждый вызов
STEP = 0.7            # пауза между строками журнала заданий
HOST = "servervolga"

_lock = threading.RLock()
_jobs = {}
_jid = itertools.count(1)
S = {}                # id -> сервер (с модемами, таблицей, modlink)
H = {}                # хост


def _now():
    return int(time.time())


def _pw(seed, n=12):
    h = hashlib.sha256(seed.encode()).hexdigest()
    abc = "ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz23456789"
    return "".join(abc[int(h[i:i + 2], 16) % len(abc)] for i in range(0, 2 * n, 2))


def _modems(sid, mode, ns, bad=()):
    rows, t = [], _now()
    for i, n in enumerate(ns):
        real = 81 + (i % 170)
        port = 1100 + real
        row = {"n": n, "real": real, "proxy": "188.134.95.184:%d" % port, "state": "ok", "problems": [],
               "iface": ("eth%d" % (i + 1)) if mode == "usb" else "pv%d" % n,
               "ext_ip": "94.25.%d.%d" % (100 + (n * 7) % 150, 2 + (n * 13) % 250),
               "since": t - 3600 * (1 + (n * 17) % 90),
               "_login": "modem%d" % real, "_pw": _pw("%s-%s" % (sid, n), 8), "_enabled": True}
        if n in bad:
            row.update(state="warn", problems=["proxy-down: прокси 188.134.95.184:%d не отвечает" % port],
                       ext_ip=None, since=t - 420)
        rows.append(row)
    return rows


def _table_csv(srv):
    lines = ["n,real,proxy,enabled"]
    for m in srv["modems"]:
        lines.append("%d,%d,%s:%s:%s,%d" % (m["n"], m["real"], m["proxy"], m["_login"], m["_pw"],
                                            1 if m["_enabled"] else 0))
    return "\n".join(lines) + "\n"


def _modlink(sid, ns, count):
    rows = []
    for i, n in enumerate(ns[:count]):
        rows.append({"id": i + 1, "enabled": i != 3, "name": "m%d" % n, "login": "u%d" % n,
                     "password": _pw("ml-%s-%s" % (sid, n), 12), "port": 20000 + n,
                     "lan_ip": "192.168.%d.100" % n, "modem_ip": "192.168.%d.1" % n,
                     "reconnect_port": 21000 + n, "interval_min": 0 if i % 3 else 10})
    return rows


def _server(sid, name, ip, mode, ns, bad=(), ml=0, cores=4, ram_gb=8, disk_gb=40):
    srv = {"id": sid, "name": name, "ip": ip, "state": "running", "mode": mode, "mpspace": "running",
           "cores": cores, "ram_gb": ram_gb, "disk_gb": disk_gb,
           "modems": _modems(sid, mode, ns, bad),
           "source": {"source": "google",
                      "url": "https://docs.google.com/spreadsheets/d/1Fake%sTableId/edit#gid=0" % sid,
                      "local": "/etc/proxyveth/table.csv", "synced": _now() - 95, "stale": False},
           "modlink": _modlink(sid, ns, ml), "ml_log": {}, "hivelink": False, "auth_mp": True}
    srv["table"] = _table_csv(srv)
    return srv


def reset():
    with _lock:
        _jobs.clear()
        S.clear()
        S[1000] = _server(1000, "server1", "192.168.88.5", "usb", list(range(201, 221)), ml=6,
                          cores=8, ram_gb=16, disk_gb=80)
        S[2000] = _server(2000, "pcs2000", "192.168.88.7", "usb", list(range(201, 206)), ml=5)
        S[200] = _server(200, "LTEspace", "192.168.88.20", "gw",
                         [n for n in range(11, 102) if n != 88], bad=(57,), ml=3, cores=16, ram_gb=32,
                         disk_gb=120)
        H.clear()
        H.update({"name": HOST, "kind": "proxmox", "version": VERSION, "pve": "9.1",
                  "cpu": {"cores": 32, "load": 2.4},
                  "ram": {"total_gb": 128, "used_gb": 71.3},
                  "disk": {"total_gb": 1863, "used_gb": 412},
                  "hivelink": {"installed": True, "version": VERSION, "modems": 3, "ok": 3}})


reset()


def _pause():
    if DELAY:
        time.sleep(DELAY)


def _srv(sid):
    try:
        srv = S.get(int(sid))
    except (TypeError, ValueError):
        srv = None
    if srv is None:
        raise Fail("ВМ %s нет в PCS — pcs server list" % sid)
    return srv


def _summary(srv):
    c = {"ok": 0, "warn": 0, "broken": 0, "total": 0}
    for m in srv["modems"]:
        if not m["_enabled"]:
            continue
        c["total"] += 1
        if m["state"] in c:
            c[m["state"]] += 1
    return c


def _public(srv):
    return {"id": srv["id"], "name": srv["name"], "ip": srv["ip"], "state": srv["state"], "mode": srv["mode"],
            "modems": _summary(srv), "mpspace": srv["mpspace"]}


def _row(m):
    return {k: v for k, v in m.items() if not k.startswith("_")}


# ── §9 ──────────────────────────────────────────────────────────────────────

def host_info():
    _pause()
    with _lock:
        return copy.deepcopy(H)


def servers():
    _pause()
    with _lock:
        return [_public(s) for s in sorted((s for s in S.values() if s), key=lambda s: s["id"])]


def server(sid):
    _pause()
    with _lock:
        srv = _srv(sid)
        out = _public(srv)
        out.update(cores=srv["cores"], ram_gb=srv["ram_gb"], disk_gb=srv["disk_gb"], os="Ubuntu 24.04",
                   versions={"pcs": VERSION, "proxyveth": VERSION, "modlink": VERSION,
                             "hivelink": VERSION if srv["hivelink"] else None})
        return out


def _job(title, lines, done=None):
    jid = "j%d" % next(_jid)
    j = {"id": jid, "title": title, "state": "running", "log": [], "result": None, "started": _now()}
    with _lock:
        _jobs[jid] = j

    def work():
        try:
            for ln in lines:
                time.sleep(STEP)
                if isinstance(ln, Exception):
                    raise ln
                with _lock:
                    j["log"].append("%s %s" % (time.strftime("%H:%M:%S"), ln))
            res = done() if done else None
            with _lock:
                j["result"], j["state"] = res, "done"
        except Fail as e:
            with _lock:
                j["log"].append("%s ✗ %s" % (time.strftime("%H:%M:%S"), e))
                j["state"], j["result"] = "failed", {"error": str(e)}
    threading.Thread(target=work, daemon=True).start()
    return jid


def create_server(params):
    _pause()
    name = str(params.get("name") or "").strip()
    if not name:
        raise Fail("нужно имя ВМ")
    with _lock:
        if any(s and s["name"] == name for s in S.values()):
            raise Fail("ВМ с именем %s уже есть — другое имя" % name)
        sid = next(i for i in itertools.count(1000, 1000) if i not in S)
        S[sid] = None                    # номер занят, пока идёт задание
    mode = params.get("mode") or "usb"
    ip = "192.168.88.%d" % (30 + sid // 1000)
    lines = ["ВМ %d «%s»: образ Ubuntu 24.04 — сумма сходится" % (sid, name),
             "ВМ создана: %s ядер, %s ГБ ОЗУ, диск %s ГБ" % (params.get("cores"), params.get("ram_gb"),
                                                            params.get("disk_gb")),
             "cloud-init: обновление пакетов…", "cloud-init: готово, перезагрузка на новое ядро",
             "адрес ВМ: %s" % ip, "PCS %s → /opt/pcs" % VERSION,
             "proxyveth setup (%s)%s" % (mode, ": dummy_hcd собран под ядро 6.8.0-142" if mode == "usb" else "")]
    if params.get("sheet"):
        lines += ["proxyveth source %s" % params["sheet"], "proxyveth sync: создано 0 (таблица пуста)"]
    if params.get("mpspace"):
        lines += ["mp.space: зеркала и пакеты…", "mp.space: установлен, перезагрузка"]
    if name.startswith("fail"):
        lines.append(Fail("cloud-init не закончился за 15 минут — смотри консоль ВМ %d в Proxmox" % sid))
    lines.append("готово: ВМ %d %s %s" % (sid, name, ip))

    def done():
        with _lock:
            srv = _server(sid, name, ip, mode, [], cores=params.get("cores") or 4,
                          ram_gb=params.get("ram_gb") or 8, disk_gb=params.get("disk_gb") or 40)
            srv["mpspace"] = "running" if params.get("mpspace") else None
            if not params.get("sheet"):
                srv["source"] = {"source": None, "url": None, "local": "/etc/proxyveth/table.csv", "synced": None}
            S[sid] = srv
        return {"id": sid, "ip": ip}

    jid = _job("создать ВМ %s" % name, lines, done)

    def cleanup():                       # задание упало — номер освобождается
        while _jobs[jid]["state"] == "running":
            time.sleep(0.05)
        with _lock:
            if S.get(sid) is None and _jobs[jid]["state"] == "failed":
                S.pop(sid, None)
    threading.Thread(target=cleanup, daemon=True).start()
    return jid


def delete_server(sid, confirm):
    _pause()
    if str(confirm).strip() != str(sid):
        raise Fail("подтверждение не совпало с номером ВМ %s" % sid)
    with _lock:
        _srv(sid)
        del S[int(sid)]
    return {"deleted": int(sid)}


def dns_fix(target, dns=None):
    _pause()
    if target != "host":
        _srv(target)
    return {"target": target, "dns": (dns or "1.1.1.1 8.8.8.8").split(),
            "checked": ["github.com", "docs.google.com", "mobileproxy.space"]}


def set_password(target, password):
    _pause()
    if target != "host":
        _srv(target)
    if len(password) < 6:
        raise Fail("пароль root короче 6 знаков — придумай длиннее")
    return {"target": target}


def set_key(target, pubkey=None, rotate=False):
    _pause()
    if target != "host":
        _srv(target)
    if rotate:
        return {"target": target, "fingerprint": "SHA256:" + _pw("rot%s%s" % (target, time.time()), 20)}
    return {"target": target, "added": (pubkey or "").split()[-1][:40]}


def mpspace(target, action, **kw):
    _pause()
    srv = _srv(target)
    if action == "check":
        return {"installed": bool(srv["mpspace"]), "running": srv["mpspace"] == "running",
                "auth_mp": srv["auth_mp"], "version": "2.4.1" if srv["mpspace"] else None}
    if action == "auth":
        if not kw.get("auth"):
            raise Fail("auth.mp пустой — скопируйте его из ЛК mobileproxy.space")
        srv["auth_mp"] = True
        return {"saved": "/root/auth.mp"}
    if action == "install":
        def done():
            with _lock:
                srv["mpspace"] = "running"
            return {"installed": True}
        return _job("mp.space на ВМ %s" % target, ["зеркала apt…", "пакеты mobileproxy.space…",
                                                  "auth.mp на месте", "DNS: повтор фикса после установки",
                                                  "служба работает"], done)
    raise Fail("mp.space: нет действия %s" % action)


def update(target):
    _pause()
    what = "хост и все серверы" if target in ("host", None) else "ВМ %s" % target
    if target not in ("host", None):
        _srv(target)
    return _job("обновить %s" % what, ["PCS %s: код скачан, сумма сходится" % VERSION, "хост: /opt/pcs обновлён",
                                      "серверы: /opt/pcs и setup частей…", "всё одной версии %s" % VERSION],
                lambda: {"version": VERSION})


def job(job_id):
    _pause()
    with _lock:
        j = _jobs.get(job_id)
        if j is None:
            raise Fail("задания %s нет (панель перезапускалась?)" % job_id)
        return copy.deepcopy(j)


def term_argv(target):
    if target != "host":
        _srv(target)
    banner = "фейк: консоль %s — это локальная оболочка, не сервер" % target
    return ["/bin/sh", "-c", 'printf "%s\\r\\n" "$0"; exec "${SHELL:-/bin/sh}" -i', banner]


# ── run: команды частей ─────────────────────────────────────────────────────

def run(sid, part, args, input=None):
    _pause()
    args = [a for a in args if a != "--json"]
    try:
        with _lock:
            if sid is None:
                return {"ok": True, "data": _host_run(part, args)}
            srv = _srv(sid)
            fn = {"proxyveth": _pv, "modlink": _ml, "hivelink": _hl}.get(part)
            if fn is None:
                raise Fail("нет части %s" % part)
            return {"ok": True, "data": fn(srv, args, input)}
    except Fail as e:
        return {"ok": False, "error": str(e)}


def _host_run(part, args):
    if part != "hivelink":
        raise Fail("%s на хосте не ставится — только на серверах" % part)
    if args[:1] == ["status"]:
        return {"installed": True, "version": VERSION,
                "modems": [{"n": 1, "iface": "enx0c5b8f279a64", "state": "ok", "imei": "86xxxxxxxxx0412"},
                           {"n": 2, "iface": "enx0c5b8f279a65", "state": "ok", "imei": "86xxxxxxxxx0533"},
                           {"n": 3, "iface": "enx0c5b8f279a66", "state": "ok", "imei": "86xxxxxxxxx0871"}]}
    if args[:1] == ["install"]:
        return {"installed": True, "version": VERSION}
    raise Fail("hivelink %s: в фейке нет" % " ".join(args))


def _pv(srv, a, inp):
    cmd = a[0] if a else ""
    if cmd == "status":
        rows = [_row(m) for m in srv["modems"]]
        if "--wan" in a:
            time.sleep(0.5)
        return rows
    if cmd == "problems":
        return [_row(m) for m in srv["modems"] if m["state"] != "ok"]
    if cmd == "source":
        if len(a) == 1:
            return dict(srv["source"])
        if a[1] == "local":
            srv["source"]["source"] = "local"
        else:
            srv["source"].update(source="google", url=a[1], synced=_now())
        return dict(srv["source"])
    if cmd == "mode":
        if len(a) == 1:
            return {"mode": srv["mode"]}
        if a[1] not in ("usb", "gw"):
            raise Fail("режим: usb или gw")
        if "--yes" not in a:
            raise Fail("смена режима снимет все модемы — нужен --yes")
        srv["mode"] = a[1]
        for m in srv["modems"]:
            m["iface"] = "pv%d" % m["n"] if a[1] == "gw" else m["iface"]
        return {"mode": a[1], "recreated": [m["n"] for m in srv["modems"]]}
    if cmd == "table":
        sub = a[1] if len(a) > 1 else "show"
        if sub == "show":
            return {"csv": srv["table"], "path": "/etc/proxyveth/table.csv", "source": srv["source"]["source"]}
        if sub == "pull":
            srv["source"]["synced"] = _now()
            return {"csv": srv["table"]}
        if sub == "put":
            if not inp:
                raise Fail("таблица не пришла на stdin")
            d, dis, inv, probs = ptable.lint(inp, taken_octets={88}, mp_tables=srv["mode"] == "usb")
            if not d and not dis:
                raise Fail("в таблице нет годных строк: %s" % "; ".join(probs[:3]))
            srv["table"] = inp
            return {"saved": "/etc/proxyveth/table.csv", "rows": len(d), "disabled": sorted(dis),
                    "invalid": sorted(inv), "problems": probs}
        raise Fail("proxyveth table %s: в панели нет" % sub)
    if cmd == "lint":
        d, dis, inv, probs = ptable.lint(srv["table"], taken_octets={88}, mp_tables=srv["mode"] == "usb")
        return {"rows": len(d), "disabled": sorted(dis), "invalid": sorted(inv), "problems": probs}
    if cmd == "sync":
        d, dis, inv, probs = ptable.lint(srv["table"], taken_octets={88}, mp_tables=srv["mode"] == "usb")
        have = {m["n"]: m for m in srv["modems"]}
        gone = [n for n in have if n not in d]
        if have and len(gone) * 2 > len(have) and "--force" not in a:
            raise Fail("таблица требует снести %d из %d модемов — похоже на ошибку в таблице; "
                       "если так и надо — синхронизировать с --force" % (len(gone), len(have)))
        new = []
        for n, p in sorted(d.items()):
            if n not in have:
                new.append(n)
        keep = [m for m in srv["modems"] if m["n"] in d]
        add = _modems(srv["id"], srv["mode"], new)
        for m in add:
            m["real"], m["_login"], m["_pw"] = d[m["n"]]["real"], d[m["n"]]["user"], d[m["n"]]["pw"]
            m["proxy"] = "%s:%d" % (d[m["n"]]["host"], d[m["n"]]["port"])
        srv["modems"] = sorted(keep + add, key=lambda m: m["n"])
        srv["source"]["synced"] = _now()
        return {"created": new, "recreated": [], "removed": gone, "failed": {}, "problems": probs}
    if cmd == "diag":
        n = int(a[1]) if len(a) > 1 and a[1].isdigit() else None
        m = next((m for m in srv["modems"] if m["n"] == n), None)
        if m is None:
            raise Fail("модема %s нет в таблице" % (a[1] if len(a) > 1 else "?"))
        bad = m["state"] != "ok"
        steps = [{"name": "прокси", "ok": not bad, "text": "не отвечает за 5 с" if bad else "отвечает, 38 мс"},
                 {"name": "логин", "ok": not bad, "text": "не проверить" if bad else "принят"},
                 {"name": "модем", "ok": not bad, "text": "—" if bad else "HiLink 192.168.%d.1" % m["real"]},
                 {"name": "SIM", "ok": not bad, "text": "—" if bad else "зарегистрирована, LTE"},
                 {"name": "интернет", "ok": not bad, "text": "—" if bad else "внешний IP %s" % m["ext_ip"]}]
        return {"n": n, "steps": steps, "verdict": "proxy-down: прокси не отвечает — проверьте мини-сервер "
                                                   "модема %d" % m["real"] if bad else "всё в порядке"}
    if cmd in ("up", "down", "restart"):
        return {"done": a[1:] or ["all"]}
    raise Fail("proxyveth %s: в фейке нет" % " ".join(a))


def _ml_find(srv, rid):
    r = next((r for r in srv["modlink"] if str(r["id"]) == str(rid)), None)
    if r is None:
        raise Fail("строки %s нет — modlink list" % rid)
    return r


def _ml_free(srv, key, base):
    used = {r[key] for r in srv["modlink"]}
    return next(p for p in itertools.count(base) if p not in used)


def _ml(srv, a, inp):
    cmd = a[0] if a else ""
    if cmd == "list":
        return copy.deepcopy(srv["modlink"])
    if cmd == "status":
        en = [r for r in srv["modlink"] if r["enabled"]]
        return {"service": "работает", "sing_box": "работает", "proxies": len(srv["modlink"]),
                "enabled": len(en), "rows": [{"id": r["id"], "state": "ok" if r["enabled"] else "off",
                                              "ext_ip": "94.25.%d.%d" % (110 + r["id"], 20 + r["id"])
                                              if r["enabled"] else None} for r in srv["modlink"]]}
    if cmd == "add":
        opts = dict(zip(a[1::2], a[2::2]))
        if "--lan-ip" not in opts:
            raise Fail("нужен --lan-ip")
        rid = max([r["id"] for r in srv["modlink"]] + [0]) + 1
        pw = opts.get("--password", "gen")
        port = opts.get("--port", "auto")
        rport = opts.get("--reconnect-port", "auto")
        row = {"id": rid, "enabled": True, "name": opts.get("--name", "p%d" % rid),
               "login": opts.get("--login", "u%d" % rid),
               "password": _pw("gen%s%s" % (rid, time.time())) if pw == "gen" else pw,
               "port": _ml_free(srv, "port", 20000) if port == "auto" else int(port),
               "lan_ip": opts["--lan-ip"], "modem_ip": opts.get("--modem-ip", ""),
               "reconnect_port": _ml_free(srv, "reconnect_port", 21000) if rport == "auto" else int(rport),
               "interval_min": int(opts.get("--interval", 0))}
        if any(r["port"] == row["port"] for r in srv["modlink"]):
            raise Fail("порт %d уже занят строкой modlink" % row["port"])
        srv["modlink"].append(row)
        return {"id": rid}
    if cmd == "set":
        r = _ml_find(srv, a[1])
        for kv in a[2:]:
            k, _, v = kv.partition("=")
            if k not in r or k == "id":
                raise Fail("нет поля %s" % k)
            if k == "password" and v == "gen":
                v = _pw("gen%s%s" % (r["id"], time.time()))
            if k in ("port", "reconnect_port", "interval_min"):
                v = int(v)
                if k == "port" and any(x["port"] == v and x is not r for x in srv["modlink"]):
                    raise Fail("порт %d уже занят строкой modlink" % v)
            r[k] = v
        return {"id": r["id"]}
    if cmd in ("enable", "disable"):
        _ml_find(srv, a[1])["enabled"] = cmd == "enable"
        return {"id": int(a[1])}
    if cmd == "del":
        r = _ml_find(srv, a[1])
        srv["modlink"].remove(r)
        return {"deleted": r["id"]}
    if cmd == "apply":
        return {"config": "/etc/modlink/sing-box.json", "check": "ok", "restarted": True,
                "proxies": len([r for r in srv["modlink"] if r["enabled"]])}
    if cmd == "test":
        r = _ml_find(srv, a[1])
        if not r["enabled"]:
            raise Fail("строка %s выключена" % r["id"])
        return {"id": r["id"], "ext_ip": "94.25.%d.%d" % (110 + r["id"], 20 + r["id"]),
                "hilink": bool(r["modem_ip"]), "ms": 180 + 7 * r["id"]}
    if cmd in ("reconnect", "reboot"):
        r = _ml_find(srv, a[1])
        srv["ml_log"].setdefault(r["id"], []).append("%s %s по кнопке панели" % (
            time.strftime("%F %T"), "реконнект" if cmd == "reconnect" else "перезагрузка модема"))
        return {"id": r["id"], "done": cmd}
    if cmd == "log":
        r = _ml_find(srv, a[1])
        base = ["%s реконнект по таймеру, IP 94.25.%d.%d → 94.25.%d.%d" % (
            time.strftime("%F %T", time.localtime(time.time() - 600 * k)), 110 + k, 20 + k, 111 + k, 21 + k)
            for k in (3, 2, 1)]
        return base + srv["ml_log"].get(r["id"], [])
    if cmd == "export":
        return ["%s:%d:%s:%s\thttp://%s:%d/reconnect" % (srv["ip"], r["port"], r["login"], r["password"], srv["ip"],
                                                        r["reconnect_port"]) for r in srv["modlink"] if r["enabled"]]
    if cmd == "from-ifaces":
        have = {r["lan_ip"] for r in srv["modlink"]}
        return [{"lan_ip": "192.168.%d.100" % m["n"], "modem_ip": "192.168.%d.1" % m["n"], "name": "m%d" % m["n"]}
                for m in srv["modems"] if "192.168.%d.100" % m["n"] not in have]
    raise Fail("modlink %s: в фейке нет" % " ".join(a))


def _hl(srv, a, inp):
    cmd = a[0] if a else ""
    if cmd == "status":
        if not srv["hivelink"]:
            return {"installed": False, "modems": []}
        return {"installed": True, "version": VERSION, "modems": []}
    if cmd == "install":
        srv["hivelink"] = True
        return {"installed": True, "version": VERSION}
    if cmd == "uninstall":
        srv["hivelink"] = False
        return {"installed": False}
    raise Fail("hivelink %s: в фейке нет" % " ".join(a))


if __name__ == "__main__":       # быстрый взгляд: python3 -m pcs.web.fake_api
    import json
    json.dump({"host": host_info(), "servers": servers()}, sys.stdout, ensure_ascii=False, indent=1)
    print()
