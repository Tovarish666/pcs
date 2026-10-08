"""Proxmox: ВМ, образ Ubuntu 24.04, cloud-init, адрес новой ВМ."""
import glob
import hashlib
import json
import os
import re
import shutil
import subprocess
import urllib.request

from .. import VERSION
from ..core import dns as dnsfix
from ..core.util import Fail, sh
from . import store, ui

IMG_URL = "https://cloud-images.ubuntu.com/noble/current/noble-server-cloudimg-amd64.img"
SUMS_URL = "https://cloud-images.ubuntu.com/noble/current/SHA256SUMS"
IMG_PATH = "/var/lib/vz/template/iso/ubuntu-24.04-noble.img"
SNIPPETS = "/var/lib/vz/snippets"
BASE_PKGS = ["qemu-guest-agent", "python3", "python3-yaml", "curl", "wget", "jq", "mc", "htop",
             "iptables", "iproute2", "ca-certificates"]
# режим usb: модули USB-гаджета и сборка dummy_hcd — сразу, чтобы перезагрузка на новое ядро была одна
USB_PKGS = ["linux-image-extra-virtual", "linux-headers-virtual", "dkms", "dnsmasq-base", "udhcpc"]


def is_pve():
    return bool(shutil.which("qm")) and bool(shutil.which("pvesm"))


def need_pve():
    if os.geteuid() != 0:
        raise Fail("нужен root")
    if not is_pve():
        raise Fail("qm не найден — pcs работает на хосте Proxmox VE")


def qm_status(vmid):
    r = sh("qm", "status", str(vmid), check=False, timeout=20)
    return r.stdout.split(":", 1)[1].strip() if r.returncode == 0 and ":" in r.stdout else "absent"


def qm_list():
    """{vmid: (имя, состояние)} всех ВМ хоста."""
    out = {}
    for line in sh("qm", "list", check=False, timeout=30).stdout.splitlines()[1:]:
        f = line.split()
        if len(f) >= 3 and f[0].isdigit():
            out[f[0]] = (f[1], f[2])
    return out


def used_ids():
    out = set()
    for f in glob.glob("/etc/pve/nodes/*/qemu-server/*.conf") + glob.glob("/etc/pve/nodes/*/lxc/*.conf"):
        out.add(int(os.path.basename(f)[:-5]))
    return out


def next_id():
    used = used_ids()
    for b in range(1000, 100000, 1000):
        if b not in used:
            return b
    return int(sh("pvesh", "get", "/cluster/nextid").stdout.strip())


def vm_name(vmid):
    m = re.search(r"^name: (\S+)", sh("qm", "config", str(vmid), check=False).stdout, re.M)
    return m.group(1) if m else "vm%s" % vmid


def host_net():
    r = sh("ip", "-4", "route", "get", "1.1.1.1", check=False).stdout
    gw, dev, src = (re.search(p, r) for p in (r"via (\S+)", r"dev (\S+)", r"src (\S+)"))
    mask = "24"
    if dev:
        m = re.search(r"inet \S+/(\d+)", sh("ip", "-4", "-o", "addr", "show", dev.group(1), check=False).stdout)
        mask = m.group(1) if m else "24"
    return (gw.group(1) if gw else ""), (src.group(1) if src else ""), mask


def storages():
    return [ln.split()[0] for ln in sh("pvesm", "status", "--content", "images", check=False).stdout.splitlines()[1:]
            if ln.strip()]


def ensure_image():
    name = os.path.basename(IMG_URL)
    sums = urllib.request.urlopen(SUMS_URL, timeout=30).read().decode()
    m = re.search(r"^([0-9a-f]{64}) \*?%s$" % re.escape(name), sums, re.M)
    if not m:
        raise Fail("нет суммы образа в %s" % SUMS_URL)

    def digest(path):
        h = hashlib.sha256()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                h.update(chunk)
        return h.hexdigest()
    if os.path.exists(IMG_PATH) and digest(IMG_PATH) == m.group(1):
        ui.ok("образ Ubuntu 24.04 на месте, сумма совпала")
        return
    ui.step("качаю образ Ubuntu 24.04 (~600 МБ)")
    os.makedirs(os.path.dirname(IMG_PATH), exist_ok=True)
    sh("wget", "-q", "-O", IMG_PATH + ".part", IMG_URL, timeout=1800)
    if digest(IMG_PATH + ".part") != m.group(1):
        os.unlink(IMG_PATH + ".part")
        raise Fail("образ скачался, но сумма не совпала — повтори позже")
    os.replace(IMG_PATH + ".part", IMG_PATH)
    ui.ok("образ скачан и проверен")


