"""hivelink: один проход приведения настоящих модемов к рабочему состоянию.

Гоняется таймером (15 с), udev-событием и вручную — результат один и тот же.
Шаги изолированы: сбой одного не роняет остальные. Каждый шаг смотрит только на
настоящие модемы (usb.devices()): виртуальные proxyveth на dummy_hcd не видны.

  1. питание USB      автосуспенд роняет хабы и модемы — выключаем
  2. Zero-CD          12d1:1f01 и родня — usb_modeswitch, при отказе пере-enumerate
  3. USB-конфигурация ядро часто выбирает NCM (wwanN без DHCP) — перебираем до rndis_host
  4. линк и адрес     поднять, DHCP через networkd (если сеть модемов наша)
  5. маршруты         таблица 2000+N и правило 31000+N на модем; уборка — только своего
  6. HiLink           мобильные данные и дозвон (раз в hilink_every проходов, параллельно)
  7. фикс приёма      проверка, что в памяти исправленный rndis_host
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


def step_zerocd(conf, st, devs):
    if not shutil.which("usb_modeswitch"):
        if any(usb.pid_mode(usb.attr(d, "idProduct")) == "cdrom" for d in devs):
            log_once(st, "модем в Zero-CD, а usb_modeswitch нет — hivelink install поставит", conf["quiet_repeat"])
        return
    cnt = st["cnt"]
    for d in devs:
        p, pid = usb.port(d), usb.attr(d, "idProduct")
        key = "zerocd." + p
        if usb.pid_mode(pid) != "cdrom" or usb.claimed(d):
            cnt.pop(key, None)
            continue
        cur, tot = usb.config(d)
        # Zero-CD не на первой конфигурации: там MBIM, mass-storage endpoint — единственный
        # адресат usb_modeswitch — отсутствует. Сначала cfg 1, переключение — следующим проходом.
        if cur is not None and cur != 1 and tot > 1:
            log("%s 12d1:%s Zero-CD в cfg %d/%d — ставлю cfg 1" % (p, pid, cur, tot))
            usb.set_config(d, 1)
            cnt[key] = 0
            continue
        tries = cnt.get(key, 0)
        if tries >= conf["zerocd_max_tries"]:
            log("%s 12d1:%s не переключается за %d попыток — пере-enumerate" % (p, pid, tries))
            usb.reenumerate(d)
            cnt[key] = 0
            continue
        bus, num = usb.attr(d, "busnum"), usb.attr(d, "devnum")
        if not bus or not num:
            continue
        log("%s 12d1:%s в CD-ROM — usb_modeswitch (попытка %d)" % (p, pid, tries + 1))
        sh("usb_modeswitch", "-v", "12d1", "-p", pid, "-b", bus, "-g", num,
           "-c", os.path.join(common.FILES, "zerocd.conf"), check=False, timeout=60)
        cnt[key] = tries + 1


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
        key = "dhcp." + ifn
        if m["addr"]:
            st["cnt"].pop(key, None)
            continue
        if not networkd:
            log_once(st, "systemd-networkd не запущен — модемам некому дать адрес (hivelink install)",
                     conf["quiet_repeat"])
            continue
        t = st["cnt"].get(key, 0)
        if t >= conf["dhcp_max_tries"]:
            log_once(st, "%s: нет IPv4 после %d попыток DHCP" % (ifn, t), conf["quiet_repeat"])
            continue
        st["cnt"][key] = t + 1
        log("%s (%s) без IPv4 — networkctl reconfigure" % (ifn, m["driver"]))
        sh("networkctl", "reconfigure", ifn, check=False)


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


def hilink_worker(m, conf, st):
    n, ip = m["n"], m["addr"]
    qr = conf["quiet_repeat"]
    api = hilink.Api(n, src=ip)
    try:
        api.login()
        if conf["auto_dataswitch"] and api.dataswitch() == 0:
            log("N=%d: мобильные данные выключены — включаю" % n)
            api.set_data(True)
            return None
        s = api.status()
    except hilink.HiFail as e:
        log_once(st, "N=%d: %s" % (n, e), qr, key="hilink.%d" % n)
        return None
    cs = s["conn"]
    if cs in (900, 901, 903):
        return ("ok", n)
    if cs is None:
        log_once(st, "N=%d: статус не читается (%s)" % (n, hilink.error_text(s["error"]) if s["error"] else "прошивка"),
                 qr, key="hilink.%d" % n)
        return None
    if hilink.no_registration(cs):
        log_once(st, "N=%d: нет регистрации в сети (SIM, сигнал, APN)" % n, qr, key="hilink.%d" % n)
        return None
    if not conf["auto_reconnect"]:
        return None
    now = int(time.time())
    if now - st["dial"].get(str(n), 0) < conf["reconnect_cooldown"]:
        return None
    log("N=%d: ConnectionStatus=%d (%s) — дозваниваюсь" % (n, cs, hilink.conn_text(cs)))
    try:
        api.dial(True)
    except hilink.HiFail as e:
        log("N=%d: дозвон не принят: %s" % (n, e))
    return ("dial", n, now)


def step_hilink(conf, st, mods, w):
    if not (conf["auto_dataswitch"] or conf["auto_reconnect"]):
        return
    targets = [m for m in mods if m["n"] is not None and m["addr"] and m["n"] in w]
    if not targets:
        return
    with ThreadPoolExecutor(max_workers=max(1, min(conf["hilink_concurrency"], len(targets)))) as ex:
        res = list(ex.map(lambda m: hilink_worker(m, conf, st), targets))
    for r in res:
        if not r:
            continue
        if r[0] == "ok":
            st["dial"].pop(str(r[1]), None)
        else:
            st["dial"][str(r[1])] = r[2]


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
    guard("Zero-CD", step_zerocd, conf, st, devs)
    guard("USB-конфигурация", step_usbcfg, conf, st, devs)
    try:
        mods, addrs = modems(conf)
        w = want(mods, addrs, conf, st)
    except Exception as e:
        log_once(st, "модемы: %s" % e, conf["quiet_repeat"])
    if on:
        guard("линк", step_link, conf, st, mods)
    guard("маршруты", apply_routes, w if on else {})     # сеть не наша — снимаем только своё
    if st["cycle"] % max(1, conf["hilink_every"]) == 0:
        guard("HiLink", step_hilink, conf, st, mods, w)
    guard("фикс приёма", step_fixcheck, conf, st)
    common.save_state(st)
    return st
