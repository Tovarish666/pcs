#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Проверка guest/vmodem-api без модема, без прокси и без root.

Поднимаем заглушку веб-морды Huawei и заглушку SOCKS5, между ними запускаем
настоящий vmodem-api и смотрим, что видит модем и что получает клиент.

    python3 tests/test-vmodem-api.py
"""
import asyncio
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
API = os.path.join(HERE, os.pardir, "guest", "vmodem-api")
VIRT, REAL = 64, 101
REAL_IP = "192.168.%d.1" % REAL
VIRT_IP = "192.168.%d.1" % VIRT
USER, PASSWORD = "user1", "s3cret"

HDR_END = re.compile(rb"\r?\n\r?\n")
passed, failed = 0, 0


def check(name, cond, detail=""):
    global passed, failed
    if cond:
        passed += 1
        print("  ok   %s" % name)
    else:
        failed += 1
        print("  FAIL %s%s" % (name, ("\n       " + str(detail)) if detail else ""))


async def read_head(reader):
    buf = b""
    while True:
        chunk = await reader.read(4096)
        if not chunk:
            return buf, b""
        buf += chunk
        m = HDR_END.search(buf)
        if m:
            return buf[:m.start()], buf[m.end():]


def parse_headers(head):
    out = {}
    for line in head.split(b"\r\n")[1:]:
        if b":" in line:
            k, v = line.split(b":", 1)
            out[k.strip().lower().decode()] = v.strip().decode("latin1")
    return out


# ── Заглушка веб-морды модема ──────────────────────────────────────────────
class Modem:
    def __init__(self):
        self.seen = []           # что до модема реально доехало
        self.port = 0

    async def start(self):
        server = await asyncio.start_server(self.handle, "127.0.0.1", 0)
        self.port = server.sockets[0].getsockname()[1]
        return server

    async def handle(self, reader, writer):
        head, rest = await read_head(reader)
        if not head:
            writer.close()
            return
        line = head.split(b"\r\n", 1)[0]
        method, path = (line.split() + [b"", b""])[:2]
        headers = parse_headers(head)
        body = rest
        need = int(headers.get("content-length", 0) or 0)
        while len(body) < need:
            chunk = await reader.read(need - len(body))
            if not chunk:
                break
            body += chunk
        self.seen.append({"method": method.decode(), "path": path.decode(),
                          "host": headers.get("host", ""), "headers": headers,
                          "body": body})

        status, ctype, payload, extra = "200 OK", "text/xml", b"", []
        p = path.decode()
        if p == "/":
            status, ctype = "307 Temporary Redirect", "text/html"
            extra = ["Location: http://%s/html/index.html" % REAL_IP]
            payload = b"<html>redirect</html>"
        elif p.startswith("/api/webserver/SesTokInfo"):
            payload = (b'<?xml version="1.0" encoding="UTF-8"?>'
                       b"<response><SesInfo>SessionID=abc</SesInfo><TokInfo>tok123</TokInfo></response>")
        elif p.startswith("/api/dhcp/settings"):
            payload = ('<?xml version="1.0" encoding="UTF-8"?>'
                       "<response><DhcpIPAddress>%s</DhcpIPAddress>"
                       "<DhcpStartIPAddress>192.168.%d.100</DhcpStartIPAddress></response>"
                       % (REAL_IP, REAL)).encode()
        elif p.startswith("/api/device/information"):
            payload = (b'<?xml version="1.0" encoding="UTF-8"?>'
                       b"<response><SerialNumber>9GVDW17215000571</SerialNumber></response>")
        elif p.startswith("/big"):
            ctype = "application/octet-stream"
            payload = REAL_IP.encode() + b"\x00" * 200000
        else:
            payload = b'<?xml version="1.0" encoding="UTF-8"?><response>OK</response>'

        head_out = ["HTTP/1.1 " + status,
                    "Content-Type: " + ctype,
                    "Content-Length: %d" % len(payload),
                    "Connection: close"] + extra
        writer.write(("\r\n".join(head_out) + "\r\n\r\n").encode() + payload)
        await writer.drain()
        writer.close()


# ── Заглушка SOCKS5 ────────────────────────────────────────────────────────
class Socks:
    def __init__(self, modem_port, user=USER, password=PASSWORD):
        self.modem_port = modem_port
        self.user, self.password = user, password
        self.dst = []            # куда просили соединить
        self.port = 0

    async def start(self):
        server = await asyncio.start_server(self.handle, "127.0.0.1", 0)
        self.port = server.sockets[0].getsockname()[1]
        return server

    async def handle(self, reader, writer):
        try:
            ver, n = await reader.readexactly(2)
            methods = await reader.readexactly(n)
            if 2 not in methods:
                writer.write(b"\x05\xff"); await writer.drain(); writer.close(); return
            writer.write(b"\x05\x02")
            await writer.drain()

            await reader.readexactly(1)                      # версия авторизации
            ulen = (await reader.readexactly(1))[0]
            user = (await reader.readexactly(ulen)).decode()
            plen = (await reader.readexactly(1))[0]
            password = (await reader.readexactly(plen)).decode()
            if (user, password) != (self.user, self.password):
                writer.write(b"\x01\x01"); await writer.drain(); writer.close(); return
            writer.write(b"\x01\x00")
            await writer.drain()

            head = await reader.readexactly(4)
            if head[3] != 1:
                writer.write(b"\x05\x08\x00\x01" + b"\x00" * 6); await writer.drain(); writer.close(); return
            ip = await reader.readexactly(4)
            port = int.from_bytes(await reader.readexactly(2), "big")
            self.dst.append(("%d.%d.%d.%d" % tuple(ip), port))
            writer.write(b"\x05\x00\x00\x01" + b"\x00" * 6)
            await writer.drain()

            mr, mw = await asyncio.open_connection("127.0.0.1", self.modem_port)
            await asyncio.gather(pump(reader, mw), pump(mr, writer))
        except (asyncio.IncompleteReadError, ConnectionError, OSError):
            pass
        finally:
            try:
                writer.close()
            except OSError:
                pass


async def pump(reader, writer):
    try:
        while True:
            chunk = await reader.read(65536)
            if not chunk:
                break
            writer.write(chunk)
            await writer.drain()
    except (ConnectionError, OSError):
        pass
    finally:
        try:
            writer.close()
        except OSError:
            pass


# ── Клиент к vmodem-api ────────────────────────────────────────────────────
async def request(port, method="GET", path="/", body=b"", headers=(), raw=None):
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    if raw is None:
        lines = ["%s %s HTTP/1.1" % (method, path), "Host: " + VIRT_IP] + list(headers)
        if body:
            lines.append("Content-Length: %d" % len(body))
        raw = ("\r\n".join(lines) + "\r\n\r\n").encode() + body
    writer.write(raw)
    await writer.drain()
    data = b""
    while True:
        chunk = await asyncio.wait_for(reader.read(65536), timeout=10)
        if not chunk:
            break
        data += chunk
    writer.close()
    head, _, payload = data.partition(b"\r\n\r\n")
    status = int(head.split()[1]) if head else 0
    return status, parse_headers(head), payload


async def start_api(socks_port, password=PASSWORD, virt=VIRT, real=REAL):
    env = dict(os.environ, VMODEM_SOCKS_PASS=password, PYTHONUNBUFFERED="1")
    proc = await asyncio.create_subprocess_exec(
        sys.executable, API, "--virt", str(virt), "--real", str(real),
        "--socks", "127.0.0.1:%d" % socks_port, "--socks-user", USER,
        "--listen", "127.0.0.1:0", "--log-level", "warning",
        env=env, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    line = await asyncio.wait_for(proc.stdout.readline(), timeout=10)
    m = re.search(rb"listening 127\.0\.0\.1:(\d+)", line)
    if not m:
        raise RuntimeError("vmodem-api не сказал, что слушает: %r" % line)
    return proc, int(m.group(1))


async def main():
    modem = Modem()
    modem_server = await modem.start()
    socks = Socks(modem.port)
    socks_server = await socks.start()
    proc, port = await start_api(socks.port)

    print("vmodem-api:")
    try:
        # 1. Запрос доходит до модема с его настоящим Host
        status, headers, body = await request(port, path="/api/webserver/SesTokInfo")
        check("сессия отдаётся", status == 200 and b"tok123" in body, (status, body[:80]))
        check("модем видит свой Host", modem.seen[-1]["host"] == REAL_IP, modem.seen[-1]["host"])
        check("прокси просят соединить с модемом", socks.dst[-1] == (REAL_IP, 80), socks.dst[-1])
        check("сжатие не просим", "accept-encoding" not in modem.seen[-1]["headers"],
              modem.seen[-1]["headers"])

        # 2. Настоящий адрес в теле меняется на виртуальный, длина пересчитывается
        status, headers, body = await request(port, path="/api/dhcp/settings")
        check("в теле виртуальный адрес", VIRT_IP.encode() in body, body[:120])
        check("настоящего адреса в теле нет", REAL_IP.encode() not in body, body[:120])
        check("пул DHCP тоже переписан", b"192.168.%d.100" % VIRT in body, body[:160])
        check("Content-Length совпадает с телом",
              int(headers.get("content-length", -1)) == len(body),
              (headers.get("content-length"), len(body)))

        # 3. Location: браузер и mp.space должны остаться на виртуальном адресе
        status, headers, body = await request(port, path="/")
        check("редирект на виртуальный адрес",
              status == 307 and headers.get("location", "").startswith("http://" + VIRT_IP),
              (status, headers.get("location")))

        # 4. absolute-form: модем такого не понимает, приводим к origin-form
        raw = ("GET http://%s/api/monitoring/status HTTP/1.1\r\nHost: %s\r\n\r\n"
               % (VIRT_IP, VIRT_IP)).encode()
        status, headers, body = await request(port, raw=raw)
        check("absolute-form приведён к пути",
              status == 200 and modem.seen[-1]["path"] == "/api/monitoring/status",
              modem.seen[-1]["path"])

        # 5. Перезагрузка модема проходит
        seen_before = len(modem.seen)
        ctl = b'<?xml version="1.0" encoding="UTF-8"?><request><Control>1</Control></request>'
        status, headers, body = await request(port, "POST", "/api/device/control", ctl)
        check("Control=1 (перезагрузка) проходит",
              status == 200 and len(modem.seen) == seen_before + 1 and
              b"<Control>1</Control>" in modem.seen[-1]["body"], status)

        # 6. Сброс к заводским — не проходит
        seen_before = len(modem.seen)
        ctl2 = b'<?xml version="1.0" encoding="UTF-8"?><request><Control>2</Control></request>'
        status, headers, body = await request(port, "POST", "/api/device/control", ctl2)
        check("Control=2 (сброс) не доходит до модема", len(modem.seen) == seen_before,
              modem.seen[-1] if len(modem.seen) != seen_before else "")
        check("на сброс отвечаем ошибкой Huawei", status == 200 and b"No rights" in body,
              (status, body[:80]))

        # 7. Смена подсети модема — не проходит, чтение настроек — проходит
        seen_before = len(modem.seen)
        status, _, body = await request(port, "POST", "/api/dhcp/settings", b"<request/>")
        check("POST dhcp/settings не доходит до модема", len(modem.seen) == seen_before)
        check("на POST dhcp/settings ошибка Huawei", b"No rights" in body, body[:80])
        status, _, body = await request(port, "GET", "/api/dhcp/settings")
        check("GET dhcp/settings проходит", status == 200 and len(modem.seen) == seen_before + 1)

        # 8. Двоичный ответ переливается как есть
        status, headers, body = await request(port, path="/big")
        check("двоичное тело не тронуто",
              status == 200 and len(body) == 200000 + len(REAL_IP) and body.startswith(REAL_IP.encode()),
              (status, len(body)))

        # 9. Несколько запросов подряд по новым соединениям
        oks = 0
        for _ in range(3):
            status, _, body = await request(port, path="/api/webserver/SesTokInfo")
            oks += 1 if status == 200 else 0
        check("три запроса подряд", oks == 3, oks)

        # 10. Неверный пароль прокси — 502, а не тишина
        bad_proc, bad_port = await start_api(socks.port, password="wrong")
        try:
            status, _, body = await request(bad_port, path="/api/webserver/SesTokInfo")
            check("неверный пароль прокси → 502", status == 502 and b"vmodem-api" in body,
                  (status, body[:80]))
        finally:
            bad_proc.terminate()
            await bad_proc.wait()

        # 11. --probe находит модем и его серийный номер
        env = dict(os.environ, VMODEM_SOCKS_PASS=PASSWORD)
        probe = await asyncio.create_subprocess_exec(
            sys.executable, API, "--virt", str(VIRT), "--real", str(REAL),
            "--socks", "127.0.0.1:%d" % socks.port, "--socks-user", USER, "--probe",
            env=env, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        out, _ = await probe.communicate()
        check("--probe видит модем",
              probe.returncode == 0 and b"9GVDW17215000571" in out, out[:200])
    finally:
        proc.terminate()
        await proc.wait()
        modem_server.close()
        socks_server.close()

    print("\nитого: ok %d, fail %d" % (passed, failed))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
