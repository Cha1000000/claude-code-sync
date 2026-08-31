#!/usr/bin/env python3
"""Прописать хуки ccsync в ~/.claude/settings.json.

Хуки — единственное, что нельзя привезти готовым шаблоном: пути к интерпретатору
и к хранилищу у каждой машины свои, а первый `pull` на машине, где settings.json
уже есть, заменил бы его целиком (трёхсторонний merge включается только со
второго раза, когда есть база для сравнения).

Поэтому здесь мы дописываем хуки прямо в локальный файл, абсолютными путями,
ничего в нём не трогая. Дальше `ccsync.py push tools` свернёт эти пути в
{{PYTHON}} и {{VAULT}} и отдаст в хранилище, а на других машинах `pull`
развернёт их обратно уже под них.

    python3 setup-hooks.py            прописать
    python3 setup-hooks.py --dry-run  показать, что получится
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from pathlib import Path

VAULT = Path(__file__).resolve().parent
sys.path.insert(0, str(VAULT / "bin"))

from ccsync_lib.i18n import tr  # noqa: E402 — путь к движку известен только выше


def config_dir() -> Path:
	override = os.environ.get("CLAUDE_CONFIG_DIR", "").strip()
	return Path(override) if override else Path.home() / ".claude"


def hook(command: str, timeout: int | None = None) -> dict:
	entry: dict = {"type": "command", "command": command}
	if timeout is not None:
		entry["timeout"] = timeout
	return {"hooks": [entry]}


def wanted_hooks() -> dict:
	python, engine = sys.executable, VAULT / "bin" / "cchook.py"
	cleanup = config_dir() / "hooks" / "cleanup-junk-sessions.py"
	return {
		"SessionStart": [
			hook(f"{python} {engine} run {cleanup}"),
			hook(f"{python} {engine} session-start", 45),
		],
		"Stop": [hook(f"{python} {engine} stop", 15)],
		"SessionEnd": [hook(f"{python} {engine} session-end", 190)],
	}


def already_there(existing: list, command_part: str) -> bool:
	"""Есть ли среди хуков события такой же вызов — чтобы не задваивать."""
	for group in existing:
		for entry in group.get("hooks", []) if isinstance(group, dict) else []:
			if command_part in str(entry.get("command", "")):
				return True
	return False


def main() -> int:
	parser = argparse.ArgumentParser(description=tr("Прописать хуки ccsync"))
	parser.add_argument("--dry-run", action="store_true")
	args = parser.parse_args()

	config = config_dir()
	config.mkdir(parents=True, exist_ok=True)
	settings = config / "settings.json"
	data = {}
	if settings.exists():
		try:
			data = json.loads(settings.read_text(encoding="utf-8"))
		except json.JSONDecodeError as error:
			sys.exit(tr("{path} не разбирается как JSON: {error}",
						path=settings, error=error))

	hooks = data.setdefault("hooks", {})
	added = []
	for event, groups in wanted_hooks().items():
		current = hooks.setdefault(event, [])
		for group in groups:
			command = group["hooks"][0]["command"]
			# Сверяем по хвосту команды: путь к интерпретатору мог поменяться.
			marker = command.split("cchook.py", 1)[1].strip() or "cchook.py"
			if already_there(current, f"cchook.py {marker}" if marker != "cchook.py" else marker):
				continue
			current.append(group)
			added.append(f"{event}: {command}")

	if not added:
		print(tr("Хуки уже на месте, ничего не меняю."))
		return 0

	print(tr("Будет добавлено:") if args.dry_run else tr("Добавлено:"))
	for line in added:
		print(f"  {line}")

	if args.dry_run:
		return 0

	if settings.exists():
		shutil.copy2(settings, settings.with_suffix(".json.before-ccsync"))
		print(tr("Прежний файл: {path}",
				 path=settings.with_suffix(".json.before-ccsync")))
	settings.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
	print(tr("Записано: {path}", path=settings))
	print(tr("Дальше: ccsync.py push tools — и хуки уедут на остальные машины."))
	return 0


if __name__ == "__main__":
	sys.exit(main())
