"""Pre-commit guard: fail on absolute user paths and on private patterns (one regex per line in .private-patterns.txt)."""

import pathlib
import re
import sys

patterns = [r"[A-Za-z]:\\Users\\", r"/Users/[^/\s]+/", r"/home/[^/\s]+/"]
private = pathlib.Path(".private-patterns.txt")
if private.exists():
    patterns += [
        line.strip()
        for line in private.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.startswith("#")
    ]
rx = [re.compile(p, re.IGNORECASE) for p in patterns]
bad = 0
for name in sys.argv[1:]:
    p = pathlib.Path(name)
    if p.name in {".private-patterns.txt", "check_private.py"}:
        continue
    try:
        text = p.read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError):
        continue
    for n, line in enumerate(text.splitlines(), 1):
        if any(r.search(line) for r in rx):
            print(f"{name}:{n}: private information or an absolute path")
            bad += 1
sys.exit(1 if bad else 0)
