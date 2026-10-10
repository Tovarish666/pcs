#!/usr/bin/env python3
"""Проверки команды proxyveth (pcs/proxyveth/cli.py) и переезда vmodem 4.x → proxyveth usb.

Без root, без сети и без настоящей системы: модули режима подменены записывающими
заглушками, все пути — во временном каталоге, systemctl и ip — записываются.

    python3 tests/test_proxyveth_usb_cli.py
"""
import io
import json
import os
import sys
import tempfile
import types
from contextlib import redirect_stderr, redirect_stdout

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, os.pardir))
from pcs.core import net, util  # noqa: E402
from pcs.core.util import Fail  # noqa: E402
from pcs.proxyveth import cli, usb  # noqa: E402

passed = failed = 0


def check(name, cond, detail=""):
    global passed, failed
    if cond:
        passed += 1
        print("  ok   %s" % name)
    else:
        failed += 1
        print("  FAIL %s%s" % (name, ("\n       " + str(detail)) if detail else ""))


def run(*argv):
    """→ (код, stdout, stderr) команды proxyveth."""
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        rc = cli.main(list(argv))
    util.QUIET[0] = False
    return rc, out.getvalue(), err.getvalue()


def ok_json(*argv):
    rc, out, err = run(*argv, "--json")
    try:
        return rc, json.loads(out)
    except ValueError:
        return rc, {"raw": out, "err": err}


class Fake(types.SimpleNamespace):
    """Модуль режима: записывает вызовы, отдаёт заданное."""

    def __init__(self, name, running=(), rows=None):
        super().__init__(name=name, calls=[], run=list(running), rows=rows or [])
        for f in cli.IFACE + ("rotate",):
            setattr(self, f, self._rec(f))
        self.running = lambda: list(self.run)
        self.live_hosts = lambda: ["9.9.9.9"]

    def _rec(self, f):
        def fn(*a, **k):
            self.calls.append((f, a, k))
            if f == "apply":
                return {"created": [], "recreated": [], "removed": [], "failed": {}}
            if f == "status":
                return self.rows
            if f == "diag":
                return {"n": a[0], "steps": [{"name": "прокси", "ok": True, "text": "есть"}], "verdict": "ok"}
            if f == "doctor":
                return [{"name": "слоты", "ok": True, "text": "48"}]
            if f == "rotate":
                return {"n": a[0], "before": "1.1.1.1", "after": "2.2.2.2", "seconds": 10}
            return None
        return fn

    def names(self):
        return [c[0] for c in self.calls]


TMP = tempfile.mkdtemp()
ETC = os.path.join(TMP, "etc", "proxyveth")
PATHS = dict(ETC=ETC, CONFIG=ETC + "/config.json", TABLE=ETC + "/table.csv", OLD_GW=ETC + "/env",
             RUN=os.path.join(TMP, "run", "proxyveth"), UNITD=os.path.join(TMP, "etc", "systemd", "system"))
PATHS["LOCK"] = PATHS["RUN"] + "/lock"
for k, v in PATHS.items():
    setattr(cli, k, v)
for k in ("ETC", "CONFIG", "TABLE", "RUN"):
    setattr(usb, k, PATHS[k])
usb.MDIR, usb.LIB = ETC + "/usb", os.path.join(TMP, "var", "lib", "proxyveth")
usb.ROOT = os.path.join(TMP, "clean-root")          # vmodem здесь нет
os.makedirs(usb.ROOT)
os.makedirs(PATHS["UNITD"])

said, ran, pinned = [], [], []


class Said:
    path = os.path.join(TMP, "proxyveth.log")

    def __call__(self, msg):
        said.append(msg)


cli.log = usb.log = Said()
cli.need_root = lambda: None
cli.sh = lambda *c, **k: ran.append(c) or types.SimpleNamespace(stdout="active\n", returncode=0)
net_saved = (net.local_octets, net.pin_proxies)
net.local_octets = lambda: set()
net.pin_proxies = lambda hosts, **k: pinned.append(list(hosts)) or True

