"""hivelink: фикс скорости приёма rndis_host (DKMS).

generic_rndis_bind() сообщает модему max_transfer_size = rx_urb_size = 2048 байт
(High-Speed: (1558 + 513) & ~511). Модем режет батчи под это число, RNDIS-сообщения
перестают помещаться в один URB, а rndis_rx_fixup() собирать их через границу URB
не умеет: ~85% приёма уходит в length/frame-ошибки, download ~1.3 Мбит при здоровом
upload. Баг не только Huawei (Debian #889831), в апстриме не исправлен.

Фикс — параметр модуля rx_urb_size_override: значение уходит в max_transfer_size
до согласования. На K5160: было 1.05/9.69 Мбит/с, стало ~50/~55 при 16384.
DKMS-пакет кладёт модуль в /updates (перекрывает штатный, не удаляя его) и
пересобирает его при обновлении ядра. 0 — штатное поведение ядра.
"""
import glob
import os
import re
import shutil
import tarfile
import time
import urllib.request

from ..core.util import Fail, rd, sh, wr
from . import common, usb

PKG = "hivelink-rndis-fix"
OLD_PKG = "e3372-rndis-fix"                          # старый e3372-driver
MODPROBE = "/etc/modprobe.d/hivelink-rndis.conf"
OLD_MODPROBE = "/etc/modprobe.d/e3372-rndis.conf"
WORK = common.CACHE + "/kernel-src"
DRV = "/sys/bus/usb/drivers/rndis_host"
PARAM = "/sys/module/rndis_host/parameters/rx_urb_size_override"
REL = "drivers/net/usb/rndis_host.c"

PARAM_BLOCK = """

/* --- hivelink: обход разрыва RNDIS-сообщений между URB --------------------- *
 * Штатный расчёт даёт rx_urb_size = 2048, устройство режет батчи под это
 * число, и сообщения перестают помещаться в один URB. Позволяем задать
 * размер вручную; 0 сохраняет поведение ядра без изменений.
 * -------------------------------------------------------------------------- */
static unsigned int rx_urb_size_override;
module_param(rx_urb_size_override, uint, 0644);
MODULE_PARM_DESC(rx_urb_size_override,
\t"hivelink: override RX URB size / negotiated RNDIS max_transfer_size (0 = kernel default)");
"""
ANCHOR = r"^([ \t]*)u\.init->max_transfer_size = cpu_to_le32\(dev->rx_urb_size\);"


def patch(src):
    """Исходник rndis_host.c → исправленный. Идемпотентно. Fail — апстрим переписал
    generic_rndis_bind(), патч переносить вручную (кривой патч не накладываем)."""
    if "rx_urb_size_override" in src:
        return src
    inc = list(re.finditer(r"^#include .*$", src, re.M))
    if not inc:
        raise Fail("в исходнике нет #include — это не rndis_host.c")
    pos = inc[-1].end()
    src = src[:pos] + PARAM_BLOCK + src[pos:]
    m = re.search(ANCHOR, src, re.M)
    if not m:
        raise Fail("в rndis_host.c нет строки max_transfer_size = cpu_to_le32(dev->rx_urb_size) — "
                   "апстрим переписал generic_rndis_bind(), патч надо переносить вручную")
    ind = m.group(1)
    return src[:m.start()] + "%sif (rx_urb_size_override)\n%s\tdev->rx_urb_size = rx_urb_size_override;\n" % (
        ind, ind) + src[m.start():]


# ── какое ядро и откуда его исходник ────────────────────────────────────────

def _norm(v):
    p = v.split(".")
    return ".".join((p + ["0", "0"])[:3])


def kbase(release):
    """6.8.0-45-generic → 6.8.0; 6.8.12-4-pve → 6.8.12; 6.12.48+deb13-amd64 → 6.12.48."""
    m = re.match(r"(\d+\.\d+(?:\.\d+)?)", release)
    if not m:
        raise Fail("не понял версию ядра %s" % release)
    return _norm(m.group(1))


def upstream(release, version="", signature=""):
    """Версия апстрима, на которой собрано ядро. У Debian и Ubuntu uname — это ABI
    (6.1.0-25, 6.8.0-45), а настоящая версия — в /proc/version и /proc/version_signature."""
    base = kbase(release)
    same = lambda v: v.split(".")[:2] == base.split(".")[:2]      # noqa: E731 — та же серия x.y
    last = (signature.split() or [""])[-1]
    if re.fullmatch(r"\d+\.\d+(\.\d+)?", last) and same(_norm(last)):   # Ubuntu 6.8.0-45.45-generic 6.8.12
        return _norm(last)
    # Debian 6.1.106-3 / PMX 6.8.12-4 — последнее вхождение: раньше стоит «gcc (Debian 12.2.0-14)»
    for v in reversed(re.findall(r"\b(?:Debian|PMX) (\d+\.\d+(?:\.\d+)?)", version)):
        if same(_norm(v)):
            return _norm(v)
    return base


