#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════════════════════════
#  pcs vm-fix-dns — привести DNS на ВМ в порядок (guest/pcs-fix-dns).
#
#    pcs vm-fix-dns [--vm ID] [--dns "1.1.1.1 8.8.8.8"]
# ═══════════════════════════════════════════════════════════════════════════
set -uo pipefail
# shellcheck source=../lib/common.sh
source "$(dirname "$(readlink -f "$0")")/../lib/common.sh"
source "$PCS_ROOT/lib/state.sh"
source "$PCS_ROOT/lib/ssh.sh"

usage() { pcs_usage "$0"; }

O_VM=""; O_DNS=""
while (( $# )); do
    case "$1" in
        --vm)  O_VM="$2"; shift 2 ;;
        --dns) O_DNS="$2"; shift 2 ;;
        -h|--help) usage; exit 0 ;;
        *) die "неизвестный аргумент: $1" ;;
    esac
done

pcs_begin vm-fix-dns
pcs_tmpdir
ssh_setup
state_need_vm "$O_VM"
vm_alive || die "ВМ ${VM_ID} (${VM_IP}) недоступна по SSH"

if [[ -n "$O_DNS" ]]; then
    read -r d1 d2 _ <<<"$O_DNS"
    [[ "$d1" =~ ^[0-9.]+$ && "${d2:-$d1}" =~ ^[0-9.]+$ ]] || die "--dns: нужны IPv4 через пробел"
    VM_DNS1="$d1"; VM_DNS2="${d2:-$d1}"
    state_save
fi

vm_push_tool pcs-fix-dns || die "не доставлен pcs-fix-dns"
vm_ssh "printf 'PCS_DNS1=%s\nPCS_DNS2=%s\n' '${VM_DNS1}' '${VM_DNS2}' > /etc/default/pcs-dns && /usr/local/sbin/pcs-fix-dns" 2>&1 | relay
rc=${PIPESTATUS[0]}
(( rc == 0 )) && ok "DNS на ВМ ${VM_ID} в порядке (${VM_DNS1} ${VM_DNS2})" || { bad "DNS не в порядке — см. выше"; exit 1; }
