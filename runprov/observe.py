# Copyright (c) 2026 Assistance Publique – Hôpitaux de Paris (AP-HP),
#                    Hôpital Henri-Mondor, and Taylor Thompson
# SPDX-License-Identifier: BSD-3-Clause
# Licensed under the BSD 3-Clause License — see LICENSE.
"""Automatic call observation through `sys.monitoring`. ADR-0010, stage two.

WHAT THIS IS NOT. It is not a replacement for `@run.step`. A declared step says *the author
said this call matters*; an observed one says only *the interpreter noticed this call*. The
two are kept structurally different in the record — a count, not a digest — so the difference
lives in the data rather than in a metadata field nobody reads twice.

WHY SCOPE AND NOT A CAP. Measured on 2026-09-14: reading 500 lines of TSV with the standard
library produces **1 505 Python calls, 1 504 of them inside `csv.py`** and one in the caller's
own code. A cap of a thousand would fill on `csv.py` internals and stop before recording a
single function the author wrote — a record that announces its truncation honestly and
contains the wrong thing. Filtering to code under the project root gives 4 calls, all theirs.

THE MONITORING INTERFACE IS AN ARGUMENT, NOT AN IMPORT, and that is deliberate. Reading
`sys.monitoring` at module scope makes every line here unreachable on 3.10 and 3.11 — the
L-104 problem in a new place, and it would have bought three coverage exemptions where a stub
buys none.
"""

from __future__ import annotations

__all__: list[str] = []

import pathlib
import sys
import typing

from .hashing import _value_digest

#: A code object whose qualified name contains this is compiler-generated: `<genexpr>`,
#: `<lambda>`, `<listcomp>`, `<module>`. DERIVED rather than listed, because a list of those
#: names is a list that falls out of date — `sum(x for x in rows)` compiles the generator
#: expression into its own code object, so the interpreter reports it as a call. It is
#: genuinely a call and it is not a step: it has no name the author wrote.
ANONYMOUS_MARK = "<"

#: Directories whose code is somebody else's, even when they sit under the project root. A
#: virtualenv inside the project is the ordinary layout, and its contents are dependencies.
VENDORED = ("site-packages", "dist-packages", ".venv", "node_modules")

#: Ceilings. `max_functions` bounds how many DISTINCT functions are tracked; `max_signatures`
#: bounds the distinct argument sets remembered per function. Both are bounded by VARIETY
#: rather than by volume, so a loop calling one function a million times costs one entry.
#: When either bites it is recorded — a truncated record that does not say so is the defect
#: the `observation` block exists to prevent.
MAX_FUNCTIONS = 200
MAX_SIGNATURES = 32

#: `sys.monitoring` has six tool slots. 0, 1 and 2 are reserved by convention for a debugger,
#: `coverage` and a profiler; this project runs `coverage` at 100 %, so taking a low id would
#: collide with its own gate. 3 is the first free one.
TOOL_ID = 3

MODES = ("off", "census", "arguments")


