#!/bin/bash
# Подготовка стенда для демонстрационного GIF.
#
# Поднимает две «машины» (desktop и laptop) со своими $HOME, общее хранилище в
# bare-репозитории и одну настоящую сессию на первой машине. Дальше demo.tape
# просто выполняет команды: всё, что попадает в запись, — подлинный вывод
# движка, а не постановка.
set -eu
BASE=${BASE:-/tmp/ccsync-demo}
TEMPLATE=${TEMPLATE:-$(cd "$(dirname "$0")/.." && pwd)}
rm -rf "$BASE"; mkdir -p "$BASE"

git init -q --bare --initial-branch=main "$BASE/vault.git"

machine() {                       # имя, id, дистрибутив
	local home=$BASE/$1
	mkdir -p "$home/.claude/projects" "$home/projects/myapp"
	git clone -q "$TEMPLATE" "$home/claude-code-sync"
	(cd "$home/claude-code-sync"
	 git remote set-url origin "$BASE/vault.git"
	 git config user.email demo@example.com
	 git config user.name "$1")
	# Запускаемся из каталога проекта: движок определяет сессию по тому, где
	# она была начата, — ровно так его зовёт и сам Claude Code.
	cat > "$BASE/$1.sh" <<EOF
#!/bin/bash
cd "$home/projects/myapp" || exit 1
exec env -u CLAUDE_CODE_SESSION_ID HOME="$home" CLAUDE_CONFIG_DIR="$home/.claude" \
     CCSYNC_LANG=en python3 "$home/claude-code-sync/bin/ccsync.py" "\$@"
EOF
	chmod +x "$BASE/$1.sh"
}

machine desktop
machine laptop

# Сессия, которую будем переносить: настоящий транскрипт в том виде,
# в каком его пишет Claude Code — по одной JSON-записи на строку.
slug=$(python3 -c "import re,sys; print(re.sub(r'[^A-Za-z0-9]', '-', sys.argv[1]))" \
	"$BASE/desktop/projects/myapp")
mkdir -p "$BASE/desktop/.claude/projects/$slug"
python3 - "$BASE/desktop/.claude/projects/$slug/3f2a9c1e-0b47-4d8a-9e15-7c2b6a4f8d03.jsonl" \
         "$BASE/desktop/projects/myapp" <<'PY'
import json, sys
path, cwd = sys.argv[1], sys.argv[2]
turns = [
	("user", "Add offline caching to the sync layer"),
	("assistant", "Looked through SyncRepository — the retry policy is the tricky part…"),
	("user", "Right. Let's keep the failed batch and replay it on reconnect."),
]
with open(path, "w", encoding="utf-8") as fh:
	for i, (role, text) in enumerate(turns):
		# Разделители без пробелов — ровно так пишет транскрипт сам Claude Code.
		fh.write(json.dumps({
			"type": role, "uuid": f"0000000{i}-0000-4000-8000-00000000000{i}",
			"cwd": cwd, "message": {"role": role, "content": text},
		}, ensure_ascii=False, separators=(",", ":")) + "\n")
PY

# Память: у фактов разные scope, чтобы в кадре было видно, как машина получает
# свой срез, а не всё подряд.
facts=$BASE/desktop/claude-code-sync/memory/facts
mkdir -p "$facts"
write_fact() {                    # файл, scope, заголовок, суть
	cat > "$facts/$1.md" <<EOF
---
name: $1
description: $3
metadata:
  type: project
  scope: $2
  index_title: "$3"
  index_hook: "$4"
---
$4
EOF
}
write_fact api-contract        global   "API contract"      "The sync endpoint returns 409 on a stale cursor."
write_fact release-checklist   global   "Release checklist" "Tag, changelog, then the store build."
write_fact desktop-toolchain   desktop  "Desktop toolchain" "Android SDK lives outside the home directory here."
write_fact laptop-battery      laptop   "Laptop quirk"      "Gradle daemon is capped to 2 workers on battery."

"$BASE/desktop.sh" init --id desktop --yes --note "main workstation" >/dev/null
"$BASE/desktop.sh" bind myapp "$BASE/desktop/projects/myapp" >/dev/null
(cd "$BASE/desktop/claude-code-sync" && git push -q -u origin main)
"$BASE/laptop.sh" init --id laptop --yes --note "travel laptop" >/dev/null

cat > "$BASE/env.sh" <<EOF
# Окружение для записи: переключение между «машинами» — это подмена \$HOME,
# поэтому в кадре видны обычные команды, а не обёртки поверх них.
BASE=$BASE

use_machine() {
	export HOME="\$BASE/\$1"
	export CLAUDE_CONFIG_DIR="\$HOME/.claude"
	export CCSYNC_LANG=en
	unset CLAUDE_CODE_SESSION_ID
	cd "\$HOME/projects/myapp" || return 1
	if [ "\$1" = desktop ]; then
		PS1='\[\033[38;5;114m\]desktop\[\033[0m\]:~/projects/myapp\$ '
	else
		PS1='\[\033[38;5;217m\]laptop\[\033[0m\]:~/projects/myapp\$ '
	fi
}

ccsync() { python3 "\$HOME/claude-code-sync/bin/ccsync.py" "\$@"; }

use_machine desktop
EOF

echo "стенд готов: $BASE"
