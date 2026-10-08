"""Посредник Host для модема с real ≠ n: юнит proxyveth-hostfix@N внутри netns pvN.

mp.space знает модем как 192.168.N.1, а настоящий за прокси — 192.168.R.1.
Веб-сервер E3372 сверяет заголовок Host со своим адресом и на чужой отвечает
307 без сессии (замерено на живом модеме: Host 192.168.64.1 → 307, Host
192.168.101.1 → 200). Переписать заголовок на уровне пакетов нельзя — это
содержимое потока. Поэтому 192.168.N.1:80 в netns слушает этот посредник:
правит Host в запросе, а в заголовках ответа (Location) — адрес обратно.
Тело переливается как есть.

DNAT не ставим: PREROUTING перехватил бы пакет раньше локальной доставки, и
посредник его бы не увидел.

    python3 -m pcs.proxyveth.gw_hostfix N [REAL]

Только stdlib и без pcs.core: таких процессов десятки, память на счету.
"""
import asyncio
import json
import re
import socket
import sys

SPEC = "/etc/proxyveth/gw/%d/spec.json"      # пишет pcs.proxyveth.gw при создании модема
HDR_END = re.compile(rb"\r?\n\r?\n")
ABS_URI = re.compile(rb"^(\S+)\s+https?://[^/\s]+(\S*)\s+(HTTP/\d\.\d)$", re.I)
MAX_HDR = 32 * 1024


def rewrite_request(head, host):
    """Подменить Host, погасить keep-alive, привести request-line к origin-form."""
    out, seen_host, seen_conn = [], False, False
    for i, line in enumerate(head.split(b"\r\n")):
        low = line.lower()
        if i == 0:
            # HTTP-прокси присылает absolute-form (GET http://host/path). Веб-сервер
            # модема такого не понимает и молча ждёт, пока клиент не отвалится.
            m = ABS_URI.match(line)
            if m:
                line = m.group(1) + b" " + (m.group(2) or b"/") + b" " + m.group(3)
            out.append(line)
        elif low.startswith(b"host:"):
            out.append(b"Host: " + host.encode())
            seen_host = True
        elif low.startswith(b"connection:") or low.startswith(b"proxy-connection:"):
            if not seen_conn:
                out.append(b"Connection: close")
                seen_conn = True
        else:
            out.append(line)
    if not seen_host:
        out.insert(1, b"Host: " + host.encode())
    if not seen_conn:
        out.append(b"Connection: close")
    return b"\r\n".join(out)


def rewrite_response(head, real, virt):
    """Адрес настоящего модема в заголовках ответа → виртуальный. Целым адресом:
    192.168.10.1 не должен задеть 192.168.10.100."""
    return re.sub(rb"(?<![\d.])" + re.escape(real.encode()) + rb"(?![\d])", virt.encode(), head)


async def pump(src, dst):
    try:
        while True:
            chunk = await src.read(65536)
            if not chunk:
                break
            dst.write(chunk)
            await dst.drain()
    except (ConnectionError, OSError):
        pass
    finally:
        try:
            dst.close()
        except OSError:
            pass


class Hostfix:
    """virt, real — адреса строкой («192.168.64.1», «192.168.101.1»). upstream —
    куда соединяться на самом деле (по умолчанию real:80; в тестах — свой порт)."""

    def __init__(self, virt, real, upstream=None):
        self.virt, self.real = virt, real
        self.upstream = upstream or (real, 80)

    async def relay_response(self, rreader, cwriter):
        buf = b""
        while True:
            chunk = await rreader.read(8192)
            if not chunk:
                break
            buf += chunk
            m = HDR_END.search(buf)
            if m:
                head = rewrite_response(buf[:m.start()], self.real, self.virt)
                cwriter.write(head + b"\r\n\r\n" + buf[m.end():])
                await cwriter.drain()
                await pump(rreader, cwriter)
                return
            if len(buf) > MAX_HDR:
                break
        if buf:
            cwriter.write(buf)
            await cwriter.drain()
        await pump(rreader, cwriter)

    async def handle(self, creader, cwriter):
        rwriter = None
        try:
            buf = b""              # заголовки целиком: Host правится прежде, чем уйдёт хоть байт
            while True:
                chunk = await asyncio.wait_for(creader.read(8192), timeout=15)
                if not chunk:
                    return
                buf += chunk
                m = HDR_END.search(buf)
                if m:
                    head, rest = buf[:m.start()], buf[m.end():]
                    break
                if len(buf) > MAX_HDR:
                    return
            rreader, rwriter = await asyncio.wait_for(asyncio.open_connection(*self.upstream), timeout=10)
            rwriter.write(rewrite_request(head, self.real) + b"\r\n\r\n" + rest)
            await rwriter.drain()
            await asyncio.gather(self.relay_response(rreader, cwriter), pump(creader, rwriter))
        except (asyncio.TimeoutError, ConnectionError, OSError):
            pass
        finally:
            for w in (cwriter, rwriter):
                try:
                    if w is not None:
                        w.close()
                except OSError:
                    pass


def listen(host, port):
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    if sys.platform.startswith("linux"):
        # IP_FREEBIND: адрес на eth0 может появиться на миг позже старта юнита.
        s.setsockopt(socket.IPPROTO_IP, getattr(socket, "IP_FREEBIND", 15), 1)
    s.bind((host, port))
    s.listen(128)
    s.setblocking(False)
    return s


async def serve(hf, host, port=80):
    server = await asyncio.start_server(hf.handle, sock=listen(host, port))
    async with server:
        await server.serve_forever()


def main(argv):
    if not argv or not argv[0].isdigit():
        print("✗ нужно: python3 -m pcs.proxyveth.gw_hostfix N [REAL]", file=sys.stderr)
        return 1
    n = int(argv[0])
    try:
        real = int(argv[1]) if len(argv) > 1 else int(json.load(open(SPEC % n))["real"])
    except (OSError, ValueError, KeyError) as e:
        print("✗ модем %d: не прочитал real из %s: %s" % (n, SPEC % n, e), file=sys.stderr)
        return 1
    virt = "192.168.%d.1" % n
    try:
        asyncio.run(serve(Hostfix(virt, "192.168.%d.1" % real), virt))
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
