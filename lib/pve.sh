# shellcheck shell=bash
# ═══════════════════════════════════════════════════════════════════════════
#  lib/pve.sh — всё, что касается хоста Proxmox: образы, хранилища, qm.
# ═══════════════════════════════════════════════════════════════════════════
[[ -n "${_PCS_PVE_LOADED:-}" ]] && return 0
_PCS_PVE_LOADED=1

# Ubuntu 24.04 — под сервер mobileproxy.space
UBUNTU_IMG_URL="${UBUNTU_IMG_URL:-https://cloud-images.ubuntu.com/noble/current/noble-server-cloudimg-amd64.img}"
UBUNTU_SUMS_URL="${UBUNTU_SUMS_URL:-https://cloud-images.ubuntu.com/noble/current/SHA256SUMS}"
UBUNTU_IMG_PATH="${UBUNTU_IMG_PATH:-/var/lib/vz/template/iso/ubuntu-24.04-noble.img}"

# Debian 13 — под виртуальные модемы: меньше, и в ядре есть usbip-vudc
DEBIAN_IMG_URL="${DEBIAN_IMG_URL:-https://cloud.debian.org/images/cloud/trixie/latest/debian-13-genericcloud-amd64.qcow2}"
DEBIAN_SUMS_URL="${DEBIAN_SUMS_URL:-https://cloud.debian.org/images/cloud/trixie/latest/SHA512SUMS}"
DEBIAN_IMG_PATH="${DEBIAN_IMG_PATH:-/var/lib/vz/template/iso/debian-13-genericcloud-amd64.qcow2}"

SNIP_STORE="${SNIP_STORE:-local}"
SNIP_DIR="${SNIP_DIR:-/var/lib/vz/snippets}"

# Сеть хоста: шлюз, интерфейс, адрес, маска — подсказки для статики ВМ.
host_net_defaults() {
    local line
    line="$(ip -4 route get 1.1.1.1 2>/dev/null | head -1)"
    DEF_GW="$(sed -n 's/.* via \([0-9.]\+\).*/\1/p' <<<"$line")"
    DEF_DEV="$(sed -n 's/.* dev \([^ ]\+\).*/\1/p' <<<"$line")"
    DEF_SRC="$(sed -n 's/.* src \([0-9.]\+\).*/\1/p' <<<"$line")"
    DEF_MASK=""
    [[ -n "$DEF_DEV" ]] && DEF_MASK="$(ip -4 -o addr show dev "$DEF_DEV" 2>/dev/null | awk 'NR==1{split($4,a,"/"); print a[2]}')"
    : "${DEF_GW:=}" "${DEF_DEV:=}" "${DEF_SRC:=}" "${DEF_MASK:=24}"
}

pve_storages() { pvesm status 2>/dev/null | awk 'NR>1{print $1}'; }
pve_bridges()  { ip -o link show 2>/dev/null | awk -F': ' '$2 ~ /^vmbr/{sub(/@.*/,"",$2); print $2}'; }
pve_nextid()   { pvesh get /cluster/nextid 2>/dev/null || echo 200; }

vm_exists()  { qm status "$1" >/dev/null 2>&1; }
vm_running() { qm status "$1" 2>/dev/null | grep -q running; }

vm_destroy() {                     # vm_destroy <vmid>
    local id="$1" w=0
    qm stop "$id" --skiplock >/dev/null 2>&1 || true
    while vm_running "$id" && (( w < 30 )); do sleep 2; w=$((w + 2)); done
    qm destroy "$id" --destroy-unreferenced-disks 1 --purge 1 >>"$PCS_LOG" 2>&1
}

