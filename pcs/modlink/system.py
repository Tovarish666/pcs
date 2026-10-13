"""modlink на машине: юниты, apply, установка и снятие, перенос из modlink-linux, состояние.

Юниты (docs/ARCHITECTURE.md §4, §8):
  modlink-3proxy.service  3proxy /usr/local/lib/pcs/3proxy с /etc/modlink/3proxy.cfg
  modlink.service         демон: ссылки реконнекта и ребута, таймеры, проверки, HiLink
До 13.10.2026 прокси держал sing-box (modlink-sb.service) — setup его снимает.

В modlink-linux `modlink.service` — это его sing-box (/usr/local/bin/sing-box). Такой юнит
без явного `modlink migrate` не трогаем; перенос кладёт копию в /etc/modlink/legacy/.
"""
import json
import os
import shutil
import sys
import time

from ..core import net, threeproxy
from ..core.util import Fail, jload, jsave, rd, sh, wr
from . import store
from .store import P

MARK = "# pcs:modlink"
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SVC, PROXY, PANEL = "modlink.service", "modlink-3proxy.service", "modlink-panel.service"
OLD_SB = "modlink-sb.service"          # прежний движок (sing-box) — снимается при setup


def unit_daemon():
    py = "/usr/bin/python3" if os.path.exists("/usr/bin/python3") else sys.executable
    return """%s — пишет `modlink setup`, руками не править
[Unit]
Description=modlink: ссылки реконнекта и ребута, таймеры, проверки, HiLink (PCS)
After=network-online.target
Wants=network-online.target

[Service]
ExecStart=%s %s daemon
ExecReload=/bin/kill -HUP $MAINPID
Restart=always
RestartSec=3
NoNewPrivileges=yes
ProtectHome=yes
PrivateTmp=yes

[Install]
WantedBy=multi-user.target
""" % (MARK, py, os.path.join(ROOT, "bin", "modlink"))


def unit_proxy():
    return """%s — пишет `modlink setup`, руками не править
[Unit]
Description=modlink: 3proxy — прокси из интерфейсов (PCS)
After=network-online.target
Wants=network-online.target

[Service]
ExecStart=%s %s
ExecReload=/bin/kill -USR1 $MAINPID
Restart=always
RestartSec=3
LimitNOFILE=1048576
NoNewPrivileges=yes
ProtectSystem=full
ProtectHome=yes
PrivateTmp=yes

[Install]
WantedBy=multi-user.target
""" % (MARK, threeproxy.BIN, P.cfg3)


# ── мелочи системы ─────────────────────────────────────────────────────────
def systemctl(*args):
    return sh("systemctl", *args, check=False)


def active(unit):
    try:
        return systemctl("is-active", unit).stdout.strip() or "unknown"
    except Fail:
        return "unknown"


def unit_path(name):
    return os.path.join(P.units, name)


def owner(name):
    """Чей юнит: "ours", "legacy" (чужой — modlink-linux) или None (нет)."""
    text = rd(unit_path(name))
    if not text:
        return None
    return "ours" if MARK in text else "legacy"


def write_unit(name, text):
    if rd(unit_path(name)) == text.strip():
        return False
    os.makedirs(P.units, exist_ok=True)
    wr(unit_path(name), text, 0o644)
    return True


def local_addrs():
    """{адрес: интерфейс} для IPv4 машины."""
    try:
        text = sh("ip", "-4", "-o", "addr", check=False).stdout
    except Fail:
        return {}
    return {ip: dev for dev, ip, _ in store.parse_addrs(text)}


def listening():
    """TCP-порты в LISTEN (из /proc, без ss); None — узнать нечем."""
    ports, seen = set(), False
    for p in ("/proc/net/tcp", "/proc/net/tcp6"):
        try:
            with open(p) as f:
                lines = f.read().splitlines()[1:]
        except OSError:
            continue
        seen = True
        for ln in lines:
            f = ln.split()
            if len(f) > 3 and f[3] == "0A":
                ports.add(int(f[1].rsplit(":", 1)[1], 16))
    return ports if seen else None


