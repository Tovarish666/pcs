#!/bin/bash
# ═══════════════════════════════════════════════════════════════════════════
#  bench.sh — сравнить стратегии создания модемов. На живом сервере!
#
#    bash tests/stand/bench.sh <таблица.csv> window:5 all fixed:5:2:60 ...
#
#  Перед каждой стратегией все модемы снимаются. В конце источник таблицы
#  возвращается прежний (vmodem source …).
# ═══════════════════════════════════════════════════════════════════════════
set -u
csv=$1; shift
prev=$(python3 -c 'import json; print(json.load(open("/etc/vmodem/config.json")).get("source", ""))')
systemctl stop vmodem-sync.timer
for strat in "$@"; do
    for n in $(ip netns list | awk '/^vm[0-9]+/{sub(/^vm/, "", $1); print $1}'); do vmodem down "$n" >/dev/null; done
    sleep 5
    ( while :; do cut -d' ' -f1 /proc/loadavg; sleep 2; done ) > /tmp/load.$$ & L=$!
    echo "=== $strat"
    vmodem sync --source "$csv" --strategy "$strat" --force --quiet 2>&1 | grep -E "создание|✗ модем"
    kill $L; echo "  пик load average: $(sort -n /tmp/load.$$ | tail -1) (ядер $(nproc))"; rm -f /tmp/load.$$
done
[ -n "$prev" ] && vmodem source "$prev" >/dev/null
systemctl start vmodem-sync.timer
