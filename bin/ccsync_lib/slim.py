"""Сжатие транскриптов: копия для хранилища и плановая чистка старья.

Транскрипт растёт не от разговора, а от того, что к нему прилипает. Разбор
сессий на 175 и 676 МБ (15.10.2026) показал, что три четверти веса — скриншоты,
причём каждый хранится дважды: в сообщении для модели и в `toolUseResult` для
интерфейса. Остальное — служебные записи и длинные выводы инструментов.

Ступени идут от безобидной к заметной. Первые две ничего не отнимают у
разговора и применяются всегда; следующие включаются, только если файл всё ещё
не влезает в порог. `uuid` у записей не меняется — поэтому сжатая копия
остаётся сравнимой с полной по составу записей.
"""

from __future__ import annotations

import gzip
import json
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path
from datetime import datetime

IMAGE_STUB = "[ccsync: изображение не синхронизировано]"
OUTPUT_STUB = "\n\n[ccsync: вывод сокращён, было {size} КБ]\n\n"

# Записи без `uuid`, которые в разговоре не участвуют: снимки истории файлов,
# слепки запросов к API, ссылки на PR и очередь ввода. `--resume` их не читает.
SERVICE_TYPES = frozenset({
	"file-history-snapshot",
	"file-history-delta",
	"api-request",
	"api-request-blob",
	"api-request-shape",
	"queue-operation",
	"pr-link",
	"frame-link",
})

# Записи-состояния: Claude Code дописывает их заново при каждом изменении,
# действует последняя. Из повторов оставляем только её.
LATEST_ONLY_TYPES = frozenset({
	"cost-state",
	"ai-title",
	"custom-title",
	"agent-name",
	"mode",
	"permission-mode",
	"last-prompt",
	"atis-latch",
})

OUTPUT_LIMIT = 64 * 1024
OUTPUT_HEAD = 16 * 1024
OUTPUT_TAIL = 4 * 1024

STAGES = ("images", "service", "outputs", "history")

# Куда `ccsync slim` убирает полный оригинал. Не синхронизируется.
ARCHIVE_DIR = "session-archive"


@dataclass
class SlimReport:
	"""Сколько весила копия до сжатия и после каждой ступени."""

	before: int
	after: dict[str, int] = field(default_factory=dict)
	applied: list[str] = field(default_factory=list)
	cut_at: str | None = None   # время границы /compact, если резали историю

	@property
	def final(self) -> int:
		return self.after[self.applied[-1]] if self.applied else self.before


def _size(records: list) -> int:
	return sum(len(_dump(r).encode("utf-8")) + 1 for r in records)


def _dump(record) -> str:
	if isinstance(record, str):
		return record   # строку, которую не удалось разобрать, везём как есть
	return json.dumps(record, ensure_ascii=False)


def _parse_time(value) -> datetime | None:
	if not isinstance(value, str):
		return None
	try:
		return datetime.fromisoformat(value.replace("Z", "+00:00"))
	except ValueError:
		return None


def _older_than(record: dict, cutoff: datetime | None) -> bool:
	if cutoff is None:
		return True
	moment = _parse_time(record.get("timestamp"))
	return moment is not None and moment < cutoff


# --- ступень 1: картинки ---------------------------------------------------

def _strip_images_in(node):
	"""Заменить base64-картинки текстовой пометкой. Возвращает (узел, сколько заменено)."""
	if isinstance(node, list):
		out, count = [], 0
		for item in node:
			item, found = _strip_images_in(item)
			out.append(item)
			count += found
		return out, count
	if isinstance(node, dict):
		source = node.get("source")
		if node.get("type") == "image" and isinstance(source, dict) and source.get("type") == "base64":
			return {"type": "text", "text": IMAGE_STUB}, 1
		out, count = {}, 0
		for key, value in node.items():
			value, found = _strip_images_in(value)
			out[key] = value
			count += found
		return out, count
	return node, 0


def strip_images(records: list, *, cutoff: datetime | None = None) -> int:
	"""Убрать картинки из записей (старше cutoff, если он задан). Возвращает их число."""
	total = 0
	for index, record in enumerate(records):
		if not isinstance(record, dict) or not _older_than(record, cutoff):
			continue
		message = record.get("message")
		found = 0
		if isinstance(message, dict) and isinstance(message.get("content"), list):
			content, found = _strip_images_in(message["content"])
			if found:
				record = dict(record)
				record["message"] = dict(message, content=content)
		result = record.get("toolUseResult")
		# Дубль для интерфейса: у Read картинки лежит в toolUseResult.file.base64.
		if isinstance(result, dict) and result.get("type") == "image" and isinstance(result.get("file"), dict):
			record = dict(record)
			record.pop("toolUseResult")
			found += 1
		if found:
			records[index] = record
			total += found
	return total



def strip_images_line(line: str) -> str:
	"""То же для одной строки JSONL; строку без картинок возвращает как есть.

	Проверка подстроки дешёвая, поэтому разбираются только строки, где
	картинка действительно может быть.
	"""
	if '"base64"' not in line:
		return line
	try:
		record = json.loads(line)
	except ValueError:
		return line
	records = [record]
	if not strip_images(records):
		return line
	return json.dumps(records[0], ensure_ascii=False, separators=(",", ":"))

# --- ступень 2: служебные записи --------------------------------------------

