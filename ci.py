#!/usr/bin/env python3
# Copyright (c) 2026 Assistance Publique – Hôpitaux de Paris (AP-HP),
#                    Hôpital Henri-Mondor, and Taylor Thompson
# Licensed under the CeCILL-B Free Software License Agreement — see LICENSE.
"""Run the CI locally — the SAME commands the workflow runs, because it runs these.

    python ci.py            everything, in the order CI runs it
    python ci.py lint       ruff format --check, ruff check, mypy
    python ci.py test       pytest with the coverage gate
    python ci.py build      release-check, build, twine check --strict, install the wheel
    python ci.py setup      install the dev extras and the pre-commit hooks
    python ci.py release-check   the version copies, the tag and the citation's year
                            (run by `build`; set RUNPROV_RELEASE_TAG=vX.Y.Z to rehearse
                            what a tag push would check)

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

import os
import re
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


def _coverage_args() -> list[str]:
    """The pytest coverage flags, and whether the 100% floor is among them.

    A function of its own so the decision is testable without running the suite: the one
    thing that must never happen quietly is the floor going away.
    """
    # The coverage gate lives HERE and not in pyproject's `addopts`, so a bare `pytest`
    # still works for a downstream packager without pytest-cov installed. --cov-branch is
    # the gate: statement coverage read 100% while five conditions had never been evaluated
    # both ways.
    cov = ["--cov=runprov", "--cov-branch", "--cov-report=term-missing"]
    # THE FLOOR IS ON BY DEFAULT, and off only where it is unreachable by construction
    # rather than by regression: 22 tests need a FIFO, a symlink or a file `chmod(0o000)`
    # actually makes unreadable, and Windows provides none of the three, so they skip and
    # the lines they cover go unmeasured. The Windows matrix leg in `test.yml` sets this,
    # and nothing else does -- a floor that quietly lowered itself by sniffing the platform
    # would be the same failure this repository keeps finding, so it has to be asked for in
    # a file a reader can see.
    if os.environ.get("RUNPROV_COVERAGE_FLOOR", "").lower() == "off":
        print(
            "\n!! coverage FLOOR DISABLED by RUNPROV_COVERAGE_FLOOR=off. The suite still\n"
            "!! runs and coverage is still reported -- but 100% is asserted only where the\n"
            "!! whole suite can run. If you are not the Windows CI leg, unset this.\n",
            flush=True,
        )
    else:
        cov.append("--cov-fail-under=100")
    return cov


def test() -> None:
    run(PY, "-m", "pytest", *_coverage_args())


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


#: Every place the version is written. FOUR copies, and until this existed nothing failed
#: when they drifted -- a mismatch would have been found at release, in the one build that
#: cannot be re-uploaded, and it makes the citation cite a version that was never published.
_VERSION_SOURCES = {
    "pyproject.toml": r'^version = "([^"]+)"',
    "runprov/__init__.py": r'^__version__ = "([^"]+)"',
    "CITATION.cff": r"^version: (\S+)",
    # BOTH HEADING SHAPES, because this file uses two. Keep a Changelog puts the version in
    # the BRACKETS and the date after the dash -- `## [0.2.0] — 2026-08-19` -- while the
    # pre-release heading here is `## [Unreleased] — 0.1.0`, with the version after the dash
    # instead. The original pattern read the field AFTER the dash unconditionally, so against
    # a real release it captured the DATE and reported "the version copies disagree ...
    # 'CHANGELOG.md': '2026-08-19'". Found the first time anyone cut a version, which is the
    # worst moment to find it and the reason it is fixed before there is a release to cut.
    #
    # Anchored on a LEADING DIGIT so `## [Unreleased]` with no version, or any other bracketed
    # word, is skipped rather than mistaken for one. Indifferent to the dash character, which
    # differs between the two shapes.
    "CHANGELOG.md": r"^## \[(?:Unreleased\] — )?(\d[^\]\s]*)",
}


def release_check() -> None:
    """Refuse to build anything a release cannot honestly claim.

    Run as part of `build`, so it guards the local gate, `test.yml` and `publish.yml`
    alike. The tag comes from the environment because the step loop consumes argv:
    `RUNPROV_RELEASE_TAG` locally, `GITHUB_REF_NAME` in Actions -- which is why the tag
    half of this only engages where a tag actually exists.

    Three things a tag can get wrong and no human reliably catches:

    * The four version copies disagree, so the wheel, the import and the citation name
      different versions of the same upload.
    * The TAG disagrees with all of them. `git tag v0.2.0` on a tree that still says
      0.1.0 publishes 0.1.0 -- and `v0.2.0` can never be published, because PyPI has
      the file name now.
    * The version is announced while the CHANGELOG still says `[Unreleased]` and
      `CITATION.cff` has no `date-released`, so the citation this project ships has no
      year in it.
    """
    found: dict[str, str] = {}
    for name, pattern in _VERSION_SOURCES.items():
        text = (ROOT / name).read_text(encoding="utf-8")
        m = re.search(pattern, text, re.M)
        if m is None:
            raise SystemExit(f"no version found in {name} (looked for {pattern!r})")
        found[name] = m.group(1)
    if len(set(found.values())) != 1:
        raise SystemExit(f"the version copies disagree: {found}")
    version = next(iter(found.values()))

    tag = os.environ.get("RUNPROV_RELEASE_TAG") or os.environ.get("GITHUB_REF_NAME", "")
    tagged = bool(re.fullmatch(r"v\d.*", tag))
    if tagged and tag != f"v{version}":
        raise SystemExit(
            f"tag {tag} does not match version {version}; PyPI would receive {version} "
            f"and {tag} could never be published afterwards"
        )

    changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    heading = re.search(r"^## \[([^\]]+)\]", changelog, re.M)
    unreleased = heading is not None and heading.group(1).lower() == "unreleased"
    if tagged and unreleased:
        raise SystemExit(
            f"{tag} is a release tag, but CHANGELOG.md still heads {version} as "
            f"[Unreleased]; date the section before tagging"
        )
    if not unreleased and not re.search(
        r"^date-released:", (ROOT / "CITATION.cff").read_text("utf-8"), re.M
    ):
        raise SystemExit(
            f"CHANGELOG.md presents {version} as released, but CITATION.cff has no "
            f"date-released, so the citation this project ships has no year in it"
        )
    print(f"release-check ok — {version}" + (f", tag {tag}" if tagged else ", untagged build"))


def build() -> None:
    release_check()
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
            "examples/format_compatibility.py",
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
        # AND RUN THE SUITE THE TARBALL SHIPS, from the tarball. Everything above proves the
        # sdist BUILDS and IMPORTS; nothing proved its tests pass, and the audience for an
        # sdist is precisely the person who runs them -- a Debian, conda-forge, Nix or spack
        # packager. That gap was not hypothetical: excluding PUBLISHING.md and LICENSING.md
        # (correctly) left a test asserting they exist, so the shipped suite failed on its own
        # tarball while every check here passed. Found by review, on a version that could
        # never have been re-uploaded.
        run(str(vpy), "-m", "pip", "install", "--quiet", "pytest", "pyyaml")
        run(str(vpy), "-m", "pytest", "-q", "-p", "no:cacheprovider", "tests/", cwd=unpacked)


def setup() -> None:
    run(PY, "-m", "pip", "install", "-e", ".[dev]")
    run(PY, "-m", "pre_commit", "install")
    print("\nhooks installed. `python ci.py` runs everything CI runs.")


STEPS = {
    "lint": lint,
    "test": test,
    "build": build,
    "setup": setup,
    "release-check": release_check,
}

if __name__ == "__main__":
    wanted = sys.argv[1:] or ["lint", "test", "build"]
    for name in wanted:
        if name not in STEPS:
            raise SystemExit(f"unknown step {name!r}; choose from {', '.join(STEPS)}")
    for name in wanted:
        print(f"\n=== {name} ===")
        STEPS[name]()
    print("\nOK — every step passed.")
