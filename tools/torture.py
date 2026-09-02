# Copyright (c) 2026 Assistance Publique – Hôpitaux de Paris (AP-HP),
#                    Hôpital Henri-Mondor, and Taylor Thompson
# SPDX-License-Identifier: BSD-3-Clause
# Licensed under the BSD 3-Clause License — see LICENSE.
"""Damage a real record, then read it back and see what the package says about it.

TWO THINGS THE SUITE DOES NOT DO. The suite builds records the way the package expects them
and reads them back the way the package expects to. Everything it feeds a reader was written
by someone who knew what the reader wanted. This feeds the readers records that a crash or a
bad sector produced, which is the state a provenance package actually meets in the field --
the whole reason it writes anything down is that the machine it ran on is not there any more.

  PART A -- TORN WRITES. Every artifact write in the package is a plain `Path.write_text`:
  there is no `os.replace`, no `mkstemp`, no `O_EXCL` anywhere in `runprov/`. The history
  sink is careful (`sinks.py` appends under an exclusive lock and fsyncs); the provenance
  sidecar, the YAML twin, the in-flight marker and the environment snapshot are not. A
  process that dies inside one of those calls leaves a PREFIX on disk. Part A produces
  exactly that prefix -- deterministically, at a named write site, rather than by racing a
  `SIGKILL` and hoping -- and then asks two questions: is the file all-or-nothing, and do
  the readers survive it.

  PART B -- CORRUPTED RECORDS. 100% statement and branch coverage says the TESTS reached
  every branch. It says nothing about which INPUTS reach them. Part B mutates the bytes of
  a valid history, sidecar, YAML twin, marker and pinned artifact and holds every reader to
  the exit-code contract: 0 checked-and-clean, 1 checked-and-something-is-wrong, 2
  could-not-check. A traceback is none of the three, and an exit 0 over damage is the worst
  answer this package can give.

WHY THERE ARE POSITIVE CONTROLS. "No findings" and "nothing was actually damaged" are the
same green, and a fuzzer whose corruption silently failed to apply reports the second while
looking like the first. So: every mutation asserts its own bytes changed, and two controls
run before any case -- a clean tree must verify 0, and a byte appended to a pinned input
must verify 1. If a control does not fire, this exits 2 and reports nothing, because at that
point it has not measured anything.

NOT IN THE GATE, and not in the sdist's `include`. It builds and tears down hundreds of
trees; it belongs to whoever is looking for something, not to every push.

    python tools/torture.py                 # both parts, default budget
    python tools/torture.py --part b --seed 7
    python tools/torture.py --case b:history.jsonl:truncate:2   # one case, from a report

Exit: 0 nothing found, 1 findings, 2 the run is void (a control did not fire).
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import pathlib
import random
import shutil
import subprocess
import sys
import tempfile
import typing

ROOT = pathlib.Path(__file__).resolve().parent.parent
PY = sys.executable

#: The pipeline every case is measured against. Two runs joined by a real file, so the
#: history has an edge to reconstruct, `final.tsv` PINS `mid.tsv` as an input, and there is
#: something for each reader to be right or wrong about. `open_output` rather than
#: `open(run.output(...))` deliberately: only the former writes the pin into the bytes, and
#: without a pin `verify` correctly says it could not look, which measures nothing.
#:
#: `env_snapshot_dir` AND A LOCK FILE, because without them two of the package's write sites
#: are not merely un-tortured, they are UNREACHABLE: `environment._write_snapshot` and the
#: lock-file copy in `archive_lockfiles` both run only when a snapshot directory is
#: configured, and this pipeline never configured one. The census is the thing that makes
#: this harness "derived, not listed", and it was enumerating a pipeline that did not
#: exercise the package's own configuration -- so the site whose non-atomic write was found
#: by reading could never have been found here. A census over a subset is a list with extra
#: steps.
PIPELINE = """\
import pathlib, runprov
runprov.configure(root=pathlib.Path("."), run_log=pathlib.Path("history.jsonl"),
                  env_snapshot_dir=pathlib.Path("envs"))
