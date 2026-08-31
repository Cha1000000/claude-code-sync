"""Разовый перевод реестров со старой раскладки на новую.

Было — два общих файла, которые правила каждая машина:

    machines.json        {id машины: паспорт}
    project-map.json     {ключ проекта: {id машины: путь}}

Стало — по файлу на машину, потому что владелец каждой части данных всегда один,
а общий файл лишь сводил расходящиеся правки в конфликт при первом же обмене:

    machines/<машина>.json
    project-map/<машина>.json

Функция идемпотентна: когда мигрировать нечего, она молчит и ничего не делает.
Отдельного шага для человека не нужно — код доезжает на другие машины тем же
`git pull`, что и данные, и раскладывает реестр при первом запуске.
"""

from __future__ import annotations

from .vault import Vault, read_json, write_json


def migrate(vault: Vault) -> list[str]:
	"""Разложить общие реестры по файлам машин. Возвращает список изменений."""
	changes: list[str] = []
	changes += _migrate_machines(vault)
	changes += _migrate_project_map(vault)
	return changes


def _migrate_machines(vault: Vault) -> list[str]:
	source = vault.legacy_machines_path
	if not source.exists():
		return []
	data = read_json(source, {})
	moved = 0
	for machine_id, passport in data.items():
		if not isinstance(passport, dict):
			continue
		# Id внутри файла — источник истины: имя файла проходит санитайзер.
		passport = dict(passport)
		passport.setdefault("machine_id", machine_id)
		write_json(vault.machine_path(machine_id), passport)
		moved += 1
	source.unlink()
	return [f"реестр машин разложен по файлам ({moved})"]


def _migrate_project_map(vault: Vault) -> list[str]:
	source = vault.legacy_project_map_path
	if not source.exists():
		return []
	data = read_json(source, {})
	# Переворачиваем «проект → машины» в «машина → проекты».
	by_machine: dict[str, dict[str, str]] = {}
	for project_key, entry in data.items():
		if not isinstance(entry, dict):
			continue
		for machine_id, path in entry.items():
			if path:
				by_machine.setdefault(machine_id, {})[project_key] = str(path)
	for machine_id, paths in by_machine.items():
		# Не затираем то, что машина успела записать в новой раскладке.
		existing = read_json(vault.project_shard_path(machine_id), None)
		if isinstance(existing, dict) and isinstance(existing.get("paths"), dict):
			merged = dict(paths)
			merged.update(existing["paths"])
			paths = merged
		write_json(vault.project_shard_path(machine_id),
				   {"machine_id": machine_id, "paths": paths})
	source.unlink()
	return [f"реестр проектов разложен по файлам ({len(by_machine)})"]
