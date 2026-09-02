#!/bin/bash
# Стенд: цепочка записей внутри транскрипта.
#
# Claude Code собирает разговор обратным обходом parentUuid от последней СТРОКИ
# файла — проверено запуском `claude --resume` на версии 2.1.252: когда в конец
# положена ветка с более ранним временем, читается именно она, а не самая свежая
# по timestamp.
#
# Отсюда две вещи, которые стенд и стережёт. Транскрипт может быть целым и всё
# же открываться другим разговором. И наоборот: недостижимые ветки сами по себе
# ни о чём не говорят — в любой длинной сессии их полно, это брошенные
# продолжения, к которым человек не вернулся. Тревожить можно только когда
# читаемое перестало читаться, да и то не по мелочи.
set -u
BASE=${TMPDIR:-/tmp}/ccsync-chain
SRC=$(cd "$(dirname "$0")/../../bin" && pwd)
rm -rf "$BASE"; mkdir -p "$BASE"; cd "$BASE"

ok=0; fail=0
check() {
	if [ "$2" = "$3" ]; then echo "  ✓ $1"; ok=$((ok+1))
	else echo "  ✗ $1 — ожидали [$2], получили [$3]"; fail=$((fail+1)); fi
}

python3 - "$BASE" <<'PY'
import json, pathlib, sys
base = pathlib.Path(sys.argv[1])

def rec(uid, parent, text, ts):
    return json.dumps({"type": "user", "uuid": uid, "parentUuid": parent,
                       "timestamp": ts, "cwd": "/home/alex/projects/app",
                       "sessionId": "3f2a9c1e-0b47-4d8a-9e15-7c2b6a4f8d03",
                       "message": {"role": "user", "content": text}},
                      ensure_ascii=False, separators=(",", ":"))

common = [rec("u1", None, "начало разговора", "2026-09-01T10:00:00.000Z"),
          rec("u2", "u1", "общий ход", "2026-09-01T10:01:00.000Z")]
# Ветка десктопа — кусок работы, который может пропасть из виду.
desktop = common + [rec(f"d{i}", f"d{i-1}" if i > 1 else "u2",
                        f"десктоп, ход {i}" + (" кодовое слово ЖЕЛУДЬ" if i == 6 else ""),
                        f"2026-09-01T11:0{i}:00.000Z") for i in range(1, 7)]
laptop  = common + [rec("l1", "u2", "ноутбук, ход 1", "2026-09-01T12:00:00.000Z"),
                    rec("l2", "l1", "кодовое слово ЯКОРЬ", "2026-09-01T12:01:00.000Z")]

(base / "desktop.jsonl").write_text("\n".join(desktop) + "\n", encoding="utf-8")
(base / "laptop.jsonl").write_text("\n".join(laptop) + "\n", encoding="utf-8")
# Склейка ровно как её делает git с merge=union.
merged = desktop + [l for l in laptop if l not in desktop]
(base / "merged.jsonl").write_text("\n".join(merged) + "\n", encoding="utf-8")
# Норма: человек вернулся на ход назад и переписал его.
rewound = common + [rec("r1", "u2", "первый вариант", "2026-09-01T11:00:00.000Z"),
                    rec("r2", "u2", "переписал иначе", "2026-09-01T11:05:00.000Z")]
(base / "rewound.jsonl").write_text("\n".join(rewound) + "\n", encoding="utf-8")
PY

