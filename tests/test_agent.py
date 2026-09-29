#!/usr/bin/env python3
"""Проверки агента vmodem без root, без ВМ и без сети.

  * разбор таблицы: всё, что может быть не так в конфиге;
  * диагностика прокси и модема: поддельный мир в этом же процессе —
    SOCKS5 с разными поломками, веб-морда HiLink, «интернет» с 204,
    портал оператора, дыра, куда уходит трафик;
  * конфиг прямых прокси.

    python3 tests/test_agent.py
"""
import asyncio
import importlib.machinery
import importlib.util
import os
import socket
import struct
import sys
import threading

HERE = os.path.dirname(os.path.abspath(__file__))
loader = importlib.machinery.SourceFileLoader("vmodem", os.path.join(HERE, os.pardir, "agent", "vmodem"))
spec = importlib.util.spec_from_loader("vmodem", loader)
vm = importlib.util.module_from_spec(spec)
loader.exec_module(vm)

passed = failed = 0


def check(name, cond, detail=""):
    global passed, failed
    if cond:
        passed += 1
        print("  ok   %s" % name)
    else:
        failed += 1
        print("  FAIL %s%s" % (name, ("\n       " + str(detail)) if detail else ""))


# ── таблица ────────────────────────────────────────────────────────────────
def test_lint():
    print("таблица:")
    good = "188.134.95.184:1198"
    csv = "﻿N , Real , Proxy ,enabled\n" + "\n".join([
        "201,81,%s:modem81:pw81,1" % good,
        "202,82,%s:modem82:pw82" % good,
        "202,83,%s:modem83:pw83" % good,             # n дважды
        "203,192.168.84.100,%s:modem84:pw84" % good,  # real адресом
        "204,85,%s:modem85:pw85,0" % good,            # выключен
        "1,86,%s:modem86:pw86" % good,                # делит таблицу маршрутов с 201
        "154,87,%s:modem87:pw87" % good,              # системная таблица mp.space
        "88,88,%s:modem88:pw88" % good,               # сеть самого сервера
        "205,89,188.134.95.184:1198modem89:pw89",     # кривая строка
        "206,90,188.134.95.184:0:modem90:pw90",       # порт 0
        "207,91,%s:modem91:" % good,                  # пустой пароль
        "208,abc,%s:modem92:pw92" % good,             # real не число
        "abc,93,%s:modem93:pw93" % good,              # n не число
        "209,94,bad host!:1198:modem94:pw94",         # мусор в адресе
        "210,95,%s:modem95:p:a:s:s" % good,           # двоеточия в пароле
        "211,96,%s:modem81:pw81" % good,              # та же прокси, что у 201
        "300,97,%s:modem97:pw97" % good,              # n вне диапазона
        ",,,",
        "212,,%s:modem98:pw98" % good,                # пустой real
        "213,99,%s:modem99:pw99,1,лишнее" % good,     # лишняя ячейка
    ]) + "\n"
    d, dis, inv, probs = vm.lint(csv, taken_octets={88})
    text = "\n".join(probs)
    check("годные строки", sorted(d) == [203, 210, 211, 212, 213], sorted(d))
    check("выключенная не создаётся, но и не брак", dis == {204}, dis)
    check("брак назван поимённо", inv == {1, 88, 154, 201, 202, 205, 206, 207, 208, 209, 300}, sorted(inv))
    check("n дважды — не берём ни одну", "n=202 в строках" in text, text)
    check("N и N+200 — не берём обе", "n=1,201 делят одну таблицу" in text, text)
    check("153–155 отбиты", "n=154" in text and "253–255" in text, text)
    check("сеть сервера отбита", "n=88" in text and "занята сетью" in text, text)
    check("real адресом → октет", d[203]["real"] == 84, d[203])
    check("двоеточия в пароле целы", d[210]["pw"] == "p:a:s:s", d[210]["pw"])
    check("пустой real → n, с предупреждением", d[212]["real"] == 212 and "real пуст" in text, text)
    check("лишняя ячейка замечена", "за пределами шапки" in text, text)
    check("пустая таблица — честная ошибка", "нет шапки" in vm.lint("")[3][0])
    check("чужая таблица — честная ошибка", "нет шапки" in vm.lint("что,то\n1,2\n")[3][0])
    d2, _, _, p2 = vm.lint("n,proxy\n5,h:1:u:p\n6,h:1:u:p\n")
    check("одна прокси на двоих — предупреждение", any("одной прокси" in x for x in p2), p2)
    check("хэш строки меняется вместе с прокси",
          vm.spec_hash(dict(d2[5])) != vm.spec_hash(dict(d2[5], pw="другой")))

    print("ссылка:")
    check("адресная строка → CSV",
          vm.normalize_url("https://docs.google.com/spreadsheets/d/AbC_1/edit#gid=456")
          == "https://docs.google.com/spreadsheets/d/AbC_1/export?format=csv&gid=456")
    check("опубликованная → CSV",
          vm.normalize_url("https://docs.google.com/spreadsheets/d/e/2PACX-1/pubhtml?gid=12")
          == "https://docs.google.com/spreadsheets/d/e/2PACX-1/pub?gid=12&single=true&output=csv")
    check("чужой адрес не трогаем", vm.normalize_url("https://x.ru/m.csv") == "https://x.ru/m.csv")


