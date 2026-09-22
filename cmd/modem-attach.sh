#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════════════════════════
#  pcs modem-attach — воткнуть модем в ВМ mobileproxy.space.
#
#    pcs modem-attach --n 111        воткнуть один
#    pcs modem-attach --all          воткнуть все созданные
#    pcs modem-attach --status       что воткнуто сейчас
#    pcs modem-attach --n 111 --detach   вынуть и забыть
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

O_N=""; O_ALL=""; O_VM=""; O_DETACH=""; O_STATUS=""
while (( $# )); do
    case "$1" in
        --n)      O_N="$2"; shift 2 ;;
        --all)    O_ALL=1; shift ;;
        --vm)     O_VM="$2"; shift 2 ;;
        --detach) O_DETACH=1; shift ;;
        --status) O_STATUS=1; shift ;;
        -h|--help) usage; exit 0 ;;
        *) die "неизвестный аргумент: $1 (pcs modem-attach --help)" ;;
    esac
done

pcs_begin modem-attach
pcs_tmpdir
ssh_setup
state_need_vm "$O_VM"
vm_alive || die "ВМ ${VM_ID} (${VM_IP}) недоступна по SSH"

vm_push_tool vmodem-attach || die "не доставлен vmodem-attach"

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
    ask O_N "Номер модема"
    targets=("$O_N")
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
problems=0
for n in "${targets[@]}"; do
    mdm_load "$n"
    [[ -n "${MDM_IP:-}" ]] || { bad "модем ${n}: адрес его ВМ неизвестен (pcs modem-add --n ${n})"; problems=$((problems+1)); continue; }
    hdr "Модем ${n} (${MDM_IP})"
    vm_ssh "vmodem-attach add ${n} ${MDM_IP}" 2>&1 | relay
    if [[ ${PIPESTATUS[0]} -ne 0 ]]; then bad "модем ${n} не воткнулся"; problems=$((problems+1)); continue; fi
    vm_ssh "systemctl enable --now vmodem-attach@${n}.service" 2>&1 | relay
    # Скриптам mp.space нужно время поднять интерфейс и получить адрес.
    step "Жду, пока mp.space настроит интерфейс..."
    got=""
    for i in 1 2 3 4 5 6 7 8 9 10; do
        got="$(vm_ssh "ip -4 -o addr show | awk -v p=192.168.${n}.100 '\$4 ~ \"^\"p\"/\" {print \$2; exit}'" 2>/dev/null)"
        [[ -n "$got" ]] && break
        sleep 5
    done
    if [[ -n "$got" ]]; then
        ok "модем ${n}: ${got} = 192.168.${n}.100"
    else
        warn "адрес 192.168.${n}.100 пока не появился — см. /var/log/modem-setup.log на ВМ"
        problems=$((problems+1))
    fi
done

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
