"""Идентичность машины — единственный источник истины о том, «где я».

Файл ~/.claude/ccsync-machine.json никогда не попадает в git: он описывает
конкретную машину. Всё остальное (какие вообще есть машины) лежит в репо
в machines.json, чтобы Клод знал про соседние машины, но не путал их с текущей.

Определение машины НЕ угадывается по uname/hostname при каждом запуске —
идентификатор присваивается один раз при `ccsync init` и дальше только читается.
"""

from __future__ import annotations

import json
import os
import platform
import re
import socket
from dataclasses import asdict, dataclass
from pathlib import Path

MACHINE_FILE_NAME = "ccsync-machine.json"
SECRETS_FILE_NAME = "ccsync-secrets.env"


@dataclass
class Machine:
	"""Паспорт одной машины."""

	machine_id: str
	os: str          # linux | darwin | win32
	distro: str      # Arch Linux, Ubuntu 24.04, macOS 15, Windows 11
	hostname: str
	home: str
	note: str = ""
	# Версия Claude Code. Формат транскрипта меняется между релизами, поэтому
	# сильное расхождение версий между машинами — повод обновиться, а не гадать.
	claude_version: str = ""

	def to_dict(self) -> dict:
		return asdict(self)

	@classmethod
	def from_dict(cls, data: dict) -> "Machine":
		known = {f: data.get(f, "") for f in cls.__dataclass_fields__}
		return cls(**known)

	@property
	def is_windows(self) -> bool:
		return self.os == "win32"

	def describe(self) -> str:
		return f"{self.machine_id} · {self.distro} · $HOME={self.home}"


def setup_console() -> None:
	"""Заставить stdout/stderr говорить в UTF-8.

	Консоль Windows по умолчанию в кодовой странице вроде cp866, а весь вывод
	здесь на русском — без этого получаем UnicodeEncodeError вместо отчёта.
	"""
	import sys as _sys

	for stream in (_sys.stdout, _sys.stderr):
		try:
			stream.reconfigure(encoding="utf-8", errors="replace")
		except (AttributeError, ValueError, OSError):
			pass


def detect_os() -> str:
	system = platform.system().lower()
	if system == "darwin":
		return "darwin"
	if system == "windows":
		return "win32"
	return "linux"


def detect_distro() -> str:
	"""Человекочитаемое имя ОС — только для отображения, не для логики."""
	current = detect_os()
	if current == "darwin":
		return f"macOS {platform.mac_ver()[0] or ''}".strip()
	if current == "win32":
		return f"Windows {platform.release()}".strip()
	release = Path("/etc/os-release")
	if release.exists():
		for line in release.read_text(encoding="utf-8", errors="replace").splitlines():
			if line.startswith("PRETTY_NAME="):
				return line.split("=", 1)[1].strip().strip('"')
	return platform.system() or "unknown"


def suggest_machine_id() -> str:
	"""Предложить идентификатор вида `linux-desktop`; финальное слово за человеком."""
	host = socket.gethostname().split(".")[0].lower()
	host = re.sub(r"[^a-z0-9-]+", "-", host).strip("-")
	distro_word = re.sub(r"[^a-z0-9]+", "", detect_distro().split()[0].lower()) if detect_distro() else ""
	if host and distro_word and distro_word not in host:
		return f"{distro_word}-{host}"[:40]
	return (host or distro_word or "machine")[:40]


def claude_config_dir() -> Path:
	"""Каталог конфигурации Claude Code с учётом CLAUDE_CONFIG_DIR."""
	override = os.environ.get("CLAUDE_CONFIG_DIR")
	if override:
		return Path(override).expanduser()
	return Path.home() / ".claude"


def machine_file_path() -> Path:
	return claude_config_dir() / MACHINE_FILE_NAME

def secrets_file_path() -> Path:
	return claude_config_dir() / SECRETS_FILE_NAME


