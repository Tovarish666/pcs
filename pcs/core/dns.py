"""DNS машины — один код для хоста и серверов (docs/ARCHITECTURE.md, §12).

Порт pcs-fix-dns из исходного ProxyControlService. Почему не «rm resolv.conf +
chattr +i», как было когда-то: повторный запуск падал на immutable-файле,
resolved всё равно переписывал своё, а скрипты, которым нужен resolv.conf,
молча ломались. Здесь штатный путь: resolved остаётся, серверы в нём наши,
а DNS от DHCP выключен в netplan — перебивать нечему.

На хосте Proxmox (Debian) netplan нет, а systemd-resolved часто не стоит: тогда
серверы пишутся прямо в /etc/resolv.conf (так же делает сам Proxmox), строки
search/domain/options сохраняются.

Все пути — от root, команды — через run: так проверяется на временном корне.
"""
import ipaddress
import json
import os
import re
import time

from .util import Fail, sh

DEFAULT = ["1.1.1.1", "8.8.8.8"]
FALLBACK = "9.9.9.9 1.0.0.1"
FOREIGN = ("172.20.0.2",)           # DNS tun sing-box, который он вешал на интерфейс сервера
HOSTS = ("github.com", "docs.google.com", "mobileproxy.space")
STUB = "/run/systemd/resolve/stub-resolv.conf"
NODE = "/opt/pcs/bin/pcs-node"

RESOLVED = "etc/systemd/resolved.conf.d/99-pcs.conf"
NETPLAN_DIR = "etc/netplan"
NETPLAN = "etc/netplan/99-pcs-dns.yaml"
RESOLV = "etc/resolv.conf"
UNIT = "etc/systemd/system/pcs-dns.service"
DEVICES = ("ethernets", "bonds", "bridges", "vlans")     # там, где бывает DHCP
MARK = "# PCS:"

UNIT_TEXT = """[Unit]
Description=PCS: DNS машины — свои серверы, DNS от DHCP не берём (pcs dns)
Wants=network-online.target
After=network-online.target systemd-resolved.service

[Service]
Type=oneshot
ExecStart=%s dns-fix --boot

[Install]
WantedBy=multi-user.target
""" % NODE


def at(root, rel):
    return os.path.join(root, rel)


def parse_dns(value):
    """'1.1.1.1 8.8.8.8' или '1.1.1.1,8.8.8.8' → ['1.1.1.1', '8.8.8.8']."""
    items = [x for x in re.split(r"[\s,;]+", value or "") if x]
    if not items:
        raise Fail("не задан ни один DNS: например --dns \"1.1.1.1 8.8.8.8\"")
    for x in items:
        try:
            ipaddress.ip_address(x)
        except ValueError:
            raise Fail("DNS — это адрес, а не %r: например --dns \"1.1.1.1 8.8.8.8\"" % x)
    return items


def has_resolved(root="/"):
    return any(os.path.exists(at(root, p)) for p in
               ("usr/lib/systemd/systemd-resolved", "lib/systemd/systemd-resolved"))


def has_netplan(root="/"):
    return os.path.isdir(at(root, NETPLAN_DIR)) and any(
        os.path.exists(at(root, p)) for p in ("usr/sbin/netplan", "usr/bin/netplan", "sbin/netplan"))


def _raw(path):
    try:
        with open(path) as f:
            return f.read()
    except OSError:
        return None


def current(root="/"):
    """DNS, заданные раньше (наш drop-in или наш resolv.conf), иначе DEFAULT."""
    m = re.search(r"^DNS=(.+)$", _raw(at(root, RESOLVED)) or "", re.M)
    if m and m.group(1).split():
        return m.group(1).split()
    text = _raw(at(root, RESOLV)) or ""
    if not os.path.islink(at(root, RESOLV)) and text.startswith(MARK):
        ns = re.findall(r"^nameserver\s+(\S+)", text, re.M)
        if ns:
            return ns
    return list(DEFAULT)


