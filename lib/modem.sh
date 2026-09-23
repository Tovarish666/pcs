# shellcheck shell=bash
# ═══════════════════════════════════════════════════════════════════════════
#  lib/modem.sh — что PCS знает о виртуальных модемах.
#
#  /etc/pcs/modem/template.conf        шаблон ВМ модема (пароль — права 600)
#  /etc/pcs/modem/<ВМ>/source.conf     ссылка на таблицу этой ВМ
#  /etc/pcs/modem/<ВМ>/<N>.conf        один модем: номер, прокси, VM ID, IP
#
#  Модемы принадлежат конкретной ВМ mobileproxy.space, и её номер задаёт
#  номера их ВМ: сервер 3000 → модемы 3001, 3002, 3003… Шаблон один на хост.
# ═══════════════════════════════════════════════════════════════════════════
[[ -n "${_PCS_MODEM_LOADED:-}" ]] && return 0
_PCS_MODEM_LOADED=1

PCS_MODEM_DIR="${PCS_ETC}/modem"
TPL_FILE="${PCS_MODEM_DIR}/template.conf"

# Каталог состояния модемов. Пока сервер неизвестен — общий, дальше его
# подменяет mdm_server_set. Отсюда же берётся ссылка на таблицу (lib/sheet.sh).
PCS_MODEM_STATE_DIR="$PCS_MODEM_DIR"
PCS_SERVER_ID=""

TPL_KEYS=(TPL_ID TPL_NAME TPL_PASSWORD TPL_STORAGE TPL_BRIDGE TPL_RAM TPL_BUILD_RAM TPL_CORES TPL_DISK TPL_SINGBOX)
MDM_KEYS=(MDM_N MDM_REAL MDM_VMID MDM_IP MDM_PROXY MDM_PROXY_USER MDM_PROXY_PASS MDM_NAME)

tpl_reset() {
    TPL_ID=""; TPL_NAME="vmodem-template"; TPL_PASSWORD=""
    TPL_STORAGE="local-lvm"; TPL_BRIDGE="vmbr0"
    TPL_RAM="384"; TPL_CORES="1"; TPL_DISK="4"; TPL_SINGBOX="1.10.0"
    # На сборке apt ставит десяток пакетов и в 256 МБ задыхается: строим
    # с запасом, а перед превращением в шаблон память ужимаем до рабочей.
    TPL_BUILD_RAM="1024"
}

# ── Чьи модемы ─────────────────────────────────────────────────────────────
mdm_server_set() {                 # mdm_server_set <vmid ВМ mobileproxy.space>
    PCS_SERVER_ID="$1"
    PCS_MODEM_STATE_DIR="${PCS_MODEM_DIR}/${1}"
    mdm_migrate_flat
}

# Номера ВМ модемов до версии 3.0 лежали в общем каталоге, а считались от
# фиксированной тысячи. Раскладываем их по серверам — номера ВМ у модемов
# с сервера 1000 при этом те же самые.
mdm_migrate_flat() {
    local f n moved=0
    for f in "$PCS_MODEM_DIR"/[0-9]*.conf; do
        [[ -f "$f" ]] || continue
        n="${f##*/}"; n="${n%.conf}"
        install -d -m 700 "$PCS_MODEM_STATE_DIR"
        mv -f "$f" "${PCS_MODEM_STATE_DIR}/${n}.conf" && moved=$((moved + 1))
    done
    if [[ -f "${PCS_MODEM_DIR}/source.conf" ]]; then
        install -d -m 700 "$PCS_MODEM_STATE_DIR"
        mv -f "${PCS_MODEM_DIR}/source.conf" "${PCS_MODEM_STATE_DIR}/source.conf"
        moved=$((moved + 1))
    fi
    (( moved )) && info "состояние модемов переехало в ${PCS_MODEM_STATE_DIR} (теперь оно на каждую ВМ своё)"
    return 0
}

# Чьи модемы: явный --vm или активная ВМ. Без сервера модемов не бывает.
# Нужен lib/state.sh.
mdm_server_need() {                # mdm_server_need [vmid]
    local id="${1:-}"
    [[ -n "$id" ]] || id="$(state_active_id)"
    [[ -n "$id" ]] || die "ВМ mobileproxy.space не выбрана: pcs vm-use <vmid> или --vm <vmid>"
    [[ "$id" =~ ^[0-9]+$ ]] || die "номер ВМ должен быть числом: ${id}"
    (( id % 1000 == 0 )) || warn "у ВМ ${id} номер не кратен 1000 — модемы получат ${id}+n, следи за пересечениями"
    mdm_server_set "$id"
}

# Номер ВМ модема: сервер 3000 → модем n=2 живёт на ВМ 3002.
mdm_vmid() {                       # mdm_vmid <N>
    [[ -n "$PCS_SERVER_ID" ]] || return 1
    printf '%s' "$(( PCS_SERVER_ID + $1 ))"
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

mdm_file() { printf '%s/%s.conf' "$PCS_MODEM_STATE_DIR" "$1"; }

mdm_load() {                       # mdm_load <N>
    mdm_reset
    # shellcheck disable=SC1090
    [[ -f "$(mdm_file "$1")" ]] && source "$(mdm_file "$1")"
    MDM_N="$1"
    return 0
}

mdm_save() {
    [[ -n "${MDM_N:-}" ]] || die "mdm_save: номер модема пуст"
    install -d -m 700 "$PCS_MODEM_STATE_DIR"
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
    for f in "$PCS_MODEM_STATE_DIR"/*.conf; do
        [[ -f "$f" ]] || continue
        f="${f##*/}"; f="${f%.conf}"
        # рядом лежат служебные файлы, а не модемы
        [[ "$f" =~ ^[0-9]+$ ]] || continue
        printf '%s\n' "$f"
    done
}

mdm_forget() { rm -f "$(mdm_file "$1")"; }

# ── cloud-init для клона ───────────────────────────────────────────────────
# Шаблон уже несёт пакеты и утилиты, клону нужно немного: своё имя и пароль.
# Ключи SSH cloud-init пересоздаст сам — в шаблоне они стёрты.
modem_yaml_sq() { printf "'%s'" "${1//\'/\'\'}"; }

modem_password_entry() {           # modem_password_entry <пароль>
    local h
    if h="$(printf '%s' "$1" | openssl passwd -6 -stdin 2>/dev/null)" && [[ -n "$h" ]]; then
        printf '      password: %s\n      type: hash\n' "$(modem_yaml_sq "$h")"
    else
        warn "openssl не дал хэш — пароль уйдёт в cloud-init текстом"
        printf '      password: %s\n      type: text\n' "$(modem_yaml_sq "$1")"
    fi
}

modem_clone_user_data() {          # modem_clone_user_data <файл> <имя> <пароль>
    {
        cat <<YAML
#cloud-config
# сгенерировано pcs ${PCS_VERSION} — виртуальный модем
hostname: ${2}
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
        modem_password_entry "$3"
        cat <<'YAML'

package_update: false
package_upgrade: false

runcmd:
  - [ systemctl, enable, --now, qemu-guest-agent ]
  - [ systemctl, restart, ssh ]
YAML
    } >"$1"
    [[ -f "$1" ]] && chmod 600 "$1"
    return 0
}
