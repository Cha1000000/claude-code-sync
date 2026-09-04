#!/usr/bin/env python3
"""ccsync — синхронизация Claude Code между машинами через git.

    ccsync init            завести паспорт машины и подключить её к хранилищу
    ccsync pull [что]      принять чужое и разложить по местам
    ccsync push [что]      отдать своё
    ccsync status          что расходится (ничего не меняет)
    ccsync ignore          не отправлять эту сессию в хранилище
    ccsync forget          забыть сессию везде: и здесь, и на других машинах
    ccsync bind <ключ>     привязать проект к пути на этой машине
    ccsync branches        ветки сессии, если она открывается не тем разговором
    ccsync split <id>      разнести разошедшиеся ветки по отдельным сессиям
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

from ccsync_lib import (conflicts, hostfiles, identity, ignore, memoryscope,
                        migrate_registry, scopes, sessions, tools)
from ccsync_lib.gitutil import Git
from ccsync_lib.i18n import tr
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
		print(tr("Машина уже настроена: {machine}", machine=existing.describe()))
		print(tr("Перенастроить — с флагом --force"))
		return 0

	suggested = args.id or identity.suggest_machine_id()
	machine_id = suggested
	if not args.yes and sys.stdin.isatty():
		answer = input(tr("Идентификатор этой машины [{suggested}]: ",
						 suggested=suggested)).strip()
		machine_id = answer or suggested
	note = args.note
	if not args.yes and sys.stdin.isatty() and not note:
		note = input(tr("Короткая заметка о машине (можно пусто): ")).strip()

	machine = identity.build_machine(machine_id, note)
	path = identity.save_machine(machine)
	migrate_registry.migrate(context.vault)
	context.vault.register_machine(machine)
	print(tr("Паспорт машины: {path}", path=path))
	print(f"  {machine.describe()}")

	secrets_path = identity.secrets_file_path()
	if not secrets_path.exists():
		secrets_path.write_text(
			tr("# Локальные секреты этой машины. В git не попадают (см. .gitignore).\n"
			   "# Формат:  ИМЯ_ПЕРЕМЕННОЙ=значение\n"
			   "# Пример:  GITHUB_PERSONAL_ACCESS_TOKEN=gho_xxx\n"),
			encoding="utf-8",
		)
		print(tr("Файл для секретов: {path}", path=secrets_path))
	return 0


# --- push ---------------------------------------------------------------

def cmd_push(context: Context, args) -> int:
	machine = context.require_machine()
	what = args.what
	if args.debounce and _debounced(context, args.debounce):
		context.say(tr("[ccsync] push пропущен (дебаунс {seconds} с)",
					   seconds=args.debounce))
		return 0

	before = context.git.head()
	if not _pull_and_settle(context):
		return 1
	_warn_about_lost_branches(context, before)

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
		context.say(tr("[ccsync] нечего отдавать"))
		return 0

	message = f"sync from {machine.machine_id}: " + ", ".join(done)
	commit = context.git.commit_all(message)
	context.say(f"[ccsync] {commit.out or commit.err or tr('коммит создан')}")
	push = context.git.push()
	if not push.ok:
		print(tr("[ccsync] push не прошёл: {error}", error=push.err or push.out),
			  file=sys.stderr)
		return 1
	_stamp_push(context)
	context.say(tr("[ccsync] отправлено"))
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
		context.say(tr("[ccsync] конфликт слит автоматически: {files}",
					   files=", ".join(resolved)))
		context.git.rebase_continue()

	if not context.git.rebase_in_progress():
		return True
	stuck = context.git.conflicted_files()
	print(tr("[ccsync] конфликт, который нельзя слить автоматически: {files}",
			 files=", ".join(stuck) if stuck else tr("неизвестно где")),
		  file=sys.stderr)
	print(tr("[ccsync]   разбери его в {root}: git status; "
			 "вернуться назад — git rebase --abort", root=context.root),
		  file=sys.stderr)
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
	sent = hostfiles.export(
		config.parent, config, vault.tools_dir / hostfiles.HOST_DIR_NAME,
		hostfiles.load_registry(vault.tools_dir / hostfiles.REGISTRY_FILE),
		mapper, machine)
	if sent:
		done.append(f"host({len(sent)})")
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
		context.say(tr("[ccsync] сессия {id} помечена как не синхронизируемая — пропускаю",
					   id=session_id[:8]))
		return []
	if transcript is None:
		context.say(tr("[ccsync] активная сессия не найдена — пропускаю"))
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
	if ignored.is_project_ignored(key):
		context.say(tr("[ccsync] проект {key} не синхронизируется — пропускаю", key=key))
		return []
	report = sessions.push_session(
		transcript,
		context.vault.session_dir_for(key),
		mapper,
		max_bytes=args.max_mb * 1024 * 1024,
	)
	for name, reason in report.skipped:
		print(tr("[ccsync] ВНИМАНИЕ: {name} не отправлен — {reason}",
				 name=name, reason=reason), file=sys.stderr)
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
	before = context.git.head()
	if not _pull_and_settle(context):
		return 1
	_warn_about_lost_branches(context, before)
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
				context.say(tr("[ccsync] {name}: обновлено файлов {count} "
							   "(без симлинка; см. `ccsync adopt`)",
							   name=name, count=merged))

	for name in tools.COPIED_FILES:
		source = vault.tools_dir / name
		target = config / name
		if source.exists() and (not target.exists() or source.read_bytes() != target.read_bytes()):
			target.write_bytes(source.read_bytes())
			context.say(tr("[ccsync] {name}: обновлён", name=name))

	try:
		if tools.apply_settings(vault.tools_dir / "settings.template.json", config, mapper,
								sys.executable, context.root, context.require_machine().os):
			context.say(tr("[ccsync] settings.json обновлён "
						   "(прежний — в settings.json.bak)"))
	except ValueError as error:
		print(tr("[ccsync] настройки не применены: {error}", error=error),
			  file=sys.stderr)

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
		context.say(tr("[ccsync] MCP убраны (не для этой машины): {names}",
					   names=", ".join(report.removed)))
	for name in report.kept_modified:
		context.say(tr(
			"[ccsync] MCP {name} помечен как не для этой машины, но правлен здесь руками "
			"— оставлен. Убрать: claude mcp remove {name} -s user", name=name))
	for name, problem in report.unusable:
		context.say(tr("[ccsync] MCP {name} здесь не запустится: {problem}",
					   name=name, problem=problem))
		context.say(tr(
			"[ccsync]   если он не нужен на этой машине: "
			"{command} mcp scope {name} --not-here",
			command=_ccsync_hint(), name=name))
	if report.missing_secrets:
		print(tr("[ccsync] не хватает секретов в {path}: {names}",
				 path=identity.secrets_file_path(),
				 names=", ".join(report.missing_secrets)),
			  file=sys.stderr)
	absent = tools.missing_plugins(vault.tools_dir / "plugins.json", config)
	if absent:
		context.say(tr("[ccsync] плагины не установлены здесь: {names}",
					   names=", ".join(absent)))
		context.say(tr("[ccsync]   поставить: claude plugin install {names}",
					   names=" ".join(absent)))

	host = hostfiles.apply(
		config.parent, config, vault.tools_dir / hostfiles.HOST_DIR_NAME,
		hostfiles.load_registry(vault.tools_dir / hostfiles.REGISTRY_FILE),
		mapper, context.require_machine(), dry_run=args.dry_run)
	if host.applied:
		context.say(tr("[ccsync] обвязка: {names}", names=", ".join(host.applied)))
	if host.enabled:
		context.say(tr("[ccsync] юниты включены: {names}", names=", ".join(host.enabled)))
	for key in host.kept_modified:
		context.say(tr(
			"[ccsync] {key} правлен здесь руками — оставлен как есть. "
			"Отдать свою версию: {command} push tools", key=key, command=_ccsync_hint()))
	for name, problem in host.failed:
		context.say(tr("[ccsync] юнит {name} не включился: {problem}",
					   name=name, problem=problem))
		context.say(tr("[ccsync]   включить вручную: systemctl --user enable --now {name}",
					   name=name))


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
	context.say(tr("[ccsync] память: своих {own}, общих {shared}, всего {total}",
				   own=own, shared=shared, total=len(facts)))


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
		local_path, is_bound = _resolve_or_bind(context, key, args)
		target = sessions.local_session_dir(context.config_dir, local_path)
		Path(local_path).mkdir(parents=True, exist_ok=True)
		report = sessions.pull_sessions(directory, target, mapper, local_path)
		if report.moved:
			# Спрашиваем не mapper: он собран до цикла и про привязку, сделанную
			# только что автопоиском, ещё не знает — вышло бы «найден и привязан»
			# и следом «НЕ привязан» про один и тот же проект.
			bound = "" if is_bound else tr(
				"  ← проект НЕ привязан, файлов проекта нет")
			context.say(tr("[ccsync] сессии {key}: {count} → {path}{bound}",
						   key=key, count=len(report.moved),
						   path=local_path, bound=bound))

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
				context.say(tr("[ccsync] прежняя копия {id} оставлена: "
							   "в ней есть свои записи — {path}",
							   id=transcript.stem[:8], path=path))
	_report_stale(context, stale)


def _warn_about_lost_branches(context: Context, before: str) -> None:
	"""Сказать, если слияние спрятало часть разговора.

	Транскрипты помечены `merge=union`: когда две машины продолжили одну сессию
	врозь, git складывает строки обеих версий, и записи не пропадают. Но читается
	сессия обратным обходом parentUuid от последней строки файла (проверено
	запуском `claude --resume`), поэтому одна из веток может стать недостижимой.

	Ловить это анализом самого файла бесполезно: недостижимые ветки есть почти
	в каждом длинном транскрипте — человек возвращался назад, и брошенные
	продолжения остались лежать. Значение имеет только изменение: была ветка
	видна до слияния и перестала после. Поэтому сравниваем две версии файла из
	git, а не гадаем по одной.
	"""
	changed = context.git.changed_since(before, "sessions")
	for relative in changed:
		if not relative.endswith(".jsonl"):
			continue
		old_text = context.git.file_at(before, relative)
		if old_text is None:
			continue  # файла раньше не было — терять нечего
		current = context.root / relative
		if not current.exists():
			continue
		lost = sessions.lost_after_merge(
			old_text, current.read_text(encoding="utf-8", errors="replace"))
		if lost < sessions.MIN_LOST_TO_WARN:
			# Мелочь: обычно это просто переписанный ход, а не потерянная работа.
			continue
		session_id = Path(relative).stem
		context.say(tr("[ccsync] в сессии {id} перестало читаться записей: {count}",
					   id=session_id[:8], count=lost))
		context.say(tr("[ccsync]   похоже, две машины продолжили её врозь; записи целы, "
					   "ветки видно так: {command} branches --session {id}",
					   command=_ccsync_hint(), id=session_id))


def _report_stale(context: Context, stale: int) -> None:
	if stale:
		context.say(tr("[ccsync] убрано прежних раскладок: {count}", count=stale))


def _resolve_or_bind(context: Context, key: str, args) -> tuple[str, bool]:
	"""Три ступени: привязка есть → автопоиск → вопрос → fallback.

	Возвращает путь и признак того, привязан ли проект: только fallback даёт
	каталог, рядом с которым нет файлов проекта.
	"""
	machine = context.require_machine()
	bound = context.vault.paths_for_machine(machine.machine_id).get(key)
	if bound:
		return bound, True

	candidates = find_candidates(key, machine)
	if len(candidates) == 1 and not args.no_autobind:
		context.vault.bind(key, machine.machine_id, candidates[0])
		context.say(tr("[ccsync] проект {key} найден и привязан: {path}",
					   key=key, path=candidates[0]))
		return candidates[0], True

	interactive = sys.stdin.isatty() and not context.quiet and not args.no_autobind
	if interactive:
		if candidates:
			context.say(tr("[ccsync] кандидаты для {key}:", key=key))
			for index, path in enumerate(candidates, 1):
				context.say(f"   {index}) {path}")
		answer = input(tr("Путь к проекту «{key}» на этой машине "
						  "(Enter — пропустить): ", key=key)).strip()
		if answer.isdigit() and candidates and 1 <= int(answer) <= len(candidates):
			answer = candidates[int(answer) - 1]
		if answer:
			context.vault.bind(key, machine.machine_id, answer)
			return answer, True

	fallback = str(Path(machine.home) / "claude-sessions" / key)
	return fallback, False


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


def _project_key_for_ignore(context: Context, args) -> str:
	"""Ключ проекта, который помечаем целиком: из привязки, иначе из пути."""
	path = str(Path(args.project or os.getcwd()).resolve())
	return context.mapper().project_key_for(path) or project_key_from_path(path)


def cmd_ignore(context: Context, args) -> int:
	context.require_machine()
	ignored = ignore.IgnoreList.for_config(context.config_dir)

	if args.list:
		if not ignored.entries and not ignored.projects:
			print(tr("Помеченных сессий нет."))
			return 0
		if ignored.projects:
			print(tr("Проекты целиком ({count}):", count=len(ignored.projects)))
			for key, reason in sorted(ignored.projects.items()):
				print(f"  {key}" + (f" — {reason}" if reason else ""))
		if ignored.entries:
			print(tr("Не синхронизируются ({count}):", count=len(ignored.entries)))
			for entry in sorted(ignored.entries.values(), key=lambda e: e.marked_at):
				print(f"  {entry.describe()}")
		return 0

	if args.undo:
		if ignored.unmark(args.undo):
			print(tr("Пометка снята: {id} — сессия снова синхронизируется.",
					 id=args.undo[:8]))
			return 0
		if ignored.unignore_project(args.undo):
			print(tr("Пометка снята: проект {key} снова синхронизируется.", key=args.undo))
			return 0
		print(tr("Сессия {id} и не была помечена.", id=args.undo[:8]),
			  file=sys.stderr)
		return 1

	if args.project_wide:
		key = _project_key_for_ignore(context, args)
		ignored.ignore_project(key, reason=args.reason)
		print(tr("Проект {key} больше не уедет в хранилище — целиком, "
				 "включая будущие сессии.", key=key))
		existing = context.vault.session_dir_for(key)
		if existing.is_dir() and any(existing.glob("*.jsonl")):
			print(tr("ВНИМАНИЕ: копии уже лежат в хранилище — пометка их не удаляет."))
			print(tr("  Убрать каждую:  /sync-forget <id>"))
		return 0

	session_id, _transcript, key = _session_target(context, args)
	if not session_id:
		print(tr("Не понял, какую сессию помечать. Укажи --session <id>."),
			  file=sys.stderr)
		return 2

	ignored.mark(session_id, reason=args.reason, project_key=key)
	print(tr("Сессия {id} ({key}) больше не уедет в хранилище.",
			 id=session_id[:8], key=key))

	copy_in_vault = _vault_copy(context, session_id)
	if copy_in_vault is not None:
		# Обычный случай для давно идущей сессии: Stop-хук успел отправить её
		# задолго до того, как ты решил её скрыть.
		print(tr("ВНИМАНИЕ: копия уже лежит в хранилище — пометка её не удаляет."))
		print(tr("  Убрать везде:  /sync-forget {id}", id=session_id))
	return 0


def cmd_forget(context: Context, args) -> int:
	machine = context.require_machine()
	ignored = ignore.IgnoreList.for_config(context.config_dir)
	session_id, transcript, key = _session_target(context, args)
	if not session_id:
		print(tr("Не понял, какую сессию забывать. Укажи --session <id>."),
			  file=sys.stderr)
		return 2

	if not args.yes and sys.stdin.isatty():
		answer = input(tr("Забыть сессию {id} ({key}) — везде и навсегда? [y/N] ",
						  id=session_id[:8], key=key)).strip().lower()
		if answer not in ("y", "yes", "д", "да"):
			print(tr("Отменено."))
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
			done.append(tr("копия удалена из хранилища"))
		except OSError as error:
			print(tr("[ccsync] не удалось удалить {path}: {error}",
					 path=copy_in_vault, error=error), file=sys.stderr)
	else:
		done.append(tr("в хранилище копии не было"))

	ignore.write_tombstone(context.vault.sessions_dir, session_id, key, machine.machine_id)
	done.append(tr("отметка для других машин поставлена"))

	# Копий может быть несколько: та, что ведёт Claude Code, и разложенные из
	# хранилища. Забыть — значит убрать все, иначе сессия всплывёт в /resume.
	copies = ignore.find_local_transcripts(sessions.projects_root(context.config_dir), session_id)
	if not delete_local:
		done.append(tr("локальный файл оставлен (--keep-local)"))
	elif not copies:
		done.append(tr("локального файла нет"))
	elif alive:
		# Удалять сейчас бесполезно: Claude Code пишет в этот файл и создаст
		# его заново. Уборку сделает хук при закрытии сессии.
		done.append(tr("локальный файл будет удалён после закрытия этой сессии"))
	else:
		removed = 0
		for copy in copies:
			try:
				copy.unlink()
				removed += 1
			except OSError as error:
				print(tr("[ccsync] не удалось удалить {path}: {error}",
						 path=copy, error=error), file=sys.stderr)
		if removed:
			done.append(tr("локальных файлов удалено: {count}", count=removed))

	message = f"forget session {key}/{session_id[:8]} (from {machine.machine_id})"
	commit = context.git.commit_all(message)
	context.say(f"[ccsync] {commit.out or commit.err or tr('коммит создан')}")
	push = context.git.push()
	if not push.ok:
		print(tr("[ccsync] push не прошёл: {error}", error=push.err or push.out),
			  file=sys.stderr)
		print(tr("[ccsync] другие машины узнают об удалении после успешного push"),
			  file=sys.stderr)

	print(tr("Сессия {id} ({key}) забыта:", id=session_id[:8], key=key))
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
		context.say(tr("[ccsync] забытые сессии: удалено локальных копий {count}",
					   count=removed))
	pruned = ignore.prune_tombstones(context.vault.sessions_dir, set(context.vault.load_machines()))
	if pruned:
		context.say(tr("[ccsync] отметки об удалении отработаны всеми машинами: {count}",
					   count=len(pruned)))

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
	print(tr("Машина:      {machine}", machine=machine.describe()))
	print(tr("Хранилище:   {root}", root=context.root))
	print(tr("Ветка:       {branch}", branch=context.git.current_branch()))
	dirty = context.git.dirty_paths()
	print(tr("Не отдано:   {count} файлов", count=len(dirty))
		  + (": " + ", ".join(dirty[:5]) if dirty else ""))
	facts = memoryscope.load_facts(context.vault.memory_facts_dir)
	own = [f for f in facts if f.applies_to(machine)]
	print(tr("Память:      всего {total}, применимо здесь {here}",
			 total=len(facts), here=len(own)))
	mapping = context.vault.paths_for_machine(machine.machine_id)
	print(tr("Проекты:     привязано {count}", count=len(mapping)))
	unbound = context.vault.unbound_keys(machine.machine_id)
	if unbound:
		print(tr("Не привязано: {keys}", keys=", ".join(unbound)))
	ignored = ignore.IgnoreList.for_config(context.config_dir)
	if ignored.entries:
		pending = len(ignored.pending_local_deletions())
		tail = tr(", ждут удаления локально {count}", count=pending) if pending else ""
		print(tr("Игнор:       сессий {total}, из них забыто {forgotten}{tail}",
				 total=len(ignored.entries), forgotten=ignored.forgotten_count,
				 tail=tail))
	stones = ignore.load_tombstones(context.vault.sessions_dir)
	if stones:
		known = set(context.vault.load_machines())
		waiting = [s for s in stones
				   if not known or not known.issubset(ignore.acked_by(context.vault.sessions_dir, s.session_id))]
		print(tr("Отметки об удалении: {count}", count=len(stones))
			  + (tr(", ждут другие машины {count}", count=len(waiting)) if waiting else ""))
	template_path, scopes_path = _mcp_paths(context)
	servers = tools._read_json(template_path, {})
	if isinstance(servers, dict) and servers:
		scope_map = tools.load_mcp_scopes(scopes_path)
		foreign = [name for name in servers
				   if not scopes.matches(tools.mcp_scope_for(scope_map, name), machine)]
		line = tr("MCP:         всего {total}, здесь {here}",
				  total=len(servers), here=len(servers) - len(foreign))
		if foreign:
			line += tr(", не для этой машины {count} ({names})",
					   count=len(foreign), names=", ".join(sorted(foreign)))
		print(line)
	registry = hostfiles.load_registry(_host_registry_path(context))
	if registry:
		here = [key for key in registry if hostfiles.applies_here(registry, key, machine)]
		line = tr("Обвязка:     файлов {total}, здесь {here}",
				  total=len(registry), here=len(here))
		diverged = _host_diverged(context, here)
		if diverged:
			line += tr(", расходится {count} ({names})",
					   count=len(diverged), names=", ".join(sorted(diverged)))
		print(line)
	others = context.vault.other_machines(machine.machine_id)
	if others:
		print(tr("Другие машины: {names}", names=", ".join(others)))
	print(tr("Claude Code: {version}",
			 version=machine.claude_version or tr("версия неизвестна")))
	gap = version_gap(context, machine)
	if gap:
		print(tr("  ВНИМАНИЕ: на {machine} новее — {version}. Формат транскрипта "
				 "меняется между релизами, обнови эту машину: claude update",
				 machine=gap[0], version=gap[1]))
	return 0


def _host_diverged(context: Context, keys: list[str]) -> list[str]:
	"""Файлы обвязки, которые здесь не совпадают с хранилищем.

	Причина может быть любой — правка на месте, ещё не сделанный push, не
	применённый pull. Для сводки хватает самого факта расхождения: что именно
	с ним делать, скажет `host`.
	"""
	home = context.config_dir.parent
	host_dir = context.vault.tools_dir / hostfiles.HOST_DIR_NAME
	mapper = context.mapper()
	diverged: list[str] = []
	for key in keys:
		stored = hostfiles._read_text(hostfiles.vault_path(host_dir, key))
		current = hostfiles._read_text(hostfiles.local_path(home, key))
		if stored is None or current is None or mapper.detokenize(stored) != current:
			diverged.append(key)
	return diverged


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
		print(tr("В хранилище нет ни одного MCP-сервера."))
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
			  + tr("здесь: {answer}",
				   answer=tr("да ") if here else tr("нет"))
			  + f"  {state}")
	if foreign:
		print("\n" + tr("Не для этой машины: {count}. "
						"Вернуть общим: {command} mcp scope <имя> --global",
						count=foreign, command=_ccsync_hint()))
	return 0


def _mcp_state(name, definition, local, mapper, secrets, here: bool) -> str:
	"""Короткое описание фактического состояния сервера на этой машине."""
	expanded = tools.convert_json_strings(definition, mapper.detokenize)
	rendered_text, _ = tools._fill_secrets(json.dumps(expanded, ensure_ascii=False), secrets)
	rendered = json.loads(rendered_text)
	if not here:
		if name not in local:
			return tr("убран локально")
		return (tr("ЕСТЬ локально, правлен руками") if local[name] != rendered
				else tr("будет убран при pull"))
	if name not in local:
		return tr("будет поставлен при pull")
	problem = tools.probe_runnable(rendered)
	return tr("НЕ ЗАПУСТИТСЯ: {problem}", problem=problem) if problem else tr("ок")


def _set_mcp_scope(context, args, machine, scope_map, wanted, scopes_path) -> int:
	name = args.name
	if name not in wanted:
		print(tr("Сервера {name} в хранилище нет. Известные: {names}",
				 name=name, names=", ".join(sorted(wanted))), file=sys.stderr)
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
		print(tr("ВНИМАНИЕ: в реестре нет машин: {names}. "
				 "Опечатка? Известные: {known}",
				 names=", ".join(unknown),
				 known=", ".join(sorted(context.vault.load_machines()))),
			  file=sys.stderr)
	scope_map[name] = new_scope
	tools.save_mcp_scopes(scopes_path, scope_map)
	print(f"{name}: {scopes.format(current)} → {scopes.format(new_scope)} "
		  f"({scopes.describe(new_scope, machine)})")
	print(tr("Применить здесь: {command} pull tools", command=_ccsync_hint()))
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


def _host_registry_path(context: Context) -> Path:
	return context.vault.tools_dir / hostfiles.REGISTRY_FILE


def cmd_host(context: Context, args) -> int:
	"""Скрипты и юниты claude-обвязки: что возится и куда применимо."""
	machine = context.require_machine()
	registry_path = _host_registry_path(context)
	registry = hostfiles.load_registry(registry_path)

	if getattr(args, "add", None):
		return _add_host_file(context, args, machine, registry, registry_path)
	if args.name:
		return _set_host_scope(context, args, machine, registry, registry_path)
	if not registry:
		print(tr("В хранилище нет ни одного файла обвязки. "
				 "Добавить: {command} host add ~/.local/bin/имя",
				 command=_ccsync_hint()))
		return 0

	home = context.config_dir.parent
	host_dir = context.vault.tools_dir / hostfiles.HOST_DIR_NAME
	width = max(len(key) for key in registry)
	foreign = 0
	for key in sorted(registry):
		scope = hostfiles.scope_for(registry, key)
		here = hostfiles.applies_here(registry, key, machine)
		foreign += 0 if here else 1
		print(f"  {key:<{width}}  {scopes.format(scope):<24}  "
			  + tr("здесь: {answer}", answer=tr("да ") if here else tr("нет"))
			  + f"  {_host_state(context, home, host_dir, key, here)}")
	if foreign:
		print("\n" + tr("Не для этой машины: {count}. "
						"Вернуть общим: {command} host scope <имя> --global",
						count=foreign, command=_ccsync_hint()))
	return 0


def _host_state(context: Context, home: Path, host_dir: Path, key: str, here: bool) -> str:
	"""Короткое описание фактического состояния файла на этой машине."""
	stored = hostfiles._read_text(hostfiles.vault_path(host_dir, key))
	target = hostfiles.local_path(home, key)
	current = hostfiles._read_text(target)
	if not here:
		if current is None:
			return tr("нет здесь")
		return tr("ЕСТЬ локально (scope не для этой машины)")
	if stored is None:
		return tr("ещё не отдан — уедет при push")
	rendered = context.mapper().detokenize(stored)
	if current is None:
		return tr("будет положен при pull")
	if current != rendered:
		base = hostfiles._read_text(hostfiles._base_path(context.config_dir, key))
		if base is not None and current != base:
			return tr("правлен здесь руками")
		return tr("будет обновлён при pull")
	if hostfiles.category_of(key) == "systemd" and hostfiles.wants_enable(rendered):
		return tr("ок, юнит")
	return tr("ок")


def _add_host_file(context: Context, args, machine, registry, registry_path) -> int:
	"""Взять файл под синхронизацию, определив категорию по его расположению."""
	home = context.config_dir.parent
	path = Path(args.add).expanduser()
	if not path.is_absolute():
		path = Path(os.getcwd()) / path
	path = path.resolve()
	if not path.is_file():
		print(tr("Файла {path} нет.", path=path), file=sys.stderr)
		return 1

	key = None
	for category, relative in hostfiles.CATEGORIES.items():
		root = (home / relative).resolve()
		try:
			key = f"{category}/{path.relative_to(root).as_posix()}"
			break
		except ValueError:
			continue
	if key is None:
		print(tr("{path} лежит вне известных каталогов. Ожидается один из: {dirs}",
				 path=path, dirs=", ".join(f"~/{d}" for d in hostfiles.CATEGORIES.values())),
			  file=sys.stderr)
		return 1

	if key in registry:
		print(tr("{key} уже синхронизируется ({scope})",
				 key=key, scope=scopes.format(hostfiles.scope_for(registry, key))))
		return 0
	# По умолчанию — только эта ОС: обвязка почти всегда завязана на неё, а
	# ошибочный `global` разнёс бы неработающий файл по всем машинам.
	scope = [scopes.SCOPE_GLOBAL] if args.globally else [scopes.OS_PREFIX + machine.os]
	registry[key] = scope
	hostfiles.save_registry(registry_path, registry)
	print(tr("{key}: под синхронизацией ({scope})", key=key, scope=scopes.format(scope)))
	print(tr("Отдать в хранилище: {command} push tools", command=_ccsync_hint()))
	return 0


def _set_host_scope(context: Context, args, machine, registry, registry_path) -> int:
	name = args.name
	if name not in registry:
		print(tr("Файла {name} в реестре нет. Известные: {names}",
				 name=name, names=", ".join(sorted(registry)) or "—"), file=sys.stderr)
		return 1
	current = hostfiles.scope_for(registry, name)
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
		print(tr("ВНИМАНИЕ: в реестре нет машин: {names}. "
				 "Опечатка? Известные: {known}",
				 names=", ".join(unknown),
				 known=", ".join(sorted(context.vault.load_machines()))),
			  file=sys.stderr)
	registry[name] = new_scope
	hostfiles.save_registry(registry_path, registry)
	print(f"{name}: {scopes.format(current)} → {scopes.format(new_scope)} "
		  f"({scopes.describe(new_scope, machine)})")
	print(tr("Применить здесь: {command} pull tools", command=_ccsync_hint()))
	return 0


def cmd_bind(context: Context, args) -> int:
	machine = context.require_machine()
	path = args.path or os.getcwd()
	context.vault.bind(args.key, machine.machine_id, str(Path(path).resolve()))
	print(tr("{key} → {path}  (машина {machine})",
			 key=args.key, path=path, machine=machine.machine_id))
	return 0


def cmd_machines(context: Context, args) -> int:
	machine = identity.load_machine()
	if args.forget:
		return _forget_machine(context, machine, args.forget)
	for machine_id, data in sorted(context.vault.load_machines().items()):
		mark = "→" if machine and machine_id == machine.machine_id else " "
		note = f"  — {data.get('note')}" if data.get("note") else ""
		version = data.get("claude_version") or tr("версия неизвестна")
		print(tr("{mark} {id}: {distro}, Claude Code {version}, $HOME={home}{note}",
				 mark=mark, id=machine_id, distro=data.get("distro", "?"),
				 version=version, home=data.get("home", "?"), note=note))
	return 0


def _forget_machine(context: Context, machine, target_id: str) -> int:
	"""Убрать из реестра машину, которой больше нет.

	Пока машина числится живой, отметки об удалённых сессиях ждут её
	подтверждения — то есть висят до самого срока в 180 дней.
	"""
	if machine and target_id == machine.machine_id:
		print(tr("Это текущая машина — списывать её нечего."), file=sys.stderr)
		return 2
	known = context.vault.load_machines()
	if target_id not in known:
		print(tr("Машина {id} в реестре не числится. Известные: {known}",
				 id=target_id,
				 known=", ".join(sorted(known)) or tr("нет ни одной")),
			  file=sys.stderr)
		return 1

	_pull_and_settle(context)
	removed = context.vault.forget_machine(target_id)
	if not removed:
		print(tr("У машины {id} не оказалось файлов реестра.", id=target_id),
			  file=sys.stderr)
		return 1
	commit = context.git.commit_all(f"forget machine {target_id}")
	context.say(f"[ccsync] {commit.out or commit.err or tr('коммит создан')}")
	push = context.git.push()
	print(tr("Машина {id} списана: {files}",
			 id=target_id, files=", ".join(removed)))
	if not push.ok:
		print(tr("[ccsync] push не прошёл: {error}", error=push.err or push.out),
			  file=sys.stderr)
		return 1
	return 0


def cmd_branches(context: Context, args) -> int:
	"""Показать сессии, в которых разошлись ветки."""
	context.require_machine()
	root = sessions.projects_root(context.config_dir)
	targets = ignore.find_local_transcripts(root, args.session)
	if not targets:
		print(tr("Сессия {id} на этой машине не найдена.", id=args.session[:8]),
			  file=sys.stderr)
		return 1
	shown = 0
	for path in targets:
		chain = sessions.read_chain(path)
		if not chain.forks:
			continue
		shown += 1
		found = sessions.branches(chain)
		last = chain.order[-1] if chain.order else None
		print(tr("{id} — веток: {count}  ({path})",
				 id=path.stem[:8], count=len(found), path=path))
		for index, branch in enumerate(found, 1):
			mark = tr(" ← читается сейчас") if last in branch.uuids else ""
			print(tr("  {index}) записей {size}, последняя {when}{mark}",
					 index=index, size=branch.size,
					 when=branch.last_timestamp or "?", mark=mark))
			if branch.preview:
				print(f"     «{branch.preview}»")
	if not shown:
		print(tr("В этой сессии ветки не расходятся."))
		return 0
	print(tr("Разнести ветки по отдельным сессиям: {command} split <id>",
			 command=_ccsync_hint()))
	return 0


def cmd_split(context: Context, args) -> int:
	"""Разнести разошедшиеся ветки сессии по отдельным сессиям."""
	context.require_machine()
	root = sessions.projects_root(context.config_dir)
	found = ignore.find_local_transcripts(root, args.session)
	if not found:
		print(tr("Сессия {id} на этой машине не найдена.", id=args.session[:8]),
			  file=sys.stderr)
		return 1
	transcript = found[0]
	chain = sessions.read_chain(transcript)
	if not chain.forks:
		print(tr("В сессии {id} ветки не расходятся — делить нечего.",
				 id=args.session[:8]))
		return 0
	if args.session == ignore.current_session_id():
		print(tr("Это текущая сессия: Claude Code пишет в неё прямо сейчас. "
				 "Закрой её и повтори."), file=sys.stderr)
		return 2
	if args.dry_run:
		for branch in sessions.branches(chain):
			print(tr("  ветка: записей {size}, последняя {when}",
					 size=branch.size, when=branch.last_timestamp or "?"))
		print(tr("--dry-run: ничего не записано."))
		return 0

	backup = context.config_dir / "backups" / f"split-{time.strftime('%Y%m%d-%H%M%S')}"
	results = sessions.split_transcript(transcript, backup)
	if not results:
		print(tr("Делить нечего."))
		return 0
	print(tr("Резервная копия: {path}", path=backup / transcript.name))
	for result in results:
		print(tr("Вынесено в отдельную сессию {id}: записей {count}",
				 id=result.session_id[:8], count=result.records))
		if result.preview:
			print(f"  «{result.preview}»")
	print(tr("Открыть: claude --resume <id> из каталога этого проекта"))
	print(tr("Отдать остальным машинам: {command} push session", command=_ccsync_hint()))
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
		print(tr("Резервная копия: {path}", path=backup))
	print(tr("Перенесено в хранилище: {count} элементов", count=len(moved)))
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
						help=tr("путь к хранилищу (по умолчанию — родитель bin/)"))
	common.add_argument("--quiet", action="store_true", default=argparse.SUPPRESS,
						help=tr("только ошибки"))
	return common


def build_parser() -> argparse.ArgumentParser:
	common = _common_options()
	parser = argparse.ArgumentParser(prog="ccsync", description=tr(__doc__),
									 formatter_class=argparse.RawDescriptionHelpFormatter)
	parser.add_argument("--vault", help=tr("путь к хранилищу (по умолчанию — родитель bin/)"))
	parser.add_argument("--quiet", action="store_true", help=tr("только ошибки"))
	subparsers = parser.add_subparsers(dest="command", required=True, parser_class=lambda **kw: argparse.ArgumentParser(parents=[common], **kw))

	init = subparsers.add_parser("init", help=tr("завести паспорт машины"))
	init.add_argument("--id", help=tr("идентификатор машины"))
	init.add_argument("--note", default="", help=tr("заметка о машине"))
	init.add_argument("--force", action="store_true", help=tr("перенастроить существующую"))
	init.add_argument("--yes", action="store_true", help=tr("без вопросов"))
	init.set_defaults(func=cmd_init)

	for name, func, help_text in (("push", cmd_push, tr("отдать своё")),
								  ("pull", cmd_pull, tr("принять чужое"))):
		sub = subparsers.add_parser(name, help=help_text)
		sub.add_argument("what", nargs="?", default="all", choices=["all", "tools", "memory", "session"])
		sub.add_argument("--session", help=tr("id сессии (по умолчанию — самая свежая в этом каталоге)"))
		sub.add_argument("--project", help=tr("путь проекта (по умолчанию — текущий каталог)"))
		sub.add_argument("--dry-run", action="store_true", help=tr("ничего не менять"))
		sub.add_argument("--no-autobind", action="store_true", help=tr("не привязывать проекты автоматически"))
		sub.add_argument("--from-hook", action="store_true", help=tr("взять данные сессии из stdin (хук)"))
		sub.add_argument("--debounce", type=int, default=0, help=tr("не чаще, чем раз в N секунд"))
		sub.add_argument("--max-mb", type=int, default=50, help=tr("порог размера транскрипта, МБ"))
		sub.set_defaults(func=func)

	ignore_cmd = subparsers.add_parser("ignore", help=tr("не синхронизировать эту сессию"))
	ignore_cmd.add_argument("--session", help=tr("id сессии (по умолчанию — текущая)"))
	ignore_cmd.add_argument("--project", help=tr("путь проекта (по умолчанию — текущий каталог)"))
	ignore_cmd.add_argument("--reason", default="", help=tr("зачем помечена"))
	ignore_cmd.add_argument("--list", action="store_true", help=tr("показать помеченные"))
	ignore_cmd.add_argument("--undo", metavar="ID", help=tr("снять пометку (id сессии или ключ проекта)"))
	ignore_cmd.add_argument("--project-wide", dest="project_wide", action="store_true",
							help=tr("пометить проект целиком, включая будущие сессии"))
	ignore_cmd.set_defaults(func=cmd_ignore, from_hook=False)

	forget = subparsers.add_parser("forget", help=tr("забыть сессию везде (необратимо)"))
	forget.add_argument("--session", help=tr("id сессии (по умолчанию — текущая)"))
	forget.add_argument("--project", help=tr("путь проекта (по умолчанию — текущий каталог)"))
	forget.add_argument("--reason", default="", help=tr("зачем забыта"))
	forget.add_argument("--keep-local", action="store_true",
						help=tr("оставить транскрипт на этой машине"))
	forget.add_argument("--yes", action="store_true", help=tr("без подтверждения"))
	forget.set_defaults(func=cmd_forget, from_hook=False)

	status = subparsers.add_parser("status", help=tr("что расходится"))
	status.set_defaults(func=cmd_status)

	bind = subparsers.add_parser("bind", help=tr("привязать проект к пути"))
	bind.add_argument("key", help=tr("ключ проекта"))
	bind.add_argument("path", nargs="?", help=tr("путь (по умолчанию — текущий каталог)"))
	bind.set_defaults(func=cmd_bind)

	machines = subparsers.add_parser("machines", help=tr("список машин"))
	machines.add_argument("--forget", metavar="ID",
						  help=tr("убрать из реестра машину, которой больше нет"))
	machines.set_defaults(func=cmd_machines)

	branches_cmd = subparsers.add_parser(
		"branches", help=tr("сессии, в которых разошлись ветки"))
	branches_cmd.add_argument("--session", required=True, help=tr("id сессии"))
	branches_cmd.set_defaults(func=cmd_branches)

	split_cmd = subparsers.add_parser(
		"split", help=tr("разнести разошедшиеся ветки по отдельным сессиям"))
	split_cmd.add_argument("session", help=tr("id сессии"))
	split_cmd.add_argument("--dry-run", action="store_true", help=tr("ничего не менять"))
	split_cmd.set_defaults(func=cmd_split)

	adopt = subparsers.add_parser("adopt", help=tr("перевести локальные каталоги на симлинки"))
	adopt.set_defaults(func=cmd_adopt)

	mcp = subparsers.add_parser("mcp", help=tr("MCP-серверы и их принадлежность машинам"))
	mcp_sub = mcp.add_subparsers(dest="mcp_command")
	mcp.set_defaults(func=cmd_mcp, name=None, value=None,
					 here=False, not_here=False, globally=False)
	scope_cmd = mcp_sub.add_parser(
		"scope", help=tr("показать или задать scope сервера"),
		description=tr("Без значения — показать текущий scope. Значения: global, "
					   "<машина>, os:linux, !<машина> (везде, кроме неё)."))
	scope_cmd.add_argument("name", help=tr("имя MCP-сервера"))
	scope_cmd.add_argument("value", nargs="*", help=tr("элементы scope"))
	scope_cmd.add_argument("--here", action="store_true",
						   help=tr("только эта машина"))
	scope_cmd.add_argument("--not-here", dest="not_here", action="store_true",
						   help=tr("везде, кроме этой машины"))
	scope_cmd.add_argument("--global", dest="globally", action="store_true",
						   help=tr("вернуть в общие (значение по умолчанию)"))
	scope_cmd.set_defaults(func=cmd_mcp)

	host = subparsers.add_parser(
		"host", help=tr("скрипты и systemd-юниты claude-обвязки"),
		description=tr("Файлы вне ~/.claude, которые обслуживают Claude Code: "
					   "~/.local/bin и ~/.config/systemd/user. Возится только то, "
					   "что перечислено явно."))
	host_sub = host.add_subparsers(dest="host_command")
	host.set_defaults(func=cmd_host, name=None, value=None, add=None,
					  here=False, not_here=False, globally=False)

	host_add = host_sub.add_parser("add", help=tr("взять файл под синхронизацию"))
	host_add.add_argument("add", metavar="path", help=tr("путь к файлу"))
	host_add.add_argument("--global", dest="globally", action="store_true",
						  help=tr("применим на любой ОС (по умолчанию — только текущая)"))
	host_add.set_defaults(func=cmd_host, name=None, value=None,
						  here=False, not_here=False)

	host_scope = host_sub.add_parser(
		"scope", help=tr("показать или задать scope файла"),
		description=tr("Без значения — показать текущий scope. Значения: global, "
					   "<машина>, os:linux, !<машина> (везде, кроме неё)."))
	host_scope.add_argument("name", help=tr("ключ файла, например bin/имя.sh"))
	host_scope.add_argument("value", nargs="*", help=tr("элементы scope"))
	host_scope.add_argument("--here", action="store_true",
							help=tr("только эта машина"))
	host_scope.add_argument("--not-here", dest="not_here", action="store_true",
							help=tr("везде, кроме этой машины"))
	host_scope.add_argument("--global", dest="globally", action="store_true",
							help=tr("вернуть в общие"))
	host_scope.set_defaults(func=cmd_host, add=None)
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
