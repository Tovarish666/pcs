#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════════════════════════
#  pcs modem-list — что в таблице и что из этого уже есть на хосте.
#
#    pcs modem-list                      таблица + состояние ВМ
#    pcs modem-list --source <ссылка>    разовая таблица, не запоминая
#    pcs modem-list --local              только то, что PCS уже создал
#
#  Колонка «ВМ» — клон шаблона под этот модем: её номер, состояние и адрес.
#  Снизу — модемы, которые есть на хосте, но пропали из таблицы.
# ═══════════════════════════════════════════════════════════════════════════
set -uo pipefail
# shellcheck source=../lib/common.sh
source "$(dirname "$(readlink -f "$0")")/../lib/common.sh"
source "$PCS_ROOT/lib/state.sh"
source "$PCS_ROOT/lib/pve.sh"
source "$PCS_ROOT/lib/modem.sh"
source "$PCS_ROOT/lib/sheet.sh"

usage() { pcs_usage "$0"; }

O_SOURCE=""; O_LOCAL=""; O_VM=""
while (( $# )); do
    case "$1" in
        --source) O_SOURCE="$2"; shift 2 ;;
        --vm)     O_VM="$2"; shift 2 ;;
        --local)  O_LOCAL=1; shift ;;
        -h|--help) usage; exit 0 ;;
        *) die "неизвестный аргумент: $1" ;;
    esac
done

pcs_begin modem-list
pcs_tmpdir
mdm_server_need "$O_VM"

vm_state() {                       # vm_state <vmid> → строка состояния
    local id="${1:-}"
    [[ -n "$id" ]] || { printf '—'; return; }
    command -v qm >/dev/null 2>&1 || { printf '%s' "$id"; return; }
    local st; st="$(qm status "$id" 2>/dev/null | awk '{print $2}')"
    printf '%s %s' "$id" "${st:-нет}"
}

rows=""
if [[ -z "$O_LOCAL" ]]; then
    if [[ -n "$O_SOURCE" ]]; then
        rows="$(sheet_rows "$(sheet_url_normalize "$O_SOURCE")")" || die "таблица не прочиталась"
    else
        sheet_url_load
        if [[ -n "$SHEET_URL" ]]; then
            rows="$(sheet_rows "$SHEET_URL")" || die "таблица не прочиталась"
        else
            warn "таблица не задана (pcs modem-source set <ссылка>) — показываю только своё"
            O_LOCAL=1
        fi
    fi
fi

printf '\n  %s%-5s %-5s %-24s %-16s %-6s %s%s\n' "$D" \
    "n" "real" "прокси" "ВМ" "вкл" "адрес" "$R" >&2

seen=" "
if [[ -n "$rows" ]]; then
    while IFS=$'\t' read -r n real host port login _pass enabled; do
        [[ -n "$n" ]] || continue
        mdm_load "$n"
        printf '  %-5s %-5s %-24s %-16s %-6s %s\n' \
            "$n" "$real" "${host}:${port}" "$(vm_state "${MDM_VMID:-}")" \
            "$([[ "$enabled" == "1" ]] && echo да || echo нет)" \
            "${MDM_IP:-—}" >&2
        seen+="$n "
    done <<<"$rows"
fi

extra=""
for n in $(mdm_list); do
    [[ "$seen" == *" $n "* ]] && continue
    mdm_load "$n"
    extra+="$(printf '  %-5s %-5s %-24s %-16s %-6s %s\n' \
        "$n" "${MDM_REAL:-?}" "${MDM_PROXY:-—}" "$(vm_state "${MDM_VMID:-}")" "—" "${MDM_IP:-—}")"$'\n'
done

if [[ -n "$extra" ]]; then
    printf '\n  %sнет в таблице, но есть на хосте:%s\n' "$Y" "$R" >&2
    printf '%s' "$extra" >&2
fi
echo >&2
