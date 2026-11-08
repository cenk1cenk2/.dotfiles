import subprocess


def rofi(
    prompt: str,
    choices: list[str],
    *,
    extra_args: list[str] | None = None,
) -> str | None:
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
