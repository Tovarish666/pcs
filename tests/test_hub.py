#!/usr/bin/env python3
"""Хаб без Proxmox, без root и без серверов: записи, меню, API на подменённом ssh, задания.

    python3 tests/test_hub.py
"""
import io
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import types
from contextlib import redirect_stderr, redirect_stdout

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.join(HERE, os.pardir)
sys.path.insert(0, ROOT)
from pcs import VERSION  # noqa: E402
from pcs.core.util import Fail  # noqa: E402
from pcs.hub import api, cli, jobs, menu, pve, remote, servers, store, ui, web  # noqa: E402

passed = failed = 0


def check(name, cond, detail=""):
    global passed, failed
    if cond:
        passed += 1
        print("  ok   %s" % name)
    else:
        failed += 1
        print("  FAIL %s%s" % (name, ("\n       " + str(detail)) if detail else ""))


TMP = tempfile.mkdtemp()
store.ETC = os.path.join(TMP, "etc")
remote.RUN = os.path.join(TMP, "run")
jobs.DIR = os.path.join(TMP, "jobs")
ui.LOG = os.path.join(TMP, "pcs.log")
os.makedirs(os.path.join(store.ETC, "servers"))
os.makedirs(os.path.join(store.ETC, "ssh"))
# записи, как их оставил PCS 4 на стенде
for sid, ip in ((1000, "192.168.88.5"), (2000, "192.168.88.7"), (200, "192.168.88.9"), (20000, "192.168.88.20")):
    with open(os.path.join(store.ETC, "servers", "%d.json" % sid), "w") as f:
        json.dump({"id": sid, "name": "pcs%d" % sid if sid != 200 else "old200", "ip": ip, "password": "pw%d" % sid,
                   "net": "static", "created": "2026-09-30 10:00:00", "pcs": "4.0.0", "agent": "4.1.0"}, f)
with open(os.path.join(store.ETC, "servers", "junk.json.tmp"), "w") as f:
    f.write("{")
with open(os.path.join(store.ETC, "active"), "w") as f:
    f.write("2000\n")
HUBPUB = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIOWUo9IQjZ7yjBvOYxN8E8Qd0e7oBo2vGQ0sYJ0J1pqV pcs@pve"
with open(store.key_path(), "w") as f:
    f.write("fake")
with open(store.key_path() + ".pub", "w") as f:
    f.write(HUBPUB + "\n")

print("записи серверов (формат PCS 4):")
ids = [s["id"] for s in store.all_servers()]
check("все четыре, по номерам, мусор не мешает", ids == [200, 1000, 2000, 20000], ids)
check("выбранный — 2000", store.active() == "2000")
check("запись PCS 4 читается как есть", store.load(1000)["password"] == "pw1000" and store.load("1000")["ip"] == "192.168.88.5")
check("цель без --server — выбранный", store.target()["id"] == 2000)
check("--server важнее выбранного", store.target("200")["id"] == 200)
check("--host — хост", store.target(None, host=True) == store.HOST and store.norm_id("Хост") == store.HOST)
for bad in ("20 00", "abc", "../1"):
    try:
        store.target(bad)
        check("брак номера — отказ: %r" % bad, False)
    except Fail:
        check("брак номера — отказ: %r" % bad, True)
try:
    store.load(3000)
    check("неизвестный сервер — подсказка adopt", False)
except Fail as e:
    check("неизвестный сервер — подсказка adopt", "pcs server adopt 3000" in str(e), e)

# ── подменённый мир: Proxmox и SSH ────────────────────────────────────────
INFO = {"version": VERSION, "hostname": "pcs2000", "ip": "192.168.88.7", "uptime": 100, "kernel": "6.8.0",
        "cpu": {"cores": 4, "load": [0.1, 0.1, 0.1]}, "ram": {"total": 1, "used": 0}, "disk": {"total": 1, "used": 0},
        "proxyveth": {"installed": True, "mode": "usb", "modems": {"ok": 38, "warn": 1, "broken": 1, "total": 40}},
        "mpspace": {"installed": True, "ok": True, "units": {"mproxy": "active", "nodejs-server": "active"}, "port": 1800},
        "modlink": {"installed": True}, "hivelink": {"installed": False}, "dns": ["1.1.1.1", "8.8.8.8"]}


