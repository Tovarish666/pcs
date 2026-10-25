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

from pcs.core.util import Fail, jload, jsave
from pcs.hub.node import summarize
from pcs.web import browser as br_mod

PARTS = ("proxyveth", "modlink", "hivelink")
DANGER = ("--yes", "--force")            # только с подтверждением номером сервера
ML_FIELDS = ("enabled", "name", "login", "password", "port", "lan_ip", "modem_ip", "reconnect_port", "reboot_port",
             "interval_min", "protect")
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


def rows_of(data, keys=("rows", "proxies", "suggest", "items", "modems", "list")):
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
    OV_FRESH = 8.0           # с: обзор моложе — как есть; старше — сразу прежний, свежий собирается в фоне

    def __init__(self, api, cache_ttl=4.0, audit=None, ov_path=None, browser=None):
        self.api = api
        self.browser = browser     # браузер вкладок mp.space и ЮMoney (pcs.web.browser.Manager или фейк)
        self.cache_ttl = cache_ttl
        self.audit = audit or (lambda *a: None)
        self.ov_path = ov_path     # последний обзор на диске: после перезапуска панели — сразу он
        self._ov = None            # (monotonic, данные)
        self._ov_busy = False
        self._ov_fresh_next = False
        self._live = {}            # цель → (time, сводка модемов) из последней таблицы: обзор не отстаёт от неё
        self._cache = {}
        self._clock = threading.Lock()
        self._pool = ThreadPoolExecutor(max_workers=8, thread_name_prefix="pcs-web-api")
        self.routes = [
            ("GET", r"/api/overview", self.overview),
            ("GET", r"/api/servers/(\d+)", self.server),
            ("GET", r"/api/targets/(host|\d+)", self.target_info),
            ("GET", r"/api/targets/(host|\d+)/proxyveth", self.proxyveth),              # и на ВМ, и на хосте
            ("GET", r"/api/targets/(host|\d+)/proxyveth/table", self.pv_table),
            ("POST", r"/api/targets/(host|\d+)/proxyveth/table", self.pv_table_save),
            ("GET", r"/api/targets/(host|\d+)/modlink", self.modlink),        # modlink — и на ВМ, и на хосте
            ("POST", r"/api/targets/(host|\d+)/modlink/save", self.ml_save),
            ("POST", r"/api/targets/(host|\d+)/modlink/reveal", self.ml_reveal),
            ("POST", r"/api/targets/(host|\d+)/modlink/export", self.ml_export),
            ("POST", r"/api/targets/(host|\d+)/modlink/settings", self.ml_settings),
            ("POST", r"/api/targets/(host|\d+)/modlink/import", self.ml_import),
            ("POST", r"/api/servers", self.create),
            ("POST", r"/api/servers/(\d+)/delete", self.delete),
            ("POST", r"/api/servers/(\d+)/power", self.power),
            ("POST", r"/api/servers/(\d+)/forget", self.forget_server),
            ("POST", r"/api/servers/(\d+)/color", self.color),
            ("POST", r"/api/servers/(\d+)/hostkey", self.hostkey),
            ("POST", r"/api/servers/(\d+)/reconnect", self.reconnect),
            ("GET", r"/api/vms", self.vms_other),
            ("POST", r"/api/vms/(\d+)/adopt", self.adopt),
            ("GET", r"/api/colors", self.colors),
            ("POST", r"/api/run", self.run),
            ("POST", r"/api/targets/(host|\d+)/dns", self.dns),
            ("POST", r"/api/targets/(host|\d+)/passwd", self.passwd),
            ("POST", r"/api/targets/(host|\d+)/key", self.key),
            ("POST", r"/api/targets/(host|\d+)/mpspace", self.mpspace),
            ("POST", r"/api/targets/(host|\d+)/update", self.update),
            ("GET", r"/api/jobs", self.jobs),
            ("GET", r"/api/self", self.self_info),
            ("GET", r"/api/nodes", self.nodes),
            ("POST", r"/api/nodes/join", self.node_join),
            ("POST", r"/api/nodes/([a-z0-9-]{1,40})/remove", self.node_remove),
            ("POST", r"/api/nodes/([a-z0-9-]{1,40})/login", self.node_login),
            ("POST", r"/api/nodes/([a-z0-9-]{1,40})/color", self.node_color),
            ("GET", r"/api/nodes/([a-z0-9-]{1,40})/(.+)", self.node_get),
            ("POST", r"/api/nodes/([a-z0-9-]{1,40})/(.+)", self.node_post),
            ("GET", r"/api/jobs/([A-Za-z0-9_.:-]{1,80})", self.job),
            ("POST", r"/api/jobs/([A-Za-z0-9_.:-]{1,80})/retry", self.job_retry),
            ("GET", r"/api/browser/(mp|ym|md)/(host|\d+)", self.br_status),
            ("POST", r"/api/browser/(mp|ym)/(host|\d+)/(proxy|check|start|stop|open|links)", self.br_act),
            ("POST", r"/api/browser/md/(host|\d+)/stop", self.br_md_stop),
            ("POST", r"/api/browser/md/(host|\d+)/modem", self.br_modem),
            ("POST", r"/api/browser/install", self.br_install),
            ("GET", r"/api/router", self.router_get),
            ("GET", r"/api/router/(ifaces|nat|leases|log|conf)", self.router_list),
            ("POST", r"/api/router/(save|forget|iface|nat_add|nat_set|nat_del|lease_static|reboot)", self.router_act),
            ("POST", r"/api/router/webfig", self.router_webfig),
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
        """После действия: следующий обзор — свежий и сразу (видно, что вышло), а не из запаса."""
        with self._clock:
            self._cache.clear()
            self._ov = None
            self._ov_fresh_next = True

    def _parallel(self, **jobs):
        fut = {k: self._pool.submit(*v) for k, v in jobs.items()}
        return {k: f.result() for k, f in fut.items()}

    # ── обзор ───────────────────────────────────────────────────────────────

    def overview(self, query, body):
        """Обзор хоста и ВМ. Опрос всех ВМ долгий (ВМ с 40 модемами — 10+ с), поэтому
        stale-while-revalidate: свежий (моложе OV_FRESH) — как есть; иначе сразу прежний с
        "stale": true, а новый собирается в фоне. Панель увидит stale и спросит ещё раз."""
        with self._clock:
            hit, fresh_next = self._ov, self._ov_fresh_next
        if hit and time.monotonic() - hit[0] < self.OV_FRESH:
            return ok(hit[1])
        if hit is None and not fresh_next and self.ov_path:
            d = jload(self.ov_path, None)
            if isinstance(d, dict) and isinstance(d.get("servers"), list):
                hit = (-1e9, d)
                with self._clock:
                    if self._ov is None:
                        self._ov = hit
        if hit is None:
            return ok(self._ov_build())
        self._ov_refresh()
        return ok(dict(hit[1], stale=True, age=max(0, int(time.time()) - int(hit[1].get("ts") or 0))))

    def _ov_build(self):
        t0 = time.time()
        r = self._parallel(host=(self._call, self.api.host_info), servers=(self._call, self.api.servers))
        d = {"host": r["host"].get("data"), "host_error": r["host"].get("error"),
             "servers": r["servers"].get("data") or [], "servers_error": r["servers"].get("error"),
             "ts": int(time.time())}
        with self._clock:
            self._ov_live(d, since=t0)     # таблицу смотрели, пока обзор собирался, — её сводка новее
            self._ov = (time.monotonic(), d)
            self._ov_fresh_next = False
        if self.ov_path and not d["host_error"] and not d["servers_error"]:
            try:
                jsave(self.ov_path, d, 0o600)
            except OSError:
                pass
        return d

    def _ov_live(self, d, since=0.0):
        """Сводки модемов из таблиц (новее since) → в обзор d. Под self._clock."""
        for k, (ts, m) in self._live.items():
            if ts < since:
                continue
            if k == "host":
                h = d.get("host")
                if isinstance(h, dict):
                    h["modems"] = m
                    if isinstance(h.get("proxyveth"), dict):
                        h["proxyveth"]["modems"] = m
                continue
            for s in d.get("servers") or []:
                if str(s.get("id")) == k and s.get("state") == "running":
                    s["modems"] = m

    def live(self, t, rows):
        """Свежая таблица модемов цели t: дерево, шапка ВМ и сводка хоста берут её же счёт —
        а не тот, что был при последней сборке обзора (до 8 с и больше)."""
        with self._clock:
            self._live[str(t)] = (time.time(), summarize(rows))
            if self._ov:
                self._ov_live(self._ov[1])

    def _ov_refresh(self):
        with self._clock:
            if self._ov_busy:
                return
            self._ov_busy = True

        def run():
            try:
                self._ov_build()
            except Exception:          # фон не должен ронять панель; следующий запрос попробует снова
                pass
            finally:
                with self._clock:
                    self._ov_busy = False
        threading.Thread(target=run, name="pcs-web-overview", daemon=True).start()

    def server(self, sid, query, body):
        return ok(self.api.server(int(sid)))

    def target_info(self, t, query, body):
        fn = getattr(self.api, "target_info", None)
        t = target(t)
        if fn:
            return ok(fn(t))
        return ok(self.api.host_info() if t == "host" else self.api.server(t))

    # ── proxyveth ───────────────────────────────────────────────────────────

    def proxyveth(self, sid, query, body):
        sid = target(sid)
        st = ["status"] + (["--wan"] if query.get("wan") in ("1", "true") else [])
        jobs = {"status": (self._run, sid, "proxyveth", st)}
        if query.get("only") != "status":
            jobs["source"] = (self._run, sid, "proxyveth", ["source"])
        out = self._parallel(**jobs)
        if out["status"].get("ok"):
            self.live(sid, rows_of(out["status"].get("data")))
        return ok(out)

    def pv_table(self, sid, query, body):
        e = self._run(target(sid), "proxyveth", ["table", "show"])
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
        sid = target(sid)
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
        """list (без паролей) + status. pending — правки есть, но `modlink apply` ещё не было."""
        sid = target(sid)
        if query.get("only") == "status":
            return ok({"status": self._run(sid, "modlink", ["status"])})
        r = self._parallel(list=(self._run, sid, "modlink", ["list"]), status=(self._run, sid, "modlink", ["status"]),
                           settings=(self._run, sid, "modlink", ["settings"]))
        if r["list"]["ok"]:
            d = r["list"]["data"]
            rows = []
            for row in rows_of(d):
                if isinstance(row, dict):
                    row = dict(row)
                    if row.pop("password", None):      # пароли — только по явному запросу (§8)
                        row["password_set"] = True
                    rows.append(row)
            r["list"] = ok({"rows": rows, "pending": bool(isinstance(d, dict) and d.get("pending"))})
        return ok(r)

    def ml_reveal(self, sid, query, body):
        """Пароль одной строки: `modlink list --show-pass`, запасной путь — export."""
        sid = target(sid)
        rid = str(body.get("id", "")).strip()
        if not rid.isdigit():
            raise Bad("номер строки modlink")
        e = self._run(sid, "modlink", ["list", "--show-pass"])
        if not e["ok"]:
            return e
        row = next((r for r in rows_of(e["data"]) if isinstance(r, dict) and str(r.get("id")) == rid), None)
        if row is None:
            return err("строки %s нет — обновите таблицу" % rid)
        pw = row.get("password")
        if isinstance(pw, str) and pw and set(pw) - set("*•"):
            return ok({"id": int(rid), "password": pw})
        x = self._run(sid, "modlink", ["export"])
        if x["ok"]:
            for line in _export_lines(x["data"]):
                parts = line.split("\t")[0].split(":")
                if len(parts) >= 4 and parts[1] == str(row.get("port")) and parts[2] == str(row.get("login")):
                    return ok({"id": int(rid), "password": ":".join(parts[3:])})
        return err("modlink не отдал пароль строки %s" % rid)

    def ml_settings(self, sid, query, body):
        """Общие настройки modlink: base_port и mode (порты ВСЕХ строк заново), lan_ip, wan_ip ("" — авто),
        traffic (tcp+udp | tcp), access_log, wan_check, speedtest (вкл/выкл)."""
        kv = []
        if body.get("base_port") not in (None, ""):
            kv.append("base_port=%d" % _int(body["base_port"], "базовый порт", 1, 65533))
        for k in ("lan_ip", "wan_ip"):
            if k in body:
                v = _s(body[k], k, 40).strip()
                if v and v != "auto" and not re.fullmatch(r"\d{1,3}(\.\d{1,3}){3}", v):
                    raise Bad("%s: адрес IPv4 или пусто (авто)" % k)
                kv.append("%s=%s" % (k, v or "auto"))
        if "mode" in body:
            if body["mode"] not in ("triples", "shared"):
                raise Bad("режим портов: triples или shared")
            kv.append("mode=" + body["mode"])
        if "traffic" in body:
            if body["traffic"] not in ("tcp+udp", "tcp"):
                raise Bad("трафик: tcp+udp или tcp")
            kv.append("traffic=" + body["traffic"])
        for k in ("access_log", "wan_check", "speedtest"):
            if k in body:
                kv.append("%s=%s" % (k, "on" if body[k] is True else "off" if body[k] is False else _bad(k)))
        if not kv:
            raise Bad("настройки: base_port, mode, traffic, lan_ip, wan_ip, access_log, wan_check, speedtest")
        return self._run(target(sid), "modlink", ["settings"] + kv)

    def ml_export(self, sid, query, body):
        """Строки для клиента (what: proxy | reboot | reconnect | all — как «Копировать» в modlink для Windows);
        table — строки таблицы proxyveth (виртуальные из этих настоящих)."""
        table = bool(body.get("table"))
        what = body.get("what") or "all"
        if what not in ("proxy", "reboot", "reconnect", "all"):
            raise Bad("что копировать: proxy, reboot, reconnect или all")
        e = self._run(target(sid), "modlink", ["export", "--table"] if table else ["export", "--what", what])
        if not e["ok"]:
            return e
        d = e["data"] if isinstance(e["data"], dict) else {}
        lines = _export_lines(e["data"])
        # по колонкам — чтобы вставлялось в Google-таблицу по ячейкам
        if table:
            body_ = list(csv.reader(io.StringIO("\n".join(lines))))[1:]
            cols, rows = ["n", "real", "proxy"], [r[:3] for r in body_ if len(r) >= 3]
        elif isinstance(d.get("rows"), list) and isinstance(d.get("columns"), list):
            cols, rows = d["columns"], d["rows"]
        else:                                     # старый modlink: прокси и реконнект
            cols = ["Ip:port:log:pass SOCKS5", "ReConnect"]
            rows = [(ln.split("\t") + [""])[:2] for ln in lines]
        return ok({"text": "\n".join(lines), "warnings": d.get("warnings") or [], "columns": cols, "rows": rows,
                   "what": what})

    def ml_import(self, sid, query, body):
        """Конфиг по таблице: строки «Ip:port:log:pass | ReBoot | ReConnect» (text — вставка, или url —
        лист Google-таблицы) → таблица modlink, где каждая работает ровно так. dry — только план."""
        url = str(body.get("url") or "").strip()
        text = body.get("text")
        if url:
            if not re.fullmatch(r"https?://[^\s\"'<>]{1,2000}", url):
                raise Bad("ссылка: https://…")
            args, inp = ["import", url], None
        else:
            if not isinstance(text, str) or not text.strip():
                raise Bad("вставьте строки таблицы или ссылку на лист")
            if len(text) > 512 * 1024 or "\0" in text:
                raise Bad("таблица: слишком большая или с мусором")
            args, inp = ["import", "-"], text
        how = body.get("map") or "number"
        if how not in ("number", "order"):
            raise Bad("модемы строк: number или order")
        args += ["--map", how]
        args += ["--replace"] if body.get("replace") is True else []
        args += ["--dry-run"] if body.get("dry", True) is not False else []
        args += ["--on"] if body.get("on") is True else []
        return self._run(target(sid), "modlink", args, inp)

    def ml_save(self, sid, query, body):
        """Правки таблицы modlink разом: удалить, поменять, добавить, затем apply."""
        sid = target(sid)
        steps, fail = [], False

        def step(what, args, inp=None):
            nonlocal fail
            e = self._run(sid, "modlink", args, inp)
            steps.append({"what": what, "ok": e["ok"], "error": e.get("error"), "data": e.get("data")})
            fail = fail or not e["ok"]
            return e

        dels = body.get("del") or []
        sets = body.get("set") or []
        adds = body.get("add") or []
        if not isinstance(dels, list) or not isinstance(sets, list) or not isinstance(adds, list):
            raise Bad("modlink: del/set/add — списки")
        for rid in dels:
            step("удалить %s" % rid, ["del", _rid(rid), "--yes"])    # подтверждено в панели
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
            args, secret = _ml_add(row)
            e = step("добавить %s" % (row.get("lan_ip") or "?"), args, secret)
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

    # ── браузер на одно окно через прокси (вкладки mp.space и ЮMoney) ──────────
    # Браузер всегда на этой машине (хабе), профиль — свой на хост/ВМ (node — присоединённый хост).

    def _br(self, kind, t, query):
        if self.browser is None:
            raise Bad("браузер здесь недоступен", 501)
        node = (query.get("node") or "").strip() or None
        return self.browser, self.browser.key(kind, t, node)

    def br_status(self, kind, t, query, body):
        b, k = self._br(kind, t, query)
        return ok(b.status(k))

    def br_act(self, kind, t, what, query, body):
        b, k = self._br(kind, t, query)
        if what == "proxy":
            return ok(b.set_proxy(k, _s(body.get("proxy"), "прокси", 500), body.get("type") or "http"))
        if what == "check":
            return ok(b.recheck(k))
        if what == "start":
            return ok(b.start(k))
        if what == "stop":
            return ok(b.stop(k))
        if what == "open":
            return ok(b.open_url(k, _s(body.get("url"), "адрес", 2000)))
        return ok(b.set_links(k, body.get("links")))

    def br_modem(self, t, query, body):
        """Веб модема в отдельном окне: браузер модемов этой машины (md) открывает http://IP/.
        Модем — там, где воткнут: хост — отсюда, ВМ — через SSH хаба."""
        b, k = self._br("md", t, query)
        if query.get("node"):
            raise Bad("веб модема присоединённого хоста — в панели того хоста")
        ip = _s(body.get("ip"), "адрес модема", 40).strip()
        port = body.get("port") or 80
        if not isinstance(port, int) or not br_mod.modem_addr(ip, port):
            raise Bad("адрес модема: частный IPv4 (192.168.N.1), порт веба")
        if t == "host":
            via = {"kind": "local"}
        else:
            fn = getattr(self.api, "tunnel_argv", None)
            if fn is None:
                raise Bad("этот хаб не умеет веб модемов ВМ", 501)
            via = {"kind": "ssh", "argv": fn(int(t))}
        url = "http://%s/" % ip if port == 80 else "%s://%s:%d/" % ("https" if port in (443, 8443) else "http", ip, port)
        return ok(b.open_modem(k, via, url))

    def br_md_stop(self, t, query, body):
        b, k = self._br("md", t, query)
        return ok(b.stop(k))

    def br_install(self, query, body):
        if self.browser is None:
            raise Bad("браузер здесь недоступен", 501)
        return ok(self.browser.install_job())

    # ── роутер этого хоста (MikroTik, PCS.md §20) ───────────────────────────

    def _rt(self, action, params=None):
        fn = getattr(self.api, "router", None)
        if fn is None:
            raise Bad("этот хост не умеет роутер — обновите PCS", 501)
        return fn(action, params or {})

    def router_get(self, query, body):
        """Доступ (без пароля) и сводка; не подключён или не отвечает — сводки нет, есть ошибка."""
        conf = self._rt("conf")
        out = {"conf": conf, "status": None, "error": None}
        if conf.get("configured"):
            try:
                out["status"] = self._rt("status")
            except Fail as e:
                out["error"] = str(e)
        return ok(out)

    def router_list(self, what, query, body):
        return ok(self._rt(what))

    def router_act(self, what, query, body):
        p = {}
        if what == "save":
            if set(body) <= {"hidden"}:
                p = {"hidden": bool(body.get("hidden"))}
            else:
                p = {"host": _s(body.get("host"), "адрес", 255).strip(), "user": _s(body.get("user"), "логин", 64).strip(),
                     "password": _s(body.get("password"), "пароль", 200), "https": bool(body.get("https")),
                     "port": body.get("port") if isinstance(body.get("port"), int) else None,
                     "hidden": bool(body.get("hidden"))}
        elif what in ("iface", "nat_set"):
            p = {"id": _s(body.get("id"), "номер", 12), "disabled": bool(body.get("disabled"))}
        elif what in ("nat_del", "lease_static"):
            p = {"id": _s(body.get("id"), "номер", 12)}
        elif what == "nat_add":
            p = {k: _s(body.get(k), k, 200) for k in ("proto", "port", "to", "to_port", "comment")}
        elif what == "reboot" and body.get("confirm") != "reboot":
            raise Bad("перезагрузка роутера — только с подтверждением")
        self.forget()
        return ok(self._rt(what, p))

    def router_webfig(self, query, body):
        """WebFig роутера — в окне браузера модемов хоста (он ходит в частные сети на порт веба)."""
        b, k = self._br("md", "host", query)
        conf = self._rt("conf")
        host = conf.get("host") or conf.get("default_host") or ""
        port = conf.get("port") or (443 if conf.get("https") else 80)
        if not br_mod.modem_addr(host, port):
            raise Bad("WebFig в окне — для роутера в частной сети (192.168.x.x, 10.x, 172.16–31.x) на порту веба")
        url = "%s://%s%s/" % ("https" if conf.get("https") else "http", host, "" if port in (80, 443) else ":%d" % port)
        return ok(b.open_modem(k, {"kind": "local"}, url))

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
        long_ = getattr(self.api, "is_long", None)
        if long_ and long_(part, args):
            # долгая команда — заданием: запрос не висит минутами и не теряется при обрыве связи
            self.forget()
            return ok({"job": self.api.run_job(None if t == "host" else t, part, args)})
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

    # ── управление ВМ ────────────────────────────────────────────────────────

    def power(self, sid, query, body):
        action = body.get("action")
        if action not in ("start", "shutdown", "reboot", "stop"):
            raise Bad("питание: start, shutdown, reboot или stop")
        r = self.api.vm_power(int(sid), action)
        self.forget()
        return ok(r)

    def forget_server(self, sid, query, body):
        confirm = str(body.get("confirm", "")).strip()
        if confirm != sid:
            raise Bad("подтвердите номером ВМ (%s)" % sid, 403)
        r = self.api.server_forget(int(sid), confirm)
        self.forget()
        return ok(r)

    def color(self, sid, query, body):
        r = self.api.server_color(int(sid), _s(body.get("color"), "цвет", 20).strip())
        self.forget()
        return ok(r)

    def reconnect(self, sid, query, body):
        """«Переподключить»: связь с ВМ по шагам; после — обзор заново (там «нет SSH» уйдёт сразу)."""
        fn = getattr(self.api, "server_reconnect", None)
        if fn is None:
            raise Bad("этот хаб не умеет переподключать — обновите PCS", 501)
        r = fn(int(sid))
        self.forget()
        return ok(r)

    def hostkey(self, sid, query, body):
        """Ключ SSH сервера: сверить; accept — принять ("agent" или отпечаток SHA256:…)."""
        acc = body.get("accept")
        if acc is not None and (not isinstance(acc, str) or not re.fullmatch(r"agent|SHA256:[A-Za-z0-9+/]{43}", acc)):
            raise Bad("accept: agent или отпечаток SHA256:…")
        fn = getattr(self.api, "host_key", None)
        if fn is None:
            raise Bad("этот хаб не умеет сверять ключ", 501)
        r = fn(int(sid), acc)
        if acc:
            self.forget()
        return ok(r)

    def node_color(self, name, query, body):
        r = self.api.node_color(name, _s(body.get("color"), "цвет", 20).strip())
        self.forget()
        return ok(r)

    def colors(self, query, body):
        fn = getattr(self.api, "colors", None)
        return ok(fn() if fn else [])

    def vms_other(self, query, body):
        fn = getattr(self.api, "vms_other", None)
        return ok(fn() if fn else [])

    def adopt(self, sid, query, body):
        ip = _s(body.get("ip"), "IP", 40).strip()
        if ip and not re.fullmatch(r"\d{1,3}(\.\d{1,3}){3}", ip):
            raise Bad("IP: адрес IPv4 или пусто (узнать у гостевого агента)")
        pw = _s(body.get("password"), "пароль", 200)
        jid = self.api.server_adopt(int(sid), ip=ip or None, password=pw or None)
        self.forget()
        return ok({"job": jid})

    def job(self, jid, query, body):
        return ok(self.api.job(jid))

    def job_retry(self, jid, query, body):
        fn = getattr(self.api, "job_retry", None)
        if fn is None:
            raise Bad("этот хаб не умеет повторять задания — обновите PCS", 501)
        r = fn(jid)
        self.forget()
        return ok(r)

    # ── присоединённые хосты ────────────────────────────────────────────────

    def self_info(self, query, body):
        fn = getattr(self.api, "self_fingerprint", None)
        return ok({"fingerprint": fn() if fn else ""})

    def nodes(self, query, body):
        fn = getattr(self.api, "nodes_overview", None)
        if fn is None:
            return ok([])
        return ok(self._cached("nodes", fn))

    def node_join(self, query, body):
        addr = _s(body.get("addr"), "адрес", 300, empty=False).strip()
        user = _s(body.get("user"), "логин", 200, empty=False).strip()
        pw = _s(body.get("password"), "пароль", 1000, empty=False)
        name = _s(body.get("name"), "имя", 40).strip() or None
        fp = _s(body.get("fingerprint"), "отпечаток", 200).strip() or None
        r = self.api.node_join(addr, user, pw, name=name, fingerprint=fp)
        self.forget()
        return ok(r)

    def node_remove(self, name, query, body):
        if str(body.get("confirm", "")).strip().lower() != name:
            raise Bad("подтвердите отсоединение именем хоста (%s)" % name, 403)
        r = self.api.node_remove(name)
        self.forget()
        return ok(r)

    def node_login(self, name, query, body):
        return ok(self.api.node_login(name))

    def _node(self, method, name, rest, query, body):
        """/api/nodes/<имя>/<путь> → /api/v1/<путь> того хоста (тот же конверт)."""
        if rest.startswith(("nodes", "self", "login", "v1/")) or ".." in rest:
            raise Bad("этот адрес через хаб не ходит")
        q = "&".join("%s=%s" % (k, v) for k, v in (query or {}).items() if re.fullmatch(r"[a-z_]{1,20}", k)
                     and re.fullmatch(r"[A-Za-z0-9_.-]{0,40}", str(v)))
        e = self.api.node_call(name, method, rest + ("?" + q if q else ""), body if method == "POST" else None)
        if method == "POST":
            self.forget()
        return e

    def node_get(self, name, rest, query, body):
        return self._node("GET", name, rest, query, body)

    def node_post(self, name, rest, query, body):
        return self._node("POST", name, rest, query, body)

    def jobs(self, query, body):
        fn = getattr(self.api, "jobs_recent", None)
        return ok(fn() if fn else [])


# ── мелочи ──────────────────────────────────────────────────────────────────

def _bad(k):
    raise Bad("%s: true или false" % k)


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
    if k == "port" and v != "auto" and not (v.isdigit() and 0 < int(v) < 65536):
        raise Bad("%s: порт 1–65535 или auto" % k)
    if k in ("reconnect_port", "reboot_port") and v != "auto" and not (v.isdigit() and 0 <= int(v) < 65536):
        raise Bad("%s: порт 1–65535, 0 (нет) или auto" % k)
    if k == "protect" and v not in ("0", "1"):
        raise Bad("защита: 1 или 0")
    if k == "interval_min" and not (v.isdigit() and int(v) < 100000):
        raise Bad("интервал: минуты, 0 — выключен")
    if k in ("lan_ip", "modem_ip") and v and not re.fullmatch(r"\d{1,3}(\.\d{1,3}){3}", v):
        raise Bad("%s: адрес IPv4" % k)
    if k in ("login", "password") and (not v or " " in v or ":" in v):
        raise Bad("%s: без пробелов и двоеточий, не пустой" % k)
    return v


def _ml_add(row):
    """-> (args, stdin). Набранный пароль идёт через stdin (`--password -`), не в командной строке."""
    if not row.get("lan_ip"):
        raise Bad("новая строка: нужен LAN IP")
    a, secret = ["add", "--lan-ip", _ml_val("lan_ip", row["lan_ip"])], None
    for k, flag, dflt in (("name", "--name", None), ("login", "--login", None), ("password", "--password", "gen"),
                          ("port", "--port", "auto"), ("modem_ip", "--modem-ip", None),
                          ("reconnect_port", "--reconnect-port", "auto"), ("reboot_port", "--reboot-port", "auto"),
                          ("interval_min", "--interval", None)):
        v = row.get(k)
        v = "" if v is None else str(v).strip()
        if v == "" and dflt:
            v = dflt
        if v == "":
            continue
        if k == "password" and v != "gen":
            secret = _ml_val(k, v) + "\n"
            v = "-"
        elif not (k == "password" or v == "auto"):
            v = _ml_val(k, v)
        a += [flag, v]
    if row.get("protect") is True:
        a.append("--protect")
    elif row.get("protect") is False:
        a.append("--no-protect")
    return a, secret


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
