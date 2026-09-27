#!/bin/bash
# settings.json, MCP-серверы и плагины: push отдаёт только изменённое здесь —
# устаревшая копия не откатывает правки, сделанные на другой машине (27.09)
set -u
BASE=${TMPDIR:-/tmp}/ccsync-tests
STAND=$BASE/tools-merge
SRC=$(cd "$(dirname "$0")/../../bin" && pwd)
rm -rf "$STAND"; mkdir -p "$STAND"; cd "$STAND"

ok=0; fail=0
check() {
	if [ "$2" = "$3" ]; then echo "  ✓ $1"; ok=$((ok+1))
	else echo "  ✗ $1 — ожидали [$2], получили [$3]"; fail=$((fail+1)); fi
}

# Поддельный claude: pull ставит MCP-серверы через `claude mcp add-json`, а
# настоящий CLI на стенде звать нельзя — он медленный и живёт своей жизнью.
# Подделка делает с ~/.claude.json ровно то, что делает настоящая команда.
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
	if "broken" in args[2] or "broken" in args[3]:
		print("не удалось", file=sys.stderr); sys.exit(1)
	servers[args[2]] = json.loads(args[3])
elif args[:2] == ["mcp", "remove"]:
	servers.pop(args[2], None)
else:
	sys.exit(2)
path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
EOF
chmod +x "$STAND/fakebin/claude"

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
     CCSYNC_NO_SYSTEMCTL=1 CCSYNC_LANG=ru PATH="$STAND/fakebin:\$PATH" \
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

sync1() { (cd "$STAND/m1vault" && git pull -q --rebase 2>/dev/null); }
sync2() { (cd "$STAND/m2vault" && git pull -q --rebase 2>/dev/null); }

# jset <файл JSON> <python-код над d> — правка JSON на месте, как руками.
jset() {
	python3 - "$1" "$2" <<'EOF'
import json, sys, os
path, code = sys.argv[1], sys.argv[2]
d = json.load(open(path, encoding="utf-8")) if os.path.exists(path) else {}
exec(code)
os.makedirs(os.path.dirname(path), exist_ok=True)
open(path, "w", encoding="utf-8").write(json.dumps(d, ensure_ascii=False, indent=2) + "\n")
EOF
}
# jget <файл JSON> <выражение над d> — прочитать значение; нет — «нет».
jget() {
	python3 - "$1" "$2" <<'EOF' 2>/dev/null || echo нет
import json, sys
d = json.load(open(sys.argv[1], encoding="utf-8"))
try:
	print(eval(sys.argv[2]))
except (KeyError, TypeError):
	print("нет")
EOF
}

S1=$STAND/m1/.claude/settings.json
S2=$STAND/m2/.claude/settings.json
T1=$STAND/m1vault/tools/settings.template.json
T2=$STAND/m2vault/tools/settings.template.json

# Исходное состояние: обе машины синхронны и у обеих есть снимки.
jset "$S1" 'd["alpha"] = 1'
"$STAND/m1.sh" push tools >/dev/null 2>&1
"$STAND/m2.sh" pull tools >/dev/null 2>&1

echo "ТЕСТ 1 — исходная синхронизация (проверка самого стенда)"
check "m2 получил настройку m1" "1" "$(jget "$S2" 'd["alpha"]')"

echo "ТЕСТ 2 — устаревшая машина не откатывает чужие ключи"
# m1 поменял значение и завёл новый раздел; m2 ничего не подтягивал.
jset "$S1" 'd["alpha"] = 2; d["skillOverrides"] = {"x": "off"}'
"$STAND/m1.sh" push tools >/dev/null 2>&1
"$STAND/m2.sh" push tools >/dev/null 2>&1
sync1
check "изменённое значение m1 уцелело" "2" "$(jget "$T1" 'd["alpha"]')"
check "новый раздел m1 уцелел" "off" "$(jget "$T1" 'd["skillOverrides"]["x"]')"
# Ловушка: если после push снимком станет шаблон, ключи m1, которых у m2
# ещё нет, при следующем push m2 сочтут «удалёнными здесь» и откатят.
"$STAND/m2.sh" push tools >/dev/null 2>&1
sync1
check "повторный push m2 без pull тоже ничего не откатил" "2 off" \
	"$(jget "$T1" 'd["alpha"]') $(jget "$T1" 'd["skillOverrides"]["x"]')"
"$STAND/m2.sh" pull tools >/dev/null 2>&1
check "pull привёз значение на m2" "2" "$(jget "$S2" 'd["alpha"]')"

