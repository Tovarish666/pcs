#!/usr/bin/env python3
"""Проверки proxyveth gw без root, без ВМ и без сети (кроме sing-box для sb_check).

  * конфиг sing-box — настоящим `sing-box check` 1.14;
  * номера таблиц, приоритеты, пометки iptables, имена — по командам, которые ушли бы в систему;
  * уборка снимает только своё;
  * apply: создать / пересоздать / починить / снести, защита от сноса, пауза после неудач;
  * Row и diag — по форме §6;
  * посредник Host — живым запросом через себя;
  * апстрим — поддельный SOCKS5 и HiLink в этом же процессе;
  * перенос с proxyveth-virt 3.4 на поддельном корне ФС.

    python3 tests/test_proxyveth_gw.py
"""
import asyncio
import json
import os
import re
import shutil
import socket
import struct
import sys
import tempfile
import threading
import time
import types

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, os.pardir))
sys.path.insert(0, HERE)
from pcs.core import net, singbox, util  # noqa: E402
from pcs.proxyveth import gw, gw_hostfix, gw_legacy, gw_probe  # noqa: E402
import singbox_check  # noqa: E402

passed = failed = 0


def check(name, cond, detail=""):
    global passed, failed
    if cond:
        passed += 1
        print("  ok   %s" % name)
    else:
        failed += 1
        print("  FAIL %s%s" % (name, ("\n       " + str(detail)) if detail else ""))


class World:
    """Поддельный sh: команды записываются, ответы — по префиксу (последний заданный — главнее)."""

    def __init__(self):
        self.calls, self.inputs, self.table = [], {}, []
        self.on(("ip", "rule", "del"), (2, ""))       # «нет такого правила» — циклы удаления кончаются

    def on(self, prefix, result):
        self.table.insert(0, (tuple(prefix), result))
        return self

    def __call__(self, *cmd, check=True, timeout=None, env=None, input=None):
        self.calls.append(cmd)
        if input is not None:
            self.inputs[cmd] = input
        res = (0, "")
        for pre, r in self.table:
            if cmd[:len(pre)] == pre:
                res = r(cmd) if callable(r) else r
                break
        rc, out = (0, res) if isinstance(res, str) else res
        if check and rc != 0:
            raise util.Fail("%s: rc=%d" % (" ".join(cmd), rc))
        return types.SimpleNamespace(returncode=rc, stdout=out, stderr="")

    def ran(self, *prefix):
        return [c for c in self.calls if c[:len(prefix)] == prefix]


ROOT = tempfile.mkdtemp(prefix="pv-gw-")
LOGS = []
gw.ROOT = ROOT
gw.log = LOGS.append
gw.uplink = lambda: ("192.168.88.1", "ens18")
gw.local_octets = lambda: {88}
gw.resolve = lambda h: {"px.example.net": "188.134.95.184"}.get(h, h)
os.makedirs(ROOT + "/dev/net", exist_ok=True)
open(ROOT + "/dev/net/tun", "w").close()
SB = singbox_check.binary()
REAL_CHECK = singbox.check


def sbcheck(conf, binary=None):
    return REAL_CHECK(conf, SB) if SB else (True, "")


def fresh_root():
    for d in os.listdir(ROOT):
        if d != "dev":
            shutil.rmtree(os.path.join(ROOT, d), ignore_errors=True)


def row(n, real=None, host="188.134.95.184", port=1198, user=None, pw=None, enabled=True):
    real = n if real is None else real
    return {"n": n, "real": real, "host": host, "port": port, "user": user or "modem%d" % real,
            "pw": pw or "pw%d" % real, "enabled": enabled}


def walk(o):
    if isinstance(o, dict):
        for k, v in o.items():
            yield k, v
            yield from walk(v)
    elif isinstance(o, list):
        for v in o:
            yield from walk(v)


# ── sing-box ───────────────────────────────────────────────────────────────
print("конфиг sing-box (%s):" % singbox.VERSION)
for r in (row(201, 81), row(5), row(254, 7, user="u:x", pw='p"a\\s:s')):
    conf = gw.sb_config(r, "188.134.95.184")
    keys = {k for k, _ in walk(conf)}
    check("n=%d: нет устаревших полей" % r["n"], not keys & {"sniff", "sniff_override_destination", "domain_strategy",
                                                              "independent_cache"}
          and all("address" not in s for s in conf["dns"]["servers"])
          and all(o["type"] not in ("dns", "block") for o in conf["outbounds"]), sorted(keys))
    ok, msg = singbox_check.sb_check(conf)
    if ok is not None:
        check("n=%d: sing-box check принимает" % r["n"], ok, msg)
c = gw.sb_config(row(201, 81), "188.134.95.184")
check("DNS по TCP через прокси", c["dns"]["servers"] == [{"type": "tcp", "tag": "remote", "server": "1.1.1.1",
                                                          "detour": "proxy"}] and c["dns"]["strategy"] == "ipv4_only")
check("DNS перехватывается route-действием", {"port": 53, "action": "hijack-dns"} in c["route"]["rules"])
check("tun pvtun0, своя сеть модема", c["inbounds"][0]["interface_name"] == "pvtun0"
      and c["inbounds"][0]["address"] == ["10.0.201.1/30"])
if SB:
    bad = json.loads(json.dumps(c))
    bad["dns"]["servers"] = [{"tag": "remote", "address": "tcp://1.1.1.1", "detour": "proxy"}]
    check("старый формат 1.10 — отказ (проверка настоящая)", sbcheck(bad)[0] is False)

# ── юниты ──────────────────────────────────────────────────────────────────
print("юниты:")
u = gw.unit_texts()
sb_unit, hf_unit = u["proxyveth-gw@.service"], u["proxyveth-hostfix@.service"]
check("шаблонные юниты proxyveth-gw@ и proxyveth-hostfix@", set(u) == {"proxyveth-gw@.service",
                                                                      "proxyveth-hostfix@.service"})
check("sing-box — наш бинарник, конфиг модема", "ExecStart=%s run -c /etc/proxyveth/gw/%%i/singbox.json" % singbox.BIN
      in sb_unit and "/usr/local/bin/sing-box" not in sb_unit)
check("sing-box без D-Bus", "InaccessiblePaths=-/run/dbus/system_bus_socket" in sb_unit)
check("оба — в netns pvN", all("NetworkNamespacePath=/run/netns/pv%i" in t for t in u.values()))
check("маршруты через tun — при каждом старте", "ExecStartPost=/usr/bin/python3 -m pcs.proxyveth.gw _routes %i"
      in sb_unit)
