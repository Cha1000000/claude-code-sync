#!/bin/bash
# Стенд: пустой старт заготовки на двух чистых машинах.
#
# Проверяет сам шаблон, а не движок (для движка есть tools/tests/run-all.sh):
# первая машина заводит из заготовки своё хранилище, вторая его принимает, и
# ничьи личные файлы при этом не затираются. Клонирует ЗАКОММИЧЕННОЕ состояние
# репозитория, так что незакоммиченные правки стенд не увидит.
# Проверяет, что первая машина заводит хранилище из заготовки, а вторая его
# принимает, и что личные файлы человека при этом не затираются.
set -u
BASE=${TMPDIR:-/tmp}/ccsync-fresh
TEMPLATE=${TEMPLATE:-$(cd "$(dirname "$0")/.." && pwd)}
rm -rf "$BASE"; mkdir -p "$BASE"; cd "$BASE"

ok=0; fail=0
check() {
	if [ "$2" = "$3" ]; then echo "  ✓ $1"; ok=$((ok+1))
	else echo "  ✗ $1 — ожидали [$2], получили [$3]"; fail=$((fail+1)); fi
}

git init -q --bare --initial-branch=main "$BASE/vault.git"

# Машина, как её застаёт человек: свои правила, свои настройки, свой скилл,
# своя память. Ничего из этого пропасть не должно.
setup_machine() {
	local home=$BASE/$1
	mkdir -p "$home/.claude/skills/my-skill" "$home/.claude/projects/$(echo "$home" | sed 's/[^A-Za-z0-9]/-/g')/memory"
	printf '# МОИ ПРАВИЛА\nОтвечай кратко.\n' > "$home/.claude/CLAUDE.md"
	printf '{\n  "model": "opus",\n  "theme": "dark"\n}\n' > "$home/.claude/settings.json"
	cat > "$home/.claude/skills/my-skill/SKILL.md" <<'SKILL'
---
name: my-skill
---
Мой скилл.
SKILL
	cat > "$home/.claude/projects/$(echo "$home" | sed 's/[^A-Za-z0-9]/-/g')/memory/my-fact.md" <<'FACT'
---
name: my-fact
description: тестовый факт
metadata:
  type: user
---
Я пользуюсь Kotlin.
FACT
	cat > "$BASE/$1.sh" <<EOF
#!/bin/bash
cd "$home" || exit 1
exec env -u CLAUDE_CODE_SESSION_ID HOME="$home" CLAUDE_CONFIG_DIR="$home/.claude" \
     python3 "$home/claude-code-sync/bin/ccsync.py" "\$@"
EOF
	chmod +x "$BASE/$1.sh"
}

echo "ТЕСТ 1 — первая машина заводит хранилище из шаблона"
setup_machine a
git clone -q "$TEMPLATE" "$BASE/a/claude-code-sync"
(cd "$BASE/a/claude-code-sync" && git remote set-url origin "$BASE/vault.git" &&
 git config user.email t@t && git config user.name a && git push -q -u origin main)
out=$("$BASE/a.sh" init --id linux-desktop --yes --note "первая машина" 2>&1)
check "init прошёл" "0" "$?"
check "паспорт машины заведён" "да" \
	"$([ -e "$BASE/a/.claude/ccsync-machine.json" ] && echo да || echo нет)"

echo "ТЕСТ 2 — adopt забирает то, что на машине уже было"
"$BASE/a.sh" adopt >/dev/null 2>&1
check "скилл уехал в хранилище" "да" \
	"$([ -e "$BASE/a/claude-code-sync/tools/skills/my-skill/SKILL.md" ] && echo да || echo нет)"
check "локальные скиллы стали симлинком" "да" \
	"$([ -L "$BASE/a/.claude/skills" ] && echo да || echo нет)"
check "факт памяти уехал в хранилище" "да" \
	"$([ -e "$BASE/a/claude-code-sync/memory/facts/my-fact.md" ] && echo да || echo нет)"
check "резервная копия сделана" "да" \
	"$(ls -d "$BASE/a/.claude/backups/"* >/dev/null 2>&1 && echo да || echo нет)"

