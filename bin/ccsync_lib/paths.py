"""Токенизация путей — сердце синхронизации.

Один и тот же проект на разных машинах лежит по несвязанным путям:

    linux-desktop  /home/alex/projects/MyApp
    mac-laptop         /Users/alex/My Projects/Android/Compose/MyApp
    win11-pc         D:\\Projects\\Android\\MyApp

Поэтому в репозитории пути не хранятся вовсе — вместо них токены:

    {{P:myapp}}/app/build.gradle
    {{HOME}}/.claude/settings.json

При выгрузке (tokenize) локальные пути превращаются в токены, при загрузке
(detokenize) — обратно, уже под пути текущей машины. Если проект на этой
машине не привязан, подставляется fallback-каталог, и сессия всё равно
открывается — просто без файлов проекта.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

HOME_TOKEN = "{{HOME}}"
PROJECT_TOKEN = "{{P:%s}}"

# Токен вместе с «хвостом» пути: {{P:kidguard}}/app/src/Main.kt
# Хвост обрываем на символах, которые в пути не встречаются или закрывают его.
_TOKEN_RE = re.compile(r"\{\{(HOME|P:[A-Za-z0-9_-]+)\}\}([^\s\"'`,;:)\]}<>|]*)")


@dataclass(frozen=True)
class Replacement:
	"""Одна пара «локальный корень ↔ токен». Длинные применяются первыми."""

	local_root: str
	token: str

	@property
	def sort_key(self) -> int:
		return len(self.local_root)


def to_posix(path: str) -> str:
	"""Привести разделители к прямым слешам (внутреннее представление)."""
	return path.replace("\\", "/")


def to_native(path: str, target_os: str) -> str:
	"""Развернуть разделители под целевую ОС."""
	if target_os == "win32":
		return path.replace("/", "\\")
	return path


def strip_trailing_sep(path: str) -> str:
	"""Убрать хвостовой разделитель, кроме корня."""
	cleaned = path.rstrip("/\\")
	return cleaned if cleaned else path


class PathMapper:
	"""Переводит пути между «локальным» и «репозиторным» представлением.

	project_paths — соответствие ключа проекта его пути на ЭТОЙ машине.
	Проекты, которых здесь нет, при detokenize уедут в fallback_root.
	"""

	def __init__(
		self,
		home: str,
		project_paths: dict[str, str],
		target_os: str = "linux",
		fallback_root: str | None = None,
	) -> None:
		self.home = strip_trailing_sep(home)
		self.project_paths = {k: strip_trailing_sep(v) for k, v in project_paths.items()}
		self.target_os = target_os
		self.fallback_root = strip_trailing_sep(fallback_root or f"{self.home}/claude-sessions")
		self._replacements = self._build_replacements()

	def _build_replacements(self) -> list[Replacement]:
		# Домашний каталог всегда сворачивается в {{HOME}}, даже если он же
		# заведён как проект (сессии из ~ живут под ключом "home"). Иначе путь
		# уехал бы в {{P:home}}, а на машине, где этот проект не привязан,
		# развернулся бы в fallback ~/claude-sessions/home — и хуки указали бы
		# в несуществующее место.
		items = [
			Replacement(local_root=path, token=PROJECT_TOKEN % key)
			for key, path in self.project_paths.items()
			if strip_trailing_sep(path) != self.home
		]
		items.append(Replacement(local_root=self.home, token=HOME_TOKEN))
		# Длинные корни первыми: иначе {{HOME}} съест начало пути проекта,
		# лежащего внутри домашней папки.
		return sorted(items, key=lambda r: r.sort_key, reverse=True)

	# --- локальное представление → репозиторное -------------------------

	def tokenize(self, text: str) -> str:
		"""Заменить локальные пути на токены. Хвост приводится к POSIX."""
		if not text:
			return text
		for rep in self._replacements:
			for variant in self._spellings(rep.local_root):
				text = self._replace_with_token(text, variant, rep.token)
		return text

	@staticmethod
	def _spellings(root: str) -> list[str]:
		"""Написания корня, которые могут встретиться в тексте."""
		posix = to_posix(root)
		windows = root.replace("/", "\\")
		# dict.fromkeys — сохранить порядок и убрать дубли
		return list(dict.fromkeys([root, posix, windows]))

	@staticmethod
	def _replace_with_token(text: str, root: str, token: str) -> str:
		"""Подставить токен и привести хвост пути к прямым слешам."""
		if root not in text:
			return text
		out: list[str] = []
		position = 0
		while True:
			found = text.find(root, position)
			if found == -1:
				out.append(text[position:])
				break
			out.append(text[position:found])
			tail_start = found + len(root)
			tail_end = tail_start
			while tail_end < len(text) and text[tail_end] not in " \t\n\r\"'`,;:)]}<>|":
				tail_end += 1
			out.append(token + to_posix(text[tail_start:tail_end]))
			position = tail_end
		return "".join(out)

	# --- репозиторное представление → локальное -------------------------

	def detokenize(self, text: str) -> str:
		"""Развернуть токены в пути текущей машины."""
		if not text:
			return text
		return _TOKEN_RE.sub(self._expand_token, text)

	def _expand_token(self, match: re.Match[str]) -> str:
		name, tail = match.group(1), match.group(2)
		root = self.home if name == "HOME" else self.resolve_project(name[2:])
		return to_native(f"{to_posix(root)}{tail}", self.target_os)

	def resolve_project(self, key: str) -> str:
		"""Путь проекта на этой машине; для непривязанного — fallback.

		Ключ "home" — особый: домашний каталог есть на любой машине, поэтому
		он никогда не уходит в fallback, даже если запись в реестре пуста
		(так бывает со старыми шаблонами, где путь свёрнут в {{P:home}}).
		"""
		known = self.project_paths.get(key)
		if known:
			return known
		if key == "home":
			return self.home
		return f"{self.fallback_root}/{key}"

	def is_bound(self, key: str) -> bool:
		return key in self.project_paths

	# --- служебное ------------------------------------------------------

	def project_key_for(self, path: str) -> str | None:
		"""Найти ключ проекта, которому принадлежит путь (самый длинный корень)."""
		target = to_posix(strip_trailing_sep(path))
		best_key, best_len = None, -1
		for key, root in self.project_paths.items():
			root_posix = to_posix(root)
			if (target == root_posix or target.startswith(root_posix + "/")) and len(root_posix) > best_len:
				best_key, best_len = key, len(root_posix)
		return best_key


def slug_for(path: str) -> str:
	"""Имя папки проекта в ~/.claude/projects.

	Правило Claude Code: любой символ вне [A-Za-z0-9] становится дефисом.
	Сверено со всеми реальными папками, включая кириллицу и пробелы.
	"""
	return re.sub(r"[^A-Za-z0-9]", "-", path)
