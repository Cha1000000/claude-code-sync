"""Манифест выпуска движка — bin/ENGINE.json.

Выпуск движка — это тег публичного репозитория. Манифест рядом с кодом говорит,
из каких файлов выпуск состоит и какой у каждого хеш. Он нужен двум сторонам:

    выпуск     — release-public.py собирает его из публичного дерева;
    обновление — engine.py сравнивает по нему файлы пользователя с выпуском и
                 понимает, какой из них правили руками (хеш не совпал — не трогаем).

Только stdlib: модуль едет в публичный репозиторий вместе с движком.
"""

from __future__ import annotations

import hashlib
import json
from fnmatch import fnmatchcase
from pathlib import Path

MANIFEST_PATH = "bin/ENGINE.json"
FORMAT = 1
MIN_PYTHON = "3.9"

# Что входит в выпуск. Каталоги — целиком (лишнее из них при обновлении
# удаляется), одиночные файлы и маски — поимённо. Всё остальное в хранилище
# (память, сессии, скиллы, README, свои команды) обновление не трогает никогда.
# tools/hooks сюда не входит: там живут хуки самого пользователя.
ROOTS = ("bin", "tools/tests", "tests")
SINGLE_FILES = ("setup-hooks.py", "extras/statusline.py")
GLOBS = ("tools/commands/sync-*.md",)

# Эти файлы не заменяются, а дополняются недостающими строками.
MERGE_LINES = (".gitignore", ".gitattributes")

SKIP_DIRS = frozenset({"__pycache__"})
SKIP_SUFFIXES = (".pyc", ".ccsync-tmp", ".ccsync-incoming")


def normalize(data: bytes) -> bytes:
	"""Без разницы, как git на этой ОС разложил переводы строк."""
	return data.replace(b"\r\n", b"\n")


def digest(data: bytes) -> str:
	return hashlib.sha256(normalize(data)).hexdigest()


def _skipped(relative: str) -> bool:
	parts = relative.split("/")
	return (relative == MANIFEST_PATH
			or bool(SKIP_DIRS & set(parts))
			or relative.endswith(SKIP_SUFFIXES))


def managed(relative: str) -> bool:
	"""Входит ли путь (относительно корня хранилища, через «/») в выпуск."""
	if _skipped(relative):
		return False
	if relative in SINGLE_FILES:
		return True
	if any(relative.startswith(root + "/") for root in ROOTS):
		return True
	return any(fnmatchcase(relative, pattern) and relative.count("/") == pattern.count("/")
			   for pattern in GLOBS)


def collect(root: Path, overrides: dict[str, bytes] | None = None,
			removed: frozenset[str] | set[str] = frozenset(),
			only: set[str] | None = None) -> dict[str, str]:
	"""Хеши файлов выпуска в дереве `root`.

	`overrides` — содержимое, которое лежит (или ляжет) вместо дискового или
	которого на диске ещё нет: так выпуск считает хеши до записи файлов.
	`removed` — пути, которые к моменту выпуска будут удалены.
	`only` — с диска брать только эти пути (файлы, которые git отслеживает):
	лишний неотслеживаемый файл иначе попал бы в манифест, а в тег — нет.
	"""
	overrides = overrides or {}
	found: dict[str, bytes] = {}
	for rel in overrides:
		if managed(rel):
			found[rel] = overrides[rel]
	for path in root.rglob("*"):
		if not path.is_file():
			continue
		rel = path.relative_to(root).as_posix()
		if rel.startswith(".git/") or rel in found or not managed(rel):
			continue
		if only is not None and rel not in only:
			continue
		found[rel] = path.read_bytes()
	return {rel: digest(found[rel]) for rel in sorted(found) if rel not in removed}


def build(files: dict[str, str], release: str | None, source: str) -> dict:
	return {
		"format": FORMAT,
		"release": release,
		"source": source,
		"min_python": MIN_PYTHON,
		"roots": list(ROOTS),
		"merge_lines": list(MERGE_LINES),
		"files": files,
	}


def dumps(manifest: dict) -> str:
	"""Один и тот же выпуск даёт побайтно один и тот же файл: ни дат, ни порядка."""
	return json.dumps(manifest, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def loads(text: str) -> dict:
	manifest = json.loads(text)
	if not isinstance(manifest, dict) or manifest.get("format") != FORMAT \
			or not isinstance(manifest.get("files"), dict):
		raise ValueError("unsupported ENGINE.json")
	return manifest
