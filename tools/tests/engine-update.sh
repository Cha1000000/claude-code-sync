#!/bin/bash
# Обновление движка: выпуски — подписанные теги локального «публичного»
# репозитория, хранилище пользователя — отдельный репозиторий со своим remote.
# Сети нет: тот же git, те же команды, только адреса — каталоги во временной папке.
set -u
BASE=${TMPDIR:-/tmp}/ccsync-tests
STAND=$BASE/engine-update
SRC=$(cd "$(dirname "$0")/../.." && pwd)
rm -rf "$STAND"; mkdir -p "$STAND"; cd "$STAND"

ok=0; fail=0
check() {
	if [ "$2" = "$3" ]; then echo "  ✓ $1"; ok=$((ok+1))
	else echo "  ✗ $1 — ожидали [$2], получили [$3]"; fail=$((fail+1)); fi
}
yes_no() { if "$@" >/dev/null 2>&1; then echo да; else echo нет; fi; }

KEY=$STAND/key; OTHER=$STAND/other
ssh-keygen -q -t ed25519 -N "" -f "$KEY" -C stand
ssh-keygen -q -t ed25519 -N "" -f "$OTHER" -C other
PUBLIC=$STAND/public.git; TREE=$STAND/tree

# --- «публичный репозиторий» -------------------------------------------
git init -q --bare --initial-branch=main "$PUBLIC"
mkdir -p "$TREE/tools/commands"
cp -r "$SRC/bin" "$TREE/bin"; cp -r "$SRC/tools/tests" "$TREE/tools/tests"
cp "$SRC"/tools/commands/sync-*.md "$TREE/tools/commands/"
cp "$SRC/.gitignore" "$SRC/.gitattributes" "$TREE/"
find "$TREE" -name __pycache__ -prune -exec rm -rf {} +
rm -f "$TREE/bin/ENGINE.json"
echo "# установщик хуков" > "$TREE/setup-hooks.py"
echo "ccsync-release $(cut -d' ' -f1,2 "$KEY.pub")" > "$TREE/bin/ccsync_lib/release-signers"
echo "# будет удалён в следующем выпуске" > "$TREE/bin/ccsync_lib/obsolete.py"
git -C "$TREE" init -q --initial-branch=main
git -C "$TREE" config user.email t@t; git -C "$TREE" config user.name stand
git -C "$TREE" config commit.gpgsign false
git -C "$TREE" remote add origin "$PUBLIC"

# publish ТЕГ [ключ|unsigned]: собрать манифест, закоммитить, поставить тег, отправить
publish() {
	local tag=$1 how=${2:-$KEY}
	python3 - "$TREE" "$tag" "$PUBLIC" "$SRC/bin" <<'PY'
import sys
from pathlib import Path
sys.path.insert(0, sys.argv[4])
from ccsync_lib import engine_manifest as m
root, tag, source = Path(sys.argv[1]), sys.argv[2], sys.argv[3]
files = m.collect(root)
(root / m.MANIFEST_PATH).write_text(m.dumps(m.build(files, tag, source)), encoding="utf-8")
PY
	git -C "$TREE" add -A >/dev/null && git -C "$TREE" commit -qm "выпуск $tag"
	if [ "$how" = unsigned ]; then
		git -C "$TREE" tag -a -m "$tag" "$tag"
	else
		git -C "$TREE" -c gpg.format=ssh -c user.signingkey="$how" tag -s -m "$tag" "$tag"
	fi
	git -C "$TREE" push -q --force origin HEAD:main "refs/tags/$tag"
}

publish v2099.01.01

# --- хранилище пользователя ---------------------------------------------
git init -q --bare --initial-branch=main uv.git
git clone -q uv.git uv 2>/dev/null
git -C uv config user.email u@u; git -C uv config user.name user
(cd "$TREE" && tar cf - --exclude=.git .) | tar xf - -C uv
mkdir -p uv/tools/hooks uv/memory; echo "хук пользователя" > uv/tools/hooks/mine.py
echo "свой скрипт" > uv/bin/my_extra.py
git -C uv add -A >/dev/null && git -C uv commit -qm "старт" && git -C uv push -q -u origin main
mkdir -p home/.claude

