"""modlink на машине: юниты, apply, установка и снятие, перенос из modlink-linux, состояние.

Юниты (docs/ARCHITECTURE.md §4, §8):
  modlink-sb.service  sing-box /usr/local/lib/pcs/sing-box с /etc/modlink/singbox.json
  modlink.service     демон: триггеры реконнекта, таймеры, HiLink

В modlink-linux `modlink.service` — это его sing-box (/usr/local/bin/sing-box). Такой юнит
без явного `modlink migrate` не трогаем; перенос кладёт копию в /etc/modlink/legacy/.
"""
import json
import os
import shutil
import sys
import time

from ..core import net, singbox
from ..core.util import Fail, jload, jsave, rd, sh, wr
from . import store
from .store import P

MARK = "# pcs:modlink"
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SVC, SB, PANEL = "modlink.service", "modlink-sb.service", "modlink-panel.service"


def unit_daemon():
    py = "/usr/bin/python3" if os.path.exists("/usr/bin/python3") else sys.executable
    return """%s — пишет `modlink setup`, руками не править
[Unit]
Description=modlink: триггеры реконнекта, таймеры, HiLink (PCS)
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


def unit_sb():
    return """%s — пишет `modlink setup`, руками не править
[Unit]
Description=modlink: sing-box — прокси из интерфейсов (PCS)
After=network-online.target
Wants=network-online.target

[Service]
ExecStart=%s run -c %s
Restart=always
RestartSec=3
LimitNOFILE=1048576
NoNewPrivileges=yes
ProtectSystem=full
ProtectHome=yes
PrivateTmp=yes
InaccessiblePaths=-/run/dbus/system_bus_socket

