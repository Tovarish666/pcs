#!/usr/bin/env python3
"""Проверки общего ядра pcs.core без root и без ВМ.

    python3 tests/test_core.py
"""
import io
import json
import os
import sys
import types
from contextlib import redirect_stdout

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, os.pardir))
sys.path.insert(0, HERE)
from pcs.core import net, table, util  # noqa: E402
from singbox_check import sb_check  # noqa: E402

passed = failed = 0


def check(name, cond, detail=""):
    global passed, failed
    if cond:
        passed += 1
        print("  ok   %s" % name)
    else:
        failed += 1
        print("  FAIL %s%s" % (name, ("\n       " + str(detail)) if detail else ""))


print("таблица модемов:")
good = "188.134.95.184:1198"
csv = "﻿N , Real , Proxy ,enabled\n" + "\n".join([
    "201,81,%s:modem81:pw81,1" % good,
    "202,82,%s:modem82:pw82" % good,
    "202,83,%s:modem83:pw83" % good,
    "203,192.168.84.100,%s:modem84:pw84" % good,
    "204,85,%s:modem85:pw85,0" % good,
    "1,86,%s:modem86:pw86" % good,
    "154,87,%s:modem87:pw87" % good,
    "88,88,%s:modem88:pw88" % good,
    "210,95,%s:modem95:p:a:s:s" % good,
    "212,,%s:modem98:pw98" % good,
]) + "\n"
d, dis, inv, probs = table.lint(csv, taken_octets={88})
check("годные строки", sorted(d) == [203, 210, 212], sorted(d))
check("выключенная — не брак", dis == {204}, dis)
check("брак назван", inv == {1, 88, 154, 201, 202}, sorted(inv))
check("двоеточия в пароле целы", d[210]["pw"] == "p:a:s:s")
check("real адресом → октет", d[203]["real"] == 84)
d2, _, inv2, _ = table.lint(csv, taken_octets={88}, mp_tables=False)
check("режим gw без mp.space: N и N+200, 153–155 можно", {1, 154, 201} <= set(d2), (sorted(d2), sorted(inv2)))
check("ссылка из адресной строки → CSV",
      table.normalize_url("https://docs.google.com/spreadsheets/d/AbC_1/edit#gid=456")
      == "https://docs.google.com/spreadsheets/d/AbC_1/export?format=csv&gid=456")

print("источник и локальная копия:")
import tempfile  # noqa: E402
tmp = tempfile.mkdtemp()
src, copy = os.path.join(tmp, "src.csv"), os.path.join(tmp, "copy", "table.csv")
open(src, "w").write(csv)
text, stale = table.fetch(src, copy, log=lambda m: None)
check("удачное чтение кладёт копию", not stale and open(copy).read() == csv.lstrip("﻿"))
os.unlink(src)
said = []
text, stale = table.fetch(src, copy, log=said.append)
check("источник пропал — берётся копия, с предупреждением", stale and text == csv.lstrip("﻿") and said, said)
os.unlink(copy)
try:
    table.fetch(src, copy, log=lambda m: None)
    check("нет ни источника, ни копии — честная ошибка", False)
except util.Fail:
    check("нет ни источника, ни копии — честная ошибка", True)

print("сеть:")
check("usb: таблицы как у mp.space", net.table("usb", 205) == 105 and net.prio("usb", 205) is None)
check("gw и hivelink — свои непересекающиеся диапазоны",
      net.table("gw", 5) == 1005 and net.prio("gw", 5) == 30005
      and net.table("hivelink", 5) == 2005 and net.prio("hivelink", 5) == 31005)
check("все приоритеты — до правила адресов прокси и до main",
      net.prio("hivelink", 254) < net.PIN_PRIO < 32766)
world = {
    ("ip", "-o", "link"):
        "2: eth0: <UP> mtu 1500\\    link/ether bc:24:11:18:b7:8b brd ff:ff:ff:ff:ff:ff\n"
        "5: eth1: <UP> mtu 1500\\    link/ether 0c:5b:8f:27:9a:64 brd ff:ff:ff:ff:ff:ff\n",
    ("ip", "-4", "route", "show", "default", "table", "main"):
        "default via 192.168.201.1 dev eth1\ndefault via 192.168.88.1 dev eth0 proto dhcp src 192.168.88.7 metric 100\n",
    ("ip", "-4", "route", "show", "dev", "eth0", "scope", "link", "table", "main"):
        "192.168.88.0/24 proto kernel src 192.168.88.7 metric 100\n",
    ("ip", "-4", "rule"): "0:\tfrom all lookup local\n32765:\tfrom all to 10.9.9.9 lookup 90\n",
}
ran, real_sh = [], net.sh
net.sh = lambda *c, **k: (ran.append(c), types.SimpleNamespace(stdout=world.get(c, ""), returncode=0))[1]
try:
    check("основной канал — eth0, хотя у модема метрика лучше", net.uplink_route() == ("192.168.88.1", "eth0"))
    net.pin_proxies(["188.134.95.184"], log=lambda m: None)
finally:
    net.sh = real_sh
check("адрес прокси закреплён за основным каналом",
      ("ip", "rule", "add", "priority", "32765", "to", "188.134.95.184", "lookup", "90") in ran, ran)
check("ушедшая прокси — правило снято",
      ("ip", "rule", "del", "priority", "32765", "to", "10.9.9.9", "lookup", "90") in ran)

print("вывод команд:")


def fn_ok(a):
    return 0, {"x": 1}


def fn_fail(a):
    raise util.Fail("сломалось")


buf = io.StringIO()
with redirect_stdout(buf):
    rc = util.run_cli(fn_ok, None, as_json=True)
check("--json: конверт ok/data, код 0", rc == 0 and json.loads(buf.getvalue()) == {"ok": True, "data": {"x": 1}})
buf = io.StringIO()
with redirect_stdout(buf):
    rc = util.run_cli(fn_fail, None, as_json=True)
check("--json: ошибка одной строкой, код 1", rc == 1 and json.loads(buf.getvalue()) == {"ok": False, "error": "сломалось"})
util.QUIET[0] = False

print("sing-box %s:" % __import__("pcs.core.singbox", fromlist=["x"]).VERSION)
new = {"log": {"level": "warn"},
       "dns": {"servers": [{"type": "tcp", "tag": "m", "server": "192.168.81.1", "detour": "proxy"}], "strategy": "ipv4_only"},
       "inbounds": [{"type": "direct", "tag": "dns-in", "listen": "127.0.0.1", "listen_port": 5353}],
       "outbounds": [{"type": "socks", "tag": "proxy", "server": "127.0.0.1", "server_port": 1080, "username": "u", "password": "p"}],
       "route": {"rules": [{"inbound": ["dns-in"], "action": "hijack-dns"}], "final": "proxy", "auto_detect_interface": False}}
ok, msg = sb_check(new)
if ok is not None:
    check("конфиг в новом формате принимается", ok, msg)
    old = json.loads(json.dumps(new))
    old["dns"]["servers"] = [{"tag": "m", "address": "tcp://192.168.81.1", "detour": "proxy"}]
    ok2, msg2 = sb_check(old)
    check("старый формат DNS-серверов — отказ (так и надо)", ok2 is False, msg2)

print("\nитого: ok %d, fail %d" % (passed, failed))
sys.exit(1 if failed else 0)
