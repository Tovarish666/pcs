#!/usr/bin/env python3
"""Проверки pcs (хост Proxmox) без Proxmox и без root: cloud-init, номера ВМ, командная строка.

    python3 tests/test_pcs.py
"""
import os
import subprocess
import sys
import types

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.join(HERE, os.pardir)
sys.path.insert(0, ROOT)
from pcs import VERSION  # noqa: E402
from pcs.hub import cli, pve, servers  # noqa: E402
from pcs.core.util import Fail  # noqa: E402

PCS = os.path.join(ROOT, "bin", "pcs")
passed = failed = 0


def check(name, cond, detail=""):
    global passed, failed
    if cond:
        passed += 1
        print("  ok   %s" % name)
    else:
        failed += 1
        print("  FAIL %s%s" % (name, ("\n       " + str(detail)) if detail else ""))


print("cloud-init сервера:")
real_sh = pve.sh
pve.sh = lambda *c, **k: types.SimpleNamespace(stdout="$6$salt$HASH'q\n", returncode=0) \
    if c[:2] == ("openssl", "passwd") else real_sh(*c, **k)
ud = pve.user_data("pcs1000", "секрет", "ssh-ed25519 AAAA pcs@host", ["1.1.1.1", "8.8.8.8"], "usb")
gw = pve.user_data("pcs3000", "секрет", "ssh-ed25519 AAAA pcs@host", ["9.9.9.9"], "gw")
pve.sh = real_sh
check("это cloud-config", ud.startswith("#cloud-config"))
check("ключ хаба положен root", "ssh-ed25519 AAAA pcs@host" in ud)
check("пароль уходит хэшем, кавычки экранированы", "'$6$salt$HASH''q'" in ud and "секрет" not in ud, ud)
check("usb: модули USB-гаджета (linux-image-extra-virtual)", "linux-image-extra-virtual" in ud)
check("usb: заголовки ядра для dkms", "linux-headers-virtual" in ud and "- dkms" in ud)
check("usb: DHCP-клиент для своей стороны сервера", "- udhcpc" in ud)
check("gw: без лишнего ядра и dkms", "linux-image-extra-virtual" not in gw and "dkms" not in gw and "- iptables" in gw)
check("sshd: root по паролю, файл 10-, а не 99-", "10-pcs.conf" in ud and "PermitRootLogin yes" in ud)
check("DNS — тот же drop-in, что ставит pcs dns", "DNS=1.1.1.1 8.8.8.8" in ud and "DNSStubListener=yes" in ud
      and "DNS=9.9.9.9" in gw)
check("авто-обновления выключены", "unattended-upgrades" in ud)
check("метка конца cloud-init", "/var/lib/pcs-cloud-init-done" in ud)
try:
    import yaml  # noqa: F401
    doc = yaml.safe_load(ud)
    check("cloud-init разбирается YAML", doc["write_files"][1]["content"].startswith("# PCS:")
          and "qemu-guest-agent" in doc["packages"], doc)
except ImportError:
    from pcs.core import dns
    doc = dns.mini_yaml(ud)
    check("cloud-init разбирается (без PyYAML — упрощённо)", doc.get("hostname") == "pcs1000", doc)

print("адрес новой ВМ до guest agent:")
check("IPv6 link-local из MAC — как у живой ВМ на стенде",
      pve.link_local("bc:24:11:18:b7:8b") == "fe80::be24:11ff:fe18:b78b", pve.link_local("bc:24:11:18:b7:8b"))

print("номера ВМ:")
real_used = pve.used_ids
pve.used_ids = lambda: {100, 1000, 2000}
check("свободная тысяча", pve.next_id() == 3000, pve.next_id())
pve.used_ids = lambda: set()
check("на пустом хосте — 1000", pve.next_id() == 1000)
pve.used_ids = real_used

print("параметры создания:")
p = servers.normalize({"name": "pcs3000", "cores": "2", "ram_gb": 8, "disk_gb": 60, "mode": "gw",
                       "sheet": "https://docs.google.com/spreadsheets/d/x/edit", "mpspace": "true", "dns": "9.9.9.9 1.1.1.1"})
check("числа, режим, mp.space, DNS", p["cores"] == 2 and p["ram_gb"] == 8 and p["mode"] == "gw" and p["mpspace"] is True
      and p["dns"] == ["9.9.9.9", "1.1.1.1"], p)
check("по умолчанию — usb, 4 ядра, 4 ГБ, 40 ГБ, DHCP", servers.normalize({})["mode"] == "usb"
      and servers.normalize({})["ip"] == "" and servers.normalize({})["disk_gb"] == 40)
for bad, why in (({"mode": "virt"}, "режим"), ({"ip": "192.168.1.60"}, "маск"), ({"name": "мой сервер"}, "имя"),
                 ({"cores": 0}, "cores"), ({"sheet": "javascript:alert(1)"}, "таблица"), ({"dns": "8.8.8.8 x"}, "DNS"),
                 ({"password": "a\nb"}, "перевод строки")):
    try:
        servers.normalize(bad)
        check("брак отсекается до действий: %s" % why, False)
    except Fail as e:
        check("брак отсекается до действий: %s" % why, True, e)