# ── поддельный мир ─────────────────────────────────────────────────────────
class World:
    """SOCKS5, за которым настоящий модем 192.168.81.1 и «интернет»."""

    def __init__(self):
        self.socks_mode = "ok"      # ok | code2-all | code2-private | http | refuse-net
        self.net_mode = "ok"        # ok | captive | blackhole
        self.conn, self.sim = "901", "1"
        self.user, self.pw = "u", "p"
        self.loop = asyncio.new_event_loop()
        self.ready = threading.Event()
        threading.Thread(target=self._run, daemon=True).start()
        self.ready.wait()

    def _run(self):
        asyncio.set_event_loop(self.loop)
        self.srv = self.loop.run_until_complete(asyncio.start_server(self.handle, "127.0.0.1", 0))
        self.port = self.srv.sockets[0].getsockname()[1]
        self.ready.set()
        self.loop.run_forever()

    async def http_reply(self, r, w, dst):
        await r.readuntil(b"\r\n\r\n")
        if dst == "192.168.81.1":
            body = ("<response><SesInfo>SessionID=x</SesInfo><TokInfo>t</TokInfo>"
                    "<ConnectionStatus>%s</ConnectionStatus><SimStatus>%s</SimStatus>"
                    "<CurrentNetworkType>19</CurrentNetworkType><SignalIcon>4</SignalIcon></response>"
                    % (self.conn, self.sim)).encode()
            w.write(b"HTTP/1.1 200 OK\r\nContent-Length: %d\r\n\r\n%s" % (len(body), body))
        elif dst in ("connectivitycheck.gstatic.com", "cp.cloudflare.com"):
            if self.net_mode == "blackhole":
                await asyncio.sleep(30)
                return
            if self.net_mode == "captive":
                w.write(b"HTTP/1.1 302 Found\r\nLocation: http://pay.operator/unpaid\r\nContent-Length: 0\r\n\r\n")
            else:
                w.write(b"HTTP/1.1 204 No Content\r\n\r\n")
        elif dst == "api.ipify.org":
            w.write(b"HTTP/1.1 200 OK\r\nContent-Length: 11\r\n\r\n203.0.113.7")
        await w.drain()
        w.close()

    async def handle(self, r, w):
        try:
            if self.socks_mode == "http":
                await r.read(3)
                w.write(b"HTTP/1.1 404 Not Found\r\nContent-Length: 0\r\n\r\n")
                await w.drain()
                w.close()
                return
            _, nm = await r.readexactly(2)
            await r.readexactly(nm)
            w.write(b"\x05\x02")
            await w.drain()
            await r.readexactly(1)
            user = (await r.readexactly((await r.readexactly(1))[0])).decode()
            pw = (await r.readexactly((await r.readexactly(1))[0])).decode()
            if self.socks_mode != "code2-all" and (user, pw) != (self.user, self.pw):
                w.write(b"\x01\x01")
                await w.drain()
                w.close()
                return
            w.write(b"\x01\x00")
            _, cmd, _, at = await r.readexactly(4)
            dst = socket.inet_ntoa(await r.readexactly(4)) if at == 1 else \
                (await r.readexactly((await r.readexactly(1))[0])).decode()
            await r.readexactly(2)
            private = dst.startswith("192.168.")
            code = 0
            if self.socks_mode == "code2-all" or (self.socks_mode == "code2-private" and private):
                code = 2
            elif self.socks_mode == "refuse-net" and not private:
                code = 4
            elif private and dst != "192.168.81.1":
                await asyncio.sleep(30)         # настоящего модема по этому адресу нет
                return
            w.write(bytes([5, code, 0, 1, 0, 0, 0, 0, 0, 0]))
            await w.drain()
            if code:
                w.close()
                return
            await self.http_reply(r, w, dst)
        except (asyncio.IncompleteReadError, ConnectionError, OSError):
            pass


