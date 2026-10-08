#!/usr/bin/env python3
"""Проверки режима usb proxyveth (pcs/proxyveth/usb.py) без root, без ВМ и без сети.

  * разбор таблицы в режиме usb (N и N+200, 153–155 — как у mp.space);
  * диагностика прокси и модема: поддельный мир в этом же процессе —
    SOCKS5 с разными поломками, веб-морда HiLink, «интернет» с 204,
    портал оператора, дыра, куда уходит трафик;
  * модем внутри: конфиг sing-box (настоящий `sing-box check`), юниты, шины USB;
  * Row, status, diag и apply по интерфейсу §6 — на подменённой системе.

    python3 tests/test_proxyveth_usb.py
"""
import asyncio
import json
import os
import socket
import sys
import tempfile
import threading
import types

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, os.pardir))
sys.path.insert(0, HERE)
from pcs.core import singbox, table  # noqa: E402
from pcs.proxyveth import usb as vm  # noqa: E402
from singbox_check import sb_check  # noqa: E402

passed = failed = 0
vm.log = lambda msg: None                 # журнал /var/log/proxyveth в тестах не нужен


def check(name, cond, detail=""):
    global passed, failed
    if cond:
        passed += 1
        print("  ok   %s" % name)
    else:
        failed += 1
        print("  FAIL %s%s" % (name, ("\n       " + str(detail)) if detail else ""))


def patch(obj, **kw):
    """Подменить атрибуты; вернуть функцию, которая всё вернёт обратно."""
    old = {k: getattr(obj, k) for k in kw}
    for k, v in kw.items():
        setattr(obj, k, v)
    return lambda: [setattr(obj, k, v) for k, v in old.items()]


# ── таблица ────────────────────────────────────────────────────────────────
def test_lint():
    print("таблица (режим usb):")
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
    d, dis, inv, probs = table.lint(csv, taken_octets={88}, mp_tables=True)
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
    check("пустая таблица — честная ошибка", "нет шапки" in table.lint("")[3][0])
    check("чужая таблица — честная ошибка", "нет шапки" in table.lint("что,то\n1,2\n")[3][0])
    d2, _, _, p2 = table.lint("n,proxy\n5,h:1:u:p\n6,h:1:u:p\n")
    check("одна прокси на двоих — предупреждение", any("одной прокси" in x for x in p2), p2)
    check("хэш строки меняется вместе с прокси",
          vm.spec_hash(dict(d2[5])) != vm.spec_hash(dict(d2[5], pw="другой")))

    print("ссылка:")
    check("адресная строка → CSV",
          table.normalize_url("https://docs.google.com/spreadsheets/d/AbC_1/edit#gid=456")
          == "https://docs.google.com/spreadsheets/d/AbC_1/export?format=csv&gid=456")
    check("опубликованная → CSV",
          table.normalize_url("https://docs.google.com/spreadsheets/d/e/2PACX-1/pubhtml?gid=12")
          == "https://docs.google.com/spreadsheets/d/e/2PACX-1/pub?gid=12&single=true&output=csv")
    check("чужой адрес не трогаем", table.normalize_url("https://x.ru/m.csv") == "https://x.ru/m.csv")


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
        cls, info, ip = vm.check_up(dict(p, **kw.get("p", {})))
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

    print("diag N — шаги по цепочке (§6):")
    undo = patch(vm, load_cfg=lambda: vm.DEFAULTS, one=lambda n: dict(p, n=n),
                 check_local=lambda n, cfg: ("ok", []), host_side=lambda n: ("3-1", "eth1"),
                 addr_of=lambda i: "192.168.201.100")
    try:
        w.socks_mode, w.net_mode, w.conn, w.sim = "ok", "ok", "901", "1"
        d = vm.diag(201)
        names = [s["name"] for s in d["steps"]]
        check("исправный: прокси → логин → модем → SIM → интернет → своя сторона",
              names == ["прокси", "логин", "модем", "SIM", "интернет", "своя сторона"] and all(s["ok"] for s in d["steps"]), d)
        check("исправный: verdict ok, внешний IP", d["verdict"] == "ok" and d["ext_ip"] == "203.0.113.7", d)
        check("конверт §6: n, steps[name, ok, text], verdict",
              d["n"] == 201 and all(set(s) == {"name", "ok", "text"} for s in d["steps"]), d)
        p_bad = dict(p, real=99)
        vm.one = lambda n: dict(p_bad, n=n)
        d = vm.diag(201)
        oks = [(s["name"], s["ok"]) for s in d["steps"]]
        check("модема за прокси нет: обрыв на шаге «модем», дальше не идём",
              oks == [("прокси", True), ("логин", True), ("модем", False), ("своя сторона", True)], oks)
        check("сломан апстрим, своя сторона цела → warn", d["verdict"] == "warn", d["verdict"])
        vm.check_local = lambda n, cfg: ("broken", ["usb0 без адреса"])
        d = vm.diag(201)
        check("своя сторона сломана → broken", d["verdict"] == "broken" and not d["steps"][-1]["ok"], d)
    finally:
        undo()
    vm.socks_connect = orig


