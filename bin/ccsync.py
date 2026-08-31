#!/usr/bin/env python3
"""ccsync — синхронизация Claude Code между машинами через git.

    ccsync init            завести паспорт машины и подключить её к хранилищу
    ccsync pull [что]      принять чужое и разложить по местам
    ccsync push [что]      отдать своё
    ccsync status          что расходится (ничего не меняет)
    ccsync ignore          не отправлять эту сессию в хранилище
    ccsync forget          забыть сессию везде: и здесь, и на других машинах
    ccsync bind <ключ>     привязать проект к пути на этой машине
    ccsync adopt           перевести локальные каталоги на симлинки в репо
    ccsync machines        список известных машин
    ccsync mcp             MCP-серверы и их принадлежность машинам

«что» — all (по умолчанию) | tools | memory | session
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from ccsync_lib import (conflicts, identity, ignore, memoryscope,
                        migrate_registry, scopes, sessions, tools)
from ccsync_lib.gitutil import Git
from ccsync_lib.paths import PathMapper
from ccsync_lib.vault import (RESERVED_SESSION_DIRS, Vault, find_candidates,
                              project_key_from_path)

DEBOUNCE_STAMP = ".ccsync-last-push"
MEMORY_LINK_NAME = "memory"


# --- инфраструктура -----------------------------------------------------

def vault_root(explicit: str | None) -> Path:
	if explicit:
		return Path(explicit).expanduser().resolve()
	env = os.environ.get("CCSYNC_VAULT")
	if env:
		return Path(env).expanduser().resolve()
	# Репозиторий — родитель каталога bin/, где лежит этот скрипт.
	return Path(__file__).resolve().parent.parent


class Context:
	"""Всё, что нужно любой команде: машина, хранилище, git, маппер путей."""

	def __init__(self, args) -> None:
		self.quiet = getattr(args, "quiet", False)
		self.root = vault_root(getattr(args, "vault", None))
		self.vault = Vault(self.root)
		self.git = Git(self.root)
		self.config_dir = identity.claude_config_dir()
		self.machine = identity.load_machine()

	def require_machine(self) -> identity.Machine:
		if self.machine is None:
			raise SystemExit(
				"Машина не настроена. Запусти сначала:  python3 bin/ccsync.py init"
			)
		return self.machine

	def mapper(self) -> PathMapper:
		machine = self.require_machine()
		return PathMapper(
			home=machine.home,
			project_paths=self.vault.paths_for_machine(machine.machine_id),
			target_os=machine.os,
		)

	def say(self, message: str) -> None:
		if not self.quiet:
			print(message)


# --- init ---------------------------------------------------------------

def cmd_init(context: Context, args) -> int:
	existing = identity.load_machine()
	if existing and not args.force:
		print(f"Машина уже настроена: {existing.describe()}")
		print("Перенастроить — с флагом --force")
		return 0

	suggested = args.id or identity.suggest_machine_id()
	machine_id = suggested
	if not args.yes and sys.stdin.isatty():
		answer = input(f"Идентификатор этой машины [{suggested}]: ").strip()
		machine_id = answer or suggested
	note = args.note
	if not args.yes and sys.stdin.isatty() and not note:
		note = input("Короткая заметка о машине (можно пусто): ").strip()

	machine = identity.build_machine(machine_id, note)
	path = identity.save_machine(machine)
	migrate_registry.migrate(context.vault)
	context.vault.register_machine(machine)
	print(f"Паспорт машины: {path}")
	print(f"  {machine.describe()}")

	secrets_path = identity.secrets_file_path()
	if not secrets_path.exists():
		secrets_path.write_text(
			"# Локальные секреты этой машины. В git не попадают (см. .gitignore).\n"
			"# Формат:  ИМЯ_ПЕРЕМЕННОЙ=значение\n"
			"# Пример:  GITHUB_PERSONAL_ACCESS_TOKEN=gho_xxx\n",
			encoding="utf-8",
		)
		print(f"Файл для секретов: {secrets_path}")
	return 0


# --- push ---------------------------------------------------------------

def cmd_push(context: Context, args) -> int:
	machine = context.require_machine()
	what = args.what
	if args.debounce and _debounced(context, args.debounce):
		context.say(f"[ccsync] push пропущен (дебаунс {args.debounce} с)")
		return 0

	if not _pull_and_settle(context):
		return 1

	mapper = context.mapper()
	done: list[str] = []
	done.extend(migrate_registry.migrate(context.vault))
	_refresh_machine_record(context, machine)

	if what in ("all", "tools"):
		done.extend(_push_tools(context, mapper))
	if what in ("all", "memory"):
		done.extend(_push_memory(context))
	if what in ("all", "session"):
		done.extend(_push_session(context, args, mapper))

	if not done:
		context.say("[ccsync] нечего отдавать")
		return 0

	message = f"sync from {machine.machine_id}: " + ", ".join(done)
	commit = context.git.commit_all(message)
	context.say(f"[ccsync] {commit.out or commit.err or 'коммит создан'}")
	push = context.git.push()
	if not push.ok:
		print(f"[ccsync] push не прошёл: {push.err or push.out}", file=sys.stderr)
		return 1
	_stamp_push(context)
	context.say("[ccsync] отправлено")
	return 0


def _pull_and_settle(context: Context) -> bool:
	"""Принять чужое и, если понадобится, уладить конфликт.

	Реестры разложены по машинам и конфликтовать не могут, но шаблоны в tools/
	общие: разошедшиеся машины встречаются на них. Такие конфликты сливаем по
	узлам JSON и продолжаем rebase. False — хранилище осталось в конфликте,
	работать с ним нельзя.
	"""
	result = context.git.pull()
	if result.ok:
		return True
	context.say(f"[ccsync] git pull: {result.err or result.out}")

	# Не rebase (нет сети, нет upstream) — не наша забота, работаем дальше.
	if not context.git.rebase_in_progress():
		return not context.git.conflicted_files()

	# Rebase встаёт на каждом конфликтующем коммите, поэтому идём по шагам.
	for _ in range(20):
		if not context.git.rebase_in_progress():
			return True
		resolved, remaining = conflicts.resolve_json_conflicts(context.git)
		if remaining or not resolved:
			break
		context.say("[ccsync] конфликт слит автоматически: " + ", ".join(resolved))
		context.git.rebase_continue()

	if not context.git.rebase_in_progress():
		return True
	stuck = context.git.conflicted_files()
	print("[ccsync] конфликт, который нельзя слить автоматически: "
		  + (", ".join(stuck) if stuck else "неизвестно где"), file=sys.stderr)
	print(f"[ccsync]   разбери его в {context.root}: git status; "
		  "вернуться назад — git rebase --abort", file=sys.stderr)
	return False


def _refresh_machine_record(context: Context, machine: identity.Machine) -> None:
	"""Поддерживать в реестре актуальную версию Claude Code этой машины."""
	current = identity.detect_claude_version()
	if current and current != machine.claude_version:
		machine.claude_version = current
		identity.save_machine(machine)
	context.vault.register_machine(machine)


def version_gap(context: Context, machine: identity.Machine) -> tuple[str, str] | None:
	"""Самая свежая версия среди машин, если она новее локальной."""
	newest_id, newest = "", machine.claude_version or ""
	for machine_id, data in context.vault.load_machines().items():
		other = str(data.get("claude_version") or "")
		if other and identity.version_tuple(other) > identity.version_tuple(newest):
			newest_id, newest = machine_id, other
	return (newest_id, newest) if newest_id else None


def _push_tools(context: Context, mapper: PathMapper) -> list[str]:
	vault, config = context.vault, context.config_dir
	machine = context.require_machine()
	done: list[str] = []
	settings_template = vault.tools_dir / "settings.template.json"
	python_exe, root = sys.executable, context.root
	if tools.export_settings(config, settings_template, mapper, python_exe, root):
		done.append("settings")
		# То, что мы только что отдали, становится базой для будущих слияний:
		# иначе следующий pull посчитал бы наши же правки «чужими».
		tools.remember_settings_base(settings_template, config, mapper, python_exe, root,
									 machine.os)

	# Там, где симлинки недоступны (Windows без Developer Mode), локальные папки —
	# копии, и правки в них надо забрать обратно руками, иначе новый скилл,
	# поставленный на такой машине, никуда не уедет.
	for name in tools.LINKED_DIRS:
		local = config / name
		if local.is_dir() and not local.is_symlink():
			copied = tools.merge_tree(local, vault.tools_dir / name)
			if copied:
				done.append(f"{name}({copied})")
	secret_keys = tools.export_mcp(
		config, vault.tools_dir / "mcp-servers.template.json", mapper,
		machine, vault.tools_dir / tools.MCP_SCOPES_FILE)
	done.append(f"mcp({len(secret_keys)} секретов замаскировано)" if secret_keys else "mcp")
	tools.export_plugins(config, vault.tools_dir / "plugins.json")
	done.append("plugins")
	for name in tools.COPIED_FILES:
		source = config / name
		if source.exists() and not source.is_symlink():
			target = vault.tools_dir / name
			target.parent.mkdir(parents=True, exist_ok=True)
			target.write_bytes(source.read_bytes())
	return done


def _push_memory(context: Context) -> list[str]:
	facts = memoryscope.load_facts(context.vault.memory_facts_dir)
	if not facts:
		return []
	context.vault.memory_index.parent.mkdir(parents=True, exist_ok=True)
	context.vault.memory_index.write_text(memoryscope.render_full_index(facts), encoding="utf-8")
	return [f"memory({len(facts)})"]


def _push_session(context: Context, args, mapper: PathMapper) -> list[str]:
	machine = context.require_machine()
	transcript, project_path, session_id = _locate_transcript(context, args)
	# Пометку проверяем до всего остального: она действует и тогда, когда файла
	# уже нет на месте, и стоит дешевле любой работы с диском.
	ignored = ignore.IgnoreList.for_config(context.config_dir)
	if ignored.is_ignored(session_id):
		context.say(f"[ccsync] сессия {session_id[:8]} помечена как не синхронизируемая — пропускаю")
		return []
	if transcript is None:
		context.say("[ccsync] активная сессия не найдена — пропускаю")
		return []
	# Ключ определяет каталог, где сессия была запущена, а не текущий рабочий:
	# Клод мог перейти в другой проект, но транскрипт остался на месте. Возьми
	# мы cwd — тот же диалог уехал бы в хранилище дважды, под двумя ключами.
	# Явный --project сильнее: раз человек указал проект, ему виднее.
	if not args.project:
		origin = sessions.origin_path(transcript)
		if origin:
			project_path = origin
	key = context.vault.ensure_project_key(project_path, machine.machine_id, home=machine.home)
	report = sessions.push_session(
		transcript,
		context.vault.session_dir_for(key),
		mapper,
		max_bytes=args.max_mb * 1024 * 1024,
	)
	for name, reason in report.skipped:
		print(f"[ccsync] ВНИМАНИЕ: {name} не отправлен — {reason}", file=sys.stderr)
	return [f"session {key}/{transcript.stem[:8]}"] if report.moved else []


def _locate_transcript(context: Context, args) -> tuple[Path | None, str, str]:
	"""Найти транскрипт: из хука, по id, по id текущей сессии, самый свежий.

	Возвращает (файл, путь проекта, id сессии). Id нужен даже когда файла нет:
	по нему проверяется пометка «не синхронизировать».
	"""
	if getattr(args, "from_hook", False):
		payload = _read_hook_payload()
		path = payload.get("transcript_path")
		if path and Path(path).exists():
			transcript = Path(path)
			return transcript, payload.get("cwd") or os.getcwd(), transcript.stem
	project_path = args.project or os.getcwd()
	directory = sessions.local_session_dir(context.config_dir, project_path)

	# Id живой сессии Claude Code кладёт в окружение — это точнее, чем «самый
	# свежий файл», когда в одном каталоге открыто несколько сессий. Но если
	# каталог проекта задан явно, окружение не при делах: речь о чужом проекте.
	wanted = args.session
	if not wanted and not args.project:
		wanted = ignore.current_session_id()

	if wanted:
		# Ищем по всем каталогам проектов и берём самую свежую копию. Смотреть
		# сперва в каталог текущего cwd нельзя: рядом может лежать застывший
		# дубль той же сессии, разложенный из хранилища, — и он перехватит.
		found = ignore.find_local_transcript(sessions.projects_root(context.config_dir), wanted)
		return found, project_path, wanted

	newest = sessions.newest_transcript(directory)
	return newest, project_path, (newest.stem if newest else "")


def _read_hook_payload() -> dict:
	if sys.stdin.isatty():
		return {}
	try:
		raw = sys.stdin.read()
		return json.loads(raw) if raw.strip() else {}
	except (json.JSONDecodeError, ValueError):
		return {}


def _debounced(context: Context, seconds: int) -> bool:
	stamp = context.config_dir / DEBOUNCE_STAMP
	if not stamp.exists():
		return False
	return (time.time() - stamp.stat().st_mtime) < seconds


def _stamp_push(context: Context) -> None:
	stamp = context.config_dir / DEBOUNCE_STAMP
	stamp.parent.mkdir(parents=True, exist_ok=True)
	stamp.write_text(str(int(time.time())), encoding="utf-8")


# --- pull ---------------------------------------------------------------

def cmd_pull(context: Context, args) -> int:
	machine = context.require_machine()
	what = args.what
	if not _pull_and_settle(context):
		return 1
	for change in migrate_registry.migrate(context.vault):
		context.say(f"[ccsync] {change}")

	mapper = context.mapper()
	if what in ("all", "tools"):
		_pull_tools(context, mapper, args)
	if what in ("all", "memory"):
		_pull_memory(context, machine)
	if what in ("all", "session"):
		_pull_sessions(context, args)
	return 0


def _pull_tools(context: Context, mapper: PathMapper, args) -> None:
	vault, config = context.vault, context.config_dir
	# На Windows симлинки доступны при Developer Mode; пробуем, откат — копия.
	allow_symlink = True

	for name in tools.LINKED_DIRS:
		source = vault.tools_dir / name
		if not source.exists():
			continue
		try:
			how = tools.link_or_copy(source, config / name, allow_symlink=allow_symlink)
			if how != "already":
				context.say(f"[ccsync] {name}: {how}")
		except FileExistsError:
			# Реальный каталог на месте симлинка — это работа для `adopt`.
			merged = tools.merge_tree(source, config / name)
			if merged:
				context.say(f"[ccsync] {name}: обновлено файлов {merged} (без симлинка; см. `ccsync adopt`)")

	for name in tools.COPIED_FILES:
		source = vault.tools_dir / name
		target = config / name
		if source.exists() and (not target.exists() or source.read_bytes() != target.read_bytes()):
			target.write_bytes(source.read_bytes())
			context.say(f"[ccsync] {name}: обновлён")

	try:
		if tools.apply_settings(vault.tools_dir / "settings.template.json", config, mapper,
								sys.executable, context.root, context.require_machine().os):
			context.say("[ccsync] settings.json обновлён (прежний — в settings.json.bak)")
	except ValueError as error:
		print(f"[ccsync] настройки не применены: {error}", file=sys.stderr)

	report = tools.apply_mcp(
		vault.tools_dir / "mcp-servers.template.json",
		mapper,
		identity.load_secrets(),
		config,
		machine=context.require_machine(),
		scopes_path=vault.tools_dir / tools.MCP_SCOPES_FILE,
		dry_run=args.dry_run,
	)
	if report.applied:
		context.say("[ccsync] MCP: " + ", ".join(report.applied))
	if report.removed:
		context.say("[ccsync] MCP убраны (не для этой машины): " + ", ".join(report.removed))
	for name in report.kept_modified:
		context.say(
			f"[ccsync] MCP {name} помечен как не для этой машины, но правлен здесь руками "
			f"— оставлен. Убрать: claude mcp remove {name} -s user")
	for name, problem in report.unusable:
		context.say(f"[ccsync] MCP {name} здесь не запустится: {problem}")
		context.say(
			f"[ccsync]   если он не нужен на этой машине: "
			f"{_ccsync_hint()} mcp scope {name} --not-here")
	if report.missing_secrets:
		print(
			"[ccsync] не хватает секретов в "
			f"{identity.secrets_file_path()}: {', '.join(report.missing_secrets)}",
			file=sys.stderr,
		)
	absent = tools.missing_plugins(vault.tools_dir / "plugins.json", config)
	if absent:
		context.say("[ccsync] плагины не установлены здесь: " + ", ".join(absent))
		context.say("[ccsync]   поставить: claude plugin install " + " ".join(absent))


def _pull_memory(context: Context, machine: identity.Machine) -> None:
	facts_source = context.vault.memory_facts_dir
	if not facts_source.exists():
		return
	memory_dir = sessions.local_session_dir(context.config_dir, machine.home) / MEMORY_LINK_NAME
	memory_dir.mkdir(parents=True, exist_ok=True)
	facts_link = memory_dir / "facts"
	try:
		tools.link_or_copy(facts_source, facts_link, allow_symlink=True)
	except FileExistsError:
		tools.merge_tree(facts_source, facts_link)
	# Сводный индекс — производный файл и в git не хранится (он пересобирается
	# из фактов, а общий файл давал конфликт при добавлении фактов на разных
	# машинах). Держим его свежим на каждой машине сами.
	context.vault.memory_index.parent.mkdir(parents=True, exist_ok=True)
	context.vault.memory_index.write_text(
		memoryscope.render_full_index(memoryscope.load_facts(facts_source)),
		encoding="utf-8")
	facts = memoryscope.load_facts(facts_source)
	index = memoryscope.render_local_index(facts, machine, link_prefix="facts/")
	(memory_dir / "MEMORY.md").write_text(index, encoding="utf-8")
	own = sum(1 for f in facts if f.applies_to(machine) and not f.is_global)
	shared = sum(1 for f in facts if f.is_global)
	context.say(f"[ccsync] память: своих {own}, общих {shared}, всего {len(facts)}")


def _pull_sessions(context: Context, args) -> None:
	machine = context.require_machine()
	mapper = context.mapper()
	_apply_tombstones(context)
	projects_root = sessions.projects_root(context.config_dir)
	stale = 0
	for directory in sorted(context.vault.sessions_dir.glob("*")):
		if not directory.is_dir() or directory.name in RESERVED_SESSION_DIRS:
			continue
		key = directory.name
		local_path = _resolve_or_bind(context, key, args)
		target = sessions.local_session_dir(context.config_dir, local_path)
		Path(local_path).mkdir(parents=True, exist_ok=True)
		report = sessions.pull_sessions(directory, target, mapper, local_path)
		if report.moved:
			bound = "" if mapper.is_bound(key) else "  ← проект НЕ привязан, файлов проекта нет"
			context.say(f"[ccsync] сессии {key}: {len(report.moved)} → {local_path}{bound}")

		# Проект мог сменить место: пока он не был привязан, сессии лежали в
		# ~/claude-sessions/<ключ>. После привязки прежняя раскладка осталась бы
		# рядом, и одна сессия двоилась бы в /resume — со старой, оборванной
		# копией. Убираем такие хвосты, но только когда ничего не теряем.
		alive = ignore.current_session_id()
		for transcript in sorted(directory.glob("*.jsonl")):
			removed, spared = sessions.drop_stale_copies(
				projects_root, transcript.stem, target / transcript.name,
				skip_session_id=alive,
			)
			stale += len(removed)
			for path in spared:
				context.say(f"[ccsync] прежняя копия {transcript.stem[:8]} оставлена: "
							f"в ней есть свои записи — {path}")
	_report_stale(context, stale)


def _report_stale(context: Context, stale: int) -> None:
	if stale:
		context.say(f"[ccsync] убрано прежних раскладок: {stale}")


def _resolve_or_bind(context: Context, key: str, args) -> str:
	"""Три ступени: привязка есть → автопоиск → вопрос → fallback."""
	machine = context.require_machine()
	bound = context.vault.paths_for_machine(machine.machine_id).get(key)
	if bound:
		return bound

	candidates = find_candidates(key, machine)
	if len(candidates) == 1 and not args.no_autobind:
		context.vault.bind(key, machine.machine_id, candidates[0])
		context.say(f"[ccsync] проект {key} найден и привязан: {candidates[0]}")
		return candidates[0]

	interactive = sys.stdin.isatty() and not context.quiet and not args.no_autobind
	if interactive:
		if candidates:
			context.say(f"[ccsync] кандидаты для {key}:")
			for index, path in enumerate(candidates, 1):
				context.say(f"   {index}) {path}")
		answer = input(f"Путь к проекту «{key}» на этой машине (Enter — пропустить): ").strip()
		if answer.isdigit() and candidates and 1 <= int(answer) <= len(candidates):
			answer = candidates[int(answer) - 1]
		if answer:
			context.vault.bind(key, machine.machine_id, answer)
			return answer

	fallback = str(Path(machine.home) / "claude-sessions" / key)
	return fallback


# --- не синхронизировать / забыть ---------------------------------------

def _vault_copy(context: Context, session_id: str) -> Path | None:
	"""Копия транскрипта в хранилище, в каком бы проекте она ни лежала."""
	return next(context.vault.sessions_dir.glob(f"*/{session_id}.jsonl"), None)


def _session_target(context: Context, args) -> tuple[str, Path | None, str]:
	"""Что помечаем: id сессии, её локальный файл и ключ проекта."""
	transcript, project_path, session_id = _locate_transcript(context, args)
	copy_in_vault = _vault_copy(context, session_id) if session_id else None
	# Ключ берём у уже уехавшей копии: он точнее, чем вычисленный из пути,
	# и не заводит лишнюю запись в project-map.json.
	key = copy_in_vault.parent.name if copy_in_vault else project_key_from_path(project_path)
	return session_id, transcript, key


def cmd_ignore(context: Context, args) -> int:
	context.require_machine()
	ignored = ignore.IgnoreList.for_config(context.config_dir)

	if args.list:
		if not ignored.entries:
			print("Помеченных сессий нет.")
			return 0
		print(f"Не синхронизируются ({len(ignored.entries)}):")
		for entry in sorted(ignored.entries.values(), key=lambda e: e.marked_at):
			print(f"  {entry.describe()}")
		return 0

	if args.undo:
		if ignored.unmark(args.undo):
			print(f"Пометка снята: {args.undo[:8]} — сессия снова синхронизируется.")
			return 0
		print(f"Сессия {args.undo[:8]} и не была помечена.", file=sys.stderr)
		return 1

	session_id, _transcript, key = _session_target(context, args)
	if not session_id:
		print("Не понял, какую сессию помечать. Укажи --session <id>.", file=sys.stderr)
		return 2

	ignored.mark(session_id, reason=args.reason, project_key=key)
	print(f"Сессия {session_id[:8]} ({key}) больше не уедет в хранилище.")

	copy_in_vault = _vault_copy(context, session_id)
	if copy_in_vault is not None:
		# Обычный случай для давно идущей сессии: Stop-хук успел отправить её
		# задолго до того, как ты решил её скрыть.
		print("ВНИМАНИЕ: копия уже лежит в хранилище — пометка её не удаляет.")
		print(f"  Убрать везде:  /sync-forget {session_id}")
	return 0


def cmd_forget(context: Context, args) -> int:
	machine = context.require_machine()
	ignored = ignore.IgnoreList.for_config(context.config_dir)
	session_id, transcript, key = _session_target(context, args)
	if not session_id:
		print("Не понял, какую сессию забывать. Укажи --session <id>.", file=sys.stderr)
		return 2

	if not args.yes and sys.stdin.isatty():
		answer = input(f"Забыть сессию {session_id[:8]} ({key}) — везде и навсегда? [y/N] ").strip().lower()
		if answer not in ("y", "yes", "д", "да"):
			print("Отменено.")
			return 0

	alive = session_id == ignore.current_session_id()
	delete_local = not args.keep_local

	# Пометку ставим ПЕРВЫМ делом, до любых операций с git: фоновый Stop-push,
	# стартовавший параллельно, должен увидеть её и не залить файл обратно.
	ignored.mark(session_id, reason=args.reason, project_key=key, forgotten=True,
				 delete_local_pending=delete_local and alive)

	if not _pull_and_settle(context):
		# Пометка уже стоит, так что отсюда сессия больше не уедет; удаление
		# в хранилище доделаем после того, как конфликт разобран.
		return 1

	done: list[str] = []
	copy_in_vault = _vault_copy(context, session_id)
	if copy_in_vault is not None:
		try:
			copy_in_vault.unlink()
			done.append("копия удалена из хранилища")
		except OSError as error:
			print(f"[ccsync] не удалось удалить {copy_in_vault}: {error}", file=sys.stderr)
	else:
		done.append("в хранилище копии не было")

	ignore.write_tombstone(context.vault.sessions_dir, session_id, key, machine.machine_id)
	done.append("отметка для других машин поставлена")

	# Копий может быть несколько: та, что ведёт Claude Code, и разложенные из
	# хранилища. Забыть — значит убрать все, иначе сессия всплывёт в /resume.
	copies = ignore.find_local_transcripts(sessions.projects_root(context.config_dir), session_id)
	if not delete_local:
		done.append("локальный файл оставлен (--keep-local)")
	elif not copies:
		done.append("локального файла нет")
	elif alive:
		# Удалять сейчас бесполезно: Claude Code пишет в этот файл и создаст
		# его заново. Уборку сделает хук при закрытии сессии.
		done.append("локальный файл будет удалён после закрытия этой сессии")
	else:
		removed = 0
		for copy in copies:
			try:
				copy.unlink()
				removed += 1
			except OSError as error:
				print(f"[ccsync] не удалось удалить {copy}: {error}", file=sys.stderr)
		if removed:
			done.append(f"локальных файлов удалено: {removed}")

	message = f"forget session {key}/{session_id[:8]} (from {machine.machine_id})"
	commit = context.git.commit_all(message)
	context.say(f"[ccsync] {commit.out or commit.err or 'коммит создан'}")
	push = context.git.push()
	if not push.ok:
		print(f"[ccsync] push не прошёл: {push.err or push.out}", file=sys.stderr)
		print("[ccsync] другие машины узнают об удалении после успешного push", file=sys.stderr)

	print(f"Сессия {session_id[:8]} ({key}) забыта:")
	for line in done:
		print(f"  · {line}")
	return 0 if push.ok else 1


def _apply_tombstones(context: Context) -> None:
	"""Снести локальные копии сессий, забытых на других машинах."""
	machine = context.require_machine()
	stones = ignore.load_tombstones(context.vault.sessions_dir)
	if not stones:
		return
	projects_root = sessions.projects_root(context.config_dir)
	alive = ignore.current_session_id()
	removed = 0
	for stone in stones:
		if stone.session_id == alive:
			# Забыли сессию, в которой прямо сейчас работаем: удалять её файл
			# бесполезно (Claude Code создаст его заново) и вредно — получится
			# огрызок. Уборку сделает хук при закрытии, флаг уже стоит.
			continue
		copies = ignore.find_local_transcripts(projects_root, stone.session_id)
		if copies:
			failed = False
			for transcript in copies:
				try:
					transcript.unlink()
				except OSError:
					# Не смогли удалить — не подтверждаем, попробуем позже.
					failed = True
			if failed:
				continue
			removed += len(copies)
		ignore.ack(context.vault.sessions_dir, stone.session_id, machine.machine_id)
	if removed:
		context.say(f"[ccsync] забытые сессии: удалено локальных копий {removed}")
	pruned = ignore.prune_tombstones(context.vault.sessions_dir, set(context.vault.load_machines()))
	if pruned:
		context.say(f"[ccsync] отметки об удалении отработаны всеми машинами: {len(pruned)}")

	# Подтверждения и вычищенные отметки коммитим здесь же. Оставлять их в
	# рабочем каталоге до ближайшего push нельзя: следующий `pull --rebase
	# --autostash` попытается наложить их на чужие изменения тех же файлов и
	# упрётся в конфликт на ровном месте.
	relative = str(ignore.tombstones_dir(context.vault.sessions_dir).relative_to(context.root))
	commit = context.git.commit_paths(f"tombstones: {machine.machine_id}", relative)
	if commit.ok and "нечего коммитить" not in commit.out:
		context.git.push()


# --- прочие команды -----------------------------------------------------

def cmd_status(context: Context, args) -> int:
	machine = context.require_machine()
	print(f"Машина:      {machine.describe()}")
	print(f"Хранилище:   {context.root}")
	print(f"Ветка:       {context.git.current_branch()}")
	dirty = context.git.dirty_paths()
	print(f"Не отдано:   {len(dirty)} файлов" + (": " + ", ".join(dirty[:5]) if dirty else ""))
	facts = memoryscope.load_facts(context.vault.memory_facts_dir)
	own = [f for f in facts if f.applies_to(machine)]
	print(f"Память:      всего {len(facts)}, применимо здесь {len(own)}")
	mapping = context.vault.paths_for_machine(machine.machine_id)
	print(f"Проекты:     привязано {len(mapping)}")
	unbound = context.vault.unbound_keys(machine.machine_id)
	if unbound:
		print(f"Не привязано: {', '.join(unbound)}")
	ignored = ignore.IgnoreList.for_config(context.config_dir)
	if ignored.entries:
		pending = len(ignored.pending_local_deletions())
		tail = f", ждут удаления локально {pending}" if pending else ""
		print(f"Игнор:       сессий {len(ignored.entries)}, из них забыто "
			  f"{ignored.forgotten_count}{tail}")
	stones = ignore.load_tombstones(context.vault.sessions_dir)
	if stones:
		known = set(context.vault.load_machines())
		waiting = [s for s in stones
				   if not known or not known.issubset(ignore.acked_by(context.vault.sessions_dir, s.session_id))]
		print(f"Отметки об удалении: {len(stones)}" + (f", ждут другие машины {len(waiting)}" if waiting else ""))
	template_path, scopes_path = _mcp_paths(context)
	servers = tools._read_json(template_path, {})
	if isinstance(servers, dict) and servers:
		scope_map = tools.load_mcp_scopes(scopes_path)
		foreign = [name for name in servers
				   if not scopes.matches(tools.mcp_scope_for(scope_map, name), machine)]
		line = f"MCP:         всего {len(servers)}, здесь {len(servers) - len(foreign)}"
		if foreign:
			line += f", не для этой машины {len(foreign)} ({', '.join(sorted(foreign))})"
		print(line)
	others = context.vault.other_machines(machine.machine_id)
	if others:
		print("Другие машины: " + ", ".join(others))
	print(f"Claude Code: {machine.claude_version or 'версия неизвестна'}")
	gap = version_gap(context, machine)
	if gap:
		print(f"  ВНИМАНИЕ: на {gap[0]} новее — {gap[1]}. Формат транскрипта меняется "
			  f"между релизами, обнови эту машину: claude update")
	return 0


def _ccsync_hint() -> str:
	"""Как позвать движок с этой машины — для подсказок в выводе."""
	script = Path(__file__).resolve()
	try:
		return f"python3 ~/{script.relative_to(Path.home()).as_posix()}"
	except ValueError:
		return f"python3 {script}"


def _mcp_paths(context: Context) -> tuple[Path, Path]:
	tools_dir = context.vault.tools_dir
	return tools_dir / "mcp-servers.template.json", tools_dir / tools.MCP_SCOPES_FILE


def cmd_mcp(context: Context, args) -> int:
	"""Список MCP-серверов с их скоупами; `mcp scope` — задать принадлежность."""
	machine = context.require_machine()
	template_path, scopes_path = _mcp_paths(context)
	scope_map = tools.load_mcp_scopes(scopes_path)
	wanted = tools._read_json(template_path, {})
	if not isinstance(wanted, dict) or not wanted:
		print("В хранилище нет ни одного MCP-сервера.")
		return 0
	if args.name:
		return _set_mcp_scope(context, args, machine, scope_map, wanted, scopes_path)

	mapper = context.mapper()
	local = tools.read_global_config(context.config_dir).get("mcpServers") or {}
	secrets = identity.load_secrets()
	width = max(len(name) for name in wanted)
	foreign = 0
	for name in sorted(wanted):
		scope = tools.mcp_scope_for(scope_map, name)
		here = scopes.matches(scope, machine)
		foreign += 0 if here else 1
		state = _mcp_state(name, wanted[name], local, mapper, secrets, here)
		print(f"  {name:<{width}}  {scopes.format(scope):<24}  "
			  f"здесь: {'да ' if here else 'нет'}  {state}")
	if foreign:
		print(f"\nНе для этой машины: {foreign}. "
			  f"Вернуть общим: {_ccsync_hint()} mcp scope <имя> --global")
	return 0


def _mcp_state(name, definition, local, mapper, secrets, here: bool) -> str:
	"""Короткое описание фактического состояния сервера на этой машине."""
	expanded = tools.convert_json_strings(definition, mapper.detokenize)
	rendered_text, _ = tools._fill_secrets(json.dumps(expanded, ensure_ascii=False), secrets)
	rendered = json.loads(rendered_text)
	if not here:
		if name not in local:
			return "убран локально"
		return "ЕСТЬ локально, правлен руками" if local[name] != rendered else "будет убран при pull"
	if name not in local:
		return "будет поставлен при pull"
	problem = tools.probe_runnable(rendered)
	return f"НЕ ЗАПУСТИТСЯ: {problem}" if problem else "ок"


def _set_mcp_scope(context, args, machine, scope_map, wanted, scopes_path) -> int:
	name = args.name
	if name not in wanted:
		print(f"Сервера {name} в хранилище нет. Известные: " + ", ".join(sorted(wanted)),
			  file=sys.stderr)
		return 1
	current = tools.mcp_scope_for(scope_map, name)
	if args.globally:
		new_scope = [scopes.SCOPE_GLOBAL]
	elif args.here:
		new_scope = [machine.machine_id]
	elif args.not_here:
		new_scope = scopes.without_machine(current, machine)
	elif args.value:
		new_scope = scopes.parse(args.value)
	else:
		print(f"{name}: {scopes.format(current)}")
		return 0
	unknown = _unknown_machines(context, new_scope)
	if unknown:
		print(f"ВНИМАНИЕ: в реестре нет машин: {', '.join(unknown)}. "
			  f"Опечатка? Известные: {', '.join(sorted(context.vault.load_machines()))}",
			  file=sys.stderr)
	scope_map[name] = new_scope
	tools.save_mcp_scopes(scopes_path, scope_map)
	print(f"{name}: {scopes.format(current)} → {scopes.format(new_scope)} "
		  f"({scopes.describe(new_scope, machine)})")
	print(f"Применить здесь: {_ccsync_hint()} pull tools")
	return 0


def _unknown_machines(context: Context, scope: list[str]) -> list[str]:
	"""Имена машин из scope, которых нет в реестре, — обычно это опечатка."""
	known = set(context.vault.load_machines())
	unknown: list[str] = []
	for raw in scope:
		entry = raw.strip().lstrip(scopes.NEGATE).strip()
		if not entry or entry == scopes.SCOPE_GLOBAL or entry.startswith(scopes.OS_PREFIX):
			continue
		if entry not in known:
			unknown.append(entry)
	return unknown


def cmd_bind(context: Context, args) -> int:
	machine = context.require_machine()
	path = args.path or os.getcwd()
	context.vault.bind(args.key, machine.machine_id, str(Path(path).resolve()))
	print(f"{args.key} → {path}  (машина {machine.machine_id})")
	return 0


def cmd_machines(context: Context, args) -> int:
	machine = identity.load_machine()
	if args.forget:
		return _forget_machine(context, machine, args.forget)
	for machine_id, data in sorted(context.vault.load_machines().items()):
		mark = "→" if machine and machine_id == machine.machine_id else " "
		note = f"  — {data.get('note')}" if data.get("note") else ""
		version = data.get("claude_version") or "версия неизвестна"
		print(f"{mark} {machine_id}: {data.get('distro', '?')}, Claude Code {version}, "
			  f"$HOME={data.get('home', '?')}{note}")
	return 0


def _forget_machine(context: Context, machine, target_id: str) -> int:
	"""Убрать из реестра машину, которой больше нет.

	Пока машина числится живой, отметки об удалённых сессиях ждут её
	подтверждения — то есть висят до самого срока в 180 дней.
	"""
	if machine and target_id == machine.machine_id:
		print("Это текущая машина — списывать её нечего.", file=sys.stderr)
		return 2
	known = context.vault.load_machines()
	if target_id not in known:
		print(f"Машина {target_id} в реестре не числится. Известные: "
			  + ", ".join(sorted(known)) or "нет ни одной", file=sys.stderr)
		return 1

	_pull_and_settle(context)
	removed = context.vault.forget_machine(target_id)
	if not removed:
		print(f"У машины {target_id} не оказалось файлов реестра.", file=sys.stderr)
		return 1
	commit = context.git.commit_all(f"forget machine {target_id}")
	context.say(f"[ccsync] {commit.out or commit.err or 'коммит создан'}")
	push = context.git.push()
	print(f"Машина {target_id} списана: " + ", ".join(removed))
	if not push.ok:
		print(f"[ccsync] push не прошёл: {push.err or push.out}", file=sys.stderr)
		return 1
	return 0


def cmd_adopt(context: Context, args) -> int:
	"""Перенести локальные каталоги в репозиторий и заменить их симлинками."""
	machine = context.require_machine()
	config = context.config_dir
	backup = config / "backups" / f"pre-ccsync-{time.strftime('%Y%m%d-%H%M%S')}"
	moved: list[str] = []

	for name in tools.LINKED_DIRS:
		local = config / name
		if not local.exists() or local.is_symlink():
			continue
		destination = context.vault.tools_dir / name
		tools.merge_tree(local, destination)
		_backup_and_replace(local, backup / name, destination, machine, moved, name)

	memory_local = sessions.local_session_dir(config, machine.home) / MEMORY_LINK_NAME
	if memory_local.is_dir():
		facts = context.vault.memory_facts_dir
		facts.mkdir(parents=True, exist_ok=True)
		for item in memory_local.glob("*.md"):
			if item.name.upper() == "MEMORY.MD" or item.is_symlink():
				continue
			target = facts / item.name
			if not target.exists():
				target.write_bytes(item.read_bytes())
				moved.append(f"memory/{item.name}")

	if backup.exists():
		print(f"Резервная копия: {backup}")
	print(f"Перенесено в хранилище: {len(moved)} элементов")
	return 0


def _backup_and_replace(local: Path, backup_path: Path, destination: Path,
						machine, moved: list[str], name: str) -> None:
	import shutil
	backup_path.parent.mkdir(parents=True, exist_ok=True)
	shutil.copytree(local, backup_path, dirs_exist_ok=True)
	shutil.rmtree(local)
	tools.link_or_copy(destination, local, allow_symlink=True)
	moved.append(name)


# --- разбор аргументов --------------------------------------------------

def _common_options() -> argparse.ArgumentParser:
	"""Опции, которые принимаются и до, и после имени подкоманды.

	Хуки пишут `ccsync.py pull --quiet`, а человек — `ccsync.py --quiet pull`;
	SUPPRESS не даёт подкоманде затереть значение, заданное перед ней.
	"""
	common = argparse.ArgumentParser(add_help=False)
	common.add_argument("--vault", default=argparse.SUPPRESS,
						help="путь к хранилищу (по умолчанию — родитель bin/)")
	common.add_argument("--quiet", action="store_true", default=argparse.SUPPRESS,
						help="только ошибки")
	return common


def build_parser() -> argparse.ArgumentParser:
	common = _common_options()
	parser = argparse.ArgumentParser(prog="ccsync", description=__doc__,
									 formatter_class=argparse.RawDescriptionHelpFormatter)
	parser.add_argument("--vault", help="путь к хранилищу (по умолчанию — родитель bin/)")
	parser.add_argument("--quiet", action="store_true", help="только ошибки")
	subparsers = parser.add_subparsers(dest="command", required=True, parser_class=lambda **kw: argparse.ArgumentParser(parents=[common], **kw))

	init = subparsers.add_parser("init", help="завести паспорт машины")
	init.add_argument("--id", help="идентификатор машины")
	init.add_argument("--note", default="", help="заметка о машине")
	init.add_argument("--force", action="store_true", help="перенастроить существующую")
	init.add_argument("--yes", action="store_true", help="без вопросов")
	init.set_defaults(func=cmd_init)

	for name, func, help_text in (("push", cmd_push, "отдать своё"), ("pull", cmd_pull, "принять чужое")):
		sub = subparsers.add_parser(name, help=help_text)
		sub.add_argument("what", nargs="?", default="all", choices=["all", "tools", "memory", "session"])
		sub.add_argument("--session", help="id сессии (по умолчанию — самая свежая в этом каталоге)")
		sub.add_argument("--project", help="путь проекта (по умолчанию — текущий каталог)")
		sub.add_argument("--dry-run", action="store_true", help="ничего не менять")
		sub.add_argument("--no-autobind", action="store_true", help="не привязывать проекты автоматически")
		sub.add_argument("--from-hook", action="store_true", help="взять данные сессии из stdin (хук)")
		sub.add_argument("--debounce", type=int, default=0, help="не чаще, чем раз в N секунд")
		sub.add_argument("--max-mb", type=int, default=50, help="порог размера транскрипта, МБ")
		sub.set_defaults(func=func)

	ignore_cmd = subparsers.add_parser("ignore", help="не синхронизировать эту сессию")
	ignore_cmd.add_argument("--session", help="id сессии (по умолчанию — текущая)")
	ignore_cmd.add_argument("--project", help="путь проекта (по умолчанию — текущий каталог)")
	ignore_cmd.add_argument("--reason", default="", help="зачем помечена")
	ignore_cmd.add_argument("--list", action="store_true", help="показать помеченные")
	ignore_cmd.add_argument("--undo", metavar="ID", help="снять пометку")
	ignore_cmd.set_defaults(func=cmd_ignore, from_hook=False)

	forget = subparsers.add_parser("forget", help="забыть сессию везде (необратимо)")
	forget.add_argument("--session", help="id сессии (по умолчанию — текущая)")
	forget.add_argument("--project", help="путь проекта (по умолчанию — текущий каталог)")
	forget.add_argument("--reason", default="", help="зачем забыта")
	forget.add_argument("--keep-local", action="store_true",
						help="оставить транскрипт на этой машине")
	forget.add_argument("--yes", action="store_true", help="без подтверждения")
	forget.set_defaults(func=cmd_forget, from_hook=False)

	status = subparsers.add_parser("status", help="что расходится")
	status.set_defaults(func=cmd_status)

	bind = subparsers.add_parser("bind", help="привязать проект к пути")
	bind.add_argument("key", help="ключ проекта")
	bind.add_argument("path", nargs="?", help="путь (по умолчанию — текущий каталог)")
	bind.set_defaults(func=cmd_bind)

	machines = subparsers.add_parser("machines", help="список машин")
	machines.add_argument("--forget", metavar="ID",
						  help="убрать из реестра машину, которой больше нет")
	machines.set_defaults(func=cmd_machines)

	adopt = subparsers.add_parser("adopt", help="перевести локальные каталоги на симлинки")
	adopt.set_defaults(func=cmd_adopt)

	mcp = subparsers.add_parser("mcp", help="MCP-серверы и их принадлежность машинам")
	mcp_sub = mcp.add_subparsers(dest="mcp_command")
	mcp.set_defaults(func=cmd_mcp, name=None, value=None,
					 here=False, not_here=False, globally=False)
	scope_cmd = mcp_sub.add_parser(
		"scope", help="показать или задать scope сервера",
		description="Без значения — показать текущий scope. Значения: global, "
					"<машина>, os:linux, !<машина> (везде, кроме неё).")
	scope_cmd.add_argument("name", help="имя MCP-сервера")
	scope_cmd.add_argument("value", nargs="*", help="элементы scope")
	scope_cmd.add_argument("--here", action="store_true",
						   help="только эта машина")
	scope_cmd.add_argument("--not-here", dest="not_here", action="store_true",
						   help="везде, кроме этой машины")
	scope_cmd.add_argument("--global", dest="globally", action="store_true",
						   help="вернуть в общие (значение по умолчанию)")
	scope_cmd.set_defaults(func=cmd_mcp)
	return parser


def main(argv: list[str] | None = None) -> int:
	identity.setup_console()
	parser = build_parser()
	args = parser.parse_args(argv)
	context = Context(args)
	try:
		return args.func(context, args)
	except KeyboardInterrupt:
		return 130
	except SystemExit as error:
		print(error, file=sys.stderr)
		return 2


if __name__ == "__main__":
	raise SystemExit(main())
