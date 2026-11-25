"""A ResultStore on the local filesystem: one file per result key.

Used by local and container workers until results go to S3 (phase 3). A results
directory mounted as a volume survives container restarts, so the cache does too.
"""

import os
import re
import tempfile
from pathlib import Path

_KEY = re.compile(r"^[0-9a-f]{64}$")


class DirStore:
    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, key: str) -> Path:
        # Keys come from result_key(), but check anyway: a key like "../../etc/x" must
        # never become a path outside the results directory.
        if not _KEY.match(key):
            raise ValueError(f"not a result key: {key!r}")
        return self.root / key

    def exists(self, key: str) -> bool:
        return self._path(key).is_file()

    def put(self, key: str, data: bytes) -> None:
        # Write to a temp file in the same directory, then rename over the target.
        # os.replace is atomic on one filesystem, so a crash mid-write leaves either no
        # file or the old one, never a half-written result that exists() would trust.
        target = self._path(key)
        fd, tmp = tempfile.mkstemp(dir=self.root, prefix=".tmp-")
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(data)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, target)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise
