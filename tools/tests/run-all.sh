#!/bin/bash
# Прогнать все стенды ccsync. Ничего на машине не трогают: каждый поднимает
# собственный $HOME и собственный bare-репозиторий во временном каталоге.
set -u
cd "$(dirname "$0")"
total_ok=0; total_fail=0
for stand in private-sessions.sh registries.sh session-keys.sh; do
	echo "══ $stand"
	out=$(bash "./$stand" 2>&1)
	echo "$out" | grep -E "^(ТЕСТ|  [✓✗])" || true
	line=$(echo "$out" | grep "^ИТОГО" || echo "ИТОГО: успешно 0, провалено 1")
	ok=$(echo "$line" | grep -oP 'успешно \K\d+'); fail=$(echo "$line" | grep -oP 'провалено \K\d+')
	total_ok=$((total_ok + ok)); total_fail=$((total_fail + fail))
	echo "   $line"; echo
done
echo "ВСЕГО: успешно $total_ok, провалено $total_fail"
exit $((total_fail > 0))
