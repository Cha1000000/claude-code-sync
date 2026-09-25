#!/bin/bash
# Секреты: ездят зашифрованными, а из транскриптов вырезаются
set -u
BASE=${TMPDIR:-/tmp}/ccsync-tests
STAND=$BASE/secrets
SRC=$(cd "$(dirname "$0")/../../bin" && pwd)
rm -rf "$STAND"; mkdir -p "$STAND"; cd "$STAND"

ok=0; fail=0
check() {
	if [ "$2" = "$3" ]; then echo "  ✓ $1"; ok=$((ok+1))
	else echo "  ✗ $1 — ожидали [$2], получили [$3]"; fail=$((fail+1)); fi
}

if ! command -v age >/dev/null 2>&1; then
	echo "ТЕСТ пропущен — age не установлен"
	echo "ИТОГО: успешно 0, провалено 0"
	exit 0
fi

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

SECRET="sk-stand-4f2a9c1e8b7d6a5c3e0f1b2d4a6c8e0f"

echo "ТЕСТ 1 — секрет уезжает зашифрованным"
age-keygen -o "$STAND/m1/.claude/ccsync-age.key" 2>/dev/null
chmod 600 "$STAND/m1/.claude/ccsync-age.key"
printf '%s' "$SECRET" > "$STAND/m1/.claude/stand.key"
"$STAND/m1.sh" secrets add-recipient >/dev/null
"$STAND/m1.sh" secrets add "$STAND/m1/.claude/stand.key" >/dev/null
"$STAND/m1.sh" push tools >/dev/null 2>&1

cipher="$STAND/m1vault/tools/secrets/.claude/stand.key.age"
[ -f "$cipher" ] && has_cipher=yes || has_cipher=no
check "шифротекст лёг в хранилище" yes "$has_cipher"
grep -q "$SECRET" "$cipher" 2>/dev/null && leaked=yes || leaked=no
check "открытого секрета в нём нет" no "$leaked"
head -1 "$cipher" 2>/dev/null | grep -q "age-encryption" && aged=yes || aged=no
check "это действительно age" yes "$aged"

echo "ТЕСТ 2 — без ключа расшифровать нельзя"
(cd "$STAND/m2vault" && git pull -q --rebase 2>/dev/null)
out=$("$STAND/m2.sh" pull tools 2>&1)
[ -f "$STAND/m2/.claude/stand.key" ] && appeared=yes || appeared=no
check "файл не появился" no "$appeared"
echo "$out" | grep -q "не расшифровать" && told=yes || told=no
check "сказано, почему" yes "$told"

echo "ТЕСТ 3 — с ключом секрет раскладывается"
cp "$STAND/m1/.claude/ccsync-age.key" "$STAND/m2/.claude/ccsync-age.key"
"$STAND/m2.sh" pull tools >/dev/null 2>&1
got=$(cat "$STAND/m2/.claude/stand.key" 2>/dev/null || echo "")
check "содержимое доехало" "$SECRET" "$got"
mode=$(stat -c '%a' "$STAND/m2/.claude/stand.key" 2>/dev/null || echo "")
check "права 600" "600" "$mode"

echo "ТЕСТ 4 — разошедшийся файл не затирается"
printf '%s' "sk-stand-ДРУГОЙ-КЛЮЧ-НА-ЭТОЙ-МАШИНЕ" > "$STAND/m2/.claude/stand.key"
out=$("$STAND/m2.sh" pull tools 2>&1)
kept=$(cat "$STAND/m2/.claude/stand.key")
check "локальная версия уцелела" "sk-stand-ДРУГОЙ-КЛЮЧ-НА-ЭТОЙ-МАШИНЕ" "$kept"
echo "$out" | grep -q "здесь другой" && warned=yes || warned=no
check "про расхождение сказано" yes "$warned"

echo "ТЕСТ 5 — секреты вырезаются из транскрипта"
res=$(python3 - "$STAND" "$SECRET" 2>/dev/null <<'PY'
import sys, pathlib, json
stand = pathlib.Path(sys.argv[1]); secret = sys.argv[2]
sys.path.insert(0, str(stand / "m1vault/bin"))
from ccsync_lib import redact, sessions
from ccsync_lib.paths import PathMapper
home = stand / "m1"; src = stand / "src.jsonl"
records = [
	{"type": "user", "message": {"content": f"мой ключ {secret} вот"}},
	{"type": "user", "message": {"content": "GITHUB: ghp_" + "a" * 36}},
	{"type": "user", "message": {"content": "обычный текст про claude-opus-5 и порт 20128"}},
]
src.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in records) + "\n")
r = redact.for_home(home, [".claude/stand.key"])
out = stand / "out.jsonl"
sessions.transform_transcript(src, out, PathMapper(home=str(home), project_paths={}),
                              mode="tokenize", redactor=r)
