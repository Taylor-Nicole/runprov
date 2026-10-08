#!/usr/bin/env python3
# Copyright (c) 2026 Assistance Publique – Hôpitaux de Paris (AP-HP),
#                    Hôpital Henri-Mondor, and Taylor Nicole Thompson
# SPDX-License-Identifier: BSD-3-Clause
# Licensed under the BSD 3-Clause License — see LICENSE.
"""Run the CI locally — the SAME commands the workflow runs, because it runs these.

    python ci.py            everything, in the order CI runs it
    python ci.py lint       ruff format --check, ruff check, mypy
    python ci.py test       pytest with the coverage gate
    python ci.py build      release-check, build, twine check --strict, install the wheel
    python ci.py setup      install the dev extras and the pre-commit hooks
    python ci.py surface    rewrite docs/public-surface.txt from what the package exposes
    python ci.py torture    damage a real record and read it back (tools/torture.py)
    python ci.py bench      prove tools/bench.py can still tell its verdicts apart
    python ci.py release-check   the version copies, the tag and the citation's year
                            (run by `build`; set RUNPROV_RELEASE_TAG=vX.Y.Z to rehearse
                            what a tag push would check)

`torture` IS NOT IN THE DEFAULT GATE, because it builds and tears down about ninety trees
and takes roughly a minute. It exits 0 today; it exited 1 until ADR-0005 made every
provenance write atomic, which is the finding it was written to produce.

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

import json
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

#: COVERAGE'S OWN REPORT, WRITTEN BY THE GATE RUN AND READ BACK BY IT [K-37]. The README quotes
#: the statement and branch counts to convey scale, and that sentence has now drifted FIVE times.
#: L-09's fix field asked for the structural remedy four drifts ago — *"have `ci.py` regenerate
#: these numbers so they cannot drift again"* — and each repair since was another exact number
#: that went stale, the last one taken from the SUITE'S AST PROXY rather than from coverage.
#:
#: THE PROXY RUNS 5.2% HIGH, PERMANENTLY, AND THAT IS WHY THE DOCUMENT AND THE GATE MUST SHARE
#: ONE INSTRUMENT. Measured: 5 829 statements by the AST walk against 5 543 by coverage, a ratio
#: of 1.052 — so a README quoting coverage while a guard compares against the proxy burns half of
#: a 10% tolerance band from the day it is written, and the next repair reaches for the guard's
#: number again. That is the mechanism behind all five drifts.
#:
#: Gitignored: it is derived, it changes with every line added, and a provenance tool whose
#: repository tracks a stale coverage artefact is a poor advertisement.
SCALE_REPORT = ROOT / ".coverage-scale.json"


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
    attribution_check()


#: A commit message line that names an assistant as CO-AUTHOR. Matched on the trailer's
#: MEANING -- a `Co-Authored-By:` key whose value names the assistant -- rather than on a
#: display name: `Claude Opus 5` and `Claude Opus 5 (1M context)` were two spellings of the
#: same thing, and matching the name would have missed 61 of the 230 removed on 2026-09-15.
#: A bare `claude` is deliberately NOT enough; this history contains a commit whose SUBJECT
#: is about the `.claude/` directory, and it is a true statement that must survive.
_ATTRIBUTION = re.compile(r"^\s*Co-authored-by\s*:.*(claude|anthropic)", re.I | re.M)


def _attribution_offenders(log: str) -> list[str]:
    """The SHAs in `git log --format=%H%x00%B%x00%x00` output whose message carries one.

    A pure function so the decision is testable without a repository, which is the same
    reason `_coverage_args` is one.
    """
    bad = []
    for chunk in log.split("\0\0\n"):
        if not chunk.strip():
            continue
        sha, _, body = chunk.partition("\0")
        if _ATTRIBUTION.search(body):
            bad.append(sha.strip()[:9])
    return bad


def attribution_check() -> None:
    """Refuse a history that names an assistant as co-author. Run as part of `lint`.

    WHY THIS EXISTS IN CI AND NOT ONLY IN A HOOK. `.git/hooks` is not versioned, so a fresh
    clone has no hook and the first commit from it is unguarded. This runs wherever `lint`
    runs, which is every push.

    WHY IT MATTERS MORE THAN IT LOOKS. GitHub parses `Co-Authored-By:` and counts the named
    account as a contributor. Removing 230 of them on 2026-09-15 cost a rewrite of 272
    commits, a force-push of a public repository and 253 regenerated SHA citations -- and
    three closed pull requests STILL hold frozen `refs/pull/*/head` snapshots of the old
    commits, which GitHub refuses every write to and no API can delete. A trailer that
    reaches a branch with a pull request is permanent.

    IT CANNOT PASS VACUOUSLY. `git log` returning nothing would otherwise be indistinguishable
    from a clean history -- the failure this project has caught five times -- so an empty log
    is an error, not a pass.
    """
    proc = subprocess.run(
        ["git", "log", "--all", "--format=%H%x00%B%x00%x00"],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        raise SystemExit(f"attribution-check: git log failed: {proc.stderr.strip()}")
    seen = proc.stdout.count("\0\0\n")
    if seen == 0:
        raise SystemExit(
            "attribution-check: git log returned no commits, so this check examined "
            "nothing. A green that means 'nothing was looked at' is the defect it guards."
        )
    bad = _attribution_offenders(proc.stdout)
    if bad:
        raise SystemExit(
            f"attribution-check: {len(bad)} commit(s) name an assistant as co-author: "
            f"{', '.join(bad[:10])}{' …' if len(bad) > 10 else ''}"
        )
    print(f"attribution-check ok — {seen} commits, none naming an assistant as co-author")


def _coverage_args() -> list[str]:
    """The pytest coverage flags, and whether the 100% floor is among them.

    A function of its own so the decision is testable without running the suite: the one
    thing that must never happen quietly is the floor going away.
    """
    # The coverage gate lives HERE and not in pyproject's `addopts`, so a bare `pytest`
    # still works for a downstream packager without pytest-cov installed. --cov-branch is
    # the gate: statement coverage read 100% while five conditions had never been evaluated
    # both ways.
    cov = [
        "--cov=runprov",
        "--cov-branch",
        "--cov-report=term-missing",
        #: AND COVERAGE'S OWN TOTALS TO A FILE [K-37], so `scale_drift` below compares the
        #: README's figures with the instrument that measured them rather than with a proxy.
        f"--cov-report=json:{SCALE_REPORT}",
    ]
    # THE FLOOR IS ON BY DEFAULT, and off only where it is unreachable by construction rather
    # than by regression: some tests need a FIFO, a symlink or a file `chmod(0o000)` actually
    # makes unreadable, Windows provides none of the three, so they skip and the lines they
    # cover go unmeasured. The Windows matrix leg in `test.yml` sets this, and nothing else
    # does -- a floor that quietly lowered itself by sniffing the platform would be the same
    # failure this repository keeps finding, so it has to be asked for in a file a reader can
    # see.
    #
    # HOW MANY TESTS, AND IT IS SAID IN ONE PLACE [Audit N]. This comment used to say "22
    # tests" while `test.yml`'s own comment said "49 tests" for the same set, with a dated
    # measurement of 722 passed / 50 skipped beside it. Two figures for one fact, on no
    # instrument, in the two files that set the gate -- and correcting both would only reset
    # the clock, which is what this repository keeps paying for. So the count lives in
    # `test.yml` beside the `coverage_floor: "off"` it justifies, dated and with its run id,
    # and this comment states the mechanism and no number.
    #
    # AND THIS FLOOR IS THE PROBE INSTRUMENT, which nothing credited until Audit N looked for
    # one [guards-11]. The suite's filesystem probes (`_can_symlink`, `_chmod_denies_read` at
    # the top of `tests/test_runprov.py`) decide whether whole tests run, and a probe that
    # silently starts answering False disables up to a fifth of the suite while a bare
    # `pytest` stays green with zero failures. This floor catches that WITHOUT asserting
    # anything about a probe, because an unmeasured line is the probe's consequence: measured
    # against a 2-missing / 0-partial baseline, forcing `_can_symlink` False is 16 missing / 3
    # partial and forcing `_chmod_denies_read` False is 8 / 1, each naming the production
    # lines that went unmeasured. The probe comment in the suite carries the full table and
    # the two probes this cannot see.
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


#: THE QUOTED FIGURE AND THE KEY COVERAGE REPORTS IT UNDER. Two figures, one sentence, and the
#: patterns are anchored on the sentence so rewording it fails loudly rather than checking
#: nothing — which is the half `test_the_quoted_scale_figures_have_not_drifted_out_of_meaning`
#: already learned when a known-stale figure sat inside the tolerance of a baseline it had never
#: been compared against.
_SCALE_FIGURES = {
    "statements": (r"about ([\d,]+) statements and [\d,]+ branches", "num_statements"),
    "branches": (r"about [\d,]+ statements and ([\d,]+) branches", "num_branches"),
}

#: TEN PER CENT IS THE ARGUMENT AND NOT A CONVENIENT SLACK. These figures exist to convey SCALE
#: — *a small package, fully covered*. A number 5% out still conveys it; one 17% out does not,
#: and by then nobody trusts the rest of the section either.
SCALE_TOLERANCE = 0.10


def scale_drift(readme: str, totals: dict[str, int]) -> list[str]:
    """Which quoted scale figures are more than 10% from COVERAGE'S OWN totals. K-37.

    A function rather than inline code so it is testable without a gate run, which is the same
    reason `_coverage_args` is one: the thing that must never happen quietly is this check
    becoming vacuous.

    IT LIVES HERE AND NOT ONLY IN THE SUITE BECAUSE OF THE ORDER OF EVENTS. Coverage's totals do
    not exist until the suite has finished, so a test inside that run can only read the PREVIOUS
    run's file — and CI checks out a fresh tree every time, where there is no previous run. The
    comparison therefore has to happen after the run, which is here. The suite tests this
    function and asserts that `test()` calls it.
    """
    drifted = []
    for label, (pattern, key) in _SCALE_FIGURES.items():
        found = re.search(pattern, readme)
        if not found:
            drifted.append(f"{label}: the sentence quoting it is gone from README.md ({pattern})")
            continue
        stated = int(found.group(1).replace(",", ""))
        actual = totals[key]
        if abs(stated - actual) > SCALE_TOLERANCE * actual:
            drifted.append(f"{label}: README says {stated:,}, coverage measured {actual:,}")
    return drifted


def _check_scale() -> None:
    """Hold the README's two quoted figures to the numbers the run just measured. K-37."""
    if not SCALE_REPORT.is_file():
        raise SystemExit(
            f"scale-check: {SCALE_REPORT.name} was not written, so the README's statement and "
            "branch figures were compared with nothing. `_coverage_args` asks pytest-cov for it; "
            "a missing file means the report flag went away, not that the figures are fine."
        )
    totals = json.loads(SCALE_REPORT.read_text(encoding="utf-8"))["totals"]
    drifted = scale_drift((ROOT / "README.md").read_text(encoding="utf-8"), totals)
    if drifted:
        raise SystemExit(
            "scale-check: these figures no longer convey the scale they were written to convey "
            f"— re-measure and update README.md: {'; '.join(drifted)}"
        )
    print(
        f"scale-check ok — README within {SCALE_TOLERANCE:.0%} of coverage's own "
        f"{totals['num_statements']} statements and {totals['num_branches']} branches"
    )


