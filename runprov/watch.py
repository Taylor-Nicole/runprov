# Copyright (c) 2026 Assistance Publique – Hôpitaux de Paris (AP-HP),
#                    Hôpital Henri-Mondor, and Taylor Thompson
# Licensed under the BSD 3-Clause License — see LICENSE.
"""Notice the reads that bypassed registration, and say so.

THE GAP THIS CLOSES. `run.input(p)` makes registration the ordinary way to open a file, but
it cannot make it the ONLY way: a plain `open(p)` still works, the read is absent from the
record, and until now **nothing said so**. The record then looks complete while being
incomplete, which is worse than an obviously missing one because it invites trust it has not
earned. That is the same shape as the defect this package replaced — `append_log(entry)`
recorded what its author BELIEVED a step read.

WHAT IT CAN AND CANNOT SEE, stated plainly because the difference matters:

* A script that creates a `Run` and then opens files without registering them — **seen**.
* A script that never imports `runprov` at all — **not seen, ever.** Code that is not
  imported does not run. Only a static check over the source, or wrapping the command with
  `runprov exec`, can reach that case, and this module does not pretend otherwise.

HOW. `sys.addaudithook` (CPython 3.8+) fires an `open` event for every file the process
opens, including from C extensions like pandas and h5py, which is the reason for choosing it
over patching `builtins.open`. A hook cannot be removed once installed — the interpreter
offers no API for it — so exactly ONE is installed per process, at the first `Run` that asks
for it, and it is inert whenever no run is active.

WHY THE FILTER IS AGGRESSIVE. A run legitimately opens hundreds of files: the interpreter's
own imports, locale data, fonts, certificates, the package's own records. Reporting those
would produce a warning nobody reads, and a warning nobody reads is the failure mode the
deterministic-pin decision exists to prevent — a permanently red check teaches its audience
to ignore it. So only files that are plausibly *data* are reported: inside the project root,
existing, regular, not the provenance directory's own output, not Python source, and not
already registered.
"""

from __future__ import annotations

# A MODULE'S `__all__` RATIFIES THE PACKAGE'S PROMISE; IT NEVER MAKES ONE.
# A name belongs here if and only if `runprov/__init__.py` re-exports it and lists it in the
# package `__all__` (decided 2026-08-19, ledger L-24; the list, and its count, live
# there and nowhere else). Nothing else qualifies:
# cross-module use inside `runprov/` is INTERNAL and `__all__` neither describes nor protects
# it; tests reach into internals on purpose and prove nothing; and prose that documents a
# printed string, a CLI flag or a record key is not an instruction to call a name.
# Adding or withdrawing a promise is a package-level decision taken in `__init__.py`.
# Nothing here is promised. The feature is reached through `Run` — `warn_unregistered_reads`
# on `Project`, and the `unregistered_reads` record key — never by calling into this module.
# It is one week old, and freezing a surface on it now would freeze a first draft.
__all__: list[str] = []

import os
import pathlib
import sys
import typing

#: Suffixes that are CODE or bookkeeping rather than data. Reading these is not a provenance
#: omission: `.py` is covered by the code section, and the rest are the interpreter's own.
_NOT_DATA = frozenset(
    {".py", ".pyc", ".pyi", ".pyd", ".so", ".dylib", ".dll", ".pth", ".egg-link", ".dist-info"}
)

#: Directory names whose contents are never the user's data.
_NOT_DATA_DIRS = frozenset(
    {
        "__pycache__",
        "site-packages",
        "dist-packages",
        ".git",
        ".venv",
        "venv",
        ".tox",
        ".mypy_cache",
    }
)


#: How many distinct paths one run will remember. A BOUND ON MEMORY, not on usefulness: a run
#: that opens more than this many distinct files has a provenance problem the first few hundred
#: names already describe. Measured 2026-08-19: the exit-time filter costs ~50 us per distinct
#: path (one `resolve()` and one `stat()`), so 400 unregistered files add ~21 ms at exit and a
#: realistic run of three adds 0.4 ms. The cap keeps the pathological case bounded rather than
#: letting a run that opens a million files accumulate a million strings.
WATCH_MAX_PATHS = 2000


class _Watcher:
    """The single process-wide hook, and the set of runs currently listening.

    One instance exists per process because `sys.addaudithook` is irreversible. Nested or
    sequential runs attach and detach from it; when none are attached it does nothing beyond
    an `is` check per open, which is the cheapest a hook can be.
    """

    def __init__(self) -> None:
        self._active: list[typing.Any] = []  # a Run; typing it would be a cycle
        self._installed = False

    def install(self) -> None:
        if self._installed:
            return
        # Installed ONCE and never removed, so this must stay cheap and must never raise:
        # an exception in an audit hook propagates into whatever the caller was doing, which
        # would mean this feature could break a run it exists to describe.
        sys.addaudithook(self._hook)
        self._installed = True

    def attach(self, run: object) -> None:
        self.install()
        self._active.append(run)

    def detach(self, run: object) -> None:
        try:
            self._active.remove(run)
        except ValueError:  # pragma: no cover - detach is only ever called after attach
            pass

    def _hook(self, event: str, args: tuple[typing.Any, ...]) -> None:
        if event != "open" or not self._active:
            return
        try:
            path = args[0]
            if not isinstance(path, (str, bytes, os.PathLike)):
                return  # a file DESCRIPTOR, not a name; nothing to attribute
            for run in self._active:
                if len(run._opened) < WATCH_MAX_PATHS:
                    run._opened.add(os.fsdecode(path))
        except Exception:  # pragma: no cover - defensive; see install()
            return  # never let bookkeeping break the caller


