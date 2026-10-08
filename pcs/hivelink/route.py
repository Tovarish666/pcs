"""hivelink: маршрутизация модемов — своя таблица и своё правило на каждый модем.

Таблица и приоритет — только из pcs.core.net (2000+N и 31000+N). Уборка снимает
правила только в своём диапазоне приоритетов и чистит только свои таблицы:
правила mp.space, proxyveth и чьи угодно ещё на 192.168.N.100 не трогаются.
Здесь только чистые функции над выводом `ip`: что выполнить — решает plan().
"""
import re

from ..core import net

N_MIN, N_MAX = 1, 254


def table(n):
    return net.table("hivelink", n)


def prio(n):
    return net.prio("hivelink", n)


PRIO_MIN, PRIO_MAX = prio(N_MIN), prio(N_MAX)
TABLE_MIN, TABLE_MAX = table(N_MIN), table(N_MAX)


def rules(text):
    """`ip -4 rule show` → [(приоритет, [селекторы…], таблица)]."""
    out = []
    for ln in text.splitlines():
        m = re.match(r"^\s*(\d+):\s+(.*?)\s+lookup\s+(\S+)", ln)
        if m:
            out.append((int(m.group(1)), m.group(2).split(), m.group(3)))
    return out


def tables(text):
    """`ip -4 route show table all` → {номер таблицы: [строки]} (только числовые)."""
    out = {}
    for ln in text.splitlines():
        m = re.search(r"\btable (\d+)\b", ln)
        if m:
            out.setdefault(int(m.group(1)), []).append(ln.strip())
    return out


def ours(p):
    return PRIO_MIN <= p <= PRIO_MAX


def plan(want, rules_text, routes_text):
    """Команды, чтобы маршрутизация модемов стала ровно want = {N: (iface, адрес)}.

    Порядок: снять лишние свои правила → очистить лишние свои таблицы →
    доложить маршруты → доложить правила. Пустой want — снять всё своё.
    """
    cmds, seen = [], set()
    for p, sel, tbl in rules(rules_text):
        if not ours(p):
            continue
        n = p - PRIO_MIN + N_MIN
        w = want.get(n)
        if w and n not in seen and sel == ["from", w[1]] and tbl == str(table(n)):
            seen.add(n)
            continue
        cmds.append(["ip", "rule", "del", "priority", str(p)] + sel + ["lookup", tbl])
    by_table = tables(routes_text)
    for t in sorted(by_table):
        if TABLE_MIN <= t <= TABLE_MAX and t - TABLE_MIN + N_MIN not in want:
            cmds.append(["ip", "route", "flush", "table", str(t)])
    for n in sorted(want):
        ifn, ip = want[n]
        t = str(table(n))
        lines = [ln + " " for ln in by_table.get(table(n), [])]
        sub = [ln for ln in lines if ln.startswith("192.168.%d.0/24 dev %s " % (n, ifn)) and "src %s " % ip in ln]
        dflt = [ln for ln in lines if ln.startswith("default via 192.168.%d.1 dev %s " % (n, ifn))]
        if len(lines) != len(sub) + len(dflt):          # чужая строка или старый интерфейс
            cmds.append(["ip", "route", "flush", "table", t])
            sub = dflt = []
        if not sub:
            cmds.append(["ip", "route", "replace", "192.168.%d.0/24" % n, "dev", ifn, "src", ip, "table", t])
        if not dflt:
            cmds.append(["ip", "route", "replace", "default", "via", "192.168.%d.1" % n, "dev", ifn, "table", t])
        if n not in seen:
            cmds.append(["ip", "rule", "add", "priority", str(prio(n)), "from", ip, "lookup", t])
    return cmds


def legacy_plan(rules_text, base=1000):
    """Правила старого e3372-driver — узнаваемо его: приоритет base+N, from 192.168.N.100,
    таблица N (или 10000+N для 253–255). Таблицу чистим, только если на неё больше никто
    не ссылается: старые номера 100–252 совпадают с таблицами proxyveth usb и mp.space."""
    cmds, gone, drop = [], set(), []
    all_rules = rules(rules_text)
    for i, (p, sel, tbl) in enumerate(all_rules):
        if len(sel) != 2 or sel[0] != "from":
            continue
        m = re.fullmatch(r"192\.168\.(\d+)\.100", sel[1])
        if not m:
            continue
        n = int(m.group(1))
        old = n if 1 <= n <= 252 else 10000 + n
        if p == base + n % 8000 and tbl == str(old):
            cmds.append(["ip", "rule", "del", "priority", str(p)] + sel + ["lookup", tbl])
            gone.add(i)
            drop.append(tbl)
    left = {tbl for i, (_, _, tbl) in enumerate(all_rules) if i not in gone}
    for tbl in sorted(set(drop), key=int):
        if tbl not in left:
            cmds.append(["ip", "route", "flush", "table", tbl])
    return cmds


def parse_addrs(text):
    """`ip -4 -o addr show` → {iface: ["192.168.101.100/24", …]}."""
    out = {}
    for ln in text.splitlines():
        m = re.match(r"^\d+:\s+(\S+)\s+inet\s+(\S+)", ln)
        if m:
            out.setdefault(m.group(1).split("@")[0], []).append(m.group(2))
    return out


def modem_addr(addrs):
    """(адрес, N) модемного адреса 192.168.N.100; (первый адрес, None), если адрес другой."""
    first = None
    for a in addrs:
        ip = a.split("/")[0]
        m = re.fullmatch(r"192\.168\.(\d+)\.100", ip)
        if m and N_MIN <= int(m.group(1)) <= N_MAX:
            return ip, int(m.group(1))
        first = first or ip
    return first, None


def busy_octets(addrs, modem_ifaces):
    """{N: iface} — подсети 192.168.N.0/24, занятые НЕ настоящими модемами: сеть хоста
    (в том числе адрес .100 на сетевухе хоста) и виртуальные модемы proxyveth."""
    out = {}
    for ifn, lst in addrs.items():
        if ifn in modem_ifaces:
            continue
        for a in lst:
            m = re.match(r"^192\.168\.(\d+)\.\d+", a)
            if m:
                out.setdefault(int(m.group(1)), ifn)
    return out