def test_notify():
    print("оповещения:")
    vm.LIB = tempfile.mkdtemp()
    sent = []
    undo = patch(vm, server_ip=lambda: "10.0.0.7", notify=lambda cfg, text: sent.append(text))
    try:
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
    finally:
        undo()


def test_proxy_conf():
    print("прямые прокси (из vmodem):")
    cfg = vm.merge(vm.DEFAULTS, {"proxy": {"enabled": True, "user": "u1", "pass": "p1", "base_port": 20000}})
    c = vm.proxy_conf(cfg, [201, 202])
    ports = [i["listen_port"] for i in c["inbounds"]]
    check("порт = база + n", ports == [20201, 20202], ports)
    check("выход с адреса модема", c["outbounds"][0]["inet4_bind_address"] == "192.168.201.100", c["outbounds"][0])
    check("имена — DNS самого модема по TCP через его же выход",
          c["dns"]["servers"][0] == {"type": "tcp", "tag": "dns-201", "server": "192.168.201.1", "detour": "out-201"}
          and c["outbounds"][0]["domain_resolver"]["server"] == "dns-201", c["dns"]["servers"][0])
    check("непонятное — отказ (route-действие, без outbound block)",
          c["route"]["rules"][-1] == {"action": "reject"} and all(o["type"] == "direct" for o in c["outbounds"]))
    check("логин обязателен", c["inbounds"][0]["users"] == [{"username": "u1", "password": "p1"}])
    ok, msg = sb_check(c)
    if ok is not None:
        check("sing-box %s принимает конфиг прямых прокси" % singbox.VERSION, ok, msg)

    print("вид для сервера:")
    u = vm.DEFAULTS["usb"]
    check("Huawei 12d1:14dc", (u["vendor"], u["product_id"]) == ("0x12d1", "0x14dc"))
    check("MAC со стороны сервера — как у всех E3372h", u["host_mac"] == "0c:5b:8f:27:9a:64")
    check("строки HUAWEI_MOBILE", u["manufacturer"] == u["product"] == "HUAWEI_MOBILE")
    check("стратегия по умолчанию — окно 5", vm.DEFAULTS["strategy"] == "window:5")


