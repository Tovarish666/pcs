#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════════════════════════
#  pcs vm-create — ВМ Ubuntu 24.04 под софт mobileproxy.space.
#
#    pcs vm-create [--id N] [--name mpspace] [--cores 8] [--ram 8192] [--disk 50]
#                  [--storage local-lvm] [--bridge vmbr0]
#                  [--ip 10.0.0.50/24 --gw 10.0.0.1 | --dhcp]
#                  [--dns "1.1.1.1 8.8.8.8"] [--replace] [--yes]
#
#  Чего не передали флагом — спросит. Пароль root только вводом или через
#  PCS_VM_PASSWORD в окружении: в аргументах он виден в `ps`.
#
#  Что получается: cloud image + cloud-init, root по паролю через SSH,
#  qemu-guest-agent, DNS через pcs-fix-dns. Состояние — /etc/pcs/mp/<id>.conf,
#  ВМ становится активной для остальных команд.
# ═══════════════════════════════════════════════════════════════════════════
set -uo pipefail
# shellcheck source=../lib/common.sh
source "$(dirname "$(readlink -f "$0")")/../lib/common.sh"
source "$PCS_ROOT/lib/state.sh"
source "$PCS_ROOT/lib/ssh.sh"
source "$PCS_ROOT/lib/pve.sh"

usage() { pcs_usage "$0"; }

O_ID=""; O_NAME=""; O_CORES=""; O_RAM=""; O_DISK=""; O_STORAGE=""; O_BRIDGE=""
O_NET=""; O_CIDR=""; O_GW=""; O_DNS=""; O_REPLACE=""
while (( $# )); do
    case "$1" in
        --id)      O_ID="$2"; shift 2 ;;
        --name)    O_NAME="$2"; shift 2 ;;
        --cores)   O_CORES="$2"; shift 2 ;;
        --ram)     O_RAM="$2"; shift 2 ;;
        --disk)    O_DISK="$2"; shift 2 ;;
        --storage) O_STORAGE="$2"; shift 2 ;;
        --bridge)  O_BRIDGE="$2"; shift 2 ;;
        --ip)      O_NET="static"; O_CIDR="$2"; shift 2 ;;
        --gw)      O_GW="$2"; shift 2 ;;
        --dhcp)    O_NET="dhcp"; shift ;;
        --dns)     O_DNS="$2"; shift 2 ;;
        --replace) O_REPLACE=1; shift ;;
        --yes|-y)  export PCS_YES=1; shift ;;
        -h|--help) usage; exit 0 ;;
        *) die "неизвестный аргумент: $1 (pcs vm-create --help)" ;;
    esac
done

pcs_begin vm-create "$@"
need_pve
pcs_tmpdir
ssh_setup
host_net_defaults

# ── Параметры ──────────────────────────────────────────────────────────────
hdr "Новая ВМ mobileproxy.space"

ask O_ID "VM ID" "$(pve_nextid)"
[[ "$O_ID" =~ ^[0-9]+$ ]] || die "VM ID должен быть числом"
if vm_exists "$O_ID"; then
    existing="$(qm config "$O_ID" 2>/dev/null | awk '/^name:/{print $2}')"
    [[ -n "$O_REPLACE" ]] || die "ВМ ${O_ID} (${existing:-?}) уже есть. Пересоздать: --replace"
    confirm "Удалить ВМ ${O_ID} (${existing:-?}) со всеми дисками и создать заново?" no \
        || die "отменено"
    step "Удаляю ВМ ${O_ID}..."
    vm_destroy "$O_ID" || die "не удалось удалить ВМ ${O_ID}"
    state_forget "$O_ID"
    ok "Старая ВМ удалена"
fi

ask O_NAME "Имя (латиница, цифры, дефис)" "mpspace"
[[ "$O_NAME" =~ ^[a-zA-Z0-9]([a-zA-Z0-9-]*[a-zA-Z0-9])?$ ]] || die "недопустимое имя: ${O_NAME}"
ask O_CORES "CPU, ядер" "8"
ask O_RAM   "RAM, МБ" "8192"
ask O_DISK  "Диск, ГБ" "50"

info "Хранилища: $(pve_storages | paste -sd' ')"
ask O_STORAGE "Хранилище" "local-lvm"
info "Мосты: $(pve_bridges | paste -sd' ')"
ask O_BRIDGE "Мост" "vmbr0"

if [[ -z "$O_NET" ]]; then
    info "Хост: ${DEF_SRC:-?} на ${DEF_DEV:-?}, шлюз ${DEF_GW:-?}, маска /${DEF_MASK}"
    info "Статика надёжнее: IP известен заранее, выяснять после загрузки нечего"
    ask O_NET "Сеть: static или dhcp" "static"
