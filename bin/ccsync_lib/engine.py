"""Обновление движка из выпуска публичного репозитория.

Движок живёт в хранилище пользователя (`bin/`), а выпуски — это подписанные
теги публичного репозитория. Модуль приносит выпуск и аккуратно кладёт его
поверх, ничего не теряя:

    план       — какие файлы новые, какие обновятся, какие пользователь правил;
    бэкап      — всё, что будет заменено, уходит в ~/.claude/backups/engine-*;
    запись     — атомарно, под замком, который видят хуки;
    самопроверка и откат — если новый движок не запускается, возвращаем старый;
    коммит     — только записанные пути (не `bin` целиком) и push.

Опорой для «кто правил файл» служит манифест установленного выпуска
(bin/ENGINE.json): хеш не совпал — файл правили руками, и мы его не трогаем.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import contextlib
import io

from . import engine_manifest as manifest_lib
from . import held, tools
from .i18n import tr

TAG_RE = re.compile(r"^v(\d{4})\.(\d{2})\.(\d{2})(?:\.(\d+))?$")
STATE_NAME = "ccsync-engine-state.json"
LOCK_NAME = "ccsync-engine.lock"
BACKUPS_DIR = "backups"
LOCK_TTL = 300  # секунд: хуки пропускают свои действия, пока замок свежий
LIST_TIMEOUT = 5
CHECK_EVERY = 24 * 3600     # как часто спрашивать сеть о новых выпусках
RETRY_AFTER = 3600          # после неудачной попытки не стучимся чаще
REMIND_AFTER = 7 * 24 * 3600
POLICY_FILE = "engine-update.json"
MODES = ("notify", "auto", "off")
DEFAULT_MODE = "notify"
CLONE_TIMEOUT = 180
SIGNERS_REL = "bin/ccsync_lib/release-signers"

# Результаты проверки подписи тега.
VERIFIED = "verified"            # подпись сошлась с ключом из уже установленного движка
VERIFIED_SELF = "verified-self"  # сошлась только с ключом из самого выпуска
UNSIGNED = "unsigned"            # подписи нет (или источник — не тег)
FAILED = "failed"                # подпись есть, но не сходится
UNAVAILABLE = "unavailable"      # проверить нечем (нет git/ssh-keygen нужной версии)


class EngineError(RuntimeError):
	"""Ошибка, которую надо показать пользователем как есть."""


# --- теги --------------------------------------------------------------

def tag_key(tag: str) -> tuple[int, int, int, int] | None:
	found = TAG_RE.match(tag)
	if not found:
		return None
	year, month, day, number = found.groups()
	return int(year), int(month), int(day), int(number or 1)


def latest_tag(tags: list[str], exclude: set[str] | frozenset[str] = frozenset()) -> str | None:
	valid = [tag for tag in tags if tag_key(tag) and tag not in exclude]
	return max(valid, key=tag_key) if valid else None


def list_remote_tags(url: str, timeout: float = LIST_TIMEOUT) -> list[str] | None:
	"""Теги публичного репозитория. None — нет сети, таймаут, нет репозитория."""
	env = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GCM_INTERACTIVE": "never"}
	try:
		proc = subprocess.run(["git", "ls-remote", "--tags", "--refs", url],
							  capture_output=True, text=True, encoding="utf-8",
							  errors="replace", timeout=timeout, env=env)
	except (OSError, subprocess.SubprocessError):
		return None
	if proc.returncode:
		return None
	tags = []
	for line in proc.stdout.splitlines():
		_, _, ref = line.partition("\t")
		if ref.startswith("refs/tags/"):
			tags.append(ref[len("refs/tags/"):])
	return tags


# --- состояние на машине ------------------------------------------------

def state_path(config_dir: Path) -> Path:
	return config_dir / STATE_NAME


def load_state(config_dir: Path) -> dict:
	try:
		data = json.loads(state_path(config_dir).read_text(encoding="utf-8"))
	except (OSError, ValueError):
		return {}
	return data if isinstance(data, dict) else {}


def save_state(config_dir: Path, state: dict) -> None:
	path = state_path(config_dir)
	path.parent.mkdir(parents=True, exist_ok=True)
	_write_bytes(path, (json.dumps(state, indent=2, sort_keys=True, ensure_ascii=False)
						+ "\n").encode("utf-8"))


def lock_path(config_dir: Path) -> Path:
	return config_dir / LOCK_NAME


def is_locked(config_dir: Path) -> bool:
	"""Идёт ли обновление прямо сейчас: хуки в это время ничего не трогают."""
	try:
		return time.time() - lock_path(config_dir).stat().st_mtime < LOCK_TTL
	except OSError:
		return False


def acquire_lock(config_dir: Path) -> None:
	"""Создать замок атомарно (O_EXCL): двух обновлений сразу быть не может."""
	path = lock_path(config_dir)
	path.parent.mkdir(parents=True, exist_ok=True)
	for _ in range(2):
		try:
			handle = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
		except FileExistsError:
			if is_locked(config_dir):
				raise EngineError(tr("Обновление уже идёт (замок {path}).", path=path))
			path.unlink(missing_ok=True)  # замок протух: прошлое обновление оборвалось
			continue
		with os.fdopen(handle, "w") as stream:
			stream.write(str(os.getpid()))
		return
	raise EngineError(tr("Обновление уже идёт (замок {path}).", path=path))


def touch_lock(config_dir: Path) -> None:
	try:
		os.utime(lock_path(config_dir))
	except OSError:
		pass


def release_lock(config_dir: Path) -> None:
	lock_path(config_dir).unlink(missing_ok=True)


# --- файлы ------------------------------------------------------------

def _write_bytes(path: Path, data: bytes, mode_from: Path | None = None) -> None:
	"""Через временный файл: оборванная запись не оставит половину файла."""
	path.parent.mkdir(parents=True, exist_ok=True)
	handle, name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.",
									suffix=".ccsync-tmp")
	try:
		with os.fdopen(handle, "wb") as stream:
			stream.write(data)
		if mode_from is not None:
			shutil.copymode(mode_from, name)
		os.replace(name, path)
	finally:
		Path(name).unlink(missing_ok=True)


def _hash_of(path: Path) -> str | None:
	try:
		return manifest_lib.digest(path.read_bytes())
	except OSError:
		return None


def load_installed(root: Path) -> dict | None:
	"""Манифест установленного выпуска; None — его нет (старое хранилище)."""
	try:
		return manifest_lib.loads((root / manifest_lib.MANIFEST_PATH).read_text(encoding="utf-8"))
	except (OSError, ValueError):
		return None


# --- план -------------------------------------------------------------

@dataclass
class Plan:
	new: list[str] = field(default_factory=list)       # файла не было
	update: list[str] = field(default_factory=list)    # не правлен — заменяем
	overwrite: list[str] = field(default_factory=list)  # правлен, но --adopt — заменяем с бэкапом
	same: list[str] = field(default_factory=list)      # уже как в выпуске
	edited: list[str] = field(default_factory=list)    # правлен здесь — оставляем
	remove: list[str] = field(default_factory=list)    # в выпуске больше нет — удаляем
	remove_kept: list[str] = field(default_factory=list)  # не нужен выпуску, но правлен — оставляем
	merge: dict[str, list[str]] = field(default_factory=dict)  # файл → дописываемые строки

	def writes(self) -> list[str]:
		return sorted(self.new + self.update + self.overwrite)

	def touched(self) -> list[str]:
		return sorted(set(self.writes() + self.remove + list(self.merge)))

	def empty(self) -> bool:
		return not (self.new or self.update or self.overwrite or self.remove or self.merge)


def make_plan(root: Path, release: dict, source: Path, installed: dict | None,
			  adopt: bool) -> Plan:
	"""Что и с чем делать. `source` — дерево выпуска, `root` — хранилище."""
	plan = Plan()
	base = (installed or {}).get("files", {})
	_check_paths(release["files"], source)
	for rel in base:
		# Манифест установленного выпуска лежит у пользователя на диске, ему тоже не верим.
		if not _safe_rel(rel):
			raise EngineError(tr("Манифест содержит недопустимый путь: {path}", path=rel))
	for name in release.get("merge_lines", []):
		if name not in manifest_lib.MERGE_LINES:
			raise EngineError(tr("Манифест просит дополнить неположенный файл: {path}", path=name))
	for rel, wanted in sorted(release["files"].items()):
		local = _hash_of(root / rel)
		if local is None:
			plan.new.append(rel)
		elif local == wanted:
			plan.same.append(rel)
		elif installed is not None and rel in base and base[rel] == wanted:
			# Выпуск этот файл не менял, а у пользователя он другой — это его правка,
			# обновлять тут нечего.
			plan.same.append(rel)
		elif installed is not None and rel in base and local == base[rel]:
			plan.update.append(rel)
		elif adopt:
			plan.overwrite.append(rel)
		else:
			plan.edited.append(rel)
	for rel, old in sorted(base.items()):
		if rel in release["files"]:
			continue
		local = _hash_of(root / rel)
		if local is None:
			continue
		(plan.remove if local == old else plan.remove_kept).append(rel)
	for name in release.get("merge_lines", []):
		added = _missing_lines(root / name, source / name)
		if added:
			plan.merge[name] = added
	return plan


def _safe_rel(rel: object) -> bool:
	"""Путь из манифеста: относительный, без «..», и такой, что входит в выпуск."""
	if not isinstance(rel, str) or not rel or rel.startswith("/") or "\\" in rel \
			or ":" in rel.split("/")[0]:
		return False
	return ".." not in rel.split("/") and manifest_lib.managed(rel)


def _check_paths(files: dict, source: Path) -> None:
	"""Выпуск пишет только в свои пути и ничего не тащит через симлинки."""
	for rel, digest in files.items():
		if not _safe_rel(rel) or not isinstance(digest, str):
			raise EngineError(tr("Манифест содержит недопустимый путь: {path}", path=rel))
		path = source / rel
		if path.is_symlink() or not path.is_file():
			raise EngineError(tr("В выпуске нет обычного файла {path} — выпуск повреждён.", path=rel))
		if manifest_lib.digest(path.read_bytes()) != digest:
			raise EngineError(tr("Файл {path} не совпадает с манифестом выпуска — выпуск повреждён "
								 "или подменён.", path=rel))


def _meaningful(text: str) -> list[str]:
	return [line for line in text.splitlines() if line.strip() and not line.startswith("#")]


def _missing_lines(local: Path, wanted: Path) -> list[str]:
	"""Строки выпуска (без комментариев), которых нет в локальном файле."""
	try:
		wanted_text = wanted.read_text(encoding="utf-8")
	except OSError:
		return []
	try:
		have = set(local.read_text(encoding="utf-8").splitlines())
	except OSError:
		have = set()
	return [line for line in _meaningful(wanted_text) if line not in have]


# --- источник выпуска --------------------------------------------------

@dataclass
class Source:
	root: Path               # дерево выпуска
	tag: str | None          # тег, если источник — тег
	verification: str        # одно из VERIFIED… UNAVAILABLE
	detail: str = ""
	temporary: Path | None = None

	def close(self) -> None:
		if self.temporary:
			shutil.rmtree(self.temporary, ignore_errors=True)


def _git(repo: Path, *args: str, timeout: float | None = None,
		 extra: list[str] | None = None) -> subprocess.CompletedProcess:
	env = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GCM_INTERACTIVE": "never"}
	return subprocess.run(["git", *(extra or []), "-C", str(repo), *args],
						  capture_output=True, text=True, encoding="utf-8",
						  errors="replace", timeout=timeout, env=env)


def verify_tag(repo: Path, tag: str, anchor: Path | None) -> tuple[str, str]:
	"""Проверить подпись тега по файлу allowed signers. (результат, пояснение)."""
	if anchor is None or not anchor.exists():
		return UNAVAILABLE, "нет файла release-signers"
	extra = ["-c", "gpg.format=ssh", "-c", f"gpg.ssh.allowedSignersFile={anchor}"]
	try:
		proc = _git(repo, "verify-tag", tag, extra=extra, timeout=30)
	except (OSError, subprocess.SubprocessError) as error:
		return UNAVAILABLE, str(error)
	if proc.returncode == 0:
		return VERIFIED, ""
	text = (proc.stderr + proc.stdout).strip()
	lowered = text.lower()
	if "no signature" in lowered or "cannot verify a non-tag" in lowered:
		return UNSIGNED, text
	if "usage" in lowered or "unknown option" in lowered or "gpg.format" in lowered \
			or "ssh-keygen" in lowered and "not found" in lowered:
		return UNAVAILABLE, text
	return FAILED, text


def fetch_tag(url: str, tag: str, anchor: Path | None) -> Source:
	"""Мелкий клон тега во временный каталог и проверка подписи."""
	temporary = Path(tempfile.mkdtemp(prefix="ccsync-engine-"))
	dest = temporary / "release"
	try:
		proc = subprocess.run(
			["git", "clone", "--quiet", "--depth", "1", "--branch", tag, url, str(dest)],
			capture_output=True, text=True, encoding="utf-8", errors="replace",
			timeout=CLONE_TIMEOUT,
			env={**os.environ, "GIT_TERMINAL_PROMPT": "0", "GCM_INTERACTIVE": "never"})
	except (OSError, subprocess.SubprocessError) as error:
		shutil.rmtree(temporary, ignore_errors=True)
		raise EngineError(tr("Не удалось скачать выпуск {tag}: {error}",
							 tag=tag, error=error)) from error
	if proc.returncode:
		shutil.rmtree(temporary, ignore_errors=True)
		raise EngineError(tr("Не удалось скачать выпуск {tag}: {error}",
							 tag=tag, error=proc.stderr.strip() or proc.stdout.strip()))
	state, detail = verify_tag(dest, tag, anchor)
	if anchor is None:
		state, detail = _verify_self(dest, tag)
	return Source(dest, tag, state, detail, temporary)


def _verify_self(repo: Path, tag: str) -> tuple[str, str]:
	"""Нет установленного ключа: сверяем с тем, что лежит в самом выпуске."""
	state, detail = verify_tag(repo, tag, repo / SIGNERS_REL)
	return (VERIFIED_SELF if state == VERIFIED else state), detail


def directory_source(path: Path, anchor: Path | None) -> Source:
	"""Готовый каталог (клон): подпись проверяем, только если на HEAD стоит тег."""
	path = path.expanduser().resolve()
	if not (path / manifest_lib.MANIFEST_PATH).exists():
		raise EngineError(tr("В {path} нет bin/ENGINE.json — это не выпуск движка.", path=path))
	tag = None
	if (path / ".git").exists():
		described = _git(path, "describe", "--exact-match", "--tags", "HEAD")
		if described.returncode == 0:
			tag = described.stdout.strip() or None
	if tag is None:
		return Source(path, None, UNSIGNED, tr("каталог без тега на HEAD"))
	state, detail = verify_tag(path, tag, anchor)
	if anchor is None:
		state, detail = _verify_self(path, tag)
	return Source(path, tag, state, detail)


# --- замок, бэкап, откат ------------------------------------------------

def _stamp() -> str:
	return datetime.now().strftime("%Y%m%d-%H%M%S")


def backup_root(config_dir: Path) -> Path:
	return config_dir / BACKUPS_DIR


def list_backups(config_dir: Path) -> list[Path]:
	base = backup_root(config_dir)
	if not base.is_dir():
		return []
	return sorted(path for path in base.iterdir()
				  if path.name.startswith("engine-") and (path / "journal.json").exists())


def _read_json(path: Path) -> dict:
	try:
		data = json.loads(path.read_text(encoding="utf-8"))
	except (OSError, ValueError):
		return {}
	return data if isinstance(data, dict) else {}


def make_backup(config_dir: Path, root: Path, plan: Plan, old_release: str | None,
				new_release: str | None) -> Path:
	"""Сохранить всё, что будет заменено, удалено или дополнено, и журнал."""
	directory = backup_root(config_dir) / f"engine-{_stamp()}"
	suffix = 1
	while directory.exists():
		suffix += 1
		directory = backup_root(config_dir) / f"engine-{_stamp()}-{suffix}"
	files = directory / "files"
	saved = []
	for rel in sorted(set(plan.update + plan.overwrite + plan.remove + list(plan.merge)
						  + [manifest_lib.MANIFEST_PATH])):
		source = root / rel
		if source.is_file():
			target = files / rel
			target.parent.mkdir(parents=True, exist_ok=True)
			shutil.copy2(source, target)
			saved.append(rel)
	created = list(plan.new)
	if not (root / manifest_lib.MANIFEST_PATH).exists():
		created.append(manifest_lib.MANIFEST_PATH)  # откат из --adopt не должен оставить манифест
	journal = {
		"from": old_release, "to": new_release, "saved": saved,
		"created": sorted(created), "root": str(root), "committed": False,
		"rolled_back": False,
	}
	_write_bytes(directory / "journal.json",
				 (json.dumps(journal, indent=2, ensure_ascii=False) + "\n").encode("utf-8"))
	return directory


def restore_backup(directory: Path, root: Path) -> list[str]:
	"""Вернуть файлы из бэкапа, а созданные выпуском — убрать. Список затронутого."""
	journal = _read_json(directory / "journal.json")
	touched = []
	for rel in journal.get("saved", []):
		source = directory / "files" / rel
		if source.is_file():
			_write_bytes(root / rel, source.read_bytes(), mode_from=source)
			touched.append(rel)
	for rel in journal.get("created", []):
		target = root / rel
		if target.is_file():
			target.unlink()
			touched.append(rel)
	journal["rolled_back"] = True
	_write_bytes(directory / "journal.json",
				 (json.dumps(journal, indent=2, ensure_ascii=False) + "\n").encode("utf-8"))
	return sorted(set(touched))


def self_check(root: Path, written: list[str]) -> str | None:
	"""Новый движок хотя бы запускается. Текст ошибки или None."""
	for rel in written:
		if rel.endswith(".py"):
			try:
				compile((root / rel).read_bytes(), rel, "exec")
			except (SyntaxError, ValueError) as error:
				return f"{rel}: {error}"
			except OSError:
				continue
	entry = root / "bin" / "ccsync.py"
	for args in (["--help"], ["--vault", str(root), "engine", "status"]):
		try:
			proc = subprocess.run([sys.executable, str(entry), *args], capture_output=True,
								  text=True, encoding="utf-8", errors="replace", timeout=60,
								  env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})
		except (OSError, subprocess.SubprocessError) as error:
			return f"ccsync.py {' '.join(args)}: {error}"
		if proc.returncode:
			tail = (proc.stderr or proc.stdout).strip().splitlines()[-3:]
			return f"ccsync.py {' '.join(args)}: " + " | ".join(tail)
	return None


# --- команды -----------------------------------------------------------

def _vault_python_ok(release: dict) -> str | None:
	need = str(release.get("min_python") or "3.9")
	try:
		wanted = tuple(int(part) for part in need.split(".")[:2])
	except ValueError:
		return None
	if sys.version_info[:2] < wanted:
		return tr("Выпуску нужен Python {need}+, здесь {have}.",
				  need=need, have=".".join(map(str, sys.version_info[:3])))
	return None


def installed_anchor(root: Path) -> Path | None:
	"""Файл ключей из УЖЕ установленного движка — ему доверяем больше, чем скачанному."""
	path = root / SIGNERS_REL
	return path if path.exists() else None


def resolve_source(context, args, installed: dict | None, state: dict) -> Source:
	anchor = installed_anchor(context.root)
	if args.source:
		return directory_source(Path(args.source), anchor)
	url = (installed or {}).get("source")
	if not url:
		raise EngineError(tr("В хранилище нет bin/ENGINE.json, откуда брать выпуск, неизвестно. "
							 "Клонируй репозиторий шаблона и запусти: "
							 "python3 <клон>/bin/ccsync.py --vault <хранилище> "
							 "engine update --source <клон> --adopt"))
	bad = set(state.get("bad", []))
	if args.tag:
		tag = args.tag
	else:
		tags = list_remote_tags(url, timeout=15)
		if tags is None:
			raise EngineError(tr("Репозиторий {url} недоступен — проверить выпуски не удалось.",
								 url=url))
		tag = latest_tag(tags, exclude=bad)
		if tag is None:
			raise EngineError(tr("В репозитории нет выпусков (тегов vГГГГ.ММ.ДД)."))
	if tag in bad and args.tag:
		print(tr("Внимание: выпуск {tag} помечен плохим — на этой машине он уже не прошёл "
				 "самопроверку.", tag=tag), file=sys.stderr)
	return fetch_tag(url, tag, anchor)


def _describe(rels: list[str], limit: int = 8) -> str:
	shown = ", ".join(rels[:limit])
	return shown + (f" … (+{len(rels) - limit})" if len(rels) > limit else "")


def print_plan(plan: Plan, release_label: str, installed_label: str | None) -> None:
	print(tr("Установлен выпуск: {old}  →  в источнике: {new}",
			 old=installed_label or tr("неизвестно (старое хранилище)"), new=release_label))
	rows = [
		(tr("новых файлов:"), plan.new),
		(tr("обновится:"), plan.update),
		(tr("перезапишется (--adopt, прежнее — в бэкап):"), plan.overwrite),
		(tr("уйдёт (в выпуске больше нет):"), plan.remove),
		(tr("правлено здесь — ОСТАВЛЕНО:"), plan.edited),
		(tr("правлено здесь и выпуску не нужно — оставлено:"), plan.remove_kept),
	]
	for label, rels in rows:
		if rels:
			print(f"  {label} {len(rels)}  {_describe(rels)}")
	for name, lines in plan.merge.items():
		print("  " + tr("{name}: дописать строки: {lines}", name=name, lines=", ".join(lines)))
	if plan.same:
		print("  " + tr("без изменений: {count}", count=len(plan.same)))


def _confirm(prompt: str) -> bool:
	if not sys.stdin.isatty():
		return False
	try:
		return input(prompt).strip().lower() in ("y", "yes", "д", "да")
	except EOFError:
		return False


def cmd_update(context, args) -> int:
	config_dir = context.config_dir
	root = context.root
	if (root / "release-public.py").exists():
		raise EngineError(tr("Это хранилище мейнтейнера: движок правится здесь, а выпуск "
							 "делает release-public.py."))
	state = load_state(config_dir)
	installed = load_installed(root)
	if installed is None and not (args.adopt and args.source):
		raise EngineError(tr("В хранилище нет bin/ENGINE.json (оно создано старым движком). "
							 "Однократная установка: клонируй репозиторий шаблона и запусти "
							 "python3 <клон>/bin/ccsync.py --vault <хранилище> "
							 "engine update --source <клон> --adopt"))
	if is_locked(config_dir):
		raise EngineError(tr("Обновление уже идёт (замок {path}).", path=lock_path(config_dir)))
	if context.git.rebase_in_progress() or context.git.conflicted_files():
		raise EngineError(tr("В хранилище незавершённый rebase или конфликт — сначала разберись с ним."))
	if not args.dry_run and context.git.has_remote():
		# Сначала чужое: другая машина могла уже поставить этот выпуск.
		pulled = context.git.pull()
		if not pulled.ok:
			raise EngineError(tr("git pull не удался: {error}", error=pulled.err or pulled.out))
		installed = load_installed(root)

	source = resolve_source(context, args, installed, state)
	try:
		release = manifest_lib.loads((source.root / manifest_lib.MANIFEST_PATH)
									 .read_text(encoding="utf-8"))
		label = release.get("release") or source.tag or "?"
		if source.tag and release.get("release") != source.tag:
			raise EngineError(tr("Манифест выпуска называет его {label}, а тег — {tag}: "
								 "выпуск подменён или собран неверно.", label=label, tag=source.tag))
		problem = _vault_python_ok(release)
		if problem:
			raise EngineError(problem)
		if source.verification == FAILED:
			raise EngineError(tr("Подпись тега {tag} НЕ сходится с ключом выпусков — "
								 "ставить нельзя. {detail}", tag=source.tag, detail=source.detail))
		# Из сети — только подпись ключом из УЖЕ установленного движка: ключ из самого
		# скачанного выпуска ничего не доказывает.
		if args.source is None and source.verification != VERIFIED \
				and not args.allow_unverified:
			raise EngineError(tr("Подпись выпуска {tag} проверить не удалось ({state}). "
								 "Если ты доверяешь источнику — повтори с --allow-unverified.",
								 tag=source.tag, state=source.verification))
		old_key, new_key = tag_key((installed or {}).get("release") or ""), tag_key(label)
		if old_key and new_key and new_key < old_key and not args.tag and not args.source:
			raise EngineError(tr("Выпуск {new} старее установленного {old}: понижение версии "
								 "только явным --tag.", new=label, old=installed.get("release")))
		plan = make_plan(root, release, source.root, installed, args.adopt)
		if getattr(args, "strict", False) and (
				source.verification != VERIFIED or plan.edited or plan.remove_kept):
			raise EngineError(tr("Автообновление не применяет выпуск {tag}: подпись не сошлась "
								 "с установленным ключом или есть правленные файлы. "
								 "Поставь вручную: /sync-update", tag=label))
		old_label = (installed or {}).get("release")
		print_plan(plan, label, old_label)
		print("  " + tr("подпись: {state}", state=_verification_text(source)))
		# Даже если файлы менять нечем (всё совпало или правлено вами), манифест
		# должен встать: иначе выпуск навсегда числится «новым».
		manifest_stale = installed is None or installed != release
		if plan.empty() and not manifest_stale:
			print(tr("Движок уже на этом выпуске, делать нечего."))
			return 0
		if args.dry_run:
			print(tr("--dry-run: ничего не записано."))
			return 0
		if not args.yes and not _confirm(tr("Применить? [y/N] ")):
			print(tr("Ничего не записано. Подтверди: добавь --yes"), file=sys.stderr)
			return 2
		return _apply(context, args, source, release, plan, installed, label, state)
	finally:
		source.close()


def _verification_text(source: Source) -> str:
	return {
		VERIFIED: tr("сходится с ключом выпусков"),
		VERIFIED_SELF: tr("сходится только с ключом из самого выпуска"),
		UNSIGNED: tr("не проверялась (источник — каталог без подписанного тега)"),
		UNAVAILABLE: tr("проверить нечем"),
		FAILED: tr("НЕ СХОДИТСЯ"),
	}.get(source.verification, source.verification)


def _apply(context, args, source: Source, release: dict, plan: Plan, installed: dict | None,
		   label: str, state: dict) -> int:
	root, config_dir = context.root, context.config_dir
	acquire_lock(config_dir)
	backup = None
	try:
		if load_installed(root) != installed:
			raise EngineError(tr("Хранилище изменилось, пока считался план, — повтори команду."))
		old_label = (installed or {}).get("release")
		backup = make_backup(config_dir, root, plan, old_label, label)
		try:
			written = _write_release(root, source.root, release, plan)
			touch_lock(config_dir)
			failure = self_check(root, written)
		except BaseException as error:
			# Что бы ни оборвало запись (диск, сигнал, битый выпуск) — файлы должны
			# вернуться: наполовину новый bin/ хуки следующей сессии запустили бы.
			restore_backup(backup, root)
			if isinstance(error, Exception) and not isinstance(error, EngineError):
				raise EngineError(tr("Запись выпуска оборвалась, прежний движок возвращён: "
									 "{error}", error=error)) from error
			raise
		if failure:
			restore_backup(backup, root)
			state.setdefault("bad", [])
			if source.tag and source.tag not in state["bad"]:
				state["bad"].append(source.tag)
			save_state(config_dir, state)
			raise EngineError(tr("Новый движок не прошёл самопроверку, прежний возвращён: "
								 "{failure}", failure=failure))
		state["installed"] = label
		if source.tag in state.get("bad", []):
			state["bad"].remove(source.tag)  # поставили явным --tag и всё работает
		save_state(config_dir, state)
		committed = pushed = None
		if not args.no_commit:
			touch_lock(config_dir)
			committed, pushed = _commit(context, plan, label)
			journal = _read_json(backup / "journal.json")
			journal["committed"] = bool(committed)
			_write_bytes(backup / "journal.json",
						 (json.dumps(journal, indent=2, ensure_ascii=False) + "\n").encode("utf-8"))
	finally:
		release_lock(config_dir)
	_print_result(plan, label, backup, committed, pushed, args.no_commit)
	return 0


def _write_release(root: Path, source: Path, release: dict, plan: Plan) -> list[str]:
	written = []
	for rel in plan.writes():
		_write_bytes(root / rel, (source / rel).read_bytes(), mode_from=source / rel)
		written.append(rel)
	for rel in plan.remove:
		(root / rel).unlink(missing_ok=True)
	for name, lines in plan.merge.items():
		path = root / name
		try:
			text = path.read_text(encoding="utf-8")
		except OSError:
			text = ""
		if text and not text.endswith("\n"):
			text += "\n"
		_write_bytes(path, (text + "\n".join(lines) + "\n").encode("utf-8"))
		written.append(name)
	# Манифест — последним: пока он не лёг, прежний выпуск считается установленным.
	manifest_target = root / manifest_lib.MANIFEST_PATH
	_write_bytes(manifest_target, (source / manifest_lib.MANIFEST_PATH).read_bytes())
	written.append(manifest_lib.MANIFEST_PATH)
	return written


def _commit(context, plan: Plan, label: str) -> tuple[bool, str | None]:
	"""Коммит ТОЛЬКО затронутых путей (синхронизация правки движка не коммитит)."""
	paths = sorted(set(plan.touched() + [manifest_lib.MANIFEST_PATH]))
	for rel in paths:
		added = context.git.run("add", "--", rel)
		# Удалённый файл мог не отслеживаться — это не беда; а записанный обязан встать в индекс.
		if not added.ok and (context.root / rel).exists():
			return False, added.err or added.out
	result = context.git.commit_paths(f"Update ccsync engine to {label}", *paths)
	if not result.ok:
		return False, result.err or result.out
	left = [rel for rel in context.git.uncommitted(*paths) if (context.root / rel).exists()]
	if left:
		return False, tr("не закоммичено: {files}", files=", ".join(left[:5]))
	pushed = context.git.push()
	return True, None if pushed.ok else (pushed.err or pushed.out)


def _print_result(plan: Plan, label: str, backup: Path | None, committed, pushed,
				  no_commit: bool) -> None:
	print(tr("Движок обновлён до {label}: записано {count} файлов.",
			 label=label, count=len(plan.writes()) + len(plan.merge) + 1))
	if plan.edited or plan.remove_kept:
		print(tr("Правленные тобой файлы не тронуты: {files}. Сравни с выпуском и "
				 "перенеси нужное сам.", files=_describe(plan.edited + plan.remove_kept)))
	if "setup-hooks.py" in plan.writes():
		print(tr("Изменился setup-hooks.py — запусти python3 setup-hooks.py (он идемпотентен)."))
	if backup:
		print(tr("Бэкап: {path}  (откат: engine rollback)", path=backup))
	if no_commit:
		print(tr("Коммит пропущен (--no-commit): закоммить пути движка сам."))
	elif committed is False:
		print(tr("Коммит не получился: {error}", error=pushed), file=sys.stderr)
	elif pushed:
		print(tr("Коммит сделан, но push не прошёл: {error}", error=pushed), file=sys.stderr)
	else:
		print(tr("Закоммичено и отправлено — остальные машины получат это обычным pull."))
	print(tr("Перезапусти Claude Code, чтобы хуки подхватили новый движок."))


def cmd_rollback(context, args) -> int:
	config_dir, root = context.config_dir, context.root
	backups = [path for path in list_backups(config_dir)
			   if not _read_json(path / "journal.json").get("rolled_back")]
	if args.list:
		for path in list_backups(config_dir):
			journal = _read_json(path / "journal.json")
			mark = " " + tr("(откачен)") if journal.get("rolled_back") else ""
			print(f"{path.name}  {journal.get('from')} → {journal.get('to')}{mark}")
		return 0
	if not backups:
		print(tr("Откатывать нечего: бэкапов обновлений нет."), file=sys.stderr)
		return 1
	target = backups[-1]
	journal = _read_json(target / "journal.json")
	if args.dry_run:
		print(tr("Откатили бы {name}: {old} ← {new}", name=target.name,
				 old=journal.get("from"), new=journal.get("to")))
		return 0
	acquire_lock(config_dir)
	try:
		restored = restore_backup(target, root)
	finally:
		release_lock(config_dir)
	state = load_state(config_dir)
	state["installed"] = journal.get("from")
	if journal.get("to") and journal["to"] not in state.setdefault("bad", []):
		state["bad"].append(journal["to"])
	save_state(config_dir, state)
	print(tr("Возвращён выпуск {old}: восстановлено файлов — {count}.",
			 old=journal.get("from") or "?", count=len(restored)))
	if journal.get("committed"):
		print(tr("Обновление уже было закоммичено: закоммить откат сам "
				 "(git add для путей движка) — иначе другие машины останутся на новом."))
	return 0


def cmd_status(context, args) -> int:
	root, config_dir = context.root, context.config_dir
	installed = load_installed(root)
	state = load_state(config_dir)
	if installed is None:
		print(tr("Манифеста выпуска нет (bin/ENGINE.json): движок поставлен старым "
				 "способом или это хранилище мейнтейнера."))
		return 0
	print(tr("Выпуск:      {label}", label=installed.get("release") or "—"))
	print(tr("Источник:    {url}", url=installed.get("source") or "—"))
	edited = [rel for rel, wanted in sorted(installed["files"].items())
			  if _hash_of(root / rel) != wanted]
	print(tr("Файлов:      {total}, отличаются от выпуска {count}",
			 total=len(installed["files"]), count=len(edited)) + (": " + _describe(edited) if edited else ""))
	if state.get("bad"):
		print(tr("Плохие выпуски: {tags}", tags=", ".join(state["bad"])))
	return 0


# --- проверка новых выпусков и политика ---------------------------------

def policy_path(root: Path) -> Path:
	return root / "tools" / POLICY_FILE


def load_mode(root: Path) -> str:
	try:
		data = tools.read_vault_json(policy_path(root), expect=dict)
	except tools.DamagedFile:
		return DEFAULT_MODE
	mode = (data or {}).get("mode", DEFAULT_MODE)
	return mode if mode in MODES else DEFAULT_MODE


def _iso(moment: float | None = None) -> str:
	return datetime.fromtimestamp(moment or time.time(), timezone.utc).isoformat(timespec="seconds")


def _age(stamp: str | None) -> float:
	"""Сколько секунд прошло с отметки; без отметки — «давно»."""
	try:
		return time.time() - datetime.fromisoformat(stamp).timestamp()
	except (TypeError, ValueError):
		return float("inf")


def check_for_release(context, force: bool = False) -> str | None:
	"""Раз в сутки спросить сеть о новом выпуске и сказать о нём один раз.

	Молчит при любой неудаче: нет сети, нет манифеста, хранилище мейнтейнера,
	режим off. Возвращает тег нового выпуска, если он есть.
	"""
	root, config_dir = context.root, context.config_dir
	if (root / "release-public.py").exists():
		return None
	installed = load_installed(root)
	mode = load_mode(root)
	if installed is None or mode == "off" or not installed.get("source"):
		return None
	state = load_state(config_dir)
	if not force and (_age(state.get("last_check")) < CHECK_EVERY
					  or _age(state.get("last_attempt")) < RETRY_AFTER):
		return None
	state["last_attempt"] = _iso()
	tags = list_remote_tags(installed["source"])
	if tags is None:
		save_state(config_dir, state)
		return None
	state["last_check"] = _iso()
	latest = latest_tag(tags, exclude=set(state.get("bad", [])))
	state["latest_seen"] = latest
	current = tag_key(installed.get("release") or "")
	newer = latest if latest and (current is None or tag_key(latest) > current) else None
	if newer and mode == "auto":
		save_state(config_dir, state)
		_auto_update(context, newer)
		return newer
	if newer and _age(state.setdefault("notified", {}).get(newer)) > REMIND_AFTER:
		held.add_notice(config_dir, tr(
			"Вышел выпуск движка ccsync {new} (у тебя {old}). Посмотреть, что нового, "
			"и поставить: /sync-update", new=newer, old=installed.get("release") or "?"))
		state["notified"][newer] = _iso()
	save_state(config_dir, state)
	return newer


def _auto_update(context, tag: str) -> None:
	"""Режим auto: поставить подписанный выпуск без вопросов и сообщить итог."""
	from types import SimpleNamespace
	args = SimpleNamespace(source=None, tag=tag, adopt=False, yes=True, dry_run=False,
						   no_commit=False, allow_unverified=False, strict=True)
	out, err = io.StringIO(), io.StringIO()
	try:
		with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
			code = cmd_update(context, args)
	except EngineError as error:
		held.add_notice(context.config_dir, tr("Автообновление движка до {tag} не удалось: {error}",
											   tag=tag, error=error))
		return
	if code == 0:
		held.add_notice(context.config_dir, tr(
			"Движок ccsync автоматически обновлён до {tag}. Перезапусти Claude Code, "
			"чтобы хуки подхватили новый. Откат: engine rollback", tag=tag))


def cmd_check(context, args) -> int:
	newer = check_for_release(context, force=args.force)
	if not context.quiet:
		if newer:
			print(tr("Есть новый выпуск: {tag}. Поставить: /sync-update", tag=newer))
		else:
			print(tr("Новых выпусков нет (или проверка пропущена: режим off, нет сети, не прошло суток)."))
	return 0


def cmd_mode(context, args) -> int:
	path = policy_path(context.root)
	if args.value is None:
		print(tr("Режим обновлений движка: {mode}", mode=load_mode(context.root)))
		return 0
	path.parent.mkdir(parents=True, exist_ok=True)
	_write_bytes(path, (json.dumps({"mode": args.value}, indent=2) + "\n").encode("utf-8"))
	print(tr("Режим обновлений движка: {mode} (уедет в хранилище с push tools)", mode=args.value))
	return 0


def run(context, args) -> int:
	try:
		if args.engine_command == "update":
			return cmd_update(context, args)
		if args.engine_command == "rollback":
			return cmd_rollback(context, args)
		if args.engine_command == "check":
			return cmd_check(context, args)
		if args.engine_command == "mode":
			return cmd_mode(context, args)
		return cmd_status(context, args)
	except EngineError as error:
		print(str(error), file=sys.stderr)
		return 1
