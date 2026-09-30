#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════════════════════════
#  install.sh — поставить или обновить PCS.
#
#      bash <(curl -fsSL https://raw.githubusercontent.com/Tovarish666/pcs/main/install.sh)
#
#  На хосте Proxmox — команда pcs (/opt/pcs): создаёт серверы и управляет ими.
#  На обычном сервере или ПК с Ubuntu — модемы прямо на нём (vmodem-install.sh).
#
#  PCS_REPO=owner/repo  PCS_BRANCH=main — откуда брать.
# ═══════════════════════════════════════════════════════════════════════════
set -euo pipefail

PCS_REPO="${PCS_REPO:-Tovarish666/pcs}"
PCS_BRANCH="${PCS_BRANCH:-main}"
DEST="${PCS_DEST:-/opt/pcs}"

ok()  { printf '  \033[32m✓\033[0m %s\n' "$*"; }
die() { printf '  \033[31m✗\033[0m %s\n' "$*" >&2; exit 1; }

[[ $EUID -eq 0 ]] || die "нужен root"
command -v curl >/dev/null || die "нет curl"
command -v python3 >/dev/null || die "нет python3"

tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT
curl -fsSL --retry 3 "https://codeload.github.com/${PCS_REPO}/tar.gz/refs/heads/${PCS_BRANCH}" -o "$tmp/pcs.tgz" \
    || die "не скачался ${PCS_REPO}@${PCS_BRANCH}"
mkdir -p "$tmp/src"
tar -xzf "$tmp/pcs.tgz" -C "$tmp/src" --strip-components=1
for f in pcs agent/vmodem agent/vmodem-api; do
    python3 -c 'import ast, sys; ast.parse(open(sys.argv[1], encoding="utf-8").read())' "$tmp/src/$f" \
        || die "$f не разбирается — не ставлю"
done
chmod 755 "$tmp/src/pcs" "$tmp/src/agent/"*

if command -v qm >/dev/null 2>&1; then
    rm -rf "${DEST}.new" "${DEST}.prev"
    cp -a "$tmp/src" "${DEST}.new"
    printf 'PCS_REPO=%s\nPCS_BRANCH=%s\n' "$PCS_REPO" "$PCS_BRANCH" > "${DEST}.new/.source"   # pcs update — отсюда же
    [[ -d "$DEST" ]] && mv "$DEST" "${DEST}.prev"
    mv "${DEST}.new" "$DEST"
    ln -sf "$DEST/pcs" /usr/local/bin/pcs
    ok "PCS $("$DEST/pcs" version) → ${DEST}, команда: pcs"
    ok "дальше: pcs   (меню)   или   pcs server create"
else
    # Не Proxmox — модемы прямо здесь (то же, что vmodem-install.sh; параметры — его)
    bash "$tmp/src/vmodem-install.sh" --src "$tmp/src" "$@"
fi