def applied_rows():
    doc = jload(P.applied, {})
    out = []
    for r in doc.get("proxies", []) if isinstance(doc, dict) else []:
        try:
            out.append(store.norm(r))
        except Fail:
            pass
    return out


def pending(rows, applied=None):
    key = (lambda rs: json.dumps(sorted(rs, key=lambda r: r["id"]), sort_keys=True))
    return key(rows) != key(applied if applied is not None else applied_rows())


def dirs():
    for d, mode in ((P.etc, 0o700), (P.lib, 0o700), (P.log, 0o750), (P.run, 0o700)):
        os.makedirs(d, mode=mode, exist_ok=True)
        os.chmod(d, mode)


def ext_ip_saved():
    return store.settings()["wan_ip"]


def save_ext_ip(ip):
    store.save_settings(wan_ip=ip)


# ── apply ──────────────────────────────────────────────────────────────────
def ports_of(r):
    return {p for p in (r["port"], r["reconnect_port"], r["reboot_port"]) if p}


def wait_active(unit, sec=8):
    st = active(unit)
    for _ in range(sec * 2):
        if st == "active":
            break
        time.sleep(0.5)
        st = active(unit)
    return st


def wait_listen(ports, sec=8):
    """Ждать, пока все порты в LISTEN. → порты, которые так и не поднялись."""
    left = set(ports)
    for _ in range(sec * 4):
        lst = listening()
        if lst is None:
            return set()
        left = set(ports) - lst
        if not left:
            break
        time.sleep(0.25)
    return left


def journal(unit, n=8):
    try:
        return " | ".join(sh("journalctl", "-u", unit, "-n", str(n), "--no-pager", "-o", "cat", check=False)
                          .stdout.strip().splitlines()[-n:])
    except Fail:
        return ""


def start_proxy(text, ports):
    """Записать конфиг 3proxy и (пере)запустить; не поднялся — вернуть прежний конфиг. → как запустили."""
    prev = rd(P.cfg3)
    wr(P.cfg3, text, 0o600)
    systemctl("enable", PROXY)
    how = "restart"
    if active(PROXY) == "active":
        systemctl("reload", PROXY)              # SIGUSR1: 3proxy перечитывает конфиг, соединения живут
        how = "reload"
        if wait_listen(ports, 4):
            systemctl("restart", PROXY)         # перечитать не вышло — честный перезапуск
            how = "restart"
    else:
        systemctl("restart", PROXY)
    st = wait_active(PROXY)
    left = wait_listen(ports) if st == "active" else set(ports)
    if left:
        why = journal(PROXY)
        if prev and prev != text:
            wr(P.cfg3, prev, 0o600)
            systemctl("restart", PROXY)
        raise Fail("3proxy не поднял порт %s (%s)%s%s" % (", ".join(map(str, sorted(left)[:5])), st,
                   ": " + why if why else "", " — вернул прежний конфиг" if prev and prev != text else ""))
    return how