echo "ТЕСТ 3 — правки разных ключей на двух машинах доезжают обе"
jset "$S1" 'd["beta"] = "m1"'
"$STAND/m1.sh" push tools >/dev/null 2>&1
jset "$S2" 'd["gamma"] = "m2"'
"$STAND/m2.sh" push tools >/dev/null 2>&1
sync1
check "ключ m1 на месте" "m1" "$(jget "$T1" 'd["beta"]')"
check "ключ m2 доехал" "m2" "$(jget "$T1" 'd["gamma"]')"

echo "ТЕСТ 4 — один ключ правили обе: побеждает здешнее, как в pull"
"$STAND/m1.sh" pull tools >/dev/null 2>&1
"$STAND/m2.sh" pull tools >/dev/null 2>&1
jset "$S1" 'd["delta"] = "one"'
"$STAND/m1.sh" push tools >/dev/null 2>&1
jset "$S2" 'd["delta"] = "two"'
"$STAND/m2.sh" push tools >/dev/null 2>&1
sync1
check "в хранилище значение последней правившей машины" "two" "$(jget "$T1" 'd["delta"]')"

echo "ТЕСТ 4б — после своей отправки машина принимает чужую правку того же ключа"
# m2 отдал delta=two. Теперь m1 (подтянув) правит delta снова. Для m2 это
# чистое изменение на той стороне — его надо принять, а не считать конфликтом.
"$STAND/m1.sh" pull tools >/dev/null 2>&1
jset "$S1" 'd["delta"] = "three"'
"$STAND/m1.sh" push tools >/dev/null 2>&1
"$STAND/m2.sh" pull tools >/dev/null 2>&1
check "m2 принял новое значение m1" "three" "$(jget "$S2" 'd["delta"]')"

echo "ТЕСТ 5 — удаление ключа здесь уходит в хранилище"
"$STAND/m2.sh" pull tools >/dev/null 2>&1
jset "$S2" 'd.pop("gamma")'
"$STAND/m2.sh" push tools >/dev/null 2>&1
sync1
check "ключ удалён из шаблона" "нет" "$(jget "$T1" 'd["gamma"]')"
check "соседний ключ не пострадал" "m1" "$(jget "$T1" 'd["beta"]')"

echo "ТЕСТ 5б — ключ, добавленный здесь до pull, не теряется"
jset "$S2" 'd["kappa"] = "до pull"'
"$STAND/m2.sh" pull tools >/dev/null 2>&1
"$STAND/m2.sh" push tools >/dev/null 2>&1
sync1
check "неотданный ключ уехал после pull" "до pull" "$(jget "$T1" 'd["kappa"]')"

echo "ТЕСТ 6 — без снимка настройки не отдаются"
"$STAND/m1.sh" pull tools >/dev/null 2>&1
jset "$S1" 'd["alpha"] = 3'
"$STAND/m1.sh" push tools >/dev/null 2>&1
rm -f "$STAND/m2/.claude/.ccsync-settings-base.json"
out=$("$STAND/m2.sh" push tools 2>&1)
sync1
check "значение m1 уцелело" "3" "$(jget "$T1" 'd["alpha"]')"
check "push сказал, что settings.json не отдан" "да" \
	"$(echo "$out" | grep -q 'не отдано.*settings.json' && echo да || echo нет)"
"$STAND/m2.sh" pull tools >/dev/null 2>&1
check "после pull снимок снова есть" "да" \
	"$([ -e "$STAND/m2/.claude/.ccsync-settings-base.json" ] && echo да || echo нет)"

echo "ТЕСТ 7 — пути хуков в шаблоне остаются токенами"
check "в шаблоне нет абсолютных путей машин" "нет" \
	"$(grep -q "$STAND" "$T1" && echo да || echo нет)"

C1=$STAND/m1/.claude.json
C2=$STAND/m2/.claude.json
M1=$STAND/m1vault/tools/mcp-servers.template.json
srv() { # srv <.claude.json> <имя> <json-определение>
	jset "$1" "d.setdefault('mcpServers', {})['$2'] = $3"
}
# Исходное состояние MCP: обе машины синхронны.
"$STAND/m1.sh" pull tools >/dev/null 2>&1
"$STAND/m2.sh" pull tools >/dev/null 2>&1
srv "$C1" alpha-srv '{"command": "a", "args": ["1"]}'
"$STAND/m1.sh" push tools >/dev/null 2>&1
"$STAND/m2.sh" pull tools >/dev/null 2>&1