check("перезапуск без предела", all("Restart=always" in t and "StartLimitIntervalSec=0" in t for t in u.values()))
check("не включаются сами при загрузке (их поднимает sync)", all("[Install]" not in t for t in u.values()))
check("посредник — модулем", "ExecStart=/usr/bin/python3 -m pcs.proxyveth.gw_hostfix %i" in hf_unit)

# ── номера и пометки ───────────────────────────────────────────────────────
print("номера, имена, пометки:")
check("таблица и приоритет — из pcs.core.net", gw.rt(5) == net.table("gw", 5) == 1005
      and gw.prio(5) == net.prio("gw", 5) == 30005 and gw.prio(254) < net.PIN_PRIO)
check("все правила внутри netns помечены", all("--comment pcs:proxyveth" in ln for ln in gw.ns_rules().splitlines()
                                              if ln.startswith("-A")))
check("TCPMSS — первым в FORWARD и помечен", gw.MSS[:2] == ["FORWARD", "1"] and "pcs:proxyveth" in gw.MSS)
check("NAT к прокси — с адреса netns, через канал, помечен",
      gw.nat_rule(5, "ens18") == ["POSTROUTING", "-s", "192.168.5.254/32", "-o", "ens18", "-m", "comment",
                                  "--comment", "pcs:proxyveth", "-j", "MASQUERADE"])
check("пометка в кавычках (как печатает iptables) узнаётся",
      gw.tagged('-A POSTROUTING -s 192.168.5.254/32 -o ens18 -m comment --comment "pcs:proxyveth" -j MASQUERADE')
      is not None and gw.tagged('-A POSTROUTING -m comment --comment "pcs:hivelink" -j MASQUERADE') is None)

# ── создание одного модема ─────────────────────────────────────────────────
print("подъём модема:")
fresh_root()
INNER_OK = ("default dev pvtun0 scope link\n10.0.%d.0/30 dev pvtun0 proto kernel scope link src 10.0.%d.1\n"
            "188.134.95.184 via 192.168.%d.100 dev eth0 src 192.168.%d.254\n"
            "192.168.%d.0/24 dev eth0 proto kernel scope link src 192.168.%d.254\n")


def inner_ok(n):
    return INNER_OK % ((n,) * 6)


w = World()
w.on(("ip", "-n", "pv201", "-4", "route", "show"), inner_ok(201))
w.on(("iptables", "-w", "5", "-t", "nat", "-C"), (1, ""))
gw.sh = w
ip = gw.create(row(201, 81, host="px.example.net"), ("192.168.88.1", "ens18"), gw.DEFAULTS)
check("адрес прокси резолвится один раз на сервере", ip == "188.134.95.184")
check("veth pv201 ↔ eth0 в netns pv201 одной командой",
      ("ip", "link", "add", "pv201", "type", "veth", "peer", "name", "eth0", "netns", "pv201") in w.calls)
check("сервер 192.168.201.100/24, netns 192.168.201.254/24",
      ("ip", "addr", "add", "192.168.201.100/24", "dev", "pv201") in w.calls
      and ("ip", "-n", "pv201", "addr", "add", "192.168.201.254/24", "dev", "eth0") in w.calls)
check("real ≠ n: 192.168.201.1 в netns — посреднику",
      ("ip", "-n", "pv201", "addr", "add", "192.168.201.1/32", "dev", "eth0") in w.calls)
check("rp_filter=0 только на своих интерфейсах",
      ("sysctl", "-qw", "net.ipv4.conf.pv201.rp_filter=0") in w.calls
      and not any("conf.all.rp_filter" in " ".join(c) for c in w.calls if c[:2] != ("ip", "netns")))
check("ip rule с явным приоритетом", ("ip", "rule", "add", "priority", "30201", "from", "192.168.201.100",
                                      "lookup", "1201") in w.calls)
check("таблица 1201: default через netns",
      ("ip", "route", "replace", "default", "via", "192.168.201.254", "dev", "pv201", "table", "1201") in w.calls)
check("NAT через основной канал, с пометкой",
      ("iptables", "-w", "5", "-t", "nat", "-A") + tuple(gw.nat_rule(201, "ens18")) in w.calls)
check("маршрут к прокси — мимо туннеля, с адресом для NAT",
      ("ip", "-n", "pv201", "route", "add", "188.134.95.184/32", "via", "192.168.201.100", "dev", "eth0",
       "src", "192.168.201.254") in w.calls)
rest = [k for k in w.inputs if "iptables-restore" in k]
check("iptables в netns — одним iptables-restore", rest and w.inputs[rest[0]] == gw.ns_rules())
check("sing-box и посредник — юнитами, не дочерними процессами",
      ("systemctl", "start", "proxyveth-gw@201") in w.calls and ("systemctl", "start", "proxyveth-hostfix@201") in w.calls
      and not any("sing-box" in " ".join(c) for c in w.calls))
d = ROOT + "/etc/proxyveth/gw/201"
check("конфиги модема — 600 в каталоге 700", oct(os.stat(d + "/singbox.json").st_mode & 0o777) == "0o600"
      and oct(os.stat(d).st_mode & 0o777) == "0o700")
check("spec.json для посредника и маршрутов", json.load(open(d + "/spec.json")) == {"n": 201, "real": 81,
                                                                                   "proxy_ip": "188.134.95.184"})
w = World()
w.on(("ip", "-n", "pv5", "-4", "route", "show"), inner_ok(5))
w.on(("iptables", "-w", "5", "-t", "nat", "-C"), (1, ""))
gw.sh = w
gw.create(row(5), ("192.168.88.1", "ens18"), gw.DEFAULTS)
check("real = n: посредника нет", not w.ran("systemctl", "start", "proxyveth-hostfix@5")
      and not any("192.168.5.1/32" in c for c in w.calls))
w = World()
w.on(("systemctl", "start", "proxyveth-gw@5"), (1, ""))
w.on(("journalctl",), "FATAL start tun: operation not permitted\n")
gw.sh = w
try:
    gw.create(row(5), ("192.168.88.1", "ens18"), gw.DEFAULTS)
    check("сбой sing-box — честная ошибка", False)
except util.Fail as e:
    check("сбой sing-box — ошибка с шагом и журналом", "шаг «sing-box»" in str(e) and "operation not permitted" in str(e), e)
    check("…и полуживое снесено", ("ip", "netns", "del", "pv5") in w.calls[-12:]
          and not os.path.exists(ROOT + "/etc/proxyveth/gw/5"))

