#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════════════════════════
#  pcs modem-attach — воткнуть модем в ВМ mobileproxy.space.
#
#    pcs modem-attach --n 111        воткнуть один
#    pcs modem-attach --n 111,112    воткнуть несколько
#    pcs modem-attach --all          воткнуть все созданные
#    pcs modem-attach --status       что воткнуто сейчас
#    pcs modem-attach --n 111 --detach   вынуть и забыть
#    pcs modem-attach --n 111 --force    воткнуть без софта mp.space на ВМ
#
#  Модем отдаёт своё USB-устройство по сети (usbipd на его ВМ), здесь оно
#  подхватывается vhci-hcd и становится обычным USB-модемом Huawei. Дальше
#  им занимаются скрипты mp.space: DHCP, адрес 192.168.<N>.100, маршруты.
#
#  Подключение держит служба vmodem-attach@<N> — переживает перезагрузку
#  ВМ модема и моргание сети.
# ═══════════════════════════════════════════════════════════════════════════
set -uo pipefail
# shellcheck source=../lib/common.sh
source "$(dirname "$(readlink -f "$0")")/../lib/common.sh"
source "$PCS_ROOT/lib/state.sh"
source "$PCS_ROOT/lib/ssh.sh"
source "$PCS_ROOT/lib/pve.sh"
source "$PCS_ROOT/lib/modem.sh"

usage() { pcs_usage "$0"; }

O_N=""; O_ALL=""; O_VM=""; O_DETACH=""; O_STATUS=""; O_FORCE=""
while (( $# )); do
    case "$1" in
        --n)      O_N="${O_N:+${O_N},}$2"; shift 2 ;;
        --all)    O_ALL=1; shift ;;
        --vm)     O_VM="$2"; shift 2 ;;
        --detach) O_DETACH=1; shift ;;
        --status) O_STATUS=1; shift ;;
        --force)  O_FORCE=1; shift ;;
        -h|--help) usage; exit 0 ;;
        *) die "неизвестный аргумент: $1 (pcs modem-attach --help)" ;;
    esac
done

pcs_begin modem-attach
pcs_tmpdir
ssh_setup
state_need_vm "$O_VM"
mdm_server_need "$VM_ID"
vm_alive || die "ВМ ${VM_ID} (${VM_IP}) недоступна по SSH"

vm_push_tool vmodem-attach || die "не доставлен vmodem-attach"

# Воткнуть USB мало: интерфейс поднимают, адрес выдают и маршруты пишут
# собственные скрипты mp.space. Без них модем виден в lsusb и больше нигде.
if [[ -z "$O_STATUS$O_DETACH" ]] && ! vm_has_mp; then
    if [[ -z "$O_FORCE" ]]; then
        bad "на ВМ ${VM_ID} нет софта mobileproxy.space"
        info "Модем воткнётся, но интерфейс поднимать некому — адрес 192.168.<N>.100 не появится."
        die "сначала: pcs mp-install   (всё равно воткнуть: --force)"
    fi
    warn "софта mobileproxy.space на ВМ нет (--force) — адрес 192.168.<N>.100 не появится"
fi

if [[ -n "$O_STATUS" ]]; then
    vm_ssh "vmodem-attach list" 2>&1 | relay
    exit 0
fi

