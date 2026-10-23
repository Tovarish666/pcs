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


def answers(**got):
    """Подменённая проверка серверов: сколько из PROBES ответили; не названные — все."""
    def probe(servers):
        probe.asked += list(servers)
        return {x: got.get(x.replace(".", "_"), dns.PROBES) for x in servers}
    probe.asked = []
    return probe


def fix(root, w, **kw):
    kw.setdefault("probe", answers())
    return dns.fix(root=root, run=w.run, log=lambda m: None, ensure_base=w.base, sleep=lambda s: None, **kw)


def watch(root, w, probe):
    return dns.watch(root=root, run=w.run, log=lambda m: None, sleep=lambda s: None, probe=probe)


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

print("проверка сервера запросами:")
pkt = dns.query_packet("github.com", 0x1234)
check("запрос A: id, RD, один вопрос, имя метками", pkt[:6] == b"\x12\x34\x01\x00\x00\x01"
      and b"\x06github\x03com\x00\x00\x01\x00\x01" in pkt, pkt)
check("ответ NOERROR на наш id — сервер жив", dns.answered(b"\x12\x34\x81\x80" + b"\0" * 8, 0x1234))
check("NXDOMAIN — тоже ответ", dns.answered(b"\x12\x34\x81\x83" + b"\0" * 8, 0x1234))
check("SERVFAIL, чужой id, запрос вместо ответа, обрывок — нет",
      not dns.answered(b"\x12\x34\x81\x82" + b"\0" * 8, 0x1234)
      and not dns.answered(b"\x43\x21\x81\x80" + b"\0" * 8, 0x1234)
      and not dns.answered(b"\x12\x34\x01\x00" + b"\0" * 8, 0x1234) and not dns.answered(b"\x12\x34", 0x1234))


class Sock:
    """UDP-сокет понарошку: отвечает по сценарию на последний отправленный запрос."""

    def __init__(self, replies):
        self.replies, self.sent, self.closed = list(replies), [], False

    def __call__(self, fam, kind):
        self.fam = fam
        return self

    def settimeout(self, t):
        self.timeout = t

    def sendto(self, data, addr):
        self.sent.append((data, addr))

    def close(self):
        self.closed = True

    def recvfrom(self, n):
        if not self.replies:
            raise OSError("timed out")
        qid = self.sent[-1][0][:2]
        kind = self.replies.pop(0)
        if kind == "ok":
            return qid + b"\x81\x80" + b"\0" * 8, None
        return b"\xff\xff\x81\x80" + b"\0" * 8, None     # чужой ответ (опоздавший)


sk = Sock(["ok"])
check("ответил — True, запрос на 53 порт, сокет закрыт", dns.ask("9.9.9.9", "github.com", sock=sk)
      and sk.sent[0][1] == ("9.9.9.9", 53) and sk.closed)
sk = Sock(["late", "ok"])
check("чужой ответ пропускается, ждём свой", dns.ask("9.9.9.9", "github.com", sock=sk))
check("тишина — False", not dns.ask("1.1.1.1", "github.com", sock=Sock([])))
import socket as _socket  # noqa: E402
sk = Sock(["ok"])
dns.ask("2606:4700:4700::1111", "github.com", sock=sk)
check("IPv6-сервер — IPv6-сокет", sk.fam == _socket.AF_INET6)
seen = []
score = dns.probe(["1.1.1.1", "9.9.9.9", "1.1.1.1"], n=6,
                  ask=lambda srv, name: seen.append((srv, name)) or (srv == "9.9.9.9" or name == "github.com"))
check("баллы: сколько из 6 ответили, повтор сервера — один раз", score == {"1.1.1.1": 2, "9.9.9.9": 6}, score)
check("имена по кругу из HOSTS", {n for _, n in seen} == set(dns.HOSTS), seen)

print("выбор серверов:")
use, sc = dns.choose(["1.1.1.1", "8.8.8.8"], probe=answers())
check("все отвечают — заданные как есть", use == ["1.1.1.1", "8.8.8.8"])
pr = answers(**{"1_1_1_1": 6})
use, sc = dns.choose(["1.1.1.1", "8.8.8.8"], probe=pr)
check("теряющий 1.1.1.1 (6/10) заменён первым рабочим из запаса", use == ["8.8.8.8", "9.9.9.9"], use)
check("запас проверяется, только когда нужен", dns.choose(["9.9.9.9"], probe=answers())[0] == ["9.9.9.9"])
pr = answers(**{"1_1_1_1": 3, "8_8_8_8": 4, "9_9_9_9": 0})
use, sc = dns.choose(["1.1.1.1", "8.8.8.8"], probe=pr)
check("оба теряют, 9.9.9.9 молчит — следующие из запаса", use == ["77.88.8.8", "1.0.0.1"], use)
nobody = lambda servers: {x: 0 for x in servers}  # noqa: E731
check("не ответил никто (сети нет) — заданные: хуже не делать",
      dns.choose(["1.1.1.1", "8.8.8.8"], probe=nobody)[0] == ["1.1.1.1", "8.8.8.8"])
use, _ = dns.choose(["1.1.1.1"], probe=lambda servers: {x: (dns.PROBES if x == "192.168.88.1" else 0) for x in servers},
                    extra=("192.168.88.1",))
check("молчит весь интернет-DNS — роутер последним запасным", use == ["192.168.88.1"], use)
use, _ = dns.choose(["1.1.1.1", "8.8.8.8"], probe=answers(**{"1_1_1_1": 6, "8_8_8_8": 5, "9_9_9_9": dns.PROBE_OK,
                                                             "77_88_8_8": dns.PROBE_OK}))
