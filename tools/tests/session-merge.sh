#!/bin/bash
# Стенд: сессии не теряются при синхронизации двух машин.
#
# 08–15.10.2026 сессия на 175 МБ шесть дней не уезжала с десктопа (порог push
# был 50 МБ, предупреждение уходило в /dev/null), ноутбук открыл её устаревшую
# копию и отправил, а pull десктопа заменил ею полную локальную — по mtime,
# который после git checkout всегда свежее. Стенд стережёт каждое звено:
# pull сливает по uuid, а не заменяет; push не стирает чужую ветку; картинки
# вырезаются только в копии для хранилища; пропуск виден на обеих машинах.
set -u
BASE=${TMPDIR:-/tmp}/ccsync-tests
STAND=$BASE/session-merge
SRC=$(cd "$(dirname "$0")/../../bin" && pwd)
rm -rf "$STAND"; mkdir -p "$STAND"; cd "$STAND"

ok=0; fail=0
check() { # check "описание" "ожидаемое" "фактическое"
	if [ "$2" = "$3" ]; then echo "  ✓ $1"; ok=$((ok+1))
	else echo "  ✗ $1 — ожидали [$2], получили [$3]"; fail=$((fail+1)); fi
}

git init -q --bare --initial-branch=master remote.git
for m in m1 m2; do
	git clone -q remote.git ${m}vault
	mkdir -p ${m}vault/bin && cp -r "$SRC"/* ${m}vault/bin/
	rm -rf ${m}vault/bin/ccsync_lib/__pycache__
	cp "$SRC/../.gitignore" ${m}vault/.gitignore
	(cd ${m}vault && git config user.email t@t && git config user.name "stand $m")
	mkdir -p $m/.claude/projects $m/work
	cat > "$STAND/$m.sh" <<EOF
#!/bin/bash
cd "$STAND/$m/work" || exit 1
exec env -u CLAUDE_CODE_SESSION_ID HOME="$STAND/$m" CLAUDE_CONFIG_DIR="$STAND/$m/.claude" \
     python3 "$STAND/${m}vault/bin/ccsync.py" "\$@"
EOF
	cat > "$STAND/$m-context.sh" <<EOF
#!/bin/bash
cd "$STAND/$m/work" || exit 1
exec env -u CLAUDE_CODE_SESSION_ID HOME="$STAND/$m" CLAUDE_CONFIG_DIR="$STAND/$m/.claude" \
     python3 "$STAND/${m}vault/bin/session-context.py" "\$@"
EOF
	chmod +x "$STAND/$m.sh" "$STAND/$m-context.sh"
done
(cd m1vault && git add -A >/dev/null && git commit -qm "стенд" && git push -q -u origin master)
(cd m2vault && git fetch -q origin && git reset -q --hard origin/master &&
 git branch --set-upstream-to=origin/master master >/dev/null 2>&1)

S=5e551011-aaaa-4bbb-8ccc-000000000001   # обычная сессия с картинкой
H=5e551011-aaaa-4bbb-8ccc-000000000002   # слишком большая, чтобы уехать
slug() { python3 -c "
import sys; sys.path.insert(0,'$SRC')
from ccsync_lib.paths import slug_for; print(slug_for('$STAND/$1/work'))"; }
DIR1="$STAND/m1/.claude/projects/$(slug m1)"; mkdir -p "$DIR1"
DIR2="$STAND/m2/.claude/projects/$(slug m2)"
VAULT_S="$STAND/m1vault/sessions/work/$S.jsonl"

# add <файл> <машина> <sid> <uuid> <parent|-> <минута> [image|big] — дописать запись.
add() {
	python3 - "$@" <<'PY'
import json, sys
path, machine, sid, uid, parent, minute = sys.argv[1:7]
extra = sys.argv[7] if len(sys.argv) > 7 else ""
record = {"type": "user", "uuid": uid, "parentUuid": None if parent == "-" else parent,
          "timestamp": f"2026-09-01T10:{int(minute):02d}:00.000Z",
          "cwd": f"{machine}/work", "sessionId": sid,
          "message": {"role": "user", "content": f"ход {uid}"}}
if extra == "image":
    picture = "iVBORw0KGgo" + "A" * 4000
    record["message"]["content"] = [
        {"type": "tool_result", "tool_use_id": "t1",
         "content": [{"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": picture}}]}]
    record["toolUseResult"] = {"type": "image", "file": {"base64": picture, "type": "image/png"}}
if extra == "big":
    record["message"]["content"] = "длинный текст " * 90000
with open(path, "a", encoding="utf-8") as handle:
    handle.write(json.dumps(record, ensure_ascii=False) + "\n")
PY
}
# Набор ходов без учёта порядка строк.
uuidset() { uuids "$1" | tr ' ' '\n' | sort | tr '\n' ' ' | sed 's/ $//'; }
uuids() { python3 -c "
import json, sys
print(' '.join(json.loads(l)['uuid'] for l in open(sys.argv[1]) if l.strip()))" "$1" 2>/dev/null; }
images() { grep -c iVBORw0KGgo "$1" 2>/dev/null; true; }
push() { rm -f "$STAND/$1/.claude/.ccsync-last-push"; "$STAND/$1.sh" push session --quiet --session "${@:2}" >/dev/null 2>&1; }

"$STAND/m1.sh" init --id m1 --yes --note "машина 1" >/dev/null
(cd "$STAND/m2vault" && git pull -q --rebase)
"$STAND/m2.sh" init --id m2 --yes --note "машина 2" >/dev/null
"$STAND/m2.sh" bind work "$STAND/m2/work" >/dev/null
"$STAND/m2.sh" push tools >/dev/null 2>&1
"$STAND/m1.sh" pull tools >/dev/null 2>&1

M1="$STAND/m1"; M2="$STAND/m2"
add "$DIR1/$S.jsonl" "$M1" $S u1 - 0
add "$DIR1/$S.jsonl" "$M1" $S u2 u1 1 image
add "$DIR1/$S.jsonl" "$M1" $S u3 u2 2

echo "ТЕСТ 1 — картинки вырезаются только в копии для хранилища"
push m1 $S
check "сессия уехала" "u1 u2 u3" "$(uuids "$VAULT_S")"
check "в хранилище картинок нет" "0" "$(images "$VAULT_S")"
check "в хранилище — пометка вместо картинки" "1" "$(grep -c 'изображение не синхронизировано' "$VAULT_S")"
check "локально картинка цела (и в дубле toolUseResult)" "1" "$(images "$DIR1/$S.jsonl")"
check "в строке две копии base64" "2" "$(grep -o iVBORw0KGgo "$DIR1/$S.jsonl" | wc -l)"

echo "ТЕСТ 2 — pull не трогает полную локальную копию"
"$STAND/m2.sh" pull session >/dev/null 2>&1
check "вторая машина получила сессию" "u1 u2 u3" "$(uuids "$DIR2/$S.jsonl")"
add "$DIR1/$S.jsonl" "$M1" $S u4 u3 3
add "$DIR1/$S.jsonl" "$M1" $S u5 u4 4
before=$(md5sum < "$DIR1/$S.jsonl")
"$STAND/m1.sh" pull session >/dev/null 2>&1
check "у первой машины неотправленные ходы на месте" "u1 u2 u3 u4 u5" "$(uuids "$DIR1/$S.jsonl")"
check "файл не переписан вовсе" "$before" "$(md5sum < "$DIR1/$S.jsonl")"
check "картинка не заменена пометкой из хранилища" "1" "$(images "$DIR1/$S.jsonl")"
check "резервной копии нет — нечего было спасать" "0" "$(ls -d "$M1"/.claude/backups/pull-merge-* 2>/dev/null | wc -l)"

echo "ТЕСТ 3 — разошлись: push второй машины не стирает ветку первой"
add "$DIR2/$S.jsonl" "$M2" $S l1 u3 30
push m1 $S
push m2 $S
(cd "$STAND/m1vault" && git pull -q --rebase >/dev/null 2>&1)
check "в хранилище обе ветки" "l1 u1 u2 u3 u4 u5" "$(uuidset "$VAULT_S")"

echo "ТЕСТ 4 — pull при расхождении сливает, а старое кладёт в резервную копию"
add "$DIR1/$S.jsonl" "$M1" $S u6 u5 40
"$STAND/m1.sh" pull session --quiet >/dev/null 2>&1
all=$(uuids "$DIR1/$S.jsonl")
check "все ходы обеих машин" "l1 u1 u2 u3 u4 u5 u6" "$(uuidset "$DIR1/$S.jsonl")"
check "последней строкой — свежая ветка (её откроет --resume)" "u6" "${all##* }"
check "картинка уцелела при слиянии" "1" "$(images "$DIR1/$S.jsonl")"
check "резервная копия до слияния" "1" "$(ls "$M1"/.claude/backups/pull-merge-*/*.jsonl 2>/dev/null | wc -l)"
check "сообщение для следующего SessionStart" "1" \
	"$("$STAND/m1-context.sh" | grep -c "$(echo $S | cut -c1-8)")"
