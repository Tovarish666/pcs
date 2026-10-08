"""WebSocket (RFC 6455) на stdlib и мост «вкладка браузера ↔ pty» для терминала панели.

Протокол терминала поверх WebSocket:
  клиент → сервер: двоичный кадр — ввод (байты как есть);
                   текстовый — управление JSON: {"type": "resize", "cols": C, "rows": R};
  сервер → клиент: двоичный кадр — вывод pty; закрытие — когда процесс кончился.
"""
import base64
import binascii
import fcntl
import hashlib
import json
import os
import pwd
import select
import signal
import struct
import subprocess
import sys
import termios
import time

GUID = b"258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
CONT, TEXT, BIN, CLOSE, PING, PONG = 0x0, 0x1, 0x2, 0x8, 0x9, 0xA
MAX_MSG = 1 << 20


class WSError(Exception):
    """Нарушение протокола: закрыть соединение с кодом code (1002, 1007, 1009)."""

    def __init__(self, code, text):
        super().__init__(text)
        self.code = code


def accept(key):
    return base64.b64encode(hashlib.sha1(key.strip().encode("ascii") + GUID).digest()).decode("ascii")


def key_ok(key):
    try:
        return len(base64.b64decode((key or "").strip(), validate=True)) == 16
    except (binascii.Error, ValueError):
        return False


def _xor(data, mask):
    n = len(data)
    if not n:
        return b""
    m = (mask * (n // 4 + 1))[:n]
    return (int.from_bytes(data, "big") ^ int.from_bytes(m, "big")).to_bytes(n, "big")


def frame(op, payload=b"", fin=True, mask=None):
    """Один кадр. mask=4 байта — так шлёт клиент (сервер не маскирует)."""
    if isinstance(payload, str):
        payload = payload.encode("utf-8")
    b0 = (0x80 if fin else 0) | op
    m = 0x80 if mask is not None else 0
    n = len(payload)
    if n < 126:
        hdr = struct.pack("!BB", b0, m | n)
    elif n < 65536:
        hdr = struct.pack("!BBH", b0, m | 126, n)
    else:
        hdr = struct.pack("!BBQ", b0, m | 127, n)
    if mask is not None:
        return hdr + mask + _xor(payload, mask)
    return hdr + payload


def close_frame(code=1000, reason="", mask=None):
    return frame(CLOSE, struct.pack("!H", code) + reason.encode("utf-8")[:120], mask=mask)


class Parser:
    """Сообщения из потока байт: feed(data) -> [(op, payload)].

    Склеивает фрагменты; управляющие кадры отдаёт сразу, даже посреди фрагментов.
    TEXT отдаётся строкой, остальное — байтами. need_mask=True — мы сервер.
    """

    def __init__(self, need_mask=True, max_msg=MAX_MSG):
        self.need_mask, self.max_msg = need_mask, max_msg
        self.buf = bytearray()
        self.fop, self.frag = None, bytearray()

    def feed(self, data):
        self.buf += data
        out = []
        while True:
            f = self._one()
            if f is None:
                return out
            m = self._message(*f)
            if m is not None:
                out.append(m)

    def _one(self):
        b = self.buf
        if len(b) < 2:
            return None
        fin, rsv, op = b[0] & 0x80, b[0] & 0x70, b[0] & 0x0F
        masked, n = b[1] & 0x80, b[1] & 0x7F
        if rsv:
            raise WSError(1002, "расширения не согласованы")
        if op not in (CONT, TEXT, BIN, CLOSE, PING, PONG):
            raise WSError(1002, "неизвестный код кадра %d" % op)
        if bool(masked) != self.need_mask:
            raise WSError(1002, "кадр клиента без маски" if self.need_mask else "кадр сервера с маской")
        if op >= 0x8 and (not fin or n > 125):
            raise WSError(1002, "управляющий кадр фрагментирован или длинный")
        pos = 2
        if n == 126:
            if len(b) < 4:
                return None
            n, pos = struct.unpack_from("!H", b, 2)[0], 4
        elif n == 127:
            if len(b) < 10:
                return None
            n, pos = struct.unpack_from("!Q", b, 2)[0], 10
            if n >> 63:
                raise WSError(1002, "длина кадра")
        if n + len(self.frag) > self.max_msg:
            raise WSError(1009, "сообщение больше %d байт" % self.max_msg)
        mask = None
        if masked:
            if len(b) < pos + 4:
                return None
            mask, pos = bytes(b[pos:pos + 4]), pos + 4
        if len(b) < pos + n:
            return None
        payload = bytes(b[pos:pos + n])
        del b[:pos + n]
        if mask:
            payload = _xor(payload, mask)
        return bool(fin), op, payload

    def _message(self, fin, op, payload):
        if op >= 0x8:
            if op == CLOSE and len(payload) == 1:
                raise WSError(1002, "кадр закрытия длиной 1")
            return op, payload
        if op == CONT:
            if self.fop is None:
                raise WSError(1002, "продолжение без начала")
            self.frag += payload
            if not fin:
                return None
            op, payload, self.fop, self.frag = self.fop, bytes(self.frag), None, bytearray()
        else:
            if self.fop is not None:
                raise WSError(1002, "новое сообщение посреди фрагментов")
            if not fin:
                self.fop, self.frag = op, bytearray(payload)
                return None
        if op == TEXT:
            try:
                return op, payload.decode("utf-8")
            except UnicodeDecodeError:
                raise WSError(1007, "текст не в UTF-8")
        return op, payload


# ── pty ──────────────────────────────────────────────────────────────────────

# Дочерний процесс становится лидером сессии (start_new_session) и берёт pty
# управляющим терминалом: без этого нет Ctrl-C, job control и SIGWINCH. Запуск —
# через subprocess (fork+exec в C), а не pty.fork: панель многопоточная.
_SPAWN = (
    "import fcntl, os, sys, termios\n"
    "try:\n    fcntl.ioctl(0, termios.TIOCSCTTY, 0)\nexcept OSError:\n    pass\n"
    "try:\n    os.execvp(sys.argv[1], sys.argv[1:])\n"
    "except OSError as e:\n    sys.stderr.write('✗ %s: %s\\r\\n' % (sys.argv[1], e.strerror))\n    sys.exit(127)\n"
)


def _winsize(fd, cols, rows):
    cols, rows = max(2, min(int(cols), 1000)), max(1, min(int(rows), 1000))
    fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))


