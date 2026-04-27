#!/usr/bin/env python3
"""
Kestrel local terminal dialog.

Usage:
  python3 local_dialog.py           — interactive REPL
  python3 local_dialog.py <text>    — one-shot response
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import kestrel_persona as persona

_PROMPT = "→ "


def run_repl() -> None:
    print(persona.build_ready_state())
    print()
    try:
        while True:
            try:
                line = input(_PROMPT).strip()
            except EOFError:
                print(persona.build_farewell())
                return
            if not line:
                continue
            intent = persona.classify_intent(line)
            response = persona.handle(line)
            print(response)
            print()
            if intent == "FAREWELL":
                return
    except KeyboardInterrupt:
        print()
        print(persona.build_farewell())


def run_once(text: str) -> None:
    print(persona.handle(text))


if __name__ == "__main__":
    if len(sys.argv) > 1:
        run_once(" ".join(sys.argv[1:]))
    else:
        run_repl()
