"""Присоединённые хосты PCS: VPS с Ubuntu и другие хосты Proxmox (PCS.md §19).

Связь — по API их панели: https://адрес:666/api/v1/…, токен Bearer и отпечаток сертификата,
запомненный при присоединении. Логин и пароль панели нужны один раз — на присоединение,
не хранятся. Подменить хост посредине нельзя: другой сертификат — отказ до отправки токена.

    /etc/pcs/nodes/<имя>.json  {"name", "host", "port", "fingerprint", "token", "token_id",
                                "kind", "remote_name", "version", "added"}   (600)
"""
import http.client
import json
import os
import re
import socket
import ssl
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor

from .. import VERSION
from ..core.util import Fail, jload, jsave
from ..web import tls
from . import store

PORT = 666


def ndir():
    return os.path.join(store.ETC, "nodes")


def path(name):
    return os.path.join(ndir(), "%s.json" % slug(name))


def slug(name):
    s = re.sub(r"[^a-z0-9-]+", "-", str(name or "").strip().lower()).strip("-")[:40]
    if not s:
        raise Fail("имя хоста — латиница, цифры и дефис")
    return s


def parse_addr(addr):
    """«95.165.83.236», «95.165.83.236:666», «https://vps.example:666/» → (хост, порт)."""
    a = str(addr or "").strip()
    if not a:
        raise Fail("нужен адрес хоста: IP или имя, можно с портом")
    if "://" not in a:
        a = "https://" + a
    u = urllib.parse.urlsplit(a)
    host = u.hostname
    if not host or not re.fullmatch(r"[A-Za-z0-9.\-:]+", host):
        raise Fail("адрес «%s» — не IP и не имя хоста" % addr)
    try:
        port = u.port or PORT
    except ValueError:
        raise Fail("порт в адресе «%s» — не число" % addr)
    return host, port


def request(host, port, method, rpath, body=None, token=None, fingerprint=None, timeout=20, joining=False):
    """Запрос к панели хоста по HTTPS. Отпечаток сверяется ДО отправки чего-либо.
    → (HTTP-код, JSON, отпечаток)."""
    conn = http.client.HTTPSConnection(host, port, context=tls.client_context(), timeout=timeout)
    try:
        try:
            conn.connect()
        except (OSError, ssl.SSLError) as e:
            raise Fail("хост %s:%d не отвечает по HTTPS (%s) — открыт ли порт %d и стоит ли там PCS 5 со связью хостов"
                       % (host, port, getattr(e, "strerror", None) or e, port))
        fp = tls.fingerprint_der(conn.sock.getpeercert(binary_form=True))
        if fingerprint and fp != fingerprint:
            if joining:
                raise Fail("сертификат хоста %s:%d не совпал с указанным отпечатком (там %s) — это может быть подмена; "
                           "сверь отпечаток в панели того хоста: настройка → «Связь хостов»" % (host, port, fp))
            raise Fail("сертификат хоста %s:%d не тот, что при присоединении — это может быть подмена. "
                       "Если хост переустановлен: pcs node remove и присоединить заново" % (host, port))
        hdr = {"Accept": "application/json", "User-Agent": "pcs-hub/" + VERSION}
        data = None
        if body is not None:
            data = json.dumps(body, ensure_ascii=False).encode("utf-8")
            hdr["Content-Type"] = "application/json"
        if token:
            hdr["Authorization"] = "Bearer " + token
        try:
            conn.request(method, rpath, body=data, headers=hdr)
            r = conn.getresponse()
            raw = r.read()
        except (OSError, http.client.HTTPException, ssl.SSLError) as e:
            raise Fail("хост %s:%d: связь оборвалась (%s)" % (host, port, e))
        try:
            obj = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            raise Fail("хост %s:%d ответил не JSON (HTTP %d) — там старая версия PCS? pcs update на нём"
                       % (host, port, r.status))
        return r.status, obj, fp
    finally:
        conn.close()


def load(name):
    rec = jload(path(name), None)
    if not rec:
        raise Fail("хоста «%s» нет среди присоединённых — список: pcs node list" % name)
    return rec


def public(rec):
    return {k: v for k, v in rec.items() if k != "token"}


def listing():
    d = ndir()
    out = []
    for f in sorted(os.listdir(d)) if os.path.isdir(d) else []:
        if f.endswith(".json"):
            rec = jload(os.path.join(d, f), None)
            if rec:
                out.append(public(rec))
    return out