check "сообщение показано один раз" "0" \
	"$("$STAND/m1-context.sh" | grep -c "$(echo $S | cut -c1-8)")"

echo "ТЕСТ 5 — вторая машина получает ветку первой"
push m1 $S
"$STAND/m2.sh" pull session >/dev/null 2>&1
check "у второй машины всё" "l1 u1 u2 u3 u4 u5 u6" \
	"$(uuidset "$DIR2/$S.jsonl")"

echo "ТЕСТ 6 — слишком большая сессия: не молчим"
add "$DIR1/$H.jsonl" "$M1" $H h1 - 0 big
add "$DIR1/$H.jsonl" "$M1" $H h2 h1 1 big
push m1 $H --max-mb 1
check "в хранилище не уехала" "нет" "$([ -e "$STAND/m1vault/sessions/work/$H.jsonl" ] && echo да || echo нет)"
check "отмечена у себя" "1" "$(grep -c "$H" "$M1/.claude/ccsync-held.json" 2>/dev/null || echo 0)"
check "отмечена в хранилище для других машин" "1" "$(grep -c "$H" "$STAND/m1vault/held/m1.json" 2>/dev/null || echo 0)"
check "SessionStart здесь предупреждает" "1" "$("$STAND/m1-context.sh" | grep -c 'не уезжает в хранилище с')"
(cd "$STAND/m2vault" && git pull -q --rebase >/dev/null 2>&1)
check "на другой машине — про устаревшую копию" "1" \
	"$("$STAND/m2-context.sh" --session $H | grep -c 'здесь открыта устаревшая копия')"