def apply(log, binary=None):
    """Таблица → конфиг 3proxy → запуск (reload, а не обрыв соединений) → проверка портов; демон перечитывает."""
    if owner(SVC) == "legacy":
        raise Fail("на машине старый modlink-linux: его modlink.service держит порты прокси. "
                   "Сначала перенос — посмотреть: modlink migrate --dry-run, перенести: "
                   "modlink migrate --force --stop-panel (порты и пароли его строк — как были)")
    if not threeproxy.installed(binary or threeproxy.BIN):
        raise Fail("нет 3proxy %s — поставить: modlink setup" % threeproxy.VERSION)
    rows = store.load()["proxies"]
    text = store.config3(rows, store.settings(), store.guard_extra())
    en = [r for r in rows if r["enabled"]]
    own = {p for r in applied_rows() for p in ports_of(r)}
    busy = sorted({p for r in en for p in ports_of(r) if p not in own and not store.PROBE[0](p)})
    if busy:
        who = next(r for r in en if busy[0] in ports_of(r))
        raise Fail("порт %s уже слушает кто-то другой (ss -ltnp) — смени: modlink set %d port=auto"
                   % (", ".join(map(str, busy[:5])), who["id"]))
    dirs()
    warn = []
    if en:
        how = start_proxy(text, {r["port"] for r in en})
        log("3proxy: %d прокси на %d портах (%s)" % (len(en), len({r["port"] for r in en}),
                                                    "перечитал конфиг" if how == "reload" else "перезапущен"))
    else:
        wr(P.cfg3, text, 0o600)
        systemctl("disable", "--now", PROXY)
        log("включённых строк нет — 3proxy остановлен")
    jsave(P.applied, {"t": int(time.time()), "proxies": rows}, 0o600)
    addrs = local_addrs()
    for r in en:
        if addrs and r["lan_ip"] not in addrs:
            warn.append("строка %d: адреса %s на машине нет — прокси не выйдет в сеть, пока интерфейс не поднимется"
                        % (r["id"], r["lan_ip"]))
    ours = owner(SVC) == "ours"
    if ours:
        systemctl("reload-or-restart", SVC)
    elif owner(SVC) == "legacy":
        warn.append("modlink.service — от старого modlink-linux: ссылки и таймеры не работают; перенос: modlink migrate")
    else:
        warn.append("демона modlink нет: ссылки реконнекта/ребута и таймеры не работают — modlink setup")
    if ours:
        time.sleep(1.5)             # демон перечитал и занял порты управления
    for rid, t in sorted(jload(P.state, {}).get("triggers", {}).items()) if ours else ():
        if not t.get("ok"):
            warn.append("порт управления %s: не поднят — %s" % (rid, t.get("error") or "?"))
    for w in warn:
        log("⚠ " + w)
    return {"enabled": len(en), "proxy": active(PROXY), "daemon": active(SVC), "warnings": warn}


def drop_singbox(log):
    """Прежний движок modlink — sing-box (modlink-sb.service): снять. Сам sing-box PCS остаётся (proxyveth)."""
    if owner(OLD_SB) != "ours":
        return False
    systemctl("disable", "--now", OLD_SB)
    os.unlink(unit_path(OLD_SB))
    systemctl("daemon-reload")
    log("прокси modlink теперь на 3proxy: sing-box (modlink-sb) остановлен и снят")
    return True


def resync(log, binary=None):
    """После обновления кода: конфиг из уже применённого, если генератор поменялся. Правки не применяет."""
    rows = applied_rows()
    if not rows:
        return False
    text = store.config3(rows, store.settings(), store.guard_extra())
    en = [r for r in rows if r["enabled"]]
    if rd(P.cfg3) == text.strip():
        if en and active(PROXY) != "active":
            systemctl("enable", "--now", PROXY)
        return False
    if en:
        start_proxy(text, {r["port"] for r in en})
    else:
        wr(P.cfg3, text, 0o600)
    log("конфиг 3proxy собран заново (строки те же)")
    return True


# ── установка и снятие ─────────────────────────────────────────────────────
def take_over(log):
    """Юнит modlink.service от modlink-linux (его sing-box) → копия в legacy/, остановить, убрать."""
    os.makedirs(P.legacy, mode=0o700, exist_ok=True)
    keep = os.path.join(P.legacy, SVC)
    if not os.path.exists(keep):
        shutil.copy2(unit_path(SVC), keep)
    sb_keep = os.path.join(P.legacy, "singbox.json")
    if os.path.exists(P.singbox) and not os.path.exists(sb_keep) and not os.path.exists(P.applied):
        shutil.copy2(P.singbox, sb_keep)
    systemctl("disable", "--now", SVC)
    os.unlink(unit_path(SVC))
    log("старый modlink.service (sing-box modlink-linux) остановлен; копия — %s" % P.legacy)


