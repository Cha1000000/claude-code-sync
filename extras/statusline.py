#!/usr/bin/env python3
"""Статус-строка Claude Code.

Claude Code передаёт скрипту на stdin JSON с состоянием сессии, а первую
строку вывода рисует над полем ввода. Показываем: модель, уровень усилий,
расход лимитов (5-часового и недельного) со временем сброса, заполненность
контекста и стоимость сессии.

Подключается через "statusLine" в ~/.claude/settings.json.
"""

import json
import os
import sys
from datetime import datetime

RESET = "\033[0m"
BOLD = "\033[1m"
DIM = "\033[90m"
CYAN = "\033[36m"
GREEN = "\033[32m"
YELLOW = "\033[33m"
RED = "\033[31m"

# Дни недели захардкожены намеренно: strftime зависит от локали процесса,
# а её у дочернего процесса statusline может не быть.
DAYS = ("Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс")

BAR_WIDTH = 7
SEP = "   "


def color_for(pct):
    """Зелёный / жёлтый / красный в зависимости от того, сколько уже съедено."""
    if pct >= 80:
        return RED
    if pct >= 50:
        return YELLOW
    return GREEN


def bar(pct):
    filled = min(BAR_WIDTH, max(0, round(pct / 100 * BAR_WIDTH)))
    return "▰" * filled + "▱" * (BAR_WIDTH - filled)


def pct_text(pct):
    # Не превращаем ненулевой расход в честный ноль округлением.
    if 0 < pct < 1:
        return "<1%"
    return f"{round(pct)}%"


def reset_text(ts):
    when = datetime.fromtimestamp(ts)
    if when.date() == datetime.now().date():
        return when.strftime("%H:%M")
    return f"{DAYS[when.weekday()]} {when.strftime('%H:%M')}"


def limit_block(label, data, with_bar):
    pct = float(data.get("used_percentage") or 0)
    tint = color_for(pct)
    parts = [f"{DIM}{label}{RESET}"]
    if with_bar:
        parts.append(f"{tint}{bar(pct)}{RESET}")
    parts.append(f"{tint}{pct_text(pct)}{RESET}")
    resets_at = data.get("resets_at")
    if resets_at:
        parts.append(f"{DIM}⟳ {reset_text(resets_at)}{RESET}")
    return " ".join(parts)


def term_width():
    """Ширина терминала. stdout перенаправлен, поэтому спрашиваем у /dev/tty."""
    try:
        fd = os.open("/dev/tty", os.O_RDONLY)
    except OSError:
        return 0
    try:
        return os.get_terminal_size(fd).columns
    except OSError:
        return 0
    finally:
        os.close(fd)


def build(d):
    width = term_width()
    # 0 = ширину определить не удалось, тогда показываем всё.
    with_bars = width == 0 or width >= 100
    with_extras = width == 0 or width >= 85

    segments = []

    model = d.get("model", {}).get("display_name") or "?"
    head = f"{BOLD}{CYAN}{model}{RESET}"
    if d.get("fast_mode"):
        head += f" {YELLOW}⚡{RESET}"
    effort = d.get("effort", {}).get("level")
    if effort:
        head += f" {DIM}· {effort}{RESET}"
    segments.append(head)

    limits = d.get("rate_limits") or {}
    if "five_hour" in limits:
        segments.append(limit_block("5ч", limits["five_hour"], with_bars))
    if "seven_day" in limits:
        segments.append(limit_block("нед", limits["seven_day"], with_bars))

    if with_extras:
        ctx = d.get("context_window", {}).get("used_percentage")
        if ctx is not None:
            segments.append(f"{DIM}ctx{RESET} {color_for(ctx)}{round(ctx)}%{RESET}")

        cost = d.get("cost", {}).get("total_cost_usd")
        if cost is not None:
            segments.append(f"{DIM}${cost:.2f}{RESET}")

    return SEP.join(segments)


def main():
    raw = sys.stdin.read()
    try:
        data = json.loads(raw)
    except ValueError:
        print(f"{DIM}statusline: некорректный вход{RESET}")
        return
    try:
        print(build(data))
    except Exception as exc:  # строка не должна ломать интерфейс
        model = data.get("model", {}).get("display_name", "?")
        print(f"{CYAN}{model}{RESET} {DIM}(statusline: {exc}){RESET}")


if __name__ == "__main__":
    main()
