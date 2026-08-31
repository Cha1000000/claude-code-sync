#!/usr/bin/env python3
import json
import re
import sys
from pathlib import Path

COMMAND_RE = re.compile(r"<local-command-caveat>.*?<command-name>[^<]+</command-name>", re.S)


def is_trivial_user_turn(record):
	content = record.get("message", {}).get("content")
	if not isinstance(content, str):
		return False
	return bool(COMMAND_RE.search(content))


def session_is_junk(path):
	saw_user_turn = False
	with path.open(encoding="utf-8") as fh:
		for line in fh:
			line = line.strip()
			if not line:
				continue
			try:
				record = json.loads(line)
			except json.JSONDecodeError:
				continue
			if record.get("type") != "user":
				continue
			saw_user_turn = True
			if not is_trivial_user_turn(record):
				return False
	return saw_user_turn


def main():
	try:
		payload = json.load(sys.stdin)
	except (json.JSONDecodeError, ValueError):
		return 0

	transcript_path = payload.get("transcript_path")
	if not transcript_path:
		return 0

	current = Path(transcript_path)
	sessions_dir = current.parent
	if not sessions_dir.is_dir():
		return 0

	for jsonl_file in sessions_dir.glob("*.jsonl"):
		if jsonl_file == current or jsonl_file.name == current.name:
			continue
		try:
			if session_is_junk(jsonl_file):
				jsonl_file.unlink()
		except OSError:
			continue

	return 0


if __name__ == "__main__":
	sys.exit(main())
