"""Память со скоупами: что применимо на этой машине, а что — соседней.

Память общая на все машины, но факты неравноценны. «Основной язык проекта — Kotlin»
верно везде; «Docker установлен на рабочем ноутбуке» — только на одной машине.
Поэтому у факта есть scope, а локальный MEMORY.md собирается под текущую машину:
своё и общее — в основных разделах, чужое — свёрнутой строкой с пометкой
«не применять здесь». Файлы чужих фактов при этом лежат рядом и читаются,
если про них спросить прямо.

Формат scope в metadata разбирает общий модуль `scopes` — та же грамматика,
что и у MCP-серверов:
    scope: global                          — верно везде
    scope: os:linux                        — на любой машине с этой ОС
    scope: linux-desktop                 — только эта машина
    scope: [linux-desktop, work-laptop]  — перечисленные машины
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from . import scopes
from .identity import Machine
from .scopes import OS_PREFIX, SCOPE_GLOBAL  # noqa: F401 — прежние имена модуля


@dataclass
class Fact:
	path: Path
	name: str
	description: str = ""
	title: str = ""
	hook: str = ""
	fact_type: str = ""
	scope: list[str] = field(default_factory=lambda: [SCOPE_GLOBAL])

	@property
	def display_title(self) -> str:
		return self.title or self.name.replace("-", " ").capitalize()

	@property
	def display_hook(self) -> str:
		return self.hook or self.description

	def applies_to(self, machine: Machine) -> bool:
		return scopes.matches(self.scope, machine)

	@property
	def is_global(self) -> bool:
		return scopes.is_global(self.scope)


def parse_frontmatter(text: str) -> tuple[dict, str]:
	"""Минимальный разбор YAML-заголовка: скаляры, вложенность в 1 уровень, списки.

	Полноценный YAML не нужен и не гарантирован в окружении — формат фактов
	простой и стабильный, а лишняя зависимость на всех пяти машинах ни к чему.
	"""
	if not text.startswith("---"):
		return {}, text
	lines = text.splitlines()
	closing = next((i for i in range(1, len(lines)) if lines[i].strip() == "---"), None)
	if closing is None:
		return {}, text
	data: dict = {}
	current_section: str | None = None
	for raw in lines[1:closing]:
		if not raw.strip() or raw.lstrip().startswith("#"):
			continue
		indented = raw[:1] in (" ", "\t")
		if ":" not in raw:
			continue
		key, _, value = raw.partition(":")
		key, value = key.strip(), value.strip()
		if not indented:
			current_section = None
			if value == "":
				data[key] = {}
				current_section = key
			else:
				data[key] = _parse_scalar(value)
		elif current_section is not None and isinstance(data.get(current_section), dict):
			data[current_section][key] = _parse_scalar(value)
	return data, "\n".join(lines[closing + 1:]).lstrip("\n")


def _parse_scalar(value: str):
	value = value.strip()
	if value.startswith("[") and value.endswith("]"):
		inner = value[1:-1].strip()
		if not inner:
			return []
		return [item.strip().strip('"').strip("'") for item in inner.split(",")]
	if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
		return value[1:-1].replace('\\"', '"')
	return value


def parse_fact(path: Path) -> Fact:
	text = path.read_text(encoding="utf-8", errors="replace")
	meta, _ = parse_frontmatter(text)
	metadata = meta.get("metadata") if isinstance(meta.get("metadata"), dict) else {}
	scope = scopes.parse(metadata.get("scope")) or [SCOPE_GLOBAL]
	return Fact(
		path=path,
		name=str(meta.get("name") or path.stem),
		description=str(meta.get("description") or ""),
		title=str(metadata.get("index_title") or ""),
		hook=str(metadata.get("index_hook") or ""),
		fact_type=str(metadata.get("type") or ""),
		scope=scope,
	)


def load_facts(facts_dir: Path) -> list[Fact]:
	if not facts_dir.is_dir():
		return []
	return [
		parse_fact(path)
		for path in sorted(facts_dir.glob("*.md"))
		if path.name.upper() != "MEMORY.MD"
	]


def render_local_index(facts: list[Fact], machine: Machine, link_prefix: str = "") -> str:
	"""Собрать MEMORY.md под конкретную машину."""
	own: list[Fact] = []
	shared: list[Fact] = []
	foreign: list[Fact] = []
	for fact in facts:
		if fact.is_global:
			shared.append(fact)
		elif fact.applies_to(machine):
			own.append(fact)
		else:
			foreign.append(fact)

	lines = [
		f"# Memory index — машина: {machine.machine_id} ({machine.distro})",
		"",
		"<!-- Файл собирается автоматически (ccsync pull). Правки сюда вносить бесполезно:",
		"     заголовок и хук факта живут в его frontmatter (index_title / index_hook),",
		"     а принадлежность машине — в metadata.scope. -->",
		"",
	]
	if shared:
		lines += ["## Общее (верно на всех машинах)", ""]
		lines += [_index_line(f, link_prefix) for f in shared] + [""]
	if own:
		lines += [f"## Про эту машину ({machine.machine_id})", ""]
		lines += [_index_line(f, link_prefix) for f in own] + [""]
	if foreign:
		lines += ["## Другие машины — НЕ применять здесь без проверки", ""]
		for machine_id, count in _count_by_machine(foreign).items():
			lines.append(f"- **{machine_id}**: {count} фактов")
		lines += [
			"",
			f"Файлы лежат рядом ({link_prefix or './'}), читать по прямому запросу.",
			"",
		]
	return "\n".join(lines).rstrip() + "\n"


def _index_line(fact: Fact, link_prefix: str) -> str:
	hook = fact.display_hook.strip()
	tail = f" — {hook}" if hook else ""
	return f"- [{fact.display_title}]({link_prefix}{fact.path.name}){tail}"


def _count_by_machine(facts: list[Fact]) -> dict[str, int]:
	counts: dict[str, int] = {}
	for fact in facts:
		for entry in fact.scope:
			entry = entry.strip()
			if entry and entry != SCOPE_GLOBAL:
				counts[entry] = counts.get(entry, 0) + 1
	return dict(sorted(counts.items()))


def render_full_index(facts: list[Fact]) -> str:
	"""Полный индекс для просмотра репозитория человеком (memory/index.md)."""
	lines = [
		"# Все факты памяти",
		"",
		"Сводка по репозиторию. На каждой машине Claude Code видит свой срез —",
		"его собирает `ccsync pull` в локальный MEMORY.md по полю `metadata.scope`.",
		"",
		"| Факт | Тип | Scope |",
		"|---|---|---|",
	]
	for fact in facts:
		scope = ", ".join(fact.scope)
		lines.append(f"| [{fact.display_title}](facts/{fact.path.name}) | {fact.fact_type or '—'} | {scope} |")
	return "\n".join(lines) + "\n"


def set_scope(path: Path, scope: list[str], title: str = "", hook: str = "") -> bool:
	"""Проставить scope (и, при желании, строку индекса) в frontmatter факта."""
	text = path.read_text(encoding="utf-8", errors="replace")
	meta, _ = parse_frontmatter(text)
	if not meta:
		return False
	updates = {"scope": scopes.format(scope)}
	if title:
		updates["index_title"] = _quote(title)
	if hook:
		updates["index_hook"] = _quote(hook)
	new_text = text
	for key, value in updates.items():
		new_text = _upsert_metadata_line(new_text, key, value)
	if new_text != text:
		path.write_text(new_text, encoding="utf-8")
		return True
	return False


def _quote(value: str) -> str:
	escaped = value.replace('"', '\\"')
	return f'"{escaped}"'


def _upsert_metadata_line(text: str, key: str, value: str) -> str:
	"""Вписать `  key: value` внутрь блока metadata, заменив прежнее значение."""
	pattern = re.compile(rf"^(\s+){re.escape(key)}:.*$", re.MULTILINE)
	if pattern.search(text):
		return pattern.sub(lambda m: f"{m.group(1)}{key}: {value}", text, count=1)
	anchor = re.compile(r"^metadata:\s*$", re.MULTILINE)
	match = anchor.search(text)
	if not match:
		return text
	insert_at = match.end()
	return text[:insert_at] + f"\n  {key}: {value}" + text[insert_at:]
