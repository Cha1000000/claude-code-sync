"""Claude-обвязка за пределами ~/.claude: скрипты и systemd-юниты.

Скиллы и настройки живут внутри `~/.claude` и уезжают целиком. Но обвязка,
которая обслуживает Claude Code снаружи, лежит в общесистемных каталогах:
чистилка транскриптов — в `~/.local/bin`, её таймер — в `~/.config/systemd/user`.
Каталоги эти общие, рядом лежит и то, что к Claude отношения не имеет вовсе
(монтирование облака, синхронизация буфера обмена, иконки в трее), поэтому
возить их целиком нельзя. Отсюда явный реестр: `tools/host-files.json`
перечисляет ровно те файлы, которые едут, и скоуп каждого.

Ключ реестра — путь внутри `tools/host/`, первый сегмент задаёт категорию, она
же целевой каталог: `bin/x.py` → `~/.local/bin/x.py`. Формат значения тот же,
что у MCP-серверов (имя → scope), и читается теми же функциями из `scopes`.

Два свойства выводятся из самого файла, а не хранятся в реестре:

* всё из `bin/` получает бит `x` — скрипт без него бесполезен, а git его не
  переносит (это видно на `~/.claude/statusline.py`: локально `rwxr-xr-x`,
  в хранилище `rw-r--r--`);
* юнит включается, если у него есть секция `[Install]`. Именно она и означает
  «этот юнит включают»; у `.service`, который запускается таймером, её нет, и
  включать его не нужно.

Ручную правку на месте не затираем: рядом с локальным файлом держится снимок
того, что в прошлый раз пришло из хранилища (по образцу базы для settings.json).
Если локальный файл со снимком разошёлся — значит его правили здесь, и pull его
не трогает, а сообщает об этом.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from . import scopes
from .i18n import tr
from .identity import Machine
from .paths import PathMapper

# Категория → каталог относительно домашнего.
CATEGORIES: dict[str, str] = {
	"bin": ".local/bin",
	"systemd": ".config/systemd/user",
}

# Категории, которым нужен именно Linux: на macOS расписанием заведует launchd,
# на Windows — планировщик задач, и файл systemd там просто мусор.
LINUX_ONLY = ("systemd",)

# Снимки того, что в прошлый раз пришло из хранилища, — чтобы отличить
# нетронутый файл от правленного здесь руками.
BASE_DIR_NAME = "ccsync-host-base"

REGISTRY_FILE = "host-files.json"
HOST_DIR_NAME = "host"

# Аварийный выключатель для тестового стенда: там systemd нет, а если и есть —
# трогать пользовательские юниты тестом нельзя.
NO_SYSTEMCTL_ENV = "CCSYNC_NO_SYSTEMCTL"


@dataclass
class HostReport:
	applied: list[str] = field(default_factory=list)
	skipped: list[str] = field(default_factory=list)
	enabled: list[str] = field(default_factory=list)
	# Правленные здесь руками — молча перезаписать нельзя.
	kept_modified: list[str] = field(default_factory=list)
	# Не удалось включить юнит: (имя, причина).
	failed: list[tuple[str, str]] = field(default_factory=list)

	def summary(self) -> str:
		parts = []
		if self.applied:
			parts.append(tr("положено: {names}", names=", ".join(self.applied)))
		if self.enabled:
			parts.append(tr("включено: {names}", names=", ".join(self.enabled)))
		return "; ".join(parts) or tr("изменений нет")


def load_registry(path: Path) -> dict[str, list[str]]:
	"""Карта «файл → scope». Формат общий со скоупами MCP."""
	return scopes.load_map(path)


def save_registry(path: Path, registry: dict[str, list[str]]) -> None:
	# keep_global: здесь карта — сам список того, что возится, и запись со
	# значением по умолчанию тоже обязана в ней остаться.
	scopes.save_map(path, registry, keep_global=True)


def scope_for(registry: dict[str, list[str]], key: str) -> list[str]:
	return scopes.entry_for(registry, key)


def category_of(key: str) -> str:
	"""Первый сегмент ключа: он же категория, он же целевой каталог."""
	return key.split("/", 1)[0]


def is_known(key: str) -> bool:
	return category_of(key) in CATEGORIES and "/" in key


def suits_os(key: str, machine: Machine) -> bool:
	"""Годится ли категория для этой ОС — отдельно от scope самого файла."""
	return category_of(key) not in LINUX_ONLY or machine.os == "linux"


def applies_here(registry: dict[str, list[str]], key: str, machine: Machine) -> bool:
	return suits_os(key, machine) and scopes.matches(scope_for(registry, key), machine)


def local_path(home: Path, key: str) -> Path:
	category, _, tail = key.partition("/")
	return home / CATEGORIES[category] / tail


def vault_path(host_dir: Path, key: str) -> Path:
	return host_dir / key


def _base_path(config_dir: Path, key: str) -> Path:
	return config_dir / BASE_DIR_NAME / key


def _read_text(path: Path) -> str | None:
	try:
		return path.read_text(encoding="utf-8")
	except (OSError, UnicodeDecodeError):
		return None


def _write_text(path: Path, text: str) -> None:
	path.parent.mkdir(parents=True, exist_ok=True)
	path.write_text(text, encoding="utf-8")


def wants_enable(text: str) -> bool:
	"""Есть ли у юнита секция [Install] — то есть предполагается ли его включать."""
	return any(line.strip().lower() == "[install]" for line in text.splitlines())


# --- push ---------------------------------------------------------------

def export(home: Path, config_dir: Path, host_dir: Path,
		   registry: dict[str, list[str]], mapper: PathMapper,
		   machine: Machine) -> list[str]:
	"""Забрать локальные файлы в хранилище, заменив пути на токены.

	Файл, которого здесь нет, — не ошибка: его могло не быть на этой машине
	вовсе (чужой scope) или он ещё не приехал.
	"""
	sent: list[str] = []
	for key in sorted(registry):
		if not is_known(key) or not applies_here(registry, key, machine):
			continue
		source = local_path(home, key)
		text = _read_text(source)
		if text is None:
			continue
		tokenized = mapper.tokenize(text)
		target = vault_path(host_dir, key)
		if _read_text(target) == tokenized:
			continue
		_write_text(target, tokenized)
		# То, что мы только что отдали, становится снимком: иначе следующий
		# pull посчитал бы нашу же правку чужой и не стал бы её применять.
		_write_text(_base_path(config_dir, key), text)
		sent.append(key)
	return sent


# --- pull ---------------------------------------------------------------

def apply(home: Path, config_dir: Path, host_dir: Path,
		  registry: dict[str, list[str]], mapper: PathMapper,
		  machine: Machine, *, dry_run: bool = False) -> HostReport:
	"""Разложить файлы из хранилища по местам на этой машине."""
	report = HostReport()
	units: list[str] = []

	for key in sorted(registry):
		if not is_known(key):
			report.skipped.append(key)
			continue
		if not applies_here(registry, key, machine):
			continue
		stored = _read_text(vault_path(host_dir, key))
		if stored is None:
			continue

		rendered = mapper.detokenize(stored)
		target = local_path(home, key)
		current = _read_text(target)

		if current == rendered:
			if category_of(key) == "systemd" and wants_enable(rendered):
				units.append(target.name)
			continue

		if current is not None:
			base = _read_text(_base_path(config_dir, key))
			if base is not None and current != base:
				# Файл правили здесь. Перезапись стёрла бы правку молча.
				report.kept_modified.append(key)
				continue

		if dry_run:
			report.applied.append(key)
			continue

		if current is not None:
			shutil.copy2(target, target.with_suffix(target.suffix + ".bak"))
		_write_text(target, rendered)
		_write_text(_base_path(config_dir, key), rendered)
		if category_of(key) == "bin":
			target.chmod(target.stat().st_mode | 0o111)
		elif wants_enable(rendered):
			units.append(target.name)
		report.applied.append(key)

	if units and not dry_run:
		activate_units(units, report, machine)
	return report


def activate_units(names: list[str], report: HostReport, machine: Machine) -> None:
	"""daemon-reload и enable --now, чтобы приехавший таймер сразу работал.

	Сбой здесь не должен валить pull: файл юнита уже лежит на месте, и человеку
	достаточно сказать, что включить осталось руками.
	"""
	if machine.os != "linux" or os.environ.get(NO_SYSTEMCTL_ENV):
		return

	def run(*args: str) -> tuple[bool, str]:
		try:
			done = subprocess.run(["systemctl", "--user", *args],
								  capture_output=True, text=True, timeout=30)
		except (OSError, subprocess.SubprocessError) as error:
			return False, str(error)
		return done.returncode == 0, (done.stderr or done.stdout).strip()

	ok, problem = run("daemon-reload")
	if not ok:
		for name in names:
			report.failed.append((name, problem or "daemon-reload"))
		return

	for name in names:
		ok, problem = run("is-enabled", name)
		if ok:
			continue
		ok, problem = run("enable", "--now", name)
		if ok:
			report.enabled.append(name)
		else:
			report.failed.append((name, problem or "enable"))
