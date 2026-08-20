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

# A MODULE'S `__all__` RATIFIES THE PACKAGE'S PROMISE; IT NEVER MAKES ONE.
# A name belongs here if and only if `runprov/__init__.py` re-exports it and lists it in the
# package `__all__` (22 names, decided 2026-08-19, ledger L-24). Nothing else qualifies:
# cross-module use inside `runprov/` is INTERNAL and `__all__` neither describes nor protects
# it; tests reach into internals on purpose and prove nothing; and prose that documents a
# printed string, a CLI flag or a record key is not an instruction to call a name.
# Adding or withdrawing a promise is a package-level decision taken in `__init__.py`.
# `git` is here BY RATIFICATION, not by evidence, and it is the one name in this list with
# an open question against it: a bare `git` collides with GitPython's top-level module in
# an importing namespace, and the contract is 'swallow every exception, return None, 20s
# timeout' — provenance capture rather than a general-purpose runner. Withdrawing or
# renaming it is a package-level decision and is still open; this file cannot take it.
# `default_run_id` and `default_generation` are WITHDRAWN and stay reachable here.
__all__ = [
    "DEFAULT_CODE_PATHS",
    "DEFAULT_TRACKED",
    "Project",
    "active",
    "configure",
    "detect_root",
    "is_configured",
]

import dataclasses
import datetime as dt
import inspect
import os
import pathlib
import re
import subprocess
import typing

from .sinks import JsonlSink, RecordSink, TeeSink, YamlLogSink

# Packages whose version is recorded with every run. EMPTY BY DEFAULT, and that is the
# considered answer rather than an omission.
#
# It used to be `("numpy", "pandas", "scipy", "sklearn")` -- the source project's stack,
# shipped as everyone's default. For a run that uses none of them the record then carried
#
#     "packages": {"numpy": null, "pandas": null, "scipy": null, "sklearn": null}
#
# on every single line of the history, forever. Each of those nulls is truthful --  `None`
# means "asked for, not present" -- but nobody asked. The package had guessed what mattered
# and then recorded four answers to a question the user never posed, which is the same
# failure as prose provenance one level down: a field populated by assumption rather than
# by observation.
#
# There is no domain-neutral list. A genomics pipeline, a Django service and a PyTorch
# training run share no package worth pinning, and the facts that ARE universal -- the
# interpreter, the platform, the environment manager, the lock files -- are recorded
# unconditionally and were never in this list. So the honest default is to record nothing
# here until asked, which also keeps `None` meaning what it says.
#
# Paste this if you want the scientific stack back; it is one line, and it is now a
# decision the project made rather than one it inherited:
#
#     configure(root=REPO, tracked_packages=("numpy", "pandas", "scipy", "sklearn"))
#
# `run.module(mod)` is the sharper tool for the thing this field is usually reached for --
# it records where an import actually RESOLVED from, and hashes it.
DEFAULT_TRACKED: tuple[str, ...] = ()

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
    Measured 2026-08-18 on `hcv-genotyping-release` (3,058 tracked files, 2.9 GB working
    tree), best of seven, warm: unscoped `git status --porcelain` 35.3 ms, scoped 23.8 ms.
    The module used to run BOTH on every `Run` — 59.2 ms — so running the unscoped one and
    filtering in Python is 59.2 ms → 35.3 ms, a fix that is also faster.

    An earlier version of this note gave "scoped 12.2, unscoped 16.5, so 19.2 → 14.1", which
    cannot be true of any measurement: two calls cost their sum (28.7, not 19.2), and an
    "after" of 14.1 is less than the single call it consists of. Both figures are a snapshot
    of a repository that grows; the durable claim is the SHAPE — two calls became one, and
    the one that survived is the more expensive of the two, which is still a saving.
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


