#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════════════════════════
#  pcs modem-add — сделать виртуальный модем из строки таблицы.
#
#    pcs modem-add --n 201
#    pcs modem-add --n 201 --real 1 --proxy 1.2.3.4:15000:login:pass
#    pcs modem-add --n 201 --id 1201 --ip 10.0.0.101/24 --gw 10.0.0.1
#
#  Без --real и --proxy берёт их из таблицы (pcs modem-source). ВМ — клон
#  шаблона (pcs modem-template), внутри поднимается vmodem: USB-устройство
#  Huawei, DHCP с DNS, туннель в прокси и веб-морда модема.
#
#  Номер ВМ по умолчанию — 1000 + n, адрес — по DHCP (узнаём через guest
#  agent). Для постоянного адреса: --ip и --gw.
#
#    --server-ip IP  чей usbip: адрес ВМ mobileproxy.space (по умолчанию
#                    берётся активная ВМ PCS)
#    --no-up         только создать и настроить, не поднимать
#    --replace       снести ВМ с этим номером и сделать заново
# ═══════════════════════════════════════════════════════════════════════════
set -uo pipefail
# shellcheck source=../lib/common.sh
source "$(dirname "$(readlink -f "$0")")/../lib/common.sh"
source "$PCS_ROOT/lib/state.sh"
source "$PCS_ROOT/lib/ssh.sh"
source "$PCS_ROOT/lib/pve.sh"
source "$PCS_ROOT/lib/modem.sh"
source "$PCS_ROOT/lib/sheet.sh"

usage() { pcs_usage "$0"; }

O_N=""; O_REAL=""; O_PROXY=""; O_ID=""; O_CIDR=""; O_GW=""
O_SERVER_IP=""; O_NO_UP=""; O_REPLACE=""; O_SOURCE=""
while (( $# )); do
    case "$1" in
        --n)         O_N="$2"; shift 2 ;;
        --real)      O_REAL="$2"; shift 2 ;;
        --proxy)     O_PROXY="$2"; shift 2 ;;
        --id)        O_ID="$2"; shift 2 ;;
        --ip)        O_CIDR="$2"; shift 2 ;;
        --gw)        O_GW="$2"; shift 2 ;;
        --server-ip) O_SERVER_IP="$2"; shift 2 ;;
        --source)    O_SOURCE="$2"; shift 2 ;;
        --no-up)     O_NO_UP=1; shift ;;
        --replace)   O_REPLACE=1; shift ;;
        --yes|-y)    export PCS_YES=1; shift ;;
        -h|--help)   usage; exit 0 ;;
        *) die "неизвестный аргумент: $1 (pcs modem-add --help)" ;;
    esac
done

pcs_begin modem-add
need_pve
pcs_tmpdir
ssh_setup
tpl_need
host_net_defaults

ask O_N "Номер модема (n из таблицы)"
[[ "$O_N" =~ ^[0-9]+$ ]] && (( O_N >= 1 && O_N <= 254 )) || die "n: нужен номер 1..254"
[[ "$O_N" =~ ^(153|154|155)$ ]] && die "n=${O_N}: на сервере mp.space это системная таблица маршрутов"

# ── Строка таблицы ─────────────────────────────────────────────────────────
if [[ -z "$O_REAL" || -z "$O_PROXY" ]]; then
    src="$O_SOURCE"
    [[ -n "$src" ]] && src="$(sheet_url_normalize "$src")" || { sheet_url_need; src="$SHEET_URL"; }
    step "Ищу модем ${O_N} в таблице..."
    row="$(sheet_rows "$src" | awk -F'\t' -v n="$O_N" '$1 == n {print; exit}')"
    [[ -n "$row" ]] || die "модема ${O_N} нет в таблице (pcs modem-list)"
    IFS=$'\t' read -r _n s_real s_host s_port s_login s_pass s_enabled <<<"$row"
    [[ -n "$O_REAL" ]]  || O_REAL="$s_real"
    [[ -n "$O_PROXY" ]] || O_PROXY="${s_host}:${s_port}:${s_login}:${s_pass}"
    [[ "$s_enabled" == "1" ]] || warn "в таблице модем ${O_N} выключен (enabled=0) — всё равно создаю"
    ok "из таблицы: real=${O_REAL}, прокси ${s_host}:${s_port}"
fi
[[ "$O_REAL" =~ ^[0-9]+$ ]] && (( O_REAL >= 1 && O_REAL <= 254 )) || die "real: нужен октет 1..254"
[[ "$O_PROXY" == *:*:*:* ]] || die "прокси: нужен host:port:login:pass"

# ── Адрес сервера mobileproxy.space ────────────────────────────────────────
if [[ -z "$O_SERVER_IP" ]]; then
    active="$(state_active_id)"
    if [[ -n "$active" ]]; then
        state_load "$active"
        O_SERVER_IP="${VM_IP:-}"
        [[ -n "$O_SERVER_IP" ]] && info "USB заберёт ВМ ${active} (${O_SERVER_IP})"
    fi
fi
[[ -n "$O_SERVER_IP" ]] || warn "адрес ВМ mobileproxy.space неизвестен — USB сможет забрать кто угодно (--server-ip)"

# ── ВМ ─────────────────────────────────────────────────────────────────────
mdm_load "$O_N"
VMID="${O_ID:-${MDM_VMID:-$(( ${TPL_VMID_BASE:-1000} + O_N ))}}"
NAME="vmodem-${O_N}"

