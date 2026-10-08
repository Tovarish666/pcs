#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════════════════════════
#  install.sh — поставить или обновить PCS на хост Proxmox VE.
#
#      bash <(curl -fsSL https://raw.githubusercontent.com/Tovarish666/pcs/main/install.sh)
#
#  Кладёт код в /opt/pcs, команды pcs и hivelink в /usr/local/bin, спрашивает
#  логин и пароль веб-панели (порт 666) и включает её, ставит hivelink на хост.
#  Серверы (ВМ Ubuntu) дальше создаёт сам pcs: pcs server create или меню pcs.
#
#  PCS_REPO=owner/repo PCS_BRANCH=main   откуда брать (pcs update берёт оттуда же)
#  PCS_SRC=/путь/к/дереву                поставить из готового дерева, без GitHub
#  PCS_WEB_USER=admin PCS_WEB_PASS=…     вход в панель без вопросов
# ═══════════════════════════════════════════════════════════════════════════
set -euo pipefail

PCS_REPO="${PCS_REPO:-Tovarish666/pcs}"
PCS_BRANCH="${PCS_BRANCH:-main}"
DEST="${PCS_DEST:-/opt/pcs}"

ok()   { printf '  \033[32m✓\033[0m %s\n' "$*"; }
warn() { printf '  \033[33m⚠\033[0m %s\n' "$*"; }
die()  { printf '  \033[31m✗\033[0m %s\n' "$*" >&2; exit 1; }

[[ $EUID -eq 0 ]] || die "нужен root: sudo bash install.sh"
if ! command -v qm >/dev/null 2>&1 || ! command -v pvesm >/dev/null 2>&1 || [[ ! -d /etc/pve ]]; then
    die "PCS ставится на хост Proxmox VE (qm, pvesm, /etc/pve). Режим «PCS прямо на VPS с Ubuntu» пока не сделан."
fi
command -v python3 >/dev/null || die "нет python3: apt install python3"
python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' \
    || die "нужен Python 3.11+ (Debian 12/13), а стоит $(python3 -V 2>&1)"

tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT
mkdir -p "$tmp/src"
if [[ -n "${PCS_SRC:-}" ]]; then
    [[ -f "$PCS_SRC/bin/pcs" ]] || die "в $PCS_SRC нет bin/pcs — это не дерево PCS 5"
    tar -C "$PCS_SRC" --exclude=.git --exclude=__pycache__ -cf - . | tar -C "$tmp/src" -xf -
    from="$PCS_SRC"
else
    command -v curl >/dev/null || die "нет curl: apt install curl"
    curl -fsSL --retry 3 "https://codeload.github.com/${PCS_REPO}/tar.gz/refs/heads/${PCS_BRANCH}" -o "$tmp/pcs.tgz" \
        || die "не скачался ${PCS_REPO}@${PCS_BRANCH}"
    tar -xzf "$tmp/pcs.tgz" -C "$tmp/src" --strip-components=1
    from="${PCS_REPO}@${PCS_BRANCH}"
fi
[[ -f "$tmp/src/bin/pcs" && -f "$tmp/src/pcs/__init__.py" ]] || die "в ${from} нет PCS 5 (bin/pcs, pcs/)"
python3 -m compileall -q "$tmp/src/pcs" >/dev/null || die "код из ${from} не компилируется — не ставлю"
chmod 755 "$tmp/src/bin/"*
printf 'PCS_REPO=%s\nPCS_BRANCH=%s\n' "$PCS_REPO" "$PCS_BRANCH" > "$tmp/src/.source"   # pcs update — отсюда же

# /opt/pcs целиком: прежний — в /opt/pcs.prev (откат — переименовать обратно)
rm -rf "${DEST}.new"
cp -a "$tmp/src" "${DEST}.new"
if [[ -d "$DEST" ]]; then
    rm -rf "${DEST}.prev"
    mv "$DEST" "${DEST}.prev"
fi
mv "${DEST}.new" "$DEST"
for c in pcs hivelink; do ln -sfn "$DEST/bin/$c" "/usr/local/bin/$c"; done
install -d -m 700 /etc/pcs
install -d -m 755 /var/lib/pcs /var/log/pcs
ok "PCS $("$DEST/bin/pcs" version) → ${DEST} (из ${from}), команды: pcs, hivelink"

# ── веб-панель: логин и пароль ─────────────────────────────────────────────
ask() {   # ask "вопрос" [тихо] → ответ на stdout; без терминала — пусто
    local reply=""
    [[ -r /dev/tty ]] || return 0
    if [[ "${2:-}" == "silent" ]]; then
        read -rsp "  ? $1: " reply </dev/tty || true; echo >/dev/tty
    else
        read -rp "  ? $1: " reply </dev/tty || true
    fi
    printf '%s' "$reply"
}
if [[ -f /etc/pcs/web.json && -z "${PCS_WEB_PASS:-}" ]]; then
    ok "вход в панель уже задан (сменить: pcs web passwd)"
    systemctl try-restart pcs-web.service 2>/dev/null || true
else
    user="${PCS_WEB_USER:-}"
    pass="${PCS_WEB_PASS:-}"
    if [[ -z "$pass" ]]; then
        echo "  Веб-панель PCS (порт 666): логин и пароль для входа."
        [[ -n "$user" ]] || user="$(ask "Логин [admin]")"
        pass="$(ask "Пароль (не короче 8)" silent)"
        if [[ -n "$pass" && "$(ask "Ещё раз" silent)" != "$pass" ]]; then
            warn "пароли не совпали"
            pass=""
        fi
    fi
    user="${user:-admin}"
    if [[ -z "$pass" ]]; then
        warn "пароль панели не задан — панель выключена. Потом: pcs web passwd && pcs web on"
    elif printf '%s\n' "$pass" | "$DEST/bin/pcs" web passwd --user "$user"; then
        "$DEST/bin/pcs" web on || warn "панель не включилась — см. выше; потом: pcs web on"
    else
        warn "вход в панель не сохранён — см. выше; потом: pcs web passwd"
    fi
fi

# ── hivelink на хост: ставится сам ─────────────────────────────────────────
if "$DEST/bin/hivelink" install; then
    ok "hivelink на хосте"
else
    warn "hivelink на хост не встал (см. выше) — PCS работает и без него; потом: hivelink install"
fi

ok "дальше: pcs   (меню)   или   pcs server create --mode usb|gw"
