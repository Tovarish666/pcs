#!/usr/bin/env bash
# Проверка cmd/modem-template.sh и guest/vmodem-setup без Proxmox и без root:
# смотрим cloud-init, который уедет в ВМ модема, и режим показа у vmodem-setup.
#   bash tests/test-modem-template.sh
set -u
cd "$(dirname "$0")/.." || exit 1
pass=0; fail=0

ok_()  { pass=$((pass + 1)); printf '  ok   %s\n' "$1"; }
bad_() { fail=$((fail + 1)); printf '  FAIL %s\n%s\n' "$1" "${2:-}"; }
has()   { case "$2" in *"$3"*) ok_ "$1" ;; *) bad_ "$1" "       нет: $3" ;; esac; }
hasnt() { case "$2" in *"$3"*) bad_ "$1" "       нашлось: $3" ;; *) ok_ "$1" ;; esac; }

echo "modem-template:"
ud="$(PCS_MODEM_PASSWORD='tpl-pass' bash cmd/modem-template.sh --print-user-data 2>/dev/null)"

has "это cloud-config"          "$ud" "#cloud-config"
has "пакет usbip"               "$ud" "- usbip"
has "dnsmasq без своей службы"   "$ud" "- dnsmasq-base"
has "python3 для vmodem-api"    "$ud" "- python3"
has "guest agent"               "$ud" "- qemu-guest-agent"
has "модуль usbip-vudc с загрузки" "$ud" "usbip-vudc"
has "configfs с загрузки"       "$ud" "configfs"
has "вход root по паролю"       "$ud" "PermitRootLogin yes"
has "авто-обновления выключены" "$ud" "unattended-upgrades"
case "$ud" in
    *"type: hash"*) ok_ "пароль уехал хэшем" ;;
    *"type: text"*) ok_ "пароль текстом (openssl без -6 — так на macOS, на Proxmox будет хэш)" ;;
    *) bad_ "пароль в cloud-init" ;;
esac

# Утилиты должны уехать в ВМ байт в байт.
for f in vmodem vmodem-api vmodem-setup; do
    if printf '%s' "$ud" | python3 -c '
import base64, hashlib, sys
want_path, name = sys.argv[1], sys.argv[2]
text = sys.stdin.read().splitlines()
blob = None
for i, line in enumerate(text):
    if line.strip() == "- path: /usr/local/sbin/" + name:
        for line2 in text[i:i + 6]:
            if line2.strip().startswith("content:"):
                blob = line2.split("content:", 1)[1].strip()
                break
        break
if not blob:
    sys.exit(1)
have = hashlib.sha256(base64.b64decode(blob)).hexdigest()
want = hashlib.sha256(open(want_path, "rb").read()).hexdigest()
sys.exit(0 if have == want else 2)
' "guest/$f" "$f"; then
        ok_ "${f} уезжает в ВМ без изменений"
    else
        bad_ "${f} уезжает в ВМ без изменений" "       (код $?)"
    fi
done

# ── cloud-init клона: то, что получит каждый модем ─────────────────────────
tmp="$(mktemp -d)"; trap 'rm -rf "$tmp"' EXIT
export PCS_ETC="$tmp/etc" PCS_LOG_DIR="$tmp/log" PCS_LOG="$tmp/log/pcs.log"; mkdir -p "$tmp/log"
bash -c 'source lib/common.sh; source lib/modem.sh; modem_clone_user_data "$1" vmodem-201 "clone-pass"' \
    _ "$tmp/clone.yaml" 2>/dev/null
cud="$(cat "$tmp/clone.yaml" 2>/dev/null)"
has "у клона своё имя"          "$cud" "hostname: vmodem-201"
has "клон не переставляет пакеты" "$cud" "package_update: false"
hasnt "клон не тащит утилиты заново" "$cud" "encoding: b64"
case "$cud" in
    *"type: hash"*|*"type: text"*) ok_ "пароль у клона задан" ;;
    *) bad_ "пароль у клона задан" ;;
esac

echo
echo "vmodem-setup:"
out="$(bash guest/vmodem-setup --dry-run 2>&1)"
has "модуль libcomposite"     "$out" "modprobe libcomposite"
has "модуль usbip-vudc"       "$out" "modprobe usbip-vudc"
has "configfs монтируется"    "$out" "mount -t configfs"
has "список модулей на диск"  "$out" "/etc/modules-load.d/vmodem.conf"
has "sing-box версии 1.10.0"  "$out" "sing-box-1.10.0-linux-amd64.tar.gz"
hasnt "ничего не ставится"    "$out" "поставлен"

out2="$(bash guest/vmodem-setup --dry-run --singbox-version 1.11.1 2>&1)"
has "версия sing-box меняется флагом" "$out2" "sing-box-1.11.1-linux-amd64.tar.gz"

# Облачное ядро Debian: USB в нём вырезан, надо поставить обычное и
# попросить перезагрузку (код 2). Подменяем uname, modprobe и apt-get.
stub="$tmp/stub"; mkdir -p "$stub"
printf '#!/bin/sh\n[ "$1" = "-r" ] && echo "6.12.0-cloud-amd64" || exec /usr/bin/uname "$@"\n' >"$stub/uname"
printf '#!/bin/sh\nexit 1\n' >"$stub/modprobe"
printf '#!/bin/sh\necho "apt-get $*"\nexit 0\n' >"$stub/apt-get"
printf '#!/bin/sh\nexit 0\n' >"$stub/mountpoint"
chmod +x "$stub"/*
out4="$(PATH="$stub:$PATH" MODULES_FILE="$tmp/modules.conf" bash guest/vmodem-setup 2>&1)"; rc4=$?
[[ $rc4 -eq 2 ]] && ok_ "облачное ядро: просит перезагрузку (код 2)" \
    || bad_ "облачное ядро: просит перезагрузку (код 2)" "       код $rc4"
has "ставит обычное ядро" "$out4" "linux-image-amd64"

# Эта машина — не модем: проверка должна честно упасть и сказать, чего нет.
out3="$(bash guest/vmodem-setup --check 2>&1)"; rc=$?
[[ $rc -ne 0 ]] && ok_ "--check падает там, где модема нет" \
    || bad_ "--check падает там, где модема нет" "       код $rc"
has "--check называет, чего не хватает" "$out3" "usbipd"

echo
echo "итого: ok ${pass}, fail ${fail}"
[[ $fail -eq 0 ]]