def test_layout():
    print("модем внутри:")
    vm.MDIR, vm.LOGD = tempfile.mkdtemp(), tempfile.mkdtemp()
    p = {"n": 201, "real": 81, "host": "188.134.95.184", "port": 1198, "user": "modem81", "pw": "pw"}
    vm.write_configs(p)
    sb = json.load(open(os.path.join(vm.mdir(201), "singbox.json")))
    px = sb["outbounds"][0]
    check("к прокси — через сокет внутри netns, не напрямую",
          (px["server"], px["server_port"]) == ("127.0.0.1", vm.RELAY_PORT), px)
    check("стек system — tun создаётся прямо в netns", sb["inbounds"][0]["stack"] == "system", sb["inbounds"][0])
    check("tun называется pvtun0", sb["inbounds"][0]["interface_name"] == "pvtun0")
    check("любой DNS (и на 8.8.8.8) — к DNS настоящего модема",
          {"port": 53, "action": "hijack-dns"} in sb["route"]["rules"]
          and {"inbound": ["dns-in"], "action": "hijack-dns"} in sb["route"]["rules"], sb["route"]["rules"])
    check("DNS модема — tcp://192.168.real.1 через прокси в формате 1.12+",
          sb["dns"]["servers"] == [{"type": "tcp", "tag": "modem", "server": "192.168.81.1", "detour": "proxy"}],
          sb["dns"]["servers"])
    check("без служебного outbound dns и без sniff на inbound",
          all(o["type"] not in ("dns", "block") for o in sb["outbounds"])
          and not any(k.startswith("sniff") for i in sb["inbounds"] for k in i), sb)
    ok, msg = sb_check(sb)
    if ok is not None:
        check("sing-box %s принимает конфиг модема (без deprecated)" % singbox.VERSION, ok, msg)
    env = open(os.path.join(vm.mdir(201), "env")).read()
    check("пароль прокси — только в proxy.pass (600), не в env",
          "pw" not in env.split("SOCKS_USER=")[0] and open(os.path.join(vm.mdir(201), "proxy.pass")).read() == "pw\n"
          and oct(os.stat(os.path.join(vm.mdir(201), "proxy.pass")).st_mode & 0o777) == "0o600", env)
    dq = vm.dnsmasq_conf(p)
    check("DHCP выдаёт ровно .100, шлюз и DNS — сам модем",
          "dhcp-range=192.168.201.100,192.168.201.100,255.255.255.0,24h" in dq and "dhcp-option=6,192.168.201.1" in dq, dq)

    units = vm.units()
    sock, relay = units["proxyveth-px@.socket"], units["proxyveth-px@.service"]
    check("юниты — proxyveth-{dns,px,sb,web}@N, и ни следа vmodem",
          {"proxyveth-dns@.service", "proxyveth-px@.socket", "proxyveth-px@.service", "proxyveth-sb@.service",
           "proxyveth-web@.service"} <= set(units) and all(k.startswith("proxyveth-") for k in units)
          and "vmodem" not in json.dumps(units) + vm.HOOK_SH + vm.HOST_RULE_TEXT + vm.HOST_LINK_TEXT)
    check("сокет ретранслятора — в netns модема pvN, на том же порту",
          "NetworkNamespacePath=/run/netns/pv%i" in sock and "127.0.0.1:%d" % vm.RELAY_PORT in sock, sock)
    check("сам ретранслятор — systemd-socket-proxyd на сервере (своего netns нет)",
          "NetworkNamespacePath" not in relay and "systemd-socket-proxyd" in relay and "${SOCKS}" in relay, relay)
    check("sing-box — в netns модема", "NetworkNamespacePath=/run/netns/pv%i" in units["proxyveth-sb@.service"])
    check("после (пере)запуска sing-box — маршруты через tun",
          "ExecStartPost=" in units["proxyveth-sb@.service"] and " routes %i" in units["proxyveth-sb@.service"])
    web = units["proxyveth-web@.service"]
    check("веб-морда — на сервере, слушает в netns, пароль из файла",
          "NetworkNamespacePath" not in web and "--netns /run/netns/pv%i" in web
          and "modem_web.py" in web and "--socks-pass-file" in web, web)
    check("перезагрузка через веб-морду → replug --reboot своим юнитом",
          "--unit=proxyveth-reboot-%i" in web and "replug %i --reboot" in web, web)
    check("строка та же — пересоздание из-за новой схемы", vm.same_row(201, dict(p)))
    check("строка поменялась — так и сказать", not vm.same_row(201, dict(p, pw="другой")))
    check("модема ещё нет — не «новая схема»", not vm.same_row(202, dict(p, n=202)))

    check("свой sing-box из pcs.core.singbox, не общий /usr/local/bin",
          not singbox.BIN.startswith("/usr/local/bin/")
          and all(singbox.BIN + " run" in units[u] for u in ("proxyveth-sb@.service", "proxyveth-proxy.service")))

    print("DNS сервера:")
    check("sing-box без D-Bus — не пишет DNS своего tun в resolved сервера",
          "InaccessiblePaths=-/run/dbus/system_bus_socket" in units["proxyveth-sb@.service"])
    world2 = {("resolvectl", "dns"): "Global: 1.1.1.1 8.8.8.8\nLink 2 (eth0): 172.20.0.2\n"
                                     "Link 4 (eth1): 192.168.201.1\nLink 6 (eth2): 172.20.0.2 192.168.202.1\n"}
    ran2 = []

    def fake_sh2(*c, **k):
        ran2.append(c)
        return types.SimpleNamespace(stdout=world2.get(c, ""), returncode=0)
    undo = patch(vm, sh=fake_sh2)
    try:
        vm.drop_tun_dns()
    finally:
        undo()
    fixed = [c[2] for c in ran2 if c[:2] == ("resolvectl", "revert")]
    check("чужой 172.20.0.2 снят там, где висит, и только там", fixed == ["eth0", "eth2"], fixed)
    check("DNS из аренды возвращается", ("networkctl", "renew", "eth0") in ran2, ran2)

    print("сторона сервера:")
    check("udev — только свои модемы (dummy_hcd/vhci_hcd), настоящие — забота hivelink",
          'DEVPATH=="/devices/platform/dummy_hcd.*|/devices/platform/vhci_hcd.*"' in vm.HOST_RULE_TEXT
          and "proxyveth-host@%k.service" in vm.HOST_RULE_TEXT)
    check("имена ethN, а не enx… по общему MAC", "NamePolicy=\n" in vm.HOST_LINK_TEXT)
    check("таблицы как у mp.space: 100 + октет % 200", "t=$((100 + $(echo \"$ip\" | cut -d. -f3) % 200))" in vm.HOOK_SH)
    check("хук udhcpc — с префиксом proxyveth", os.path.basename(vm.HOOK).startswith("proxyveth-"))

    print("шины USB:")
    vm.HCD_DRV, vm.GROOT = tempfile.mkdtemp(), tempfile.mkdtemp()
    for k in range(4):
        os.mkdir(os.path.join(vm.HCD_DRV, "dummy_hcd.%d" % k))
    os.mkdir(os.path.join(vm.GROOT, "pv201"))
    open(os.path.join(vm.GROOT, "pv201", "UDC"), "w").write("dummy_udc.0\n")
    writes = []
    undo = patch(vm, wr=lambda path, value, mode=None: writes.append((os.path.basename(path), value)))
    try:
        vm.park_idle_hcds({"modems": {"202": {"slot": "dummy_udc.2"}}})
        parked = sorted(v for f, v in writes if f == "unbind")
        writes.clear()
        vm.bind(203, "dummy_udc.9")
    finally:
        undo()
    check("пустые шины убраны, шины модемов — нет", parked == ["dummy_hcd.1", "dummy_hcd.3"], parked)
    check("модем перезагружается (гаджет отвязан) — его шину не трогаем", "dummy_hcd.2" not in parked)
    check("воткнуть модем — сначала вернуть шину его слота",
          writes[:2] == [("bind", "dummy_hcd.9"), ("UDC", "dummy_udc.9")], writes)
    check("гаджет модема — pvN", vm.gdir(203).endswith("/pv203"))