def test() -> None:
    run(PY, "-m", "pytest", *_coverage_args())
    _check_scale()


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


def matrix_check() -> None:
    """Refuse a tag whose COMMIT has not been through the hosted matrix. Added 2026-10-01.

    **NOT part of `build`, and that is deliberate twice over.** It needs the network and the
    `gh` CLI, which the local gate must not; and inside Actions it would be a run asking about
    itself. It is a step a human runs before `git tag`, which is where the gap was.

    THE GAP IT CLOSES, measured: the Windows leg was red from 2026-09-30 to 2026-10-01 and
    thirteen commits went by with the local gate green each time. `ci.py` runs ONE platform, so
    *the gate is green* and *CI is green* are different claims, and the second was being
    asserted from the first. **`publish.yml` does not cover it either** — that workflow gates
    the upload on `needs: [build, test]`, but its own test job is `ubuntu-latest` only, so the
    sole Windows signal is `test.yml` on the commit being tagged.

    WHAT THE THREE FAILURES WERE, because the shape recurs: fixtures calling
    `Path.write_text` with no `newline=""`, which writes CRLF on Windows while this package
    hashes the bytes AS WRITTEN. `chain` answered CANNOT_CHECK and `report` answered ALTERED,
    both correctly, about files the fixtures had changed by accident.

    IT READS THE WINDOWS JOB BY NAME rather than the run's conclusion. A conclusion can be
    `success` while a leg was skipped, and `skipped` is not `passed` — the distinction that
    makes `0` mean *checked and fine* everywhere else in this package.
    """
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"], capture_output=True, text=True, check=True
    ).stdout.strip()
    if subprocess.run(["git", "status", "--porcelain"], capture_output=True, text=True).stdout:
        raise SystemExit(
            "the tree is dirty, so the matrix cannot have seen what you are about to tag"
        )
    probe = subprocess.run(
        [
            "gh",
            "run",
            "list",
            "--limit",
            "10",
            "--commit",
            head,
            "--workflow",
            "test.yml",
            "--json",
            "databaseId,status,conclusion",
        ],
        capture_output=True,
        text=True,
    )
    if probe.returncode != 0:
        raise SystemExit(
            f"could not ask GitHub about {head[:7]} — `gh` is required for this step and it "
            f"said: {probe.stderr.strip()[:200]}"
        )
    runs = json.loads(probe.stdout or "[]")
    if not runs:
        raise SystemExit(
            f"no `test.yml` run for {head[:7]}. Push the commit and let the matrix finish "
            "BEFORE tagging: a tag publishes to PyPI and a PyPI file can never be replaced."
        )
    run = runs[0]
    if run["status"] != "completed":
        raise SystemExit(f"the matrix for {head[:7]} is still {run['status']} — wait for it")
    jobs = json.loads(
        subprocess.run(
            ["gh", "run", "view", str(run["databaseId"]), "--json", "jobs"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout
    )["jobs"]
    #: EVERY JOB BY NAME, so a leg that was SKIPPED is not read as a leg that passed.
    bad = {j["name"]: (j.get("conclusion") or j.get("status")) for j in jobs}
    bad = {name: got for name, got in bad.items() if got != "success"}
    if bad:
        listed = "\n".join(f"    {got:10} {name}" for name, got in sorted(bad.items()))
        raise SystemExit(f"the matrix for {head[:7]} is not green:\n{listed}")
    legs = sorted(j["name"] for j in jobs)
    if not any("windows" in name for name in legs):
        raise SystemExit(
            f"no windows job in the matrix for {head[:7]} — this step exists for that leg: {legs}"
        )
    print(f"matrix-check ok — {len(jobs)} job(s) green on {head[:7]}, windows included")


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

    # `twine check --strict` PASSES WITHOUT CHECKING THE DESCRIPTION when readme_renderer has
    # no markdown backend installed: it validates the metadata, finds it cannot render
    # `text/markdown`, and reports PASSED anyway. That is the check most worth having, because
    # PyPI freezes the rendered description at upload and the only fix afterwards is another
    # release. Measured 2026-09-10 on this very venv: `readme_renderer.markdown.render()`
    # returned None while `twine check --strict` printed PASSED for both artifacts.
    #
    # So the renderer is proved to work FIRST, on a fragment whose output is known. A positive
    # control, because "nothing failed" and "nothing was examined" print the same word.
    # chr(10) rather than an escaped newline: this string is SOURCE passed to `-c`, and a
    # literal newline inside its single-quoted argument is a SyntaxError. The first
    # version of this probe had exactly that, so it exited non-zero and reported a
    # missing renderer — a guard failing for a reason other than the one it names,
    # which is worse than no guard at all.
    probe = (
        "import readme_renderer.markdown as m, sys;"
        "h = m.render('# t' + chr(10) + chr(10) + '`c`' + chr(10));"
        "sys.exit(0 if h and '<h1' in h and '<code>' in h else 1)"
    )
    if subprocess.run([PY, "-c", probe], capture_output=True).returncode != 0:
        raise SystemExit(
            "readme_renderer cannot render markdown, so `twine check --strict` would pass "
            "without looking at the description — which is the part PyPI freezes.\n"
            f"Install the backend into this environment:  {PY} -m pip install comrak"
        )
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
        #
        # THE LIST BELOW WENT STALE THE FIRST TIME `include` GREW, which is the failure this
        # whole check exists to prevent, one level up: L-106 added `GETTING-STARTED.md` and
        # `docs` to `include` and closed with "Both now ship in the sdist", extending neither
        # `want` nor any test. Deleting that line from `include` again left every check green
        # with all three ADRs gone from the tarball. So the guard is now DERIVED from the list
        # it guards -- see `named` below -- and this set keeps only what derivation cannot
        # say: individual files inside a directory entry.
        want = {
            "CHANGELOG.md",
            "CITATION.cff",
            "CODE_OF_CONDUCT.md",
            "CONTRIBUTING.md",
            "GETTING-STARTED.md",
            "LICENSE",
            "README.md",
            "SECURITY.md",
            "WHY.md",
            "ci.py",
            "examples/summarise.py",
            "examples/format_compatibility.py",
            "examples/data/measurements.tsv",
            # THE ADRs THEMSELVES, not merely something under `docs/`. A directory entry is
            # satisfied by any one member, and `docs/adr/README.md` is an INDEX whose three
            # relative links go to these files -- so the shape to guard against is the one
            # where the index ships and everything it points at does not.
        }
        # EVERY ADR, READ FROM THE DIRECTORY rather than named one by one. Three were listed
        # here and a fourth would have been silently unguarded — the same hand-maintained
        # list, one level down, that this whole check exists to replace. A directory entry in
        # `include` is satisfied by ANY one member, and `docs/adr/README.md` is an index whose
        # links go to these files, so "something under docs/ shipped" is not the property
        # wanted: each ADR is.
        want |= {f"docs/adr/{f.name}" for f in sorted((ROOT / "docs" / "adr").glob("*.md"))}
        if missing := sorted(want - members):
            raise SystemExit(
                f"{sdist.name} is missing {missing}. Anything not named in "
                f"[tool.hatch.build.targets.sdist] include is silently left out."
            )
        # EVERY ENTRY IN `include` MUST HAVE PRODUCED SOMETHING, derived from pyproject so
        # that adding a file to the list extends this check by itself. It catches the other
        # direction too: an entry that names a path which no longer exists ships nothing and
        # says nothing, which is how a rename quietly empties the tarball.
        pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
        block = pyproject[pyproject.index("[tool.hatch.build.targets.sdist]") :]
        block = block[block.index("include = [") : block.index("]", block.index("include = ["))]
        named = re.findall(r'"([^"]+)"', block)
        if not named:
            raise SystemExit("could not read [tool.hatch.build.targets.sdist] include")
        empty = sorted(
            entry
            for entry in named
            if not any(m == entry or m.startswith(entry + "/") for m in members)
        )
        if empty:
            raise SystemExit(
                f"{sdist.name} contains nothing for {empty}, which pyproject's sdist "
                f"`include` names. Either the entry is stale or the files moved."
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


SURFACE_FILE = ROOT / "docs" / "public-surface.txt"

#: The explanation that ships with the surface list. Held here so the file is entirely
#: derived — see `surface()`.
SURFACE_HEADER = """# THE PUBLIC SURFACE, WRITTEN DOWN SO THAT REMOVING ANY OF IT IS VISIBLE.
#
# Generated by `test_the_public_surface_matches_what_is_written_down`, which also enforces it.
# Regenerate with:  python ci.py surface
#
# WHY THIS FILE EXISTS. A downstream user upgraded runprov and it broke. They surveyed the
# history for `feat!` and `BREAKING` and found nothing — because the three commits that broke
# them were typed `refactor:`:
#
#     25fd221  refactor  withdrew PIN_UNSAFE, VOLATILE, VOLATILE_JSON, default_generation,
#                        default_run_id from the package __all__
#     8a7452e  refactor  withdrew Capture, MemorySink, git, installed_packages, write_snapshot
#     f9b5a74  refactor  renamed Run.write_json to Run.output_json
#
# A CONVENTION THAT RECORDS INTENT CANNOT SEE A BREAKAGE THE AUTHOR DID NOT INTEND. Every one
# of those was made deliberately and none was thought of as breaking, because each was framed
# as "this was never really promised" — which is true of the decision and false for the person
# who was importing the name.
#
# So this file does not record intent. It records the surface. Removing or renaming anything
# below forces an edit here, which shows up in the diff, which is the moment to type the
# commit `feat!` and write it in the CHANGELOG.
#
#   Name  [class] / [callable] / [value]   a promised top-level name
#   Name.member                            a public method or property of a promised class
#   Name:field                             a public dataclass field of a promised class
"""


def public_surface() -> list[str]:
    """The public surface as the package actually presents it, one line per promise.

    DEFINED HERE AND NOWHERE ELSE. `ci.py surface` writes the file from this and the suite
    checks the file against this, so the generator and the checker cannot disagree -- which is
    the failure mode this repository has repaired more often than any other.

    `__all__` IS NOT THE WHOLE SURFACE, and that is the half that made the reported breakage
    invisible: a promised class carries its public methods, so renaming `Run.write_json` to
    `Run.output_json` broke callers while `__all__` never moved.
    """
    import inspect

    import runprov

    out: list[str] = []
    for name in sorted(runprov.__all__):
        obj = getattr(runprov, name)
        if inspect.isclass(obj):
            # A DATACLASS FIELD WHOSE DEFAULT IS A FUNCTION IS NOT A METHOD. The first version
            # of this listed `Project.generation` and `Project.run_id` as both: they are fields
            # holding a callable -- `Project(run_id=lambda: "r")` -- and `Project.generation()`
            # is not something anyone is promised.
            fields = set(getattr(obj, "__dataclass_fields__", {}))
            out.append(f"{name}  [class]")
            # STATIC AND CLASS METHODS COUNT TOO. `vars()` hands back the DESCRIPTOR, and
            # `inspect.isfunction` is False for both -- so a promised `@staticmethod` would
            # have been invisible to a check whose entire job is not to miss things. None
            # exists today; the hole would have opened silently the first time one did.
            out += [
                f"{name}.{m}"
                for m, v in sorted(vars(obj).items())
                if not m.startswith("_")
                and m not in fields
                and (inspect.isfunction(v) or isinstance(v, (property, staticmethod, classmethod)))
            ]
            out += [f"{name}:{f}" for f in sorted(fields) if not f.startswith("_")]
        else:
            out.append(f"{name}  [{'callable' if callable(obj) else 'value'}]")
    return out


def surface() -> None:
    """Rewrite `docs/public-surface.txt` from what the package actually exposes.

    THAT FILE SAID `python -c "import tests.surface"`, WHICH IS NOT A THING. An instruction
    that does not run is worse than none, because the reader tries it before disbelieving it
    -- and claims that stopped being true are this repository's entire subject. This is the
    command; the suite is what enforces the result.

    THE HEADER LIVES HERE, NOT IN THE FILE. It used to be read back out of the file being
    rewritten, so deleting the file made `ci.py surface` raise `FileNotFoundError` -- a
    regenerate command that cannot regenerate -- and took the explanation with it. The whole
    file is derived now, and deleting it is a recoverable mistake.
    """
    lines = public_surface()
    SURFACE_FILE.parent.mkdir(parents=True, exist_ok=True)
    SURFACE_FILE.write_text(SURFACE_HEADER + "\n".join(lines) + "\n", encoding="utf-8")
    print(f"wrote {len(lines)} surface line(s) to {SURFACE_FILE.name}")


def torture() -> None:
    """Damage a real record and read it back. NOT a gate — see the head of that file.

    Shelled out rather than imported so it runs under the same interpreter as every other
    step and cannot import anything of this file's by accident: it is an instrument pointed
    AT the package, and one that shared a process with its subject would be measuring a
    tree that this file had already touched.
    """
    run(PY, str(ROOT / "tools" / "torture.py"))


def bench() -> None:
    """Prove `tools/bench.py` can still tell CAUGHT from SURVIVED from VOID. NOT the gate.

    THE HARNESS IS AN INSTRUMENT AND AN INSTRUMENT NEEDS A CONTROL, which is the same reason
    `corpus` below asks whether the FIXTURE is honest rather than whether the package agrees
    with it. `bench.py` decides whether a guard noticed a deliberate break; if it can no longer
    produce one of its three verdicts, every answer it gives afterwards still LOOKS like an
    answer. So its own six cases run here -- one CAUGHT, one SURVIVED and four VOID, each
    asserted on the REASON it fired and not merely on the verdict, because two of them passed
    for the wrong reason while printing `ok` the first time they were written.

    NOT IN THE DEFAULT GATE. It copies this tree twelve times and runs pytest twelve times.
    `RUNPROV_BENCH_ROOT` points the copies at a fast disk; unset, each run gets a temporary
    directory. It must never point inside this repository -- `bench.py`'s control 1 refuses
    that, and the reason is in its docstring.
    """
    run(PY, str(ROOT / "tools" / "bench.py"), "selfcheck", "--python", PY)


def corpus() -> None:
    """Self-check the cross-version record corpus. NOT the gate over it — that is in the suite.

    This asks whether the committed FIXTURE is honest (no paths from the machine that built
    it, a manifest that matches the scenario on disk, a working leak scanner proved by a
    planted path). Whether the PACKAGE still agrees with those records is a different
    question, and it belongs in `ci.py test` where it runs on every leg of the matrix rather
    than in a step somebody has to remember.
    """
    run(PY, str(ROOT / "tools" / "corpus.py"), "verify")


STEPS = {
    "lint": lint,
    "test": test,
    "build": build,
    "setup": setup,
    "surface": surface,
    "torture": torture,
    #: NOT in the default gate: twelve tree copies and twelve pytest runs. See `bench`.
    "bench": bench,
    "corpus": corpus,
    "release-check": release_check,
    #: NOT in the default `lint test build`: it needs the network and `gh`. Run before a tag.
    "matrix-check": matrix_check,
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
