"""Sibling-file loaders: prompts, CSS, anything that ships alongside a script."""

import os

def load_relative_file(filename: str, relative_to: str) -> str:
    """Read a file living next to `relative_to` (pass `__file__`).

    Returns the raw contents — caller decides whether to strip, parse,
    or feed to something that cares about trailing whitespace (CSS does,
    prompts mostly don't)."""
    path = os.path.join(os.path.dirname(os.path.abspath(relative_to)), filename)
    with open(path) as f:
        return f.read()

def load_prompt(filename: str, relative_to: str) -> str:
    """Read a sibling prompt file, stripped of surrounding whitespace."""
    return load_relative_file(filename, relative_to).strip()
