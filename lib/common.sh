# shellcheck shell=bash
# ═══════════════════════════════════════════════════════════════════════════
#  lib/common.sh — вывод, лог, ввод, базовые проверки.
#  Подключается каждой командой из cmd/. Сама ничего не делает.
#
#  Сообщения для человека идут в stderr: stdout команды остаётся под данные
#  (например, IP), чтобы команды можно было звать друг из друга.
# ═══════════════════════════════════════════════════════════════════════════
[[ -n "${_PCS_COMMON_LOADED:-}" ]] && return 0
_PCS_COMMON_LOADED=1

PCS_VERSION="3.0.0-dev"
PCS_REPO="${PCS_REPO:-Tovarish666/pcs}"
PCS_BRANCH="${PCS_BRANCH:-main}"

PCS_ROOT="${PCS_ROOT:-$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")/.." && pwd)}"
PCS_ETC="${PCS_ETC:-/etc/pcs}"
PCS_LOG_DIR="${PCS_LOG_DIR:-/var/log/pcs}"
PCS_LOG="${PCS_LOG:-${PCS_LOG_DIR}/pcs-$(date +%Y%m%d).log}"
PCS_CMD="${PCS_CMD:-pcs}"

# ── Цвета ──────────────────────────────────────────────────────────────────
if [[ -t 2 && -z "${NO_COLOR:-}" ]]; then
    R=$'\033[0m'; G=$'\033[32m'; RD=$'\033[31m'; Y=$'\033[33m'
    C=$'\033[36m'; B=$'\033[1m'; D=$'\033[2m'
else
    R=""; G=""; RD=""; Y=""; C=""; B=""; D=""
fi

# ── Лог и вывод ────────────────────────────────────────────────────────────
_log() {
    printf '%s [%s] %s\n' "$(date '+%F %T')" "$PCS_CMD" "$*" >>"$PCS_LOG" 2>/dev/null || true
}
ok()   { _log "OK    $*"; printf '  %s✓%s %s\n' "$G"  "$R" "$*" >&2; }
warn() { _log "WARN  $*"; printf '  %s⚠%s %s\n' "$Y"  "$R" "$*" >&2; }
bad()  { _log "FAIL  $*"; printf '  %s✗%s %s\n' "$RD" "$R" "$*" >&2; }
step() { _log "STEP  $*"; printf '  %s→%s %s\n' "$D"  "$R" "$*" >&2; }
info() { _log "INFO  $*"; printf '  %sℹ%s %s\n' "$C"  "$R" "$*" >&2; }
hdr()  {
    _log "==== $*"
    printf '\n%s── %s ──%s\n' "$B" "$*" "$R" >&2
}
die()  { bad "$*"; exit 1; }

# Строки из чужого вывода (скрипты на ВМ) — с отступом, в лог целиком.
relay() {
    local line
    while IFS= read -r line; do
        _log "  | $line"
        printf '    %s%s%s\n' "$D" "$line" "$R" >&2
    done
}

pcs_init_log() {
    mkdir -p "$PCS_LOG_DIR" 2>/dev/null && chmod 700 "$PCS_LOG_DIR" 2>/dev/null
    touch "$PCS_LOG" 2>/dev/null && chmod 600 "$PCS_LOG" 2>/dev/null
    return 0
}

# ── Ввод ───────────────────────────────────────────────────────────────────
# Терминал есть? Без него (cron, ssh без -t, pipe) спрашивать нельзя —
# берём умолчание или падаем с подсказкой, каким флагом передать значение.
has_tty() { [[ -z "${PCS_NONINTERACTIVE:-}" ]] && { : </dev/tty; } 2>/dev/null; }

# ask VAR "Вопрос" [умолчание]
# Если VAR уже задана (флагом) — не спрашивает.
ask() {
    local __var="$1" __q="$2" __def="${3:-}" __ans=""
    [[ -n "${!__var:-}" ]] && return 0
    if ! has_tty; then
        [[ -n "$__def" ]] || die "не задано: ${__q} (передай флагом)"
        printf -v "$__var" '%s' "$__def"
        return 0
    fi
    if [[ -n "$__def" ]]; then
        printf '  %s?%s %s %s[%s]%s: ' "$C" "$R" "$__q" "$D" "$__def" "$R" >&2
    else
        printf '  %s?%s %s: ' "$C" "$R" "$__q" >&2
    fi
    IFS= read -r __ans </dev/tty || true
    printf -v "$__var" '%s' "${__ans:-$__def}"
}

# ask_opt VAR "Вопрос" — можно оставить пустым (Enter — пропустить).
# Без терминала молча оставляет пустым.
ask_opt() {
    local __var="$1" __q="$2" __ans=""
    [[ -n "${!__var:-}" ]] && return 0
    has_tty || return 0
    printf '  %s?%s %s %s[Enter — пропустить]%s: ' "$C" "$R" "$__q" "$D" "$R" >&2
    IFS= read -r __ans </dev/tty || true
    printf -v "$__var" '%s' "$__ans"
}

# ask_secret VAR "Вопрос" [confirm] — ввод без эха, с повтором при confirm.
ask_secret() {
    local __var="$1" __q="$2" __confirm="${3:-}" __a="" __b=""
    [[ -n "${!__var:-}" ]] && return 0
    has_tty || die "не задано: ${__q} (передай флагом)"
    while true; do
        printf '  %s?%s %s: ' "$C" "$R" "$__q" >&2
        IFS= read -rs __a </dev/tty || true; echo >&2
        [[ -n "$__a" ]] || { warn "пусто"; continue; }
        [[ -z "$__confirm" ]] && break
        printf '  %s?%s повтори: ' "$C" "$R" >&2
        IFS= read -rs __b </dev/tty || true; echo >&2
        [[ "$__a" == "$__b" ]] && break
        warn "не совпало, ещё раз"
    done
    printf -v "$__var" '%s' "$__a"
}

# confirm "Вопрос" [yes|no] — PCS_YES=1 отвечает «да» без вопроса.
confirm() {
    local q="$1" def="${2:-no}" a=""
    [[ -n "${PCS_YES:-}" ]] && return 0
    if ! has_tty; then
        [[ "$def" == "yes" ]]
        return
    fi
    printf '  %s?%s %s (yes/no) %s[%s]%s: ' "$C" "$R" "$q" "$D" "$def" "$R" >&2
    IFS= read -r a </dev/tty || true
    a="${a:-$def}"; a="$(printf '%s' "$a" | tr '[:upper:]' '[:lower:]')"
    [[ "$a" == "yes" || "$a" == "y" || "$a" == "да" ]]
}

# ── Проверки окружения ─────────────────────────────────────────────────────
need_root() { [[ $EUID -eq 0 ]] || die "нужен root"; }

need_pve() {
    local c
    for c in qm pvesm pvesh; do
        command -v "$c" >/dev/null 2>&1 || die "нет $c — это не хост Proxmox"
    done
}

# Справка команды — её шапка-комментарий между рамками ═══.
pcs_usage() {
    awk 'NR > 2 && /^# ═/ { exit } NR > 2 { sub(/^# ?/, ""); sub(/^ /, ""); print }' "$1" >&2
}

# Временный каталог команды, убирается на выходе.
pcs_tmpdir() {
    PCS_TMP="$(mktemp -d /tmp/pcs.XXXXXX)" || die "не создать временный каталог"
    # shellcheck disable=SC2064
    trap "rm -rf '$PCS_TMP'" EXIT
}

# Стандартное начало каждой команды.
pcs_begin() {
    PCS_CMD="$1"
    need_root
    pcs_init_log
    _log "---- start: $PCS_CMD ${*:2}"
}
