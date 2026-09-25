#!/bin/bash
# Копируемые файлы (CLAUDE.md, statusline.py): правка на одной машине не
# затирается ни pull-ом, ни push-ем устаревшей копии с другой
set -u
BASE=${TMPDIR:-/tmp}/ccsync-tests
STAND=$BASE/copied-files
SRC=$(cd "$(dirname "$0")/../../bin" && pwd)
rm -rf "$STAND"; mkdir -p "$STAND"; cd "$STAND"

ok=0; fail=0
check() {
	if [ "$2" = "$3" ]; then echo "  ✓ $1"; ok=$((ok+1))
	else echo "  ✗ $1 — ожидали [$2], получили [$3]"; fail=$((fail+1)); fi
}

git init -q --bare --initial-branch=master remote.git
for m in m1 m2; do
	git clone -q remote.git ${m}vault 2>/dev/null
	mkdir -p ${m}vault/bin && cp -r "$SRC"/* ${m}vault/bin/
	rm -rf ${m}vault/bin/ccsync_lib/__pycache__
	cp "$SRC/../.gitignore" ${m}vault/.gitignore
	(cd ${m}vault && git config user.email t@t && git config user.name "stand $m")
	mkdir -p $m/.claude/projects
	cat > "$STAND/$m.sh" <<EOF
#!/bin/bash
cd "$STAND/$m" || exit 1
exec env -u CLAUDE_CODE_SESSION_ID HOME="$STAND/$m" CLAUDE_CONFIG_DIR="$STAND/$m/.claude" \
     CCSYNC_NO_SYSTEMCTL=1 CCSYNC_LANG=ru \
     python3 "$STAND/${m}vault/bin/ccsync.py" "\$@"
EOF
	chmod +x "$STAND/$m.sh"
done
(cd m1vault && git add -A >/dev/null && git commit -qm "стенд" && git push -q -u origin master)
(cd m2vault && git fetch -q origin && git reset -q --hard origin/master &&
 git branch --set-upstream-to=origin/master master >/dev/null 2>&1)

"$STAND/m1.sh" init --id m1 --yes --note "машина 1" >/dev/null
(cd "$STAND/m2vault" && git pull -q --rebase)
"$STAND/m2.sh" init --id m2 --yes --note "машина 2" >/dev/null

M1=$STAND/m1/.claude/CLAUDE.md
M2=$STAND/m2/.claude/CLAUDE.md
VAULT1=$STAND/m1vault/tools/CLAUDE.md
VAULT2=$STAND/m2vault/tools/CLAUDE.md
sync1() { (cd "$STAND/m1vault" && git pull -q --rebase 2>/dev/null); }
sync2() { (cd "$STAND/m2vault" && git pull -q --rebase 2>/dev/null); }

echo "ТЕСТ 1 — обычная правка доезжает до другой машины"
echo "версия 1" > "$M1"
"$STAND/m1.sh" push tools >/dev/null 2>&1
sync2; "$STAND/m2.sh" pull tools >/dev/null 2>&1
check "в хранилище версия 1" "версия 1" "$(cat "$VAULT1")"
check "на m2 приехала версия 1" "версия 1" "$(cat "$M2")"
check "на m2 появился снимок" "версия 1" "$(cat "$STAND/m2/.claude/ccsync-copied-base/CLAUDE.md" 2>/dev/null)"

echo "ТЕСТ 2 — pull не затирает правку, ещё не отданную в хранилище"
echo "правка на m2" > "$M2"
out=$("$STAND/m2.sh" pull tools 2>&1)
check "правка на m2 уцелела" "правка на m2" "$(cat "$M2")"
check "pull сообщил, что файл оставлен" "да" \
	"$(echo "$out" | grep -q 'CLAUDE.md правлен здесь' && echo да || echo нет)"

echo "ТЕСТ 3 — push устаревшей копии не затирает чужую свежую правку"
"$STAND/m2.sh" push tools >/dev/null 2>&1
sync1
check "в хранилище правка m2" "правка на m2" "$(cat "$VAULT1")"
# m1 ещё не делал pull: у него нетронутая «версия 1». Её выгрузка — ошибка.
"$STAND/m1.sh" push tools >/dev/null 2>&1
sync2
check "push m1 не затёр правку m2" "правка на m2" "$(cat "$VAULT2")"
"$STAND/m1.sh" pull tools >/dev/null 2>&1
check "pull привёз правку m2 на m1" "правка на m2" "$(cat "$M1")"

echo "ТЕСТ 4 — правка на m1 после подтягивания уезжает как обычно"
echo "правка на m1" > "$M1"
"$STAND/m1.sh" push tools >/dev/null 2>&1
sync2; "$STAND/m2.sh" pull tools >/dev/null 2>&1
check "m2 получил правку m1" "правка на m1" "$(cat "$M2")"

echo "ТЕСТ 5 — первый запуск нового движка (снимка ещё нет)"
rm -rf "$STAND/m2/.claude/ccsync-copied-base"
echo "старое на m2" > "$M2"
"$STAND/m2.sh" pull tools >/dev/null 2>&1
check "взята версия из хранилища" "правка на m1" "$(cat "$M2")"
check "прежняя сохранена в .bak" "старое на m2" "$(cat "$M2.bak" 2>/dev/null)"
check "снимок создан" "правка на m1" "$(cat "$STAND/m2/.claude/ccsync-copied-base/CLAUDE.md" 2>/dev/null)"

echo "ТЕСТ 6 — свои файлы из tools/copied-files.json"
printf '["my-statusline.py", "../escape.txt", "sub/dir.txt"]\n' > "$STAND/m1vault/tools/copied-files.json"
echo "моя статус-строка" > "$STAND/m1/.claude/my-statusline.py"
echo "не в списке" > "$STAND/m1/.claude/unlisted.txt"
echo "побег" > "$STAND/m1/escape.txt"
"$STAND/m1.sh" push tools >/dev/null 2>&1
check "свой файл уехал в хранилище" "моя статус-строка" \
	"$(cat "$STAND/m1vault/tools/my-statusline.py" 2>/dev/null)"
check "файл не из списка не уехал" "нет" \
	"$([ -e "$STAND/m1vault/tools/unlisted.txt" ] && echo да || echo нет)"
check "имя с «..» проигнорировано" "нет" \
	"$([ -e "$STAND/m1vault/escape.txt" ] && echo да || echo нет)"
sync2; "$STAND/m2.sh" pull tools >/dev/null 2>&1
check "свой файл приехал на m2" "моя статус-строка" \
	"$(cat "$STAND/m2/.claude/my-statusline.py" 2>/dev/null)"

echo "ИТОГО: успешно $ok, провалено $fail"
rm -rf "$STAND"
exit $((fail > 0))
