#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════════════════════════
#  pcs modem-update — обновить утилиты на ВМ модемов.
#
#    pcs modem-update --all
#    pcs modem-update --n 112
#    pcs modem-update --all --restart
#
#  Кладёт на ВМ модемов свежие vmodem, vmodem-api и vmodem-setup из этой
#  версии PCS. Шаблон при этом не трогается: новые модемы всё равно получают
#  утилиты заново при создании, а вот созданные раньше остаются со старыми.
#
#  --restart — ещё и поднять модем заново (vmodem up): связь с сервером
#  прервётся на несколько секунд, зато починки применяются сразу.
# ═══════════════════════════════════════════════════════════════════════════
set -uo pipefail
# shellcheck source=../lib/common.sh
source "$(dirname "$(readlink -f "$0")")/../lib/common.sh"
source "$PCS_ROOT/lib/state.sh"
source "$PCS_ROOT/lib/ssh.sh"
source "$PCS_ROOT/lib/pve.sh"
source "$PCS_ROOT/lib/modem.sh"

usage() { pcs_usage "$0"; }

O_N=""; O_ALL=""; O_RESTART=""
while (( $# )); do
    case "$1" in
        --n)       O_N="${O_N:+${O_N},}$2"; shift 2 ;;
        --all)     O_ALL=1; shift ;;
        --restart) O_RESTART=1; shift ;;
        --yes|-y)  export PCS_YES=1; shift ;;
        -h|--help) usage; exit 0 ;;
        *) die "неизвестный аргумент: $1 (pcs modem-update --help)" ;;
    esac
done

pcs_begin modem-update
pcs_tmpdir
ssh_setup
tpl_load
[[ -n "${TPL_PASSWORD:-}" ]] || die "пароля ВМ модемов нет: /etc/pcs/modem/template.conf (pcs modem-template)"

targets=()
if [[ -n "$O_ALL" ]]; then
    while read -r n; do [[ -n "$n" ]] && targets+=("$n"); done < <(mdm_list)
    (( ${#targets[@]} )) || die "созданных модемов нет (pcs modem-add)"
elif [[ -n "$O_N" ]]; then
    for n in ${O_N//,/ }; do
        [[ "$n" =~ ^[0-9]+$ ]] || die "номер модема: ${n}"
        targets+=("$n")
    done
else
    die "кого обновляем: --n <номер> или --all"
fi

problems=0
for n in "${targets[@]}"; do
    mdm_load "$n"
    hdr "Модем ${n}"
    if [[ -z "${MDM_IP:-}" ]]; then
        bad "адрес ВМ модема ${n} неизвестен (pcs modem-add --n ${n})"; problems=$((problems + 1)); continue
    fi
    VM_IP="$MDM_IP"; VM_SSH_PORT=22; VM_PASSWORD="$TPL_PASSWORD"
    if ! vm_alive; then
        bad "ВМ модема ${n} (${MDM_IP}) не отвечает по SSH"; problems=$((problems + 1)); continue
    fi
    okall=1
    for t in vmodem vmodem-api vmodem-setup; do
        vm_push_tool "$t" >/dev/null || { bad "не доставлен ${t}"; okall=0; }
    done
    (( okall )) || { problems=$((problems + 1)); continue; }
    ok "утилиты обновлены (PCS ${PCS_VERSION})"
    if [[ -n "$O_RESTART" ]]; then
        vm_ssh "vmodem up" 2>&1 | relay
        [[ ${PIPESTATUS[0]} -eq 0 ]] || { bad "модем ${n} не поднялся заново"; problems=$((problems + 1)); }
    fi
done

echo >&2
if (( problems == 0 )); then
    ok "обновлено модемов: ${#targets[@]}"
    [[ -n "$O_RESTART" ]] || info "чтобы починки применились: pcs modem-update --all --restart"
else
    bad "проблем: ${problems} — см. выше"
    exit 1
fi
