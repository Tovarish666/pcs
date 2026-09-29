#!/bin/bash
# ═══════════════════════════════════════════════════════════════════════════
#  ops.sh — отказы в работе и то, как их разбирает vmodem. На живом сервере!
#
#    bash tests/stand/ops.sh N1 N2 N3 N4
#
#  N1 — у него упадёт туннель; N2 — сломается своя сеть; N3 — сервер потеряет
#  адрес; N4 — пропадёт netns. Плюс: таблица недоступна, таблица на 2 строки.
#  Модемы должны вернуться сами. Трафик модемов на время теста рвётся.
# ═══════════════════════════════════════════════════════════════════════════
set -u
[ $# -eq 4 ] || { echo "нужно 4 номера работающих модемов"; exit 1; }
A=$1 B=$2 Cn=$3 D=$4
ip100() { curl -s -m 10 --interface "192.168.$1.100" https://api.ipify.org; }
sync_() { vmodem sync "$@" 2>&1 | grep -E "план|таблица|✗|⚠ строки|сервер не взял|  [0-9]+: |создание"; }
back() {  # back N сек — ждать, пока через модем снова пойдёт трафик
    local t0; t0=$(date +%s)
    while [ $(( $(date +%s) - t0 )) -lt "$2" ]; do
        [ -n "$(ip100 "$1")" ] && { echo "  ✓ $1: трафик идёт ($(( $(date +%s) - t0 )) с), IP $(ip100 "$1")"; return 0; }
        sleep 1
    done
    echo "  ✗ $1: трафика нет за $2 с"; return 1
}

echo "── упал sing-box модема $A — systemd и ExecStartPost сами"
systemctl kill -s KILL "vmodem-sb@$A"; back "$A" 60

echo "── у модема $B пропал адрес на своей стороне — sync пересоздаёт"
ip -n "vm$B" addr flush dev usb0; sync_; back "$B" 60

echo "── сервер потерял адрес модема $Cn — sync переподключает USB"
E=$(ip -4 -o addr | awk -v a="192.168.$Cn.100/" 'index($4, a) == 1 {print $2}')
ip addr flush dev "$E"; sync_; back "$Cn" 90

echo "── кто-то удалил netns модема $D — sync создаёт заново"
ip netns del "vm$D"; sync_; back "$D" 60

echo "── таблица недоступна — берётся последняя удачная"
sync_ --source "https://docs.google.com/spreadsheets/d/no-such-sheet/edit"

echo "── в таблице осталось две строки — массово не сносит"
head -3 /var/lib/vmodem/last-good.csv > /tmp/two.csv
sync_ --source /tmp/two.csv
echo "  модемов после: $(ip netns list | grep -c '^vm')"