eng() {  # engine <команда…> в хранилище пользователя
	env -u CLAUDE_CODE_SESSION_ID HOME="$STAND/home" CLAUDE_CONFIG_DIR="$STAND/home/.claude" \
		CCSYNC_LANG=ru GIT_TERMINAL_PROMPT=0 python3 "$STAND/uv/bin/ccsync.py" --vault "$STAND/uv" engine "$@"
}
tree_hash() { (cd uv && find bin tools/tests tools/commands .gitignore setup-hooks.py -type f \
	-not -path '*/__pycache__/*' -print0 | sort -z | xargs -0 sha256sum | sha256sum | cut -d' ' -f1); }
tag_of() { python3 -c "import json;print(json.load(open('$STAND/uv/bin/ENGINE.json'))['release'])"; }

echo "ТЕСТ 1: status на свежем выпуске"
out=$(eng status 2>&1)
check "выпуск назван" 1 "$(echo "$out" | grep -c 'v2099.01.01')"
check "отличий нет" 1 "$(echo "$out" | grep -c 'отличаются от выпуска 0')"

# --- второй выпуск: правка, новый файл, удалённый файл, строка в .gitignore, новая команда
echo "# выпуск 2" >> "$TREE/bin/ccsync_lib/slim.py"
echo "# новое" > "$TREE/bin/ccsync_lib/newmod.py"
rm "$TREE/bin/ccsync_lib/obsolete.py"
echo "*.stand-new" >> "$TREE/.gitignore"
echo "# выпуск 2" >> "$TREE/tools/tests/dry-run.sh"
echo "новая команда" > "$TREE/tools/commands/sync-update.md"
publish v2099.02.01

# правка пользователя в файле, который выпуск тоже меняет, и в файле, который не меняет
echo "# моя правка" >> uv/tools/tests/dry-run.sh
echo "# моя правка" >> uv/bin/ccsync_lib/redact.py
echo "заметка" > uv/notes.md; git -C uv add notes.md; git -C uv commit -qm "заметка" -q; echo "ещё правка" >> uv/notes.md
git -C uv push -q origin main

echo "ТЕСТ 2: --dry-run показывает план и ничего не пишет"
before=$(tree_hash)
out=$(eng update --dry-run 2>&1); code=$?
check "код 0" 0 "$code"
check "метка выпуска в плане" 1 "$(echo "$out" | grep -c 'v2099.02.01')"
check "правка пользователя названа" 1 "$(echo "$out" | grep -c 'правлено здесь — ОСТАВЛЕНО: 1  tools/tests/dry-run.sh')"
check "подпись сходится" 1 "$(echo "$out" | grep -c 'подпись: сходится с ключом выпусков')"
check "файлы не тронуты" "$before" "$(tree_hash)"

echo "ТЕСТ 3: без --yes и без терминала — отказ, ничего не записано"
eng update </dev/null >/dev/null 2>&1; check "код 2" 2 "$?"
check "файлы не тронуты" "$before" "$(tree_hash)"

echo "ТЕСТ 4: update --yes"
out=$(eng update --yes 2>&1); code=$?
check "код 0" 0 "$code"
check "выпуск стал v2099.02.01" v2099.02.01 "$(tag_of)"
check "изменённый файл обновлён" 1 "$(grep -c '^# выпуск 2' uv/bin/ccsync_lib/slim.py)"
check "новый файл появился" да "$(yes_no test -f uv/bin/ccsync_lib/newmod.py)"
check "ненужный файл удалён" нет "$(yes_no test -e uv/bin/ccsync_lib/obsolete.py)"
check "новая команда появилась" да "$(yes_no test -f uv/tools/commands/sync-update.md)"
check "строка дописана в .gitignore" 1 "$(grep -c '^\*\.stand-new$' uv/.gitignore)"
check "старые строки .gitignore на месте" 1 "$(grep -c '^\*\.ccsync-tmp$' uv/.gitignore)"
check "правка пользователя (там же, где выпуск) цела" 1 "$(grep -c '^# моя правка' uv/tools/tests/dry-run.sh)"
check "правка пользователя (выпуск не менял) цела" 1 "$(grep -c '^# моя правка' uv/bin/ccsync_lib/redact.py)"
check "свой скрипт в bin цел" да "$(yes_no test -f uv/bin/my_extra.py)"
check "хук пользователя цел" да "$(yes_no test -f uv/tools/hooks/mine.py)"
check "в отчёте — оставленный файл" 1 "$(echo "$out" | grep -c 'Правленные тобой файлы не тронуты: tools/tests/dry-run.sh')"
check "бэкап создан" 1 "$(ls "$STAND"/home/.claude/backups | grep -c '^engine-')"

echo "ТЕСТ 5: коммит — только пути выпуска, остальное не утащено"
files=$(git -C uv show --name-only --format= HEAD | sort | tr '\n' ' ')
check "сообщение коммита" "Update ccsync engine to v2099.02.01" "$(git -C uv log -1 --format=%s)"
check "заметка не в коммите" 0 "$(echo "$files" | grep -c notes.md)"
check "правка пользователя не в коммите" 0 "$(echo "$files" | grep -c 'tests/dry-run.sh')"
check "манифест в коммите" 1 "$(echo "$files" | grep -c 'bin/ENGINE.json')"
check "удаление в коммите" 1 "$(echo "$files" | grep -c 'obsolete.py')"
check "заметка осталась несохранённой" " M notes.md" "$(git -C uv status --porcelain -- notes.md | head -1)"
check "коммит ушёл в remote" "$(git -C uv rev-parse HEAD)" "$(git -C uv.git rev-parse main)"

echo "ТЕСТ 6: вторая машина получает обновление обычным pull"
git clone -q uv.git uv2 2>/dev/null
check "тот же манифест" "$(sha256sum < uv/bin/ENGINE.json)" "$(sha256sum < uv2/bin/ENGINE.json)"
check "новый файл есть" да "$(yes_no test -f uv2/bin/ccsync_lib/newmod.py)"

echo "ТЕСТ 7: повторный update — делать нечего"
out=$(eng update --yes 2>&1); check "код 0" 0 "$?"
check "сказано, что уже стоит" 1 "$(echo "$out" | grep -c 'уже на этом выпуске')"

echo "ТЕСТ 8: rollback возвращает файлы"
eng rollback >/dev/null 2>&1; check "код 0" 0 "$?"
check "выпуск вернулся" v2099.01.01 "$(tag_of)"
check "правка выпуска 2 ушла" 0 "$(grep -c '^# выпуск 2' uv/bin/ccsync_lib/slim.py)"
check "созданный файл убран" нет "$(yes_no test -e uv/bin/ccsync_lib/newmod.py)"
check "удалённый вернулся" да "$(yes_no test -f uv/bin/ccsync_lib/obsolete.py)"
check "правка пользователя цела" 1 "$(grep -c '^# моя правка' uv/bin/ccsync_lib/redact.py)"
out=$(eng rollback 2>&1); check "второй rollback — откатывать нечего" 1 "$?"
out=$(eng update --yes --no-commit 2>&1)
check "откаченный выпуск не предлагается заново" 1 "$(echo "$out" | grep -c 'уже на этом выпуске')"
check "выпуск 2 назван плохим" 1 "$(eng status 2>&1 | grep -c 'Плохие выпуски: v2099.02.01')"

