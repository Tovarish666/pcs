#!/usr/bin/env bash
# Проверка выбора номера ВМ без Proxmox: тысяча под сервер должна быть
# свободна целиком, а считаться это должно мгновенно — не по одному номеру
# через qm (на живом хосте это занимало минуты).
#   bash tests/test-pve-ids.sh
set -u
cd "$(dirname "$0")/.." || exit 1
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
pass=0; fail=0
ok_()  { pass=$((pass + 1)); printf '  ok   %s\n' "$1"; }
bad_() { fail=$((fail + 1)); printf '  FAIL %s\n%s\n' "$1" "${2:-}"; }
eq()   { [[ "$2" == "$3" ]] && ok_ "$1" || bad_ "$1" "       получили $2, ждали $3"; }

# Два подставных Proxmox. Молчаливый — для основной части: всё берётся из
# конфигов. Говорливый занимает тысячи 1000–4000 и нужен, чтобы увидеть,
# спрашивали его вообще или нет.
quiet="$TMP/quiet"; loud="$TMP/loud"; mkdir -p "$quiet" "$loud"
printf '#!/bin/sh\necho "VMID NAME"\n'   >"$quiet/qm"
printf '#!/bin/sh\necho "VMID Status"\n' >"$quiet/pct"
printf '#!/bin/sh\necho "VMID NAME"\necho "1000 из-qm"\necho "2000 из-qm"\necho "3000 из-qm"\n' >"$loud/qm"
printf '#!/bin/sh\necho "VMID Status"\necho "4000 из-pct"\n' >"$loud/pct"
chmod +x "$quiet"/* "$loud"/*

conf="$TMP/pve"; mkdir -p "$conf/nodes/pve/qemu-server" "$conf/nodes/pve/lxc"
mk() { : > "$conf/nodes/pve/$1/$2.conf"; }

next() {                           # next [каталог подставного proxmox] [каталог конфигов]
    ( export PATH="${1:-$quiet}:$PATH" PVE_CONF_DIR="${2:-$conf}"
      export PCS_LOG="$TMP/log" PCS_LOG_DIR="$TMP"
      source lib/common.sh; source lib/pve.sh; pve_next_server_id )
}

echo "номера ВМ:"

eq "на пустом хосте — 1000" "$(next)" "1000"

mk qemu-server 100
eq "ВМ 100 не мешает тысяче" "$(next)" "1000"

mk qemu-server 1000
eq "занятая 1000 — берём 2000" "$(next)" "2000"

# Модем занимает тысячу сервера целиком: 2111 закрывает всю вторую тысячу.
mk qemu-server 2111
eq "модем в тысяче тоже занимает её" "$(next)" "3000"

mk lxc 3000
eq "контейнер считается так же" "$(next)" "4000"

# Верхняя граница тысячи — +254: 4255 её свободной не ломает.
mk qemu-server 4255
eq "за пределом тысячи не мешает" "$(next)" "4000"

# Конфиги на месте — qm и pct не спрашиваем: иначе ответом было бы 5000.
eq "лишних запусков qm нет" "$(next "$loud")" "4000"

# Каталога конфигов нет — только qm и pct и остаются, а они заняли 1000–4000.
eq "без конфигов спрашиваем qm и pct" "$(next "$loud" "$TMP/пусто")" "5000"

# Считаться должно мгновенно: раньше здесь было 255 запусков qm на тысячу.
start="$(date +%s)"
for i in 1 2 3 4 5; do next >/dev/null; done
took=$(( $(date +%s) - start ))
[[ $took -le 5 ]] && ok_ "пять вычислений уложились в ${took}с" \
    || bad_ "пять вычислений уложились в 5с" "       заняло ${took}с"

echo
echo "итого: ok ${pass}, fail ${fail}"
[[ $fail -eq 0 ]]
