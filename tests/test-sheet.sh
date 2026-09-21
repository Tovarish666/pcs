#!/usr/bin/env bash
# Проверка разбора таблицы модемов и приведения ссылки Google-таблицы к CSV.
#   bash tests/test-sheet.sh
set -u
cd "$(dirname "$0")/.." || exit 1
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
export PCS_ETC="$TMP/etc" PCS_LOG_DIR="$TMP/log" PCS_LOG="$TMP/log/pcs.log" NO_COLOR=1
mkdir -p "$TMP/log"
pass=0; fail=0

ok_()  { pass=$((pass + 1)); printf '  ok   %s\n' "$1"; }
bad_() { fail=$((fail + 1)); printf '  FAIL %s\n%s\n' "$1" "${2:-}"; }
eq()   { [[ "$2" == "$3" ]] && ok_ "$1" || bad_ "$1" "       было:  $2
       ждали: $3"; }
has()  { case "$2" in *"$3"*) ok_ "$1" ;; *) bad_ "$1" "       нет: $3" ;; esac; }

parse() { python3 lib/sheet-parse.py; }          # stdin → строки, stderr отдельно

echo "разбор таблицы:"

# ── Как в живой таблице ────────────────────────────────────────────────────
out="$(printf 'n,real,proxy\n201,1,188.134.88.13:12000:modem1:pass1\n202,2,188.134.88.13:12002:modem2:pass2\n' | parse 2>"$TMP/err")"
eq "строк разобрано" "$(printf '%s\n' "$out" | grep -c .)" "2"
eq "первая строка" "$(head -1 <<<"$out")" "$(printf '201\t1\t188.134.88.13\t12000\tmodem1\tpass1\t1')"

# ── BOM, CRLF и лишние пробелы в шапке ─────────────────────────────────────
out="$(printf '\xef\xbb\xbfN , Real , Proxy \r\n 201 , 1 , 1.2.3.4:15000:log:pw \r\n' | parse 2>"$TMP/err")"
eq "BOM и CRLF не мешают" "$(head -1 <<<"$out")" "$(printf '201\t1\t1.2.3.4\t15000\tlog\tpw\t1')"

# ── Формы записи real ──────────────────────────────────────────────────────
out="$(printf 'n,real,proxy\n10,192.168.101.1,h:1:l:p\n11,192.168.102.100,h:1:l:p\n12,103,h:1:l:p\n13,,h:1:l:p\n' | parse 2>"$TMP/err")"
eq "real как адрес веб-морды"  "$(sed -n 1p <<<"$out" | cut -f2)" "101"
eq "real как адрес мини-сервера" "$(sed -n 2p <<<"$out" | cut -f2)" "102"
eq "real числом"               "$(sed -n 3p <<<"$out" | cut -f2)" "103"
eq "пустой real = n"           "$(sed -n 4p <<<"$out" | cut -f2)" "13"

# ── Отдельные колонки и пароль с двоеточием ────────────────────────────────
out="$(printf 'n,proxy_host,proxy_port,login,password\n5,1.2.3.4,1080,user,p:a:ss\n' | parse 2>"$TMP/err")"
eq "отдельные колонки" "$(head -1 <<<"$out")" "$(printf '5\t5\t1.2.3.4\t1080\tuser\tp:a:ss')"$'\t1'
out="$(printf 'n,proxy\n6,1.2.3.4:1080:user:p:a:ss\n' | parse 2>"$TMP/err")"
eq "двоеточия в пароле одной строкой" "$(head -1 <<<"$out" | cut -f6)" "p:a:ss"

# ── enabled ────────────────────────────────────────────────────────────────
out="$(printf 'n,proxy,enabled\n7,h:1:l:p,0\n8,h:1:l:p,1\n9,h:1:l:p,нет\n' | parse 2>"$TMP/err")"
eq "enabled=0 выключает"   "$(sed -n 1p <<<"$out" | cut -f7)" "0"
eq "enabled=1 включает"    "$(sed -n 2p <<<"$out" | cut -f7)" "1"
eq "enabled=нет выключает" "$(sed -n 3p <<<"$out" | cut -f7)" "0"

# ── Кривые строки не роняют таблицу ────────────────────────────────────────
out="$(printf 'n,proxy\n300,h:1:l:p\n20,h:abc:l:p\n21,нехватает\n22,:1:l:p\n23,h:1:l:p\n' | parse 2>"$TMP/err")"
eq "годная строка осталась" "$(printf '%s\n' "$out" | grep -c .)" "1"
err="$(cat "$TMP/err")"
has "n вне диапазона назван"  "$err" "n 300 вне"
has "порт назван"             "$err" "порт прокси"
has "формат прокси назван"    "$err" "host:port:login:pass"

