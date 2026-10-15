#!/usr/bin/env python3
"""SessionStart-хук: сообщить Клоду, на какой машине он запущен.

Память общая на все машины, поэтому без этой подсказки Клод рискует
применить здесь то, что верно только на соседней машине. Хук печатает
короткий блок, который попадает в контекст ДО первого сообщения человека,
так что определять окружение по uname уже не нужно.

Печатает только факты и всегда завершается кодом 0: сломанный хук не должен
мешать работать.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))


def main() -> int:
	try:
		from ccsync_lib import identity, memoryscope
		from ccsync_lib.i18n import tr
		from ccsync_lib.vault import Vault

		identity.setup_console()
		machine = identity.load_machine()
		if machine is None:
			print(tr("[ccsync] Машина не настроена: python3 bin/ccsync.py init"))
			return 0

		vault = Vault(Path(__file__).resolve().parent.parent)
		lines = [
			tr("[ccsync] Машина: {id} · {distro} · $HOME={home}",
			   id=machine.machine_id, distro=machine.distro, home=machine.home)
		]

		cwd = os.getcwd()
		bound = vault.paths_for_machine(machine.machine_id)
		key = next((k for k, p in bound.items() if _same_path(p, cwd)), None)
		if key:
			lines.append(tr("[ccsync] Проект: {key} → {path} (привязан)",
							key=key, path=cwd))
		else:
			lines.append(tr("[ccsync] Каталог: {path} "
							"(проект не привязан к хранилищу)", path=cwd))

		facts = memoryscope.load_facts(vault.memory_facts_dir)
		if facts:
			own = sum(1 for f in facts if f.applies_to(machine) and not f.is_global)
			shared = sum(1 for f in facts if f.is_global)
			foreign = len(facts) - own - shared
			line = tr("[ccsync] Память: этой машины {own}, общих {shared}",
					  own=own, shared=shared)
			if foreign:
				line += tr(", про другие машины {count} — "
						   "НЕ применять здесь без проверки", count=foreign)
			lines.append(line)

		broken = _unusable_mcp(vault, machine)
		if broken:
			# Сам pull работает с --quiet, и его вывод уходит в никуда, поэтому
			# про приехавший, но неработающий сервер сказать больше негде.
			for name, problem in broken:
				lines.append(tr("[ccsync] MCP {name} здесь не запустится: {problem}",
								name=name, problem=problem))
			names = " ".join(name for name, _ in broken)
			lines.append(tr("[ccsync]   если он не нужен на этой машине: "
							"/sync-mcp {names} --not-here", names=names))

		others = vault.other_machines(machine.machine_id)
		if others:
			lines.append(tr("[ccsync] Другие машины: {names}",
							names=", ".join(sorted(others))))

		lines.extend(_sync_warnings(vault, machine, _session_arg()))

		print("\n".join(lines))
	except Exception as error:  # хук не имеет права ломать запуск сессии
		print(f"[ccsync] контекст машины недоступен: {error}", file=sys.stderr)  # noqa: перевод недоступен, если импорт упал
	return 0


def _unusable_mcp(vault, machine) -> list[tuple[str, str]]:
	"""MCP-серверы, нужные здесь по scope, но заведомо неработающие.

	Проверка дешёвая (существование файла и поиск в PATH) и молчит, пока всё
	в порядке: строка появляется ровно тогда, когда с другой машины приехал
	сервер, которому здесь нечем запускаться.
	"""
	try:
		from ccsync_lib import identity, scopes, tools

		template = vault.tools_dir / "mcp-servers.template.json"
		servers = tools._read_json(template, {})
		if not isinstance(servers, dict) or not servers:
			return []
		scope_map = tools.load_mcp_scopes(vault.tools_dir / tools.MCP_SCOPES_FILE)
		local = tools.read_global_config(identity.claude_config_dir()).get("mcpServers") or {}
		broken: list[tuple[str, str]] = []
		for name in sorted(servers):
			if name not in local:
				continue  # ещё не применён — про него скажет сам pull
			if not scopes.matches(tools.mcp_scope_for(scope_map, name), machine):
				continue
			problem = tools.probe_runnable(local[name])
			if problem:
				broken.append((name, problem))
		return broken
	except Exception:
		return []


def _session_arg() -> str:
	if "--session" in sys.argv:
		index = sys.argv.index("--session")
		if index + 1 < len(sys.argv):
			return sys.argv[index + 1]
	return ""


def _sync_warnings(vault, machine, session_id: str) -> list[str]:
	"""Что фоновая синхронизация не смогла сказать сама.

	Хуки запускают движок молча, поэтому неотправленные сессии и слияния
	становятся видны только здесь. Самое важное — открытая сейчас сессия, у
	которой на другой машине есть неотправленные записи: 14.10.2026 ноутбук
	открыл именно такую копию, отставшую на шесть дней, и никто не предупредил.
	"""
	try:
		from ccsync_lib import held, identity
		from ccsync_lib.i18n import tr
		from ccsync_lib.vault import machine_file_stem

		config = identity.claude_config_dir()
		lines = list(held.take_notices(config))
		for sid, entry in sorted(held.load_local(config).items()):
			lines.append(tr("[ccsync] ВНИМАНИЕ: сессия {id} не уезжает в хранилище с {since} — "
							"{size} МБ, после сжатия {slimmed} МБ (порог {limit} МБ); "
							"поможет /compact в ней или {command} slim",
							id=sid[:8], since=str(entry.get("since", "?"))[:10],
							size=int(entry.get("size", 0)) // 1048576,
							slimmed=int(entry.get("slimmed", 0)) // 1048576,
							limit=_limit_mb(), command="ccsync"))
		if session_id:
			own = machine_file_stem(machine.machine_id)
			for other, entry in held.held_elsewhere(vault.root, own, session_id):
				lines.append(tr("[ccsync] ВНИМАНИЕ: эта сессия на {machine} не уезжает в хранилище "
								"с {since} — здесь открыта устаревшая копия. Продолжать лучше там.",
								machine=other, since=str(entry.get("since", "?"))[:10]))
		return lines
	except Exception:
		return []


def _limit_mb() -> int:
	from ccsync_lib import sessions
	return sessions.DEFAULT_MAX_BYTES // 1048576


def _same_path(left: str, right: str) -> bool:
	try:
		return Path(left).resolve() == Path(right).resolve()
	except OSError:
		return str(left).rstrip("/\\") == str(right).rstrip("/\\")


if __name__ == "__main__":
	raise SystemExit(main())
