#!/usr/bin/env python3
"""Проверка: все команды из справки /help реально обрабатываются.

Повод: команда /summary работала, но в справке её не было — правку справки
сделали, а она молча не применилась. Этот скрипт ловит такие расхождения:
  python3 check_commands.py
"""
from __future__ import annotations

import re
import sys

import commands


def help_commands() -> list[str]:
    return re.findall(r"^/([a-z0-9]+)", commands.HELP, flags=re.MULTILINE)


def handled_commands() -> set[str]:
    src = open("commands.py", encoding="utf-8").read()
    found: set[str] = set()
    for m in re.finditer(r'(?:cmd|cmd)\s*(?:in|==)\s*\(?\[?([^:]+):', src):
        found.update(re.findall(r'"([a-z0-9]+)"', m.group(1)))
    return found


def main() -> int:
    in_help = help_commands()
    handled = handled_commands()
    missing = [c for c in in_help if c not in handled and c not in ("help", "start")]
    # алиасы (week/day/check/…) — это не отдельные команды, а синонимы
    aliases = {"start", "check", "update", "dip", "capital", "day", "week"}
    unlisted = sorted(handled - set(in_help) - aliases)
    print(f"команд в справке: {len(in_help)} → {', '.join('/' + c for c in in_help)}")
    if missing:
        print("❌ описаны в справке, но НЕ обрабатываются:", ", ".join("/" + c for c in missing))
    if unlisted:
        print("⚠️ обрабатываются, но НЕ в справке:", ", ".join("/" + c for c in unlisted))
    if not missing and not unlisted:
        print("✅ справка и обработчик совпадают")
    return 1 if missing else 0


if __name__ == "__main__":
    raise SystemExit(main())
