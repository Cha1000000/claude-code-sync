"""Язык вывода.

Движок написан по-русски, и русский текст остаётся источником: он же служит
ключом перевода. Перевод ищется по самой строке, а не нашёлся — печатается
оригинал. Поэтому новая строка в коде никогда не ломает вывод: она просто
остаётся непереведённой, и это видно.

Язык берётся из CCSYNC_LANG, иначе из локали системы, иначе английский.
Каталоги лежат рядом, в locales/<язык>.json: {русская строка: перевод}.

    say(tr("[ccsync] {name}: обновлён", name=name))

Подстановки идут через format, поэтому переводчик волен переставлять их
местами — в английском порядок слов другой. Строку с фигурными скобками, но
без подстановок, format не трогает вовсе: токены вида {{VAULT}} должны
доехать до пользователя как есть.
"""

from __future__ import annotations

import json
import locale
import os
from pathlib import Path

LOCALES_DIR = Path(__file__).resolve().parent / "locales"

# Язык оригинала: для него перевод не нужен по определению.
SOURCE_LANG = "ru"


def _detect_lang() -> str:
	explicit = os.environ.get("CCSYNC_LANG", "").strip()
	if explicit:
		return explicit[:2].lower()
	for name in ("LC_ALL", "LC_MESSAGES", "LANG"):
		value = os.environ.get(name, "").strip()
		if value and value.lower() not in ("c", "posix"):
			return value[:2].lower()
	# В Windows переменных локали обычно нет — спрашиваем систему.
	try:
		system = locale.getlocale()[0]
	except (ValueError, TypeError):
		system = None
	return system[:2].lower() if system else "en"


def _load(lang: str) -> dict[str, str]:
	if lang == SOURCE_LANG:
		return {}
	path = LOCALES_DIR / f"{lang}.json"
	if not path.exists():
		# Незнакомый язык лучше показать по-английски, чем по-русски:
		# английский тут — общий знаменатель, а не «ещё один перевод».
		path = LOCALES_DIR / "en.json"
	try:
		data = json.loads(path.read_text(encoding="utf-8"))
	except (OSError, json.JSONDecodeError, ValueError):
		return {}
	return data if isinstance(data, dict) else {}


LANG = _detect_lang()
CATALOG = _load(LANG)


def tr(text: str, /, **values) -> str:
	"""Перевести строку и подставить значения."""
	template = CATALOG.get(text, text)
	if not values:
		return template
	try:
		return template.format(**values)
	except (KeyError, IndexError, ValueError):
		# Перевод разошёлся с кодом по именам полей — лучше оригинал, чем падение.
		return text.format(**values)
