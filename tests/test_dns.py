#!/usr/bin/env python3
"""DNS-фикс (pcs/core/dns.py, §12) на временном корне файловой системы, без root.

    python3 tests/test_dns.py
"""
import os
import shutil
import stat
import sys
import tempfile
import types

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, os.pardir))
from pcs.core import dns  # noqa: E402
from pcs.core.util import Fail  # noqa: E402

passed = failed = 0


def check(name, cond, detail=""):
    global passed, failed
    if cond:
        passed += 1
        print("  ok   %s" % name)
    else:
        failed += 1
        print("  FAIL %s%s" % (name, ("\n       " + str(detail)) if detail else ""))


CLOUD = """# This file is generated from information provided by the datasource.
network:
    ethernets:
        eth0:
            dhcp4: true
            match:
                macaddress: bc:24:11:18:b7:8b
            set-name: eth0
    version: 2
"""
STATIC = """network:
  version: 2
  ethernets:
    ens18:
      addresses:
        - 192.168.88.7/24
      routes:
        - to: default
          via: 192.168.88.1
      nameservers:
        addresses: [1.1.1.1, 8.8.8.8]
"""


def mkroot(netplan=CLOUD, resolved=True, np=True, resolv="nameserver 192.168.88.1\n"):
    r = tempfile.mkdtemp()
    for p in (["usr/lib/systemd/systemd-resolved"] if resolved else []) + (["usr/sbin/netplan"] if np else []):
        os.makedirs(os.path.join(r, os.path.dirname(p)), exist_ok=True)
        open(os.path.join(r, p), "w").close()
    os.makedirs(os.path.join(r, "etc"), exist_ok=True)
    if netplan is not None:
        os.makedirs(os.path.join(r, "etc/netplan"))
        with open(os.path.join(r, "etc/netplan/50-cloud-init.yaml"), "w") as f:
            f.write(netplan)
    if resolv is not None:
        with open(os.path.join(r, "etc/resolv.conf"), "w") as f:
            f.write(resolv)
    return r


class World:
    """Подменённые команды: что звали и в каком порядке."""

    def __init__(self, links="", getent_ok=True, generate_rc=0):
        self.calls, self.links, self.getent_ok, self.generate_rc = [], links, getent_ok, generate_rc

    def run(self, *cmd, check=True, timeout=None, input=None):
        self.calls.append(cmd)
        out, rc = "", 0
        if cmd[:2] == ("resolvectl", "dns"):
            out = self.links
        elif cmd[:2] == ("resolvectl", "revert"):
            self.links = "\n".join(ln for ln in self.links.splitlines() if "(%s)" % cmd[2] not in ln)
        elif cmd[:2] == ("resolvectl", "status"):
            out = "Global\n  DNS Servers: 1.1.1.1 8.8.8.8\nLink 2 (eth0)\n  Current DNS Server: 192.168.88.1\n"
        elif cmd[0] == "getent":
            rc = 0 if self.getent_ok else 2
        elif cmd[:2] == ("netplan", "generate"):
            rc = self.generate_rc
        return types.SimpleNamespace(returncode=rc, stdout=out, stderr="ошибка разбора" if rc else "")

    def base(self, log=print):
        self.calls.append(("ensure_base",))

    def idx(self, cmd):
        return next((i for i, c in enumerate(self.calls) if c[:len(cmd)] == cmd), -1)

    def has(self, cmd):
        return self.idx(cmd) >= 0


def fix(root, w, **kw):
    return dns.fix(root=root, run=w.run, log=lambda m: None, ensure_base=w.base, sleep=lambda s: None, **kw)


def read(root, rel):
    with open(os.path.join(root, rel)) as f:
        return f.read()


print("разбор netplan без PyYAML:")
d = dns.mini_yaml(CLOUD)
check("cloud-init: eth0 с dhcp4", d["network"]["ethernets"]["eth0"]["dhcp4"] is True, d)
check("MAC с двоеточиями цел", d["network"]["ethernets"]["eth0"]["match"]["macaddress"] == "bc:24:11:18:b7:8b")
d = dns.mini_yaml(STATIC)
check("статика: списки не ломают разбор", d["network"]["ethernets"]["ens18"]["nameservers"]["addresses"]
      == ["1.1.1.1", "8.8.8.8"] and "dhcp4" not in d["network"]["ethernets"]["ens18"], d)
d = dns.mini_yaml("network:\n  ethernets:\n    eth1: {dhcp4: yes, dhcp6: no}\n  bridges:\n    \"br 0\":\n      dhcp6: on\n")
check("однострочное отображение и кавычки в имени", d["network"]["ethernets"]["eth1"] == {"dhcp4": True, "dhcp6": False}
      and d["network"]["bridges"]["br 0"]["dhcp6"] is True, d)

