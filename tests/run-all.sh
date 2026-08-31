#!/bin/bash
# Проверки самой заготовки: шаблон и перевод.
# Движок проверяется отдельно — tools/tests/run-all.sh.
#
# Стенды клонируют ЗАКОММИЧЕННОЕ состояние репозитория, так что незакоммиченную
# правку они не увидят: сначала git commit, потом прогон.
set -u
cd "$(dirname "$0")" || exit 1
fail=0

echo "═══ полнота перевода ═══"
python3 i18n-coverage.py || fail=$((fail+1))

for rig in fresh-start.sh i18n-english.sh; do
	echo
	echo "═══ $rig ═══"
	bash "$rig" || fail=$((fail+1))
done

echo
if [ $fail -eq 0 ]; then echo "ВСЁ ПРОЙДЕНО"; else echo "ПРОВАЛЕНО СТЕНДОВ: $fail"; fi
exit $fail
