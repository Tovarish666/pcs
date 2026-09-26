# shellcheck shell=bash
# ═══════════════════════════════════════════════════════════════════════════
#  lib/state.sh — что PCS знает о ВМ mobileproxy.space.
#
#  /etc/pcs/mp/<vmid>.conf   параметры ВМ (IP, пароль root, SSH-порт, DNS)
#  /etc/pcs/mp/active        ВМ, с которой работают команды без --vm
#
#  Значения пишутся через printf %q и права 600: пароль с пробелом или $(...)
#  не ломает файл и не исполняется при чтении.
# ═══════════════════════════════════════════════════════════════════════════
[[ -n "${_PCS_STATE_LOADED:-}" ]] && return 0
_PCS_STATE_LOADED=1

PCS_MP_DIR="${PCS_ETC}/mp"
STATE_KEYS=(VM_ID VM_NAME VM_IP VM_SSH_PORT VM_PASSWORD VM_DNS1 VM_DNS2 VM_NET_MODE VM_BRIDGE VM_PROXY)

state_reset() {
    VM_ID=""; VM_NAME=""; VM_IP=""; VM_SSH_PORT="22"; VM_PASSWORD=""
    VM_DNS1="1.1.1.1"; VM_DNS2="8.8.8.8"; VM_NET_MODE="static"; VM_BRIDGE="vmbr0"
    VM_PROXY=""                    # HTTP-прокси для скачивания софта mp.space
}
state_reset

state_file() { printf '%s/%s.conf' "$PCS_MP_DIR" "$1"; }
state_exists() { [[ -f "$(state_file "$1")" ]]; }

state_load() {                     # state_load <vmid>
    state_reset
    local f; f="$(state_file "$1")"
    # shellcheck disable=SC1090
    [[ -f "$f" ]] && source "$f"
    VM_ID="$1"
}

state_save() {
    [[ -n "${VM_ID:-}" ]] || die "state_save: VM_ID пуст"
    install -d -m 700 "$PCS_MP_DIR"
    local f t k
    f="$(state_file "$VM_ID")"; t="${f}.tmp"
    {
        echo "# pcs ${PCS_VERSION}: ВМ mobileproxy.space. Значения экранированы — править через pcs."
        for k in "${STATE_KEYS[@]}"; do printf '%s=%q\n' "$k" "${!k:-}"; done
    } >"$t"
    chmod 600 "$t"
    mv -f "$t" "$f"
}

state_active_id() { cat "${PCS_MP_DIR}/active" 2>/dev/null || true; }

state_set_active() {
    install -d -m 700 "$PCS_MP_DIR"
    printf '%s\n' "$1" >"${PCS_MP_DIR}/active"
    chmod 600 "${PCS_MP_DIR}/active"
}

state_ids() {
    local f
    for f in "$PCS_MP_DIR"/*.conf; do
        [[ -f "$f" ]] || continue
        f="${f##*/}"; printf '%s\n' "${f%.conf}"
    done
}

state_forget() {
    rm -f "$(state_file "$1")"
    [[ "$(state_active_id)" == "$1" ]] && rm -f "${PCS_MP_DIR}/active"
    return 0
}

# Загрузить ВМ для работы: явный --vm, иначе активная. Недостающее — спросить.
# После вызова заданы VM_ID, VM_IP, VM_PASSWORD.
state_need_vm() {                  # state_need_vm [vmid]
    local id="${1:-}"
    [[ -n "$id" ]] || id="$(state_active_id)"
    [[ -n "$id" ]] || die "ВМ не выбрана: pcs vm-use <vmid> или --vm <vmid>"
    state_load "$id"
    local changed=""
    if [[ -z "$VM_IP" ]]; then ask VM_IP "IP ВМ ${id}"; changed=1; fi
    if [[ -z "$VM_PASSWORD" ]]; then ask_secret VM_PASSWORD "Root-пароль ВМ ${id}"; changed=1; fi
    [[ -n "$changed" ]] && state_save
    return 0
}