CSV = "n,real,proxy,enabled\n" + "".join("%d,%d,10.0.0.%d:1080:u%d:p%d\n" % (n, 80 + n, n, n, n) for n in range(1, 6))


def put_cfg(**cfg):
    os.makedirs(ETC, exist_ok=True)
    json.dump(cfg, open(cli.CONFIG, "w"))


def src(text):
    path = os.path.join(TMP, "sheet-%d.csv" % len(os.listdir(TMP)))
    open(path, "w").write(text)
    return path


def test_basics():
    print("команда:")
    rc, out, _ = run()
    check("без аргументов — короткая справка", rc == 0 and "proxyveth sync [--force]" in out and "routes" not in out, out)
    rc, out, err = run("frobnicate")
    check("неизвестная команда — одна строка ✗ по-русски, код 1",
          rc == 1 and err.strip().startswith("✗ нет такой команды") and "Traceback" not in err and not out, err)
    rc, d = ok_json("version")
    check("version --json — конверт ok/data", rc == 0 and d["ok"] and "version" in d["data"], d)
    rc, d = ok_json("status")
    check("режим не задан — ошибка в конверте", rc == 1 and d == {"ok": False, "error": "режим сервера не задан: proxyveth mode usb|gw"}, d)

    print("режим gw ещё не написан:")
    put_cfg(mode="usb", source=src(CSV))
    fake = Fake("usb")
    real_mm = cli.mode_mod
    saved = sys.modules.get("pcs.proxyveth.gw")
    sys.modules["pcs.proxyveth.gw"] = types.ModuleType("pcs.proxyveth.gw")      # без функций §6
    try:
        cli.mode_mod = lambda m: fake if m == "usb" else real_mm(m)
        rc, d = ok_json("mode", "gw", "--yes")
    finally:
        cli.mode_mod = real_mm
        if saved is None:
            sys.modules.pop("pcs.proxyveth.gw", None)
        else:
            sys.modules["pcs.proxyveth.gw"] = saved
    check("понятная ошибка «режим gw ещё не готов»", rc == 1 and "режим gw ещё не готов" in d.get("error", ""), d)
    check("а usb при этом не снят", "teardown" not in fake.names() and json.load(open(cli.CONFIG))["mode"] == "usb")


def with_modes(**mods):
    real = cli.mode_mod

    def mm(m):
        if m not in mods:
            raise Fail("режим %s ещё не готов" % m)
        return mods[m]
    cli.mode_mod = mm
    return lambda: setattr(cli, "mode_mod", real)


