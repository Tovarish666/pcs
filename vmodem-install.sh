#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════════════════════════
#  vmodem-install.sh — виртуальные модемы на любом сервере или ПК с Ubuntu,
#  без Proxmox и без pcs.
#
#      bash <(curl -fsSL https://raw.githubusercontent.com/Tovarish666/pcs/main/vmodem-install.sh) \
#           --sheet 'https://docs.google.com/spreadsheets/d/ID/edit#gid=0'
#
#  Ставит агент vmodem (модемы с их DHCP, DNS и веб-мордой, диагностика,
#  оповещения), собирает dummy_hcd под ядро и поднимает модемы по таблице.
#  Повторный запуск — обновление: модемы пересоздаются, только если надо.
#
#    --sheet URL      таблица модемов (n, real, proxy); без неё — только установка
#    --mpspace        затем софт mobileproxy.space (в конце сервер перезагрузится)
#      --auth JSON    auth.mp из ЛК mobileproxy.space
#      --proxy URL    HTTP-прокси, если зеркала mobileproxy.space не открываются
#      --no-reboot    не перезагружаться после mobileproxy.space
#    --slots N        слотов под модемы (по умолчанию 48; не больше свободных шин USB)
#
#  PCS_REPO=owner/repo  PCS_BRANCH=main — откуда брать.
# ═══════════════════════════════════════════════════════════════════════════
set -euo pipefail

PCS_REPO="${PCS_REPO:-Tovarish666/pcs}"
PCS_BRANCH="${PCS_BRANCH:-main}"

ok()   { printf '  \033[32m✓\033[0m %s\n' "$*"; }
warn() { printf '  \033[33m⚠\033[0m %s\n' "$*"; }
die()  { printf '  \033[31m✗\033[0m %s\n' "$*" >&2; exit 1; }
usage() {
    cat <<'EOF'
vmodem-install.sh — виртуальные модемы на Ubuntu без Proxmox.

  --sheet URL      таблица модемов (n, real, proxy); без неё — только установка
  --mpspace        затем софт mobileproxy.space (в конце сервер перезагрузится)
    --auth JSON    auth.mp из ЛК mobileproxy.space
    --proxy URL    HTTP-прокси, если зеркала mobileproxy.space не открываются
    --no-reboot    не перезагружаться после mobileproxy.space
  --slots N        слотов под модемы (по умолчанию 48)

PCS_REPO=owner/repo  PCS_BRANCH=main — откуда брать.
EOF
}

sheet="" mpspace="" auth="" proxy="" noreboot="" slots="" src=""
while (($#)); do
    case "$1" in
        --sheet)     sheet="${2:?--sheet: нужна ссылка}"; shift 2 ;;
        --mpspace)   mpspace=1; shift ;;
        --auth)      auth="${2:?--auth: нужен JSON}"; shift 2 ;;
        --proxy)     proxy="${2:?--proxy: нужен адрес}"; shift 2 ;;
        --no-reboot) noreboot=1; shift ;;
        --slots)     slots="${2:?--slots: нужно число}"; shift 2 ;;
        --src)       src="${2:?}"; shift 2 ;;          # уже скачанный репозиторий (так зовёт install.sh)
        -h|--help)   usage; exit 0 ;;
        *)           die "непонятный параметр: $1 (--help — список)" ;;
    esac
done

# ── Можно ли здесь ────────────────────────────────────────────────────────
[[ $EUID -eq 0 ]] || die "нужен root: sudo -i, потом снова"
command -v qm >/dev/null 2>&1 && die "это хост Proxmox: здесь ставится pcs (install.sh), а модемы живут в ВМ — pcs server create"
[[ "$(uname -m)" == x86_64 ]] || die "нужен x86_64 (sing-box ставится под amd64), здесь $(uname -m)"
# shellcheck disable=SC1091
. /etc/os-release
case "${ID:-}" in
    ubuntu) [[ "${VERSION_ID:-}" == 24.04 ]] || warn "проверено на Ubuntu 24.04, здесь ${PRETTY_NAME:-?}" ;;
    debian) warn "${PRETTY_NAME:-Debian}: не основной вариант, проверено на Ubuntu 24.04" ;;
    *)      die "нужна Ubuntu (здесь ${PRETTY_NAME:-неизвестная ОС})" ;;
esac

