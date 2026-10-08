"""JSON-слой панели поверх API хаба (docs/ARCHITECTURE.md §9).

Тонкий: разбирает запрос, зовёт функцию api и отдаёт конверт как у команд —
{"ok": true, "data": …} или {"ok": false, "error": "…"}. Само действие — в api.

    Panel(api).handle("GET", "/api/overview", query={}, body=None) -> (http-код, конверт)
"""
import csv
import io
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from pcs.core.util import Fail

PARTS = ("proxyveth", "modlink", "hivelink")
DANGER = ("--yes", "--force")            # только с подтверждением номером сервера
ML_FIELDS = ("enabled", "name", "login", "password", "port", "lan_ip", "modem_ip", "reconnect_port", "interval_min")
_CTL = re.compile(r"[\x00-\x1f\x7f]")


class Bad(Exception):
    """Кривой запрос от браузера: HTTP 400 (или code)."""

    def __init__(self, text, code=400):
        super().__init__(text)
        self.code = code


def ok(data=None):
    return {"ok": True, "data": data}


def err(text):
    return {"ok": False, "error": str(text)}


def target(s):
    """'host' или номер ВМ (int)."""
    s = str(s).strip()
    if s == "host":
        return "host"
    if s.isdigit() and 0 < int(s) < 1000000000:
        return int(s)
    raise Bad("сервер: номер ВМ или host")


def _s(v, what, maxlen=500, empty=True):
    if v is None:
        v = ""
    if isinstance(v, bool):
        v = "1" if v else "0"
    if isinstance(v, (int, float)):
        v = str(v)
    if not isinstance(v, str):
        raise Bad("%s: нужна строка" % what)
    if _CTL.search(v) or len(v) > maxlen:
        raise Bad("%s: управляющие символы или слишком длинно" % what)
    if not empty and not v.strip():
        raise Bad("%s: пусто" % what)
    return v


def envelope(x):
    """api.run отдаёт конверт; если пришли голые данные — завернуть."""
    if isinstance(x, dict) and "ok" in x and ("data" in x or "error" in x):
        return {"ok": bool(x["ok"]), "data": x.get("data")} if x["ok"] else err(x.get("error") or "ошибка без текста")
    return ok(x)


def rows_of(data, keys=("rows", "proxies", "items", "modems", "list")):
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for k in keys:
            if isinstance(data.get(k), list):
                return data[k]
    return []


def to_csv(rows):
    cols = []
    for r in rows:
        for k in r:
            if k not in cols:
                cols.append(k)
    f = io.StringIO()
    w = csv.writer(f, lineterminator="\n")
    w.writerow(cols)
    for r in rows:
        w.writerow(["" if r.get(c) is None else r.get(c) for c in cols])
    return f.getvalue()