def test_diag():
    print("диагностика прокси и модема:")
    vm.CHECK_URL = ("connectivitycheck.gstatic.com", 80, "/generate_204")
    w = World()
    p = {"n": 201, "real": 81, "host": "127.0.0.1", "port": w.port, "user": "u", "pw": "p"}
    orig = vm.socks_connect

    def fast(p_, dst, dport, timeout=8):      # заглушке хватает 2 с, живым прокси — 8
        return orig(p_, dst, dport, timeout=2)
    vm.socks_connect = fast

    def case(name, want, **kw):
        for k, v in dict(socks_mode="ok", net_mode="ok", conn="901", sim="1").items():
            setattr(w, k, kw.get(k, v))
        cls, info, ip = vm.diag(dict(p, **kw.get("p", {})))
        check("%s → %s" % (name, want), cls == want, (cls, info))
        return info, ip

    info, ip = case("всё исправно", "ok")
    check("внешний IP виден", ip == "203.0.113.7", ip)
    case("неверный пароль", "proxy-auth", p={"pw": "не тот"})
    case("пускает на рукопожатии, режет на соединении (код 2)", "proxy-auth", socks_mode="code2-all")
    case("не пускает в сеть модема — не ген1", "modem-blocked", socks_mode="code2-private")
    case("на порту HTTP, а не SOCKS5", "proxy-down", socks_mode="http")
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    closed = s.getsockname()[1]
    s.close()
    case("порт закрыт", "proxy-down", p={"port": closed})
    case("настоящего модема нет по адресу", "modem-unreachable", p={"real": 99})
    case("SIM нет", "sim", sim="255")
    case("у модема нет связи", "no-data", conn="902")
    info, _ = case("SIM не оплачена — портал оператора", "captive", net_mode="captive")
    check("портал назван", any("pay.operator" in x for x in info), info)
    case("нет мобильных данных (код 4)", "no-internet", socks_mode="refuse-net")
    case("трафик уходит в никуда", "no-internet", net_mode="blackhole")
    vm.socks_connect = orig


def test_notify():
    print("оповещения:")
    import tempfile
    vm.LIB = tempfile.mkdtemp()
    vm.server_ip = lambda: "10.0.0.7"
    sent = []
    vm.notify = lambda cfg, text: sent.append(text)
    cfg = vm.DEFAULTS
    ok_row = {"n": 201, "real": 81, "local": "ok", "local_problems": [], "up": "ok", "up_info": [], "ip": "1.2.3.4", "t": 1}
    bad_row = dict(ok_row, up="no-internet", up_info=["нет ответа"], ip="")
    vm.remember([ok_row], cfg)
    check("исправный модем — молчим", sent == [], sent)
    vm.remember([bad_row], cfg)
    check("одна неудачная проверка — ещё не повод будить", sent == [], sent)
    vm.remember([bad_row], cfg)
    check("вторая подряд — оповещение", len(sent) == 1 and "модем 201" in sent[0] and "нет интернета" in sent[0], sent)
    vm.remember([bad_row], cfg)
    check("дальше не повторяем", len(sent) == 1, sent)
    vm.remember([ok_row], cfg)
    check("восстановление — сразу", len(sent) == 2 and "снова в порядке" in sent[1], sent)
    vm.remember([bad_row], cfg)
    vm.remember([ok_row], cfg)
    check("мигнул и вернулся — тишина", len(sent) == 2, sent)


def test_proxy_conf():
    print("прямые прокси:")
    cfg = vm.merge(vm.DEFAULTS, {"proxy": {"enabled": True, "user": "u1", "pass": "p1", "base_port": 20000}})
    c = vm.proxy_conf(cfg, [201, 202])
    ports = [i["listen_port"] for i in c["inbounds"]]
    check("порт = база + n", ports == [20201, 20202], ports)
    check("выход с адреса модема", c["outbounds"][0]["inet4_bind_address"] == "192.168.201.100", c["outbounds"][0])
    check("имена — DNS самого модема", c["dns"]["servers"][0]["address"] == "tcp://192.168.201.1", c["dns"]["servers"][0])
    check("непонятное — в блок", c["route"]["final"] == "block")
    check("логин обязателен", c["inbounds"][0]["users"] == [{"username": "u1", "password": "p1"}])

    print("вид для сервера:")
    u = vm.DEFAULTS["usb"]
    check("Huawei 12d1:14dc", (u["vendor"], u["product_id"]) == ("0x12d1", "0x14dc"))
    check("MAC со стороны сервера — как у всех E3372h", u["host_mac"] == "0c:5b:8f:27:9a:64")
    check("строки HUAWEI_MOBILE", u["manufacturer"] == u["product"] == "HUAWEI_MOBILE")
    check("стратегия по умолчанию — окно 5", vm.DEFAULTS["strategy"] == "window:5")


if __name__ == "__main__":
    test_lint()
    test_diag()
    test_notify()
    test_proxy_conf()
    print("\nитого: ok %d, fail %d" % (passed, failed))
    sys.exit(1 if failed else 0)