class Net:
    def __init__(self):
        self.cmds = []
        self.down = {"192.168.88.5"}
        self.old = {"192.168.88.20"}

    def call(self, s, cmd, input=None, timeout=None, key=None):
        self.cmds.append((s["id"], cmd, input))
        if s["ip"] in self.down:
            raise Fail("нет SSH до сервера %s (%s): Connection timed out" % (s["id"], s["ip"]))
        out, rc, err = "", 0, ""
        if cmd.startswith("pcs-node") and s["ip"] in self.old:
            return types.SimpleNamespace(returncode=127, stdout="", stderr="bash: pcs-node: command not found")
        if cmd.startswith("pcs-node info"):
            out = json.dumps({"ok": True, "data": INFO})
        elif cmd.startswith("pcs-node key --add") or cmd.startswith("pcs-node key --drop"):
            out = json.dumps({"ok": True, "data": {"added": True, "removed": 1, "fp": "SHA256:x"}})
        elif cmd.startswith("pcs-node key"):
            out = json.dumps({"ok": True, "data": [{"type": "ssh-ed25519", "fp": "SHA256:x", "comment": "pcs@pve"}]})
        elif cmd.startswith("pcs-node dns-fix"):
            out = json.dumps({"ok": True, "data": {"dns": ["1.1.1.1"], "ok": True}})
        elif cmd.startswith("pcs-node passwd"):
            out = json.dumps({"ok": True, "data": {"changed": True}})
        elif cmd.startswith("pcs-node mpspace check"):
            out = json.dumps({"ok": True, "data": {"installed": True, "ok": True}})
        elif cmd.startswith("proxyveth status"):
            out = "предупреждение до конверта\n" + json.dumps({"ok": True, "data": [{"n": 201, "state": "ok"}]})
        elif cmd.startswith("modlink weird"):
            out, err, rc = "Traceback (most recent call last):", "сломалось", 1
        return types.SimpleNamespace(returncode=rc, stdout=out, stderr=err)


net = Net()
remote.call = net.call
VM = {"1000": "running", "2000": "running", "200": "stopped", "20000": "running"}
pve.is_pve = lambda: True
pve.qm_status = lambda vmid: VM.get(str(vmid), "absent")
servers.host_ip = lambda: "88.87.69.240"

print("API: серверы:")
lst = api.servers()
by = {d["id"]: d for d in lst}
check("все серверы, порядок по номерам", [d["id"] for d in lst] == [200, 1000, 2000, 20000])
check("2000: работает, usb, модемы 38/40, mp.space", by[2000]["state"] == "running" and by[2000]["mode"] == "usb"
      and by[2000]["modems"] == {"ok": 38, "warn": 1, "broken": 1, "total": 40} and by[2000]["mpspace"]["ok"], by[2000])
check("200: остановлена — по SSH не ходим", by[200]["state"] == "stopped" and not any(c[0] == 200 for c in net.cmds))
check("1000: нет SSH — offline с причиной", by[1000]["state"] == "offline" and "Connection" in by[1000]["error"])
check("20000: нет pcs-node — old, подсказка pcs update", by[20000]["state"] == "old" and "pcs update" in by[20000]["error"])
check("ключи §9 на месте, без внутренностей", all(set(("id", "name", "ip", "state", "mode", "modems", "mpspace"))
                                                  <= set(d) and "info" not in d for d in lst))
check("режим запомнен в записи сервера", store.load(2000).get("mode") == "usb")
d = api.server(2000)
check("server(id): версии частей и подробности", d["versions"]["server"] == VERSION and d["versions"]["proxyveth"] == VERSION
      and d["versions"]["hivelink"] is None and d["outdated"] is False and d["kernel"] == "6.8.0", d)
try:
    api.server("host")
    check("server('host') — отказ", False)
except Fail:
    check("server('host') — отказ", True)
h = api.host_info()
check("host_info: §9 ключи; hivelink не готов — не падение", set(("name", "kind", "version", "cpu", "ram", "disk",
                                                                   "hivelink")) <= set(h)
      and h["kind"] == "proxmox" and h["version"] == VERSION and h["hivelink"].get("installed") is False, h)