def join(addr, user, password, name=None, fingerprint=None, hub_name=None):
    """Присоединить хост: его логин и пароль панели → токен. → запись (без токена) и сводка хоста."""
    host, port = parse_addr(addr)
    want = tls.norm_fp(fingerprint) or None
    if not user or not password:
        raise Fail("нужны логин и пароль панели того хоста")
    status, obj, fp = request(host, port, "POST", "/api/v1/join",
                              {"user": user, "password": password, "name": hub_name or socket.gethostname()},
                              fingerprint=want, timeout=40, joining=True)
    if not obj.get("ok"):
        raise Fail("хост %s не присоединился: %s" % (host, obj.get("error") or "HTTP %d" % status))
    d = obj.get("data") or {}
    info = d.get("host") or {}
    nm = slug(name or info.get("name") or host)
    old = jload(path(nm), None)
    if old and (old.get("host"), old.get("port")) != (host, port):
        raise Fail("имя «%s» уже занято хостом %s:%s — дайте другое имя" % (nm, old.get("host"), old.get("port")))
    rec = {"name": nm, "host": host, "port": port, "fingerprint": fp, "token": d.get("token"),
           "token_id": d.get("token_id"), "kind": info.get("kind"), "remote_name": info.get("name"),
           "version": d.get("version"), "added": int(time.time())}
    if not rec["token"]:
        raise Fail("хост %s не выдал токен — там старая версия PCS?" % host)
    os.makedirs(ndir(), mode=0o700, exist_ok=True)
    jsave(path(nm), rec, 0o600)
    return dict(public(rec), info=info)


def call(name, method, rpath, body=None, timeout=60):
    """Запрос к API присоединённого хоста. rpath — как у панели без /api («targets/host/proxyveth»).
    → конверт {"ok", "data"|"error"}."""
    rec = load(name)
    rpath = rpath.lstrip("/")
    status, obj, _ = request(rec["host"], rec["port"], method, "/api/v1/" + rpath, body,
                             token=rec["token"], fingerprint=rec.get("fingerprint"), timeout=timeout)
    if status == 401:
        return {"ok": False, "error": "хост %s не принял токен — присоедините его заново" % name}
    if not isinstance(obj, dict) or "ok" not in obj:
        return {"ok": False, "error": "хост %s: ответ без конверта (HTTP %d)" % (name, status)}
    return obj


def remove(name):
    """Отсоединить: хост отзывает наш токен (если отвечает), запись — удаляем."""
    rec = load(name)
    revoked = False
    try:
        revoked = bool(call(name, "POST", "leave", {}, timeout=15).get("ok"))
    except Fail:
        pass
    os.unlink(path(rec["name"]))
    return {"name": rec["name"], "revoked": revoked}


def login_url(name, scheme="http"):
    """Одноразовая ссылка входа в панель хоста (60 с). http — как обычно открывают панель."""
    rec = load(name)
    e = call(name, "POST", "login-link", {}, timeout=15)
    if not e.get("ok"):
        raise Fail(e.get("error") or "хост %s не дал ссылку входа" % name)
    host = "[%s]" % rec["host"] if ":" in rec["host"] else rec["host"]
    return {"url": "%s://%s:%d%s" % (scheme, host, rec["port"], e["data"]["path"]), "ttl": e["data"].get("ttl", 60)}


def overview_one(rec, timeout=10):
    out = dict(public(rec), ok=False, error=None, host_info=None, servers=[])
    try:
        e = call(rec["name"], "GET", "overview", timeout=timeout)
    except Fail as x:
        out["error"] = str(x)
        return out
    if not e.get("ok"):
        out["error"] = e.get("error")
        return out
    d = e.get("data") or {}
    out.update(ok=True, host_info=d.get("host"), servers=d.get("servers") or [],
               error=d.get("host_error") or d.get("servers_error"))
    return out


def overview(timeout=10):
    """Все присоединённые хосты параллельно; недоступный — с ошибкой, а не падение всего."""
    recs = []
    for n in listing():
        try:
            recs.append(load(n["name"]))
        except Fail:
            pass
    if not recs:
        return []
    with ThreadPoolExecutor(max_workers=min(8, len(recs))) as ex:
        return list(ex.map(lambda r: overview_one(r, timeout), recs))
