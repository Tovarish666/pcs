"""hivelink: пути, настройки, состояние прохода, журнал, apt."""
import json
import os
import threading
import time

from ..core.util import Fail, Log, jload, jsave, rd, sh, wr

PART = "hivelink"
ETC = "/etc/hivelink"
CONF = ETC + "/config.json"
# Выключенные модемы по USB-порту (hivelink off): два модема с одним N мешают друг другу —
# лишний отцепляется от USB (authorized=0) и не возвращается после перезагрузки.
OFF = ETC + "/off.json"
VAR = "/var/lib/hivelink"
NUMBERS = VAR + "/numbers.json"     # номера SIM (hivelink number N): {N: {number, operator, t, …}}
CONFMAP = VAR + "/confmap.json"     # модель → USB-конфигурация, дающая нужный драйвер
FLAGS = VAR + "/flags.json"         # что установщик поменял в системе — вернуть при снятии
RUN = "/run/hivelink"
LOCK = RUN + "/lock"
STATE = RUN + "/state.json"         # счётчики попыток: до перезагрузки
LOGDIR = "/var/log/hivelink"
CACHE = "/var/cache/hivelink"
FILES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "files")
# Скрипт mp.space, который сам берёт адрес у каждого воткнутого модема (udhcpc), и его юнит
MP_SETUP = "/usr/local/bin/modem-interface-setup.sh"
MP_UNIT = "modem-hotplug@%s.service"

DEFAULTS = {
    "net": "auto",                  # auto | on | off — DHCP и маршруты модемов (auto: off при mp.space)
    "prefer_driver": "rndis_host",  # целевой сетевой драйвер
    "fallback_drivers": ["cdc_ether"],
    "bad_drivers": ["huawei_cdc_ncm", "cdc_mbim", "cdc_ncm", "qmi_wwan"],   # raw-IP: без DHCP и web-API
    "cfg_max_tries": 8,             # перебор USB-конфигураций
    "zerocd_max_tries": 3,          # usb_modeswitch до пере-enumerate
    "zerocd_parallel": 8,           # usb_modeswitch разом (на разные модемы)
    "zerocd_wait": 25,              # с: повтор для того же модема не раньше (он переключается)
    "nodrv_grace": 3,               # проходов ждать привязки драйвера
    "dhcp_max_tries": 10,           # подряд; потом — раз в dhcp_retry_every (не бросаем навсегда)
    "dhcp_retry_every": 300,
    "mp_grace": 45,                 # с: сеть у mp.space — модем без адреса дольше — его настройку заново
    "mp_retry_gap": 60,             # с между такими перезапусками (после dhcp_max_tries — dhcp_retry_every)
    "hilink_every": 4,              # web-API — раз в столько проходов
    "hilink_concurrency": 8,
    "auto_dataswitch": True,
    "data_grace": 45,               # с: данные выключены дольше — включить (меньше — это реконнект mp.space)
    "auto_reconnect": True,
    "reconnect_cooldown": 180,      # с между попытками починки одного модема
    # лестница починки: дозвон → откл/вкл → перерегистрация в сети → перезагрузка модема
    "auto_heal": True,
    "stuck_grace": 180,             # с: «подключается»/«отключается» дольше — завис
    "heal_reboot_after": 600,       # с без связи — перезагрузить модем
    "heal_reboot_every": 1800,      # с: перезагрузка одного модема не чаще
    "radio_grace": 90,              # с: IMEI пуст дольше — радиочасть зависла (не загрузка)
    "radio_reboot_wait": 120,       # с: после перезагрузки модем должен переподключиться за это время
    "netcheck_every": 300,          # с: у «онлайн» — ходит ли трафик (TCP к netcheck_host); 0 — нет
    "netcheck_fails": 3,            # подряд неудач — «онлайн, но трафика нет»
    "netcheck_host": "1.1.1.1",
    "api_dead_after": 600,          # с: web-API молчит дольше — заново подключить USB
    "api_reenum_every": 1800,
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


def load_off():
    """{USB-порт: {"t": когда, "was": что это было}}."""
    d = jload(OFF, {})
    return {str(k): v if isinstance(v, dict) else {} for k, v in d.items()} if isinstance(d, dict) else {}


def save_off(d):
    os.makedirs(ETC, exist_ok=True)
    jsave(OFF, d, mode=0o644)


def load_state():
    st = jload(STATE, {})
    if not isinstance(st, dict):
        st = {}
    for k in ("cnt", "probe", "msg", "heal", "hubs"):
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
