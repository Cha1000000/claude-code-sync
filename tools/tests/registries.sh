#!/bin/bash
# Реестры и конфликты: расхождение машин, авторазрешение
set -u
BASE=${TMPDIR:-/tmp}/ccsync-tests
STAND=$BASE/registries
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
	mkdir -p $m/.claude/projects $m/work-$m
	cat > "$STAND/$m.sh" <<EOF
#!/bin/bash
cd "$STAND/$m" || exit 1
exec env -u CLAUDE_CODE_SESSION_ID HOME="$STAND/$m" CLAUDE_CONFIG_DIR="$STAND/$m/.claude" \
     python3 "$STAND/${m}vault/bin/ccsync.py" "\$@"
EOF
	chmod +x "$STAND/$m.sh"
done
(cd m1vault && git add -A >/dev/null && git commit -qm "стенд" && git push -q -u origin master)
(cd m2vault && git fetch -q origin && git reset -q --hard origin/master &&
 git branch --set-upstream-to=origin/master master >/dev/null 2>&1)

# Обрыв и восстановление связи — так моделируем «ноутбук был вне сети».
offline() { (cd "$STAND/$1vault" && git remote set-url origin "$STAND/nowhere.git"); }
online()  { (cd "$STAND/$1vault" && git remote set-url origin "$STAND/remote.git"); }

"$STAND/m1.sh" init --id m1 --yes --note "машина 1" >/dev/null
"$STAND/m1.sh" push tools >/dev/null 2>&1
(cd "$STAND/m2vault" && git pull -q --rebase)
"$STAND/m2.sh" init --id m2 --yes --note "машина 2" >/dev/null
"$STAND/m2.sh" push tools >/dev/null 2>&1
(cd "$STAND/m1vault" && git pull -q --rebase)

echo "ТЕСТ 1 — реестры разложены по машинам"
check "файл машины m1" "да" "$([ -e "$STAND/m1vault/machines/m1.json" ] && echo да || echo нет)"
check "файл машины m2 доехал до m1" "да" "$([ -e "$STAND/m1vault/machines/m2.json" ] && echo да || echo нет)"
check "монолитный machines.json не создаётся" "нет" \
	"$([ -e "$STAND/m1vault/machines.json" ] && echo да || echo нет)"

echo "ТЕСТ 2 — обе машины работали вне сети и разошлись"
offline m1; offline m2
"$STAND/m1.sh" bind projA "$STAND/m1/work-m1" >/dev/null
"$STAND/m1.sh" push tools >/dev/null 2>&1
"$STAND/m2.sh" bind projB "$STAND/m2/work-m2" >/dev/null
"$STAND/m2.sh" push tools >/dev/null 2>&1
online m1; online m2
"$STAND/m1.sh" push tools >/dev/null 2>&1     # первый вернулся — просто отдал
out2=$("$STAND/m2.sh" push tools 2>&1)          # второй вернулся — тут раньше был конфликт
check "второй push прошёл без ручного вмешательства" "0" "$(echo "$out2" | grep -c 'разбери')"
check "хранилище не в конфликте" "нет" \
	"$([ -e "$STAND/m2vault/.git/rebase-merge" ] && echo да || echo нет)"
(cd "$STAND/m1vault" && git pull -q --rebase 2>/dev/null)
check "привязка m1 на месте" "1" "$(grep -c projA "$STAND/m1vault/project-map/m1.json" 2>/dev/null)"
check "привязка m2 тоже на месте" "1" "$(grep -c projB "$STAND/m1vault/project-map/m2.json" 2>/dev/null)"
check "обе машины в реестре" "2" "$(ls "$STAND/m1vault/machines/" | wc -l)"

echo "ТЕСТ 3 — конфликт общего шаблона сливается сам"
(cd "$STAND/m1vault" && mkdir -p tools && echo '{"общее": 1}' > tools/plugins.json &&
 git add -A >/dev/null && git commit -qm "шаблон" && git push -q)
(cd "$STAND/m2vault" && git pull -q --rebase)
offline m1; offline m2
(cd "$STAND/m1vault" && echo '{"общее": 1, "от-m1": "да"}' > tools/plugins.json &&
 git add -A >/dev/null && git commit -qm "правка m1")
(cd "$STAND/m2vault" && echo '{"общее": 1, "от-m2": "да"}' > tools/plugins.json &&
 git add -A >/dev/null && git commit -qm "правка m2")
online m1; online m2
(cd "$STAND/m1vault" && git push -q)
out3=$("$STAND/m2.sh" pull memory 2>&1)
check "ccsync сообщил, что слил конфликт" "1" "$(echo "$out3" | grep -c 'слит автоматически')"
check "правка m1 уцелела" "1" "$(grep -c 'от-m1' "$STAND/m2vault/tools/plugins.json")"
check "правка m2 уцелела" "1" "$(grep -c 'от-m2' "$STAND/m2vault/tools/plugins.json")"
check "rebase завершён" "нет" \
	"$([ -e "$STAND/m2vault/.git/rebase-merge" ] && echo да || echo нет)"
(cd "$STAND/m2vault" && git push -q 2>/dev/null)