_WATCHER = _Watcher()


def attach(run: object) -> None:
    """Start noticing opens on behalf of `run`."""
    _WATCHER.attach(run)


def detach(run: object) -> None:
    """Stop noticing. Safe to call more than once."""
    _WATCHER.detach(run)


#: Every path this PACKAGE has written in this process — sidecars, YAML twins, the history,
#: the transformation log, in-flight markers.
#:
#: PROCESS-WIDE, BECAUSE THE OBSERVATION IS. `_Watcher._hook` attributes every `open` in the
#: process to EVERY attached `Run`, and the "these files are ours" subtraction used to be
#: assembled per-instance from `self.provenance_path`, `self._written_paths` and the project
#: logs. Process-wide observation, per-object subtraction — so two overlapping runs each
#: reported the OTHER's sidecar. Measured on the nested shape the package documents and
#: tests: the outer run's record, and its history line, carried
#: `unregistered_reads: ['i.prov.json', 'i.prov.yml']` — the inner run's own sidecar and
#: twin, files runprov itself wrote — and stderr told the author to `run.input()` them.
#:
#: That is L-108 across two runs: the package reporting its own file as the user's oversight.
#: L-108 was fixed by ORDERING, which protects a run only from ITSELF.
#:
#: A SET OF FILES, NEVER OF DIRECTORIES. `run_log=` and `provenance=` frequently point at the
#: project root, and excluding a parent directory would silently disable the whole check —
#: both forms of that bug have been written and caught in this file already.
#:
#: Unbounded, and bounded in practice by the number of records a process writes; the entries
#: are short strings and it does not grow with the number of READS, which is the thing that
#: grows without limit here.
_OURS: set[str] = set()


def own(*paths: str | pathlib.Path | None) -> None:
    """Record that this package wrote `paths`, so no run reports them as a missed read.

    Called as soon as each path is KNOWN rather than at exit, because a concurrent run needs
    the answer while it is still open — an exit-time list only ever helped the run exiting.
    """
    for path in paths:
        if path is None:
            continue
        try:
            _OURS.add(str(pathlib.Path(path).resolve()))
        except (OSError, RuntimeError):  # guards-ok: see `unregistered`
            _OURS.add(str(path))


def unregistered(
    opened: typing.Iterable[str],
    registered: typing.Iterable[str],
    root: pathlib.Path,
    exclude: typing.Iterable[pathlib.Path] = (),
) -> list[str]:
    """Which of `opened` look like unregistered data reads, as paths relative to `root`.

    Pure and total: it takes strings and returns strings, so the filter can be tested
    directly rather than through a run, and no failure here can affect a caller.
    """
    # `RuntimeError` BESIDE `OSError`, AT EVERY `resolve()` IN THIS FUNCTION. `Path.resolve()`
    # raises `RuntimeError("Symlink loop from ...")` on CPython 3.10-3.12, and it is NOT an
    # OSError -- so one looping path among the hundreds a run opens escaped this function,
    # whose own docstring promises it is "pure and total ... no failure here can affect a
    # caller". It landed in `run.py`'s catch-all and abandoned the ENTIRE unregistered-read
    # check for that run.
    #
    # Measured, with a control: same script, one genuinely unregistered `data/secret.tsv`.
    # Without a loop -> reported, `unregistered_reads: ['data/secret.tsv']`. With one ->
    # `WARNING: could not check for unregistered reads` and `unregistered_reads` ABSENT from
    # the record -- which is byte-identical to what a clean run writes. A run that missed a
    # read became indistinguishable from one that missed nothing.
    #
    # Fifth instance of this exception family (see L-98); this module was added after that
    # sweep and inherited none of it.
    reg = set()
    for r in registered:
        try:
            reg.add(str(pathlib.Path(r).resolve()))
        except (OSError, RuntimeError):  # pragma: no cover - a path we cannot resolve
            reg.add(str(r))
    # THE COMPREHENSION WAS THE WORST OF THE FOUR, because it was not guarded at all. An
    # unresolvable exclusion is one we cannot match against; dropping it costs at most a
    # false positive on the package's own file, and raising costs the whole check.
    skip = []
    for e in exclude:
        try:
            skip.append(pathlib.Path(e).resolve())
        except (OSError, RuntimeError):
            continue
    # RESOLVED, because every path below is resolved before comparison and `relative_to` is
    # literal: an unresolved root containing a symlink (pytest's tmp_path on some systems,
    # `/tmp` on macOS) matches nothing and silently reports no findings at all.
    try:
        root = pathlib.Path(root).resolve()
    except (OSError, RuntimeError):  # pragma: no cover - a root we cannot resolve
        root = pathlib.Path(root)

    out: set[str] = set()
    for name in opened:
        try:
            p = pathlib.Path(name)
            if p.suffix in _NOT_DATA:
                continue
            rp = p.resolve()
            if str(rp) in reg or str(rp) in _OURS:
                continue
            if not rp.is_file():
                continue
            if _NOT_DATA_DIRS & set(rp.parts):
                continue
            if any(rp == s or s in rp.parents for s in skip):
                continue
            rel = rp.relative_to(root)  # raises if outside the project
        except (OSError, ValueError, RuntimeError):
            # outside the root, unresolvable, a symlink loop, or gone by now: not our
            # business, and never a reason to abandon the other paths in this run.
            continue
        out.add(str(rel))
    return sorted(out)
