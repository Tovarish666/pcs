"""Таблица модемов proxyveth — одна на оба режима (usb и gw).

Колонки: n, real, proxy (host:port:login:pass — пароль может содержать «:»)
или вместо proxy четыре колонки host/port/login/password; enabled — по желанию.
  n     — номер модема для сервера: модем 192.168.n.1, сервер получает 192.168.n.100;
  real  — октет настоящего модема за прокси (81, 192.168.81.1 или 192.168.81.100);
          пусто — real = n.

Источник: ссылка на Google-таблицу (из адресной строки — сама станет CSV) или
локальный файл. После каждого удачного чтения текст ложится в локальную копию;
если источник недоступен — берётся копия (и об этом говорится).
"""
import csv
import io
import os
import re
import time
import urllib.request

from .util import Fail

ALIASES = {"n": "n", "num": "n", "номер": "n", "real": "real", "modem": "real",
           "реальный": "real", "proxy": "proxy", "прокси": "proxy", "socks": "proxy",
           "proxy_host": "host", "host": "host", "proxy_port": "port", "port": "port",
           "login": "user", "user": "user", "password": "pw", "pass": "pw",
           "enabled": "enabled", "вкл": "enabled", "on": "enabled"}
OFF = {"0", "false", "no", "off", "нет", "выкл", "-"}


def normalize_url(u):
    """Ссылка из адресной строки Google → ссылка на CSV. Чужие ссылки — как есть."""
    m = re.search(r"docs\.google\.com/spreadsheets/d/(e/)?([A-Za-z0-9_-]+)", u)
    if not m or "output=csv" in u or "format=csv" in u:
        return u
    gm = re.search(r"[#?&]gid=(\d+)", u)
    gid = gm.group(1) if gm else "0"
    if m.group(1):
        return "https://docs.google.com/spreadsheets/d/e/%s/pub?gid=%s&single=true&output=csv" % (m.group(2), gid)
    return "https://docs.google.com/spreadsheets/d/%s/export?format=csv&gid=%s" % (m.group(2), gid)


def fetch(src, copy_path, log=print):
    """→ (текст, взят_из_копии). Удачное чтение источника обновляет копию."""
    err = None
    local = not re.match(r"(https?|file)://", src)
    for pause in (3, 8, 0):              # сеть и Google иногда рвут TLS — не повод сдаваться
        try:
            if local:
                with open(src, encoding="utf-8-sig") as f:
                    text = f.read()
            else:
                req = urllib.request.Request(normalize_url(src), headers={"User-Agent": "pcs"})
                text = urllib.request.urlopen(req, timeout=30).read().decode("utf-8-sig")
            if text.lstrip().startswith("<"):
                err = Fail("вместо CSV пришёл HTML — у таблицы нет доступа по ссылке?")
                break
            if os.path.abspath(src) != os.path.abspath(copy_path):
                os.makedirs(os.path.dirname(copy_path), exist_ok=True)
                with open(copy_path + ".tmp", "w") as f:
                    f.write(text)
                os.chmod(copy_path + ".tmp", 0o600)
                os.replace(copy_path + ".tmp", copy_path)
            return text, False
        except Exception as e:
            err = e
            if local:
                break
            time.sleep(pause)
    if os.path.exists(copy_path) and os.path.abspath(src) != os.path.abspath(copy_path):
        log("⚠ таблица недоступна (%s) — беру локальную копию" % err)
        return open(copy_path).read(), True
    raise Fail("таблица недоступна, а локальной копии нет: %s" % err)


