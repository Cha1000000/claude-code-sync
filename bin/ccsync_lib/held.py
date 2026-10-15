"""Неотправленные сессии и сообщения, которые хук не может сказать сам.

Хуки запускают движок молча (`--quiet`, вывод в никуда), поэтому всё важное,
что случилось в фоне, теряется. Так 08–15.10.2026 сессия на 175 МБ шесть дней
не уезжала в хранилище, и об этом не знал никто, — пока другая машина не
открыла её устаревшую копию.

Два механизма:

- **held** — сессии, которые push не смог отдать. Пишутся и локально, и в
  хранилище (`held/<машина>.json`, каждая машина пишет только свой файл), чтобы
  о них знала не только эта машина: другая предупредит, открывая такую сессию.
- **notices** — короткие сообщения из фонового pull/push. Их печатает
  SessionStart следующей сессии и после этого забывает.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

LOCAL_HELD = "ccsync-held.json"
LOCAL_NOTICES = "ccsync-notices.json"
VAULT_HELD_DIR = "held"

# Сообщений больше этого не копим: если их никто не видит неделями, длинный
# список всё равно никто не прочтёт.
MAX_NOTICES = 20


def _read(path: Path, default):
	try:
		data = json.loads(path.read_text(encoding="utf-8"))
	except (OSError, ValueError):
		return default
	return data if isinstance(data, type(default)) else default


def _write(path: Path, data) -> None:
	path.parent.mkdir(parents=True, exist_ok=True)
	path.write_text(json.dumps(data, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")


def vault_file(vault_root: Path, machine_stem: str) -> Path:
	return vault_root / VAULT_HELD_DIR / f"{machine_stem}.json"


def load_local(config_dir: Path) -> dict[str, dict]:
	return _read(config_dir / LOCAL_HELD, {})


def mark(config_dir: Path, vault_root: Path, machine_stem: str, machine_id: str,
		 session_id: str, *, project: str, size: int, slimmed: int) -> bool:
	"""Запомнить, что сессия не уехала. True — если это новость.

	Запись делается один раз, при первом пропуске: размеры растут с каждым
	ходом, и обновлять их значило бы коммитить файл каждые пять минут.
	"""
	held = load_local(config_dir)
	if session_id in held:
		return False
	held[session_id] = {"since": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
						"project": project, "size": size, "slimmed": slimmed}
	_sync(config_dir, vault_root, machine_stem, machine_id, held)
	return True


def clear(config_dir: Path, vault_root: Path, machine_stem: str, machine_id: str,
		  session_id: str) -> bool:
	held = load_local(config_dir)
	if session_id not in held:
		return False
	held.pop(session_id)
	_sync(config_dir, vault_root, machine_stem, machine_id, held)
	return True


def _sync(config_dir: Path, vault_root: Path, machine_stem: str, machine_id: str,
		  held: dict) -> None:
	_write(config_dir / LOCAL_HELD, held)
	target = vault_file(vault_root, machine_stem)
	if held:
		_write(target, {"machine_id": machine_id, "sessions": held})
	elif target.exists():
		target.unlink()


def held_elsewhere(vault_root: Path, own_stem: str, session_id: str) -> list[tuple[str, dict]]:
	"""Машины, на которых у этой сессии есть неотправленные записи."""
	found: list[tuple[str, dict]] = []
	directory = vault_root / VAULT_HELD_DIR
	if not directory.is_dir():
		return found
	for path in sorted(directory.glob("*.json")):
		if path.stem == own_stem:
			continue
		data = _read(path, {})
		entry = (data.get("sessions") or {}).get(session_id)
		if isinstance(entry, dict):
			found.append((str(data.get("machine_id") or path.stem), entry))
	return found


def add_notice(config_dir: Path, text: str) -> None:
	notices = _read(config_dir / LOCAL_NOTICES, [])
	if text in notices:
		return
	notices.append(text)
	_write(config_dir / LOCAL_NOTICES, notices[-MAX_NOTICES:])


def take_notices(config_dir: Path) -> list[str]:
	"""Забрать накопленные сообщения: после показа они больше не нужны."""
	path = config_dir / LOCAL_NOTICES
	notices = _read(path, [])
	if notices:
		try:
			path.unlink()
		except OSError:
			return []
	return notices
