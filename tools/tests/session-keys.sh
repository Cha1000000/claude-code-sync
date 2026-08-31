#!/bin/bash
# Ключ сессии: смена каталога, дубли, прежние раскладки
set -u
BASE=${TMPDIR:-/tmp}/ccsync-tests
STAND=$BASE/session-keys
SRC=$(cd "$(dirname "$0")/../../bin" && pwd)
rm -rf "$STAND"; mkdir -p "$STAND"; cd "$STAND"

ok=0; fail=0
check() {
	if [ "$2" = "$3" ]; then echo "  ✓ $1"; ok=$((ok+1))
	else echo "  ✗ $1 — ожидали [$2], получили [$3]"; fail=$((fail+1)); fi
}

git init -q --bare --initial-branch=master remote.git
git clone -q remote.git vault 2>/dev/null
mkdir -p vault/bin && cp -r "$SRC"/* vault/bin/
rm -rf vault/bin/ccsync_lib/__pycache__
cp "$SRC/../.gitignore" vault/.gitignore
(cd vault && git config user.email t@t && git config user.name stand &&
 git add -A >/dev/null && git commit -qm "стенд" && git push -q -u origin master)

HOMEDIR=$STAND/home
mkdir -p "$HOMEDIR/.claude/projects" "$HOMEDIR/work"
cc() { cd "$1"; shift; env -u CLAUDE_CODE_SESSION_ID HOME="$HOMEDIR" \
	CLAUDE_CONFIG_DIR="$HOMEDIR/.claude" python3 "$STAND/vault/bin/ccsync.py" "$@"; }

SLUG_HOME=$(python3 -c "
import sys; sys.path.insert(0,'$SRC')
from ccsync_lib.paths import slug_for; print(slug_for('$HOMEDIR'))")
SID=dddddddd-4444-4444-4444-444444444444
DIR="$HOMEDIR/.claude/projects/$SLUG_HOME"; mkdir -p "$DIR"

# Транскрипт живёт в папке каталога ЗАПУСКА ($HOME), но последние записи —
# уже с другим рабочим каталогом: Клод перешёл в ~/work.
{
  printf '{"type":"user","cwd":"%s","sessionId":"%s","message":{"role":"user","content":"начали дома"}}\n' "$HOMEDIR" "$SID"
  printf '{"type":"assistant","cwd":"%s","sessionId":"%s","message":{"role":"assistant","content":"перешли в проект"}}\n' "$HOMEDIR/work" "$SID"
} > "$DIR/$SID.jsonl"

cc "$HOMEDIR" init --id m1 --yes --note стенд >/dev/null

echo "ТЕСТ 1 — push из СМЕНЁННОГО каталога кладёт сессию по месту запуска"
cc "$HOMEDIR/work" push session --session $SID >/dev/null 2>&1
check "сессия ушла под ключ home" "да" \
	"$([ -e "$STAND/vault/sessions/home/$SID.jsonl" ] && echo да || echo нет)"
check "дубля под ключом work нет" "нет" \
	"$([ -e "$STAND/vault/sessions/work/$SID.jsonl" ] && echo да || echo нет)"
check "ключей проектов с сессиями ровно один" "1" \
	"$(ls -d "$STAND/vault/sessions"/*/ 2>/dev/null | grep -vc tombstones)"

echo "ТЕСТ 2 — повторный push из третьего каталога ничего не раздваивает"
mkdir -p "$HOMEDIR/other"
rm -f "$HOMEDIR/.claude/.ccsync-last-push"
printf '{"type":"user","cwd":"%s","sessionId":"%s","message":{"role":"user","content":"третий каталог"}}\n' "$HOMEDIR/other" "$SID" >> "$DIR/$SID.jsonl"
cc "$HOMEDIR/other" push session --session $SID >/dev/null 2>&1
check "по-прежнему один ключ" "1" \
	"$(ls -d "$STAND/vault/sessions"/*/ 2>/dev/null | grep -vc tombstones)"
check "копия в home обновилась" "3" "$(wc -l < "$STAND/vault/sessions/home/$SID.jsonl")"

echo "ТЕСТ 3 — явный --project остаётся сильнее"
rm -f "$HOMEDIR/.claude/.ccsync-last-push"
cc "$HOMEDIR" push session --session $SID --project "$HOMEDIR/work" >/dev/null 2>&1
check "человек сказал work — значит work" "да" \
	"$([ -e "$STAND/vault/sessions/work/$SID.jsonl" ] && echo да || echo нет)"