def kname(ver):
    """Имя версии на kernel.org: x.y.0 называется x.y (linux-6.8.tar.xz, тег v6.8)."""
    x, y, z = ver.split(".")
    return "%s.%s" % (x, y) if z == "0" else ver


def tar_url(ver):
    return "https://cdn.kernel.org/pub/linux/kernel/v%s.x/linux-%s.tar.xz" % (ver.split(".")[0], kname(ver))


def file_urls(ver):
    """Один файл вместо архива ядра на 140 МБ."""
    tag = "v" + kname(ver)
    return ["https://git.kernel.org/pub/scm/linux/kernel/git/stable/linux.git/plain/%s?h=%s" % (REL, tag),
            "https://raw.githubusercontent.com/gregkh/linux/%s/%s" % (tag, REL)]


def header_pkgs(release):
    if release.endswith("-pve"):
        return ["proxmox-headers-" + release, "pve-headers-" + release]
    return ["linux-headers-" + release]


# ── состояние ───────────────────────────────────────────────────────────────

def param():
    v = rd(PARAM)
    return int(v) if v.isdigit() else None


def bound_real():
    """Интерфейсы, привязанные к rndis_host, кроме виртуальных шин."""
    return [p for p in sorted(glob.glob(DRV + "/*:*")) if not usb.is_virtual(p)]


def modinfo_file():
    return sh("modinfo", "-F", "filename", "rndis_host", check=False).stdout.strip()


def dkms_versions(pkg):
    out = sh("dkms", "status", check=False).stdout if shutil.which("dkms") else ""
    return sorted({m.group(1) for m in re.finditer(r"^%s[/,]\s*([^,:\s]+)" % re.escape(pkg), out, re.M)})


def dkms_ready(release):
    if not shutil.which("dkms"):
        return False
    out = sh("dkms", "status", "-m", PKG, check=False).stdout
    return any(release in ln and "installed" in ln for ln in out.splitlines())


def state():
    """Для status/doctor: {active, size, file, dkms, needed}."""
    p = param()
    f = modinfo_file() if shutil.which("modinfo") else ""
    return {"active": bool(p), "size": p, "file": f or None, "dkms": "/updates/" in f,
            "needed": bool(bound_real())}


# ── сборка ──────────────────────────────────────────────────────────────────

def _fetch(url, path=None, timeout=60):
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            if path is None:
                return r.read(4 << 20).decode("utf-8", "replace")
            with open(path + ".part", "wb") as f:
                shutil.copyfileobj(r, f, 1 << 20)
            os.replace(path + ".part", path)
            return path
    except OSError as e:
        raise Fail("%s: %s" % (url, e))


def source(ver, release, log):
    cache = "%s/rndis_host-%s.c" % (WORK, ver)
    if os.path.exists(cache):
        return rd(cache)
    os.makedirs(WORK, exist_ok=True)
    local = glob.glob("/lib/modules/%s/build/%s" % (release, REL))
    errs = []
    if local:
        log("исходник rndis_host.c найден локально: %s" % local[0])
        text = rd(local[0])
    else:
        text = ""
        for url in file_urls(ver):
            try:
                t = _fetch(url)
            except Fail as e:
                errs.append(str(e))
                continue
            if "generic_rndis_bind" in t:
                log("исходник rndis_host.c (ядро %s) скачан: %s" % (ver, url.split("/")[2]))
                text = t
                break
            errs.append("%s: не исходник rndis_host.c" % url)
        if not text:
            url, tar = tar_url(ver), "%s/linux-%s.tar.xz" % (WORK, kname(ver))
            log("тяну архив ядра %s (нужен один файл): %s" % (ver, url))
            try:
                _fetch(url, tar, timeout=900)
                with tarfile.open(tar, "r:xz") as t:
                    f = t.extractfile("linux-%s/%s" % (kname(ver), REL))
                    text = f.read().decode("utf-8", "replace") if f else ""
            except (Fail, KeyError, tarfile.TarError, OSError) as e:
                errs.append(str(e))
            finally:
                if os.path.exists(tar):
                    os.unlink(tar)
    if not text:
        raise Fail("не нашёл исходник rndis_host.c для ядра %s (%s) — положи его в %s и повтори"
                   % (ver, "; ".join(errs)[:400], cache))
    wr(cache, text)
    return text


def tools(release, log):
    need = []
    if not shutil.which("dkms"):
        need.append("dkms")
    if not shutil.which("gcc") or not shutil.which("make"):
        need.append("build-essential")
    if need:
        common.apt_install(need, log)
    if os.path.isdir("/lib/modules/%s/build" % release):
        return
    errs = []
    for p in header_pkgs(release):
        try:
            common.apt_install([p], log)
            break
        except Fail as e:
            errs.append(str(e))
    if not os.path.isdir("/lib/modules/%s/build" % release):
        raise Fail("нет заголовков ядра %s (/lib/modules/%s/build): %s" % (release, release, "; ".join(errs)[:500]))


DKMS_CONF = """PACKAGE_NAME="%s"
PACKAGE_VERSION="%s"
BUILT_MODULE_NAME[0]="rndis_host"
# /updates имеет приоритет над kernel/drivers/net/usb: модуль перекрывает штатный,
# не удаляя его. Откат — снять пакет.
DEST_MODULE_LOCATION[0]="/updates"
AUTOINSTALL="yes"
MAKE[0]="make -C ${kernel_source_dir} M=${dkms_tree}/${PACKAGE_NAME}/${PACKAGE_VERSION}/build modules"
CLEAN="make -C ${kernel_source_dir} M=${dkms_tree}/${PACKAGE_NAME}/${PACKAGE_VERSION}/build clean"
"""


def modprobe_text(size):
    return ("# hivelink: обход бага rndis_host (разрыв RNDIS-сообщений между URB).\n"
            "# 0 вернёт штатное поведение ядра — медленно, но безопасно.\n"
            "options rndis_host rx_urb_size_override=%d\n" % size)


def build(size, release, log):
    tools(release, log)
    ver = upstream(release, rd("/proc/version"), rd("/proc/version_signature"))
    pkgver = "1.0-k" + ver
    code = patch(source(ver, release, log))
    d = "/usr/src/%s-%s" % (PKG, pkgver)
    shutil.rmtree(d, ignore_errors=True)
    os.makedirs(d)
    wr(d + "/rndis_host.c", code)
    wr(d + "/Makefile", "obj-m += rndis_host.o")
    wr(d + "/dkms.conf", DKMS_CONF % (PKG, pkgver))
    log("собираю %s %s под ядро %s (DKMS, пару минут)" % (PKG, pkgver, release))
    sh("dkms", "remove", "-m", PKG, "-v", pkgver, "--all", check=False)
    r = sh("dkms", "add", "-m", PKG, "-v", pkgver, check=False)
    if r.returncode != 0:
        raise Fail("dkms add: %s" % common.tail(r.stderr or r.stdout))
    r = sh("dkms", "build", "-m", PKG, "-v", pkgver, "-k", release, check=False, timeout=1800)
    if r.returncode != 0:
        raise Fail("сборка не прошла: %s — подробности /var/lib/dkms/%s/%s/build/make.log"
                   % (common.tail(r.stderr or r.stdout, 4), PKG, pkgver))
    drop_legacy(log)                    # старый модуль снимаем, только когда новый собран
    r = sh("dkms", "install", "-m", PKG, "-v", pkgver, "-k", release, "--force", check=False, timeout=600)
    if r.returncode != 0:
        raise Fail("dkms install: %s" % common.tail(r.stderr or r.stdout))


def initramfs(release, log, want_ours=True):
    """Debian собирает initramfs с MODULES=most: туда попадает и rndis_host. При загрузке
    его грузит udev из образа — штатный, и /etc/modprobe.d не применяется. Тогда фикс
    молча не работает после каждой перезагрузки. Пересобираем образ, если там rndis_host."""
    img = "/boot/initrd.img-" + release
    if not (shutil.which("update-initramfs") and shutil.which("lsinitramfs") and os.path.exists(img)):
        return
    lines = [ln for ln in sh("lsinitramfs", img, check=False, timeout=300).stdout.splitlines() if "rndis_host" in ln]
    if not lines or want_ours == any("/updates/" in ln for ln in lines):
        return
    log("в initramfs %s rndis_host — пересобираю образ" % ("штатный" if want_ours else "наш"))
    r = sh("update-initramfs", "-u", "-k", release, check=False, timeout=900)
    os.makedirs(common.LOGDIR, exist_ok=True)
    wr(common.LOGDIR + "/initramfs.log", (r.stdout or "") + (r.stderr or ""))
    if r.returncode != 0:
        log("⚠ update-initramfs не отработал (%s/initramfs.log): без этого фикс не переживёт перезагрузку"
            % common.LOGDIR)


