"""Хранилище: расположение файлов в репо и два реестра — машин и проектов.

Реестр проектов отвечает на вопрос «где лежит проект X на машине Y»: на Windows
он может быть на D:\\, а на маке — в каталоге с пробелами, и вычислить одно из
другого нельзя. Поэтому соответствие хранится явно, по id машины.

Оба реестра разложены по файлам — по одному на машину:

    machines/<машина>.json        паспорт
    project-map/<машина>.json     {ключ проекта: путь на этой машине}

Так сделано ради git: владелец каждой части данных — ровно одна машина, и когда
каждая пишет в свой файл, расходящиеся правки сливаются сами. Пока реестры были
общими файлами, две машины, поработавшие врозь, встречались конфликтом при
первом же обмене.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

from .identity import Machine
from .ignore import TOMBSTONES_DIR_NAME

MACHINES_DIR_NAME = "machines"
PROJECT_MAP_DIR_NAME = "project-map"

# Общие реестры прежнего образца. Мы их только читаем — чтобы хранилище,
# созданное до перехода, не потеряло данные; запись всегда идёт в файл машины.
LEGACY_MACHINES_NAME = "machines.json"
LEGACY_PROJECT_MAP_NAME = "project-map.json"

# Каталоги внутри sessions/, которые проектами не являются.
RESERVED_SESSION_DIRS = {TOMBSTONES_DIR_NAME}


def project_key_from_path(path: str) -> str:
	"""Ключ проекта по имени каталога: «My Vault» → «my-vault»."""
	name = Path(path.replace("\\", "/")).name or "root"
	key = re.sub(r"[^A-Za-z0-9]+", "-", name).strip("-").lower()
	return key or "root"


def read_json(path: Path, default):
	if not path.exists():
		return default
	try:
		return json.loads(path.read_text(encoding="utf-8"))
	except (json.JSONDecodeError, ValueError):
		return default


def write_json(path: Path, data) -> None:
	path.parent.mkdir(parents=True, exist_ok=True)
	path.write_text(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def machine_file_stem(machine_id: str) -> str:
	"""Имя файла для машины.

	Идентификатор задаёт человек, а имя файла должно пережить три разные
	файловые системы: пробелы, слэши и кириллица в именах — источник бед, а
	macOS ещё и нормализует юникод по-своему, из-за чего одно и то же имя
	выглядит для git двумя разными файлами.

	Поэтому имя приводится к ASCII, а когда что-то пришлось заменить —
	дополняется отпечатком id. Без него «ubuntu-ноут» и «ubuntu-нетбук»
	получили бы один файл на двоих, то есть потеряли бы данные друг друга.
	Настоящий id всегда лежит внутри файла, так что обратное преобразование
	имени не нужно.
	"""
	safe = re.sub(r"[^A-Za-z0-9._-]+", "-", machine_id).strip("-.")
	if safe and safe == machine_id:
		return safe
	digest = hashlib.sha1(machine_id.encode("utf-8")).hexdigest()[:8]
	return f"{safe}-{digest}" if safe else f"machine-{digest}"


class Vault:
	"""Файловая раскладка репозитория плюс оба реестра."""

	def __init__(self, root: Path) -> None:
		self.root = Path(root)

	# --- раскладка ------------------------------------------------------

	@property
	def tools_dir(self) -> Path:
		return self.root / "tools"

	@property
	def memory_facts_dir(self) -> Path:
		return self.root / "memory" / "facts"

	@property
	def memory_index(self) -> Path:
		return self.root / "memory" / "index.md"

	@property
	def sessions_dir(self) -> Path:
		return self.root / "sessions"

	@property
	def plans_dir(self) -> Path:
		return self.root / "plans"

	@property
	def machines_dir(self) -> Path:
		return self.root / MACHINES_DIR_NAME

	@property
	def project_map_dir(self) -> Path:
		return self.root / PROJECT_MAP_DIR_NAME

	@property
	def legacy_machines_path(self) -> Path:
		return self.root / LEGACY_MACHINES_NAME

	@property
	def legacy_project_map_path(self) -> Path:
		return self.root / LEGACY_PROJECT_MAP_NAME

	def session_dir_for(self, project_key: str) -> Path:
		return self.sessions_dir / project_key

	# --- реестр машин ---------------------------------------------------

	def load_machines(self) -> dict[str, dict]:
		"""Все известные машины: id → паспорт."""
		found: dict[str, dict] = dict(read_json(self.legacy_machines_path, {}))
		for path in sorted(self.machines_dir.glob("*.json")):
			data = read_json(path, None)
			if not isinstance(data, dict):
				continue
			# Id берём изнутри файла: имя файла могло пройти санитайзер.
			machine_id = str(data.get("machine_id") or path.stem)
			found[machine_id] = data
		return found

	def machine_path(self, machine_id: str) -> Path:
		return self.machines_dir / f"{machine_file_stem(machine_id)}.json"

	def register_machine(self, machine: Machine) -> None:
		"""Записать паспорт машины в её собственный файл."""
		write_json(self.machine_path(machine.machine_id), machine.to_dict())

	def other_machines(self, current_id: str) -> dict[str, dict]:
		return {k: v for k, v in self.load_machines().items() if k != current_id}

	def forget_machine(self, machine_id: str) -> list[str]:
		"""Убрать машину из обоих реестров. Возвращает удалённые пути."""
		removed: list[str] = []
		for path in (self.machine_path(machine_id), self.project_shard_path(machine_id)):
			if path.exists():
				path.unlink()
				removed.append(str(path.relative_to(self.root)))
		return removed

	# --- реестр проектов ------------------------------------------------

	def project_shard_path(self, machine_id: str) -> Path:
		return self.project_map_dir / f"{machine_file_stem(machine_id)}.json"

	def load_project_map(self) -> dict[str, dict[str, str]]:
		"""Сводный вид «ключ проекта → {машина: путь}», собранный из файлов машин.

		Форма сохранена прежней: ею пользуется весь остальной код.
		"""
		mapping: dict[str, dict[str, str]] = {}
		for key, entry in read_json(self.legacy_project_map_path, {}).items():
			if isinstance(entry, dict):
				mapping[key] = dict(entry)
		for path in sorted(self.project_map_dir.glob("*.json")):
			data = read_json(path, None)
			if not isinstance(data, dict):
				continue
			machine_id = str(data.get("machine_id") or path.stem)
			paths = data.get("paths")
			if not isinstance(paths, dict):
				continue
			for key, local_path in paths.items():
				if local_path:
					mapping.setdefault(key, {})[machine_id] = str(local_path)
		return mapping

	def paths_for_machine(self, machine_id: str) -> dict[str, str]:
		"""Ключ проекта → локальный путь; только привязанные к этой машине."""
		return {
			key: entry[machine_id]
			for key, entry in self.load_project_map().items()
			if isinstance(entry, dict) and entry.get(machine_id)
		}

	def bind(self, project_key: str, machine_id: str, path: str) -> None:
		"""Записать путь проекта — только в файл своей машины."""
		shard = self.project_shard_path(machine_id)
		data = read_json(shard, None)
		paths = data.get("paths") if isinstance(data, dict) else None
		paths = dict(paths) if isinstance(paths, dict) else {}
		paths[project_key] = str(path)
		write_json(shard, {"machine_id": machine_id, "paths": paths})

	def unbound_keys(self, machine_id: str) -> list[str]:
		"""Проекты, у которых есть сессии, но нет пути на этой машине."""
		mapping = self.load_project_map()
		bound = set(self.paths_for_machine(machine_id))
		with_sessions = {
			directory.name
			for directory in self.sessions_dir.glob("*")
			if directory.is_dir() and directory.name not in RESERVED_SESSION_DIRS
		}
		return sorted((with_sessions | set(mapping)) - bound)

	def ensure_project_key(self, path: str, machine_id: str, home: str | None = None) -> str:
		"""Ключ для локального пути; при необходимости заводит новую запись.

		Если такой путь уже привязан к этой машине — вернётся существующий ключ,
		даже если имя каталога с тех пор изменилось.
		"""
		mapping = self.load_project_map()
		normalized = str(path).rstrip("/\\")
		for key, entry in mapping.items():
			if isinstance(entry, dict) and str(entry.get(machine_id, "")).rstrip("/\\") == normalized:
				return key
		# Домашний каталог называется одинаково везде, хотя зовётся alex,
		# Alex или ещё как — имя пользователя ключом быть не должно.
		if home and normalized == str(home).rstrip("/\\"):
			key = "home"
			self.bind(key, machine_id, normalized)
			return key
		key = project_key_from_path(normalized)
		# Ключ занят другим путём — разводим суффиксом.
		if key in mapping and mapping[key].get(machine_id, normalized) != normalized:
			suffix = 2
			while f"{key}-{suffix}" in mapping:
				suffix += 1
			key = f"{key}-{suffix}"
		self.bind(key, machine_id, normalized)
		return key


SEARCH_ROOTS_BY_OS = {
	"linux": ["~/projects", "~/Projects", "~/src", "~/dev", "~/Documents", "~/work"],
	"darwin": ["~/projects", "~/Projects", "~/Documents", "~/Developer", "~/src", "~/work"],
	"win32": ["~/projects", "~/Projects", "~/Documents", "D:/Projects", "D:/projects", "C:/Projects"],
}


def find_candidates(project_key: str, machine: Machine, max_depth: int = 3) -> list[str]:
	"""Ступень 1 привязки: поискать каталог с похожим именем в типичных местах."""
	wanted = project_key.replace("-", "").lower()
	found: list[str] = []
	for raw_root in SEARCH_ROOTS_BY_OS.get(machine.os, SEARCH_ROOTS_BY_OS["linux"]):
		root = Path(raw_root).expanduser()
		if not root.is_dir():
			continue
		for candidate in _walk_limited(root, max_depth):
			if re.sub(r"[^a-z0-9]+", "", candidate.name.lower()) == wanted:
				found.append(str(candidate))
	return list(dict.fromkeys(found))


def _walk_limited(root: Path, max_depth: int):
	"""Обход в ширину с ограничением глубины, без «скрытых» каталогов."""
	frontier = [(root, 0)]
	while frontier:
		current, depth = frontier.pop(0)
		if depth >= max_depth:
			continue
		try:
			children = [c for c in current.iterdir() if c.is_dir() and not c.name.startswith(".")]
		except (PermissionError, OSError):
			continue
		for child in children:
			yield child
			frontier.append((child, depth + 1))
