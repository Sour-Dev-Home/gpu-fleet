"""The worker's install ID (ADR 0026, Q87): who a series of metrics belongs to.

A random UUID made on the first start and kept in a per-user file, so the same install is the same
series across restarts. It names an install, not a person: nothing in it derives from the machine,
the user or the network. It is the OTel resource attribute service.instance.id, and nothing writes
it into the repository, an image or a log line.

  Windows            %APPDATA%\\fleet\\worker-id
  elsewhere          $XDG_CONFIG_HOME/fleet/worker-id  (default ~/.config/fleet/worker-id)
  FLEET_WORKER_ID    overrides it, and stores nothing (notebooks, containers, tests)

The file is created 0600 in a 0700 folder where the OS has such bits. Windows ignores them; there
the profile folder's own ACL (the user, SYSTEM and administrators) is the protection.
"""

import logging
import os
import re
import uuid
from collections.abc import Mapping
from pathlib import Path

log = logging.getLogger(__name__)

# What an override may look like: it becomes a metric label, so keep it short and plain.
_OVERRIDE = re.compile(r"[A-Za-z0-9._-]{1,64}")


def _config_dir(env: Mapping[str, str], windows: bool) -> Path:
    home = Path(env.get("USERPROFILE" if windows else "HOME") or Path.home())
    if windows:
        base = Path(env.get("APPDATA") or home / "AppData" / "Roaming")
    else:
        base = Path(env.get("XDG_CONFIG_HOME") or home / ".config")
    return base / "fleet"


def _read(path: Path) -> str | None:
    try:
        text = path.read_text(encoding="utf-8").strip()
        return str(uuid.UUID(text)) if text else None
    except (OSError, ValueError):
        return None


def _store(path: Path, value: str) -> str:
    """Write `value` unless another start got there first; return what the file now holds."""
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(value + "\n")
    try:
        try:
            os.link(tmp, path)  # create-if-absent, atomically: two first starts agree on one ID
        except FileExistsError:
            other = _read(path)
            if other is not None:
                return other
            os.replace(tmp, path)  # a damaged file: replace it
        except OSError:
            os.replace(tmp, path)  # a file system without hard links
    finally:
        tmp.unlink(missing_ok=True)
    return value


def install_id(env: Mapping[str, str] = os.environ, windows: bool | None = None) -> str:
    """The override if set, else the stored ID, made on first use. Raises ValueError for a
    FLEET_WORKER_ID that is not plain. A folder that cannot be written is no reason to stop a
    worker: the ID then lasts for this run only, and a warning says so."""
    override = env.get("FLEET_WORKER_ID", "").strip()
    if override:
        if not _OVERRIDE.fullmatch(override):
            raise ValueError("FLEET_WORKER_ID must be 1-64 characters from A-Z a-z 0-9 . _ -")
        return override
    path = _config_dir(env, os.name == "nt" if windows is None else windows) / "worker-id"
    existing = _read(path)
    if existing is not None:
        return existing
    fresh = str(uuid.uuid4())
    try:
        return _store(path, fresh)
    except OSError as e:
        # The error type only: its message holds the path, which holds the user's name.
        log.warning(
            "worker ID not saved (%s): this run has its own ID; set FLEET_WORKER_ID to keep one",
            type(e).__name__,
        )
        return fresh