pathlib.Path("in.tsv").write_text("a\\n", encoding="utf-8")
pathlib.Path("uv.lock").write_text("version = 1\\n", encoding="utf-8")
with runprov.Run("step1", provenance=pathlib.Path("out/step1.prov.json")) as r:
    r.input("in.tsv")
    fh = r.open_output("out/mid.tsv"); fh.write("b\\n"); fh.close()
with runprov.Run("step2", provenance=pathlib.Path("out/step2.prov.json")) as r:
    r.input("out/mid.tsv")
    fh = r.open_output("out/final.tsv"); fh.write("c\\n"); fh.close()
"""

#: Every command that READS a record, in the spelling a person types. `prune --dry-run`
#: rather than `prune`: it is here to be a reader, and the deleting half needs a harness
#: that can tell a live marker from a dead one, which is a different job (see the module
#: docstring of `prune.py`). `verify` twice, because `--format json` is a second renderer
#: over the same finding and a record that breaks one need not break the other.
READERS: tuple[tuple[str, list[str]], ...] = (
    ("verify", ["verify"]),
    ("verify --format json", ["verify", "--format", "json"]),
    ("show", ["show", "--log", "history.jsonl"]),
    ("show --stale --rehash", ["show", "--stale", "--rehash", "--log", "history.jsonl"]),
    ("log", ["log", "--log", "history.jsonl"]),
    ("lineage", ["lineage", "--log", "history.jsonl"]),
    ("prune --dry-run", ["prune", "--dry-run", "--log", "history.jsonl"]),
)

#: The whole of it. Anything else is the contract broken, not a finding about the data.
CONTRACT = frozenset({0, 1, 2})

#: What each damaged file is, in one word, and whether some pin COVERS it as an input. The
#: second column is the sharp oracle: change the bytes of a file a pin covers and `verify`
#: must say 1. It is the only reader answer that is knowable in advance, so it is the only
#: one asserted -- the rest are held to the contract and to "no traceback", which is all
#: that can be claimed without deciding the package's behaviour from inside its own harness.
TARGETS: dict[str, bool] = {
    "history.jsonl": False,
    "out/step1.prov.json": False,
    "out/step1.prov.yml": False,
    "out/mid.tsv": True,
    "out/final.tsv": False,
    "transformation_log.yml": False,
}


class Finding(typing.NamedTuple):
    """One thing that should not have happened, and the case that produced it."""

    case: str
    what: str
    detail: str


def _sh(argv: list[str], cwd: pathlib.Path) -> tuple[int, str]:
    """Run a command in `cwd` and return its exit code and everything it said."""
    p = subprocess.run(  # noqa: S603 - argv is built here, no shell
        argv,
        cwd=cwd,
        capture_output=True,
        text=True,
        env={**os.environ, "PYTHONPATH": str(ROOT)},
        check=False,
    )
    return p.returncode, p.stdout + p.stderr


def _build(tree: pathlib.Path, preload: str = "") -> tuple[int, str]:
    """Produce a real record in `tree`.

    REBUILT PER CASE rather than built once and copied. A record stores the ABSOLUTE cwd it
    was made in, and `show` resolves every artifact path against that recorded cwd -- so a
    tree copied elsewhere has its readers looking at the ORIGINAL files, and a mutation in
    the copy is invisible. That is a harness that measures nothing while printing green.
    """
    (tree / "out").mkdir(parents=True, exist_ok=True)
    (tree / "build.py").write_text(PIPELINE, encoding="utf-8")
    (tree / "_go.py").write_text(
        (preload or "") + "import runpy\nrunpy.run_path('build.py', run_name='__main__')\n",
        encoding="utf-8",
    )
    return _sh([PY, "_go.py"], tree)


def _read(tree: pathlib.Path, case: str, expect_one: bool) -> list[Finding]:
    """Run every reader over `tree` and hold each to the contract."""
    out: list[Finding] = []
    for name, argv in READERS:
        code, said = _sh([PY, "-m", "runprov", *argv], tree)
        if "Traceback (most recent call last)" in said:
            last = [ln for ln in said.splitlines() if ln.strip()][-1:]
            out.append(Finding(case, f"{name}: TRACEBACK", last[0] if last else ""))
        elif code not in CONTRACT:
            out.append(
                Finding(case, f"{name}: exit {code}", f"outside the contract {sorted(CONTRACT)}")
            )
        elif expect_one and name == "verify" and code != 1:
            out.append(
                Finding(
                    case, f"{name}: exit {code}", "a pinned INPUT was altered; 1 was the answer"
                )
            )
    return out


# --------------------------------------------------------------------------------------
# PART A -- torn writes
# --------------------------------------------------------------------------------------

#: Injected ahead of the pipeline. With no `TORTURE_NTH` it only COUNTS, which is how the
#: write sites are enumerated instead of hand-listed -- the standing rule in this repository
#: after eight instances of a check whose scope, not logic, had stopped covering what it
#: names. A site added next year is tortured without anyone remembering to add it.
#:
#: TWO SEAMS, and the second one exists because the first stopped seeing anything. When the
#: writes moved to `_atomic` (ADR-0005) this preload still patched `Path.write_text`, still
#: ran, still reported no findings -- and the census line quietly said "0 of them runprov's".
#: A harness that has lost sight of its subject prints exactly what a harness that found
#: nothing prints. `part_a` now VOIDS the run when the count is zero, for that reason.
#:
#: `atomic_write_text` is rebound in every module that imported the NAME, not only in
#: `_atomic` where it is defined: `from ._atomic import atomic_write_text` copies the
#: binding, so patching the definition alone would leave every real call site untouched --
#: the same shape of miss, one level down.
#:
#: FOUR SEAMS NOW, and the third and fourth are the same lesson a third time. `write_bytes`
#: was never patched, so `archive_lockfiles`'s `target.write_bytes(source.read_bytes())` --
#: the seventh non-atomic write in the package, and the worst of them, because the target is
#: content-addressed and a truncated copy is reused for ever under a name asserting a digest
#: it no longer has -- was invisible to a census whose whole purpose is not needing anyone to
#: remember it. `write_text` is not "how this package writes files"; it is one of the ways.
TORN = """\
import os, pathlib, json, base64, inspect, sys
import runprov, runprov._atomic as _A