print("API: команды частей:")
net.cmds.clear()
env = api.run(2000, "proxyveth", ["status"])
check("run: конверт от `proxyveth status --json`", env == {"ok": True, "data": [{"n": 201, "state": "ok"}]}, env)
check("run: --json дописан, команда — на нужном сервере", net.cmds[-1][:2] == (2000, "proxyveth status --json"))
api.run("2000", "proxyveth", ["source", "https://x?a=1&b=2; rm -rf /"])
check("аргументы экранированы — из таблицы может прийти что угодно",
      net.cmds[-1][1] == "proxyveth source 'https://x?a=1&b=2; rm -rf /' --json", net.cmds[-1])
env = api.run(2000, "modlink", ["weird"])
check("не JSON — конверт с ошибкой, без исключения", env["ok"] is False and "сломалось" in env["error"], env)
for args, why in (((None, "proxyveth", ["status"]), "на хосте только hivelink"), ((2000, "bash", ["-c", "id"]), "чужая часть"),
                  ((2000, "proxyveth", "status"), "args не список"), ((3000, "proxyveth", []), "неизвестный сервер")):
    try:
        api.run(*args)
        check("run: отказ — %s" % why, False)
    except Fail:
        check("run: отказ — %s" % why, True)
try:
    api.run(1000, "proxyveth", ["status"])
    check("run: нет SSH — Fail", False)
except Fail as e:
    check("run: нет SSH — Fail", "нет SSH" in str(e))

print("API: DNS, пароль, ключ:")
net.cmds.clear()
d = api.dns_fix(2000, "1.1.1.1 8.8.8.8")
check("dns_fix → pcs-node dns-fix --dns", net.cmds[-1][1] == "pcs-node dns-fix --dns '1.1.1.1 8.8.8.8' --json" and d["ok"])
api.dns_fix(2000, ["9.9.9.9"])
check("dns списком", "--dns 9.9.9.9" in net.cmds[-1][1])
n = len(net.cmds)
try:
    api.dns_fix(2000, "1.1.1.1; reboot")
    check("брак DNS — отказ до SSH", False)
except Fail:
    check("брак DNS — отказ до SSH", len(net.cmds) == n)
d = api.set_password(2000, "Нов0й:пароль'")
check("пароль — по stdin, не в командной строке", net.cmds[-1][1] == "pcs-node passwd --json"
      and net.cmds[-1][2] == "Нов0й:пароль'\n" and "пароль" not in net.cmds[-1][1])
check("пароль запомнен в записи сервера, в ответе не светится", store.load(2000)["password"] == "Нов0й:пароль'"
      and d["password"] is None)
d = api.set_password(2000, "")
check("пустой — сгенерирован и возвращён", d["password"] and len(d["password"]) >= 12
      and store.load(2000)["password"] == d["password"])
k = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIGy5r2bU1y7kEJZ9Tq0Hn0Ff7n4gJr8vJ8m1yYwV2x9Q me@laptop"
d = api.set_key(2000, k)
check("set_key: ключ — по stdin", net.cmds[-1][1] == "pcs-node key --add --json" and net.cmds[-1][2] == k + "\n"
      and d["target"] == 2000)
d = api.set_key(2000)
check("set_key без ключа — список и ключ хаба", d["keys"][0]["fp"] == "SHA256:x" and d["hub"]["pub"] == HUBPUB
      and d["hub"]["fp"].startswith("SHA256:"), d)
try:
    api.set_key(2000, "не ключ")
    check("set_key: не ключ — отказ", False)
except Fail:
    check("set_key: не ключ — отказ", True)
try:
    api.set_key("host", rotate=True)
    check("rotate у хоста — отказ", False)
except Fail as e:
    check("rotate у хоста — отказ", "сервер" in str(e))

if shutil.which("ssh-keygen"):
    print("ротация ключа хаба для сервера:")
    alive_with = []
    remote.alive = lambda s, timeout=30, key=None: (alive_with.append(key), key is not None)[1]
    net.cmds.clear()
    d = api.set_key(2000, rotate=True)
    s = store.load(2000)
    newpub = open(s["key"] + ".pub").read().strip()
    check("новый ключ: файл, запись сервера, отпечаток", s["key"].endswith("srv-2000_ed25519") and os.path.exists(s["key"])
          and d["rotated"] and d["fp"].startswith("SHA256:"), d)
    check("сначала положен новый, проверен им, потом снят старый",
          [c[1] for c in net.cmds] == ["pcs-node key --add --json", "pcs-node key --drop --json"]
          and net.cmds[0][2] == newpub + "\n" and net.cmds[1][2] == HUBPUB + "\n" and alive_with[-1].endswith(".new"),
          net.cmds)
    check("вход с ключом проверяется без общего мастер-соединения",
          "ControlPath=none" in remote.base(s, key=s["key"]) and "IdentitiesOnly=yes" in remote.base(s))
    check("общий ключ хаба цел (он у других серверов)", open(store.key_path() + ".pub").read().strip() == HUBPUB)
    remote.alive = lambda s, timeout=30, key=None: False
    net.cmds.clear()
    try:
        api.set_key(1000 if False else 2000, rotate=True)
        check("новый ключ не пускает — откат", False)
    except Fail as e:
        s2 = store.load(2000)
        check("новый ключ не пускает — откат, прежний на месте", "прежний" in str(e) and s2["key"] == s["key"]
              and not os.path.exists(s["key"] + ".new") and net.cmds[-1][1] == "pcs-node key --drop --json", net.cmds)

