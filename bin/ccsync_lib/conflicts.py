"""Автоматическое разрешение конфликтов в общих JSON-файлах.

Реестры машин и проектов конфликтовать больше не могут — они разложены по
файлам машин. Но шаблоны в tools/ (настройки, MCP, список плагинов) по смыслу
общие: их перезаписывает каждый push. Если две машины поработали врозь и
разошлись, git честно останавливается на конфликте, хотя правки почти всегда
касаются разных ключей и сливаются без потерь.

Здесь мы берём три версии файла прямо из индекса git — общего предка, чужую
сторону и свою — и сливаем их по узлам той же функцией, что применяется к
settings.json. При спорном значении побеждает локальное: машина лучше знает про
собственные пути и настройки.

Всё, что не JSON или не разбирается, остаётся человеку: файл фактов или скилл,
изменённый на двух машинах по-разному, за него сливать нельзя.
"""

from __future__ import annotations

import json

from .gitutil import Git
from .tools import merge_json

# Ступени индекса git при конфликте: общий предок, «наше», «их».
STAGE_BASE, STAGE_OURS, STAGE_THEIRS = 1, 2, 3


def resolve_json_conflicts(git: Git) -> tuple[list[str], list[str]]:
	"""Слить конфликтующие JSON-файлы. Возвращает (разрешённые, оставшиеся)."""
	resolved: list[str] = []
	remaining: list[str] = []
	for path in git.conflicted_files():
		if not path.endswith(".json"):
			remaining.append(path)
			continue
		merged = _merge_stages(git, path)
		if merged is None:
			remaining.append(path)
			continue
		target = git.root / path
		target.write_text(
			json.dumps(merged, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
			encoding="utf-8",
		)
		if git.run("add", "--", path).ok:
			resolved.append(path)
		else:
			remaining.append(path)
	return resolved, remaining


def _merge_stages(git: Git, path: str):
	"""Слить три ступени индекса. None — если хоть одна не разобралась."""
	base = _read_stage(git, STAGE_BASE, path)
	ours = _read_stage(git, STAGE_OURS, path)
	theirs = _read_stage(git, STAGE_THEIRS, path)
	# Отсутствие предка нормально (файл добавлен с обеих сторон), а вот без
	# одной из сторон сливать нечего — это удаление, и решать его человеку.
	if ours is None or theirs is None:
		return None
	# При rebase «наше» — то, на что накладываем (чужой коммит), «их» — наш
	# собственный коммит. Локальным для merge_json считаем именно его.
	return merge_json(base if base is not None else {}, theirs, ours)


def _read_stage(git: Git, stage: int, path: str):
	result = git.run("show", f":{stage}:{path}")
	if not result.ok or not result.out:
		return None
	try:
		return json.loads(result.out)
	except (json.JSONDecodeError, ValueError):
		return None
