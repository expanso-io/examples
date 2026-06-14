"""Every job YAML in the repo must pass offline validation.

Runs ``expanso-cli job validate <file> --offline`` against each file under
jobs/ (recursively, so compose and test variants are covered too). Skips
cleanly when the CLI isn't installed — CI's installer step handles that case
with a warning.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
JOB_FILES = sorted(ROOT.glob("jobs/**/*.yaml")) + sorted(ROOT.glob("jobs/**/*.yml"))

pytestmark = pytest.mark.skipif(
    shutil.which("expanso-cli") is None,
    reason="expanso-cli not installed",
)


@pytest.mark.skipif(not JOB_FILES, reason="no job files under jobs/ yet")
@pytest.mark.parametrize("job_file", JOB_FILES, ids=lambda p: str(p.relative_to(ROOT)))
def test_job_validates_offline(job_file: Path) -> None:
    proc = subprocess.run(
        ["expanso-cli", "job", "validate", str(job_file), "--offline"],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 0, (
        f"validation failed for {job_file}:\n{proc.stdout}\n{proc.stderr}"
    )
