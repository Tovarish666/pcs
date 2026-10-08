"""Действия над модемом строки: реконнект, ребут, проверка — и журнал реконнектов строки.

Их зовут и команда, и демон. Один модем — одно действие за раз: блокировка по адресу
HiLink (у двух строк на одном модеме она общая), между процессами — flock.
"""
import fcntl
import ipaddress
import os
import time
from contextlib import contextmanager

from ..core.util import Busy, Fail
from . import hilink
from .store import P

LOG_MAX = 1 << 20             # потом — в <id>.log.1, старый .1 уходит
HILINK_PORT = [80]            # тесты подменяют на фальшивый модем
NET = {"0": "нет сети", "1": "2G", "2": "2G", "3": "2G", "4": "3G", "5": "3G", "6": "3G", "7": "3G",
       "9": "3G", "19": "4G", "101": "4G"}


def log_line(rid, how, ok, dt, text):
    path = P.rowlog(rid)
    try:
        os.makedirs(P.log, mode=0o750, exist_ok=True)
        if os.path.exists(path) and os.path.getsize(path) > LOG_MAX:
            os.replace(path, path + ".1")
        with open(path, "a", encoding="utf-8") as f:
            f.write("%s | %s | %s | %.1fс | %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), how,
                                                     "ok" if ok else "fail", dt, " ".join(str(text).split())))
    except OSError:
        pass


def tail(rid, n=50):
    lines = []
    for p in (P.rowlog(rid) + ".1", P.rowlog(rid)):
        try:
            with open(p, encoding="utf-8", errors="replace") as f:
                lines += f.read().splitlines()
        except OSError:
            pass
    return lines[-n:] if n else lines


def parse_line(line):
    f = [x.strip() for x in line.split("|", 4)]
    if len(f) < 5:
        return None
    return {"t": f[0], "how": f[1], "ok": f[2] == "ok", "dt": f[3], "text": f[4]}


def last(rid):
    t = tail(rid, 1)
    return parse_line(t[0]) if t else None


def last_ip(rid):
    for line in reversed(tail(rid, 20)):
        e = parse_line(line)
        if e and e["ok"] and e["text"]:
            try:
                return str(ipaddress.IPv4Address(e["text"].split()[0]))
            except ValueError:
                continue
    return None


@contextmanager
def modem_lock(ip):
    os.makedirs(P.run, mode=0o700, exist_ok=True)
    f = open(P.modemlock(ip), "w")
    try:
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise Busy("модем %s: уже идёт реконнект или ребут — повтори, когда закончится" % ip)
        yield
    finally:
        f.close()


def need_modem(r):
    if not r["modem_ip"]:
        raise Fail("строка %d: нет modem_ip — HiLink не задан (modlink set %d modem_ip=192.168.N.1)" % (r["id"], r["id"]))
    return hilink.HiLink(r["modem_ip"], HILINK_PORT[0])


def reconnect(r, how="cli"):
    """Смена IP через HiLink; внешний IP — запросом с адреса lan_ip (тем же путём, что прокси)."""
    m = need_modem(r)
    t0 = time.time()
    with modem_lock(r["modem_ip"]):
        prev = last_ip(r["id"])
        try:
            m.reconnect()
        except Fail as e:
            log_line(r["id"], how, False, time.time() - t0, e)
            raise
        ip, why = None, ""
        for i in range(2):             # маршрут через модем оживает не мгновенно
            try:
                ip = hilink.ext_ip(source=r["lan_ip"], timeout=5)
                break
            except Fail as e:
                why = str(e)
                if not i:
                    hilink.HiLink.pause(2)
    dt = time.time() - t0
    text = ip if ip else "подключён; %s" % why
    if ip and prev == ip:
        text += " (тот же, что был)"
    log_line(r["id"], how, True, dt, text)
    out = {"id": r["id"], "ok": True, "dt": round(dt, 1)}
    if ip:
        out["ip"] = ip
        out["same"] = prev == ip
    return out


def reboot(r, how="cli"):
    m = need_modem(r)
    t0 = time.time()
    with modem_lock(r["modem_ip"]):
        try:
            m.reboot()
        except Fail as e:
            log_line(r["id"], "reboot/" + how, False, time.time() - t0, e)
            raise
    log_line(r["id"], "reboot/" + how, True, time.time() - t0, "перезагрузка отправлена")
    return {"id": r["id"], "ok": True}


def hilink_info(r):
    if not r["modem_ip"]:
        return {"ok": False, "error": "modem_ip не задан"}
    try:
        st = need_modem(r).status()
    except Fail as e:
        return {"ok": False, "error": str(e)}
    code = st.get("ConnectionStatus", "")
    return {"ok": code == "901", "status": hilink.STATUS.get(code, code or "?"),
            "net": NET.get(st.get("CurrentNetworkTypeEx") or st.get("CurrentNetworkType", ""), ""),
            "signal": st.get("SignalIcon", "")}


def test(r):
    """Внешний IP через сам прокси (127.0.0.1:port, CONNECT с логином) и состояние HiLink."""
    try:
        ip = hilink.ext_ip(proxy=("127.0.0.1", r["port"], r["login"], r["password"]), timeout=10)
        px = {"ok": True, "ip": ip}
    except Fail as e:
        px = {"ok": False, "error": str(e)}
    return {"id": r["id"], "proxy": px, "hilink": hilink_info(r)}