check("--ram 4096 (PCS 4, мегабайты) → 4 ГБ, --ram 8 → 8 ГБ", cli.ram_gb(4096) == 4 and cli.ram_gb(8) == 8)

print("разбор аргументов:")
a = cli.parser().parse_args(["server", "create", "--mode", "gw", "--sheet", "https://x", "--mpspace", "--id", "3000"])
check("server create --mode gw --sheet --mpspace", (a.cmd, a.scmd, a.mode, a.sheet, a.mpspace, a.id)
      == ("server", "create", "gw", "https://x", True, 3000), a)
a = cli.parser().parse_args(["dns", "--host", "--dns", "1.1.1.1 8.8.8.8", "--json"])
check("dns --host --dns --json", a.host and a.dns == "1.1.1.1 8.8.8.8" and a.json)
a = cli.parser().parse_args(["update", "--server", "2000", "--server", "3000"])
check("update --server несколько раз", a.server == ["2000", "3000"] and not a.all and not a.host)
try:
    cli.parser().parse_args(["update", "--host", "--all"])
    check("update --host и --all вместе — ошибка", False)
except Fail:
    check("update --host и --all вместе — ошибка", True)
a = cli.parser().parse_args(["key", "--server", "2000", "--add", "ssh-ed25519 AAAA me@laptop"])
check("key --add PUBKEY", a.add == "ssh-ed25519 AAAA me@laptop" and a.server == "2000")
rest, srv, host = cli.split_target(["status", "--server", "2000", "--wan", "--json"])
check("proxyveth: --server где угодно, остальное как есть", rest == ["status", "--wan", "--json"] and srv == "2000")
rest, srv, host = cli.split_target(["install", "--host"])
check("hivelink --host", rest == ["install"] and host and srv is None)
rest, srv, host = cli.split_target(["source", "https://x?a=1", "--server=200"])
check("--server=ID", rest == ["source", "https://x?a=1"] and srv == "200")
rest, _, host = cli.split_target(["status", "--host"])
check("--host — всегда флаг хаба (у proxyveth это ошибка, не выбранный сервер)", rest == ["status"] and host)

print("командная строка:")


def pcs(*args, stdin=subprocess.DEVNULL):
    return subprocess.run([sys.executable, PCS] + list(args), capture_output=True, text=True, stdin=stdin)


r = pcs("version")
check("pcs version", r.returncode == 0 and r.stdout.strip() == VERSION, r.stdout + r.stderr)
r = pcs("version", "--json")
check("pcs version --json — конверт", r.stdout.strip() == '{"ok": true, "data": "%s"}' % VERSION, r.stdout)
r = pcs("server", "create", "--help")
check("create --help: режим, таблица и mp.space одной командой",
      all(x in r.stdout for x in ("--mode", "--sheet", "--mpspace")), r.stdout)
r = pcs()
check("без терминала — справка, а не меню", "pcs server create" in r.stdout and r.returncode == 0, r.stdout[:200])
check("в справке — команды §6", all(x in r.stdout for x in (
    "pcs proxyveth", "pcs modlink", "pcs hivelink", "--host", "pcs web on|off|status|passwd", "pcs update",
    "pcs key", "--rotate", "pcs mpspace")), r.stdout)
check("старых имён нет", "vmodem" not in r.stdout and "pcs m " not in r.stdout)
r = pcs("server", "bogus")
check("неверная команда — одна строка ✗, код 1, без трассы", r.returncode == 1 and r.stderr.count("\n") == 1
      and "✗" in r.stderr and "Traceback" not in r.stderr, r.stderr)
r = pcs("m", "status")
check("pcs m больше нет", r.returncode == 1 and "Traceback" not in r.stderr, r.stderr)
if os.geteuid() != 0:
    r = pcs("status", "--json")
    check("--json: ошибка — конвертом", r.returncode == 1 and r.stdout.strip() == '{"ok": false, "error": "нужен root"}',
          r.stdout + r.stderr)
if os.geteuid() != 0:
    r = subprocess.run(["bash", os.path.join(ROOT, "install.sh")], capture_output=True, text=True, stdin=subprocess.DEVNULL)
    check("install.sh без root — отказ одной строкой", r.returncode == 1 and "нужен root" in r.stderr, r.stderr)
with open(os.path.join(ROOT, "install.sh")) as f:
    inst = f.read()
check("install.sh: только Proxmox, VPS — понятный отказ", "qm" in inst and "pvesm" in inst and "VPS" in inst
      and "vmodem" not in inst)
check("install.sh: панель (PCS_WEB_USER/PCS_WEB_PASS) и hivelink на хост",
      "web passwd" in inst and "web on" in inst and "PCS_WEB_PASS" in inst and '/bin/hivelink" install' in inst)
r = subprocess.run([sys.executable, os.path.join(ROOT, "bin", "pcs-node"), "version"], capture_output=True, text=True)
check("pcs-node version", r.returncode == 0 and r.stdout.strip() == VERSION, r.stdout + r.stderr)

print("\nитого: ok %d, fail %d" % (passed, failed))
sys.exit(1 if failed else 0)