# ── интерфейс режима (§6) ──────────────────────────────────────────────────
ROW_KEYS = {"n", "real", "proxy", "state", "problems", "iface", "ext_ip", "since"}


def test_row():
    print("Row (§6):")
    p = {"n": 201, "real": 81, "host": "188.134.95.184", "port": 1198, "user": "u", "pw": "секрет"}
    base = {"n": 201, "real": 81, "local": "ok", "local_problems": [], "usb": "3-1", "iface": "eth1",
            "host_ip": "192.168.201.100", "up": "ok", "up_info": [], "ip": "94.25.229.40", "t": 1}
    r = vm.to_row(base, p, {"since": 1760000000})
    check("ровно поля Row", set(r) == ROW_KEYS, sorted(r))
    check("исправный → ok, внешний IP, с какого времени",
          r["state"] == "ok" and r["ext_ip"] == "94.25.229.40" and r["since"] == 1760000000 and not r["problems"], r)
    check("прокси без логина и пароля", r["proxy"] == "188.134.95.184:1198" and "секрет" not in json.dumps(r), r)
    for name, over, want in (
            ("апстрим сломан, своя сторона цела → warn", dict(up="proxy-auth", up_info=["логин/пароль не приняты"]), "warn"),
            ("сервер ещё не взял адрес → warn", dict(local="wait-host", local_problems=["у eth1 нет 192.168.201.100"]), "warn"),
            ("своя сторона сломана → broken", dict(local="broken", local_problems=["usb0 без адреса"]), "broken"),
            ("модема нет → absent", dict(local="absent", local_problems=["модема нет"]), "absent"),
            ("перезагружается → rebooting", dict(local="rebooting", local_problems=["перезагружается"]), "rebooting")):
        r = vm.to_row(dict(base, **over), p, {})
        check(name, r["state"] == want and r["problems"], r)
    r = vm.to_row(dict(base, up="proxy-auth", up_info=["логин/пароль не приняты"]), p, {})
    check("причина апстрима — по-человечески", "логин" in r["problems"][0], r["problems"])

    print("status — все модемы, и выключенные, и отбракованные:")
    rows_in = [dict(base), dict(base, n=202, real=82, local="absent", local_problems=["модема нет"], iface="", ip="")]
    undo = patch(vm, load_cfg=lambda: vm.DEFAULTS,
                 table_now=lambda: ({201: p, 202: dict(p, n=202, real=82)}, {204}, {209}),
                 live_specs=lambda: {}, evaluate=lambda specs, cfg, **kw: rows_in,
                 remember=lambda rows, cfg, **kw: None)
    try:
        rows = vm.status()
    finally:
        undo()
    by = {r["n"]: r for r in rows}
    check("строки по порядку номеров, у каждой — поля Row",
          [r["n"] for r in rows] == [201, 202, 204, 209] and all(set(r) == ROW_KEYS for r in rows), rows)
    check("из таблицы, но не создан → absent", by[202]["state"] == "absent")
    check("enabled=0 → disabled", by[204]["state"] == "disabled")
    check("кривая строка → absent с подсказкой про lint", by[209]["state"] == "absent" and "lint" in by[209]["problems"][0])


