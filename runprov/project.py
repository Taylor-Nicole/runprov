# Copyright (c) 2026 Hôpital Henri-Mondor and Taylor Thompson
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
import os
import pathlib
import subprocess
import typing

# Packages whose version is recorded with every run. The audit's list is an ML stack;
# yours will differ, which is why it is a field and not a constant.
DEFAULT_TRACKED = ("numpy", "pandas", "scipy", "sklearn")

# Paths whose modification means "the code that ran is not the committed code". Anything
# that changes behaviour belongs here — including declarative inputs like a rule registry
# or a config directory, which are code in every sense that matters to a result.
DEFAULT_CODE_PATHS = ("src", "scripts", "conf", "pyproject.toml", "Makefile")


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
    code_paths: tuple[str, ...] = DEFAULT_CODE_PATHS
    tracked_packages: tuple[str, ...] = DEFAULT_TRACKED
    run_id: typing.Callable[[], str] = default_run_id
    generation: typing.Callable[[], str] = default_generation

    def resolved_run_log(self) -> pathlib.Path:
        return self.run_log or (self.root / "provenance" / "runs.jsonl")


_ACTIVE: Project | None = None


def configure(
    project: Project | None = None,
    **kwargs: typing.Any,  # noqa: ANN401 - these are Project's own fields, typed there
) -> Project:
    """Install the project every later `Run()` uses. Call once, at import of your paths
    module — not inside each script, which is how three scripts end up disagreeing."""
    global _ACTIVE
    _ACTIVE = project if project is not None else Project(**kwargs)
    return _ACTIVE


def active() -> Project:
    global _ACTIVE
    if _ACTIVE is None:
        _ACTIVE = Project()
    return _ACTIVE
