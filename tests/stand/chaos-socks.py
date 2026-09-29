#!/usr/bin/env python3
"""Поддельный SOCKS5 для отказов, которые на живой SIM не устроить.

    chaos-socks.py <слушать ip:port> <режим> <настоящая прокси host:port> <логин> <пароль-файл>

Любой логин принимается. До 192.168.0.0/16 (веб-морда настоящего модема)
ходит через настоящую прокси — «прокси работает, модем на связи». С
интернетом — по режиму:
  captive    HTTP 302 на портал оператора — как неоплаченная SIM
  blackhole  соединение «установлено», но данные никуда не идут
  refuse     SOCKS-ответ 4 (host unreachable) — нет мобильных данных
  ok         всё через настоящую прокси
"""
import asyncio
import ipaddress
import socket
import struct
import sys

LISTEN, MODE, UP, USER, PWF = sys.argv[1:6]
UP_HOST, UP_PORT = UP.rsplit(":", 1)
PW = open(PWF).read().strip()


async def upstream(dst, port):
    r, w = await asyncio.open_connection(UP_HOST, int(UP_PORT))
    w.write(b"\x05\x01\x02")
    await w.drain()
    await r.readexactly(2)
    u, p = USER.encode(), PW.encode()
    w.write(b"\x01" + bytes([len(u)]) + u + bytes([len(p)]) + p)
    await w.drain()
    await r.readexactly(2)
    w.write(b"\x05\x01\x00\x03" + bytes([len(dst)]) + dst.encode() + struct.pack(">H", port))
    await w.drain()
    h = await r.readexactly(4)
    await r.readexactly({1: 6, 4: 18}.get(h[3], 0) or (await r.readexactly(1))[0] + 2)
    if h[1] != 0:
        raise OSError("upstream %d" % h[1])
    return r, w


async def pipe(r, w):
    try:
        while True:
            d = await r.read(65536)
            if not d:
                break
            w.write(d)
            await w.drain()
    except OSError:
        pass
    finally:
        w.close()


async def handle(r, w):
    try:
        _, nm = await r.readexactly(2)
        await r.readexactly(nm)
        w.write(b"\x05\x02")
        await w.drain()
        await r.readexactly(1)
        await r.readexactly((await r.readexactly(1))[0])
        await r.readexactly((await r.readexactly(1))[0])
        w.write(b"\x01\x00")
        await w.drain()
        _, cmd, _, at = await r.readexactly(4)
        if at == 1:
            dst = socket.inet_ntoa(await r.readexactly(4))
        elif at == 3:
            dst = (await r.readexactly((await r.readexactly(1))[0])).decode()
        else:
            w.close()
            return
        port = struct.unpack(">H", await r.readexactly(2))[0]
        ok = b"\x05\x00\x00\x01\x00\x00\x00\x00\x00\x00"
        try:
            local = ipaddress.ip_address(dst) in ipaddress.ip_network("192.168.0.0/16")
        except ValueError:
            local = False
        if cmd != 1:
            w.write(b"\x05\x07\x00\x01\x00\x00\x00\x00\x00\x00")
            await w.drain()
            w.close()
            return
        if local or MODE == "ok":
            ur, uw = await upstream(dst, port)
            w.write(ok)
            await w.drain()
            await asyncio.gather(pipe(r, uw), pipe(ur, w))
            return
        if MODE == "refuse":
            w.write(b"\x05\x04\x00\x01\x00\x00\x00\x00\x00\x00")
            await w.drain()
            w.close()
            return
        w.write(ok)
        await w.drain()
        if MODE == "blackhole":
            await asyncio.sleep(120)
            w.close()
            return
        await r.read(65536)
        w.write(b"HTTP/1.1 302 Found\r\nLocation: http://pay.operator.example/unpaid\r\n"
                b"Content-Length: 0\r\nConnection: close\r\n\r\n")
        await w.drain()
        w.close()
    except (OSError, asyncio.IncompleteReadError):
        w.close()


async def main():
    host, port = LISTEN.rsplit(":", 1)
    srv = await asyncio.start_server(handle, host, int(port))
    async with srv:
        await srv.serve_forever()

asyncio.run(main())
