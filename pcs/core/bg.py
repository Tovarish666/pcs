"""Долгие команды — в фоне: обрыв SSH их не убивает, результат не теряется.

Команда, запущенная из терминала (setup, migrate, install, mode, sync…), сама
перезапускается фоновым юнитом systemd (pcs-bg-ID) и пишет всё в журнал; терминал
только показывает этот журнал. Связь оборвалась — команда доработает; снова
посмотреть: `pcs job` на хосте, `pcs-node bg` на сервере.

    rc = bg.handoff("modlink-migrate", argv)   # None — работать здесь (не терминал / уже в фоне)

В /var/lib/pcs/bg: ID.json — {"id", "title", "argv", "state": running|done|failed, "rc",
"started", "finished", "pid"}; ID.log — вывод команды.
"""
import os
import re
import secrets
import shutil
import subprocess
import sys
import time

from .util import Fail, jload, jsave

DIR = os.environ.get("PCS_BG_DIR", "/var/lib/pcs/bg")
KEEP = 50
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.realpath(__file__))))
ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
ENV = "PCS_BG"                 # уже в фоне (или в задании хаба) — второй раз не уходить
CONFIRMED = "PCS_BG_CONFIRMED"  # подтверждения дал человек в терминале до ухода в фон


def wanted():
    """Уходить в фон: человек в терминале, root, есть systemd, ещё не в фоне."""
    return (not os.environ.get(ENV) and sys.stdin.isatty() and sys.stdout.isatty()
            and hasattr(os, "geteuid") and os.geteuid() == 0 and bool(shutil.which("systemd-run")))


def confirmed():
    return bool(os.environ.get(CONFIRMED))


def _p(rid, ext):
    if not re.fullmatch(r"[a-z0-9-]{6,60}", rid or ""):
        raise Fail("нет такого фонового запуска: %s" % rid)
    return os.path.join(DIR, "%s.%s" % (rid, ext))


def is_id(rid):
    return bool(rid) and os.path.exists(os.path.join(DIR, "%s.json" % rid)) and bool(re.fullmatch(r"[a-z0-9-]{6,60}", rid))


def self_argv(args):
    """Эта же команда: тот же python и тот же скрипт bin/…, другие аргументы."""
    return [sys.executable, os.path.realpath(sys.argv[0])] + list(args)


def handoff(name, args, title=None, argv=None):
    """Если человек в терминале — перезапустить команду в фоне и показывать журнал.
    → код фоновой команды, или None (делать здесь: не терминал, уже в фоне, нет systemd)."""
    if not wanted():
        return None
    rid = start(name, argv or self_argv(args), title)
    return follow(rid)


def start(name, argv, title=None, launcher=None):
    os.makedirs(DIR, mode=0o700, exist_ok=True)
    prune()
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:24] or "cmd"
    rid = "%s-%s-%s" % (time.strftime("%Y%m%d-%H%M%S"), slug, secrets.token_hex(2))
    fd = os.open(_p(rid, "log"), os.O_CREAT | os.O_WRONLY | os.O_APPEND, 0o600)
    os.close(fd)
    jsave(_p(rid, "json"), {"id": rid, "title": title or name, "argv": list(argv), "state": "running",
                            "rc": None, "started": int(time.time()), "finished": None, "pid": None,
                            "host": os.uname().nodename}, 0o600)
    (launcher or launch)(rid)
    return rid


def launch(rid):
    log = _p(rid, "log")
    exe = [sys.executable, "-c", "import sys; sys.path.insert(0, %r); from pcs.core import bg; "
           "sys.exit(bg.exec_run(sys.argv[1]))" % ROOT, rid]
    env = {ENV: "1", CONFIRMED: "1", "PYTHONUNBUFFERED": "1", "PCS_BG_DIR": DIR}
    for k in ("PATH", "LANG", "LC_ALL", "PCS_ETC", "PCS_RUN", "PCS_LOG", "PCS_JOBS", "MODLINK_ROOT"):
        if os.environ.get(k):
            env[k] = os.environ[k]
    r = subprocess.run(["systemd-run", "--unit", "pcs-bg-%s" % rid, "--collect", "--quiet",
                        "-p", "StandardOutput=append:%s" % log, "-p", "StandardError=append:%s" % log]
                       + ["--setenv=%s=%s" % kv for kv in env.items()] + exe, capture_output=True, text=True)
    if r.returncode != 0:
        raise Fail("не запустилось в фоне (systemd-run): %s" % (r.stderr.strip() or r.returncode))


def exec_run(rid):
    """Исполнитель внутри юнита: команда → журнал, код → состояние."""
    st = jload(_p(rid, "json"), None)
    if not st:
        return 2
    st["pid"] = os.getpid()
    jsave(_p(rid, "json"), st, 0o600)
    try:
        rc = subprocess.call(st["argv"], stdin=subprocess.DEVNULL)
    except OSError as e:
        print("  ✗ не запустилось: %s" % e, flush=True)
        rc = 127
    st.update(state="done" if rc == 0 else "failed", rc=rc, finished=int(time.time()))
    print("\n  %s %s" % ("✓" if rc == 0 else "✗", "готово" if rc == 0 else "завершилось с ошибкой (код %d)" % rc), flush=True)
    jsave(_p(rid, "json"), st, 0o600)
    return rc


