# Copyright (c) 2026 Assistance Publique – Hôpitaux de Paris (AP-HP),
#                    Hôpital Henri-Mondor, and Taylor Thompson
# Licensed under the CeCILL-B Free Software License Agreement — see LICENSE.
# https://cecill.info/
"""`Run` — one pass of one script, recorded so it can be reconstructed or invalidated.

The interface is the point
-------------------------
`run.input(p)` RETURNS the path. So the natural way to open a file is also the registering
way, and a read that skips registration is a visible omission rather than the default:

    df = pd.read_csv(run.input(path))          # registered
    df = pd.read_csv(path)                     # not — and a checker can see the difference

The predecessor this replaces took provenance as prose — `{"input": f"{a}, {b}"}` — so it
recorded what the author believed the step read. 248 files imported it. Universal adoption
did not make the record true, because the interface could not tell the difference between
a description and a fact.

What is deliberately excluded from `header()` is documented on that method: no timestamp,
no run id. Both were tried; both made every pinned artifact differ on every run.
"""

from __future__ import annotations

import contextlib
import datetime as dt
import hashlib
import importlib.metadata
import inspect
import json
import os
import pathlib
import platform
import shlex
import signal
import sys
import threading
import traceback
import types
import typing
import uuid
import weakref

from ._report import diagnostic, summary
from .environment import archive_lockfiles, lockfiles, manager, write_snapshot
from .hashing import describe, moved_since, pin_digest, sha256
from .project import (
    OTHER_FILES_KEPT,
    Project,
    active,
    classify_status,
    git,
    is_configured,
    is_repository,
)
from .terminal import Capture

# The record format, named and versioned. A consumer -- a script, a dashboard, an agent
# reading the history -- can branch on this instead of guessing from which keys happen to
# be present. Bump it when a field changes meaning, never when one is added.
#
# v1 -> v2 (ADR-029, 2026-08-11): `content_sha256` is COMPUTED DIFFERENTLY -- volatile
# stripping now reaches inside gzip, spans block boundaries, and is scoped to runprov's own
# records; `sha256_tree` gained a separator. A v1 digest and a v2 digest answer different
# questions, so comparing them across the boundary is meaningless and the marker says so.
SCHEMA = "runprov.run.v2"

# The HISTORY line is a different shape from the sidecar -- a summary, flattened, with
# `git_commit` where the record has a whole `code` block -- and both answered to
# `runprov.run.v1`. A consumer branching on the marker, which is the only reason the field
# exists, would have applied the wrong reader to one of them.
HISTORY_SCHEMA = "runprov.history.v2"

# How many dirty files the terminal warning names before it says how many more. The record
# keeps all of them; this is the line a human reads, and a 300-file tree used to bury the
# sentence that matters under 300 lines of stderr.
DIRTY_FILES_SHOWN = 10

#: Path components that mean "not this project's own code, even though it is under the
#: root": an environment, an installed copy, a build tree. What lives in them is a
#: dependency, and dependencies are answered by `packages` and the environment snapshot.
_NOT_PROJECT_CODE = frozenset(
    {".venv", "venv", "env", "site-packages", "dist-packages", "node_modules", "build", "dist"}
)

# Formats where a leading comment block is not a comment, so `open_output` REFUSES rather
# than writing one. The reason is per-suffix because they fail differently, and the
# difference is what a caller needs in order to know what to do instead.
#
# NEWICK IS WHY THIS TABLE EXISTS, and it is the only entry that fails silently. A tree
# pinned with `#` lines still PARSES. Measured on Biopython 1.85, a 3-taxon tree:
#
#   baseline terminals: 3 ['HCV1a_ref', 'HCV1b_ref', 'HCV2a']
#   pinned   terminals: 6 ['default', 'yet', '1', 'HCV1a_ref', 'HCV1b_ref', 'HCV2a']
#
# No exception, no warning. The phantom taxa are harvested out of the pin's own prose --
# `generation : (default)` and `commit : NONE -- no commit to name (yet)` supply the first
# two, and the third comes from the digits, so it varies with the pin. A downstream clade
# assignment consumes that without complaint, which makes it the exact silent-wrongness
# this package exists to refuse, arriving through the method advertised as the safe default.
#
# The others are loud, and they are here because a fix that only knew about Newick would
# leave the same defect standing in every one of them.
PIN_UNSAFE = {
    ".nwk": "Newick has no comment syntax — the pin's own words parse as taxon names, "
    "SILENTLY (measured: a 3-taxon tree reads back with 6)",
    ".newick": "Newick has no comment syntax — the pin parses as taxon names, silently",
    ".nh": "Newick has no comment syntax — the pin parses as taxon names, silently",
    ".tree": "Newick has no comment syntax — the pin parses as taxon names, silently",
    ".fastq": "FASTQ has no comment syntax at all; a record must begin with '@'",
    ".fq": "FASTQ has no comment syntax at all; a record must begin with '@'",
    ".fasta": "a leading '#' breaks `samtools faidx`, and Bio.SeqIO warns it will become "
    "a ValueError. FASTA's only spec-legal comment is ';'",
    ".fa": "a leading '#' breaks `samtools faidx` (see .fasta)",
    ".fna": "a leading '#' breaks `samtools faidx` (see .fasta)",
    ".faa": "a leading '#' breaks `samtools faidx` (see .fasta)",
    ".ffn": "a leading '#' breaks `samtools faidx` (see .fasta)",
    ".vcf": "`##fileformat` must be the FIRST line; bcftools reports 'unknown file type' "
    "even when the pin uses '##'",
    ".sam": "'@provenance' is not a valid header record type; only '@CO' is, so samtools "
    "fails to read the header even when the pin uses '@'",
    # TEXT, and that is the trap in them: `open_output` can write these perfectly happily,
    # and the result does not look damaged until something tries to parse it. Found by
    # instrumenting a real matplotlib script that saves `.svg` -- the figure came out and
    # `ET.parse` then failed at line 1, column 1. `#` is a comment in a TSV, a YAML, a TOML
    # and an INI; it is an id selector in CSS, a macro parameter in TeX, and simply invalid
    # in XML and JSON, which have no comment syntax at all.
    ".svg": "SVG is XML, which has no comment syntax a leading '#' can use — measured: "
    "the file is written and then `ET.parse` fails at line 1, column 1",
    ".xml": "XML has no line-comment syntax; a leading '#' is not well-formed",
    ".html": "HTML has no line-comment syntax; a leading '#' is text, not a comment",
    ".htm": "HTML has no line-comment syntax; a leading '#' is text, not a comment",
    ".xhtml": "XHTML is XML; a leading '#' is not well-formed",
    ".json": "JSON has no comment syntax — measured: `json.loads` fails at char 0",
    ".jsonl": "JSON Lines: every line must be one JSON value, and a '#' line is not one",
    ".geojson": "GeoJSON is JSON; it has no comment syntax",
    ".ipynb": "a notebook is JSON; a '#' line makes it unopenable rather than commented",
    ".tex": "TeX comments with '%'; '#' is a macro parameter character and will error",
    ".bam": "binary; `open_output` is text mode and a pin would corrupt it",
    ".cram": "binary; `open_output` is text mode and a pin would corrupt it",
    ".parquet": "binary; `open_output` is text mode and a pin would corrupt it",
    ".h5": "binary; `open_output` is text mode and a pin would corrupt it",
    ".hdf5": "binary; `open_output` is text mode and a pin would corrupt it",
    ".npy": "binary; `open_output` is text mode and a pin would corrupt it",
    ".npz": "binary; `open_output` is text mode and a pin would corrupt it",
    ".xlsx": "binary; `open_output` is text mode and a pin would corrupt it",
    ".gz": "compressed; `open_output` is text mode and a pin would corrupt it",
    ".bgz": "compressed; `open_output` is text mode and a pin would corrupt it",
    ".zst": "compressed; `open_output` is text mode and a pin would corrupt it",
    ".bz2": "compressed; `open_output` is text mode and a pin would corrupt it",
    ".zip": "an archive; `open_output` is text mode and a pin would corrupt it",
    ".png": "binary; `open_output` is text mode and a pin would corrupt it",
    ".pdf": "binary; `open_output` is text mode and a pin would corrupt it",
}

#: THE ALLOWLIST: suffixes whose format is known to treat a leading `#` line as a comment,
#: and whose first line is not otherwise special. Anything not named here gets a SIDECAR.
#:
#: This is the inversion. The rule used to be "pin in-band unless the suffix is on a list of
#: known-unsafe formats", and that default corrupts whatever nobody thought of -- which over
#: three rounds of review was Newick (a pinned tree PARSED and came back with three phantom
#: taxa), then SVG and JSON (found by instrumenting a real plotting script), then pickle.
#: Each fix added a row and left the default intact. A guard whose default is to corrupt is
#: not a guard, so the default is now the safe outcome and the list is of what is SAFE.
#:
#: Deliberately NOT here, though `#` is a comment in all of them: `.py`, `.sh`, `.pl`, `.rb`
#: and friends. Their first line can be a shebang, and a pin above it stops the file being
#: executable -- "is `#` a comment" is not the same question as "is line 1 free".
#: `.md` IS here: `#` renders as a heading rather than a comment, which is visible but not
#: corrupting, and it has been pinned that way from the start.
PIN_INLINE = frozenset(
    {".bed", ".bedgraph", ".cfg", ".conf", ".csv", ".gff", ".gff3", ".gtf", ".ini",
     ".md", ".properties", ".tab", ".toml", ".tsv", ".txt", ".yaml", ".yml"}
)  # fmt: skip

#: Of the above, the ones that are BINARY or COMPRESSED. `open_output` opens in text mode,
#: so these are refused whatever happens to the pin -- the mode is the problem, not the
#: comment. Everything else in `PIN_UNSAFE` is text and gets a SIDECAR instead.
#: Naming them buys a better MESSAGE, not the protection -- the allowlist already sends an
#: unknown suffix to the sidecar. These get "you cannot write this through a text handle"
#: instead, which is the actual problem for a caller holding a `pickle.dump`.
PIN_BINARY = frozenset(
    {
    ".arrow", ".bam", ".bcf", ".bgz", ".bz2", ".cram", ".db", ".feather", ".gif", ".gz", ".h5",
    ".h5ad", ".hdf5", ".joblib", ".jpeg", ".jpg", ".loom", ".mat", ".nc", ".npy", ".npz",
    ".onnx", ".parquet", ".pdf", ".pickle", ".pkl", ".png", ".pt", ".pth", ".qza", ".rds",
    ".safetensors", ".sqlite", ".sqlite3", ".tif", ".tiff", ".webp", ".xlsx", ".zip", ".zst"
    }
)  # fmt: skip

#: What a sidecar pin is called: `calls.jsonl` -> `calls.jsonl.prov.txt`. The suffix is
#: APPENDED rather than replacing, so two artifacts differing only in extension cannot
#: collide on one sidecar, and the artifact it belongs to is readable from the name.
PIN_SIDECAR_SUFFIX = ".prov.txt"

