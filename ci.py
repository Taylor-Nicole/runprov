#!/usr/bin/env python3
# Copyright (c) 2026 Assistance Publique – Hôpitaux de Paris (AP-HP),
#                    Hôpital Henri-Mondor, and Taylor Thompson
# Licensed under the CeCILL-B Free Software License Agreement — see LICENSE.
"""Run the CI locally — the SAME commands the workflow runs, because it runs these.

    python ci.py            everything, in the order CI runs it
    python ci.py lint       ruff format --check, ruff check, mypy
    python ci.py test       pytest with the coverage gate
    python ci.py build      build, twine check --strict, install the wheel and import it
    python ci.py setup      install the dev extras and the pre-commit hooks

Why a script rather than a list of steps in the workflow
--------------------------------------------------------
If the workflow holds the commands and the contributing guide holds a copy of them, the
copy is wrong within a month and "it passes locally" stops meaning anything. `test.yml`
calls THIS FILE, so local and CI cannot drift: there is one definition.

Plain Python with no dependencies so it behaves the same on Windows, where `make` is not
a given. Every command is printed before it runs, so a failure is reproducible by reading
the output rather than by reading this file.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def run(*cmd: str, cwd: Path | None = None) -> None:
    printable = " ".join(cmd)
    print(f"\n$ {printable}", flush=True)
    r = subprocess.run(cmd, cwd=cwd or ROOT)
    if r.returncode != 0:
        raise SystemExit(f"FAILED ({r.returncode}): {printable}")


PY = sys.executable


def lint() -> None:
    # ruff format IS black (byte-compatible); ruff's I rules ARE isort. One tool, so two
    # formatters can never disagree about the same file.
    run(PY, "-m", "ruff", "format", "--check", "--diff", ".")
    run(PY, "-m", "ruff", "check", ".")
    run(PY, "-m", "mypy", "runprov/")


def test() -> None:
    run(PY, "-m", "pytest")


def build() -> None:
    if (ROOT / "dist").is_dir():
        shutil.rmtree(ROOT / "dist")  # never check a stale artifact
    run(PY, "-m", "build")
    run(
        PY, "-m", "twine", "check", "--strict", *[str(p) for p in sorted((ROOT / "dist").iterdir())]
    )
    # Install the WHEEL into a clean environment and import it from somewhere else. This
    # is what catches a file that is in git and missing from the package -- the source
    # tree would have imported it happily.
    with tempfile.TemporaryDirectory() as tmp:
        venv = Path(tmp) / "v"
        run(PY, "-m", "venv", str(venv))
        vpy = venv / ("Scripts" if sys.platform == "win32" else "bin") / "python"
        wheel = next((ROOT / "dist").glob("*.whl"))
        run(str(vpy), "-m", "pip", "install", "--quiet", str(wheel))
        run(str(vpy), "-c", "import runprov; print(runprov.__version__)", cwd=Path(tmp))


def setup() -> None:
    run(PY, "-m", "pip", "install", "-e", ".[dev]")
    run(PY, "-m", "pre_commit", "install")
    print("\nhooks installed. `python ci.py` runs everything CI runs.")


STEPS = {"lint": lint, "test": test, "build": build, "setup": setup}

if __name__ == "__main__":
    wanted = sys.argv[1:] or ["lint", "test", "build"]
    for name in wanted:
        if name not in STEPS:
            raise SystemExit(f"unknown step {name!r}; choose from {', '.join(STEPS)}")
    for name in wanted:
        print(f"\n=== {name} ===")
        STEPS[name]()
    print("\nOK — every step passed.")
