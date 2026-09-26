#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════════════════════════
#  pcs vm-usb-ports — сколько виртуальных модемов принимает ВМ.
#
#    pcs vm-usb-ports                    показать, сколько влезает
#    pcs vm-usb-ports --modems 80        сделать так, чтобы влезло 80
#    pcs vm-usb-ports --set              пересобрать под 120 (умолчание)
#    pcs vm-usb-ports --ports 15 --controllers 8
#    pcs vm-usb-ports --revert           вернуть стоковый модуль ядра
#
#  Ядро Ubuntu принимает ровно 8 устройств USB/IP: CONFIG_USBIP_VHCI_HC_PORTS=8
#  и CONFIG_USBIP_VHCI_NR_HCS=1 — константы времени сборки, параметра модуля
#  нет. Девятый модем не втыкается совсем.
#
#  Команда пересобирает vhci-hcd из исходников того же ядра с числами, с
#  которыми ядро собирает Debian: 15 портов на 8 контроллеров, 120 модемов.
#  Сборка отдаётся dkms, поэтому переживает перезагрузку и смену ядра.
#
#  Пересборка снимает воткнутые модемы на пару секунд — службы vmodem-attach@
#  втыкают их обратно сами.
# ═══════════════════════════════════════════════════════════════════════════
set -uo pipefail
# shellcheck source=../lib/common.sh
source "$(dirname "$(readlink -f "$0")")/../lib/common.sh"
source "$PCS_ROOT/lib/state.sh"
source "$PCS_ROOT/lib/ssh.sh"

usage() { pcs_usage "$0"; }

O_VM=""; O_MODEMS=""; O_SET=""; O_PORTS=""; O_HCS=""; O_REVERT=""
while (( $# )); do
    case "$1" in
        --vm)          O_VM="$2"; shift 2 ;;
        --modems)      O_MODEMS="$2"; shift 2 ;;
        --ports)       O_PORTS="$2"; O_SET=1; shift 2 ;;
        --controllers) O_HCS="$2"; O_SET=1; shift 2 ;;
        --set)         O_SET=1; shift ;;
        --revert)      O_REVERT=1; shift ;;
        --yes|-y)      export PCS_YES=1; shift ;;
        -h|--help)     usage; exit 0 ;;
        *) die "неизвестный аргумент: $1 (pcs vm-usb-ports --help)" ;;
    esac
done

pcs_begin vm-usb-ports
pcs_tmpdir
ssh_setup
state_need_vm "$O_VM"
vm_alive || die "ВМ ${VM_ID} (${VM_IP}) недоступна по SSH"
vm_push_tool vhci-ports >/dev/null || die "не доставлен vhci-ports"

if [[ -n "$O_REVERT" ]]; then
    hdr "Возвращаю стоковый vhci-hcd на ВМ ${VM_ID}"
    vm_ssh "vhci-ports revert" 2>&1 | relay
    exit "${PIPESTATUS[0]}"
fi

hdr "USB-порты ВМ ${VM_ID} (${VM_NAME:-?})"
vm_ssh "vhci-ports status" 2>&1 | relay

# Нужно ли вообще что-то делать.
if [[ -n "$O_MODEMS" && -z "$O_SET" ]]; then
    [[ "$O_MODEMS" =~ ^[0-9]+$ ]] || die "--modems: нужно число"
    if vm_ssh "vhci-ports need ${O_MODEMS}" 2>&1 | relay; [[ ${PIPESTATUS[0]} -eq 0 ]]; then
        exit 0
    fi
    O_SET=1
fi
[[ -n "$O_SET" ]] || exit 0

echo >&2
warn "vhci-hcd будет пересобран из исходников этого ядра — это несколько минут"
info "модемы на время перезагрузки модуля отвалятся и вернутся сами"
confirm "Пересобрать?" yes || die "отменено"

# Сборка долгая и рвёт USB — пускаем отвязанно от SSH.
cat >"$PCS_TMP/usbports.sh" <<EOF
set -u
vhci-ports set ${O_PORTS:+--ports ${O_PORTS}} ${O_HCS:+--controllers ${O_HCS}}
EOF
vm_run_detached usb-ports "$PCS_TMP/usbports.sh" 1800 \
    || die "пересборка не удалась (лог на ВМ: /root/pcs-run/usb-ports.log)"

hdr "Итог"
vm_ssh "vhci-ports status" 2>&1 | relay
[[ -z "$O_MODEMS" ]] || { vm_ssh "vhci-ports need ${O_MODEMS}" 2>&1 | relay; exit "${PIPESTATUS[0]}"; }
