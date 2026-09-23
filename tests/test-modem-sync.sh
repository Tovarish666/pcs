#!/usr/bin/env bash
# Проверка pcs modem-sync без Proxmox и без root: план (--dry-run) на
# подставном qm. Что команда потом реально делает, проверяется на стенде —
# здесь важно, что она правильно разбирает таблицу и состояние хоста.
#   bash tests/test-modem-sync.sh
set -u
cd "$(dirname "$0")/.." || exit 1
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
pass=0; fail=0

ok_()  { pass=$((pass + 1)); printf '  ok   %s\n' "$1"; }
bad_() { fail=$((fail + 1)); printf '  FAIL %s\n%s\n' "$1" "${2:-}"; }
has() {                            # has "название" <текст> <подстрока>
    case "$2" in *"$3"*) ok_ "$1" ;; *) bad_ "$1" "       нет: $3" ;; esac
}
hasnt() {
    case "$2" in *"$3"*) bad_ "$1" "       нашлось: $3" ;; *) ok_ "$1" ;; esac
}

# ── Подставной Proxmox ─────────────────────────────────────────────────────
STUB="$TMP/stub"; mkdir -p "$STUB" "$TMP/log"
cat >"$STUB/qm" <<'EOF'
#!/bin/sh
# 1111 крутится, 1112 и 1117 выключены, остальных нет
[ "$1" = "status" ] || exit 0
case "$2" in
    1111) echo "status: running"; exit 0 ;;
    1112|1117) echo "status: stopped"; exit 0 ;;
    9000) echo "status: stopped"; exit 0 ;;
    *) exit 1 ;;
esac
EOF
printf '#!/bin/sh\nexit 0\n' >"$STUB/pvesm"
printf '#!/bin/sh\nexit 0\n' >"$STUB/pvesh"
chmod +x "$STUB"/*

# ── Состояние хоста ────────────────────────────────────────────────────────
etc="$TMP/etc/modem"; mkdir -p "$etc"
printf 'TPL_ID=9000\nTPL_VMID_BASE=1000\n' >"$etc/template.conf"
printf 'SHEET_URL=%s\n' "$TMP/table.csv" >"$etc/source.conf"
for pair in "111:1111" "112:1112" "117:1117" "120:1120"; do
    n="${pair%%:*}"; v="${pair##*:}"
    printf 'MDM_N=%s\nMDM_VMID=%s\nMDM_IP=192.168.88.%s\nMDM_PROXY=1.2.3.4:1080\n' "$n" "$v" "$n" >"$etc/${n}.conf"
done

cat >"$TMP/table.csv" <<'EOF'
n,real,proxy,enabled
111,81,1.2.3.4:1080:l:p,1
112,82,1.2.3.4:1080:l:p,1
113,83,1.2.3.4:1080:l:p,1
114,84,1.2.3.4:1080:l:p,1
115,85,1.2.3.4:1080:l:p,1
116,86,1.2.3.4:1080:l:p,1
117,87,1.2.3.4:1080:l:p,0
EOF

# Команда хочет root — здесь его нет и не надо: всё равно --dry-run.
plan() {
    ( export PCS_ETC="$TMP/etc" PCS_LOG_DIR="$TMP/log" PCS_LOG="$TMP/log/pcs.log"
      export PATH="$STUB:$PATH"
      # shellcheck source=../lib/common.sh
      source lib/common.sh
      need_root() { :; }
      source cmd/modem-sync.sh "$@" ) 2>&1
}

echo "modem-sync:"
out="$(plan --dry-run)"
has "создать те, которых нет"        "$out" "создать: 113 114 115 116"
has "запустить выключенную ВМ"       "$out" "запустить: 112"
has "готовые не трогать"             "$out" "уже готовы: 111"
has "enabled=0 — остановить"         "$out" "остановить: 117"
has "лишний модем замечен"           "$out" "нет в таблице: 120"
has "--dry-run ничего не делает"     "$out" "ничего не делаю"
has "подсказка про --prune"          "$out" "--prune"

# --only сужает работу, но не делает всю таблицу «лишней»
out2="$(plan --dry-run --only 113,114)"
has "--only: создать только своих"   "$out2" "создать: 113 114"
hasnt "--only: чужих не трогает"     "$out2" "115"
hasnt "--only: 111 не в плане"       "$out2" "уже готовы"
hasnt "--only: 120 не стал лишним"   "$out2" "нет в таблице"

# Таблица разовая, из аргумента
cp "$TMP/table.csv" "$TMP/other.csv"
printf '118,88,1.2.3.4:1080:l:p,1\n' >>"$TMP/other.csv"
out3="$(plan --dry-run --source "$TMP/other.csv")"
has "--source: своя таблица"         "$out3" "113 114 115 116 118"

# Пустой --only: делать нечего, и это видно
out4="$(plan --dry-run --only 200)"
has "нечего делать — сказано прямо"  "$out4" "нечего делать"

# --help разбирается до всего остального: ни root, ни Proxmox не нужны.
out5="$(bash cmd/modem-sync.sh --help 2>&1)"
has "--help показывает команду"      "$out5" "pcs modem-sync --prune"
hasnt "--help не читает таблицу"     "$out5" "Читаю таблицу"
rc_help=0; bash cmd/modem-sync.sh --help >/dev/null 2>&1 || rc_help=$?
[[ $rc_help -eq 0 ]] && ok_ "--help выходит с нулём" || bad_ "--help выходит с нулём" "       код $rc_help"

echo
echo "итого: ok ${pass}, fail ${fail}"
[[ $fail -eq 0 ]]
