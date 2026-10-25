"""The escape fixtures' check: did anything escape?

exec runs it with what escape.c printed placed in its working directory as
`output`, the checks a box may pass as `allowed`, one name a line and
`#` starting a comment, and `program.yaml` beside this file as its
manifest. escape.c prints one line per thing it tried, `<check>
REFUSED|ESCAPED|INFO <detail>`. The check reports through the folder exec
names in `UNICON_REPORT`: `escaped`, how many lines say ESCAPED of a check
not allowed, `score` 1 and `verdict` 0 when there is none and at least one
line says what was tried, and otherwise `verdict` 1 with those lines as
its message. Every line goes to staff, so what a box sees is on record
however the run ends.
"""

import os
import sys
from pathlib import Path

LIMIT = 4000
"""The most characters of a message: the lines past it are cut."""


def judged(lines: list[str], allowed: set[str]) -> tuple[int, list[str]]:
    """The verdict, 0 or 1, and the lines that say ESCAPED of a check not
    in `allowed`."""
    said = [line.split(" ", 2) for line in lines if line.strip()]
    escaped = [
        " ".join(words)
        for words in said
        if words[1:2] == ["ESCAPED"] and words[0] not in allowed
    ]
    tried = any(words[1:2] in (["REFUSED"], ["INFO"]) for words in said)
    return (0 if tried and not escaped else 1), escaped


def main() -> int:
    report = Path(os.environ["UNICON_REPORT"])
    lines = Path("output").read_text(errors="replace").splitlines()
    allowed = {
        line.partition("#")[0].strip()
        for line in Path("allowed").read_text().splitlines()
    } - {""}
    verdict, escaped = judged(lines, allowed)
    (report / "escaped").write_text(f"{len(escaped)}\n")
    (report / "verdict").write_text(f"{verdict}\n")
    if verdict == 0:
        (report / "score").write_text("1\n")
    else:
        message = "\n".join(escaped) or "escape.c printed nothing it tried"
        (report / "message").write_text(message[:LIMIT])
    (report / "staff_message").write_text("\n".join(lines)[:LIMIT])
    return 0


if __name__ == "__main__":
    sys.exit(main())