class Panel:
    def __init__(self, api, cache_ttl=4.0, audit=None):
        self.api = api
        self.cache_ttl = cache_ttl
        self.audit = audit or (lambda *a: None)
        self._cache = {}
        self._clock = threading.Lock()
        self._pool = ThreadPoolExecutor(max_workers=8, thread_name_prefix="pcs-web-api")
        self.routes = [
            ("GET", r"/api/overview", self.overview),
            ("GET", r"/api/servers/(\d+)", self.server),
            ("GET", r"/api/servers/(\d+)/proxyveth", self.proxyveth),
            ("GET", r"/api/servers/(\d+)/proxyveth/table", self.pv_table),
            ("POST", r"/api/servers/(\d+)/proxyveth/table", self.pv_table_save),
            ("GET", r"/api/servers/(\d+)/modlink", self.modlink),
            ("POST", r"/api/servers/(\d+)/modlink/save", self.ml_save),
            ("POST", r"/api/servers/(\d+)/modlink/reveal", self.ml_reveal),
            ("POST", r"/api/servers/(\d+)/modlink/export", self.ml_export),
            ("POST", r"/api/servers", self.create),
            ("POST", r"/api/servers/(\d+)/delete", self.delete),
            ("POST", r"/api/run", self.run),
            ("POST", r"/api/targets/(host|\d+)/dns", self.dns),
            ("POST", r"/api/targets/(host|\d+)/passwd", self.passwd),
            ("POST", r"/api/targets/(host|\d+)/key", self.key),
            ("POST", r"/api/targets/(host|\d+)/mpspace", self.mpspace),
            ("POST", r"/api/targets/(host|\d+)/update", self.update),
            ("GET", r"/api/jobs/([A-Za-z0-9_.:-]{1,80})", self.job),
        ]
        self.routes = [(m, re.compile(p + r"\Z"), fn) for m, p, fn in self.routes]

    # ── разбор ──────────────────────────────────────────────────────────────

    def handle(self, method, path, query=None, body=None, user="-"):
        """-> (http-код, конверт). Fail из api — 200 с ok=false: ответ дошёл, дело не вышло."""
        allowed = []
        for m, rx, fn in self.routes:
            mt = rx.match(path)
            if not mt:
                continue
            if m != method:
                allowed.append(m)
                continue
            if method == "POST":
                self.audit(user, method, path)
            try:
                return 200, fn(*mt.groups(), query=query or {}, body=body if isinstance(body, dict) else {})
            except Bad as e:
                return e.code, err(e)
            except Fail as e:
                return 200, err(e)
        if allowed:
            return 405, err("метод %s здесь не годится" % method)
        return 404, err("нет такого адреса")

    def _call(self, fn, *a, **kw):
        """Вызов api с Fail → конверт ошибки (для сводных ответов из нескольких частей)."""
        try:
            return ok(fn(*a, **kw))
        except Fail as e:
            return err(e)

    def _run(self, sid, part, args, inp=None):
        """api.run(id, part, args) → конверт. sid='host' → id=None."""
        sid = None if sid == "host" else sid
        try:
            if inp is None:
                return envelope(self.api.run(sid, part, list(args)))
            try:
                return envelope(self.api.run(sid, part, list(args), input=inp))
            except TypeError:
                return err("хаб не умеет передавать данные команде (нужен api.run(…, input=)) — обновите PCS")
        except Fail as e:
            return err(e)

    def _cached(self, key, fn):
        now = time.monotonic()
        with self._clock:
            hit = self._cache.get(key)
            if hit and now - hit[0] < self.cache_ttl:
                return hit[1]
        val = fn()
        with self._clock:
            self._cache[key] = (time.monotonic(), val)
        return val

    def forget(self):
        with self._clock:
            self._cache.clear()

    def _parallel(self, **jobs):
        fut = {k: self._pool.submit(*v) for k, v in jobs.items()}
        return {k: f.result() for k, f in fut.items()}

    # ── обзор ───────────────────────────────────────────────────────────────

    def overview(self, query, body):
        def get():
            r = self._parallel(host=(self._call, self.api.host_info), servers=(self._call, self.api.servers))
            return {"host": r["host"].get("data"), "host_error": r["host"].get("error"),
                    "servers": r["servers"].get("data") or [], "servers_error": r["servers"].get("error"),
                    "ts": int(time.time())}
        return ok(self._cached("overview", get))

    def server(self, sid, query, body):
        return ok(self.api.server(int(sid)))

    # ── proxyveth ───────────────────────────────────────────────────────────

    def proxyveth(self, sid, query, body):
        sid = int(sid)
        st = ["status"] + (["--wan"] if query.get("wan") in ("1", "true") else [])
        jobs = {"status": (self._run, sid, "proxyveth", st)}
        if query.get("only") != "status":
            jobs["source"] = (self._run, sid, "proxyveth", ["source"])
        return ok(self._parallel(**jobs))

    def pv_table(self, sid, query, body):
        e = self._run(int(sid), "proxyveth", ["table", "show"])
        if not e["ok"]:
            return e
        d = e["data"]
        if isinstance(d, str):
            text = d
        elif isinstance(d, dict) and isinstance(d.get("csv") or d.get("text"), str):
            text = d.get("csv") or d.get("text")
        elif rows_of(d):
            text = to_csv(rows_of(d))
        else:
            text = ""
        return ok({"csv": text, "meta": {k: v for k, v in d.items() if k not in ("csv", "text", "rows")}
                   if isinstance(d, dict) else {}})

    def pv_table_save(self, sid, query, body):
        sid = int(sid)
        text = body.get("csv")
        if not isinstance(text, str) or len(text) > 2000000 or "\0" in text:
            raise Bad("таблица: нужен текст CSV")
        if not text.strip():
            raise Bad("таблица пустая — не сохраняю")
        saved = self._run(sid, "proxyveth", ["table", "put"], inp=text if text.endswith("\n") else text + "\n")
        out = {"saved": saved}
        if saved["ok"] and body.get("make_main"):
            out["source"] = self._run(sid, "proxyveth", ["source", "local"])
        if not saved["ok"]:
            return err(saved["error"])
        if "source" in out and not out["source"]["ok"]:
            return err("сохранено, но главной не стала: %s" % out["source"]["error"])
        return ok(out)

    # ── modlink ─────────────────────────────────────────────────────────────

    def modlink(self, sid, query, body):
        sid = int(sid)
        r = self._parallel(list=(self._run, sid, "modlink", ["list"]), status=(self._run, sid, "modlink", ["status"]))
        if r["list"]["ok"]:
            rows = []
            for row in rows_of(r["list"]["data"]):
                if isinstance(row, dict):
                    row = dict(row)
                    pw = row.pop("password", None)      # пароли — только по явному запросу (§8)
                    row["password_set"] = bool(pw) or bool(row.get("password_set"))
                    rows.append(row)
            r["list"] = ok(rows)
        return ok(r)

    def ml_reveal(self, sid, query, body):
        sid = int(sid)
        rid = str(body.get("id", "")).strip()
        if not rid.isdigit():
            raise Bad("номер строки modlink")
        e = self._run(sid, "modlink", ["list"])
        if not e["ok"]:
            return e
        row = next((r for r in rows_of(e["data"]) if isinstance(r, dict) and str(r.get("id")) == rid), None)
        if row is None:
            return err("строки %s нет — обновите таблицу" % rid)
        pw = row.get("password")
        if isinstance(pw, str) and pw and set(pw) - set("*•"):
            return ok({"id": int(rid), "password": pw})
        # list прячет пароли — берём из export по порту и логину
        x = self._run(sid, "modlink", ["export"])
        if x["ok"]:
            for line in _export_lines(x["data"]):
                parts = line.split("\t")[0].split(":")
                if len(parts) >= 4 and parts[1] == str(row.get("port")) and parts[2] == str(row.get("login")):
                    return ok({"id": int(rid), "password": ":".join(parts[3:])})
        return err("modlink не отдал пароль строки %s" % rid)

    def ml_export(self, sid, query, body):
        e = self._run(int(sid), "modlink", ["export"])
        if not e["ok"]:
            return e
        return ok({"text": "\n".join(_export_lines(e["data"]))})

    def ml_save(self, sid, query, body):
        """Правки таблицы modlink разом: удалить, поменять, добавить, затем apply."""
        sid = int(sid)
        steps, fail = [], False

        def step(what, args):
            nonlocal fail
            e = self._run(sid, "modlink", args)
            steps.append({"what": what, "ok": e["ok"], "error": e.get("error"), "data": e.get("data")})
            fail = fail or not e["ok"]
            return e

        dels = body.get("del") or []
        sets = body.get("set") or []
        adds = body.get("add") or []
        if not isinstance(dels, list) or not isinstance(sets, list) or not isinstance(adds, list):
            raise Bad("modlink: del/set/add — списки")
        for rid in dels:
            step("удалить %s" % rid, ["del", _rid(rid)])
        for ch in sets:
            if not isinstance(ch, dict) or not isinstance(ch.get("fields"), dict):
                raise Bad("modlink: правка строки — {id, fields}")
            rid, fields = _rid(ch.get("id")), ch["fields"]
            if "enabled" in fields:
                step("%s %s" % ("включить" if fields["enabled"] else "выключить", rid),
                     ["enable" if fields["enabled"] else "disable", rid])
            kv = ["%s=%s" % (k, _ml_val(k, v)) for k, v in fields.items() if k != "enabled" and _ml_field(k)]
            if kv:
                step("строка %s" % rid, ["set", rid] + kv)
        for row in adds:
            if not isinstance(row, dict):
                raise Bad("modlink: новая строка — объект")
            e = step("добавить %s" % (row.get("lan_ip") or "?"), _ml_add(row))
            if e["ok"] and row.get("enabled") is False and isinstance(e.get("data"), dict) and "id" in e["data"]:
                step("выключить %s" % e["data"]["id"], ["disable", str(e["data"]["id"])])
        applied = None
        if body.get("apply", True) and not fail:
            applied = step("применить", ["apply"])
        res = {"steps": steps, "applied": bool(applied and applied["ok"])}
        if fail:
            bad = [s for s in steps if not s["ok"]]
            return {"ok": False, "error": "%s: %s" % (bad[0]["what"], bad[0]["error"]), "data": res}
        return ok(res)

    # ── общее: команда части на сервере или хосте ──────────────────────────

    def run(self, query, body):
        t = target(body.get("target", ""))
        part = body.get("part")
        if part not in PARTS:
            raise Bad("часть: %s" % ", ".join(PARTS))
        args = body.get("args")
        if not isinstance(args, list) or not args or len(args) > 40:
            raise Bad("args — непустой список")
        args = [_s(a, "аргумент", 2000) for a in args if a != "--json"]
        if any(a in DANGER for a in args) and str(body.get("confirm", "")).strip() != str(t):
            raise Bad("опасное действие: подтвердите номером сервера (%s)" % t, 403)
        e = self._run(t, part, args)
        if args[0] in ("mode", "sync", "up", "down", "install", "uninstall"):
            self.forget()
        return e

    # ── хост и серверы: DNS, доступы, софт, обновление ──────────────────────

    def dns(self, t, query, body):
        dns = _s(body.get("dns"), "DNS", 200).strip() or None
        if dns and not re.fullmatch(r"[0-9a-fA-F.:\s,]+", dns):
            raise Bad("DNS: адреса через пробел, например 1.1.1.1 8.8.8.8")
        return ok(self.api.dns_fix(target(t), dns=dns.replace(",", " ") if dns else None))

    def passwd(self, t, query, body):
        pw = _s(body.get("password"), "пароль", 200, empty=False)
        return ok(self.api.set_password(target(t), pw))

    def key(self, t, query, body):
        if body.get("rotate"):
            return ok(self.api.set_key(target(t), rotate=True))
        pub = _s(body.get("pubkey"), "ключ", 16000, empty=False).strip()
        if not re.match(r"(ssh-(ed25519|rsa|dss)|ecdsa-sha2-nistp\d+|sk-[\w@.-]+) [A-Za-z0-9+/=]+", pub):
            raise Bad("ключ: строка вида «ssh-ed25519 AAAA… имя»")
        return ok(self.api.set_key(target(t), pubkey=pub))

    def mpspace(self, t, query, body):
        action = body.get("action")
        if action not in ("install", "auth", "check"):
            raise Bad("mp.space: install, auth или check")
        kw = {}
        if action == "auth":
            kw["auth"] = _s(body.get("auth"), "auth.mp", 100000, empty=False).strip()
        if action == "install" and body.get("auth"):
            kw["auth"] = _s(body.get("auth"), "auth.mp", 100000).strip()
        self.forget()
        return ok(_job_or(self.api.mpspace(target(t), action, **kw)))

    def update(self, t, query, body):
        fn = getattr(self.api, "update", None)
        if fn is None:
            return err("хаб пока не умеет обновлять из панели (нет api.update) — pcs update в терминале")
        self.forget()
        return ok(_job_or(fn(target(t))))

    # ── ВМ: создать, удалить; задания ───────────────────────────────────────

    def create(self, query, body):
        name = _s(body.get("name"), "имя", 63, empty=False).strip()
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]{0,62}", name):
            raise Bad("имя: латиница, цифры и дефис (это имя хоста)")
        p = {"name": name,
             "cores": _int(body.get("cores", 4), "ядра", 1, 256),
             "ram_gb": _int(body.get("ram_gb", 8), "ОЗУ, ГБ", 1, 4096),
             "disk_gb": _int(body.get("disk_gb", 40), "диск, ГБ", 8, 65536),
             "mode": body.get("mode", "usb"),
             "sheet": _s(body.get("sheet"), "ссылка на таблицу", 2000).strip() or None,
             "mpspace": bool(body.get("mpspace"))}
        if p["mode"] not in ("usb", "gw"):
            raise Bad("режим: usb или gw")
        if p["sheet"] and not re.match(r"https://", p["sheet"]):
            raise Bad("ссылка на таблицу: https://docs.google.com/…")
        jid = self.api.create_server(p)
        self.forget()
        return ok({"job": jid})

    def delete(self, sid, query, body):
        confirm = str(body.get("confirm", "")).strip()
        if confirm != sid:
            raise Bad("подтвердите удаление номером ВМ (%s)" % sid, 403)
        r = self.api.delete_server(int(sid), confirm)
        self.forget()
        return ok(_job_or(r))

    def job(self, jid, query, body):
        return ok(self.api.job(jid))