def test_sync():
    print("sync — общее для обоих режимов:")
    fake = Fake("usb", running=[1, 2, 3, 4, 5])
    undo = with_modes(usb=fake)
    try:
        put_cfg(mode="usb", source=src(CSV))
        pinned.clear()
        rc, d = ok_json("sync")
        args = [c for c in fake.calls if c[0] == "apply"][-1]
        check("apply получил годные строки таблицы", rc == 0 and d["ok"] and sorted(args[1][0]) == [1, 2, 3, 4, 5], (d, args))
        check("копия таблицы легла в /etc/proxyveth/table.csv", open(cli.TABLE).read() == CSV)
        check("адреса прокси закреплены: из таблицы и у работающих модемов",
              pinned and set(pinned[-1]) == {"10.0.0.%d" % n for n in range(1, 6)} | {"9.9.9.9"}, pinned)

        put_cfg(mode="usb", source=src("n,real,proxy,enabled\n"))
        rc, d = ok_json("sync")
        a = [c for c in fake.calls if c[0] == "apply"][-1]
        check("пустая таблица — считается сломанной, берётся прошлая копия",
              sorted(a[1][0]) == [1, 2, 3, 4, 5] and d["data"]["stale"] and "сломанной" in d["data"]["problems"][0]
              and open(cli.TABLE).read() == CSV, d)

        put_cfg(mode="usb", source=src(CSV.split("\n2,")[0] + "\n"))
        rc, d = ok_json("sync")
        keep = [c for c in fake.calls if c[0] == "apply"][-1][1][1]["keep"]
        check("снести 4 из 5 — подозрительно: без --force не сносим", keep == [2, 3, 4, 5] and d["data"]["held"] == [2, 3, 4, 5], (keep, d))
        rc, d = ok_json("sync", "--force")
        a = [c for c in fake.calls if c[0] == "apply"][-1]
        check("с --force — сносим, force уходит в режим", a[1][1]["keep"] == [] and a[2] == {"force": True}, a)

        bad = CSV.replace("3,83,10.0.0.3:1080:u3:p3", "3,83,10.0.0.3:0:u3:p3")
        put_cfg(mode="usb", source=src(bad))
        rc, d = ok_json("sync")
        a = [c for c in fake.calls if c[0] == "apply"][-1]
        check("кривая строка про работающий модем — модем не трогаем", 3 not in a[1][0] and a[1][1]["keep"] == [3], a)

        pair = "n,real,proxy\n1,81,h:1:u:p1\n201,82,h:1:u:p2\n7,87,h:1:u:p7\n"
        put_cfg(mode="usb", source=src(pair))
        ok_json("sync")
        usb_d = sorted([c for c in fake.calls if c[0] == "apply"][-1][1][0])
        fake_gw = Fake("gw", running=[1])
        undo2 = with_modes(gw=fake_gw)
        put_cfg(mode="gw", source=src(pair))
        ok_json("sync")
        undo2()
        gw_d = sorted([c for c in fake_gw.calls if c[0] == "apply"][-1][1][0])
        check("N и N+200: в usb — брак (таблицы mp.space), в gw — можно", usb_d == [7] and gw_d == [1, 7, 201], (usb_d, gw_d))

        put_cfg(mode="usb", source=src(CSV))
        fake.calls.clear()
        lk = util.lock(cli.LOCK, wait=0)
        try:
            rc, out, err = run("sync", "--quiet")
        finally:
            lk.close()
        check("таймер при занятой блокировке — пропускает заход молча", rc == 0 and "apply" not in fake.names() and not err, (rc, err))
        units = cli.sync_units()
        check("общий таймер proxyveth-sync: при загрузке и раз в 2 минуты",
              "sync --quiet" in units["proxyveth-sync.service"] and "OnUnitInactiveSec=120" in units["proxyveth-sync.timer"]
              and "OnBootSec=15" in units["proxyveth-sync.timer"])

        failing = Fake("usb", running=[1])
        failing.apply = lambda d, cfg, force=False: {"created": [], "recreated": [], "removed": [], "failed": {2: "нет слота"}}
        undo3 = with_modes(usb=failing)
        rc_json, d = ok_json("sync")
        rc_txt, _, _ = run("sync")
        undo3()
        check("--json: неудача модема — в data, конверт ok (ошибка — только когда команда не сработала)",
              rc_json == 0 and d["ok"] and d["data"]["failed"] == {"2": "нет слота"}, d)
        check("без --json — код 1, если модем не вышел", rc_txt == 1)
    finally:
        undo()


