# shellcheck shell=bash
# ═══════════════════════════════════════════════════════════════════════════
#  lib/sheet.sh — таблица с модемами: откуда берём и как читаем.
#
#  Источник правды — Google-таблица с колонками n, real, proxy. PCS её
#  только читает: ссылка лежит в /etc/pcs/modem/source.conf.
#  Разбор — lib/sheet-parse.py, здесь скачивание и нормализация ссылки.
# ═══════════════════════════════════════════════════════════════════════════
[[ -n "${_PCS_SHEET_LOADED:-}" ]] && return 0
_PCS_SHEET_LOADED=1

# Таблица своя у каждой ВМ mobileproxy.space: каталог задаёт lib/modem.sh.
sheet_conf() { printf '%s/source.conf' "${PCS_MODEM_STATE_DIR:-${PCS_ETC}/modem}"; }
SHEET_URL=""

# Ссылку можно давать любую: из адресной строки, «Опубликовать в интернете»
# или прямую выгрузку. Приводим к виду, который отдаёт CSV.
sheet_url_normalize() {
    local u="$1" id gid="0"
    if [[ "$u" =~ docs\.google\.com/spreadsheets/d/(e/)?([A-Za-z0-9_-]+) ]]; then
        # Уже готовые адреса не трогаем.
        if [[ "$u" == *"output=csv"* || ( "$u" == *"export"* && "$u" == *"format=csv"* ) ]]; then
            printf '%s' "$u"; return 0
        fi
        id="${BASH_REMATCH[2]}"
        [[ "$u" =~ [#?\&]gid=([0-9]+) ]] && gid="${BASH_REMATCH[1]}"
        if [[ "$u" == *"/spreadsheets/d/e/"* ]]; then
            printf 'https://docs.google.com/spreadsheets/d/e/%s/pub?gid=%s&single=true&output=csv' "$id" "$gid"
        else
            printf 'https://docs.google.com/spreadsheets/d/%s/export?format=csv&gid=%s' "$id" "$gid"
        fi
        return 0
    fi
    printf '%s' "$u"
}

sheet_url_load() {
    SHEET_URL=""
    local f; f="$(sheet_conf)"
    # shellcheck disable=SC1090
    [[ -f "$f" ]] && source "$f"
    return 0
}

sheet_url_save() {                 # sheet_url_save <url>
    local f; f="$(sheet_conf)"
    install -d -m 700 "$(dirname "$f")"
    local t="${f}.tmp"
    {
        echo "# pcs ${PCS_VERSION}: откуда берём таблицу модемов"
        printf 'SHEET_URL=%q\n' "$1"
    } >"$t"
    chmod 600 "$t"
    mv -f "$t" "$f"
    SHEET_URL="$1"
}

sheet_url_need() {
    sheet_url_load
    [[ -n "${SHEET_URL:-}" ]] || die "таблица не задана: pcs modem-source set <ссылка>"
}

# Скачать таблицу в файл. Локальный путь тоже принимается — удобно для проверок.
sheet_fetch() {                    # sheet_fetch <url|путь> <куда>
    local url="$1" dest="$2"
    if [[ -f "$url" ]]; then
        cp "$url" "$dest" || return 1
    else
        curl -fsSL --retry 3 --max-time 60 "$url" -o "$dest" || {
            bad "таблица не скачалась: ${url}"
            return 1
        }
    fi
    [[ -s "$dest" ]] || { bad "таблица пустая"; return 1; }
    # Google отдаёт HTML, когда доступа нет: «Опубликовать в интернете» или
    # «Доступ по ссылке» не включены. CSV с этого символа не начинается.
    if [[ "$(head -c1 "$dest")" == "<" ]]; then
        bad "вместо таблицы пришёл HTML — нет доступа по ссылке?"
        info "Файл → Поделиться → Доступ по ссылке (читатель) или Опубликовать в интернете → CSV"
        return 1
    fi
    return 0
}

# Строки таблицы: n real host port login pass enabled — через табуляцию.
# Замечания разборщика уходят в stderr и в лог.
sheet_rows() {                     # sheet_rows [url|путь]
    local url="${1:-}" tmp
    [[ -n "$url" ]] || { sheet_url_need; url="$SHEET_URL"; }
    tmp="$(mktemp "${PCS_TMP:-/tmp}/sheet.XXXXXX")" || return 1
    sheet_fetch "$url" "$tmp" || { rm -f "$tmp"; return 1; }
    python3 "${PCS_ROOT}/lib/sheet-parse.py" <"$tmp" 2> >(relay)
    local rc=$?
    rm -f "$tmp"
    return $rc
}
