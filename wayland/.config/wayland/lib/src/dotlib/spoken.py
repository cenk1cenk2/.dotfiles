"""Turn spelled-out punctuation in a transcript into the symbols it names.

A dictated command arrives as `kubectl dash n ollama exec deploy slash
ollama`, and rebuilding `kubectl -n ollama exec deploy/ollama` from that is
a lookup, not a judgement - one that every small rewrite model gets wrong in
its own way. Doing it here leaves the model only the prose.

Only a run carrying at least two punctuation words within a few words of each
other is converted, because that is what dictated code sounds like; a single
one is prose ("the dot on the map") and is left alone. Digit words become
digits only right beside a converted symbol, so a spoken handle such as
`cenk one cenk two` keeps its words instead of being guessed at.

`at` is the one punctuation word prose uses all the time, so it never starts a
run: it converts only between two converted pieces, `john dot doe at example
dot com`, and `cenk at kilic dot dev` keeps its `at`."""

from __future__ import annotations

import re

SYMBOLS = {
    "dot": ".",
    "slash": "/",
    "dash": "-",
    "hyphen": "-",
    "underscore": "_",
    "colon": ":",
}
DIGITS = {
    "zero": "0",
    "one": "1",
    "two": "2",
    "three": "3",
    "four": "4",
    "five": "5",
    "six": "6",
    "seven": "7",
    "eight": "8",
    "nine": "9",
    "oh": "0",
    "o": "0",
}
DASHES = {"dash", "hyphen"}
# Words, not letters, when they stand alone in a sentence.
PROSE_LETTERS = {"a", "i"}
# Non-punctuation words allowed between two punctuation words of one run.
MAX_GAP = 4

_TOKEN = re.compile(r"^(\W*)(.*?)(\W*)$")


def punctuate(text: str) -> str:
    """Convert spoken punctuation in `text`, leaving prose untouched."""
    parts = re.split(r"(\s+)", text)
    words = parts[0::2]
    seps = [""] + parts[1::2]
    cores = [_TOKEN.match(w).group(2).lower() for w in words]
    count = len(words)

    candidates = [i for i, core in enumerate(cores) if core in SYMBOLS]
    active: set[int] = set()
    run = candidates[:1]
    for i in candidates[1:]:
        if i - run[-1] - 1 <= MAX_GAP:
            run.append(i)
            continue
        if len(run) >= 2:
            active.update(run)
        run = [i]
    if len(run) >= 2:
        active.update(run)
    for i, core in enumerate(cores):
        if (
            core == "at"
            and i - 2 in active
            and i + 2 < count
            and cores[i + 2] == "dot"
            and i + 2 in active
        ):
            active.add(i)
    if not active:
        return text

    def letter(i: int) -> bool:
        return (
            0 <= i < count
            and len(cores[i]) == 1
            and cores[i].isalpha()
            and cores[i] not in PROSE_LETTERS
        )

    out = list(words)
    glue = [False] * (count + 1)
    symbols = {**SYMBOLS, "at": "@"}
    i = 0
    while i < count:
        if i not in active:
            i += 1
            continue
        prefix, _, suffix = _TOKEN.match(words[i]).groups()
        if cores[i] in DASHES and i + 1 in active and cores[i + 1] in DASHES:
            out[i] = f"{prefix}--"
            out[i + 1] = _TOKEN.match(words[i + 1]).group(3)
            glue[i + 1] = True
            i += 2
            continue
        out[i] = f"{prefix}{symbols[cores[i]]}{suffix}"
        glue[i + 1] = True
        flag = (
            cores[i] in DASHES
            and letter(i + 1)
            and cores[i + 1] not in DIGITS
            and i - 1 not in active
        )
        if not flag:
            glue[i] = True
        i += 1

    i = 0
    while i < count:
        if cores[i] not in DIGITS:
            i += 1
            continue
        end = i
        while end + 1 < count and cores[end + 1] in DIGITS:
            end += 1
        span = range(i, end + 1)
        real = any(cores[j] not in ("oh", "o") for j in span)
        after_letter = letter(i - 1) and glue[i - 1] and i - 2 in active
        if real and (i - 1 in active or end + 1 in active or after_letter):
            for j in span:
                prefix, _, suffix = _TOKEN.match(words[j]).groups()
                out[j] = f"{prefix}{DIGITS[cores[j]]}{suffix}"
                glue[j] = glue[j] or j > i or after_letter
            if letter(end + 1):
                glue[end + 1] = True
        i = end + 1

    for i in active:
        start = i - 1
        while letter(start - 1):
            start -= 1
        if letter(i - 1) and glue[i]:
            for j in range(start + 1, i):
                glue[j] = True

    return "".join(("" if glue[i] else seps[i]) + out[i] for i in range(count))