print("DNS из аргумента:")
check("пробелы и запятые", dns.parse_dns("1.1.1.1, 8.8.8.8 9.9.9.9") == ["1.1.1.1", "8.8.8.8", "9.9.9.9"])
try:
    dns.parse_dns("1.1.1.1 google.com")
    check("не адрес — честная ошибка", False)
except Fail as e:
    check("не адрес — честная ошибка", "google.com" in str(e), e)

print("сервер Ubuntu (resolved + netplan, DHCP):")
root = mkroot()
os.chmod(os.path.join(root, "etc/netplan/50-cloud-init.yaml"), 0o600)
cloud_before = read(root, "etc/netplan/50-cloud-init.yaml")
w = World(links="Global: 1.1.1.1 8.8.8.8\nLink 2 (eth0): 192.168.88.1\nLink 3 (eth1):\nLink 7 (eth5): 172.20.0.2\n"
                "Link 9 (eth201): 192.168.201.1\n")
res = fix(root, w)
conf = read(root, dns.RESOLVED)
check("resolved: наши серверы и все ключи §12.1", all(x in conf for x in (
    "DNS=1.1.1.1 8.8.8.8", "FallbackDNS=9.9.9.9 1.0.0.1", "Domains=~.", "DNSStubListener=yes", "DNSSEC=no",
    "DNSOverTLS=no", "Cache=yes")), conf)
np_path = os.path.join(root, dns.NETPLAN)
np = read(root, dns.NETPLAN)
check("99-pcs-dns.yaml: eth0 без DNS и доменов от DHCP", "eth0:" in np and "dhcp4-overrides:" in np
      and "use-dns: false" in np and "use-domains: false" in np and "dhcp6-overrides" not in np, np)
check("99-pcs-dns.yaml — права 600", stat.S_IMODE(os.stat(np_path).st_mode) == 0o600)
check("наш файл разбирается обратно", dns.mini_yaml(np)["network"]["ethernets"]["eth0"]["dhcp4-overrides"]["use-dns"] is False)
check("50-cloud-init.yaml не тронут", read(root, "etc/netplan/50-cloud-init.yaml") == cloud_before)
check("chattr -i снят с resolv.conf", w.has(("chattr", "-i")))
check("netplan generate, потом ensure_base, потом apply",
      0 <= w.idx(("netplan", "generate")) < w.idx(("ensure_base",)) < w.idx(("netplan", "apply")), w.calls)
link = os.path.join(root, "etc/resolv.conf")
check("resolv.conf → stub resolved", os.path.islink(link) and os.readlink(link) == dns.STUB)
check("resolved перезапущен, кэш сброшен", w.has(("systemctl", "restart", "systemd-resolved"))
      and w.has(("resolvectl", "flush-caches")))
check("чужой 172.20.0.2 снят: revert + renew", w.has(("resolvectl", "revert", "eth5"))
      and w.has(("networkctl", "renew", "eth5")), w.calls)
check("остаток DNS от DHCP на eth0 снят", w.has(("resolvectl", "revert", "eth0")))
check("DNS модема на его интерфейсе не трогаем", not w.has(("resolvectl", "revert", "eth201")))
check("pcs-dns.service при загрузке", "dns-fix --boot" in read(root, dns.UNIT)
      and w.has(("systemctl", "enable", "pcs-dns.service")))
check("проверка имён", res["ok"] and set(res["check"]) == set(dns.HOSTS) and all(res["check"].values()), res)
check("сводка: что поменялось", "/" + dns.NETPLAN in res["changed"] and res["netplan"] == "применён", res)

print("повторный запуск:")
w2 = World(links="Global: 1.1.1.1 8.8.8.8\nLink 2 (eth0):\n")
res = fix(root, w2)
check("netplan не применяется, если файл не менялся",
      not w2.has(("netplan", "apply")) and not w2.has(("netplan", "generate")) and not w2.has(("ensure_base",)), w2.calls)
check("сводка: без изменений", res["netplan"] == "без изменений" and res["changed"] == [], res)
check("DNS запомнены в drop-in", dns.current(root) == ["1.1.1.1", "8.8.8.8"])
w3 = World()
fix(root, w3, dns=["9.9.9.9"])
check("новые DNS — только resolved, netplan как был", "DNS=9.9.9.9\n" in read(root, dns.RESOLVED)
      and not w3.has(("netplan", "apply")))
w4 = World()
res = fix(root, w4, boot=True)
check("при загрузке берутся запомненные DNS, юнит не переписывается", res["dns"] == ["9.9.9.9"]
      and not w4.has(("systemctl", "enable", "pcs-dns.service")))
shutil.rmtree(root)