echo "ТЕСТ 9: явный --tag ставит откаченный выпуск, --no-commit не коммитит"
head_before=$(git -C uv rev-parse HEAD)
eng update --tag v2099.02.01 --yes --no-commit >/dev/null 2>&1; check "код 0" 0 "$?"
check "выпуск стал v2099.02.01" v2099.02.01 "$(tag_of)"
check "коммита нет" "$head_before" "$(git -C uv rev-parse HEAD)"
eng rollback >/dev/null 2>&1

echo "ТЕСТ 10: выпуск, который не запускается, откатывается сам"
echo "def broken(:" > "$TREE/bin/ccsync_lib/slim.py"
publish v2099.03.01
before=$(tree_hash)
out=$(eng update --tag v2099.03.01 --yes --no-commit 2>&1); check "код 1" 1 "$?"
check "причина названа" 1 "$(echo "$out" | grep -c 'не прошёл самопроверку')"
check "файлы как были" "$before" "$(tree_hash)"
check "выпуск помечен плохим" 1 "$(eng status 2>&1 | grep -c 'v2099.03.01')"
git -C "$TREE" revert --no-edit HEAD >/dev/null 2>&1

echo "ТЕСТ 11: подпись не тем ключом — отказ"
echo "# выпуск 4" >> "$TREE/bin/ccsync_lib/redact.py"
publish v2099.04.01 "$OTHER"
before=$(tree_hash)
out=$(eng update --tag v2099.04.01 --yes --no-commit 2>&1); check "код 1" 1 "$?"
check "причина названа" 1 "$(echo "$out" | grep -c 'НЕ сходится')"
check "--allow-unverified не обходит неверную подпись" 1 "$(eng update --tag v2099.04.01 --allow-unverified --yes --no-commit >/dev/null 2>&1; echo $?)"
check "файлы как были" "$before" "$(tree_hash)"

echo "ТЕСТ 12: выпуск без подписи — отказ, но с --allow-unverified проходит"
echo "# выпуск 5" >> "$TREE/bin/ccsync_lib/redact.py"
publish v2099.05.01 unsigned
out=$(eng update --tag v2099.05.01 --yes --no-commit 2>&1); check "код 1" 1 "$?"
check "причина названа" 1 "$(echo "$out" | grep -c 'Подпись выпуска v2099.05.01 проверить не удалось')"
out=$(eng update --tag v2099.05.01 --allow-unverified --dry-run 2>&1); check "с флагом — план показан" 0 "$?"

echo "ТЕСТ 13: выпуск требует более нового Python — отказ"
git -C "$TREE" reset -q --hard v2099.01.01 2>/dev/null
echo "# выпуск 6" >> "$TREE/bin/ccsync_lib/redact.py"
python3 - "$TREE" "$SRC/bin" "$PUBLIC" <<'PY'
import sys
from pathlib import Path
sys.path.insert(0, sys.argv[2])
from ccsync_lib import engine_manifest as m
root = Path(sys.argv[1])
manifest = m.build(m.collect(root), "v2099.06.01", sys.argv[3])
manifest["min_python"] = "99.0"
(root / m.MANIFEST_PATH).write_text(m.dumps(manifest), encoding="utf-8")
PY
git -C "$TREE" add -A >/dev/null; git -C "$TREE" commit -qm "выпуск 6"
git -C "$TREE" -c gpg.format=ssh -c user.signingkey="$KEY" tag -s -m v2099.06.01 v2099.06.01
git -C "$TREE" push -q origin HEAD:refs/heads/six "refs/tags/v2099.06.01"
out=$(eng update --tag v2099.06.01 --yes --no-commit 2>&1); check "код 1" 1 "$?"
check "причина названа" 1 "$(echo "$out" | grep -c 'нужен Python 99.0')"