def resolved_text(dns):
    return ("%s DNS машины (pcs dns). DNS от DHCP не берём — см. /etc/netplan/99-pcs-dns.yaml\n"
            "[Resolve]\nDNS=%s\nFallbackDNS=%s\nDomains=~.\nDNSStubListener=yes\n"
            "DNSSEC=no\nDNSOverTLS=no\nCache=yes\n" % (MARK, " ".join(dns), FALLBACK))


# ── netplan: без PyYAML (на хосте его может не быть) ───────────────────────
def _scalar(v):
    v = v.strip()
    if len(v) >= 2 and v[0] == v[-1] and v[0] in "'\"":
        return v[1:-1]
    low = v.lower()
    if low in ("true", "yes", "on"):
        return True
    if low in ("false", "no", "off"):
        return False
    return v


def _flow(v):
    """{dhcp4: true, dhcp6: no} — простое однострочное отображение."""
    out = {}
    for part in v.strip()[1:-1].split(","):
        k, sep, val = part.partition(":")
        if sep and k.strip():
            out[_scalar(k)] = _scalar(val)
    return out


def mini_yaml(text):
    """Блочные отображения YAML — ровно то, что пишут cloud-init и люди в netplan.
    Последовательности (адреса, маршруты) пропускаются: для DNS-фикса они не нужны."""
    top = {}
    stack = [(-1, top)]
    for raw in text.splitlines():
        line = re.sub(r"(^|\s)#.*$", "", raw).rstrip()
        if not line.strip():
            continue
        ind = len(line) - len(line.lstrip(" "))
        s = line.strip()
        while stack[-1][0] >= ind:
            stack.pop()
        parent = stack[-1][1]
        if s.startswith("-"):
            stack.append((ind, {}))          # элемент списка: его поля — в никуда
            continue
        m = re.match(r"""^("[^"]*"|'[^']*'|[^:]+?)\s*:(?:\s+(.*))?$""", s)
        if not m:
            continue
        key, val = _scalar(m.group(1)), (m.group(2) or "").strip()
        if not val:
            node = {}
            parent[key] = node
            stack.append((ind, node))
        elif val.startswith("{") and val.endswith("}"):
            parent[key] = _flow(val)
        elif val.startswith("["):
            parent[key] = [_scalar(x) for x in val.strip("[]").split(",") if x.strip()]
        else:
            parent[key] = _scalar(val)
    return top


def load_yaml(text, use_yaml=True):
    if use_yaml:
        try:
            import yaml                      # в облачном образе Ubuntu есть всегда
            return yaml.safe_load(text) or {}
        except ImportError:
            pass
        except Exception:
            return {}
    try:
        return mini_yaml(text)
    except Exception:
        return {}


def _on(v):
    return v is True or str(v).lower() in ("true", "yes", "on")


def dhcp_devices(root="/", use_yaml=True):
    """{раздел: {имя: (dhcp4, dhcp6)}} по всем /etc/netplan/*.yaml, кроме нашего."""
    found = {}
    d = at(root, NETPLAN_DIR)
    for name in sorted(os.listdir(d)) if os.path.isdir(d) else []:
        if not name.endswith(".yaml") or name == os.path.basename(NETPLAN):
            continue
        doc = load_yaml(_raw(os.path.join(d, name)) or "", use_yaml)
        net = doc.get("network") if isinstance(doc, dict) else None
        for sect in DEVICES:
            devs = (net or {}).get(sect) if isinstance(net, dict) else None
            for dev, cfg in (devs or {}).items() if isinstance(devs, dict) else []:
                cfg = cfg if isinstance(cfg, dict) else {}
                old = found.setdefault(sect, {}).get(str(dev), (False, False))
                found[sect][str(dev)] = (old[0] or _on(cfg.get("dhcp4")), old[1] or _on(cfg.get("dhcp6")))
    return {s: {n: v for n, v in devs.items() if v[0] or v[1]} for s, devs in found.items()
            if any(v[0] or v[1] for v in devs.values())}


