"""Апстрим модема gw: прокси ген1 → настоящий модем (HiLink) → интернет.

Всё — прямо через SOCKS5, мимо своей стороны: так видно, кто сломан — прокси,
модем, SIM или наш туннель. Только stdlib, без root.
"""
import re
import socket
import struct

CHECK = (("connectivitycheck.gstatic.com", 80, "/generate_204"), ("www.google.com", 80, "/generate_204"))
IP_ECHO = ("api.ipify.org", 80, "/")


class Down(Exception):
    """Сломан апстрим. kind: proxy | auth | connect; code — ответ SOCKS5 на CONNECT."""

    def __init__(self, kind, text, code=None):
        Exception.__init__(self, text)
        self.kind, self.code = kind, code


def _recv(s, n):
    buf = b""
    while len(buf) < n:
        c = s.recv(n - len(buf))
        if not c:
            break
        buf += c
    return buf


def socks(row, dst, port, timeout=8):
    """TCP до dst:port через прокси модема → сокет. Поломка — Down с местом."""
    where = "%s:%s" % (row["host"], row["port"])
    try:
        s = socket.create_connection((row["host"], int(row["port"])), timeout=timeout)
    except OSError as e:
        raise Down("proxy", "прокси %s не отвечает: %s" % (where, e))
    s.settimeout(timeout)
    stage = "proxy"
    try:
        s.sendall(b"\x05\x01\x02")
        r = _recv(s, 2)
        if r[:1] == b"H":
            raise Down("proxy", "на %s отвечает HTTP, а не SOCKS5 — не тот порт?" % where)
        if len(r) < 2 or r[0] != 5:
            raise Down("proxy", "на %s отвечает не SOCKS5" % where)
        if r[1] == 0xFF:
            raise Down("auth", "прокси не принимает вход по логину и паролю")
        if r[1] == 2:
            stage = "auth"
            u, pw = row["user"].encode(), row["pw"].encode()
            s.sendall(b"\x01" + bytes([len(u)]) + u + bytes([len(pw)]) + pw)
            r = _recv(s, 2)
            if len(r) < 2 or r[1] != 0:
                raise Down("auth", "логин или пароль не приняты")
        stage = "connect"
        try:
            addr = b"\x01" + socket.inet_aton(dst)
        except OSError:
            addr = b"\x03" + bytes([len(dst)]) + dst.encode()
        s.sendall(b"\x05\x01\x00" + addr + struct.pack(">H", port))
        r = _recv(s, 4)
        if len(r) < 2 or r[1] != 0:
            code = r[1] if len(r) > 1 else None
            raise Down("connect", "прокси не соединил с %s:%d (код %s)" % (dst, port, "—" if code is None else code), code)
        atyp = r[3] if len(r) > 3 else 1
        rest = 6 if atyp == 1 else 18 if atyp == 4 else (_recv(s, 1) or b"\0")[0] + 2
        _recv(s, rest)
        return s
    except Down:
        s.close()
        raise
    except OSError as e:
        s.close()
        if stage == "connect":
            raise Down("connect", "%s при соединении с %s:%d" % (e.__class__.__name__, dst, port))
        raise Down("proxy", "прокси оборвал рукопожатие (%s) — лимит соединений?" % e.__class__.__name__)


def http(s, host, path, cookie="", body=None, token=""):
    """Один запрос по готовому сокету → (код, заголовки, тело). Сокет закрывается."""
    if body is None:
        req = "GET %s HTTP/1.1\r\nHost: %s\r\n" % (path, host)
    else:
        req = ("POST %s HTTP/1.1\r\nHost: %s\r\nContent-Type: application/x-www-form-urlencoded; charset=UTF-8\r\n"
               "Content-Length: %d\r\n" % (path, host, len(body.encode())))
        if token:
            req += "__RequestVerificationToken: %s\r\n" % token
    if cookie:
        req += "Cookie: %s\r\n" % cookie
    data = b""
    try:
        s.sendall((req + "Connection: close\r\n\r\n" + (body or "")).encode())
        while len(data) < 1 << 20:
            c = s.recv(65536)
            if not c:
                break
            data += c
    except OSError:
        pass
    finally:
        s.close()
    head, _, b = data.partition(b"\r\n\r\n")
    try:
        code = int(head.split(b" ")[1]) if head.startswith(b"HTTP/") else 0
    except (IndexError, ValueError):
        code = 0
    return code, head.decode("latin1"), b.decode("utf-8", "replace")


def xml(tag, text):
    m = re.search(r"<%s>([^<]*)</%s>" % (tag, tag), text)
    return m.group(1) if m else ""