# ── Дубли ──────────────────────────────────────────────────────────────────
out="$(printf 'n,proxy\n30,first:1:l:p\n30,second:2:l:p\n' | parse 2>"$TMP/err")"
eq "дубль n: берём первый" "$(head -1 <<<"$out" | cut -f3)" "first"
has "про дубль сказано" "$(cat "$TMP/err")" "уже был"

# ── Грабли сервера mp.space ────────────────────────────────────────────────
printf 'n,proxy\n154,h:1:l:p\n' | parse >/dev/null 2>"$TMP/err"
has "154 — системная таблица маршрутов" "$(cat "$TMP/err")" "системная таблица"
printf 'n,proxy\n1,h:1:l:p\n201,h:1:l:p\n' | parse >/dev/null 2>"$TMP/err"
has "N и N+200 делят таблицу" "$(cat "$TMP/err")" "делят одну таблицу"

# ── Совсем не та таблица ───────────────────────────────────────────────────
printf 'что,то,своё\n1,2,3\n' | parse >/dev/null 2>"$TMP/err"; rc=$?
[[ $rc -eq 1 ]] && ok_ "без шапки — честная ошибка" || bad_ "без шапки — честная ошибка" "       код $rc"
has "сказано, каких колонок ждём" "$(cat "$TMP/err")" "n и proxy"

echo
echo "ссылка на таблицу:"
norm() { bash -c 'source lib/common.sh; source lib/modem.sh; source lib/sheet.sh; sheet_url_normalize "$1"' _ "$1"; }
eq "адресная строка → CSV" \
   "$(norm 'https://docs.google.com/spreadsheets/d/ABC123_x/edit#gid=456')" \
   "https://docs.google.com/spreadsheets/d/ABC123_x/export?format=csv&gid=456"
eq "gid из параметра" \
   "$(norm 'https://docs.google.com/spreadsheets/d/ABC123_x/edit?gid=789#gid=789')" \
   "https://docs.google.com/spreadsheets/d/ABC123_x/export?format=csv&gid=789"
eq "без gid — первый лист" \
   "$(norm 'https://docs.google.com/spreadsheets/d/ABC123_x/edit')" \
   "https://docs.google.com/spreadsheets/d/ABC123_x/export?format=csv&gid=0"
eq "опубликованная таблица" \
   "$(norm 'https://docs.google.com/spreadsheets/d/e/2PACX-1vABC/pubhtml?gid=12')" \
   "https://docs.google.com/spreadsheets/d/e/2PACX-1vABC/pub?gid=12&single=true&output=csv"
eq "готовый CSV не трогаем" \
   "$(norm 'https://docs.google.com/spreadsheets/d/e/2PACX-1vABC/pub?output=csv')" \
   "https://docs.google.com/spreadsheets/d/e/2PACX-1vABC/pub?output=csv"
eq "чужой адрес не трогаем" \
   "$(norm 'https://example.com/modems.csv')" "https://example.com/modems.csv"
eq "локальный файл не трогаем" "$(norm '/tmp/modems.csv')" "/tmp/modems.csv"

# ── HTML вместо таблицы ────────────────────────────────────────────────────
printf '<!DOCTYPE html><html>нет доступа</html>\n' >"$TMP/notcsv.html"
out="$(bash -c 'source lib/common.sh; source lib/modem.sh; source lib/sheet.sh; sheet_fetch "$1" "$2"' _ "$TMP/notcsv.html" "$TMP/out.csv" 2>&1)"; rc=$?
[[ $rc -ne 0 ]] && ok_ "HTML вместо таблицы отбит" || bad_ "HTML вместо таблицы отбит"
has "подсказка про доступ" "$out" "нет доступа по ссылке"

# ── Локальный файл как источник ────────────────────────────────────────────
printf 'n,real,proxy\n201,1,1.2.3.4:15000:l:p\n' >"$TMP/modems.csv"
out="$(bash -c 'source lib/common.sh; source lib/modem.sh; source lib/sheet.sh; sheet_rows "$1"' _ "$TMP/modems.csv" 2>/dev/null)"
eq "файл читается как таблица" "$(cut -f1 <<<"$out")" "201"

echo
echo "итого: ok ${pass}, fail ${fail}"
[[ $fail -eq 0 ]]