_seen = []
_nth = os.environ.get("TORTURE_NTH")


def _census(kind, path, size, mine):
    _seen.append((len(_seen) + 1, str(path), size, mine, kind))
    with open(os.environ["TORTURE_CENSUS"], "w", encoding="utf-8") as fh:
        json.dump(_seen, fh)
    return len(_seen)


def _report(path, prior, intended, kept, debris):
    # `intended` IS str OR bytes -- the same report shape covers both seams, and the oracle
    # in part_a compares raw bytes either way.
    raw = intended.encode("utf-8") if isinstance(intended, str) else intended
    with open(os.environ["TORTURE_TORN"], "w", encoding="utf-8") as fh:
        json.dump({"path": str(path), "kept": kept, "whole": len(raw), "debris": debris,
                   "prior": None if prior is None else base64.b64encode(prior).decode(),
                   "intended": base64.b64encode(raw).decode()}, fh)


def _cut(text):
    return int(len(text) * float(os.environ["TORTURE_FRAC"]))


_real_wt = pathlib.Path.write_text


def _wt(self, data, *a, **k):
    mine = os.sep + "runprov" + os.sep in inspect.stack()[1].filename
    if _nth and _census("write_text", self, len(data), mine) == int(_nth):
        _report(self, self.read_bytes() if self.exists() else None, data, _cut(data), False)
        _real_wt(self, data[: _cut(data)], *a, **k)
        os._exit(137)
    elif not _nth:
        _census("write_text", self, len(data), mine)
    return _real_wt(self, data, *a, **k)


_real_wb = pathlib.Path.write_bytes


def _wb(self, data, *a, **k):
    mine = os.sep + "runprov" + os.sep in inspect.stack()[1].filename
    if _nth and _census("write_bytes", self, len(data), mine) == int(_nth):
        _report(self, self.read_bytes() if self.exists() else None, data, _cut(data), False)
        _real_wb(self, data[: _cut(data)], *a, **k)
        os._exit(137)
    elif not _nth:
        _census("write_bytes", self, len(data), mine)
    return _real_wb(self, data, *a, **k)


_real_at = _A.atomic_write_text