fi
case "$O_NET" in
    static)
        ask O_CIDR "IP ВМ с маской, например 10.0.0.50/${DEF_MASK}"
        [[ "$O_CIDR" =~ ^[0-9]{1,3}(\.[0-9]{1,3}){3}/[0-9]{1,2}$ ]] || die "нужен формат IP/маска: ${O_CIDR}"
        ask O_GW "Шлюз" "$DEF_GW"
        [[ -n "$O_GW" ]] || die "шлюз не задан"
        ;;
    dhcp) ;;
    *) die "сеть: static или dhcp, а не «${O_NET}»" ;;
esac

ask O_DNS "DNS-серверы ВМ" "1.1.1.1 8.8.8.8"
read -r DNS1 DNS2 _ <<<"$O_DNS"
DNS2="${DNS2:-$DNS1}"

O_PASSWORD="${PCS_VM_PASSWORD:-}"
ask_secret O_PASSWORD "Root-пароль ВМ (им же ходит SSH)" confirm

state_reset
VM_ID="$O_ID"; VM_NAME="$O_NAME"; VM_BRIDGE="$O_BRIDGE"; VM_NET_MODE="$O_NET"
VM_DNS1="$DNS1"; VM_DNS2="$DNS2"; VM_SSH_PORT=22
VM_PASSWORD="$O_PASSWORD"
[[ "$O_NET" == "static" ]] && VM_IP="${O_CIDR%%/*}"
state_save
info "Лог: ${PCS_LOG}"

# ── cloud-init user-data ───────────────────────────────────────────────────
yaml_sq() { printf "'%s'" "${1//\'/\'\'}"; }

# Пароль в сниппет попадает хэшем (SHA-512), а не текстом.
password_entry() {
    local h
    if h="$(printf '%s' "$VM_PASSWORD" | openssl passwd -6 -stdin 2>/dev/null)" && [[ -n "$h" ]]; then
        printf '      password: %s\n      type: hash\n' "$(yaml_sq "$h")"
    else
        warn "openssl не дал хэш — пароль уйдёт в cloud-init текстом"
        printf '      password: %s\n      type: text\n' "$(yaml_sq "$VM_PASSWORD")"
    fi
}

build_user_data() {                # build_user_data <файл>
    local fixdns_b64
    fixdns_b64="$(b64_file "$PCS_ROOT/guest/pcs-fix-dns")"
    {
        cat <<YAML
#cloud-config
# сгенерировано pcs ${PCS_VERSION}
hostname: ${VM_NAME}
fqdn: ${VM_NAME}
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
  - curl
  - wget
  - ca-certificates
  - iproute2
  - iptables
  - psmisc
  - net-tools
  - python3
  - python3-yaml
  - jq
  - mc

write_files:
  - path: /etc/ssh/sshd_config.d/10-pcs.conf
    permissions: '0644'
    content: |
      # 10-, а НЕ 99-: sshd берёт первое найденное значение, а cloud-образ
      # кладёт PasswordAuthentication no в 60-cloudimg-settings.conf.
      PermitRootLogin yes
      PasswordAuthentication yes
      KbdInteractiveAuthentication no
      # без этого логин висит 30с, когда нет обратной зоны
      UseDNS no
  - path: /etc/default/pcs-dns
    permissions: '0644'
    content: |
      PCS_DNS1=${VM_DNS1}
      PCS_DNS2=${VM_DNS2}
  - path: /usr/local/sbin/pcs-fix-dns
    permissions: '0755'
    encoding: b64
    content: ${fixdns_b64}

runcmd:
  - [ systemctl, enable, --now, qemu-guest-agent ]
  - [ /usr/local/sbin/pcs-fix-dns ]
  - [ systemctl, restart, ssh ]
YAML
    } >"$1"
    chmod 600 "$1"
}

# ── Шаги ───────────────────────────────────────────────────────────────────
fail_hint() {
    bad "$1"
    info "ВМ ${VM_ID} оставлена для диагностики: qm terminal ${VM_ID} (выход Ctrl+O)"
    info "Начать заново: pcs vm-create --id ${VM_ID} --replace"
    exit 1
}

hdr "Образ Ubuntu 24.04"
ubuntu_image_ensure || die "без образа дальше нельзя"

hdr "Создание ВМ ${VM_ID}"
qm create "$VM_ID" \
    --name "$VM_NAME" --memory "$O_RAM" --cores "$O_CORES" \
    --balloon 0 --cpu host --numa 0 \
    --net0 "virtio,bridge=${VM_BRIDGE}" \
    --ostype l26 --machine q35 --scsihw virtio-scsi-single \
    --agent "enabled=1,fstrim_cloned_disks=1" \
    --serial0 socket --tablet 0 --onboot 1 \
    --description "pcs ${PCS_VERSION}: mobileproxy.space" >>"$PCS_LOG" 2>&1 \
    || die "qm create не прошёл (подробности в ${PCS_LOG})"
