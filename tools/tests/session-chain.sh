#!/bin/bash
# Стенд: цепочка записей внутри транскрипта.
#
# Claude Code собирает разговор обратным обходом parentUuid от последней записи,
# а не порядком строк в файле. Отсюда неочевидное следствие: транскрипт может
# быть целым — все записи на месте, JSON валиден — и при этом открываться не тем
# разговором. Так бывает после git-склейки двух машин, продолживших одну сессию
# врозь: merge=union сохраняет обе ветки, но в контекст попадает одна.
#
# Проверяем, что движок это различает и не делает вид, будто всё в порядке.
set -u
BASE=${TMPDIR:-/tmp}/ccsync-chain
SRC=$(cd "$(dirname "$0")/../../bin" && pwd)
rm -rf "$BASE"; mkdir -p "$BASE"; cd "$BASE"

ok=0; fail=0
check() {
	if [ "$2" = "$3" ]; then echo "  ✓ $1"; ok=$((ok+1))
	else echo "  ✗ $1 — ожидали [$2], получили [$3]"; fail=$((fail+1)); fi
}

# Транскрипты: общий корень, потом две машины продолжают сессию независимо.
# Кодовые слова — чтобы было видно, какая ветка доедет до контекста.
python3 - "$BASE" <<'PY'
import json, sys, pathlib
base = pathlib.Path(sys.argv[1])

def rec(uuid, parent, text, ts):
    return json.dumps({"type": "user", "uuid": uuid, "parentUuid": parent,
                       "timestamp": ts, "cwd": "/home/alex/projects/app",
                       "message": {"role": "user", "content": text}},
                      ensure_ascii=False, separators=(",", ":"))

common = [rec("u1", None, "начало разговора", "2026-09-01T10:00:00.000Z"),
          rec("u2", "u1", "общий ход", "2026-09-01T10:01:00.000Z")]
desktop = common + [rec("d1", "u2", "ветка десктопа", "2026-09-01T11:00:00.000Z"),
                    rec("d2", "d1", "кодовое слово ЖЕЛУДЬ", "2026-09-01T11:01:00.000Z")]
laptop  = common + [rec("l1", "u2", "ветка ноутбука", "2026-09-01T12:00:00.000Z"),
                    rec("l2", "l1", "кодовое слово ЯКОРЬ", "2026-09-01T12:01:00.000Z")]

(base / "desktop.jsonl").write_text("\n".join(desktop) + "\n", encoding="utf-8")
(base / "laptop.jsonl").write_text("\n".join(laptop) + "\n", encoding="utf-8")
# Склейка ровно как её делает git с merge=union: строки обеих сторон подряд.
merged = desktop + [l for l in laptop if l not in desktop]
(base / "merged.jsonl").write_text("\n".join(merged) + "\n", encoding="utf-8")
PY

chain() {   # файл, что посчитать
	python3 -c "
import sys; sys.path.insert(0, '$SRC')
from pathlib import Path
from ccsync_lib.sessions import read_chain
c = read_chain(Path('$1'))
print($2)
"
}

echo "ТЕСТ 1 — обычный транскрипт: одна линия, ветвлений нет"
check "все записи достижимы" "4" "$(chain "$BASE/desktop.jsonl" "len(c.reachable)")"
check "ветвлений нет" "0" "$(chain "$BASE/desktop.jsonl" "len(c.forks)")"

echo "ТЕСТ 2 — склеенный транскрипт: записи целы, но читается одна ветка"
check "записей в файле" "6" "$(chain "$BASE/merged.jsonl" "len(c.parents)")"
check "ветвление обнаружено" "1" "$(chain "$BASE/merged.jsonl" "len(c.forks)")"
check "в контекст попадает только 4 записи" "4" "$(chain "$BASE/merged.jsonl" "len(c.reachable)")"
check "доезжает ветка ноутбука (ЯКОРЬ)" "True" "$(chain "$BASE/merged.jsonl" "'l2' in c.reachable")"
check "ветка десктопа (ЖЕЛУДЬ) недостижима" "False" "$(chain "$BASE/merged.jsonl" "'d2' in c.reachable")"

echo "ТЕСТ 3 — копию с читаемой веткой не удаляем ради склеенной"
# Раскладка: в одном каталоге проекта лежит склеенный файл (его «оставляем»),
# в другом — копия с целой веткой десктопа. По множеству uuid копия полностью
# входит в склеенный, и прежняя проверка сочла бы её безопасной для удаления.
mkdir -p "$BASE/projects/-home-alex-projects-app" "$BASE/projects/-home-alex-old"
sess=3f2a9c1e-0b47-4d8a-9e15-7c2b6a4f8d03
cp "$BASE/merged.jsonl"  "$BASE/projects/-home-alex-projects-app/$sess.jsonl"
cp "$BASE/desktop.jsonl" "$BASE/projects/-home-alex-old/$sess.jsonl"
result=$(python3 -c "
import sys; sys.path.insert(0, '$SRC')
from pathlib import Path
from ccsync_lib.sessions import drop_stale_copies
removed, spared = drop_stale_copies(
    Path('$BASE/projects'), '$sess',
    Path('$BASE/projects/-home-alex-projects-app/$sess.jsonl'))
print(f'{len(removed)} {len(spared)}')
")
check "копия сохранена, а не удалена" "0 1" "$result"
check "файл копии на месте" "да" \
	"$([ -e "$BASE/projects/-home-alex-old/$sess.jsonl" ] && echo да || echo нет)"

echo "ТЕСТ 4 — настоящий дубль по-прежнему убирается"
# Тот же случай, но оставляем полный транскрипт, а копия — его усечённая версия.
rm -rf "$BASE/projects2"; mkdir -p "$BASE/projects2/-home-alex-projects-app" "$BASE/projects2/-home-alex-old"
cp "$BASE/desktop.jsonl" "$BASE/projects2/-home-alex-projects-app/$sess.jsonl"
head -2 "$BASE/desktop.jsonl" > "$BASE/projects2/-home-alex-old/$sess.jsonl"
result=$(python3 -c "
import sys; sys.path.insert(0, '$SRC')
from pathlib import Path
from ccsync_lib.sessions import drop_stale_copies
removed, spared = drop_stale_copies(
    Path('$BASE/projects2'), '$sess',
    Path('$BASE/projects2/-home-alex-projects-app/$sess.jsonl'))
print(f'{len(removed)} {len(spared)}')
")
check "оборванная копия удалена" "1 0" "$result"

echo
echo "ИТОГО: успешно $ok, провалено $fail"
exit $fail
