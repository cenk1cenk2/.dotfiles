import subprocess
from typing import Optional

def rofi(
    prompt: str,
    choices: list[str],
    *,
    extra_args: Optional[list[str]] = None,
) -> Optional[str]:
    if not choices:
        return None

    proc = subprocess.run(
        [
            "rofi",
            "-dmenu",
            "-i",
            "-p",
            prompt,
            *(extra_args or []),
        ],
        input="\n".join(choices),
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        return None

    selected = proc.stdout.strip()
    return selected or None

def rofi_with_icons(
    prompt: str,
    entries: list[tuple[str, str]],
    *,
    extra_args: Optional[list[str]] = None,
) -> Optional[int]:
    """Show rofi with icons and return the index of the selected entry.

    The wire format is rofi's meta-pipe: `<label>\\0icon\\x1f<icon>\\n` per line.
    `-format i` asks rofi to emit the index of the chosen line on stdout.
    """
    if not entries:
        return None

    payload = "".join(f"{label}\x00icon\x1f{icon}\n" for label, icon in entries)

    proc = subprocess.run(
        [
            "rofi",
            "-dmenu",
            "-i",
            "-p",
            prompt,
            "-format",
            "i",
            "-show-icons",
            *(extra_args or []),
        ],
        input=payload,
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        return None

    try:
        return int(proc.stdout.strip())
    except ValueError:
        return None