check "про другие сессии там молчим" "0" \
	"$("$STAND/m2-context.sh" --session $S | grep -c 'устаревшая копия')"
push m1 $H
check "с обычным порогом уехала" "да" "$([ -e "$STAND/m1vault/sessions/work/$H.jsonl" ] && echo да || echo нет)"
check "отметка снята и в хранилище" "нет" "$([ -e "$STAND/m1vault/held/m1.json" ] && echo да || echo нет)"
check "сообщение «снова уезжает»" "1" "$("$STAND/m1-context.sh" | grep -c 'снова уезжает')"

echo "ТЕСТ 7 — restore-session"
cp "$DIR1/$S.jsonl" "$STAND/good.jsonl"
gzip -k "$STAND/good.jsonl"
"$STAND/m1.sh" restore-session $S --from "$DIR1/$H.jsonl" --force >/dev/null 2>&1
check "чужой транскрипт не принимается" "1" "$?"
"$STAND/m1.sh" restore-session $S --from "$STAND/good.jsonl.gz" >/dev/null 2>&1
check "свежую сессию без --force не трогает" "2" "$?"
head -3 "$DIR1/$S.jsonl" > "$STAND/short.jsonl"
cp "$STAND/short.jsonl" "$DIR1/$S.jsonl"
"$STAND/m1.sh" restore-session $S --from "$STAND/good.jsonl.gz" --force >/dev/null 2>&1
check "восстановлено из .gz" "l1 u1 u2 u3 u4 u5 u6" \
	"$(uuidset "$DIR1/$S.jsonl")"
check "прежняя версия в резервной копии" "u1 u2 u3" "$(uuids "$(ls "$M1"/.claude/backups/restore-*/$S.jsonl | head -1)")"

echo "ТЕСТ 8 — slim сжимает локальную копию, оригинал в архив"
"$STAND/m1.sh" slim $S --force >/dev/null 2>&1
check "локально картинки больше нет" "0" "$(images "$DIR1/$S.jsonl")"
check "разговор цел" "7" "$(uuids "$DIR1/$S.jsonl" | wc -w)"
check "в архиве оригинал с картинкой" "1" "$(zcat "$M1"/.claude/session-archive/$S-*.jsonl.gz | grep -c iVBORw0KGgo)"