#: Markers that SOME parsers of a format accept, offered only when the caller asks for them
#: by name. Measured rather than assumed, and the caveat is the reason each entry carries
#: one: `;` is the legacy Pearson FASTA comment and Biopython reads it without complaint,
#: while `samtools faidx` rejects the file outright. That is a real trade a caller can make
#: knowingly and must not be made for them, so the default stays the sidecar.
#: An empty caveat means there is no trade at all -- the marker is simply that format's
#: comment syntax, and `#` was only ever the wrong default for it. A non-empty caveat is
#: said on stderr at the moment the trade is made.
PIN_ALTERNATIVE = {
    ".sql": ("-- ", ""),
    ".tex": ("% ", ""),
    ".fasta": ("; ", "Biopython reads it; `samtools faidx` REJECTS the file"),
    ".fa": ("; ", "Biopython reads it; `samtools faidx` REJECTS the file"),
    ".fna": ("; ", "Biopython reads it; `samtools faidx` REJECTS the file"),
    ".faa": ("; ", "Biopython reads it; `samtools faidx` REJECTS the file"),
    ".ffn": ("; ", "Biopython reads it; `samtools faidx` REJECTS the file"),
}


class Terminated(BaseException):
    """A termination signal arrived while a `Run` was open. See `Run._catch_signals`.

    **BaseException, not Exception**, and deliberately: `except Exception:` around a
    pipeline step is ordinary, and if it swallowed a SIGTERM the step would go on to report
    success for work the operating system had already stopped. `KeyboardInterrupt` and
    `SystemExit` sit outside `Exception` for the same reason, and a `kill` belongs with
    them rather than with a `ValueError`.

    Catch it if you want the conventional shell exit status, which nothing here imposes:

        try:
            with Run("step", provenance=PROV) as run:
                ...
        except Terminated as t:
            raise SystemExit(128 + t.signum) from t
    """

    def __init__(self, signum: int) -> None:
        self.signum = signum
        self.name = signal.Signals(signum).name
        super().__init__(f"terminated by {self.name} ({signum})")


def _jsonable(obj: typing.Any) -> typing.Any:  # noqa: ANN401 - walks arbitrary record data
    """Replace non-finite floats with their names, recursively.

    `json.dumps` emits bare `NaN`, `Infinity` and `-Infinity`, which are **not JSON**: every
    strict parser rejects the document. So one NaN metric — an AUROC on a class with no
    positives, a loss that diverged — made the sidecar AND the history line unreadable to
    anything that is not Python, which is most of what reads a provenance record.

    The value becomes the STRING `"NaN"`, not `null` and not a dropped key. A dropped key
    loses the measurement; `null` says "not measured", which is a different fact from
    "measured, and the answer was not a number".
    """
    if isinstance(obj, float):
        if obj != obj:
            return "NaN"
        if obj in (float("inf"), float("-inf")):
            return "Infinity" if obj > 0 else "-Infinity"
        return obj
    if isinstance(obj, dict):
        return {k: _jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_jsonable(v) for v in obj]
    # No branch for `str`, `int`, `bool` or `None`: none of them has `.item()`, so all four
    # fall past the test below unchanged. Two drafts carried such branches and mutation
    # testing could not tell either from its absence — they encoded a distinction that does
    # not exist, which is the kind of line that later reads as load-bearing and is not.
    item = getattr(obj, "item", None)
    if callable(item):
        # NUMPY AND PANDAS SCALARS. `numpy.float64` subclasses `float` and survived the
        # branch above; `numpy.int64` and `numpy.bool_` subclass NEITHER `int` NOR `bool`,
        # so they fell through to the record's `default=str` and a COUNT WAS RECORDED AS
        # THE STRING "6". Measured: `run.note("n", df["v"].sum())` wrote `"6"`, which
        # compares unequal to 6 in every downstream check and renders quoted in the YAML
        # view. `df.nunique()`, `.sum()` and `(s > 1).any()` are the ordinary spellings, so
        # this is the common case rather than an exotic one.
        #
        # Duck-typed on `.item()` rather than importing numpy, because this package has no
        # dependencies and must not acquire one to describe a caller's data. It is the same
        # rule the predecessor's `json_safe()` used, for the same reason.
        try:
            # Recursive, not a bare `item()`: `numpy.float64("nan").item()` is a Python
            # NaN, which `json.dumps` writes as bare `NaN` -- not JSON, and rejected by
            # every strict parser. An AUROC on a class with no positives is exactly that
            # value and arrives from numpy, so the two rules have to compose.
            return _jsonable(item())
        except Exception as exc:  # guards-ok: `.item()` on something that is not a scalar
            # -- a 3-element array raises ValueError -- must not abort the caller's run. It
            # falls through to the recorded string, which is what happened before.
            del exc
    return obj


def _release_abandoned(cap: Capture) -> None:
    """Stop a capture whose `Run` was collected without ever being closed.

    Idempotent by construction: a capture already stopped is no longer on the live stack,
    and `stop()` on it restores nothing and closes nothing twice. Silent, because this runs
    from a finalizer -- possibly during interpreter shutdown, where the streams a warning
    would use may already be gone.
    """
    try:
        cap.stop()
    except Exception as exc:  # guards-ok: a finalizer that raises prints an
        # unhandled-exception notice from deep inside the interpreter and helps nobody.
        del exc


#: Sidecar paths written in THIS process, and by which run. A sidecar is one run's record;
#: two runs sharing a path leaves the file describing whichever finished last. See `write`.
_SIDECARS: dict[str, str] = {}
_SIDECARS_LOCK = threading.Lock()


def _subclass_files(cls: type) -> frozenset[pathlib.Path]:
    """The files defining `Run` SUBCLASSES in this instance's MRO.

    A project that wraps `Run` — to bind a default project, to add a field, to keep an old
    hashing convention — is doing the obvious thing, and `scripts/audit/_provenance.py` in
    the host project is exactly that. Without this the wrapper's own file is the first frame
    outside the package, so every script using it records THE WRAPPER as its script.

    Derived from the MRO rather than from a registry or a marker attribute, so it costs a
    wrapper nothing: subclassing is the declaration.
    """
    out = set()
    for klass in cls.__mro__:
        # `Run` itself lives inside the package and is already excluded by path; `object`
        # and any non-Run mixin are not shims and must not silence a real caller frame.
        if klass is Run or not issubclass(klass, Run):
            continue
        f = getattr(sys.modules.get(klass.__module__), "__file__", None)
        if f:
            out.add(pathlib.Path(f).resolve())
    return frozenset(out)


def _caller_file(skip: frozenset[pathlib.Path] = frozenset()) -> pathlib.Path | None:
    """The script that constructed the Run — not this package's own file, nor a shim's.

    `_provenance.py` computed `script_sha256` as `Path(__file__).parent / f"{script}.py"`,
    which held only while it sat in the same directory as its callers. Promoting the module
    breaks that silently: the hash becomes None for every script and nothing says so. Walk
    the stack instead, skipping frames inside this package.

    `skip` carries the files defining `Run` subclasses (see `_subclass_files`). It is a
    PREFERENCE, not an exclusion, and the difference is the whole of the second rule below:

    1. the first frame outside the package that is not a subclass-defining file — the real
       script, when a wrapper sits between it and here;
    2. failing that, the first frame outside the package at all. A script that defines its
       own `Run` subclass **and uses it** is a single file that is both, and excluding it
       outright would record `script_file: null` for exactly the self-contained script this
       walk exists to find. Recording the file twice over is right; recording nothing is not.
    """
    here = pathlib.Path(__file__).resolve().parent
    fallback: pathlib.Path | None = None
    for fr in inspect.stack()[1:]:
        f = pathlib.Path(fr.filename).resolve()
        if here in f.parents or not f.is_file():
            continue
        if fallback is None:
            fallback = f
        if f not in skip:
            return f
    return fallback


_IMPLICIT_WARNED = False

# Once per process, like the one above: not being under version control is a stable fact.
_NO_REPO_WARNED = False


def _warn_no_git(root: pathlib.Path, *, repository: bool) -> None:
    """Say that git could not be read — loudly for an anomaly, once for a way of working.

    A repository whose `git status` did not run is a surprise: a missing binary, the 20 s
    timeout, a corrupt index. Something is wrong and it is wrong right now, so it is said
    in full, on every run.

    Not being under version control is not a surprise. It is how a great many people work,
    it will be true of every run they ever make, and there is nothing to do about it. The
    old text shouted the same four lines at them forever, and a warning that fires
    identically on every run of a condition the reader cannot change is precisely the
    permanently-red check this package was written to end -- the one that teaches a reader
    to skip warnings, including the ones that matter. Once per process, one line, and it
    still says the thing that must not be lost: unknown is not clean.

    The RECORD does not change in either case. `git_status_captured: false` is written the
    same way, every `git_*` field is null, and `python -m runprov log` prints
    `DIRTY STATE UNKNOWN (git status did not run)` on the row. This is the terminal, which
    is for a human who is about to decide whether to keep reading.
    """
    if repository:
        diagnostic(
            f"  PROVENANCE WARNING: `git status` did not run in {root}, and this IS a "
            f"repository; this run's dirty state is UNKNOWN, not clean "
            f"(git_status_captured: false). No git binary, a corrupt index, or the 20 s "
            f"timeout — every git_* field in this record means 'we could not look'."
        )
        return
    global _NO_REPO_WARNED
    if _NO_REPO_WARNED:
        return
    _NO_REPO_WARNED = True
    diagnostic(
        f"  PROVENANCE NOTE: {root} is not a git repository, so this run records no commit "
        f"and its dirty state is UNKNOWN rather than clean (git_status_captured: false). "
        f"Said once per process, because it is a stable fact and not an event."
    )


def _warn_implicit_project(project: Project) -> None:
    """Say, once, that nothing configured this — the exact state of the forgetful script.

    `configure(run_log=...)` called from a paths module binds the history for every script
    that imports that module. A script that forgets the import does not fail: it gets a
    freshly detected project with a different root and a different history file, appends
    there, prints a sidecar path that looks completely normal, and the "one continuous
    history" quietly becomes two. Nothing else in the record can distinguish that from a
    project that simply has no configuration, so the condition itself is what gets reported.
    """
    global _IMPLICIT_WARNED
    if _IMPLICIT_WARNED:
        return
    _IMPLICIT_WARNED = True
    diagnostic(
        f"  PROVENANCE WARNING: no configure() has run in this process, so this Run uses an "
        f"AUTO-DETECTED project:\n"
        f"    root     {project.root}\n"
        f"    history  {project.history_destination()}\n"
        f"    If this project configures runprov from a paths module, THIS SCRIPT DID NOT "
        f"IMPORT IT, and this run will not join the project's history. Call configure() "
        f"— even with the defaults — to make the choice explicit and silence this."
    )