def test_apply():
    print("apply — план приведения к таблице:")
    pa = {"n": 1, "real": 81, "host": "h", "port": 1, "user": "u", "pw": "p"}
    desired = {1: pa, 2: dict(pa, n=2, real=82), 4: dict(pa, n=4, real=84)}
    st = {"modems": {"1": {"hash": vm.spec_hash(pa), "plugged": 0}, "2": {"hash": "старый"}}, "bad_slots": []}
    did = {"destroy": [], "create": None, "proxy": None}
    said = []
    undo = patch(vm, state=lambda: st, save_state=lambda s: None, ensure_module=lambda cfg: None,
                 apply_hostside=lambda cfg: None, running=lambda: [1, 2, 3, 9],
                 check_local=lambda n, cfg: ("ok", []), same_row=lambda n, p: True,
                 destroy=lambda n, quiet=False: did["destroy"].append(n),
                 create_all=lambda todo, d, cfg, s, strat: (did.update(create=list(todo)) or
                                                           ({n: 1.0 for n in todo if n != 4}, {4: "сервер не взял адрес за 90с"}, 2.0)),
                 park_idle_hcds=lambda s: None, apply_proxy=lambda cfg, ns: did.update(proxy=list(ns)),
                 evaluate=lambda *a, **k: [], remember=lambda *a, **k: None, log=said.append)
    try:
        res = vm.apply(desired, dict(vm.DEFAULTS, keep=[9]))
    finally:
        undo()
    check("снесено только лишнее (9 — кривая строка, его не трогаем)", res["removed"] == [3] and did["destroy"] == [3], res)
    check("сначала новые, потом пересоздаваемые", did["create"] == [4, 2], did["create"])
    check("ответ по §6: created / recreated / removed / failed",
          set(res) == {"created", "recreated", "removed", "failed"} and res["recreated"] == [2]
          and res["created"] == [] and res["failed"] == {4: "сервер не взял адрес за 90с"}, res)
    check("причина пересоздания названа: новая схема модема", any("новая схема модема" in m for m in said), said)
    check("прямые прокси — и на сохранённом модеме с кривой строкой", did["proxy"] == [1, 2, 9], did["proxy"])


