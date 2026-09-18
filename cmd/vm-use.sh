#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════════════════════════
#  pcs vm-use — какая ВМ mobileproxy.space активна.
#
#    pcs vm-use                  список: известные PCS и остальные ВМ Proxmox
#    pcs vm-use <id> [--ip IP]   сделать активной; незнакомую — завести
#    pcs vm-use --forget <id>    забыть (саму ВМ не трогает)
# ═══════════════════════════════════════════════════════════════════════════
set -uo pipefail
# shellcheck source=../lib/common.sh
source "$(dirname "$(readlink -f "$0")")/../lib/common.sh"
source "$PCS_ROOT/lib/state.sh"
source "$PCS_ROOT/lib/pve.sh"

usage() { pcs_usage "$0"; }

O_ID=""; O_IP=""; O_FORGET=""
while (( $# )); do
    case "$1" in
        --ip)     O_IP="$2"; shift 2 ;;
        --forget) O_FORGET="$2"; shift 2 ;;
        -h|--help) usage; exit 0 ;;
        *) [[ -z "$O_ID" ]] || die "лишний аргумент: $1"; O_ID="$1"; shift ;;
    esac
done

pcs_begin vm-use
need_pve

if [[ -n "$O_FORGET" ]]; then
    state_forget "$O_FORGET"
    ok "ВМ ${O_FORGET} забыта (в Proxmox осталась как была)"
    exit 0
fi

vm_status() { qm status "$1" 2>/dev/null | awk '{print $2}'; }

if [[ -z "$O_ID" ]]; then
    active="$(state_active_id)"
    printf '\n  %s%-3s %-7s %-18s %-16s %s%s\n' "$D" "" "ID" "Имя" "IP" "Статус" "$R" >&2
    known=" "
    for id in $(state_ids); do
        state_load "$id"
        mark=" "; [[ "$id" == "$active" ]] && mark="${G}●${R}"
        st="$(vm_status "$id")"; st="${st:-нет в Proxmox}"
        printf '  %-3s %-7s %-18s %-16s %s\n' "$mark" "$id" "${VM_NAME:-?}" "${VM_IP:-—}" "$st" >&2
        known+="$id "
    done
    while read -r id name st _; do
        [[ -n "$id" && "$id" != "VMID" && "$known" != *" $id "* ]] || continue
        printf '  %s%-3s %-7s %-18s %-16s %s%s\n' "$D" "" "$id" "$name" "—" "$st" "$R" >&2
    done < <(qm list 2>/dev/null)
    echo >&2
    info "● — активная. Выбрать: pcs vm-use <id>"
    exit 0
fi

[[ "$O_ID" =~ ^[0-9]+$ ]] || die "ID — число"
vm_exists "$O_ID" || warn "ВМ ${O_ID} в Proxmox не найдена — запоминаю всё равно"

state_load "$O_ID"
if [[ -z "$VM_NAME" ]]; then
    VM_NAME="$(qm config "$O_ID" 2>/dev/null | awk '/^name:/{print $2}')"
fi
if [[ -n "$O_IP" ]]; then
    VM_IP="$O_IP"
elif [[ -z "$VM_IP" ]]; then
    ask VM_IP "IP ВМ ${O_ID}"
fi
if [[ -z "$VM_PASSWORD" ]]; then
    ask_secret VM_PASSWORD "Root-пароль ВМ ${O_ID} (для SSH)"
fi
state_save
state_set_active "$O_ID"
ok "Активная ВМ: ${O_ID} (${VM_NAME:-?}) @ ${VM_IP}"