[Install]
WantedBy=multi-user.target
""" % (MARK, singbox.BIN, P.singbox)


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
    return str(jload(P.config, {}).get("ext_ip") or "")


def save_ext_ip(ip):
    cfg = jload(P.config, {})
    cfg["ext_ip"] = ip
    jsave(P.config, cfg, 0o600)


# ── apply ──────────────────────────────────────────────────────────────────
def check_config(cfg, binary):
    if not os.path.exists(binary):
        raise Fail("нет sing-box %s (%s) — поставить: modlink setup" % (singbox.VERSION, binary))
    ok, msg = singbox.check(cfg, binary)
    if not ok:
        raise Fail("sing-box check не принял конфиг: %s" % " ".join(msg.split())[:300])


def wait_active(unit, sec=8):
    st = active(unit)
    for _ in range(sec * 2):
        if st == "active":
            break
        time.sleep(0.5)
        st = active(unit)
    return st


def apply(log, binary=None):
    """Таблица → конфиг sing-box → sing-box check → запись → перезапуск modlink-sb; демон перечитывает."""
    binary = binary or singbox.BIN
    rows = store.load()["proxies"]
    cfg = store.config(rows)
    check_config(cfg, binary)
    en = [r for r in rows if r["enabled"]]
    own = {p for r in applied_rows() for p in (r["port"], r["reconnect_port"])}
    busy = [r for r in en if r["port"] not in own and not store.PROBE[0](r["port"])]
    if busy:
        raise Fail("порт %s уже слушает кто-то другой (ss -ltnp) — смени: modlink set %d port=auto"
                   % (", ".join(str(r["port"]) for r in busy), busy[0]["id"]))
    dirs()
    jsave(P.singbox, cfg, 0o600)
    jsave(P.applied, {"t": int(time.time()), "proxies": rows}, 0o600)
    warn = []
    addrs = local_addrs()
    for r in en:
        if addrs and r["lan_ip"] not in addrs:
            warn.append("строка %d: адреса %s на машине нет — прокси не выйдет в сеть, пока интерфейс не поднимется"
                        % (r["id"], r["lan_ip"]))
    ours = owner(SVC) == "ours"
    if ours:
        systemctl("reload-or-restart", SVC)
    elif owner(SVC) == "legacy":
        warn.append("modlink.service — от старого modlink-linux: триггеры и таймеры не работают; перенос: modlink migrate")
    else:
        warn.append("демона modlink нет: триггеры и таймеры не работают — modlink setup")
    if en:
        systemctl("enable", SB)
        systemctl("restart", SB)
        st = wait_active(SB)
        if st != "active":
            raise Fail("sing-box не поднялся (%s): journalctl -u modlink-sb -n 30" % st)
        log("sing-box: %d прокси" % len(en))
    else:
        systemctl("disable", "--now", SB)
        log("включённых строк нет — sing-box остановлен")
    if ours:
        time.sleep(1.5)             # демон перечитал и занял порты триггеров
    for rid, t in sorted(jload(P.state, {}).get("triggers", {}).items()) if ours else ():
        if not t.get("ok"):
            warn.append("строка %s: триггер не поднят — %s" % (rid, t.get("error") or "?"))
    for w in warn:
        log("⚠ " + w)
    return {"enabled": len(en), "sb": active(SB), "daemon": active(SVC), "warnings": warn}


def resync(log, binary=None):
    """После обновления кода: конфиг из уже применённого, если генератор поменялся. Правки не применяет."""
    rows = applied_rows()
    if not rows:
        return False
    cfg = store.config(rows)
    if jload(P.singbox, None) == cfg:
        if any(r["enabled"] for r in rows) and active(SB) != "active":
            systemctl("enable", "--now", SB)
        return False
    check_config(cfg, binary or singbox.BIN)
    jsave(P.singbox, cfg, 0o600)
    if any(r["enabled"] for r in rows):
        systemctl("enable", SB)
        systemctl("restart", SB)
    log("конфиг sing-box пересобран под новую версию (строки те же)")
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
    """Подготовить машину: база PCS, sing-box 1.14, юниты. Повторный вызов безопасен."""
    warn = []
    net.ensure_base(log)
    dirs()
    try:
        if singbox.install(binary or singbox.BIN):
            log("sing-box %s поставлен: %s" % (singbox.VERSION, binary or singbox.BIN))
    except Fail:
        raise
    except Exception as e:
        raise Fail("sing-box %s не скачался (%s) — проверь доступ к github.com" % (singbox.VERSION, e))
    changed = write_unit(SB, unit_sb())
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
    resync(log, binary)
    if owner(SVC) == "ours":
        systemctl("enable", SVC)
        systemctl("restart", SVC)
    if active(PANEL) == "active":
        warn.append("работает старая панель modlink-panel (:5000) — не трогаю")
    for w in warn:
        log("⚠ " + w)
    return {"singbox": binary or singbox.BIN, "daemon": active(SVC), "sb": active(SB), "warnings": warn}


def uninstall(log, purge=False):
    """Снять своё: юниты modlink и modlink-sb; с purge — и свои файлы. sing-box PCS общий — остаётся."""
    for name in (SVC, SB):
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
        for f in (P.proxies, P.config) + (() if "singbox.json" in restored else (P.singbox,)):
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
    if doc["proxies"] and not same and not force:
        raise Fail("в таблице modlink уже %d строк — перенос заменит их; заменить: modlink migrate --force"
                   % len(doc["proxies"]))
    out = {"rows": [store.masked(r) for r in rows], "ext_ip": ext_ip, "warnings": warn, "dry_run": dry_run}
    if dry_run:
        return out
    if not same:
        store.save({"next_id": store.FIRST_FREE_ID, "proxies": rows,
                    "migrated": {"from": src, "t": int(time.time())}})
        log("таблица: %d строк из %s (порты и пароли как были)" % (len(rows), src))
    else:
        log("таблица уже перенесена — строки не трогаю")
    if ext_ip and not ext_ip_saved():
        save_ext_ip(ext_ip)
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
    warn += [w for w in s["warnings"] + a["warnings"] if w not in warn]
    for w in warn:
        log("⚠ " + w)
    out.update({"panel": panel, "sb": a["sb"], "daemon": a["daemon"], "warnings": warn})
    return out


# ── состояние ──────────────────────────────────────────────────────────────
def status():
    from . import ops
    doc = store.load()
    applied = {r["id"]: r for r in applied_rows()}
    addrs, lst = local_addrs(), listening()
    st = jload(P.state, {})
    trig, nxt = st.get("triggers", {}), st.get("next", {})
    rows = []
    for r in doc["proxies"]:
        a = applied.get(r["id"])
        t = trig.get(str(r["id"]), {})
        last = ops.last(r["id"])
        e = {"id": r["id"], "name": r["name"], "enabled": r["enabled"], "applied": a == r,
             "port": r["port"], "port_up": None if lst is None else r["port"] in lst,
             "lan_ip": r["lan_ip"], "iface": addrs.get(r["lan_ip"]),
             "reconnect_port": r["reconnect_port"], "trigger_up": bool(t.get("ok")) if t else None,
             "trigger_error": t.get("error"), "interval_min": r["interval_min"],
             "next": nxt.get(str(r["id"])), "last": last}
        if not r["enabled"]:
            e["state"] = "disabled"
        elif not a or not a["enabled"]:
            e["state"] = "pending"
        elif (addrs and not e["iface"]) or e["port_up"] is False:
            e["state"] = "broken"
        elif e["trigger_up"] is False or (last and not last["ok"]):
            e["state"] = "warn"
        else:
            e["state"] = "ok"
        rows.append(e)
    out = {"sb": active(SB), "daemon": active(SVC), "unit": owner(SVC),
           "pending": pending(doc["proxies"], list(applied.values())), "rows": rows}
    if out["unit"] == "legacy" or os.path.exists(unit_path(PANEL)):
        out["panel"] = active(PANEL)
    return out
