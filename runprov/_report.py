# Copyright (c) 2026 Assistance Publique – Hôpitaux de Paris (AP-HP),
#                    Hôpital Henri-Mondor, and Taylor Thompson
# Licensed under the CeCILL-B Free Software License Agreement — see LICENSE.
# https://cecill.info/
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


def diagnostic(*lines: str) -> None:
    """Something that bears on whether the record is TRUE. stderr, always, unsilenceable."""
    for line in lines:
        # flush because a warning is often the last thing emitted before the process dies,
        # and stderr redirected into a file by a job runner is not guaranteed to be
        # line-buffered on every platform this package claims to support.
        print(line, file=sys.stderr, flush=True)


def summary(*lines: str) -> None:
    """Routine confirmation that the run was recorded. stderr, and `RUNPROV_QUIET` hides it."""
    if quiet():
        return
    diagnostic(*lines)