# ── мелочи ──────────────────────────────────────────────────────────────────

def _int(v, what, lo, hi):
    try:
        n = int(v)
    except (TypeError, ValueError):
        raise Bad("%s: нужно число" % what)
    if not lo <= n <= hi:
        raise Bad("%s: от %d до %d" % (what, lo, hi))
    return n


def _rid(v):
    v = str(v).strip()
    if not v.isdigit():
        raise Bad("номер строки modlink")
    return v


def _ml_field(k):
    if k not in ML_FIELDS:
        raise Bad("modlink: нет поля %s" % k)
    return True


def _ml_val(k, v):
    v = _s(v, k, 200).strip()
    if k in ("port", "reconnect_port") and v != "auto" and not (v.isdigit() and 0 < int(v) < 65536):
        raise Bad("%s: порт 1–65535 или auto" % k)
    if k == "interval_min" and not (v.isdigit() and int(v) < 100000):
        raise Bad("интервал: минуты, 0 — выключен")
    if k in ("lan_ip", "modem_ip") and v and not re.fullmatch(r"\d{1,3}(\.\d{1,3}){3}", v):
        raise Bad("%s: адрес IPv4" % k)
    if k in ("login", "password") and (not v or " " in v or ":" in v):
        raise Bad("%s: без пробелов и двоеточий, не пустой" % k)
    return v


