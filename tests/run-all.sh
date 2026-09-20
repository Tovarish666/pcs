#!/usr/bin/env bash
# Все проверки, которые идут без ВМ, без Proxmox и без root.
set -u
cd "$(dirname "$0")/.." || exit 1
rc=0

echo "── синтаксис ──"
for f in pcs install.sh lib/*.sh cmd/*.sh guest/mp-auth guest/pcs-fix-dns tests/*.sh; do
    bash -n "$f" || { echo "  FAIL $f"; rc=1; }
done
python3 -c 'import ast,sys; ast.parse(open("guest/vmodem-api", encoding="utf-8").read())' \
    || { echo "  FAIL guest/vmodem-api"; rc=1; }
[[ $rc -eq 0 ]] && echo "  ok   все скрипты разбираются"

echo
echo "── mp-auth ──"
bash tests/test-mp-auth.sh || rc=1

echo
echo "── vmodem-api ──"
python3 tests/test-vmodem-api.py || rc=1

echo
[[ $rc -eq 0 ]] && echo "всё зелёное" || echo "есть падения"
exit $rc
