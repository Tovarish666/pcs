# shellcheck shell=bash
# ═══════════════════════════════════════════════════════════════════════════
#  lib/ssh.sh — SSH к ВМ по логину/паролю, без ключей.
#
#  Пароль отдаётся через SSH_ASKPASS_REQUIRE=force — штатный механизм
#  OpenSSH ≥ 8.4 (Proxmox 7 = 8.4, Proxmox 8 = 9.2), сторонних пакетов не надо.
#  sshpass — только запасной путь на старых хостах.
#
#  Использует VM_IP, VM_SSH_PORT, VM_PASSWORD из lib/state.sh.
# ═══════════════════════════════════════════════════════════════════════════
[[ -n "${_PCS_SSH_LOADED:-}" ]] && return 0
_PCS_SSH_LOADED=1

SSH_METHOD=""; SSH_VER=""; _PCS_ASKPASS=""

# UserKnownHostsFile=/dev/null обязателен: пересозданная ВМ получает тот же IP
# с новым host key, и StrictHostKeyChecking=no от этого не спасает.
SSH_OPTS=(
    -o StrictHostKeyChecking=no
    -o UserKnownHostsFile=/dev/null
    -o GlobalKnownHostsFile=/dev/null
    -o LogLevel=ERROR
    -o ConnectTimeout=10
    -o ServerAliveInterval=15
    -o ServerAliveCountMax=4
    -o PreferredAuthentications=password,keyboard-interactive
    -o PubkeyAuthentication=no
    -o NumberOfPasswordPrompts=1
)

ssh_setup() {
    [[ -n "$SSH_METHOD" ]] && return 0
    # Сам пароль в файле не лежит — askpass читает его из окружения ssh.
    [[ -n "${PCS_TMP:-}" ]] || pcs_tmpdir
    _PCS_ASKPASS="${PCS_TMP}/askpass"
    printf '#!/bin/sh\nprintf "%%s\\n" "$PCS_SSH_PASS"\n' >"$_PCS_ASKPASS"
    chmod 700 "$_PCS_ASKPASS"
    local major minor
    SSH_VER="$(ssh -V 2>&1 | sed -n 's/^OpenSSH_\([0-9]\+\.[0-9]\+\).*/\1/p')"
    major="${SSH_VER%%.*}"; minor="${SSH_VER##*.}"
    [[ "$major" =~ ^[0-9]+$ ]] || major=0
    [[ "$minor" =~ ^[0-9]+$ ]] || minor=0
    if (( major > 8 || (major == 8 && minor >= 4) )); then
        SSH_METHOD="askpass"
    elif command -v sshpass >/dev/null 2>&1; then
        SSH_METHOD="sshpass"
    else
        die "OpenSSH ${SSH_VER:-?} старее 8.4 и нет sshpass: apt-get install -y sshpass"
    fi
}

_ssh_run() {                       # _ssh_run <ssh|scp> аргументы...
    local bin="$1"; shift
    ssh_setup
    case "$SSH_METHOD" in
        askpass)
            PCS_SSH_PASS="${VM_PASSWORD:-}" SSH_ASKPASS="$_PCS_ASKPASS" \
            SSH_ASKPASS_REQUIRE=force DISPLAY="${DISPLAY:-:0}" \
                "$bin" "$@" ;;
        sshpass)
            SSHPASS="${VM_PASSWORD:-}" sshpass -e "$bin" "$@" ;;
    esac
}

vm_ssh() {                         # vm_ssh "команда"
    _ssh_run ssh "${SSH_OPTS[@]}" -p "${VM_SSH_PORT:-22}" "root@${VM_IP}" "$@" </dev/null
}
vm_ssh_in() {                      # vm_ssh_in "команда" — stdin уходит на ВМ
    _ssh_run ssh "${SSH_OPTS[@]}" -p "${VM_SSH_PORT:-22}" "root@${VM_IP}" "$@"
}
vm_scp() {                         # vm_scp <локальный файл> <путь на ВМ>
    _ssh_run scp -q "${SSH_OPTS[@]}" -P "${VM_SSH_PORT:-22}" "$1" "root@${VM_IP}:$2"
}
vm_alive() { [[ -n "${VM_IP:-}" ]] && vm_ssh true 2>/dev/null; }