def reload(size, log):
    """Файл на диске подменён, а в памяти старый модуль: rmmod не выгружает драйвер, к
    которому привязаны интерфейсы. Отвязать → выгрузить → загрузить → проверить параметр.
    Пока это идёт, проход драйвера не мешает: install держит блокировку hivelink."""
    unbound = []
    for i in range(5):
        for p in bound_real():
            name = os.path.basename(p)
            try:
                wr(DRV + "/unbind", name)
                unbound.append(name)
            except OSError:
                pass
        time.sleep(3 if unbound else 0)
        if os.path.isdir("/sys/module/rndis_host"):
            sh("rmmod", "rndis_host", check=False)
        if not os.path.isdir("/sys/module/rndis_host"):
            sh("modprobe", "rndis_host", check=False)
        if param() == size or (size == 0 and param() is None):
            break
    back = 0
    for name in dict.fromkeys(unbound):              # вернуть ровно то, что отвязали
        dev = os.path.join(usb.SYS, "bus/usb/devices", name)
        if os.path.isdir(dev) and not os.path.exists(dev + "/driver"):
            try:
                wr(DRV + "/bind", name)
                back += 1
            except OSError:
                pass
    for intf in glob.glob(os.path.join(usb.SYS, "bus/usb/devices/*:*")):   # RNDIS (e0) без драйвера
        if rd(intf + "/bInterfaceClass") == "e0" and not os.path.exists(intf + "/driver") \
                and not usb.is_virtual(intf):
            try:
                wr(DRV + "/bind", os.path.basename(intf))
                back += 1
            except OSError:
                pass
    if back:
        log("привязано обратно интерфейсов rndis_host: %d" % back)


def ensure(size, log):
    """Фикс в нужном состоянии: size > 0 — собран, загружен и с этим размером; 0 — снят.
    Повторный вызов без перемен ничего не пересобирает и модуль не дёргает."""
    release = os.uname().release
    if size <= 0:
        if remove(log):
            log("фикс rndis_host снят (rx_urb_size = 0)")
        return state()
    built = False
    if not dkms_ready(release):
        build(size, release, log)
        built = True
    if common.put(MODPROBE, modprobe_text(size)) or built:
        sh("depmod", "-a", release, check=False)
    initramfs(release, log)
    # Тот же параметр с тем же размером (в том числе модуль старого e3372) — модемы не
    # дёргаем: новый модуль загрузится сам при следующей перезагрузке.
    if param() != size:
        reload(size, log)
    st = state()
    if not st["dkms"]:
        raise Fail("на диске штатный rndis_host (%s) — DKMS не подменил модуль" % (st["file"] or "?"))
    if st["size"] != size:
        raise Fail("в памяти rx_urb_size_override=%s, нужно %d: модуль не перезагрузился — "
                   "отключи модемы с rndis_host или перезагрузи машину" % (st["size"], size))
    return st


def drop_legacy(log):
    """Старый DKMS-пакет e3372-rndis-fix."""
    done = False
    for v in dkms_versions(OLD_PKG):
        sh("dkms", "remove", "-m", OLD_PKG, "-v", v, "--all", check=False)
        shutil.rmtree("/usr/src/%s-%s" % (OLD_PKG, v), ignore_errors=True)
        done = True
    if os.path.exists(OLD_MODPROBE):
        os.unlink(OLD_MODPROBE)
        done = True
    if done:
        log("старый фикс %s снят" % OLD_PKG)
    return done


def remove(log):
    """Снять фикс (свой и старый) и вернуть в память штатный модуль."""
    done = drop_legacy(log)
    for v in dkms_versions(PKG):
        sh("dkms", "remove", "-m", PKG, "-v", v, "--all", check=False)
        shutil.rmtree("/usr/src/%s-%s" % (PKG, v), ignore_errors=True)
        done = True
    if os.path.exists(MODPROBE):
        os.unlink(MODPROBE)
        done = True
    if done:
        release = os.uname().release
        sh("depmod", "-a", release, check=False)
        initramfs(release, log, want_ours=False)
        reload(0, log)
    shutil.rmtree(WORK, ignore_errors=True)
    return done