def is_repository(root: pathlib.Path) -> bool:
    """Is there a repository here at all? A FILESYSTEM check, deliberately.

    It exists to tell two very different situations apart, which `git()` returning `None`
    cannot: a project that is simply not under version control, and a repository whose
    `git status` did not run. The first is a stable fact about how someone works. The
    second is an anomaly — a missing binary, a 20 s timeout, a corrupt index — and only the
    second deserves to be shouted about on every run.

    No subprocess, because the reason this is being asked is usually that git did not work,
    and asking git whether git works is not an answer. `.git` is a directory in an ordinary
    clone and a FILE in a worktree or a submodule, so both count; the walk goes up because
    a project root can sit below the repository top level.
    """
    for d in (root, *root.parents):
        if (d / ".git").exists():
            return True
    return False


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


# KEYWORD-ONLY, so field ORDER is not part of the API. Without `kw_only=True` a frozen
# dataclass accepts positional arguments, and 16 fields of a released package would freeze
# their order forever: inserting one beside its logical neighbour would be a breaking change
# rather than an addition.
#
# It is not hypothetical. `warn_unregistered_reads` was added at index 5 on 2026-08-19, which
# shifted `env_snapshot_dir` from 5 to 6 and `terminal_log_dir` from 6 to 7. A caller who had
# written `Project(root, run_log, transformation_log, True, True, SNAPDIR)` would now bind a
# `Path` to a BOOL field and get `env_snapshot_dir = None` -- measured, and it raises nothing,
# because indices 1/2 and 6/7 are all `Path | None` and 3/4/5 are all `bool`. A silent
# mis-binding in the object that decides where every record goes.
#
# `kw_only=True` needs Python 3.10, which is already the floor. Free today; a breaking change
# the day after PyPI.
@dataclasses.dataclass(frozen=True, kw_only=True)
class Project:
    """Everything `Run` needs to know about its surroundings.

    `run_log` defaults under the root rather than to any path this repository uses, so a
    misconfigured install writes somewhere obvious instead of appending to a history it
    does not belong to.
    """

    root: pathlib.Path = dataclasses.field(default_factory=detect_root)
    run_log: pathlib.Path | None = None
    # THE HUMAN-READABLE TWIN of `run_log`, appended in step with it: one `- step:` entry
    # per run, the shape the original transformation log used, so the project has a file a
    # person opens and reads rather than a JSONL a person greps. None puts it beside the
    # history at `<root>/provenance/transformation_log.yml`.
    #
    # A VIEW, not the record. `runs.jsonl` remains the source of truth precisely because a
    # single YAML document is what failed before -- a corrupt line costs one line there and
    # everything after it here -- and this file can be regenerated at any time with
    # `python -m runprov log --format yaml`. Set `write_transformation_log=False` to skip
    # it; nothing else changes if you do.
    transformation_log: pathlib.Path | None = None
    write_transformation_log: bool = True
    # The readable twin of each JSON sidecar: `summary.prov.json` gets `summary.prov.yml`.
    # Both, because the JSON is what `verify` and other tools read and the YAML is what a
    # person opens beside an artifact. Written from the same record in the same call, so
    # they cannot drift. False writes only the JSON.
    write_yaml_sidecar: bool = True
    # NOTICE READS THAT BYPASSED REGISTRATION, and say so at the end of the run. On by
    # default, because a record that looks complete while being incomplete is worse than an
    # obviously missing one, and because silence here is what let the predecessor's log be
    # trusted for four years. See `watch.py` for what it can and cannot see -- in short, it
    # sees a run that forgot to register a read, and it can never see a script that does not
    # import this package at all.
    #
    # Set False for a step that deliberately reads files it does not want recorded. The
    # finding lands in the record as `unregistered_reads` as well as on stderr, because a
    # warning is ephemeral and a field is checkable three years later.
    warn_unregistered_reads: bool = True
    # Where full environment snapshots go. None disables them; `environment.packages` in
    # each record still carries the tracked subset. Opt-in because a snapshot is only
    # worth writing where someone will look for it, and content-addressed so enabling it
    # costs one file per DISTINCT environment rather than one per run.
    env_snapshot_dir: pathlib.Path | None = None
    # Where captured terminal output goes, auto-named `<script>_<run_id>.log`. None
    # disables capture. Opt-in for the same reason snapshots are, and one directory rather
    # than a path per script because ~50 pipeline steps must not each need editing to adopt
    # it. A per-Run `terminal_log=` overrides this; that is a default and an override, not
    # two ways to do one thing.
    #
    # The run id BELONGS in this filename, unlike in `header()` where it was removed for
    # making every artifact differ on every run. A log is per-pass evidence and is never
    # compared across runs, so there is nothing for a stamp to destabilise — and two runs
    # of one script sharing a log file would silently overwrite the first one's evidence.
    terminal_log_dir: pathlib.Path | None = None
    # WHERE records go. None means "a JsonlSink at resolved_run_log()". Supplying one is
    # how a lab points many pipelines at a shared store without forking the package.
    sink: RecordSink | None = None
    code_paths: tuple[str, ...] = DEFAULT_CODE_PATHS
    tracked_packages: tuple[str, ...] = DEFAULT_TRACKED
    #: Write a sidecar per RUN instead of one that the next run overwrites.
    #:
    #: `provenance=OUT.with_name("summary.prov.json")` names ONE path, so the tenth run of
    #: a script leaves one sidecar and the nine before it are gone. The append-only history
    #: still holds every one of those runs -- nothing about the RECORD is lost -- but the
    #: file sitting beside the artifact describes only the last one, and a reader looking
    #: for "when did this column appear" is reading the wrong run's answer.
    #:
    #: With this on, the stamp of the run is inserted before the final suffix:
    #:
    #:     summary.prov.json  ->  summary.20260813T143012Z.5709a907.prov.json
    #:
    #: Sortable first, unique second: the time is what a person browses by, and the run_uid
    #: is what makes two runs inside the same second still two files.
    sidecar_per_run: bool = False
    #: Hash the project's OWN modules that the run actually imported.
    #:
    #: `git_commit` identifies the code only when the tree is clean, and during development
    #: it never is. `script_sha256` pins the entry point and nothing it calls -- so a run
    #: whose result changed because `src/utils/stats.py` changed recorded a commit, a clean
    #: entry script, and no trace of the file that did it.
    #:
    #: Scoped to modules resolving UNDER the project root, because third-party packages are
    #: already answered by `packages` and `env_snapshot_dir`, and hashing site-packages on
    #: every run would cost far more than it says.
    hash_imported_code: bool = True
    #: How many imported modules are hashed before the rest are counted instead. A record
    #: must not become the repository it describes; `code.imported.omitted` states the tail.
    imported_code_max: int = 200
    run_id: typing.Callable[[], str] = default_run_id
    generation: typing.Callable[[], str] = default_generation

    def resolved_run_log(self) -> pathlib.Path:
        return self.run_log or (self.root / "provenance" / "runs.jsonl")

    def resolved_transformation_log(self) -> pathlib.Path:
        return self.transformation_log or (
            self.resolved_run_log().parent / "transformation_log.yml"
        )

    def resolved_sink(self) -> RecordSink:
        """Where records go: the JSONL, and the YAML view beside it.

        A SUPPLIED `sink` IS THE WHOLE STORY. The extension point exists so a lab can send
        records to one shared database, and quietly writing a YAML file next to it would be
        this package deciding where someone else's records live.
        """
        if self.sink is not None:
            return self.sink
        history = JsonlSink(self.resolved_run_log())
        if not self.write_transformation_log:
            return history
        return TeeSink(history, YamlLogSink(self.resolved_transformation_log()))

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
    # BOTH, OR NEITHER SILENTLY. `configure(existing, write_yaml_sidecar=False)` used to
    # return `existing` unchanged and DISCARD every keyword without a word -- a silent
    # no-op, which is this package's characteristic defect and the one shape it must not
    # ship. Raising is right rather than merging: a caller who passes both has two different
    # ideas of the project in one call, and picking one for them would be a guess.
    if project is not None and kwargs:
        raise TypeError(
            f"configure() takes a Project OR its fields, not both; got a Project and "
            f"{sorted(kwargs)}. Use dataclasses.replace(project, ...) to adjust one, or "
            f"pass the fields alone."
        )
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