# Чистилка — обвязка Claude Code этой машины (tools/host), в публичной
# версии движка её нет; там тест просто пропускается.
PRUNE="$SRC/../tools/host/bin/claude-sessions-prune.py"
if [ -f "$PRUNE" ]; then
echo "ТЕСТ 9 — claude-sessions-prune: скриншоты и временные файлы старше 7 дней"
ln -s "$STAND/m1vault" "$M1/claude-code-sync"
O=5e551011-aaaa-4bbb-8ccc-000000000003   # давно не открывали
F=5e551011-aaaa-4bbb-8ccc-000000000004   # писали только что
add "$DIR1/$O.jsonl" "$M1" $O o1 - 0 image
add "$DIR1/$F.jsonl" "$M1" $F f1 - 0 image
python3 - "$DIR1/$O.jsonl" <<'PY'
import json, sys
from datetime import datetime, timezone
picture = "iVBORw0KGgo" + "B" * 4000
record = {"type": "user", "uuid": "o2", "parentUuid": "o1", "sessionId": sys.argv[1].split("/")[-1][:-6],
          "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z"),
          "message": {"role": "user", "content": [{"type": "image", "source": {"type": "base64", "data": picture}}]}}
answer = {"type": "assistant", "uuid": "o3", "parentUuid": "o2", "sessionId": record["sessionId"],
          "timestamp": record["timestamp"], "message": {"role": "assistant", "content": "вижу"}}
open(sys.argv[1], "a").write(json.dumps(record) + "\n" + json.dumps(answer) + "\n")
PY
touch -d '1 day ago' "$DIR1/$O.jsonl"
o_mtime=$(stat -c %Y "$DIR1/$O.jsonl")
mkdir -p "$DIR1/$O/tool-results" "$STAND/tmp/claude-$(id -u)/proj/sess/scratchpad" \
	"$M1/.claude/backups/old" "$M1/.claude/backups/new"
echo x > "$DIR1/$O/tool-results/old.txt"; touch -d '10 days ago' "$DIR1/$O/tool-results/old.txt"
echo x > "$DIR1/$O/tool-results/new.txt"
echo x > "$STAND/tmp/claude-$(id -u)/proj/sess/scratchpad/old.png"
touch -d '10 days ago' "$STAND/tmp/claude-$(id -u)/proj/sess/scratchpad/old.png"
echo x > "$STAND/tmp/claude-$(id -u)/proj/sess/scratchpad/new.png"
touch -d '40 days ago' "$M1/.claude/backups/old"; touch -d '10 days ago' "$M1/.claude/backups/new"
prune() { (cd "$M1" && env HOME="$M1" TMPDIR="$STAND/tmp" python3 "$PRUNE" "$@" 2>&1); }
before=$(md5sum < "$DIR1/$O.jsonl")
prune --dry-run >/dev/null
check "dry-run ничего не трогает" "$before $(ls "$DIR1/$O/tool-results" | wc -l)" \
	"$(md5sum < "$DIR1/$O.jsonl") $(ls "$DIR1/$O/tool-results" | wc -l)"
prune >/dev/null
check "старая картинка вырезана, свежая осталась" "0 1" \
	"$(grep -c iVBORw0KGgoAAA "$DIR1/$O.jsonl") $(grep -c iVBORw0KGgoBBB "$DIR1/$O.jsonl")"
check "разговор цел" "o1 o2 o3" "$(uuids "$DIR1/$O.jsonl")"
check "mtime сохранён" "$o_mtime" "$(stat -c %Y "$DIR1/$O.jsonl")"
check "в файл, куда только что писали, не лезем" "1" "$(images "$DIR1/$F.jsonl")"
check "старый вывод инструмента удалён, свежий нет" "new.txt" "$(ls "$DIR1/$O/tool-results")"
check "scratchpad: старое удалено, свежее нет" "new.png" "$(ls "$STAND/tmp/claude-$(id -u)/proj/sess/scratchpad")"
check "резервные копии: удалена только старше 30 дней" "new" "$(ls "$M1/.claude/backups" | grep -v -e pull-merge -e restore)"
else
	echo "ТЕСТ 9 — пропущен: нет claude-sessions-prune"
fi

