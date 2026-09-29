#!/bin/bash
# ═══════════════════════════════════════════════════════════════════════════
#  scen.sh — таблица со сценариями поломок поверх настоящей. На живом сервере!
#
#    bash tests/stand/scen.sh /var/lib/vmodem/last-good.csv > /tmp/scen.csv
#    vmodem sync --source /tmp/scen.csv && vmodem status
#
#  Берёт первую строку настоящей таблицы как образец рабочей прокси и
#  добавляет модемы 41–50 с известными поломками. Поддельные прокси «портал
#  оператора / дыра / нет данных» поднимаются здесь же (tests/stand/chaos-socks.py).
# ═══════════════════════════════════════════════════════════════════════════
set -u
src=${1:-/var/lib/vmodem/last-good.csv}
here=$(dirname "$(readlink -f "$0")")
row=$(awk -F, 'NR > 1 && $3 ~ /:.*:.*:/ {print; exit}' "$src")
real=$(cut -d, -f2 <<<"$row" | tr -d ' ')
px=$(cut -d, -f3 <<<"$row")                  # host:port:login:pass
login=$(cut -d: -f3 <<<"$px")
cut -d: -f4- <<<"$px" > /tmp/chaos.pass
for m in captive:21001 blackhole:21002 refuse:21003; do
    systemctl stop "chaos-${m%%:*}" 2>/dev/null; systemctl reset-failed "chaos-${m%%:*}" 2>/dev/null
    systemd-run -q --unit="chaos-${m%%:*}" python3 "$here/chaos-socks.py" "0.0.0.0:${m##*:}" "${m%%:*}" \
        "$(cut -d: -f1,2 <<<"$px")" "$login" /tmp/chaos.pass
done
me=$(ip -4 route get 1.1.1.1 | sed -n 's/.* src \([0-9.]*\).*/\1/p')
cat "$src"
echo "41,$real,${px%:*}:НЕВЕРНЫЙ"          # неверный пароль
echo "42,$real,$(cut -d: -f1 <<<"$px"):1:$login:x"   # прокси не отвечает
echo "43,250,$px"                           # за прокси нет модема 192.168.250.1
echo "44,$real,$me:21001:any:any"           # SIM не оплачена — портал оператора
echo "45,$real,$me:21002:any:any"           # трафик уходит в никуда
echo "46,$real,$me:21003:any:any"           # нет мобильных данных
echo "47,$real,$px"                         # n=47 дважды
echo "47,$real,$px"
echo "48,$real,$px"                         # 48 и 248 делят таблицу маршрутов
echo "248,$real,$px"
echo "49,$real,$(tr ':' '-' <<<"$px")"      # кривая строка