print("API: удаление, mp.space, задания, терминал:")
deleted = []
servers.delete = lambda sid: (deleted.append(sid), {"id": sid, "deleted": True})[1]
try:
    api.delete_server(2000, "1000")
    check("удаление: не тот номер — отказ", False)
except Fail as e:
    check("удаление: не тот номер — отказ", not deleted and "2000" in str(e))
check("удаление: номер совпал", api.delete_server(2000, " 2000 ")["deleted"] and deleted == [2000])
launched = []
jobs.launch = lambda jid: launched.append(jid)
jid = api.mpspace(2000, "install", auth='{"auth": "K:K", "port": 1800}', proxy="http://u:p@h:3128")
pp = os.path.join(jobs.DIR, jid + ".params")
check("mp.space install → задание, секреты — в файле 600", launched == [jid] and stat.S_IMODE(os.stat(pp).st_mode) == 0o600
      and json.load(open(pp))["auth"] == '{"auth": "K:K", "port": 1800}')
j = api.job(jid)
check("job(): running, заголовок, без секретов", j["state"] == "running" and "2000" in j["title"]
      and "K:K" not in json.dumps(j) and j["log"] == [], j)
try:
    api.mpspace(2000, "install")
    check("второй install на тот же сервер — отказ", False)
except Fail:
    check("второй install на тот же сервер — отказ", True)
check("mp.space check", api.mpspace(2000, "check") == {"installed": True, "ok": True})
for args in (("host", "check"), (2000, "remove")):
    try:
        api.mpspace(*args)
        check("mp.space: отказ %s" % (args,), False)
    except Fail:
        check("mp.space: отказ %s" % (args,), True)
try:
    api.create_server({"name": "плохое имя"})
    check("create_server: брак — до задания", False)
except Fail:
    check("create_server: брак — до задания", len(launched) == 1)
jid2 = api.create_server({"name": "pcs3000", "cores": 2, "ram_gb": 4, "disk_gb": 40, "mode": "gw", "sheet": "",
                          "mpspace": False, "replace": True})
p = json.load(open(os.path.join(jobs.DIR, jid2 + ".params")))
check("create_server → задание; replace из панели не проходит", launched[-1] == jid2 and p["mode"] == "gw"
      and "replace" not in p and p["dns"] == ["1.1.1.1", "8.8.8.8"], p)
t = api.term_argv(2000)
check("term_argv сервера: ssh -tt root@IP ключом хаба", t[0] == "ssh" and "-tt" in t and t[-1] == "root@192.168.88.7"
      and "-i" in t, t)
check("term_argv хоста: bash -l", api.term_argv("host") == ["bash", "-l"])
try:
    api.job("../../etc/passwd")
    check("job: чужой путь — отказ", False)
except Fail:
    check("job: чужой путь — отказ", True)

print("задания:")


def inproc(kinds):
    def launcher(jid):
        with open(os.path.join(jobs.DIR, jid + ".log"), "a") as f, redirect_stdout(f):
            jobs.run(jid, kinds)
    return launcher


def ok_job(p):
    print("  → шаг 1")
    print("  \033[32m✓\033[0m шаг 2 для %s" % p["x"])
    return {"done": p["x"]}


def bad_job(p):
    raise Fail("не вышло: %s" % p["x"])


def crash_job(p):
    return 1 / 0