def _ml_add(row):
    if not row.get("lan_ip"):
        raise Bad("новая строка: нужен LAN IP")
    a = ["add", "--lan-ip", _ml_val("lan_ip", row["lan_ip"])]
    for k, flag, dflt in (("name", "--name", None), ("login", "--login", None), ("password", "--password", "gen"),
                          ("port", "--port", "auto"), ("modem_ip", "--modem-ip", None),
                          ("reconnect_port", "--reconnect-port", "auto"), ("interval_min", "--interval", None)):
        v = row.get(k)
        v = "" if v is None else str(v).strip()
        if v == "" and dflt:
            v = dflt
        if v != "":
            a += [flag, v if (k == "password" and v == "gen") else _ml_val(k, v)]
    return a


def _export_lines(d):
    if isinstance(d, str):
        return [l for l in d.splitlines() if l.strip()]
    if isinstance(d, dict):
        for k in ("lines", "text"):
            if k in d:
                return _export_lines(d[k])
    if isinstance(d, list):
        return [str(x) for x in d if str(x).strip()]
    return []


def _job_or(r):
    """mpspace/update/delete: id задания (строка или число) или готовый словарь."""
    if isinstance(r, (str, int)) and not isinstance(r, bool):
        return {"job": r}
    return {"result": r}