def netplan_text(devs):
    if not devs:
        return None
    off = ["        use-dns: false", "        use-domains: false"]
    lines = ["%s DNS от DHCP не берём (pcs dns). 50-cloud-init.yaml не трогаем." % MARK,
             "network:", "  version: 2"]
    for sect in DEVICES:
        if not devs.get(sect):
            continue
        lines.append("  %s:" % sect)
        for name in sorted(devs[sect]):
            d4, d6 = devs[sect][name]
            lines.append("    %s:" % (name if re.fullmatch(r"[A-Za-z0-9_.@-]+", name) else json.dumps(name)))
            if d4:
                lines += ["      dhcp4-overrides:"] + off
            if d6:
                lines += ["      dhcp6-overrides:"] + off
    return "\n".join(lines) + "\n"


def dhcp_names(devs):
    return {n for sect in devs.values() for n in sect}


# ── мелочи с файлами ───────────────────────────────────────────────────────
def put(path, text, mode=0o644):
    """Записать, если отличается. → True, если файл поменялся."""
    if _raw(path) == text and not os.path.islink(path):
        return False
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".pcs-tmp"
    with open(tmp, "w") as f:
        f.write(text)
    os.chmod(tmp, mode)
    if os.path.islink(path):
        os.unlink(path)
    os.replace(tmp, path)
    return True


def restore(path, old, mode=0o600):
    if old is None:
        if os.path.exists(path):
            os.unlink(path)
    else:
        put(path, old, mode)


def link_dns(out):
    """`resolvectl dns` → {интерфейс: [серверы]} и глобальные."""
    links, glob = {}, []
    for line in out.splitlines():
        m = re.match(r"^Link \d+ \(([^)]+)\):\s*(.*)$", line.strip())
        if m:
            links[m.group(1)] = m.group(2).split()
        elif line.strip().startswith("Global:"):
            glob = line.split(":", 1)[1].split()
    return links, glob


def foreign(out, dns, dhcp):
    """Чужой DNS на интерфейсе: след tun sing-box или DNS из DHCP там, где мы его выключили."""
    links, _ = link_dns(out)
    bad = []
    for link, servers in sorted(links.items()):
        for srv in servers:
            srv = srv.split("#")[0]
            if srv in dns:
                continue
            if srv in FOREIGN or link in dhcp:
                bad.append((link, srv))
    return bad


def resolv_text(dns, old):
    keep = [ln for ln in (old or "").splitlines()
            if re.match(r"^\s*(search|domain|options)\s", ln)]
    return "\n".join(["%s DNS хоста (pcs dns --host). Свои серверы, без systemd-resolved." % MARK]
                     + keep + ["nameserver %s" % x for x in dns[:3]]) + "\n"