echo "ТЕСТ 4 — то, что сливать нельзя, остаётся человеку"
(cd "$STAND/m1vault" && git pull -q --rebase)
(cd "$STAND/m1vault" && mkdir -p memory/facts && echo "исходный текст" > memory/facts/f.md &&
 git add -A >/dev/null && git commit -qm "факт" && git push -q)
(cd "$STAND/m2vault" && git pull -q --rebase)
offline m1; offline m2
(cd "$STAND/m1vault" && echo "версия m1" > memory/facts/f.md &&
 git add -A >/dev/null && git commit -qm "факт m1")
(cd "$STAND/m2vault" && echo "версия m2" > memory/facts/f.md &&
 git add -A >/dev/null && git commit -qm "факт m2")
online m1; online m2
(cd "$STAND/m1vault" && git push -q)
out4=$("$STAND/m2.sh" pull memory 2>&1); code4=$?
check "ccsync остановился и позвал человека" "1" "$(echo "$out4" | grep -c 'нельзя слить автоматически')"
check "код возврата — ошибка" "1" "$code4"
check "назван конкретный файл" "1" "$(echo "$out4" | grep -c 'memory/facts/f.md')"
(cd "$STAND/m2vault" && git rebase -q --abort 2>/dev/null; git reset -q --hard origin/master)

echo "ТЕСТ 5 — списание машины"
out5=$("$STAND/m1.sh" machines --forget m2 2>&1)
check "файл машины m2 удалён" "нет" \
	"$([ -e "$STAND/m1vault/machines/m2.json" ] && echo да || echo нет)"
check "её пути тоже удалены" "нет" \
	"$([ -e "$STAND/m1vault/project-map/m2.json" ] && echo да || echo нет)"
check "текущую машину списать нельзя" "2" \
	"$("$STAND/m1.sh" machines --forget m1 >/dev/null 2>&1; echo $?)"
check "неизвестную машину — понятная ошибка" "1" \
	"$("$STAND/m1.sh" machines --forget нет-такой >/dev/null 2>&1; echo $?)"

echo "ТЕСТ 6 — миграция старого хранилища"
mkdir -p "$STAND/old/machines-check"
cd "$STAND"; git clone -q remote.git m3vault 2>/dev/null
cat > m3vault/machines.json <<'EOF'
{"старая": {"machine_id": "старая", "os": "linux", "distro": "Ubuntu", "home": "/home/u", "hostname": "u"}}
EOF
cat > m3vault/project-map.json <<'EOF'
{"проект": {"старая": "/home/u/проект"}, "home": {"старая": "/home/u"}}
EOF
mig=$(python3 -c "
import sys; sys.path.insert(0,'$STAND/m3vault/bin')
from pathlib import Path
from ccsync_lib.vault import Vault
from ccsync_lib import migrate_registry
v = Vault(Path('$STAND/m3vault'))
before_m = set(v.load_machines()); before_p = v.load_project_map()
migrate_registry.migrate(v)
after_m = set(v.load_machines()); after_p = v.load_project_map()
print('да' if before_m == after_m and before_p == after_p else 'нет')
")
check "данные до и после миграции совпадают" "да" "$mig"
check "монолиты удалены" "нет" \
	"$([ -e "$STAND/m3vault/machines.json" ] || [ -e "$STAND/m3vault/project-map.json" ] && echo да || echo нет)"
check "машина из старого реестра видна после миграции" "да" \
	"$(python3 -c "
import sys; sys.path.insert(0,'$STAND/m3vault/bin')
from pathlib import Path
from ccsync_lib.vault import Vault
print('да' if 'старая' in Vault(Path('$STAND/m3vault')).load_machines() else 'нет')
")"
check "повторная миграция ничего не ломает" "0" \
	"$(python3 -c "
import sys; sys.path.insert(0,'$STAND/m3vault/bin')
from pathlib import Path
from ccsync_lib.vault import Vault
from ccsync_lib import migrate_registry
print(len(migrate_registry.migrate(Vault(Path('$STAND/m3vault')))))
")"

echo "ТЕСТ 7 — хранилище старого образца читается без миграции"
rm -rf "$STAND/m4vault"; mkdir -p "$STAND/m4vault"
cat > "$STAND/m4vault/machines.json" <<'EOF'
{"древняя": {"machine_id": "древняя", "home": "/home/x"}}
EOF
check "старый реестр машин виден" "древняя" \
	"$(python3 -c "
import sys; sys.path.insert(0,'$SRC')
from pathlib import Path
from ccsync_lib.vault import Vault
print(','.join(Vault(Path('$STAND/m4vault')).load_machines()))
")"

echo "ТЕСТ 8 — имя файла для машины с необычным id"
check "пробелы и слэши не создают подкаталогов" "1" \
	"$(python3 -c "
import sys; sys.path.insert(0,'$SRC')
from ccsync_lib.vault import machine_file_stem
print(1 if '/' not in machine_file_stem('my machine/2') else 0)
")"
check "похожие id с кириллицей не делят один файл" "1" \
	"$(python3 -c "
import sys; sys.path.insert(0,'$SRC')
from ccsync_lib.vault import machine_file_stem
print(1 if machine_file_stem('ubuntu-ноут') != machine_file_stem('ubuntu-нетбук') else 0)
")"

echo
echo "ИТОГО: успешно $ok, провалено $fail"
exit $fail
