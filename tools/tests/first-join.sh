#!/bin/bash
# Подключение машины, где Claude Code уже жил до ccsync: хранилище не затирается,
# прежнее машины сохраняется, её память и MCP не разъезжаются на всех молча
set -u
BASE=${TMPDIR:-/tmp}/ccsync-tests
STAND=$BASE/first-join
SRC=$(cd "$(dirname "$0")/../../bin" && pwd)
rm -rf "$STAND"; mkdir -p "$STAND"; cd "$STAND"

ok=0; fail=0
check() {
	if [ "$2" = "$3" ]; then echo "  ✓ $1"; ok=$((ok+1))
	else echo "  ✗ $1 — ожидали [$2], получили [$3]"; fail=$((fail+1)); fi
}

# Поддельный claude — как в tools-merge.sh: настоящий CLI на стенде звать нельзя.
mkdir -p "$STAND/fakebin"
cat > "$STAND/fakebin/claude" <<'EOF'
#!/usr/bin/env python3
import json, os, sys
from pathlib import Path
args = sys.argv[1:]
if args[:1] == ["--version"]:
	print("9.9.9 (Claude Code)"); sys.exit(0)
path = Path(os.environ["HOME"]) / ".claude.json"
data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
servers = data.setdefault("mcpServers", {})
if args[:2] == ["mcp", "add-json"]:
	servers[args[2]] = json.loads(args[3])
elif args[:2] == ["mcp", "remove"]:
	servers.pop(args[2], None)
else:
	sys.exit(2)
path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
EOF
chmod +x "$STAND/fakebin/claude"

