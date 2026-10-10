"""Доступы и DNS цели — хоста или сервера: DNS-фикс, пароль root, ключи SSH, порт SSH.

На хосте — функции pcs-node прямо здесь, на сервере — pcs-node по SSH.
"""
import os
import secrets
import time

from ..core import dns as dnsfix
from ..core import util
from ..core.util import Fail, sh
from . import node, remote, store, ui

HOST = store.HOST


def dns_fix(t, dns=None):
    if t == HOST:
        lk = util.lock(os.path.join(remote.RUN, "dns.lock"), what="правка DNS")
        try:
            return dnsfix.fix(dns, log=ui.info)
        finally:
            lk.close()
    return remote.node(t, "dns-fix", *(["--dns", " ".join(dns)] if dns else []), timeout=300)


def set_password(t, password=None):
    """Пароль root. Пустой — сгенерировать (и вернуть: его нужно показать человеку)."""
    generated = not password
    password = password or secrets.token_urlsafe(12)
    node.check_password(password)
    if t == HOST:
        util.need_root()
        node.passwd(password)
    else:
        remote.node(t, "passwd", input=password + "\n", timeout=60)
        t["password"] = password
        store.save(t)
    return {"target": "host" if t == HOST else t["id"], "changed": True,
            "password": password if generated else None}


def hub_key():
    pub = remote.ensure_key()
    k = node.parse_key(pub)
    return {"pub": pub, "fp": node.fingerprint(k[1]) if k else ""}


def key_list(t):
    if t == HOST:
        return node.keys()
    return remote.node(t, "key", timeout=30)


def key_add(t, pub):
    if not node.parse_key(pub or ""):
        raise Fail("это не открытый ключ SSH (ожидается «ssh-ed25519 AAAA… комментарий»)")
    if t == HOST:
        util.need_root()
        return node.key_add(pub)
    return remote.node(t, "key", "--add", input=pub.strip() + "\n", timeout=30)


def rotate(s):
    """Свой ключ хаба для сервера: новый кладётся, проверяется входом и только потом
    старый снимается. Сервер недоступен — ничего не меняется."""
    if s == HOST:
        raise Fail("у хоста нет ключа хаба — ротация бывает только для сервера (--server ID)")
    final = os.path.join(store.ETC, "ssh", "srv-%s_ed25519" % s["id"])
    new = final + ".new"
    for f in (new, new + ".pub"):
        if os.path.exists(f):
            os.unlink(f)
    old_path = remote.key_of(s)
    with open(old_path + ".pub") as f:
        old_pub = f.read().strip()
    sh("ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", "pcs-%s@%s-%s" % (s["id"], os.uname().nodename,
                                                                            time.strftime("%Y%m%d")), "-f", new)
    with open(new + ".pub") as f:
        new_pub = f.read().strip()
    try:
        remote.node(s, "key", "--add", input=new_pub + "\n", timeout=30)
        remote.drop_masters()
        if not remote.alive(s, key=new):
            remote.node(s, "key", "--drop", input=new_pub + "\n", timeout=30)
            raise Fail("с новым ключом сервер %s не пускает — оставил прежний" % s["id"])
    except Fail:
        for f in (new, new + ".pub"):
            if os.path.exists(f):
                os.unlink(f)
        raise
    os.replace(new, final)
    os.replace(new + ".pub", final + ".pub")
    s["key"] = final
    store.save(s)
    remote.drop_masters()
    dropped = remote.node(s, "key", "--drop", input=old_pub + "\n", timeout=30)
    k = node.parse_key(new_pub)
    return {"target": s["id"], "rotated": True, "fp": node.fingerprint(k[1]), "removed_old": dropped.get("removed", 0)}


def ssh_port(s, port):
    if s == HOST:
        raise Fail("порт SSH хоста Proxmox PCS не меняет — только у серверов")
    d = remote.node(s, "ssh-port", str(port), timeout=60)
    s["port"] = int(port)
    remote.drop_masters()
    for _ in range(10):
        time.sleep(2)
        if remote.alive(s, timeout=15):
            store.save(s)
            return dict(d, checked=True)
    store.save(s)
    raise Fail("порт %d задан, но SSH на нём не отвечает — проверь через qm terminal %s" % (port, s["id"]))


def term_argv(t):
    if t == HOST:
        return ["bash", "-l"]
    return remote.base(t, tty=True)

