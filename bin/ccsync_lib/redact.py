"""Маскировка секретов в транскриптах, уезжающих в хранилище.

Ключи API попадают в диалог сами собой: их вставляют в чат, они мелькают в
выводе `cat` и в конфигах, которые Клод показывает. Транскрипт при этом едет
в общий репозиторий целиком, и один такой ключ обесценивает всё шифрование
`secrets`: незачем шифровать `provider.key`, если тот же ключ лежит рядом
открытым текстом в истории диалога.

Работаем в две руки:

* **точным совпадением** — значения, которые машина и так знает: строки из
  `ccsync-secrets.env`, содержимое файлов из реестра секретов, приватный ключ
  age. Это самый надёжный путь, ложных срабатываний быть не может;
* **по форме** — распространённые формы ключей (`sk-…`, `ghp_…`, JWT и прочие)
  на случай, когда ключ в диалоге есть, а на машине его нет.

Маскировка **односторонняя**: при `pull` секрет не восстанавливается. Так и
задумано — транскрипт это история, а не рабочий конфиг, и незачем возить в нём
живые ключи. Восстанавливать нечего и не нужно.

Порог длины у шаблонов намеренно высокий: короткие совпадения чаще оказываются
обычным текстом, чем ключом.
"""

from __future__ import annotations

import re
from pathlib import Path

PLACEHOLDER = "{{SECRET:%s}}"

# Значения короче этого не маскируем точным совпадением: строка вроде "1" или
# "true" из env-файла встречается в тексте постоянно и не является секретом.
MIN_EXACT_LENGTH = 12

# Формы ключей, которые узнаются по виду. Имя нужно только для плейсхолдера,
# чтобы в транскрипте было видно, что именно вырезано.
PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
	("api-key", re.compile(r"\bsk-[A-Za-z0-9][A-Za-z0-9_-]{15,}")),
	("github-token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}")),
	("google-oauth", re.compile(r"\bya29\.[A-Za-z0-9_\-]{20,}")),
	("google-refresh", re.compile(r"\b1//[A-Za-z0-9_\-]{20,}")),
	("google-api-key", re.compile(r"\bAIza[A-Za-z0-9_\-]{30,}")),
	("jwt", re.compile(r"\beyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}")),
	("slack-token", re.compile(r"\bxox[baprs]-[A-Za-z0-9\-]{10,}")),
	("kimchi-token", re.compile(r"\bcastai_v1_[A-Za-z0-9_]{20,}")),
	("aws-access-key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
	("openrouter-key", re.compile(r"\bsk-or-v1-[a-f0-9]{32,}")),
	("private-key-block", re.compile(
		r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]{0,4000}?-----END [A-Z ]*PRIVATE KEY-----")),
)


class Redactor:
	"""Заменяет секреты в тексте. Собирается один раз на весь транскрипт."""

	def __init__(self, exact: dict[str, str] | None = None, *, patterns: bool = True):
		# значение секрета → метка для плейсхолдера
		self._exact = exact or {}
		self._patterns = PATTERNS if patterns else ()

	def __bool__(self) -> bool:
		return bool(self._exact) or bool(self._patterns)

	def mask(self, text: str) -> str:
		if not text:
			return text
		# Сначала точные совпадения: у них есть осмысленная метка (имя
		# переменной или файла), и они точнее любого шаблона.
		for value, label in self._exact.items():
			if value in text:
				text = text.replace(value, PLACEHOLDER % label)
		for label, pattern in self._patterns:
			text = pattern.sub(PLACEHOLDER % label, text)
		return text


def _values_from_env_file(path: Path) -> dict[str, str]:
	"""Значения из файла вида KEY=VALUE. Метка — имя переменной."""
	found: dict[str, str] = {}
	if not path.exists():
		return found
	try:
		content = path.read_text(encoding="utf-8", errors="replace")
	except OSError:
		return found
	for line in content.splitlines():
		line = line.strip()
		if not line or line.startswith("#") or "=" not in line:
			continue
		name, _, value = line.partition("=")
		value = value.strip().strip('"').strip("'")
		if len(value) >= MIN_EXACT_LENGTH:
			found[value] = name.strip()
	return found


def _value_from_plain_file(path: Path) -> str | None:
	"""Файл, который целиком является секретом (например provider.key)."""
	if not path.exists() or path.stat().st_size > 8192:
		return None
	try:
		value = path.read_text(encoding="utf-8", errors="replace").strip()
	except OSError:
		return None
	if len(value) < MIN_EXACT_LENGTH or "\n" in value:
		return None
	return value


def for_home(home: Path, secret_files: list[str] | None = None) -> Redactor:
	"""Собрать маскировщик по секретам, которые есть на этой машине.

	`secret_files` — ключи реестра секретов (пути относительно `home`).
	Файл вида KEY=VALUE разбирается построчно, односрочный файл берётся целиком.
	"""
	exact: dict[str, str] = {}

	# Локальные секреты Claude-обвязки известны всегда, даже если реестр пуст.
	known = list(secret_files or [])
	for extra in (".claude/ccsync-secrets.env", ".claude/ccsync-age.key"):
		if extra not in known:
			known.append(extra)

	for relative in known:
		path = home / relative
		if not path.exists():
			continue
		name = path.name
		if path.suffix == ".env" or name.endswith(".env"):
			exact.update(_values_from_env_file(path))
			continue
		value = _value_from_plain_file(path)
		if value:
			exact[value] = name

	# Приватный ключ age — многострочный, точное совпадение по его секретной
	# строке: age-keygen пишет её последней и она начинается с AGE-SECRET-KEY.
	identity = home / ".claude/ccsync-age.key"
	if identity.exists():
		for line in identity.read_text(encoding="utf-8", errors="replace").splitlines():
			line = line.strip()
			if line.startswith("AGE-SECRET-KEY"):
				exact[line] = "age-identity"

	return Redactor(exact)
