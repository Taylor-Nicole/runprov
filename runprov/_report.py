# Copyright (c) 2026 Assistance Publique – Hôpitaux de Paris (AP-HP),
#                    Hôpital Henri-Mondor, and Taylor Thompson
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
    """
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


def summary(*lines: str) -> None:
    """Routine confirmation that the run was recorded. stderr, and `RUNPROV_QUIET` hides it."""
    if quiet():
        return
    diagnostic(*lines)
