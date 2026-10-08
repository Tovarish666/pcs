"""API хаба для веб-панели (docs/ARCHITECTURE.md, §9). Пишет A, использует E.

Все функции возвращают dict/list; ошибки — pcs.core.util.Fail (текст для человека).
Долгое — фоновым заданием: функция сразу отдаёт job_id, ход — job(job_id).

Значения, о которых сигнатуры §9 молчат:
  servers()[i]["state"]  running | stopped | paused | absent (ВМ нет в Proxmox)
                         | offline (ВМ работает, SSH не отвечает) | old (нет pcs-node — pcs update)
  servers()[i]["mode"]   "usb" | "gw" | None
  servers()[i]["mpspace"] None (не спрашивали) или {"installed", "ok", "units": {юнит: состояние}, "port", "auth"}
  target                 номер сервера (int или str) или "host"
"""
from .. import VERSION
from ..core.util import Fail
from . import jobs, node, ops, remote, servers as srv, store

PARTS = ("proxyveth", "modlink", "hivelink")


def _target(target):
    if target in (None, "", store.HOST, "хост"):
        return store.HOST
    return store.load(target)


def _server(id):
    t = _target(id)
    if t == store.HOST:
        raise Fail("это действие — для сервера, а не для хоста")
    return t


def host_info():
    """{"name", "kind": "proxmox", "version", "cpu", "ram", "disk", "hivelink": {...}, "ip", "dns"}."""
    return srv.host_overview()


def servers():
    """[{"id", "name", "ip", "state", "mode", "modems": {"ok","warn","broken","total"}, "mpspace", "version", "error"}]."""
    return [{k: v for k, v in d.items() if k != "info"} for d in srv.overview_all()]


def server(id):
    """То же, что в servers(), + версии частей и подробности (ядро, uptime, ресурсы, modlink, hivelink)."""
    s = _server(id)
    d = srv.overview(s, timeout=25)
    info = d.pop("info", None) or {}
    d["versions"] = {"host": VERSION, "server": info.get("version"),
                     "proxyveth": info.get("version") if (info.get("proxyveth") or {}).get("installed") else None,
                     "modlink": info.get("version") if (info.get("modlink") or {}).get("installed") else None,
                     "hivelink": info.get("version") if (info.get("hivelink") or {}).get("installed") else None}
    d["outdated"] = bool(info.get("version")) and info.get("version") != VERSION
    for k in ("kernel", "uptime", "cpu", "ram", "disk", "modlink", "hivelink", "dns", "hostname"):
        d[k] = info.get(k)
    d["proxyveth"] = info.get("proxyveth")
    d["net"] = s.get("net")
    return d


def create_server(params):
    """params: name, cores, ram_gb, disk_gb, mode, sheet, mpspace(bool)
    (+ необязательные id, ip "A.B.C.D/M", gw, dns, password, auth, proxy, storage, bridge). → job_id."""
    p = srv.normalize(params)
    p.pop("replace", None)
    return jobs.start("create_server", p, title="Создание ВМ %s" % (p.get("name") or p.get("id") or ""),
                      target=p.get("id") or "new")


def delete_server(id, confirm):
    """confirm должен совпасть с id — как ввод номера сервера в меню."""
    s = _server(id)
    if str(confirm).strip() != str(s["id"]):
        raise Fail("для удаления введи номер сервера: %s" % s["id"])
    if jobs.busy(target=s["id"]):
        raise Fail("с сервером %s идёт задание — дождись его" % s["id"])
    return srv.delete(s["id"])


def run(id, part, args, timeout=300):
    """Конверт {"ok", "data"|"error"} от `<part> <args> --json` на сервере; id=None — хост (там только hivelink)."""
    if part not in PARTS:
        raise Fail("часть — одна из: %s" % ", ".join(PARTS))
    if not isinstance(args, (list, tuple)) or not all(isinstance(a, (str, int)) for a in args):
        raise Fail("args — список строк")
    t = _target(id)
    if t == store.HOST:
        if part != "hivelink":
            raise Fail("на хосте есть только hivelink; %s — на серверах" % part)
        return remote.local_part(part, list(args), timeout=timeout)
    return remote.part(t, part, list(args), timeout=timeout)


def dns_fix(target, dns=None):
    """target: id сервера или "host"; dns — "1.1.1.1 8.8.8.8" или список. → сводка фикса."""
    from ..core import dns as dnsfix
    lst = None
    if dns:
        lst = dnsfix.parse_dns(" ".join(dns) if isinstance(dns, (list, tuple)) else str(dns))
    return ops.dns_fix(_target(target), lst)


def set_password(target, password):
    """Пустой password — сгенерировать; тогда он вернётся в ответе ("password")."""
    return ops.set_password(_target(target), password)


def set_key(target, pubkey=None, rotate=False):
    """pubkey — добавить ключ root; rotate — новый ключ хаба для сервера;
    ни того ни другого — список ключей root и ключ хаба."""
    t = _target(target)
    if rotate:
        return ops.rotate(t)
    if pubkey:
        return dict(ops.key_add(t, pubkey), target="host" if t == store.HOST else t["id"])
    return {"target": "host" if t == store.HOST else t["id"], "keys": ops.key_list(t), "hub": ops.hub_key()}


def mpspace(target, action, **kw):
    """install → job_id (kw: auth, proxy, no_reboot); auth → dict (kw: auth); check → dict."""
    s = _server(target)
    if action == "install":
        if jobs.busy("mpspace", s["id"]):
            raise Fail("mp.space на сервер %s уже ставится" % s["id"])
        return jobs.start("mpspace", {"id": s["id"], "auth": kw.get("auth") or "", "proxy": kw.get("proxy") or "",
                                      "no_reboot": bool(kw.get("no_reboot"))},
                          title="mp.space на сервер %s" % s["id"], target=s["id"])
    if action == "auth":
        if not kw.get("auth"):
            raise Fail("нужен auth.mp из ЛК: {\"auth\": \"…\", \"port\": …}")
        return remote.node(s, "mpspace", "auth", input=str(kw["auth"]).strip() + "\n", timeout=60)
    if action == "check":
        return remote.node(s, "mpspace", "check", timeout=60)
    raise Fail("действие mp.space — install, auth или check")


def update(target=None):
    """Не из §9, но нужно панели: обновить PCS. target: "host" (хост из GitHub и все серверы),
    id сервера (код хоста на него) или "all" (на все серверы). → job_id."""
    if target in (None, "", store.HOST):
        params, key = {"host": True, "servers": "all"}, "all"
    elif target == "all":
        params, key = {"servers": "all"}, "all"
    else:
        params, key = {"servers": [_server(target)["id"]]}, _server(target)["id"]
    if jobs.busy("update"):
        raise Fail("обновление уже идёт")
    return jobs.start("update", params, title="Обновление PCS (%s)" % key, target=key)


def job(job_id):
    """{"state": "running|done|failed", "log": [...], "result": ..., "error", "title", "kind", …}."""
    return jobs.get(job_id)


def term_argv(target):
    """argv для pty терминала: ssh -tt … или bash -l для хоста."""
    return ops.term_argv(_target(target))


# ── исполнители фоновых заданий (pcs job run ID) ──────────────────────────
def _job_create(p):
    return srv.create(p, select=False)


def _job_mpspace(p):
    s = store.load(p["id"])
    return srv.mpspace_install(s, p.get("auth"), p.get("proxy"), p.get("no_reboot"))


KINDS = {"create_server": _job_create, "mpspace": _job_mpspace, "update": srv.update}
summarize = node.summarize
