"""hivelink: один проход приведения настоящих модемов к рабочему состоянию.

Гоняется таймером (15 с), udev-событием и вручную — результат один и тот же.
Шаги изолированы: сбой одного не роняет остальные. Каждый шаг смотрит только на
настоящие модемы (usb.devices()): виртуальные proxyveth на dummy_hcd не видны.

  1. питание USB      автосуспенд роняет хабы и модемы — выключаем
  2. Zero-CD          12d1:1f01 и родня — usb_modeswitch, при отказе пере-enumerate
  3. USB-конфигурация ядро часто выбирает NCM (wwanN без DHCP) — перебираем до rndis_host
  4. линк и адрес     поднять, DHCP через networkd (если сеть модемов наша)
  5. маршруты         таблица 2000+N и правило 31000+N на модем; уборка — только своего
  6. HiLink           мобильные данные и лестница починки (раз в hilink_every проходов, параллельно)
  7. фикс приёма      проверка, что в памяти исправленный rndis_host

Модем без связи не ждёт команды человека — см. heal(): чем дольше нет связи, тем сильнее
средство, вплоть до перезагрузки модема (с ограничением по частоте).
"""
import os
import shutil
import time
from concurrent.futures import ThreadPoolExecutor

from ..core.util import Fail, jload, jsave, rd, sh, wr
from . import common, hilink, rndis, route, usb
from .common import log, log_once


def ok_drivers(conf):
    return [conf["prefer_driver"]] + list(conf["fallback_drivers"])


def modems(conf, addr_text=None):
    """Сетевые интерфейсы настоящих модемов: [{iface, dev, port, driver, addr, n}]."""
    if addr_text is None:
        addr_text = sh("ip", "-4", "-o", "addr", "show", check=False).stdout
    addrs = route.parse_addrs(addr_text)
    out = []
    for d in usb.devices():
        for ifn in usb.ifaces(d):
            ip, n = route.modem_addr(addrs.get(ifn, []))
            out.append({"iface": ifn, "dev": d, "port": usb.port(d), "driver": usb.iface_driver(ifn),
                        "addr": ip, "n": n})
    return out, addrs


def want(mods, addrs, conf, st=None):
    """{N: (iface, адрес)} — каким модемам нужна маршрутизация. Подсеть, занятая сетью
    машины или виртуальным модемом, — пропуск; два модема в одной подсети — первый."""
    busy = route.busy_octets(addrs, {m["iface"] for m in mods})
    out = {}
    for m in sorted(mods, key=lambda m: m["port"]):
        n = m["n"]
        if n is None or m["driver"] not in ok_drivers(conf):
            continue
        if n in busy:
            if st is not None:
                log_once(st, "N=%d (%s): подсеть 192.168.%d.0/24 занята интерфейсом %s — модем пропущен, "
                         "смени подсеть модема" % (n, m["iface"], n, busy[n]), conf["quiet_repeat"])
            continue
        if n in out:
            if st is not None:
                log_once(st, "N=%d: %s и %s в одной подсети — %s пропущен, смени подсеть модема"
                         % (n, out[n][0], m["iface"], m["iface"]), conf["quiet_repeat"])
            continue
        out[n] = (m["iface"], m["addr"])
    return out


# ── шаги ────────────────────────────────────────────────────────────────────

def step_power(conf, st):
    if not conf["usb_autosuspend_off"]:
        return
    for d in usb.devices() + usb.hubs():
        path = os.path.join(d, "power", "control")
        if os.path.exists(path) and usb.attr(d, "power/control") != "on":
            try:
                wr(path, "on")
            except OSError:
                pass


def step_off(conf, st, devs, off=None):
    """Выключенные модемы (hivelink off): отцепить от USB, если снова подключились (перезагрузка,
    переткнули). → устройства, с которыми работают остальные шаги: без выключенных и отцепленных."""
    off = common.load_off() if off is None else off
    out = []
    for d in devs:
        p = usb.port(d)
        if p in off:
            if usb.authorized(d) and usb.set_authorized(d, False):
                log("%s: модем выключен (hivelink off) — отцеплен от USB; включить: hivelink on %s" % (p, p))
            continue
        if usb.authorized(d):           # отцеплен кем-то ещё (reset идёт) — не трогаем
            out.append(d)
    return out


