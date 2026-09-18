#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════════════════════════
#  pcs mp-install — софт mobileproxy.space на ВМ.
#
#    pcs mp-install [--vm ID] [--auth '<json>' | --auth-file F]
#                   [--skip-modems] [--no-reboot]
#
#  По шагам:
#    1. утилиты PCS на ВМ (mp-auth, pcs-fix-dns)
#    2. apt: выключить авто-обновления; модули ядра для USB-модемов
#    3. install.sh от mp.space — nodejs-server, mproxy, monit
#    4. auth.mp — если передан или введён (Enter — позже: pcs mp-auth set)
#    5. setup-modem-management.sh от mp.space — маршрутизация модемов
#    6. перезагрузка, DNS заново, проверка служб
#
#  Скрипты mp.space качаются и выполняются на самой ВМ, отвязанно от SSH:
#  они трогают сеть и могут порвать сессию — установка при этом не умирает.
#  Повторный запуск безопасен: install.sh не трогает уже заданный auth.mp.
# ═══════════════════════════════════════════════════════════════════════════
set -uo pipefail
# shellcheck source=../lib/common.sh
source "$(dirname "$(readlink -f "$0")")/../lib/common.sh"
source "$PCS_ROOT/lib/state.sh"
source "$PCS_ROOT/lib/ssh.sh"

MP_DOWNLOADS="${MP_DOWNLOADS:-https://mobileproxy.space/downloads/sp}"

usage() { pcs_usage "$0"; }

O_VM=""; O_AUTH=""; O_SKIP_MODEMS=""; O_NO_REBOOT=""
while (( $# )); do
    case "$1" in
        --vm)          O_VM="$2"; shift 2 ;;
        --auth)        O_AUTH="$2"; shift 2 ;;
        --auth-file)   [[ -r "$2" ]] || die "не читается: $2"; O_AUTH="$(cat "$2")"; shift 2 ;;
        --skip-modems) O_SKIP_MODEMS=1; shift ;;
        --no-reboot)   O_NO_REBOOT=1; shift ;;
        -h|--help)     usage; exit 0 ;;
        *) die "неизвестный аргумент: $1 (pcs mp-install --help)" ;;
    esac
done

pcs_begin mp-install
pcs_tmpdir
ssh_setup
state_need_vm "$O_VM"
vm_alive || die "ВМ ${VM_ID} (${VM_IP}) недоступна по SSH"
info "ВМ ${VM_ID} (${VM_NAME:-?}) @ ${VM_IP}   лог: ${PCS_LOG}"

if [[ -z "$O_AUTH" ]] && has_tty; then
    info "auth.mp: ЛК → Мой прокси-бизнес → Сервера → иконка ↓ у нужного сервера"
    info "Формат: {\"auth\":\"KEY:KEY\",\"port\":1800}. Можно задать позже: pcs mp-auth set"
    ask_opt O_AUTH "Содержимое auth.mp"
fi

# Скрипт для запуска на ВМ: качает URL и выполняет. Скачавшийся HTML
# вместо скрипта (404, заглушка провайдера) не исполняется.
remote_fetch_run() {               # remote_fetch_run <url> <файл-результат>
    cat >"$2" <<EOF
set -u
export DEBIAN_FRONTEND=noninteractive
cd /root/pcs-run
f="\$(basename "$1")"
echo "качаю $1"
wget -q --timeout=60 --tries=3 -O "\$f" "$1" || { echo "не скачался $1"; exit 10; }
head -1 "\$f" | grep -q '^#!' || { echo "скачалось не то:"; head -3 "\$f"; exit 11; }
bash "\$f"
EOF
}

# ── 1. утилиты PCS ─────────────────────────────────────────────────────────
hdr "1/6 Утилиты PCS на ВМ"
vm_push_tool mp-auth     || die "не доставлен mp-auth"
vm_push_tool pcs-fix-dns || die "не доставлен pcs-fix-dns"
vm_ssh "test -f /etc/default/pcs-dns || printf 'PCS_DNS1=%s\nPCS_DNS2=%s\n' '${VM_DNS1}' '${VM_DNS2}' > /etc/default/pcs-dns"
ok "mp-auth, pcs-fix-dns → /usr/local/sbin"

# ── 2. модули ядра ─────────────────────────────────────────────────────────
# В облачном образе ядро «virtual» без linux-modules-extra — там лежат
# драйверы USB-модемов (cdc_ether и др.) и USB/IP. Мета-пакет
# linux-image-extra-virtual держит их и для будущих ядер.
#
# Заодно глушим авто-обновления (install.sh от mp.space всё равно их выключит,
# но позже): на свежей ВМ unattended-upgrades держит блокировку apt, а
# install.sh ставит пакеты через «|| warn» — и молча остался бы без них.
hdr "2/6 Подготовка apt и модули ядра для USB-модемов"
cat >"$PCS_TMP/kernel.sh" <<'EOF'
set -u
export DEBIAN_FRONTEND=noninteractive
systemctl disable --now unattended-upgrades.service apt-daily.timer apt-daily-upgrade.timer >/dev/null 2>&1
echo "авто-обновления apt выключены"
APT="apt-get -o DPkg::Lock::Timeout=600 -y -qq"
$APT update
$APT install linux-image-extra-virtual && echo "linux-image-extra-virtual: ok"
if $APT install "linux-modules-extra-$(uname -r)"; then
    echo "linux-modules-extra-$(uname -r): ok"
