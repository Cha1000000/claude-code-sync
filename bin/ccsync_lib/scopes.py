"""Скоупы: что применимо на этой машине, а что — на соседней.

Одна и та же грамматика описывает принадлежность и факта памяти, и MCP-сервера:

    global                     — верно везде (значение по умолчанию)
    os:linux                   — на любой машине с этой ОС
    linux-desktop            — только эта машина
    [linux-desktop, mac]     — перечисленные машины
    !work-laptop             — везде, КРОМЕ этой машины

Отрицание нужно там, где сервер работает почти везде, а мешает ровно на одной
машине. Через белый список это пришлось бы записать перечислением всех
остальных — и тогда следующая подключённая машина не получила бы сервер, хотя
должна. Отрицание сохраняет «по умолчанию везде» и вычитает исключение.

Правило разрешения: элементы с `!` вычитают всегда и сильнее любого
положительного совпадения. Если после них остался хоть один положительный
элемент, он работает как белый список; если положительных нет вовсе —
подразумевается `global` минус исключения.
"""

from __future__ import annotations

import json
from pathlib import Path

from .i18n import tr
from .identity import Machine

SCOPE_GLOBAL = "global"
OS_PREFIX = "os:"
NEGATE = "!"


def parse(value) -> list[str]:
	"""Строка, список или ничего → список элементов скоупа."""
	if isinstance(value, list):
		items = [str(v).strip() for v in value]
	elif isinstance(value, str):
		items = [value.strip()]
	else:
		items = []
	return [item for item in items if item]


def format(scope: list[str]) -> str:  # noqa: A001 — имя по смыслу, конфликта нет
	"""Список → компактная запись для файла или вывода."""
	if not scope:
		return SCOPE_GLOBAL
	if len(scope) == 1:
		return scope[0]
	return "[" + ", ".join(scope) + "]"


def _entry_matches(entry: str, machine: Machine) -> bool:
	"""Совпадает ли один положительный элемент с машиной."""
	if entry == SCOPE_GLOBAL:
		return True
	if entry.startswith(OS_PREFIX):
		return entry[len(OS_PREFIX):] == machine.os
	return entry == machine.machine_id


def matches(scope: list[str], machine: Machine) -> bool:
	"""Применим ли скоуп к этой машине."""
	positive: list[str] = []
	for raw in scope:
		entry = raw.strip()
		if not entry:
			continue
		if entry.startswith(NEGATE):
			if _entry_matches(entry[len(NEGATE):].strip(), machine):
				return False
		else:
			positive.append(entry)
	if not positive:
		# Только исключения (или пусто) — значит «везде, кроме перечисленного».
		return True
	return any(_entry_matches(entry, machine) for entry in positive)


def is_global(scope: list[str]) -> bool:
	"""Ровно `global`, без исключений: факт или сервер применим буквально везде."""
	entries = [s.strip() for s in scope if s.strip()]
	if any(entry.startswith(NEGATE) for entry in entries):
		return False
	return any(entry == SCOPE_GLOBAL for entry in entries)


def without_machine(scope: list[str], machine: Machine) -> list[str]:
	"""Вычесть машину из скоупа: сахар для `--not-here`.

	Сначала убираем упоминания машины по имени. Если этого хватило — скоуп
	больше не применим здесь, и добавлять нечего. Если машина всё ещё подходит
	(так бывает у `global` и у `os:linux`, где имя вообще не названо) —
	дописываем явное исключение, чтобы сервер по-прежнему доезжал до остальных
	машин, включая ещё не подключённые.
	"""
	kept: list[str] = []
	for raw in scope:
		entry = raw.strip()
		if not entry or entry == machine.machine_id:
			continue
		if entry == NEGATE + machine.machine_id:
			continue
		kept.append(entry)
	if kept and not matches(kept, machine):
		return kept
	# `global` рядом с исключением ничего не добавляет — оно и так «везде, кроме».
	return [e for e in kept if e != SCOPE_GLOBAL] + [NEGATE + machine.machine_id]


def describe(scope: list[str], machine: Machine) -> str:
	"""Короткое пояснение для человека, зачем сервер здесь есть или нет."""
	if matches(scope, machine):
		return tr("применим здесь")
	return tr("не для этой машины")


# --- карта «имя → scope» в файле ----------------------------------------
#
# Одним и тем же файлом описываются и MCP-серверы (tools/mcp-scopes.json), и
# файлы обвязки (tools/host-files.json): имя, а рядом — где оно применимо.


def load_map(path: Path) -> dict[str, list[str]]:
	"""Прочитать карту. Отсутствие ключа означает `global`."""
	try:
		raw = json.loads(path.read_text(encoding="utf-8"))
	except (OSError, json.JSONDecodeError, ValueError):
		return {}
	if not isinstance(raw, dict):
		return {}
	parsed: dict[str, list[str]] = {}
	for name, value in raw.items():
		scope = parse(value)
		if scope:
			parsed[str(name)] = scope
	return parsed


def save_map(path: Path, scope_map: dict[str, list[str]], *,
			 keep_global: bool = False) -> None:
	"""Записать карту.

	По умолчанию `global` не храним: это и есть значение по умолчанию, а список
	сущностей всё равно берётся из другого места (для MCP — из шаблона).
	Там, где карта сама и есть список — как у файлов обвязки, — запись нужна
	даже для `global`: выкинув её, мы забыли бы, что файл вообще синхронизируется.
	"""
	payload: dict[str, object] = {}
	for name, scope in sorted(scope_map.items()):
		if not scope or (is_global(scope) and not keep_global):
			continue
		payload[name] = scope[0] if len(scope) == 1 else scope
	if not payload and not path.exists():
		return
	path.parent.mkdir(parents=True, exist_ok=True)
	path.write_text(
		json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
		encoding="utf-8",
	)


def entry_for(scope_map: dict[str, list[str]], name: str) -> list[str]:
	return scope_map.get(name) or [SCOPE_GLOBAL]