class Run:
    """Collects provenance for a single run.

    Use it as a context manager AND pass `provenance=`. Both, or a crash records nothing:

        with Run("build_labels", vars(args), provenance=PROV) as run:
            ...

    Args:
        script: the name recorded, and the key a history is grouped by.
        params: the parameters that shaped the result. Recorded verbatim.
        project: where this is happening. Defaults to the configured project.
        script_path: override the auto-detected caller file (wrappers, notebooks).
        terminal_log: capture what this run prints. A path captures there; `False`
            disables capture even where the project configures `terminal_log_dir`; `None`
            (the default) follows the project. Capture is a TEE — output still reaches the
            terminal unchanged — and the record states which mechanism was used, because
            `capture: "python"` cannot see a subprocess and an empty log would otherwise be
            indistinguishable from a quiet run.
        provenance: where the sidecar goes, AND the switch that makes `__exit__` write.
            Without it a `with` block records nothing when the body raises — `__exit__`
            has nowhere to write to — so `with Run(...) as run:` plus `run.write(P)` at
            the end is as silent on a crash as no `with` block at all. That is the trap
            this argument creates and it is documented here because it is not guessable:
            two plausible shapes, both silent, one correct. Given `provenance=`, calling
            `write()` yourself is optional; the history is appended once, at exit, with
            the final status.
    """

    def __init__(
        self,
        script: str,
        params: dict[str, typing.Any] | None = None,
        *,
        project: Project | None = None,
        script_path: pathlib.Path | None = None,
        provenance: pathlib.Path | None = None,
        terminal_log: pathlib.Path | bool | None = None,
    ) -> None:
        self.project = project or active()
        self.project_source = (
            "argument" if project is not None else ("configured" if is_configured() else "implicit")
        )
        if self.project_source == "implicit":
            _warn_implicit_project(self.project)
        root = self.project.root
        # ONE unscoped status, classified in Python. This was TWO calls -- a scoped one
        # for the boolean and an unscoped one for a bare count -- so the whole-tree cost
        # was already being paid, and paid twice, to reach a NARROWER answer. Measured on
        # the host repo (2,994 tracked files, 2.6 GB): two calls 19.2 ms, one call 14.1 ms.
        everything = git(root, "status", "--porcelain")
        state = classify_status(everything, self.project.code_paths)
        dirty = "\n".join(state.code)

        # `type(self)`, not `Run` — the point is to see the subclass the CALLER used.
        sp = pathlib.Path(script_path) if script_path else _caller_file(_subclass_files(type(self)))
        self.record: dict[str, typing.Any] = {
            "schema": SCHEMA,
            "script": script,
            # C8. `run_id` is a CHAIN id -- 30 stages of one pass share it -- and the ad-hoc
            # fallback collides at one-second resolution, so no run had a unique address and
            # an edge in a lineage graph could not say which run it came from. uuid4, in the
            # sidecar and the history, and DELIBERATELY NOT IN THE PIN: a uuid is a timestamp
            # wearing a different name, and embedding one made two identical runs over
            # identical inputs both report 80 artifacts CHANGED.
            "run_uid": uuid.uuid4().hex,
            "run_id": self.project.run_id(),
            "generation": self.project.generation(),
            "started_utc": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            # THE FULL INVOCATION, re-runnable as written. This recorded only the
            # basename -- `step_review.py` -- which says neither which interpreter ran it
            # nor from where, and the predecessor this package replaces did better:
            # `/home/.../envs/hcv_genotyping_env/bin/python3 src/preprocessing/x.py
            #  --inputs data/...`. That names the environment, which for a mamba/uv layout
            # with several venvs is most of the answer to "why did this run differ".
            "command": " ".join(shlex.quote(a) for a in [sys.executable, *sys.argv]),
            "argv": list(sys.argv),
            # A relative script path in `command` means nothing without this.
            "cwd": str(pathlib.Path.cwd()),
            "parameters": params or {},
            "code": {
                # The detected root, recorded. A module that can be wrong about which
                # repository it is in must at least say which one it chose.
                "project_root": str(root),
                "git_commit": git(root, "rev-parse", "HEAD"),
                "git_commit_short": git(root, "rev-parse", "--short", "HEAD"),
                "git_branch": git(root, "rev-parse", "--abbrev-ref", "HEAD"),
                "git_status_captured": state.captured,
                "git_code_dirty": bool(state.code),
                "git_dirty_code_files": list(state.code),
                # Code changes the configured `code_paths` did NOT cover. Non-empty means
                # the scope does not match this project's layout, and it is the field that
                # would have named the integrator's `pkg/` on day one.
                "git_dirty_outside_code_paths": list(state.outside_code_paths),
                # The whole tree, code included: "is this working tree modified at all",
                # which is layout-independent by construction and needs no list to be true.
                "git_tree_dirty": bool(state.code or state.other),
                # UNCHANGED MEANING (whole-tree change count), so no consumer breaks --
                # but no longer the only trace, which was the defect.
                "git_other_changes": len(everything.splitlines()) if everything else 0,
                "git_dirty_other_files": list(state.other[:OTHER_FILES_KEPT]),
                "git_other_files_omitted": max(0, len(state.other) - OTHER_FILES_KEPT),
                # What "code" meant for THIS record. A boolean whose definition lives only
                # in the caller's configuration is uninterpretable once the run is history.
                "code_paths": list(self.project.code_paths),
                "script_file": str(sp) if sp else None,
                "script_sha256": sha256(sp) if sp and sp.is_file() else None,
            },
            "environment": {
                "python": sys.version.split()[0],
                "platform": platform.platform(),
                "hostname": platform.node(),
                "cpu_count": os.cpu_count(),
                "packages": self._versions(),
                # WHICH TOOL BUILT THIS, and the evidence for saying so. A package list
                # describes an environment; it does not say how to rebuild one, and
                # `uv sync`, `mamba env create` and `poetry install` are different commands
                # over different files. Detected from disk and environment, never by
                # running anything, and carrying no paths -- see `environment.manager`.
                "manager": manager(),
                # The DECLARATION beside the result. The snapshot says which versions were
                # installed; the lock says how to install them again, and the digest says
                # which lock -- naming `uv.lock` without pinning its content names a file
                # that moves.
                "lockfiles": lockfiles(pathlib.Path(self.project.root)),
            },
            # WHERE THE HISTORY LINE WENT. The console printed the sidecar path and never
            # this one, so a split history -- two projects, two runs.jsonl, one of them
            # nobody is reading -- was invisible from the terminal.
            "history": {
                "destination": self.project.history_destination(),
                "sink": type(self.project.resolved_sink()).__name__,
                "project_source": self.project_source,
            },
            "seeds": [],
            "inputs": [],
            "outputs": [],
        }
        self._pending: list[pathlib.Path] = []
        self.provenance_path = self._sidecar_name(provenance) if provenance else None
        self._written = False
        self._warned_cwd_moved = False
        # Inside a `with` block the run is NOT over when write() is called -- the work can
        # still fail afterwards. The sidecar is written eagerly (a caller may want it on
        # disk), and the HISTORY line is deferred to __exit__, the only moment the final
        # status is known. Without this, write() inside the block appended `status: ok`
        # and __exit__ declined to correct it, so a script dying in teardown, in a final
        # assertion or in a `finally` was greppable as a SUCCESS.
        self._in_context = False
        self._deferred_history: pathlib.Path | None = None
        # `_written` means "a sidecar exists". `_history_appended` means "the history has
        # this run". Conflating them appended TWICE for one run -- both lines carrying the
        # same run_id, so any tally over runs.jsonl was silently inflated. They are
        # different facts and they now have different flags.
        self._history_appended = False
        # WHERE the sidecar actually went. The exit-time correction used to be gated on
        # `provenance_path is not None`, so a run that called write() without the kwarg
        # left the history saying `failed` and the sidecar saying `ok` -- and the sidecar
        # is the file a human opens.
        self._last_written: pathlib.Path | None = None
        # Whether `header()` has already rendered a pin. An input registered after that
        # point is NOT in the pin already embedded in an artifact, and no later inspection
        # can tell -- the artifact simply understates itself, in its own body.
        self._pin_rendered = False
        # Started BEFORE the warnings below, deliberately. The log answers "what appeared
        # on the terminal during this run", and a dirty-tree warning is part of that
        # evidence -- a log that omits the reason the commit does not identify the code is
        # missing the line most worth having. A tee copies rather than diverts, so they
        # still reach stderr either way; the only question is whether they are also on disk.
        self._capture = self._begin_capture(terminal_log)
        if not state.captured:
            # "We could not look" said out loud. This printed NOTHING and recorded
            # `git_code_dirty: false`, which reads as a verified clean tree -- the single
            # most consequential boolean in the record, failing toward the reassuring
            # answer.
            #
            # TWO SITUATIONS, and they used to print the same four lines. Not being under
            # version control is a stable fact about how someone works; a repository whose
            # `git status` did not run is an anomaly. Repeating an alarm on every run for a
            # permanent condition the reader cannot act on is the "permanently red check"
            # this package refuses elsewhere -- it trains people to stop reading warnings,
            # including the ones that matter. The RECORD is unchanged either way:
            # `git_status_captured: false` and every `git_*` field null.
            _warn_no_git(root, repository=is_repository(root))
        elif dirty:
            # DIAGNOSTIC, and the load-bearing one: it says the commit in the record does
            # not identify what ran. It goes to stderr unconditionally and RUNPROV_QUIET
            # does not reach it.
            #
            # CAPPED. The list was every dirty line, so a working tree with 300 modified
            # files buried the sentence that matters under 300 lines of stderr. The count
            # of what was omitted is printed, because a silently shortened list is a
            # different claim from a short one -- and the full set is in the record.
            lines = dirty.splitlines()
            shown = lines[:DIRTY_FILES_SHOWN]
            more = len(lines) - len(shown)
            diagnostic(
                "  PROVENANCE WARNING: CODE is modified relative to git_commit; the "
                "commit does not identify what ran:",
                *(f"    {line}" for line in shown),
                *([f"    … and {more} more (all of them are in the record)"] if more else []),
            )

    # ---------------------------------------------------------------- terminal capture
    def _sidecar_name(self, provenance: str | pathlib.Path) -> pathlib.Path:
        """Where the sidecar goes, and whether the next run is allowed to land on it.

        Off, this is the path the caller named and the tenth run overwrites the ninth. The
        HISTORY still holds all ten -- the record is never lost -- but the file beside the
        artifact answers only for the last one.

        On (`Project(sidecar_per_run=True)`), the run's stamp goes before the final suffix:

            summary.prov.json -> summary.20260813T143012Z.5709a907.prov.json

        Time first because that is what a person browses by, run_uid second because two
        runs inside one second are still two runs.

        BEFORE EVERY SUFFIX, not before the last one. `Path("summary.prov.json").suffix` is
        `.json` and its stem is `summary.prov`, so inserting there produces
        `summary.prov.20260813T135628Z.9644b22e.json` -- which no longer matches
        `*.prov.json`, the glob every tool and every reader uses to find these. Measured by
        writing three of them and watching the glob return nothing. The compound suffix is
        part of what the name MEANS, so the stamp goes in front of it.
        """
        p = pathlib.Path(provenance)
        if not self.project.sidecar_per_run:
            return p
        stamp = self.record["started_utc"].replace("-", "").replace(":", "")
        uid = str(self.record.get("run_uid", ""))[:8] or "nouid"
        head, dot, suffixes = p.name.partition(".")
        return p.with_name(f"{head}.{stamp}.{uid}{dot}{suffixes}")

    def _begin_capture(self, requested: pathlib.Path | bool | None) -> Capture | None:
        """Resolve the three-way switch and start capturing. NEVER raises."""
        if requested is False:
            return None
        path: pathlib.Path | None
        if requested is None or requested is True:
            d = self.project.terminal_log_dir
            if d is None:
                return None
            # `<script>_<run_id>.log`. The run id is what stops two passes of one script
            # from overwriting each other's evidence.
            path = pathlib.Path(d) / f"{self.record['script']}_{self.record['run_id']}.log"
        else:
            path = pathlib.Path(requested)
        try:
            cap = Capture(path)
            cap.start()
        except Exception as exc:  # guards-ok: capture is an addition to the record, never
            # a precondition for it. Whatever fails here, the run proceeds unrecorded-by-tee
            diagnostic(f"  WARNING: terminal capture could not start: {exc}")
            return None
        # A RUN THAT IS NEVER ENTERED, WRITTEN, OR EXITED still started this capture --
        # capture begins in `__init__`, deliberately, so that a caller using `write()`
        # without a `with` block is still recorded. Measured: a Run built and abandoned
        # left fds 1 and 2 dup2'd to its pipe for the life of the process, and every line
        # the program printed afterwards went on accumulating in ITS log file. Output still
        # reached the terminal -- the tee holds -- so nothing looked wrong, and the log
        # ended up describing a run that never happened plus everything that came after it.
        #
        # `weakref.finalize`, not `__del__`: it does not put a finalizer on `Run`, it cannot
        # resurrect the object, and it also fires at interpreter exit -- which covers the
        # other half, a process that ends with a run still open.
        weakref.finalize(self, _release_abandoned, cap)
        return cap

    def _end_capture(self) -> None:
        """Stop the tee and put the result in the record. Idempotent, and never raises.

        MUST run before outputs are hashed. The log is still being appended to while the
        run is alive, so a hash taken first describes a file that no longer exists in that
        form — the artifact would be pinned to a prefix of itself.
        """
        if self._capture is None:
            return
        cap, self._capture = self._capture, None
        try:
            rec = cap.stop()
        except Exception as exc:  # guards-ok: see _persist -- this runs while an exception
            # may already be in flight and must not become the failure the caller sees
            diagnostic(f"  WARNING: terminal capture could not stop cleanly: {exc}")
            return
        if rec is not None:
            self.record["terminal_log"] = rec

    def terminal_log(self, path: str | pathlib.Path) -> pathlib.Path:
        """Register a log THIS RUN DID NOT CAPTURE — one the caller already produced.

        The primitive under the automatic capture, and useful on its own: a step driven by
        `make step 2>&1 | tee logs/step.log`, or a harness that already collects a child's
        output, has the file this field is for and needs no capture at all. It is hashed
        at `write()` like any other artifact, and it takes no ownership of any stream — so
        it is the shape to reach for wherever taking over fds 1 and 2 would be unwelcome.

        RETURNS the path, like `input()` and `output()`, so registering stays the easy way.
        """
        p = pathlib.Path(path)
        self.record["terminal_log"] = {"path": str(p), "capture": "caller"}
        self._pending.append(p)
        return p

    # ------------------------------------------------------------- failure recording
    def _catch_signals(self) -> None:
        """Turn a termination signal into an exception, so the ordinary failure path runs.

        A crash was recorded and a `kill` was not, and on a cluster the second is how long
        runs actually end: SLURM's time limit is SIGTERM-then-SIGKILL, `scancel` is SIGTERM,
        `docker stop` is SIGTERM, and closing a terminal on a detached job is SIGHUP. Every
        one of them left no sidecar and no history line — indistinguishable from a run that
        never started, which is the silence this package exists to end. (SIGINT already
        worked: Python raises `KeyboardInterrupt` for it, which `__exit__` records.)

        Raising is the whole mechanism. There is no separate write-the-record-from-a-handler
        path, because a signal handler runs at an arbitrary bytecode boundary and doing I/O
        from one is how you get a half-written record. Raising hands the run back to
        `__exit__`, which already knows how to hash outputs, mark unproduced ones MISSING,
        stop the capture and append the history — the same path a `ZeroDivisionError` takes.

        `Terminated` derives from **BaseException**, like `KeyboardInterrupt`, so a broad
        `except Exception:` in a pipeline step cannot swallow a termination and turn it into
        a run that reports success.

        IT WILL NOT TAKE A HANDLER THE CALLER INSTALLED. A script with its own SIGTERM
        handler has decided what termination means for it, and overriding that to improve a
        log would be provenance changing the run it claims to observe. The record says which
        signals were armed and why not, rather than implying coverage it does not have.

        **SIGKILL and SIGSTOP cannot be caught by anything**, so `kill -9`, the OOM killer,
        and SLURM's follow-up after the grace period still leave nothing. That is a property
        of the operating system, not a gap to be closed later, and it is stated here so the
        absence of a record is not read as the absence of a run.
        """
        self._signal_restore: list[tuple[int, typing.Any]] = []
        armed: dict[str, str] = {}
        for name in ("SIGTERM", "SIGHUP"):
            sig = getattr(signal, name, None)
            if sig is None:  # SIGHUP does not exist on Windows
                armed[name] = "absent on this platform"
                continue
            try:
                previous = signal.getsignal(sig)
                if previous is not signal.SIG_DFL:
                    armed[name] = "not armed — the caller has its own handler"
                    continue
                signal.signal(sig, self._on_signal)
            except (ValueError, OSError):
                # guards-ok: `signal.signal` is main-thread-only, and provenance must not
                # be the reason a worker thread dies. Stated in the record either way.
                armed[name] = "not armed — not the main thread"
                continue
            self._signal_restore.append((sig, previous))
            armed[name] = "armed"
        self.record["signals"] = armed

    def _on_signal(self, signum: int, frame: types.FrameType | None) -> None:
        """Raise, and do nothing else. See `_catch_signals` on why there is no I/O here."""
        raise Terminated(signum)

    def _release_signals(self) -> None:
        for sig, previous in getattr(self, "_signal_restore", []):
            with contextlib.suppress(ValueError, OSError):  # guards-ok: as above
                signal.signal(sig, previous)
        self._signal_restore = []

    def __enter__(self) -> Run:
        """Use `with Run(..., provenance=P) as run:` so a CRASH still leaves a record.

        Without this, `write()` is the last line of a script and a step that dies halfway
        records nothing at all — which is precisely the defect this package was written to
        replace. The predecessor appended only on success, so its "300 runs" was 300
        *completed* runs with an unknown denominator. Building the replacement with the
        same hole and a better interface would have been the funnier version of the same
        mistake, and it shipped that way for one commit.

        A termination signal is caught here too — see `_catch_signals`. It is armed on
        ENTRY rather than in `__init__` because raising only helps if there is a block to
        unwind: outside a `with`, there is no `__exit__` to record anything.
        """
        self._in_context = True
        self._catch_signals()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: types.TracebackType | None,
        # Literal[False], not bool, and mypy is right to insist. A `bool` return says
        # "this context manager MAY swallow exceptions". It must never. The type says so
        # now, so no caller has to read the body to find out.
    ) -> typing.Literal[False]:
        # A clean SystemExit is not a failure. `raise SystemExit(main())` is how a CLI
        # ends, and treating every non-None exc_type as failure poisoned the exact query
        # the deferral was built to make trustworthy: grep '"status": "failed"'.
        clean_exit = (
            exc_type is not None
            and issubclass(exc_type, SystemExit)
            and (getattr(exc, "code", None) in (0, None))
        )
        if exc_type is not None and not clean_exit:
            self.record["status"] = "failed"
            self.record["failure"] = {
                "type": exc_type.__name__,
                "message": str(exc)[:2000],
                # Tail, not head: the frames nearest the failure are the informative ones.
                "traceback": "".join(traceback.format_exception(exc_type, exc, tb))[-4000:],
            }
        self._in_context = False
        # Before anything that can block or raise. Leaving our handler installed past the
        # block would let a signal arriving during teardown raise INSIDE `_finish`, where
        # the record is being written -- so the mechanism for recording a termination would
        # be the thing that lost the record.
        self._release_signals()
        # AT EXIT, so a module imported halfway through the work is still counted. See
        # `_imported_code`; guarded because provenance must not be what ends the run.
        if self.project.hash_imported_code:
            try:
                self.record["code"]["imported"] = self._imported_code()
            except Exception as exc:  # guards-ok: a partial answer beats a lost record
                self.record["code"]["imported"] = {"error": str(exc)}
        # FIRST, before _finish() hashes anything. The capture is still appending while the
        # run is alive, so hashing the log before stopping pins a prefix of it -- and on the
        # fd path fds 1 and 2 are still the pipe, so every diagnostic _finish emits would go
        # into the file being described instead of to the terminal.
        self._end_capture()
        try:
            self._finish()
        except Exception as exc:  # never replace the exception being recorded
            diagnostic(f"  WARNING: provenance capture failed during exit: {exc}")
        return False  # NEVER swallow the caller's exception.

    def _finish(self) -> None:
        """The exit-time capture, isolated so a failure in it cannot mask the run's."""
        target = self._last_written or self.provenance_path
        if self.provenance_path is not None and not self._written:
            self.write(self.provenance_path)
        elif self._written and target is not None:
            # Already on disk, and the status may have just changed under it. Rewrite so
            # the sidecar carries the truth rather than the optimistic snapshot.
            self._persist(target)
        if self._deferred_history is not None and not self._history_appended:
            path, self._deferred_history = self._deferred_history, None
            self._append_history(path)

    def _imported_code(self) -> dict[str, typing.Any]:
        """The project's OWN modules this run imported, each hashed. See `hash_imported_code`.

        THE GAP THIS CLOSES. `git_commit` identifies the code only when the tree is clean,
        and during development it never is; `script_sha256` pins the entry point and nothing
        it calls. So a run whose numbers changed because `src/utils/stats.py` changed
        recorded a commit, a clean-looking entry script, and no trace of the file that did
        it. `run.module(m)` answers for one module a caller thought to name -- this answers
        for every one that was actually loaded.

        READ AT EXIT, not at construction, because imports happen lazily: a module pulled in
        halfway through the work is part of what ran and would be invisible to a snapshot
        taken at the start.

        UNDER THE ROOT ONLY. Third-party packages are already answered by `packages` and by
        `env_snapshot_dir`, and hashing site-packages on every run would cost far more than
        it says -- so a virtualenv living inside the root is excluded too, since what is in
        it is a dependency rather than this project's code.

        Sorted by path so two runs over unchanged code produce the same list in the same
        order, and therefore the same `digest` -- which is what makes "did any first-party
        code change between these two runs" a comparison of one string.
        """
        root = pathlib.Path(self.project.root).resolve()
        seen: dict[str, str] = {}
        for mod in list(sys.modules.values()):
            f = getattr(mod, "__file__", None)
            if not f:
                continue
            try:
                p = pathlib.Path(f).resolve()
                rel = p.relative_to(root).as_posix()
            except (ValueError, OSError):  # guards-ok: outside the root, or unresolvable
                continue
            # A virtualenv or an installed copy INSIDE the root is a dependency, not code.
            if any(part in _NOT_PROJECT_CODE for part in pathlib.Path(rel).parts):
                continue
            if rel in seen:
                continue
            try:
                seen[rel] = sha256(p)
            except OSError:  # guards-ok: a module whose file has since gone is not a
                # reason to fail the run; it simply cannot be hashed
                continue

        ordered = sorted(seen.items())
        kept = ordered[: self.project.imported_code_max]
        body = "\n".join(f"{h}  {r}" for r, h in kept)
        return {
            "count": len(ordered),
            "omitted": max(0, len(ordered) - len(kept)),
            # ONE digest over the whole set, so the history line can carry the answer to
            # "did any first-party code change" without carrying every file to say it.
            "digest": hashlib.sha256(body.encode()).hexdigest() if kept else None,
            "files": [{"path": r, "sha256": h} for r, h in kept],
        }

    def _versions(self) -> dict[str, typing.Any]:
        """Versions of the tracked packages, READ rather than imported.

        This was `__import__(mod).__version__`, so constructing a `Run` imported numpy,
        pandas, scipy and sklearn — whether or not the script used them. A provenance
        object that CHANGES THE PROGRAM IT OBSERVES is the one thing this package cannot
        be. It is not only latency: numpy and MKL fix their thread-pool configuration at
        import time, libraries install `warnings` filters at import time, and some set
        matplotlib's backend. The run was measurably different because it was traced, and
        the trace said nothing about that.

        Measured in a conda env holding all four, constructing one `Run` in an interpreter
        that had imported none of them:

                            imports                          RSS      warm     cold
            before   numpy, pandas, scipy, sklearn        +135 MB   0.633 s   2.909 s
            after    nothing                                +3 MB   0.336 s   1.017 s

        Two timings because one would have been a claim about a page cache rather than
        about this code: the same pair measured again on a just-mounted disk cost four times
        as much on both sides. **The memory figure is the stable one**, and the import set is
        the point regardless of either. The recorded versions are identical in every run,
        `sklearn: 1.7.1` included, so this buys the reduction and changes no record.

        `sys.modules` FIRST, because if the script imported it, the object it actually has
        is the truth — an editable install, a `sys.path` shim or a vendored copy can differ
        from what any metadata says, and `module()` exists precisely because that happens.
        Distribution metadata second, via `packages_distributions()` so that the import name
        and the distribution name are allowed to differ: `sklearn` is `scikit-learn`, and
        looking up the import name would have reported the most-used tracked package in
        science as absent.

        THE MEANING NARROWS SLIGHTLY AND THAT IS THE TRADE. Before, a tracked package that
        was installed but unused was reported by importing it. Now it is reported from
        metadata, and one that is neither imported nor an installed distribution records
        `None`. That is the honest answer to "what was in this environment" — the old one
        answered "what could I have imported if I tried", and it charged the run to find out.
        """
        out: dict[str, typing.Any] = {}
        by_module: dict[str, list[str]] | None = None
        for mod in self.project.tracked_packages:
            live = sys.modules.get(mod)
            version = getattr(live, "__version__", None) if live is not None else None
            if isinstance(version, str):
                out[mod] = version
                continue
            if by_module is None:
                # Built once per run and ONLY when something has to be looked up, so a
                # script that imported everything it tracks never pays for the scan.
                try:
                    by_module = importlib.metadata.packages_distributions()  # type: ignore[assignment]
                except Exception:  # guards-ok: no metadata is a reason to record None,
                    # never a reason to fail the run this is describing
                    by_module = {}
            # None IS the record: "this package was not present in the run's environment"
            # is a fact worth keeping, and a tracked package that is absent must not abort
            # somebody's run. Left in place if every candidate distribution fails.
            out[mod] = None
            for dist in (by_module or {}).get(mod) or [mod]:
                with contextlib.suppress(Exception):  # guards-ok: as above
                    out[mod] = importlib.metadata.version(dist)
                    break
        return out

    # ---------------------------------------------------------------- registration
    def _anchor(self, p: pathlib.Path) -> pathlib.Path:
        """Freeze a relative path against the directory it was registered from.

        A record has exactly ONE `cwd`, and every relative path in it is read against that
        one value. A script calling `os.chdir` breaks that silently. Measured before this
        existed: `run.output("rel.tsv")` followed by a chdir recorded `path: "rel.tsv"`
        with `cwd:` the ORIGINAL directory, while the digest was taken from the file under
        the NEW one. The record named a file that did not exist and carried the hash of a
        different one — no error, no warning, and nothing downstream able to tell.

        Resolving against the CURRENT directory is what the caller means, so that is kept.
        What changes is the recorded spelling: once the two disagree the path is stored
        ABSOLUTE, because a relative path is only meaningful beside the `cwd` it belongs
        to and this record no longer holds that one.
        """
        if p.is_absolute() or str(pathlib.Path.cwd()) == self.record["cwd"]:
            return p
        here = pathlib.Path.cwd()
        if not self._warned_cwd_moved:
            # ONCE per run, not once per path: a script that chdirs and then registers
            # thirty files has made one decision, not thirty.
            self._warned_cwd_moved = True
            diagnostic(
                f"  PROVENANCE NOTICE: {self.record['script']}: the working directory has "
                f"moved since this run started.\n"
                f"    started in : {self.record['cwd']}\n"
                f"    now in     : {here}\n"
                f"    Relative paths are being recorded ABSOLUTE, because a relative one is "
                f"only meaningful\n"
                f"    beside the cwd it belongs to and this record holds the other. Those "
                f"paths are machine-\n"
                f"    specific, so records from two machines will no longer compare equal. "
                f"If the cwd moved\n"
                f"    in ANOTHER THREAD, this is a race and the file registered may not be "
                f"the one intended."
            )
        return here / p

    def input(self, path: str | pathlib.Path) -> pathlib.Path:
        """Hash and record a read. RETURNS the path, so registering is the easy path."""
        p = self._anchor(pathlib.Path(path))
        if not p.exists():
            # A bare FileNotFoundError from inside os.stat names neither the script nor
            # the fact that provenance registration raised it. The read was going to fail
            # anyway; failing here with the context is strictly more useful.
            # A DANGLING SYMLINK is not the same as a missing file, and `exists()` reports
            # both as absent. Saying which one it is turns "check the path" into "the link
            # is there, its target is not".
            dangling = p.is_symlink()
            raise FileNotFoundError(
                f"{self.record['script']}: cannot register input {p} — "
                + (
                    f"it is a symlink whose target does not exist ({os.readlink(p)!r}). "
                    if dangling
                    else "it does not exist. "
                )
                + "A registered input is hashed and pinned, so it must be present at "
                "registration time. Register it after producing it, or check the path."
            )
        if self._pin_rendered:
            diagnostic(
                f"  PROVENANCE WARNING: {self.record['script']}: input registered AFTER the "
                f"pin was rendered — {p}\n"
                f"    header() has already been written into an artifact, and that pin does "
                f"NOT list this input. The artifact understates what it was made from, and "
                f"nothing downstream can detect it. Register every input before header()."
            )
        try:
            self.record["inputs"].append(describe(p))
        except OSError as exc:
            # A file that EXISTS and cannot be read. `exists()` is true -- stat works --
            # so the check above passes and the failure surfaces from inside `sha256` as a
            # bare `PermissionError` naming neither the script nor the fact that provenance
            # raised it. Same treatment as the missing-input case, for the same reason.
            raise OSError(
                f"{self.record['script']}: cannot register input {p} — it exists but "
                f"could not be read ({exc}). A registered input is hashed and pinned, so "
                f"it must be readable at registration time."
            ) from exc
        except ValueError as exc:
            # `describe` refuses a FIFO/socket/device rather than blocking on it. Re-raised
            # with the script name, because the bare hang this replaces named neither the
            # script nor the path -- there was no output at all.
            raise ValueError(f"{self.record['script']}: {exc}") from exc
        return p

    def output(self, path: str | pathlib.Path) -> pathlib.Path:
        """Register a write. Hashed in `write()`, not here.

        Callers do `df.to_csv(run.output(p))`, so the file does not exist yet at call
        time. Hashing eagerly silently dropped every output whose writer had not yet run.

        The path is anchored HERE rather than in `write()`, so that a `chdir` between the
        two cannot move which file gets hashed. See `_anchor`.
        """
        p = self._anchor(pathlib.Path(path))
        self._pending.append(p)
        return p

    def open_output(self, path: str | pathlib.Path, comment: str = "# ") -> typing.IO[str]:
        """Open a text artifact for writing, REGISTERED and PINNED, in UTF-8.

        The pin as the default of the write path. Doing it by hand takes three things a
        caller has to remember separately, and forgetting any one of them is silent:

            with open(run.output(OUT), "w", encoding="utf-8") as fh:   # register
                fh.write(run.header())                                 # pin
                ...                                                    # and utf-8

        Measured on the source project: **63 of 155 `run.output()` call sites sit in scripts
        that write no pin at all.** That is not carelessness, it is the shape of the API —
        the correct spelling is three steps and the incomplete one is one step, so the
        incomplete one wins. This makes the correct spelling the short one:

            with run.open_output(OUT) as fh:
                df.to_csv(fh, sep="\t", index=False)

        `encoding="utf-8"` is forced and cannot be overridden. `header()` contains an em
        dash, so under the machine's locale encoding the same artifact is 189 bytes on one
        machine and 187 on another — a different SHA-256 for identical data — and raises
        outright under ascii. A provenance package whose artifact hashes depend on the
        writer's locale has one job and does not do it.

        `comment` is the marker the pin is written behind, because `#` is not a comment
        everywhere. **There is no way to ask for no pin**: that is what `output()` is for,
        and a caller writing parquet or a PNG should use it.

        AND FOR SOME FORMATS IT REFUSES, because "not a comment everywhere" was left as the
        caller's problem and a caller cannot see the consequence. `PIN_UNSAFE` lists them
        with the reason each one fails. The entry that made this a guard rather than a note
        is Newick: a pinned tree does not raise, it *parses*, and comes back with extra
        terminals named out of the pin's own prose — 3 taxa in, 6 out, measured. A loud
        refusal is recoverable; a tree that is quietly wrong is the defect this package
        exists to prevent, produced by the method advertised as the safe default.

        The refusal names `output()`, which is the answer: register the artifact, write the
        bytes yourself, and the record still describes it — only the in-artifact pin is
        given up, which for a format that cannot hold one was never available. A caller who
        wants a pin in a format with its own comment syntax can still write
        `run.header(";")` by hand.

        Refused BEFORE registering, so a rejected path leaves no pending output behind to
        be recorded as `MISSING` by a run that never intended to write it.

        Registers the output first, so a crash between here and the write still records the
        artifact as `MISSING` rather than losing it.
        """
        suffix = pathlib.Path(path).suffix.lower()
        why = PIN_UNSAFE.get(suffix, "the format is not known to accept a `#` comment")
        # BINARY IS A REFUSAL, and it is not about the pin: this method opens in text mode,
        # so a caller cannot write a PNG or a BAM through the handle it returns whatever
        # the pin does. `output()` plus `pin_sidecar()` is the route for those.
        if suffix in PIN_BINARY:
            raise ValueError(
                f"{self.record['script']}: cannot open {pathlib.Path(path).name} here — "
                f"{why}.\n"
                f"    Use `p = run.output(path)`, write it with whatever library owns the "
                f"format, and call `run.pin_sidecar(p)` for the provenance beside it."
            )

        # An explicitly requested ALTERNATIVE marker: the caller has named a comment
        # character this format's parsers may accept, which is a trade they are entitled to
        # make and must not have made for them. Only the exact marker in the table, so
        # passing `"## "` for a VCF still cannot get through -- that one was MEASURED to
        # fail even though it is the format's own marker.
        # AN ALLOWLIST, and inverting it is the point. This asked `is the suffix KNOWN to be
        # unsafe?`, so a format nobody had thought of got a `#` pin written into it -- and
        # every round of review found another one nobody had thought of: Newick, then SVG
        # and JSON from a real plotting script, then pickle. A guard whose default is to
        # corrupt is not a guard. The question is now `is this format KNOWN to take a `#`
        # comment?`, so an unrecognised suffix gets the sidecar, which is safe for anything.
        alternative = PIN_ALTERNATIVE.get(suffix)
        inline = suffix in PIN_INLINE or (alternative is not None and comment == alternative[0])
        if alternative is not None and inline and alternative[1]:
            diagnostic(
                f"  PROVENANCE NOTE: pinning {pathlib.Path(path).name} in-band with "
                f"{comment!r} — {alternative[1]}. `run.pin_sidecar()` avoids the trade."
            )

        p = pathlib.Path(self.output(path))
        p.parent.mkdir(parents=True, exist_ok=True)
        # No **kwargs, deliberately. Every option a caller might pass here is either
        # already decided (encoding, mode) or a reason to use `output()` and open the file
        # themselves. A pinning helper with a dozen knobs is a second `open()`.
        fh = open(p, "w", encoding="utf-8")
        try:
            if inline:
                fh.write(self.header(comment))
        except Exception:  # guards-ok: an artifact half-written by this method would be
            # worse than one this method refused to open -- close before re-raising
            fh.close()
            raise
        if not inline:
            # TEXT, but with nowhere to put a comment. The artifact is written untouched
            # and the pin goes beside it, so "this artifact can say what it was made from"
            # survives for a FASTA or a JSONL exactly as it does for a TSV.
            self.pin_sidecar(p, comment=comment)
        return fh

    def pin_sidecar(self, path: str | pathlib.Path, comment: str = "# ") -> pathlib.Path:
        """Write the pin BESIDE an artifact instead of inside it, and register it.

        The general answer for every format that cannot hold a comment: a FASTA, a JSONL, a
        BAM, a PNG, a parquet. `<artifact>.prov.txt` carries exactly what `header()` would
        have written into the file, so the property that matters -- an artifact that can say
        what it was made from, after the run's own sidecar has been overwritten by the next
        run -- survives for formats that could never take an in-band pin.

        Registered as an output, so it is hashed and recorded like any other artifact and
        `verify` can read it. It is a real file the run produced, not a note about one.

        Weaker than an in-band pin, and honestly so: a sidecar can be separated from its
        artifact by a copy, a move or a `tar` that takes one and not the other. That is why
        in-band is still the default wherever the format allows it. It is much stronger than
        nothing, which is what these formats had.

        Call it after `run.output(p)` for a file another library writes:

            fig = run.output(OUT / "panel.png")
            plt.savefig(fig)
            run.pin_sidecar(fig)
        """
        target = pathlib.Path(path)
        side = target.with_name(target.name + PIN_SIDECAR_SUFFIX)
        side.parent.mkdir(parents=True, exist_ok=True)
        # The artifact's own name is in the sidecar, because a `.prov.txt` that has been
        # separated from what it describes should still say what it described.
        body = f"{comment}provenance for: {target.name}\n{self.header(comment)}"
        side.write_text(body, encoding="utf-8")
        self.output(side)
        return side

    def write_json(
        self, path: str | pathlib.Path, payload: dict[str, typing.Any], key: str = "_provenance"
    ) -> pathlib.Path:
        """Write a JSON artifact with the pin embedded as a KEY, registered and hashed.

        JSON has no comment syntax, so there is no in-band pin a file handle could write --
        `open_output` gives a `.json` a sidecar for exactly that reason. What JSON does have
        is structure, and a top-level key is a place a pin can live where every parser will
        read it and none will choke on it. That cannot be done through a handle, because it
        means serialising the whole document, so it is its own method.

        IT CHANGES YOUR SCHEMA, and that is why it is opt-in rather than what `.json` does
        by default. A consumer iterating top-level keys sees one more than it wrote. Pass a
        `key` your readers ignore, or use `output()` and take the sidecar.

        The pin is stored as STRUCTURE, not as the rendered comment block: a consumer
        reading the digests should not have to parse prose out of a string.

        A mapping only. A JSON array has nowhere to put a key, and wrapping it in an object
        to make room would change what the document IS rather than annotate it -- so that
        raises here, where the caller can see it, rather than silently restructuring.

            run.write_json(OUT / "calls.json", {"variants": rows})
        """
        if not isinstance(payload, dict):
            raise TypeError(
                f"{self.record['script']}: write_json needs a mapping to add {key!r} to, "
                f"not {type(payload).__name__}. Wrapping it would change what the document "
                f"is; use `run.output(path)` and `run.pin_sidecar(path)` instead."
            )
        if key in payload:
            raise ValueError(
                f"{self.record['script']}: {key!r} is already in the payload — refusing to "
                f"overwrite it. Pass a different `key=`."
            )
        p = pathlib.Path(self.output(path))
        p.parent.mkdir(parents=True, exist_ok=True)
        pin = {
            "script": self.record["script"],
            "generation": self.record["generation"],
            "commit": self.record["code"]["git_commit_short"],
            # The same expression the in-band pin uses, so a checker reading either sees
            # the same digest for the same input. See `hashing.pin_digest`.
            "inputs": sorted(
                {(pin_digest(i), self._pin_name(i.get("path", "?"))) for i in self.record["inputs"]}
            ),
        }
        self._pin_rendered = True
        p.write_text(
            json.dumps({key: pin, **payload}, indent=2, sort_keys=False, default=str) + "\n",
            encoding="utf-8",
        )
        return p

    def environment_snapshot(
        self, directory: str | pathlib.Path | None = None
    ) -> dict[str, typing.Any] | None:
        """Capture the FULL installed package set, content-addressed.

        `environment.packages` records only the tracked subset -- enough to explain a
        numerical difference in the usual case, useless when the cause is a package nobody
        thought to track. This is the whole set, written as `env-<sha16>.txt`, so two runs
        in the same environment reference one file and a changed environment is visible as
        a changed digest rather than as a diff across timestamped filenames.

        Called automatically by `write()` when the project configures a directory.
        """
        d = directory or self.project.env_snapshot_dir
        if d is None:
            return None
        try:
            rec = write_snapshot(pathlib.Path(d))
            # Hashing a lock file records WHICH one; copying it means the run can still be
            # rebuilt after that file has moved on -- which it will, because a lock file
            # changes every time a dependency does. Same directory and the same
            # content-addressing as the package snapshot.
            rec["lockfiles"] = archive_lockfiles(pathlib.Path(self.project.root), pathlib.Path(d))
        except Exception as exc:  # never let provenance capture break a run
            diagnostic(f"  WARNING: could not write environment snapshot: {exc}")
            rec = {"error": str(exc)}
        self.record["environment"]["snapshot"] = rec
        return rec

    def seeds(self, seeds: typing.Iterable[int]) -> None:
        """RECORD the seeds this run used. It does NOT set them.

        The name reads like a setter and it is not one: nothing here touches `random`,
        `numpy.random`, `torch` or `PYTHONHASHSEED`. Seed your generators yourself and
        report the same values here — a run that calls this and never seeds anything
        records a seed it did not use, which is worse than recording none, because the
        record then asserts a reproducibility that does not hold.

        It is a list because a run usually has more than one generator and they are not
        interchangeable; recording only the one you remembered is how a rerun diverges in
        a way nothing explains. Replaces on each call rather than accumulating.

        Coerced with `int()` at registration, so a `numpy.int64` seed lands as a number the
        record can be read back from, and a value that is not seed-shaped fails HERE, where
        the caller can see which one it was.
        """
        self.record["seeds"] = [int(s) for s in seeds]

    def note(self, key: str, value: typing.Any) -> None:  # noqa: ANN401
        """Any, deliberately: a note is whatever number or string the script wants recorded."""
        self.record.setdefault("notes", {})[key] = value

    def module(self, module: types.ModuleType) -> None:
        """Record where an imported module actually RESOLVED from.

        A commit is not sufficient provenance when two installables share a name: a PEP 660
        finder can resolve an import to a sibling project's copy, and `sys.path.insert`
        will not override it. A record naming this repository's commit then attributes the
        run to code that never executed. Recording the resolved `__file__` and its hash is
        what makes that case detectable at all.
        """
        f = getattr(module, "__file__", None)
        rec: dict[str, typing.Any] = {
            "module": getattr(module, "__name__", str(module)),
            "resolved_file": f,
        }
        if f:  # guards-ok: a module with no __file__ (builtin, namespace package) is
            # recorded with resolved_file: null rather than skipped — the absence is
            # the finding, since a builtin cannot be the vendored copy this guards against
            fp = pathlib.Path(f)
            rec["inside_project"] = str(self.project.root) in str(fp.resolve())
            if fp.is_file():  # guards-ok: no sha256 key means the resolved path is not
                # a readable file, which resolved_file already states. Hashing a
                # non-existent path would raise inside provenance capture instead.
                rec["sha256"] = sha256(fp)
            if not rec["inside_project"]:
                diagnostic(
                    f"  PROVENANCE WARNING: {rec['module']} resolved OUTSIDE the project root: {f}"
                )
        self.record.setdefault("modules", []).append(rec)

    # ---------------------------------------------------------------- the pin
    def header(self, comment: str = "# ") -> str:
        """The PIN, as a comment block to embed in the artifact ITSELF.

        A sidecar provenance file is overwritten by the next run, so a committed artifact
        and the record describing it drift apart. The failure that produced this method: a
        locked holdout recorded the hash of its own body and nothing about the corpus it
        was drawn from; its input then moved four times without the lock being able to say
        so, and three fabricated labels sat inside a locked evaluation set.

        **A lock is only as good as the thing it pins. An artifact that cannot say what it
        was made from cannot be invalidated when that thing changes.**

        Excluded on purpose, both learned by breaking it:

        * **No timestamp.** Every artifact would differ on every run for no reason.
        * **No run id** — a run id embeds a UTC stamp, so it is a timestamp wearing a
          different name. The first version carried one and two identical runs over
          identical inputs both reported 80 artifacts CHANGED. `run_id` answers *which
          pass wrote this*; the pin answers *what it was made from*. Only the second
          belongs in the artifact; the sidecar already carries the first.

        Input hashes are the opposite: they change only when an input changes, which is
        exactly when the artifact should read as CHANGED.
        """
        c = comment
        code = self.record["code"]
        # NOT `None`. The first artifact a new project produces is written before its
        # first commit, and a pin reading "commit: None" is a value a reader has to
        # interpret. Say what it means: there is no commit to name.
        commit = code["git_commit_short"] or "NONE — no commit to name (yet)"
        lines = [
            f"{c}provenance — this artifact and what produced it",
            f"{c}  script     : {self.record['script']}",
            f"{c}  generation : {self.record['generation']}",
            f"{c}  commit     : {commit}"
            + (
                "  (CODE DIRTY — the commit does not identify what ran)"
                if code["git_code_dirty"]
                else ""
            ),
        ]
        # SORTED AND DEDUPED, and that is the determinism guarantee rather than a tidy-up.
        # This rendered inputs in REGISTRATION order, and `Path.glob()` returns filesystem
        # order -- so the ordinary `for p in DIR.glob("*.tsv"): run.input(p)` produced a
        # different pin on a different machine while the data was identical. That is the
        # 80-artifacts-CHANGED regression this method exists to prevent, arriving through a
        # different door. Duplicates collapse for the same reason: a data-dependent read
        # loop must not move the pin.
        #
        # The SIDECAR still records every registration, in order. How many times a script
        # opened a file is a fact about the run, and facts about the run live there.
        pinned: dict[tuple[str, str], None] = {}
        for i in self.record["inputs"]:
            # Content digest FIRST: two runs over the same data must pin identically. The
            # precedence lives in `pin_digest` so that `verify` reads pins with the same
            # expression that wrote them -- see its docstring for why that matters.
            pinned.setdefault((pin_digest(i), self._pin_name(i.get("path", "?"))), None)
        ins = sorted(pinned)
        if ins:
            lines.append(f"{c}  inputs ({len(ins)}), sha256:")
            for sha, name in ins:
                lines.append(f"{c}    {sha}  {name}")
        else:
            # NOT silence. "Derived from nothing" and "reads bypass run.input()" look
            # identical in an artifact unless one of them says so.
            lines.append(
                f"{c}  inputs     : NONE REGISTERED. Either this artifact is "
                f"derived from nothing, or its reads bypass run.input()."
            )
        self._pin_rendered = True
        return "\n".join(lines) + "\n"

    @staticmethod
    def _safe_for_pin(name: str) -> str:
        """Make a filename safe to write into a pin, changing nothing else.

        Two hazards, both measured on real behaviour rather than imagined:

        **A newline forges an entry.** The pin is line-oriented. A file named
        `a.tsv\\n#     0000000000000000  NEVER_READ.tsv` produced a pin whose body read
        `inputs (1)` above TWO listed inputs, the second naming a file nobody read with a
        digest nobody computed. A lone `\\r` does the same to any reader that honours it.

        **An undecodable name breaks the caller's write.** A POSIX filename is bytes;
        Python decodes an invalid one with `surrogateescape`, so the name carries lone
        surrogates and `fh.write(run.header())` under UTF-8 raises `UnicodeEncodeError`.
        Provenance then kills the artifact it was describing.

        SURGICAL, character by character. The first version ran the whole name through
        `unicode_escape` whenever any character offended, which mangled `café` into
        `caf\\xe9` in a committed artifact for a corpus that has accented filenames. The
        offending character is escaped; every other one is left exactly as it is.

        The name is KEPT in escaped form rather than dropped: an odd filename is a fact
        about the run, and a pin that silently omits an input is the defect this whole
        block exists to prevent.
        """
        named = {"\n": "\\n", "\r": "\\r", "\t": "\\t"}
        out = []
        for ch in name:
            code = ord(ch)
            if ch in named:
                out.append(named[ch])
            elif code < 0x20 or code == 0x7F:
                out.append(f"\\x{code:02x}")
            elif 0xDC80 <= code <= 0xDCFF:
                # `surrogateescape` maps an undecodable byte B to U+DC00+B. Render the BYTE,
                # which is what was actually on disk.
                out.append(f"\\x{code - 0xDC00:02x}")
            elif 0xD800 <= code <= 0xDFFF:
                out.append(f"\\u{code:04x}")  # any other lone surrogate: still unencodable
            else:
                out.append(ch)
        return "".join(out)

    def _pin_name(self, raw: str) -> str:
        """The label a pinned input carries INSIDE an artifact.

        Relative to the project root where possible; otherwise `<external>/<name>` rather
        than the absolute path. An absolute path embeds this machine's directory layout in
        a committed artifact, so the same data pinned on two machines produced two
        different pins -- the same machine-dependence class as the encoding defect. The
        full path stays in the sidecar, where it is information rather than a comparison
        key.
        """
        try:
            p = pathlib.Path(raw)
            if not p.is_absolute():
                # RESOLVED AGAINST THE RUN'S CWD FIRST. `relative_to` on a raw relative
                # string always raises against an absolute root, so `run.input("data/x.tsv")`
                # -- the natural spelling -- pinned as `<external>/x.tsv`, announcing a file
                # as foreign to the very repository holding it.
                p = pathlib.Path(self.record["cwd"]) / p
            root = pathlib.Path(self.project.root)
            for candidate, base in self._pin_bases(p, root):
                try:
                    rel = candidate.relative_to(base)
                except ValueError:
                    continue
                # `.as_posix()`, NOT `str()`. `str(PurePath)` renders the platform
                # separator, so the same input pinned on Windows and on Linux produced two
                # DIFFERENT pins for identical data -- the exact machine-dependence this
                # method's docstring says it exists to prevent. `describe()` already used
                # as_posix() for the tree hash; the pin did not. Found by the Windows CI
                # job, not by review.
                return self._safe_for_pin(rel.as_posix())
        except (TypeError, OSError, RuntimeError):
            # RuntimeError is `resolve()` on a SYMLINK LOOP, and it is not an OSError --
            # so it was never caught here, escaped `_pin_name`, escaped `header()`, and
            # killed the run at the moment it tried to describe itself. A loop is a
            # misconfigured mount or a broken staging step, which is a fact about the
            # inputs worth recording, not a reason to lose the run: it pins as external,
            # which is what "we could not place this under the root" means.
            pass
        return self._safe_for_pin(f"<external>/{pathlib.Path(raw).name}")

    @staticmethod
    def _pin_bases(p: pathlib.Path, root: pathlib.Path) -> list[tuple[pathlib.Path, pathlib.Path]]:
        """The two ways a path can be under the root, in the order they should be tried.

        AS SPELLED FIRST, symlinks intact. `resolve()` alone followed every link, so the
        standard layout where `data/` is a symlink to a big disk -- and every Nextflow or
        Snakemake work directory, which stages inputs as symlinks -- pinned real repository
        data as `<external>/x.tsv`. That is wrong twice over: it announces a file as foreign
        to the repository holding it, and `<external>/` is deliberately not a path, so
        `verify` reports it UNVERIFIABLE. Those inputs were unpinnable AND uncheckable.

        The spelled form is also the more stable one. `/mnt/bigdisk/data/x.tsv` is this
        machine's mount layout; `data/x.tsv` is what the repository looks like everywhere,
        which is the property the pin is for.

        RESOLVED SECOND, because the root itself can be reached through a link -- macOS
        `/tmp` is `/private/tmp`, and plenty of clusters mount homes through one. There the
        spelled path is not under the spelled root and only resolving finds the relationship.

        A path containing `..` skips the spelled attempt entirely: `link/../x` normalises to
        the parent of the LINK, while on disk it means the parent of its TARGET, so the
        cheap normalisation would name a file that is not the one that was hashed. Rare, and
        the resolved form answers it correctly, so the ambiguity is declined rather than
        guessed at.
        """
        bases = []
        if ".." not in p.parts:
            bases.append((pathlib.Path(os.path.normpath(p)), pathlib.Path(os.path.normpath(root))))
        bases.append((p.resolve(), root.resolve()))
        return bases

    # ---------------------------------------------------------------- finish
    def write(self, path: str | pathlib.Path) -> pathlib.Path:
        """Hash the registered outputs, write the record, append to the history.

        CALLING THIS *INSTEAD OF* PASSING `provenance=` IS THE TRAP, and it is the reason
        this method has a warning rather than a one-line description. `write()` runs where
        you put it, so a run that dies before that line writes nothing at all: no sidecar,
        no history entry, no warning — and a crash is the case the record was for. The
        method cannot detect the mistake, because a call that never happens has nothing to
        detect. Only the constructor can: `provenance=` is what arms `__exit__`, which is
        the one place the final status is known.

        Calling it INSIDE a `with Run(..., provenance=PROV)` block is fine and sometimes
        useful — a caller may want the record at a second path. The history is still
        appended exactly once, at exit, with the true status. It is `write()` as a
        SUBSTITUTE for `provenance=` that loses the run, not `write()` itself.

        Returns the path written, so a caller can register or log it.
        """
        p = pathlib.Path(path)
        if (
            self.project.env_snapshot_dir is not None
            and "snapshot" not in self.record["environment"]
        ):
            self.environment_snapshot()
        # THE UNCLOSED HALF OF R10. `unstable_during_hash` catches a file rewritten WHILE it
        # was read; nothing caught one rewritten a second later, so a run could pin
        # `sha256: abc...` and finish beside a file that no longer had those bytes. A stat
        # per input, not a re-hash -- see `moved_since` for why, and for the resolution
        # limit it does not hide.
        changed = []
        for entry in self.record["inputs"]:
            # Against the RECORDED cwd, exactly as the output loop below does. These two
            # loops disagreed: outputs anchored, inputs did not, so a `chdir` between
            # registration and here stat'd a relative input against the wrong directory.
            why = moved_since(entry, base=pathlib.Path(self.record["cwd"]))
            if why:
                entry["changed_after_registration"] = why
                changed.append(f"{entry.get('path', '?')} ({why})")
        if changed:
            diagnostic(
                f"  PROVENANCE WARNING: {self.record['script']}: "
                f"{len(changed)} registered input(s) CHANGED after they were read:\n"
                + "".join(f"    {c}\n" for c in changed)
                + "    The digests in this record and in any pin are what the run actually\n"
                "    read. They no longer describe what is on disk, and a checker comparing\n"
                "    the two will report a difference that is real but is not the run's."
            )

        # REBUILT, not appended to. `outputs` is derived entirely from `_pending`, and a
        # second `write()` -- which this method's own docstring calls legitimate, for a
        # caller wanting the record at a second path -- used to append the same files
        # again. The count doubled for one artifact, and inside a `with` block the history
        # line is deferred to `__exit__`, so it read the already-doubled list and the
        # doubling reached the permanent, append-only history. The existing guard counts
        # history LINES, which stayed correct while what was inside them did not.
        self.record["outputs"] = []
        seen: set[pathlib.Path] = set()
        for q in self._pending:
            if q in seen:
                continue
            seen.add(q)
            # Against the RECORDED cwd, never the current one. `output()` anchored this
            # path when it was registered, so a relative entry means "relative to
            # record['cwd']" — and resolving it against a directory the script has since
            # chdir'd into is what hashed one file while naming another.
            target = q if q.is_absolute() else pathlib.Path(self.record["cwd"]) / q
            if target.exists():
                try:
                    described = describe(target)
                except (OSError, ValueError) as exc:
                    # AN OUTPUT THAT CANNOT BE HASHED IS A FINDING, NOT A REASON TO LOSE
                    # THE RUN. Unguarded, this raise left `write()`, left `_finish()`, and
                    # was swallowed by `__exit__`'s catch-all as a one-line warning: no
                    # sidecar, no history entry, exit 0 -- WITH `provenance=` and WITH a
                    # `with` block, the two things documented to guarantee a record. Every
                    # input, note, seed and successfully written output went with it, for
                    # one awkward file among them.
                    #
                    # Reachable without contrivance: a Snakemake `pipe()` output or a bash
                    # process substitution (a FIFO, which `describe` refuses BY DESIGN
                    # rather than blocking on), a file a container wrote as root, an NFS
                    # permission quirk, or an output deleted between `exists()` and here.
                    #
                    # `input()` has guarded exactly this call all along and raises AT
                    # REGISTRATION, before the work -- the asymmetry was the defect:
                    # `output()` defers hashing to here, AFTER the work, where raising
                    # destroys a record that is otherwise complete and true.
                    described = {
                        "path": str(q),
                        "kind": "UNHASHABLE",
                        "note": f"exists but could not be hashed: {exc}",
                    }
                    # Announced, unlike MISSING, because the two degrade differently: a
                    # MISSING output is a file the user can see is absent, while this one
                    # is present and ordinary-looking and merely has no digest in the
                    # record -- the silent degradation this package exists to refuse.
                    diagnostic(
                        f"  PROVENANCE WARNING: {self.record['script']}: output {q} exists "
                        f"but could not be hashed ({exc}).\n"
                        f"    It is recorded as UNHASHABLE with no digest. The run IS "
                        f"recorded; this artifact cannot be pinned or joined on."
                    )
                # Keep the spelling the caller registered: `describe` reports the path it
                # was handed, and substituting the resolved one would rewrite every
                # ordinary relative output into an absolute path for no reason.
                described["path"] = str(q)
                self.record["outputs"].append(described)
            else:
                # A registered output that was never written is a FINDING, not an
                # omission. Dropping it here is how a stage reports success having
                # produced nothing.
                self.record["outputs"].append(
                    {"path": str(q), "kind": "MISSING", "note": "registered but never written"}
                )
        self.record.setdefault("status", "ok")
        self.record["finished_utc"] = dt.datetime.now(dt.timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
        # `already` asks "does the history have this run?", NOT "does a sidecar exist?".
        # Conflating the two appended twice for one run, both lines carrying the same
        # run_id, so any tally over runs.jsonl was silently inflated.
        already = self._history_appended
        # A SIDECAR IS ONE RUN'S RECORD, and writing two runs to one path leaves the file
        # describing whichever finished last while the artifact beside it came from the
        # other. The history keeps both, so nothing is lost -- but the sidecar is what a
        # reader opens next to an output, and last-write-wins is not something it should
        # discover by noticing the run_id is unfamiliar.
        #
        # Per PROCESS and by RESOLVED path: two Runs in one script sharing a path is the
        # case this catches. Two separate processes cannot see each other here, and a lock
        # would be the wrong price for a naming mistake.
        resolved = str(pathlib.Path(p).resolve())
        with _SIDECARS_LOCK:
            prior = _SIDECARS.get(resolved)
            _SIDECARS[resolved] = self.record["run_uid"]
        if prior and prior != self.record["run_uid"]:
            diagnostic(
                f"  PROVENANCE WARNING: {self.record['script']}: this sidecar was already "
                f"written by ANOTHER RUN in this process —\n"
                f"    {resolved}\n"
                f"    It now describes this run and no longer describes the earlier one, "
                f"whose outputs may sit\n"
                f"    beside it. Both are still in the history; give each run its own "
                f"`provenance=` path."
            )
        self._written = True
        self._last_written = p  # so __exit__ can correct THIS file, kwarg or not
        self._persist(p)  # never raises; a sidecar failure must not lose the history
        if self._in_context and not already:
            self._deferred_history = p  # see __init__; __exit__ appends it once
        elif already:
            # A second write() rewrites the sidecar deliberately (a caller may want the
            # record at two paths) but must NOT append a second history line: one run is
            # one line, or every count taken from the history is wrong.
            summary("  (history already recorded for this run; sidecar rewritten only)")
        else:
            self._append_history(p)

        if self.record.get("status") == "failed":
            # DIAGNOSTIC: the run did not do what it was asked to. Not quietenable.
            diagnostic(
                f"  RUN FAILED — recorded: {self.record['failure']['type']}: "
                f"{self.record['failure']['message'][:120]}"
            )
        # CONFIRMATION: the record was written, and here is what is in it. Every fact in
        # these two lines is also in the sidecar this line names, which is why this is the
        # one message RUNPROV_QUIET may hide.
        summary(
            f"provenance -> {p}",
            # The HISTORY path, not only the sidecar. Printing the sidecar and never this
            # is what made a split history invisible from the terminal.
            f"  history -> {self.record['history']['destination']}\n"
            f"  code {self.record['code']['git_commit_short']}"
            f"{' (CODE DIRTY)' if self.record['code']['git_code_dirty'] else ''}"
            # "we could not look" must not print as the reassuring answer.
            f"{'' if self.record['code']['git_status_captured'] else ' (DIRTY STATE UNKNOWN)'}"
            f"  inputs {len(self.record['inputs'])}"
            f"  outputs {len(self.record['outputs'])}"
            f"  seeds {self.record['seeds']}",
        )
        return p

    def _encode(self) -> str:
        """Serialise the record, degrading a value that cannot encode rather than dying.

        `default=str` handles unencodable VALUES and not KEYS, so `note("confusion",
        {(1, "a"): 0.9})` -- what a per-class confusion matrix looks like -- raised
        TypeError, the sidecar was never written, and the whole run vanished. A note that
        cannot be encoded is worth less than the run it belongs to.
        """
        try:
            return json.dumps(_jsonable(self.record), indent=2, default=str)
        except (TypeError, ValueError) as exc:
            diagnostic(
                f"  WARNING: could not serialise part of the record ({exc}); "
                f"degrading the offending entries"
            )
            for key in ("notes", "parameters"):
                section = self.record.get(key)
                if not isinstance(section, dict):
                    continue
                for k, v in list(section.items()):
                    try:
                        json.dumps({k: v}, default=str)
                    except (TypeError, ValueError):
                        section[k] = f"UNSERIALISABLE <{type(v).__name__}>"
            return json.dumps(_jsonable(self.record), indent=2, default=str)

    def _persist(self, p: pathlib.Path) -> bool:
        """Write the sidecar. NEVER raises.

        Split out of write() so __exit__ can correct a record that has become false
        without appending a second history line. It swallows because of what it is: this
        ran while an exception was already in flight, and a provenance module that
        replaces the failure it is recording destroys the diagnostic AND the record. The
        rule `sinks.py` states for sinks -- "a sink that can abort a run gets removed from
        the run" -- was never applied to the sidecar path. Measured before this: a
        RuntimeError from the user's code was replaced by NotADirectoryError from here.
        """
        text = self._encode()
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(text, encoding="utf-8")
            return True
        except OSError as exc:
            diagnostic(f"  WARNING: could not write the provenance sidecar to {p}: {exc}")
            return False

    def _append_history(self, prov_path: pathlib.Path) -> None:
        """Append the summary to the project's sink — one line per run, forever.

        Each sidecar holds only the LATEST run of a script; the history holds every run
        ever. It is the one thing an experiment tracker gives you that per-run files do
        not, and keeping it as a git-tracked JSONL rather than a service means it is
        diffable, travels with the repository, and needs nothing a reviewer cannot run.

        Where it goes is the project's business (`Project.sink`), not this method's.
        """
        r = self.record
        summary = {
            "schema": HISTORY_SCHEMA,
            "script": r["script"],
            "run_uid": r["run_uid"],
            "run_id": r["run_id"],
            "generation": r["generation"],
            # So `grep '"status": "failed"' runs.jsonl` is the whole query. A history of
            # successes only cannot tell you how often a step fails.
            "status": r.get("status", "ok"),
            "failure": r.get("failure"),
            "started_utc": r["started_utc"],
            "finished_utc": r["finished_utc"],
            "command": r["command"],
            "cwd": r["cwd"],
            "parameters": r["parameters"],
            "seeds": r["seeds"],
            "git_commit": r["code"]["git_commit_short"],
            # A SUMMARY, not the list. The full per-file hashes are in the sidecar; the
            # history is appended forever, and 50 modules per line would multiply it. One
            # digest answers "did any first-party code change between these two runs",
            # which is the question the history is asked, and the count says how much it
            # is a digest OF.
            "imported_code": {
                "count": (r["code"].get("imported") or {}).get("count"),
                "digest": (r["code"].get("imported") or {}).get("digest"),
            },
            "git_code_dirty": r["code"]["git_code_dirty"],
            "git_status_captured": r["code"]["git_status_captured"],
            "git_tree_dirty": r["code"]["git_tree_dirty"],
            # A history line that says which history it belongs to. Once a line has been
            # copied, archived or merged, this is the only record of where it landed.
            "history_destination": r["history"]["destination"],
            "project_source": r["history"]["project_source"],
            # THE PATH, not only its hash. This carried `script_sha256` alone, which
            # answers "did the code change" and not "which file was it" — so the history
            # could not say what ran without opening the sidecar, and the sidecar holds
            # only that script's LATEST run. The old transformation log's `script:` field,
            # hand-typed and demonstrably wrong (it names `proteins_ns5b_domains/…` for a
            # step whose `run_command` runs `…_3utr/v65/…`), is the field this replaces,
            # so the replacement has to be present in the same file a reader reads.
            "script_file": r["code"]["script_file"],
            "packages": r["environment"]["packages"],
            # The content-addressed full package set, when the project configures one.
            # `packages` is the tracked subset — four names by default — which is enough
            # to explain the usual numerical difference and useless when the cause is a
            # package nobody thought to track. Absent (not null) when snapshots are off,
            # because "this project does not capture them" and "capture was attempted and
            # produced nothing" are different facts and only one of them is true here.
            **(
                {"environment_snapshot": r["environment"]["snapshot"]}
                if "snapshot" in r["environment"]
                else {}
            ),
            # BOTH identities. The pin uses `content_sha256`; this carried `sha256`
            # alone, so the same input had two names and nothing could join a pinned
            # artifact back to the run that made it without rehashing every candidate.
            "inputs": [
                {
                    "path": i["path"],
                    "sha256": i.get("sha256") or i.get("sha256_tree"),
                    "content_sha256": i.get("content_sha256"),
                }
                for i in r["inputs"]
            ],
            "outputs": [
                {
                    "path": o["path"],
                    "sha256": o.get("sha256") or o.get("sha256_tree"),
                    "content_sha256": o.get("content_sha256"),
                }
                for o in r["outputs"]
            ],
            # Where the FULL record is. Without this an archiver reading the history can
            # find every artifact a run produced EXCEPT its own provenance.
            "provenance_path": str(prov_path),
            "notes": r.get("notes", {}),
            # The old log's `terminal_log_file`, and more than it: the mechanism is carried
            # alongside the path, so a reader can tell an empty log that saw everything from
            # one that could never have seen a subprocess. Absent when nothing was captured.
            **({"terminal_log": r["terminal_log"]} if "terminal_log" in r else {}),
        }
        self._history_appended = True
        # `_jsonable` here too: the sink dumps SEPARATELY, so sanitising only the
        # sidecar left the history line carrying bare NaN, unreadable to a strict parser.
        self.project.resolved_sink().append(_jsonable(summary))
