#!/usr/bin/env bash
# Проверка guest/mp-auth без ВМ и без root: временный каталог вместо
# /home/nodejs/work, без перезапуска служб и chown.
#   bash tests/test-mp-auth.sh
set -u
cd "$(dirname "$0")/.." || exit 1
MPA="$PWD/guest/mp-auth"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
export MP_WORK="$WORK" MP_AUTH_NO_RESTART=1 MP_AUTH_NO_CHOWN=1
F="$WORK/auth.mp"
pass=0; fail=0

t() {                              # t "название" <ожидаемый код> команда...
    local name="$1" want="$2"; shift 2
    local out rc
    out="$("$@" 2>&1)"; rc=$?
    if [[ "$rc" == "$want" ]]; then pass=$((pass + 1)); printf '  ok   %s\n' "$name"
    else fail=$((fail + 1)); printf '  FAIL %s (код %s, ждали %s)\n%s\n' "$name" "$rc" "$want" "$out"; fi
}
eq() {                             # eq "название" <факт> <ожидание>
    if [[ "$2" == "$3" ]]; then pass=$((pass + 1)); printf '  ok   %s\n' "$1"
    else fail=$((fail + 1)); printf '  FAIL %s\n       было:  %s\n       ждали: %s\n' "$1" "$2" "$3"; fi
}
# Права файла: у GNU это -c, у BSD -f. Порядок важен: на Linux `stat -f`
# не падает, а печатает сведения о файловой системе, и «запасной» вариант
# уже не сработал бы.
perm_of() { stat -c %a "$1" 2>/dev/null || stat -f %Lp "$1" 2>/dev/null; }

field() { python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))[sys.argv[2]])' "$F" "$1"; }

echo "mp-auth:"
t "без порта и без файла — ошибка"      2 bash "$MPA" set 'ABCD1234:EFGH5678'
eq "файл не создан"                     "$([[ -f $F ]] && echo yes || echo no)" "no"

t "JSON из ЛК"                          0 bash "$MPA" set '{"auth":"ABCD1234:EFGH5678","port":1800}'
eq "auth записан"                       "$(field auth)" "ABCD1234:EFGH5678"
eq "port записан числом"                "$(field port)" "1800"
eq "права 600"                          "$(perm_of "$F")" "600"

t "порт строкой → число"                0 bash "$MPA" set '{"auth":"K1K1K1K1:K2K2K2K2","port":"1900"}'
eq "port 1900"                          "$(field port)" "1900"

t "голый ключ — порт остаётся"          0 bash "$MPA" set 'NEWKEY11:NEWKEY22'
eq "auth сменился"                      "$(field auth)" "NEWKEY11:NEWKEY22"
eq "port прежний"                       "$(field port)" "1900"

t "--key + --port"                      0 bash "$MPA" set --key 'KKKK1111:KKKK2222' --port 2000
eq "port 2000"                          "$(field port)" "2000"

t "из stdin"                            0 bash -c "printf '%s' '{\"auth\":\"STDIN111:STDIN222\",\"port\":2100}' | bash '$MPA' set -"
eq "auth из stdin"                      "$(field auth)" "STDIN111:STDIN222"

before="$(cat "$F")"
t "битый JSON — ошибка"                 2 bash "$MPA" set '{"auth": "x", '
t "порт 70000 — ошибка"                 2 bash "$MPA" set '{"auth":"AAAA1111:BBBB2222","port":70000}'
t "пробел в ключе — ошибка"             2 bash "$MPA" set '{"auth":"AAAA 1111","port":1800}'
t "нет auth — ошибка"                   2 bash "$MPA" set '{"port":1800}'
eq "после ошибок файл не тронут"        "$(cat "$F")" "$before"

t "лишние поля сохраняются"             0 bash "$MPA" set '{"auth":"EXTRA111:EXTRA222","port":2200,"note":"x"}'
eq "note на месте"                      "$(field note)" "x"

for i in 1 2 3 4 5 6 7; do bash "$MPA" set "{\"auth\":\"LOOP000${i}:LOOP000${i}\",\"port\":2300}" >/dev/null 2>&1; sleep 1; done
eq "бэкапов не больше 5"                "$(ls "$F".bak.* | wc -l | tr -d ' ')" "5"

out="$(bash "$MPA" show 2>&1)"
eq "show маскирует ключ"                "$(grep -c 'LOOP0007:LOOP0007' <<<"$out")" "0"
out="$(bash "$MPA" show --full 2>&1)"
eq "show --full показывает ключ"        "$(grep -c 'LOOP0007:LOOP0007' <<<"$out")" "1"
t "check на валидном файле"             0 bash "$MPA" check

echo
echo "итого: ok ${pass}, fail ${fail}"
[[ $fail -eq 0 ]]
