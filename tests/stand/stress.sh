#!/bin/bash
# ═══════════════════════════════════════════════════════════════════════════
#  stress.sh — трафик через все модемы и по кругу поломки. На живом сервере!
#
#    systemd-run --unit=stress bash tests/stand/stress.sh 900 [url]
#
#  Через каждый модем 8 закачек подряд, а модемы по очереди: пересоздаются
#  (down + sync), переподключаются по USB, у них падает sing-box. Каждый круг
#  печатает, сколько соединений к прокси и сколько трафика ушло в модемы.
#  Упало ядро — сервер перезагрузится сам (kernel.panic), boot_id сменится:
#  сравнить с /root/stress.boot. Текст паники — в последовательной консоли ВМ.
# ═══════════════════════════════════════════════════════════════════════════
dur=${1:-600}
url=${2:-http://cachefly.cachefly.net/10mb.test}
end=$((SECONDS + dur))
sysctl -qw kernel.panic=10 kernel.panic_on_oops=1
cat /proc/sys/kernel/random/boot_id > /root/stress.boot
mods=$(ip netns list | awk '/^vm[0-9]+/{sub(/^vm/, "", $1); print $1}' | sort -n)
ports=$(sed -n 's/^SOCKS=.*:\([0-9]*\)$/\1/p' /etc/vmodem/m/*/env | sort -u | paste -sd'|' -)
load() {    # $1 — октет модема; адрес сервера на нём 192.168.$1.100
    while [ $SECONDS -lt $end ]; do
        curl -s -m 20 -o /dev/null --interface "192.168.$1.100" "$url" || sleep 1
    done
}
for n in $mods; do for k in $(seq 1 8); do load "$n" & done; done
cycle=0
while [ $SECONDS -lt $end ]; do
    cycle=$((cycle + 1))
    for n in $mods; do
        case $((cycle % 3)) in
            0) vmodem down "$n" >/dev/null 2>&1; vmodem sync --quiet >/dev/null 2>&1 ;;
            1) vmodem replug "$n" >/dev/null 2>&1 ;;
            2) systemctl kill --kill-whom=main -s KILL "vmodem-sb@$n" 2>/dev/null ;;
        esac
        sleep 3
    done
    tx=0
    for n in $mods; do
        b=$(ip netns exec "vm$n" cat /sys/class/net/usb0/statistics/tx_bytes 2>/dev/null)
        tx=$((tx + ${b:-0}))
    done
    conn=$(ss -tn state established | grep -cE ":($ports)\b")
    echo "круг $cycle $(date +%T) модемов $(ip netns list | grep -c '^vm') к прокси $conn в модемы $((tx / 1048576)) МБ"
done
wait
echo "готово: $cycle кругов"