git init -q --bare --initial-branch=master remote.git
git clone -q remote.git m1vault 2>/dev/null
mkdir -p m1vault/bin && cp -r "$SRC"/* m1vault/bin/ && rm -rf m1vault/bin/ccsync_lib/__pycache__
cp "$SRC/../.gitignore" m1vault/.gitignore
for m in m1 m2; do
	mkdir -p $m/.claude/projects
	cat > "$STAND/$m.sh" <<EOF
#!/bin/bash
cd "$STAND/$m" || exit 1
exec env -u CLAUDE_CODE_SESSION_ID HOME="$STAND/$m" CLAUDE_CONFIG_DIR="$STAND/$m/.claude" \
     CCSYNC_NO_SYSTEMCTL=1 CCSYNC_LANG=ru PATH="$STAND/fakebin:\$PATH" \
     python3 "$STAND/${m}vault/bin/ccsync.py" "\$@"
EOF
	chmod +x "$STAND/$m.sh"
done
(cd m1vault && git config user.email t@t && git config user.name "stand m1" &&
 git add -A >/dev/null && git commit -qm "стенд" && git push -q -u origin master)

# ── m1: живая машина, хранилище уже наполнено
C1=$STAND/m1/.claude
mkdir -p $C1/skills/shared $C1/commands
echo "НОВАЯ версия скилла" > $C1/skills/shared/SKILL.md
echo "команда с m1" > $C1/commands/cmd.md
echo "НОВЫЙ CLAUDE.md" > $C1/CLAUDE.md
echo '{"model":"opus","env":{"FROM_M1":"1"}}' > $C1/settings.json
echo '{"mcpServers":{"shared-mcp":{"type":"stdio","command":"echo","args":["m1"]}}}' > $STAND/m1/.claude.json
M1MEM=$C1/projects/$(python3 -c "import re,sys;print(re.sub(r'[^A-Za-z0-9]','-',sys.argv[1]))" "$STAND/m1")/memory
mkdir -p "$M1MEM"
printf -- '---\nname: first\nmetadata:\n  type: user\n---\nпамять первой машины\n' > "$M1MEM/first_note.md"
"$STAND/m1.sh" init --id m1 --yes --note "живая" >/dev/null
"$STAND/m1.sh" adopt >/dev/null 2>&1
FIRST_ADOPTED=$([ -f "$STAND/m1vault/memory/facts/first_note.md" ] && echo yes || echo no)
mkdir -p "$STAND/m1vault/memory/facts"
printf -- '---\nname: f1\nmetadata:\n  type: user\n  scope: global\n  index_title: "Общий факт"\n  index_hook: "с m1"\n---\nтекст\n' \
	> "$STAND/m1vault/memory/facts/f1.md"
"$STAND/m1.sh" push all >/dev/null 2>&1
"$STAND/m1.sh" pull tools >/dev/null 2>&1

# ── m2: давняя машина — свой ~/.claude, ccsync никогда не стоял
C2=$STAND/m2/.claude
mkdir -p $C2/skills/shared $C2/skills/local-only
echo "СТАРАЯ версия скилла" > $C2/skills/shared/SKILL.md
echo "скилл только с этой машины" > $C2/skills/local-only/SKILL.md
echo "СТАРЫЙ CLAUDE.md" > $C2/CLAUDE.md
echo '{"model":"sonnet","env":{"LOCAL_ONLY":"1"}}' > $C2/settings.json
echo '{"mcpServers":{"local-mcp":{"type":"stdio","command":"echo","args":["local"]}}}' > $STAND/m2/.claude.json
MEM=$C2/projects/$(python3 -c "import re,sys;print(re.sub(r'[^A-Za-z0-9]','-',sys.argv[1]))" "$STAND/m2")/memory
mkdir -p "$MEM"
printf '%s\n' "- [Заметка машины](local_note.md) — только здесь" "Прямо в индексе: важное знание" > "$MEM/MEMORY.md"
echo "давнее знание этой машины" > "$MEM/local_note.md"
printf -- '---\nname: scoped\nmetadata:\n  type: project\n  scope: m2\n  index_title: "Размеченная"\n  index_hook: "со scope"\n---\nтекст\n' \
	> "$MEM/scoped_note.md"
# Память другого проекта — Claude Code ведёт её в каталоге каждого проекта
mkdir -p "$C2/projects/-work-other-proj/memory"
echo "заметка другого проекта" > "$C2/projects/-work-other-proj/memory/other_note.md"
touch -d '2025-06-01' $C2/skills/shared/SKILL.md $C2/skills/local-only/SKILL.md $C2/CLAUDE.md $C2/settings.json

# Шаги BOOTSTRAP: клон живого хранилища, init, pull all
git clone -q remote.git m2vault 2>/dev/null
(cd m2vault && git config user.email t@t && git config user.name "stand m2")
"$STAND/m2.sh" init --id m2 --yes --note "давняя" >/dev/null
pull_out=$("$STAND/m2.sh" pull all --no-autobind 2>&1)

V=$STAND/m2vault/tools
sync1() { (cd "$STAND/m1vault" && git pull -q --rebase 2>/dev/null); }

echo "ТЕСТ 1 — pull на давней машине сохраняет её прежнее"
check "CLAUDE.md → .bak" "СТАРЫЙ CLAUDE.md" "$(cat $C2/CLAUDE.md.bak 2>/dev/null)"
grep -q LOCAL_ONLY $C2/settings.json.bak 2>/dev/null && kept=yes || kept=no
check "settings.json → .bak" yes "$kept"
grep -q "Прямо в индексе" "$MEM/MEMORY.md.bak" 2>/dev/null && kept=yes || kept=no
check "прежний MEMORY.md → .bak" yes "$kept"
echo "$pull_out" | grep -q "прежний MEMORY.md этой машины сохранён" && told=yes || told=no
check "про сохранение сказано" yes "$told"
head -1 "$MEM/MEMORY.md" | grep -q "^# Memory index" && gen=yes || gen=no
check "на месте — сгенерированный индекс" yes "$gen"
grep -qx '<!-- ccsync: generated -->' "$MEM/MEMORY.md" && marked=yes || marked=no
check "у индекса есть непереводимая метка" yes "$marked"

echo "ТЕСТ 2 — повторный pull не плодит копий"
"$STAND/m2.sh" pull memory >/dev/null 2>&1
check "копия MEMORY.md одна" 1 "$(ls "$MEM" | grep -c '^MEMORY.md.bak')"
# Индекс, записанный до появления метки, тоже свой: копии быть не должно
grep -vx '<!-- ccsync: generated -->' "$MEM/MEMORY.md" > "$MEM/old-style" && mv "$MEM/old-style" "$MEM/MEMORY.md"
"$STAND/m2.sh" pull memory >/dev/null 2>&1
check "старый индекс без метки — тоже без копии" 1 "$(ls "$MEM" | grep -c '^MEMORY.md.bak')"

echo "ТЕСТ 3 — MCP этой машины можно пометить до push"
mcp_out=$("$STAND/m2.sh" mcp 2>&1)
echo "$mcp_out" | grep "local-mcp" | grep -q "только здесь" && shown=yes || shown=no
check "mcp показывает сервер, которого нет в хранилище" yes "$shown"
"$STAND/m2.sh" mcp scope local-mcp --here >/dev/null 2>&1
check "scope --here принят" m2 "$(python3 -c "import json;print(json.load(open('$V/mcp-scopes.json')).get('local-mcp'))" 2>/dev/null)"

echo "ТЕСТ 4 — adopt не делает заметки без scope общими"
check "на первой машине хранилища заметка без scope переносится" yes "$FIRST_ADOPTED"
adopt_out=$("$STAND/m2.sh" adopt 2>&1)
[ -f "$STAND/m2vault/memory/facts/local_note.md" ] && moved=yes || moved=no
check "заметка без scope не перенесена" no "$moved"
[ -f "$STAND/m2vault/memory/facts/scoped_note.md" ] && moved=yes || moved=no
check "заметка со scope перенесена" yes "$moved"
echo "$adopt_out" | grep -q "local_note.md" && told=yes || told=no
check "про неперенесённую сказано" yes "$told"
echo "$adopt_out" | grep -q -- "-work-other-proj/memory" && told=yes || told=no
check "про память другого проекта сказано" yes "$told"
[ -L $C2/skills ] && linked=yes || linked=no
check "skills стал симлинком" yes "$linked"

echo "ТЕСТ 5 — push давней машины не затирает хранилище"
"$STAND/m2.sh" push all >/dev/null 2>&1
sync1
V1=$STAND/m1vault/tools
check "свежий скилл остался" "НОВАЯ версия скилла" "$(cat $V1/skills/shared/SKILL.md)"
check "CLAUDE.md остался" "НОВЫЙ CLAUDE.md" "$(cat $V1/CLAUDE.md)"
check "настройки остались" "opus {'FROM_M1': '1'}" \
	"$(python3 -c "import json;d=json.load(open('$V1/settings.template.json'));print(d.get('model'),d.get('env'))")"
check "скилл только с этой машины уехал" "скилл только с этой машины" "$(cat $V1/skills/local-only/SKILL.md 2>/dev/null)"

echo "ТЕСТ 6 — помеченный сервер в шаблоне есть, но живой машине не ставится"
python3 -c "import json,sys;sys.exit(0 if 'local-mcp' in json.load(open('$V1/mcp-servers.template.json')) else 1)" && tpl=yes || tpl=no
check "local-mcp в шаблоне" yes "$tpl"
"$STAND/m1.sh" pull tools >/dev/null 2>&1
servers=$(python3 -c "import json;print(' '.join(sorted(json.load(open('$STAND/m1/.claude.json'))['mcpServers'])))")
check "у m1 только свой сервер" "shared-mcp" "$servers"

echo "ТЕСТ 7 — нечитаемый чужой MEMORY.md не затирается"
printf 'чужая память\n' > "$MEM/MEMORY.md"; chmod 200 "$MEM/MEMORY.md"
out=$("$STAND/m2.sh" pull memory 2>&1)
chmod 600 "$MEM/MEMORY.md"
check "содержимое цело" "чужая память" "$(cat "$MEM/MEMORY.md")"
echo "$out" | grep -q "MEMORY.md не обновлён" && told=yes || told=no
check "про это сказано" yes "$told"
"$STAND/m2.sh" pull memory >/dev/null 2>&1
check "после починки прав — сохранён в новую копию" 2 "$(ls "$MEM" | grep -c '^MEMORY.md.bak')"

echo "ИТОГО: успешно $ok, провалено $fail"
exit $((fail > 0))
