# Copyright (c) 2026 Assistance Publique – Hôpitaux de Paris (AP-HP),
#                    Hôpital Henri-Mondor, and Taylor Nicole Thompson
# SPDX-License-Identifier: BSD-3-Clause
# Licensed under the BSD 3-Clause License — see LICENSE.
"""The cross-version record corpus: build it, move it, and check it is honest.

THE HOLE THIS CLOSES. Every other test in this project builds a record with the same code
that reads it, so the suite cannot see the one failure a provenance package must never have:
**a record written by last year's version that this year's version reads differently, or
refuses.** That is not hypothetical here. Audit C found `sha256_tree` had silently CHANGED on
Linux and macOS inside a fix meant to make it machine-INdependent, which would have made every
directory input pinned by 0.1.0 … 0.4.0 verify STALE with nothing on disk touched — and the
suite was green through all of it, because the suite had no record older than itself.

Four other defects across the two audits had the same shape, and this harness would have
caught them mechanically, without an auditor: the history-shape-versus-sidecar-shape confusion
behind B's A-03/A-04 (fixtures hand-built in the wrong shape, so the suite and the command were
each self-consistent and mutually wrong), C-02, and C-01. Thirty agents found those. A corpus
finds them on every commit, forever, in eight seconds.

HOW IT WORKS. For each released tag: make a virtualenv, `pip install runprov==<that version>`
from PyPI, run `tools/corpus_scenario.py` inside it, and keep the resulting tree under
`tests/corpus/<version>/tree/`. The tests then materialise those trees and hold TODAY's package
to them. Generation needs the network and is manual; the corpus is committed, so the gate is
offline and runs everywhere.

WHAT IS NORMALISED, AND WHAT MUST NEVER BE. Measured rather than assumed (see the scenario's
docstring): with relative paths the ONLY absolute values that reach a record are `cwd` and
`command`, both in `prov/*.json`, `prov/*.jsonl` and `prov/*.yml`. Nothing digests those files,
so rewriting them is free.

  **The artifacts and the environment snapshot are copied BYTE FOR BYTE and never touched.**
  The artifact carries the pin whose body digest `verify` re-derives, and the snapshot's
  filename IS the digest of its body. Rewriting either would destroy the exact thing the
  corpus exists to check — a corpus that had to be edited to be moved would be measuring the
  editor. This is the rule to read before changing anything in this file.

Usage:

    python tools/corpus.py generate --all       # or --version 0.3.0; needs the network
    python tools/corpus.py verify               # self-check: is the corpus honest?
    python tools/corpus.py list
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile
import typing

ROOT = pathlib.Path(__file__).resolve().parent.parent
CORPUS = ROOT / "tests" / "corpus"
SCENARIO = ROOT / "tools" / "corpus_scenario.py"
MANIFEST = CORPUS / "MANIFEST.json"

#: The one token that survives into the committed corpus. Everything else is normalised to a
#: literal, because a token is a thing a reader has to know about and each one is a chance for
#: a test to assert against the placeholder rather than against the record.
ROOT_TOKEN = "@@RUNPROV_CORPUS_ROOT@@"  # noqa: S105 - a path placeholder, not a secret

#: Only these are rewritten. Matched on the full suffix rather than `Path.suffix`, which
#: returns `.json` for `align.prov.json` and would silently include a hypothetical
#: `artifact.json` output — the compound-suffix trap this package has already met twice.
RECORD_SUFFIXES = (".json", ".jsonl", ".yml")

#: Leak shapes, checked ON TOP of the exact generation paths rather than instead of them. The
#: exact check (the literal root, the literal venv, `$HOME`, the temp dir) is the one that is
#: complete; this second list catches a path that arrived from somewhere nobody predicted,
#: which is the only kind worth a second net.
#: AN ABSOLUTE PATH IS A LEAK, AND THE RULE IS DERIVED RATHER THAN LISTED. This was
#: `LEAK_SHAPES = ("/home/", "/Users/", "/tmp/", …)` — a hand-typed set of prefixes, which is
#: the scope pattern this file warns about forty lines below and then committed itself. The
#: repository lives under `/mnt/`, no needle matched it, and **32 files carrying the generating
#: machine's absolute path shipped in the published 0.5.0 sdist** before anyone noticed.
#:
#: So the question is no longer "is it one of these places" but "is it rooted at all". A record
#: that has been normalised carries NO absolute path except the placeholder; anything else is
#: either a leak or a normalisation that did not reach. `(?<![\w:@])` keeps URLs and
#: `scheme://host/path` out, and two segments are required so a bare `/` is not a hit.
#:
#: Measured over the whole corpus when this replaced the list: ONE distinct path flagged, the
#: real leak, in 165 files, with no false positives.
ABSOLUTE_PATH = re.compile(r"(?<![\w:@])(?:/(?:[\w.+-]+/)+[\w.+-]+|[A-Za-z]:[\\/][\w.\\/+-]+)")


# --------------------------------------------------------------------------- shared with tests
def corpus_versions() -> list[str]:
    """The versions actually present, DERIVED from the directory. Never a list.

    The gate iterates this, so dropping a tree removes it from the gate and adding one adds it,
    with nothing to remember. A hand-typed version list is the scope pattern that this project
    has now shipped seven times.
    """
    if not CORPUS.is_dir():
        return []
    return sorted((p.name for p in CORPUS.iterdir() if (p / "tree").is_dir()), key=_as_number)


def _as_number(version: str) -> tuple[int, ...]:
    """`0.10.0` sorts after `0.9.0`. String order puts it before, and would keep doing so
    quietly for exactly as long as nobody had ten minor releases."""
    return tuple(int(part) if part.isdigit() else 0 for part in version.split("."))


def materialise(version: str, dest: pathlib.Path) -> pathlib.Path:
    """Unpack one captured tree into `dest` and point its records at it. Returns `dest`.

    THE SUBSTITUTION IS POSIX-SPELLED, including on Windows: `C:/Users/…`, not `C:\\Users\\…`.
    A native Windows path inside a JSON string needs every separator escaped, and a text-level
    replace produces `"C:\\Users"` — an invalid escape that makes the record unreadable, so
    the harness would fail on the one platform whose path handling it exists to check.
    `pathlib` accepts the forward-slash spelling on Windows, so this costs nothing.
    """
    source = CORPUS / version / "tree"
    if not source.is_dir():
        raise FileNotFoundError(f"no corpus for {version}; run `python tools/corpus.py generate`")
    shutil.copytree(source, dest, dirs_exist_ok=True)
    here = dest.resolve().as_posix()
    for path in sorted(dest.rglob("*")):
        if path.is_file() and path.name.endswith(RECORD_SUFFIXES):
            text = path.read_text(encoding="utf-8")
            if ROOT_TOKEN in text:
                path.write_text(text.replace(ROOT_TOKEN, here), encoding="utf-8", newline="")
    return dest


def leaks(tree: pathlib.Path, extra: typing.Iterable[str] = ()) -> list[str]:
    """Absolute paths from the generating machine that survived normalisation.

    A committed fixture carrying somebody's `$HOME` is both a privacy problem and a
    correctness one: it makes the corpus describe the machine that built it rather than the
    version that wrote it.
    """
    found = []
    extra = [n for n in extra if n]
    for path in sorted(tree.rglob("*")):
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):  # a binary fixture is not a leak vector here
            continue
        where = path.relative_to(tree).as_posix()
        for hit in ABSOLUTE_PATH.findall(text):
            if ROOT_TOKEN in hit:
                continue
            found.append(f"{where}: {hit!r}")
        for needle in extra:  # a caller may still name something the shape cannot see
            if needle in text:
                found.append(f"{where}: {needle!r}")
    return sorted(set(found))


def scenario_digest() -> str:
    """The sha256 of the scenario that produced the corpus.

    RECORDED, AND CHECKED BY A TEST. The corpus is only meaningful as *what version X did with
    scenario S*; if S is edited and the trees are not rebuilt, every assertion still passes
    while describing a scenario that no longer exists. That is a green over nothing, which is
    the failure this whole harness was built to stop — so it may not be introduced by the
    harness itself.
    """
    # NORMALISED TEXT, NOT RAW BYTES, and Windows CI taught this rather than review. A `.py`
    # file checked out under `core.autocrlf=true` arrives with CRLF, so hashing its bytes gave
    # a different answer on Windows than on the machine that built the corpus — and the check
    # meant to prove the scenario had not changed failed on a platform where nothing had.
    #
    # This is the same rule as A-15 and C-11 inside the package itself: a content-addressed
    # thing must hash what it MEANS, not how the filesystem spelled it today. Fixing it here
    # rather than with a `.gitattributes` entry makes it independent of anyone's git config,
    # which is the stronger of the two fixes.
    text = SCENARIO.read_text(encoding="utf-8").replace("\r\n", "\n")
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------- generation
def released_versions() -> list[str]:
    """Every `v*` tag, which is every version that exists on PyPI. Derived from git."""
    out = subprocess.run(
        ["git", "tag", "--list", "v*"],  # noqa: S607 - git is on PATH by design here
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    return sorted(
        (t.strip().lstrip("v") for t in out.stdout.splitlines() if t.strip()), key=_as_number
    )


def _rechain(tree: pathlib.Path) -> int:
    """Recompute `prev` over the SCRUBBED bytes. Returns how many lines were re-linked.

    WHY THIS HAS TO EXIST, and it is a real tension rather than a tidy-up. From 0.6.0 a history
    line carries `prev`, the sha256 of the line before it **as written** (ADR-0016 R-1/R-2 —
    no canonicalisation, because a verifier hashes what it reads). `_normalise` then rewrites
    those very bytes to take this machine's paths, venv and hostname out of them. Every digest
    it signed is stale the moment it does, and `runprov chain` over the captured tree reports
    BROKEN on every edge — correctly. The bytes really did change.

    The corpus cannot keep both properties. It exists to be PUBLISHED, so the scrub is not
    optional; and the chain is over bytes, so no scrub can be invisible to it. Shipping the
    broken tree would mean a tamper-evidence tool distributing an example it accuses, and
    dropping `prev` from the capture would make the corpus blind to the one feature 0.6.0 adds.

    So the chain is rebuilt over the records as published. The fixture then attests what it can
    honestly attest — that these bytes are internally consistent — and not that they are the
    bytes some run once emitted, which the scrub has already made untrue. `rechained` goes in
    the manifest so nobody reads a corpus tree as a forensic artefact.

    Sequential by necessity: `prev` is the FIRST field (R-19), so rewriting line n changes line
    n's bytes and therefore line n+1's claim. Each line is hashed only after the one before it
    has been finalised.
    """
    relinked = 0
    for history in sorted(tree.rglob("*.jsonl")):
        lines = history.read_bytes().split(b"\n")
        trailing = lines.pop() if lines and lines[-1] == b"" else None
        out: list[bytes] = []
        for line in lines:
            try:
                record = json.loads(line)
            except ValueError:  # pragma: no cover - a corpus tree holds no torn lines
                out.append(line)
                continue
            if not isinstance(record, dict) or "prev" not in record:
                out.append(line)  # pre-0.6.0, or a line that made no claim (R-22)
                continue
            claim = "GENESIS" if not out else hashlib.sha256(out[-1]).hexdigest()
            # `prev` first, and json.dumps with the same defaults the sink uses, so the line
            # differs from what was captured in exactly the fields that were scrubbed.
            rebuilt = {"prev": claim, **{k: v for k, v in record.items() if k != "prev"}}
            out.append(json.dumps(rebuilt, default=str).encode("utf-8"))
            relinked += 1
        body = b"\n".join(out)
        history.write_bytes(body + b"\n" if trailing is not None else body)
    return relinked


def _normalise(tree: pathlib.Path, root: pathlib.Path, venv_python: pathlib.Path) -> None:
    """Rewrite the record files in place. See the module docstring for what is off limits."""
    swaps = [
        (root.resolve().as_posix(), ROOT_TOKEN),
        (str(root.resolve()), ROOT_TOKEN),
        # LITERALS, NOT TOKENS. `command` and `hostname` are informational; replacing them with
        # a plausible constant keeps the record readable and gives a test nothing to assert
        # against a placeholder by mistake.
        (str(venv_python), "python"),
        (venv_python.as_posix(), "python"),
        (os.uname().nodename if hasattr(os, "uname") else "", "corpus-host"),
        # THE SCENARIO'S OWN PATH, which is NOT under the corpus tree and so was reached by
        # none of the swaps above. It is the repository's path, and it landed in `command`,
        # `argv[0]` and `code.script_file` of every record — 32 files of it in the published
        # 0.5.0 sdist. Replaced with the bare name for the reason stated above: a literal keeps
        # the record readable, and says what ran without saying from where.
        (SCENARIO.resolve().as_posix(), SCENARIO.name),
        (str(SCENARIO.resolve()), SCENARIO.name),
    ]
    for path in sorted(tree.rglob("*")):
        if not (path.is_file() and path.name.endswith(RECORD_SUFFIXES)):
            continue
        text = path.read_text(encoding="utf-8")
        for needle, token in swaps:
            if needle:
                text = text.replace(needle, token)
        path.write_text(text, encoding="utf-8", newline="")


def generate(version: str) -> dict[str, typing.Any]:
    """Install `version` from PyPI into a throwaway venv, run the scenario, capture the tree."""
    print(f"  {version}: creating a virtualenv")
    with tempfile.TemporaryDirectory() as tmp:
        area = pathlib.Path(tmp)
        venv = area / "venv"
        subprocess.run(  # noqa: S603 - argv is built here, no shell
            [sys.executable, "-m", "venv", str(venv)], check=True, capture_output=True
        )
        python = venv / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
        print(f"  {version}: pip install runprov=={version}")
        install = subprocess.run(  # noqa: S603 - argv is built here, no shell
            [str(python), "-m", "pip", "install", "--quiet", f"runprov=={version}"],
            capture_output=True,
            text=True,
        )
        if install.returncode:
            raise SystemExit(
                f"could not install runprov=={version} — the corpus needs the network.\n"
                f"{install.stderr.strip()[:800]}"
            )
        work = area / "work"
        work.mkdir()
        print(f"  {version}: running the scenario")
        ran = subprocess.run(  # noqa: S603 - argv is built here, no shell
            [str(python), str(SCENARIO), str(work)], capture_output=True, text=True
        )
        if ran.returncode:
            raise SystemExit(f"scenario failed under {version}:\n{ran.stdout}\n{ran.stderr}")

        dest = CORPUS / version / "tree"
        if dest.exists():
            shutil.rmtree(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(work, dest)
        _normalise(dest, work, python)
        # AFTER the scrub, never before: the scrub is what invalidates the digests, so a chain
        # rebuilt first would be stale again by the time the tree is written out.
        relinked = _rechain(dest)

        remaining = leaks(dest, extra=[str(area), os.path.expanduser("~")])
        if remaining:
            raise SystemExit(
                f"{version}: the capture still carries paths from this machine, which must "
                f"never be committed:\n  " + "\n  ".join(remaining)
            )
        scenario = json.loads((dest / "scenario.json").read_text(encoding="utf-8"))
        files = sorted(p.relative_to(dest).as_posix() for p in dest.rglob("*") if p.is_file())
        note = f", {relinked} line(s) re-chained over the scrubbed bytes" if relinked else ""
        print(f"  {version}: captured {len(files)} files{note}")
        return {
            "captured_utc": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            # STATED, because a reader must not mistake a corpus tree for a forensic one. The
            # scrub rewrote the bytes the chain signed; these links are over what is published.
            "rechained": relinked,
            "generated_by_python": f"{sys.version_info[0]}.{sys.version_info[1]}",
            "scenario": scenario,
            "files": files,
        }


def generate_all(versions: list[str]) -> None:
    CORPUS.mkdir(parents=True, exist_ok=True)
    manifest: dict[str, typing.Any] = {}
    if MANIFEST.is_file():
        manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    manifest.setdefault("versions", {})
    for version in versions:
        manifest["versions"][version] = generate(version)
    manifest["scenario_sha256"] = scenario_digest()
    manifest["generated_utc"] = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    MANIFEST.write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8", newline="\n"
    )
    print(f"\n  manifest -> {MANIFEST.relative_to(ROOT)}")


# --------------------------------------------------------------------------- self-check
def self_check() -> int:
    """Is the corpus honest? Not "does the package pass it" — the suite asks that.

    A POSITIVE CONTROL IS PART OF THIS, because "no leaks found" and "nothing was scanned"
    are the same green, and this file exists to stop exactly that class of answer.
    """
    versions = corpus_versions()
    if not versions:
        print("  NO CORPUS. `python tools/corpus.py generate --all` builds it.")
        return 2
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8")) if MANIFEST.is_file() else {}
    bad = 0

    recorded = manifest.get("scenario_sha256")
    current = scenario_digest()
    if recorded != current:
        print(f"  SCENARIO CHANGED since the corpus was built ({recorded} -> {current}).")
        print("  The trees describe a scenario that no longer exists. Regenerate.")
        bad += 1

    for version in versions:
        tree = CORPUS / version / "tree"
        found = leaks(tree)
        artifacts = sorted((tree / "out").glob("*.tsv"))
        scenario = json.loads((tree / "scenario.json").read_text(encoding="utf-8"))
        if found:
            bad += 1
            print(f"  {version}: LEAKED PATHS\n    " + "\n    ".join(found))
        if not artifacts:
            bad += 1
            print(f"  {version}: no artifacts — a tree with nothing in it passes every test")
        if version not in manifest.get("versions", {}):
            bad += 1
            print(f"  {version}: present on disk and absent from the manifest")
        print(
            f"  {version}: {len(artifacts)} artifact(s), "
            f"step={scenario.get('step')}, python {scenario.get('python')}, "
            f"{'clean' if not found else 'LEAKS'}"
        )

    with tempfile.TemporaryDirectory() as tmp:
        planted = pathlib.Path(tmp) / "planted"
        materialise(versions[0], planted)
        (planted / "prov" / "planted.json").write_text('{"cwd": "/home/someone/x"}', "utf-8")
        if not leaks(planted):
            bad += 1
            print("  THE SCANNER DOES NOT WORK: a planted absolute path was not found.")
        else:
            print("  positive control: a planted absolute path IS found")

    print(f"\n  {'OK' if not bad else f'{bad} PROBLEM(S)'} — {len(versions)} version(s)")
    return 1 if bad else 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="corpus", description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    gen = sub.add_parser("generate", help="build the corpus from PyPI (needs the network)")
    gen.add_argument("--version", action="append", dest="versions", metavar="X.Y.Z")
    gen.add_argument("--all", action="store_true", help="every released tag")
    sub.add_parser("verify", help="self-check: is the committed corpus honest?")
    sub.add_parser("list", help="what is in the corpus")
    args = ap.parse_args(argv)

    if args.cmd == "generate":
        versions = args.versions or (released_versions() if args.all else [])
        if not versions:
            raise SystemExit("name --version X.Y.Z, or --all for every released tag")
        generate_all(versions)
        return self_check()
    if args.cmd == "verify":
        return self_check()
    for version in corpus_versions():
        tree = CORPUS / version / "tree"
        count = sum(1 for p in tree.rglob("*") if p.is_file())
        print(f"  {version}: {count} files")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