def alive(pid):
    try:
        os.kill(int(pid), 0)
        return True
    except (OSError, ValueError, TypeError):
        return False


def get(rid, lines=400):
    st = jload(_p(rid, "json"), None)
    if not st:
        raise Fail("нет такого фонового запуска: %s" % rid)
    if st.get("state") == "running":
        gone = (st.get("pid") and not alive(st["pid"])) or \
               (not st.get("pid") and time.time() - st.get("started", 0) > 60)
        if gone:
            st.update(state="failed", rc=None, finished=int(time.time()))
            st["error"] = "оборвалось (процесс исчез) — смотри журнал"
            jsave(_p(rid, "json"), st, 0o600)
    out = {k: st.get(k) for k in ("id", "title", "state", "rc", "started", "finished", "error")}
    out["kind"] = "bg"
    if lines:
        try:
            with open(_p(rid, "log"), errors="replace") as f:
                out["log"] = [ANSI.sub("", ln.rstrip("\n")) for ln in f.readlines()[-lines:]]
        except OSError:
            out["log"] = []
    return out


def recent(n=20):
    if not os.path.isdir(DIR):
        return []
    ids = sorted((f[:-5] for f in os.listdir(DIR) if f.endswith(".json")), reverse=True)[:n]
    out = []
    for rid in ids:
        try:
            out.append(get(rid, lines=0))
        except Fail:
            pass
    return out


def prune():
    ids = sorted({f.split(".")[0] for f in os.listdir(DIR)}, reverse=True) if os.path.isdir(DIR) else []
    for rid in ids[KEEP:]:
        if jload(os.path.join(DIR, rid + ".json"), {}).get("state") == "running":
            continue
        for ext in ("json", "log"):
            try:
                os.unlink(os.path.join(DIR, "%s.%s" % (rid, ext)))
            except OSError:
                pass


HUB_ID = "PCS_HUB_SERVER"       # хаб передаёт номер сервера: подсказка — та, что набирают на хосте


def hint(rid):
    """Как посмотреть снова: на хосте (там хаб) — pcs job; на сервере — с хоста pcs job --server N,
    там же — pcs-node bg."""
    if os.path.isdir("/etc/pve"):
        return "pcs job %s" % rid
    if os.environ.get(HUB_ID):
        return "pcs job --server %s %s (с хоста)" % (os.environ[HUB_ID], rid)
    return "pcs-node bg %s" % rid


def tail(path, getstate, done_states=("done", "failed"), poll=0.5, out=None, sleep=time.sleep):
    """Печатать новое из журнала, пока getstate() не скажет «закончилось». → последнее состояние."""
    out = out or sys.stdout
    pos = 0
    while True:
        st = getstate()
        try:
            with open(path, "rb") as f:
                f.seek(pos)
                chunk = f.read()
                pos = f.tell()
        except OSError:
            chunk = b""
        if chunk:
            out.write(chunk.decode("utf-8", "replace"))
            out.flush()
        if st.get("state") in done_states:
            return st
        sleep(poll)


def follow(rid, out=None, sleep=time.sleep):
    """Показывать журнал фонового запуска до конца. Ctrl+C — перестать смотреть (команда идёт).
    → код команды (130 — смотреть перестали, 1 — оборвалось)."""
    out = out or sys.stdout
    st = get(rid, lines=0)
    out.write("  ▸ в фоне: %s\n    связь оборвётся — не страшно, команда доработает; снова посмотреть: %s\n\n"
              % (st["title"], hint(rid)))
    out.flush()
    try:
        st = tail(_p(rid, "log"), lambda: get(rid, lines=0), out=out, sleep=sleep)
    except KeyboardInterrupt:
        out.write("\n  ▸ перестал смотреть; команда идёт дальше: %s\n" % hint(rid))
        return 130
    if st.get("error"):
        out.write("  ✗ %s\n" % st["error"])
    return st["rc"] if isinstance(st.get("rc"), int) else 1


STATE = {"running": "идёт", "done": "готово", "failed": "ошибка"}


def main(argv):
    """`pcs-node bg [ID]` — список фоновых запусков или журнал одного (идёт — смотреть до конца)."""
    import json
    as_json = "--json" in argv
    argv = [a for a in argv if a != "--json"]
    if not argv:
        lst = recent()
        if as_json:
            print(json.dumps({"ok": True, "data": lst}, ensure_ascii=False))
        elif not lst:
            print("  фоновых запусков нет")
        for d in [] if as_json else lst:
            print("  %s  %-10s %s%s" % (d["id"], STATE.get(d["state"], d["state"]), d["title"],
                                         ("  ✗ " + d["error"]) if d.get("error") else ""))
        return 0
    rid = argv[0]
    if as_json:
        print(json.dumps({"ok": True, "data": get(rid)}, ensure_ascii=False))
        return 0
    d = get(rid, lines=0)
    if d["state"] == "running" and sys.stdout.isatty():
        return follow(rid)
    for ln in get(rid, lines=200)["log"]:
        print(ln)
    return 0 if d["state"] == "done" else 1
