#!/usr/bin/env bash
# Проверка установки софта mp.space без ВМ: строки прокси для скрипта на ВМ
# и то, что команда предупреждает про прокси в справке.
#   bash tests/test-mp-install.sh
set -u
cd "$(dirname "$0")/.." || exit 1
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
pass=0; fail=0
ok_()  { pass=$((pass + 1)); printf '  ok   %s\n' "$1"; }
bad_() { fail=$((fail + 1)); printf '  FAIL %s\n%s\n' "$1" "${2:-}"; }
has()  { case "$2" in *"$3"*) ok_ "$1" ;; *) bad_ "$1" "       нет: $3" ;; esac; }
hasnt(){ case "$2" in *"$3"*) bad_ "$1" "       нашлось: $3" ;; *) ok_ "$1" ;; esac; }

export PCS_LOG_DIR="$TMP" PCS_LOG="$TMP/pcs.log"

# Пароль прокси спокойно бывает с пробелом, кавычкой и $ — строки для ВМ
# собираются через printf %q, так что дойти он должен как есть.
PX='http://user:p@ss w"rd$1@1.2.3.4:3128'
env_out="$(bash -c 'source lib/common.sh; source lib/state.sh; source lib/ssh.sh
                    VM_PROXY="$1"; vm_proxy_env' _ "$PX" 2>&1)"

echo "mp-install:"
has "http_proxy задаётся"  "$env_out" "export http_proxy="
has "HTTPS_PROXY тоже"     "$env_out" "HTTPS_PROXY="
has "локальное мимо прокси" "$env_out" "192.168.0.0/16"

# Главное: то, что доедет до ВМ, должно разворачиваться в исходный адрес.
got="$(bash -c "$env_out"'; printf %s "$https_proxy"')"
[[ "$got" == "$PX" ]] && ok_ "адрес прокси доезжает без искажений" \
    || bad_ "адрес прокси доезжает без искажений" "       получили: $got"

empty="$(bash -c 'source lib/common.sh; source lib/state.sh; source lib/ssh.sh
                  VM_PROXY=""; vm_proxy_env' 2>&1)"
[[ -z "$empty" ]] && ok_ "без прокси ничего не экспортируется" \
    || bad_ "без прокси ничего не экспортируется" "       вывод: $empty"

# Прокси хранится рядом с остальными параметрами ВМ, а файл — 600.
st="$(bash -c 'source lib/common.sh; source lib/state.sh
               PCS_ETC="$1"; PCS_MP_DIR="$1/mp"
               VM_ID=1000; VM_IP=10.0.0.1; VM_PASSWORD=x; VM_PROXY="$2"; state_save
               state_reset; state_load 1000; printf %s "$VM_PROXY"' _ "$TMP" "$PX" 2>&1)"
[[ "$st" == "$PX" ]] && ok_ "прокси запоминается в состоянии ВМ" \
    || bad_ "прокси запоминается в состоянии ВМ" "       получили: $st"
perm="$(stat -c %a "$TMP/mp/1000.conf" 2>/dev/null || stat -f %Lp "$TMP/mp/1000.conf" 2>/dev/null)"
[[ "$perm" == "600" ]] && ok_ "состояние с прокси — 600" || bad_ "состояние с прокси — 600" "       права $perm"

help="$(bash cmd/mp-install.sh --help 2>&1)"
has "справка про --proxy"      "$help" "--proxy"
has "справка про зеркала"      "$help" "зеркал"
has "шагов теперь семь"        "$help" "7. перезагрузка"

# ВМ с модемами не обязана быть сервером mp.space: зависимости быть не должно.
ath="$(bash cmd/modem-attach.sh --help 2>&1)"
hasnt "modem-attach не требует mp.space" "$ath" "--force"
dep="$(bash cmd/modem-deploy.sh --help 2>&1)"
has "modem-deploy честно про это говорит" "$dep" "не обязана быть сервером"
has "modem-deploy поднимает потолок USB"  "$dep" "потолок USB"

# Потолок USB: своя команда и дебиановские числа по умолчанию.
usb="$(bash cmd/vm-usb-ports.sh --help 2>&1)"
has "vm-usb-ports про 8 устройств"  "$usb" "ровно 8 устройств"
has "vm-usb-ports про dkms"         "$usb" "dkms"
vp="$(bash guest/vhci-ports set --dry-run 2>&1)"
has "по умолчанию 15 × 8 = 120"     "$vp" "15 портов × 8 контроллеров = 120"
has "модули идут в dkms"            "$vp" "dkms install"
bad32="$(bash guest/vhci-ports set --controllers 32 --dry-run 2>&1)"; rc32=$?
[[ $rc32 -ne 0 ]] && ok_ "32 контроллера отбиты (на них ядро падало)" \
    || bad_ "32 контроллера отбиты (на них ядро падало)"

echo
echo "итого: ok ${pass}, fail ${fail}"
[[ $fail -eq 0 ]]
