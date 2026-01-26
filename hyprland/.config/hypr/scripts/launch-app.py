#!/usr/bin/env python3
"""
Hyprland application launcher that reads variable definitions from definitions.conf
and launches applications with proper configuration.
"""

import sys
import re
import subprocess
from pathlib import Path

def parse_definitions(config_path: Path) -> dict[str, str]:
    """Parse Hyprland definitions.conf and extract variable definitions."""
    definitions = {}

    if not config_path.exists():
        return definitions

    with open(config_path) as f:
        for line in f:
            line = line.strip()
            # Skip comments and empty lines
            if not line or line.startswith("#"):
                continue

            # Match variable definitions: $var = value
            match = re.match(r"\$(\w+)\s*=\s*(.+)", line)
            if match:
                var_name, value = match.groups()
                definitions[var_name] = value.strip()

    return definitions

def expand_variables(value: str, definitions: dict[str, str]) -> str:
    """Expand variable references in a value."""

    # Replace $var references with their values
    def replace_var(match):
        var_name = match.group(1)
        return definitions.get(var_name, match.group(0))

    return re.sub(r"\$(\w+)", replace_var, value)

def main():
    if len(sys.argv) < 2:
        print("Usage: launch-app.py <variable-name>")
        print("Example: launch-app.py process_manager")
        sys.exit(1)

    var_name = sys.argv[1]

    # Parse definitions from hyprland config
    config_dir = Path.home() / ".config" / "hypr"
    definitions_file = config_dir / "definitions.conf"

    definitions = parse_definitions(definitions_file)

    # Get the command from definitions
    command = definitions.get(var_name)

    if not command:
        print(f"Variable ${var_name} not found in definitions.conf")
        print(f"Available variables: {', '.join(sorted(definitions.keys()))}")
        sys.exit(1)

    # Expand any nested variables
    command = expand_variables(command, definitions)

    # Execute the command
    subprocess.run(command, shell=True)

if __name__ == "__main__":
    main()
