#!/usr/bin/env bash
# Проверка guest/vmodem без ВМ и без root: конфиг и режим показа (--dry-run).
# Что реально поднимается в ядре (гаджет, usbip, sing-box), здесь не
# проверить — проверяется, что команды и конфиги собираются правильно.
#   bash tests/test-vmodem.sh
set -u
cd "$(dirname "$0")/.." || exit 1
VM="$PWD/guest/vmodem"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
export VMODEM_ETC="$TMP/etc" VMODEM_RUN="$TMP/run" VMODEM_LOG_DIR="$TMP/log" VMODEM_LIB="$TMP/lib"
PASSWORD='p@ss w"rd$1'
pass=0; fail=0

ok_()  { pass=$((pass + 1)); printf '  ok   %s\n' "$1"; }
bad_() { fail=$((fail + 1)); printf '  FAIL %s\n%s\n' "$1" "${2:-}"; }
has() {                            # has "название" <текст> <подстрока>
    case "$2" in *"$3"*) ok_ "$1" ;; *) bad_ "$1" "       нет: $3" ;; esac
}
hasnt() {
    case "$2" in *"$3"*) bad_ "$1" "       нашлось: $3" ;; *) ok_ "$1" ;; esac
}
# Права файла: у GNU это -c, у BSD -f. Порядок важен: на Linux `stat -f`
# не падает, а печатает сведения о файловой системе, и «запасной» вариант
# уже не сработал бы.
perm_of() { stat -c %a "$1" 2>/dev/null || stat -f %Lp "$1" 2>/dev/null; }

rc_is() {                          # rc_is "название" <ожидаемый код> команда...
    local name="$1" want="$2"; shift 2
    local out; out="$("$@" 2>&1)"; local rc=$?
    if [[ "$rc" == "$want" ]]; then ok_ "$name"; else bad_ "$name" "       код $rc, ждали $want: $out"; fi
}

echo "vmodem:"

# ── Конфиг ─────────────────────────────────────────────────────────────────
rc_is "прокся строкой host:port:login:pass" 0 \
    bash "$VM" config set N=64 REAL=101 "PROXY=1.2.3.4:15000:login1:${PASSWORD}" USBIP_ALLOW=10.0.0.50
conf="$(cat "$TMP/etc/vmodem.conf")"
has "host:port разобран"   "$conf" "PROXY=1.2.3.4:15000"
has "логин разобран"       "$conf" "PROXY_USER=login1"
# В файле пароль экранирован (printf %q) — сверяем то, что читается обратно.
saved="$(bash -c 'source "$1"; printf %s "$PROXY_PASS"' _ "$TMP/etc/vmodem.conf")"
[[ "$saved" == "$PASSWORD" ]] && ok_ "пароль читается обратно как есть" \
    || bad_ "пароль читается обратно как есть" "       было: $saved"
has "серийник придуман"    "$conf" "SERIAL=VMODEM00000064"
perm="$(perm_of "$TMP/etc/vmodem.conf")"
[[ "$perm" == "600" ]] && ok_ "конфиг 600" || bad_ "конфиг 600" "       права $perm"

show="$(bash "$VM" config show 2>&1)"
hasnt "show не печатает пароль" "$show" "$PASSWORD"
has   "show показывает адреса"  "$show" "192.168.64.1"

rc_is "N=154 отбит (таблица маршрутов mp.space)" 1 bash "$VM" config set N=154
rc_is "N=300 отбит"                              1 bash "$VM" config set N=300
rc_is "мусор в PROXY отбит"                      1 bash "$VM" config set PROXY=шляпа
conf2="$(cat "$TMP/etc/vmodem.conf")"
[[ "$conf" == "$conf2" ]] && ok_ "после ошибок конфиг не тронут" || bad_ "после ошибок конфиг не тронут"

# ── Параметры со stdin (так их шлёт PCS: пароль не виден в ps) ─────────────
rc_is "config set - читает stdin" 0 bash -c \
    "printf 'N=65\nREAL=102\nPROXY=5.6.7.8:1080:lg:%s\n' \"\$1\" | bash \"\$2\" config set -" _ "$PASSWORD" "$VM"
conf3="$(cat "$TMP/etc/vmodem.conf")"
has "N со stdin"      "$conf3" "N=65"
has "real со stdin"   "$conf3" "REAL=102"
saved2="$(bash -c 'source "$1"; printf %s "$PROXY_PASS"' _ "$TMP/etc/vmodem.conf")"
[[ "$saved2" == "$PASSWORD" ]] && ok_ "пароль со stdin не искажён" || bad_ "пароль со stdin не искажён" "       было: $saved2"
# вернуть прежние значения для остальных проверок
bash "$VM" config set N=64 REAL=101 "PROXY=1.2.3.4:15000:login1:${PASSWORD}" >/dev/null 2>&1

