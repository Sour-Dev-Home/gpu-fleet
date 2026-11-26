"""CI audits every package's uv lockfile with a hash-pinned pip-audit (Q95)."""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CI = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")


def test_ci_runs_pip_audit_over_the_frozen_lockfile_of_every_package():
    locked = sorted(p.parent.name for p in ROOT.glob("*/uv.lock"))
    assert {"broker", "worker", "killswitch"} <= set(locked)
    m = re.search(r"for pkg in ([^;\n]+)(?:;|\n)\s*do", CI)
    assert m, "no audit loop"
    assert (
        sorted(m.group(1).split()) == locked
    )  # a new package with a lockfile must be added to the loop
    assert "uv export --frozen --no-hashes" in CI
    assert re.search(r"pip-audit -r \S+ --no-deps --disable-pip", CI)


def test_the_audit_tooling_is_hash_pinned():
    step = CI[CI.index("name: pip-audit") :]
    step = step[: step.index("- name: Build images")]
    assert "pip install --require-hashes -r .github/ci/requirements.txt" in step
    req_in = (ROOT / ".github/ci/requirements.in").read_text(encoding="utf-8")
    assert re.search(r"(?m)^pip-audit==\d+\.\d+\.\d+$", req_in)
    req = (ROOT / ".github/ci/requirements.txt").read_text(encoding="utf-8")
    entry = req[req.index("pip-audit==") :].split("\n    # via")[0]
    assert "--hash=sha256:" in entry
