#!/usr/bin/env python3
"""Hyprland application launcher driven by variables in definitions.conf."""

import argparse
import re
import subprocess
import sys
from pathlib import Path

DEFINITIONS_PATH = Path.home() / ".config" / "hypr" / "definitions.conf"

class LaunchApp:
    def __init__(self, args):
        self.args = args

    def run(self):
        definitions = self._parse_definitions(DEFINITIONS_PATH)
        raw = definitions.get(self.args.variable)
        if not raw:
            print(
                f"Variable ${self.args.variable} not found in definitions.conf",
                file=sys.stderr,
            )
            print(
                f"Available variables: {', '.join(sorted(definitions))}",
                file=sys.stderr,
            )
            sys.exit(1)

        command = self._expand(raw, definitions)
        subprocess.run(command, shell=True, check=False)

    @staticmethod
    def _parse_definitions(path: Path) -> dict[str, str]:
        definitions: dict[str, str] = {}
        if not path.exists():
            return definitions

        with open(path) as f:
            for raw in f:
                line = raw.strip()
                if not line or line.startswith("#"):
                    continue
                match = re.match(r"\$(\w+)\s*=\s*(.+)", line)
                if match:
                    name, value = match.groups()
                    definitions[name] = value.strip()

        return definitions

    @staticmethod
    def _expand(value: str, definitions: dict[str, str]) -> str:
        def replace(match: re.Match) -> str:
            return definitions.get(match.group(1), match.group(0))

        return re.sub(r"\$(\w+)", replace, value)

def main():
    parser = argparse.ArgumentParser(
        description="Launch an application defined by a hyprland variable",
    )
    parser.add_argument(
        "variable",
        help="Variable name from definitions.conf (e.g. process_manager)",
    )
    args = parser.parse_args()

    LaunchApp(args).run()

if __name__ == "__main__":
    main()