# ── уборка — только своё ───────────────────────────────────────────────────
print("уборка:")
NAT = "\n".join([
    "-P POSTROUTING ACCEPT",
    "-A POSTROUTING -s 192.168.5.0/24 -o ens18 -j MASQUERADE",
    '-A POSTROUTING -s 192.168.5.254/32 -o ens18 -m comment --comment "pcs:proxyveth" -j MASQUERADE',
    '-A POSTROUTING -s 192.168.6.254/32 -o ens18 -m comment --comment "pcs:proxyveth" -j MASQUERADE',
    '-A POSTROUTING -s 192.168.5.0/24 -m comment --comment "pcs:hivelink" -j MASQUERADE', ""])
w = World().on(("iptables", "-w", "5", "-t", "nat", "-S", "POSTROUTING"), NAT)
gw.sh = w
gw.remove(5, quiet=True)
dels = w.ran("iptables", "-w", "5", "-t", "nat", "-D")
check("снято только помеченное своё правило модема 5",
      dels == [("iptables", "-w", "5", "-t", "nat", "-D", "POSTROUTING", "-s", "192.168.5.254/32", "-o", "ens18",
                "-m", "comment", "--comment", "pcs:proxyveth", "-j", "MASQUERADE")], dels)
check("юниты остановлены, netns и таблица сняты",
      ("systemctl", "stop", "proxyveth-hostfix@5", "proxyveth-gw@5") in w.calls
      and ("ip", "netns", "del", "pv5") in w.calls and ("ip", "route", "flush", "table", "1005") in w.calls)
check("правило — по своему приоритету", ("ip", "rule", "del", "priority", "30005") in w.calls)
RULES = ("0:\tfrom all lookup local\n30005:\tfrom 192.168.5.100 lookup 1005\n30009:\tfrom 192.168.9.100 lookup 1009\n"
         "31005:\tfrom 192.168.5.100 lookup 2005\n32764:\tfrom 192.168.7.100 lookup modem7\n"
         "32765:\tfrom all to 188.134.95.184 lookup 90\n32766:\tfrom all lookup main\n")
w = World().on(("iptables", "-w", "5", "-t", "nat", "-S", "POSTROUTING"), NAT).on(("ip", "-4", "rule", "show"), RULES)
w.on(("systemctl", "list-units"), "proxyveth-gw@5.service loaded active running x\n"
                                  "proxyveth-gw@9.service loaded active running x\n"
                                  "proxyveth-hostfix@9.service loaded failed failed x\n")
gw.sh = w
gw.sweep({5})
check("хвосты: правило 30009 — да; hivelink, mp.space, ядро — нет",
      [c for c in w.ran("ip", "rule", "del")] == [("ip", "rule", "del", "priority", "30009")], w.ran("ip", "rule", "del"))
check("хвосты: NAT модема 6 — да, 5 и чужое — нет",
      [c[8] for c in w.ran("iptables", "-w", "5", "-t", "nat", "-D")] == ["192.168.6.254/32"])
check("хвосты: юниты модема 9 остановлены, 5 — нет",
      ("systemctl", "stop", "proxyveth-gw@9.service") in w.calls and ("systemctl", "stop", "proxyveth-hostfix@9.service")
      in w.calls and not w.ran("systemctl", "stop", "proxyveth-gw@5.service"))

# ── своя сторона и Row ─────────────────────────────────────────────────────
print("своя сторона и состояние:")


def world_snap(mods, units=None, nat_dev="ens18", rules_extra=""):
    s = {"netns": {"pv%d" % n for n in mods}, "veth": {"pv%d" % n for n in mods},
         "addr": {"pv%d" % n: ["192.168.%d.100/24" % n] for n in mods},
         "rules": "".join("%d:\tfrom 192.168.%d.100 lookup %d\n" % (30000 + n, n, 1000 + n) for n in mods) + rules_extra,
         "routes": "".join("default via 192.168.%d.254 dev pv%d table %d\n" % (n, n, 1000 + n) for n in mods),
         "nat": [(n, nat_dev, []) for n in mods], "uplink": gw.uplink(),
         "units": units if units is not None else dict(
             [("proxyveth-gw@%d.service" % n, "active") for n in mods]
             + [("proxyveth-hostfix@%d.service" % n, "active") for n in mods])}
    return s


s = world_snap([5, 201])
check("целый модем — без поломок", gw.local(5, row(5), s, inner_ok(5)) == [] and
      gw.local(201, row(201, 81), s, inner_ok(201)) == [])
s2 = world_snap([5], units={})
check("sing-box лёг — видно", [c for c, _ in gw.local(5, row(5), s2, inner_ok(5))] == ["sb"])
s3 = world_snap([5], nat_dev="eth9")
check("NAT на старом канале — видно", [c for c, _ in gw.local(5, row(5), s3, inner_ok(5))] == ["nat"])
check("нет маршрута в туннель — видно", "route" in [c for c, _ in gw.local(5, row(5), s, "")])
s4 = world_snap([201], units={"proxyveth-gw@201.service": "active"})
check("посредник лёг — видно (только при real ≠ n)", [c for c, _ in gw.local(201, row(201, 81), s4, inner_ok(201))]
      == ["hostfix"])
fresh_root()
gw.save_state({"desired": {str(n): r for n, r in {5: row(5), 6: row(6), 7: row(7), 8: row(8, enabled=False),
                                                     9: row(9), 10: row(10)}.items()},
               "modems": {}, "down": []})
S = world_snap([5, 6, 9, 10])
S["units"].pop("proxyveth-gw@6.service")
real_snap, real_inner, real_ext, real_quick = gw.snap, gw.inner_routes, gw.ext_ip, gw_probe.quick
gw.snap = lambda full=True: S
gw.inner_routes = inner_ok
gw.ext_ip = lambda n: {10: "94.25.229.40"}.get(n, "")
gw_probe.quick = lambda r: "прокси %s:%d не отвечает: timed out" % (r["host"], r["port"]) if r["n"] == 9 else ""
gw.sh = World()
rows = {r["n"]: r for r in gw.status(wan=True)}
check("Row — ровно поля §6", all(set(r) == {"n", "real", "proxy", "state", "problems", "iface", "ext_ip", "since"}
                                 for r in rows.values()), rows.get(5))
check("ok / broken / absent / disabled", (rows[5]["state"], rows[6]["state"], rows[7]["state"], rows[8]["state"])
      == ("broken", "broken", "absent", "disabled"), {n: (r["state"], r["problems"]) for n, r in rows.items()})
