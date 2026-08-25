"""Shared scaffolding for the dotfiles scripts projects.

Deliberately empty: consumers import submodules directly, e.g.

    from dotlib.cli import create_logger
    from dotlib.desktop import is_headless

Re-exporting here would make every consumer pay for `rich` even when it
only wants a stdlib-only module.
"""
