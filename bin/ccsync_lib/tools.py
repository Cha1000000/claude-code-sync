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
from .i18n import tr
from .identity import Machine, find_claude
from .paths import PathMapper

# Имена переменных окружения, значения которых считаем секретами.
SECRET_MARKERS = ("TOKEN", "KEY", "SECRET", "PASSWORD", "PASSWD", "CREDENTIAL")

SECRET_TEMPLATE = "{{ENV:%s}}"

# Карта «имя MCP-сервера → scope». Лежит рядом с шаблоном, в tools/.
MCP_SCOPES_FILE = "mcp-scopes.json"

# Что синхронизируем «как есть», через симлинк.
LINKED_DIRS = ("skills", "commands", "hooks", "plans", "agents")

# Одиночные файлы ~/.claude, которые просто копируются в обе стороны.
# Копируются как есть, без токенизации путей, — значит внутри не должно быть
# абсолютных путей машины и секретов.
COPIED_FILES = ("CLAUDE.md", "statusline.py")

# Свои дополнительные файлы того же рода — списком имён в tools/ хранилища:
# ["my-statusline.py", "extra-settings.json"]. Это данные, а не код движка.
EXTRA_COPIED_FILE = "copied-files.json"


def copied_files(tools_dir: Path) -> tuple[str, ...]:
	"""Всё, что копируется: встроенные файлы плюс свои из copied-files.json.

	Принимаем только простые имена файлов ~/.claude — без каталогов и «..»,
	чтобы запись в списке не могла указать за пределы каталога конфигурации.
	"""
	names = list(COPIED_FILES)
	extra = read_vault_json(tools_dir / EXTRA_COPIED_FILE, expect=list) or []
	if isinstance(extra, list):
		for name in extra:
			if (isinstance(name, str) and name and name == Path(name).name
					and name not in (".", "..") and name not in names):
				names.append(name)
	return tuple(names)

# Снимки того, что в последний раз синхронизировалось, — чтобы отличить правку
# на этой машине от устаревшей копии (по образцу hostfiles).
COPIED_BASE_DIR = "ccsync-copied-base"


def _copied_base(config_dir: Path, name: str) -> Path:
	return config_dir / COPIED_BASE_DIR / name


def _read_bytes(path: Path) -> bytes | None:
	try:
		return path.read_bytes()
	except OSError:
		return None


def _write_bytes(path: Path, data: bytes) -> None:
	path.parent.mkdir(parents=True, exist_ok=True)
	path.write_bytes(data)


def export_copied(config_dir: Path, tools_dir: Path,
				  *, dry_run: bool = False) -> tuple[list[str], list[str]]:
	"""Выгрузить копируемые файлы, правленные на этой машине.

	Возвращает (отданные, не отданные). Не отдаётся файл, у которого в
	хранилище другая версия, если здесь он со времени последней синхронизации
	не менялся: это свежая правка с другой машины, и выгрузка затёрла бы её.
	Так же — если снимка нет вовсе (движок со снимками здесь ещё не
	синхронизировался): по одной копии не понять, чья версия новее. Снимок
	заведёт ближайший pull.

	dry_run — снимки не обновляются: они лежат на машине, а не в хранилище.
	"""
	sent: list[str] = []
	stale: list[str] = []
	for name in copied_files(tools_dir):
		source = config_dir / name
		if not source.exists() or source.is_symlink():
			continue
		local = source.read_bytes()
		target = tools_dir / name
		stored = _read_bytes(target)
		base_path = _copied_base(config_dir, name)
		base = _read_bytes(base_path)
		if stored == local:
			if base != local and not dry_run:
				_write_bytes(base_path, local)
			continue
		if stored is not None and (base is None or local == base):
			stale.append(name)
			continue
		_write_bytes(target, local)
		if not dry_run:
			_write_bytes(base_path, local)
		sent.append(name)
	return sent, stale


