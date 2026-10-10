"""hivelink: пути, настройки, состояние прохода, журнал, apt."""
import json
import os
import threading
import time

from ..core.util import Fail, Log, jload, jsave, rd, sh, wr

PART = "hivelink"
ETC = "/etc/hivelink"
CONF = ETC + "/config.json"
VAR = "/var/lib/hivelink"
CONFMAP = VAR + "/confmap.json"     # модель → USB-конфигурация, дающая нужный драйвер
FLAGS = VAR + "/flags.json"         # что установщик поменял в системе — вернуть при снятии
RUN = "/run/hivelink"
LOCK = RUN + "/lock"
STATE = RUN + "/state.json"         # счётчики попыток: до перезагрузки
LOGDIR = "/var/log/hivelink"
CACHE = "/var/cache/hivelink"
FILES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "files")
# Скрипт mp.space, который сам берёт адрес у каждого воткнутого модема (udhcpc)
MP_SETUP = "/usr/local/bin/modem-interface-setup.sh"

DEFAULTS = {
    "net": "auto",                  # auto | on | off — DHCP и маршруты модемов (auto: off при mp.space)
    "prefer_driver": "rndis_host",  # целевой сетевой драйвер
    "fallback_drivers": ["cdc_ether"],
    "bad_drivers": ["huawei_cdc_ncm", "cdc_mbim", "cdc_ncm", "qmi_wwan"],   # raw-IP: без DHCP и web-API
    "cfg_max_tries": 8,             # перебор USB-конфигураций
    "zerocd_max_tries": 3,          # usb_modeswitch до пере-enumerate
    "nodrv_grace": 3,               # проходов ждать привязки драйвера
    "dhcp_max_tries": 10,
    "hilink_every": 4,              # web-API — раз в столько проходов
    "hilink_concurrency": 8,
    "auto_dataswitch": True,
    "auto_reconnect": True,
    "reconnect_cooldown": 180,      # с между дозвонами одного модема
    "usb_autosuspend_off": True,
    "quiet_repeat": 300,            # с: одинаковое сообщение не чаще
    "rx_urb_size": 16384,           # фикс приёма rndis_host; 0 — выключен
}

log = Log(PART)
_once = threading.Lock()


def load_conf():
    try:
        with open(CONF) as f:
            raw = json.load(f)
    except FileNotFoundError:
        raw = {}
    except (OSError, ValueError) as e:
        raise Fail("%s не читается (%s) — поправь или удали: вернутся умолчания" % (CONF, e))
    if not isinstance(raw, dict):
        raise Fail("%s: ждал объект JSON — поправь или удали" % CONF)
    conf = dict(DEFAULTS)
    for k, v in raw.items():
        if k in DEFAULTS and type(v) is type(DEFAULTS[k]):
            conf[k] = v
    if conf["net"] not in ("auto", "on", "off"):
        conf["net"] = "auto"
    return conf


def net_mode(conf):
    """on — DHCP и маршруты модемов делает hivelink; off — их делает другой (mp.space)."""
    if conf["net"] == "auto":
        return "off" if os.path.exists(MP_SETUP) else "on"
    return conf["net"]


def load_state():
    st = jload(STATE, {})
    if not isinstance(st, dict):
        st = {}
    for k in ("cnt", "probe", "msg", "dial"):
        if not isinstance(st.get(k), dict):
            st[k] = {}
    if not isinstance(st.get("cycle"), int):
        st["cycle"] = 0
    return st


def save_state(st):
    os.makedirs(RUN, exist_ok=True)
    jsave(STATE, st)


def log_once(st, msg, repeat=300, key=None):
    """Одно и то же от сорока модемов не забивает журнал: не чаще раза в repeat секунд."""
    key = (key or msg)[:120]
    now = int(time.time())
    with _once:
        if now - st["msg"].get(key, 0) < repeat:
            return
        st["msg"][key] = now
    log(msg)


def rotate_log(limit=5 << 20):
    path = log.path
    try:
        if os.path.getsize(path) > limit:
            os.replace(path, path + ".1")
    except OSError:
        pass


def put(path, text, mode=0o644):
    """Записать, только если содержимое другое. True — файл поменялся."""
    if os.path.exists(path) and rd(path) == text.strip():
        return False
    os.makedirs(os.path.dirname(path), exist_ok=True)
    wr(path, text, mode)
    return True


def flags():
    f = jload(FLAGS, {})
    return f if isinstance(f, dict) else {}


def set_flag(key, value):
    f = flags()
    f[key] = value
    jsave(FLAGS, f, mode=0o644)


def tail(text, n=6):
    lines = [ln for ln in (text or "").strip().splitlines() if ln.strip()]
    return " | ".join(lines[-n:])[:600]


def installed_pkg(name):
    r = sh("dpkg-query", "-W", "-f=${Status}", name, check=False)
    return r.returncode == 0 and "ok installed" in r.stdout


def apt_install(pkgs, log=log):
    """apt-get install только недостающего. Ошибку apt не глушим — она уходит в Fail."""
    missing = [p for p in pkgs if not installed_pkg(p)]
    if not missing:
        return []
    log("ставлю: %s" % " ".join(missing))
    env = dict(os.environ, DEBIAN_FRONTEND="noninteractive")
    cmd = ("apt-get", "-o", "DPkg::Lock::Timeout=600", "-y", "--no-install-recommends", "install", *missing)
    r = sh(*cmd, check=False, env=env, timeout=1800)
    if r.returncode != 0:
        log("apt-get install не прошёл — обновляю списки пакетов и повторяю")
        u = sh("apt-get", "-o", "DPkg::Lock::Timeout=600", "update", check=False, env=env, timeout=600)
        if u.returncode != 0:
            log("⚠ apt-get update: %s" % tail(u.stderr or u.stdout, 3))
        r = sh(*cmd, check=False, env=env, timeout=1800)
    if r.returncode != 0:
        raise Fail("apt-get install %s: %s" % (" ".join(missing), tail(r.stderr or r.stdout)))
    return missing
