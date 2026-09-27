#!/bin/bash
# --dry-run: push и pull показывают, что сделали бы, но не меняют ничего —
# ни хранилище, ни remote, ни файлы машины
set -u
BASE=${TMPDIR:-/tmp}/ccsync-tests
STAND=$BASE/dry-run
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
	mkdir -p $m/.claude/projects $m/work
	cat > "$STAND/$m.sh" <<EOF
#!/bin/bash
cd "$STAND/$m/work" || exit 1
exec env -u CLAUDE_CODE_SESSION_ID HOME="$STAND/$m" CLAUDE_CONFIG_DIR="$STAND/$m/.claude" \
     CCSYNC_NO_SYSTEMCTL=1 CCSYNC_LANG=ru \
     python3 "$STAND/${m}vault/bin/ccsync.py" "\$@"
EOF
	chmod +x "$STAND/$m.sh"
done
(cd m1vault && git add -A >/dev/null && git commit -qm "стенд" && git push -q -u origin master)
(cd m2vault && git fetch -q origin && git reset -q --hard origin/master &&
 git branch --set-upstream-to=origin/master master >/dev/null 2>&1)
"$STAND/m1.sh" init --id m1 --yes --note "машина 1" >/dev/null 2>&1
(cd "$STAND/m2vault" && git pull -q --rebase)
"$STAND/m2.sh" init --id m2 --yes --note "машина 2" >/dev/null 2>&1

# Отпечаток всего, что есть на машине и в её хранилище, плюс вершина remote.
print_state() {
	local m=$1
	(cd "$STAND/$m" && find . -type f -exec md5sum {} + | sort)
	(cd "$STAND/${m}vault" && git rev-parse HEAD && git status --porcelain)
	git -C "$STAND/remote.git" rev-parse master
}

# У m1 есть сессия в проекте work, уехавшая в хранилище.
SLUG=$(python3 -c "
import sys; sys.path.insert(0,'$SRC')
from ccsync_lib.paths import slug_for; print(slug_for('$STAND/m1/work'))")
SID=aaaaaaaa-1111-1111-1111-111111111111
TR=$STAND/m1/.claude/projects/$SLUG/$SID.jsonl
mkdir -p "$(dirname "$TR")"
printf '{"type":"user","cwd":"%s","sessionId":"%s","message":{"role":"user","content":"текст"}}\n' \
	"$STAND/m1/work" "$SID" > "$TR"
"$STAND/m1.sh" push all >/dev/null 2>&1

echo "ТЕСТ 1 — push --dry-run ничего не меняет, но показывает, что ушло бы"
echo "новая инструкция" > "$STAND/m1/.claude/CLAUDE.md"
python3 -c "
import json, os; p='$STAND/m1/.claude/settings.json'
d=json.load(open(p)) if os.path.exists(p) else {}
d['alpha']=1; open(p,'w').write(json.dumps(d))"
before=$(print_state m1)
out=$("$STAND/m1.sh" push all --dry-run 2>&1)
check "машина, хранилище и remote не тронуты" "$before" "$(print_state m1)"
check "в отчёте CLAUDE.md" "да" \
	"$(echo "$out" | sed -n '/Ушло бы/,$p' | grep -q 'tools/CLAUDE.md' && echo да || echo нет)"
check "в отчёте шаблон настроек" "да" \
	"$(echo "$out" | sed -n '/Ушло бы/,$p' | grep -q 'settings.template.json' && echo да || echo нет)"
"$STAND/m1.sh" push all >/dev/null 2>&1
check "настоящий push после этого отдаёт то же" "новая инструкция" \
	"$(git -C "$STAND/remote.git" show master:tools/CLAUDE.md)"

echo "ТЕСТ 2 — pull --dry-run не удаляет сессию, забытую на другой машине"
"$STAND/m2.sh" forget --yes --session $SID >/dev/null 2>&1
before=$(print_state m1)
out=$("$STAND/m1.sh" pull all --dry-run 2>&1)
check "машина, хранилище и remote не тронуты" "$before" "$(print_state m1)"
check "транскрипт на месте" "да" "$([ -e "$TR" ] && echo да || echo нет)"
check "в отчёте сказано, что удалил бы" "да" \
	"$(echo "$out" | grep -q 'dry-run.*забытые сессии' && echo да || echo нет)"
check "в отчёте пришедшая отметка" "да" \
	"$(echo "$out" | sed -n '/Пришло бы/,$p' | grep -q "tombstones/$SID" && echo да || echo нет)"
"$STAND/m1.sh" pull all >/dev/null 2>&1
check "контроль: настоящий pull транскрипт удаляет" "нет" "$([ -e "$TR" ] && echo да || echo нет)"

echo
echo "ИТОГО: успешно $ok, провалено $fail"
rm -rf "$STAND"
exit $((fail > 0))
