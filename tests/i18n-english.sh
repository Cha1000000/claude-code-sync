#!/bin/bash
# Стенд: в английском режиме вывод английский — везде.
#
# Проверка от обратного: гоняем команды с CCSYNC_LANG=en и ищем в выводе
# кириллицу. Нашли — значит строку забыли обернуть в tr() или перевести;
# tests/i18n-coverage.py такого не поймает, потому что для него необёрнутой
# строки просто не существует.
set -u
BASE=${TMPDIR:-/tmp}/ccsync-i18n
TEMPLATE=${TEMPLATE:-$(cd "$(dirname "$0")/.." && pwd)}
rm -rf "$BASE"; mkdir -p "$BASE"; cd "$BASE"

ok=0; fail=0
check() {
	if [ "$2" = "$3" ]; then echo "  ✓ $1"; ok=$((ok+1))
	else echo "  ✗ $1 — ожидали [$2], получили [$3]"; fail=$((fail+1)); fi
}

# Русский текст в выводе английского режима — это и есть провал.
cyrillic() { echo "$1" | grep -coP '[а-яА-ЯёЁ]' || true; }

git init -q --bare --initial-branch=main "$BASE/vault.git"
mkdir -p "$BASE/home/.claude"
git clone -q "$TEMPLATE" "$BASE/home/claude-code-sync"
(cd "$BASE/home/claude-code-sync" && git remote set-url origin "$BASE/vault.git" &&
 git config user.email t@t && git config user.name t && git push -q -u origin main)

run() {
	env -u CLAUDE_CODE_SESSION_ID HOME="$BASE/home" CLAUDE_CONFIG_DIR="$BASE/home/.claude" \
		CCSYNC_LANG=en LANG=en_US.UTF-8 \
		python3 "$BASE/home/claude-code-sync/bin/ccsync.py" "$@" 2>&1
}

echo "ТЕСТ 1 — справка"
check "ccsync --help без кириллицы" "0" "$(cyrillic "$(run --help)")"
check "push --help без кириллицы" "0" "$(cyrillic "$(run push --help)")"
check "forget --help без кириллицы" "0" "$(cyrillic "$(run forget --help)")"

echo "ТЕСТ 2 — работа с хранилищем"
check "init без кириллицы" "0" "$(cyrillic "$(run init --id en-box --yes --note test)")"
check "push all без кириллицы" "0" "$(cyrillic "$(run push all)")"
check "pull all без кириллицы" "0" "$(cyrillic "$(run pull all)")"
check "status без кириллицы" "0" "$(cyrillic "$(run status)")"
check "machines без кириллицы" "0" "$(cyrillic "$(run machines)")"
check "mcp без кириллицы" "0" "$(cyrillic "$(run mcp)")"
check "ignore --list без кириллицы" "0" "$(cyrillic "$(run ignore --list)")"
check "bind без кириллицы" "0" "$(cyrillic "$(run bind demo "$BASE/home")")"

echo "ТЕСТ 3 — вывод действительно английский, а не пустой"
check "status говорит по-английски" "1" "$(run status | grep -c '^Machine:')"
check "init сообщил про паспорт" "1" \
	"$(run init --id en-box --yes 2>&1 | grep -c 'already configured')"

echo "ТЕСТ 4 — блок «где ты» для Клода"
context=$(env -u CLAUDE_CODE_SESSION_ID HOME="$BASE/home" CLAUDE_CONFIG_DIR="$BASE/home/.claude" \
	CCSYNC_LANG=en python3 "$BASE/home/claude-code-sync/bin/session-context.py" 2>&1)
check "контекст машины без кириллицы" "0" "$(cyrillic "$context")"
check "контекст машины не пуст" "1" "$(echo "$context" | grep -c '\[ccsync\] Machine:')"

echo "ТЕСТ 5 — MEMORY.md собирается по-английски"
mkdir -p "$BASE/home/claude-code-sync/memory/facts"
cat > "$BASE/home/claude-code-sync/memory/facts/demo.md" <<'FACT'
---
name: demo
description: demo fact
metadata:
  type: user
  scope: global
  index_title: "Demo"
  index_hook: "a demo fact"
---
Body.
FACT
run pull memory >/dev/null 2>&1
memfile=$(find "$BASE/home/.claude/projects" -name MEMORY.md | head -1)
check "MEMORY.md собран" "да" "$([ -n "$memfile" ] && echo да || echo нет)"
check "MEMORY.md без кириллицы" "0" "$(cyrillic "$(cat "$memfile" 2>/dev/null)")"
check "заголовок переведён" "1" "$(grep -c '^# Memory index — machine:' "$memfile" 2>/dev/null || echo 0)"

echo "ТЕСТ 6 — русский режим остаётся русским"
ru=$(env -u CLAUDE_CODE_SESSION_ID HOME="$BASE/home" CLAUDE_CONFIG_DIR="$BASE/home/.claude" \
	CCSYNC_LANG=ru python3 "$BASE/home/claude-code-sync/bin/ccsync.py" status 2>&1)
check "status по-русски" "1" "$(echo "$ru" | grep -c '^Машина:')"

echo
echo "ИТОГО: успешно $ok, провалено $fail"
exit $fail