def apply_copied(config_dir: Path, tools_dir: Path,
				 *, dry_run: bool = False) -> tuple[list[str], list[str]]:
	"""Разложить копируемые файлы из хранилища. Возвращает (обновлённые, оставленные).

	Файл, правленный здесь (разошёлся со снимком), не трогаем — он уедет со
	следующим push. Снимка нет (первая синхронизация после обновления движка) —
	берём версию из хранилища, прежнюю кладём рядом в .bak.

	dry_run — ничего не пишется, возвращается то, что было бы сделано.
	"""
	applied: list[str] = []
	kept: list[str] = []
	for name in copied_files(tools_dir):
		stored = _read_bytes(tools_dir / name)
		if stored is None:
			continue
		target = config_dir / name
		current = _read_bytes(target)
		base_path = _copied_base(config_dir, name)
		base = _read_bytes(base_path)
		if current == stored:
			if base != stored and not dry_run:
				_write_bytes(base_path, stored)
			continue
		if current is not None and base is not None and current != base:
			kept.append(name)
			continue
		if dry_run:
			applied.append(name)
			continue
		if current is not None and base is None:
			shutil.copy2(target, target.with_name(target.name + ".bak"))
		_write_bytes(target, stored)
		_write_bytes(base_path, stored)
		applied.append(name)
	return applied, kept


@dataclass
class ToolsReport:
	applied: list[str] = field(default_factory=list)
	skipped: list[str] = field(default_factory=list)
	missing_secrets: list[str] = field(default_factory=list)
	# Серверы, убранные с этой машины: их scope говорит, что здесь они лишние.
	removed: list[str] = field(default_factory=list)
	# Лишние здесь, но правленные руками — стирать чужую правку молча нельзя.
	kept_modified: list[str] = field(default_factory=list)
	# Применимы здесь и правлены здесь — pull их не трогает, отдаст push.
	kept_local: list[str] = field(default_factory=list)
	# Удалены здесь, а в хранилище не менялись — pull их не возвращает.
	deleted_here: list[str] = field(default_factory=list)
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
					python_exe: str = "", vault_root: Path | None = None,
					*, dry_run: bool = False) -> tuple[bool, bool]:
	"""settings.json → шаблон с токенизированными путями.

	В шаблон уходит только изменённое здесь. Трёхсторонний merge: снимок (на
	чём машина в прошлый раз сошлась с хранилищем), здешний settings.json и
	шаблон в хранилище. Ключ, который здесь не трогали, остаётся таким, каким
	его оставила другая машина, — даже если здешняя копия устарела. Правили и
	здесь, и там — побеждает здешнее, как и при pull. Сравниваем в токенах:
	значения из хранилища не проходят туда-обратно через развёртку путей и
	возвращаются в шаблон нетронутыми.

	Возвращает (отдано, не_отдано). Не отдаётся, если шаблон в хранилище уже
	есть, а снимка нет: неизвестно, какие ключи меняла эта машина.

	dry_run — снимок не обновляется.
	"""
	source = config_dir / "settings.json"
	local = _read_json(source, None)
	if not isinstance(local, dict):
		return False, False

	def collapse(text: str) -> str:
		if vault_root is not None:
			text = collapse_machine_tokens(text, python_exe, vault_root)
		return mapper.tokenize(text)

	local_tokens = convert_json_strings(local, collapse)
	incoming = read_vault_json(template_path, expect=dict)
	base_path = config_dir / SETTINGS_BASE_NAME
	if isinstance(incoming, dict):
		base = _read_json(base_path, None)
		if not isinstance(base, dict):
			return False, True
		merged = merge_json(convert_json_strings(base, collapse), local_tokens, incoming)
		template = _order_like(merged, incoming)
	else:
		# Шаблона ещё нет (первая машина) — отдавать здешнее целиком нечем рисковать.
		template = local_tokens

	_write_text_atomic(template_path, json.dumps(template, ensure_ascii=False, indent=2) + "\n")
	if not dry_run:
		# Снимок — сам здешний settings.json, а не шаблон: на нём машина теперь
		# сошлась с хранилищем. Шаблон может нести чужие ключи, которых здесь
		# ещё нет, и следующий push принял бы их отсутствие за удаление здесь.
		base_path.write_text(json.dumps(local, ensure_ascii=False, indent=2) + "\n",
							 encoding="utf-8")
	return True, False


