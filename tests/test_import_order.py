"""Every module imports first in a fresh interpreter (no order-dependent import cycles)."""

import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
MODULES = sorted(
    ".".join(path.relative_to(ROOT).with_suffix("").parts).removesuffix(".__init__")
    for path in (ROOT / "ams_assess").rglob("*.py")
)


@pytest.mark.parametrize("module", MODULES)
def test_module_imports_standalone(module):
    result = subprocess.run(
        [sys.executable, "-c", f"import {module}"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=60,
        env={"PATH": "/usr/bin:/bin", "HOME": str(ROOT / "tmp")},
    )
    assert result.returncode == 0, result.stderr.strip().splitlines()[-1:]