def load_machine() -> Machine | None:
	"""Прочитать паспорт машины. None — значит `ccsync init` ещё не запускали."""
	path = machine_file_path()
	if not path.exists():
		return None
	try:
		return Machine.from_dict(json.loads(path.read_text(encoding="utf-8")))
	except (json.JSONDecodeError, TypeError, ValueError):
		return None


def save_machine(machine: Machine) -> Path:
	path = machine_file_path()
	path.parent.mkdir(parents=True, exist_ok=True)
	path.write_text(
		json.dumps(machine.to_dict(), ensure_ascii=False, indent=2) + "\n",
		encoding="utf-8",
	)
	return path


def find_claude() -> str | None:
	"""Путь к исполняемому claude.

	На Windows это .cmd/.exe, и вызов по голому имени из другого процесса может
	не сработать — ищем через which, а затем в местах нативной установки,
	которые могут быть вне PATH у процесса-хука.
	"""
	import shutil

	found = shutil.which("claude")
	if found:
		return found
	for candidate in (
		Path.home() / ".local" / "bin" / "claude",
		Path.home() / ".local" / "bin" / "claude.exe",
		Path.home() / "AppData" / "Local" / "Programs" / "claude" / "claude.exe",
	):
		if candidate.exists():
			return str(candidate)
	return None


def detect_claude_version() -> str:
	"""Версия установленного Claude Code, например «2.1.246». Пусто — если не найден."""
	import re
	import subprocess

	executable = find_claude()
	if not executable:
		return ""
	try:
		result = subprocess.run(
			[executable, "--version"], capture_output=True, text=True, timeout=20,
			encoding="utf-8", errors="replace",
		)
	except (OSError, subprocess.SubprocessError):
		return ""
	match = re.search(r"\d+\.\d+\.\d+", result.stdout or "")
	return match.group(0) if match else ""


def version_tuple(version: str) -> tuple[int, ...]:
	"""«2.1.246» → (2, 1, 246); нераспознанное → (0,), чтобы не падать при сравнении."""
	parts = [p for p in version.split(".") if p.isdigit()]
	return tuple(int(p) for p in parts) if parts else (0,)


def build_machine(machine_id: str, note: str = "") -> Machine:
	return Machine(
		machine_id=machine_id,
		os=detect_os(),
		distro=detect_distro(),
		hostname=socket.gethostname(),
		home=str(Path.home()),
		note=note,
		claude_version=detect_claude_version(),
	)


# Секреты, которые не нужно вписывать руками: их отдаёт уже установленный
# инструмент. Ключ — имя переменной, значение — команда, печатающая секрет.
DERIVABLE_SECRETS = {
	"GITHUB_PERSONAL_ACCESS_TOKEN": ["gh", "auth", "token"],
	"GITHUB_TOKEN": ["gh", "auth", "token"],
}


def load_secrets() -> dict[str, str]:
	"""Локальные секреты в формате KEY=value. В git не попадают никогда.

	Чего нет в файле — пробуем добыть у местных инструментов (например токен
	GitHub у `gh`), чтобы на новой машине не приходилось копировать его руками.
	"""
	secrets: dict[str, str] = {}
	path = secrets_file_path()
	if path.exists():
		for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
			line = line.strip()
			if not line or line.startswith("#") or "=" not in line:
				continue
			key, value = line.split("=", 1)
			value = value.strip().strip('"').strip("'")
			if value:
				secrets[key.strip()] = value
	for key, command in DERIVABLE_SECRETS.items():
		if key not in secrets:
			derived = _run_for_secret(command)
			if derived:
				secrets[key] = derived
	return secrets


def _run_for_secret(command: list[str]) -> str | None:
	"""Выполнить команду и вернуть её вывод как секрет. Молча — при любой осечке."""
	import shutil
	import subprocess

	if not shutil.which(command[0]):
		return None
	try:
		result = subprocess.run(
			command, capture_output=True, text=True, timeout=15,
			encoding="utf-8", errors="replace",
		)
	except (OSError, subprocess.SubprocessError):
		return None
	value = (result.stdout or "").strip()
	return value if result.returncode == 0 and value else None