echo "ТЕСТ 14: источник недоступен — отказ без записи"
before=$(tree_hash)
cp uv/bin/ENGINE.json "$STAND/ENGINE.json.keep"
python3 - "$STAND/uv/bin/ENGINE.json" <<'PY'
import json, sys
p = sys.argv[1]; d = json.load(open(p)); d["source"] = "/нет/такого/репозитория.git"
json.dump(d, open(p, "w"), ensure_ascii=False)
PY
out=$(eng update --yes --no-commit 2>&1); check "код 1" 1 "$?"
check "причина названа" 1 "$(echo "$out" | grep -c 'недоступен')"
cp "$STAND/ENGINE.json.keep" uv/bin/ENGINE.json
check "файлы как были" "$before" "$(tree_hash)"

echo "ТЕСТ 15: замок — обновление не стартует, хуки видят замок"
touch home/.claude/ccsync-engine.lock
out=$(eng update --yes --no-commit 2>&1); check "код 1" 1 "$?"
check "причина названа" 1 "$(echo "$out" | grep -c 'Обновление уже идёт')"
hooked() { CLAUDE_CONFIG_DIR="$STAND/home/.claude" python3 -c "
import sys; sys.path.insert(0, '$SRC/bin'); import cchook; print(cchook.engine_update_running())"; }
check "хук видит свежий замок" True "$(hooked)"
python3 -c "import os,time; os.utime('home/.claude/ccsync-engine.lock', (time.time()-600,)*2)"
check "старый замок хук игнорирует" False "$(hooked)"
rm -f home/.claude/ccsync-engine.lock

echo "ТЕСТ 16: старое хранилище без манифеста — --adopt из клона"
git init -q --bare --initial-branch=main old.git
git clone -q old.git old 2>/dev/null; git -C old config user.email o@o; git -C old config user.name old
git -C "$TREE" checkout -q -f v2099.01.01 2>/dev/null
(cd "$TREE" && tar cf - --exclude=.git .) | tar xf - -C old && rm -f old/bin/ENGINE.json
echo "# древняя версия" >> old/bin/ccsync_lib/slim.py
echo "# моя правка" >> old/bin/ccsync_lib/redact.py
rm -f old/bin/ccsync_lib/release-signers  # у старых движков файла ключей нет
git -C old add -A >/dev/null; git -C old commit -qm "старое"; git -C old push -q -u origin main
git clone -q --branch v2099.02.01 "$PUBLIC" clone 2>/dev/null
oldeng() { env -u CLAUDE_CODE_SESSION_ID HOME="$STAND/home" CLAUDE_CONFIG_DIR="$STAND/home/.claude" CCSYNC_LANG=ru \
	GIT_TERMINAL_PROMPT=0 python3 "$STAND/clone/bin/ccsync.py" --vault "$STAND/old" engine "$@"; }
oldeng update --source "$STAND/clone" --yes --no-commit >/dev/null 2>&1; check "без --adopt — отказ" 1 "$?"
out=$(oldeng update --source "$STAND/clone" --adopt --yes 2>&1); check "с --adopt — код 0" 0 "$?"
check "манифест появился" да "$(yes_no test -f old/bin/ENGINE.json)"
check "файл приведён к выпуску" 1 "$(grep -c '^# выпуск 2' old/bin/ccsync_lib/slim.py)"
check "своя правка перезаписана" 0 "$(grep -c '^# моя правка' old/bin/ccsync_lib/redact.py)"
bk=$(ls -d "$STAND"/home/.claude/backups/engine-* | tail -1)
check "прежняя версия — в бэкапе" 1 "$(grep -c '^# моя правка' "$bk/files/bin/ccsync_lib/redact.py")"
check "после этого работает обычный update" 0 "$(CCSYNC_LANG=ru python3 old/bin/ccsync.py --vault "$STAND/old" engine status >/dev/null 2>&1; echo $?)"

echo "ТЕСТ 17: хранилище мейнтейнера не обновляется"
touch uv/release-public.py
eng update --yes --no-commit >/dev/null 2>&1; check "код 1" 1 "$?"
rm -f uv/release-public.py

echo "ТЕСТ 18: check сообщает о новом выпуске один раз"
NOTES=$STAND/home/.claude/ccsync-notices.json
rm -f "$NOTES"
eng check --force >/dev/null 2>&1; check "код 0" 0 "$?"
check "уведомление появилось" 1 "$(grep -c 'Вышел выпуск движка ccsync v2099.06.01' "$NOTES" 2>/dev/null)"
eng check --force >/dev/null 2>&1
check "повторно не дублируется" 1 "$(grep -c 'Вышел выпуск' "$NOTES")"

echo "ТЕСТ 19: check не ходит в сеть чаще раза в сутки и уважает режим off"
python3 - "$STAND/home/.claude/ccsync-engine-state.json" <<'PY'
import json, sys
p = sys.argv[1]; d = json.load(open(p)); d["notified"] = {}; json.dump(d, open(p, "w"))
PY
rm -f "$NOTES"
eng check >/dev/null 2>&1
check "без --force в тот же день — тишина" нет "$(yes_no test -f "$NOTES")"
eng mode off >/dev/null 2>&1
eng check --force >/dev/null 2>&1
check "режим off — тишина" нет "$(yes_no test -f "$NOTES")"
check "режим записан" off "$(python3 -c "import json;print(json.load(open('$STAND/uv/tools/engine-update.json'))['mode'])")"

echo "ТЕСТ 20: режим auto ставит подписанный выпуск сам"
git -C "$TREE" checkout -q -f v2099.01.01 2>/dev/null
echo "# автовыпуск" >> "$TREE/bin/ccsync_lib/obsolete.py"
publish v2099.07.01
eng mode auto >/dev/null 2>&1
rm -f "$NOTES"
eng check --force >/dev/null 2>&1; check "код 0" 0 "$?"
check "выпуск стал v2099.07.01" v2099.07.01 "$(tag_of)"
check "итог — в уведомлении" 1 "$(grep -c 'автоматически обновлён до v2099.07.01' "$NOTES" 2>/dev/null)"
check "правка пользователя цела" 1 "$(grep -c '^# моя правка' uv/tools/tests/dry-run.sh)"

echo "ТЕСТ 21: auto не трогает выпуск, который задевает правленый файл"
echo "# автовыпуск 2" >> "$TREE/tools/tests/dry-run.sh"
publish v2099.08.01
before=$(tree_hash)
rm -f "$NOTES"
eng check --force >/dev/null 2>&1
check "выпуск остался v2099.07.01" v2099.07.01 "$(tag_of)"
check "файлы как были" "$before" "$(tree_hash)"
check "причина — в уведомлении" 1 "$(grep -c 'Автообновление движка до v2099.08.01 не удалось' "$NOTES" 2>/dev/null)"

echo "ТЕСТ 22: манифест с путём за пределы хранилища и подменённый файл — отказ"
cp -r clone evil
python3 - "$STAND/evil/bin/ENGINE.json" <<'PY'
import json, sys
p = sys.argv[1]; d = json.load(open(p)); d["files"]["bin/../../escape.txt"] = "0" * 64
json.dump(d, open(p, "w"))
PY
before=$(tree_hash)
out=$(eng update --source "$STAND/evil" --yes --no-commit 2>&1); check "код 1" 1 "$?"
check "причина названа" 1 "$(echo "$out" | grep -c 'недопустимый путь')"
check "за пределы ничего не записано" нет "$(yes_no test -e "$STAND/escape.txt")"
cp -r clone evil2
echo "# подмена" >> evil2/bin/ccsync_lib/redact.py
out=$(eng update --source "$STAND/evil2" --yes --no-commit 2>&1); check "код 1 при подмене файла" 1 "$?"
check "причина названа" 1 "$(echo "$out" | grep -c 'не совпадает с манифестом')"
check "файлы как были" "$before" "$(tree_hash)"

echo
echo "ИТОГО: успешно $ok, провалено $fail"
[ "$fail" -eq 0 ]