check("warn — прокси не отвечает, наша сторона цела", rows[9]["state"] == "warn" and "не отвечает" in rows[9]["problems"][0])
check("туннель без выхода при живой прокси — наша сторона (broken)", rows[5]["state"] == "broken"
      and "нашей стороне" in rows[5]["problems"][0])
check("внешний IP через модем", rows[10]["state"] == "ok" and rows[10]["ext_ip"] == "94.25.229.40"
      and rows[10]["proxy"] == "188.134.95.184:1198" and rows[10]["iface"] == "pv10")
since = rows[9]["since"]
rows2 = {r["n"]: r for r in gw.status(wan=False)}
check("без --wan: апстрим из последней проверки, since не сбрасывается",
      rows2[9]["state"] == "warn" and rows2[9]["since"] == since and rows2[10]["ext_ip"] == "94.25.229.40")
gw.save_state(dict(gw.load_state(), down=[7]))
check("снятый вручную — absent с подсказкой", "proxyveth up 7" in {r["n"]: r for r in gw.status()}[7]["problems"][0])
S["netns"].add("ns_7")
gw.save_state(dict(gw.load_state(), down=[]))
check("ещё по-старому — absent с подсказкой", "proxyveth-virt" in {r["n"]: r for r in gw.status()}[7]["problems"][0])
S["netns"].discard("ns_7")

# ── diag ───────────────────────────────────────────────────────────────────
print("diag:")
real_chain, real_web, real_dns = gw_probe.chain, gw.web, gw.dns_probe
gw_probe.chain = lambda r: ([("прокси", True, "SOCKS5"), ("логин", True, "принят"), ("модем", True, "HiLink"),
                             ("SIM", True, "ок"), ("интернет", True, "есть, внешний IP 94.25.229.40")], "", "94.25.229.40")
gw.web = lambda n: (200, "<SesInfo>x</SesInfo>")
gw.dns_probe = lambda n: True
dg = gw.diag(10)
check("diag — форма §6", set(dg) == {"n", "steps", "verdict"} and all(set(x) == {"name", "ok", "text"}
                                                                        for x in dg["steps"]), dg)
check("diag: цепочка от своей стороны до веб-морды", [x["name"] for x in dg["steps"]] ==
      ["своя сторона", "прокси", "логин", "модем", "SIM", "интернет", "через туннель", "DNS через туннель",
       "веб-морда 192.168.10.1"] and dg["verdict"] == "ok", [x["name"] for x in dg["steps"]])
gw_probe.chain = lambda r: ([("прокси", True, "SOCKS5"), ("логин", False, "логин или пароль не приняты")], "auth", "")
gw.ext_ip = lambda n: ""
check("diag: плохой логин — warn", gw.diag(10)["verdict"] == "warn")
check("diag: модема нет — absent", gw.diag(7)["verdict"] == "absent")
try:
    gw.diag(77)
    check("diag: модема нет в таблице — честная ошибка", False)
except util.Fail as e:
    check("diag: модема нет в таблице — честная ошибка", "нет в таблице" in str(e))
gw_probe.chain, gw.web, gw.dns_probe, gw.ext_ip, gw_probe.quick = real_chain, real_web, real_dns, real_ext, real_quick

# ── apply ──────────────────────────────────────────────────────────────────
print("apply:")
fresh_root()
STATE = {"run": set(), "legacy": set(), "bad": {}, "fail": set()}
made, gone, fixed = [], [], []


def fake_snap(full=True):
    s = world_snap(sorted(STATE["run"]))
    s["netns"] |= {"ns_%d" % n for n in STATE["legacy"]}
    return s


def fake_create(r, up, o):
    if r["n"] in STATE["fail"]:
        raise util.Fail("модем %d, шаг «sing-box»: не стартует" % r["n"])
    made.append(r["n"])
    STATE["run"].add(r["n"])
    return r["host"]


def fake_remove(n, quiet=False):
    gone.append(n)
    STATE["run"].discard(n)


def fake_fix(n, r, bad, up):
    fixed.append(n)
    STATE["bad"].pop(n, None)
    return n != 66


gw.snap, gw.create, gw.remove, gw.fix = fake_snap, fake_create, fake_remove, fake_fix
gw.local = (lambda real_local: lambda n, r, s, inner=None: list(STATE["bad"].get(n, [])))(gw.local)
gw.sweep = lambda alive: None
gw.sh = World()
CFG = {"mode": "gw"}
res = gw.apply({5: row(5), 6: row(6), 201: row(201, 81)}, CFG)
check("пусто → созданы все", sorted(res["created"]) == [5, 6, 201] and not res["failed"], res)
check("unit-шаблоны записаны", os.path.exists(ROOT + "/etc/systemd/system/proxyveth-gw@.service"))
made.clear()
res = gw.apply({5: row(5), 6: row(6), 201: row(201, 81)}, CFG)
check("повтор — ничего не трогает", res == {"created": [], "recreated": [], "removed": [], "failed": {}} and not made, res)
res = gw.apply({5: row(5), 6: row(6, pw="new"), 201: row(201, 82)}, CFG)
check("строка поменялась — пересоздан", sorted(res["recreated"]) == [6, 201], res)
STATE["bad"] = {5: [("sb", "sing-box не работает")]}
made.clear()
res = gw.apply({5: row(5), 6: row(6, pw="new"), 201: row(201, 82)}, CFG)
check("лёг sing-box — починен без пересоздания", fixed == [5] and not made and not res["recreated"], res)
STATE["bad"] = {5: [("veth", "у pv5 нет 192.168.5.100")]}
gw.fix = lambda n, r, bad, up: False
res = gw.apply({5: row(5), 6: row(6, pw="new"), 201: row(201, 82)}, CFG)
check("пропал адрес — пересоздан", res["recreated"] == [5], res)
gw.fix = fake_fix
res = gw.apply({5: row(5), 201: row(201, 82)}, CFG)
check("пропал из таблицы — снят (один из трёх)", res["removed"] == [6] and 6 not in STATE["run"], res)
res = gw.apply({5: row(5), 201: row(201, 82, enabled=False)}, CFG)
check("выключен в таблице — снят", res["removed"] == [201], res)
STATE["run"] |= {7, 8, 9}
gone.clear()
res = gw.apply({5: row(5)}, CFG)
check("таблица сносит больше половины — ничего не снято", not gone and res.get("held") == [7, 8, 9], res)
res = gw.apply({5: row(5), 7: None, 8: None, 9: None}, CFG)
check("None — «не трогать»: не снято", not gone and not res.get("held"), res)
res = gw.apply({5: row(5)}, CFG, force=True)
check("…а с --force — снято", sorted(gone) == [7, 8, 9], res)
STATE["fail"] = {12}
res = gw.apply({5: row(5), 12: row(12)}, CFG)
retry = json.load(open(ROOT + gw.RETRY))
check("не поднялся — в failed, пауза 2 минуты", 12 in res["failed"] and retry["12"]["fails"] == 1
      and 110 < retry["12"]["next"] - time.time() <= 120, (res, retry))