echo "ТЕСТ 8 — MCP: исходная синхронизация (проверка стенда)"
check "m2 получил сервер m1" "['1']" "$(jget "$C2" 'd["mcpServers"]["alpha-srv"]["args"]')"

echo "ТЕСТ 9 — MCP: устаревшая машина не удаляет чужой новый сервер"
srv "$C1" beta-srv '{"command": "b"}'
"$STAND/m1.sh" push tools >/dev/null 2>&1
"$STAND/m2.sh" push tools >/dev/null 2>&1
sync1
check "сервер m1 остался в шаблоне" "b" "$(jget "$M1" 'd["beta-srv"]["command"]')"
"$STAND/m2.sh" push tools >/dev/null 2>&1
sync1
check "и после повторного push m2" "b" "$(jget "$M1" 'd["beta-srv"]["command"]')"

echo "ТЕСТ 10 — MCP: устаревшая машина не откатывает чужую правку сервера"
jset "$C1" 'd["mcpServers"]["alpha-srv"]["args"] = ["2"]'
"$STAND/m1.sh" push tools >/dev/null 2>&1
"$STAND/m2.sh" push tools >/dev/null 2>&1
sync1
check "правка m1 уцелела" "['2']" "$(jget "$M1" 'd["alpha-srv"]["args"]')"

echo "ТЕСТ 11 — MCP: сервер, добавленный здесь, уезжает рядом с чужими"
srv "$C2" gamma-srv '{"command": "g"}'
"$STAND/m2.sh" push tools >/dev/null 2>&1
sync1
check "новый сервер m2 в шаблоне" "g" "$(jget "$M1" 'd["gamma-srv"]["command"]')"
check "серверы m1 не пострадали" "b ['2']" \
	"$(jget "$M1" 'd["beta-srv"]["command"]') $(jget "$M1" 'd["alpha-srv"]["args"]')"

echo "ТЕСТ 12 — MCP: сервер, удалённый здесь, уходит из шаблона"
"$STAND/m2.sh" pull tools >/dev/null 2>&1
jset "$C2" 'd["mcpServers"].pop("gamma-srv")'
"$STAND/m2.sh" push tools >/dev/null 2>&1
sync1
check "удалённого сервера нет" "нет" "$(jget "$M1" 'd["gamma-srv"]')"
check "остальные на месте" "b" "$(jget "$M1" 'd["beta-srv"]["command"]')"

echo "ТЕСТ 13 — MCP: сервер чужого scope не трогаем"
"$STAND/m1.sh" mcp scope beta-srv --here >/dev/null 2>&1
"$STAND/m1.sh" push tools >/dev/null 2>&1
"$STAND/m2.sh" pull tools >/dev/null 2>&1
"$STAND/m2.sh" push tools >/dev/null 2>&1
sync1
check "сервер только для m1 остался в шаблоне" "b" "$(jget "$M1" 'd["beta-srv"]["command"]')"

echo "ТЕСТ 14 — MCP: без снимка существующее в хранилище не трогаем"
jset "$C1" 'd["mcpServers"]["alpha-srv"]["args"] = ["3"]'
"$STAND/m1.sh" push tools >/dev/null 2>&1
rm -f "$STAND/m2/.claude/ccsync-mcp-base.json"
srv "$C2" delta-srv '{"command": "d"}'
out=$("$STAND/m2.sh" push tools 2>&1)
sync1
check "правка m1 уцелела" "['3']" "$(jget "$M1" 'd["alpha-srv"]["args"]')"
check "новый сервер m2 всё равно уехал" "d" "$(jget "$M1" 'd["delta-srv"]["command"]')"
check "push сказал, что alpha-srv не отдан" "да" \
	"$(echo "$out" | grep -q 'не отдано.*mcp:alpha-srv' && echo да || echo нет)"

echo "ТЕСТ 15 — MCP: секреты в шаблон не попадают"
srv "$C2" eps-srv '{"command": "e", "env": {"API_KEY": "sekret-123"}}'
"$STAND/m2.sh" push tools >/dev/null 2>&1
sync1
check "значение секрета не уехало" "нет" "$(grep -q 'sekret-123' "$M1" && echo да || echo нет)"
check "вместо него метка" "{{ENV:API_KEY}}" "$(jget "$M1" 'd["eps-srv"]["env"]["API_KEY"]')"