echo "ТЕСТ 10 — находки ревью"
py() { python3 -c "
import json, sys; sys.path.insert(0, '$SRC')
from pathlib import Path
from ccsync_lib import sessions
$1
"; }
check "U+2028 и NEL внутри строки не рвут запись при слиянии" "2" \
	"$(py "a = json.dumps({'uuid': 'a', 't': 'x y\x85z'}, ensure_ascii=False)
b = json.dumps({'uuid': 'b'})
print(len(sessions.merge_lines([a], [a, b]).lines))")"
check "second-has-all: свои записи без uuid не теряются и идут первыми" "title a b" \
	"$(py "t = json.dumps({'type': 'custom-title', 'customTitle': 'x'})
a = json.dumps({'uuid': 'a'}); b = json.dumps({'uuid': 'b'})
r = sessions.merge_lines([a, t], [a, b])
print(r.kind == 'second-has-all' and ' '.join('title' if 'custom' in l else json.loads(l)['uuid'] for l in r.lines))")"
check "merged: запись без uuid в другом форматировании не дублируется" "1" \
	"$(py "t1 = json.dumps({'type': 'custom-title', 'customTitle': 'x'}, separators=(',', ':'))
t2 = json.dumps({'type': 'custom-title', 'customTitle': 'x'})
a = json.dumps({'uuid': 'a'}); b = json.dumps({'uuid': 'b'})
print(sum('custom' in l for l in sessions.merge_lines([a, t1], [b, t2]).lines))")"
check "write_atomic не пишет поверх дописанного и не мусорит" "False old 0" \
	"$(py "p = Path('$STAND/atomic.txt'); p.write_text('old')
stamp = (p.stat().st_size, p.stat().st_mtime_ns)
open(p, 'a').write('+')
ok = sessions.write_atomic(p, 'new', expect=stamp)
print(ok, p.read_text()[:3], len(list(p.parent.glob('.atomic.txt.*'))))")"

add "$DIR1/$S.jsonl" "$M1" $S u7 u6 50
python3 - "$DIR1/$S.jsonl" <<'PY'
import json, sys
rec = {"type": "user", "uuid": "u8", "parentUuid": "u7", "timestamp": "2026-09-01T10:51:00.000Z",
       "sessionId": "5e551011-aaaa-4bbb-8ccc-000000000001", "message": {"role": "user", "content": "строка с разделителем\x85и NEL"}}
open(sys.argv[1], "a", encoding="utf-8").write(json.dumps(rec, ensure_ascii=False) + "\n")
PY
push m1 $S; push m1 $S
check "после двух push все строки хранилища — целый JSON" "0" \
	"$(python3 -c "
import json, sys
bad = 0
for line in open(sys.argv[1], encoding='utf-8'):
    try: json.loads(line)
    except ValueError: bad += 1
print(bad)" "$VAULT_S")"

"$STAND/m2.sh" pull session --quiet >/dev/null 2>&1
add "$DIR2/$S.jsonl" "$M2" $S l2 l1 55
push m2 $S
push m1 $S
"$STAND/m1.sh" pull session --quiet >/dev/null 2>&1
check "чужие записи, влитые в хранилище при push, доходят до локальной копии" "1" \
	"$(uuids "$DIR1/$S.jsonl" | tr ' ' '\n' | grep -c '^l2$')"

if [ -f "$PRUNE" ]; then
mkdir -p "$STAND/tmp/claude-$(id -u)/proj/fresh/scratchpad"
prune >/dev/null
check "пустой scratchpad только что начатой сессии не удалён" "да" \
	"$([ -d "$STAND/tmp/claude-$(id -u)/proj/fresh/scratchpad" ] && echo да || echo нет)"
fi

echo "ТЕСТ 11 — синхронизация не коммитит правки самого движка"
echo "# незаконченная правка" >> "$STAND/m1vault/bin/ccsync.py"
echo "черновик" > "$STAND/m1vault/bin/ccsync_lib/draft.py"
add "$DIR1/$S.jsonl" "$M1" $S u9 u8 59
out=$(rm -f "$M1/.claude/.ccsync-last-push"; "$STAND/m1.sh" push session --session $S 2>&1)
check "сессия закоммичена и отправлена" "1" \
	"$(cd "$STAND/m1vault" && git log origin/master -1 --name-only --format= | grep -c "$S")"
check "правка движка не закоммичена" "M bin/ccsync.py" \
	"$(cd "$STAND/m1vault" && git status --porcelain -- bin/ccsync.py | sed 's/^ //')"
check "новый файл движка не закоммичен" "?? bin/ccsync_lib/draft.py" \
	"$(cd "$STAND/m1vault" && git status --porcelain --untracked-files=all -- bin/ccsync_lib/draft.py)"
check "об этом сказано" "1" "$(echo "$out" | grep -c 'правки движка не коммитятся')"

echo
echo "ИТОГО: успешно $ok, провалено $fail"
exit $fail
