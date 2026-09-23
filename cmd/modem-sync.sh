#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════════════════════════
#  pcs modem-sync — привести хост в соответствие с таблицей.
#
#    pcs modem-sync                    чего нет — создать, что есть — поднять
#    pcs modem-sync --dry-run          только показать план, ничего не делать
#    pcs modem-sync --only 112,113     заняться только этими номерами
#    pcs modem-sync --no-attach        не втыкать в ВМ mobileproxy.space
#    pcs modem-sync --prune            снести модемы, которых нет в таблице
#    pcs modem-sync --source <ссылка>  разовая таблица, не запоминая
#
#  Руками не делает ничего: создаёт через pcs modem-add, втыкает через
#  pcs modem-attach — то же самое, что набрать их по очереди. Команду можно
#  гонять повторно: готовые модемы она не трогает, только показывает.
#
#  Строка с enabled=0 — модем вынимается из сервера и ВМ останавливается,
#  но не удаляется: вернуть = поправить таблицу и повторить. Удаляет только
#  --prune и только то, чего в таблице нет вовсе.
# ═══════════════════════════════════════════════════════════════════════════
set -uo pipefail
# shellcheck source=../lib/common.sh
source "$(dirname "$(readlink -f "$0")")/../lib/common.sh"
source "$PCS_ROOT/lib/state.sh"
source "$PCS_ROOT/lib/pve.sh"
source "$PCS_ROOT/lib/modem.sh"
source "$PCS_ROOT/lib/sheet.sh"

usage() { pcs_usage "$0"; }

O_ONLY=""; O_DRY=""; O_NO_ATTACH=""; O_PRUNE=""; O_SOURCE=""; O_VM=""
while (( $# )); do
    case "$1" in
        --only)       O_ONLY="$2"; shift 2 ;;
        --source)     O_SOURCE="$2"; shift 2 ;;
        --vm)         O_VM="$2"; shift 2 ;;
        --dry-run|-n) O_DRY=1; shift ;;
        --no-attach)  O_NO_ATTACH=1; shift ;;
        --prune)      O_PRUNE=1; shift ;;
        --yes|-y)     export PCS_YES=1; shift ;;
        -h|--help)    usage; exit 0 ;;
        *) die "неизвестный аргумент: $1 (pcs modem-sync --help)" ;;
    esac
done

pcs_begin modem-sync
need_pve
pcs_tmpdir
mdm_server_need "$O_VM"
tpl_load

# ── Таблица ────────────────────────────────────────────────────────────────
src="$O_SOURCE"
if [[ -n "$src" ]]; then src="$(sheet_url_normalize "$src")"; else sheet_url_need; src="$SHEET_URL"; fi
step "Читаю таблицу..."
rows="$(sheet_rows "$src")" || die "таблица не прочиталась"
[[ -n "$rows" ]] || die "в таблице нет ни одной строки модема"

