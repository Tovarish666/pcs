"""Мелочи, нужные всем частям: команды, файлы, журнал, блокировка, вывод --json."""
import fcntl
import json
import os
import subprocess
import sys
import time


class Fail(Exception):
    """Ожидаемая ошибка: показывается человеку одной строкой, без трассы."""


class Busy(Fail):
    """Занято другой командой той же части — повторить позже."""


def sh(*cmd, check=True, timeout=None, env=None, input=None):
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, env=env, input=input)
    except FileNotFoundError:
        raise Fail("нет программы %s" % cmd[0])
    except subprocess.TimeoutExpired:
        raise Fail("%s: не уложилось в %sс" % (" ".join(cmd[:3]), timeout))
    if check and r.returncode != 0:
        raise Fail("%s: %s" % (" ".join(cmd), (r.stderr or r.stdout).strip()[:300]))
    return r


def nsh(ns, *args, check=True):
    """ip -n NS … — команда ip внутри netns."""
    return sh("ip", "-n", ns, *args, check=check)


def nsx(ns, *cmd, check=True):
    """Любая команда внутри netns."""
    return sh("ip", "netns", "exec", ns, *cmd, check=check)


def rd(path, default=""):
    try:
        with open(path) as f:
            return f.read().strip()
    except OSError:
        return default


def wr(path, value, mode=None):
    with open(path, "w") as f:
        f.write(value if value.endswith("\n") else value + "\n")
    if mode is not None:
        os.chmod(path, mode)


def jload(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def jsave(path, data, mode=0o600):
    """Атомарно: сначала .tmp, потом rename — полузаписанного файла не бывает."""
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=1, ensure_ascii=False)
    os.chmod(tmp, mode)
    os.replace(tmp, path)


def merge(base, over):
    """Глубокое слияние словарей: значения over поверх base."""
    out = dict(base)
    for k, v in (over or {}).items():
        out[k] = merge(base[k], v) if isinstance(v, dict) and isinstance(base.get(k), dict) else v
    return out


class Log:
    """Печать человеку и строка в журнал части: Log("proxyveth")("текст")."""

    def __init__(self, part, logdir=None):
        self.path = os.path.join(logdir or "/var/log/%s" % part, part + ".log")

    def __call__(self, msg):
        if not QUIET[0]:
            print("  " + msg, flush=True)
        try:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            with open(self.path, "a") as f:
                f.write("%s %s\n" % (time.strftime("%F %T"), msg))
        except OSError:
            pass


QUIET = [False]           # при --json печать человеку выключена (см. run_cli)


def lock(path, wait=300, what="команда"):
    """Одна правка за раз. Держать возвращённый файл, пока работа не кончится."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    f = open(path, "w")
    t0, said = time.time(), False
    while True:
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return f
        except OSError:
            if time.time() - t0 >= wait:
                raise Busy("занято: идёт другая %s — повтори позже" % what)
            if not said and wait and not QUIET[0]:
                print("  … жду, пока закончится идущая %s" % what, flush=True)
                said = True
            time.sleep(1)


def need_root():
    if os.geteuid() != 0:
        raise Fail("нужен root")


def run_cli(fn, args, as_json=False):
    """Единый конверт для всех команд всех частей (docs/ARCHITECTURE.md, «Команды»).

    fn(args) возвращает (код, данные). С --json печатается
    {"ok": true, "data": …} или {"ok": false, "error": "…"}; код возврата 0/1.
    Без --json данные печатает сама команда, здесь — только ошибка.
    """
    QUIET[0] = as_json
    try:
        rc, data = fn(args)
    except KeyboardInterrupt:
        rc, data = 130, None
        if as_json:
            print(json.dumps({"ok": False, "error": "прервано"}, ensure_ascii=False))
        return rc
    except Fail as e:
        if as_json:
            print(json.dumps({"ok": False, "error": str(e)}, ensure_ascii=False))
        else:
            print("  ✗ %s" % e, file=sys.stderr)
        return 1
    if as_json:
        print(json.dumps({"ok": rc == 0, "data": data}, ensure_ascii=False))
    return rc