def drop_service(records: list) -> list:
	latest: dict[str, int] = {}
	for index, record in enumerate(records):
		if isinstance(record, dict) and record.get("type") in LATEST_ONLY_TYPES:
			latest[record["type"]] = index
	kept = []
	for index, record in enumerate(records):
		kind = record.get("type") if isinstance(record, dict) else None
		if kind in SERVICE_TYPES:
			continue
		if kind in LATEST_ONLY_TYPES and latest[kind] != index:
			continue
		kept.append(record)
	return kept


# --- ступень 3: длинные выводы инструментов --------------------------------

def _shorten(text: str) -> str:
	size = len(text.encode("utf-8"))
	if size <= OUTPUT_LIMIT:
		return text
	return text[:OUTPUT_HEAD] + OUTPUT_STUB.format(size=size // 1024) + text[-OUTPUT_TAIL:]


def _shorten_result(block: dict) -> tuple[dict, bool]:
	content = block.get("content")
	if isinstance(content, str):
		short = _shorten(content)
		return (dict(block, content=short), True) if short is not content else (block, False)
	if isinstance(content, list):
		changed, out = False, []
		for item in content:
			if isinstance(item, dict) and item.get("type") == "text" and isinstance(item.get("text"), str):
				short = _shorten(item["text"])
				if short is not item["text"]:
					item, changed = dict(item, text=short), True
			out.append(item)
		return (dict(block, content=out), True) if changed else (block, False)
	return block, False


def shorten_outputs(records: list) -> int:
	total = 0
	for index, record in enumerate(records):
		if not isinstance(record, dict) or record.get("type") != "user":
			continue
		message = record.get("message")
		content = message.get("content") if isinstance(message, dict) else None
		if not isinstance(content, list):
			continue
		changed, out = False, []
		for block in content:
			if isinstance(block, dict) and block.get("type") == "tool_result":
				block, did = _shorten_result(block)
				changed = changed or did
			out.append(block)
		if changed:
			record = dict(record, message=dict(message, content=out))
			# Полный вывод дублируется в toolUseResult — без него интерфейс
			# покажет то, что лежит в сообщении.
			record.pop("toolUseResult", None)
			records[index] = record
			total += 1
	return total


# --- ступень 4: начало разговора до границы /compact ------------------------

def cut_history(records: list, target: int) -> tuple[list, str | None]:
	"""Отрезать начало по самой ранней границе /compact, после которой влезаем в порог.

	Граница выбрана не «по смыслу», а по механике: от неё Claude Code сам начинает
	контекст модели, и сводка разговора лежит сразу за ней. Поэтому срез по ней
	не меняет того, что модель знает о сессии, а историю оставляем по максимуму —
	ранняя граница лучше поздней. Записи без `uuid` (заголовок, режим) не режем.
	"""
	sizes = [len(_dump(r).encode("utf-8")) + 1 for r in records]
	if sum(sizes) <= target:
		return records, None
	keep_meta = [0] * (len(records) + 1)   # вес служебных записей до позиции i
	for index, record in enumerate(records):
		is_meta = isinstance(record, dict) and not record.get("uuid")
		keep_meta[index + 1] = keep_meta[index] + (sizes[index] if is_meta else 0)
	suffix = [0] * (len(records) + 1)
	for index in range(len(records) - 1, -1, -1):
		suffix[index] = suffix[index + 1] + sizes[index]
	for index, record in enumerate(records):
		if not (isinstance(record, dict) and record.get("subtype") == "compact_boundary"):
			continue
		if suffix[index] + keep_meta[index] <= target:
			kept = [r for r in records[:index] if isinstance(r, dict) and not r.get("uuid")]
			return kept + records[index:], record.get("timestamp")
	return records, None


# --- всё вместе -------------------------------------------------------------

def parse_lines(lines) -> list:
	records = []
	for line in lines:
		line = line.rstrip("\n")
		if not line.strip():
			continue
		try:
			records.append(json.loads(line))
		except json.JSONDecodeError:
			records.append(line)
	return records


def slim_records(records: list, *, target: int,
				 measure_all: bool = False) -> tuple[list, SlimReport]:
	"""Сжать записи до порога target (в байтах).

	Ступени 1–2 применяются всегда, 3–4 — пока копия больше порога.
	measure_all — прогнать все ступени независимо от порога (для замера).
	"""
	records = list(records)
	report = SlimReport(before=_size(records))

	def done(stage: str) -> None:
		report.applied.append(stage)
		report.after[stage] = _size(records)

	strip_images(records)
	done("images")
	records = drop_service(records)
	done("service")
	if measure_all or report.final > target:
		shorten_outputs(records)
		done("outputs")
	if measure_all or report.final > target:
		records, report.cut_at = cut_history(records, target)
		done("history")
	return records, report


def dump_records(records: list) -> str:
	return "".join(_dump(r) + "\n" for r in records)


def read_maybe_gzip(path: Path) -> list[str]:
	"""Строки транскрипта из .jsonl или из архива .jsonl.gz."""
	opener = gzip.open if path.suffix == ".gz" else open
	with opener(path, "rt", encoding="utf-8", errors="replace") as handle:
		return [line.rstrip("\n") for line in handle if line.strip()]


def archive_original(transcript: Path, archive_dir: Path) -> Path:
	"""Сложить полный транскрипт в gzip-архив перед сжатием. Возвращает путь архива."""
	archive_dir.mkdir(parents=True, exist_ok=True)
	target = archive_dir / f"{transcript.stem}-{time.strftime('%Y%m%d-%H%M%S')}.jsonl.gz"
	with transcript.open("rb") as source, gzip.open(target, "wb", compresslevel=6) as dest:
		shutil.copyfileobj(source, dest)
	return target