# ── сам фикс ───────────────────────────────────────────────────────────────
def fix(dns=None, root="/", run=sh, log=print, boot=False, ensure_base=None, sleep=time.sleep, tries=None):
    """Привести DNS машины в порядок (§12). Идемпотентно. → сводка; ошибка — Fail."""
    dns = list(dns) if dns else current(root)
    for x in dns:
        parse_dns(x)
    res = {"dns": dns, "mode": "resolved" if has_resolved(root) else "resolv.conf",
           "changed": [], "netplan": "нет", "foreign": [], "check": {}}
    resolv = at(root, RESOLV)
    # 0. наследие старых версий: immutable resolv.conf
    if os.path.exists(resolv) and not os.path.islink(resolv):
        run("chattr", "-i", resolv, check=False)

    dhcp = set()
    if res["mode"] == "resolved":
        # 1. наши серверы; Domains=~. — маршрут по умолчанию для всех имён
        if put(at(root, RESOLVED), resolved_text(dns)):
            res["changed"].append("/" + RESOLVED)
        # 2–3. netplan: DNS от DHCP не брать — только там, где DHCP включён
        if has_netplan(root):
            devs = dhcp_devices(root)
            dhcp = dhcp_names(devs)
            res["netplan"] = netplan_step(root, devs, run, log, ensure_base, res)
        # 4. resolv.conf — штатный симлинк на stub resolved
        if not (os.path.islink(resolv) and os.readlink(resolv) == STUB):
            if os.path.lexists(resolv):
                os.unlink(resolv)
            os.symlink(STUB, resolv)
            res["changed"].append("/" + RESOLV)
        run("systemctl", "enable", "systemd-resolved", check=False)
        run("systemctl", "restart", "systemd-resolved", check=False)
        sleep(1)
        run("resolvectl", "flush-caches", check=False)
        # 6. чужой DNS на интерфейсах (172.20.0.2 от sing-box, остатки DHCP)
        for link, srv in foreign(run("resolvectl", "dns", check=False).stdout, dns, dhcp):
            run("resolvectl", "revert", link, check=False)
            run("networkctl", "renew", link, check=False)
            res["foreign"].append("%s: %s" % (link, srv))
            log("DNS интерфейса %s: снят чужой %s" % (link, srv))
    else:
        old = _raw(resolv) if not os.path.islink(resolv) else None
        if old is not None and not old.startswith(MARK) and not os.path.exists(resolv + ".pcs-bak"):
            put(resolv + ".pcs-bak", old)
        if put(resolv, resolv_text(dns, old)):
            res["changed"].append("/" + RESOLV)

    # 6. то же при каждой загрузке
    if not boot and put(at(root, UNIT), UNIT_TEXT):
        res["changed"].append("/" + UNIT)
        run("systemctl", "daemon-reload", check=False)
    if not boot:
        run("systemctl", "enable", "pcs-dns.service", check=False)

    # 5. проверка — без неё «починили» ничего не значит
    tries = tries or (15 if boot else 4)
    for i in range(tries):
        res["check"] = {h: run("getent", "ahostsv4", h, check=False, timeout=15).returncode == 0 for h in HOSTS}
        if all(res["check"].values()):
            break
        if i + 1 < tries:
            sleep(2)
    left = []
    if res["mode"] == "resolved":
        left = foreign(run("resolvectl", "dns", check=False).stdout, dns, dhcp)
    bad = [h for h, good in res["check"].items() if not good]
    if bad or left:
        why = []
        if bad:
            why.append("не резолвится %s" % ", ".join(bad))
        if left:
            why.append("на интерфейсах чужой DNS: %s" % ", ".join("%s %s" % x for x in left))
        status = ""
        if res["mode"] == "resolved":
            status = run("resolvectl", "status", check=False).stdout.strip()
            for ln in status.splitlines()[:30]:
                log("  " + ln)
        brief = "; ".join(" ".join(ln.split()) for ln in status.splitlines()
                          if re.match(r"^\s*(Global|Link \d+|Current DNS|DNS Servers)", ln))[:400]
        raise Fail("DNS не в порядке: %s%s. Проверь, открыт ли выход на %s по 53 порту"
                   % ("; ".join(why), (" (resolvectl: %s)" % brief) if brief else "", " ".join(dns)))
    res["ok"] = True
    return res


def netplan_step(root, devs, run, log, ensure_base, res):
    path = at(root, NETPLAN)
    new, old = netplan_text(devs), _raw(path)
    if new == old:
        return "без изменений"
    if new is None:
        os.unlink(path)
    else:
        put(path, new, 0o600)
    r = run("netplan", "generate", check=False)
    if r.returncode != 0:
        # сеть важнее идеального DNS: наш drop-in netplan не переварил — убираем
        restore(path, old)
        run("netplan", "generate", check=False)
        err = (r.stderr or r.stdout or "").strip().splitlines()
        log("⚠ netplan не принял 99-pcs-dns.yaml — откатил: %s" % (err[-1] if err else "?"))
        return "откат"
    if ensure_base is None:
        from .net import ensure_base               # netplan apply без него стирает маршрутизацию модемов
    ensure_base(log=log)
    a = run("netplan", "apply", check=False, timeout=120)
    if a.returncode != 0:
        log("⚠ netplan apply: %s" % (a.stderr or a.stdout or "").strip()[:200])
    res["changed"].append("/" + NETPLAN)
    return "применён"