# Secure Boot: модуль dummy_hcd собирается здесь же, и ядро загрузит его, только если
# ключ, которым dkms его подписал, зарегистрирован в прошивке. В ВМ Proxmox (SeaBIOS)
# этого вопроса нет, на обычном ПК — часто есть.
secure_boot() {
    local f
    for f in /sys/firmware/efi/efivars/SecureBoot-*; do
        [[ -e "$f" && "$(od -An -t u1 -j4 -N1 "$f" | tr -d ' ')" == 1 ]] && return 0
    done
    return 1
}
if secure_boot && ! lsmod | grep -q '^dummy_hcd'; then
    MOK=/var/lib/shim-signed/mok/MOK.der
    if ! { command -v mokutil >/dev/null && [[ -f $MOK ]] && mokutil --test-key "$MOK" 2>&1 | grep -q "already enrolled"; }; then
        cat >&2 <<EOF
  ✗ Включён Secure Boot: собранный здесь модуль dummy_hcd ядро не загрузит.
    Вариант 1 — выключить Secure Boot в BIOS/UEFI и запустить установку снова.
    Вариант 2 — зарегистрировать ключ dkms (нужен доступ к экрану ПК):
        apt-get install -y dkms mokutil shim-signed
        [ -f $MOK ] || update-secureboot-policy --new-key
        mokutil --import $MOK        # придумать пароль на один раз
        reboot                       # синий экран MOK Manager: Enroll MOK → Continue → пароль
      и после перезагрузки запустить установку снова.
EOF
        exit 1
    fi
    ok "Secure Boot: ключ dkms зарегистрирован"
fi

# ── Пакеты ────────────────────────────────────────────────────────────────
# Заголовки — и под будущие ядра: иначе после обновления ядра dkms не пересоберёт
# dummy_hcd, и модемы не поднимутся (vmodem соберёт сам, но нужен интернет).
export DEBIAN_FRONTEND=noninteractive
pkgs=(curl ca-certificates python3 dkms "linux-headers-$(uname -r)")
while read -r meta; do
    flavour=${meta#linux-image-}
    [[ $flavour == extra-* || $flavour == unsigned-* ]] && continue
    pkgs+=("linux-headers-$flavour")
    [[ $flavour == virtual* ]] && pkgs+=("linux-image-extra-${flavour}")   # модули USB-гаджетов
done < <(dpkg-query -W -f='${Package} ${db:Status-Status}\n' 'linux-image-*' 2>/dev/null |
         awk '$2 == "installed" && $1 !~ /-[0-9]+\.[0-9]+\.[0-9]+-/ {print $1}')
apt-get -o DPkg::Lock::Timeout=600 -qq update >/dev/null || warn "apt-get update с ошибками — пробую дальше"
avail=()
for p in "${pkgs[@]}"; do
    apt-cache show "$p" >/dev/null 2>&1 && avail+=("$p") || warn "нет пакета $p — пропускаю"
done
apt-get -o DPkg::Lock::Timeout=600 -y -qq install "${avail[@]}" >/dev/null || die "не поставились пакеты: ${avail[*]}"
ok "пакеты: ${avail[*]}"

# ── Агент ─────────────────────────────────────────────────────────────────
tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT
if [[ -z "$src" ]]; then
    curl -fsSL --retry 3 "https://codeload.github.com/${PCS_REPO}/tar.gz/refs/heads/${PCS_BRANCH}" -o "$tmp/pcs.tgz" \
        || die "не скачался ${PCS_REPO}@${PCS_BRANCH}"
    mkdir -p "$tmp/src"
    tar -xzf "$tmp/pcs.tgz" -C "$tmp/src" --strip-components=1
    src="$tmp/src"
fi
for f in vmodem vmodem-api; do
    python3 -c 'import ast, sys; ast.parse(open(sys.argv[1], encoding="utf-8").read())' "$src/agent/$f" \
        || die "agent/$f не разбирается — не ставлю"
done
install -m 0755 "$src/agent/vmodem" /usr/local/sbin/vmodem
install -m 0755 "$src/agent/vmodem-api" /usr/local/sbin/vmodem-api
ok "агент vmodem $(vmodem version) → /usr/local/sbin"

vmodem setup ${slots:+--slots "$slots"}

if [[ -n "$sheet" ]]; then
    vmodem source "$sheet"
    vmodem sync
fi

if [[ -n "$mpspace" ]]; then
    [[ -n "$noreboot" ]] || warn "после mobileproxy.space сервер перезагрузится; модемы поднимутся сами"
    vmodem mpspace install ${auth:+--auth "$auth"} ${proxy:+--proxy "$proxy"} ${noreboot:+--no-reboot}
    exit 0
fi

echo
[[ -n "$sheet" ]] || ok "дальше: vmodem source <ссылка на Google-таблицу> && vmodem sync"
ok "состояние: vmodem status · что умеет: vmodem --help · обновить: запустить этот скрипт снова"
