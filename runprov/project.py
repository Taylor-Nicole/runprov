# Copyright (c) 2026 Assistance Publique – Hôpitaux de Paris (AP-HP),
#                    Hôpital Henri-Mondor, and Taylor Thompson
# Licensed under the CeCILL-B Free Software License Agreement — see LICENSE.
# https://cecill.info/
"""Where the run is happening: repo root, run log, what counts as code.

Everything in this module exists because `_provenance.py` knew the answers by position.
`REPO = pathlib.Path(__file__).resolve().parents[2]` is correct exactly once — for a file
at `scripts/audit/_provenance.py` in one repository. Move the file, vendor it, or import
it from a second project and it silently points somewhere else. A provenance module that
can be wrong about which repository it is in is worse than none, because every record it
writes still looks authoritative.

So the location is CONFIGURED, with detection as a default and the detected value written
into every record. A reader can always see which root a run believed it had.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import inspect
import os
import pathlib
import re
import subprocess
import typing

from .sinks import JsonlSink, RecordSink

# Packages whose version is recorded with every run. The audit's list is an ML stack;
# yours will differ, which is why it is a field and not a constant.
DEFAULT_TRACKED = ("numpy", "pandas", "scipy", "sklearn")

# Paths whose modification means "the code that ran is not the committed code". Anything
# that changes behaviour belongs here — including declarative inputs like a rule registry
# or a config directory, which are code in every sense that matters to a result.
#
# THIS IS A HINT, NOT THE DEFINITION. It used to be the pathspec of `git status`, so a
# project laid out any other way — `pkg/` instead of `src/` — edited uncommitted code and
# was recorded `git_code_dirty: false`, with an empty file list and a clean-looking commit.
# The most consequential boolean in the record failed toward the reassuring answer, and it
# did so for every layout its author had not thought of. `classify_status` now reads the
# WHOLE tree and uses this list to widen, never to narrow: a changed file is code if it is
# under one of these paths OR if it is recognisably source (`looks_like_code`).
DEFAULT_CODE_PATHS = ("src", "scripts", "conf", "pyproject.toml", "Makefile")

# Suffixes of files that are EXECUTABLE SOURCE in any layout, in any directory. Deliberately
# not `.json`, `.yaml`, `.csv`, `.tsv`, `.parquet` or `.md`: those are what a pipeline
# WRITES, and a boolean that goes red on its own outputs is a permanently red check, which
# trains everyone to ignore it. Declarative-but-behavioural inputs (a rule registry, a conf
# directory) are exactly what `code_paths` is for — the project declares those, because only
# the project knows which of its data files are read as configuration.
CODE_SUFFIXES = frozenset(
    {
        ".bash",
        ".c",
        ".cc",
        ".cpp",
        ".cs",
        ".cwl",
        ".go",
        ".h",
        ".hpp",
        ".ipynb",
        ".java",
        ".jl",
        ".js",
        ".lua",
        ".m",
        ".mjs",
        ".nf",
        ".pl",
        ".ps1",
        ".py",
        ".pyi",
        ".pyx",
        ".r",
        ".rb",
        ".rmd",
        ".rs",
        ".scala",
        ".sh",
        ".smk",
        ".sql",
        ".ts",
        ".wdl",
        ".zsh",
    }
)

# Basenames with no informative suffix that decide how the code is built or entered.
CODE_BASENAMES = frozenset(
    {
        "cmakelists.txt",
        "containerfile",
        "dockerfile",
        "gnumakefile",
        "justfile",
        "makefile",
        "meson.build",
        "nextflow.config",
        "rakefile",
        "setup.cfg",
        "setup.py",
        "snakefile",
        "pyproject.toml",
    }
)

# How many non-code change lines are kept verbatim in a record. `git_other_changes` was an
# integer with no file list, so "3 other changes" could be data churn or three edited
# modules and no reader could tell. The list is capped because a repository with a 273 MB
# report tree can have thousands of untracked files, and a provenance record must not
# become one; `git_other_files_omitted` states what was left out.
OTHER_FILES_KEPT = 50


def git(root: pathlib.Path, *args: str) -> str | None:
    """Run git in `root`. Returns None on any failure, including git not existing.

    Never raises. Provenance capture that can abort a run gets removed from the run.
    """
    try:
        # S603/S607 are suppressed in .ruff.toml for THIS FILE, with the reason there,
        # rather than inline. The formatter decides which physical line a multi-line call
        # reports on, so an inline suppression silently stops applying the next time
        # anyone runs `ruff format` -- and a suppression a reformat can detach is not a
        # decision, it is a coincidence. (Writing the directive token in this comment also
        # made ruff parse the prose AS a directive, which is its own small lesson.)
        r = subprocess.run(
            ("git", "-C", str(root), *args), capture_output=True, text=True, timeout=20
        )
        return r.stdout.strip() if r.returncode == 0 else None
    except Exception:
        return None


def looks_like_code(rel_path: str) -> bool:
    """Is this repository-relative path recognisably source, whatever the layout?

    Extension and basename only — no filesystem access, no repository state, so it answers
    the same way for a deleted file as for a modified one. It exists because the alternative
    is a configured path list, and a configured path list is right for exactly the layouts
    its author enumerated. False here is not "clean": it is "not code by this test", and the
    path is still recorded under `git_dirty_other_files` where a reader can see it.
    """
    name = rel_path.rsplit("/", 1)[-1].lower()
    if name in CODE_BASENAMES:
        return True
    dot = name.rfind(".")
    return dot > 0 and name[dot:] in CODE_SUFFIXES


# `XY<space>` — the status field of one porcelain line. `{1,2}` rather than `{2}` for the
# reason `_split_status` explains: the first line arrives one character short.
_PORCELAIN = re.compile(r"^([ ?!ACDMRTUX]{1,2}) ")


def _split_status(line: str) -> tuple[str, list[str]]:
    """One `git status --porcelain` line, restored to its true form, and the paths in it.

    `git()` ends with `stdout.strip()`, which eats the LEADING SPACE OF THE FIRST LINE: an
    unstaged edit arrives as `M pkg/mod.py` instead of ` M pkg/mod.py`. That cost nothing
    while the lines were only printed, and it is a defect the moment anything reads them —
    the path parsed from a fixed column 3 would have been `kg/mod.py`, and the status code
    would have read as STAGED. Both are restored here rather than in `git()`, whose strip
    every other caller depends on.

    A rename is `R  old -> new` and BOTH ends count: moving a module out of the way is a
    code change whichever end you look at. A path git had to quote (`"src/caf\\303\\251.py"`)
    keeps its escapes — they change neither the suffix nor the prefix test — but loses the
    quotes, which would. A line that does not parse yields NO paths, and the caller treats
    that as code: an unreadable status line must not resolve to the reassuring answer.
    """
    m = _PORCELAIN.match(line)
    if m is None:
        return line, []
    restored = line if len(m.group(1)) == 2 else " " + line
    out = []
    for raw in restored[3:].split(" -> "):
        p = raw.strip()
        if len(p) > 1 and p.startswith('"') and p.endswith('"'):
            p = p[1:-1]
        p = p.rstrip("/")  # an untracked directory is reported as `?? pkg/`
        if p:
            out.append(p)
    return restored, out


def _under(rel: str, scope: str) -> bool:
    s = scope.strip("/")
    return rel == s or rel.startswith(s + "/")


@dataclasses.dataclass(frozen=True)
class DirtyState:
    """What one whole-tree `git status --porcelain` says, split into answerable parts.

    `captured` is the field that stops "we could not look" from being recorded as "verified
    clean". `git()` returns None on ANY failure — no repository, no git binary, a 20-second
    timeout — and `bool(None)` is False, so the two were indistinguishable in every record
    this package had written.
    """

    captured: bool
    code: tuple[str, ...] = ()
    other: tuple[str, ...] = ()
    outside_code_paths: tuple[str, ...] = ()


def classify_status(status: str | None, code_paths: typing.Sequence[str]) -> DirtyState:
    """Split a whole-tree porcelain status into code changes and everything else.

    ONE unscoped `git status` is the source of truth and `code_paths` filters its output,
    rather than `code_paths` being the pathspec that decides what git is allowed to see.
    Measured on the host repository (2,994 tracked files, 273 MB of reports): scoped
    12.2 ms, unscoped 16.5 ms — and the module already ran BOTH on every `Run`, so reading
    the whole tree and filtering in Python is 19.2 ms → 14.1 ms, a fix that is also faster.
    """
    if status is None:
        return DirtyState(captured=False)
    code: list[str] = []
    other: list[str] = []
    outside: list[str] = []
    for raw_line in status.splitlines():
        if not raw_line.strip():
            continue
        line, paths = _split_status(raw_line)
        in_scope = any(_under(p, c) for p in paths for c in code_paths)
        # `not paths` FIRST: a line this parser could not read is counted as code, because
        # the alternative is a status line silently resolving to "not code, nothing to see".
        if not paths or in_scope or any(looks_like_code(p) for p in paths):
            code.append(line)
            if not in_scope:
                outside.append(line)
        else:
            other.append(line)
    return DirtyState(True, tuple(code), tuple(other), tuple(outside))


def detect_root(start: pathlib.Path | None = None) -> pathlib.Path:
    """The git top level containing `start`, else `start` itself.

    Detection is a DEFAULT, never an assertion. `Project.root` is recorded in every run
    so a wrong detection is visible in the artifact rather than inferred later.
    """
    s = pathlib.Path(start or pathlib.Path.cwd()).resolve()
    base = s if s.is_dir() else s.parent
    top = git(base, "rev-parse", "--show-toplevel")
    return pathlib.Path(top) if top else base


def _utc() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def default_run_id() -> str:
    """One pass over the data. Env `RUNPROV_RUN_ID` wins so a chain can share one id.

    An unset id becomes `adhoc_<utc>` and is LABELLED as such on purpose: a script someone
    ran by hand and a stage of an orchestrated chain must not be indistinguishable in the
    history. They were, for 1,310 entries, and that is what made the history unreadable.
    """
    return os.environ.get("RUNPROV_RUN_ID") or f"adhoc_{_utc()}"


def default_generation() -> str:
    """Which corpus. A GENERATION is a corpus; a RUN is one pass over it (ADR-024/026)."""
    return os.environ.get("RUNPROV_GENERATION") or "(default)"


@dataclasses.dataclass(frozen=True)
class Project:
    """Everything `Run` needs to know about its surroundings.

    `run_log` defaults under the root rather than to any path this repository uses, so a
    misconfigured install writes somewhere obvious instead of appending to a history it
    does not belong to.
    """

    root: pathlib.Path = dataclasses.field(default_factory=detect_root)
    run_log: pathlib.Path | None = None
    # Where full environment snapshots go. None disables them; `environment.packages` in
    # each record still carries the tracked subset. Opt-in because a snapshot is only
    # worth writing where someone will look for it, and content-addressed so enabling it
    # costs one file per DISTINCT environment rather than one per run.
    env_snapshot_dir: pathlib.Path | None = None
    # WHERE records go. None means "a JsonlSink at resolved_run_log()". Supplying one is
    # how a lab points many pipelines at a shared store without forking the package.
    sink: RecordSink | None = None
    code_paths: tuple[str, ...] = DEFAULT_CODE_PATHS
    tracked_packages: tuple[str, ...] = DEFAULT_TRACKED
    run_id: typing.Callable[[], str] = default_run_id
    generation: typing.Callable[[], str] = default_generation

    def resolved_run_log(self) -> pathlib.Path:
        return self.run_log or (self.root / "provenance" / "runs.jsonl")

    def resolved_sink(self) -> RecordSink:
        return self.sink if self.sink is not None else JsonlSink(self.resolved_run_log())

    def history_destination(self) -> str:
        """WHERE the continuous history actually is, as one printable, recordable string.

        The console printed the SIDECAR path and never this one, so a project whose
        `configure(run_log=...)` had not been imported appended to a second history file
        under a different root and nothing on the terminal differed. The README promises
        "one continuous history, appended to forever"; that promise is only checkable if
        each run says which file it joined.

        A sink with no `path` (a database, a queue) is named by its type — that is all
        there is to say, and saying it is better than printing a run log the sink ignores.
        """
        if self.sink is None:
            return str(self.resolved_run_log())
        path = getattr(self.sink, "path", None)
        name = type(self.sink).__name__
        return f"{name}({path})" if path is not None else name


_ACTIVE: Project | None = None
# Whether `configure()` has run IN THIS PROCESS — not whether a project exists. `active()`
# invents one on demand, which is right (a provenance module must not refuse to record),
# but a run against an invented project is the exact condition of the script that forgot to
# import its project's paths module, so it must be distinguishable and it must say so.
_CONFIGURED = False


def is_configured() -> bool:
    """True once `configure()` has installed a project in this process.

    Exposed so a pipeline can assert it (`assert runprov.is_configured()`) at start-up
    rather than discovering at the end that half its runs went to a different history.
    """
    return _CONFIGURED


def configure(
    project: Project | None = None,
    **kwargs: typing.Any,  # noqa: ANN401 - these are Project's own fields, typed there
) -> Project:
    """Install the project every later `Run()` uses. Call once, at import of your paths
    module — not inside each script, which is how three scripts end up disagreeing."""
    global _ACTIVE, _CONFIGURED
    proj = project if project is not None else Project(**kwargs)
    if proj.sink is not None:
        # `runtime_checkable` checks attribute PRESENCE, not signature. Before this,
        # `isinstance([], RecordSink)` was True and `configure(sink=[])` was accepted --
        # every record then vanished into a list nobody held, while the docstring below
        # promised failure "at configuration". An unfalsifiable guard is worse than none:
        # it is quoted as evidence. So bind the real signature against a specimen record.
        fn = getattr(type(proj.sink), "append", None)
        if not callable(fn):
            raise TypeError(
                f"sink {type(proj.sink).__name__} has no callable `append`; RecordSink "
                f"needs `append(self, record: dict) -> None`."
            )
        try:
            inspect.signature(fn).bind(proj.sink, {})
        except TypeError as exc:
            raise TypeError(
                f"sink {type(proj.sink).__name__}.append does not accept one record "
                f"({exc}); RecordSink needs `append(self, record: dict) -> None`."
            ) from exc
    _ACTIVE = proj
    # Set only on the success path: a `configure()` that RAISED did not configure anything,
    # and recording it as configured would suppress the warning for the one caller who most
    # needs it. Note it is never cleared — a later failed call cannot un-configure a process.
    _CONFIGURED = True
    return _ACTIVE


def active() -> Project:
    global _ACTIVE
    if _ACTIVE is None:
        _ACTIVE = Project()
    return _ACTIVE
