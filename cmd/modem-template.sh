#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════════════════════════
#  pcs modem-template — шаблон ВМ виртуального модема.
#
#    pcs modem-template [--id 9000] [--storage local-lvm] [--bridge vmbr0]
#                       [--ram 256] [--cores 1] [--disk 4]
#                       [--singbox 1.10.0] [--ip 10.0.0.60/24 --gw 10.0.0.1]
#                       [--replace] [--yes] [--print-user-data]
#
#  Собирается один раз: Debian 13 из облачного образа, пакеты (usbip,
#  dnsmasq, python3, iptables), модули ядра для USB-устройства, sing-box и
#  наши утилиты vmodem, vmodem-api, vmodem-setup. В конце ВМ выключается и
#  превращается в шаблон Proxmox — модемы будут его связанными клонами.
#
#  Пароль root спрашивается или берётся из PCS_MODEM_PASSWORD: он один на
#  все модемы и лежит в /etc/pcs/modem/template.conf с правами 600.
#
#  --print-user-data печатает cloud-init, который уедет в ВМ, и выходит:
#  посмотреть глазами или собрать ВМ руками, без Proxmox и без root.
# ═══════════════════════════════════════════════════════════════════════════
set -uo pipefail
# shellcheck source=../lib/common.sh
source "$(dirname "$(readlink -f "$0")")/../lib/common.sh"
source "$PCS_ROOT/lib/state.sh"
source "$PCS_ROOT/lib/ssh.sh"
source "$PCS_ROOT/lib/pve.sh"
source "$PCS_ROOT/lib/modem.sh"

usage() { pcs_usage "$0"; }

O_ID=""; O_STORAGE=""; O_BRIDGE=""; O_RAM=""; O_CORES=""; O_DISK=""
O_SINGBOX=""; O_CIDR=""; O_GW=""; O_REPLACE=""; O_PRINT=""
while (( $# )); do
    case "$1" in
        --id)      O_ID="$2"; shift 2 ;;
        --storage) O_STORAGE="$2"; shift 2 ;;
        --bridge)  O_BRIDGE="$2"; shift 2 ;;
        --ram)     O_RAM="$2"; shift 2 ;;
        --cores)   O_CORES="$2"; shift 2 ;;
        --disk)    O_DISK="$2"; shift 2 ;;
        --singbox) O_SINGBOX="$2"; shift 2 ;;
        --ip)      O_CIDR="$2"; shift 2 ;;
        --gw)      O_GW="$2"; shift 2 ;;
        --replace) O_REPLACE=1; shift ;;
        --yes|-y)  export PCS_YES=1; shift ;;
        --print-user-data) O_PRINT=1; shift ;;
        -h|--help) usage; exit 0 ;;
        *) die "неизвестный аргумент: $1 (pcs modem-template --help)" ;;
    esac
done

if [[ -z "$O_PRINT" ]]; then
    pcs_begin modem-template
    need_pve
    pcs_tmpdir
    ssh_setup
    host_net_defaults
fi
tpl_load

if [[ -n "$O_PRINT" ]]; then
    # Показать cloud-init и выйти: ни Proxmox, ни root не нужны.
    TPL_ID="${O_ID:-${TPL_ID:-9000}}"
    TPL_RAM="${O_RAM:-$TPL_RAM}"; TPL_CORES="${O_CORES:-$TPL_CORES}"
    TPL_DISK="${O_DISK:-$TPL_DISK}"; TPL_STORAGE="${O_STORAGE:-$TPL_STORAGE}"
    TPL_BRIDGE="${O_BRIDGE:-$TPL_BRIDGE}"; TPL_SINGBOX="${O_SINGBOX:-$TPL_SINGBOX}"
    TPL_PASSWORD="${PCS_MODEM_PASSWORD:-${TPL_PASSWORD:-}}"
    [[ -n "$TPL_PASSWORD" ]] || die "задай пароль: PCS_MODEM_PASSWORD=... pcs modem-template --print-user-data"
fi

if [[ -z "$O_PRINT" ]]; then
hdr "Шаблон ВМ виртуального модема"