py() { python3 -c "
import sys; sys.path.insert(0, '$SRC')
from pathlib import Path
from ccsync_lib import sessions
$1
"; }

echo "ТЕСТ 1 — как читается обычный транскрипт"
check "все записи достижимы" "8" "$(py "print(len(sessions.read_chain(Path('$BASE/desktop.jsonl')).reachable))")"
check "ветвлений нет" "0" "$(py "print(len(sessions.read_chain(Path('$BASE/desktop.jsonl')).forks))")"

echo "ТЕСТ 2 — склейка: записи целы, но читается одна ветка"
check "записей в файле" "10" "$(py "print(len(sessions.read_chain(Path('$BASE/merged.jsonl')).parents))")"
check "в контекст попадает только ветка ноутбука" "4" \
	"$(py "print(len(sessions.read_chain(Path('$BASE/merged.jsonl')).reachable))")"
check "ЯКОРЬ читается" "True" "$(py "print('l2' in sessions.read_chain(Path('$BASE/merged.jsonl')).reachable)")"
check "ЖЕЛУДЬ пропал из виду" "False" "$(py "print('d6' in sessions.read_chain(Path('$BASE/merged.jsonl')).reachable)")"

echo "ТЕСТ 3 — тревожим только по делу"
check "склейка: потеряно больше порога" "True" \
	"$(py "b=Path('$BASE/desktop.jsonl').read_text(); a=Path('$BASE/merged.jsonl').read_text()
print(sessions.lost_after_merge(b, a) >= sessions.MIN_LOST_TO_WARN)")"
check "возврат назад: молчим" "True" \
	"$(py "b='\n'.join(Path('$BASE/rewound.jsonl').read_text().splitlines()[:3])
a=Path('$BASE/rewound.jsonl').read_text()
print(sessions.lost_after_merge(b, a) < sessions.MIN_LOST_TO_WARN)")"
check "ничего не менялось: молчим" "0" \
	"$(py "t=Path('$BASE/desktop.jsonl').read_text(); print(sessions.lost_after_merge(t, t))")"

echo "ТЕСТ 4 — уборка дублей не выбрасывает читаемое"
mkdir -p "$BASE/projects/-home-alex-projects-app" "$BASE/projects/-home-alex-old"
sess=3f2a9c1e-0b47-4d8a-9e15-7c2b6a4f8d03
cp "$BASE/merged.jsonl"  "$BASE/projects/-home-alex-projects-app/$sess.jsonl"
cp "$BASE/desktop.jsonl" "$BASE/projects/-home-alex-old/$sess.jsonl"
check "копия с читаемой веткой сохранена" "0 1" \
	"$(py "r,s = sessions.drop_stale_copies(Path('$BASE/projects'), '$sess',
    Path('$BASE/projects/-home-alex-projects-app/$sess.jsonl'))
print(len(r), len(s))")"

rm -rf "$BASE/p2"; mkdir -p "$BASE/p2/-home-alex-projects-app" "$BASE/p2/-home-alex-old"
cp "$BASE/desktop.jsonl" "$BASE/p2/-home-alex-projects-app/$sess.jsonl"
head -2 "$BASE/desktop.jsonl" > "$BASE/p2/-home-alex-old/$sess.jsonl"
check "оборванный дубль по-прежнему убирается" "1 0" \
	"$(py "r,s = sessions.drop_stale_copies(Path('$BASE/p2'), '$sess',
    Path('$BASE/p2/-home-alex-projects-app/$sess.jsonl'))
print(len(r), len(s))")"

echo "ТЕСТ 5 — split разносит ветки по отдельным сессиям"
mkdir -p "$BASE/split/backup"
cp "$BASE/merged.jsonl" "$BASE/split/$sess.jsonl"
out=$(py "res = sessions.split_transcript(Path('$BASE/split/$sess.jsonl'), Path('$BASE/split/backup'))
print(len(res), res[0].records if res else 0)")
check "вынесена одна ветка из восьми записей" "1 8" "$out"
check "в исходном ветвлений не осталось" "0" \
	"$(py "print(len(sessions.read_chain(Path('$BASE/split/$sess.jsonl')).forks))")"
check "исходный читается целиком" "True" \
	"$(py "c = sessions.read_chain(Path('$BASE/split/$sess.jsonl'))
print(len(c.reachable) == len(c.parents))")"
check "вынесенная читается целиком" "True" \
	"$(py "import glob
p = [f for f in glob.glob('$BASE/split/*.jsonl') if '$sess' not in f][0]
c = sessions.read_chain(Path(p))
print(len(c.reachable) == len(c.parents))")"
check "ЖЕЛУДЬ уехал в вынесенную" "1" \
	"$(grep -lc ЖЕЛУДЬ "$BASE"/split/*.jsonl | grep -vc "$sess" || true)"
check "ЯКОРЬ остался в исходной" "1" "$(grep -c ЯКОРЬ "$BASE/split/$sess.jsonl")"
check "у вынесенной свой sessionId" "0" \
	"$(py "import glob, json
p = [f for f in glob.glob('$BASE/split/*.jsonl') if '$sess' not in f][0]
ids = {json.loads(l)['sessionId'] for l in open(p) if l.strip()}
print(sum(1 for i in ids if i == '$sess'))")"
check "бэкап исходного на месте" "да" \
	"$([ -e "$BASE/split/backup/$sess.jsonl" ] && echo да || echo нет)"

echo
echo "ИТОГО: успешно $ok, провалено $fail"
exit $fail