class Term:
    """Процесс на pty. close() снимает его вместе со всей группой."""

    def __init__(self, argv, cols=80, rows=24, env=None):
        if not argv:
            raise ValueError("пустая команда терминала")
        env = dict(os.environ if env is None else env)
        pw = pwd.getpwuid(os.getuid())
        env.setdefault("HOME", pw.pw_dir)
        env.setdefault("USER", pw.pw_name)
        env.setdefault("LOGNAME", pw.pw_name)
        env.setdefault("LANG", "C.UTF-8")
        env["TERM"] = "xterm-256color"
        master, slave = os.openpty()
        try:
            _winsize(slave, cols, rows)
            self.p = subprocess.Popen([sys.executable, "-c", _SPAWN] + [str(a) for a in argv],
                                      stdin=slave, stdout=slave, stderr=slave, env=env,
                                      cwd=env["HOME"] if os.path.isdir(env["HOME"]) else "/",
                                      close_fds=True, start_new_session=True)
        except BaseException:
            os.close(master)
            raise
        finally:
            os.close(slave)
        self.fd = master
        self.pid = self.p.pid
        os.set_blocking(master, False)

    def resize(self, cols, rows):
        _winsize(self.fd, cols, rows)

    def alive(self):
        return self.p.poll() is None

    def close(self):
        try:
            os.close(self.fd)            # ведущая сторона закрыта → SIGHUP сеансу
        except OSError:
            pass
        for sig, wait in ((signal.SIGHUP, 1.0), (signal.SIGTERM, 2.0), (signal.SIGKILL, 5.0)):
            if self.p.poll() is not None:
                break
            try:
                os.killpg(self.pid, sig)
            except OSError:
                pass
            try:
                self.p.wait(wait)
            except subprocess.TimeoutExpired:
                pass
        if self.p.poll() is None:
            self.p.wait()