def step_zerocd(conf, st, devs, now=None):
    """Zero-CD (12d1:1f01 и родня) → рабочий режим: usb_modeswitch шлёт HuaweiNewMode в
    mass-storage интерфейс ТЕКУЩЕЙ конфигурации. Конфигурацию не меняем: модем в cfg 2 (MBIM —
    её выбирает ядро) на запись cfg 1 сам переподключается, ядро снова ставит cfg 2 — и так
    по кругу (SpbVL-1, октябрь 2026). А CD-интерфейс есть и в cfg 2. Модемы — параллельно:
    сорок по одному — минуты. Не переключился за zerocd_max_tries — пере-enumerate."""
    if not shutil.which("usb_modeswitch"):
        if any(usb.pid_mode(usb.attr(d, "idProduct")) == "cdrom" for d in devs):
            log_once(st, "модем в Zero-CD, а usb_modeswitch нет — hivelink install поставит", conf["quiet_repeat"])
        return
    cnt = st["cnt"]
    now = time.time() if now is None else now
    jobs = []
    for d in devs:
        p, pid = usb.port(d), usb.attr(d, "idProduct")
        key = "zerocd." + p
        if usb.pid_mode(pid) != "cdrom" or usb.claimed(d):
            for k in (key, "zcfg." + p, "zerocd_t." + p):
                cnt.pop(k, None)
            continue
        # Команда ушла — модему нужно 5–10 с, чтобы отключиться и прийти в новом режиме. Проходы
        # (udev на каждое событие, таймер) чаще: не повторять раньше zerocd_wait, иначе за секунды
        # набираются «неудачи» и USB дёргается посреди переключения.
        if now - cnt.get("zerocd_t." + p, 0) < conf["zerocd_wait"]:
            continue
        ms = usb.ms_iface(d)
        if ms is None:
            # В этой конфигурации CD-части нет — только тогда пробуем cfg 1, и не каждый проход:
            # иначе модем, который на смену конфигурации переподключается, крутится вечно
            cur, tot = usb.config(d)
            n = cnt.get("zcfg." + p, 0)
            cnt["zcfg." + p] = n + 1
            if cur is not None and cur != 1 and tot > 1 and n % 4 == 0:
                log("%s 12d1:%s Zero-CD без CD-интерфейса в cfg %d/%d — ставлю cfg 1" % (p, pid, cur, tot))
                usb.set_config(d, 1)
            continue
        tries = cnt.get(key, 0)
        cnt["zerocd_t." + p] = now
        if tries >= conf["zerocd_max_tries"]:
            log("%s 12d1:%s не переключается за %d попыток — пере-enumerate" % (p, pid, tries))
            usb.reenumerate(d)
            cnt[key] = 0
            continue
        bus, num = usb.attr(d, "busnum"), usb.attr(d, "devnum")
        if bus and num:
            jobs.append((p, pid, bus, num, ms, tries))
            cnt[key] = tries + 1
    if not jobs:
        return
    log("Zero-CD: переключаю %d (usb_modeswitch, по %d разом)" % (len(jobs), conf["zerocd_parallel"]))

    def switch(j):
        p, pid, bus, num, ms, tries = j
        r = sh("usb_modeswitch", "-v", "12d1", "-p", pid, "-b", bus, "-g", num,
               "-c", os.path.join(common.FILES, "zerocd.conf"), "-i", str(ms), check=False, timeout=30)
        out = (r.stdout or "") + (r.stderr or "")
        if "successfully sent" not in out and "No devices in default mode" not in out:   # второе — уже ушёл
            last = [ln.strip() for ln in out.splitlines() if ln.strip()][-1:] or ["нет ответа"]
            log("%s 12d1:%s usb_modeswitch не отправил (попытка %d): %s" % (p, pid, tries + 1, last[0][:120]))

    with ThreadPoolExecutor(max_workers=max(1, conf["zerocd_parallel"])) as ex:
        list(ex.map(switch, jobs))


def load_confmap():
    m = jload(common.CONFMAP, {})
    return m if isinstance(m, dict) else {}


def best_probed(probes, conf):
    fallback = None
    for cfg in sorted(probes, key=int):
        if probes[cfg] == conf["prefer_driver"]:
            return int(cfg)
        if probes[cfg] in conf["fallback_drivers"] and fallback is None:
            fallback = int(cfg)
    return fallback


