# shellcheck shell=bash
# ═══════════════════════════════════════════════════════════════════════════
#  lib/modem.sh — что PCS знает о виртуальных модемах.
#
#  /etc/pcs/modem/template.conf   шаблон ВМ модема (в нём пароль — права 600)
#  /etc/pcs/modem/<N>.conf        один модем: номер, прокси, VM ID, IP
# ═══════════════════════════════════════════════════════════════════════════
[[ -n "${_PCS_MODEM_LOADED:-}" ]] && return 0
_PCS_MODEM_LOADED=1

PCS_MODEM_DIR="${PCS_ETC}/modem"
TPL_FILE="${PCS_MODEM_DIR}/template.conf"

TPL_KEYS=(TPL_ID TPL_NAME TPL_PASSWORD TPL_STORAGE TPL_BRIDGE TPL_RAM TPL_CORES TPL_DISK TPL_SINGBOX)
MDM_KEYS=(MDM_N MDM_REAL MDM_VMID MDM_IP MDM_PROXY MDM_PROXY_USER MDM_PROXY_PASS MDM_NAME)

tpl_reset() {
    TPL_ID=""; TPL_NAME="vmodem-template"; TPL_PASSWORD=""
    TPL_STORAGE="local-lvm"; TPL_BRIDGE="vmbr0"
    TPL_RAM="256"; TPL_CORES="1"; TPL_DISK="4"; TPL_SINGBOX="1.10.0"
}
tpl_reset

tpl_load() {
    tpl_reset
    # shellcheck disable=SC1090
    [[ -f "$TPL_FILE" ]] && source "$TPL_FILE"
    return 0
}

tpl_save() {
    install -d -m 700 "$PCS_MODEM_DIR"
    local t="${TPL_FILE}.tmp" k
    {
        echo "# pcs ${PCS_VERSION}: шаблон ВМ виртуального модема. Права 600 — здесь пароль."
        for k in "${TPL_KEYS[@]}"; do printf '%s=%q\n' "$k" "${!k:-}"; done
    } >"$t"
    chmod 600 "$t"
    mv -f "$t" "$TPL_FILE"
}

tpl_need() {
    tpl_load
    [[ -n "${TPL_ID:-}" ]] || die "шаблона модема нет: сначала pcs modem-template"
    vm_exists "$TPL_ID" || die "шаблон ${TPL_ID} пропал из Proxmox — собери заново: pcs modem-template"
}

# ── Отдельный модем ────────────────────────────────────────────────────────
mdm_reset() {
    MDM_N=""; MDM_REAL=""; MDM_VMID=""; MDM_IP=""
    MDM_PROXY=""; MDM_PROXY_USER=""; MDM_PROXY_PASS=""; MDM_NAME=""
}

mdm_file() { printf '%s/%s.conf' "$PCS_MODEM_DIR" "$1"; }

mdm_load() {                       # mdm_load <N>
    mdm_reset
    # shellcheck disable=SC1090
    [[ -f "$(mdm_file "$1")" ]] && source "$(mdm_file "$1")"
    MDM_N="$1"
    return 0
}

mdm_save() {
    [[ -n "${MDM_N:-}" ]] || die "mdm_save: номер модема пуст"
    install -d -m 700 "$PCS_MODEM_DIR"
    local f t k
    f="$(mdm_file "$MDM_N")"; t="${f}.tmp"
    {
        echo "# pcs ${PCS_VERSION}: виртуальный модем ${MDM_N}. Права 600 — здесь пароль прокси."
        for k in "${MDM_KEYS[@]}"; do printf '%s=%q\n' "$k" "${!k:-}"; done
    } >"$t"
    chmod 600 "$t"
    mv -f "$t" "$f"
}

mdm_list() {
    local f
    for f in "$PCS_MODEM_DIR"/*.conf; do
        [[ -f "$f" ]] || continue
        f="${f##*/}"; f="${f%.conf}"
        [[ "$f" == "template" ]] && continue
        printf '%s\n' "$f"
    done
}

mdm_forget() { rm -f "$(mdm_file "$1")"; }
