"""Инструменты: настройки, MCP-серверы, плагины, скиллы и команды.

Здесь два разных механизма, и разница принципиальная:

* Скиллы, команды, хуки, планы и файлы памяти — это то, что правит человек.
  Они становятся симлинками в репозиторий: правка сразу попадает в git.
* settings.json, MCP и список плагинов Claude Code переписывает сам, на лету.
  Симлинк там опасен, поэтому они рендерятся из шаблонов при каждом pull.

Секреты (токены) в шаблон не попадают: значение заменяется на {{ENV:ИМЯ}},
а подставляется из локального ccsync-secrets.env.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from . import scopes
from .identity import Machine, find_claude
from .paths import PathMapper

# Имена переменных окружения, значения которых считаем секретами.
SECRET_MARKERS = ("TOKEN", "KEY", "SECRET", "PASSWORD", "PASSWD", "CREDENTIAL")

SECRET_TEMPLATE = "{{ENV:%s}}"

# Карта «имя MCP-сервера → scope». Лежит рядом с шаблоном, в tools/.
MCP_SCOPES_FILE = "mcp-scopes.json"

# Что синхронизируем «как есть», через симлинк.
LINKED_DIRS = ("skills", "commands", "hooks", "plans")

# Одиночные файлы, которые просто копируются в обе стороны.
COPIED_FILES = ("CLAUDE.md", "statusline.py")


@dataclass
class ToolsReport:
	applied: list[str] = field(default_factory=list)
	skipped: list[str] = field(default_factory=list)
	missing_secrets: list[str] = field(default_factory=list)
	# Серверы, убранные с этой машины: их scope говорит, что здесь они лишние.
	removed: list[str] = field(default_factory=list)
	# Лишние здесь, но правленные руками — стирать чужую правку молча нельзя.
	kept_modified: list[str] = field(default_factory=list)
	# Применимы здесь, но не запустятся: нет бинаря или файла. (имя, причина)
	unusable: list[tuple[str, str]] = field(default_factory=list)

	def summary(self) -> str:
		parts = []
		if self.applied:
			parts.append("применено: " + ", ".join(self.applied))
		if self.removed:
			parts.append("убрано: " + ", ".join(self.removed))
		if self.skipped:
			parts.append("пропущено: " + ", ".join(self.skipped))
		return "; ".join(parts) or "изменений нет"


def is_secret_key(key: str) -> bool:
	upper = key.upper()
	return any(marker in upper for marker in SECRET_MARKERS)


# --- общая работа с деревьями ------------------------------------------

def link_or_copy(source: Path, target: Path, *, allow_symlink: bool = True) -> str:
	"""Связать target → source. Где симлинки недоступны (Windows) — скопировать.

	Возвращает применённый способ: "symlink" | "copy" | "already".
	"""
	source = source.resolve()
	if target.is_symlink():
		if Path(os.readlink(target)) == source or target.resolve() == source:
			return "already"
		target.unlink()
	elif target.exists():
		raise FileExistsError(str(target))
	target.parent.mkdir(parents=True, exist_ok=True)
	if allow_symlink:
		try:
			target.symlink_to(source, target_is_directory=source.is_dir())
			return "symlink"
		except (OSError, NotImplementedError):
			pass
	if source.is_dir():
		shutil.copytree(source, target, dirs_exist_ok=True)
	else:
		shutil.copy2(source, target)
	return "copy"


def iter_files(root: Path, _seen: set[str] | None = None):
	"""Все файлы дерева, ПРОХОДЯ сквозь симлинки на каталоги.

	Так сделано намеренно: часть скиллов стоит симлинками на ~/.agents/skills.
	Обычный rglob такой каталог считает файлом-симлинком и пропускает целиком —
	на другой машине скилл бы просто не появился. Защита от петель — по
	реальному пути каталога.
	"""
	seen = _seen if _seen is not None else set()
	real_root = str(root.resolve())
	if real_root in seen:
		return
	seen.add(real_root)
	try:
		entries = sorted(root.iterdir())
	except (PermissionError, OSError):
		return
	for entry in entries:
		if entry.is_dir():
			yield from iter_files(entry, seen)
		elif entry.is_file():
			yield entry


def merge_tree(source: Path, target: Path) -> int:
	"""Скопировать содержимое source в target, не удаляя лишнего. Вернуть счёт файлов."""
	if not source.is_dir():
		return 0
	count = 0
	for item in iter_files(source):
		relative = item.relative_to(source)
		destination = target / relative
		destination.parent.mkdir(parents=True, exist_ok=True)
		if destination.exists() and destination.stat().st_mtime >= item.stat().st_mtime:
			continue
		shutil.copy2(item, destination)
		count += 1
	return count


# --- settings.json ------------------------------------------------------

def convert_json_strings(node, convert):
	"""Применить преобразование ко всем строкам внутри разобранного JSON.

	Важно делать это именно по узлам, а не по тексту документа: путь Windows
	`C:\\Users\\alex`, подставленный в JSON-строку напрямую, даёт невалидную
	escape-последовательность `\\U` и ломает весь файл. Сериализация обратно
	экранирует слеши сама.
	"""
	if isinstance(node, str):
		return convert(node)
	if isinstance(node, list):
		return [convert_json_strings(item, convert) for item in node]
	if isinstance(node, dict):
		return {key: convert_json_strings(value, convert) for key, value in node.items()}
	return node


def export_settings(config_dir: Path, template_path: Path, mapper: PathMapper,
					python_exe: str = "", vault_root: Path | None = None) -> bool:
	"""settings.json → шаблон с токенизированными путями."""
	source = config_dir / "settings.json"
	if not source.exists():
		return False
	try:
		data = json.loads(source.read_text(encoding="utf-8"))
	except json.JSONDecodeError:
		return False

	def collapse(text: str) -> str:
		if vault_root is not None:
			text = collapse_machine_tokens(text, python_exe, vault_root)
		return mapper.tokenize(text)

	template_path.parent.mkdir(parents=True, exist_ok=True)
	template_path.write_text(
		json.dumps(convert_json_strings(data, collapse), ensure_ascii=False, indent=2) + "\n",
		encoding="utf-8",
	)
	return True


SETTINGS_BASE_NAME = ".ccsync-settings-base.json"

# Машинные токены в settings.json: интерпретатор и путь к хранилищу у каждой
# машины свои, а команда хука должна собираться одинаково на всех ОС.
PYTHON_TOKEN = "{{PYTHON}}"
VAULT_TOKEN = "{{VAULT}}"


def quote_arg(value: str) -> str:
	"""Закавычить путь, если в нём пробелы: «C:\\Program Files\\…» иначе развалится."""
	if not value or (value.startswith('"') and value.endswith('"')):
		return value
	return f'"{value}"' if " " in value else value


def render_machine_tokens(text: str, python_exe: str, vault_root: Path,
						  target_os: str = "linux") -> str:
	"""Подставить интерпретатор и путь хранилища этой машины.

	Хвост после {{VAULT}} (например `/bin/cchook.py`) приводится к разделителям
	целевой ОС и кавычится ВМЕСТЕ с корнем: иначе путь, где есть пробел, развалился
	бы — закрывающая кавычка встала бы перед хвостом, а не после него.
	"""
	import re as _re

	def expand_vault(match: _re.Match[str]) -> str:
		tail = match.group(1)
		full = f"{str(vault_root).rstrip('/')}{tail}"
		if target_os == "win32":
			full = full.replace("/", "\\")
		return quote_arg(full)

	text = _re.sub(r"\{\{VAULT\}\}([^\s\"']*)", expand_vault, text)
	return text.replace(PYTHON_TOKEN, quote_arg(python_exe))


def collapse_machine_tokens(text: str, python_exe: str, vault_root: Path) -> str:
	"""Обратная замена — свернуть локальные значения в токены перед выгрузкой.

	Выполняется ДО общей токенизации путей: иначе путь к хранилищу, лежащему
	внутри домашней папки, успел бы превратиться в {{P:home}}/claude-code-sync
	и на машине с другой раскладкой каталогов собрался бы неверно.
	"""
	for value, token in ((str(vault_root), VAULT_TOKEN), (python_exe, PYTHON_TOKEN)):
		if not value:
			continue
		text = text.replace(quote_arg(value), token).replace(value, token)
	return text


def merge_json(base, local, incoming):
	"""Трёхсторонний merge словарей настроек.

	Ключ, который менялся только с одной стороны, берётся оттуда. Если менялся
	с обеих — побеждает локальное значение: настройки этой машины важнее, а
	потерять их молча (как случилось с хуками) недопустимо.
	"""
	if not (isinstance(base, dict) and isinstance(local, dict) and isinstance(incoming, dict)):
		if local == base:
			return incoming
		return local
	result = dict(local)
	for key in set(local) | set(incoming) | set(base):
		in_base, in_local, in_incoming = key in base, key in local, key in incoming
		if not in_incoming:
			# Ключ удалили в хранилище; удаляем и здесь, только если локально не трогали.
			if in_local and in_base and local[key] == base[key]:
				result.pop(key, None)
			continue
		if not in_local:
			if not (in_base and base[key] == incoming[key]):
				result[key] = incoming[key]
			continue
		result[key] = merge_json(base.get(key), local[key], incoming[key])
	return result


def apply_settings(template_path: Path, config_dir: Path, mapper: PathMapper,
				   python_exe: str = "", vault_root: Path | None = None,
				   target_os: str = "linux") -> bool:
	"""Слить шаблон с локальным settings.json под текущую машину."""
	if not template_path.exists():
		return False
	target = config_dir / "settings.json"
	base_path = config_dir / SETTINGS_BASE_NAME
	try:
		template = json.loads(template_path.read_text(encoding="utf-8"))
	except json.JSONDecodeError as error:
		raise ValueError(f"шаблон настроек не разбирается как JSON: {error}") from error

	def expand(text: str) -> str:
		text = mapper.detokenize(text)
		if vault_root is not None:
			text = render_machine_tokens(text, python_exe, vault_root, target_os)
		return text

	incoming = convert_json_strings(template, expand)
	rendered_text = json.dumps(incoming, ensure_ascii=False, indent=2) + "\n"

	if not target.exists():
		target.write_text(rendered_text, encoding="utf-8")
		base_path.write_text(rendered_text, encoding="utf-8")
		return True

	local = _read_json(target, {})
	base = _read_json(base_path, None)
	merged = incoming if base is None else merge_json(base, local, incoming)
	merged_text = json.dumps(merged, ensure_ascii=False, indent=2) + "\n"

	base_path.write_text(rendered_text, encoding="utf-8")
	if merged_text == target.read_text(encoding="utf-8"):
		return False
	shutil.copy2(target, target.with_suffix(".json.bak"))
	target.write_text(merged_text, encoding="utf-8")
	return True


def remember_settings_base(template_path: Path, config_dir: Path, mapper: PathMapper,
						   python_exe: str = "", vault_root: Path | None = None,
						   target_os: str = "linux") -> None:
	"""Зафиксировать текущее состояние шаблона как базу для будущих merge."""
	if not template_path.exists():
		return
	try:
		template = json.loads(template_path.read_text(encoding="utf-8"))
	except json.JSONDecodeError:
		return

	def expand(text: str) -> str:
		text = mapper.detokenize(text)
		if vault_root is not None:
			text = render_machine_tokens(text, python_exe, vault_root, target_os)
		return text

	(config_dir / SETTINGS_BASE_NAME).write_text(
		json.dumps(convert_json_strings(template, expand), ensure_ascii=False, indent=2) + "\n",
		encoding="utf-8",
	)


# --- MCP-серверы --------------------------------------------------------

def read_global_config(config_dir: Path) -> dict:
	"""~/.claude.json — берём оттуда только ветку mcpServers."""
	path = config_dir.parent / ".claude.json"
	if not path.exists():
		path = Path.home() / ".claude.json"
	if not path.exists():
		return {}
	try:
		return json.loads(path.read_text(encoding="utf-8"))
	except (json.JSONDecodeError, ValueError):
		return {}


def load_mcp_scopes(path: Path) -> dict[str, list[str]]:
	"""Карта «сервер → scope». Отсутствие ключа означает `global`."""
	raw = _read_json(path, {})
	if not isinstance(raw, dict):
		return {}
	parsed: dict[str, list[str]] = {}
	for name, value in raw.items():
		scope = scopes.parse(value)
		if scope:
			parsed[str(name)] = scope
	return parsed


def save_mcp_scopes(path: Path, scope_map: dict[str, list[str]]) -> None:
	"""Записать карту. `global` не храним: это и есть значение по умолчанию."""
	payload: dict[str, object] = {}
	for name, scope in sorted(scope_map.items()):
		if not scope or scopes.is_global(scope):
			continue
		payload[name] = scope[0] if len(scope) == 1 else scope
	if not payload and not path.exists():
		return
	path.parent.mkdir(parents=True, exist_ok=True)
	path.write_text(
		json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
		encoding="utf-8",
	)


def mcp_scope_for(scope_map: dict[str, list[str]], name: str) -> list[str]:
	return scope_map.get(name) or [scopes.SCOPE_GLOBAL]


def export_mcp(
	config_dir: Path,
	template_path: Path,
	mapper: PathMapper,
	machine: Machine,
	scopes_path: Path,
) -> list[str]:
	"""mcpServers → шаблон. Пути токенизируются, секреты маскируются.

	Локальная ветка `mcpServers` — источник истины только для серверов,
	применимых на этой машине. Сервер, помеченный чужим scope, в конфиге
	отсутствует по определению, и вычёркивать его из общего шаблона мы не
	вправе: он переносится из прежней версии как есть.
	"""
	servers = read_global_config(config_dir).get("mcpServers") or {}
	masked: dict[str, dict] = {}
	secret_keys: list[str] = []
	for name, definition in servers.items():
		entry = convert_json_strings(definition, mapper.tokenize)
		env = entry.get("env")
		if isinstance(env, dict):
			for key in list(env):
				if is_secret_key(key):
					env[key] = SECRET_TEMPLATE % key
					secret_keys.append(key)
		masked[name] = entry
	previous = _read_json(template_path, {})
	scope_map = load_mcp_scopes(scopes_path)
	if isinstance(previous, dict):
		for name, definition in previous.items():
			if name in masked:
				continue
			if not scopes.matches(mcp_scope_for(scope_map, name), machine):
				masked[name] = definition
	template_path.parent.mkdir(parents=True, exist_ok=True)
	template_path.write_text(
		json.dumps(masked, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
		encoding="utf-8",
	)
	return sorted(set(secret_keys))


def apply_mcp(
	template_path: Path,
	mapper: PathMapper,
	secrets: dict[str, str],
	config_dir: Path,
	*,
	machine: Machine,
	scopes_path: Path,
	dry_run: bool = False,
) -> ToolsReport:
	"""Привести MCP-серверы этой машины в соответствие с шаблоном и скоупами.

	Применимый здесь сервер ставится через `claude mcp add-json`; помеченный
	чужим scope — убирается через `claude mcp remove`, но только если запись
	совпадает с шаблонной. Расхождение означает ручную правку, и стирать её
	молча нельзя.
	"""
	report = ToolsReport()
	if not template_path.exists():
		return report
	wanted = json.loads(template_path.read_text(encoding="utf-8"))
	scope_map = load_mcp_scopes(scopes_path)
	existing = read_global_config(config_dir).get("mcpServers") or {}
	for name, definition in wanted.items():
		expanded = convert_json_strings(definition, mapper.detokenize)
		rendered_text, missing = _fill_secrets(
			json.dumps(expanded, ensure_ascii=False), secrets)
		rendered = json.loads(rendered_text)
		if not scopes.matches(mcp_scope_for(scope_map, name), machine):
			_remove_foreign_server(name, rendered, existing, report, dry_run=dry_run)
			continue
		# Секретов не хватает только там, где сервер вообще нужен.
		report.missing_secrets.extend(missing)
		if name in existing and rendered == existing[name]:
			report.skipped.append(name)
		elif dry_run:
			report.applied.append(f"{name} (dry-run)")
		else:
			executable = find_claude()
			if not executable:
				report.skipped.append(f"{name} (не найден исполняемый файл claude)")
				continue
			result = subprocess.run(
				[executable, "mcp", "add-json", name, rendered_text, "-s", "user"],
				capture_output=True, text=True, encoding="utf-8", errors="replace",
			)
			if result.returncode == 0:
				report.applied.append(name)
			else:
				report.skipped.append(
					f"{name} (ошибка: {(result.stderr or result.stdout).strip()[:80]})")
		problem = probe_runnable(rendered)
		if problem:
			report.unusable.append((name, problem))
	report.missing_secrets = sorted(set(report.missing_secrets))
	return report


def _remove_foreign_server(
	name: str,
	rendered: dict,
	existing: dict,
	report: ToolsReport,
	*,
	dry_run: bool,
) -> None:
	"""Убрать с этой машины сервер, помеченный чужим scope."""
	if name not in existing:
		return
	if existing[name] != rendered:
		report.kept_modified.append(name)
		return
	if dry_run:
		report.removed.append(f"{name} (dry-run)")
		return
	executable = find_claude()
	if not executable:
		report.skipped.append(f"{name} (не найден исполняемый файл claude)")
		return
	result = subprocess.run(
		[executable, "mcp", "remove", name, "-s", "user"],
		capture_output=True, text=True, encoding="utf-8", errors="replace",
	)
	if result.returncode == 0:
		report.removed.append(name)
	else:
		report.skipped.append(
			f"{name} (не убран: {(result.stderr or result.stdout).strip()[:80]})")


# Абсолютный путь: POSIX-корень, ~ или «буква диска» Windows.
_ABSOLUTE_PATH = re.compile(r"^(/|~/|[A-Za-z]:[\\/])")


def probe_runnable(definition: dict) -> str | None:
	"""Заведётся ли сервер здесь. Возвращает причину, если очевидно нет.

	Проверка нарочно поверхностная: есть ли сам исполняемый файл и лежат ли на
	месте абсолютные пути в аргументах. Это подсказка, а не приговор — сервер
	вправе создать свой файл сам, поэтому результат никуда не применяется
	автоматически, а только показывается человеку.
	"""
	if not isinstance(definition, dict):
		return None
	if definition.get("type") not in (None, "", "stdio"):
		return None  # http/sse проверять нечем, туда мы не ходим
	command = definition.get("command")
	if isinstance(command, str) and command:
		if _ABSOLUTE_PATH.match(command):
			if not Path(command).expanduser().exists():
				return f"нет файла {command}"
		elif shutil.which(command) is None:
			return f"нет команды {command} в PATH"
	for arg in definition.get("args") or []:
		if isinstance(arg, str) and _ABSOLUTE_PATH.match(arg):
			if not Path(arg).expanduser().exists():
				return f"нет файла {arg}"
	return None


def _fill_secrets(text: str, secrets: dict[str, str]) -> tuple[str, list[str]]:
	"""Подставить значения секретов; вернуть список недостающих."""
	missing: list[str] = []
	for key, value in secrets.items():
		text = text.replace(SECRET_TEMPLATE % key, value)
	for marker in _find_secret_markers(text):
		missing.append(marker)
		# Оставить плейсхолдер нельзя — сервер стартует с мусорным значением.
		text = text.replace(SECRET_TEMPLATE % marker, "")
	return text, missing


def _find_secret_markers(text: str) -> list[str]:
	import re
	return sorted(set(re.findall(r"\{\{ENV:([A-Za-z0-9_]+)\}\}", text)))


# --- плагины ------------------------------------------------------------

def export_plugins(config_dir: Path, target_path: Path) -> bool:
	"""Список плагинов и маркетплейсов — без самих клонов (19 МБ восстановимы)."""
	plugins_dir = config_dir / "plugins"
	settings = config_dir / "settings.json"
	payload = {
		"installed_plugins": _read_json(plugins_dir / "installed_plugins.json", {}),
		"known_marketplaces": _read_json(plugins_dir / "known_marketplaces.json", {}),
		"enabled": _read_json(settings, {}).get("enabledPlugins", {}),
		"extra_marketplaces": _read_json(settings, {}).get("extraKnownMarketplaces", {}),
	}
	target_path.parent.mkdir(parents=True, exist_ok=True)
	target_path.write_text(
		json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
		encoding="utf-8",
	)
	return True


def missing_plugins(plugins_path: Path, config_dir: Path) -> list[str]:
	"""Какие плагины из репо ещё не стоят на этой машине."""
	if not plugins_path.exists():
		return []
	payload = _read_json(plugins_path, {})
	wanted = payload.get("enabled") or {}
	installed_names = installed_plugin_names(config_dir)
	return sorted(name for name, on in wanted.items() if on and name not in installed_names)


def installed_plugin_names(config_dir: Path) -> set[str]:
	"""Имена вида `plugin@marketplace`, установленные на этой машине.

	Формат v2: {"version": 2, "plugins": {"name@marketplace": [...]}} — ключи
	уже полные. Более старая раскладка {marketplace: {name: ...}} тоже понимается.
	"""
	installed = _read_json(config_dir / "plugins" / "installed_plugins.json", {})
	if not isinstance(installed, dict):
		return set()
	if isinstance(installed.get("plugins"), dict):
		return set(installed["plugins"].keys())
	names: set[str] = set()
	for marketplace, entries in installed.items():
		if marketplace == "version":
			continue
		if isinstance(entries, (dict, list)):
			names.update(f"{name}@{marketplace}" for name in entries)
	return names


def _read_json(path: Path, default):
	if not path.exists():
		return default
	try:
		return json.loads(path.read_text(encoding="utf-8"))
	except (json.JSONDecodeError, ValueError):
		return default
