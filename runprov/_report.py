# Copyright (c) 2026 Assistance Publique – Hôpitaux de Paris (AP-HP),
#                    Hôpital Henri-Mondor, and Taylor Nicole Thompson
# SPDX-License-Identifier: BSD-3-Clause
# Licensed under the BSD 3-Clause License — see LICENSE.
"""Where runprov's OWN messages go. Never the caller's stdout.

The defect this exists to close
-------------------------------
The library half called bare `print()`, which is stdout. stdout is the caller's data
channel:

    $ python step.py > result.tsv
    $ head -2 result.tsv
    provenance -> prov.json
      code None  inputs 0  outputs 0  seeds []

A provenance tool that corrupts the artifact it is recording is worse than no provenance
tool. `__main__.py` already had this right — its rendered log goes to stdout because the
log IS its output, and its counts and errors go to stderr. The library has no output;
everything it says is about the run, so everything it says is stderr.

stderr rather than `logging`
----------------------------
`logging` is stdlib, so it does not breach the zero-dependency rule — it fails on a
different axis. An unconfigured logger routes WARNING and above to stderr through the
last-resort handler, but **drops INFO entirely**, so the `provenance -> path` confirmation
would vanish for every caller who has not called `basicConfig` — that is, all of them
until each of ~50 pipeline steps is edited. Handing a library a global, caller-configured,
order-dependent singleton to achieve "write to fd 2" buys nothing: `print(..., file=
sys.stderr)` is the whole requirement, needs no configuration to be correct, and cannot be
silenced by somebody else's `basicConfig(level=ERROR)`.

Two channels, and the split is a rule rather than a taste
----------------------------------------------------------
**A message is a DIAGNOSTIC if it reports something that could make the record wrong,
incomplete or misattributed.** Those go to stderr unconditionally and cannot be turned
off — a provenance tool that can be told to stop warning is exactly the tool that gets
told to stop warning. Everything else is CONFIRMATION of normal operation: useful, routine,
and repeated once per step, so a 30-stage chain may quieten it with `RUNPROV_QUIET=1`.

The quiet switch deliberately reaches only `summary()`. `RUNPROV_QUIET=1` cannot suppress
a dirty-tree warning, a failed sidecar write, a lost history line or a `RUN FAILED`, and
`test_quiet_cannot_silence_a_warning` holds that line.
"""

from __future__ import annotations

import os
import sys
import threading
import typing

#: Set to any value other than these to silence the per-run confirmation. An env var and
#: not a `Project` field: the noise belongs to the INVOCATION (a chain, a cron job, a CI
#: run), not to the code, so the person who wants it quiet is the one running the pipeline
#: rather than the one who edits `configure()`. It also has to reach `sinks._exclusive`,
#: which has no `Project` in scope and never will.
QUIET_ENV = "RUNPROV_QUIET"

# "" covers unset. The rest are the spellings people actually type when they mean "no",
# and reading `RUNPROV_QUIET=0` as *quiet* is the sort of surprise that gets a switch
# blamed for a missing message.
_FALSEY = frozenset({"", "0", "false", "no", "off"})


def quiet() -> bool:
    """Whether routine confirmation is suppressed. Read per call — never cached, so a
    test (or a caller) can change the variable and have it take effect."""
    return os.environ.get(QUIET_ENV, "").strip().lower() not in _FALSEY


#: ONE LOCK FOR STDERR, because the heartbeat thread and the main thread both write here and
#: two interleaved lines are one garbled line — in the module whose job is to degrade well.
#: Re-created after a fork: threads do not survive `os.fork()`, so a child inheriting a lock
#: held by a thread that no longer exists would deadlock on its first message.
_WRITE_LOCK = threading.Lock()


def _reset_lock_after_fork() -> None:  # pragma: no cover - only runs in a forked child
    global _WRITE_LOCK
    _WRITE_LOCK = threading.Lock()


if hasattr(os, "register_at_fork"):  # pragma: no branch - present on every POSIX build
    os.register_at_fork(after_in_child=_reset_lock_after_fork)


