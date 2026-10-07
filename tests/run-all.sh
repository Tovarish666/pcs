#!/usr/bin/env bash
# Всё, что проверяется без root, без ВМ и без сети (кроме скачивания sing-box для
# проверки конфигов — без сети эти проверки пропускаются, а не падают).
# Каждый tests/test_*.py подхватывается сам: печатает «итого: ok N, fail M», код 0/1.
set -u
cd "$(dirname "$0")/.." || exit 1
rc=0
echo "── синтаксис ──"
while IFS= read -r f; do
    python3 -m py_compile "$f" || { echo "  FAIL $f"; rc=1; }
done < <(find pcs bin tests -type f \( -name '*.py' -o -path 'bin/*' \) | sort)
for f in install.sh; do bash -n "$f" || { echo "  FAIL $f"; rc=1; }; done
[[ $rc -eq 0 ]] && echo "  ok   всё компилируется"
for t in tests/test_*.py; do
    echo; echo "── ${t#tests/} ──"
    python3 "$t" || rc=1
done
echo
[[ $rc -eq 0 ]] && echo "всё зелёное" || echo "есть падения"
exit $rc