echo "ТЕСТ 3 — push отдаёт состояние в пустое хранилище"
out=$("$BASE/a.sh" push all 2>&1); code=$?
check "push завершился без ошибки" "0" "$code"
check "шаблон настроек собран из локальных" "1" \
	"$(grep -c '"model"' "$BASE/a/claude-code-sync/tools/settings.template.json")"

echo "ТЕСТ 4 — установщик хуков не трогает чужие настройки"
env -u CLAUDE_CODE_SESSION_ID HOME="$BASE/a" CLAUDE_CONFIG_DIR="$BASE/a/.claude" \
	python3 "$BASE/a/claude-code-sync/setup-hooks.py" >/dev/null 2>&1
check "CLAUDE.md остался своим" "1" "$(grep -c 'МОИ ПРАВИЛА' "$BASE/a/.claude/CLAUDE.md")"
check "своя настройка theme уцелела" "1" "$(grep -c theme "$BASE/a/.claude/settings.json")"
check "своя настройка model уцелела" "1" "$(grep -c model "$BASE/a/.claude/settings.json")"
check "хуки прописаны" "1" \
	"$(grep -c 'cchook.py session-start' "$BASE/a/.claude/settings.json")"
check "повторный запуск не задваивает" "1" \
	"$(env -u CLAUDE_CODE_SESSION_ID HOME="$BASE/a" CLAUDE_CONFIG_DIR="$BASE/a/.claude" \
	   python3 "$BASE/a/claude-code-sync/setup-hooks.py" 2>&1 | grep -c 'уже на месте')"
"$BASE/a.sh" push tools >/dev/null 2>&1
check "хуки уехали токенами, без путей машины" "4" \
	"$(grep -c '{{PYTHON}} {{VAULT}}/bin/cchook.py' "$BASE/a/claude-code-sync/tools/settings.template.json")"

echo "ТЕСТ 5 — вторая машина принимает состояние"
setup_machine b
git clone -q "$BASE/vault.git" "$BASE/b/claude-code-sync"
(cd "$BASE/b/claude-code-sync" && git config user.email t@t && git config user.name b)
"$BASE/b.sh" init --id work-laptop --yes --note "вторая машина" >/dev/null 2>&1
out=$("$BASE/b.sh" pull all 2>&1); code=$?
check "pull завершился без ошибки" "0" "$code"
check "скилл первой машины доехал" "1" \
	"$(grep -c 'Мой скилл' "$BASE/b/.claude/skills/my-skill/SKILL.md" 2>/dev/null || echo 0)"
check "команды разложены" "да" \
	"$([ -e "$BASE/b/.claude/commands/sync-push.md" ] && echo да || echo нет)"
check "CLAUDE.md второй машины не затёрт" "1" "$(grep -c 'МОИ ПРАВИЛА' "$BASE/b/.claude/CLAUDE.md")"
check "обе машины в реестре" "2" "$(ls "$BASE/b/claude-code-sync/machines/"*.json | wc -l)"
memfile=$(find "$BASE/b/.claude/projects" -name MEMORY.md | head -1)
check "MEMORY.md собран" "да" "$([ -n "$memfile" ] && echo да || echo нет)"
check "хуки доехали и развернулись под вторую машину" "1" \
	"$(grep -c "$BASE/b/claude-code-sync/bin/cchook.py session-start" "$BASE/b/.claude/settings.json")"
check "в настройках второй машины не осталось токенов" "0" \
	"$(grep -c '{{' "$BASE/b/.claude/settings.json")"
check "факт виден на второй машине" "1" \
	"$(grep -c 'my-fact\|Kotlin' "$memfile" 2>/dev/null || echo 0)"

echo "ТЕСТ 6 — стенды движка работают из клона шаблона"
res=$(cd "$BASE/b/claude-code-sync" && timeout 600 bash tools/tests/run-all.sh 2>&1 | tail -1)
check "стенды прошли" "1" "$(echo "$res" | grep -c 'провалено 0')"

echo
echo "ИТОГО: успешно $ok, провалено $fail"
exit $fail