# Облачный образ с проверкой контрольной суммы. Уже скачанный и совпавший
# повторно не качается; upstream обновился — перекачиваем.
#   cloud_image_ensure <url> <url списка сумм> <куда> <sha256sum|sha512sum> <описание>
cloud_image_ensure() {
    local url="$1" sums_url="$2" path="$3" sumcmd="${4:-sha256sum}" what="${5:-образ}"
    local name expected actual
    name="${url##*/}"
    expected="$(wget -qO- --timeout=30 "$sums_url" \
                | awk -v n="$name" '$2 == n || $2 == "*" n {print $1; exit}')"
    [[ -n "$expected" ]] || { bad "не получить список сумм: ${sums_url} (DNS/интернет на хосте?)"; return 1; }
    if [[ -f "$path" ]]; then
        actual="$($sumcmd "$path" | awk '{print $1}')"
        [[ "$actual" == "$expected" ]] && { ok "${what}: на месте, сумма совпала"; return 0; }
        warn "${what}: сумма не совпала (upstream обновился) — качаю заново"
        rm -f "$path"
    fi
    mkdir -p "$(dirname "$path")"
    step "Качаю ${what}..."
    wget -q --show-progress --timeout=60 -O "${path}.part" "$url" \
        || { rm -f "${path}.part"; bad "${what}: не скачался"; return 1; }
    actual="$($sumcmd "${path}.part" | awk '{print $1}')"
    [[ "$actual" == "$expected" ]] || { rm -f "${path}.part"; bad "${what}: сумма не совпала"; return 1; }
    mv -f "${path}.part" "$path"
    ok "${what}: скачан и проверен"
}

ubuntu_image_ensure() {
    cloud_image_ensure "$UBUNTU_IMG_URL" "$UBUNTU_SUMS_URL" "$UBUNTU_IMG_PATH" \
        sha256sum "образ Ubuntu 24.04 (~600 МБ)"
}

debian_image_ensure() {
    cloud_image_ensure "$DEBIAN_IMG_URL" "$DEBIAN_SUMS_URL" "$DEBIAN_IMG_PATH" \
        sha512sum "образ Debian 13 (~350 МБ)"
}

# Хранилище local должно уметь snippets — туда кладётся user-data cloud-init.
snippets_ensure() {
    if pvesm status --content snippets 2>/dev/null | awk 'NR>1{print $1}' | grep -qx "$SNIP_STORE"; then
        mkdir -p "$SNIP_DIR"; return 0
    fi
    local cur
    cur="$(awk -v s="$SNIP_STORE" '$0=="dir: "s{f=1;next} /^[a-z]+: /{f=0} f && /^[[:space:]]*content /{print $2; exit}' \
           /etc/pve/storage.cfg 2>/dev/null)"
    [[ -n "$cur" ]] || cur="iso,vztmpl,backup"
    step "Включаю content=snippets на хранилище ${SNIP_STORE}"
    pvesm set "$SNIP_STORE" --content "${cur},snippets" >>"$PCS_LOG" 2>&1 \
        || { bad "не удалось включить snippets на ${SNIP_STORE}"; return 1; }
    mkdir -p "$SNIP_DIR"
}

# Без --format: на local-lvm qcow2 не бывает, формат выбирает Proxmox.
qm_import_disk() {                 # qm_import_disk <vmid> <образ> <хранилище>
    qm importdisk "$1" "$2" "$3" >>"$PCS_LOG" 2>&1 && return 0
    qm disk import "$1" "$2" "$3" >>"$PCS_LOG" 2>&1
}

# IPv4 гостя через QEMU guest agent (для DHCP).
qm_agent_ip() {                    # qm_agent_ip <vmid> → IP в stdout
    local out ip
    out="$(qm guest cmd "$1" network-get-interfaces 2>/dev/null)" || return 1
    ip="$(printf '%s' "$out" \
          | grep -oE '"ip-address"[[:space:]]*:[[:space:]]*"[0-9]{1,3}(\.[0-9]{1,3}){3}"' \
          | grep -oE '[0-9]{1,3}(\.[0-9]{1,3}){3}' \
          | grep -vE '^(127\.|169\.254\.)' | head -1)"
    [[ -n "$ip" ]] || return 1
    printf '%s' "$ip"
}