def step_usbcfg(conf, st, devs):
    """Ядро выбирает USB-конфигурацию само и при массовом включении промахивается: NCM
    (huawei_cdc_ncm, wwanN без DHCP и web-API) вместо RNDIS/ECM. Это стабильно — само не
    разойдётся. Перебираем конфигурации, запоминаем, какой драйвер даёт каждая, садимся на
    prefer_driver (или fallback). Удачная запоминается по модели+прошивке (idProduct:bcdDevice):
    одинаковые модемы потом правятся сразу, без перебора."""
    ok, bad = ok_drivers(conf), conf["bad_drivers"]
    prefer = conf["prefer_driver"]
    cnt, probes_all = st["cnt"], st["probe"]
    cmap = load_confmap()
    cmap0 = dict(cmap)
    for d in devs:
        p, pid = usb.port(d), usb.attr(d, "idProduct")
        mode = usb.pid_mode(pid)
        if not pid or mode == "cdrom" or usb.claimed(d):
            continue
        if mode in ("stick", "stick-serial"):
            log_once(st, "%s 12d1:%s — stick-прошивка, HiLink'ом не станет: нужна перепрошивка, "
                     "hivelink его не трогает" % (p, pid), 86400)
            continue
        num = usb.attr(d, "devnum")
        if cnt.get("gaveup." + p) == num:     # пере-enumerate (новый devnum) — пробуем снова
            continue
        cnt.pop("gaveup." + p, None)
        key = "%s:%s" % (pid, usb.attr(d, "bcdDevice") or "0")
        cur, tot = usb.config(d)
        if cur is None:
            continue
        probes = probes_all.setdefault(p, {})
        drv = usb.net_driver(d, ok, bad)
        if not drv:
            # Драйвер не привязался: ждём nodrv_grace проходов, потом считаем конфигурацию
            # пустой (serial-only) и идём к следующей — иначе модем провисит вечно.
            nodrv = cnt.get("nodrv." + p, 0)
            if nodrv < conf["nodrv_grace"] or tot <= 1:
                cnt["nodrv." + p] = nodrv + 1
                continue
            cnt["nodrv." + p] = 0
            probes[str(cur)] = "none"
            nxt = cur % tot + 1
            log("%s: cfg %d не даёт сетевого драйвера — пробую cfg %d" % (p, cur, nxt))
            usb.set_config(d, nxt)
            continue
        cnt.pop("nodrv." + p, None)
        probes[str(cur)] = drv
        if drv == prefer:
            cmap[key] = cur
            cnt.pop("cfgtry." + p, None)
            probes_all.pop(p, None)
            continue
        if tot <= 1:
            if drv in ok:
                cmap[key] = cur
            else:
                log_once(st, "%s 12d1:%s: единственная USB-конфигурация даёт %s — нужна HiLink-прошивка"
                         % (p, pid, drv), conf["quiet_repeat"])
            continue
        best = cmap.get(key)
        if isinstance(best, int) and best != cur and 1 <= best <= tot:
            log("%s: cfg %d даёт %s; у модели %s нужная cfg %d — переключаю" % (p, cur, drv, key, best))
            usb.set_config(d, best)
            continue
        tries = cnt.get("cfgtry." + p, 0)
        if tries >= conf["cfg_max_tries"]:
            best = best_probed(probes, conf)
            if best and best != cur:
                log("%s: перебор исчерпан, сажусь на лучшую известную cfg %d" % (p, best))
                usb.set_config(d, best)
            elif drv in ok:
                cmap[key] = cur
                log_once(st, "%s 12d1:%s: %s недоступен, остаюсь на %s (cfg %d)" % (p, pid, prefer, drv, cur),
                         conf["quiet_repeat"])
            else:
                cnt["gaveup." + p] = num
                log("%s 12d1:%s: ни одна USB-конфигурация не даёт %s — бросаю (hivelink recfg вернёт)"
                    % (p, pid, "/".join(ok)))
            cnt["cfgtry." + p] = 0
            probes_all.pop(p, None)
            continue
        nxt = cur % tot + 1
        log("%s: cfg %d → %s (цель %s), пробую cfg %d [%d/%d]" % (p, cur, drv, prefer, nxt, tries + 1,
                                                                 conf["cfg_max_tries"]))
        usb.set_config(d, nxt)
        cnt["cfgtry." + p] = tries + 1
    if cmap != cmap0:
        jsave(common.CONFMAP, cmap, mode=0o644)


