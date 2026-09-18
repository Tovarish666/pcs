#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════════════════════════
#  pcs mp-auth — ключ mobileproxy.space (auth.mp) на ВМ.
#
#    pcs mp-auth [--vm ID] show [--full]
#    pcs mp-auth [--vm ID] set ['<json>' | --file F | --key K [--port P]]
#    pcs mp-auth [--vm ID] check
#
#  set без значения — спросит. JSON берётся из ЛК: Мой прокси-бизнес →
#  Сервера → иконка ↓ у сервера. Формат: {"auth":"KEY:KEY","port":1800}
#
#  Работу делает утилита mp-auth на самой ВМ (guest/mp-auth); здесь она
#  доставляется свежей и вызывается по SSH. Значение идёт через stdin —
#  никаких кавычек в удалённой командной строке.
# ═══════════════════════════════════════════════════════════════════════════
set -uo pipefail
# shellcheck source=../lib/common.sh
source "$(dirname "$(readlink -f "$0")")/../lib/common.sh"
source "$PCS_ROOT/lib/state.sh"
source "$PCS_ROOT/lib/ssh.sh"

usage() { pcs_usage "$0"; }

O_VM=""; ACTION=""; ARGS=()
while (( $# )); do
    case "$1" in
        --vm) O_VM="$2"; shift 2 ;;
        -h|--help) usage; exit 0 ;;
        show|set|check) [[ -z "$ACTION" ]] && ACTION="$1" || ARGS+=("$1"); shift ;;
        *) ARGS+=("$1"); shift ;;
    esac
done
[[ -n "$ACTION" ]] || ACTION="show"

pcs_begin mp-auth
pcs_tmpdir
ssh_setup
state_need_vm "$O_VM"
vm_alive || die "ВМ ${VM_ID} (${VM_IP}) недоступна по SSH"
vm_push_tool mp-auth || die "не удалось доставить mp-auth на ВМ"

case "$ACTION" in
    show)
        full=""
        [[ " ${ARGS[*]:-} " == *" --full "* ]] && full="--full"
        vm_ssh "mp-auth show ${full}" 2>&1 | relay
        ;;
    check)
        vm_ssh "mp-auth check" 2>&1 | relay
        rc=${PIPESTATUS[0]}
        (( rc == 0 )) && ok "auth.mp и nodejs-server в порядке" || bad "есть проблемы — см. выше"
        exit "$rc"
        ;;
    set)
        value=""; key=""; port=""
        set -- "${ARGS[@]+"${ARGS[@]}"}"
        while (( $# )); do
            case "$1" in
                --file) [[ -r "$2" ]] || die "не читается: $2"; value="$(cat "$2")"; shift 2 ;;
                --key)  key="$2"; shift 2 ;;
                --port) port="$2"; shift 2 ;;
                *)      value="$1"; shift ;;
            esac
        done
        [[ -n "$key" ]] && value="$key"
        if [[ -z "$value" ]]; then
            info "ЛК → Мой прокси-бизнес → Сервера → иконка ↓ у нужного сервера"
            info "Формат: {\"auth\":\"KEY:KEY\",\"port\":1800}"
            ask value "Содержимое auth.mp"
        fi
        [[ -n "$value" ]] || die "пусто"
        remote="mp-auth set -"
        if [[ -n "$port" ]]; then
            [[ "$port" =~ ^[0-9]+$ ]] || die "--port: нужно число"
            remote+=" --port ${port}"
        fi
        printf '%s' "$value" | vm_ssh_in "$remote" 2>&1 | relay
        rc=${PIPESTATUS[1]}
        (( rc == 0 )) && ok "auth.mp обновлён на ВМ ${VM_ID}" || { bad "не записан (код ${rc})"; exit "$rc"; }
        ;;
esac