def _order_like(value, reference):
	"""Разложить ключи value в порядке reference.

	Иначе шаблон менялся бы от одной перестановки ключей, и каждый push с
	другой машины давал бы в git пустой шум.
	"""
	if not (isinstance(value, dict) and isinstance(reference, dict)):
		return value
	ordered = {key: _order_like(value[key], reference[key]) for key in reference if key in value}
	ordered.update((key, item) for key, item in value.items() if key not in ordered)
	return ordered


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
				   target_os: str = "linux", *, dry_run: bool = False) -> bool:
	"""Слить шаблон с локальным settings.json под текущую машину.

	dry_run — ничего не пишется; True значит, что settings.json изменился бы.
	"""
	if not template_path.exists():
		return False
	target = config_dir / "settings.json"
	base_path = config_dir / SETTINGS_BASE_NAME
	try:
		template = json.loads(template_path.read_text(encoding="utf-8"))
	except json.JSONDecodeError as error:
		raise ValueError(tr("шаблон настроек не разбирается как JSON: {error}",
							error=error)) from error

	def expand(text: str) -> str:
		text = mapper.detokenize(text)
		if vault_root is not None:
			text = render_machine_tokens(text, python_exe, vault_root, target_os)
		return text

	incoming = convert_json_strings(template, expand)
	rendered_text = json.dumps(incoming, ensure_ascii=False, indent=2) + "\n"

	if not target.exists():
		if dry_run:
			return True
		target.write_text(rendered_text, encoding="utf-8")
		base_path.write_text(rendered_text, encoding="utf-8")
		return True

	local = _read_json(target, {})
	base = _read_json(base_path, None)
	merged = incoming if base is None else merge_json(base, local, incoming)
	merged_text = json.dumps(merged, ensure_ascii=False, indent=2) + "\n"

	if dry_run:
		return merged_text != target.read_text(encoding="utf-8")
	base_path.write_text(rendered_text, encoding="utf-8")
	if merged_text == target.read_text(encoding="utf-8"):
		return False
	shutil.copy2(target, target.with_suffix(".json.bak"))
	target.write_text(merged_text, encoding="utf-8")
	return True


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


# Карта «имя → scope» устроена одинаково для MCP-серверов и для файлов обвязки,
# поэтому сама работа с файлом живёт в scopes; здесь — привычные имена.

def load_mcp_scopes(path: Path) -> dict[str, list[str]]:
	"""Карта «сервер → scope». Отсутствие ключа означает `global`."""
	return scopes.load_map(path)


def save_mcp_scopes(path: Path, scope_map: dict[str, list[str]]) -> None:
	"""Записать карту. `global` не храним: это и есть значение по умолчанию."""
	scopes.save_map(path, scope_map)


def mcp_scope_for(scope_map: dict[str, list[str]], name: str) -> list[str]:
	return scopes.entry_for(scope_map, name)


MCP_BASE_NAME = "ccsync-mcp-base.json"


def _mcp_template_view(servers: dict, mapper: PathMapper) -> tuple[dict, list[str]]:
	"""Серверы в том виде, в каком они лежат в шаблоне: пути — токены, секреты — метки.

	В этом же виде хранится и снимок: сравнивать здешнее с хранилищем можно
	только в одном представлении, а значения секретов на диск класть незачем.
	"""
	view: dict[str, dict] = {}
	secret_keys: list[str] = []
	for name, definition in servers.items():
		entry = convert_json_strings(definition, mapper.tokenize)
		env = entry.get("env") if isinstance(entry, dict) else None
		if isinstance(env, dict):
			for key in list(env):
				if is_secret_key(key):
					env[key] = SECRET_TEMPLATE % key
					secret_keys.append(key)
		view[name] = entry
	return view, secret_keys


def _mcp_filter(servers: dict, scope_map: dict[str, list[str]], machine: Machine,
				*, here: bool) -> dict:
	return {name: definition for name, definition in servers.items()
			if scopes.matches(mcp_scope_for(scope_map, name), machine) == here}