def ipv4(text):
    m = re.search(r"(?<![\d.])(\d{1,3}(?:\.\d{1,3}){3})(?![\d.])", text or "")
    return m.group(1) if m else ""


def internet(row):
    """→ (True, "") или (False, почему). Одна медленная проверка через слабую прокси —
    ещё не поломка: две цели и повтор первой; портал оператора — сразу."""
    why = ""
    for host, port, path in (CHECK[0], CHECK[0], CHECK[1]):
        try:
            code, head, _ = http(socks(row, host, port, timeout=12), host, path)
        except Down as e:
            why = "в интернет не выходит: %s" % e
            continue
        if code == 204:
            return True, ""
        if code == 0:
            why = "соединение открывается, но ответа нет"
            continue
        loc = re.search(r"(?im)^location:\s*(\S+)", head)
        return False, "вместо 204 пришёл HTTP %s%s — SIM не оплачена, портал оператора?" % (
            code, (" → " + loc.group(1)) if loc else "")
    return False, why


def public_ip(row):
    try:
        return ipv4(http(socks(row, IP_ECHO[0], IP_ECHO[1], timeout=12), IP_ECHO[0], IP_ECHO[2])[2])
    except Down:
        return ""


def quick(row):
    """Апстрим жив? Для status --wan: "" — да, иначе текст поломки."""
    try:
        s = socks(row, CHECK[0][0], CHECK[0][1], timeout=8)
    except Down as e:
        return str(e)
    code = http(s, CHECK[0][0], CHECK[0][2])[0]
    return "" if code == 204 else "нет интернета через модем (HTTP %s)" % (code or "—")


def chain(row):
    """Цепочка прокси → логин → модем → SIM → интернет.

    → (шаги [(имя, ok, текст)], где сломано ("" | proxy | auth | modem | sim | internet), внешний IP).
    """
    steps = []
    real = "192.168.%d.1" % row["real"]
    proxy_ok = ("прокси", True, "SOCKS5 на %s:%s отвечает" % (row["host"], row["port"]))
    try:
        s = socks(row, real, 80)
    except Down as e:
        if e.kind == "proxy":
            return steps + [("прокси", False, str(e))], "proxy", ""
        steps.append(proxy_ok)
        if e.kind == "auth":
            return steps + [("логин", False, str(e))], "auth", ""
        if e.code == 2:
            # Код 2 бывает и от неверного пароля (пускает на рукопожатии, режет на
            # соединении), и от запрета частных адресов. Различаем по интернету.
            try:
                socks(row, CHECK[0][0], CHECK[0][1]).close()
            except Down:
                return steps + [("логин", False, "прокси принимает вход, но ничего не пускает (код 2) — "
                                                 "неверный логин или пароль?")], "auth", ""
            return steps + [("логин", True, "принят"),
                            ("модем", False, "прокси пускает в интернет, но не в сеть модема %s — это не прокси ген1?"
                             % real)], "modem", ""
        return steps + [("логин", True, "принят"),
                        ("модем", False, "прокси работает, но до %s не достучаться: %s" % (real, e))], "modem", ""
    steps += [proxy_ok, ("логин", True, "принят")]
    code, _, body = http(s, real, "/api/webserver/SesTokInfo")
    if code != 200 or "<SesInfo>" not in body:
        return steps + [("модем", False, "%s отвечает, но это не HiLink (HTTP %s)" % (real, code or "—"))], "modem", ""
    steps.append(("модем", True, "HiLink на %s отвечает" % real))
    try:
        _, _, st = http(socks(row, real, 80), real, "/api/monitoring/status", cookie=xml("SesInfo", body))
        conn, sim = xml("ConnectionStatus", st), xml("SimStatus", st)
        text = "связь %s, SIM %s, сеть %s, сигнал %s" % (
            {"901": "есть", "902": "нет", "900": "подключается", "903": "рвётся"}.get(conn, conn or "?"),
            {"1": "ок", "0": "ошибка", "255": "нет SIM"}.get(sim, sim or "?"),
            xml("CurrentNetworkType", st) or "?", xml("SignalIcon", st) or "?")
        if sim and sim != "1":
            return steps + [("SIM", False, text)], "sim", ""
        if conn and conn != "901":
            return steps + [("SIM", False, text + " — у модема нет связи")], "sim", ""
        steps.append(("SIM", True, text))
    except Down as e:
        steps.append(("SIM", True, "статус не прочитался (%s) — дальше" % e))
    ok, why = internet(row)
    if not ok:
        return steps + [("интернет", False, why)], "internet", ""
    ip = public_ip(row)
    return steps + [("интернет", True, "есть" + (", внешний IP %s" % ip if ip else ""))], "", ip