echo "ТЕСТ 16 — MCP: сервер, добавленный здесь до pull, не теряется"
# Если снимком после pull станут здешние серверы целиком, неотданный сервер
# окажется в снимке, и следующий push сочтёт его удалённым в хранилище.
srv "$C2" zeta-srv '{"command": "z"}'
"$STAND/m2.sh" pull tools >/dev/null 2>&1
"$STAND/m2.sh" push tools >/dev/null 2>&1
sync1
check "неотданный сервер уехал после pull" "z" "$(jget "$M1" 'd["zeta-srv"]["command"]')"

echo "ТЕСТ 17 — MCP: сервер, который не встал при pull, не удаляется push-ем"
srv "$C1" broken-srv '{"command": "x"}'
"$STAND/m1.sh" push tools >/dev/null 2>&1
"$STAND/m2.sh" pull tools >/dev/null 2>&1
check "на m2 он и правда не встал" "нет" "$(jget "$C2" 'd["mcpServers"]["broken-srv"]')"
"$STAND/m2.sh" push tools >/dev/null 2>&1
sync1
check "в шаблоне он остался" "x" "$(jget "$M1" 'd["broken-srv"]["command"]')"

echo "ТЕСТ 18 — MCP: обновление, которое не встало при pull, не откатывается push-ем"
# Сервер у m2 есть, m1 его обновил, а у m2 обновление не встало. Выкинь его
# из снимка — push решит, что правили обе стороны, и вернёт старую версию.
jset "$C1" 'd["mcpServers"]["alpha-srv"]["args"] = ["broken-4"]'
"$STAND/m1.sh" push tools >/dev/null 2>&1
"$STAND/m2.sh" pull tools >/dev/null 2>&1
check "на m2 осталась прежняя версия" "['3']" "$(jget "$C2" 'd["mcpServers"]["alpha-srv"]["args"]')"
"$STAND/m2.sh" push tools >/dev/null 2>&1
sync1
check "в шаблоне новая версия m1" "['broken-4']" "$(jget "$M1" 'd["alpha-srv"]["args"]')"

echo "ТЕСТ 27 — MCP: pull не затирает сервер, поправленный здесь руками"
srv "$C1" own-srv '{"command": "o", "args": ["1"]}'
"$STAND/m1.sh" push tools >/dev/null 2>&1
"$STAND/m2.sh" pull tools >/dev/null 2>&1
jset "$C2" 'd["mcpServers"]["own-srv"]["args"] = ["m2"]'
out=$("$STAND/m2.sh" pull tools 2>&1)
check "правка на m2 уцелела" "['m2']" "$(jget "$C2" 'd["mcpServers"]["own-srv"]["args"]')"
check "pull сказал, что сервер оставлен" "да" \
	"$(echo "$out" | grep -q 'own-srv правлен здесь' && echo да || echo нет)"
"$STAND/m2.sh" push tools >/dev/null 2>&1
sync1
check "push отдал правку m2" "['m2']" "$(jget "$M1" 'd["own-srv"]["args"]')"

echo "ТЕСТ 28 — MCP: сервер, не правленный здесь, pull обновляет"
"$STAND/m1.sh" pull tools >/dev/null 2>&1
jset "$C1" 'd["mcpServers"]["own-srv"]["args"] = ["m1-снова"]'
"$STAND/m1.sh" push tools >/dev/null 2>&1
"$STAND/m2.sh" pull tools >/dev/null 2>&1
check "m2 получил новую версию" "['m1-снова']" "$(jget "$C2" 'd["mcpServers"]["own-srv"]["args"]')"

echo "ТЕСТ 29 — MCP: удалённый здесь сервер pull не возвращает, push удаляет"
jset "$C2" 'd["mcpServers"].pop("own-srv")'
out=$("$STAND/m2.sh" pull tools 2>&1)
check "сервер не вернулся" "нет" "$(jget "$C2" 'd["mcpServers"]["own-srv"]')"
check "pull сказал об этом" "да" \
	"$(echo "$out" | grep -q 'own-srv удалён здесь' && echo да || echo нет)"
"$STAND/m2.sh" push tools >/dev/null 2>&1
sync1
check "удаление уехало в шаблон" "нет" "$(jget "$M1" 'd["own-srv"]')"

