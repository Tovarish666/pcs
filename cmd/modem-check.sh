#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════════════════════════
#  pcs modem-check — проверить модемы целиком: от их ВМ до внешнего адреса.
#
#    pcs modem-check                 все созданные
#    pcs modem-check --n 113         один
#    pcs modem-check --n 113,114     несколько
#    pcs modem-check --quick         без внешнего адреса, только состояние
#    pcs modem-check --rotate        ещё и сменить IP, убедиться, что сменился
#
#  Что проверяется на каждый модем:
#    1. ВМ модема запущена в Proxmox
#    2. внутри неё всё поднято — vmodem check (гаджет, usbipd, туннель, DHCP)
#    3. на ВМ mobileproxy.space — USB воткнут, есть 192.168.<N>.100,
#       веб-морда 192.168.<N>.1 отвечает
#    4. внешний адрес через этот модем — свой у каждого
#
#  Ничего не чинит и ничего не меняет (кроме --rotate, который меняет IP
#  на стороне оператора). Код возврата 1, если хоть что-то не сошлось.
# ═══════════════════════════════════════════════════════════════════════════
set -uo pipefail
# shellcheck source=../lib/common.sh
source "$(dirname "$(readlink -f "$0")")/../lib/common.sh"
source "$PCS_ROOT/lib/state.sh"
source "$PCS_ROOT/lib/ssh.sh"
source "$PCS_ROOT/lib/pve.sh"
source "$PCS_ROOT/lib/modem.sh"

usage() { pcs_usage "$0"; }

O_N=""; O_VM=""; O_QUICK=""; O_ROTATE=""
while (( $# )); do
    case "$1" in
        --n)      O_N="${O_N:+${O_N},}$2"; shift 2 ;;
        --vm)     O_VM="$2"; shift 2 ;;
        --quick)  O_QUICK=1; shift ;;
        --rotate) O_ROTATE=1; shift ;;
        -h|--help) usage; exit 0 ;;
        *) die "неизвестный аргумент: $1 (pcs modem-check --help)" ;;
    esac
done

pcs_begin modem-check
pcs_tmpdir
ssh_setup
tpl_load
state_need_vm "$O_VM"

SRV_IP="$VM_IP"; SRV_PORT="${VM_SSH_PORT:-22}"; SRV_PASS="$VM_PASSWORD"; SRV_ID="$VM_ID"
use_server() { VM_IP="$SRV_IP"; VM_SSH_PORT="$SRV_PORT"; VM_PASSWORD="$SRV_PASS"; }
use_modem()  { VM_IP="$1";      VM_SSH_PORT=22;          VM_PASSWORD="${TPL_PASSWORD:-}"; }

targets=()
if [[ -n "$O_N" ]]; then
    for n in ${O_N//,/ }; do
        [[ "$n" =~ ^[0-9]+$ ]] || die "номер модема: ${n}"
        targets+=("$n")
    done
else
    while read -r n; do [[ -n "$n" ]] && targets+=("$n"); done < <(mdm_list)
    (( ${#targets[@]} )) || die "созданных модемов нет (pcs modem-add)"
fi

use_server
vm_alive || die "ВМ ${SRV_ID} (${SRV_IP}) недоступна по SSH"
vm_push_tool vmodem-attach >/dev/null || die "не доставлен vmodem-attach"

problems=0
oops() { bad "$*"; problems=$((problems + 1)); }

for n in "${targets[@]}"; do
    mdm_load "$n"
    hdr "Модем ${n}"

    # 1. ВМ модема
    if [[ -z "${MDM_VMID:-}" ]]; then
        oops "PCS про модем ${n} ничего не знает (pcs modem-add --n ${n})"; continue
    elif ! vm_exists "$MDM_VMID"; then
        oops "ВМ ${MDM_VMID} в Proxmox нет"; continue
    elif ! vm_running "$MDM_VMID"; then
        oops "ВМ ${MDM_VMID} выключена (pcs modem-sync)"; continue
    else
        ok "ВМ ${MDM_VMID} запущена, адрес ${MDM_IP:-?}"
    fi

    # 2. Внутри ВМ модема
    if [[ -n "${MDM_IP:-}" && -n "${TPL_PASSWORD:-}" ]]; then
        use_modem "$MDM_IP"
        if vm_alive; then
            vm_ssh "vmodem check" 2>&1 | relay
            [[ ${PIPESTATUS[0]} -eq 0 ]] || oops "внутри ВМ модема ${n} не всё поднято"
        else
            oops "ВМ модема ${n} (${MDM_IP}) не отвечает по SSH"
        fi
        use_server
    else
        warn "модем ${n}: нечем зайти на его ВМ — проверяю только со стороны сервера"
    fi

    # 3. Сторона mobileproxy.space
    vm_ssh "vmodem-attach check ${n}" 2>&1 | relay
    [[ ${PIPESTATUS[0]} -eq 0 ]] || oops "на сервере модем ${n} не в порядке"

    # 4. Внешний адрес
    if [[ -n "$O_ROTATE" ]]; then
        vm_ssh "vmodem-attach rotate ${n}" 2>&1 | relay
        [[ ${PIPESTATUS[0]} -eq 0 ]] || oops "модем ${n}: IP не сменился"
    elif [[ -z "$O_QUICK" ]]; then
        vm_ssh "vmodem-attach ip ${n}" 2>&1 | relay
        [[ ${PIPESTATUS[0]} -eq 0 ]] || oops "модем ${n}: внешний адрес не получен"
    fi
done

echo >&2
if (( problems == 0 )); then
    ok "все модемы в порядке: ${#targets[@]}"
else
    bad "проблем: ${problems} — см. выше"
    exit 1
fi