check("запасному хватает 9 из 10 (одна случайная потеря не отсеивает; ВМ 5000 осталась с одним роутером)",
      use == ["9.9.9.9", "77.88.8.8"], use)
pr = answers(**{"1_1_1_1": dns.PROBE_OK})
use, _ = dns.choose(["1.1.1.1", "8.8.8.8"], now=["9.9.9.9", "8.8.8.8"], probe=pr)
check("заменённый вернётся, только если ответил на всё (9/10 мало)", use == ["8.8.8.8", "9.9.9.9"], use)
use, _ = dns.choose(["1.1.1.1", "8.8.8.8"], now=["1.1.1.1", "8.8.8.8"], probe=pr)
check("стоящий сейчас 9/10 — остаётся (без дребезга)", use == ["1.1.1.1", "8.8.8.8"], use)
check("журнал: кто и сколько ответил", dns.lossy(["1.1.1.1", "8.8.8.8"], ["8.8.8.8", "9.9.9.9"],
                                                {"1.1.1.1": 6}) == "1.1.1.1 (6/10)")
gw = types.SimpleNamespace(returncode=0, stderr="", stdout="default via 192.168.4.1 dev eth12 metric 104\n"
                           "default via 192.168.88.1 dev eth0 proto dhcp src 192.168.88.21 metric 100\n")
check("шлюз — с меньшей метрикой (модемы mp.space тоже в main)", dns.gateway(lambda *c, **k: gw) == "192.168.88.1")
check("шлюза нет — None", dns.gateway(lambda *c, **k: types.SimpleNamespace(stdout="")) is None)

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
check("таймер перепроверки: раз в 10 минут dns-fix --watch", "dns-fix --watch" in read(root, dns.CHECK_UNIT)
      and "OnUnitActiveSec=10min" in read(root, dns.TIMER)
      and w.has(("systemctl", "enable", "--now", "pcs-dns-check.timer")))
check("все заданные отвечают — без строки PCS-WANT", dns.WANT not in conf and res["dns"] == res["want"])
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

print("теряющий запросы DNS (у провайдера 1.1.1.1 и 8.8.8.8 по UDP теряются):")
root = mkroot()
w = World()
logs = []
res = dns.fix(root=root, run=w.run, log=logs.append, ensure_base=w.base, sleep=lambda s: None,
              probe=answers(**{"1_1_1_1": 6, "8_8_8_8": 7}))
conf = read(root, dns.RESOLVED)
check("стоят рабочие из запаса", "DNS=9.9.9.9 77.88.8.8\n" in conf and res["dns"] == ["9.9.9.9", "77.88.8.8"], conf)
check("заданные запомнены (PCS-WANT) — current их и отдаёт", dns.current(root) == ["1.1.1.1", "8.8.8.8"]
      and dns.active(root) == ["9.9.9.9", "77.88.8.8"])
check("в журнале — кто терял и сколько ответил", any("1.1.1.1 (6/10), 8.8.8.8 (7/10)" in m for m in logs), logs)
check("сводка: заданные и баллы", res["want"] == ["1.1.1.1", "8.8.8.8"] and res["probe"]["1.1.1.1"] == 6, res)
w = World()
res = watch(root, w, answers(**{"1_1_1_1": dns.PROBES, "8_8_8_8": dns.PROBE_OK}))
check("таймер: 8.8.8.8 ответил 9/10 — мало, чтобы вернуть; 1.1.1.1 вернулся",
      res["dns"] == ["1.1.1.1", "9.9.9.9"] and "DNS=1.1.1.1 9.9.9.9\n" in read(root, dns.RESOLVED)
      and w.has(("systemctl", "restart", "systemd-resolved")), (res, read(root, dns.RESOLVED)))
w = World()
res = watch(root, w, answers(**{"8_8_8_8": dns.PROBE_OK}))
check("таймер: ничего не поменялось — resolved не трогаем (только смотрим шлюз)", res["changed"] == []
      and all(c[0] == "ip" for c in w.calls), (res, w.calls))
w = World()
watch(root, w, answers())
check("таймер: всё отвечает — заданные на месте, PCS-WANT убран", "DNS=1.1.1.1 8.8.8.8\n" in read(root, dns.RESOLVED)
      and dns.WANT not in read(root, dns.RESOLVED))
w = World()
res = fix(root, w, dns=["8.8.8.8"], probe=answers(**{"8_8_8_8": dns.PROBE_OK}))
check("новые DNS руками: 9/10 хватает (порог для стоящих)", res["dns"] == ["8.8.8.8"], res)
shutil.rmtree(root)
root = mkroot()
w = World()
res = watch(root, w, answers(**{"1_1_1_1": 0}))
check("таймер там, где pcs dns не запускали, — не наше дело", res["mode"] is None and not w.calls
      and not os.path.exists(os.path.join(root, dns.RESOLVED)), res)
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
res = watch(root, w, answers(**{"1_1_1_1": 2}))
rc2 = read(root, "etc/resolv.conf")
check("хост: теряющий заменён прямо в resolv.conf, search и options на месте",
      "nameserver 8.8.8.8\nnameserver 9.9.9.9" in rc2 and dns.WANT + " 1.1.1.1 8.8.8.8" in rc2
      and "search example.lan" in rc2 and "options edns0" in rc2 and not w.has(("systemctl", "restart", "systemd-resolved")), rc2)
check("хост: заданные помнятся", dns.current(root) == ["1.1.1.1", "8.8.8.8"] and dns.active(root) == ["8.8.8.8", "9.9.9.9"])
watch(root, World(), answers())
check("хост: заработал — вернулся тот же файл", read(root, "etc/resolv.conf") == rc)
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