made.clear()
res = gw.apply({5: row(5), 12: row(12)}, CFG)
check("во время паузы не дёргаем", "повтор после 1" in res["failed"].get(12, "") and not made, res)
res = gw.apply({5: row(5), 12: row(12)}, CFG, force=True)
retry = json.load(open(ROOT + gw.RETRY))
check("пауза растёт и модем не бросаем", retry["12"]["fails"] == 2 and retry["12"]["next"] - time.time() > 250, retry)
STATE["fail"] = set()
res = gw.apply({5: row(5), 12: row(12)}, CFG, force=True)
check("поднялся — счёт неудач сброшен", res["created"] == [12] and "12" not in json.load(open(ROOT + gw.RETRY)))
STATE["legacy"] = {14}
res = gw.apply({5: row(5), 12: row(12), 14: row(14)}, CFG)
check("модем ещё у proxyveth-virt — не трогаем", 14 not in made and "proxyveth-virt" in res["failed"][14], res)
STATE["legacy"] = set()
res = gw.apply({5: row(5), 12: row(12), 88: row(88)}, CFG)
check("октет сети сервера — отказ", "занята сетью" in res["failed"][88], res)
made.clear()
gw.down(12)
res = gw.apply({5: row(5), 12: row(12)}, CFG)
check("down — модем снят и таймер его не возвращает", 12 not in STATE["run"] and not made and 12 not in res["failed"])
gw.up(12)
check("up — вернулся", made == [12] and 12 not in gw.load_state()["down"])
try:
    gw.up(99)
    check("up несуществующего — честная ошибка", False)
except util.Fail as e:
    check("up несуществующего — честная ошибка", "нет в таблице" in str(e))
gw.down()
check("down all — снято всё, всё помечено", not STATE["run"] and set(gw.load_state()["down"]) >= {5, 12})
made.clear()
gw.up()
check("up all — подняты все из таблицы", sorted(made) == [5, 12])
gw.uplink = lambda: None
try:
    gw.apply({5: row(5)}, CFG)
    check("нет основного канала — честная ошибка", False)
except util.Fail as e:
    check("нет основного канала — честная ошибка", "основной канал" in str(e))
gw.uplink = lambda: ("192.168.88.1", "ens18")

# ── teardown, setup, doctor, маршруты ──────────────────────────────────────
print("teardown, setup, doctor:")
fresh_root()
gw.snap = real_snap
gw.remove = fake_remove
gw.sweep = lambda alive: gone.append(("sweep", tuple(sorted(alive))))
w = World().on(("ip", "-o", "link", "show", "type", "veth"), "7: pv5@if2: <UP>\n8: pv6@if2: <UP>\n9: mdm7@if2: <UP>\n")
w.on(("systemctl", "list-units"), "proxyveth-gw@9.service loaded failed failed x\n")
gw.sh = w
gone.clear()
gw.ensure_units()
gw.teardown()
check("teardown: свои модемы и хвосты, чужой veth — нет", sorted(x for x in gone if isinstance(x, int)) == [5, 6, 9]
      and ("sweep", ()) in gone, gone)
check("teardown: шаблоны юнитов убраны", not os.path.exists(ROOT + "/etc/systemd/system/proxyveth-gw@.service"))
real_base, real_install = net.ensure_base, singbox.install
net.ensure_base = lambda log=print: None
singbox.install = lambda *a, **k: False
gw.sh = World()
res = gw.setup({"mode": "gw"})
check("setup на чистом сервере — без переноса", res is None)
check("setup: каталоги 700, юниты на месте", oct(os.stat(ROOT + "/etc/proxyveth").st_mode & 0o777) == "0o700"
      and os.path.exists(ROOT + "/etc/systemd/system/proxyveth-hostfix@.service"))
res = gw.doctor()
check("doctor — список {name, ok, text}", res and all(set(x) == {"name", "ok", "text"} for x in res), res)
check("doctor видит отсутствие sing-box и основной канал", any(x["name"].startswith("sing-box") for x in res)
      and any(x["name"] == "основной канал" and x["ok"] for x in res))
w = World().on(("ip", "-4", "-o", "addr", "show", "dev", "pvtun0"), "9: pvtun0    inet 10.0.201.1/30 scope global\n")
gw.sh = w
os.makedirs(ROOT + "/etc/proxyveth/gw/201", exist_ok=True)
json.dump({"n": 201, "real": 81}, open(ROOT + "/etc/proxyveth/gw/201/spec.json", "w"))
check("_routes: default и веб-морда настоящего модема — в tun", gw.main(["_routes", "201"]) == 0
      and ("ip", "route", "replace", "default", "dev", "pvtun0") in w.calls
      and ("ip", "route", "replace", "192.168.81.1/32", "dev", "pvtun0") in w.calls)

# ── посредник Host ─────────────────────────────────────────────────────────
print("посредник Host:")
req = (b"GET /api/webserver/SesTokInfo HTTP/1.1\r\nHost: 192.168.64.1\r\nConnection: keep-alive\r\nUser-Agent: test")
out = gw_hostfix.rewrite_request(req, "192.168.101.1")
check("Host подменён, виртуальный убран", b"Host: 192.168.101.1" in out and b"Host: 192.168.64.1" not in out)
check("keep-alive погашен, остальное цело", b"Connection: close" in out and b"User-Agent: test" in out
      and b"keep-alive" not in out)
out = gw_hostfix.rewrite_request(b"GET http://192.168.64.1/html/index.html HTTP/1.1\r\nHost: 192.168.64.1", "192.168.101.1")
check("absolute-form → origin-form", out.split(b"\r\n")[0] == b"GET /html/index.html HTTP/1.1")
check("без Host — добавлен", b"Host: 192.168.101.1" in gw_hostfix.rewrite_request(b"GET / HTTP/1.0", "192.168.101.1"))
check("в ответе адрес правится целым", gw_hostfix.rewrite_response(
    b"Location: http://192.168.10.1/x\r\nX: 192.168.10.100", "192.168.10.1", "192.168.64.1")
      == b"Location: http://192.168.64.1/x\r\nX: 192.168.10.100")