echo "ТЕСТ 30 — MCP: удалённый здесь, но обновлённый там — возвращается"
srv "$C1" own2-srv '{"command": "o2", "args": ["1"]}'
"$STAND/m1.sh" push tools >/dev/null 2>&1
"$STAND/m2.sh" pull tools >/dev/null 2>&1
jset "$C2" 'd["mcpServers"].pop("own2-srv")'
jset "$C1" 'd["mcpServers"]["own2-srv"]["args"] = ["2"]'
"$STAND/m1.sh" push tools >/dev/null 2>&1
"$STAND/m2.sh" pull tools >/dev/null 2>&1
check "обновлённый сервер вернулся" "['2']" "$(jget "$C2" 'd["mcpServers"]["own2-srv"]["args"]')"

P1=$STAND/m1/.claude/plugins/installed_plugins.json
P2=$STAND/m2/.claude/plugins/installed_plugins.json
PJ=$STAND/m1vault/tools/plugins.json
plug() { # plug <installed_plugins.json> <имя> <версия>
	jset "$1" "d.setdefault('version', 2); d.setdefault('plugins', {})['$2'] = [{'scope': 'user', 'version': '$3'}]"
}
# Исходное состояние: у обеих машин один и тот же плагин, обе синхронны.
"$STAND/m1.sh" pull tools >/dev/null 2>&1
plug "$P1" p1@mk 1
jset "$S1" 'd.setdefault("enabledPlugins", {})["p1@mk"] = True'
"$STAND/m1.sh" push tools >/dev/null 2>&1
plug "$P2" p1@mk 1
"$STAND/m2.sh" pull tools >/dev/null 2>&1

echo "ТЕСТ 19 — плагины: устаревшая машина не выключает чужой плагин"
jset "$S1" 'd["enabledPlugins"]["p2@mk"] = True'
"$STAND/m1.sh" push tools >/dev/null 2>&1
"$STAND/m2.sh" push tools >/dev/null 2>&1
sync1
check "включённый на m1 плагин остался в списке" "True" "$(jget "$PJ" 'd["enabled"]["p2@mk"]')"

echo "ТЕСТ 20 — плагины: устаревшая запись об установке не затирает свежую"
plug "$P1" p1@mk 2
"$STAND/m1.sh" push tools >/dev/null 2>&1
"$STAND/m2.sh" push tools >/dev/null 2>&1
sync1
check "в манифесте версия m1" "2" "$(jget "$PJ" 'd["installed_plugins"]["plugins"]["p1@mk"][0]["version"]')"

echo "ТЕСТ 21 — плагины: установленный здесь уезжает рядом с чужими"
plug "$P2" p3@mk 1
"$STAND/m2.sh" push tools >/dev/null 2>&1
sync1
check "новый плагин m2 в манифесте" "1" "$(jget "$PJ" 'd["installed_plugins"]["plugins"]["p3@mk"][0]["version"]')"
check "запись m1 не пострадала" "2" "$(jget "$PJ" 'd["installed_plugins"]["plugins"]["p1@mk"][0]["version"]')"

echo "ТЕСТ 22 — плагины: без снимка существующее не трогаем, новое добавляем"
rm -f "$STAND/m2/.claude/ccsync-plugins-base.json"
plug "$P2" p4@mk 1
"$STAND/m2.sh" push tools >/dev/null 2>&1
sync1
check "запись m1 цела" "2" "$(jget "$PJ" 'd["installed_plugins"]["plugins"]["p1@mk"][0]["version"]')"
check "новый плагин добавлен" "1" "$(jget "$PJ" 'd["installed_plugins"]["plugins"]["p4@mk"][0]["version"]')"

echo "ТЕСТ 23 — первый pull со снимками: разошедшаяся запись не уходит поверх свежей"
# Общей точки у здешней записи и хранилища не было никогда. Выбрось её из
# снимка — push решит, что правили обе стороны, и здешнее победит.
plug "$P1" p1@mk 5
"$STAND/m1.sh" push tools >/dev/null 2>&1
rm -f "$STAND/m2/.claude/ccsync-plugins-base.json"
"$STAND/m2.sh" pull tools >/dev/null 2>&1
"$STAND/m2.sh" push tools >/dev/null 2>&1
sync1
check "в манифесте свежая версия m1" "5" "$(jget "$PJ" 'd["installed_plugins"]["plugins"]["p1@mk"][0]["version"]')"