def printable(text: str) -> str:
    """One field's worth of text, safe to interpolate into a line a person reads.

    LIFTED OUT OF `__main__._render_unreadable` [K-21], WHOSE OWN ARGUMENT IS THE REASON:
    *printing that raw hands the terminal whatever corrupted the file*. The same class is
    reachable from a RECORD and from a POLICY rather than from a torn line — a declared input
    path may contain a newline (legal on POSIX and stored verbatim), and a policy's `why` may be
    a paragraph of rationale, which is the most natural thing a laboratory writes. A newline
    inside one interpolated field forges WHOLE LINES on the page, including a line reading
    exactly `GATE: MET (exit 0)` inside a VIOLATED report, and a CI log scraped for `GATE:` reads
    the forged one first. **So this is not an adversarial case:** an innocent multi-line `why`
    garbles the page identically.

    IT LIVES HERE AND NOT IN `__main__` BECAUSE OF THE IMPORT DIRECTION. `policy.py` must not
    import the command module — `PolicyError`'s own docstring forbids it in those words, because
    a policy parser that dragged `__main__` in would make `import runprov.policy` build an
    argument parser — so a transform both of them need belongs in a module both may import.

    AND IT MOVED HERE FROM `terminal.py` FOR THE SAME REASON, ONE LAYER DOWN [Audit M].
    `_write` below is the package's only stderr emission point and it needs this transform;
    `terminal` imports `_report`, so `_report` importing `terminal` is a circular import that
    stops the package coming up — measured, not reasoned. This module imports nothing from the
    package, so it is where a transform everything needs can live. The nine modules that use
    it import it from here; `terminal.py` carries the comment saying why it is not there.

    THE JSON SIDE IS LEFT ALONE, deliberately: `json.dumps` already escapes, and a payload
    carrying pre-escaped text would hand a consumer a string that is not the one in the record.
    One structure, two renderings — and escaping is the renderer's job.

    A SPACE SURVIVES AND NOTHING ELSE NON-PRINTABLE DOES. `str.isprintable()` is False for a
    space, so a page whose columns were escaped would be unreadable; every other character that
    is not printable — a newline, a tab, a terminal escape sequence, a stray control byte —
    becomes its `unicode_escape` form. Accented letters, CJK and the em dash are printable and
    pass through unchanged, which matters for a `why` a French laboratory writes.
    """
    return "".join(
        c if c.isprintable() or c == " " else c.encode("unicode_escape").decode() for c in text
    )


def printable_lines(lines: typing.Iterable[str]) -> list[str]:
    """Every line a renderer is about to emit, escaped. `printable`, applied once per line.

    THE SHAPE L-03 EARNED. `printable` is a FIELD transform, and escaping fields one at a time
    is a list of call sites — the thing this repository has had to widen nine times, and the
    thing that left five renderers forgeable after K-21 escaped two. A renderer has exactly one
    place where its lines become output; applying the transform there covers every field it
    interpolates, including the ones nobody has written yet.

    PER LINE AND NOT OVER THE JOINED PAGE, which is the one way to get this wrong: `printable`
    of a whole page escapes the newlines the page is MADE of, and the result is one very long
    line. The separator is the renderer's, and only what sits between separators came from
    outside.
    """
    return [printable(line) for line in lines]


