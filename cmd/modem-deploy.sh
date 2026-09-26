#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════════════════════════
#  pcs modem-deploy — развернуть виртуальные модемы для ВМ mobileproxy.space.
#
#    pcs modem-deploy                        спросит ссылку, если её нет
#    pcs modem-deploy --source <ссылка>      сменить таблицу и развернуть
#    pcs modem-deploy --source <ссылка> --recreate   снести старые и заново
#    pcs modem-deploy --vm 3000              для другой ВМ, не активной
#
#  Один пункт на весь путь, после vm-create:
#    1. ссылка на Google-таблицу с модемами (n, real, proxy) — своя у каждой
#       ВМ, запоминается
#    2. потолок USB на ВМ — чтобы влезли все модемы из таблицы
#    3. шаблон ВМ модема (по умолчанию 9999) — собирается, если его ещё нет
#    4. модемы по таблице — создаются, поднимаются и втыкаются в эту ВМ
#
#  ВМ не обязана быть сервером mobileproxy.space: модемы воткнутся в любую.
#  Просто адреса 192.168.<N>.100 на той стороне выдаёт софт mp.space, и без
#  него интерфейсы поднимать вам.
#
#  Номера ВМ модемов идут от номера сервера: ВМ 3000 → модемы 3001, 3002,
#  3003… Так по номеру ВМ видно, чей это модем.
#
#  --recreate сносит все модемы этой ВМ и создаёт заново по текущей таблице:
#  так меняют таблицу целиком. Без него уже готовые модемы не трогаются.
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

O_SOURCE=""; O_VM=""; O_RECREATE=""; O_TPL=""; O_NO_ATTACH=""
while (( $# )); do
    case "$1" in
        --source)     O_SOURCE="$2"; shift 2 ;;
        --vm)         O_VM="$2"; shift 2 ;;
        --template)   O_TPL="$2"; shift 2 ;;
        --recreate)   O_RECREATE=1; shift ;;
        --no-attach)  O_NO_ATTACH=1; shift ;;
        --yes|-y)     export PCS_YES=1; shift ;;
        -h|--help)    usage; exit 0 ;;
        *) die "неизвестный аргумент: $1 (pcs modem-deploy --help)" ;;
    esac
done

pcs_begin modem-deploy
need_pve
pcs_tmpdir
ssh_setup
mdm_server_need "$O_VM"
state_need_vm "$PCS_SERVER_ID"
vm_exists "$PCS_SERVER_ID" || die "ВМ ${PCS_SERVER_ID} в Proxmox нет (pcs vm-use)"

hdr "Модемы для ВМ ${PCS_SERVER_ID} (${VM_NAME:-?} @ ${VM_IP:-?})"
info "Номера ВМ модемов: ${PCS_SERVER_ID}+n — модем 5 будет ВМ $(( PCS_SERVER_ID + 5 ))"

if [[ -z "$O_NO_ATTACH" ]]; then
    vm_alive || die "ВМ ${PCS_SERVER_ID} (${VM_IP}) недоступна по SSH"
    # ВМ не обязана быть сервером mobileproxy.space — просто скажем, что там.
    if vm_has_mp; then
        ok "софт mobileproxy.space на ВМ на месте — интерфейсы поднимет он"
    else
        warn "софта mobileproxy.space на ВМ нет: модемы воткнутся по USB, но"
        warn "адреса 192.168.<N>.100 никто не выдаст — поднимать интерфейсы вам"
    fi
fi

sub() { local c="$1"; shift; bash "$PCS_ROOT/cmd/${c}.sh" "$@"; }

# ── 1. Таблица ─────────────────────────────────────────────────────────────
hdr "1/4 Таблица модемов"
sheet_url_load
if [[ -n "$O_SOURCE" ]]; then
    sub modem-source --vm "$PCS_SERVER_ID" set "$O_SOURCE" || die "таблица не принята"
elif [[ -n "${SHEET_URL:-}" ]]; then
    ok "таблица уже задана: ${SHEET_URL}"
    info "сменить: pcs modem-deploy --source <ссылка> [--recreate]"
else
    info "Нужна Google-таблица с колонками n, real, proxy (host:port:login:pass)."
    info "Ссылку можно скопировать прямо из адресной строки браузера."
    ask O_SOURCE "Ссылка на таблицу"
    [[ -n "$O_SOURCE" ]] || die "без таблицы разворачивать нечего"
    sub modem-source --vm "$PCS_SERVER_ID" set "$O_SOURCE" || die "таблица не принята"
fi

# ── 2. Потолок USB ─────────────────────────────────────────────────────────
# Стоковое ядро принимает восемь устройств USB/IP. Поднять потолок надо до
# того, как создавать модемы, иначе девятый просто не воткнётся.
hdr "2/4 Потолок USB на ВМ"
if [[ -n "$O_NO_ATTACH" ]]; then
    info "втыкать не просили (--no-attach) — потолок не трогаю"
else
    # Ссылку мог только что записать modem-source в отдельном процессе.
    sheet_url_load
    want="$(sheet_rows "$SHEET_URL" 2>/dev/null | grep -c . || true)"
    [[ "$want" =~ ^[0-9]+$ ]] && (( want > 0 )) || want=1
    info "в таблице модемов: ${want}"
    sub vm-usb-ports --vm "$PCS_SERVER_ID" --modems "$want" --yes || die "не хватает USB-портов на ВМ"
fi

# ── 3. Шаблон ──────────────────────────────────────────────────────────────
hdr "3/4 Шаблон ВМ модема"
tpl_load
if [[ -n "${TPL_ID:-}" ]] && vm_exists "$TPL_ID"; then
    ok "шаблон ${TPL_ID} на месте"
else
    [[ -n "${TPL_ID:-}" ]] && warn "шаблон ${TPL_ID} из состояния пропал из Proxmox — собираю заново"
    tpl="${O_TPL:-${TPL_ID:-9999}}"
    step "Собираю шаблон ${tpl} (это долго: образ, пакеты, sing-box)..."
    sub modem-template --id "$tpl" --yes || die "шаблон не собрался"
    tpl_load
fi

# ── 4. Модемы ──────────────────────────────────────────────────────────────
hdr "4/4 Модемы по таблице"
if [[ -n "$O_RECREATE" ]]; then
    old=()
    while read -r n; do [[ -n "$n" ]] && old+=("$n"); done < <(mdm_list)
    if (( ${#old[@]} )); then
        warn "будут снесены модемы ВМ ${PCS_SERVER_ID}: ${old[*]}"
        confirm "Удалить их и создать заново по таблице?" no || die "отменено"
        for n in "${old[@]}"; do
            mdm_load "$n"
            [[ -n "$O_NO_ATTACH" ]] || sub modem-attach --n "$n" --detach --vm "$PCS_SERVER_ID" || true
            if [[ -n "${MDM_VMID:-}" ]] && vm_exists "$MDM_VMID"; then
                step "Удаляю ВМ ${MDM_VMID} (модем ${n})..."
                vm_destroy "$MDM_VMID" || warn "ВМ ${MDM_VMID} не удалилась"
            fi
            mdm_forget "$n"
        done
        ok "старые модемы сняты"
    else
        info "сносить нечего — модемов у этой ВМ ещё не было"
    fi
fi

sub modem-sync --vm "$PCS_SERVER_ID" --yes ${O_NO_ATTACH:+--no-attach} || exit 1

echo >&2
ok "модемы ВМ ${PCS_SERVER_ID} развёрнуты"
info "Проверить целиком: pcs modem-check"
info "Сменить таблицу: pcs modem-deploy --source <ссылка> --recreate"