def _at(path, text, encoding="utf-8"):
    # THE CRASH GOES INSIDE THE TEMPORARY FILE, which is the only window a torn write still
    # has: the destination is not opened until the rename, and the rename is atomic. So this
    # writes the prefix where `_atomic` would have written it and exits WITHOUT renaming --
    # exactly the state a killed process leaves -- and the oracle then asks whether the
    # destination is untouched, which is the whole of what ADR-0005 promises.
    path = pathlib.Path(path)
    if _nth and _census("atomic", path, len(text), True) == int(_nth):
        tmp = path.with_name(f".{path.name}.torture{_A.TEMP_SUFFIX}")
        _report(path, path.read_bytes() if path.exists() else None, text, _cut(text), True)
        _real_wt(tmp, text[: _cut(text)], encoding=encoding)
        os._exit(137)
    elif not _nth:
        _census("atomic", path, len(text), True)
    return _real_at(path, text, encoding)


_real_ab = _A.atomic_write_bytes


def _ab(path, data):
    # The bytes twin of `_at`, and the same crash-inside-the-temporary-file model.
    path = pathlib.Path(path)
    if _nth and _census("atomic_bytes", path, len(data), True) == int(_nth):
        tmp = path.with_name(f".{path.name}.torture{_A.TEMP_SUFFIX}")
        _report(path, path.read_bytes() if path.exists() else None, data, _cut(data), True)
        _real_wb(tmp, data[: _cut(data)])
        os._exit(137)
    elif not _nth:
        _census("atomic_bytes", path, len(data), True)
    return _real_ab(path, data)


pathlib.Path.write_text = _wt
pathlib.Path.write_bytes = _wb
# EVERY MODULE THAT IMPORTED THE NAME, derived from sys.modules rather than listed.
for _m in list(sys.modules.values()):
    if not getattr(_m, "__name__", "").startswith("runprov"):
        continue
    if hasattr(_m, "atomic_write_text"):
        _m.atomic_write_text = _at
    if hasattr(_m, "atomic_write_bytes"):
        _m.atomic_write_bytes = _ab
