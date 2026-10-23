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
import os

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


def target_info(target):
    """Сводка цели: сам хост (host_info) или сервер (server)."""
    return host_info() if _target(target) == store.HOST else server(target)


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


def run(id, part, args, timeout=300, input=None):
    """Конверт {"ok", "data"|"error"} от `<part> <args> --json` на сервере; id=None — хост (там hivelink и modlink).
    input — текст на stdin команды (таблица proxyveth, пароль modlink): секреты не в командной строке."""
    if part not in PARTS:
        raise Fail("часть — одна из: %s" % ", ".join(PARTS))
    if not isinstance(args, (list, tuple)) or not all(isinstance(a, (str, int)) for a in args):
        raise Fail("args — список строк")
    t = _target(id)
    if t == store.HOST:
        if part not in remote.HOST_PARTS:
            raise Fail("на хосте есть hivelink и modlink; %s — на серверах" % part)
        return remote.local_part(part, list(args), timeout=timeout, input=input)
    return remote.part(t, part, list(args), timeout=timeout, input=input)


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
    """install → job_id (kw: auth, proxy, no_reboot); auth → dict (kw: auth); check → dict.
    target "host" — сам хост, только Ubuntu (VPS): на Proxmox mp.space положил бы все ВМ."""
    if _target(target) == store.HOST:
        ok_, why = srv.mpspace_allowed()
        if action == "check":
            return node.mpspace_info()
        if not ok_:
            raise Fail(why)
        if action == "install":
            if jobs.busy("mpspace_host", "host"):
                raise Fail("mp.space на этот хост уже ставится")
            return jobs.start("mpspace_host", {"auth": kw.get("auth") or "", "proxy": kw.get("proxy") or "",
                                               "no_reboot": bool(kw.get("no_reboot"))},
                              title="mp.space на этот хост", target="host")
        if action == "auth":
            if not kw.get("auth"):
                raise Fail("нужен auth.mp из ЛК: {\"auth\": \"…\", \"port\": …}")
            return node.mp_auth(str(kw["auth"]).strip())
        raise Fail("действие mp.space — install, auth или check")
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


LONG = {"proxyveth": ("setup", "mode", "sync", "up", "down", "restart", "on", "off"),
        "modlink": ("setup", "migrate", "uninstall"),
        "hivelink": ("install", "uninstall")}


def is_long(part, args):
    """Долгая команда части — её панель запускает заданием, а не держит запрос минутами."""
    a = [str(x) for x in args]
    if not a or a[0] not in LONG.get(part, ()) or "--dry-run" in a:
        return False
    if part == "proxyveth" and a[0] in ("up", "down", "restart"):
        return len(a) > 1 and a[1] == "all"
    return True


def run_job(id, part, args, input=None):
    """Долгая команда части — фоновым заданием: обрыв связи с панелью ей не мешает. → job_id."""
    if part not in PARTS:
        raise Fail("часть — одна из: %s" % ", ".join(PARTS))
    t = _target(id)
    key = "host" if t == store.HOST else t["id"]
    if t == store.HOST and part not in remote.HOST_PARTS:
        raise Fail("на хосте есть hivelink и modlink; %s — на серверах" % part)
    if jobs.busy("part", key):
        raise Fail("на %s уже идёт долгая команда — дождись её (pcs job)" % ("хосте" if key == "host" else "сервере %s" % key))
    return jobs.start("part", {"id": None if key == "host" else key, "part": part, "args": [str(x) for x in args],
                               "input": input},
                      title="%s %s — %s" % (part, " ".join(str(x) for x in args), "хост" if key == "host" else "ВМ %s" % key),
                      target=key)


# ── управление ВМ ──────────────────────────────────────────────────────────
def vm_power(id, action):
    """start | shutdown | reboot | stop — через Proxmox."""
    s = _server(id)
    if jobs.busy(target=s["id"]):
        raise Fail("с сервером %s идёт задание — дождись его" % s["id"])
    return srv.power(s["id"], action)


def server_forget(id, confirm):
    """Убрать из PCS (ВМ не трогается). confirm должен совпасть с id."""
    s = _server(id)
    if str(confirm).strip() != str(s["id"]):
        raise Fail("чтобы убрать из PCS, введи номер сервера: %s" % s["id"])
    if jobs.busy(target=s["id"]):
        raise Fail("с сервером %s идёт задание — дождись его" % s["id"])
    return srv.forget(s["id"])


def server_color(id, color):
    """Цвет сервера в дереве: red, orange, yellow, green, cyan, blue, violet или "" (по номеру)."""
    return srv.set_color(_server(id)["id"], color)


def tunnel_argv(id):
    """argv ssh хаба до сервера (последний — root@IP): по нему браузер веба модемов ходит к модемам ВМ."""
    s = _server(id)
    remote.call(s, "true", timeout=30)        # заодно: ключ сменился — сверить и принять сейчас
    return remote.base(s)


def host_key(id, accept=None):
    """Ключ SSH сервера: что знает хаб, что показывает сервер, что видит guest agent (known, shown,
    agent, same, agent_ok). accept="SHA256:…" — принять этот; accept="agent" — принять подтверждённый агентом."""
    s = _server(id)
    if accept == "agent":
        return dict(remote.rekey(s), accepted=True)
    if accept:
        return dict(remote.rekey(s, accept=str(accept).strip()), accepted=True)
    return remote.host_key(s)