def test_teardown():
    print("teardown — снять всё своё:")
    tmp = tempfile.mkdtemp()
    paths = dict(UNITD=os.path.join(tmp, "units"), MDIR=os.path.join(tmp, "etc", "usb"), RUN=os.path.join(tmp, "run"),
                 LIB=os.path.join(tmp, "lib"), HOST_RULE=os.path.join(tmp, "80-proxyveth-host.rules"),
                 HOST_LINK=os.path.join(tmp, "10-proxyveth-cdc.link"), MODPROBE=os.path.join(tmp, "proxyveth.conf"),
                 MODLOAD=os.path.join(tmp, "proxyveth-load.conf"), HOOK=os.path.join(tmp, "hook"),
                 GROOT=os.path.join(tmp, "gadgets"), HCD_DRV=os.path.join(tmp, "hcd"))
    for d in ("UNITD", "MDIR", "RUN", "LIB", "GROOT", "HCD_DRV"):
        os.makedirs(paths[d])
    keep_cfg = os.path.join(tmp, "etc", "config.json")
    open(keep_cfg, "w").write("{}")
    gone, ran = [], []
    undo = patch(vm, running=lambda: [201], destroy=lambda n, quiet=False: gone.append(n),
                 sh=lambda *c, **k: ran.append(c) or types.SimpleNamespace(stdout="", returncode=0),
                 log=lambda m: None, **paths)
    try:
        os.mkdir(os.path.join(vm.GROOT, "pv202"))           # гаджет без netns — тоже наш
        os.mkdir(os.path.join(vm.GROOT, "other"))
        for name in vm.units():
            open(os.path.join(vm.UNITD, name), "w").write("x")
        open(os.path.join(vm.UNITD, "modlink.service"), "w").write("чужое")
        for f in ("HOST_RULE", "HOST_LINK", "MODPROBE", "MODLOAD", "HOOK"):
            open(paths[f], "w").write("x")
        vm.teardown()
        left = sorted(os.listdir(vm.UNITD))
    finally:
        undo()
    check("сняты модемы с netns и без", gone == [201, 202], gone)
    check("свои юниты удалены, чужие — нет", left == ["modlink.service"], left)
    check("udev, .link, modprobe, хук, конфиги модемов — удалены",
          not any(os.path.exists(paths[f]) for f in ("HOST_RULE", "HOST_LINK", "MODPROBE", "MODLOAD", "HOOK", "MDIR")))
    check("настройки сервера остаются", os.path.exists(keep_cfg))
    check("прямые прокси и usbipd остановлены",
          ("systemctl", "disable", "--now", "proxyveth-proxy", "proxyveth-usbipd") in ran, ran)


if __name__ == "__main__":
    test_lint()
    test_diag()
    test_notify()
    test_proxy_conf()
    test_layout()
    test_row()
    test_apply()
    test_teardown()
    print("\nитого: ok %d, fail %d" % (passed, failed))
    sys.exit(1 if failed else 0)
