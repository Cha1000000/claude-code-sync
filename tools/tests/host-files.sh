#!/bin/bash
# Обвязка: скрипты ~/.local/bin и systemd-юниты переезжают между машинами
set -u
BASE=${TMPDIR:-/tmp}/ccsync-tests
STAND=$BASE/host-files
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
	mkdir -p $m/.claude/projects $m/.local/bin $m/.config/systemd/user
	# CCSYNC_NO_SYSTEMCTL: на стенде systemd трогать нельзя — юниты у него общие
	# с живой сессией, и enable зацепил бы настоящую машину.
	cat > "$STAND/$m.sh" <<EOF
#!/bin/bash
cd "$STAND/$m" || exit 1
exec env -u CLAUDE_CODE_SESSION_ID HOME="$STAND/$m" CLAUDE_CONFIG_DIR="$STAND/$m/.claude" \
     CCSYNC_NO_SYSTEMCTL=1 \
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

# Скрипт с абсолютным путём внутри: он должен пережить переезд в чужой $HOME.
cat > "$STAND/m1/.local/bin/tool.sh" <<EOF
#!/bin/bash
echo "живу в $STAND/m1/.local/bin"
EOF
chmod +x "$STAND/m1/.local/bin/tool.sh"
cat > "$STAND/m1/.config/systemd/user/tool.timer" <<'EOF'
[Timer]
OnCalendar=daily

[Install]
WantedBy=timers.target
EOF
cat > "$STAND/m1/.local/bin/only-here.sh" <<'EOF'
#!/bin/bash
echo только для m1
EOF
chmod +x "$STAND/m1/.local/bin/only-here.sh"

echo "ТЕСТ 1 — файлы берутся под синхронизацию"
"$STAND/m1.sh" host add "$STAND/m1/.local/bin/tool.sh" >/dev/null
"$STAND/m1.sh" host add "$STAND/m1/.config/systemd/user/tool.timer" >/dev/null
"$STAND/m1.sh" host add "$STAND/m1/.local/bin/only-here.sh" >/dev/null
"$STAND/m1.sh" host scope bin/only-here.sh --here >/dev/null
"$STAND/m1.sh" push tools >/dev/null 2>&1
check "скрипт лёг в хранилище" "да" \
	"$([ -e "$STAND/m1vault/tools/host/bin/tool.sh" ] && echo да || echo нет)"
check "юнит лёг в хранилище" "да" \
	"$([ -e "$STAND/m1vault/tools/host/systemd/tool.timer" ] && echo да || echo нет)"
check "реестр создан" "да" \
	"$([ -e "$STAND/m1vault/tools/host-files.json" ] && echo да || echo нет)"
check "абсолютный путь заменён токеном" "да" \
	"$(grep -q '{{HOME}}/.local/bin' "$STAND/m1vault/tools/host/bin/tool.sh" && echo да || echo нет)"
check "чужой домашний каталог не уехал" "нет" \
	"$(grep -q "$STAND/m1" "$STAND/m1vault/tools/host/bin/tool.sh" && echo да || echo нет)"

echo "ТЕСТ 2 — на другой машине файлы раскладываются по местам"
(cd "$STAND/m2vault" && git pull -q --rebase)
"$STAND/m2.sh" pull tools >/dev/null 2>&1
check "скрипт положен" "да" \
	"$([ -e "$STAND/m2/.local/bin/tool.sh" ] && echo да || echo нет)"
check "юнит положен" "да" \
	"$([ -e "$STAND/m2/.config/systemd/user/tool.timer" ] && echo да || echo нет)"
check "исполняемый бит выставлен" "да" \
	"$([ -x "$STAND/m2/.local/bin/tool.sh" ] && echo да || echo нет)"
check "путь развёрнут под этот дом" "да" \
	"$(grep -q "$STAND/m2/.local/bin" "$STAND/m2/.local/bin/tool.sh" && echo да || echo нет)"
check "файл чужого scope не появился" "нет" \
	"$([ -e "$STAND/m2/.local/bin/only-here.sh" ] && echo да || echo нет)"
check "status показывает обвязку" "да" \
	"$("$STAND/m2.sh" status 2>/dev/null | grep -q 'Обвязка' && echo да || echo нет)"
check "status не жалуется на расхождение" "нет" \
	"$("$STAND/m2.sh" status 2>/dev/null | grep -q 'расходится' && echo да || echo нет)"

echo "ТЕСТ 3 — ручную правку на месте не затираем"
echo "# правка руками" >> "$STAND/m2/.local/bin/tool.sh"
printf '#!/bin/bash\necho версия два\n' > "$STAND/m1/.local/bin/tool.sh"
"$STAND/m1.sh" push tools >/dev/null 2>&1
(cd "$STAND/m2vault" && git pull -q --rebase)
out=$("$STAND/m2.sh" pull tools 2>&1)
check "правка на месте уцелела" "да" \
	"$(grep -q 'правка руками' "$STAND/m2/.local/bin/tool.sh" && echo да || echo нет)"
