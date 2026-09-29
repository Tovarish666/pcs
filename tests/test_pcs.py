#!/usr/bin/env python3
"""Проверки pcs (хост Proxmox) без Proxmox и без root.

    python3 tests/test_pcs.py
"""
import importlib.machinery
import importlib.util
import os
import subprocess
import sys
import types

HERE = os.path.dirname(os.path.abspath(__file__))
PCS = os.path.join(HERE, os.pardir, "pcs")
loader = importlib.machinery.SourceFileLoader("pcs_cli", PCS)
spec = importlib.util.spec_from_loader("pcs_cli", loader)
pcs = importlib.util.module_from_spec(spec)
loader.exec_module(pcs)

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
real_sh = pcs.sh
pcs.sh = lambda *c, **k: types.SimpleNamespace(stdout="$6$salt$HASH'q\n", returncode=0) \
    if c[:2] == ("openssl", "passwd") else real_sh(*c, **k)
ud = pcs.user_data("mp1000", "секрет", "ssh-ed25519 AAAA pcs@host", ["1.1.1.1", "8.8.8.8"])
pcs.sh = real_sh
check("это cloud-config", ud.startswith("#cloud-config"))
check("ключ PCS положен root", "ssh-ed25519 AAAA pcs@host" in ud)
check("пароль уходит хэшем, кавычки экранированы", "'$6$salt$HASH''q'" in ud and "секрет" not in ud, ud)
check("модули USB-гаджета (linux-image-extra-virtual)", "linux-image-extra-virtual" in ud)
check("заголовки ядра для dkms", "linux-headers-virtual" in ud and "- dkms" in ud)
check("DHCP-клиент для своей стороны сервера", "- udhcpc" in ud)
check("sshd: root по паролю, файл 10-, а не 99-", "10-pcs.conf" in ud and "PermitRootLogin yes" in ud)
check("DNS задан через resolved", "DNS=1.1.1.1 8.8.8.8" in ud)
check("авто-обновления выключены", "unattended-upgrades" in ud)
check("метка конца cloud-init", "/var/lib/pcs-cloud-init-done" in ud)

print("адрес новой ВМ до guest agent:")
check("IPv6 link-local из MAC — как у живой ВМ на стенде",
      pcs.link_local("bc:24:11:18:b7:8b") == "fe80::be24:11ff:fe18:b78b", pcs.link_local("bc:24:11:18:b7:8b"))

print("номера ВМ:")
pcs.used_ids = lambda: {100, 1000, 2000}
check("свободная тысяча", pcs.next_id() == 3000, pcs.next_id())
pcs.used_ids = lambda: set()
check("на пустом хосте — 1000", pcs.next_id() == 1000)

print("командная строка:")
r = subprocess.run([sys.executable, PCS, "version"], capture_output=True, text=True)
check("pcs version", r.returncode == 0 and r.stdout.strip() == pcs.VERSION, r.stdout + r.stderr)
r = subprocess.run([sys.executable, PCS, "server", "create", "--help"], capture_output=True, text=True)
check("create --help: таблица и mp.space одной командой", "--sheet" in r.stdout and "--mpspace" in r.stdout, r.stdout)
r = subprocess.run([sys.executable, PCS], capture_output=True, text=True, stdin=subprocess.DEVNULL)
check("без терминала — справка, а не меню", "pcs server create" in r.stdout, r.stdout[:200])
check("в меню есть всё из концепции", all(any(w in t for t, _ in pcs.MENU) for w in
      ("Создать сервер", "таблиц", "сменить IP", "mobileproxy.space", "DNS", "пароль", "Telegram")))

print("\nитого: ok %d, fail %d" % (passed, failed))
sys.exit(1 if failed else 0)
