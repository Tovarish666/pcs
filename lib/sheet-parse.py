#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Разбор таблицы модемов в простые строки.

  stdin:  CSV (обычно выгрузка Google-таблицы)
  stdout: n <таб> real <таб> host <таб> port <таб> login <таб> pass <таб> enabled
  stderr: что не так со строками — по одной строке на замечание
  код:    0 — разобрана хотя бы одна строка, 1 — не разобралось ничего

Формат таблицы:
    n      номер, под которым модем видит mobileproxy.space:
           он же 192.168.<n>.1 (модем) и 192.168.<n>.100 (сервер)
    real   октет настоящего модема за прокси. Принимает 101,
           192.168.101.1 и 192.168.101.100 — берётся третий октет,
           чтобы колонку можно было заполнять копипастой откуда угодно.
           Нет колонки — real = n.
    proxy  host:port:login:pass. Либо отдельные колонки proxy_host,
           proxy_port, login, password.
    enabled  необязательная: 0 — строку не брать.

Одна кривая строка не должна ронять всю таблицу: про неё пишется замечание,
остальные разбираются дальше.
"""

import csv
import io
import re
import sys

N_MIN, N_MAX = 1, 254

ALIASES = {
    "n": "n", "num": "n", "number": "n", "номер": "n",
    "real": "real", "modem": "real", "modem_n": "real", "modem_ip": "real",
    "mgmt": "real", "real_n": "real", "реальный": "real",
    "proxy": "proxy", "socks": "proxy", "прокси": "proxy",
    "proxy_host": "proxy_host", "host": "proxy_host", "ip": "proxy_host",
    "server": "proxy_host",
    "proxy_port": "proxy_port", "port": "proxy_port",
    "login": "login", "user": "login", "username": "login",
    "password": "password", "pass": "password",
    "enabled": "enabled", "on": "enabled", "вкл": "enabled",
}
OFF = {"0", "false", "no", "off", "нет", "выкл", "disabled", "-"}


def warn(msg):
    sys.stderr.write(msg + "\n")


def norm_header(cell):
    key = (cell or "").strip().lower().replace(" ", "_").lstrip("﻿")
    return ALIASES.get(key, key)


def parse_real(raw, n, where):
    """Октет модема на дальней стороне: 101, 192.168.101.1, 192.168.101.100."""
    raw = (raw or "").strip()
    if not raw:
        return n
    if raw.count(".") == 3:
        parts = raw.split(".")
        if not all(p.isdigit() for p in parts):
            raise ValueError("адрес модема не разбирается: %r" % raw)
        if (parts[0], parts[1]) != ("192", "168"):
            raise ValueError("адрес модема вне 192.168.0.0/16: %s" % raw)
        raw = parts[2]
    if not raw.isdigit():
        raise ValueError("real не число: %r" % raw)
    real = int(raw)
    if not N_MIN <= real <= N_MAX:
        raise ValueError("real %d вне %d..%d" % (real, N_MIN, N_MAX))
    return real


def parse_proxy(raw):
    """host:port:login:pass — пароль может содержать двоеточия."""
    parts = (raw or "").strip().split(":", 3)
    if len(parts) < 4:
        raise ValueError("прокси не в формате host:port:login:pass: %r" % raw)
    return parts[0].strip(), parts[1].strip(), parts[2].strip(), parts[3]


def find_header(rows):
    """Шапка — первая строка, где есть n и что-то про прокси."""
    for i, row in enumerate(rows[:5]):
        keys = [norm_header(c) for c in row]
        if "n" in keys and ("proxy" in keys or "proxy_host" in keys):
            return i, keys
    return None, None


def main():
    data = sys.stdin.buffer.read().decode("utf-8-sig", "replace")
    rows = list(csv.reader(io.StringIO(data)))
    if not rows:
        warn("таблица пустая")
        return 1

    head_idx, keys = find_header(rows)
    if head_idx is None:
        warn("не нашлась шапка: нужны колонки n и proxy "
             "(или proxy_host, proxy_port, login, password)")
        return 1

    col = {}
    for i, k in enumerate(keys):
        col.setdefault(k, i)

    def cell(row, name):
        i = col.get(name)
        return row[i] if i is not None and i < len(row) else ""

    seen = {}
    out = []
    for lineno, row in enumerate(rows[head_idx + 1:], start=head_idx + 2):
        if not row or not cell(row, "n").strip():
            continue
        where = "строка %d" % lineno
        try:
            n_raw = cell(row, "n").strip()
            if not n_raw.isdigit():
                raise ValueError("n не число: %r" % n_raw)
            n = int(n_raw)
            if not N_MIN <= n <= N_MAX:
                raise ValueError("n %d вне %d..%d" % (n, N_MIN, N_MAX))
            real = parse_real(cell(row, "real"), n, where)

            if "proxy" in col and cell(row, "proxy").strip():
                host, port, login, password = parse_proxy(cell(row, "proxy"))
            else:
                host = cell(row, "proxy_host").strip()
                port = cell(row, "proxy_port").strip()
                login = cell(row, "login").strip()
                password = cell(row, "password")
            if not host:
                raise ValueError("нет адреса прокси")
            if not re.fullmatch(r"[A-Za-z0-9._-]+", host):
                raise ValueError("подозрительный адрес прокси: %r" % host)
            if not port.isdigit() or not 0 < int(port) < 65536:
                raise ValueError("порт прокси: %r" % port)

            enabled = 0 if cell(row, "enabled").strip().lower() in OFF else 1

            if n in seen:
                warn("%s: n=%d уже был в строке %d — беру первую" % (where, n, seen[n]))
                continue
            seen[n] = lineno
            out.append((n, real, host, int(port), login, password, enabled))
        except ValueError as e:
            warn("%s: %s" % (where, e))

    if not out:
        warn("ни одной годной строки")
        return 1

    # Номера, из-за которых потом будет больно на сервере mobileproxy.space:
    # его скрипты кладут модем N в таблицу маршрутов modem(N %% 200 + 1),
    # поэтому 153/154/155 попадают в системные default/main/local,
    # а N и N+200 делят одну таблицу между собой.
    by_table = {}
    for n, *_ in out:
        if n in (153, 154, 155):
            warn("n=%d: на сервере mp.space это системная таблица маршрутов — поменяй номер" % n)
        by_table.setdefault(n % 200, []).append(n)
    for _, group in sorted(by_table.items()):
        if len(group) > 1:
            warn("n=%s делят одну таблицу маршрутов на сервере (N и N+200) — оставь один"
                 % ", ".join(str(g) for g in sorted(group)))

    for row in out:
        sys.stdout.write("\t".join(str(x) for x in row) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