async def hostfix_live():
    seen = []

    async def modem(reader, writer):
        head = await reader.readuntil(b"\r\n\r\n")
        seen.append(head)
        host = re.search(rb"(?im)^host:\s*(\S+)", head).group(1)
        if host != b"192.168.101.1":
            writer.write(b"HTTP/1.1 307 Temporary Redirect\r\nContent-Length: 0\r\n\r\n")
        else:
            writer.write(b"HTTP/1.1 200 OK\r\nLocation: http://192.168.101.1/html/index.html\r\n"
                         b"Content-Length: 18\r\n\r\nbody 192.168.101.1")
        await writer.drain()
        writer.close()

    msrv = await asyncio.start_server(modem, "127.0.0.1", 0)
    hf = gw_hostfix.Hostfix("192.168.64.1", "192.168.101.1", upstream=msrv.sockets[0].getsockname()[:2])
    hsrv = await asyncio.start_server(hf.handle, sock=gw_hostfix.listen("127.0.0.1", 0))
    r, wr = await asyncio.open_connection(*hsrv.sockets[0].getsockname()[:2])
    wr.write(b"GET / HTTP/1.1\r\nHost: 192.168.64.1\r\nConnection: keep-alive\r\n\r\n")
    await wr.drain()
    resp = await asyncio.wait_for(r.read(), 5)
    wr.close()
    for srv in (hsrv, msrv):
        srv.close()
    return resp, seen


resp, seen = asyncio.run(hostfix_live())
check("живой запрос: модем видит свой Host и отвечает 200", resp.startswith(b"HTTP/1.1 200")
      and b"Host: 192.168.101.1" in seen[0], (resp, seen))
check("Location — на виртуальный адрес, тело как есть",
      b"Location: http://192.168.64.1/html/index.html" in resp and resp.endswith(b"body 192.168.101.1"), resp)

# ── апстрим через SOCKS5 ───────────────────────────────────────────────────
print("апстрим (поддельные SOCKS5 и HiLink):")


def rx(c, n):
    b = b""
    while len(b) < n:
        x = c.recv(n - len(b))
        if not x:
            raise OSError("closed")
        b += x
    return b


def socks_server(user, pw, targets):
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(32)

    def conn(c):
        try:
            c.settimeout(5)
            rx(c, 3)
            c.sendall(b"\x05\x02")
            rx(c, 1)
            u = rx(c, rx(c, 1)[0]).decode()
            p = rx(c, rx(c, 1)[0]).decode()
            if (u, p) != (user, pw):
                c.sendall(b"\x01\x01")
                return
            c.sendall(b"\x01\x00")
            h = rx(c, 4)
            dst = socket.inet_ntoa(rx(c, 4)) if h[3] == 1 else rx(c, rx(c, 1)[0]).decode()
            rx(c, 2)
            fn = targets.get(dst)
            if not fn:
                c.sendall(b"\x05\x04\x00\x01" + b"\0" * 6)
                return
            c.sendall(b"\x05\x00\x00\x01" + b"\0" * 6)
            req = b""
            while b"\r\n\r\n" not in req:
                x = c.recv(4096)
                if not x:
                    break
                req += x
            c.sendall(fn(req))
        except OSError:
            pass
        finally:
            c.close()

    def loop():
        while True:
            try:
                c, _ = srv.accept()
            except OSError:
                return
            threading.Thread(target=conn, args=(c,), daemon=True).start()

    threading.Thread(target=loop, daemon=True).start()
    return srv.getsockname()[1]


def hilink(sim="1", conn="901"):
    def fn(req):
        if b"SesTokInfo" in req:
            return b"HTTP/1.1 200 OK\r\n\r\n<response><SesInfo>SessionID=abc</SesInfo><TokInfo>t</TokInfo></response>"
        return ("HTTP/1.1 200 OK\r\n\r\n<response><ConnectionStatus>%s</ConnectionStatus><SimStatus>%s</SimStatus>"
                "<CurrentNetworkType>19</CurrentNetworkType><SignalIcon>4</SignalIcon></response>" % (conn, sim)).encode()
    return fn


WORLD = {"connectivitycheck.gstatic.com": lambda r: b"HTTP/1.1 204 No Content\r\n\r\n",
         "api.ipify.org": lambda r: b"HTTP/1.1 200 OK\r\nContent-Length: 12\r\n\r\n94.25.229.40"}
good = socks_server("modem81", "pw81", dict(WORLD, **{"192.168.81.1": hilink()}))
nosim = socks_server("modem81", "pw81", dict(WORLD, **{"192.168.81.1": hilink(sim="255")}))
nomodem = socks_server("modem81", "pw81", dict(WORLD))
dead = socket.socket()
dead.bind(("127.0.0.1", 0))
dead_port = dead.getsockname()[1]
dead.close()


def prow(port, pw="pw81"):
    return {"n": 201, "real": 81, "host": "127.0.0.1", "port": port, "user": "modem81", "pw": pw}


steps, where, ip = gw_probe.chain(prow(good))
check("всё живо: прокси → логин → модем → SIM → интернет", [s[0] for s in steps] ==
      ["прокси", "логин", "модем", "SIM", "интернет"] and all(s[1] for s in steps) and where == "" and
      ip == "94.25.229.40", steps)
check("прокси не отвечает", gw_probe.chain(prow(dead_port))[1] == "proxy")
check("неверный пароль", gw_probe.chain(prow(good, pw="bad"))[1] == "auth")
check("до настоящего модема не достучаться", gw_probe.chain(prow(nomodem))[1] == "modem")
check("нет SIM", gw_probe.chain(prow(nosim))[1] == "sim")
check("quick: жив — пусто, мёртв — текст", gw_probe.quick(prow(good)) == "" and "не отвечает" in
      gw_probe.quick(prow(dead_port)))

# ── перенос с proxyveth-virt 3.4 ───────────────────────────────────────────
print("перенос с proxyveth-virt 3.4:")
env = gw_legacy.read_env('# c\nexport SHEET_CSV_URL="https://docs.google.com/x/pub?output=csv"\nAPI_URL=\nVETH_PREFIX=\'mdm\'\n')
check("env: кавычки, export, пустые", env == {"SHEET_CSV_URL": "https://docs.google.com/x/pub?output=csv",
                                             "API_URL": "", "VETH_PREFIX": "mdm"}, env)