echo "ТЕСТ 4 — из нескольких копий берётся живая, а не застывший огрызок"
OTHERSLUG=$(python3 -c "
import sys; sys.path.insert(0,'$SRC')
from ccsync_lib.paths import slug_for; print(slug_for('$HOMEDIR/work'))")
mkdir -p "$HOMEDIR/.claude/projects/$OTHERSLUG"
# Огрызок в чужой папке: одна строка, но создан РАНЬШЕ живого файла.
printf '{"type":"user","cwd":"%s","sessionId":"%s","message":{"role":"user","content":"огрызок"}}\n' "$HOMEDIR/work" "$SID" \
	> "$HOMEDIR/.claude/projects/$OTHERSLUG/$SID.jsonl"
touch -d "-1 hour" "$HOMEDIR/.claude/projects/$OTHERSLUG/$SID.jsonl"
picked=$(python3 -c "
import sys; sys.path.insert(0,'$STAND/vault/bin')
from pathlib import Path
from ccsync_lib import ignore
p = ignore.find_local_transcript(Path('$HOMEDIR/.claude/projects'), '$SID')
print(p.parent.name)")
check "выбрана свежая копия" "$SLUG_HOME" "$picked"
check "видны обе копии" "2" \
	"$(python3 -c "
import sys; sys.path.insert(0,'$STAND/vault/bin')
from pathlib import Path
from ccsync_lib import ignore
print(len(ignore.find_local_transcripts(Path('$HOMEDIR/.claude/projects'), '$SID')))")"

echo "ТЕСТ 5 — forget сносит ВСЕ копии, а не одну"
rm -f "$HOMEDIR/.claude/.ccsync-last-push"
cc "$HOMEDIR" forget --yes --session $SID >/dev/null 2>&1
check "локальных копий не осталось" "0" \
	"$(find "$HOMEDIR/.claude/projects" -name "$SID.jsonl" | wc -l)"

echo "ТЕСТ 6 — проект сменил место: прежняя раскладка не остаётся дублем"
SID2=eeeeeeee-5555-5555-5555-555555555555
# Сессия проекта, который на этой машине ещё не привязан: она уедет в fallback.
mkdir -p "$STAND/vault/sessions/faraway"
printf '{"type":"user","cwd":"/other/machine/faraway","sessionId":"%s","uuid":"u1","message":{"role":"user","content":"раз"}}\n' "$SID2" \
	> "$STAND/vault/sessions/faraway/$SID2.jsonl"
cc "$HOMEDIR" pull session >/dev/null 2>&1
FB=$(python3 -c "
import sys; sys.path.insert(0,'$SRC')
from ccsync_lib.paths import slug_for; print(slug_for('$HOMEDIR/claude-sessions/faraway'))")
check "пока проект не привязан — сессия в fallback" "да" \
	"$([ -e "$HOMEDIR/.claude/projects/$FB/$SID2.jsonl" ] && echo да || echo нет)"

# Привязываем проект к настоящему пути и тянем снова.
mkdir -p "$HOMEDIR/faraway"
cc "$HOMEDIR" bind faraway "$HOMEDIR/faraway" >/dev/null 2>&1
cc "$HOMEDIR" pull session >/dev/null 2>&1
REAL=$(python3 -c "
import sys; sys.path.insert(0,'$SRC')
from ccsync_lib.paths import slug_for; print(slug_for('$HOMEDIR/faraway'))")
check "сессия легла по настоящему пути" "да" \
	"$([ -e "$HOMEDIR/.claude/projects/$REAL/$SID2.jsonl" ] && echo да || echo нет)"
check "прежняя раскладка убрана" "нет" \
	"$([ -e "$HOMEDIR/.claude/projects/$FB/$SID2.jsonl" ] && echo да || echo нет)"
check "копия ровно одна" "1" \
	"$(find "$HOMEDIR/.claude/projects" -name "$SID2.jsonl" | wc -l)"

echo "ТЕСТ 7 — копию с собственными записями не трогаем"
SID3=ffffffff-6666-6666-6666-666666666666
printf '{"type":"user","cwd":"/other/machine/faraway","sessionId":"%s","uuid":"a1","message":{"role":"user","content":"раз"}}\n' "$SID3" \
	> "$STAND/vault/sessions/faraway/$SID3.jsonl"
cc "$HOMEDIR" pull session >/dev/null 2>&1
# Подкладываем «свою» версию в чужой каталог: в ней есть запись, которой нет в хранилище.
mkdir -p "$HOMEDIR/.claude/projects/$FB"
printf '{"type":"user","cwd":"x","sessionId":"%s","uuid":"a1","message":{"role":"user","content":"раз"}}\n{"type":"user","cwd":"x","sessionId":"%s","uuid":"ЛИШНЯЯ","message":{"role":"user","content":"два"}}\n' "$SID3" "$SID3" \
	> "$HOMEDIR/.claude/projects/$FB/$SID3.jsonl"
out=$(cc "$HOMEDIR" pull session 2>&1)
check "копия сохранена" "да" \
	"$([ -e "$HOMEDIR/.claude/projects/$FB/$SID3.jsonl" ] && echo да || echo нет)"
check "и об этом сказано" "1" "$(echo "$out" | grep -c 'есть свои записи')"

echo
echo "ИТОГО: успешно $ok, провалено $fail"
exit $fail