def ensure_snippets():
    if "local" not in sh("pvesm", "status", "--content", "snippets", check=False).stdout:
        with open("/etc/pve/storage.cfg") as f:
            cur = re.search(r"^dir: local\n(?:\s+.*\n)*?\s+content (\S+)", f.read(), re.M)
        sh("pvesm", "set", "local", "--content", (cur.group(1) + ",snippets") if cur else "iso,vztmpl,backup,snippets")
    os.makedirs(SNIPPETS, exist_ok=True)


def vm_mac(vmid):
    m = re.search(r"^net0:.*?([0-9A-Fa-f:]{17})", sh("qm", "config", str(vmid)).stdout, re.M)
    return m.group(1).lower() if m else ""


def link_local(mac):
    """IPv6 link-local по MAC (EUI-64) — так его строит systemd-networkd в Ubuntu."""
    b = [int(x, 16) for x in mac.split(":")]
    b[0] ^= 2
    w = [b[0] << 8 | b[1], b[2] << 8 | 0xff, 0xfe << 8 | b[3], b[4] << 8 | b[5]]
    return "fe80::" + ":".join("%x" % x for x in w)


def vm_ip_via_ll(vmid, bridge):
    """Пока guest agent не поставлен, ВМ уже отвечает по IPv6 link-local, а ключ PCS
    cloud-init кладёт в первые секунды: спрашиваем её IPv4 прямо по SSH."""
    mac = vm_mac(vmid)
    if not mac:
        return ""
    r = subprocess.run(["ssh", "-i", store.key_path(), "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=no",
                        "-o", "UserKnownHostsFile=/dev/null", "-o", "ConnectTimeout=5", "-o", "LogLevel=ERROR",
                        "root@%s%%%s" % (link_local(mac), bridge),
                        "ip -4 -o addr show scope global | awk '{split($4,a,\"/\"); print a[1]; exit}'"],
                       capture_output=True, text=True, timeout=20)
    ip = r.stdout.strip()
    return ip if re.fullmatch(r"\d+\.\d+\.\d+\.\d+", ip) else ""


def vm_ip(vmid, bridge="vmbr0"):
    """Адрес основной сетевухи ВМ: guest agent (по MAC net0), соседи, SSH по link-local."""
    mac = vm_mac(vmid)
    r = sh("qm", "guest", "cmd", str(vmid), "network-get-interfaces", check=False, timeout=15)
    if r.returncode == 0:
        try:
            for i in json.loads(r.stdout):
                if (i.get("hardware-address") or "").lower() == mac:
                    for a in i.get("ip-addresses", []):
                        if a.get("ip-address-type") == "ipv4":
                            return a["ip-address"]
        except ValueError:
            pass
    for line in sh("ip", "-4", "neigh", "show", check=False).stdout.splitlines():
        if mac and mac in line.lower() and "FAILED" not in line:
            return line.split()[0]
    try:
        return vm_ip_via_ll(vmid, bridge) if os.path.exists(store.key_path()) else ""
    except (subprocess.TimeoutExpired, OSError):
        return ""


def yq(v):
    return "'" + v.replace("'", "''") + "'"


def user_data(name, password, pubkey, dns, mode="usb"):
    """cloud-init сервера. Пароль — хэшем; DNS — тем же drop-in, что ставит pcs dns."""
    h = sh("openssl", "passwd", "-6", "-stdin", input=password).stdout.strip()
    pkgs = BASE_PKGS + (USB_PKGS if mode == "usb" else [])
    resolved = "".join("      %s\n" % ln for ln in dnsfix.resolved_text(dns).splitlines())
    return """#cloud-config
# pcs %(ver)s — сервер под модемы (proxyveth %(mode)s)
hostname: %(name)s
manage_etc_hosts: true
disable_root: false
ssh_pwauth: true
users:
  - name: root
    lock_passwd: false
    ssh_authorized_keys:
      - %(key)s
chpasswd:
  expire: false
  users:
    - {name: root, password: %(hash)s, type: hash}
timezone: Europe/Moscow
package_update: true
package_upgrade: true
packages:
%(pkgs)s
write_files:
  - path: /etc/ssh/sshd_config.d/10-pcs.conf
    content: |
      # 10-, а не 99-: sshd берёт первое значение, а образ кладёт «нет» в 60-cloudimg
      PermitRootLogin yes
      PasswordAuthentication yes
      KbdInteractiveAuthentication no
      UseDNS no
  - path: /etc/systemd/resolved.conf.d/99-pcs.conf
    content: |
%(resolved)sruncmd:
  - [systemctl, enable, --now, qemu-guest-agent]
  - [systemctl, disable, --now, unattended-upgrades, apt-daily.timer, apt-daily-upgrade.timer]
  - [systemctl, restart, systemd-resolved]
  - [systemctl, restart, ssh]
  - [touch, /var/lib/pcs-cloud-init-done]
""" % dict(ver=VERSION, mode=mode, name=name, key=pubkey, hash=yq(h),
           pkgs="".join("  - %s\n" % p for p in pkgs).rstrip("\n"), resolved=resolved)