check("источник: API_URL главнее", gw_legacy.source({"API_URL": "https://a", "SHEET_CSV_URL": "https://b"}) == "https://a")
check("источник: SHEET_CSV_URL", gw_legacy.source(env) == "https://docs.google.com/x/pub?output=csv")
check("источник: SHEET_ID + GID", gw_legacy.source({"SHEET_ID": "AbC", "SHEET_GID": "7"}) ==
      "https://docs.google.com/spreadsheets/d/AbC/export?format=csv&gid=7")
check("источник: нет — пусто", gw_legacy.source({}) == "")
OLD = {"modems": {
    "5": {"proxy_host": "188.134.95.184", "proxy_port": 1198, "login": "modem5", "password": "p:5", "real": 5, "enabled": True},
    "6": {"proxy_host": "188.134.95.184", "proxy_port": 1199, "login": "modem101", "password": "p6", "real": 101, "enabled": True},
    "8": {"proxy_host": "188.134.95.184", "proxy_port": 1200, "login": "m8", "password": "p8", "real": 8, "enabled": False},
    "9": {"proxy_host": "bad host", "proxy_port": 1, "login": "x", "password": "y"}},
    "last_sync": "2026-10-01T10:00:00"}
rows_, bad_ = gw_legacy.old_rows(OLD)
check("старые строки → формат gw", rows_[6] == {"n": 6, "real": 101, "host": "188.134.95.184", "port": 1199,
                                                "user": "modem101", "pw": "p6", "enabled": True} and rows_[5]["pw"] == "p:5")
check("кривая старая строка — названа, не взята", 9 not in rows_ and "n=9" in bad_[0], bad_)


def old_machine():
    fresh_root()
    for p, text in {"/etc/proxyveth/env": "SHEET_CSV_URL=https://docs.google.com/spreadsheets/d/e/X/pub?output=csv\n",
                    "/etc/proxyveth/config.json": json.dumps(OLD),
                    "/etc/proxyveth/singbox/modem_5.json": "{}", "/etc/proxyveth/singbox/modem_6.json": "{}",
                    "/etc/proxyveth/logs/watchdog.log": "old\n", "/etc/proxyveth/restart_counts.json": "{}",
                    "/etc/proxyveth/table.csv": "n,proxy\n",
                    "/etc/sysctl.d/99-proxyveth.conf": "net.ipv4.ip_forward = 1\n",
                    "/usr/local/bin/proxyveth.py": "#!/usr/bin/env python3\n",
                    "/usr/local/bin/proxyveth.py.bak.20260901-120000": "#!/usr/bin/env python3\n",
                    "/usr/local/bin/sing-box": "modlink",
                    "/etc/netns/ns_5/resolv.conf": "nameserver 1.1.1.1\n", "/etc/netns/ns_6/resolv.conf": "x\n",
                    "/run/proxyveth/ns_5.pid": "100\n", "/run/proxyveth/hostfix_6.pid": "101\n",
                    "/proc/100/cmdline": "/usr/local/bin/sing-box\0run\0-c\0/etc/proxyveth/singbox/modem_5.json\0",
                    "/proc/101/cmdline": "/usr/bin/python3\0/usr/local/bin/proxyveth.py\0_hostfix\0006\000101\0",
                    "/proc/102/cmdline": "/usr/local/bin/sing-box\0run\0-c\0/etc/modlink/sb.json\0",
                    "/proc/103/cmdline": "/usr/bin/python3\0/usr/local/bin/proxyveth.py\0_hostfix\00065\000101\0"}.items():
        os.makedirs(os.path.dirname(ROOT + p), exist_ok=True)
        open(ROOT + p, "w").write(text)
    os.makedirs(ROOT + "/etc/systemd/system", exist_ok=True)
    for u in gw_legacy.UNITS:
        open(ROOT + "/etc/systemd/system/" + u, "w").write(
            "[Service]\nExecStart=/usr/bin/python3 /usr/local/bin/proxyveth.py up all\n")
    os.symlink("/usr/local/bin/proxyveth.py", ROOT + "/usr/local/bin/proxyveth")
    w = World()
    w.on(("ip", "netns", "list"), lambda c: "".join("%s (id: %d)\n" % (x, i) for i, x in enumerate(sorted(NETNS))))
    w.on(("ip", "netns", "del"), lambda c: NETNS.discard(c[3]) or "")
    w.on(("ip", "-o", "link", "show", "type", "veth"), "7: mdm5@if2: <UP>\n8: mdm6@if2: <UP>\n9: veth0@if3: <UP>\n")
    w.on(("ip", "-4", "rule", "show"), "0:\tfrom all lookup local\n32763:\tfrom 192.168.6.100 lookup 1006\n"
                                        "32764:\tfrom 192.168.5.100 lookup 1005\n32762:\tfrom 192.168.7.100 lookup modem7\n"
                                        "32766:\tfrom all lookup main\n")
    w.on(("iptables", "-w", "5", "-t", "nat", "-S", "POSTROUTING"),
         "-P POSTROUTING ACCEPT\n-A POSTROUTING -s 192.168.5.0/24 -o ens18 -j MASQUERADE\n"
         "-A POSTROUTING -s 192.168.6.0/24 -o ens18 -j MASQUERADE\n-A POSTROUTING -s 192.168.7.0/24 -o ens18 -j MASQUERADE\n"
         "-A POSTROUTING -s 192.168.5.0/24 -o ens18 -m comment --comment \"pcs:hivelink\" -j MASQUERADE\n")
    return w


NETNS = set()
killed = []


def fake_kill(pid, sig):
    killed.append(pid)
    try:
        os.unlink(ROOT + "/proc/%d/cmdline" % pid)
    except OSError:
        pass


gw_legacy.kill = fake_kill
gw_legacy.sleep = lambda s: None
singbox.check = sbcheck
gw.create = fake_create
gw.remove = fake_remove
gw.ext_ip = lambda n: "94.25.229.%d" % n
STATE["run"], STATE["fail"] = set(), set()
NETNS = {"ns_5", "ns_6", "ns_99", "pv44"}
w = old_machine()
gw.sh = w
f = gw_legacy.find()
check("найдено старое: модемы, юниты, скрипт", f and f["ns"] == [5, 6] and len(f["units"]) == 4 and f["script"]
      and f["source"].startswith("https://docs.google.com"), f and (f["ns"], f["units"]))
check("ns_99 без признаков старого — не наш", 99 not in f["ns"])
made.clear()
cfg = {"mode": "gw"}
rep = gw.setup(cfg)
check("перенос: оба работающих модема — по-новому", rep and sorted(rep["moved"]) == [5, 6] and sorted(made) == [5, 6]
      and not rep["failed"], rep)
