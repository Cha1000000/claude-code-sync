"""Тонкая обёртка над git. Никакой магии — только то, что нужно синхронизации."""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path


class GitError(RuntimeError):
	pass


@dataclass
class GitResult:
	code: int
	out: str
	err: str

	@property
	def ok(self) -> bool:
		return self.code == 0


class Git:
	def __init__(self, root: Path) -> None:
		self.root = Path(root)

	def run(self, *args: str, check: bool = False, env: dict | None = None) -> GitResult:
		proc = subprocess.run(
			["git", *args],
			cwd=self.root,
			capture_output=True,
			text=True,
			encoding="utf-8",
			errors="replace",
			env=env,
		)
		result = GitResult(proc.returncode, proc.stdout.strip(), proc.stderr.strip())
		if check and not result.ok:
			raise GitError(f"git {' '.join(args)}: {result.err or result.out}")
		return result

	# --- состояние ------------------------------------------------------

	def is_repo(self) -> bool:
		return (self.root / ".git").exists()

	def has_remote(self) -> bool:
		return bool(self.run("remote").out)

	def dirty_paths(self) -> list[str]:
		result = self.run("status", "--porcelain")
		# Срезать ровно три символа нельзя: вывод приходит уже обрезанным по
		# краям, и у первой строки статуса вида " M файл" ведущий пробел уже
		# потерян — путь лишался первой буквы.
		return [line.split(None, 1)[-1] for line in result.out.splitlines() if line.strip()]

	def is_dirty(self) -> bool:
		return bool(self.dirty_paths())

	def current_branch(self) -> str:
		return self.run("rev-parse", "--abbrev-ref", "HEAD").out or "main"

	# --- обмен ----------------------------------------------------------

	def pull(self) -> GitResult:
		"""Принять чужое до того, как отдавать своё."""
		if not self.has_remote():
			return GitResult(0, "нет remote — пропускаю pull", "")
		return self.run("pull", "--rebase", "--autostash")

	def commit_all(self, message: str) -> GitResult:
		self.run("add", "-A")
		staged = self.run("diff", "--cached", "--name-only").out
		if not staged:
			return GitResult(0, "нечего коммитить", "")
		return self.run("commit", "-m", message)

	def commit_paths(self, message: str, *paths: str) -> GitResult:
		"""Закоммитить только указанные пути, не забирая остальные правки."""
		self.run("add", "--", *paths)
		staged = self.run("diff", "--cached", "--name-only", "--", *paths).out
		if not staged:
			return GitResult(0, "нечего коммитить", "")
		return self.run("commit", "-m", message, "--", *paths)

	def rebase_in_progress(self) -> bool:
		"""Незавершённый rebase: коммитить в таком состоянии нельзя.

		После неудачного `pull --rebase` ветка отцеплена, и коммит уходит «в
		никуда» — пуш такого состояния молча теряет работу.
		"""
		git_dir = Path(self.run("rev-parse", "--git-dir").out or ".git")
		if not git_dir.is_absolute():
			git_dir = self.root / git_dir
		return any((git_dir / name).exists() for name in ("rebase-merge", "rebase-apply"))

	def push(self) -> GitResult:
		if not self.has_remote():
			return GitResult(0, "нет remote — пропускаю push", "")
		branch = self.current_branch()
		result = self.run("push", "origin", branch)
		if not result.ok and "no upstream" in (result.err or "").lower():
			return self.run("push", "-u", "origin", branch)
		return result

	def rebase_continue(self) -> GitResult:
		"""Продолжить rebase после того, как конфликт разрешён.

		Редакторы отключаем: git иначе откроет его для сообщения коммита и
		подвесит хук намертво — окна-то никакого нет.
		"""
		import os

		env = dict(os.environ, GIT_EDITOR="true", GIT_SEQUENCE_EDITOR="true")
		return self.run("rebase", "--continue", env=env)

	def conflicted_files(self) -> list[str]:
		result = self.run("diff", "--name-only", "--diff-filter=U")
		return [line for line in result.out.splitlines() if line.strip()]

	def last_commit_for(self, relative_path: str) -> str:
		"""Строка «машина, когда» по последнему коммиту, тронувшему файл."""
		result = self.run("log", "-1", "--format=%s|%ar", "--", relative_path)
		return result.out
