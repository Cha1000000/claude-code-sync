#!/bin/bash
# Проверка переноса сессии живым Claude Code — единственная, которую нельзя
# сделать автотестом.
#
# Все остальные стенды проверяют файлы: доехал, лежит по нужному пути, записи те
# же. Ни один из них не отвечает на вопрос, который на самом деле волнует: видит
# ли модель на другой стороне содержание разговора. Отличить это глазами
# невозможно — с пустым контекстом Claude отвечает так же уверенно.
#
# Приём простой: кладём в транскрипт кодовое слово и спрашиваем его после
# восстановления. Скрипт готовит такой транскрипт; запустить Claude он может сам
# (--run) или отдать вам готовую команду.
#
#   bash tools/tests/resume-check.sh            подготовить и показать команду
#   bash tools/tests/resume-check.sh --run      подготовить и сразу спросить
#   bash tools/tests/resume-check.sh --clean    убрать за собой
set -u
WORD=${WORD:-ЖЕЛУДЬ}
PROJECT=${PROJECT:-${TMPDIR:-/tmp}/ccsync-resume-check}
CONFIG=${CLAUDE_CONFIG_DIR:-$HOME/.claude}
SLUG=$(python3 -c "import re,sys; print(re.sub(r'[^A-Za-z0-9]','-',sys.argv[1]))" "$PROJECT")
DIR="$CONFIG/projects/$SLUG"

if [ "${1:-}" = "--clean" ]; then
	rm -rf "$DIR" "$PROJECT"
	echo "Убрано: $DIR и $PROJECT"
	exit 0
fi

mkdir -p "$PROJECT" "$DIR"
SESSION=$(python3 - "$DIR" "$PROJECT" "$WORD" <<'PY'
import json, sys, uuid
from pathlib import Path
directory, cwd, word = Path(sys.argv[1]), sys.argv[2], sys.argv[3]
session = str(uuid.uuid4())

def record(uid, parent, role, text, ts):
    entry = {"parentUuid": parent, "isSidechain": False, "type": role, "uuid": uid,
             "timestamp": ts, "userType": "external", "entrypoint": "cli",
             "cwd": cwd, "sessionId": session, "version": "2.1.252"}
    if role == "user":
        entry["message"] = {"role": "user", "content": text}
    else:
        entry["message"] = {"model": "claude-opus-4-5", "id": "msg_" + uuid.uuid4().hex[:20],
                            "type": "message", "role": "assistant",
                            "content": [{"type": "text", "text": text}],
                            "stop_reason": "end_turn",
                            "usage": {"input_tokens": 10, "output_tokens": 5}}
    return entry

ids = [f"{i:08d}-0000-4000-8000-{uuid.uuid4().hex[:12]}" for i in range(4)]
rows = [record(ids[0], None, "user", f"Запомни кодовое слово: {word}. Просто подтверди.",
               "2026-01-01T10:00:00.000Z"),
        record(ids[1], ids[0], "assistant", f"Запомнил: {word}.", "2026-01-01T10:00:05.000Z"),
        record(ids[2], ids[1], "user", "Хорошо, продолжим позже.", "2026-01-01T10:01:00.000Z"),
        record(ids[3], ids[2], "assistant", "Договорились.", "2026-01-01T10:01:04.000Z")]
(directory / f"{session}.jsonl").write_text(
    "\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n", encoding="utf-8")
print(session)
PY
)

QUESTION="Какое кодовое слово я просил запомнить? Ответь одним словом, без пояснений."
echo "Транскрипт с кодовым словом $WORD подготовлен:"
echo "  $DIR/$SESSION.jsonl"
echo

if [ "${1:-}" = "--run" ]; then
	command -v claude >/dev/null || { echo "claude не найден в PATH"; exit 1; }
	echo "Спрашиваю у Claude Code…"
	answer=$(cd "$PROJECT" && timeout 180 claude --print --resume "$SESSION" "$QUESTION" </dev/null 2>&1 | tail -1)
	echo "Ответ: $answer"
	if echo "$answer" | grep -q "$WORD"; then
		echo "✓ перенос работает: контекст восстановился"
		status=0
	else
		echo "✗ слово не названо — контекст не доехал"
		status=1
	fi
	echo
	echo "Убрать за собой: bash $0 --clean"
	exit $status
fi

echo "Выполните и посмотрите, назовёт ли Claude слово $WORD:"
echo "  cd $PROJECT && claude --resume $SESSION"
echo "  затем спросите: $QUESTION"
echo
echo "Или сразу автоматически:  bash $0 --run"
echo "Убрать за собой:          bash $0 --clean"