def lint(text, taken_octets=frozenset(), mp_tables=True):
    """→ (годные {n: строка}, выключенные {n}, отбракованные {n}, замечания [str]).

    mp_tables — N и N+200 делят таблицу маршрутов mp.space (и proxyveth usb), а
    153–155 — это её системные таблицы 253–255. Для режима gw без mp.space — False.
    """
    rows = list(csv.reader(io.StringIO(text.lstrip("﻿"))))
    problems, head, keys, idx = [], None, [], {}
    for i, row in enumerate(rows[:5]):
        keys = [ALIASES.get(c.strip().lower().replace(" ", "_"), c.strip().lower()) for c in row]
        if "n" in keys and ("proxy" in keys or "host" in keys):
            head, idx = i, {k: j for j, k in reversed(list(enumerate(keys)))}
            break
    if head is None:
        return {}, set(), set(), ["нет шапки: нужны колонки n и proxy (или host, port, login, password)"]

    def cell(row, k):
        j = idx.get(k)
        return row[j].strip() if j is not None and j < len(row) else ""

    parsed, invalid = [], set()
    for ln, row in enumerate(rows[head + 1:], start=head + 2):
        raw_n = cell(row, "n")
        if not any(c.strip() for c in row) or not raw_n:
            continue
        where = "строка %d" % ln
        extra = [c for c in row[len(keys):] if c.strip()]
        if extra:
            problems.append("⚠ %s: значения за пределами шапки %s не читаются — нет колонки?" % (where, extra))
        try:
            if not raw_n.isdigit():
                raise Fail("n не число: %r" % raw_n)
            n = int(raw_n)
            try:
                if not 1 <= n <= 254:
                    raise Fail("n=%d вне 1..254" % n)
                if mp_tables and n in (153, 154, 155):
                    raise Fail("n=%d: у mp.space это системная таблица маршрутов (253–255)" % n)
                if n in taken_octets:
                    raise Fail("n=%d: 192.168.%d.0/24 уже занята сетью самого сервера" % (n, n))
                r = cell(row, "real")
                if not r:
                    problems.append("⚠ %s: real пуст — считаю real = n = %d" % (where, n))
                    r = str(n)
                if r.count(".") == 3:
                    parts = r.split(".")
                    if parts[:2] != ["192", "168"]:
                        raise Fail("real вне 192.168.0.0/16: %s" % r)
                    r = parts[2]
                if not r.isdigit() or not 1 <= int(r) <= 254:
                    raise Fail("real не октет: %r" % r)
                if cell(row, "proxy"):
                    parts = cell(row, "proxy").split(":", 3)
                    if len(parts) < 4:
                        raise Fail("прокси не host:port:login:pass: %r" % cell(row, "proxy"))
                    host, port, user, pw = [x.strip() for x in parts]
                else:
                    host, port, user, pw = cell(row, "host"), cell(row, "port"), cell(row, "user"), cell(row, "pw")
                if not re.fullmatch(r"[A-Za-z0-9.-]+", host or ""):
                    raise Fail("адрес прокси: %r" % host)
                if not port.isdigit() or not 0 < int(port) < 65536:
                    raise Fail("порт прокси: %r" % port)
                if not user or not pw:
                    raise Fail("у прокси нет логина или пароля")
                if re.search(r"\s", user + pw):
                    raise Fail("пробел/таб в логине или пароле")
            except Fail:
                invalid.add(n)
                raise
            parsed.append({"n": n, "real": int(r), "host": host, "port": int(port), "user": user,
                           "pw": pw, "enabled": cell(row, "enabled").lower() not in OFF, "line": ln})
        except Fail as e:
            problems.append("%s: %s" % (where, e))

    by_n = {}                        # один n дважды — неясно, какая строка верная: ни одну
    for p in parsed:
        by_n.setdefault(p["n"], []).append(p)
    for n, ps in by_n.items():
        if len(ps) > 1:
            problems.append("n=%d в строках %s — неясно, какая верная, не беру ни одну"
                            % (n, ", ".join(str(p["line"]) for p in ps)))
            invalid.add(n)
    keep = [ps[0] for ps in by_n.values() if len(ps) == 1]

    if mp_tables:                    # N и N+200 делят одну таблицу — одна строка сломает другую
        by_tab, ok = {}, []
        for p in sorted(keep, key=lambda p: p["n"]):
            by_tab.setdefault(p["n"] % 200, []).append(p)
        for ps in by_tab.values():
            if len(ps) == 1:
                ok.append(ps[0])
                continue
            problems.append("n=%s делят одну таблицу маршрутов (N и N+200) — не беру ни одну"
                            % ",".join(str(p["n"]) for p in ps))
            invalid.update(p["n"] for p in ps)
        keep = ok

    by_px = {}
    for p in keep:
        by_px.setdefault((p["host"], p["port"], p["user"]), []).append(p["n"])
    for (h, pt, u), ns in sorted(by_px.items()):
        if len(ns) > 1:
            problems.append("⚠ n=%s на одной прокси %s:%d:%s — смена IP у одного меняет у всех"
                            % (",".join(map(str, ns)), h, pt, u))

    desired = {p["n"]: p for p in keep if p["enabled"]}
    disabled = {p["n"] for p in keep if not p["enabled"]}
    return desired, disabled, invalid, problems