else
    echo "для текущего ядра $(uname -r) extra нет — появятся после перезагрузки на новое"
fi
exit 0
EOF
vm_run_detached kernel "$PCS_TMP/kernel.sh" 900 || warn "модули ядра не поставились — вернёмся к этому на этапе модемов"

# ── 3. install.sh ──────────────────────────────────────────────────────────
hdr "3/6 install.sh (mp.space)"
remote_fetch_run "${MP_DOWNLOADS}/install.sh" "$PCS_TMP/mp-install.sh"
vm_run_detached mp-install "$PCS_TMP/mp-install.sh" 2400 \
    || die "install.sh завершился с ошибкой (лог на ВМ: /root/pcs-run/mp-install.log)"
ok "install.sh отработал"

# ── 4. auth.mp ─────────────────────────────────────────────────────────────
hdr "4/6 auth.mp"
if [[ -n "$O_AUTH" ]]; then
    printf '%s' "$O_AUTH" | vm_ssh_in "mp-auth set -" 2>&1 | relay
    [[ ${PIPESTATUS[1]} -eq 0 ]] || die "auth.mp не записан — проверь значение и повтори: pcs mp-auth set"
else
    warn "auth.mp не задан: сервер работает со случайным ключом от install.sh"
    info "Задать: pcs mp-auth set"
fi

# ── 5. setup-modem-management.sh ───────────────────────────────────────────
hdr "5/6 setup-modem-management.sh (mp.space)"
if [[ -n "$O_SKIP_MODEMS" ]]; then
    warn "пропущено (--skip-modems)"
else
    remote_fetch_run "${MP_DOWNLOADS}/setup-modem-management.sh" "$PCS_TMP/mp-modems.sh"
    vm_run_detached mp-modems "$PCS_TMP/mp-modems.sh" 1200 \
        || die "setup-modem-management.sh завершился с ошибкой (лог на ВМ: /root/pcs-run/mp-modems.log)"
    ok "setup-modem-management.sh отработал"
fi

# ── 6. перезагрузка и проверка ─────────────────────────────────────────────
hdr "6/6 Перезагрузка и проверка"
if [[ -n "$O_NO_REBOOT" ]]; then
    warn "перезагрузка пропущена (--no-reboot): GRUB/initramfs от mp.space применятся при следующей"
else
    step "Перезагружаю ВМ..."
    vm_ssh "(sleep 1; systemctl reboot) >/dev/null 2>&1 &" || true
    wait_reboot 300 || die "ВМ не поднялась после перезагрузки (qm terminal ${VM_ID})"
fi

# Софт mp.space трогает сеть и resolv.conf — DNS приводим в порядок заново.
vm_ssh "/usr/local/sbin/pcs-fix-dns" 2>&1 | relay
[[ ${PIPESTATUS[0]} -eq 0 ]] && ok "DNS в порядке" || warn "DNS требует внимания: pcs vm-fix-dns"

problems=0
for s in nodejs-server mproxy monit; do
    st="$(vm_ssh "systemctl is-active $s" 2>/dev/null)"
    if [[ "$st" == "active" ]]; then ok "служба $s: active"; else bad "служба $s: ${st:-нет}"; problems=$((problems + 1)); fi
done
if vm_ssh "curl -s -m 5 http://127.0.0.1:48372/health" 2>/dev/null | grep -q .; then
    ok "mproxy API :48372 отвечает"
else
    warn "mproxy API :48372 не ответил"
fi
vm_ssh "mp-auth check" 2>&1 | relay
[[ ${PIPESTATUS[0]} -eq 0 ]] || problems=$((problems + 1))

wan="$(vm_ssh "curl -s -4 -m 8 'http://ip-api.com/line/?fields=query'" 2>/dev/null | head -1)"
port="$(vm_ssh "jq -r '.port // empty' /home/nodejs/work/auth.mp" 2>/dev/null)"

echo >&2
printf '  %sДля ЛК mobileproxy.space%s  (Мой прокси-бизнес → Сервера → ✏)\n' "$B" "$R" >&2
printf '    Статический IP : %s\n' "${wan:-?}" >&2
printf '    LocalIP        : %s\n' "$VM_IP" >&2
printf '    Root login     : root, пароль — заданный при создании ВМ\n' >&2
printf '    SSH порт       : %s\n' "${VM_SSH_PORT:-22}" >&2
printf '    Порт сервера   : %s\n' "${port:-?}" >&2
printf '    OS             : Unix\n' >&2
echo >&2
if (( problems == 0 )); then
    ok "mobileproxy.space установлен на ВМ ${VM_ID}"
else
    bad "установка закончилась с проблемами: ${problems} — см. выше"
    exit 1
fi
