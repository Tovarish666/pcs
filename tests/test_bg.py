#!/usr/bin/env python3
"""Долгие команды в фоне (pcs/core/bg.py, задания хаба, панель) — без root и без systemd.

    python3 tests/test_bg.py
"""
import io
import json
import os
import sys
import tempfile
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.join(HERE, os.pardir)
sys.path.insert(0, ROOT)
TMP = tempfile.mkdtemp(prefix="pcs-bg-test-")
os.environ["PCS_BG_DIR"] = os.path.join(TMP, "bg")
os.environ["PCS_JOBS"] = os.path.join(TMP, "jobs")
from pcs.core import bg  # noqa: E402
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


def inline(rid):
    """Вместо systemd-run: исполнитель сразу, вывод — в журнал (как StandardOutput=append:)."""
    log = bg._p(rid, "log")
    real = sys.stdout
    import subprocess
    st = bg.jload(bg._p(rid, "json"), {})
    with open(log, "a") as f:
        rc = subprocess.call(st["argv"], stdout=f, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL)
    st.update(state="done" if rc == 0 else "failed", rc=rc, finished=int(time.time()), pid=os.getpid())
    bg.jsave(bg._p(rid, "json"), st, 0o600)
    sys.stdout = real


print("фон: когда уходить")
check("не терминал (тесты, хаб, панель) — работать здесь", bg.wanted() is False)
os.environ["PCS_BG"] = "1"
check("уже в фоне — второй раз не уходить", bg.wanted() is False and bg.handoff("x", ["a"]) is None)
del os.environ["PCS_BG"]
check("подтверждение из терминала доходит до фона", not bg.confirmed())
os.environ[bg.CONFIRMED] = "1"
check("PCS_BG_CONFIRMED — подтверждено", bg.confirmed())
del os.environ[bg.CONFIRMED]

print("фон: запуск, журнал, итог")
rid = bg.start("modlink migrate", [sys.executable, "-c", "print('строка 1'); print('строка 2')"], "modlink migrate",
               launcher=inline)
d = bg.get(rid)
check("состояние done, код 0, журнал — вывод команды", d["state"] == "done" and d["rc"] == 0
      and d["log"] == ["строка 1", "строка 2"] and d["kind"] == "bg", d)
check("id узнаётся (pcs job ID различает задания и фоновые запуски)", bg.is_id(rid) and not bg.is_id("20990101-x"))
rid2 = bg.start("hivelink install", [sys.executable, "-c", "import sys; print('сломалось'); sys.exit(3)"], launcher=inline)
check("ошибка: failed и код команды", bg.get(rid2)["state"] == "failed" and bg.get(rid2)["rc"] == 3)
check("список: новые сверху", [x["id"] for x in bg.recent()][:2] == sorted([rid, rid2], reverse=True))

print("фон: смотреть журнал (follow)")
rid3 = bg.start("proxyveth sync", [sys.executable, "-c", "print('x')"], launcher=lambda r: None)
st = bg.jload(bg._p(rid3, "json"), {})
st["pid"] = os.getpid()
bg.jsave(bg._p(rid3, "json"), st, 0o600)


def later():
    time.sleep(0.3)
    with open(bg._p(rid3, "log"), "a") as f:
        f.write("создано 205\n")
    s = bg.jload(bg._p(rid3, "json"), {})
    s.update(state="done", rc=0)
    bg.jsave(bg._p(rid3, "json"), s, 0o600)


threading.Thread(target=later).start()
out = io.StringIO()
rc = bg.follow(rid3, out=out, sleep=lambda s: time.sleep(0.05))
check("follow: журнал по мере появления и код в конце", rc == 0 and "создано 205" in out.getvalue()
      and "связь оборвётся — не страшно" in out.getvalue() and "pcs-node bg %s" % rid3 in out.getvalue(), out.getvalue())


def ctrl_c(s):
    raise KeyboardInterrupt


