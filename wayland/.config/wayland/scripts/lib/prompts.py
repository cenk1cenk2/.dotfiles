"""Prompt-file loader for system prompts living next to scripts."""

import os

def load_prompt(filename: str, relative_to: str) -> str:
    """Read a text file living next to the caller.

    Pass `relative_to=__file__` from the calling module."""
    path = os.path.join(os.path.dirname(os.path.abspath(relative_to)), filename)
    with open(path) as f:
        return f.read().strip()
