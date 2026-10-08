"""Что хаб помнит о серверах: /etc/pcs/servers/<id>.json и /etc/pcs/active.

Формат записей PCS 4 читается как есть: {"id", "name", "ip", "password", "net",
"created", "pcs", "agent"}. PCS 5 добавляет по необходимости "port" (SSH),
"key" (свой ключ после pcs key --rotate), "mode", "version".
Выбранным может быть и сам хост: в active тогда «host».
"""
import os
import re

from ..core.util import Fail, jload, jsave

ETC = os.environ.get("PCS_ETC", "/etc/pcs")
HOST = "host"


def srv_dir():
    return os.path.join(ETC, "servers")


def key_path():
    return os.path.join(ETC, "ssh", "id_ed25519")


def known_hosts():
    return os.path.join(ETC, "ssh", "known_hosts")


def path(sid):
    return os.path.join(srv_dir(), "%s.json" % sid)


def norm_id(sid):
    """'2000' → '2000'; 'host'/'хост' → 'host'; мусор — Fail."""
    s = str(sid).strip().lower()
    if s in ("host", "хост"):
        return HOST
    if not re.fullmatch(r"\d{1,9}", s):
        raise Fail("номер сервера — это число (VM ID), а не %r" % str(sid))
    return str(int(s))


def load(sid):
    sid = norm_id(sid)
    if sid == HOST:
        raise Fail("это хост, а не сервер: выбери ВМ (pcs server use ID)")
    s = jload(path(sid), None)
    if not isinstance(s, dict):
        raise Fail("сервер %s PCS не знает: pcs server adopt %s (список — pcs server list)" % (sid, sid))
    s["id"] = int(s.get("id") or sid)
    s.setdefault("name", "vm%s" % sid)
    s.setdefault("ip", "")
    return s


def save(s):
    os.makedirs(srv_dir(), mode=0o700, exist_ok=True)
    jsave(path(s["id"]), s, 0o600)


def drop(sid):
    try:
        os.unlink(path(sid))
    except OSError:
        pass
    if active() == str(sid):
        try:
            os.unlink(os.path.join(ETC, "active"))
        except OSError:
            pass


def all_servers():
    out = []
    d = srv_dir()
    names = [n for n in os.listdir(d) if re.fullmatch(r"\d+\.json", n)] if os.path.isdir(d) else []
    for n in sorted(names, key=lambda x: int(x[:-5])):
        try:
            out.append(load(n[:-5]))
        except Fail:
            pass
    return out


def active():
    try:
        with open(os.path.join(ETC, "active")) as f:
            v = f.read().strip()
    except OSError:
        return ""
    try:
        return norm_id(v) if v else ""
    except Fail:
        return ""


def set_active(sid):
    sid = norm_id(sid)
    if sid != HOST:
        load(sid)
    os.makedirs(ETC, mode=0o700, exist_ok=True)
    with open(os.path.join(ETC, "active"), "w") as f:
        f.write(sid + "\n")
    return sid


def target(server=None, host=False, default_host=False):
    """Кому команда: 'host' или запись сервера. Явное --server/--host важнее выбранного."""
    if host:
        return HOST
    sid = norm_id(server) if server not in (None, "") else (active() or (HOST if default_host else ""))
    if not sid:
        raise Fail("сервер не выбран: pcs server use ID (список — pcs server list) или --server ID")
    return HOST if sid == HOST else load(sid)


def label(t):
    return "хост" if t == HOST else "сервер %s (%s)" % (t["id"], t.get("ip") or "?")
