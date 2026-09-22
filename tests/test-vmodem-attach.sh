#!/usr/bin/env bash
# Проверка guest/vmodem-attach без ВМ и без root: конфиг, разбор usbip port,
# юнит службы и режим показа.
#   bash tests/test-vmodem-attach.sh
set -u
cd "$(dirname "$0")/.." || exit 1
VA="$PWD/guest/vmodem-attach"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
export ATTACH_ETC="$TMP/etc"
pass=0; fail=0
ok_()  { pass=$((pass + 1)); printf '  ok   %s\n' "$1"; }
bad_() { fail=$((fail + 1)); printf '  FAIL %s\n%s\n' "$1" "${2:-}"; }
has()  { case "$2" in *"$3"*) ok_ "$1" ;; *) bad_ "$1" "       нет: $3" ;; esac; }

# Подменяем usbip: он же источник правды про «воткнуто/не воткнуто».
stub="$TMP/stub"; mkdir -p "$stub"
cat > "$stub/usbip" <<'EOS'
#!/bin/sh
case "$1" in
  port) cat "$USBIP_PORT_OUT" 2>/dev/null ;;
  attach) echo "attach $*" >> "$USBIP_CALLS" ;;
  detach) echo "detach $*" >> "$USBIP_CALLS" ;;
esac
exit 0
EOS
cat > "$stub/lsmod" <<'EOS'
#!/bin/sh
echo "vhci_hcd               45056  0"
EOS
cat > "$stub/modprobe" <<'EOS'
#!/bin/sh
exit 0
EOS
chmod +x "$stub"/*
export PATH="$stub:$PATH" USBIP_CALLS="$TMP/calls" USBIP_PORT_OUT="$TMP/port.out"
: > "$USBIP_CALLS"; : > "$USBIP_PORT_OUT"

echo "vmodem-attach:"

out="$(bash "$VA" add 111 192.168.88.3 2>&1)"
has "модем записан в конфиг" "$(cat "$TMP/etc/111.conf" 2>/dev/null)" "IP=192.168.88.3"
has "позвали usbip attach"   "$(cat "$USBIP_CALLS")" "attach attach -r 192.168.88.3 -d usbip-vudc.0"
has "сказал, что не увидел устройства" "$out" "устройства не видно"

# Теперь usbip port показывает наш модем — значит воткнут.
cat > "$USBIP_PORT_OUT" <<'EOS'
Imported USB devices
====================
Port 00: <Port in Use> at High Speed(480Mbps)
       HUAWEI : unknown product (12d1:14dc)
       3-1 -> usbip://192.168.88.3:3240/usbip-vudc.0
EOS
out="$(bash "$VA" up 111 2>&1)"
has "повторный вызов видит, что уже воткнут" "$out" "уже воткнут"
out="$(bash "$VA" list 2>&1)"
has "в списке модем"     "$out" "111"
has "в списке адрес ВМ"  "$out" "192.168.88.3"
has "в списке состояние" "$out" "воткнут"

: > "$USBIP_CALLS"
out="$(bash "$VA" down 111 2>&1)"
has "вынимаем по номеру порта" "$(cat "$USBIP_CALLS")" "detach detach -p 00"

out="$(bash "$VA" add 111 не-адрес 2>&1)"; rc=$?
[[ $rc -ne 0 ]] && ok_ "кривой адрес отбит" || bad_ "кривой адрес отбит"

out="$(bash "$VA" up 999 2>&1)"; rc=$?
[[ $rc -ne 0 ]] && ok_ "незаведённый модем отбит" || bad_ "незаведённый модем отбит"
has "подсказка, как завести" "$out" "vmodem-attach add"

echo
echo "итого: ok ${pass}, fail ${fail}"
[[ $fail -eq 0 ]]