"""


def part_a(
    work: pathlib.Path, fractions: tuple[float, ...], only: str | None
) -> tuple[list[Finding], int]:
    """Crash inside each write in turn, and ask what is on disk afterwards.

    ONE FINDING PER WRITE SITE, not one per fraction. Three lines saying the same file is
    not atomic at 0%, 50% and 90% is one fact reported three times, and a report padded with
    restatements is one nobody reads to the end of.

    THE PIPELINE'S OWN WRITES ARE TORTURED AND NOT COUNTED. `in.tsv` is written by the
    script under `runprov`, not by the package, so its atomicity is not this project's
    finding -- but a run whose INPUT was torn is a tree the readers still have to survive,
    which is the other half of part A and applies to every site whoever wrote it.

    WHAT THIS DOES NOT COVER, said here so nobody reads its green as covering it. The
    `atomic` seam wraps `atomic_write_text` and models the crash from OUTSIDE it, so it
    checks that each CALL SITE goes through the helper -- not that the helper is correct.
    An `_atomic` rewritten to copy onto the destination instead of renaming onto it passes
    part A untouched. That property is held by the unit tests (`test_a_write_that_fails_
    leaves_the_previous_record_untouched` and its two neighbours, which fail on exactly that
    rewrite); the division is deliberate, and it is written down because a harness silently
    not covering something is the failure this whole file exists to make loud.
    """
    findings: list[Finding] = []
    census = work / "census.json"
    os.environ["TORTURE_CENSUS"] = str(census)
    os.environ.pop("TORTURE_NTH", None)
    _build(work / "census-tree", TORN)
    sites = json.loads(census.read_text(encoding="utf-8")) if census.is_file() else []
    mine = [s for s in sites if s[3]]
    kinds = ", ".join(sorted({s[4] for s in mine}))
    print(
        f"  part A: {len(sites)} write site(s) in one clean run, {len(mine)} of them "
        f"runprov's" + (f" ({kinds})" if kinds else "")
    )
    # VOID, NOT GREEN. A run that finds none of the package's writes has lost sight of its
    # subject, and prints what a run that found no problem prints. This is not hypothetical:
    # ADR-0005 moved every site off `Path.write_text` and this harness reported "0 findings"
    # over "0 of them runprov's" until the seam was added.
    if not mine:
        return [
            Finding(
                "a:census",
                "VOID: NOT ONE OF THE PACKAGE'S WRITES WAS SEEN",
                "the preload no longer patches the seam the package writes through",
            )
        ], 0

    n = 0
    for nth, path, size, ours, kind in sites:
        rel = pathlib.PurePath(path).name
        torn: list[str] = []
        for frac in fractions:
            if kind in ("write_text", "write_bytes") and size and int(size * frac) == size:
                continue  # not a tear
            case = f"a:{nth}:{rel}:{frac}"
            if only and not case.startswith(only):
                continue
            n += 1
            t = work / f"a{nth}-{int(frac * 100)}"
            report = work / f"torn{nth}-{int(frac * 100)}.json"
            os.environ["TORTURE_NTH"], os.environ["TORTURE_FRAC"] = str(nth), str(frac)
            os.environ["TORTURE_TORN"] = str(report)
            _build(t, TORN)
            if not report.is_file():
                findings.append(Finding(case, "THE TEAR DID NOT LAND", f"site {nth} never ran"))
                continue
            r = json.loads(report.read_text(encoding="utf-8"))
            # RELATIVE TO THE TREE, because that is the cwd the child wrote it from.
            wrote = t / r["path"]
            on_disk = wrote.read_bytes() if wrote.is_file() else b""
            whole = base64.b64decode(r["intended"])
            prior = base64.b64decode(r["prior"]) if r["prior"] is not None else b""
            if on_disk not in (whole, prior):
                torn.append(f"{int(frac * 100)}% -> {len(on_disk)} of {len(whole)} B")
            findings += _read(t, case, expect_one=False)
            shutil.rmtree(t, ignore_errors=True)
        if torn and ours:
            findings.append(
                Finding(
                    f"a:{nth}",
                    f"NOT ATOMIC: a crash inside this write leaves a prefix — {rel}",
                    f"site {nth} ({kind}), written by runprov; torn at {', '.join(torn)}",
                )
            )
    for k in ("TORTURE_NTH", "TORTURE_FRAC", "TORTURE_TORN"):
        os.environ.pop(k, None)
    return findings, n


# --------------------------------------------------------------------------------------
# PART B -- corrupted records
# --------------------------------------------------------------------------------------


def _mutations(data: bytes, rng: random.Random) -> list[tuple[str, bytes | None]]:
    """The damage a disk, a truncation or a bad line actually produces.

    `None` means STRUCTURALLY INAPPLICABLE -- there is no brace in a TSV to drop, and no
    digest in a two-byte file to bump -- as against an operator that ran and changed
    nothing, which the caller reports as a fault in this file. The two look identical on
    disk and only one of them is a finding, so they are distinguished here rather than
    guessed at there.
    """
    if not data:
        return []
    at = rng.randrange(len(data))
    mid = len(data) // 2
    return [
        ("flip-bit", data[:at] + bytes([data[at] ^ (1 << rng.randrange(8))]) + data[at + 1 :]),
        ("truncate", data[: rng.randrange(len(data))]),
        ("empty", b""),
        ("drop-byte", data[:at] + data[at + 1 :]),
        ("insert-nul", data[:at] + b"\x00" + data[at:]),
        ("break-utf8", data[:at] + b"\xc3\x28" + data[at:]),
        ("dup-half", data[:mid] + data[:mid] + data[mid:]),
        ("zero-fill", b"\x00" * len(data)),
        ("shuffle-digest", _bump_hex(data, rng)),
        ("drop-brace", next((data.replace(c, b"", 1) for c in (b"{", b"[") if c in data), None)),
        ("giant-line", data + b"\n" + b"{" * 20000 + b"\n"),
    ]


def _bump_hex(data: bytes, rng: random.Random) -> bytes | None:
    """Change one character of the longest hex run -- a digest, wherever it happens to sit."""
    best, run = (0, 0), 0
    for i, c in enumerate(data + b"."):
        if c in b"0123456789abcdef":
            run += 1
        else:
            if run > best[1]:
                best = (i - run, run)
            run = 0
    if best[1] < 8:
        return None
    at = best[0] + rng.randrange(best[1])
    return data[:at] + bytes([ord("0") if data[at] != ord("0") else ord("f")]) + data[at + 1 :]


def part_b(work: pathlib.Path, seed: int, only: str | None) -> tuple[list[Finding], int]:
    """Mutate one file of an otherwise valid record, then read the whole thing back."""
    findings: list[Finding] = []
    n = 0
    for target, pinned in TARGETS.items():
        rng = random.Random(f"{seed}:{target}")  # noqa: S311 - damage, not a secret
        probe = work / "probe"
        shutil.rmtree(probe, ignore_errors=True)
        _build(probe)
        if not (probe / target).is_file():
            findings.append(Finding(f"b:{target}", "TARGET ABSENT", "a clean run did not write it"))
            continue
        clean = (probe / target).read_bytes()
        shutil.rmtree(probe, ignore_errors=True)
        for i, (op, damaged) in enumerate(_mutations(clean, rng)):
            case = f"b:{target}:{op}:{i}"
            if only and not case.startswith(only):
                continue
            if damaged is None:
                continue  # nothing of that shape in this file
            if damaged == clean:
                findings.append(Finding(case, "THE MUTATION DID NOT LAND", f"{op} left the bytes"))
                continue
            n += 1
            t = work / f"b{i}"
            shutil.rmtree(t, ignore_errors=True)
            _build(t)
            (t / target).write_bytes(damaged)
            findings += _read(t, case, expect_one=pinned)
            shutil.rmtree(t, ignore_errors=True)
    return findings, n


# --------------------------------------------------------------------------------------


def controls(work: pathlib.Path) -> list[str]:
    """Prove the oracle can tell clean from damaged, BEFORE anything is claimed about either.

    Both directions, because one of them alone is a tautology: a harness that only checks
    "clean is clean" passes with a broken reader, and one that only checks "damage is seen"
    passes with a reader that condemns everything.
    """
    bad: list[str] = []
    t = work / "control"
    shutil.rmtree(t, ignore_errors=True)
    code, said = _build(t)
    if code != 0:
        return [f"the pipeline itself failed (exit {code}): {said.strip().splitlines()[-1:]}"]
    code, _ = _sh([PY, "-m", "runprov", "verify"], t)
    if code != 0:
        bad.append(f"a CLEAN tree verified {code}, not 0 -- the oracle cannot recognise clean")
    (t / "out/mid.tsv").write_bytes((t / "out/mid.tsv").read_bytes() + b"x")
    code, _ = _sh([PY, "-m", "runprov", "verify"], t)
    if code != 1:
        bad.append(
            f"an ALTERED pinned input verified {code}, not 1 -- the oracle cannot see damage"
        )
    shutil.rmtree(t, ignore_errors=True)
    return bad


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--part", choices=("a", "b", "both"), default="both")
    ap.add_argument("--seed", type=int, default=0, help="the mutations are a function of this")
    ap.add_argument("--case", default=None, help="one case id, as printed in a finding")
    ap.add_argument("--fractions", default="0,0.5,0.9", help="how much of a torn write survives")
    ap.add_argument("--keep", action="store_true", help="do not delete the work directory")
    args = ap.parse_args(argv)

    work = pathlib.Path(tempfile.mkdtemp(prefix="runprov-torture-"))
    print(f"# runprov torture — seed {args.seed}, work {work}")
    try:
        if void := controls(work):
            for line in void:
                print(f"  CONTROL FAILED: {line}")
            print("\nVOID — nothing was measured, so nothing is reported.")
            return 2
        print("  controls: clean verifies 0, an altered pinned input verifies 1")

        found: list[Finding] = []
        cases = 0
        if args.part in ("a", "both"):
            f, n = part_a(work, tuple(float(x) for x in args.fractions.split(",")), args.case)
            found += f
            cases += n
        if args.part in ("b", "both"):
            f, n = part_b(work, args.seed, args.case)
            found += f
            cases += n
    finally:
        if not args.keep:
            shutil.rmtree(work, ignore_errors=True)

    print(f"\n# {cases} case(s), {len(found)} finding(s)")
    for f in found:
        print(
            f"  {f.what}\n    {f.detail}\n    reproduce: --case {f.case} --seed {args.seed} --keep"
        )
    return 1 if found else 0


if __name__ == "__main__":
    raise SystemExit(main())