t = out.read_text()
print("yes" if secret not in t else "no")
print("yes" if "ghp_aaaa" not in t else "no")
print("yes" if "claude-opus-5 и порт 20128" in t else "no")
PY
)
check "секрет вырезан"            yes "$(echo "$res" | sed -n 1p)"
check "github-токен вырезан"      yes "$(echo "$res" | sed -n 2p)"
check "обычный текст не тронут"   yes "$(echo "$res" | sed -n 3p)"

sync1() { (cd "$STAND/m1vault" && git pull -q --rebase 2>/dev/null); }
sync2() { (cd "$STAND/m2vault" && git pull -q --rebase 2>/dev/null); }
K1="$STAND/m1/.claude/ccsync-age.key"
vault_plain() { age --decrypt --identity "$1" "$2/tools/secrets/.claude/stand.key.age" 2>/dev/null; }

echo "ТЕСТ 6 — правка на m2 уезжает, и нетронутый m1 её получает"
"$STAND/m2.sh" push tools >/dev/null 2>&1
sync1
check "в хранилище версия m2" "sk-stand-ДРУГОЙ-КЛЮЧ-НА-ЭТОЙ-МАШИНЕ" "$(vault_plain "$K1" "$STAND/m1vault")"
"$STAND/m1.sh" pull tools >/dev/null 2>&1
check "m1 обновился (не «разошлись»)" "sk-stand-ДРУГОЙ-КЛЮЧ-НА-ЭТОЙ-МАШИНЕ" "$(cat "$STAND/m1/.claude/stand.key")"
grep -q "$SECRET" "$STAND/m1/.claude/ccsync-secrets-base.json" 2>/dev/null && plain_base=yes || plain_base=no
check "в снимке нет открытого секрета" no "$plain_base"

echo "ТЕСТ 7 — push устаревшей копии не затирает свежую"
printf '%s' "sk-stand-ТРЕТЬЯ-ВЕРСИЯ-С-M1" > "$STAND/m1/.claude/stand.key"
"$STAND/m1.sh" push tools >/dev/null 2>&1
sync2
"$STAND/m2.sh" push tools >/dev/null 2>&1
sync1
check "в хранилище осталась версия m1" "sk-stand-ТРЕТЬЯ-ВЕРСИЯ-С-M1" "$(vault_plain "$K1" "$STAND/m1vault")"
"$STAND/m2.sh" pull tools >/dev/null 2>&1
check "m2 получил версию m1" "sk-stand-ТРЕТЬЯ-ВЕРСИЯ-С-M1" "$(cat "$STAND/m2/.claude/stand.key")"

echo "ТЕСТ 8 — push без изменений не перешифровывает"
before=$(sha256sum "$STAND/m1vault/tools/secrets/.claude/stand.key.age" | cut -c1-64)
"$STAND/m1.sh" push tools >/dev/null 2>&1
after=$(sha256sum "$STAND/m1vault/tools/secrets/.claude/stand.key.age" | cut -c1-64)
check "шифротекст не менялся" "$before" "$after"

echo "ТЕСТ 9 — новый получатель: перешифровывается хранилище, а не старая копия"
printf '%s' "sk-stand-ЧЕТВЁРТАЯ-С-M2" > "$STAND/m2/.claude/stand.key"
"$STAND/m2.sh" push tools >/dev/null 2>&1
# m2 заводит собственный ключ и просится в получатели
K2="$STAND/m2/.claude/ccsync-age.key"
rm -f "$K2"; age-keygen -o "$K2" 2>/dev/null; chmod 600 "$K2"
sync2
"$STAND/m2.sh" secrets add-recipient >/dev/null
"$STAND/m2.sh" push tools >/dev/null 2>&1
# m1 подтягивает только хранилище: локально у него всё ещё третья версия
sync1
"$STAND/m1.sh" push tools >/dev/null 2>&1
sync2
check "новый получатель расшифровывает" "sk-stand-ЧЕТВЁРТАЯ-С-M2" "$(vault_plain "$K2" "$STAND/m2vault")"
check "старый тоже" "sk-stand-ЧЕТВЁРТАЯ-С-M2" "$(vault_plain "$K1" "$STAND/m2vault")"

echo "ИТОГО: успешно $ok, провалено $fail"
exit $((fail > 0))