ok "ВМ создана"

step "Импорт диска в ${O_STORAGE}..."
qm_import_disk "$VM_ID" "$UBUNTU_IMG_PATH" "$O_STORAGE" || fail_hint "импорт диска не прошёл"
disk="$(qm config "$VM_ID" | awk -F': ' '/^unused[0-9]+:/{print $2; exit}')"
[[ -n "$disk" ]] || fail_hint "импортированный диск не найден в qm config"
qm set "$VM_ID" --scsi0 "${disk},discard=on,ssd=1,iothread=1" --boot order=scsi0 >>"$PCS_LOG" 2>&1 \
    || fail_hint "не удалось подключить диск"
qm resize "$VM_ID" scsi0 "${O_DISK}G" >>"$PCS_LOG" 2>&1 || fail_hint "не удалось расширить диск"
ok "Диск ${O_DISK} ГБ подключён"

hdr "cloud-init"
snippets_ensure || fail_hint "нет хранилища под сниппеты"
snip="${SNIP_DIR}/pcs-${VM_ID}.yaml"
build_user_data "$snip"
ok "user-data: ${snip}"
# cloudinit-диск: сначала целевое хранилище, иначе local
if ! qm set "$VM_ID" --ide2 "${O_STORAGE}:cloudinit" >>"$PCS_LOG" 2>&1; then
    warn "${O_STORAGE} не держит cloudinit-диск — кладу на local"
    qm set "$VM_ID" --ide2 "local:cloudinit" >>"$PCS_LOG" 2>&1 || fail_hint "cloudinit-диск не создан"
fi
# --cicustom user= подменяет только user-data; network-config Proxmox
# по-прежнему строит из --ipconfig0/--nameserver.
qm set "$VM_ID" --cicustom "user=${SNIP_STORE}:snippets/pcs-${VM_ID}.yaml" >>"$PCS_LOG" 2>&1
qm set "$VM_ID" --nameserver "${VM_DNS1} ${VM_DNS2}" >>"$PCS_LOG" 2>&1
if [[ "$VM_NET_MODE" == "static" ]]; then
    qm set "$VM_ID" --ipconfig0 "ip=${O_CIDR},gw=${O_GW}" >>"$PCS_LOG" 2>&1
    ok "Сеть: ${O_CIDR}, шлюз ${O_GW}"
else
    qm set "$VM_ID" --ipconfig0 "ip=dhcp" >>"$PCS_LOG" 2>&1
    ok "Сеть: DHCP"
fi

hdr "Запуск"
qm start "$VM_ID" >>"$PCS_LOG" 2>&1 || vm_running "$VM_ID" || fail_hint "ВМ не запускается"
ok "ВМ запущена"

if [[ -z "$VM_IP" ]]; then
    VM_IP="$(vm_wait_ip "$VM_ID" "${PCS_WAIT_IP:-900}")" || VM_IP=""
    [[ -n "$VM_IP" ]] || { ask VM_IP "адрес не определился — впиши вручную (qm terminal ${VM_ID})"; }
    [[ -n "$VM_IP" ]] || fail_hint "IP ВМ неизвестен"
    ok "IP: ${VM_IP}"
    state_save
fi

wait_ssh 420 || fail_hint "SSH по паролю не поднялся за 7 минут (journalctl -u cloud-init на ВМ)"
step "Жду окончания cloud-init..."
vm_ssh "cloud-init status --wait >/dev/null 2>&1; cloud-init status 2>/dev/null" 2>&1 | relay
ok "cloud-init отработал"

hdr "Проверка"
vm_sh_check='
echo "hostname  $(hostname)"
echo "resolv    $(readlink -f /etc/resolv.conf)"
echo "pwauth    $(sshd -T 2>/dev/null | grep -m1 ^passwordauthentication)"
echo "agent     $(systemctl is-active qemu-guest-agent 2>/dev/null)"
for h in github.com mobileproxy.space; do
  getent ahostsv4 $h >/dev/null 2>&1 && echo "dns       $h ok" || echo "dns       $h FAIL"
done'
out="$(vm_ssh "$vm_sh_check" 2>&1)"
printf '%s\n' "$out" | relay
grep -q 'FAIL' <<<"$out" && warn "DNS отвечает не полностью — pcs vm-fix-dns"
grep -q 'passwordauthentication yes' <<<"$out" || warn "sshd: вход по паролю выключен (?)"

state_save
state_set_active "$VM_ID"
echo >&2
ok "ВМ готова: ${VM_ID} (${VM_NAME}) @ ${VM_IP} — активная"
info "Дальше: pcs mp-install"
