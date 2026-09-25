"""Секреты, которые ездят между машинами в зашифрованном виде.

Обычное правило хранилища — секретам в git не место, и `tools.py` даже
маскирует их в шаблоне MCP. Но ключи API нужны на каждой машине, а носить их
руками неудобно. Компромисс: файл лежит в репозитории зашифрованным (age),
а ключ расшифровки — единственное, что переносится вручную и в git не попадает
никогда.

Устройство повторяет `hostfiles`: явный реестр `tools/secret-files.json`
перечисляет ровно те файлы, которые едут, и scope каждого. Ключ реестра —
путь относительно домашнего каталога (`.claude/provider.key`), он же задаёт,
куда файл ляжет на другой машине. Шифротекст хранится рядом, в
`tools/secrets/<путь>.age`.

Получателей может быть несколько: `tools/secrets/recipients.txt` собирает
публичные ключи всех машин, и файл шифруется сразу для всех — тогда каждая
машина расшифровывает своим ключом, и общего секрета на всех не возникает.
Новая машина добавляется через `ccsync secrets add-recipient`, после чего
файлы надо перешифровать (`ccsync push tools`).

Чего этот модуль намеренно НЕ делает:

* не трогает локальный файл, если он разошёлся с тем, что приехало. Для
  секретов молча затереть чужую версию хуже, чем сообщить о расхождении;
* не пишет ничего, если приватного ключа на машине нет, — просто говорит, что
  секреты не расшифровать;
* не лезет за ключом никуда, кроме `~/.claude/ccsync-age.key`.
"""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from . import scopes
from .i18n import tr
from .identity import Machine

# Реестр «путь относительно $HOME → scope», рядом с host-files.json.
REGISTRY_FILE = "secret-files.json"

# Каталог с шифротекстом внутри tools/.
SECRETS_DIR_NAME = "secrets"

# Публичные ключи получателей, по одному на строку. Комментарии через '#'.
RECIPIENTS_FILE = "recipients.txt"

# Приватный ключ этой машины. В git не попадает никогда — переносится руками.
IDENTITY_PATH = ".claude/ccsync-age.key"

SUFFIX = ".age"


@dataclass
class SecretsReport:
	applied: list[str] = field(default_factory=list)
	# Приехали, но локально файл другой — не трогаем, пусть решает человек.
	diverged: list[str] = field(default_factory=list)
	# Применимы здесь, но расшифровать нечем.
	locked: list[str] = field(default_factory=list)

	def summary(self) -> str:
		parts = []
		if self.applied:
			parts.append(tr("применено: {items}", items=", ".join(self.applied)))
		if self.diverged:
			parts.append(tr("разошлись: {items}", items=", ".join(self.diverged)))
		if self.locked:
			parts.append(tr("не расшифровать: {items}", items=", ".join(self.locked)))
		return "; ".join(parts)


def age_available() -> bool:
	return shutil.which("age") is not None


def identity_path(home: Path) -> Path:
	return home / IDENTITY_PATH


def load_registry(path: Path) -> dict[str, list[str]]:
	return scopes.load_map(path)


def save_registry(path: Path, registry: dict[str, list[str]]) -> None:
	# keep_global как у файлов обвязки: карта сама и есть список того, что едет,
	# и выкинув global-запись, мы забыли бы про сам файл.
	scopes.save_map(path, registry, keep_global=True)


def load_recipients(secrets_dir: Path) -> list[str]:
	source = secrets_dir / RECIPIENTS_FILE
	if not source.exists():
		return []
	keys = []
	for line in source.read_text(encoding="utf-8").splitlines():
		line = line.split("#", 1)[0].strip()
		if line:
			keys.append(line)
	return keys


def add_recipient(secrets_dir: Path, public_key: str, label: str) -> bool:
	"""Добавить публичный ключ машины. False — такой уже записан."""
	secrets_dir.mkdir(parents=True, exist_ok=True)
	if public_key in load_recipients(secrets_dir):
		return False
	target = secrets_dir / RECIPIENTS_FILE
	prefix = "" if not target.exists() or target.read_text(encoding="utf-8").endswith("\n") else "\n"
	with target.open("a", encoding="utf-8") as handle:
		handle.write(f"{prefix}{public_key}  # {label}\n")
	return True


def public_key_of(home: Path) -> str | None:
	"""Публичный ключ этой машины, выведенный из приватного."""
	identity = identity_path(home)
	if not identity.exists() or not age_available():
		return None
	result = subprocess.run(["age-keygen", "-y", str(identity)],
							capture_output=True, text=True)
	if result.returncode != 0:
		return None
	return result.stdout.strip() or None


def _cipher_path(secrets_dir: Path, key: str) -> Path:
	return secrets_dir / (key + SUFFIX)


def export(home: Path, secrets_dir: Path, registry: dict[str, list[str]],
		   machine: Machine) -> list[str]:
	"""Зашифровать локальные секреты в хранилище. Возвращает имена отданных."""
	if not registry or not age_available():
		return []
	recipients = load_recipients(secrets_dir)
	if not recipients:
		return []

	sent: list[str] = []
	for key, scope in registry.items():
		if not scopes.matches(scopes.parse(scope), machine):
			continue
		source = home / key
		if not source.exists():
			continue
		target = _cipher_path(secrets_dir, key)
		target.parent.mkdir(parents=True, exist_ok=True)

		command = ["age", "--encrypt"]
		for recipient in recipients:
			command += ["--recipient", recipient]
		command += ["--output", str(target)]
		result = subprocess.run(command, input=source.read_bytes(),
								capture_output=True)
		if result.returncode == 0:
			sent.append(key)
	return sent


def apply(home: Path, secrets_dir: Path, registry: dict[str, list[str]],
		  machine: Machine) -> SecretsReport:
	"""Расшифровать применимые здесь секреты и разложить по местам."""
	report = SecretsReport()
	if not registry:
		return report

	identity = identity_path(home)
	have_key = identity.exists() and age_available()

	for key, scope in registry.items():
		if not scopes.matches(scopes.parse(scope), machine):
			continue
		cipher = _cipher_path(secrets_dir, key)
		if not cipher.exists():
			continue
		if not have_key:
			report.locked.append(key)
			continue

		result = subprocess.run(
			["age", "--decrypt", "--identity", str(identity), str(cipher)],
			capture_output=True)
		if result.returncode != 0:
			report.locked.append(key)
			continue

		plain = result.stdout
		target = home / key
		if target.exists():
			if target.read_bytes() == plain:
				continue
			# Локальная версия другая: возможно, здесь обновили ключ.
			# Молча затирать секрет нельзя — говорим и оставляем как есть.
			report.diverged.append(key)
			continue

		target.parent.mkdir(parents=True, exist_ok=True)
		target.write_bytes(plain)
		target.chmod(0o600)
		report.applied.append(key)
	return report