def bridge(sock, term, ping_every=25.0, dead_after=90.0, max_pending=1 << 20):
    """Гоняет байты между WebSocket (sock, рукопожатие уже сделано) и pty.

    Кончается, когда закрылась вкладка, процесс завершился или клиент молчит
    dead_after секунд после пинга. Процесс снимается всегда.
    """
    parser = Parser(need_mask=True)
    pending = bytearray()           # ввод, который pty ещё не принял
    last_rx = time.monotonic()
    pinged = None

    def send(data):
        sock.sendall(data)

    try:
        while True:
            want_w = [term.fd] if pending else []
            r, w, _ = select.select([sock, term.fd], want_w, [], min(5.0, ping_every))
            now = time.monotonic()
            if sock in r:
                data = sock.recv(65536)
                if not data:
                    return "клиент ушёл"
                last_rx, pinged = now, None
                for op, payload in parser.feed(data):
                    if op == BIN:
                        pending += payload
                        if len(pending) > max_pending:
                            send(close_frame(1009, "слишком много ввода"))
                            return "переполнен ввод"
                    elif op == TEXT:
                        _control(term, payload)
                    elif op == PING:
                        send(frame(PONG, payload))
                    elif op == CLOSE:
                        send(frame(CLOSE, payload[:2]))
                        return "вкладка закрыта"
            if pending and term.fd in w:
                try:
                    n = os.write(term.fd, bytes(pending[:65536]))
                    del pending[:n]
                except BlockingIOError:
                    pass
                except OSError:
                    pending.clear()
            if term.fd in r:
                try:
                    out = os.read(term.fd, 65536)
                except BlockingIOError:
                    out = None
                except OSError:          # Linux: EIO, когда процесс завершился
                    out = b""
                if out == b"":
                    send(close_frame(1000, "процесс завершился"))
                    _drain_close(sock, parser)
                    return "процесс завершился"
                if out:
                    send(frame(BIN, out))
            elif not term.alive():       # сам процесс вышел, а pty держит его фоновый потомок
                for _ in range(64):
                    try:
                        out = os.read(term.fd, 65536)
                    except OSError:
                        break
                    if not out:
                        break
                    send(frame(BIN, out))
                send(close_frame(1000, "процесс завершился"))
                _drain_close(sock, parser)
                return "процесс завершился"
            if pinged is None and now - last_rx > ping_every:
                send(frame(PING, b"pcs"))
                pinged = now
            elif pinged is not None and now - last_rx > dead_after:
                return "клиент не отвечает"
    except WSError as e:
        try:
            send(close_frame(e.code, str(e)))
        except OSError:
            pass
        return "протокол: %s" % e
    except OSError:
        return "соединение оборвалось"
    finally:
        term.close()


def _control(term, text):
    try:
        m = json.loads(text)
        if m.get("type") == "resize":
            term.resize(m["cols"], m["rows"])
    except (ValueError, KeyError, TypeError, AttributeError, OSError):
        pass


def _drain_close(sock, parser, wait=1.0):
    """После своего CLOSE подождать ответный (вежливое закрытие), но недолго."""
    end = time.monotonic() + wait
    try:
        while time.monotonic() < end:
            r, _, _ = select.select([sock], [], [], max(0.0, end - time.monotonic()))
            if not r:
                return
            data = sock.recv(65536)
            if not data:
                return
            if any(op == CLOSE for op, _ in parser.feed(data)):
                return
    except (OSError, WSError):
        pass