def test_commands():
    print("остальные команды:")
    rows = [{"n": 1, "real": 81, "proxy": "10.0.0.1:1080", "state": "ok", "problems": [], "iface": "eth1",
             "ext_ip": "1.2.3.4", "since": 1},
            {"n": 2, "real": 82, "proxy": "10.0.0.2:1080", "state": "warn", "problems": ["прокси не отвечает"],
             "iface": "eth2", "ext_ip": None, "since": 1},
            {"n": 4, "real": None, "proxy": None, "state": "disabled", "problems": ["выключен в таблице"],
             "iface": None, "ext_ip": None, "since": 1}]
    fake = Fake("usb", running=[1, 2], rows=rows)
    undo = with_modes(usb=fake)
    try:
        put_cfg(mode="usb", source=src(CSV))
        rc, d = ok_json("status", "--wan")
        check("status --json — список Row как есть, --wan уходит в режим",
              rc == 0 and d["data"] == rows and ("status", (), {"wan": True}) in fake.calls, d)
        rc, out, _ = run("status")
        check("status в терминале — таблица и сводка", "в порядке 1, с предупреждением 1" in out and "прокси не отвечает" in out, out)
        rc, d = ok_json("problems")
        check("problems — только проблемные (выключенный — не проблема)", [r["n"] for r in d["data"]] == [2], d)

        rc, d = ok_json("diag", "7")
        check("diag N → mod.diag(N)", d["ok"] and ("diag", (7,), {}) in fake.calls, d)
        rc, d = ok_json("diag", "0")
        check("номер вне 1..254 — понятная ошибка", rc == 1 and "1..254" in d["error"], d)

        fake.calls.clear()
        rc, d = ok_json("down", "all")
        check("down all без --yes в скрипте — отказ, ничего не снято", rc == 1 and "--yes" in d["error"] and not fake.calls, d)
        for argv, want in ((("up", "all"), [("up", (None,))]), (("down", "5"), [("down", (5,))]),
                           (("restart", "3"), [("down", (3,)), ("up", (3,))]),
                           (("down", "all", "--yes"), [("down", (None,))])):
            fake.calls.clear()
            rc, d = ok_json(*argv)
            got = [(c[0], c[1]) for c in fake.calls if c[0] in ("up", "down")]
            check("%s → %s" % (" ".join(argv), want), rc == 0 and got == want, (d, got))

        rc, d = ok_json("rotate", "3")
        check("rotate N → mod.rotate(N)", d["ok"] and d["data"]["after"] == "2.2.2.2", d)
        rc, d = ok_json("reboot", "3")
        check("нет функции у режима — так и сказать", rc == 1 and "команды reboot нет" in d["error"], d)

        rc, d = ok_json("doctor")
        names = [c["name"] for c in d["data"]]
        check("doctor: общее + проверки режима", {"режим", "источник таблицы", "таймер proxyveth-sync", "слоты"} <= set(names), names)

        rc, d = ok_json("lint")
        check("lint --json: годные, выключенные, брак", d["ok"] and d["data"]["ok"] == [1, 2, 3, 4, 5], d)
        put_cfg(mode="usb", source=src("n,real,proxy\n1,81,h:0:u:p\n"))
        rc, out, _ = run("lint")
        check("lint в терминале: брак виден, код 1 (даже если вся таблица сломана)", rc == 1 and "порт прокси" in out, out)
        check("а копия после lint сломанной таблицы — прошлая", open(cli.TABLE).read().startswith("n,real,proxy,enabled\n1,81"))
    finally:
        undo()


def test_source():
    print("источник и локальная копия:")
    put_cfg(mode="usb", source=src(CSV))
    os.path.exists(cli.TABLE) and os.unlink(cli.TABLE)
    rc, d = ok_json("source", os.path.join(TMP, "нет-такого.csv"))
    check("нечитаемый источник не запоминается", rc == 1 and "не запоминаю" in d["error"]
          and "нет-такого" not in json.load(open(cli.CONFIG))["source"], d)
    rc, d = ok_json("source", os.path.join(TMP, "нет-такого.csv"), "--force")
    check("с --force — запоминается", rc == 0 and json.load(open(cli.CONFIG))["source"].endswith("нет-такого.csv"), d)
    path = src(CSV)
    rc, d = ok_json("source", path)
    check("читаемый — запомнен, копия положена", d["ok"] and d["data"]["ok"] == [1, 2, 3, 4, 5]
          and open(cli.TABLE).read() == CSV and json.load(open(cli.CONFIG))["source"] == path, d)
    rc, d = ok_json("table", "pull")
    check("table pull — копия из источника", d["ok"] and d["data"]["ok"] == [1, 2, 3, 4, 5], d)
    rc, d = ok_json("table")
    check("table (show) — текст копии и разбор", d["ok"] and d["data"]["text"].startswith("n,real,proxy") and d["data"]["path"] == cli.TABLE, d)
    rc, d = ok_json("source", "local")
    check("source local — главной становится копия", rc == 0 and json.load(open(cli.CONFIG))["source"] == "local")
    rc, d = ok_json("table", "pull")
    check("table pull при source local без ссылки на Google — понятная ошибка", rc == 1 and "ссылки на Google-таблицу нет" in d["error"], d)
    rc, d = ok_json("table", "edit")
    check("table edit — только в терминале", rc == 1 and "терминале" in d["error"], d)
    real_stdin = sys.stdin
    try:
        sys.stdin = io.StringIO("n,real,proxy\n7,8,h.example:1080:u:p\n")
        rc, d = ok_json("table", "put")
        check("table put — копия со stdin (так сохраняет панель)", d["ok"] and d["data"]["ok"] == [7]
              and open(cli.TABLE).read().startswith("n,real,proxy\n7,8,"), d)
        sys.stdin = io.StringIO("что-то не то\n")
        rc, d = ok_json("table", "put")
        check("table put без годных строк — отказ, копия цела", rc == 1 and "годной" in d["error"]
              and "7,8," in open(cli.TABLE).read(), d)
    finally:
        sys.stdin = real_stdin
    open(cli.TABLE, "w").write(CSV)
    os.unlink(cli.TABLE)
    rc, d = ok_json("source", "local")
    check("source local без копии — пустая копия с шапкой", rc == 0 and open(cli.TABLE).read().startswith("n,real,proxy"), d)