def export_mcp(
	config_dir: Path,
	template_path: Path,
	mapper: PathMapper,
	machine: Machine,
	scopes_path: Path,
	*,
	dry_run: bool = False,
) -> tuple[list[str], list[str]]:
	"""mcpServers → шаблон. Пути токенизируются, секреты маскируются.

	Возвращает (замаскированные секреты, не отданные серверы).

	В шаблон уходит только изменённое здесь — так же, как с settings.json:
	трёхсторонний merge снимка (на чём машина в прошлый раз сошлась с
	хранилищем), здешних серверов и шаблона. Сервера нет ни здесь, ни в снимке
	— он новый с другой машины и остаётся. Был в снимке и пропал здесь — его
	удалили здесь, и он уходит из шаблона.

	Здешние серверы — источник истины только для применимых на этой машине.
	Сервер с чужим scope здесь отсутствует по определению и переносится из
	хранилища как есть.

	Снимка нет — неизвестно, что меняли здесь: новые здешние серверы
	добавляются, а существующие в хранилище не трогаются.

	dry_run — снимок не обновляется.
	"""
	scope_map = load_mcp_scopes(scopes_path)
	servers = read_global_config(config_dir).get("mcpServers") or {}
	local, secret_keys = _mcp_template_view(
		_mcp_filter(servers, scope_map, machine, here=True), mapper)
	previous = read_vault_json(template_path, expect=dict)
	base_path = config_dir / MCP_BASE_NAME
	base = _read_json(base_path, None)
	stale: list[str] = []
	if isinstance(previous, dict):
		incoming = _mcp_filter(previous, scope_map, machine, here=True)
		foreign = _mcp_filter(previous, scope_map, machine, here=False)
		if isinstance(base, dict):
			merged = merge_json(_mcp_filter(base, scope_map, machine, here=True),
								local, incoming)
		else:
			merged = dict(incoming)
			for name, definition in local.items():
				if name not in incoming:
					merged[name] = definition
				elif definition != incoming[name]:
					stale.append(f"mcp:{name}")
		template = {**foreign, **merged}
	else:
		template = local

	_write_json(template_path, template)
	# Снимок — здешние серверы: на них машина теперь сошлась с хранилищем (те,
	# что отстают, здесь не правили, и для них он и так совпадал со здешним).
	# Без снимка его не заводим: отданное не всё, и заводит его pull.
	if not dry_run and (isinstance(base, dict) or not isinstance(previous, dict)):
		_write_json(base_path, local)
	return sorted(set(secret_keys)), stale


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

	Применимый сервер, поправленный или удалённый здесь, тоже не трогаем — см.
	_changed_here: иначе pull молча откатывал бы здешнюю правку, которую
	следующий push как раз собирался отдать.
	"""
	report = ToolsReport()
	if not template_path.exists():
		return report
	wanted = read_vault_json(template_path, expect=dict)
	scope_map = load_mcp_scopes(scopes_path)
	existing = read_global_config(config_dir).get("mcpServers") or {}
	local_view, _ = _mcp_template_view(existing, mapper)
	base = _read_json(config_dir / MCP_BASE_NAME, None)
	for name, definition in wanted.items():
		expanded = convert_json_strings(definition, mapper.detokenize)
		rendered_text, missing = _fill_secrets(
			json.dumps(expanded, ensure_ascii=False), secrets)
		rendered = json.loads(rendered_text)
		if not scopes.matches(mcp_scope_for(scope_map, name), machine):
			_remove_foreign_server(name, rendered, existing, report, dry_run=dry_run)
			continue
		change = _changed_here(name, definition, local_view, base)
		if change == "edited":
			report.kept_local.append(name)
			continue
		if change == "deleted":
			report.deleted_here.append(name)
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
	if not dry_run:
		_remember_mcp_agreement(wanted, config_dir, mapper, machine, scope_map)
	return report


def _changed_here(name: str, incoming: dict, local_view: dict, base) -> str | None:
	"""Менялся ли сервер здесь со времени последнего схождения с хранилищем.

	"edited" — поправлен здесь (правили и там — всё равно побеждает здешнее,
	как с settings.json); "deleted" — удалён здесь, а в хранилище не менялся.
	None — ставим версию из хранилища: здесь сервер не трогали, или удалили, но
	в хранилище его с тех пор обновили. Снимка нет — тоже None: что правили
	здесь, неизвестно, и pull ведёт себя как раньше.

	Сравнение — в виде шаблона: секреты там метки, так что правка одного лишь
	значения ключа правкой не считается — ключи приезжают из ccsync-secrets.env.
	"""
	if not isinstance(base, dict) or name not in base:
		return None
	known = base[name]
	if name in local_view:
		here = local_view[name]
		return "edited" if here != known and here != incoming else None
	return "deleted" if incoming == known else None


def _remember_mcp_agreement(wanted: dict, config_dir: Path, mapper: PathMapper,
							machine: Machine, scope_map: dict[str, list[str]]) -> None:
	"""Снимок после pull — см. snapshot_after_pull.

	Здешние серверы целиком снимком быть не могут: добавленный здесь и ещё не
	отданный попал бы в снимок, и следующий push счёл бы его удалённым в
	хранилище. Шаблон целиком — тоже: сервер, который не встал (нет claude,
	ошибка), push принял бы за удалённый здесь, а не вставшее обновление —
	за правку с обеих сторон, и вернул бы старую версию.
	"""
	servers = read_global_config(config_dir).get("mcpServers") or {}
	local, _ = _mcp_template_view(_mcp_filter(servers, scope_map, machine, here=True), mapper)
	base_path = config_dir / MCP_BASE_NAME
	old = _read_json(base_path, {})
	snapshot = snapshot_after_pull(
		local, _mcp_filter(wanted, scope_map, machine, here=True),
		old if isinstance(old, dict) else {})
	_write_json(base_path, snapshot if isinstance(snapshot, dict) else {})


_MISSING = object()


def snapshot_after_pull(local, incoming, old):
	"""Точка схождения здешнего с хранилищем после pull.

	Где здешнее совпало с хранилищем — это общее значение. Где не совпало, pull
	их не свёл (правка здесь, которую pull оставил, или то, что не встало), и
	точка схождения остаётся прежней. Идём по словарям вглубь, чтобы разошедшийся
	элемент не тянул за собой в «прежнее» сошедшихся соседей.

	Прежней точки нет (первый pull со снимками), а значения разошлись — что
	здесь правили, неизвестно, и здешнее считаем нетронутым: тогда push не
	отдаст его поверх хранилища. Та же осторожность, что и без снимка вовсе.
	Что есть только здесь (ещё не отдано) или только там (не встало), в снимок
	не попадает: иначе push счёл бы первое удалённым в хранилище, второе —
	удалённым здесь.
	"""
	if local == incoming:
		return local
	if isinstance(local, dict) and isinstance(incoming, dict):
		previous = old if isinstance(old, dict) else {}
		result = {}
		for key in {*local, *incoming, *previous}:
			value = snapshot_after_pull(local.get(key, _MISSING),
										incoming.get(key, _MISSING),
										previous.get(key, _MISSING))
			if value is not _MISSING:
				result[key] = value
		return result
	if old is not _MISSING:
		return old
	if local is not _MISSING and incoming is not _MISSING:
		return local
	return _MISSING


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

PLUGINS_BASE_NAME = "ccsync-plugins-base.json"


def _plugins_view(config_dir: Path) -> dict:
	"""Справочная часть манифеста — как её видит эта машина."""
	plugins_dir = config_dir / "plugins"
	return {
		"installed_plugins": _read_json(plugins_dir / "installed_plugins.json", {}),
		"known_marketplaces": _read_json(plugins_dir / "known_marketplaces.json", {}),
	}


def export_plugins(config_dir: Path, target_path: Path, settings_template: Path,
				   *, dry_run: bool = False) -> bool:
	"""Список плагинов и маркетплейсов — без самих клонов (19 МБ восстановимы).

	enabled и extra_marketplaces — это ключи settings.json, поэтому берутся из
	шаблона настроек, уже слитого трёхсторонне: из здешнего файла в хранилище
	вернулась бы устаревшая копия, и плагин, включённый на другой машине, здесь
	бы «выключился». Справочные installed_plugins и known_marketplaces сливаются
	так же, по снимку. Снимка нет — новые записи добавляются, существующие в
	хранилище не трогаются.

	dry_run — снимок не обновляется.
	"""
	local = _plugins_view(config_dir)
	previous = read_vault_json(target_path, expect=dict)
	base_path = config_dir / PLUGINS_BASE_NAME
	base = _read_json(base_path, None)
	if isinstance(previous, dict):
		incoming = {key: previous.get(key, {}) for key in local}
		if isinstance(base, dict):
			reference = merge_json(base, local, incoming)
		else:
			reference = _union_missing(incoming, local)
	else:
		reference = local
	# Битый шаблон настроек — не «плагинов не включено»: иначе список включённых
	# в хранилище опустел бы.
	settings = read_vault_json(settings_template, expect=dict) or {}
	payload = {
		**reference,
		"enabled": settings.get("enabledPlugins", {}),
		"extra_marketplaces": settings.get("extraKnownMarketplaces", {}),
	}
	_write_json(target_path, payload)
	# Снимок — здешнее: на нём машина теперь сошлась с хранилищем. Без снимка
	# его не заводим — отдано не всё; заводит его pull.
	if not dry_run and (isinstance(base, dict) or not isinstance(previous, dict)):
		_write_json(base_path, local)
	return True


def remember_plugins_agreement(config_dir: Path, plugins_path: Path) -> None:
	"""Снимок после pull — см. snapshot_after_pull.

	Плагины pull не ставит, только подсказывает, так что здесь просто
	фиксируется, на чём машина с хранилищем уже совпадает.
	"""
	manifest = _read_json(plugins_path, None)
	if not isinstance(manifest, dict):
		return
	local = _plugins_view(config_dir)
	base_path = config_dir / PLUGINS_BASE_NAME
	old = _read_json(base_path, {})
	snapshot = snapshot_after_pull(
		local, {key: manifest.get(key, {}) for key in local},
		old if isinstance(old, dict) else {})
	_write_json(base_path, snapshot if isinstance(snapshot, dict) else {})


def _union_missing(incoming, local):
	"""Добавить к incoming то, чего в нём нет, из local, ничего не заменяя."""
	if not (isinstance(incoming, dict) and isinstance(local, dict)):
		return incoming
	result = dict(incoming)
	for key, value in local.items():
		result[key] = _union_missing(incoming[key], value) if key in incoming else value
	return result


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


def _write_json(path: Path, data) -> None:
	_write_text_atomic(path, json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n")


def _write_text_atomic(path: Path, text: str) -> None:
	"""Записать через временный файл: оборванная запись не оставит огрызка.

	Огрызок шаблона в хранилище опасен вдвойне: следующий push с любой машины
	увидел бы вместо общего шаблона мусор.
	"""
	path.parent.mkdir(parents=True, exist_ok=True)
	temporary = path.with_name(path.name + ".tmp")
	temporary.write_text(text, encoding="utf-8")
	os.replace(temporary, path)


class DamagedFile(ValueError):
	"""Файл в хранилище есть, но не разбирается.

	Принять его за «файла нет» нельзя: выгрузка решила бы, что она первая, и
	записала бы поверх здешнее целиком — ровно та потеря, от которой защищают
	снимки.
	"""

	def __init__(self, path: Path, reason: str) -> None:
		super().__init__(f"{path}: {reason}")
		self.path = path


def read_vault_json(path: Path, *, expect: type):
	"""JSON из хранилища: None — файла нет; битый или не того вида — DamagedFile."""
	if not path.exists():
		return None
	try:
		data = json.loads(path.read_text(encoding="utf-8"))
	except (OSError, UnicodeDecodeError, ValueError) as error:
		raise DamagedFile(path, str(error)) from error
	if not isinstance(data, expect):
		raise DamagedFile(path, f"ожидался {expect.__name__}, а там {type(data).__name__}")
	return data


def _read_json(path: Path, default):
	if not path.exists():
		return default
	try:
		return json.loads(path.read_text(encoding="utf-8"))
	except (json.JSONDecodeError, ValueError):
		return default