# Положить утилиту из guest/ на ВМ: vm_push_tool <имя> [каталог на ВМ]
vm_push_tool() {
    local name="$1" dst="${2:-/usr/local/sbin}"
    local src="${PCS_ROOT}/guest/${name}"
    [[ -f "$src" ]] || die "нет ${src}"
    vm_scp "$src" "/tmp/.pcs-${name}" \
        && vm_ssh "install -m 0755 /tmp/.pcs-${name} ${dst}/${name} && rm -f /tmp/.pcs-${name}"
}

wait_ssh() {                       # wait_ssh [сек]
    local limit="${1:-420}" t=0
    step "Ждём SSH на ${VM_IP}:${VM_SSH_PORT:-22}..."
    while (( t < limit )); do
        vm_alive && { ok "SSH отвечает (${t}с)"; return 0; }
        sleep 5; t=$((t + 5))
        (( t % 30 == 0 )) && step "…${t}с"
    done
    return 1
}

# Дождаться, пока SSH пропадёт (ВМ ушла в ребут), затем вернётся.
wait_reboot() {                    # wait_reboot [сек на подъём]
    local t=0
    while (( t < 90 )) && vm_alive; do sleep 3; t=$((t + 3)); done
    wait_ssh "${1:-300}"
}

# ═══════════════════════════════════════════════════════════════════════════
#  vm_run_detached <имя> <локальный скрипт> [таймаут, сек]
#
#  Долгие установки (apt upgrade, скрипты mp.space) трогают сеть и способны
#  порвать SSH-сессию — тогда `ssh ВМ 'bash install.sh'` умирает вместе с
#  установкой. Поэтому скрипт запускается на ВМ отвязанным (setsid nohup),
#  а мы опрашиваем его лог и файл с кодом возврата. Обрыв связи на время
#  опроса не страшен — через 5 секунд спросим снова.
#
#  Полный лог остаётся на ВМ в /root/pcs-run/<имя>.log и копируется в лог PCS.
# ═══════════════════════════════════════════════════════════════════════════
vm_run_detached() {
    local name="$1" script="$2" limit="${3:-2400}"
    local dir="${PCS_RUN_DIR:-/root/pcs-run}" seen=0 t=0 rc="" chunk n fails=0

    vm_ssh "install -d -m 700 ${dir} && rm -f ${dir}/${name}.rc ${dir}/${name}.log" \
        || { bad "ВМ недоступна"; return 1; }
    vm_scp "$script" "${dir}/${name}.sh" || { bad "не удалось скопировать ${name}.sh"; return 1; }
    vm_ssh "cd ${dir} && setsid nohup bash -c 'bash ${dir}/${name}.sh; echo \$? > ${dir}/${name}.rc' \
            >${dir}/${name}.log 2>&1 </dev/null &" \
        || { bad "не удалось запустить ${name}"; return 1; }

    while (( t < limit )); do
        sleep 5; t=$((t + 5))
        if chunk="$(vm_ssh "tail -n +$((seen + 1)) ${dir}/${name}.log 2>/dev/null; \
                            echo; echo \"__PCS_RC=\$(cat ${dir}/${name}.rc 2>/dev/null)\"" 2>/dev/null)"; then
            fails=0
            rc="$(printf '%s\n' "$chunk" | sed -n 's/^__PCS_RC=//p' | tail -1)"
            # Последняя строка может быть недописанной — её покажем в следующий
            # раз целиком. Когда скрипт уже закончил, показываем всё до конца.
            if [[ -n "$rc" ]]; then
                chunk="$(printf '%s\n' "$chunk" | sed '/^__PCS_RC=/d')"
            else
                chunk="$(printf '%s\n' "$chunk" | sed '/^__PCS_RC=/d' | sed '$d')"
            fi
            if [[ -n "$chunk" ]]; then
                n="$(printf '%s\n' "$chunk" | wc -l)"
                seen=$((seen + n))
                printf '%s\n' "$chunk" | relay
            fi
            [[ -n "$rc" ]] && break
        else
            fails=$((fails + 1))
            (( fails == 1 )) && warn "связь с ВМ пропала — жду (установка идёт на ВМ сама)"
        fi
    done

    if [[ -z "$rc" ]]; then
        bad "${name}: не закончился за ${limit}с (лог на ВМ: ${dir}/${name}.log)"
        return 124
    fi
    return "$rc"
}
