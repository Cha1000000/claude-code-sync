"""Сессии, которые не должны уезжать в общее хранилище.

Два уровня строгости:

    ignore  — «впредь не отправлять»: пометка, после которой push пропускает
              транскрипт. Ничего не удаляет.
    forget  — «забыть везде»: пометка плюс удаление копии из хранилища и
              tombstone, по которому остальные машины снесут копию у себя.

Список игнора живёт локально (~/.claude/ccsync-ignore.json) сознательно: игнор
нужен только там, где сессия физически пишется, а id приватных сессий незачем
показывать в репозитории. В git уезжают только tombstone'ы — отметки об уже
забытом, где содержимого нет по определению.

Отдельная тонкость: локальный .jsonl живой сессии удалять бесполезно — Claude
Code продолжает в него писать и файл возрождается. Поэтому удаление у себя
откладывается до закрытия сессии (см. sweep_local).
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

IGNORE_FILE_NAME = "ccsync-ignore.json"
TOMBSTONES_DIR_NAME = "tombstones"

# Страховка на случай машины, которую списали, не убрав из machines.json:
# без неё её неполученное подтверждение держало бы отметку вечно.
DEFAULT_MAX_AGE_DAYS = 180


def now_stamp() -> str:
	"""Отметка времени с часовым поясом — читаемая и сравнимая."""
	return datetime.now().astimezone().isoformat(timespec="seconds")


def current_session_id() -> str | None:
	"""Id сессии, внутри которой мы запущены.

	Claude Code кладёт его в окружение, поэтому гадать по времени файлов не
	нужно. Пусто — значит команду запустили не из сессии (например из cron).
	"""
	value = os.environ.get("CLAUDE_CODE_SESSION_ID", "").strip()
	return value or None


def _write_json_atomic(path: Path, data) -> None:
	"""Записать через временный файл: оборванная запись не бьёт список."""
	path.parent.mkdir(parents=True, exist_ok=True)
	temporary = path.with_suffix(path.suffix + ".tmp")
	temporary.write_text(
		json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
		encoding="utf-8",
	)
	os.replace(temporary, path)


# --- локальный список игнора ---------------------------------------------


@dataclass
class IgnoreEntry:
	"""Одна помеченная сессия."""

	session_id: str
	reason: str = ""
	marked_at: str = ""
	project_key: str = ""
	# Была команда forget, а не просто ignore.
	forgotten: bool = False
	# Локальный файл ещё ждёт удаления: пометку поставили в живой сессии.
	delete_local_pending: bool = False

	def to_dict(self) -> dict:
		return {
			"reason": self.reason,
			"marked_at": self.marked_at,
			"project_key": self.project_key,
			"forgotten": self.forgotten,
			"delete_local_pending": self.delete_local_pending,
		}

	@classmethod
	def from_dict(cls, session_id: str, data: dict) -> "IgnoreEntry":
		return cls(
			session_id=session_id,
			reason=str(data.get("reason") or ""),
			marked_at=str(data.get("marked_at") or ""),
			project_key=str(data.get("project_key") or ""),
			forgotten=bool(data.get("forgotten")),
			delete_local_pending=bool(data.get("delete_local_pending")),
		)

	def describe(self) -> str:
		kind = "забыта" if self.forgotten else "не синхронизируется"
		where = f" · {self.project_key}" if self.project_key else ""
		why = f" · {self.reason}" if self.reason else ""
		pending = " · локальный файл ждёт закрытия сессии" if self.delete_local_pending else ""
		return f"{self.session_id[:8]} — {kind}{where}{why}{pending}"


class IgnoreList:
	"""Локальный список помеченных сессий.

	Запись остаётся навсегда: она и есть «липкость» пометки — сессию можно
	открыть заново через /resume, и она по-прежнему не уедет.
	"""

	def __init__(self, path: Path) -> None:
		self.path = Path(path)
		self.entries: dict[str, IgnoreEntry] = {}
		self._load()

	@classmethod
	def for_config(cls, config_dir: Path) -> "IgnoreList":
		return cls(Path(config_dir) / IGNORE_FILE_NAME)

	def _load(self) -> None:
		if not self.path.exists():
			return
		try:
			data = json.loads(self.path.read_text(encoding="utf-8"))
		except (json.JSONDecodeError, ValueError, OSError):
			# Битый список не должен блокировать работу: считаем его пустым,
			# но и не перезаписываем молча — перезапись случится при mark().
			return
		raw = data.get("ignored") if isinstance(data, dict) else None
		if not isinstance(raw, dict):
			return
		for session_id, payload in raw.items():
			if isinstance(payload, dict):
				self.entries[session_id] = IgnoreEntry.from_dict(session_id, payload)

	def save(self) -> None:
		_write_json_atomic(
			self.path,
			{
				"version": 1,
				"ignored": {key: entry.to_dict() for key, entry in self.entries.items()},
			},
		)

	# --- запросы ---------------------------------------------------------

	def is_ignored(self, session_id: str | None) -> bool:
		return bool(session_id) and session_id in self.entries

	def get(self, session_id: str) -> IgnoreEntry | None:
		return self.entries.get(session_id)

	def pending_local_deletions(self) -> list[IgnoreEntry]:
		return [e for e in self.entries.values() if e.delete_local_pending]

	@property
	def forgotten_count(self) -> int:
		return sum(1 for e in self.entries.values() if e.forgotten)

	# --- изменения -------------------------------------------------------

	def mark(
		self,
		session_id: str,
		*,
		reason: str = "",
		project_key: str = "",
		forgotten: bool = False,
		delete_local_pending: bool = False,
	) -> IgnoreEntry:
		"""Пометить сессию. Повторный вызов усиливает пометку, но не ослабляет."""
		existing = self.entries.get(session_id)
		entry = IgnoreEntry(
			session_id=session_id,
			reason=reason or (existing.reason if existing else ""),
			marked_at=existing.marked_at if existing and existing.marked_at else now_stamp(),
			project_key=project_key or (existing.project_key if existing else ""),
			forgotten=forgotten or bool(existing and existing.forgotten),
			delete_local_pending=delete_local_pending
			or bool(existing and existing.delete_local_pending),
		)
		self.entries[session_id] = entry
		self.save()
		return entry

	def clear_pending(self, session_id: str) -> None:
		entry = self.entries.get(session_id)
		if entry and entry.delete_local_pending:
			entry.delete_local_pending = False
			self.save()

	def unmark(self, session_id: str) -> bool:
		if session_id not in self.entries:
			return False
		del self.entries[session_id]
		self.save()
		return True


# --- отложенное удаление локальных транскриптов ---------------------------


def find_local_transcripts(projects_root: Path, session_id: str) -> list[Path]:
	"""Все копии <id>.jsonl по каталогам проектов, свежая первой.

	Копий может быть несколько: сессию, разложенную из хранилища, Claude Code
	кладёт в папку своего проекта, и рядом может лежать та же сессия, пришедшая
	под другим ключом. Порядок по времени правки, чтобы «первая» всегда была
	живой, а не застывшим дублем.
	"""
	if not projects_root.is_dir():
		return []
	found = [p for p in projects_root.glob(f"*/{session_id}.jsonl") if p.is_file()]
	return sorted(found, key=lambda p: p.stat().st_mtime, reverse=True)


def find_local_transcript(projects_root: Path, session_id: str) -> Path | None:
	"""Самая свежая копия <id>.jsonl. None — если её нет."""
	found = find_local_transcripts(projects_root, session_id)
	return found[0] if found else None


def sweep_local(config_dir: Path, *, skip_current: bool = True,
				skip_session_id: str | None = None) -> list[str]:
	"""Удалить локальные транскрипты, ждавшие закрытия своей сессии.

	Вызывается из хуков SessionEnd (штатно) и SessionStart (добивает случай,
	когда сессию прибили жёстко и SessionEnd не отработал).

	skip_current=True — не трогать файл сессии, внутри которой мы запущены:
	Claude Code пишет в него дальше и тут же создаст заново. В SessionEnd,
	наоборот, нужен skip_current=False: сессия уже закрыта, и удалять надо
	именно её файл, хотя переменная окружения всё ещё указывает на неё.
	"""
	config_dir = Path(config_dir)
	ignored = IgnoreList.for_config(config_dir)
	pending = ignored.pending_local_deletions()
	if not pending:
		return []

	projects_root = config_dir / "projects"
	alive = skip_session_id or (current_session_id() if skip_current else None)
	removed: list[str] = []
	for entry in pending:
		if alive and entry.session_id == alive:
			continue
		copies = find_local_transcripts(projects_root, entry.session_id)
		if not copies:
			# Файлов нет — значит удалять больше нечего.
			ignored.clear_pending(entry.session_id)
			continue
		failed = False
		for transcript in copies:
			try:
				transcript.unlink()
			except OSError:
				failed = True
		if failed:
			continue
		ignored.clear_pending(entry.session_id)
		removed.append(entry.session_id)
	return removed


# --- tombstone'ы в хранилище ----------------------------------------------
#
# Всё делается ТОЛЬКО созданием файлов: сама отметка и отдельные пустые файлы
# подтверждений. Поэтому одновременный forget или ack с двух машин git сливает
# без конфликтов — в отличие от общего JSON, который пришлось бы мержить.


@dataclass
class Tombstone:
	session_id: str
	project_key: str
	removed_by: str
	removed_at: str

	def age_days(self) -> float:
		try:
			removed = datetime.fromisoformat(self.removed_at)
		except (TypeError, ValueError):
			return 0.0
		now = datetime.now().astimezone()
		if removed.tzinfo is None:
			removed = removed.replace(tzinfo=now.tzinfo)
		return (now - removed).total_seconds() / 86400.0


def tombstones_dir(sessions_dir: Path) -> Path:
	return Path(sessions_dir) / TOMBSTONES_DIR_NAME


def write_tombstone(sessions_dir: Path, session_id: str, project_key: str,
					machine_id: str) -> Path:
	"""Записать отметку об удалённой сессии и сразу подтвердить её от себя."""
	directory = tombstones_dir(sessions_dir)
	path = directory / f"{session_id}.json"
	if not path.exists():
		_write_json_atomic(path, {
			"session_id": session_id,
			"project_key": project_key,
			"removed_by": machine_id,
			"removed_at": now_stamp(),
		})
	ack(sessions_dir, session_id, machine_id)
	return path


def load_tombstones(sessions_dir: Path) -> list[Tombstone]:
	directory = tombstones_dir(sessions_dir)
	if not directory.is_dir():
		return []
	found: list[Tombstone] = []
	for path in sorted(directory.glob("*.json")):
		try:
			data = json.loads(path.read_text(encoding="utf-8"))
		except (json.JSONDecodeError, ValueError, OSError):
			continue
		session_id = str(data.get("session_id") or path.stem)
		found.append(Tombstone(
			session_id=session_id,
			project_key=str(data.get("project_key") or ""),
			removed_by=str(data.get("removed_by") or ""),
			removed_at=str(data.get("removed_at") or ""),
		))
	return found


def ack_path(sessions_dir: Path, session_id: str, machine_id: str) -> Path:
	return tombstones_dir(sessions_dir) / f"{session_id}.ack-{machine_id}"


def ack(sessions_dir: Path, session_id: str, machine_id: str) -> bool:
	"""Отметиться «эта машина удаление отработала». True — если отметка новая."""
	path = ack_path(sessions_dir, session_id, machine_id)
	if path.exists():
		return False
	path.parent.mkdir(parents=True, exist_ok=True)
	path.write_text("", encoding="utf-8")
	return True


def acked_by(sessions_dir: Path, session_id: str) -> set[str]:
	directory = tombstones_dir(sessions_dir)
	if not directory.is_dir():
		return set()
	prefix = f"{session_id}.ack-"
	return {p.name[len(prefix):] for p in directory.glob(f"{session_id}.ack-*")}


def prune_tombstones(sessions_dir: Path, known_machines: set[str],
					 max_age_days: int = DEFAULT_MAX_AGE_DAYS) -> list[str]:
	"""Убрать отметки, отработанные всеми машинами (или совсем старые).

	Список машин берётся из machines.json. Пустой список означает, что реестр
	ещё не заполнен, — в этом случае удаляем только по сроку, чтобы не снести
	отметку раньше, чем её увидят.
	"""
	removed: list[str] = []
	for stone in load_tombstones(sessions_dir):
		confirmed = acked_by(sessions_dir, stone.session_id)
		everyone = bool(known_machines) and known_machines.issubset(confirmed)
		expired = stone.age_days() > max_age_days
		if not (everyone or expired):
			continue
		for path in tombstones_dir(sessions_dir).glob(f"{stone.session_id}.*"):
			try:
				path.unlink()
			except OSError:
				pass
		removed.append(stone.session_id)
	return removed