rid4 = bg.start("x", [sys.executable, "-c", "pass"], launcher=lambda r: None)
st = bg.jload(bg._p(rid4, "json"), {})
st["pid"] = os.getpid()
bg.jsave(bg._p(rid4, "json"), st, 0o600)
out = io.StringIO()
check("Ctrl+C — перестать смотреть, команда идёт (130)", bg.follow(rid4, out=out, sleep=ctrl_c) == 130
      and "идёт дальше" in out.getvalue() and bg.get(rid4)["state"] == "running", out.getvalue())
st["pid"] = 99999999
bg.jsave(bg._p(rid4, "json"), st, 0o600)
d = bg.get(rid4)
check("процесс исчез — failed «оборвалось», а не вечное «идёт»", d["state"] == "failed" and "оборвалось" in d["error"], d)
try:
    bg.get("../../etc/passwd")
    check("чужой путь вместо id — Fail", False)
except Fail:
    check("чужой путь вместо id — Fail", True)

print("pcs-node bg")
buf = io.StringIO()
real, sys.stdout = sys.stdout, buf
try:
    rc_list = bg.main(["--json"])
    rc_one = bg.main([rid])
finally:
    sys.stdout = real
lines = buf.getvalue().splitlines()
check("pcs-node bg --json — список", rc_list == 0 and json.loads(lines[0])["ok"] and len(json.loads(lines[0])["data"]) >= 3)
check("pcs-node bg ID — журнал готового", rc_one == 0 and "строка 2" in buf.getvalue())

print("части: уходят в фон после подтверждений")
from pcs.proxyveth import cli as pv  # noqa: E402
calls = []
real_handoff = bg.handoff
bg.handoff = lambda name, args, title=None, argv=None: (calls.append((name, list(args))), 0)[1]
pv.ARGV[:] = ["mode", "gw", "--yes"]
try:
    pv.to_bg(None, "mode")
    check("to_bg: ушло в фон — Handoff с кодом", False)
except pv.Handoff as h:
    check("to_bg: ушло в фон — Handoff с кодом, аргументы те же", h.rc == 0 and calls[-1] == ("proxyveth-mode", ["mode", "gw", "--yes"]), calls)
bg.handoff = lambda name, args, title=None, argv=None: None
pv.to_bg(None, "sync")
check("не терминал — to_bg ничего не делает", True)
bg.handoff = real_handoff
os.environ[bg.CONFIRMED] = "1"


class A:
    yes = False
    json = False


try:
    pv.confirm(A(), "режим usb → gw", "gw")
    check("в фоне подтверждение не спрашивается (его дали в терминале)", True)
except Fail as e:
    check("в фоне подтверждение не спрашивается (его дали в терминале)", False, e)
del os.environ[bg.CONFIRMED]
src = open(os.path.join(ROOT, "pcs", "proxyveth", "cli.py"), encoding="utf-8").read()
i_mode = src.index("def cmd_mode")
seg = src[i_mode:src.index("def cmd_source")]
check("proxyveth mode: подтверждение → фон → блокировка (фоновая копия возьмёт её сама)",
      seg.index("confirm(a") < seg.index('to_bg(a, "mode")') < seg.index("with held()"))
seg = src[src.index("def cmd_updown"):src.index("def print_rows")]
check("proxyveth up/down/restart all: подтверждение → фон → блокировка",
      seg.index("confirm(a") < seg.index("to_bg(a, a.cmd)") < seg.index("with held()"))
ml = open(os.path.join(ROOT, "pcs", "modlink", "cli.py"), encoding="utf-8").read()
check("modlink: setup, migrate (не --dry-run), uninstall --yes — в фон",
      'a.cmd in ("setup", "migrate", "uninstall")' in ml and "dry_run" in ml and "bg.handoff(" in ml)
hl = open(os.path.join(ROOT, "pcs", "hivelink", "cli.py"), encoding="utf-8").read()
check("hivelink: install, uninstall — в фон", 'cmd in ("install", "uninstall")' in hl and "bg.handoff(" in hl)

