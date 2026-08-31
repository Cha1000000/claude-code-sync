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
