"""Перенос транскриптов сессий между машинами.

Транскрипт — это JSONL, где каждая строка самостоятельна, а в записях зашит
абсолютный `cwd` машины-источника. Переносим построчно: разбираем JSON,
переписываем `cwd` и токенизируем пути в тексте.

Проверено на живой машине: отдельного индекса сессий у Claude Code нет,
источник истины — сами .jsonl, поэтому файл, положенный в правильную папку
`~/.claude/projects/<слаг>/`, виден `--resume` и поднимает контекст.
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import time
import uuid as uuid_module
from dataclasses import dataclass, field
from pathlib import Path

from . import slim
from .paths import PathMapper, slug_for
from .redact import Redactor

# GitHub отклоняет файлы больше 100 МБ. Порог — для копии после сжатия
# (см. slim.py): 10 МБ запаса до жёсткого предела.
DEFAULT_MAX_BYTES = 90 * 1024 * 1024

# Исходник больше этого даже не читаем: разбор такого файла в память сам по
# себе опасен, а влезть в порог после сжатия у него шансов нет.
HARD_LIMIT_BYTES = 2 * 1024 * 1024 * 1024

# Поля записи, в которых лежит именно путь, а не свободный текст.
PATH_FIELDS = ("cwd",)

# Временные файлы атомарной записи. В .gitignore хранилища — на случай, если
# процесс убили раньше, чем он успел убрать за собой.
TEMP_SUFFIX = ".ccsync-tmp"


@dataclass
class TransferReport:
	moved: list[str]
	skipped: list[tuple[str, str]]  # (файл, причина)
	merged: list[str] = field(default_factory=list)       # слиты две разошедшиеся версии
	kept_local: list[str] = field(default_factory=list)   # локальная полнее — не тронута
	sizes: dict[str, tuple[int, int]] = field(default_factory=dict)  # имя → (было, стало)

	def summary(self) -> str:
		parts = []
		if self.moved:
			parts.append(f"перенесено: {len(self.moved)}")
		if self.skipped:
			parts.append(f"пропущено: {len(self.skipped)}")
		return ", ".join(parts) or "изменений нет"


def projects_root(config_dir: Path) -> Path:
	return config_dir / "projects"


def local_session_dir(config_dir: Path, project_path: str) -> Path:
	return projects_root(config_dir) / slug_for(project_path)


def newest_transcript(directory: Path) -> Path | None:
	"""Самый свежий транскрипт в папке проекта (подпапки субагентов не в счёт)."""
	if not directory.is_dir():
		return None
	files = [f for f in directory.glob("*.jsonl") if f.is_file()]
	if not files:
		return None
	return max(files, key=lambda f: f.stat().st_mtime)


def origin_path(transcript: Path, max_lines: int = 40) -> str | None:
	"""Каталог, в котором сессия была ЗАПУЩЕНА.

	Claude Code держит транскрипт в папке каталога запуска и не переносит его,
	даже если рабочий каталог сессии потом сменился (а он меняется, стоит Клоду
	перейти в другой проект). Поэтому ключ проекта надо брать отсюда, а не из
	cwd процесса: иначе один и тот же диалог уезжает в хранилище дважды — под
	старым ключом застывший огрызок, под новым продолжение.

	Путь берётся из первой записи с `cwd` и сверяется с именем папки: слаг
	необратим, но проверить соответствие можно. Не сошлось — значит запись не о
	том, и лучше вернуть None, чем угадывать.
	"""
	try:
		with transcript.open(encoding="utf-8", errors="replace") as fh:
			for index, line in enumerate(fh):
				if index >= max_lines:
					break
				if '"cwd"' not in line:
					continue
				try:
					record = json.loads(line)
				except json.JSONDecodeError:
					continue
				candidate = record.get("cwd")
				if isinstance(candidate, str) and candidate:
					return candidate if slug_for(candidate) == transcript.parent.name else None
	except OSError:
		return None
	return None


def transform_transcript(
	source: Path,
	destination: Path,
	mapper: PathMapper,
	*,
	mode: str,
	cwd_override: str | None = None,
	redactor: Redactor | None = None,
) -> int:
	"""Переписать транскрипт построчно.

	mode="tokenize"   — локальные пути → токены (выгрузка в репо)
	mode="detokenize" — токены → локальные пути (загрузка на машину)

	`redactor` вырезает секреты и имеет смысл только при выгрузке: обратно
	секрет не восстанавливается, и это намеренно.
	"""
	convert = mapper.tokenize if mode == "tokenize" else mapper.detokenize
	if redactor is not None and mode == "tokenize":
		path_convert = convert

		def convert(value: str) -> str:  # noqa: F811 — сознательная обёртка
			return redactor.mask(path_convert(value))
	destination.parent.mkdir(parents=True, exist_ok=True)
	written = 0
	with source.open(encoding="utf-8", errors="replace") as src, \
			destination.open("w", encoding="utf-8") as dst:
		for line in src:
			line = line.rstrip("\n")
			if not line.strip():
				continue
			dst.write(_convert_line(line, convert, cwd_override) + "\n")
			written += 1
	return written


def _convert_line(line: str, convert, cwd_override: str | None) -> str:
	try:
		record = json.loads(line)
	except json.JSONDecodeError:
		# Битую строку не теряем, но и не трогаем — пусть доедет как есть.
		return line
	record = _convert_node(record, convert)
	if cwd_override is not None and isinstance(record, dict):
		for field in PATH_FIELDS:
			if field in record:
				record[field] = cwd_override
	return json.dumps(record, ensure_ascii=False)


def _convert_node(node, convert):
	"""Рекурсивно применить преобразование ко всем строкам записи."""
	if isinstance(node, str):
		return convert(node)
	if isinstance(node, list):
		return [_convert_node(item, convert) for item in node]
	if isinstance(node, dict):
		return {key: _convert_node(value, convert) for key, value in node.items()}
	return node


def record_uuids(transcript: Path) -> set[str]:
	"""Идентификаторы записей транскрипта — чтобы сравнивать копии по существу."""
	found: set[str] = set()
	try:
		with transcript.open(encoding="utf-8", errors="replace") as fh:
			for line in fh:
				if '"uuid"' not in line:
					continue
				try:
					record = json.loads(line)
				except json.JSONDecodeError:
					continue
				value = record.get("uuid")
				if isinstance(value, str) and value:
					found.add(value)
	except OSError:
		return set()
	return found


@dataclass
class RecordInfo:
	"""Кусочек записи, которого хватает человеку, чтобы узнать свою ветку."""

	timestamp: str | None
	preview: str


@dataclass
class Branch:
	"""Одна линия разговора: от корня до хвоста."""

	tip: str
	uuids: set[str]
	last_timestamp: str | None
	preview: str

	@property
	def size(self) -> int:
		return len(self.uuids)


@dataclass
class Chain:
	"""Как записи транскрипта связаны между собой.

	Claude Code собирает разговор обратным обходом `parentUuid` от последней
	записи, а не порядком строк в файле. Поэтому «файл на месте и все записи
	целы» и «сессия восстановится тем, чем была» — разные утверждения, и
	второе надо проверять отдельно.
	"""

	order: list[str]                 # uuid в порядке появления в файле
	parents: dict[str, str | None]   # uuid → parentUuid
	children: dict[str, list[str]]   # parentUuid → его дети
	meta: dict[str, "RecordInfo"]    # uuid → время и начало реплики

	@property
	def tips(self) -> list[str]:
		"""Записи без продолжения — хвосты веток."""
		return [uuid for uuid in self.order if uuid not in self.children]

	@property
	def reachable(self) -> set[str]:
		"""Записи, которые попадут в контекст при восстановлении."""
		if not self.order:
			return set()
		seen: set[str] = set()
		current: str | None = self.order[-1]
		while current and current not in seen:
			seen.add(current)
			current = self.parents.get(current)
		return seen

	@property
	def forks(self) -> list[str]:
		"""Записи, у которых больше одного продолжения.

		В нормальном транскрипте таких нет вовсе: линия одна, а сегменты после
		`/compact` живут отдельными цепочками, а не ветвлением. Ветвление
		появляется, когда две машины продолжили одну сессию врозь и git склеил
		их файлы объединением строк (`merge=union` в .gitattributes): записи
		целы, но в контекст приедет только одна из веток.
		"""
		return [parent for parent, kids in self.children.items() if len(kids) > 1]


def read_chain(transcript: Path) -> Chain:
	"""Разобрать связи записей транскрипта."""
	order: list[str] = []
	parents: dict[str, str | None] = {}
	children: dict[str, list[str]] = {}
	meta: dict[str, RecordInfo] = {}
	try:
		with transcript.open(encoding="utf-8", errors="replace") as fh:
			for line in fh:
				if '"uuid"' not in line:
					continue
				try:
					record = json.loads(line)
				except json.JSONDecodeError:
					continue
				uuid = record.get("uuid")
				if not isinstance(uuid, str) or not uuid:
					continue
				parent = record.get("parentUuid")
				parent = parent if isinstance(parent, str) and parent else None
				order.append(uuid)
				parents[uuid] = parent
				meta[uuid] = RecordInfo(timestamp=record.get("timestamp"),
										preview=_preview(record))
				if parent:
					children.setdefault(parent, []).append(uuid)
	except OSError:
		pass
	return Chain(order=order, parents=parents, children=children, meta=meta)


def _preview(record: dict, limit: int = 90) -> str:
	"""Начало реплики — чтобы человек узнал свою ветку по первым словам."""
	message = record.get("message")
	content = message.get("content") if isinstance(message, dict) else None
	if isinstance(content, list):
		parts = [b.get("text", "") for b in content if isinstance(b, dict)]
		content = " ".join(p for p in parts if p)
	if not isinstance(content, str):
		return ""
	text = " ".join(content.split())
	return text[:limit] + ("…" if len(text) > limit else "")


def walk_back(chain: Chain, start: str) -> set[str]:
	"""Записи, достижимые от указанной обратным обходом parentUuid."""
	seen: set[str] = set()
	current: str | None = start
	while current and current not in seen:
		seen.add(current)
		current = chain.parents.get(current)
	return seen


def branches(chain: Chain) -> list[Branch]:
	"""Ветки разговора — по одной на хвост.

	Ветки делят общий префикс: до точки расхождения записи у них одни и те же.
	Поэтому в набор ветки входит и общая часть — так каждая ветка остаётся
	самодостаточной сессией, которую можно открыть отдельно.
	"""
	found: list[Branch] = []
	for tip in chain.tips:
		uuids = walk_back(chain, tip)
		info = chain.meta.get(tip)
		# Для превью берём первую запись, которой нет в других ветках, — общее
		# начало у всех одинаковое и ветку по нему не отличить.
		found.append(Branch(tip=tip, uuids=uuids,
							last_timestamp=info.timestamp if info else None,
							preview=info.preview if info else ""))
	for branch in found:
		unique = branch.uuids - set().union(*(b.uuids for b in found if b is not branch)) \
			if len(found) > 1 else branch.uuids
		for uuid in chain.order:
			if uuid in unique and chain.meta.get(uuid) and chain.meta[uuid].preview:
				branch.preview = chain.meta[uuid].preview
				break
	return found


def chain_from_text(text: str) -> Chain:
	"""Разобрать связи записей из уже прочитанного текста транскрипта."""
	order: list[str] = []
	parents: dict[str, str | None] = {}
	children: dict[str, list[str]] = {}
	meta: dict[str, RecordInfo] = {}
	for line in text.splitlines():
		if '"uuid"' not in line:
			continue
		try:
			record = json.loads(line)
		except json.JSONDecodeError:
			continue
		uuid = record.get("uuid")
		if not isinstance(uuid, str) or not uuid:
			continue
		parent = record.get("parentUuid")
		parent = parent if isinstance(parent, str) and parent else None
		order.append(uuid)
		parents[uuid] = parent
		meta[uuid] = RecordInfo(timestamp=record.get("timestamp"), preview=_preview(record))
		if parent:
			children.setdefault(parent, []).append(uuid)
	return Chain(order=order, parents=parents, children=children, meta=meta)


# Порог, ниже которого о потерянных записях не говорим. Структурно «человек
# вернулся назад и переписал ход» и «git наложил чужую ветку» неотличимы: в обоих
# случаях прежний хвост перестаёт читаться. Различает их только масштаб —
# переписанный ход это одна-две записи, а чужая ветка это кусок работы.
MIN_LOST_TO_WARN = 6


def lost_after_merge(before: str, after: str) -> int:
	"""Сколько записей было видно до слияния и перестало быть видно после.

	Единственный надёжный признак того, что склейка навредила. Просто наличие
	недостижимых веток ни о чём не говорит: в любом долгом транскрипте их полно
	— это брошенные продолжения, к которым человек сам не вернулся. Важно
	другое: была ветка читаемой до слияния и перестала после.
	"""
	old = chain_from_text(before)
	new = chain_from_text(after)
	if not old.order:
		return 0
	return len(old.reachable - new.reachable)


@dataclass
class SplitResult:
	"""Вынесенная ветка: куда легла и что в ней."""

	path: Path
	session_id: str
	records: int
	preview: str


def split_transcript(transcript: Path, backup_dir: Path | None = None) -> list[SplitResult]:
	"""Разнести разошедшиеся ветки сессии по отдельным файлам.

	Восстановление идёт от последней строки файла обратным обходом parentUuid
	(проверено запуском `claude --resume`: побеждает именно порядок строк, а не
	время записи). Поэтому из склеенной сессии читается ровно одна ветка, и
	выбрать другую, не трогая данные, нельзя.

	Выход — сделать каждую ветку самостоятельной сессией: у неё будет ровно один
	хвост, и обе трактовки «последней записи» дадут одно и то же. Главная ветка
	(та, что читается сейчас) остаётся в исходном файле, остальные уезжают в
	новые файлы со своим `sessionId` — иначе сессия спорит сама с собой.

	Служебные записи без `uuid` в новые файлы не копируются: проверено, что
	сессия из одних только записей с `uuid` открывается и держит контекст.

	Возвращает список вынесенных веток; пустой, если расхождения нет.
	"""
	chain = read_chain(transcript)
	found = branches(chain)
	if len(found) < 2 or not chain.order:
		return []
	last = chain.order[-1]
	main = next((b for b in found if last in b.uuids), found[-1])
	others = [b for b in found if b is not main]
	if not others:
		return []

	lines = transcript.read_text(encoding="utf-8", errors="replace").splitlines()
	parsed: list[tuple[str, dict | None]] = []
	for line in lines:
		if not line.strip():
			continue
		try:
			parsed.append((line, json.loads(line)))
		except json.JSONDecodeError:
			parsed.append((line, None))

	if backup_dir is not None:
		backup_dir.mkdir(parents=True, exist_ok=True)
		(backup_dir / transcript.name).write_text(
			"\n".join(line for line, _ in parsed) + "\n", encoding="utf-8")

	results: list[SplitResult] = []
	for branch in others:
		new_id = str(uuid_module.uuid4())
		out: list[str] = []
		for _, record in parsed:
			if not record:
				continue
			uuid = record.get("uuid")
			if not isinstance(uuid, str) or uuid not in branch.uuids:
				continue
			moved = dict(record)
			moved["sessionId"] = new_id
			if "session_id" in moved:
				moved["session_id"] = new_id
			out.append(json.dumps(moved, ensure_ascii=False))
		if not out:
			continue
		target = transcript.with_name(f"{new_id}.jsonl")
		target.write_text("\n".join(out) + "\n", encoding="utf-8")
		results.append(SplitResult(path=target, session_id=new_id,
								   records=len(out), preview=branch.preview))

	# Из исходного убираем записи, оставшиеся только в вынесенных ветках.
	kept: list[str] = []
	for line, record in parsed:
		uuid = record.get("uuid") if record else None
		if isinstance(uuid, str) and uuid not in main.uuids:
			continue
		kept.append(line)
	transcript.write_text("\n".join(kept) + "\n", encoding="utf-8")
	return results


def drop_stale_copies(projects_root: Path, session_id: str, keep: Path,
					  *, skip_session_id: str | None = None,
					  dry_run: bool = False) -> tuple[list[Path], list[Path]]:
	"""Убрать копии сессии, оставшиеся в других каталогах проектов.

	Такие копии появляются, когда проект меняет место: пока он не привязан,
	сессия раскладывается в ~/claude-sessions/<ключ>, а после привязки — уже по
	настоящему пути. Старая раскладка при этом остаётся, и одна и та же сессия
	начинает двоиться в /resume, причём устаревшая копия обрывается на середине.

	Удаляем только то, что заведомо ничего не теряет. Проверок две, и вторая
	неочевидна: мало чтобы все записи копии нашлись в оставляемой — надо ещё,
	чтобы читаемая часть копии осталась читаемой. Если оставляемая склеена из
	двух разошедшихся веток, цепочка в ней уводит в чужую ветку, и записи,
	которые в копии попадали в контекст, в ней окажутся недостижимы. Формально
	ничего не потеряно, фактически человек откроет не тот разговор.

	Файл текущей сессии не трогаем никогда — Claude Code пишет в него прямо
	сейчас.

	Возвращает (удалённые, оставленные-из-осторожности). В dry_run ничего не
	удаляется — «удалённые» означает «были бы удалены».
	"""
	if not projects_root.is_dir():
		return [], []
	others = [
		p for p in projects_root.glob(f"*/{session_id}.jsonl")
		if p.is_file() and p != keep
	]
	if not others:
		return [], []
	kept_chain = read_chain(keep)
	kept_uuids = set(kept_chain.parents)
	kept_reachable = kept_chain.reachable
	removed: list[Path] = []
	spared: list[Path] = []
	for copy in others:
		if skip_session_id and session_id == skip_session_id:
			spared.append(copy)
			continue
		copy_chain = read_chain(copy)
		if set(copy_chain.parents) - kept_uuids:
			# В копии есть то, чего нет в оставляемой, — не наше дело решать.
			spared.append(copy)
			continue
		if copy_chain.reachable - kept_reachable:
			# Записи на месте, но в оставляемой они выпали из цепочки: её
			# восстановление даст другой разговор. Тоже не наше дело решать.
			spared.append(copy)
			continue
		if dry_run:
			removed.append(copy)
			continue
		try:
			copy.unlink()
			removed.append(copy)
		except OSError:
			spared.append(copy)
	return removed, spared


# Служебные обёртки, которые Claude Code пишет как реплики пользователя:
# сообщением человека ни одна из них не является.
SERVICE_PREFIXES = (
	"<local-command",
	"<command-name>",
	"<system-reminder",
	"Caveat:",
)

# Страховка от ошибки разбора: разговор такого размера пустым не бывает,
# и тратить время на его перебор незачем.
NEVER_EMPTY_ABOVE_BYTES = 512 * 1024


def is_empty_transcript(transcript: Path) -> bool:
	"""Ни одной реплики человека и ни одного ответа модели.

	Такие файлы заводятся сами: заглушка `bridge-session` от связи с claude.ai
	и сессия, где успели нажать только слэш-команду вроде /model. Содержимого
	в них нет по определению, а в хранилище они занимают место и требуют потом
	отдельного `forget`, чтобы уйти оттуда.
	"""
	try:
		if transcript.stat().st_size > NEVER_EMPTY_ABOVE_BYTES:
			return False
		with transcript.open(encoding="utf-8", errors="replace") as handle:
			for line in handle:
				line = line.strip()
				if not line:
					continue
				try:
					record = json.loads(line)
				except ValueError:
					# Битую строку считаем содержимым: лучше отправить лишнее,
					# чем потерять разговор из-за сбоя разбора.
					return False
				kind = record.get("type")
				if kind == "assistant":
					return False
				if kind != "user" or record.get("isMeta") or record.get("isCompactSummary"):
					continue
				text = _preview(record, limit=200)
				if text and not text.startswith(SERVICE_PREFIXES):
					return False
	except OSError:
		return False
	return True


# --- слияние двух версий одного транскрипта ---------------------------------

@dataclass
class MergeResult:
	"""Чем две версии сессии отличаются и что из этого получилось.

	kind:
	  "same"        — записи одни и те же;
	  "first-has-all"  — в первой есть всё, что во второй (и, может, больше);
	  "second-has-all" — наоборот;
	  "merged"      — у каждой есть свои записи, текст собран из обеих.
	"""

	kind: str
	lines: list[str]
	first_fresher: bool = False   # у первой версии есть своя ветка новее всего во второй

	@property
	def text(self) -> str:
		return "".join(line + "\n" for line in self.lines)


def _line_uuid(line: str) -> str | None:
	if '"uuid"' not in line:
		return None
	try:
		record = json.loads(line)
	except json.JSONDecodeError:
		return None
	value = record.get("uuid") if isinstance(record, dict) else None
	return value if isinstance(value, str) and value else None


def _line_time(line: str) -> str:
	try:
		record = json.loads(line)
	except json.JSONDecodeError:
		return ""
	value = record.get("timestamp") if isinstance(record, dict) else None
	return value if isinstance(value, str) else ""


def _canonical(line: str) -> str:
	"""Запись без uuid в виде, не зависящем от форматирования.

	Строки, прошедшие через хранилище, сериализованы заново (с пробелами после
	запятых), а локальные — компактно; сравнивать их как строки нельзя, иначе
	каждое слияние дописывает такие записи дублями.
	"""
	try:
		return json.dumps(json.loads(line), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
	except ValueError:
		return line


def _index(lines: list[str]) -> tuple[list[tuple[str, str | None]], dict[str, str]]:
	pairs = [(line, _line_uuid(line)) for line in lines if line.strip()]
	by_uuid = {uuid: line for line, uuid in pairs if uuid}
	return pairs, by_uuid


# Названия сессии. `/resume` берёт их из начала и конца файла, а не из всей
# середины, поэтому после слияния действующее название должно стоять в хвосте.
TITLE_TYPES = ("custom-title", "agent-name", "ai-title")
TITLE_TAIL_LINES = 50


def _titles_to_tail(lines: list[str]) -> list[str]:
	"""Продублировать действующие названия в конец, если они ушли вглубь файла.

	Копия, а не перенос: прежние записи остаются как были («без потерь»), а при
	чтении побеждает последняя. Повторное слияние второй раз ничего не допишет:
	название уже в хвосте.
	"""
	latest: dict[str, tuple[int, str]] = {}
	for position, line in enumerate(lines):
		for kind in TITLE_TYPES:
			if f'"{kind}"' in line[:40]:
				try:
					record = json.loads(line)
				except json.JSONDecodeError:
					break
				if isinstance(record, dict) and record.get("type") == kind:
					latest[kind] = (position, line)
				break
	border = len(lines) - TITLE_TAIL_LINES
	stale = [line for position, line in latest.values() if position < border]
	return lines + stale if stale else lines


def merge_lines(first: list[str], second: list[str]) -> MergeResult:
	result = _merge_lines(first, second)
	if result.kind in ("second-has-all", "merged"):
		result.lines = _titles_to_tail(result.lines)
	return result


def _merge_lines(first: list[str], second: list[str]) -> MergeResult:
	"""Слить две версии одного транскрипта без потерь.

	`first` — версия, которой при совпадении `uuid` отдаётся предпочтение: на
	своей машине это полная локальная копия, а копия из хранилища могла пройти
	сжатие (slim.py) и потерять картинки. Сравнение идёт по составу записей
	(`uuid`), а не по времени изменения файла: mtime файла в git — это время
	checkout, и он ничего не говорит о том, где разговор длиннее. Именно так
	15.10.2026 ноутбучная копия на 13 829 записей заменила локальную на 22 045.

	При расхождении в конец ставится ветка с более свежей репликой: `--resume`
	читает разговор от последней строки файла, и открываться должна последняя
	по времени работа. Вторая ветка остаётся в файле целиком (её покажет
	`ccsync branches`).
	"""
	first_pairs, first_by_uuid = _index(first)
	second_pairs, second_by_uuid = _index(second)
	first_ids, second_ids = set(first_by_uuid), set(second_by_uuid)

	def prefer_first(pairs):
		return [first_by_uuid.get(uuid, line) if uuid else line for line, uuid in pairs]

	if second_ids <= first_ids:
		kind = "same" if first_ids == second_ids else "first-has-all"
		return MergeResult(kind, [line for line, _ in first_pairs])
	if first_ids <= second_ids:
		# Записи без uuid (заголовки, сводки, снимки файлов для /rewind) есть
		# только у первой версии: копия из хранилища прошла сжатие. Терять их
		# нельзя. Ставим в начало, чтобы для «последний побеждает» (заголовок
		# сессии) верх брали более свежие записи второй версии.
		seen_second = {_canonical(line) for line, uuid in second_pairs if not uuid}
		own = [line for line, uuid in first_pairs if not uuid and _canonical(line) not in seen_second]
		return MergeResult("second-has-all", own + prefer_first(second_pairs))

	own_first = max((_line_time(first_by_uuid[u]) for u in first_ids - second_ids), default="")
	own_second = max((_line_time(second_by_uuid[u]) for u in second_ids - first_ids), default="")
	fresher = own_first > max((_line_time(line) for line in second_by_uuid.values()), default="")
	if own_first >= own_second:
		older, newer, older_ids = prefer_first(second_pairs), first_pairs, second_ids
	else:
		older, newer, older_ids = [line for line, _ in first_pairs], second_pairs, first_ids
	present = {_canonical(line) for line in older if not _line_uuid(line)}
	tail = [line for line, uuid in newer
			if (uuid and uuid not in older_ids) or (not uuid and _canonical(line) not in present)]
	return MergeResult("merged", older + tail, fresher)


def _read_lines(path: Path) -> list[str]:
	with path.open(encoding="utf-8", errors="replace") as handle:
		return [line.rstrip("\n") for line in handle if line.strip()]


def _stamp(path: Path) -> tuple[int, int]:
	stat = path.stat()
	return stat.st_size, stat.st_mtime_ns


def write_atomic(path: Path, text: str, *, expect: tuple[int, int] | None = None) -> bool:
	"""Записать через временный файл: прерванный хук не оставит половину файла.

	Имя временного файла уникальное — Stop и SessionEnd могут писать одну
	сессию одновременно, — а при любой ошибке он удаляется: в хранилище
	`git add -A` иначе закоммитил бы огрызок.

	expect — (размер, mtime) цели на момент чтения. Если файл с тех пор
	изменился (в открытую сессию дописали реплику), ничего не пишем и
	возвращаем False: замена выбросила бы дописанное.
	"""
	path.parent.mkdir(parents=True, exist_ok=True)
	handle, name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=TEMP_SUFFIX)
	temporary = Path(name)
	try:
		with os.fdopen(handle, "w", encoding="utf-8") as stream:
			stream.write(text)
		if expect is not None and path.exists() and _stamp(path) != expect:
			return False
		os.replace(temporary, path)
		return True
	finally:
		temporary.unlink(missing_ok=True)


# --- снимок копий в хранилище -----------------------------------------------

class SeenVaultFiles:
	"""Какую версию каждой сессии в хранилище эта машина уже разобрала.

	Хранится размер и mtime файла хранилища на момент разбора. Пока они те же,
	сравнивать заново нечего; `git pull` меняет mtime только у файлов, которые
	действительно изменились. Без этого снимка каждый старт сессии читал бы
	сотни мегабайт транскриптов, а у хука на это 25 секунд.
	"""

	def __init__(self, path: Path) -> None:
		self.path = path
		try:
			data = json.loads(path.read_text(encoding="utf-8"))
		except (OSError, ValueError):
			data = {}
		self.data: dict[str, list[int]] = data if isinstance(data, dict) else {}

	@staticmethod
	def _stamp(vault_file: Path) -> list[int]:
		stat = vault_file.stat()
		return [stat.st_size, stat.st_mtime_ns]

	def unchanged(self, key: str, vault_file: Path) -> bool:
		return self.data.get(key) == self._stamp(vault_file)

	def known(self, key: str) -> bool:
		return key in self.data

	def remember(self, key: str, vault_file: Path) -> None:
		self.data[key] = self._stamp(vault_file)
		self.save()

	def save(self) -> None:
		# Параллельный запуск может затереть чужую строку снимка — это не
		# страшно: следующий pull просто сверит тот файл заново.
		write_atomic(self.path, json.dumps(self.data, ensure_ascii=False, indent=1))


def push_session(
	transcript: Path,
	vault_session_dir: Path,
	mapper: PathMapper,
	*,
	max_bytes: int = DEFAULT_MAX_BYTES,
	redactor: Redactor | None = None,
	seen: SeenVaultFiles | None = None,
	seen_key: str | None = None,
) -> TransferReport:
	"""Выгрузить один транскрипт в репозиторий.

	Копия для хранилища сжимается (slim.py): картинки и служебные записи не
	едут никогда, остальные ступени — пока копия больше порога. Если в
	хранилище уже лежат записи, которых здесь нет (другая машина продолжила
	сессию, а эта ещё не подтянула), они не стираются, а сливаются со здешними.
	"""
	if not transcript.exists():
		return TransferReport([], [(str(transcript), "файл не найден")])
	size = transcript.stat().st_size
	if size > HARD_LIMIT_BYTES:
		reason = f"{size / 1048576:.0f} МБ — слишком большой даже для сжатия, пропущен"
		return TransferReport([], [(transcript.name, reason)], sizes={transcript.name: (size, size)})
	if is_empty_transcript(transcript):
		return TransferReport([], [(transcript.name, "пустая сессия, отправлять нечего")])

	convert = mapper.tokenize
	if redactor is not None:
		def convert(value: str) -> str:  # noqa: F811 — сознательная обёртка
			return redactor.mask(mapper.tokenize(value))
	with transcript.open(encoding="utf-8", errors="replace") as source:
		# Картинки — до трёх четвертей веса транскрипта, а в копию они не едут
		# никогда. Вырезаем их сразу при чтении, чтобы не держать в памяти.
		ours = [_convert_line(slim.strip_images_line(line.rstrip("\n")), convert, None)
				for line in source if line.strip()]

	destination = vault_session_dir / transcript.name
	result = TransferReport([], [])
	complete = True   # в копии хранилища нет ничего, чего нет здесь
	if destination.exists():
		# Сначала слияние, потом сжатие: иначе начало, срезанное по /compact,
		# вернулось бы из прежней копии в хранилище и раздуло её снова.
		merge = merge_lines(ours, _read_lines(destination))
		ours = merge.lines
		complete = merge.kind in ("same", "first-has-all")
		if merge.kind == "merged":
			result.merged.append(transcript.name)
	records, _ = slim.slim_records(slim.parse_lines(ours), target=max_bytes)
	del ours
	text = slim.dump_records(records)
	del records
	final = len(text.encode("utf-8"))
	result.sizes[transcript.name] = (size, final)
	if final > max_bytes:
		reason = (f"{size / 1048576:.0f} МБ, после сжатия {final / 1048576:.0f} МБ — "
				  f"больше порога {max_bytes / 1048576:.0f} МБ, не отправлен")
		result.skipped.append((transcript.name, reason))
		return result
	write_atomic(destination, text)
	if seen is not None and seen_key and complete:
		# Свою же выгрузку при следующем pull разбирать незачем. Но если в неё
		# влились чужие записи, которых здесь нет, pull должен их забрать.
		seen.remember(seen_key, destination)
	result.moved.append(transcript.name)
	return result


def pull_sessions(
	vault_session_dir: Path,
	target_dir: Path,
	mapper: PathMapper,
	local_project_path: str,
	*,
	dry_run: bool = False,
	seen: SeenVaultFiles | None = None,
	seen_prefix: str = "",
	backup_dir: Path | None = None,
) -> TransferReport:
	"""Разложить транскрипты проекта под текущую машину, ничего не теряя.

	Локальный транскрипт заменяется, только если в нём нет ничего, чего нет во
	входящем. Если у каждой версии есть свои записи, они сливаются, а прежний
	локальный файл перед этим копируется в backup_dir.

	dry_run — ничего не пишется, moved перечисляет то, что было бы разложено.
	"""
	report = TransferReport([], [])
	if not vault_session_dir.is_dir():
		return report
	if not dry_run:
		target_dir.mkdir(parents=True, exist_ok=True)
	for transcript in sorted(vault_session_dir.glob("*.jsonl")):
		destination = target_dir / transcript.name
		key = seen_prefix + transcript.name
		if destination.exists():
			if seen is not None and seen.unchanged(key, transcript):
				report.skipped.append((transcript.name, "в хранилище без изменений"))
				continue
			if (seen is None or not seen.known(key)) and \
					destination.stat().st_mtime >= transcript.stat().st_mtime:
				# Первая встреча и локальная копия свежее: прежнее правило, оно
				# безопасно — локальное не трогаем. Дальше сверяемся по снимку.
				report.skipped.append((transcript.name, "локальная копия не старше"))
				if seen is not None and not dry_run:
					seen.remember(key, transcript)
				continue
		if dry_run:
			report.moved.append(transcript.name)
			continue
		incoming = destination.with_name(f".{destination.name}.ccsync-incoming")
		try:
			transform_transcript(transcript, incoming, mapper, mode="detokenize",
								 cwd_override=local_project_path)
			if not destination.exists():
				os.replace(incoming, destination)
				report.moved.append(transcript.name)
			else:
				stamp = _stamp(destination)
				merge = merge_lines(_read_lines(destination), _read_lines(incoming))
				if merge.kind in ("same", "first-has-all"):
					report.kept_local.append(transcript.name)
				else:
					if merge.kind == "merged" and backup_dir is not None:
						# Ветки разошлись. Слияние ничего не выбрасывает, но копию
						# прежнего файла на всякий случай оставляем.
						backup_dir.mkdir(parents=True, exist_ok=True)
						shutil.copy2(destination, backup_dir / destination.name)
					if not write_atomic(destination, merge.text, expect=stamp):
						# Пока сливали, в сессию дописали — она открыта. Снимок не
						# запоминаем: следующий pull попробует снова.
						report.skipped.append((transcript.name, "сессия изменилась во время слияния — позже"))
						continue
					report.moved.append(transcript.name)
					if merge.kind == "merged":
						report.merged.append(transcript.name)
		except OSError as error:
			# В Windows файл открытой сессии бывает заблокирован. Одна сессия не
			# должна останавливать разбор остальных.
			report.skipped.append((transcript.name, f"не записан: {error}"))
			continue
		finally:
			incoming.unlink(missing_ok=True)
		if seen is not None:
			seen.remember(key, transcript)
	return report
