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
from dataclasses import dataclass
from pathlib import Path

from .paths import PathMapper, slug_for

# GitHub отклоняет файлы больше 100 МБ. Держим запас и не тащим гиганты.
DEFAULT_MAX_BYTES = 50 * 1024 * 1024

# Поля записи, в которых лежит именно путь, а не свободный текст.
PATH_FIELDS = ("cwd",)


@dataclass
class TransferReport:
	moved: list[str]
	skipped: list[tuple[str, str]]  # (файл, причина)

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
) -> int:
	"""Переписать транскрипт построчно.

	mode="tokenize"   — локальные пути → токены (выгрузка в репо)
	mode="detokenize" — токены → локальные пути (загрузка на машину)
	"""
	convert = mapper.tokenize if mode == "tokenize" else mapper.detokenize
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
				if parent:
					children.setdefault(parent, []).append(uuid)
	except OSError:
		pass
	return Chain(order=order, parents=parents, children=children)


def drop_stale_copies(projects_root: Path, session_id: str, keep: Path,
					  *, skip_session_id: str | None = None) -> tuple[list[Path], list[Path]]:
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

	Возвращает (удалённые, оставленные-из-осторожности).
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
		try:
			copy.unlink()
			removed.append(copy)
		except OSError:
			spared.append(copy)
	return removed, spared


def push_session(
	transcript: Path,
	vault_session_dir: Path,
	mapper: PathMapper,
	*,
	max_bytes: int = DEFAULT_MAX_BYTES,
) -> TransferReport:
	"""Выгрузить один транскрипт в репозиторий."""
	if not transcript.exists():
		return TransferReport([], [(str(transcript), "файл не найден")])
	size = transcript.stat().st_size
	if size > max_bytes:
		reason = f"{size / 1048576:.0f} МБ — больше порога {max_bytes / 1048576:.0f} МБ, пропущен"
		return TransferReport([], [(transcript.name, reason)])
	destination = vault_session_dir / transcript.name
	transform_transcript(transcript, destination, mapper, mode="tokenize", cwd_override=None)
	return TransferReport([transcript.name], [])


def pull_sessions(
	vault_session_dir: Path,
	target_dir: Path,
	mapper: PathMapper,
	local_project_path: str,
) -> TransferReport:
	"""Разложить транскрипты проекта под текущую машину."""
	moved: list[str] = []
	skipped: list[tuple[str, str]] = []
	if not vault_session_dir.is_dir():
		return TransferReport(moved, skipped)
	target_dir.mkdir(parents=True, exist_ok=True)
	for transcript in sorted(vault_session_dir.glob("*.jsonl")):
		destination = target_dir / transcript.name
		if destination.exists() and destination.stat().st_mtime >= transcript.stat().st_mtime:
			skipped.append((transcript.name, "локальная копия не старше"))
			continue
		transform_transcript(
			transcript,
			destination,
			mapper,
			mode="detokenize",
			cwd_override=local_project_path,
		)
		moved.append(transcript.name)
	return TransferReport(moved, skipped)