cj = json.load(open(ROOT + "/etc/proxyveth/config.json"))
check("источник — в config.json и в cfg; старые ключи убраны",
      cj == {"source": f["source"]} and cfg == {"mode": "gw", "source": f["source"]}, (cj, cfg))
check("старые процессы — только свои (sing-box 5, посредник 6), чужой sing-box жив",
      sorted(killed) == [100, 101], killed)
check("старые veth сняты, чужой veth0 — нет", ("ip", "link", "del", "mdm5") in w.calls and
      ("ip", "link", "del", "mdm6") in w.calls and not w.ran("ip", "link", "del", "veth0"))
check("старые правила — по их приоритету, чужие — нет",
      sorted(c for c in w.ran("ip", "rule", "del") if len(c) > 5) ==
      [("ip", "rule", "del", "priority", "32763", "from", "192.168.6.100", "lookup", "1006"),
       ("ip", "rule", "del", "priority", "32764", "from", "192.168.5.100", "lookup", "1005")], w.ran("ip", "rule", "del"))
dels = [c[8] for c in w.ran("iptables", "-w", "5", "-t", "nat", "-D")]
check("старый MASQUERADE — только своих модемов и без пометок", sorted(dels) == ["192.168.5.0/24", "192.168.6.0/24"]
      and all(len(c) == 13 for c in w.ran("iptables", "-w", "5", "-t", "nat", "-D")), w.ran("iptables", "-w", "5", "-t", "nat", "-D"))
check("старые юниты: стоп без убийства модемов, потом убраны",
      ("systemctl", "disable", "--now") + gw_legacy.UNITS in w.calls
      and not any(os.path.exists(ROOT + "/etc/systemd/system/" + u) for u in gw_legacy.UNITS)
      and not os.path.exists(ROOT + "/run/systemd/system/proxyveth.service.d"))
bk = ROOT + gw_legacy.BACKUP
check("копия старого: env, config.json, скрипт, юниты, логи",
      all(os.path.exists(bk + "/" + x) for x in ("env", "config.json", "proxyveth.py", "99-proxyveth.conf",
                                                 "units/proxyveth.service", "logs/watchdog.log",
                                                 "proxyveth.py.bak.20260901-120000"))
      and json.load(open(bk + "/config.json")) == OLD)
check("старые файлы убраны, чужие и новые — на месте",
      not any(os.path.exists(ROOT + p) for p in ("/etc/proxyveth/env", "/usr/local/bin/proxyveth.py",
                                                  "/etc/sysctl.d/99-proxyveth.conf", "/etc/proxyveth/singbox",
                                                  "/etc/proxyveth/logs", "/etc/netns/ns_5", "/run/proxyveth/ns_5.pid"))
      and open(ROOT + "/usr/local/bin/sing-box").read() == "modlink"
      and os.path.exists(ROOT + "/etc/proxyveth/table.csv"))
check("симлинк proxyveth — на новую команду", os.readlink(ROOT + "/usr/local/bin/proxyveth")
      == os.path.join(gw.CODE, "bin", "proxyveth"))
st = gw.load_state()
check("таблица до первого sync — из старого config.json", sorted(st["desired"]) == ["5", "6", "8"]
      and sorted(st["modems"]) == ["5", "6"], st)
check("после переноса следов нет, повтор — ничего не делает", gw_legacy.find() is None and gw.setup({}) is None)

STATE["run"], STATE["fail"], killed[:] = set(), {5}, []
NETNS = {"ns_5", "ns_6"}
w = old_machine()
gw.sh = w
try:
    gw.setup({"mode": "gw"})
    check("пробный модем не поднялся — откат", False)
except util.Fail as e:
    check("пробный модем не поднялся — откат и честная ошибка", "пробном модеме 5" in str(e), e)
check("откат: старые юниты включены обратно, ничего не удалено",
      ("systemctl", "enable", "--now", "--no-block", "proxyveth.service", "proxyveth-watchdog.service",
       "proxyveth-autosync.timer") in w.calls
      and all(os.path.exists(ROOT + "/etc/systemd/system/" + u) for u in gw_legacy.UNITS)
      and not os.path.exists(ROOT + "/run/systemd/system/proxyveth.service.d")
      and "modems" in json.load(open(ROOT + "/etc/proxyveth/config.json"))
      and os.path.exists(ROOT + "/usr/local/bin/proxyveth.py"), w.ran("systemctl"))
check("откат: модем 6 не тронут", "ns_6" in NETNS and 101 not in killed)

STATE["run"], STATE["fail"], killed[:] = set(), set(), []
NETNS = {"ns_5", "ns_6"}
gw.ext_ip = lambda n: "" if n in STATE["run"] else "94.25.229.5"
w = old_machine()
gw.sh = w
try:
    gw.setup({"mode": "gw"})
    check("по-новому нет интернета, а по-старому был — откат", False)
except util.Fail as e:
    check("по-новому нет интернета, а по-старому был — откат", "по-новому нет" in str(e), e)
gw.ext_ip = lambda n: "94.25.229.%d" % n

STATE["run"], killed[:] = set(), []
NETNS = {"ns_5", "ns_6"}
w = old_machine()
gw.sh = w
os.environ[gw_legacy.LIMIT] = "1"
rep = gw.setup({"mode": "gw"})
del os.environ[gw_legacy.LIMIT]
check("PROXYVETH_MIGRATE_LIMIT=1: только пробный, остальное ждёт", rep["moved"] == [5] and rep["left"] == [6]
      and "ns_6" in NETNS and os.path.exists(ROOT + "/usr/local/bin/proxyveth.py"), rep)
res = gw.apply({5: row(5), 6: row(6)}, {"mode": "gw"})
check("…sync не трогает модем, который ещё у старого", "proxyveth-virt" in res["failed"].get(6, ""), res)
rep = gw.setup({"mode": "gw"})
check("…повторный setup доносит остальное", rep["moved"] == [6] and gw_legacy.find() is None, rep)

NETNS = {"ns_5"}
w = old_machine()
os.unlink(ROOT + "/etc/proxyveth/config.json")
gw.sh = w
try:
    gw.setup({})
    check("модемы есть, списка нет — не переносим вслепую", False)
except util.Fail as e:
    check("модемы есть, списка нет — не переносим вслепую", "вслепую" in str(e) and "ns_5" in NETNS, e)

singbox.check, net.ensure_base, singbox.install = REAL_CHECK, real_base, real_install
shutil.rmtree(ROOT, ignore_errors=True)
print("\nитого: ok %d, fail %d" % (passed, failed))
sys.exit(1 if failed else 0)