kinds = {"ok": ok_job, "bad": bad_job, "crash": crash_job}
j = jobs.get(jobs.start("ok", {"x": "a"}, "проверка", target=7, launcher=inproc(kinds)))
check("готово: результат, журнал без цветов", j["state"] == "done" and j["result"] == {"done": "a"}
      and "  ✓ шаг 2 для a" in j["log"] and j["target"] == "7", j)
check("параметры удалены, как только прочитаны", not os.path.exists(os.path.join(jobs.DIR, j["id"] + ".params")))
j = jobs.get(jobs.start("bad", {"x": "b"}, launcher=inproc(kinds)))
check("провал: текст ошибки", j["state"] == "failed" and j["error"] == "не вышло: b" and j["finished"], j)
j = jobs.get(jobs.start("crash", {}, launcher=inproc(kinds)))
check("исключение — failed, а не вечный running", j["state"] == "failed" and "ZeroDivisionError" in j["error"], j)
j = jobs.get(jobs.start("nope", {}, launcher=inproc(kinds)))
check("неизвестный вид — failed", j["state"] == "failed")
jid = jobs.start("ok", {"x": 1}, launcher=lambda jid: None)
st = json.load(open(os.path.join(jobs.DIR, jid + ".json")))
st["pid"] = 999999
json.dump(st, open(os.path.join(jobs.DIR, jid + ".json"), "w"))
check("процесс исчез — failed с объяснением", jobs.get(jid)["state"] == "failed" and "оборвалось" in jobs.get(jid)["error"])
check("список последних", len(jobs.recent()) >= 6 and jobs.recent()[0]["id"] >= jobs.recent()[-1]["id"])
check("файлы заданий — 600", stat.S_IMODE(os.stat(os.path.join(jobs.DIR, jid + ".json")).st_mode) == 0o600)

print("доставка кода на сервер:")
data = remote.tarball()
names = tarfile.open(fileobj=io.BytesIO(data)).getnames()
check("в tar — пакет и команды", all(n in names for n in ("bin/pcs-node", "bin/proxyveth", "bin/modlink",
                                                          "pcs/hub/node.py", "pcs/core/dns.py", "pcs/__init__.py")))
check("без .git, тестов и кэша", not any(n.split("/")[0] in ("tests", ".git") or "__pycache__" in n
                                        or n.endswith(".pyc") for n in names), sorted(names)[:8])
ti = tarfile.open(fileobj=io.BytesIO(data)).getmember("bin/pcs-node")
check("команды исполняемые, владелец root", ti.mode & 0o111 and ti.uname == "root")
check("на сервере: компиляция до подмены, симлинки команд, версия",
      remote.INSTALL.index("compileall") < remote.INSTALL.index("mv /opt/pcs.new /opt/pcs")
      and "for c in proxyveth modlink hivelink pcs-node" in remote.INSTALL and "ln -sfn /opt/pcs/bin/$c" in remote.INSTALL)
check("конверт после лишних строк", remote.envelope('мусор\n{"ok": true, "data": 1}') == {"ok": True, "data": 1}
      and remote.envelope("Traceback")["ok"] is False)

print("сервер: setup после доставки:")
calls = []
remote.deliver = lambda s, root=None: (calls.append("deliver"), VERSION)[1]
remote.stream = lambda s, cmd, tty=False: (calls.append(cmd), 0)[1]
remote.node = lambda s, *a, **k: (calls.append("pcs-node " + " ".join(a)), {"dns": ["1.1.1.1"]})[1]
remote.ssh = lambda s, cmd, check=True, timeout=None, input=None: types.SimpleNamespace(returncode=0, stdout="yes\n", stderr="")
s = store.load(2000)
quiet = io.StringIO()
with redirect_stdout(quiet):
    servers.setup(s, mode="gw", sheet="https://docs.google.com/x", fresh=True)
check("новая ВМ: код → DNS → proxyveth setup → mode → modlink setup → таблица → sync", calls == [
    "deliver", "pcs-node dns-fix", "proxyveth setup", "proxyveth mode gw --yes", "modlink setup",
    "proxyveth source https://docs.google.com/x", "proxyveth sync"], calls)
calls.clear()
with redirect_stdout(quiet):
    servers.setup(s)
check("обновление: код → proxyveth setup → modlink setup (DNS не трогаем)", calls == [
    "deliver", "proxyveth setup", "modlink setup"], calls)
calls.clear()
remote.ssh = lambda s, cmd, check=True, timeout=None, input=None: types.SimpleNamespace(returncode=1, stdout="", stderr="")
with redirect_stdout(quiet):
    servers.setup(s)
