#!/bin/bash
# Приватные сессии: ignore, forget, отложенное удаление
set -u
BASE=${TMPDIR:-/tmp}/ccsync-tests
STAND=$BASE/private-sessions
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
	chmod +x "$STAND/$m.sh"
done
(cd m1vault && git add -A >/dev/null && git commit -qm "стенд" && git push -q -u origin master)
(cd m2vault && git fetch -q origin && git reset -q --hard origin/master &&
 git branch --set-upstream-to=origin/master master >/dev/null 2>&1)

SLUG=$(python3 -c "
import sys; sys.path.insert(0,'$SRC')
from ccsync_lib.paths import slug_for; print(slug_for('$STAND/m1/work'))")
DIR="$STAND/m1/.claude/projects/$SLUG"; mkdir -p "$DIR"
for id in aaaaaaaa-1111-1111-1111-111111111111 bbbbbbbb-2222-2222-2222-222222222222 cccccccc-3333-3333-3333-333333333333; do
	printf '{"type":"user","cwd":"%s","sessionId":"%s","message":{"role":"user","content":"текст %s"}}\n' \
		"$STAND/m1/work" "$id" "$id" > "$DIR/$id.jsonl"; sleep 0.05
done
A=aaaaaaaa-1111-1111-1111-111111111111
B=bbbbbbbb-2222-2222-2222-222222222222
C=cccccccc-3333-3333-3333-333333333333
hook() { # hook <машина> <действие> <session_id>
	local m=$1 act=$2 sid=$3
	local dir="$STAND/$m/.claude/projects/$SLUG"
	cd "$STAND/$m/work"
	printf '{"session_id":"%s","transcript_path":"%s","cwd":"%s"}' "$sid" "$dir/$sid.jsonl" "$STAND/$m/work" |
	env HOME="$STAND/$m" CLAUDE_CONFIG_DIR="$STAND/$m/.claude" CLAUDE_CODE_SESSION_ID="$sid" \
		python3 "$STAND/${m}vault/bin/cchook.py" "$act" >/dev/null 2>&1
	sleep 2
}
inv() { ls "$STAND/m1vault/sessions/work" 2>/dev/null | wc -l; }
has() { [ -e "$STAND/m1vault/sessions/work/$1.jsonl" ] && echo да || echo нет; }
loc() { [ -e "$STAND/$2/.claude/projects/$SLUG/$1.jsonl" ] && echo да || echo нет; }

"$STAND/m1.sh" init --id m1 --yes --note "машина 1" >/dev/null

echo "ТЕСТ 0 — обычный push непомеченной сессии"
"$STAND/m1.sh" push session --session $A >/dev/null 2>&1
check "aaaa уехала в хранилище" "да" "$(has $A)"

# Вторая машина подключается так, как описано в BOOTSTRAP: сначала забирает
# готовое хранилище, потом заводит себя. Реестры машин и проектов при этом
# дополняются, а не создаются заново на обеих сторонах.
(cd "$STAND/m2vault" && git pull -q --rebase)
"$STAND/m2.sh" init --id m2 --yes --note "машина 2" >/dev/null
"$STAND/m2.sh" bind work "$STAND/m2/work" >/dev/null
"$STAND/m2.sh" push tools >/dev/null 2>&1
"$STAND/m1.sh" pull tools >/dev/null 2>&1

echo "ТЕСТ 1 — ignore блокирует отправку"
"$STAND/m1.sh" ignore --session $B --reason "приватное" >/dev/null
rm -f "$STAND/m1/.claude/.ccsync-last-push"
"$STAND/m1.sh" push session --session $B >/dev/null 2>&1
check "bbbb НЕ уехала" "нет" "$(has $B)"

echo "ТЕСТ 2/3 — хук Stop: точный id и уважение пометки"
rm -f "$STAND/m1/.claude/.ccsync-last-push"
hook m1 stop $B
check "через хук bbbb не уехала" "нет" "$(has $B)"
check "чужая свежая cccc не уехала вместо неё" "нет" "$(has $C)"
rm -f "$STAND/m1/.claude/.ccsync-last-push"
hook m1 stop $C
check "непомеченная cccc через хук уехала" "да" "$(has $C)"

echo "ТЕСТ 4 — forget живой сессии"
cd "$STAND/m1/work"
env HOME="$STAND/m1" CLAUDE_CONFIG_DIR="$STAND/m1/.claude" CLAUDE_CODE_SESSION_ID=$C \
	python3 "$STAND/m1vault/bin/ccsync.py" forget --yes >/dev/null 2>&1
check "копия cccc удалена из хранилища" "нет" "$(has $C)"
check "локальный файл живой сессии пока на месте" "да" "$(loc $C m1)"
hook m1 session-start $C
check "SessionStart не тронул файл живой сессии" "да" "$(loc $C m1)"
hook m1 session-end $C
check "SessionEnd удалил файл закрытой сессии" "нет" "$(loc $C m1)"
check "и не залил её обратно в хранилище" "нет" "$(has $C)"

echo "ТЕСТ 5/6 — forget по id и tombstone на второй машине"
"$STAND/m2.sh" pull session >/dev/null 2>&1
M2DIR=$(ls "$STAND/m2/.claude/projects/" 2>/dev/null | head -1)
check "m2 получила aaaa" "да" "$([ -e "$STAND/m2/.claude/projects/$M2DIR/$A.jsonl" ] && echo да || echo нет)"
check "pull НЕ создал каталог-пустышку claude-sessions/tombstones" "нет" \
	"$([ -e "$STAND/m2/claude-sessions/tombstones" ] && echo да || echo нет)"
rm -f "$STAND/m1/.claude/.ccsync-last-push"
"$STAND/m1.sh" forget --yes --session $A >/dev/null 2>&1
check "aaaa удалена из хранилища" "нет" "$(has $A)"
check "и локально на m1" "нет" "$(loc $A m1)"
"$STAND/m2.sh" pull session >/dev/null 2>&1
check "m2 снесла свою копию по отметке" "нет" \
	"$([ -e "$STAND/m2/.claude/projects/$M2DIR/$A.jsonl" ] && echo да || echo нет)"

echo "ТЕСТ 7 — очистка отметок после подтверждения всеми"
"$STAND/m2.sh" push tools >/dev/null 2>&1
"$STAND/m1.sh" pull session >/dev/null 2>&1
check "отметки убраны" "0" "$(ls -A "$STAND/m1vault/sessions/tombstones" 2>/dev/null | wc -l)"

echo "ТЕСТ 8 — регресс и голая машина"
check "status на m1 не падает" "0" "$("$STAND/m1.sh" status >/dev/null 2>&1; echo $?)"
check "status на m2 не падает" "0" "$("$STAND/m2.sh" status >/dev/null 2>&1; echo $?)"
check "'tombstones' не числится непривязанным проектом" "0" \
	"$("$STAND/m2.sh" status 2>/dev/null | grep -c 'Не привязано.*tombstones')"
check "ignore --list на машине без файла игнора" "0" "$("$STAND/m2.sh" ignore --list >/dev/null 2>&1; echo $?)"
check "ignore --undo снимает пометку" "0" "$("$STAND/m1.sh" ignore --undo $B >/dev/null 2>&1; echo $?)"
check "после undo bbbb снова уезжает" "да" \
	"$(rm -f "$STAND/m1/.claude/.ccsync-last-push"; "$STAND/m1.sh" push session --session $B >/dev/null 2>&1; has $B)"
check "undo несуществующей пометки — понятная ошибка, код 1" "1" \
	"$("$STAND/m1.sh" ignore --undo 00000000-0000-0000-0000-000000000000 >/dev/null 2>&1; echo $?)"

echo
echo "ИТОГО: успешно $ok, провалено $fail"
exit $fail