def test_mode():
    print("смена режима:")
    fu, fg = Fake("usb", running=[1]), Fake("gw")
    undo = with_modes(usb=fu, gw=fg)
    try:
        put_cfg(mode="usb", source=src(CSV))
        rc, d = ok_json("mode")
        check("mode без аргумента — текущий", d["data"] == {"mode": "usb"}, d)
        rc, d = ok_json("mode", "gw")
        check("смена без --yes в скрипте — отказ, ничего не тронуто",
              rc == 1 and "--yes" in d["error"] and not fu.calls and json.load(open(cli.CONFIG))["mode"] == "usb", d)
        ran.clear()
        rc, d = ok_json("mode", "gw", "--yes")
        check("снят старый режим, поднят новый, модемы приведены к таблице",
              d["ok"] and fu.names() == ["teardown"] and fg.names()[:2] == ["setup", "apply"], (d, fu.names(), fg.names()))
        check("режим записан, таймер включён", json.load(open(cli.CONFIG))["mode"] == "gw"
              and ("systemctl", "enable", "--now", "proxyveth-sync.timer") in ran, ran)
        fu.calls.clear()
        fg.calls.clear()
        rc, d = ok_json("mode", "gw", "--yes")
        check("тот же режим — только setup, без teardown", "teardown" not in fg.names() and "setup" in fg.names(), fg.names())
    finally:
        undo()


