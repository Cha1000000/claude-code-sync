#!/usr/bin/env python3
"""Единая точка входа для хуков Claude Code — одинаковая на всех ОС.

Раньше хуки были shell-однострочниками:

    (timeout 120 python3 ~/claude-code-sync/bin/ccsync.py push session --quiet >/dev/null 2>&1 &) || true

В нативной Windows от этой строки не работает ничего: `~` не раскрывается,
`python3` называется иначе, `2>/dev/null` и `|| true` — синтаксис sh, а `timeout`
там вообще утилита паузы. Поэтому таймаут, фоновый запуск и подавление ошибок
переехали сюда, в Python, а в settings.json остаётся простой вызов:

    <интерпретатор> <хранилище>/bin/cchook.py session-start

Хук никогда не мешает работать: любая ошибка гасится, код возврата всегда 0.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

BIN_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(BIN_DIR))

CCSYNC = BIN_DIR / "ccsync.py"
SESSION_CONTEXT = BIN_DIR / "session-context.py"

# Действие → (аргументы ccsync, таймаут в секундах, запускать ли в фоне)
ACTIONS = {
	"session-start": (["pull", "--quiet", "--no-autobind"], 25, False),
	"session-end": (["push", "all", "--quiet"], 180, False),
	"stop": (["push", "session", "--debounce", "300", "--quiet"], 120, True),
}


def python_executable() -> str:
	"""Интерпретатор, которым запускать дочерние процессы."""
	return sys.executable or "python3"


def read_payload() -> tuple[str, dict]:
	"""Прочитать JSON, который Claude Code передаёт хуку на stdin.

	Возвращает и исходный текст, и разобранный словарь: текст нужен, чтобы
	передать payload дальше стороннему скрипту-хуку (тот читает свой stdin сам),
	а словарь — чтобы достать session_id.
	"""
	if sys.stdin is None or sys.stdin.isatty():
		return "", {}
	try:
		raw = sys.stdin.read()
	except Exception:
		return "", {}
	if not raw.strip():
		return raw, {}
	try:
		data = json.loads(raw)
	except (json.JSONDecodeError, ValueError):
		return raw, {}
	return raw, data if isinstance(data, dict) else {}


def run_quiet(args: list[str], timeout: int, payload: str = "") -> None:
	"""Выполнить и проглотить любой исход: хук не имеет права ломать сессию."""
	try:
		subprocess.run(
			args,
			timeout=timeout,
			stdout=subprocess.DEVNULL,
			stderr=subprocess.DEVNULL,
			input=payload,
			text=True,
		)
	except Exception:
		pass


def run_detached(args: list[str]) -> None:
	"""Запустить в фоне, не дожидаясь: аналог `( … & )`, но кроссплатформенно."""
	kwargs: dict = {
		"stdout": subprocess.DEVNULL,
		"stderr": subprocess.DEVNULL,
		"stdin": subprocess.DEVNULL,
	}
	if os.name == "nt":
		# Не открывать консольное окно и отвязать от процесса-родителя.
		kwargs["creationflags"] = (
			getattr(subprocess, "CREATE_NO_WINDOW", 0)
			| getattr(subprocess, "DETACHED_PROCESS", 0)
		)
	else:
		kwargs["start_new_session"] = True
	try:
		subprocess.Popen(args, **kwargs)
	except Exception:
		pass


def emit_session_context() -> None:
	"""Напечатать блок «на какой ты машине» — он попадает Клоду в контекст."""
	try:
		result = subprocess.run(
			[python_executable(), str(SESSION_CONTEXT)],
			timeout=20,
			capture_output=True,
			text=True,
			encoding="utf-8",
			errors="replace",
		)
		if result.stdout:
			sys.stdout.write(result.stdout)
	except Exception:
		pass


def sweep_forgotten(*, skip_current: bool) -> None:
	"""Удалить локальные транскрипты забытых сессий. Молча — как и всё в хуке."""
	try:
		from ccsync_lib import identity, ignore

		ignore.sweep_local(identity.claude_config_dir(), skip_current=skip_current)
	except Exception:
		pass


def main(argv: list[str]) -> int:
	if os.name == "nt":
		# Консоль Windows по умолчанию не в UTF-8, а в выводе кириллица.
		for stream in (sys.stdout, sys.stderr):
			try:
				stream.reconfigure(encoding="utf-8", errors="replace")
			except Exception:
				pass

	action = argv[1] if len(argv) > 1 else ""
	raw_payload, payload = read_payload()

	# Универсальный режим: тихо запустить сторонний python-скрипт-хук.
	# Нужен, чтобы чужие хуки не приходилось писать shell-однострочниками
	# вида `python3 … 2>/dev/null || true`, которые в Windows не работают.
	# Payload передаём дальше: такие скрипты читают его из своего stdin.
	if action == "run":
		script = argv[2] if len(argv) > 2 else ""
		if script and Path(script).exists():
			run_quiet([python_executable(), script, *argv[3:]], 60, raw_payload)
		return 0

	if action not in ACTIONS:
		# Неизвестное действие — молча выходим: хук не место для ругани.
		return 0

	args, timeout, detached = ACTIONS[action]
	command = [python_executable(), str(CCSYNC), *args]
	# Id сессии из payload точнее, чем «самый свежий транскрипт в каталоге»:
	# при двух сессиях в одном проекте иначе можно отправить чужую.
	session_id = str(payload.get("session_id") or "").strip()
	if session_id and args and args[0] == "push":
		command += ["--session", session_id]
	if detached:
		run_detached(command)
	else:
		run_quiet(command, timeout)

	if action == "session-end":
		# Сессия закрыта — можно убрать транскрипты, которые ждали её конца.
		sweep_forgotten(skip_current=False)
	if action == "session-start":
		# Добиваем случай, когда сессию прибили жёстко и SessionEnd не отработал.
		sweep_forgotten(skip_current=True)
		emit_session_context()
	return 0


if __name__ == "__main__":
	raise SystemExit(main(sys.argv))
