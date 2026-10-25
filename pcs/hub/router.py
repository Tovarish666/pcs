"""Роутер этого хоста (PCS.md §20): доступ хранится здесь же, в /etc/pcs/router.json (0600).

У каждого хоста — свой роутер и свой доступ: хосты в вакууме (§19.4); роутер другого хоста —
телепортом в его панель. Адрес по умолчанию — шлюз маршрута по умолчанию (192.168.88.1).

    call("status") / call("save", {"host", "user", "password", "https", "port", "hidden"}) / …
"""
import os
import re
import time

from pcs.core import mikrotik
from pcs.core.util import Fail, jload, jsave, rd, sh
from . import store

FIELDS = ("host", "user", "password", "https", "port", "hidden")


def path():
    return os.path.join(store.ETC, "router.json")


class FileConf:
    """Доступ к роутеру на диске хоста (у фейковой панели — в памяти, с тем же load/save)."""

    def load(self):
        c = jload(path(), {})
        return c if isinstance(c, dict) else {}

    def save(self, c):
        jsave(path(), c, 0o600)


FILE = FileConf()


def load():
    return FILE.load()


def gateway():
    """Шлюз маршрута по умолчанию этого хоста (обычно это и есть роутер)."""
    try:
        m = re.search(r"default via (\S+)", sh("ip", "-4", "route", "show", "default", check=False).stdout or "")
    except Fail:
        return ""
    return m.group(1) if m else ""


def my_mac(ip):
    """MAC интерфейса, через который хост ходит к ip: по нему роутер знает, на каком порту моста хост."""
    try:
        m = re.search(r"\bdev (\S+)", sh("ip", "-4", "route", "get", ip, check=False).stdout or "") if ip else None
    except Fail:
        return ""
    return rd("/sys/class/net/%s/address" % m.group(1)).strip() if m else ""


def brief(io=FILE):
    """Для дерева и обзора — без сети: подключён ли, как зовут, скрыт ли."""
    c = io.load()
    return {"configured": bool(c.get("user")), "hidden": bool(c.get("hidden")), "host": c.get("host") or "",
            "name": c.get("identity") or "", "model": c.get("model") or ""}


def public(c):
    return {"host": c.get("host") or "", "default_host": gateway(), "user": c.get("user") or "",
            "password_set": bool(c.get("password")), "https": bool(c.get("https")), "port": c.get("port") or None,
            "hidden": bool(c.get("hidden")), "configured": bool(c.get("user"))}


def client(c=None, make=mikrotik.Client):
    c = c if c is not None else load()
    if not c.get("user"):
        raise Fail("роутер не подключён: укажи адрес, логин и пароль (вкладка «подключение»)")
    host = c.get("host") or gateway()
    if not host:
        raise Fail("не знаю адрес роутера: у хоста нет маршрута по умолчанию — впиши адрес")
    return make(host, c.get("user"), c.get("password") or "", https=c.get("https"), port=c.get("port"))


def _host_of(cl):
    return getattr(cl, "host", None) or "192.168.88.1"


def save(p, make=mikrotik.Client, io=FILE):
    """Проверить и сохранить доступ. Пароль пустой — прежний. hidden — только спрятать/показать в дереве."""
    c = io.load()
    if set(p) <= {"hidden"}:
        c["hidden"] = bool(p.get("hidden"))
        io.save(c)
        return public(c)
    host = str(p.get("host") or "").strip()
    if host and not re.fullmatch(r"[A-Za-z0-9.-]{1,253}|\[?[0-9A-Fa-f:]{2,39}\]?", host):
        raise Fail("адрес роутера: IP или имя, например 192.168.88.1")
    user = str(p.get("user") or "").strip()
    if not user or len(user) > 64 or re.search(r"[\s:\x00-\x1f]", user):
        raise Fail("логин роутера: без пробелов и двоеточий")
    pw = p.get("password")
    pw = c.get("password") or "" if pw in (None, "") else str(pw)
    if len(pw) > 200 or re.search(r"[\x00-\x1f]", pw):
        raise Fail("пароль: до 200 знаков")
    port = p.get("port")
    if port not in (None, "", 0):
        try:
            port = int(port)
        except (TypeError, ValueError):
            raise Fail("порт: число")
        if not 1 <= port <= 65535:
            raise Fail("порт: 1–65535")
    else:
        port = None
    new = dict(c, host=host, user=user, password=pw, https=bool(p.get("https")), port=port,
               hidden=bool(p.get("hidden", c.get("hidden"))))
    st = mikrotik.status(client(new, make))          # не пускает — не сохраняем
    new.update(identity=st["identity"], model=st["board"] or st["model"], checked=int(time.time()))
    io.save(new)
    return dict(public(new), status=st)


def forget(io=FILE):
    c = io.load()
    for k in ("user", "password", "identity", "model", "checked"):
        c.pop(k, None)
    io.save(c)
    return public(c)


def call(action, p=None, make=mikrotik.Client, io=FILE):
    """Действие с роутером → данные для панели. Ошибки — Fail с текстом для человека."""
    p = p or {}
    if action == "conf":
        return public(io.load())
    if action == "save":
        return save(p, make, io)
    if action == "forget":
        return forget(io)
    c = io.load()
    cl = client(c, make)
    rip = _host_of(cl)
    if action == "status":
        st = mikrotik.status(cl)
        if (st["identity"], st["board"] or st["model"]) != (c.get("identity"), c.get("model")):
            c.update(identity=st["identity"], model=st["board"] or st["model"])
            io.save(c)
        return dict(st, where=cl.where(), webfig=cl.where() + "/")
    if action == "ifaces":
        return mikrotik.interfaces(cl, rip, my_mac(rip))
    if action == "iface":
        return mikrotik.iface_set(cl, p.get("id"), bool(p.get("disabled")), rip, my_mac(rip))
    if action == "nat":
        return {"rules": mikrotik.nat(cl), "wan": mikrotik.wan_lists(cl), "lan": [str(n) for n in mikrotik.lan_nets(cl)]}
    if action == "nat_add":
        return mikrotik.nat_add(cl, p.get("proto") or "tcp", p.get("port"), p.get("to"), p.get("to_port") or "",
                                p.get("comment") or "", mikrotik.lan_nets(cl))
    if action == "nat_set":
        return mikrotik.nat_set(cl, p.get("id"), bool(p.get("disabled")))
    if action == "nat_del":
        return mikrotik.nat_del(cl, p.get("id"))
    if action == "leases":
        return mikrotik.leases(cl)
    if action == "lease_static":
        return mikrotik.lease_static(cl, p.get("id"))
    if action == "log":
        return mikrotik.log(cl)
    if action == "reboot":
        return mikrotik.reboot(cl)
    raise Fail("роутер: нет такого действия")