def test_migrate():
    print("переезд vmodem 4.x → proxyveth usb (временный корень):")
    root = os.path.join(TMP, "vmodem-root")

    def mk(path, body="", link=None):
        full = os.path.join(root, path.lstrip("/"))
        os.makedirs(os.path.dirname(full), exist_ok=True)
        if link:
            os.symlink(link, full)
        elif body is None:
            os.makedirs(full, exist_ok=True)
        else:
            open(full, "w").write(body)
        return full

    sheet = "https://docs.google.com/spreadsheets/d/AbC/edit#gid=0"
    mk("/etc/vmodem/config.json", json.dumps({
        "source": sheet, "slots": 40, "hostside": "off", "dns": ["1.1.1.1"], "strategy": "window:3",
        "proxy": {"enabled": True, "user": "pu", "pass": "pp", "base_port": 21000},
        "notify": {"telegram_token": "T", "telegram_chat": "C", "webhook": ""}}))
    mk("/etc/vmodem/m/201/proxy.pass", "секрет\n")
    mk("/var/lib/vmodem/last-good.csv", CSV)
    mk("/var/lib/vmodem/health.json", json.dumps({"201": {"verdict": "ok", "told": "ok"}}))
    mk("/run/vmodem/state.json", json.dumps({"modems": {"201": {"slot": "dummy_udc.3"}}, "bad_slots": ["dummy_udc.7"]}))
    for n in (201, 202):
        mk("/run/netns/vm%d" % n)
    mk("/run/netns/foreign")
    g = "/sys/kernel/config/usb_gadget/vm201"
    mk(g + "/UDC", "dummy_udc.3\n")
    mk(g + "/functions/ecm.usb0", None)
    mk(g + "/configs/c.1/ecm.usb0", link=os.path.join(root, g.lstrip("/"), "functions/ecm.usb0"))
    mk("/sys/class/net/vx202")
    units = ("vmodem-dns@.service", "vmodem-px@.socket", "vmodem-px@.service", "vmodem-sb@.service",
             "vmodem-api@.service", "vmodem-host@.service", "vmodem-proxy.service", "vmodem-usbipd.service",
             "vmodem-sync.service", "vmodem-sync.timer")
    for u in units:
        mk("/etc/systemd/system/" + u, "[Unit]\n")
    mk("/etc/systemd/system/timers.target.wants/vmodem-sync.timer", link="/etc/systemd/system/vmodem-sync.timer")
    for f in usb.OLD_FILES:
        mk(f, "старое\n")
    mk("/usr/local/lib/vmodem/sing-box", "sing-box 1.10")
    mk("/usr/local/lib/vmodem/udhcpc-hook", "#!/bin/sh")
    mk("/var/log/vmodem/vmodem.log", "история\n")
    mk("/usr/src/vmodem-dummy-hcd-6.8/dummy_hcd.c", "#define MAX_NUM_UDC 64\n")
    foreign = ["/etc/systemd/system/modlink.service", "/usr/local/bin/modem-interface-setup.sh",
               "/etc/udev/rules.d/90-mpspace.rules", "/usr/local/bin/sing-box", "/etc/systemd/system/proxyveth-virt.service",
               "/home/nodejs/work/auth.mp"]
    for f in foreign:
        mk(f, "чужое\n")

    paths = os.path.join(root, "etc", "proxyveth")
    saved = {k: getattr(cli, k) for k in ("ETC", "CONFIG", "TABLE", "OLD_GW", "RUN", "LOCK", "UNITD")}
    saved_usb = {k: getattr(usb, k) for k in ("ETC", "CONFIG", "TABLE", "RUN", "MDIR", "LIB", "ROOT", "sh",
                                              "setup", "build_dummy_hcd")}
    usb_ran, setups, syncs, builds = [], [], [], []
    cli.ETC, cli.CONFIG, cli.TABLE, cli.OLD_GW = paths, paths + "/config.json", paths + "/table.csv", paths + "/env"
    cli.RUN = os.path.join(root, "run", "proxyveth")
    cli.LOCK, cli.UNITD = cli.RUN + "/lock", os.path.join(root, "etc", "systemd", "system")
    usb.ETC, usb.CONFIG, usb.TABLE, usb.RUN = cli.ETC, cli.CONFIG, cli.TABLE, cli.RUN
    usb.MDIR, usb.LIB, usb.ROOT = paths + "/usb", os.path.join(root, "var", "lib", "proxyveth"), root
    usb.sh = lambda *c, **k: usb_ran.append(c) or types.SimpleNamespace(stdout="", returncode=0)
    usb.setup = lambda cfg: setups.append(dict(cfg))
    order = []
    usb.build_dummy_hcd = lambda: builds.append(1) or order.append("dkms")
    real_sync = cli.do_sync
    cli.do_sync = lambda cfg, mode, mod, force=False: (syncs.append((mode, cfg.get("source"))), order.append("sync")) \
        and {"created": [201]}
    try:
        check("vmodem найден", usb.vmodem_found())
        rc, d = ok_json("status")
        check("до переезда команды подсказывают proxyveth setup", rc == 1 and "proxyveth setup" in d["error"], d)
        rc, d = ok_json("setup")
        cfg = json.load(open(cli.CONFIG))
        check("setup прошёл, режим usb, переезд отмечен", rc == 0 and d["ok"] and d["data"]["mode"] == "usb"
              and d["data"]["migrated"], d)
        check("источник и настройки перенесены, DNS — нет (это хаб)",
              cfg["source"] == sheet and cfg["sheet"] == sheet and cfg["slots"] == 40 and cfg["hostside"] == "off"
              and cfg["strategy"] == "window:3" and cfg["proxy"]["base_port"] == 21000 and cfg["notify"]["telegram_token"] == "T"
              and "dns" not in cfg, cfg)
        check("конфиг — 600, каталог — 700",
              oct(os.stat(cli.CONFIG).st_mode & 0o777) == "0o600" and oct(os.stat(cli.ETC).st_mode & 0o777) == "0o700")
        check("последняя удачная таблица стала локальной копией", open(cli.TABLE).read() == CSV)
        check("здоровье модемов и плохие слоты не потеряны",
              json.load(open(os.path.join(usb.LIB, "usb-health.json")))["201"]["told"] == "ok"
              and json.load(open(os.path.join(cli.RUN, "usb.json")))["bad_slots"] == ["dummy_udc.7"])
        pos = {c: i for i, c in reversed(list(enumerate(usb_ran)))}
        i_timer = pos.get(("systemctl", "disable", "--now", "vmodem-sync.timer"), 10 ** 6)
        check("сначала старый таймер — чтобы не поднял модемы обратно",
              i_timer < pos.get(("ip", "netns", "del", "vm201"), -1), usb_ran[:5])
        check("старые модемы сняты по-старому: юниты, netns vmN, veth vxN",
              ("systemctl", "stop", "vmodem-api@201.service") in usb_ran
              and ("systemctl", "stop", "vmodem-px@202.service", "vmodem-px@202.socket") in usb_ran
              and ("ip", "netns", "del", "vm202") in usb_ran and ("ip", "link", "del", "vx202") in usb_ran, usb_ran)
        gd = os.path.join(root, g.lstrip("/"))
        check("гаджет vm201 вынут из слота и разобран",
              util.rd(gd + "/UDC") == "" and not os.path.islink(gd + "/configs/c.1/ecm.usb0"))
        check("чужой netns не тронут", not any("foreign" in " ".join(c) for c in usb_ran))
        check("новое поставлено (setup режима usb) с перенесёнными настройками",
              len(setups) == 1 and setups[0]["mode"] == "usb" and setups[0]["slots"] == 40, setups)
        left = [f for f in usb.OLD_FILES + usb.OLD_DIRS if os.path.lexists(os.path.join(root, f.lstrip("/")))]
        check("старые файлы и каталоги удалены", not left, left)
        sysd = os.listdir(os.path.join(root, "etc/systemd/system"))
        check("юниты vmodem-* удалены (и в .wants), свои proxyveth-sync — на месте",
              not [u for u in sysd if u.startswith("vmodem")]
              and not os.listdir(os.path.join(root, "etc/systemd/system/timers.target.wants"))
              and {"proxyveth-sync.service", "proxyveth-sync.timer"} <= set(sysd), sysd)
        check("журнал vmodem сохранён в /var/log/proxyveth/vmodem-4",
              not os.path.exists(os.path.join(root, "var/log/vmodem"))
              and open(os.path.join(root, "var/log/proxyveth/vmodem-4/vmodem.log")).read() == "история\n")
        check("dummy_hcd пересобран под своим именем dkms — после того, как модемы подняты",
              builds == [1] and order[-2:] == ["sync", "dkms"], order)
        check("чужое (mp.space, modlink, proxyveth-virt, /usr/local/bin/sing-box) не тронуто",
              all(open(os.path.join(root, f.lstrip("/"))).read() == "чужое\n" for f in foreign))
        check("модемы подняты по-новому сразу, не ждут таймер", syncs == [("usb", sheet)], syncs)
        check("vmodem больше не находится", not usb.vmodem_found())

        setups.clear()
        syncs.clear()
        rc, d = ok_json("setup")
        check("повторный setup — без переезда и без лишнего sync",
              rc == 0 and not d["data"]["migrated"] and len(setups) == 1 and not syncs, d)
    finally:
        for k, v in saved.items():
            setattr(cli, k, v)
        for k, v in saved_usb.items():
            setattr(usb, k, v)
        cli.do_sync = real_sync


if __name__ == "__main__":
    try:
        test_basics()
        test_sync()
        test_commands()
        test_source()
        test_mode()
        test_migrate()
    finally:
        net.local_octets, net.pin_proxies = net_saved
    print("\nитого: ok %d, fail %d" % (passed, failed))
    sys.exit(1 if failed else 0)
