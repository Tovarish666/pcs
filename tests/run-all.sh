#!/usr/bin/env bash
# Всё, что проверяется без root, без ВМ и без сети.
set -u
cd "$(dirname "$0")/.." || exit 1
rc=0
echo "── синтаксис ──"
for f in pcs agent/vmodem agent/vmodem-api tests/*.py; do
    python3 -m py_compile "$f" || { echo "  FAIL $f"; rc=1; }
done
bash -n install.sh || rc=1
bash -n vmodem-install.sh || rc=1
[[ $rc -eq 0 ]] && echo "  ok   всё компилируется"
bash vmodem-install.sh --help | grep -q -- "--sheet" && echo "  ok   vmodem-install.sh --help" \
    || { echo "  FAIL vmodem-install.sh --help"; rc=1; }
bash vmodem-install.sh --что-то >/dev/null 2>&1 && { echo "  FAIL vmodem-install.sh принял мусор"; rc=1; } \
    || echo "  ok   vmodem-install.sh: непонятный параметр — отказ"
for t in tests/test_agent.py tests/test_api.py tests/test_pcs.py; do
    echo; echo "── ${t#tests/} ──"
    python3 "$t" || rc=1
done
echo
[[ $rc -eq 0 ]] && echo "всё зелёное" || echo "есть падения"
exit $rc