if vm_exists "$VMID"; then
    [[ -n "$O_REPLACE" ]] || die "ВМ ${VMID} уже есть. Пересоздать: --replace"
    confirm "Удалить ВМ ${VMID} и сделать модем ${O_N} заново?" no || die "отменено"
    step "Удаляю ВМ ${VMID}..."
    vm_destroy "$VMID" || die "не удалось удалить ${VMID}"
fi

hdr "Модем ${O_N}: клон шаблона ${TPL_ID} → ВМ ${VMID}"
if ! qm clone "$TPL_ID" "$VMID" --name "$NAME" --full 0 >>"$PCS_LOG" 2>&1; then
    warn "связанный клон не вышел (хранилище не умеет) — делаю полный"
    qm clone "$TPL_ID" "$VMID" --name "$NAME" --full 1 >>"$PCS_LOG" 2>&1 \
        || die "qm clone не прошёл (подробности в ${PCS_LOG})"
fi
ok "ВМ ${VMID} создана"

snippets_ensure || die "нет хранилища под сниппеты"
snip="${SNIP_DIR}/pcs-modem-${O_N}.yaml"
modem_clone_user_data "$snip" "$NAME" "$TPL_PASSWORD"
qm set "$VMID" --cicustom "user=${SNIP_STORE}:snippets/$(basename "$snip")" >>"$PCS_LOG" 2>&1
qm set "$VMID" --onboot 1 >>"$PCS_LOG" 2>&1
if [[ -n "$O_CIDR" ]]; then
    [[ "$O_CIDR" == */* ]] || die "--ip: нужен формат IP/маска"
    ask O_GW "Шлюз" "$DEF_GW"
    qm set "$VMID" --ipconfig0 "ip=${O_CIDR},gw=${O_GW}" >>"$PCS_LOG" 2>&1
    MDM_IP="${O_CIDR%%/*}"
    ok "Сеть модема: ${O_CIDR}, шлюз ${O_GW}"
else
    qm set "$VMID" --ipconfig0 "ip=dhcp" >>"$PCS_LOG" 2>&1
    MDM_IP=""
    ok "Сеть модема: DHCP"
fi

step "Запускаю ВМ..."
qm start "$VMID" >>"$PCS_LOG" 2>&1 || vm_running "$VMID" || die "ВМ ${VMID} не запускается"

VM_SSH_PORT=22
VM_PASSWORD="$TPL_PASSWORD"
VM_IP="$MDM_IP"
if [[ -z "$VM_IP" ]]; then
    step "Жду адрес от guest agent..."
    t=0
    while (( t < 300 )); do
        VM_IP="$(qm_agent_ip "$VMID")" && break
        VM_IP=""; sleep 5; t=$((t + 5))
    done
    [[ -n "$VM_IP" ]] || die "адрес ВМ ${VMID} неизвестен (qm terminal ${VMID})"
fi
ok "Адрес модема: ${VM_IP}"
wait_ssh 300 || die "SSH на ${VM_IP} не поднялся (qm terminal ${VMID})"

# ── Настройка модема ───────────────────────────────────────────────────────
hdr "Настройка"
# Утилиты берём свежие из репозитория: шаблон мог собираться давно.
vm_push_tool vmodem       || die "не доставлен vmodem"
vm_push_tool vmodem-api   || die "не доставлен vmodem-api"
vm_push_tool vmodem-setup || die "не доставлен vmodem-setup"

# Параметры уходят на stdin: пароль прокси не светится в ps на ВМ и в логах.
{
    printf 'N=%s\nREAL=%s\nPROXY=%s\n' "$O_N" "$O_REAL" "$O_PROXY"
    [[ -n "$O_SERVER_IP" ]] && printf 'USBIP_ALLOW=%s\n' "$O_SERVER_IP"
} | vm_ssh_in "vmodem config set -" 2>&1 | relay
[[ ${PIPESTATUS[1]} -eq 0 ]] || die "vmodem config не принял параметры"

if [[ -n "$O_NO_UP" ]]; then
    warn "поднимать не прошу (--no-up): на ВМ это делается командой vmodem up"
else
    hdr "Подъём"
    vm_ssh "vmodem up" 2>&1 | relay
    [[ ${PIPESTATUS[0]} -eq 0 ]] || warn "vmodem up закончился с ошибкой — смотри выше и vmodem status"
    vm_ssh "vmodem install-service" 2>&1 | relay
fi

MDM_N="$O_N"; MDM_REAL="$O_REAL"; MDM_VMID="$VMID"; MDM_IP="$VM_IP"; MDM_NAME="$NAME"
MDM_PROXY="${O_PROXY%%:*}:$(cut -d: -f2 <<<"$O_PROXY")"
MDM_PROXY_USER="$(cut -d: -f3 <<<"$O_PROXY")"
MDM_PROXY_PASS="$(cut -d: -f4- <<<"$O_PROXY")"
mdm_save

echo >&2
ok "Модем ${O_N} готов: ВМ ${VMID} @ ${VM_IP}, для сервера это 192.168.${O_N}.100"
info "Подключить на ВМ mobileproxy.space: pcs modem-attach --n ${O_N}"
info "Что внутри: ssh root@${VM_IP} vmodem status"