only=" "
[[ -n "$O_ONLY" ]] && for n in ${O_ONLY//,/ }; do only+="${n} "; done
want() { [[ -z "$O_ONLY" || "$only" == *" $1 "* ]]; }

# ── План ───────────────────────────────────────────────────────────────────
# Пара «номер:ВМ» — чтобы не перечитывать конфиги на каждом шаге.
create=(); start=(); ready=(); stop=(); extra=()
seen=" "
while IFS=$'\t' read -r n real host port login _pass enabled; do
    [[ "$n" =~ ^[0-9]+$ ]] || continue
    seen+="${n} "
    want "$n" || continue
    mdm_load "$n"
    vmid="${MDM_VMID:-$(mdm_vmid "$n")}"
    if [[ "$enabled" != "1" ]]; then
        vm_exists "$vmid" && stop+=("${n}:${vmid}")
        continue
    fi
    if   ! vm_exists "$vmid";  then create+=("$n")
    elif ! vm_running "$vmid"; then start+=("${n}:${vmid}")
    else                            ready+=("${n}:${vmid}")
    fi
done <<<"$rows"

for n in $(mdm_list); do
    [[ "$seen" == *" $n "* ]] && continue
    want "$n" || continue
    mdm_load "$n"
    extra+=("${n}:${MDM_VMID:-—}")
done

# Колонками не выравниваем: printf считает байты, а не буквы, и кириллица
# в заголовке всё равно съедет.
show() {                           # show <заголовок> <номера...>
    local title="$1"; shift
    (( $# )) || return 0
    printf '  %s%s%s: %s\n' "$B" "$title" "$R" "$*" >&2
}
hdr "План"
show "создать"       "${create[@]+"${create[@]}"}"
show "запустить"     "${start[@]+"${start[@]%%:*}"}"
show "уже готовы"    "${ready[@]+"${ready[@]%%:*}"}"
show "остановить"    "${stop[@]+"${stop[@]%%:*}"}"
show "нет в таблице" "${extra[@]+"${extra[@]%%:*}"}"
(( ${#create[@]} + ${#start[@]} + ${#ready[@]} + ${#stop[@]} + ${#extra[@]} )) \
    || die "в таблице нечего делать (проверь --only и pcs modem-list)"
[[ -n "$O_PRUNE" ]] && (( ${#extra[@]} )) && warn "--prune: «нет в таблице» будут удалены вместе с ВМ"
[[ -z "$O_PRUNE" ]] && (( ${#extra[@]} )) && info "оставляю как есть; снести — pcs modem-sync --prune"

if [[ -n "$O_DRY" ]]; then
    echo >&2; info "--dry-run: ничего не делаю"
    exit 0
fi
(( ${#create[@]} )) && tpl_need
confirm "Выполнить?" yes || die "отменено"

problems=0
oops() { bad "$*"; problems=$((problems + 1)); }
sub() {                            # sub <команда pcs> <аргументы...>
    local c="$1"; shift
    bash "$PCS_ROOT/cmd/${c}.sh" "$@"
}

# ── Лишние и выключенные ───────────────────────────────────────────────────
# Сначала вынимаем из сервера: иначе он будет держать USB удаляемой ВМ.
for pair in "${stop[@]+"${stop[@]}"}" ; do
    n="${pair%%:*}"; vmid="${pair##*:}"
    hdr "Модем ${n}: в таблице выключен"
    [[ -n "$O_NO_ATTACH" ]] || sub modem-attach --n "$n" --detach ${O_VM:+--vm "$O_VM"} || true
    step "Останавливаю ВМ ${vmid}..."
    qm stop "$vmid" >>"$PCS_LOG" 2>&1 || true
    w=0; while vm_running "$vmid" && (( w < 40 )); do sleep 2; w=$((w + 2)); done
    if vm_running "$vmid"; then oops "ВМ ${vmid} не остановилась"; else ok "модем ${n} выключен"; fi
done

if [[ -n "$O_PRUNE" ]]; then
    for pair in "${extra[@]+"${extra[@]}"}"; do
        n="${pair%%:*}"; vmid="${pair##*:}"
        hdr "Модем ${n}: в таблице его нет"
        confirm "Удалить модем ${n} и его ВМ ${vmid}?" no || { info "оставлен"; continue; }
        [[ -n "$O_NO_ATTACH" ]] || sub modem-attach --n "$n" --detach ${O_VM:+--vm "$O_VM"} || true
        if [[ "$vmid" =~ ^[0-9]+$ ]] && vm_exists "$vmid"; then
            vm_destroy "$vmid" || oops "ВМ ${vmid} не удалилась"
        fi
        mdm_forget "$n"
        ok "модем ${n} удалён"
    done
fi

# ── Создание ───────────────────────────────────────────────────────────────
live=()                            # номера, которые в итоге надо воткнуть
for pair in "${ready[@]+"${ready[@]}"}"; do live+=("${pair%%:*}"); done

for n in "${create[@]+"${create[@]}"}"; do
    hdr "Модем ${n}: создаю"
    if sub modem-add --n "$n" --yes --vm "$PCS_SERVER_ID" ${O_SOURCE:+--source "$O_SOURCE"}; then
        live+=("$n")
    else
        oops "модем ${n} не создался"
    fi
done

# ── Запуск остановленных ───────────────────────────────────────────────────
for pair in "${start[@]+"${start[@]}"}"; do
    n="${pair%%:*}"; vmid="${pair##*:}"
    hdr "Модем ${n}: ВМ ${vmid} выключена"
    step "Запускаю..."
    qm start "$vmid" >>"$PCS_LOG" 2>&1 || vm_running "$vmid" || { oops "ВМ ${vmid} не запускается"; continue; }
    ok "ВМ ${vmid} запущена"
    live+=("$n")
done

# ── Адрес мог смениться ────────────────────────────────────────────────────
# ВМ модема живёт на DHCP: после перезапуска адрес бывает другой, а сервер
# втыкает USB именно по адресу. Спрашиваем guest agent и правим у себя.
for n in "${live[@]+"${live[@]}"}"; do
    mdm_load "$n"
    [[ -n "${MDM_VMID:-}" ]] || continue
    now=""
    for i in 1 2 3 4 5 6 7 8 9 10 11 12; do
        now="$(qm_agent_ip "$MDM_VMID")" && break
        sleep 5
    done
    [[ -n "$now" ]] || { warn "модем ${n}: guest agent молчит, оставляю адрес ${MDM_IP:-?}"; continue; }
    if [[ "$now" != "${MDM_IP:-}" ]]; then
        warn "модем ${n}: адрес сменился ${MDM_IP:-?} → ${now}"
        MDM_IP="$now"; mdm_save
    fi
done

# ── Втыкаем в сервер ───────────────────────────────────────────────────────
if [[ -n "$O_NO_ATTACH" ]]; then
    warn "втыкать не прошу (--no-attach): pcs modem-attach --all"
elif (( ${#live[@]} )); then
    list="$(IFS=,; printf '%s' "${live[*]}")"
    hdr "Втыкаю в ВМ mobileproxy.space: ${list}"
    sub modem-attach --n "$list" ${O_VM:+--vm "$O_VM"} || oops "не все модемы воткнулись"
fi

echo >&2
if (( problems == 0 )); then
    ok "ВМ ${PCS_SERVER_ID} сходится с таблицей: модемов в работе — ${#live[@]}"
    info "Что где: pcs modem-list"
else
    bad "с частью модемов не сложилось: ${problems} — см. выше"
    exit 1
fi