IFACE_SYSCTL = (("rp_filter", "2"),     # ответ при policy routing приходит не в «ту» таблицу
                ("arp_ignore", "1"),    # у модемов один MAC — ARP строго по интерфейсу
                ("arp_announce", "2"))


def step_link(conf, st, mods):
    """Интерфейс поднять, свои sysctl на нём (глобальные не трогаем), без адреса — DHCP заново."""
    networkd = sh("systemctl", "is-active", "systemd-networkd", check=False).stdout.strip() == "active"
    now, seen = int(time.time()), set()
    for m in mods:
        ifn = m["iface"]
        if m["driver"] not in ok_drivers(conf):
            continue
        for k, v in IFACE_SYSCTL:
            path = "/proc/sys/net/ipv4/conf/%s/%s" % (ifn, k)
            try:
                if os.path.exists(path) and rd(path) != v:
                    wr(path, v)
            except OSError:
                pass
        oper = rd("/sys/class/net/%s/operstate" % ifn)
        if not oper:                    # интерфейс уже пропал
            continue
        if oper == "down":
            sh("ip", "link", "set", ifn, "up", check=False)
        # счётчик — на интерфейс с его ifindex: модем перезагрузился и вернулся под тем же
        # именем — считаем заново (раньше старый счётчик навсегда запрещал ему DHCP)
        key = "dhcp.%s.%s" % (ifn, rd("/sys/class/net/%s/ifindex" % ifn) or "?")
        seen.add(key)
        if m["addr"]:
            st["cnt"].pop(key, None)
            st["cnt"].pop(key + ".t", None)
            continue
        if not networkd:
            log_once(st, "systemd-networkd не запущен — модемам некому дать адрес (hivelink install)",
                     conf["quiet_repeat"])
            continue
        t = st["cnt"].get(key, 0)
        if t >= conf["dhcp_max_tries"] and now - st["cnt"].get(key + ".t", 0) < conf["dhcp_retry_every"]:
            log_once(st, "%s: нет IPv4 после %d попыток DHCP — дальше раз в %d мин"
                     % (ifn, t, conf["dhcp_retry_every"] // 60), conf["quiet_repeat"])
            continue
        st["cnt"][key] = t + 1
        st["cnt"][key + ".t"] = now
        log("%s (%s) без IPv4 — networkctl reconfigure" % (ifn, m["driver"]))
        sh("networkctl", "reconfigure", ifn, check=False)
    for k in [k for k in st["cnt"] if k.startswith("dhcp.") and k.rsplit(".t", 1)[0] not in seen and k not in seen]:
        st["cnt"].pop(k, None)


def step_mp_link(conf, st, mods, now=None, unit_exists=None):
    """Сеть у mp.space: адрес модему даёт его modem-hotplug@ethN (udhcpc), один раз — на событие
    udev. При массовом включении часть запусков кончается «no valid IP obtained» (SpbVL-1,
    23.10: 18 из 40), и модем без адреса до следующего события. Нет IPv4 дольше mp_grace —
    запустить ту же штатную настройку mp.space заново; попытки — с паузой, как у DHCP."""
    now = int(time.time()) if now is None else now
    if unit_exists is None:
        unit_exists = os.path.exists("/etc/systemd/system/modem-hotplug@.service")
    if not unit_exists:
        return
    cnt, seen = st["cnt"], set()
    for m in mods:
        ifn = m["iface"]
        net = os.path.join(usb.SYS, "class/net", ifn)
        if m["driver"] not in ok_drivers(conf) or not rd(os.path.join(net, "operstate")):
            continue
        key = "mpdhcp.%s.%s" % (ifn, rd(os.path.join(net, "ifindex")) or "?")
        seen.add(key)
        if m["addr"]:
            for k in (key, key + ".t", key + ".since"):
                cnt.pop(k, None)
            continue
        since = cnt.setdefault(key + ".since", now)
        if now - since < conf["mp_grace"]:
            continue                    # модем только пришёл: его DHCP-сервер ещё поднимается
        t = cnt.get(key, 0)
        gap = conf["mp_retry_gap"] if t < conf["dhcp_max_tries"] else conf["dhcp_retry_every"]
        if now - cnt.get(key + ".t", 0) < gap:
            continue
        cnt[key], cnt[key + ".t"] = t + 1, now
        log("%s без IPv4 %d с — настройка mp.space заново (%s), попытка %d"
            % (ifn, now - since, common.MP_UNIT % ifn, t + 1))
        sh("systemctl", "start", "--no-block", common.MP_UNIT % ifn, check=False)
    for k in [k for k in cnt if k.startswith("mpdhcp.") and k.rsplit(".", 1)[0] not in seen and k not in seen]:
        cnt.pop(k, None)


def apply_routes(w):
    cmds = route.plan(w, sh("ip", "-4", "rule", "show", check=False).stdout,
                      sh("ip", "-4", "route", "show", "table", "all", check=False).stdout)
    for c in cmds:
        if c[:3] == ["ip", "rule", "del"]:
            log("убираю своё лишнее правило: %s" % " ".join(c[3:]))
        r = sh(*c, check=False)
        if r.returncode != 0 and c[1:3] == ["rule", "add"]:
            log("⚠ %s: %s" % (" ".join(c), (r.stderr or "").strip()[:200]))
    return cmds


# ── HiLink: данные и лестница починки ───────────────────────────────────────
# Модем без связи не ждёт человека. Чем дольше нет связи, тем сильнее средство
# (одна попытка на модем не чаще reconnect_cooldown):
#   0. дозвон (Action=1)                                   — «отключён», «сбой»
#   1. отключить и подключить (Action 0 → 1)               — то, что делает `hivelink reconnect`
#   2. перерегистрация: данные выкл → режим сети туда-обратно → данные вкл
#   3. перезагрузка модема — без связи дольше heal_reboot_after, не чаще heal_reboot_every
# «Без связи» — это ещё и: «подключается»/«отключается» дольше stuck_grace (с 1);
# нет регистрации при SIM на месте (с 2, сначала даём модему найти сеть самому);
# «онлайн», но TCP с адреса модема не проходит netcheck_fails раз подряд (с 1).
# Web-API молчит дольше api_dead_after, а адрес есть — USB-устройство подключается заново.
LADDER = ("дозвон", "откл → вкл", "перерегистрация в сети", "перезагрузка модема")


def pause(sec):
    time.sleep(sec)


def _mins(sec):
    return "%d мин" % (sec // 60) if sec >= 60 else "%d с" % sec


def again(fn, tries=3):
    """Повторить действие; None — вышло, иначе текст последней ошибки."""
    err = None
    for i in range(tries):
        try:
            fn()
            return None
        except hilink.HiFail as e:
            err = str(e)
            if i < tries - 1:
                pause(2)
    return err


def reregister(api):
    """Данные выкл → режим сети туда-обратно → данные вкл: модем заново регистрируется у
    оператора (как реконнект modlink). Режим и диапазоны возвращаются как были."""
    try:
        mode, band, lte = api.net_mode()
    except hilink.HiFail:
        mode = None                     # у прошивки нет net-mode — хватит передачи данных
    api.set_data(False)
    pause(2)
    back = None
    if mode:
        try:
            api.set_net_mode("02" if mode != "02" else "03", band, lte)
        except hilink.HiFail:
            pass                        # режим не сменился — и возвращать нечего
        else:
            pause(2)
            back = again(lambda: api.set_net_mode(mode, band, lte))
    err = again(lambda: api.set_data(True))     # модем без данных хуже любой ошибки
    if err:
        raise hilink.HiFail("не включилась передача данных — %s" % err)
    if back:
        raise hilink.HiFail("режим сети не вернулся к %s — %s" % (mode, back))


def heal(api, n, conf, h, now, why, first=0):
    """Следующая ступень лестницы для модема без связи. h — его состояние (st["heal"][N])."""
    if not (conf["auto_heal"] and conf["auto_reconnect"]):
        return
    h.setdefault("bad", now)
    if now - h.get("act", 0) < conf["reconnect_cooldown"]:
        return
    lvl = max(h.get("lvl", 0), first)
    down = now - h["bad"]
    if lvl >= 2 and down >= conf["heal_reboot_after"] and now - h.get("boot", 0) >= conf["heal_reboot_every"]:
        lvl = 3
    log("N=%d: %s; без связи %s — %s" % (n, why, _mins(down), LADDER[lvl]))
    h["act"] = now
    h["lvl"] = 0 if lvl == 3 else min(lvl + 1, 2)
    try:
        if lvl == 0:
            api.dial(True)
        elif lvl == 1:
            api.dial(False)
            pause(3)
            api.dial(True)
        elif lvl == 2:
            reregister(api)
        else:
            h["boot"] = now
            api.reboot()
    except hilink.HiFail as e:
        log("N=%d: %s не вышло: %s" % (n, LADDER[lvl], e))


def healthy(h):
    for k in ("bad", "lvl", "act", "stuck"):
        h.pop(k, None)


def netcheck(m, conf, h, now):
    """У «онлайн»: ходит ли трафик. False — netcheck_fails неудач подряд. Проверяем, только если
    у адреса модема есть свой выход (правило + маршрут по умолчанию), иначе проверка врёт."""
    if conf["netcheck_every"] <= 0 or not m.get("routed"):
        return True
    fails = h.get("ncf", 0)
    if not fails and now - h.get("nc", 0) < conf["netcheck_every"]:
        return True
    h["nc"] = now
    if hilink.reach(m["addr"], conf["netcheck_host"]):
        if fails:
            log("N=%d: трафик снова идёт" % m["n"])
        h.pop("ncf", None)
        return True
    h["ncf"] = fails + 1
    if h["ncf"] < conf["netcheck_fails"]:
        log("N=%d: «онлайн», но TCP к %s не прошёл (%d из %d)" % (m["n"], conf["netcheck_host"], h["ncf"],
                                                               conf["netcheck_fails"]))
        return True
    return False


def api_dead(m, conf, h, now):
    """Web-API молчит, а адрес у модема есть: после api_dead_after — USB-устройство заново
    (не чаще api_reenum_every). Питание так не снимается, но зависший web-сервер обычно оживает."""
    if not conf["auto_heal"]:
        return
    h.setdefault("api", now)
    if now - h["api"] < conf["api_dead_after"] or now - h.get("reenum", 0) < conf["api_reenum_every"]:
        return
    log("N=%d: web-API молчит %s — заново подключаю USB-устройство %s" % (m["n"], _mins(now - h["api"]), m["port"]))
    h["reenum"] = h["api"] = now
    usb.reenumerate(m["dev"])


def hilink_worker(m, conf, st, now=None):
    """Один модем: данные, состояние связи, при беде — следующая ступень лестницы.
    Пишет только в st["heal"][N] этого модема (воркеры идут параллельно)."""
    n, ip = m["n"], m["addr"]
    qr = conf["quiet_repeat"]
    h = st["heal"].setdefault(str(n), {})
    now = int(time.time()) if now is None else now
    api = hilink.Api(n, src=ip)
    try:
        api.login()
    except hilink.HiFail as e:
        log_once(st, "N=%d: %s" % (n, e), qr, key="hilink.%d" % n)
        api_dead(m, conf, h, now)
        return
    h.pop("api", None)
    try:
        if conf["auto_dataswitch"] and api.dataswitch() == 0:
            log("N=%d: мобильные данные выключены — включаю" % n)
            api.set_data(True)
            return
        s = api.status()
    except hilink.HiFail as e:
        log_once(st, "N=%d: %s" % (n, e), qr, key="hilink.%d" % n)
        return
    cs = s["conn"]
    if cs is None:
        log_once(st, "N=%d: статус не читается (%s)" % (n, hilink.error_text(s["error"]) if s["error"] else "прошивка"),
                 qr, key="hilink.%d" % n)
        return
    if cs == 901:
        h.pop("stuck", None)
        if netcheck(m, conf, h, now):
            healthy(h)
        else:
            heal(api, n, conf, h, now, "«онлайн», но трафик не идёт (%d проверки подряд)" % h["ncf"], first=1)
        return
    h.pop("ncf", None)
    if cs in (900, 903):
        h.setdefault("stuck", now)
        if now - h["stuck"] >= conf["stuck_grace"]:
            heal(api, n, conf, h, now, "«%s» уже %s" % (hilink.conn_text(cs), _mins(now - h["stuck"])), first=1)
        return
    h.pop("stuck", None)
    if hilink.no_registration(cs):
        if s.get("sim") not in (None, 1):
            log_once(st, "N=%d: SIM не готова (SimStatus=%s) — проверь SIM-карту" % (n, s["sim"]), qr,
                     key="hilink.%d" % n)
            return
        log_once(st, "N=%d: нет регистрации в сети (сигнал, баланс, APN)" % n, qr, key="hilink.%d" % n)
        h.setdefault("act", now)        # сначала модем ищет сеть сам
        heal(api, n, conf, h, now, "нет регистрации в сети", first=2)
        return
    heal(api, n, conf, h, now, "ConnectionStatus=%d (%s)" % (cs, hilink.conn_text(cs)))


def step_hilink(conf, st, mods, w):
    if not (conf["auto_dataswitch"] or conf["auto_reconnect"]):
        return
    targets = [m for m in mods if m["n"] is not None and m["addr"] and m["n"] in w]
    now = int(time.time())
    # состояние модема, которого давно нет, — забыть (перезагрузка — это минута-две без модема)
    keep = 2 * max(conf["heal_reboot_every"], conf["api_reenum_every"])
    for k in [k for k, h in st["heal"].items() if not isinstance(h, dict) or max([0] + list(h.values())) < now - keep]:
        st["heal"].pop(k, None)
    if not targets:
        return
    try:
        routed = route.routed(sh("ip", "-4", "rule", "show", check=False).stdout,
                              sh("ip", "-4", "route", "show", "table", "all", check=False).stdout)
    except Exception:
        routed = set()
    for m in targets:
        m["routed"] = m["addr"] in routed
        st["heal"].setdefault(str(m["n"]), {})          # заранее: воркеры только меняют свои ключи
    with ThreadPoolExecutor(max_workers=max(1, min(conf["hilink_concurrency"], len(targets)))) as ex:
        list(ex.map(lambda m: hilink_worker(m, conf, st), targets))


def step_fixcheck(conf, st):
    """В памяти должен быть исправленный модуль, а не только файл на диске: типичная поломка —
    штатный rndis_host подхвачен из initramfs. Только сообщаем: чинить на ходу значит
    дёргать модуль под работающими модемами."""
    if conf["rx_urb_size"] <= 0 or not rndis.bound_real() or os.path.exists(rndis.PARAM):
        return
    log_once(st, "фикс rndis_host не активен: в памяти штатный модуль, приём будет ~1.3 Мбит — hivelink install",
             3600)


def run_pass(conf=None):
    """Один проход. Вызывать под блокировкой common.LOCK."""
    common.rotate_log()
    st = common.load_state()
    if conf is None:
        try:
            conf = common.load_conf()
        except Fail as e:
            log_once(st, "⚠ %s — работаю на умолчаниях" % e, 3600)
            conf = dict(common.DEFAULTS)
    st["cycle"] = (st["cycle"] + 1) % 100000
    on = common.net_mode(conf) == "on"
    mods, w = [], {}

    def guard(name, fn, *a):
        try:
            return fn(*a)
        except Exception as e:          # шаг изолирован: остальные идут дальше
            log_once(st, "шаг %s: %s" % (name, e), conf["quiet_repeat"])

    guard("питание", step_power, conf, st)
    devs = usb.devices()
    try:
        devs = step_off(conf, st, devs)
    except Exception as e:
        log_once(st, "шаг выключенные: %s" % e, conf["quiet_repeat"])
    guard("Zero-CD", step_zerocd, conf, st, devs)
    guard("USB-конфигурация", step_usbcfg, conf, st, devs)
    try:
        mods, addrs = modems(conf)
        w = want(mods, addrs, conf, st)
    except Exception as e:
        log_once(st, "модемы: %s" % e, conf["quiet_repeat"])
    if on:
        guard("линк", step_link, conf, st, mods)
    else:
        guard("адреса mp.space", step_mp_link, conf, st, mods)
    guard("маршруты", apply_routes, w if on else {})     # сеть не наша — снимаем только своё
    if st["cycle"] % max(1, conf["hilink_every"]) == 0:
        guard("HiLink", step_hilink, conf, st, mods, w)
    guard("фикс приёма", step_fixcheck, conf, st)
    common.save_state(st)
    return st