class Observer:
    """Counts calls into the project's own code, and in `arguments` mode digests what they saw.

    One instance per run. `start()` returns the mode that is ACTUALLY active, which is not
    always the one asked for: `sys.monitoring` can refuse a tool id, and a refusal that
    produced a silent empty record would be indistinguishable from a run where nothing was
    called.
    """

    def __init__(
        self,
        root: pathlib.Path,
        mode: str,
        monitoring: typing.Any,  # noqa: ANN401 - `sys.monitoring`, or a stub in tests
        *,
        tool_id: int = TOOL_ID,
        max_functions: int = MAX_FUNCTIONS,
        max_signatures: int = MAX_SIGNATURES,
        frames: typing.Callable[[int], typing.Any] | None = None,
    ) -> None:
        if mode not in MODES:
            raise ValueError(f"mode must be one of {MODES}, not {mode!r}")
        self.root = pathlib.Path(root).resolve()
        self.mode = mode
        self.active = "off"
        self.refusal: str | None = None
        self.records: dict[str, dict[str, typing.Any]] = {}
        self.truncated_functions = 0
        self._mon = monitoring
        self._tool = tool_id
        self._max_functions = max_functions
        self._max_signatures = max_signatures
        self._frames = frames if frames is not None else sys._getframe
        # DECIDED ONCE PER FILE, not once per call. The scope test resolves a path and asks
        # whether it is under the root; doing that on every call in a hot loop would make the
        # observer the slowest thing in the run.
        self._in_scope: dict[str, bool] = {}
        # This package's own frames are never the subject. In a user's project runprov lives
        # in the virtualenv and `VENDORED` covers it; in this repository it sits under the
        # root, so it is excluded by name as well.
        self._own = pathlib.Path(__file__).resolve().parent

    # ---------------------------------------------------------------- lifecycle
    def start(self) -> str:
        """Begin observing, and return the mode actually active. Never raises."""
        if self.mode == "off":
            return "off"
        if self._mon is None:
            self.refusal = "sys.monitoring is unavailable on this interpreter"
            return "off"
        try:
            self._mon.use_tool_id(self._tool, "runprov")
        except Exception as exc:  # a busy slot is a refusal, not a crash
            self.refusal = f"tool id {self._tool} is in use ({type(exc).__name__})"
            return "off"
        self._mon.register_callback(self._tool, self._mon.events.PY_START, self._on_call)
        self._mon.set_events(self._tool, self._mon.events.PY_START)
        self.active = self.mode
        return self.mode

    def stop(self) -> None:
        """Stop observing. Idempotent, and safe to call after a failed `start`."""
        if self.active == "off":
            return
        self.active = "off"
        self._mon.set_events(self._tool, 0)
        self._mon.free_tool_id(self._tool)

    # ---------------------------------------------------------------- the callback
    def _scope(self, filename: str) -> bool:
        cached = self._in_scope.get(filename)
        if cached is not None:
            return cached
        # A NAME IN ANGLE BRACKETS IS NOT A PATH. `<stdin>`, `<string>`, `<frozen posixpath>`
        # — and `pathlib.Path("<frozen posixpath>").resolve()` does NOT raise: it resolves
        # against the current directory and lands UNDER the project root, so every frozen
        # stdlib module passed the scope test. Measured before this line existed: a run that
        # called two of its own functions recorded 1 562 calls to `<frozen posixpath>:join`,
        # because resolving a path is itself posixpath work and the observer was watching
        # itself do it.
        if filename.startswith(ANONYMOUS_MARK):
            self._in_scope[filename] = False
            return False
        try:
            path = pathlib.Path(filename).resolve()
            # AND IT MUST EXIST. A module compiled from a string carries a plausible name
            # that no file backs; `is_file` is one stat per distinct filename, cached.
            ok = path.is_file() and path.is_relative_to(self.root)
            ok = ok and not path.is_relative_to(self._own)
            ok = ok and not any(part in VENDORED for part in path.parts)
        except (OSError, ValueError):
            ok = False
        self._in_scope[filename] = ok
        return ok

    def _on_call(self, code: typing.Any, offset: int) -> None:  # noqa: ANN401 - a code object
        if ANONYMOUS_MARK in code.co_qualname or not self._scope(code.co_filename):
            return
        key = f"{pathlib.Path(code.co_filename).name}:{code.co_qualname}"
        entry = self.records.get(key)
        if entry is None:
            if len(self.records) >= self._max_functions:
                self.truncated_functions += 1
                return
            entry = self.records[key] = {"calls": 0}
        entry["calls"] += 1
        if self.active == "arguments":
            # THE FRAME IS GRABBED HERE, at depth 1 from the callback, and passed down. Asking
            # for it inside the helper reads one level too shallow and returns `_on_call`'s
            # own frame — which has no arguments of the observed function in it, so every call
            # digested identically and three different inputs recorded as one signature.
            # Measured, not reasoned: that is exactly what the first version produced.
            self._remember_signature(entry, code, self._frames(1))

    def _remember_signature(
        self,
        entry: dict[str, typing.Any],
        code: typing.Any,  # noqa: ANN401 - a code object
        frame: typing.Any,  # noqa: ANN401 - the frame that just started
    ) -> None:
        """Digest the bound arguments of the frame that just started.

        `PY_START` reports that a call began and not what was passed, so the values come from
        the frame — which is the whole extra cost of `arguments` mode over `census`, and the
        reason it is a mode rather than the default.

        DISTINCT SIGNATURES, NOT CALLS. Four hundred calls with two distinct argument sets
        record two entries, so "did this function see the same inputs as last time?" is a
        comparison of two small sets and the record is bounded by variety.
        """
        # ASSERTED, NOT ASSUMED. If the frame is not the one whose code just started, the
        # values read from it belong to something else entirely, and recording them would be
        # worse than recording nothing: a confident digest of the wrong thing.
        started: typing.Any = getattr(frame, "f_code", None)
        if started is not code:
            return
        try:
            names = started.co_varnames[: started.co_argcount]
            values = [frame.f_locals.get(n) for n in names]
        except (ValueError, AttributeError):  # no readable locals on some implementations
            return
        signature = _value_digest([_value_digest(v) for v in values])
        signatures = entry.setdefault("signatures", {})
        if signature not in signatures and len(signatures) >= self._max_signatures:
            entry["signatures_truncated"] = entry.get("signatures_truncated", 0) + 1
            return
        signatures[signature] = signatures.get(signature, 0) + 1


def default_mode(monitoring: typing.Any) -> str:  # noqa: ANN401 - `sys.monitoring` or None
    """`census` where the interpreter can do it, `off` where it cannot. ADR-0010.

    Not a user preference with a default: the capability decides, and the record says which
    it was — that is the distinction between *not observed* and *could not observe*.
    """
    return "census" if monitoring is not None else "off"