def vms_other():
    """ВМ этого Proxmox, которых нет в PCS."""
    return srv.other_vms()


def server_adopt(id, ip=None, password=None):
    """Добавить в PCS ВМ, которая уже есть в Proxmox (заданием: ключ хаба, код, части). → job_id."""
    sid = store.norm_id(id)
    if sid == store.HOST:
        raise Fail("номер ВМ — число")
    if jobs.busy("adopt", sid):
        raise Fail("ВМ %s уже добавляется" % sid)
    return jobs.start("adopt", {"id": sid, "ip": ip or "", "password": password or ""},
                      title="Добавить ВМ %s в PCS" % sid, target=sid)


def _job_adopt(p):
    return srv.adopt(p["id"], ip=p.get("ip") or "", password=p.get("password") or "", select=False)


def colors():
    return [{"id": c, "name": srv.COLOR_RU[c]} for c in srv.COLORS]


# ── присоединённые хосты (VPS и другие хосты Proxmox) ─────────────────────
def nodes():
    """Присоединённые хосты (без токенов)."""
    from . import nodes as nd
    return nd.listing()


def nodes_overview(timeout=10):
    """Сводка всех присоединённых хостов параллельно: [{name, host, port, kind, ok, error, host_info, servers}]."""
    from . import nodes as nd
    return nd.overview(timeout)


def node_join(addr, user, password, name=None, fingerprint=None):
    from . import nodes as nd
    return nd.join(addr, user, password, name=name, fingerprint=fingerprint)


def node_remove(name):
    from . import nodes as nd
    return nd.remove(name)


def node_call(name, method, path, body=None, timeout=60):
    """Запрос к API присоединённого хоста (path — как у панели без /api/). → конверт."""
    from . import nodes as nd
    return nd.call(name, method, path, body, timeout=timeout)


def node_ws(name, query):
    """WebSocket к терминалу присоединённого хоста. → (сокет, лишние байты)."""
    from . import nodes as nd
    return nd.ws_open(name, query)


def node_color(name, color):
    from . import nodes as nd
    return nd.set_color(name, srv.norm_color(color))


def node_login(name):
    """Одноразовая ссылка входа в панель присоединённого хоста (60 с)."""
    from . import nodes as nd
    return nd.login_url(name)


def self_fingerprint():
    """Отпечаток сертификата этой панели — сверить при присоединении с другого хоста."""
    from ..web import tls
    cert = tls.paths()[0]
    return tls.fingerprint_file(cert) if os.path.exists(cert) else ""


def job(job_id):
    """{"state": "running|done|failed", "log": [...], "result": ..., "error", "title", "kind", …}.
    И задания хаба, и фоновые запуски частей на хосте (pcs/core/bg)."""
    from ..core import bg
    return bg.get(job_id) if bg.is_id(job_id) else jobs.get(job_id)


def job_retry(job_id):
    """«Повторить» упавшее задание хаба (создание ВМ — продолжает ту же ВМ). → {"job": новый id}."""
    return {"job": jobs.retry(job_id)}


def jobs_recent(n=15):
    """Последние задания хаба и фоновые запуски на хосте, новые сверху (без журналов)."""
    from ..core import bg
    return sorted(jobs.recent(n) + bg.recent(n), key=lambda d: d.get("started") or 0, reverse=True)[:n]


def term_argv(target, serial=False):
    """argv для pty терминала: ssh -tt … или bash -l для хоста; serial — qm terminal (консоль ВМ без SSH)."""
    return ops.term_argv(_target(target), serial=serial)


def server_reconnect(id):
    """«Переподключить»: связь с ВМ по шагам (ВМ, агент, адрес, порт SSH, вход). → {"ok", "ip", "steps"}."""
    return srv.reconnect(_server(id)["id"])


# ── исполнители фоновых заданий (pcs job run ID) ──────────────────────────
def _job_create(p):
    return srv.create(p, select=False)


def _job_mpspace(p):
    s = store.load(p["id"])
    return srv.mpspace_install(s, p.get("auth"), p.get("proxy"), p.get("no_reboot"))


def _job_part(p):
    print("  ▸ %s %s на %s" % (p["part"], " ".join(p["args"]), "хосте" if p["id"] is None else "сервере %s" % p["id"]),
          flush=True)
    e = run(p["id"], p["part"], p["args"], timeout=3 * 3600, input=p.get("input"))
    if not e.get("ok"):
        raise Fail(e.get("error") or "ошибка без текста")
    return e.get("data")


def browser_install():
    """Браузер вкладок mp.space и ЮMoney (Firefox ESR, экран VNC) — на этот хост, заданием."""
    return jobs.start("browser_install", {}, title="Браузер для вкладок mp.space и ЮMoney", target="host")


def _job_browser_install(p):
    from ..web import browser
    return browser.install(log=lambda m: print("  " + m, flush=True))


def _job_host_setup(p):
    d = srv.host_setup(mode=p.get("mode"))
    if d["failed"]:
        raise Fail("не прошло: %s — см. журнал выше" % ", ".join(d["failed"]))
    return d


KINDS = {"create_server": _job_create, "mpspace": _job_mpspace, "update": srv.update, "part": _job_part,
         "host_setup": _job_host_setup, "mpspace_host": srv.mpspace_host, "adopt": _job_adopt,
         "browser_install": _job_browser_install}
summarize = node.summarize
