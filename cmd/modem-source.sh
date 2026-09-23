#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════════════════════════
#  pcs modem-source — откуда PCS берёт таблицу модемов.
#
#    pcs modem-source                 показать текущую ссылку
#    pcs modem-source set <ссылка>    запомнить (можно и локальный файл)
#    pcs modem-source test [ссылка]   скачать и показать, что разобралось
#
#  Ссылка своя у каждой ВМ mobileproxy.space — берётся активная или --vm ID.
#
#  Таблица — Google-таблица с колонками n, real, proxy:
#
#      n     real   proxy
#      201   1      188.134.88.13:12000:modem1:пароль
#
#  n — номер для mobileproxy.space (модем 192.168.n.1, сервер 192.168.n.100),
#  real — октет настоящего модема за прокси, proxy — host:port:login:pass.
#  Ссылку можно давать прямо из адресной строки: PCS сам приведёт её к CSV.
# ═══════════════════════════════════════════════════════════════════════════
set -uo pipefail
# shellcheck source=../lib/common.sh
source "$(dirname "$(readlink -f "$0")")/../lib/common.sh"
source "$PCS_ROOT/lib/state.sh"
source "$PCS_ROOT/lib/modem.sh"
source "$PCS_ROOT/lib/sheet.sh"

usage() { pcs_usage "$0"; }

# --vm можно поставить и до действия, и после него.
O_VM=""
[[ "${1:-}" == "--vm" ]] && { O_VM="${2:-}"; shift 2; }
ACTION="${1:-show}"; shift 2>/dev/null || true
[[ "${1:-}" == "--vm" ]] && { O_VM="${2:-}"; shift 2; }
case "$ACTION" in
    -h|--help) usage; exit 0 ;;
esac

pcs_begin modem-source
pcs_tmpdir
mdm_server_need "$O_VM"

case "$ACTION" in
    show)
        sheet_url_load
        if [[ -n "$SHEET_URL" ]]; then
            echo "  таблица: ${SHEET_URL}" >&2
        else
            warn "таблица не задана: pcs modem-source set <ссылка>"
            exit 1
        fi
        ;;
    set)
        url="${1:-}"
        [[ -n "$url" ]] || ask url "Ссылка на таблицу"
        [[ -n "$url" ]] || die "пусто"
        norm="$(sheet_url_normalize "$url")"
        [[ "$norm" == "$url" ]] || info "ссылка приведена к CSV: ${norm}"
        step "Проверяю, что таблица читается..."
        rows="$(sheet_rows "$norm")" || die "таблица не прочиталась — ссылка не сохранена"
        sheet_url_save "$norm"
        ok "запомнил: ${norm}"
        ok "модемов в таблице: $(printf '%s\n' "$rows" | grep -c .)"
        ;;
    test)
        url="${1:-}"
        [[ -n "$url" ]] && url="$(sheet_url_normalize "$url")" || { sheet_url_need; url="$SHEET_URL"; }
        rows="$(sheet_rows "$url")" || die "таблица не прочиталась"
        printf '\n  %s%-5s %-5s %-24s %-14s %s%s\n' "$D" "n" "real" "прокси" "логин" "вкл" "$R" >&2
        while IFS=$'\t' read -r n real host port login _pass enabled; do
            [[ -n "$n" ]] || continue
            printf '  %-5s %-5s %-24s %-14s %s\n' \
                "$n" "$real" "${host}:${port}" "$login" \
                "$([[ "$enabled" == "1" ]] && echo да || echo нет)" >&2
        done <<<"$rows"
        echo >&2
        ok "строк разобрано: $(printf '%s\n' "$rows" | grep -c .)"
        ;;
    *) usage; exit 1 ;;
esac