# Прежние значения из шаблона — как умолчания при пересборке.
ask O_ID      "VM ID шаблона" "${TPL_ID:-9000}"
[[ "$O_ID" =~ ^[0-9]+$ ]] || die "VM ID должен быть числом"
ask O_RAM     "RAM, МБ"   "${TPL_RAM:-256}"
ask O_CORES   "CPU, ядер" "${TPL_CORES:-1}"
ask O_DISK    "Диск, ГБ"  "${TPL_DISK:-4}"
info "Хранилища: $(pve_storages | paste -sd' ')"
ask O_STORAGE "Хранилище" "${TPL_STORAGE:-local-lvm}"
info "Мосты: $(pve_bridges | paste -sd' ')"
ask O_BRIDGE  "Мост (та же сеть, где прокси и ВМ mobileproxy.space)" "${TPL_BRIDGE:-vmbr0}"
ask O_SINGBOX "Версия sing-box" "${TPL_SINGBOX:-1.10.0}"

TPL_PASSWORD="${PCS_MODEM_PASSWORD:-${TPL_PASSWORD:-}}"
if [[ -z "$TPL_PASSWORD" ]]; then
    ask_secret TPL_PASSWORD "Root-пароль ВМ модемов (один на все)" confirm
else
    info "Пароль root взят из шаблона/окружения"
fi

if vm_exists "$O_ID"; then
    existing="$(qm config "$O_ID" 2>/dev/null | awk '/^name:/{print $2}')"
    [[ -n "$O_REPLACE" ]] || die "ВМ ${O_ID} (${existing:-?}) уже есть. Пересобрать шаблон: --replace"
    confirm "Удалить ВМ/шаблон ${O_ID} (${existing:-?}) и собрать заново?" no || die "отменено"
    step "Удаляю ${O_ID}..."
    vm_destroy "$O_ID" || die "не удалось удалить ${O_ID}"
    ok "Старый шаблон удалён"
fi

TPL_ID="$O_ID"; TPL_STORAGE="$O_STORAGE"; TPL_BRIDGE="$O_BRIDGE"
TPL_RAM="$O_RAM"; TPL_CORES="$O_CORES"; TPL_DISK="$O_DISK"; TPL_SINGBOX="$O_SINGBOX"
TPL_NAME="vmodem-template"
info "Лог: ${PCS_LOG}"
fi

# ── cloud-init ─────────────────────────────────────────────────────────────
yaml_sq() { printf "'%s'" "${1//\'/\'\'}"; }

password_entry() {
    local h
    if h="$(printf '%s' "$TPL_PASSWORD" | openssl passwd -6 -stdin 2>/dev/null)" && [[ -n "$h" ]]; then
        printf '      password: %s\n      type: hash\n' "$(yaml_sq "$h")"
    else
        warn "openssl не дал хэш — пароль уйдёт в cloud-init текстом"
        printf '      password: %s\n      type: text\n' "$(yaml_sq "$TPL_PASSWORD")"
    fi
}

guest_b64() { b64_file "$PCS_ROOT/guest/$1"; }

build_user_data() {                # build_user_data <файл>
    {
        cat <<YAML
#cloud-config
# сгенерировано pcs ${PCS_VERSION} — шаблон виртуального модема
hostname: ${TPL_NAME}
manage_etc_hosts: true
preserve_hostname: false
disable_root: false
ssh_pwauth: true

users:
  - name: root
    lock_passwd: false

chpasswd:
  expire: false
  users:
    - name: root
YAML
        password_entry
        cat <<YAML

package_update: true
package_upgrade: false
packages:
  - qemu-guest-agent
  - usbip
  - dnsmasq-base
  - python3
  - iptables
  - curl
  - ca-certificates
  - kmod
  - iproute2

write_files:
  - path: /etc/ssh/sshd_config.d/10-pcs.conf
    permissions: '0644'
    content: |
      PermitRootLogin yes
      PasswordAuthentication yes
      KbdInteractiveAuthentication no
      UseDNS no
  - path: /etc/modules-load.d/vmodem.conf
    permissions: '0644'
    content: |
      # vmodem: из них собирается USB-устройство модема
      configfs
      libcomposite
      usbip-vudc
  - path: /usr/local/sbin/vmodem
    permissions: '0755'
    encoding: b64
    content: $(guest_b64 vmodem)
  - path: /usr/local/sbin/vmodem-api
    permissions: '0755'
    encoding: b64
    content: $(guest_b64 vmodem-api)
  - path: /usr/local/sbin/vmodem-setup
    permissions: '0755'
    encoding: b64
    content: $(guest_b64 vmodem-setup)

runcmd:
  - [ systemctl, enable, --now, qemu-guest-agent ]
  - [ systemctl, disable, --now, unattended-upgrades ]
  - [ systemctl, restart, ssh ]
YAML
    } >"$1"
    [[ -f "$1" ]] && chmod 600 "$1"
    return 0
}