check("proxyveth не было — только modlink setup", calls == ["deliver", "modlink setup"], calls)
calls.clear()
remote.ssh = lambda s, cmd, check=True, timeout=None, input=None: types.SimpleNamespace(returncode=0, stdout="yes\n", stderr="")
remote.stream = lambda s, cmd, tty=False: (calls.append(cmd), 1 if cmd == "proxyveth setup" else 0)[1]
try:
    with redirect_stdout(quiet), redirect_stderr(quiet):
        servers.setup(s)
    check("proxyveth setup упал — Fail, дальше не идём", False)
except Fail:
    check("proxyveth setup упал — Fail, дальше не идём", "modlink setup" not in calls)
with redirect_stdout(quiet), redirect_stderr(quiet):
    upd = servers.update_servers([2000, 200])
check("update --server: остановленная ВМ пропущена, ошибки собраны", upd["updated"] == [] and set(upd["failed"]) == {"2000", "200"}
      and "не запущена" in upd["failed"]["200"], upd)

print("сквозные команды pcs proxyveth|modlink|hivelink:")
streamed, called = [], []
remote.stream = lambda s, cmd, tty=False: (streamed.append((s["id"], cmd, tty)), 0)[1]
real_call = subprocess.call
subprocess.call = lambda argv, **k: (called.append(argv), 0)[1]
try:
    rc = cli.passthru("proxyveth", ["status", "--wan", "--server", "200"])
    check("pcs proxyveth status --wan --server 200", rc == 0 and streamed[-1] == (200, "proxyveth status --wan", False), streamed)
    cli.passthru("modlink", ["add", "--lan-ip", "192.168.201.100", "--name", "мой модем"])
    check("без --server — выбранный; аргументы с пробелами целы",
          streamed[-1][:2] == (2000, "modlink add --lan-ip 192.168.201.100 --name 'мой модем'"), streamed[-1])
    cli.passthru("hivelink", ["install", "--host"])
    check("hivelink --host — на самом хосте", called[-1][0].endswith("bin/hivelink") and called[-1][1:] == ["install"])
    err = io.StringIO()
    real_err, sys.stderr = sys.stderr, err
    try:
        rc = cli.passthru("modlink", ["list", "--host"])
        rc2 = cli.passthru("proxyveth", ["status", "--server", "4242"])
    finally:
        sys.stderr = real_err
    check("modlink --host — отказ, неизвестный сервер — отказ", rc == 1 and rc2 == 1 and "4242" in err.getvalue(), err.getvalue())
    out = io.StringIO()
    with redirect_stdout(out):
        rc = cli.passthru("proxyveth", ["status", "--json", "--server", "4242"])
    check("ошибка хаба при --json — конвертом", rc == 1 and json.loads(out.getvalue())["ok"] is False)
    rc = cli.shell("exec", ["2000", "uptime"])
    check("pcs exec 2000 'cmd'", streamed[-1][:2] == (2000, "uptime"))
    cli.shell("exec", ["df", "-h"])
    check("pcs exec 'cmd' — на выбранном", streamed[-1][:2] == (2000, "df -h"))
finally:
    subprocess.call = real_call

print("вход в панель (web.json):")
web.ITER = 1000
web.set_login("admin", "очень-секретно")
wj = os.path.join(store.ETC, "web.json")
d = json.load(open(wj))
check("PBKDF2-SHA256 с солью (формат панели), без пароля в файле, права 600", d["password"]["algo"] == "pbkdf2-sha256" and len(d["password"]["salt"]) == 32
      and "очень" not in open(wj).read() and stat.S_IMODE(os.stat(wj).st_mode) == 0o600, d)
check("верный вход", web.verify("admin", "очень-секретно") is True)
check("неверный пароль или логин", not web.verify("admin", "очень-секретнО") and not web.verify("root", "очень-секретно"))
for u, p in (("admin", "коротко"), ("ад мин", "достаточно-длинный"), ("admin", "две\nстроки-пароля")):
    try:
        web.set_login(u, p)
        check("брак логина/пароля — отказ: %r" % u, False)
    except Fail:
        check("брак логина/пароля — отказ: %r" % u, True)
try:
    from pcs.web import server as _panel
except ImportError:
    _panel = None
