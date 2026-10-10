"""Фоновые задания хаба (§9): создать ВМ, поставить mp.space, обновить код.

Задание — отдельный процесс `pcs job run ID` в своём юните systemd (pcs-job-ID):
переживает перезапуск панели и обрыв браузера. В /var/lib/pcs/jobs:
  ID.json    состояние: {"id", "kind", "title", "state": running|done|failed, "started",
             "finished", "pid", "result", "error"}
  ID.log     всё, что задание напечатало (журнал для панели)
  ID.params  параметры (там бывают пароли): 600, удаляется, как только задание их прочло.
"""
import os
import re
import secrets
import shutil
import subprocess
import sys
import time
import traceback

from ..core.util import Fail, jload, jsave

DIR = os.environ.get("PCS_JOBS", "/var/lib/pcs/jobs")
KEEP = 50
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.realpath(__file__))))
ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")


def _p(jid, ext):
    if not re.fullmatch(r"[a-z0-9-]{6,40}", jid or ""):
        raise Fail("нет такого задания: %s" % jid)
    return os.path.join(DIR, "%s.%s" % (jid, ext))


def _save(st):
    jsave(_p(st["id"], "json"), st, 0o600)


def launch(jid):
    """Запустить исполнителя. Через systemd-run — чтобы перезапуск pcs-web его не убил."""
    log = _p(jid, "log")
    exe = [sys.executable, os.path.join(ROOT, "bin", "pcs"), "job", "run", jid]
    env = {"PYTHONUNBUFFERED": "1", "PCS_JOBS": DIR}
    for k in ("PCS_ETC", "PCS_RUN", "PCS_LOG"):
        if os.environ.get(k):
            env[k] = os.environ[k]
    if os.geteuid() == 0 and shutil.which("systemd-run"):
        r = subprocess.run(["systemd-run", "--unit", "pcs-job-%s" % jid, "--collect", "--quiet",
                            "-p", "StandardOutput=append:%s" % log, "-p", "StandardError=append:%s" % log]
                           + ["--setenv=%s=%s" % kv for kv in env.items()] + exe,
                           capture_output=True, text=True)
        if r.returncode == 0:
            return
    with open(log, "a") as out:
        subprocess.Popen(exe, stdout=out, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                         start_new_session=True, env=dict(os.environ, **env))


def start(kind, params, title="", target=None, launcher=None):
    """Завести задание и запустить. → id. target — чьё (номер сервера, «host», «all»)."""
    os.makedirs(DIR, mode=0o700, exist_ok=True)
    prune()
    jid = "%s-%s" % (time.strftime("%Y%m%d-%H%M%S"), secrets.token_hex(2))
    jsave(_p(jid, "params"), params or {}, 0o600)
    fd = os.open(_p(jid, "log"), os.O_CREAT | os.O_WRONLY | os.O_APPEND, 0o600)
    os.close(fd)
    _save({"id": jid, "kind": kind, "title": title or kind, "target": None if target is None else str(target),
           "state": "running", "started": int(time.time()),
           "finished": None, "pid": None, "result": None, "error": None})
    (launcher or launch)(jid)
    return jid


def run(jid, kinds):
    """Исполнитель (внутри `pcs job run ID`). Печать уходит в журнал задания."""
    st = jload(_p(jid, "json"), None)
    if not st:
        raise Fail("нет такого задания: %s" % jid)
    params = jload(_p(jid, "params"), {})
    try:
        os.unlink(_p(jid, "params"))
    except OSError:
        pass
    st["pid"] = os.getpid()
    _save(st)
    fn = kinds.get(st["kind"])
    try:
        if not fn:
            raise Fail("неизвестный вид задания: %s" % st["kind"])
        st["result"] = fn(params)
        st["state"] = "done"
        print("\n  ✓ готово", flush=True)
    except Fail as e:
        st["state"], st["error"] = "failed", str(e)
        print("\n  ✗ %s" % e, flush=True)
    except KeyboardInterrupt:
        st["state"], st["error"] = "failed", "прервано"
    except Exception as e:                       # noqa: BLE001 — задание не должно висеть «running»
        st["state"], st["error"] = "failed", "внутренняя ошибка: %s: %s" % (type(e).__name__, e)
        print(traceback.format_exc(), flush=True)
    st["finished"] = int(time.time())
    _save(st)
    return 0 if st["state"] == "done" else 1


def alive(pid):
    try:
        os.kill(int(pid), 0)
        return True
    except (OSError, ValueError, TypeError):
        return False


def get(jid, lines=400):
    """{"id", "kind", "title", "state", "log": [...], "result", "error", "started", "finished"}."""
    st = jload(_p(jid, "json"), None)
    if not st:
        raise Fail("нет такого задания: %s" % jid)
    if st.get("state") == "running":
        gone = (st.get("pid") and not alive(st["pid"])) or \
               (not st.get("pid") and time.time() - st.get("started", 0) > 120)
        if gone:
            st.update(state="failed", error="задание оборвалось (процесс исчез) — смотри журнал", finished=int(time.time()))
            _save(st)
    try:
        with open(_p(jid, "log"), errors="replace") as f:
            log = [ANSI.sub("", ln.rstrip("\n")) for ln in f.readlines()[-lines:]]
    except OSError:
        log = []
    out = {k: st.get(k) for k in ("id", "kind", "title", "target", "state", "result", "error", "started", "finished")}
    out["log"] = log
    return out


def recent(n=20):
    if not os.path.isdir(DIR):
        return []
    ids = sorted((f[:-5] for f in os.listdir(DIR) if f.endswith(".json")), reverse=True)[:n]
    out = []
    for jid in ids:
        try:
            d = get(jid, lines=0)
            d.pop("log", None)
            out.append(d)
        except Fail:
            pass
    return out


def prune():
    ids = sorted({f.split(".")[0] for f in os.listdir(DIR)}, reverse=True) if os.path.isdir(DIR) else []
    for jid in ids[KEEP:]:
        st = jload(os.path.join(DIR, jid + ".json"), {})
        if st.get("state") == "running":
            continue
        for ext in ("json", "log", "params"):
            try:
                os.unlink(os.path.join(DIR, "%s.%s" % (jid, ext)))
            except OSError:
                pass


def busy(kind=None, target=None):
    """Уже идёт задание (этого вида, для этой цели)? → id или None."""
    for d in recent(KEEP):
        if d["state"] == "running" and kind in (None, d["kind"]) and \
                (target is None or d.get("target") in (str(target), "all")):
            return d["id"]
    return None