if [[ -n "$O_PRINT" ]]; then
    build_user_data /dev/stdout
    exit 0
fi

fail_hint() {
    bad "$1"
    info "ВМ ${TPL_ID} оставлена как есть: qm terminal ${TPL_ID} (выход Ctrl+O)"
    info "Собрать заново: pcs modem-template --id ${TPL_ID} --replace"
    exit 1
}

# ── Сборка ─────────────────────────────────────────────────────────────────
hdr "Образ Debian 13"
debian_image_ensure || die "без образа дальше нельзя"

hdr "Создание ВМ ${TPL_ID}"
qm create "$TPL_ID" \
    --name "$TPL_NAME" --memory "$TPL_RAM" --cores "$TPL_CORES" \
    --balloon 0 --cpu host --numa 0 \
    --net0 "virtio,bridge=${TPL_BRIDGE}" \
    --ostype l26 --machine q35 --scsihw virtio-scsi-single \
    --agent "enabled=1,fstrim_cloned_disks=1" \
    --serial0 socket --tablet 0 --onboot 0 \
    --description "pcs ${PCS_VERSION}: шаблон виртуального модема" >>"$PCS_LOG" 2>&1 \
    || die "qm create не прошёл (подробности в ${PCS_LOG})"
ok "ВМ создана"

step "Импорт диска в ${TPL_STORAGE}..."
qm_import_disk "$TPL_ID" "$DEBIAN_IMG_PATH" "$TPL_STORAGE" || fail_hint "импорт диска не прошёл"
disk="$(qm config "$TPL_ID" | awk -F': ' '/^unused[0-9]+:/{print $2; exit}')"
[[ -n "$disk" ]] || fail_hint "импортированный диск не найден"
qm set "$TPL_ID" --scsi0 "${disk},discard=on,ssd=1" --boot order=scsi0 >>"$PCS_LOG" 2>&1 \
    || fail_hint "не удалось подключить диск"
qm resize "$TPL_ID" scsi0 "${TPL_DISK}G" >>"$PCS_LOG" 2>&1 || fail_hint "не удалось расширить диск"
ok "Диск ${TPL_DISK} ГБ"

hdr "cloud-init"
snippets_ensure || fail_hint "нет хранилища под сниппеты"
snip="${SNIP_DIR}/pcs-modem-tpl-${TPL_ID}.yaml"
build_user_data "$snip"
ok "user-data: ${snip}"
if ! qm set "$TPL_ID" --ide2 "${TPL_STORAGE}:cloudinit" >>"$PCS_LOG" 2>&1; then
    warn "${TPL_STORAGE} не держит cloudinit-диск — кладу на local"
    qm set "$TPL_ID" --ide2 "local:cloudinit" >>"$PCS_LOG" 2>&1 || fail_hint "cloudinit-диск не создан"