def setup(log, takeover=False, binary=None):
    """Подготовить машину: база PCS, 3proxy, юниты; прежний sing-box modlink — снять. Повторный вызов безопасен."""
    warn = []
    net.ensure_base(log)
    dirs()
    try:
        if threeproxy.install(binary or threeproxy.BIN):
            log("3proxy %s поставлен: %s" % (threeproxy.VERSION, binary or threeproxy.BIN))
    except Fail:
        raise
    except Exception as e:
        raise Fail("3proxy %s не поставился (%s) — проверь доступ к github.com" % (threeproxy.VERSION, e))
    changed = write_unit(PROXY, unit_proxy())
    migrated = bool(jload(P.proxies, {}).get("migrated")) if os.path.exists(P.proxies) else False
    if owner(SVC) == "legacy" and not (takeover or migrated):
        warn.append("modlink.service — от старого modlink-linux (в нём его sing-box); не трогаю. "
                    "Перенос: modlink migrate")
    else:
        if owner(SVC) == "legacy":
            take_over(log)
        changed = write_unit(SVC, unit_daemon()) or changed
    if changed:
        systemctl("daemon-reload")
    drop_singbox(log)
    resync(log, binary)
    if owner(SVC) == "ours":
        systemctl("enable", SVC)
        systemctl("restart", SVC)
    if active(PANEL) == "active":
        warn.append("работает старая панель modlink-panel (:5000) — не трогаю")
    for w in warn:
        log("⚠ " + w)
    return {"3proxy": binary or threeproxy.BIN, "daemon": active(SVC), "proxy": active(PROXY), "warnings": warn}


def uninstall(log, purge=False):
    """Снять своё: юниты modlink и modlink-3proxy (и прежний modlink-sb); с purge — и свои файлы."""
    for name in (SVC, PROXY, OLD_SB):
        if owner(name) == "ours":
            systemctl("disable", "--now", name)
            os.unlink(unit_path(name))
            log("снят %s" % name)
    systemctl("daemon-reload")
    restored = []
    keep = os.path.join(P.legacy, SVC)
    if os.path.exists(keep) and not os.path.exists(unit_path(SVC)):
        shutil.copy2(keep, unit_path(SVC))
        restored.append(SVC)
        sb_keep = os.path.join(P.legacy, "singbox.json")
        if os.path.exists(sb_keep):
            shutil.copy2(sb_keep, P.singbox)
            restored.append("singbox.json")
        systemctl("daemon-reload")
        log("вернул старый modlink-linux (%s), не запущен: systemctl enable --now modlink" % ", ".join(restored))
    removed = []
    if purge:
        for f in (P.proxies, P.config, P.cfg3) + (() if "singbox.json" in restored else (P.singbox,)):
            if os.path.exists(f):
                os.unlink(f)
                removed.append(f)
        for d in (P.lib, P.log, P.run) + ((P.legacy,) if restored else ()):
            if os.path.isdir(d):
                shutil.rmtree(d, ignore_errors=True)
                removed.append(d)
        if os.path.isdir(P.etc) and not os.listdir(P.etc):
            os.rmdir(P.etc)
            removed.append(P.etc)
        log("удалены свои файлы: %d" % len(removed))
    return {"restored": restored, "removed": removed}