echo "ТЕСТ 24 — сценарий 27.09 целиком: отставшая машина делает push all"
# Все пять механизмов разом. m1 и m2 сошлись на версии 1, m1 перешёл на
# версию 2 везде, m2 ничего не подтягивал и отдаёт всё, что у него есть.
mkdir -p "$STAND/m1/.local/bin" "$STAND/m2/.local/bin"
round() { # round <версия> — m1 правит всё и отдаёт
	echo "раздел v$1" > "$STAND/m1/.claude/CLAUDE.md"
	printf '#!/bin/bash\necho helper v%s\n' "$1" > "$STAND/m1/.local/bin/helper"
	jset "$S1" "d['skillOverrides'] = {'r': 'v$1'}; d['enabledPlugins']['tok@mk'] = ('$1' == '2')"
	srv "$C1" tok-srv "{'command': 'tok', 'args': ['v$1']}"
	plug "$P1" tok@mk "$1"
	"$STAND/m1.sh" push all >/dev/null 2>&1
}
round 1
"$STAND/m1.sh" host add "$STAND/m1/.local/bin/helper" >/dev/null 2>&1
"$STAND/m1.sh" push all >/dev/null 2>&1
"$STAND/m2.sh" pull all >/dev/null 2>&1
check "исходно m2 сошёлся с m1" "раздел v1" "$(cat "$STAND/m2/.claude/CLAUDE.md" 2>/dev/null)"
round 2
out=$("$STAND/m2.sh" push all 2>&1)
sync1
check "CLAUDE.md" "раздел v2" "$(cat "$STAND/m1vault/tools/CLAUDE.md")"
check "обвязка" "да" "$(grep -q 'helper v2' "$STAND/m1vault/tools/host/bin/helper" && echo да || echo нет)"
check "ключ настроек" "v2" "$(jget "$T1" 'd["skillOverrides"]["r"]')"
check "MCP-сервер" "['v2']" "$(jget "$M1" 'd["tok-srv"]["args"]')"
check "включённый плагин" "True" "$(jget "$PJ" 'd["enabled"]["tok@mk"]')"
check "запись о плагине" "2" "$(jget "$PJ" 'd["installed_plugins"]["plugins"]["tok@mk"][0]["version"]')"
check "push перечислил не отданное" "да" \
	"$(echo "$out" | grep -q 'не отдано.*CLAUDE.md.*bin/helper' && echo да || echo нет)"
"$STAND/m2.sh" pull all >/dev/null 2>&1
check "после pull m2 догнал m1" "раздел v2" "$(cat "$STAND/m2/.claude/CLAUDE.md")"

echo "ТЕСТ 25 — битый шаблон в хранилище не считается «шаблона нет»"
# Огрызок от оборванной записи или неудачная ручная правка. Прочитать его как
# «шаблона ещё нет» — значит записать поверх здешнее целиком: та же потеря.
(cd "$STAND/m1vault" && git pull -q --rebase 2>/dev/null
 for f in settings.template.json mcp-servers.template.json plugins.json; do
	printf '{"общий": \n<<<<<<< огрызок\n' > "tools/$f"
 done
 git add -A && git commit -qm "битые шаблоны" && git push -q) >/dev/null 2>&1
out=$("$STAND/m2.sh" push tools 2>&1)
sync1
for f in settings.template.json mcp-servers.template.json plugins.json; do
	check "$f не перезаписан" "да" \
		"$(grep -q 'огрызок' "$STAND/m1vault/tools/$f" && echo да || echo нет)"
done
check "push сказал, что шаблоны повреждены" "да" \
	"$(echo "$out" | grep -q 'повреждён.*settings.template.json' && echo да || echo нет)"

echo "ТЕСТ 26 — pull с битым MCP-шаблоном не падает трассировкой"
# Шаблон кладём здесь же: тест не должен зависеть от исхода соседнего.
(cd "$STAND/m1vault" && git pull -q --rebase 2>/dev/null
 printf '{"общий": \n<<<<<<< огрызок\n' > tools/mcp-servers.template.json
 echo "инструкция после битого MCP" > tools/CLAUDE.md
 git add -A && git commit -qm "битый MCP" && git push -q) >/dev/null 2>&1
out=$("$STAND/m2.sh" pull tools 2>&1); code=$?
check "без трассировки" "нет" "$(echo "$out" | grep -q Traceback && echo да || echo нет)"
check "код возврата 0" "0" "$code"
check "про MCP-шаблон сказано" "да" \
	"$(echo "$out" | grep -q 'повреждён.*mcp-servers.template.json' && echo да || echo нет)"
check "остальное применилось" "инструкция после битого MCP" "$(cat "$STAND/m2/.claude/CLAUDE.md")"

echo
echo "ИТОГО: успешно $ok, провалено $fail"
rm -rf "$STAND"
exit $((fail > 0))