fi
qm set "$TPL_ID" --cicustom "user=${SNIP_STORE}:snippets/$(basename "$snip")" >>"$PCS_LOG" 2>&1
qm set "$TPL_ID" --nameserver "1.1.1.1 8.8.8.8" >>"$PCS_LOG" 2>&1
if [[ -n "$O_CIDR" ]]; then
    [[ "$O_CIDR" == */* ]] || fail_hint "--ip: нужен формат IP/маска"
    ask O_GW "Шлюз" "$DEF_GW"
    qm set "$TPL_ID" --ipconfig0 "ip=${O_CIDR},gw=${O_GW}" >>"$PCS_LOG" 2>&1
    VM_IP="${O_CIDR%%/*}"
    ok "Сеть шаблона: ${O_CIDR}, шлюз ${O_GW}"
else
    qm set "$TPL_ID" --ipconfig0 "ip=dhcp" >>"$PCS_LOG" 2>&1
    VM_IP=""
    ok "Сеть шаблона: DHCP (сборка идёт один раз, адрес неважен)"
fi

hdr "Первая загрузка"
qm start "$TPL_ID" >>"$PCS_LOG" 2>&1 || vm_running "$TPL_ID" || fail_hint "ВМ не запускается"
VM_SSH_PORT=22
VM_PASSWORD="$TPL_PASSWORD"
if [[ -z "$VM_IP" ]]; then
    step "Жду IP от QEMU guest agent..."
    t=0
    while (( t < 300 )); do
        VM_IP="$(qm_agent_ip "$TPL_ID")" && break
        VM_IP=""; sleep 5; t=$((t + 5))
    done
    [[ -n "$VM_IP" ]] || { ask VM_IP "guest agent молчит — IP ВМ (qm terminal ${TPL_ID})"; }
    [[ -n "$VM_IP" ]] || fail_hint "IP ВМ неизвестен"
fi
ok "IP: ${VM_IP}"
wait_ssh 420 || fail_hint "SSH не поднялся за 7 минут (journalctl -u cloud-init)"
step "Жду окончания cloud-init..."
vm_ssh "cloud-init status --wait >/dev/null 2>&1; cloud-init status 2>/dev/null" 2>&1 | relay

hdr "Подготовка модемной начинки"
# Утилиты уже уехали через cloud-init, но кладём свежие: шаблон могли
# пересобирать после правок в guest/.
vm_push_tool vmodem       || fail_hint "не доставлен vmodem"
vm_push_tool vmodem-api   || fail_hint "не доставлен vmodem-api"
vm_push_tool vmodem-setup || fail_hint "не доставлен vmodem-setup"
vm_ssh "vmodem-setup --singbox-version ${TPL_SINGBOX}" 2>&1 | relay
[[ ${PIPESTATUS[0]} -eq 0 ]] || fail_hint "машина не готова быть модемом — см. выше"

hdr "Проверка"
vm_ssh "vmodem-setup --check" 2>&1 | relay
[[ ${PIPESTATUS[0]} -eq 0 ]] || fail_hint "проверка не прошла"

hdr "Превращение в шаблон"
# Следы первой загрузки убираем, иначе клоны получат тот же machine-id
# (а с ним один DHCP-идентификатор на всех) и не перезапустят cloud-init.
vm_ssh "cloud-init clean --logs >/dev/null 2>&1; \
        truncate -s 0 /etc/machine-id; rm -f /var/lib/dbus/machine-id; \
        rm -f /etc/ssh/ssh_host_*; \
        rm -rf /var/lib/vmodem /etc/vmodem /var/log/vmodem; \
        history -c 2>/dev/null; sync" 2>&1 | relay
step "Выключаю ВМ..."
vm_ssh "(sleep 1; systemctl poweroff) >/dev/null 2>&1 &" >/dev/null 2>&1 || true
t=0
while vm_running "$TPL_ID" && (( t < 120 )); do sleep 3; t=$((t + 3)); done
vm_running "$TPL_ID" && { qm stop "$TPL_ID" >>"$PCS_LOG" 2>&1; sleep 3; }
qm template "$TPL_ID" >>"$PCS_LOG" 2>&1 || fail_hint "qm template не прошёл"
ok "ВМ ${TPL_ID} стала шаблоном"

tpl_save
echo >&2
ok "Шаблон готов: ${TPL_ID} (${TPL_NAME}), sing-box ${TPL_SINGBOX}, мост ${TPL_BRIDGE}"
info "Дальше: pcs modem-add --n 64 --real 101 --proxy host:port:login:pass"
