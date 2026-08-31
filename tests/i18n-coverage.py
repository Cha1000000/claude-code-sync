#!/usr/bin/env python3
"""Проверка полноты перевода.

Проходит по коду, собирает всё, что обёрнуто в tr(), и сверяет с каталогами в
bin/ccsync_lib/locales/. Непереведённая строка не ломает движок — она просто
печатается по-русски, — поэтому забыть её легко, и ловить это должен тест.

    python3 tests/i18n-coverage.py            проверить (код возврата 1, если пусто)
    python3 tests/i18n-coverage.py --dump     выписать недостающие как заготовку JSON
"""

from __future__ import annotations

import argparse
import ast
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LOCALES = ROOT / "bin" / "ccsync_lib" / "locales"
SOURCES = sorted(ROOT.glob("bin/**/*.py")) + [ROOT / "setup-hooks.py"]


def literal(node: ast.AST, module_doc: str | None) -> str | None:
	"""Строка из первого аргумента tr(): литерал, склейка литералов или __doc__."""
	if isinstance(node, ast.Constant) and isinstance(node.value, str):
		return node.value
	# `tr(__doc__)` — справка команды лежит в докстринге модуля.
	if isinstance(node, ast.Name) and node.id == "__doc__":
		return module_doc
	if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
		left = literal(node.left, module_doc)
		right = literal(node.right, module_doc)
		return None if left is None or right is None else left + right
	return None


def collect() -> dict[str, list[str]]:
	"""msgid → где встречается."""
	found: dict[str, list[str]] = {}
	for path in SOURCES:
		if not path.exists():
			continue
		tree = ast.parse(path.read_text(encoding="utf-8"))
		module_doc = ast.get_docstring(tree, clean=False)
		for node in ast.walk(tree):
			if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
					and node.func.id == "tr" and node.args):
				continue
			text = literal(node.args[0], module_doc)
			where = f"{path.relative_to(ROOT)}:{node.lineno}"
			if text is None:
				print(f"ВНИМАНИЕ: tr() с нестроковым аргументом — {where}", file=sys.stderr)
				continue
			found.setdefault(text, []).append(where)
	return found


def main() -> int:
	parser = argparse.ArgumentParser(description="Полнота перевода")
	parser.add_argument("--dump", action="store_true", help="выписать недостающие")
	parser.add_argument("--lang", default="en", help="какой каталог проверять")
	args = parser.parse_args()

	found = collect()
	path = LOCALES / f"{args.lang}.json"
	catalog = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}

	missing = {text: where for text, where in found.items() if text not in catalog}
	extra = [text for text in catalog if text not in found]

	if args.dump:
		print(json.dumps({text: "" for text in missing}, ensure_ascii=False, indent=2))
		return 0

	print(f"Строк в коде: {len(found)}")
	print(f"Переведено ({args.lang}): {len(found) - len(missing)}")
	if missing:
		print(f"\nНе переведено: {len(missing)}")
		for text, where in list(missing.items())[:20]:
			print(f"  {where[0]}\n    {text[:90]}")
	if extra:
		print(f"\nЛишнее в каталоге (в коде такой строки нет): {len(extra)}")
		for text in extra[:10]:
			print(f"    {text[:90]}")
	return 1 if missing or extra else 0


if __name__ == "__main__":
	sys.exit(main())