def _write(line: str) -> None:
    """Write one line to stderr, and NEVER raise while doing it.

    These messages contain em dashes, and stderr on Windows is encoded with the console
    code page rather than UTF-8. Measured: the same diagnostic encodes fine under utf-8
    and cp1252, and raises `UnicodeEncodeError` under cp932 and ascii -- so on a Japanese
    or a stripped-down console, a run would die INSIDE provenance capture, at the moment
    it was trying to report something. Windows CI caught it as a mojibake byte (0x97, the
    cp1252 em dash) before it caught it as a crash.

    A provenance module that kills the run it is describing is the failure this whole
    package exists to prevent, so the message degrades instead: unencodable characters
    become the target encoding's replacement and the warning still arrives.

    THE PACKAGE'S ONE STDERR EMISSION POINT, AND THEREFORE WHERE IT IS ESCAPED
    [Audit M, escape-4]. All 27 `diagnostic()` calls in `run.py`, all 3 in `sinks.py` and
    every `summary()` line arrive here, and each of them interpolates values from outside
    this source file: paths a run opened, a policy's `why`, an exception's own words. A
    `\r` or an `\x1b[2K` in one of those moves or erases the line already printed.

    **PER LINE, AND THE NEWLINE IS DELIBERATELY NOT TOUCHED, which is the whole of what this
    does and does not buy.** `printable(message)` over the whole string was measured and is
    REFUTED: a four-line `AUTO-DETECTED project:` note came out as ONE line of `\n`
    literals, and at least eight `diagnostic()` callers pass embedded newlines on purpose
    (`run.py` at 622, 2187, 2264, 2492, 3245, 3302, 3442). `printable_lines`'s own docstring
    names that as *the one way to get this wrong*. So the message is split on its own
    separator and each line escaped.

    **WHICH MEANS A NEWLINE FORGERY IS NOT CLOSED HERE AND CANNOT BE.** A `\n` inside an
    interpolated field is indistinguishable, at this point, from a `\n` the caller wrote for
    layout: both are just the separator by the time the message is one string. Closing that
    half needs the transform where the FIELD still exists — which is why `run.py`'s
    `UNREGISTERED READ` escapes `shown` at the site, and why the in-flight banner in
    `__main__` escapes its list of lines rather than the joined page.
    """
    line = "\n".join(printable_lines(line.split("\n")))
    stream = sys.stderr
    # NO STDERR IS NOT A REASON TO USE STDOUT. `print(..., file=None)` falls back to stdout
    # by CPython's own rule, and `sys.stderr is None` is the documented state under
    # `pythonw.exe`, embedded interpreters and windowed frozen builds. Measured: with
    # `sys.stderr = None`, a run put 725 bytes of provenance chatter into the caller's data
    # channel and 0 into stderr -- the exact defect the docstring above opens with, arriving
    # through the fallback rather than through a bare `print`. Silence is the correct
    # degradation: the record is still written to disk, and the only thing lost is a message.
    if stream is None or not hasattr(stream, "write"):
        return
    try:
        with _WRITE_LOCK:
            print(line, file=stream, flush=True)
    except UnicodeEncodeError:
        try:
            enc = getattr(stream, "encoding", None) or "ascii"
            stream.write(line.encode(enc, "replace").decode(enc, "replace") + "\n")
            stream.flush()
        except Exception as exc:  # guards-ok: see below -- the fallback can fail the same way
            del exc
    except Exception as exc:  # guards-ok: and this is the one that mattered.
        # ONLY `UnicodeEncodeError` WAS CAUGHT, so an stderr that cannot be WRITTEN TO killed
        # the run. Measured: `python step.py 2>/dev/full` (ENOSPC on every write) exited 120
        # with no artifact, no sidecar and no history line -- the reporting mechanism
        # destroying the run it was describing, which is the failure this docstring names.
        # A full disk, a closed pipe (EPIPE, `... | head`) and a detached console all reach
        # it. A message is worth less than the run it is about, so the message is dropped.
        del exc


def diagnostic(*lines: str) -> None:
    """Something that bears on whether the record is TRUE. stderr, always, unsilenceable."""
    for line in lines:
        # flush because a warning is often the last thing emitted before the process dies,
        # and stderr redirected into a file by a job runner is not guaranteed to be
        # line-buffered on every platform this package claims to support.
        _write(line)


#: How many progress lines one run may print before it stops narrating. A run registering
#: four thousand inputs would otherwise scroll the terminal it is trying to reassure. When it
#: bites it SAYS SO, once — the same rule the record follows for a truncated observation.
PROGRESS_MAX_LINES = 200


def progress_enabled(setting: str | None) -> bool:
    """Whether to narrate. `None` means "decide from the capability", which is a terminal.

    THE DEFAULT IS A TTY TEST, not a preference. Piped into a file, read by a job runner, or
    running in CI, there is nobody watching and the lines are noise in somebody's log — the
    same reasoning as `auto_available` in ADR-0010: what is possible decides, and it is
    stated rather than assumed. `RUNPROV_QUIET` still wins over everything.
    """
    if quiet():
        return False
    if setting is not None:
        return setting == "on"
    stream = sys.stderr
    try:
        return bool(stream is not None and stream.isatty())
    except Exception:  # a stream that cannot answer is not a terminal
        return False


def progress(line: str, *, state: dict[str, int]) -> None:
    """One narration line, capped, counting its own suppressions.

    `state` carries the count for one run; the cap is per run rather than per process, so a
    long session of short runs is not silenced by its own history.
    """
    shown = state.get("shown", 0)
    if shown >= PROGRESS_MAX_LINES:
        if shown == PROGRESS_MAX_LINES:
            state["shown"] = shown + 1
            _write(f"  … further progress lines suppressed after {PROGRESS_MAX_LINES}")
        return
    state["shown"] = shown + 1
    _write(line)


def summary(*lines: str) -> None:
    """Routine confirmation that the run was recorded. stderr, and `RUNPROV_QUIET` hides it."""
    if quiet():
        return
    diagnostic(*lines)