# ── перенос из modlink-linux ───────────────────────────────────────────────
def migrate(log, src=None, stop_panel=False, force=False, dry_run=False, binary=None):
    src = src or P.etc
    old_sb = jload(os.path.join(P.legacy, "singbox.json"), None)
    rows, ext_ip, warn = store.legacy(src, old_sb)
    doc = store.load()
    same = not pending(rows, doc["proxies"])
    replace = len(doc["proxies"]) if doc["proxies"] and not same else 0
    if replace and dry_run:
        warn.append("в таблице modlink сейчас %d строк — перенос заменит их (modlink migrate --force)" % replace)
    out = {"rows": [store.masked(r) for r in rows], "ext_ip": ext_ip, "warnings": warn, "dry_run": dry_run,
           "replaces": replace}
    if dry_run:
        return out              # предпросмотр — всегда, даже если таблица не пустая
    if replace and not force:
        raise Fail("в таблице modlink уже %d строк — перенос заменит их; посмотреть: modlink migrate --dry-run, "
                   "заменить: modlink migrate --force" % replace)
    if not same:
        store.save({"next_id": store.FIRST_FREE_ID, "proxies": rows,
                    "migrated": {"from": src, "t": int(time.time())}})
        log("таблица: %d строк из %s (порты и пароли как были)" % (len(rows), src))
    else:
        log("таблица уже перенесена — строки не трогаю")
    if ext_ip and not ext_ip_saved():
        save_ext_ip(ext_ip)
    if rows and "base_port" not in jload(P.config, {}):
        store.save_settings(base_port=min(r["port"] for r in rows))     # базовый порт — как был
    s = setup(log, takeover=True, binary=binary)
    a = apply(log, binary=binary)
    panel = active(PANEL)
    if stop_panel and panel in ("active", "activating", "failed"):
        systemctl("disable", "--now", PANEL)
        log("старая панель modlink-panel остановлена и выключена (файлы на месте)")
        systemctl("reload-or-restart", SVC)
        panel = active(PANEL)
    elif panel == "active":
        warn.append("старая панель modlink-panel (:5000) работает: её триггеры держат порты реконнекта, "
                    "таймеры дублируют наши, а «Применить» в ней перепишет modlink.service. "
                    "Остановить: modlink migrate --stop-panel")
    for w in warn:
        log("⚠ " + w)           # setup и apply свои предупреждения уже напечатали
    warn += [w for w in s["warnings"] + a["warnings"] if w not in warn]
    out.update({"panel": panel, "proxy": a["proxy"], "daemon": a["daemon"], "warnings": warn})
    return out


# ── состояние ──────────────────────────────────────────────────────────────
def status():
    from . import ops
    doc = store.load()
    applied = {r["id"]: r for r in applied_rows()}
    addrs, lst = local_addrs(), listening()
    st = jload(P.state, {})
    trig, nxt = st.get("triggers", {}), st.get("next", {})
    checks = jload(P.checks, {})
    checks = checks if isinstance(checks, dict) else {}
    rows = []
    for r in doc["proxies"]:
        a = applied.get(r["id"])
        ctl = {p: trig.get(str(p), {}) for p in sorted({r["reconnect_port"], r["reboot_port"]} - {0})}
        bad_ctl = [p for p, t in ctl.items() if t and not t.get("ok")]
        last = ops.last(r["id"])
        chk = checks.get(str(r["id"])) or {}
        e = {"id": r["id"], "name": r["name"], "enabled": r["enabled"], "applied": a == r,
             "port": r["port"], "port_up": None if lst is None else r["port"] in lst,
             "lan_ip": r["lan_ip"], "iface": addrs.get(r["lan_ip"]),
             "reconnect_port": r["reconnect_port"], "reboot_port": r["reboot_port"],
             "trigger_up": (not bad_ctl) if any(ctl.values()) else None,
             "trigger_error": "; ".join("%d: %s" % (p, ctl[p].get("error") or "?") for p in bad_ctl) or None,
             "interval_min": r["interval_min"], "next": nxt.get(str(r["id"])), "last": last,
             "wan_ip": chk.get("wan_ip"), "wan_t": chk.get("wan_t"), "wan_error": chk.get("wan_error"),
             "speed": chk.get("speed")}
        if not r["enabled"]:
            e["state"] = "disabled"
        elif not a or not a["enabled"]:
            e["state"] = "pending"
        elif (addrs and not e["iface"]) or e["port_up"] is False:
            e["state"] = "broken"
        elif e["trigger_up"] is False or (last and not last["ok"]) or (chk.get("wan_error") and not chk.get("wan_ip")):
            e["state"] = "warn"
        else:
            e["state"] = "ok"
        rows.append(e)
    s = store.settings()
    out = {"proxy": active(PROXY), "daemon": active(SVC), "unit": owner(SVC), "engine": "3proxy %s" % threeproxy.VERSION,
           "mode": s["mode"], "traffic": s["traffic"],
           "pending": pending(doc["proxies"], list(applied.values())), "rows": rows}
    if out["unit"] == "legacy" or os.path.exists(unit_path(PANEL)):
        out["panel"] = active(PANEL)
    return out
