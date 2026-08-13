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
import tarfile
import tempfile
import zipfile
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
    # The coverage gate lives HERE and not in pyproject's `addopts`, so a bare `pytest`
    # still works for a downstream packager without pytest-cov installed. --cov-branch is
    # the gate: statement coverage read 100% while five conditions had never been evaluated
    # both ways.
    run(
        PY,
        "-m",
        "pytest",
        "--cov=runprov",
        "--cov-branch",
        "--cov-report=term-missing",
        "--cov-fail-under=100",
    )


#: Written and run inside the clean venv: the installed wheel must be able to RECORD a run,
#: not only be imported. Kept as one string so the check is the same on every platform.
_RECORD_ONE_RUN = (
    "import pathlib, runprov; "
    "runprov.configure(root=pathlib.Path('.'), run_log=pathlib.Path('h.jsonl')); "
    "pathlib.Path('in.tsv').write_text('a\\n', encoding='utf-8'); "
    "r = runprov.Run('smoke', provenance=pathlib.Path('p.json')); "
    "r.input('in.tsv'); "
    "fh = r.open_output('out.tsv'); fh.write('b\\n'); fh.close(); "
    "r.write(pathlib.Path('p.json')); "
    "print('recorded ok')"
)


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
        # PEP 561 does not apply without this file INSIDE the wheel, and its absence is
        # invisible: the source tree type-checks fine, the wheel installs fine, and every
        # consumer silently gets an untyped package. Checked against the built artifact
        # rather than against the source tree, because the source tree is not what ships.
        with zipfile.ZipFile(wheel) as zf:
            names = zf.namelist()
        if "runprov/py.typed" not in names:
            raise SystemExit(
                f"py.typed is NOT in the wheel ({wheel.name}); PEP 561 does not apply and "
                f"every consumer's type checker will ignore this package's annotations. "
                f"Wheel contents: {sorted(names)}"
            )
        run(str(vpy), "-m", "pip", "install", "--quiet", str(wheel))
        run(str(vpy), "-c", "import runprov; print(runprov.__version__)", cwd=Path(tmp))
        # The CLI is a second entry point and fails separately from the import: a module
        # that imports fine can still have a broken `__main__`, and `python -m runprov log`
        # is how the README tells a reader to inspect their own history. Given a REAL
        # record rather than an empty file -- an empty history exits non-zero on purpose,
        # so pointing this at /dev/null tested the error path and called it success.
        run(str(vpy), "-c", _RECORD_ONE_RUN, cwd=Path(tmp))
        run(str(vpy), "-m", "runprov", "log", "--log", "h.jsonl", cwd=Path(tmp))
        run(str(vpy), "-m", "runprov", "log", "--log", "h.jsonl", "--format", "yaml", cwd=Path(tmp))
        run(str(vpy), "-m", "runprov", "lineage", "--log", "h.jsonl", cwd=Path(tmp))
        # The CONSOLE SCRIPT, which is a different entry point from `-m` and fails
        # separately. `uvx runprov` and `pipx run runprov` resolve this one and cannot
        # reach a `-m` module at all, so without it the "a reviewer can install it and
        # check your claims" argument is one command short of true. Its absence is quiet:
        # every `-m` invocation keeps working.
        script = venv / ("Scripts" if sys.platform == "win32" else "bin") / "runprov"
        if sys.platform == "win32":
            script = script.with_suffix(".exe")
        if not script.exists():
            raise SystemExit(f"the `runprov` console script is NOT in the venv ({script})")
        run(str(script), "log", "--log", "h.jsonl", cwd=Path(tmp))

    # THE SDIST, which nothing checked. `twine check` reads its metadata and never builds
    # it, so a file missing from the sdist is invisible until someone installs with
    # `--no-binary`, or a downstream packager (conda-forge, a distro, spack) tries to
    # rebuild from source -- which is exactly the audience a reproducibility package has.
    # Unpack it somewhere else and build a wheel from THAT, with no source tree in reach.
    with tempfile.TemporaryDirectory() as tmp:
        sdist = next((ROOT / "dist").glob("*.tar.gz"))
        with tarfile.open(sdist) as tf:
            members = {n.split("/", 1)[-1] for n in tf.getnames()}
            tf.extractall(tmp, filter="data")
        # `include` in pyproject is an EXPLICIT list, so a doc is dropped from the tarball by
        # being forgotten rather than by being excluded -- and nothing said so. CHANGELOG.md
        # and SECURITY.md were both missing while the shipped README linked to both, and the
        # only reader affected is the one this sdist exists for: a packager rebuilding from
        # source, with no security policy to read and no changelog to attribute a version to.
        # `examples/summarise.py` is here because the SUITE RUNS IT: shipping the tests
        # without the file one of them executes would make the sdist's own tests fail for
        # the packager who runs them, which is the one audience this check exists for.
        want = {
            "CHANGELOG.md",
            "CITATION.cff",
            "CODE_OF_CONDUCT.md",
            "LICENSE",
            "README.md",
            "SECURITY.md",
            "ci.py",
            "examples/summarise.py",
            "examples/data/measurements.tsv",
        }
        if missing := sorted(want - members):
            raise SystemExit(
                f"{sdist.name} is missing {missing}. Anything not named in "
                f"[tool.hatch.build.targets.sdist] include is silently left out."
            )
        unpacked = next(Path(tmp).glob("runprov-*"))
        out = Path(tmp) / "wheel"
        run(PY, "-m", "build", "--wheel", "--outdir", str(out), str(unpacked))
        venv = Path(tmp) / "v"
        run(PY, "-m", "venv", str(venv))
        vpy = venv / ("Scripts" if sys.platform == "win32" else "bin") / "python"
        run(str(vpy), "-m", "pip", "install", "--quiet", str(next(out.glob("*.whl"))))
        run(str(vpy), "-c", "import runprov; print('sdist ok', runprov.__version__)", cwd=Path(tmp))


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