if _panel is None:
    try:
        web.serve([])
        check("pcs web serve без панели — понятная ошибка", False)
    except Fail as e:
        check("pcs web serve без панели — понятная ошибка", "веб-панели" in str(e) or "не готова" in str(e), e)
else:                                        # панель есть — serve отдаёт ей управление, не поднимая сервер
    _seen, _real = [], _panel.main
    _panel.main = lambda argv=None: _seen.append(argv) or 0
    try:
        check("pcs web serve → pcs.web.server.main(argv)", web.serve(["--port", "6660"]) == 0
              and _seen == [["--port", "6660"]], _seen)
    finally:
        _panel.main = _real

# ── меню ───────────────────────────────────────────────────────────────────
print("меню:")
FRAMES = {
    "2000": {"kind": "vm", "id": 2000, "name": "pcs2000", "ip": "192.168.88.7", "state": "running", "mode": "usb",
             "modems": {"ok": 38, "warn": 1, "broken": 1, "total": 40}, "mpspace": {"installed": True, "ok": True},
             "version": VERSION},
    "1000": {"kind": "vm", "id": 1000, "name": "pcs1000", "ip": "192.168.88.5", "state": "offline", "mode": None,
             "modems": {"ok": 0, "warn": 0, "broken": 0, "total": 0}, "mpspace": None, "version": None,
             "error": "нет SSH до сервера 1000"},
    "host": {"kind": "host", "id": "хост", "name": "pve", "ip": "88.87.69.240", "state": "running", "version": VERSION},
}


class Script:
    def __init__(self, answers):
        self.answers, self.prompts = list(answers), []

    def __call__(self, prompt):
        self.prompts.append(prompt)
        if not self.answers:
            raise EOFError
        a = self.answers.pop(0)
        if isinstance(a, BaseException):
            raise a
        return a


def run_menu(answers, sel="2000", call=None, color=False):
    with open(os.path.join(store.ETC, "active"), "w") as f:
        f.write(sel + "\n")
    inp, out, ran = Script(answers), io.StringIO(), []
    m = menu.Menu(inp=inp, out=out, call=call or (lambda argv: (ran.append(argv), 0)[1]),
                  fetch=lambda s: dict(FRAMES[s]), color=color, clear=False)
    rc = m.main()
    return types.SimpleNamespace(rc=rc, out=out.getvalue(), prompts=inp.prompts, ran=ran, menu=m)


r = run_menu(["0"])
frame = [ln for ln in r.out.splitlines() if ln.strip().startswith(("╔", "║", "╚"))]
check("рамка наверху: номер, имя, IP, ВМ, режим, модемы, mp.space",
      "ВМ 2000" in r.out and "pcs2000 · 192.168.88.7" in r.out and "proxyveth usb" in r.out
      and "модемы 38/40" in r.out and "⚠ 1" in r.out and "✗ 1" in r.out and "mp.space: работает" in r.out, r.out)
check("рамка — несколько строк одной ширины", len(frame) >= 5 and len({menu.vlen(ln.rstrip()) for ln in frame}) == 1,
      [menu.vlen(ln.rstrip()) for ln in frame])
check("сервер в строке ввода", r.prompts[0].strip() == "[2000 pcs2000 192.168.88.7] »", r.prompts[0])
check("разделы §11", all(x in r.out for x in ("Серверы", "Модемы", "Прокси", "Софт", "Сеть и доступ", "Диагностика")))
check("на главном 0 — выход, v — сменить сервер", "0  выход" in r.out and "v  сменить сервер" in r.out and r.rc == 0)

every = []
for sec in ("1", "2", "3", "4", "5", "6"):
    rr = run_menu([sec, "0", "0"])
    every.append(rr.out)
    tail = rr.out.split("\n  PCS\n")[1] if "\n  PCS\n" in rr.out else rr.out
    check("раздел %s: рамка снова наверху и «0 — назад»" % sec, rr.out.count("╔") >= 3 and "0  назад" in rr.out
          and len(rr.prompts) == 3 and all("[2000 pcs2000" in p for p in rr.prompts))
alltext = "\n".join(every).lower()
check("нет пунктов «сменить IP», «перезагрузить модем», Telegram", not any(
    x in alltext for x in ("сменить ip", "перезагрузить модем", "telegram", "rotate n", "reboot n")), alltext[:300])