print("хаб: задания из терминала и панели")
from pcs.hub import api, jobs  # noqa: E402
check("долгие команды частей — заданием", api.is_long("modlink", ["migrate", "--force", "--stop-panel"])
      and api.is_long("hivelink", ["install"]) and api.is_long("proxyveth", ["sync"])
      and api.is_long("proxyveth", ["restart", "all", "--yes"]))
check("быстрые — сразу", not api.is_long("modlink", ["migrate", "--dry-run"]) and not api.is_long("modlink", ["list"])
      and not api.is_long("proxyveth", ["up", "205"]) and not api.is_long("proxyveth", ["status"]))
check("виды заданий: part и host_setup", {"part", "host_setup", "update", "create_server", "mpspace"} <= set(api.KINDS))
started = []
real_start, real_busy = jobs.start, jobs.busy
jobs.start = lambda kind, params, title="", target=None, launcher=None: (started.append((kind, params, title, target)), "j1")[1]
jobs.busy = lambda kind=None, target=None: None
real_target = api._target
api._target = lambda t: api.store.HOST if t in (None, "host") else {"id": int(t)}
try:
    jid = api.run_job(None, "modlink", ["migrate", "--force"], input=None)
    check("run_job: задание part на хост", jid == "j1" and started[-1][0] == "part" and started[-1][1]["id"] is None
          and started[-1][1]["args"] == ["migrate", "--force"] and started[-1][3] == "host", started)
    jid = api.run_job(None, "proxyveth", ["sync"])
    check("run_job: proxyveth на хосте — тоже заданием (VPS)", started[-1][1]["part"] == "proxyveth"
          and started[-1][3] == "host", started[-1])
    try:
        api.run_job(None, "pcs-node", ["info"])
        check("run_job: чужая часть — отказ", False)
    except Fail:
        check("run_job: чужая часть — отказ", True)
finally:
    jobs.start, jobs.busy, api._target = real_start, real_busy, real_target
check("в задании хаба части в фон не уходят (PCS_BG=1 в окружении юнита)",
      '"PCS_BG": "1"' in open(os.path.join(ROOT, "pcs", "hub", "jobs.py"), encoding="utf-8").read())
hub = open(os.path.join(ROOT, "pcs", "hub", "cli.py"), encoding="utf-8").read()
for cmd, kind in (("c_update", '"update"'), ("c_host_setup", '"host_setup"'), ("c_server_create", '"create_server"'),
                  ("c_mpspace", '"mpspace"')):
    seg = hub[hub.index("def %s(" % cmd):]
    seg = seg[:seg.index("\ndef ", 5)]
    check("pcs %s из терминала — заданием (as_job)" % cmd[2:].replace("_", " "), "as_job(a, %s" % kind in seg, cmd)
os.makedirs(jobs.DIR, exist_ok=True)
jid = "20990101-000000-abcd"
jobs._save({"id": jid, "kind": "update", "title": "Обновление", "state": "running", "started": 1, "pid": os.getpid()})
open(jobs._p(jid, "log"), "w").write("шаг 1\n")


def finish():
    time.sleep(0.2)
    with open(jobs._p(jid, "log"), "a") as f:
        f.write("шаг 2\n")
    s = bg.jload(jobs._p(jid, "json"), {})
    s.update(state="done")
    jobs._save(s)


threading.Thread(target=finish).start()
out = io.StringIO()
rc = jobs.follow(jid, out=out, sleep=lambda s: time.sleep(0.05))
check("pcs job: смотреть задание хаба до конца", rc == 0 and "шаг 1" in out.getvalue() and "шаг 2" in out.getvalue()
      and "pcs job %s" % jid in out.getvalue(), out.getvalue())
lst = api.jobs_recent()
check("список для панели: и задания хаба, и фоновые запуски", {"bg"} <= {d.get("kind") for d in lst}
      and any(d["id"] == jid for d in lst), [d["id"] for d in lst])
check("api.job отдаёт и фоновый запуск", api.job(rid)["log"] == ["строка 1", "строка 2"])

print("\nитого: ok %d, fail %d" % (passed, failed))
sys.exit(1 if failed else 0)