# ── Кого втыкаем ───────────────────────────────────────────────────────────
targets=()
if [[ -n "$O_ALL" ]]; then
    while read -r n; do [[ -n "$n" ]] && targets+=("$n"); done < <(mdm_list)
    (( ${#targets[@]} )) || die "созданных модемов нет (pcs modem-add)"
else
    ask O_N "Номер модема (можно списком через запятую)"
    for n in ${O_N//,/ }; do
        [[ "$n" =~ ^[0-9]+$ ]] || die "номер модема: ${n}"
        targets+=("$n")
    done
    (( ${#targets[@]} )) || die "номер модема не задан"
fi

if [[ -n "$O_DETACH" ]]; then
    for n in "${targets[@]}"; do
        vm_ssh "vmodem-attach remove ${n}" 2>&1 | relay
    done
    exit 0
fi

# ── Один раз готовим ВМ ────────────────────────────────────────────────────
hdr "Готовлю ВМ ${VM_ID} к USB по сети"
cat >"$PCS_TMP/prep.sh" <<'EOF'
set -u
export DEBIAN_FRONTEND=noninteractive
# usbip приезжает то отдельным пакетом, то в linux-tools рядом с ядром.
if ! command -v usbip >/dev/null 2>&1 && ! ls /usr/lib/linux-tools/*/usbip >/dev/null 2>&1; then
    apt-get -o DPkg::Lock::Timeout=300 install -y -qq usbip \
        || apt-get -o DPkg::Lock::Timeout=300 install -y -qq linux-tools-generic \
        || { echo "не поставился usbip"; exit 1; }
fi
# vhci-hcd — сторона, которая принимает устройство по сети.
modprobe vhci-hcd 2>/dev/null || {
    echo "vhci-hcd не грузится, ставлю модули для текущего ядра"
    apt-get -o DPkg::Lock::Timeout=300 install -y -qq "linux-modules-extra-$(uname -r)" \
        || apt-get -o DPkg::Lock::Timeout=300 install -y -qq linux-image-extra-virtual
    modprobe vhci-hcd 2>/dev/null || { echo "vhci-hcd так и не загрузился"; exit 1; }
}
mkdir -p /etc/modules-load.d
printf '# vmodem-attach: приём USB по сети\nvhci-hcd\n' > /etc/modules-load.d/vmodem-attach.conf
echo "vhci-hcd: $(lsmod | grep -c '^vhci_hcd') модуль(ей) загружено"
exit 0
EOF
vm_run_detached attach-prep "$PCS_TMP/prep.sh" 900 || die "ВМ не готова принимать USB — см. выше"
vm_ssh "vmodem-attach install-service" 2>&1 | relay

# ── Втыкаем ────────────────────────────────────────────────────────────────
# Сначала втыкаем всех — это быстро, — и только потом одним заходом ждём
# адреса. По очереди на двадцати модемах каждый неудачный ждал бы минуту,
# и команда растягивалась на полчаса: оборвётся сессия — половина не
# воткнута. Заодно это два похода по SSH вместо двух на каждый модем.
problems=0
pairs=""; list=""
for n in "${targets[@]}"; do
    mdm_load "$n"
    if [[ -z "${MDM_IP:-}" ]]; then
        bad "модем ${n}: адрес его ВМ неизвестен (pcs modem-add --n ${n})"
        problems=$((problems + 1)); continue
    fi
    pairs+="${n} ${MDM_IP}"$'\n'
    list+="${n} "
done
[[ -n "$list" ]] || die "втыкать нечего"

hdr "Втыкаю модемы: $(wc -w <<<"$list" | tr -d ' ')"
{
    printf 'set -u\n'
    cat <<'ATTACH'
while read -r n ip; do
    [ -n "$n" ] || continue
    if vmodem-attach add "$n" "$ip" >/dev/null 2>&1; then
        systemctl enable --now "vmodem-attach@${n}.service" >/dev/null 2>&1
        echo "OK $n $ip"
    else
        echo "FAIL $n $ip"
    fi
done <<'PAIRS'
ATTACH
    printf '%sPAIRS\n' "$pairs"
} | vm_ssh_in "bash -s" >"$PCS_TMP/attach.out" 2>&1

while read -r st n ip; do
    case "$st" in
        OK)   ok "модем ${n} → ${ip}" ;;
        FAIL) bad "модем ${n} (${ip}) не воткнулся — проверь vmodem status на ВМ модема"
              problems=$((problems + 1)) ;;
        *)    [[ -n "$st" ]] && printf '    %s%s %s %s%s\n' "$D" "$st" "$n" "$ip" "$R" >&2 ;;
    esac
done <"$PCS_TMP/attach.out"

# ── Ждём, пока mp.space поднимет интерфейсы ────────────────────────────────
hdr "Жду, пока mp.space настроит интерфейсы"
{
    printf 'set -u\nLIST="%s"\n' "$list"
    cat <<'WAIT'
t=0
while [ "$t" -lt "${ATTACH_WAIT:-150}" ]; do
    miss=""
    have="$(ip -4 -o addr show 2>/dev/null)"
    for n in $LIST; do
        printf '%s' "$have" | grep -q "192\.168\.${n}\.100/" || miss="$miss $n"
    done
    [ -z "$miss" ] && { echo "ALL $t"; exit 0; }
    [ $((t % 30)) -eq 0 ] && [ "$t" -gt 0 ] && echo "WAIT $t |$miss"
    sleep 5; t=$((t + 5))
done
echo "MISS 0 |$miss"
WAIT
} | vm_ssh_in "bash -s" >"$PCS_TMP/wait.out" 2>&1

while IFS= read -r line; do
    case "$line" in
        ALL\ *)  ok "все модемы получили 192.168.<N>.100 (${line#ALL }с)" ;;
        WAIT\ *) step "${line#*|} — ещё без адреса ($(cut -d' ' -f2 <<<"$line")с)" ;;
        MISS\ *) bad "адрес так и не появился:${line#*|}"
                 info "их поднимают скрипты mp.space — смотри /var/log/modem-setup.log на ВМ"
                 problems=$((problems + 1)) ;;
        "")      ;;
        *)       printf '    %s%s%s\n' "$D" "$line" "$R" >&2 ;;
    esac
done <"$PCS_TMP/wait.out"

hdr "Итог"
vm_ssh "vmodem-attach check" 2>&1 | relay
[[ ${PIPESTATUS[0]} -eq 0 ]] || problems=$((problems+1))
echo >&2
if (( problems == 0 )); then
    ok "модемы воткнуты в ВМ ${VM_ID}"
    info "В ЛК mobileproxy.space: тип 3, local_ip 192.168.<N>.100, adm_ip 192.168.<N>.1"
else
    bad "с частью модемов не сложилось: ${problems} — см. выше"
    exit 1
fi