# ── Что сделает up ─────────────────────────────────────────────────────────
out="$(bash "$VM" up --dry-run 2>&1)"

has "гаджет Huawei по vendor"      "$out" "echo 0x12d1 > "
has "гаджет E3372h по product"     "$out" "echo 0x14dc > "
has "MAC для сервера"              "$out" "0c:5b:8f:27:9a:64"
has "функция ECM"                  "$out" "functions/ecm.usb0"
has "привязка к usbip-vudc"        "$out" "usbip-vudc"
has "адрес модема на usb0"         "$out" "ip addr replace 192.168.64.1/24 dev usb0"
has "правило на подсеть модема"    "$out" "ip rule add from 192.168.64.0/24 lookup 100"
has "маршрут по умолчанию в туннель" "$out" "ip route replace default dev vmtun0 table 100"
has "DNS модема только через туннель" "$out" "ip route replace 192.168.101.1/32 dev vmtun0"
has "NAT в туннель"                "$out" "iptables -t nat -A POSTROUTING -o vmtun0 -j MASQUERADE"
has "MSS clamp"                    "$out" "TCPMSS --clamp-mss-to-pmtu"
has "usbipd отдаёт устройство"     "$out" "usbipd --device"
has "usbip закрыт для чужих"       "$out" "--dport 3240 ! -s 10.0.0.50 -j DROP"
has "sing-box запускается"         "$out" "sing-box run -c"
has "dnsmasq запускается"          "$out" "dnsmasq --keep-in-foreground"
has "vmodem-api запускается"       "$out" "vmodem-api --virt 64 --real 101"
has "пароль отдаётся файлом"       "$out" "--socks-pass-file"
hasnt "пароля в командах нет"      "$out" "$PASSWORD"

# ── Конфиги, которые up собирает ───────────────────────────────────────────
dns="$(cat "$TMP/etc/dnsmasq.conf")"
has "DHCP выдаёт ровно .100"   "$dns" "dhcp-range=192.168.64.100,192.168.64.100"
has "шлюз — сам модем"         "$dns" "dhcp-option=3,192.168.64.1"
has "DNS — сам модем"          "$dns" "dhcp-option=6,192.168.64.1"
has "первым DNS настоящего модема" "$dns" "server=192.168.101.1"
has "слушает только свой адрес" "$dns" "listen-address=192.168.64.1"

sb="$TMP/etc/singbox.json"
if python3 -c 'import json,sys; json.load(open(sys.argv[1]))' "$sb" 2>/dev/null; then
    ok_ "singbox.json — валидный JSON"
else
    bad_ "singbox.json — валидный JSON" "$(cat "$sb" 2>/dev/null | head -5)"
fi
field() { python3 -c 'import json,sys; d=json.load(open(sys.argv[1]));
import functools
cur=d
for k in sys.argv[2].split("."):
    cur = cur[int(k)] if k.isdigit() else cur[k]
print(cur)' "$sb" "$1" 2>/dev/null; }
[[ "$(field outbounds.0.server)" == "1.2.3.4" ]] && ok_ "прокси-хост в sing-box" || bad_ "прокси-хост в sing-box"
[[ "$(field outbounds.0.server_port)" == "15000" ]] && ok_ "порт прокси числом" || bad_ "порт прокси числом"
[[ "$(field outbounds.0.username)" == "login1" ]] && ok_ "логин прокси" || bad_ "логин прокси"
[[ "$(field outbounds.0.password)" == "$PASSWORD" ]] && ok_ "пароль с кавычками не поломал JSON" || bad_ "пароль с кавычками не поломал JSON"
[[ "$(field inbounds.0.address.0)" == "172.20.0.1/30" ]] && ok_ "адрес туннеля" || bad_ "адрес туннеля"
[[ "$(field inbounds.0.sniff)" == "False" ]] && ok_ "подмена адреса на домен выключена" || bad_ "подмена адреса на домен выключена"
[[ "$(field route.final)" == "proxy" ]] && ok_ "весь трафик уходит в прокси" || bad_ "весь трафик уходит в прокси"
perm="$(perm_of "$sb")"
[[ "$perm" == "600" ]] && ok_ "singbox.json 600" || bad_ "singbox.json 600" "       права $perm"

# ── Снятие ─────────────────────────────────────────────────────────────────
out="$(bash "$VM" down --dry-run 2>&1)"
has "снимается правило подсети"  "$out" "ip rule del from 192.168.64.0/24"
has "снимается NAT"              "$out" "iptables -t nat -D POSTROUTING -o vmtun0"
has "отвязывается гаджет"        "$out" "echo  > "

echo
echo "итого: ok ${pass}, fail ${fail}"
[[ $fail -eq 0 ]]
