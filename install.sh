#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════════════════════════
#  install.sh — поставить или обновить PCS на хосте Proxmox.
#
#      bash <(curl -fsSL https://raw.githubusercontent.com/<owner>/<repo>/main/install.sh)
#
#  Кладёт репозиторий в /opt/pcs (атомарно: старая версия уходит в
#  /opt/pcs.prev), ставит команду /usr/local/bin/pcs. Состояние в /etc/pcs
#  и логи в /var/log/pcs не трогает.
#
#  PCS_REPO=owner/repo PCS_BRANCH=main — откуда брать.
# ═══════════════════════════════════════════════════════════════════════════
set -uo pipefail

PCS_REPO="${PCS_REPO:-Tovarish666/pcs}"
PCS_BRANCH="${PCS_BRANCH:-main}"
DEST="${PCS_DEST:-/opt/pcs}"
TARBALL="https://codeload.github.com/${PCS_REPO}/tar.gz/refs/heads/${PCS_BRANCH}"

ok()  { printf '  \033[32m✓\033[0m %s\n' "$*"; }
die() { printf '  \033[31m✗\033[0m %s\n' "$*" >&2; exit 1; }

[[ $EUID -eq 0 ]] || die "нужен root"
command -v qm >/dev/null 2>&1 || die "qm не найден — ставить на хост Proxmox"
command -v curl >/dev/null 2>&1 || die "нет curl"

tmp="$(mktemp -d)" || die "не создать временный каталог"
trap 'rm -rf "$tmp"' EXIT

curl -fsSL --retry 3 "$TARBALL" -o "$tmp/pcs.tgz" || die "не скачался ${TARBALL}"
mkdir -p "$tmp/src"
tar -xzf "$tmp/pcs.tgz" -C "$tmp/src" --strip-components=1 || die "архив не распаковался"
[[ -f "$tmp/src/pcs" && -d "$tmp/src/cmd" ]] || die "в архиве нет pcs/cmd — не тот репозиторий?"
bash -n "$tmp/src/pcs" || die "pcs не проходит проверку синтаксиса — не ставлю"

chmod 755 "$tmp/src/pcs" "$tmp/src"/cmd/*.sh "$tmp/src"/guest/* 2>/dev/null
rm -rf "${DEST}.new" "${DEST}.prev"
cp -a "$tmp/src" "${DEST}.new"
[[ -d "$DEST" ]] && mv "$DEST" "${DEST}.prev"
mv "${DEST}.new" "$DEST"
ln -sf "$DEST/pcs" /usr/local/bin/pcs

ok "PCS $(bash "$DEST/pcs" version) → ${DEST}"
ok "команда: pcs   (pcs help — список команд)"