check "про это сказано вслух" "да" \
	"$(echo "$out" | grep -q 'правлен здесь руками' && echo да || echo нет)"

echo "ТЕСТ 4 — нетронутый файл обновляется молча"
rm -f "$STAND/m2/.local/bin/tool.sh"
"$STAND/m2.sh" pull tools >/dev/null 2>&1
check "приехала новая версия" "да" \
	"$(grep -q 'версия два' "$STAND/m2/.local/bin/tool.sh" && echo да || echo нет)"
check "бит выставлен и на обновлённом" "да" \
	"$([ -x "$STAND/m2/.local/bin/tool.sh" ] && echo да || echo нет)"

echo "ТЕСТ 5 — категория systemd не едет на чужую ОС даже с global"
# Оба файла делаем общими: тогда единственная причина не положить юнит —
# сама категория, а не scope. Иначе тест доказывал бы работу scope, не ОС.
"$STAND/m1.sh" host scope bin/tool.sh --global >/dev/null
"$STAND/m1.sh" host scope systemd/tool.timer --global >/dev/null
"$STAND/m1.sh" push tools >/dev/null 2>&1
(cd "$STAND/m2vault" && git pull -q --rebase)
python3 - "$STAND/m2/.claude/ccsync-machine.json" <<'EOF'
import json, sys
path = sys.argv[1]
data = json.loads(open(path, encoding="utf-8").read())
data["os"] = "darwin"          # притворяемся маком: launchd вместо systemd
open(path, "w", encoding="utf-8").write(json.dumps(data, ensure_ascii=False, indent=2))
EOF
rm -f "$STAND/m2/.config/systemd/user/tool.timer" "$STAND/m2/.local/bin/tool.sh"
"$STAND/m2.sh" pull tools >/dev/null 2>&1
check "юнит на маке не появился" "нет" \
	"$([ -e "$STAND/m2/.config/systemd/user/tool.timer" ] && echo да || echo нет)"
check "а скрипт — появился" "да" \
	"$([ -e "$STAND/m2/.local/bin/tool.sh" ] && echo да || echo нет)"

T1=$STAND/m1/.local/bin/tool.sh; T2=$STAND/m2/.local/bin/tool.sh
V1=$STAND/m1vault/tools/host/bin/tool.sh
sync1() { (cd "$STAND/m1vault" && git pull -q --rebase 2>/dev/null); }

echo "ТЕСТ 6 — push устаревшей копии не затирает свежую (случай 27.09)"
# У m2 нетронутая «версия два» и снимок той же версии, а m1 уже отдал новую.
printf '#!/bin/bash\necho версия три\n' > "$T1"
"$STAND/m1.sh" push tools >/dev/null 2>&1
out=$("$STAND/m2.sh" push tools 2>&1)
sync1
check "в хранилище осталась версия три" "да" "$(grep -q 'версия три' "$V1" && echo да || echo нет)"
check "push сказал, что tool.sh не отдан" "да" \
	"$(echo "$out" | grep -q 'не отдано.*bin/tool.sh' && echo да || echo нет)"
"$STAND/m2.sh" pull tools >/dev/null 2>&1
check "pull привёз версию три" "да" "$(grep -q 'версия три' "$T2" && echo да || echo нет)"

echo "ТЕСТ 7 — без снимка push не затирает хранилище"
printf '#!/bin/bash\necho версия четыре\n' > "$T1"
"$STAND/m1.sh" push tools >/dev/null 2>&1
rm -rf "$STAND/m2/.claude/ccsync-host-base"
"$STAND/m2.sh" push tools >/dev/null 2>&1
sync1
check "в хранилище осталась версия четыре" "да" "$(grep -q 'версия четыре' "$V1" && echo да || echo нет)"
"$STAND/m2.sh" pull tools >/dev/null 2>&1
check "pull привёз версию четыре" "да" "$(grep -q 'версия четыре' "$T2" && echo да || echo нет)"

echo "ТЕСТ 8 — правка, сделанная здесь, уезжает"
printf '#!/bin/bash\necho правка на m2\n' > "$T2"
"$STAND/m2.sh" push tools >/dev/null 2>&1
sync1
check "правка m2 в хранилище" "да" "$(grep -q 'правка на m2' "$V1" && echo да || echo нет)"

echo "ТЕСТ 9 — нечитаемый файл обвязки не пропускается молча"
printf '#!/bin/bash\n# \xff\xfe не UTF-8\n' > "$T2"
out=$("$STAND/m2.sh" push tools 2>&1)
check "push сказал, что tool.sh не прочитать" "да" \
	"$(echo "$out" | grep -q 'не читается.*bin/tool.sh' && echo да || echo нет)"

echo
echo "ИТОГО: успешно $ok, провалено $fail"
exit $fail