print("netplan не принял drop-in:")
root = mkroot()
w = World(generate_rc=1)
res = fix(root, w)
check("откат: файла нет, generate ещё раз, apply не было", not os.path.exists(os.path.join(root, dns.NETPLAN))
      and len([c for c in w.calls if c[:2] == ("netplan", "generate")]) == 2 and not w.has(("netplan", "apply"))
      and not w.has(("ensure_base",)) and res["netplan"] == "откат", w.calls)
shutil.rmtree(root)
root = mkroot()
fix(root, World())
with open(os.path.join(root, "etc/netplan/50-cloud-init.yaml"), "a") as f:
    f.write("        eth1:\n            dhcp6: true\n")
before = read(root, dns.NETPLAN)
w = World(generate_rc=1)
fix(root, w)
check("откат к прежнему drop-in, а не удаление", read(root, dns.NETPLAN) == before)
shutil.rmtree(root)

print("статика без DHCP, мосты и dhcp6:")
root = mkroot(netplan=STATIC)
w = World()
res = fix(root, w)
check("DHCP нет — drop-in не нужен, netplan не трогаем", not os.path.exists(os.path.join(root, dns.NETPLAN))
      and not w.has(("netplan", "apply")) and res["netplan"] == "без изменений", (res, w.calls))
shutil.rmtree(root)
root = mkroot(netplan="network:\n  version: 2\n  bridges:\n    br0:\n      interfaces: [eth0]\n      dhcp4: true\n"
                      "      dhcp6: true\n")
fix(root, World())
np = read(root, dns.NETPLAN)
check("мост с dhcp4 и dhcp6 — в своём разделе", "bridges:" in np and "br0:" in np and "dhcp6-overrides:" in np
      and "ethernets" not in np, np)
shutil.rmtree(root)

print("DNS не работает:")
root = mkroot()
w = World(getent_ok=False)
try:
    fix(root, w)
    check("ошибка с выводом resolvectl", False)
except Fail as e:
    check("ошибка с выводом resolvectl", "github.com" in str(e) and "resolvectl" in str(e)
          and "Current DNS" in str(e), e)
check("проверка повторяется, прежде чем сдаться", len([c for c in w.calls if c[0] == "getent"]) > len(dns.HOSTS))
shutil.rmtree(root)
root = mkroot()
w = World(links="Link 2 (eth0): 192.168.88.1\n")
w.run = (lambda orig: (lambda *c, **k: types.SimpleNamespace(returncode=0, stdout="Link 2 (eth0): 192.168.88.1\n",
                                                             stderr="") if c[:2] == ("resolvectl", "dns")
                      else orig(*c, **k)))(w.run)
try:
    fix(root, w)
    check("чужой DNS не снимается — ошибка", False)
except Fail as e:
    check("чужой DNS не снимается — ошибка", "чужой DNS" in str(e) and "eth0" in str(e), e)
shutil.rmtree(root)

print("хост Proxmox (без resolved и netplan):")
root = mkroot(netplan=None, resolved=False, np=False,
              resolv="search example.lan\nnameserver 192.168.88.1\noptions edns0\n")
w = World()
res = fix(root, w, dns=["1.1.1.1", "8.8.8.8"])
rc = read(root, "etc/resolv.conf")
check("resolv.conf — файл с нашими серверами", not os.path.islink(os.path.join(root, "etc/resolv.conf"))
      and "nameserver 1.1.1.1\nnameserver 8.8.8.8" in rc and "192.168.88.1" not in rc, rc)
check("search и options сохранены", "search example.lan" in rc and "options edns0" in rc)
check("старый resolv.conf — в .pcs-bak", "192.168.88.1" in read(root, "etc/resolv.conf.pcs-bak"))
check("resolved и netplan не трогаем", not w.has(("systemctl", "restart", "systemd-resolved"))
      and not w.has(("netplan", "generate")) and not os.path.exists(os.path.join(root, dns.RESOLVED)), w.calls)
check("сводка хоста", res["mode"] == "resolv.conf" and res["ok"], res)
check("DNS хоста запомнены в resolv.conf", dns.current(root) == ["1.1.1.1", "8.8.8.8"])
w = World()
fix(root, w)
check("повтор не плодит бэкапов и не меняет файл", read(root, "etc/resolv.conf") == rc)
shutil.rmtree(root)
root = mkroot(netplan=None, resolved=False, np=False, resolv=None)
os.symlink("/run/systemd/resolve/stub-resolv.conf", os.path.join(root, "etc/resolv.conf"))
fix(root, World())
check("битый симлинк на resolved заменён файлом", not os.path.islink(os.path.join(root, "etc/resolv.conf"))
      and "nameserver 1.1.1.1" in read(root, "etc/resolv.conf"))
shutil.rmtree(root)

print("\nитого: ok %d, fail %d" % (passed, failed))
sys.exit(1 if failed else 0)