r = run_menu(["abc", "", "0"])
check("неверный ввод — подсказка, меню живо", "нет такого пункта: abc" in r.out and r.rc == 0)
r = run_menu(["1", "7", "1000", "", "0", "0"])
check("удаление: чужой номер — не подтверждено, ничего не запущено", not r.ran and "не подтверждено" in r.out)
r = run_menu(["1", "7", "", "", "0", "0"])
check("удаление: Enter — отмена", not r.ran)
r = run_menu(["1", "7", "2000", "", "0", "0"])
check("удаление: введён номер — delete --yes", r.ran == [["server", "delete", "2000", "--yes"]], r.ran)
check("предупреждение называет сервер", "Удалить ВМ 2000" in r.out)
r = run_menu(["2", "12", "gw", "2000", "", "0", "0"])
check("смена режима — после номера сервера", r.ran == [["proxyveth", "mode", "gw", "--yes", "--server", "2000"]], r.ran)
r = run_menu(["2", "12", "virt", "", "0", "0"])
check("режим не usb/gw — ошибка, не падение", not r.ran and "не подходит" in r.out)
r = run_menu(["2", "11", "all", "2001", "", "0", "0"])
check("опустить все модемы — тоже номером", not r.ran)
r = run_menu(["2", "8", "201", "", "0", "0"])
check("диагностика модема — без подтверждения", r.ran == [["proxyveth", "diag", "201", "--server", "2000"]], r.ran)
r = run_menu(["3", "8", "5", "2000", "", "0", "0"])
check("удаление прокси — номером, modlink del ID --yes", r.ran == [["modlink", "del", "5", "--yes", "--server", "2000"]], r.ran)
r = run_menu(["5", "1", "", "", "0", "0"])
check("DNS: по умолчанию 1.1.1.1 8.8.8.8 на выбранном", r.ran == [["dns", "--dns", "1.1.1.1 8.8.8.8", "--server", "2000"]], r.ran)

r = run_menu(["v", "хост", "2", "1", "", "0", "5", "1", "", "", "0", "0"])
check("v → хост: рамка и строка ввода про хост", "ХОСТ" in r.out and any("[хост pve 88.87.69.240]" in p for p in r.prompts))
check("хост выбран — модемы не для него, понятная ошибка", "выбран хост" in r.out)
check("хост: DNS — с --host", ["dns", "--dns", "1.1.1.1 8.8.8.8", "--host"] in r.ran, r.ran)
check("выбор хоста запомнен", store.active() == "host")
r = run_menu(["5", "2", "pve", "", "0", "0"], sel="host")
check("пароль хоста — подтверждение именем хоста", r.ran == [["passwd", "--host"]], r.ran)
r = run_menu(["v", "1000", "0"])
check("v → 1000: рамка другого сервера с причиной", "ВМ 1000" in r.out.split("Какой сервер?")[1]
      and "нет связи по SSH" in r.out and store.active() == "1000")
r = run_menu(["v", "9999", "", "0"], sel="2000")
check("v → неизвестный: ошибка, выбор прежний", "9999" in r.out and store.active() == "2000")
with open(os.path.join(store.ETC, "active"), "w") as f:
    f.write("")
m = menu.Menu(inp=Script(["0"]), out=io.StringIO(), call=lambda a: 0, fetch=lambda s: {}, color=False, clear=False)
m.main()
check("сервер не выбран — так и сказано", "СЕРВЕР НЕ ВЫБРАН" in m.out.getvalue())
r = run_menu(["1", "1", RuntimeError("бах"), "0"], call=lambda a: (_ for _ in ()).throw(RuntimeError("бах")))
check("сбой внутри пункта — меню живо", "внутренняя ошибка" in r.out and r.rc == 0)
r = run_menu(["2", KeyboardInterrupt()])
check("Ctrl+C — тихий выход", r.rc == 0)
r = run_menu([], sel="2000")
check("конец ввода — выход без трассы", r.rc == 0)
c1 = run_menu(["0"], sel="1000", color=True).out
c2 = run_menu(["0"], sel="2000", color=True).out
first = lambda t: re.search(r"\x1b\[1m(\x1b\[3\dm)╔", t).group(1)   # noqa: E731
check("у разных серверов разный цвет рамки", first(c1) != first(c2), (first(c1), first(c2)))

shutil.rmtree(TMP)
print("\nитого: ok %d, fail %d" % (passed, failed))
sys.exit(1 if failed else 0)
