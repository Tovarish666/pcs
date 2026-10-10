"""Вывод человеку и ввод: цвета, ✓ ⚠ →, вопросы, подтверждение номером сервера.

Ввод идёт через INPUT/SECRET, а «есть ли живой человек» — через INTERACTIVE:
так меню и вопросы проверяются тестами без терминала.
"""
import getpass
import os
import sys
import time

from ..core import util

LOG = os.environ.get("PCS_LOG", "/var/log/pcs/pcs.log")
INPUT = [input]
SECRET = [getpass.getpass]
INTERACTIVE = [None]            # None — смотреть на терминал; True/False — подменено


def interactive():
    if INTERACTIVE[0] is not None:
        return INTERACTIVE[0]
    return sys.stdin.isatty() and sys.stdout.isatty()


def palette(on):
    codes = dict(g="\033[32m", r="\033[31m", y="\033[33m", c="\033[36m", m="\033[35m", bl="\033[34m",
                 b="\033[1m", d="\033[2m", inv="\033[7m", x="\033[0m")
    return {k: (v if on else "") for k, v in codes.items()}


C = palette(sys.stdout.isatty() and not os.environ.get("NO_COLOR"))


def log(msg):
    try:
        os.makedirs(os.path.dirname(LOG), exist_ok=True)
        with open(LOG, "a") as f:
            f.write("%s %s\n" % (time.strftime("%F %T"), msg))
    except OSError:
        pass


def _say(icon, color, msg, tag, err=False):
    log("%s %s" % (tag, msg))
    if util.QUIET[0]:
        return
    print("  %s%s%s %s" % (C[color], icon, C["x"], msg), file=sys.stderr if err else sys.stdout, flush=True)


def ok(msg):
    _say("✓", "g", msg, "OK  ")


def warn(msg):
    _say("⚠", "y", msg, "WARN")


def bad(msg):
    _say("✗", "r", msg, "FAIL", err=True)


def step(msg):
    _say("→", "d", msg, "STEP")


def info(msg):
    _say("·", "c", msg, "INFO")


def hdr(msg):
    log("==== " + msg)
    if not util.QUIET[0]:
        print("\n%s── %s ──%s" % (C["b"], msg, C["x"]), flush=True)


def say(text=""):
    if not util.QUIET[0]:
        print(text, flush=True)


def ask(q, default="", secret=False):
    """Вопрос человеку. Без терминала — default (скрипты задают всё флагами)."""
    if not interactive():
        return default
    if secret:
        return SECRET[0]("  %s?%s %s: " % (C["c"], C["x"], q))
    hint = (" %s[%s]%s" % (C["d"], default, C["x"])) if default else ""
    a = INPUT[0]("  %s?%s %s%s: " % (C["c"], C["x"], q, hint))
    return a.strip() or default


def confirm(q, default=False):
    if not interactive():
        return default
    a = ask(q + " (да/нет)", "да" if default else "нет").lower()
    return a in ("да", "д", "yes", "y")


def confirm_id(what, expect, label="номер сервера"):
    """Опасное — только после ввода номера сервера (или имени хоста)."""
    if not interactive():
        return False
    print("\n  %s%s⚠ %s%s" % (C["b"], C["r"], what, C["x"]), flush=True)
    a = INPUT[0]("  %s?%s Введи %s %s%s%s, чтобы подтвердить (Enter — отмена): "
                 % (C["c"], C["x"], label, C["b"], expect, C["x"])).strip()
    return a == str(expect)


def secret_twice(q):
    """Пароль: в терминале — дважды, без терминала — строка со stdin."""
    if not interactive():
        return sys.stdin.readline().rstrip("\r\n") if not sys.stdin.isatty() else ""
    a = ask(q, secret=True)
    if not a:
        return ""
    if ask("Ещё раз", secret=True) != a:
        raise util.Fail("пароли не совпали — ничего не поменял")
    return a
