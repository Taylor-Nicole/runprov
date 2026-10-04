# Copyright (c) 2026 Assistance Publique – Hôpitaux de Paris (AP-HP),
#                    Hôpital Henri-Mondor, and Taylor Nicole Thompson
# SPDX-License-Identifier: BSD-3-Clause
# Licensed under the BSD 3-Clause License — see LICENSE.
"""`python -m runprov log` — read the continuous history back.

The history is ONE file, created once and appended to forever: every run this project has
ever recorded, in order, one JSON object per line. That is the same thing the predecessor's
`transformation_log.yml` was for, and the reason it is JSONL rather than YAML is that the
predecessor's file **stopped being readable**. Its writer appended `---` documents into a
file that began as a list. Measured on the real 24,300-line file: `safe_load` dies at line
14,547 on those `---` documents, and `safe_load_all` dies at 14,554 on something else
entirely -- an unquoted `Note:` inside a hand-written description. Nine repair scripts
exist to heal it, one of which is itself a step in the pipeline it documents.

JSONL cannot fail that way. Each line stands alone: a corrupt line costs one record, never
the file, and a reader can always skip it and say so.

Because a log nobody reads is a log nobody checks, this renders it back:

    python -m runprov log                       the timeline, newest last
    python -m runprov log --format yaml         the transformation-log shape
    python -m runprov log --failed              only the runs that died
    python -m runprov log --script build_labels --limit 5

And `verify`, which is the other half of the claim: `log` and `lineage` say what happened,
and neither says whether what happened is still true.

    python -m runprov verify                    do artifacts still match what they pin?
    python -m runprov verify results/ --root .

It reads the artifact and nothing else -- no history, no sidecar, no `configure()`, which
is the whole reason the pin is written into the bytes. See `verify.py`.
Its `--format json` answer is ADR-0017, T-33: one builder, two renderings.

`gate` IS ADR-0018, and it is the one subcommand whose answer is a POLICY rather than a
property of the records: it reads a file of rules the project wrote for itself and reports,
per rule, met / violated / could not be checked. Cited here because this module implements
the command, and `test_every_adr_is_listed_in_the_adr_index` reads this docstring to decide
which decisions are built.
"""

from __future__ import annotations

# THE ONE NAME THIS MODULE PROMISES, and not because the package re-exports it — it does not.
# `pyproject.toml`'s `[project.scripts]` names `runprov.__main__:main`, so an INSTALLED
# artefact outside this source tree depends on that symbol. That is the only other thing
# that can make a name public here, and it makes exactly this one. Everything else in this
# file is CLI plumbing; `_load`, `_show`, `_verify` and the rest carry underscores and the
# tests that reach for them are reaching into internals on purpose.
__all__ = ["main"]

import argparse
import collections
import contextlib
import json
import os
import pathlib
import re
import runpy
import shlex
import signal
import subprocess
import sys
import typing

from . import chain as chain_mod
from . import check as check_mod
from . import diff as diff_mod
from . import hashing, policy
from . import impact as impact_mod
from . import prune as prune_mod
from . import report as report_mod
from . import resources as resources_mod
from . import show as show_mod
from . import verify as verify_mod
from ._atomic import TEMP_SUFFIX, atomic_write_text
from .export import FORMATS as EXPORT_FORMATS
from .export import default_filename, render
from .hashing import PIN_DIGEST_CHARS
from .project import Project, active
from .run import START_SCHEMA, Run, Terminated
from .show import (
    MODIFIED,
    _yaml_entry,
    _yaml_header,
    in_flight,
    project_view,
    render_project_lines,
    render_run,
    run_view,
    select,
    staleness,
)
from .show import render_yaml as _yaml_doc
from .terminal import printable, printable_lines
from .verify import FAILING, GONE, OK, STALE, render_report, verify
from .watch import unregistered

#: WHAT AN EXIT CODE MEANS, in every subcommand (ledger L-81, decided 2026-09-01).
#:
#:   0  checked, and nothing is wrong
#:   1  checked, and something IS wrong — a stale or gone artifact, a named target or filter
#:      that matched nothing
#:   2  COULD NOT CHECK, or the invocation did not describe a check — no history file, no
#:      pins found, a usage mistake
#:
#: The 1/2 split is the one that had been missing, and it is the one automation needs: a CI
#: job could not tell "your artifacts are stale" from "I could not look", and both a green
#: gate over nothing and a red gate over nothing are worse than no gate. It is grep's and
#: diff's convention, so it costs a reader nothing to learn.
#:
#: `--failed` is deliberately NOT in the 1 family when it matches nothing: it selects a CLASS
#: rather than naming a thing, and "no failed runs" is the good answer, not an absent one.
#: `--script X` and `--run-id X` NAME something, so matching nothing means the name was wrong.
CANNOT_CHECK = 2


def _stream(path: pathlib.Path) -> typing.Iterator[dict[str, typing.Any] | None]:
    """Every line of the history, parsed, one at a time. `None` marks a line that would not.

    STREAMED, because the history is appended forever and this is the one place that reads
    all of it. The first version did `read_text().splitlines()`, which holds the whole file
    as one string AND a list of every line before a single record is parsed -- two full
    copies of a file whose entire design is that it never stops growing. Measured on a
    realistic 100,000-run history of 91 MB: 488 MB of peak memory to read it.

    That is the same defect `content_digest` had and for the same reason: a convenient
    whole-file read, in the function that meets the biggest file.

    THE BOUND IS THE LARGEST RECORD, NOT A CONSTANT, and saying so is the point: "one at a
    time" is true and is not the same as bounded. Measured at 2.01x the largest record, at
    both 5 MB and 20 MB, so a three-run history holding one enormous note costs twice that
    note however few runs it has.

    `run.py` caps most of what it writes -- `OTHER_FILES_KEPT` at 50, `imported_code_max` at
    200 -- but `notes` and `parameters` are uncapped caller data: `run.note("scores", <a
    value per row>)` on a million-row frame is one line. One such record then makes every
    read of the history cost twice that record, forever, in a file designed never to be
    trimmed. Whether to cap what a note may serialise is a design decision about someone
    else's data and is not made here; the cost of not capping is stated instead.
    """
    with path.open(encoding="utf-8", errors="replace") as fh:
        for line in fh:
            if not line.strip():
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                yield None
                continue
            # VALID JSON IS NOT A RECORD. `123`, `[1, 2]` and `"text"` all parse, and every
            # caller here then does `rec.get(...)` — so `log`, `show` and `lineage` died with
            # `AttributeError: 'int' object has no attribute 'get'`, in the reader whose whole
            # design property is that JSONL "loses the bad line and counts it". Measured:
            # exit 1 and a traceback from all three, over one line in an otherwise healthy
            # history.
            #
            # `null` was worse than a crash, because it was silent: it parses to `None`, which
            # is this function's sentinel for "would not parse", so the three readers counted
            # it while `log --unreadable` reported "every line parses" — two commands
            # contradicting each other about one file, which is the shape of L-99.
            #
            # A torn append cannot produce any of these: a prefix of a JSON object is not
            # valid JSON. They come from a hand-edit, a concatenation, or a writer that is not
            # runprov — all of which this reader is expected to survive rather than die on.
            yield rec if isinstance(rec, dict) else None


#: How much of an unreadable line to print. A history line is uncapped caller data — see
#: `_stream` — so a corrupted one can be megabytes, and triage needs enough to recognise the
#: line, never the whole of it. What was cut is stated rather than silently dropped.
UNREADABLE_SHOWN = 200


def _unreadable(path: pathlib.Path) -> typing.Iterator[tuple[int, str]]:
    """`(line number, raw text)` for every line of the history that will not parse.

    SEPARATE FROM `_stream` ON PURPOSE, rather than widening what it yields. `_stream` has
    five callers and a measured memory story — 2.01x the largest record, stated in its own
    docstring — and carrying the raw text alongside every parsed record would make every
    reader hold both. This holds one bad line at a time and nothing else, and it is only
    ever run by the command that asks for it.

    Two passes over the file would be needed to show records AND bad lines together, which
    is why `--unreadable` prints only the bad ones: it is a triage mode, not a view.
    """
    with path.open(encoding="utf-8", errors="replace") as fh:
        for n, line in enumerate(fh, 1):
            if not line.strip():
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                yield n, line.rstrip("\n")
                continue
            # THE SAME RULE AS `_stream`, so the count and the listing agree BY CONSTRUCTION
            # rather than by coincidence. This function re-parses independently — that is what
            # keeps it cheap — and independence is exactly how the two drifted apart: a `null`
            # line was counted by every reader and listed by none.
            if not isinstance(rec, dict):
                yield n, line.rstrip("\n")


def _render_unreadable(number: int, raw: str) -> str:
    """One line of triage output: `<lineno>: <the raw text, escaped and bounded>`.

    ESCAPED, because the reason a line will not parse is often that something wrote bytes
    into it — a half-written record from an interrupted append, a stray control character, a
    terminal escape sequence. Printing that raw hands the terminal whatever corrupted the
    file, which is a poor way to inspect corruption. `errors="replace"` upstream has already
    dealt with invalid UTF-8; this deals with what survives it.

    THE TRANSFORM ITSELF IS `terminal.printable` NOW [K-21], because the same class is reachable
    from a record and from a policy file and `policy.py` may not import this module. What stays
    here is the part that is local to triage output: the bound and the *N more character(s)*
    tail.
    """
    shown = raw[:UNREADABLE_SHOWN]
    body = printable(shown)
    cut = len(raw) - len(shown)
    return f"{number}: {body}" + (f"  … {cut} more character(s)" if cut else "")


def _is_start(rec: dict[str, typing.Any]) -> bool:
    """Is this the line appended when a run BEGAN, rather than a record of one that ran?

    FILTERED OUT OF EVERY ORDINARY READER, in `_load` and `_counted`, which is the two
    places records enter them. A `runprov.start.v1` line is not a run that happened — it is
    a run that began, and the record of what happened arrives later under the same
    `run_uid`. Counting both would report every completed run twice: `show` would double its
    run count, `log` would print an empty entry before each real one, and `lineage` would
    walk a record with no inputs and no outputs.

    The line is not noise, though, and `_InFlightScan` is where it is read: a start whose
    uid never gets a record IS the finding, and it is the only permanent evidence that a
    SIGKILLed run ever existed.
    """
    return bool(rec.get("schema") == START_SCHEMA)


def _load(path: pathlib.Path) -> tuple[list[dict[str, typing.Any]], int]:
    """Returns (records, unreadable_line_count). A bad line is COUNTED, never dropped.

    Materialises what `_stream` yields, for the callers that genuinely need every record at
    once. `log --limit N` does not, and does not use this.
    """
    rows, bad = [], 0
    for rec in _stream(path):
        if rec is None:
            bad += 1
        elif not _is_start(rec):
            rows.append(rec)
    return rows, bad


def _timeline(rows: typing.Iterable[dict[str, typing.Any]]) -> str:
    return "".join(_timeline_entry(r, separator=i > 0) for i, r in enumerate(rows))


def _timeline_entry(r: dict[str, typing.Any], *, separator: bool = True) -> str:
    """ONE run, rendered alone. Split out of `_timeline` so `log` can write each record
    as it streams past instead of building every line before printing any of them.

    `separator` LEADS the entry, for the reason spelled out in `show._yaml_entry`: a
    trailing blank line is also emitted after the last record, so the refactor ended the
    output with one. The caller passes False for the first record it writes -- which a
    streaming writer always knows, unlike which record is last (L-74).
    """
    out: list[str] = [""] if separator else []
    status = r.get("status", "ok")
    mark = "  " if status == "ok" else "!!"
    out.append(f"{mark} {r.get('started_utc', '?'):20} {r.get('script', '?')}")
    out.append(f"     run_id     {r.get('run_id', '?')}   generation {r.get('generation', '?')}")
    out.append(f"     command    {r.get('command', '?')}")
    if r.get("cwd"):
        out.append(f"     cwd        {r['cwd']}")
    out.append(
        f"     code       {r.get('git_commit', '?')}"
        + ("  DIRTY" if r.get("git_code_dirty") else "")
        # `is False`, never a default. A record written before this field existed does
        # not know the answer, and defaulting it to True would print the reassuring
        # answer for exactly the runs that cannot support it.
        + (
            "  DIRTY STATE UNKNOWN (git status did not run)"
            if r.get("git_status_captured") is False
            else ""
        )
    )
    ins, outs = r.get("inputs") or [], r.get("outputs") or []
    for i in ins:
        out.append(
            f"     in   {str(i.get('sha256') or '')[:PIN_DIGEST_CHARS]}  {i.get('path', '?')}"
        )
    for o in outs:
        out.append(
            f"     out  {str(o.get('sha256') or '')[:PIN_DIGEST_CHARS]}  {o.get('path', '?')}"
        )
    if status != "ok":
        f = r.get("failure") or {}
        out.append(f"     FAILED     {f.get('type', '?')}: {str(f.get('message', ''))[:160]}")
    if r.get("history_destination"):
        out.append(f"     history    {r['history_destination']}")
    if r.get("seeds"):
        out.append(f"     seeds      {r['seeds']}")
    # THE EMISSION POINT OF `log`'s PAGE [L-03]. Ten append sites above interpolate about
    # thirteen recorded fields — `command`, `cwd`, `script`, every input and output path, a
    # failure message, `history_destination`, `seeds` — and a newline inside any one of them
    # forges whole lines. L-03's filed remedy said *three f-strings*; there are ten, and a list
    # of ten is a list of eleven next month. One transform, per line, where the lines become
    # output. `printable` keeps a space and escapes everything else non-printable, so the
    # columns survive and a `\r` — which overwrites a line without adding one, and which a
    # line-count oracle cannot see — does not.
    return "\n".join(printable_lines(out)) + "\n"


def _yaml(rows: list[dict[str, typing.Any]]) -> str:
    """The shape of the transformation log this replaces, generated from the real record.

    Deliberately the same field names — `step`, `input`, `output`, `script`, `run_command`,
    `date` — so anyone who reads the old file can read this one. The difference is where the
    values come from: these were observed and hashed, not typed by hand.
    """

    return _yaml_header() + "".join(_yaml_entry(r) for r in rows)


#: [ADR-0017 R-5] I-18. `lineage --format json` shipped in 0.6.0 without a schema key.
#: It has no module of its own, so the constant sits beside the function that builds the
#: object, and the emitter reads it rather than writing the string a second time.
LINEAGE_SCHEMA = "runprov.lineage.v1"

#: [ADR-0017 R-5]. T-33's fifth row. `log` has no module either — it is the CLI reading the
#: history — so its constant sits here too, and both emitters read these rather than spelling
#: the string a second time where it is used.
LOG_SCHEMA = "runprov.log.v1"


def _same_destination(recorded: str, path: pathlib.Path) -> bool:
    """Whether a marker's recorded history IS the one this command was asked about.

    J-35, found while reproducing J-06. This was `str(m["history"]) != str(path)` — a
    comparison of SPELLINGS — so one file named two ways read as two destinations. Measured
    over a single marker beside a single missing history:

        --log /abs/path/runs.jsonl  ->  "Nothing has been recorded here yet"
        --log runs.jsonl            ->  "A MARKER BESIDE THIS PATH SAYS OTHERWISE: the run
                                         that left it recorded to /abs/path/runs.jsonl"
        --log ./runs.jsonl          ->  the same, a third spelling of the same file

    The second and third tell the operator the records went somewhere else and then name the
    path they just passed, with the advice *"If that names a file, pass it to --log"*. The
    marker always stores an absolute path and a relative `--log` is the ordinary way to type
    one, so the misleading branch is the one a person is most likely to reach.

    RESOLVED, NOT NORMALISED: a symlinked results directory is the same file by either name,
    and `resolve()` is what says so. `strict=False` is the default, which matters because the
    history does not exist in this branch — that is why we are here.

    A SINK IS NOT A PATH, and that case is the reason this returns a bool rather than
    normalising both sides for the caller to compare. `DbSink(...)` resolves to some nonsense
    relative to the cwd, compares unequal, and stays in `elsewhere` — which is correct, because
    it IS elsewhere. The `OSError` arm is Windows, where an invalid name raises here instead of
    coming back unequal; the string comparison is then the honest fallback and it is the
    behaviour this function replaced.
    """
    try:
        return pathlib.Path(recorded).resolve() == path.resolve()
    except OSError:
        return recorded == str(path)


def _log_no_history(
    path: pathlib.Path, args: argparse.Namespace, in_flight: list[dict[str, typing.Any]]
) -> dict[str, typing.Any]:
    """`log`'s answer when the history it was told to read is not there. [ADR-0017 R-15].

    `in_flight` IS DISCARDED HERE UNDER R-12, as in `lineage`: this payload's subject is the
    RECORDS, and a run with no record is not one of them. The parameter exists because the
    table calls all three builders identically — see `_NO_HISTORY_ANSWER`."""
    del in_flight
    return _log_answer(
        path,
        records=[],
        total=0,
        failed=0,
        unreadable=0,
        selectors=_log_selectors(args),
        matched_any=None,
        cannot_check=f"no run history at {path}",
    )


def _show_no_history(
    path: pathlib.Path, args: argparse.Namespace, in_flight: list[dict[str, typing.Any]]
) -> dict[str, typing.Any]:
    """`show`'s answer for a history that is not there. [ADR-0017 R-15]. T-33's last row.

    THIS ENTRY IS WHAT THE `KeyError` IN THE GUARD WAS WAITING FOR. When `log` was built, the
    table held two commands and `show` had no `json` to be asked for, so a lookup miss was
    unreachable and deliberately left to raise rather than fall back to a sibling's shape. It is
    reachable the moment this flag exists, and this is the entry that makes it right instead.
    """
    del args
    return show_mod.payload_no_history(path, f"no run history at {path}", in_flight)


#: The join's own counters, in the order `_lineage` returns them. ONE list, read by the one
#: builder below — a second copy is what let `lineage`'s two shapes drift apart (J-21).
_LINEAGE_COUNTERS = ("runs", "records_without_uid", "resolvable", "ambiguous", "orphan")


def _lineage_answer(
    path: pathlib.Path,
    joined: dict[str, typing.Any] | None,
    unreadable: int,
    cannot_check: str | None = None,
) -> dict[str, typing.Any]:
    """`lineage`'s answer, whichever state it is in. ONE BUILDER. J-21.

    IT WAS TWO, AND THEY DREW APART IMMEDIATELY. `lineage` was the only one of the three
    commands sharing the missing-history guard whose no-history answer was hand-written as a
    second dict instead of calling the answered path's builder — `log` and `show` both share
    theirs — and within one commit the two shapes differed by `path`, present only when the
    command could NOT answer. Key presence depending on success is exactly the inference R-8
    exists to remove, and the answered payload could not say which history it described.

    `unreadable` IS NEW AND IS THE HALF THAT MISLED A CONSUMER. Over a history where NOT ONE
    line parses, this payload used to be byte-identical to one over an empty valid history: the
    count reached stderr alone, which R-4 tells a caller not to read. `show` was fixed for this
    the day before and `lineage` was not, which is what a per-command fix costs when the
    commands share a defect. R-14 — what could not be established belongs in the same object as
    what was.

    ADDITIVE ON A PAYLOAD THAT SHIPPED in 0.6.0: `path` and `unreadable` are added, `schema` and
    `cannot_check` keep the places they took since, and the join's own six counters keep both
    their names and their order. Taylor ruled on 2026-09-30 that both halves be fixed together.
    """
    counters = dict.fromkeys(_LINEAGE_COUNTERS, 0) | {"edges": []} if joined is None else joined
    return {
        "schema": LINEAGE_SCHEMA,
        "path": path.as_posix(),
        **counters,
        "unreadable": unreadable,
        "cannot_check": cannot_check,
    }


def _lineage_no_history(
    path: pathlib.Path, args: argparse.Namespace, in_flight: list[dict[str, typing.Any]]
) -> dict[str, typing.Any]:
    """`lineage`'s entry in the missing-history table. `args` and `in_flight` are unused and are
    in the signature because the table calls every builder the same way — a lookup whose entries
    differ in arity is a lookup with a branch in it, which is what that table exists to avoid.

    R-12 IS WHY THIS DISCARDS THE SCAN RATHER THAN CARRYING IT. `in_flight` is the project
    page's own fact; a join over `run_uid` has no such field in any state, and adding one here
    so that three payloads look alike would be inventing a second source of truth for it."""
    del args, in_flight
    return _lineage_answer(path, None, 0, cannot_check=f"no run history at {path}")


#: [ADR-0017 R-15]. Every command sharing the missing-history guard, and all three can answer
#: in JSON as of T-33's last row. `show` was absent while it had no payload, and its absence was
#: what made the R-15 ratchet name it; a lookup MISS still raises rather than substituting a
#: sibling's shape, which is what a fourth command arriving here would deserve.
_NO_HISTORY_ANSWER: dict[
    str,
    typing.Callable[
        [pathlib.Path, argparse.Namespace, list[dict[str, typing.Any]]], dict[str, typing.Any]
    ],
] = {
    "log": _log_no_history,
    "lineage": _lineage_no_history,
    "show": _show_no_history,
}


def _log_selectors(args: argparse.Namespace) -> dict[str, typing.Any]:
    """What was ASKED, normalised so a default reads as *not asked*. [ADR-0017 R-8].

    `argparse` defaults `--script` and `--run-id` to `""` and `--limit` to `0`, which are
    values rather than absences — a payload carrying `"script": ""` says the caller asked for
    a script named empty string. `null` says nobody asked, which is the same distinction R-8
    draws for a field and the reason `matched` is a tri-state beside this.

    ONE BUILDER, READ BY BOTH EMITTERS: the ordinary answer and the no-history answer print
    the same block, and a second copy is how they would drift.
    """
    return {
        "script": args.script or None,
        "run_id": args.run_id or None,
        "failed": bool(args.failed),
        "limit": args.limit or None,
    }


def _log_answer(
    path: pathlib.Path,
    *,
    records: list[dict[str, typing.Any]],
    total: int,
    failed: int,
    unreadable: int,
    selectors: dict[str, typing.Any],
    matched_any: bool | None,
    matching: int = 0,
    unfinished: int = 0,
    cannot_check: str | None = None,
) -> dict[str, typing.Any]:
    """`log`'s ANSWER as one object. ADR-0017 R-3's amendment, R-5, R-7, R-9, R-12, R-15.

    NOT THE SAME THING AS `--format jsonl`, and R-3's amendment is explicit about it: `jsonl`
    is the RECORDS, one per line, as stored. This is the ANSWER — what was asked, what came
    back, and what could not be established — and a consumer asking *what did this command
    find* is asking a different question from one asking *give me the records*.

    `shown` BESIDE `total` IS THE WHOLE POINT, the same discipline `check.examined` carries:
    an empty `records` list means one thing after a selector that matched nothing and another
    over a history with nothing in it, and a payload that served both the same way would hand
    a consumer the collapse A-08 spent a row removing.

    `failed` IS SCOPED TO `matching`, NOT TO `shown` — J-14. It was counted inside `emit`, which
    runs only for records that survive `--limit`, so over four runs whose oldest failed
    `--limit 2` reported `shown 2, total 4, failed 0` and the page printed no FAILED clause at
    all. A truncation silently answered *nothing failed* about a history with a failure in it.

    AND `total` IS NOT ITS DENOMINATOR EITHER, which is why `matching` had to exist rather than
    the count simply moving. `total` is the HISTORY's size — it has to be, because it is the only
    thing separating *your selector matched nothing* from *the history is empty*, which is the
    argument above — so scoping `failed` to it would report another script's failure to someone
    who asked about this one. Measured: `log --script other` over a history whose `build` failed
    would say `1 FAILED`. The honest scope is the SELECTION, and nothing in the payload stated
    it, so a consumer could not tell what `failed` was out of.

    `unfinished` IS THE THIRD STATE THAT ARGUMENT MISSED — J-13. A history holding one start
    line and no record produced a payload equal in every content field to one over an EMPTY
    history: `total 0, shown 0, failed 0, unreadable 0, records [], cannot_check null`, with
    only `path` differing because `path` echoes the argument. So the two most different things a
    history can say — *nothing has run here* and *a run began and never came back* — were one
    answer. `show` tells them apart over the same file; this was a per-command gap.

    A COUNT AND NOT A LIST, and that is `log`'s honest limit rather than a shortcut. `show`'s
    `in_flight` carries each run WITH its liveness, which it can only do because it reads the
    `.incomplete` markers as well. `log` reads the history and nothing else, so what it knows
    is how many runs started with no ending ON RECORD — permanent, append-only evidence, and
    silent about whether anything is still running. Naming it `in_flight` would have promised
    the half it cannot see.

    `matched` IS None WHEN NOTHING WAS NAMED. `--script` and `--run-id` name a record, so
    missing them is a finding and the exit code says so; `--failed` selects a CLASS and no
    failures is the good answer. A tri-state says *not asked* without a consumer inferring it
    from the selector block — R-8's distinction, one level up from a field.

    `records` ARE AS STORED (R-9). No field is renamed on the way out, so a line here and the
    same line under `--format jsonl` are the same object.
    """
    return {
        "schema": LOG_SCHEMA,
        "path": path.as_posix(),
        "shown": len(records),
        "total": total,
        "failed": failed,
        # J-14. How many runs the SELECTORS matched, which is `failed`'s denominator and was
        # computable from nothing else in this object.
        "matching": matching,
        "unreadable": unreadable,
        # J-13. Runs whose start is in this history and whose record is not.
        "unfinished": unfinished,
        "selectors": selectors,
        # J-36. RENAMED FROM `matched`, which `show` also carried — as an INT COUNT. One name
        # for two ideas and two types across one package: here a tri-state *did a named target
        # hit anything*, there *how many matched*. Both were invented by their payload builders
        # rather than owned by a structure, so R-9 bound neither and both could move; `show`'s
        # became `matching`, which is this command's word for the same count.
        "matched_any": matched_any,
        "cannot_check": cannot_check,
        "records": records,
    }


def _lineage(
    source: pathlib.Path | typing.Sequence[dict[str, typing.Any]],
    bad: list[int] | None = None,
    scripts: dict[str, str] | None = None,
    consumers: dict[str, list[str]] | None = None,
    outputs_by: dict[str, list[str]] | None = None,
) -> dict[str, typing.Any]:
    """Reconstruct the run DAG. JOIN ON THE DIGEST, not the path (L1).

    TWO STREAMING PASSES, not one materialised list, and the comment this replaces is why
    that took a reviewer to notice: it said lineage "genuinely needs every record at once ...
    there is nothing to stream past", which is a statement about the RECORDS and the join
    does not need them. It needs an INDEX over digests. Measured at 500,000 runs: 2.2 GB
    holding the records against 310 MB holding the index, for byte-identical output.

    Pass one builds `produced` (digest -> producers) and fills `scripts` (address -> script
    name); pass two resolves each input against it. What survives both passes is one entry
    per digest and one short, shared string per run — not the parameters, notes, git state
    and terminal log of every record in the history.

    `scripts` is an OUT-PARAMETER, like `bad`, and not a key of the returned graph. The graph
    is what `lineage --format json` prints, so putting a name-per-run in it would both change
    a documented output and add 500,000 entries to that document at 500,000 runs — trading a
    memory problem for a bigger file, in the command whose output a consumer parses.

    `source` is a path to stream twice, or a re-iterable sequence for callers that already
    hold the records. A generator would silently give an empty second pass, which is the
    defect L-04 documents one module over, so the sequence branch is `iter()`-ed afresh each
    time rather than consumed.

    Matching a consumer's input to a producer's output BY PATH is a heuristic, and it is the
    reason lineage was unusable: the same path is rewritten by many runs over a project's
    life, so every read has as many candidate producers as there were writes. Measured on
    the real history at the time of the review: 991 resolvable edges, **2,743 ambiguous**.

    The digest is a fact rather than a guess -- that byte sequence was produced exactly where
    it was produced. Two rules make it total:

    1. **join on the content digest**, falling back to the raw hash for a record that has
       only that (C3 aligned them, and the pin uses the content digest);
    2. **the producer must have finished before the consumer started.** Two runs writing
       identical content is ordinary -- a rebuild that reproduces -- so the digest alone can
       have several candidates. Time settles it: an artifact cannot be read from a run that
       had not finished writing it. The latest such producer wins.

    Together those are deterministic, so `ambiguous` is 0 by construction rather than by
    luck. It is still REPORTED, because a count that can only be zero is a count nobody
    should trust without seeing it.

    Records with no `run_uid` predate C8 and cannot be an endpoint; they are counted, not
    dropped. 1,310 entries in the real history already predate `run_id`, and a graph that
    silently excluded them would understate its own coverage.
    """

    counted_once = [False]

    def records() -> typing.Iterator[dict[str, typing.Any]]:
        """A FRESH pass over the history. Called exactly twice — see the two PASS comments
        below. Returning a new iterator each time is the whole contract: handing back a
        part-consumed one would make pass two silently empty.

        UNREADABLE LINES ARE COUNTED ON THE FIRST PASS ONLY. The file is walked twice and
        `bad` accumulates, so counting on both reported every damaged line twice — measured,
        a history with 2 torn lines said "4 unreadable line(s) skipped". A reader who cannot
        trust the count of what was lost is worse off than one given no count, because the
        number looks like evidence."""
        if not isinstance(source, pathlib.Path):
            return iter(source)
        if counted_once[0]:
            return _counted(source, [0])
        counted_once[0] = True
        return _counted(source, bad if bad is not None else [0])

    def digests(io_: dict[str, typing.Any]) -> list[str]:
        """EVERY digest this entry carries, most specific first — not just the best one.

        Preferring one and stopping breaks the join across the v1/v2 boundary. A `v1`
        record holds `sha256` alone; a `v2` record of the SAME FILE holds `sha256` *and*
        `content_sha256`, and the content digest is taken over normalised text, so it
        never equals the raw one. Preferring `content_sha256` on the consumer and falling
        back to `sha256` on the producer therefore compares two different keys, and every
        edge crossing an upgrade is lost.

        Measured on a mixed history: a v2 run reading a file a v1 run had just written was
        reported as an ORPHAN INPUT — "produced by no recorded run", which is a false
        statement about provenance rather than a missing one.

        Indexing on all of them cannot invent an edge: equal `sha256` means identical
        bytes, and equal `content_sha256` means identical content by the definition this
        package already pins on. Order matters only for determinism when both hit.
        """
        out = []
        for key in ("content_sha256", "sha256", "sha256_tree"):
            d = io_.get(key)
            if d and str(d) not in out:
                out.append(str(d))
        return out

    def address(r: dict[str, typing.Any]) -> str:
        """A unique handle for one run — RECORDED where C8 reached, DERIVED where it did not.

        Requiring `run_uid` made the rule non-retroactive, and L1's whole claim is that it
        works "retroactively, by a reader rule alone". Measured when this was written: every
        one of the 2,196 records in the real history predates the field, so insisting on it
        produced a graph of 0 edges over 0 usable runs — a reader that excludes the entire
        corpus it was written to read.

        `run_uid` is for ADDRESSING, not for resolving; the digest join is what resolves.
        Where it is absent, the sidecar path plus the start time identify the run as well as
        anything can, and the count of derived addresses is reported so nobody mistakes one
        for the other.
        """
        uid = r.get("run_uid")
        if uid:
            return str(uid)
        return "derived:" + "|".join(
            str(r.get(k, "")) for k in ("script", "started_utc", "provenance_path")
        )

    # PASS ONE: the digest index, the address -> script map, and the counts. All three are
    # bounded by what the graph actually joins on rather than by the size of a record.
    no_uid = 0
    runs = 0
    produced: dict[str, list[tuple[str, str]]] = {}
    names = scripts if scripts is not None else {}
    # OUT-PARAMETERS, like `bad` and `scripts` above and for the same stated reason: they are
    # not part of the graph `lineage --format json` prints, and `impact` needs them from THIS
    # traversal rather than from a second one. ADR-0015 requires it — two walks of one history
    # that disagree about what is connected is a defect this project keeps finding, and the
    # cheapest way to guarantee they agree is to have only one walk.
    #
    # `read_by` is digest -> the runs that read it, the mirror of `produced` and the one thing
    # `impact` cannot derive from the edges. `made_by` is address -> output PATHS only: what an
    # artifact is CALLED is presentation, and holding whole records here is the memory defect
    # this function's own docstring records.
    read_by = consumers if consumers is not None else {}
    made_by = outputs_by if outputs_by is not None else {}
    for r in records():
        runs += 1
        if not r.get("run_uid"):
            no_uid += 1
        here = address(r)
        # One SHORT, SHARED string per run -- `_render_lineage` needs a name for at most the
        # 400 addresses in the edges it prints, and holding the record to get one was the
        # 94% of the memory this row is about.
        names.setdefault(here, str(r.get("script", "?")))
        when = str(r.get("finished_utc") or r.get("started_utc") or "")
        for o in r.get("outputs") or []:
            for d in digests(o):
                produced.setdefault(d, []).append((when, here))
            name = o.get("path") if isinstance(o, dict) else o
            if name:
                made_by.setdefault(here, []).append(str(name))
    for v in produced.values():
        v.sort()

    # PASS TWO: resolve every input against the index built above.
    edges: list[tuple[str, str]] = []
    resolvable = ambiguous = orphan = 0
    for r in records():
        started = str(r.get("started_utc") or "")
        for i in r.get("inputs") or []:
            # rule 2: only a producer that had FINISHED. `<=` because a stage may write and
            # a later stage in the same second may read -- second resolution is the reason
            # the ad-hoc run id collides in the first place.
            me = address(r)
            for d in digests(i):
                if me not in read_by.setdefault(d, []):
                    read_by[d].append(me)
            candidates = [c for d in digests(i) for c in produced.get(d, [])]
            before = sorted({c for c in candidates if c[0] <= started and c[1] != me})
            if not before:
                orphan += 1
                continue
            edges.append((before[-1][1], me))
            resolvable += 1
    return {
        "runs": runs,
        "records_without_uid": no_uid,
        "resolvable": resolvable,
        "ambiguous": ambiguous,
        "orphan": orphan,
        "edges": edges,
    }


#: How many edges the TEXT view prints before summarising. The graph itself is complete --
#: `--format json` carries every edge -- and a terminal is not where 40,000 of them are read.
#:
#: ONE constant, because the slice and the "N more" line have to agree. They were two
#: literal 200s, and a change to either would have left the message stating a number the
#: page had not truncated at.
EDGES_SHOWN = 200


def _render_lineage(g: dict[str, typing.Any], scripts: dict[str, str] | None = None) -> str:
    """The text view. Takes the GRAPH alone -- it used to take the records too, and rebuilt
    an address -> record map from them purely to look up a script name per edge. `_lineage`
    now carries `scripts`, which is the same answer as one short shared string per run
    instead of the whole history."""
    by_uid: dict[str, str] = scripts or {}
    out = [
        f"lineage over {g['runs']} record(s)",
        f"  resolvable edges : {g['resolvable']}",
        f"  ambiguous        : {g['ambiguous']}",
        f"  orphan inputs    : {g['orphan']}   (read, produced by no recorded run)",
        f"  derived addresses: {g['records_without_uid']}   (predate C8; addressed by "
        f"sidecar path + start time, not by a recorded uid)",
        "",
    ]
    for a, b in g["edges"][:EDGES_SHOWN]:
        out.append(f"  {by_uid.get(a, '?')} [{str(a)[:8]}] -> {by_uid.get(b, '?')} [{str(b)[:8]}]")
    if len(g["edges"]) > EDGES_SHOWN:
        out.append(f"  ... {len(g['edges']) - EDGES_SHOWN} more edge(s) not shown")
    out.append("")
    out.append(f"{len(g['edges'])} edge(s)")
    return "\n".join(out)


class UsageError(Exception):
    """A mistake in the command line, reported as a message rather than a traceback.

    `runprov exec --input no_such.tsv` used to end in `FileNotFoundError` and a stack, while
    every other user error in this CLI prints a line and exits non-zero. The message was
    already the right message; the traceback was the problem, because it buries it.
    """


class CommandFailedError(Exception):
    """The wrapped command exited non-zero.

    Raised INSIDE the `with` so `__exit__` records the run as failed, then caught so the
    wrapper exits with the COMMAND's code rather than a Python traceback. Named without a
    leading underscore because it reaches the terminal: `__exit__` prints the exception
    type, and `_CommandFailedError: sort exited 2` reads like a leaked internal when it is in
    fact the whole message.
    """


#: Below this, a negative `returncode` is not a POSIX signal. `subprocess` reports a
#: signal-killed child as -N with N <= 64 or so; Windows reports a crash as a large negative
#: NTSTATUS. -256 is comfortably past every real signal and nowhere near an NTSTATUS.
_KILLED_BY_SIGNAL_FLOOR = -256

#: How long a child gets to exit after being sent SIGTERM, before SIGKILL. Short on purpose:
#: this only runs when THIS process is already being terminated, and whatever is killing us is
#: usually counting too -- SLURM sends SIGKILL itself shortly after SIGTERM.
_CHILD_GRACE_SECONDS = 5.0


def _stop_child(proc: subprocess.Popen[bytes]) -> None:
    """Ask the child to stop, then insist. Never raises, on any platform.

    IT USED TO RAISE, AND ON THE ONE PATH THAT MUST NOT. `os.killpg` does not exist on
    Windows, so the call raised `AttributeError` — which is not an `OSError` and went
    straight through `contextlib.suppress(OSError)`. Reproduced under an emulated Windows,
    and it cost two things at once:

        RUN FAILED — recorded: AttributeError: module 'os' has no attribute 'killpg'
        child 889920 STILL RUNNING after the wrapper died — reparented to init

    The child was ORPHANED — the exact defect `start_new_session=True` was added to prevent,
    reappearing on the platform where that argument does nothing — and the record blamed
    `AttributeError` for a run that had been terminated by SIGTERM. A wrong cause in the
    record is the worse half.

    NOT A WIDER `except`. Catching `AttributeError` would have silenced the symptom and left
    the child running; the platforms simply need different calls. Where there are process
    groups, signal the GROUP: the child may have children, and `sh -c` usually does.

    THE WINDOWS PATH IS NARROWER, and saying so is the point. `start_new_session` is ignored
    there — verified, it appears nowhere in `subprocess`'s Windows branch — so there is no
    group to signal and `terminate()`/`kill()` reach the child alone. A grandchild spawned by
    a `cmd /c` can still outlive it. That is a real limit of the platform, not something this
    function can fix, and it is better stated than discovered.
    """

    def send(hard: bool) -> None:
        try:
            if hasattr(os, "killpg"):
                os.killpg(proc.pid, signal.SIGKILL if hard else signal.SIGTERM)
            elif hard:  # pragma: no cover - Windows only
                proc.kill()
            else:  # pragma: no cover - Windows only
                proc.terminate()
        except OSError:  # guards-ok: nothing left to signal
            pass

    # THE POLITE SIGNAL IS UNCONDITIONAL, and an earlier draft of this made it conditional on
    # `proc.poll() is None` — which an existing test caught with "the child must be told
    # first". On POSIX the target is the GROUP, and a group outlives its leader: the direct
    # child can be gone while the grandchildren `sh -c` started are still running. Checking
    # first would skip the signal in exactly the case where it still has work to do.
    send(hard=False)
    with contextlib.suppress(subprocess.TimeoutExpired):
        proc.wait(timeout=_CHILD_GRACE_SECONDS)
    if proc.poll() is None:
        # It ignored SIGTERM. SIGKILL cannot be ignored, and leaving it running would be the
        # orphan this whole function exists to prevent.
        send(hard=True)
        with contextlib.suppress(subprocess.TimeoutExpired):
            proc.wait(timeout=_CHILD_GRACE_SECONDS)


def _export(args: argparse.Namespace) -> int:
    """The record in somebody else's vocabulary. Reads; writes nothing this package owns.

    TWO SCOPES BECAUSE THEY ANSWER DIFFERENT QUESTIONS. The whole history is what you DEPOSIT
    — every run, every artifact, the lineage between them. One sidecar is what you ATTACH to a
    submitted artifact, and it is the only scope available to somebody holding a file and its
    sidecar with no history to read, which is the case the in-band pin exists for.
    """
    if args.sidecar:
        path = pathlib.Path(args.sidecar)
        try:
            rec = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            print(f"  runprov export: cannot read {path}: {exc}", file=sys.stderr)
            return CANNOT_CHECK
        if not isinstance(rec, dict):
            print(f"  runprov export: {path} is not a runprov sidecar", file=sys.stderr)
            return CANNOT_CHECK
        records, name = [rec], f"{rec.get('script', 'run')} — one run"
    else:
        log = pathlib.Path(args.log) if args.log else active().resolved_run_log()
        if not log.is_file():
            print(
                f"  runprov export: no run history at {log}\n"
                f"    Export one run instead by naming its sidecar: "
                f"`runprov export out/step.prov.json`.",
                file=sys.stderr,
            )
            return CANNOT_CHECK
        # COMPLETED RUNS ONLY. A `runprov.start.v1` line is a run that has begun and says
        # nothing yet about inputs or outputs; putting it in a crate would assert an activity
        # that produced nothing, which is not what a reader of that crate would understand.
        records = [r for r in (_parsed(log)) if r.get("schema") != START_SCHEMA]
        name = f"{log.parent.name} — {len(records)} run(s)"
    text = render(records, args.format, name)
    if not args.out:
        sys.stdout.write(text)
        return 0
    out = pathlib.Path(args.out)
    if out.is_dir():
        out = out / default_filename(args.format)
    try:
        out.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_text(out, text)
    except OSError as exc:
        print(f"  runprov export: cannot write {out}: {exc}", file=sys.stderr)
        return CANNOT_CHECK
    print(f"# {len(records)} run(s) -> {out} ({args.format})", file=sys.stderr)
    return 0


def _parsed(log: pathlib.Path) -> list[dict[str, typing.Any]]:
    """Every readable record in the history. Unreadable lines are skipped, as everywhere."""
    return [rec for rec in _stream(log) if rec is not None]


def _capture(args: argparse.Namespace) -> int:
    """Run a Python script inside a `Run` and record what it ACTUALLY opened.

    THE ADOPTION COST, REMOVED. `run.input(p)` is one call and it is one call in every script,
    paid by every colleague, for ever — and the scripts that most need a record are the
    exploratory ones nobody is going to refactor. This runs an UNMODIFIED script and records
    what the interpreter saw it do.

    IN-PROCESS, and that is the whole mechanism. The audit hook this package already installs
    fires for every `open` in the process; a subprocess has its own interpreter and its own
    hooks, so `runprov exec` — which spawns one — can record the command and its exit code but
    never the files inside it. `runpy` runs the script here, under this hook, so every read
    and every write is seen.

    WHAT IT DOES NOT GIVE YOU, said before somebody discovers it. An observed record is
    WIDER and WEAKER than a declared one: it lists what was opened, not what mattered, and
    nothing in it is pinned into an artifact — `open_output` is still the only way an artifact
    carries its own provenance. This is the rung below `run.input()`, not a replacement for
    it: adopt it in an afternoon, and register properly where it matters.
    """
    script = pathlib.Path(args.script)
    if not script.is_file():
        print(f"  runprov capture: no such script: {script}", file=sys.stderr)
        return CANNOT_CHECK
    proj = active()
    name = args.name or script.stem
    prov = pathlib.Path(args.provenance) if args.provenance else None
    if prov is None:
        prov = proj.resolved_run_log().parent / f"{name}.prov.json"

    code = 0
    argv_before = list(sys.argv)
    sys.argv = [str(script), *(args.rest or [])]
    try:
        with Run(name, provenance=prov, script_path=script) as run:
            try:
                runpy.run_path(str(script), run_name="__main__")
            except SystemExit as exc:
                # THE SCRIPT'S OWN EXIT CODE, so `capture` composes in a Makefile exactly as
                # `exec` does. `SystemExit(None)` and `SystemExit(0)` are both success.
                code = exc.code if isinstance(exc.code, int) else (0 if exc.code is None else 1)
            _register_observed(run, proj)
    finally:
        sys.argv = argv_before
    return code


def _register_observed(run: Run, proj: Project) -> None:
    """Turn what the hook saw into inputs and outputs. Never raises.

    A FILE THIS RUN WROTE IS AN OUTPUT, EVEN IF IT ALSO READ IT — a script that appends to a
    table has not taken it as an input, and recording it as one would draw a lineage edge from
    the file to itself.

    `unregistered` DOES THE FILTERING, and reusing it is the point rather than a shortcut: it
    already drops code, caches, site-packages, anything outside the root and this package's
    own files, and every one of those exclusions was put there by a false positive somebody
    met. A second filter here would be a second set of rules to keep in step.
    """
    mine = [pathlib.Path(str(p)) for p in (run.record.get("provenance_path"),) if p]
    written = set(unregistered(run._opened_write, [], proj.root, exclude=mine))
    read = [n for n in unregistered(run._opened, [], proj.root, exclude=mine) if n not in written]
    for name in read:
        with contextlib.suppress(OSError, ValueError):
            run.input(proj.root / name)
    for name in sorted(written):
        with contextlib.suppress(OSError, ValueError):
            run.output(proj.root / name)


def _exec(args: argparse.Namespace) -> int:
    """`runprov exec -- samtools sort in.bam -o out.bam`: a subprocess, recorded as a run.

    `run.tool()` and `run.code()` are calls a caller has to make, and nothing detects an
    unregistered `subprocess.run([...])`. For a pipeline driven from a Makefile, a Snakefile
    or a shell script there is no Python to put them in at all -- so the recording has to be
    something you can put IN FRONT of the command.

    The recorded `command` is the `runprov exec` invocation, and that is deliberate rather
    than a shortcut: it is what actually ran, AND re-running it re-runs the tool and records
    the rerun. The wrapped argv is in `parameters` where a reader can see it directly.

    Inputs and outputs are DECLARED, because they cannot be inferred without tracing every
    syscall the tool makes. That is the same bargain `run.input()` strikes in Python: the
    package will not guess what a step read.

    The command's own exit code is returned, so this composes in a Makefile or a Snakemake
    `shell:` directive without changing what failure means.
    """
    # ONLY THE LEADING SEPARATOR, and only if it is there. `nargs=REMAINDER` hands back the
    # `--` that separates runprov's own flags from the command, so one has to come off --
    # but dropping EVERY `--` rewrites the command itself. `git log -- src/` means "what
    # follows are paths"; `find . -- -weird-name` protects a leading dash. Both ran as
    # something else, and `parameters.argv` recorded the mangled list as though it were what
    # ran, which is the one thing this wrapper exists to get right.
    argv = list(args.command or [])
    if argv and argv[0] == "--":
        argv = argv[1:]
    if not argv:
        print(
            "runprov exec needs a command:  runprov exec -- samtools sort in.bam -o out.bam\n"
            "  Declare what it reads and writes so the record can hash them:\n"
            "    runprov exec --input in.bam --output out.bam -- samtools sort ...",
            file=sys.stderr,
        )
        return 2

    name = args.name or pathlib.Path(argv[0]).name
    project = active()
    # THE PROJECT DECIDES, not this file. `exec` hardcoded `{name}_{run_id}.json` — per-run
    # naming, which is `sidecar_per_run=True` behaviour reached by a second route — while
    # the library default is `sidecar_per_run=False` and overwrites. Two entry points, the
    # same decision, opposite answers, and a project that had chosen one got the other from
    # whichever door it came through. Now `exec` names the sidecar the way a `Run` in a
    # script would and lets `_sidecar_name` apply the project's setting, so changing the
    # setting changes both.
    provenance = (
        pathlib.Path(args.provenance)
        if args.provenance
        else pathlib.Path(project.root) / "provenance" / f"{name}.prov.json"
    )
    provenance.parent.mkdir(parents=True, exist_ok=True)

    returncode = 1
    try:
        with Run(
            name,
            {"argv": argv, "command": shlex.join(argv)},
            project=project,
            provenance=provenance,
            terminal_log=pathlib.Path(args.capture) if args.capture else False,
            script_path=pathlib.Path(argv[0]),
        ) as run:
            run.tool(argv[0])
            # `input()` RAISES for a path that is not there, deliberately -- a registered
            # input is hashed and pinned, so it must exist at registration. Through the CLI
            # that arrived as a raw Python traceback, which no other user error here does:
            # a mistyped `--input` is a usage mistake, not a crash, and a traceback buries
            # the message that says exactly what to fix.
            for i in args.input:
                try:
                    run.input(i)
                except OSError as exc:
                    raise UsageError(str(exc)) from exc
            for o in args.output:
                run.output(o)
            try:
                # Inheriting this process's stdout and stderr, so the command still writes
                # to the terminal it was launched from -- and so `--capture`, which works at
                # file-descriptor level, sees a subprocess's output too.
                # ITS OWN PROCESS GROUP, so a signal can reach the child as a group. Without
                # this, SIGTERM to `runprov exec` killed the wrapper and ORPHANED the child --
                # measured: a `sleep 40` outlived the runprov that started it, which on a
                # cluster means work still running after the job that owns it is gone.
                proc = subprocess.Popen(argv, start_new_session=True)
                try:
                    returncode = proc.wait()
                except BaseException:
                    # BaseException, because `Terminated` is one: the whole point is to catch
                    # the signal-turned-exception and hand the signal ON to the child before
                    # this process unwinds. Terminate the GROUP -- the child may itself have
                    # children, and `sh -c` usually does.
                    _stop_child(proc)
                    raise
            except OSError as exc:
                run.note("exec_error", f"{type(exc).__name__}: {exc}")
                raise CommandFailedError(str(exc)) from exc
            # THE RAW WAIT STATUS, unchanged: `subprocess` reports a signal-killed child as
            # -N, and that is the one value from which the signal can still be recovered.
            run.note("exit_code", returncode)
            # 128+N FOR A SIGNAL, LIKE A SHELL. `raise SystemExit(-15)` reaches the OS as
            # `-15 & 0xFF` = 241, so `exec` returned 241 where `sh -c` returns 143 -- and 247
            # for SIGKILL where a shell says 137, which is the number an OOM check looks for.
            #
            # Worse than merely different: IT COLLIDED. A child killed by SIGTERM and a child
            # that exited 241 both produced 241, two states a shell keeps apart. 255 (SIGHUP)
            # is not inert either -- `xargs` aborts an entire batch on 255 and continues on
            # 129, measured -- so the rewrite changed how a pipeline behaves, not just what a
            # number looked like.
            #
            # `exec`'s whole promise is "returns the command's own exit code, so it composes
            # without changing what failure means". This is that sentence being true.
            if _KILLED_BY_SIGNAL_FLOOR < returncode < 0:
                # Bounded below deliberately: on Windows a crashing child yields a large
                # negative NTSTATUS (e.g. -1073741819), which is not a signal number and must
                # not be read as one. Unverified on Windows -- there is no Windows here.
                signum = -returncode
                try:
                    signame = signal.Signals(signum).name
                except ValueError:  # a number this platform has no name for
                    signame = f"signal {signum}"
                run.note("signal", {"number": signum, "name": signame})
                returncode = 128 + signum
                # NAMES THE SIGNAL, because "exited -15" is both unreadable and wrong: the
                # process did not exit -15, it was killed. The parent-side vocabulary in
                # `run.Terminated` already gets this right; this matches it.
                raise CommandFailedError(f"{argv[0]} was killed by {signame} ({signum})")
            if returncode != 0:
                raise CommandFailedError(f"{argv[0]} exited {returncode}")
    except UsageError as exc:
        # A MESSAGE, not a traceback. The run is still recorded as failed by `__exit__` --
        # the mistake happened inside the block -- so the history says what was attempted.
        print(f"  runprov exec: {exc}", file=sys.stderr)
        return 2
    except CommandFailedError as exc:
        print(f"  runprov exec: recorded a FAILED run — {exc}", file=sys.stderr)
        return returncode if returncode else 1
    except Terminated as exc:
        # A NUMBER, NOT A TRACEBACK, for the commonest way a cluster job actually ends.
        # `Terminated` is a BaseException on purpose -- so an ordinary `except Exception:`
        # cannot swallow a kill -- but nothing here caught it either, so `scancel`-shaped
        # termination (SIGTERM to the process group) printed a raw Python traceback and
        # exited 1. Measured. The RECORD was already correct; only the CLI's answer was not.
        #
        # 128+N, matching a signal-killed child and matching a shell, so a wrapper that
        # inspects the code sees the same number whether the signal hit the child or hit
        # runprov itself. SLURM's time limit is SIGTERM-then-SIGKILL and `scancel` is
        # SIGTERM, which the signal-handling docstring in `run.py` already calls out.
        print(f"  runprov exec: {exc}", file=sys.stderr)
        return 128 + exc.signum
    return returncode


def _log_unreadable(args: argparse.Namespace, path: pathlib.Path) -> int:
    """`log --unreadable`: the lines the other readers counted and could not use.

    THE COUNT WAS ALREADY THERE AND WAS ONLY ACTIONABLE AS A NUMBER. Every reader in this
    package degrades correctly and says how much it lost — `2 unreadable line(s) skipped` —
    which tells a person that something is wrong and nothing about what. This prints the
    lines themselves so they can decide.

    IT IS NOT A REPAIR COMMAND, AND THERE WILL NOT BE ONE. The predecessor needed nine
    `fix_transformation_log_*.py` scripts because a single YAML document loses everything
    after the first bad block. JSONL loses the bad line and counts it, which is the whole
    reason for the format — so the record needs no repair, damage costs exactly the damaged
    lines, and a command that rewrote `runs.jsonl` would contradict the central claim that
    the record is append-only and never rewritten. It would also add a new way to lose data:
    a bad repair destroys good records, and the tool becomes the risk.

    The file that CAN be corrupted is the YAML view, and its recovery already exists and is
    printed in that file's own banner:

        python -m runprov log --format yaml > provenance/transformation_log.yml

    Regenerating a view from the record of truth is the whole heal story.

    EXIT 0 EVEN WHEN IT FINDS SOMETHING. This reports; it does not gate. `log` already
    returns 1 for "no history at that path", and making a second condition share that code
    would put two meanings on it — the overload ledger row L-81 is about, which is still
    open and still Taylor's. A caller who wants a gate has the count on stderr.
    """
    found = 0
    for number, raw in _unreadable(path):
        found += 1
        sys.stdout.write(_render_unreadable(number, raw) + "\n")
    if found:
        print(
            f"# {found} unreadable line(s) in {path}. This command reads and never writes:\n"
            f"#   the record is append-only, and a line that will not parse costs exactly\n"
            f"#   itself — every other record in this file is intact and still readable.\n"
            f"#   To rebuild the YAML view from the record of truth:\n"
            f"#     python -m runprov log --format yaml > provenance/transformation_log.yml",
            file=sys.stderr,
        )
    else:
        print(f"# every line in {path} parses.", file=sys.stderr)
    return 0


def _log(args: argparse.Namespace, path: pathlib.Path) -> int:
    """`log`, streamed: one record in memory at a time, or `--limit` of them.

    The history is appended forever and this is the command that reads all of it. Rendering
    used to build every line for every record before printing any of them, which on a
    100,000-run history means holding the whole thing twice -- once parsed, once as text.

    `--limit N` keeps a deque of N and nothing else: the last N runs are what was asked for,
    and the 99,900 before them do not need to be in memory to be skipped. Without a limit
    each record is WRITTEN as it streams past and then dropped.

    NOTHING IS DISCARDED FROM THE FILE. This reads; it has never written. `--limit` is a
    view over the history, not a trim of it -- the records it does not show are exactly
    where they were, and the next command without `--limit` shows them again.
    """
    if args.unreadable:
        # REFUSED, NOT IGNORED. Every other flag on `log` names RECORDS — a script, a run id,
        # a status, the last N of them, a rendering — and an unreadable line has no record to
        # name. There is no honest "ignored" semantics to announce, unlike `show --stale`
        # with a target, where the flag does mean something on a page it does not apply to.
        clashing = [
            flag
            for flag, on in (
                ("--limit", args.limit),
                ("--script", args.script),
                ("--run-id", args.run_id),
                ("--failed", args.failed),
                ("--format", args.format != "text"),
            )
            if on
        ]
        if clashing:
            raise UsageError(
                f"--unreadable cannot be combined with {', '.join(clashing)}: those select "
                f"and render RECORDS, and a line that will not parse has none. Run it alone."
            )
        return _log_unreadable(args, path)

    keep: collections.deque[dict[str, typing.Any]] | None = (
        collections.deque(maxlen=args.limit) if args.limit else None
    )
    bad = total = shown = n_failed = matching = 0
    # J-13. START LINES PAIRED AGAINST THEIR RECORDS, which is the only honest way to count
    # them: EVERY run leaves a start line and then a record, measured — two completed runs give
    # a four-line history — so a raw count of starts is the number of runs that BEGAN and would
    # read as alarming over a perfectly healthy project.
    #
    # AND IT COSTS ALMOST NOTHING, which is what makes it acceptable in the one reader whose
    # design forbids materialising: a run's start and its record are APPENDED ADJACENTLY, so
    # this set holds one uid at a time in the ordinary case and only grows for runs that are
    # genuinely unfinished. It holds uids, never records.
    pending: set[str] = set()

    # THE FLAGS THAT NAME A RECORD, as opposed to the one that selects a class. Only these
    # make "matched nothing" a finding — see `CANNOT_CHECK` for the whole contract.
    named = [f for f in (args.script, args.run_id) if f]
    named_hits = 0

    def named_hit(r: dict[str, typing.Any]) -> bool:
        return not (
            (args.script and r.get("script") != args.script)
            or (args.run_id and r.get("run_id") != args.run_id)
        )

    def matches(r: dict[str, typing.Any]) -> bool:
        return not (
            (args.script and r.get("script") != args.script)
            or (args.run_id and r.get("run_id") != args.run_id)
            or (args.failed and r.get("status") != "failed")
        )

    collected: list[dict[str, typing.Any]] = []

    def emit(r: dict[str, typing.Any]) -> None:
        # J-14. `n_failed` IS NO LONGER COUNTED HERE. This runs once per record that survives
        # `--limit`, so a failure older than the window was never counted — which is the whole
        # row. It is counted where the selectors are applied instead.
        nonlocal shown
        shown += 1
        if args.format == "json":
            # [ADR-0017 R-3, amended]. THIS FORMAT MATERIALISES AND THE OTHERS DO NOT, by
            # construction rather than by oversight: an ANSWER is one object, so it cannot be
            # written until the last record has gone past. `--format jsonl` remains the
            # streaming form and is what a 100,000-run history should be read with — the
            # docstring above is about those paths and still holds for them.
            collected.append(r)
            return
        if args.format == "yaml":
            sys.stdout.write(_yaml_entry(r))
        elif args.format == "jsonl":
            sys.stdout.write(json.dumps(r) + "\n")
        else:
            # `shown` was incremented above, so this is False for the first record written
            # and True after -- the streaming equivalent of `_timeline`'s `i > 0`.
            sys.stdout.write(_timeline_entry(r, separator=shown > 1))

    if args.format == "yaml":
        # ONCE, by the command rather than the renderer: streaming writes each record as
        # it passes, and a banner emitted per record is not a banner. [ADR-0017 R-4] is why
        # `json` is not in this branch: the payload is the whole of stdout.
        sys.stdout.write(_yaml_header())

    for rec in _stream(path):
        if rec is None:
            bad += 1
            continue
        if _is_start(rec):
            # J-13. COUNTED AS IT IS SKIPPED. Filtering it out of the records is right; saying
            # nothing about it anywhere is what made an interrupted run invisible.
            pending.add(str(rec.get("run_uid")))
            # NOT A RUN THAT HAPPENED. `log` streams raw rather than through `_load` or
            # `_counted` — it is the one reader that must not materialise — so the filter
            # those two apply has to be repeated here. Without it every completed run
            # printed an empty entry before its real one and the tally said 6 of 6 for
            # three runs.
            continue
        total += 1
        # J-13. ITS ENDING IS ON RECORD, so it is not unfinished. `discard` rather than
        # `remove` because a record whose start line is missing — a history rotated between the
        # two appends — is not an error to raise at a reader.
        pending.discard(str(rec.get("run_uid")))
        # THE NAME IS TESTED ON ITS OWN, before `--failed` narrows anything. Combined,
        # `--script build --failed` over a project whose `build` never failed reported
        # "nothing matched 'build'" and exited 1 — which is false twice over: the name was
        # right, and no failures is the good answer.
        if named_hit(rec):
            named_hits += 1
        if not matches(rec):
            continue
        # J-14. PAST THE SELECTORS AND BEFORE THE LIMIT, which is the one place that scopes
        # these two to what the caller asked about rather than to what fitted in the window.
        matching += 1
        n_failed += rec.get("status") == "failed"
        if keep is not None:
            keep.append(rec)
        else:
            emit(rec)

    if keep is not None:
        for rec in keep:
            emit(rec)

    if args.format == "json":
        print(
            json.dumps(
                _log_answer(
                    path,
                    records=collected,
                    total=total,
                    failed=n_failed,
                    unreadable=bad,
                    selectors=_log_selectors(args),
                    # None WHEN NOTHING WAS NAMED, rather than True — see `_log_answer`.
                    matched_any=bool(named_hits) if named else None,
                    matching=matching,
                    unfinished=len(pending),
                ),
                indent=2,
            )
        )
    # THE TALLY STAYS ON STDERR IN EVERY FORMAT, [ADR-0017 R-4]: a caller parsing stdout never
    # meets it, and a person reading the JSON in a terminal still gets the same summary line
    # they get from every other rendering.
    print(
        f"# {shown} of {total} run(s) from {path}"
        # J-14. THE DENOMINATOR IS NAMED ONLY WHEN IT DIFFERS FROM WHAT WAS SHOWN, so an
        # ordinary page prints the clause it has printed since 0.6.0 and a truncated one stops
        # leaving a reader to assume the failures were among the records in front of them.
        + (
            ""
            if not n_failed
            else f"; {n_failed} FAILED"
            if matching == shown
            else f"; {n_failed} FAILED of {matching} matching"
        )
        + (f"; {bad} unreadable line(s) skipped" if bad else "")
        # J-13. NAMED ON THE PAGE AS WELL, conditional like the two clauses above it — so a
        # history with nothing unfinished prints the line it has always printed, and J-06's
        # defect in reverse (a payload carrying what the text does not say) is not created
        # here while fixing the forward one.
        + (f"; {len(pending)} started with no ending on record" if pending else ""),
        file=sys.stderr,
    )
    # A NAME THAT MATCHED NOTHING IS A FINDING, and this printed an empty timeline and exited
    # 0 — so `--script buidl_labels` looked exactly like a project that had never run it.
    # `show <target>` has always exited 1 for the same question; one CLI cannot answer it two
    # ways (ledger L-81).
    #
    # `--failed` IS NOT IN THIS FAMILY, deliberately: it selects a CLASS, and "no failed runs"
    # is the good answer rather than an absent one. Only the flags that NAME a record count.
    if named and not named_hits:
        print(
            f"# nothing matched {', '.join(repr(n) for n in named)} — the name may be wrong."
            f"\n#   `python -m runprov show` with no target lists every script this "
            f"project has run.",
            file=sys.stderr,
        )
        return 1
    return 0


def _counted(
    path: pathlib.Path,
    bad: list[int],
    scan: _InFlightScan | None = None,
) -> typing.Iterator[dict[str, typing.Any]]:
    """The history, streamed, with unreadable lines counted into `bad` rather than dropped.

    `scan` IS FED THE START LINES THIS DROPS, which is the whole of A-20. The project page
    streams every record here and throws the `started` lines away; `_report_in_flight` then
    read the entire file a SECOND time to recover exactly those lines. Measured on a 56 MB,
    60,000-run history: 2.63 s for the pass that has to happen, 1.61 s for the one that did
    not — **+61%**, on a file that by design only grows.

    ONLY PASS A SCAN TO A PASS THAT IS EXHAUSTED. `show <target>` stops early at `--limit`
    and `--stale` makes a second pass of its own; a scan fed by either would be missing
    records and would report runs as unfinished that ended further down the file.
    """
    for rec in _stream(path):
        if rec is None:
            bad[0] += 1
            continue
        if scan is not None:
            scan.saw(rec)
        if not _is_start(rec):
            yield rec


#: How many in-flight markers `show` prints before summarising the rest.
#:
#: The banner is stderr context above the page the reader actually asked for, so it must not
#: be able to bury it. `--exit-code`'s FAILING list caps at five for the same reason; ten here
#: because a marker line is one run rather than one artifact, and a busy machine legitimately
#: has several in flight at once.
MARKERS_SHOWN = 10


class _InFlightScan:
    """Which runs started and have no ending on record — accumulated as the page streams.

    THE SECOND PASS IS THE DEFECT THIS REMOVES. `_report_in_flight` used to call
    `_unfinished`, which streamed the whole history again to recover the `started` lines the
    project page's own pass had just dropped. Measured on a 56 MB, 60,000-run history:
    2.63 s for the pass that must happen against 1.61 s for the one that need not, **+61%**,
    on a file that by design only grows. `bad[0]` was already accumulated this way and the
    docstring for `_unfinished`'s `finished` out-parameter already gave the reason — "an
    out-parameter rather than a second return value, and rather than a second read of the
    history".

    MARKERS AT CONSTRUCTION, RECORDS AS THEY STREAM, AND THAT ORDER IS LOAD-BEARING. It is
    A-05's fix and folding the passes must not undo it. Reading the history FIRST leaves a
    window: a run that completes between the two reads is in the open set (its record was
    appended after the pass hit EOF) and has no marker (`_clear_in_flight` has unlinked it),
    so it is announced as a SIGKILL death while its `status: ok` record sits in the file just
    read. Constructing this before the page's pass begins keeps markers at T1 and records at
    T2, exactly as before — the remaining window belongs to a run that STARTS between them,
    which has no marker and a live pid, and the liveness check below gets that right.

    WHAT IS HELD IS THE FINDING, NOT THE HISTORY. A start adds its uid, the matching record
    removes it, so the open set is bounded by how many runs were interrupted rather than by
    how many ever ran — on a healthy history it is empty at every point in the pass. That is
    why `show` can still stream: a reader needing every uid at once would give that back.
    """

    def __init__(self, incomplete: pathlib.Path) -> None:
        self.markers = {str(m.get("run_uid")): m for m in in_flight(incomplete)}
        self.open: dict[str, dict[str, typing.Any]] = {}
        self.finished: set[str] = set()

    def saw(self, rec: dict[str, typing.Any]) -> None:
        uid = str(rec.get("run_uid") or "")
        if not uid:
            return  # a v1 record predates run_uid and cannot be paired either way
        if _is_start(rec):
            self.open[uid] = rec
        else:
            self.open.pop(uid, None)
            # KNOWING WHICH RUNS ENDED is what lets a marker left behind by one of them be
            # ignored — `_clear_in_flight` tolerates an OSError, so a marker can outlive its
            # run. See `pending`.
            self.finished.add(uid)

    def consume(self, path: pathlib.Path) -> None:
        """Stream the history myself. For callers with no pass of their own to ride on."""
        if not path.is_file():
            return
        for rec in _stream(path):
            if rec is not None:
                self.saw(rec)

    def pending(self) -> list[dict[str, typing.Any]]:
        """Every run to report, each carrying the state to print for it."""
        # PAIRED BY A SET, NOT BY CONSUMING THE DICT. This popped from a defensive COPY of
        # `self.markers`, and the copy was a line no mutation could tell from its absence —
        # popping from `self.markers` itself produces identical output, because `report()` is
        # the only caller and calls this once. That is the shape that traps the NEXT caller:
        # a method that reads like a query and consumes its receiver. Computing the paired
        # uids up front removes the mutation entirely, so `pending()` is idempotent by
        # construction rather than by a copy somebody has to remember to keep.
        paired = {str(rec.get("run_uid")) for rec in self.open.values()}
        out: list[dict[str, typing.Any]] = []
        for rec in self.open.values():
            uid = str(rec.get("run_uid"))
            marker = self.markers.get(uid)
            # NO MARKER IS NOT A DEATH. It used to default to INTERRUPTED, which conflated
            # three different situations: the marker was cleaned away, it was never written,
            # or the run is STILL GOING. `_mark_in_flight` tolerates an OSError, warns and
            # lets the run continue, so the package itself produces "start line present,
            # marker absent, process alive" — and `.incomplete` is documented as deletable.
            # The start line carries `pid` and `host` precisely so this question can be
            # answered; nobody was asking it.
            #
            # WHICHEVER SOURCE CAN ANSWER, and neither one is privileged. Both records are
            # written by the same process, so their `pid` and `host` normally agree and
            # either gives the same state — which is why "prefer the marker" was a branch NO
            # mutation could distinguish, in either direction: always taking the marker and
            # never taking it both left the suite green.
            #
            # They diverge only when one of the two is DAMAGED — parses, but has lost its
            # `pid`. Then that source reports `?` while the other holds the answer, and `?`
            # was printed on a line carrying a live pid on this host: "we could not look"
            # rendered as a verdict with the answer beside it, on the same line. Measured
            # both ways round, because damage is not particular about which file it hits.
            #
            # The start line is tried first because it is the append-only record; where both
            # can answer they agree, so the order is a tie-break and not a judgement.
            state = show_mod.liveness(rec)
            if state == show_mod.UNTELLABLE and marker is not None:
                state = marker["state"]
            out.append({**rec, "state": state})
        # A MARKER THE HISTORY HAS NEVER HEARD OF is still worth printing: it is what a run
        # killed between its marker and its start line leaves.
        #
        # A MARKER FOR A RUN THAT FINISHED IS NOT. Every such marker used to be announced as
        # "INTERRUPTED ... ran no ending code at all", about a run whose `status: ok` record
        # was in the file this function had JUST READ. The refutation was already in hand and
        # was being discarded: the same pass that pairs starts with endings knows every uid
        # that ended.
        # `paired` IS THE DEDUPLICATION, and it is what the `pop` used to do implicitly. A run
        # with BOTH a start line and a marker appears once, from the walk above. Without it,
        # `show` prints `# 2 run(s) STARTED` above the identical row twice — measured — and
        # the finding tally doubles with it, which is L-99's "a count that looks like
        # evidence" in a reader three commits old.
        out += [
            m for uid, m in self.markers.items() if uid not in paired and uid not in self.finished
        ]
        return out

    def report(self) -> None:
        """Say which runs started and have no ending on record. Nothing if there are none.

        DRIVEN BY THE HISTORY, ENRICHED BY THE MARKERS, and that order is the whole design.
        The append-only history is what makes the finding permanent: a `started` line whose
        `run_uid` never gets a record is evidence that survives anything short of rewriting
        the record, including deleting `.incomplete`. The markers add the one thing the
        history cannot know — whether the process is still alive — so a run in progress reads
        as RUNNING rather than as a death.

        A start with no marker and no ending is INTERRUPTED without further enquiry: either
        the marker was cleaned away or it was never written, and both mean the same thing.
        """
        pending = self.pending()
        if not pending:
            return

        out = [f"# {len(pending)} run(s) STARTED with no ending recorded:"]
        # NEWEST FIRST AND CAPPED. Markers accumulate on any machine that has had a SIGKILL.
        # Measured with 10,000 of them: 10,004 lines of banner above a FOUR-line page. The
        # page is what the reader asked for and it was the part they could not see.
        #
        # THE COST IS THE BANNER, NOT THE SECONDS: 0.9 s wall for the whole command at 10,000
        # markers, against 0.28 s empty. So this caps what is PRINTED and leaves the read
        # alone — the counts below stay exact because they are computed over all of `pending`.
        ordered = sorted(pending, key=lambda x: str(x.get("started_utc", "")), reverse=True)
        for r in ordered[:MARKERS_SHOWN]:
            where = "" if r.get("state") != show_mod.UNTELLABLE else f"  on {r.get('host', '?')}"
            out.append(
                f"#   {r.get('state', '?'):12} {r.get('script', '?')!s:20} "
                f"{r.get('started_utc', '?')}  pid {r.get('pid', '?')}{where}"
            )
        if len(ordered) > MARKERS_SHOWN:
            out.append(
                f"#   … and {len(ordered) - MARKERS_SHOWN} more, oldest not shown. "
                f"`runprov prune` clears the ones that describe nothing running."
            )
        print("\n".join(out), file=sys.stderr)
        n = sum(1 for r in pending if r.get("state") == show_mod.INTERRUPTED)
        if n:
            print(
                f"#   {n} of them ran no ending code at all — a SIGKILL, the OOM killer, a "
                f"power loss or a node failure.\n"
                f"#   Anything they wrote is on disk and is NOT in the history: it looks "
                f"exactly like a completed run's output.",
                file=sys.stderr,
            )


def _report_in_flight(path: pathlib.Path) -> _InFlightScan:
    """The whole thing, for a caller with no streaming pass of its own to ride on.

    DERIVED FROM THE HISTORY BEING READ, so `--log somewhere/else.jsonl` reports what belongs
    to THAT history rather than to whichever project this shell stands in. Called even when
    the file does not exist, because a marker beside a missing history is not "nothing
    recorded" — it is a run that never got to write one.
    """
    scan = _InFlightScan(path.parent / ".incomplete")
    scan.consume(path)
    scan.report()
    # RETURNED, so the caller can read the markers this already loaded rather than opening the
    # directory a second time — and so this stays the one entry point. After A-20 folded the
    # pairing into the page's own pass, the only caller left was the missing-history branch;
    # a function alive only because tests call it is the decoration `_unfinished` was deleted
    # for, and the fix there was deletion because nothing needed it. Here something does.
    return scan


def _forget(
    directory: pathlib.Path,
    older_than: float | None = None,
    *,
    other_hosts: bool = False,
    dry: bool = False,
) -> int:
    """Do the prune and say what happened. Shared by `prune` and `show --forget-markers`.

    ONE DECISION PROCEDURE, TWO DOORS. The flag on `show` is not a second implementation —
    it is this function, so the two cannot drift into deleting different things, and there
    is one containment check to review rather than two.
    """
    p = prune_mod.plan(directory, older_than=older_than, other_hosts=other_hosts)
    gone, problems = (None, []) if dry else prune_mod.apply(p)
    print(prune_mod.render(p, gone, directory), file=sys.stderr)
    for msg in problems:
        print(f"#   {msg}", file=sys.stderr)
    return 1 if problems else 0


def _completed(path: pathlib.Path) -> typing.Iterator[dict[str, typing.Any]]:
    """The history without its in-flight markers. ADR-0014.

    A run writes TWO lines: a start marker when it begins, and the record when it ends. Both
    are runs to `select`, and the first version of `diff` happily compared a marker with a
    record — reporting that the schema, the packages and the observation block all differed,
    which they did, because one of the two was not a finished run at all. Every dimension was
    a lie with a true-looking shape.

    Filtered on `START_SCHEMA` rather than on a guessed string or on a missing key, so a
    change to the marker's shape cannot quietly reopen this.
    """
    for record in _stream(path):
        if record is None or record.get("schema") == START_SCHEMA:
            continue
        yield record


def _impact(args: argparse.Namespace) -> int:
    """ADR-0015. Exit 1 when something derives from it, 0 when nothing recorded does.

    The 1 is not "something is wrong" in the usual sense — it is "there is work to do", which
    is the same thing to a caller deciding whether to rebuild. Exit 2 when the question could
    not be asked at all: no history, or a path that is neither a file nor a digest.
    """
    project = active()
    log = pathlib.Path(args.log) if args.log else project.resolved_run_log()
    if not log.is_file():
        # [ADR-0017 R-15]. The question was formed and could not be carried out.
        if args.format == "json":
            print(
                json.dumps(
                    impact_mod.payload(None, cannot_check=f"no run history at {log}"), indent=2
                )
            )
        print(f"impact: no run history at {log}", file=sys.stderr)
        return 2

    target = str(args.target)
    if re.fullmatch(r"[0-9a-f]{16,64}", target):
        digest = target
    else:
        path = pathlib.Path(target)
        if not path.is_file():
            print(f"impact: {target} is not a file and not a digest", file=sys.stderr)
            return 2
        # HASHED NOW, because the question is about the bytes that are there — "if I change
        # this file" means the file as it stands, and a digest read out of the history would
        # answer about a version that may already be gone.
        digest = hashing.sha256(path)

    consumers: dict[str, list[str]] = {}
    names: dict[str, str] = {}
    outputs_by: dict[str, list[str]] = {}
    # I-01. THE OUT-PARAMETER WAS PASSED `None` AND THE COUNT THROWN AWAY. `_lineage` computes
    # it either way — `records()` does `_counted(source, bad if bad is not None else [0])` — so
    # this was a fact already in hand and discarded, while `lineage`, `log` and `show` all took
    # it and disclosed it.
    counted: list[int] = [0]
    graph = _lineage(log, counted, names, consumers, outputs_by)

    unregistered = 0
    runs = 0
    watch_drops = 0
    for record in _completed(log):
        runs += 1
        unregistered += len(record.get("unregistered_reads") or [])
        # C-06 of Audit C: the blind-spot tally counted only the reads the runs managed to
        # REPORT, and was silent about the ones the watch dropped before they could be.
        # SUMMED ACROSS RUNS, which makes it drop EVENTS rather than distinct paths — D-07 of
        # Audit D. The per-run field is a distinct-path floor; adding those up is not one, and
        # the line that prints it says so now instead of claiming a floor it cannot support.
        watch_drops += (record.get("observation") or {}).get("unregistered_watch_truncated") or 0

    beyond: list[int] = [0]
    steps = impact_mod.walk(
        digest, consumers, graph["edges"], names, outputs_by, args.depth, beyond
    )
    # BY KEYWORD, NOT BY POSITION. `Chain` has grown three defaulted fields in this audit and
    # each was appended in the middle of the list — I-04's two landed between `watch_drops` and
    # I-01's `unreadable`, which silently re-assigned THREE arguments in this call: the depth
    # limit received the unreadable count, `beyond_depth` received the flag, and `unreadable`
    # received the beyond count. The page then reported a truncation on a walk that cut nothing.
    # Keywords make the order of the structure irrelevant to this call, which is the only fix
    # that does not have to be re-made every time a field is added.
    chain = impact_mod.Chain(
        digest=digest,
        seeds=consumers.get(digest, []),
        steps=steps,
        unregistered=unregistered,
        runs_examined=runs,
        watch_drops=watch_drops,
        unreadable=counted[0],
        depth_limit=args.depth,
        beyond_depth=beyond[0],
    )
    if args.format == "json":
        # NOTHING ELSE ON STDOUT (R-4). Both of this command's diagnostics — the missing history
        # and the target that is neither a file nor a digest — already go to stderr and return
        # before this point, so a caller parses what it is handed instead of stripping a line
        # first, which is how a caller comes to strip the wrong one.
        #
        # J-20. A TRUNCATED WALK EXITS 2 AND MUST SAY SO IN THE PAYLOAD. It did not: the object
        # was built here, above the fold that decides the code, so `--depth 0` printed
        # `truncated: true` beside `cannot_check: null` and exited 2 — a payload asserting
        # nothing went wrong, attached to an exit code saying something did. The ratchet's own
        # comment calls that worse than no payload, and README promised the opposite.
        #
        # READ OFF `chain.truncated`, THE SAME PROPERTY THE EXIT CODE READS, twelve lines below.
        # Two expressions for one fact is H1-6, and here the fact is the verdict itself.
        print(
            json.dumps(
                impact_mod.payload(
                    chain,
                    cannot_check=(
                        f"the walk stopped at depth {args.depth} and "
                        f"{chain.beyond_depth} run(s) beyond it were not followed"
                        if chain.truncated
                        else None
                    ),
                ),
                indent=2,
            )
        )
    else:
        for line in impact_mod.render(chain, pathlib.Path(project.root)):
            print(line)
    # R-6. THE EXIT CODE IS BELOW THE FORMAT, not inside either branch: `--format json` is a
    # rendering choice, and a gate that changed verdict because it asked for a parseable answer
    # would make the two formats two different commands.
    if chain.truncated:
        # C-07 of Audit C. A-09 named two halves and only the sentence was fixed: `--depth 0`
        # empties `steps` while `seeds` stays non-empty, and this returned 0 — the code that
        # means "checked, and nothing is wrong", identical to the code for a file nothing ever
        # read. A pre-overwrite guard written as `runprov impact ref.fa --depth 0 || abort`
        # goes green and the reference is overwritten with three derived artifacts in the
        # history. 2 is COULD NOT CHECK, which is exactly what a truncated walk is.
        return CANNOT_CHECK
    return 1 if chain.steps else 0


def _diff(args: argparse.Namespace) -> int:
    """ADR-0014. Exit 0 ONLY when every dimension was comparable and identical.

    An incomparable dimension exits non-zero like a difference does, because a gate that goes
    green while half the comparison was impossible is the vacuous pass in a new place.
    """
    project = active()
    # A-21. `select`'s run_uid bucket tests `startswith(target)`, and every string starts with
    # `""` — so an empty address matched EVERY record and `runprov diff ""` compared the last
    # two runs in the history regardless of script, printing a confident change table for two
    # runs the caller never named. An address that identifies nothing is a usage mistake.
    for value, which in ((args.a, "first"), (args.b, "second")):
        if value is not None and not str(value).strip():
            print(f"diff: the {which} run address is empty", file=sys.stderr)
            return 2
    log = pathlib.Path(args.log) if args.log else project.resolved_run_log()
    if not log.is_file():
        # [ADR-0017 R-15]. The question was formed and could not be carried out.
        if args.format == "json":
            print(
                json.dumps(
                    diff_mod.payload(None, cannot_check=f"no run history at {log}"), indent=2
                )
            )
        print(f"diff: no run history at {log}", file=sys.stderr)
        return 2

    # ONE ADDRESS MEANS THE LAST TWO RUNS OF IT, which is the question actually asked: "what
    # changed since last time". The first version required two addresses, and
    # `runprov diff align align` resolved both to the same run and reported everything
    # unchanged — a comparison of a record with itself, presented as a finding.
    if args.b is None:
        # I-05. COUNTED, not silently skipped. `_counted` is `_completed` plus the tally —
        # the same filter, the same start-marker exclusion — so this is a swap rather than a
        # second way of reading the history.
        seen: list[int] = [0]
        matches = show_mod.select(_counted(log, seen), args.a, limit=2)
        unreadable = seen[0]
        if len(matches) < 2:
            # [ADR-0017 R-15]. THE HISTORY WAS READ and the address does not name two runs.
            # An answer, not a usage mistake: the invocation was well formed and the data does
            # not support it.
            if args.format == "json":
                print(
                    json.dumps(
                        diff_mod.payload(
                            None,
                            cannot_check=(
                                f"{args.a!r} matches {len(matches)} run(s) in {log}; name two runs "
                                "explicitly to compare across scripts"
                            ),
                        ),
                        indent=2,
                    )
                )
            print(
                f"diff: {args.a!r} matches {len(matches)} run(s) in {log}; "
                "name two runs explicitly to compare across scripts",
                file=sys.stderr,
            )
            return 2
        picked = matches[-2:]
    else:
        # NOT COUNTED HERE, and that is the honest answer rather than a gap. I-05 is a defect
        # of the SELECTION: the one-address form lets the history choose the pair, so a line it
        # could not read shifts that pair silently. Naming two runs resolves each by address —
        # an unreadable line shadows neither, and a named run that is unreadable matches
        # nothing and already exits 2 with a message. Counting it here would also mean tallying
        # the same file once per target, and a doubled figure is its own defect: a history with
        # two torn lines once reported "4 unreadable line(s) skipped".
        unreadable = 0
        picked = []
        for target in (args.a, args.b):
            # `show.select` RESOLVES THE ADDRESS, and reusing it is the point: a second
            # addressing scheme in one tool is how two commands come to disagree about which
            # run the user meant.
            matches = show_mod.select(_completed(log), target, limit=1)
            if not matches:
                # [ADR-0017 R-15]. A named address matched no run. The history was read.
                if args.format == "json":
                    print(
                        json.dumps(
                            diff_mod.payload(
                                None, cannot_check=f"nothing matches {target!r} in {log}"
                            ),
                            indent=2,
                        )
                    )
                print(f"diff: nothing matches {target!r} in {log}", file=sys.stderr)
                # J-25, AND IT IS NOT A PREFERENCE BETWEEN CONVENTIONS. **L-81, ratified
                # 2026-09-01 and quoted at the top of this file, puts "a named target or filter
                # that matched nothing" in the 1 family** — and `show <target>` and
                # `log --script` both implement it. `diff` returned 2, so one of the ten
                # commands contradicted a contract this package had written down, and a consumer
                # reading that contract got the wrong answer for it.
                #
                # THE OTHER THREE STATES STAY AT 2, and the line is L-81's own: 1 is *checked,
                # and something IS wrong*; 2 is *could not check*. A named address matching
                # nothing is a finding about the history. An address matching ONE run, two
                # addresses resolving to one run, and no history at all are all cases where no
                # comparison could be FORMED — which is what 2 means. Matching one run is not
                # matching nothing.
                #
                # Taylor ruled on 2026-09-30, on the evidence that L-81 already answered it.
                return 1
            picked.append(matches[-1])
        if picked[0].get("run_uid") == picked[1].get("run_uid"):
            # [ADR-0017 R-15]. Both addresses resolved, to one run. Nothing to compare is an ANSWER.
            if args.format == "json":
                print(
                    json.dumps(
                        diff_mod.payload(
                            None,
                            cannot_check=(
                                f"{args.a!r} and {args.b!r} both resolve to the same "
                                f"run ({str(picked[0].get('run_uid'))[:12]}); there is "
                                "nothing to compare"
                            ),
                        ),
                        indent=2,
                    )
                )
            print(
                f"diff: {args.a!r} and {args.b!r} both resolve to the same run "
                f"({str(picked[0].get('run_uid'))[:12]}); there is nothing to compare",
                file=sys.stderr,
            )
            return 2

    comparison = diff_mod.build(picked[0], picked[1], unreadable)
    if args.format == "json":
        # NOTHING ELSE ON STDOUT (R-4). Every diagnostic this command emits already goes to
        # stderr — the empty address, the missing history, the address that matches nothing and
        # the two that match the same run — so a caller parses what it is handed instead of
        # stripping a line first, which is how a caller comes to strip the wrong one.
        print(json.dumps(diff_mod.payload(comparison), indent=2))
    else:
        for line in diff_mod.render(comparison):
            print(line)
    # READ OFF THE STRUCTURE. The fold used to live here, beside a renderer that states the
    # same three words — two spellings of one verdict, which is the defect ADR-0017 R-1 is
    # about, in the one place where disagreeing means the table and the exit code differ.
    return 0 if comparison.settled else 1


def _resources(args: argparse.Namespace) -> int:
    """ADR-0013. What a run consumed, in the syntax of wherever it is going next.

    Exit 2 when there is nothing to report — no history, no matching run, or a run recorded
    before this block existed. A renderer that printed a request from no measurement would be
    the worst possible output, because it looks exactly like a measured one.
    """
    project = active()
    log = pathlib.Path(args.log) if args.log else project.resolved_run_log()
    if not log.is_file():
        # [ADR-0017 R-15]. AN ANSWER, not a bad invocation: it looked where it was told and
        # there is nothing to read. `resources` has its own guard rather than the shared one,
        # so this is its own line to fix.
        if args.format == "json":
            print(
                json.dumps(
                    resources_mod.payload(log, None, None, cannot_check=f"no run history at {log}"),
                    indent=2,
                )
            )
        print(f"resources: no run history at {log}", file=sys.stderr)
        return 2

    found = None
    for line in log.read_text(encoding="utf-8", errors="replace").splitlines():
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if not isinstance(record, dict) or "resources" not in record:
            continue
        if args.script and record.get("script") != args.script:
            continue
        found = record  # the LAST match: the most recent run is the one being sized

    if found is None:
        which = f" for {args.script!r}" if args.script else ""
        # [ADR-0017 R-15] AND THE MORE INTERESTING OF THE TWO. The history is there and was
        # read; no run in it carries the block. This function's own docstring says a renderer
        # printing a request from no measurement "would be the worst possible output, because it
        # looks exactly like a measured one" — a payload of nulls with `cannot_check` set is the
        # machine-readable form of refusing to.
        if args.format == "json":
            print(
                json.dumps(
                    resources_mod.payload(
                        log,
                        None,
                        None,
                        cannot_check=f"no run{which} in {log} recorded a resources block",
                    ),
                    indent=2,
                )
            )
        print(f"resources: no run{which} in {log} recorded a resources block", file=sys.stderr)
        return 2

    m = resources_mod.from_record(found["resources"])
    # J-16. ACCEPTED FOR UNIFORMITY AND SAID TO BE IGNORED, which is the treatment `verify`
    # gives `--log` four hundred lines below. `--margin` scales a REQUEST for a scheduler, and
    # `tsv`, `json` and the default page are the MEASUREMENT — so there is an honest "ignored"
    # semantics to announce here, unlike `log --unreadable`, which is refused precisely because
    # it has none. Measured before this: output byte-identical with and without the flag, and
    # nothing said, so a reader could believe their headroom had been applied.
    scaling = args.format in ("slurm", "k8s")
    if args.margin is not None and not scaling:
        print(
            f"# NOTE: --margin {args.margin:g} is accepted for uniformity and ignored by "
            f"`--format {args.format}`,\n"
            f"#   which reports what was MEASURED. The margin scales a request — it applies to "
            f"`--format slurm`\n#   and `--format k8s`, where the multiplier is printed on the "
            f"line it was applied to.",
            file=sys.stderr,
        )
    margin = 1.5 if args.margin is None else args.margin
    if args.format == "tsv":
        header, row = resources_mod.snakemake_row(m)
        print("\t".join(header))
        print("\t".join(row))
    elif args.format == "slurm":
        print("\n".join(resources_mod.render_slurm(m, margin)))
    elif args.format == "k8s":
        print("\n".join(resources_mod.render_k8s(m, margin)))
    elif args.format == "json":
        # [ADR-0017 R-4]. `--margin` is deliberately absent from the payload: it scales a
        # REQUEST for a scheduler, and this is the MEASUREMENT. A consumer applying its own
        # headroom needs the number that was measured, not one already multiplied by a default.
        print(json.dumps(resources_mod.payload(log, m, found), indent=2))
    else:
        print("\n".join(_resources_text(found, m)))
    return 0


def _resources_text(record: dict[str, typing.Any], m: resources_mod.Measurement) -> list[str]:
    """The default view: the numbers, in units a person reads, and what was NOT measured.

    THE EMISSION POINT [L-03], and this renderer is the one the row's own list did not contain.
    L-03 named `log`, `show`, `show <target>`, `report` and `diff`; the PROPERTY that replaced
    its site list found a sixth here on the first run — `script` and `run_id` are interpolated
    into the header line straight off the record. That is the whole argument for holding this
    with a property rather than with a list of renderers: the list was wrong about the count in
    the same way K-16's was, three rows earlier.
    """
    mib = resources_mod.MIB

    def size(value: int | None) -> str:
        return "not measured" if value is None else f"{value / mib:.1f} MiB"

    out = [f"{record.get('script', '?')}  ({record.get('run_id', '?')})", ""]
    out.append(f"  measured by  {m.source}")
    out.append(f"  wall         {m.wall_seconds:.2f} s")
    out.append(
        "  cpu          " + ("not measured" if m.cpu_seconds is None else f"{m.cpu_seconds:.2f} s")
    )
    cores = resources_mod.mean_cores(m)
    if cores is not None:
        out.append(f"  mean cores   {cores:.2f}")
    out.append(f"  peak memory  {size(m.max_rss_bytes)}")
    if m.source == "getrusage" and m.max_rss_bytes is not None:
        out.append(f"               FLOOR — {resources_mod.FLOOR_NOTE}")
    for note in m.unavailable:
        out.append(f"  not measured {note}")
    return printable_lines(out)


def _report(args: argparse.Namespace) -> int:
    """ADR — one artifact, one page. Exit follows the verdict, so it can gate too.

    2 when the artifact is not there, because a page about a file that does not exist is not
    a report, it is a mistake with a header on it.
    """
    artifact = pathlib.Path(args.artifact)
    if not artifact.is_file():
        print(f"report: {artifact} is not a file", file=sys.stderr)
        return 2
    project = active()
    log = pathlib.Path(args.log) if args.log else project.resolved_run_log()
    if args.log and not log.is_file():
        # I-16, NARROWED. `report` was the only one of five commands reading a history that
        # accepted one that was not there — `impact`, `diff`, `chain` and `log` each exit 2 and
        # name the path — and this said NOTHING at all, so a mistyped `--log` produced a page
        # reading "NOT FOUND in the run history supplied" and a reader took it for *there is no
        # such run*.
        #
        # THE EXIT CODE IS DELIBERATELY NOT MOVED, and the sibling comparison that suggested
        # moving it is unfair. `impact`, `diff`, `log` and `chain` CANNOT answer without a
        # history — it is their subject. `report` can: the pin is self-contained and travels with
        # the file, which is ADR-0007's second hook, and `limits.run_not_found` exists to carry
        # this absence as a fact. Exiting 2 here would remove a documented capability and break
        # the test that demonstrates R-8 through exactly this route.
        #
        # So the finding is the SILENCE, and only the silence is fixed. Whether the code should
        # also move is a behaviour change on a documented path and is Taylor's call, not an
        # applier's — recorded in the ledger as open.
        print(
            f"report: no run history at {log} — reporting from the artifact's pin alone",
            file=sys.stderr,
        )
    result = report_mod.render(artifact, pathlib.Path(project.root), log)
    if args.format == "json":
        # NOTHING ELSE ON STDOUT. Every diagnostic this command emits already goes to stderr —
        # the refusal above is the only one — so a caller parses what it is handed instead of
        # stripping a line first, which is how a caller comes to strip the wrong one.
        print(json.dumps(report_mod.payload(result.report), indent=2))
    else:
        for line in result.lines:
            print(line)
    # THE EXIT CODE DOES NOT MOVE WITH THE FORMAT. `--format json` is a rendering choice, not a
    # different question, and a gate that answers differently depending on how it was asked to
    # print is a gate nobody can reason about.
    #
    # I-10. THREE CODES, DERIVED FROM `verify`'s OWN VOCABULARY. `0 if result.ok else 1` folded
    # five verdicts into two, so `NO PIN` and `UNVERIFIABLE` — which `verify` reports as 2 for
    # the same artifact in the same second — came back as 1, "checked and wrong". ADR-0007 was
    # written to refuse exactly that: *"Collapsing them turns 'your provenance is not running at
    # all' into 'your results are stale', and sends somebody to re-run a pipeline over a problem
    # that re-running cannot touch."* `check` was fixed for this in A-08 — "three outcomes,
    # three codes" — and this command was not, though its own docstring already promises 2 for
    # an absent artifact.
    #
    # DERIVED, NOT ENUMERATED. `FAILING` is the package's own name for "checked and wrong", so a
    # verdict that is neither `OK` nor in it falls to CANNOT_CHECK — which means a SIXTH verdict
    # added to `verify` defaults to the safe answer here rather than being silently folded into
    # "wrong". Hand-listing the two would have put the scope pattern in the exit code.
    if result.report.verdict == OK:
        return 0
    if result.report.verdict in FAILING:
        return 1
    return CANNOT_CHECK


def _diag(message: str) -> None:
    """One of `gate`'s stderr diagnostics, escaped [L-23].

    **STDOUT AND STDERR ARE ONE STREAM IN A CI LOG**, which is the whole of this row. K-21's
    guard docstring promises *every field that came from outside this source file is escaped* —
    a universal the code did not have, because stdout was escaped and stderr was not. Reproduced
    with a history whose FILENAME carries a newline: stdout correctly showed zero forged lines
    and stderr emitted a bare `GATE: MET (exit 0)` on its own line, inside an invocation that
    exited 2.

    ALL FOUR OF `gate`'s DIAGNOSTICS GO THROUGH HERE, and the fourth is the one the row's own
    list missed: `gate: {exc}`, where the forgery is in the POLICY FILENAME rather than in the
    history's — the same list-instead-of-a-derivation shape L-03 criticises, one row later. A
    helper is cheaper than a list of four and covers the fifth.

    `gate` ONLY, DELIBERATELY. R-4 gives stdout to the answer, so these four lines are the whole
    of what this command says on the other stream; a package-wide stderr transform is a
    different decision, taken somewhere a reader of `_gate` would not have to find it.
    """
    print(printable(message), file=sys.stderr)


def _gate(args: argparse.Namespace) -> int:
    """ADR-0018. A written policy, checked against the runs that were actually recorded.

    THREE EXIT CODES, UNCHANGED [R-2]: 0 every rule checked and met, 1 a rule violated, 2 a rule
    that could not be checked. L-81's vocabulary, and the code is READ OFF the assessment rather
    than decided a second time here — `report`'s I-10 is the row where a second expression folded
    the verdicts and sent a reader to re-run a pipeline over a problem re-running cannot touch.

    A POLICY THAT CANNOT BE READ IS SILENT ON STDOUT [ADR-0017 R-15], and that is a decision
    rather than an omission. R-15 says silence means the INVOCATION was wrong, and the policy IS
    the question: without one there is nothing to answer about, which is the same class as
    `report` handed an artifact that is not there. A MISSING HISTORY IS THE OTHER CLASS — the
    question was formed, the command looked where it was told, and its finding is that there is
    nothing to read, so that one carries a payload.

    BOTH PATHS ARE `_posix`-SPELLED, AND A DERIVED GUARD IS WHY [K-12]. Renaming `source` to
    `policy_path` brought the field inside
    `test_no_recorded_path_is_spelled_with_a_bare_str`, whose scope is every key and keyword
    ending in `path` — so it fired on `policy_path=str(...)` the moment the rename landed. That
    is the guard working: a path in a filed payload is read on a machine other than the one that
    wrote it, which is T-08's rule and `lineage`'s `path: path.as_posix()` one command over.
    `history` is spelled the same way in the same call, because a payload POSIX in one path field
    and native in its sibling is worse than either — the reader could not tell which they hold.
    """
    try:
        loaded = policy.load(args.policy)
    except policy.PolicyError as exc:
        _diag(f"gate: {exc}")  # the policy FILENAME can carry the forgery [L-23]
        return 2

    if args.emit_policy:
        # R-14. A QUESTION ABOUT THE FILE AND NOT ABOUT ANY RUN. The history is never opened —
        # which is why this returns before the lookup below rather than after it — and `--log` is
        # SAID to be unread rather than silently defaulting to the project's history. A flag that
        # is accepted and does nothing is the silent no-op this package refuses everywhere else;
        # `verify --log` is the sibling that announces the same thing.
        if args.log:
            _diag(
                f"gate --emit-policy: --log {args.log} is not read; this answers about the "
                "policy file alone"
            )
        if args.format == "json":
            print(
                json.dumps(
                    policy.policy_payload(loaded, policy_path=hashing._posix(args.policy)), indent=2
                )
            )
        else:
            for line in policy.render_policy(loaded, policy_path=hashing._posix(args.policy)):
                print(line)
        # 0 USABLE, AND THERE IS NO 1. A policy cannot carry a *finding* — it is either a document
        # this version can act on or it is not, and the `return 2` above is the second case.
        return 0

    log = pathlib.Path(args.log) if args.log else active().resolved_run_log()
    records: list[dict[str, typing.Any]] = []
    bad = [0]
    unfinished = 0
    try:
        # THE EXISTENCE PROBE IS INSIDE THE SAME GUARD AS THE READ [L-01]. It used to sit
        # above the `try`, and K-23's repair was therefore only half applied: `Path.is_file()`
        # swallows ENOENT, ENOTDIR, EBADF and ELOOP and nothing else, so **EACCES and EIO
        # propagate out of the probe** and the command still answered with a raw traceback,
        # no stdout and exit 1 — exit 1 being *a rule was checked and your controls were
        # violated*, about a file nothing ever opened. Reproduced by making the history's
        # DIRECTORY unreadable rather than the file; the file's own mode was the half the
        # original repair fixed, and the half its acceptance test exercised.
        #
        # THE MOTIVATION MAKES THE SURVIVING ROUTE THE LIKELIER ONE: an EIO from a failing
        # disk is an `OSError` on this same path, and `stat()` is on it.
        #
        # WRAPPING THE PROBE IS THE FIX, AND ENUMERATING ERRNOS IS NOT — measured, and the
        # difference matters. **Exactly three errnos must keep `found=False`: ENOENT, ENOTDIR
        # and ELOOP**, because each describes a path that NAMES NOTHING. Catch "any OSError
        # but ENOENT" instead and ENOTDIR and ELOOP flip to `found=True` with a `read_error`
        # — the gate then reports *the history is there and I could not read it* about a path
        # that does not exist, which is the false sentence `found` exists to prevent. That
        # set is `is_file()`'s OWN ignore set, so letting `is_file()` answer and catching what
        # it declines to swallow keeps the three correct by construction rather than by a list
        # that can drift. Dropping the probe and letting the read decide is a third form and
        # it is worse than both: `open()` on a FIFO blocks forever, so the gate HANGS.
        present = log.is_file()
        if present:
            # UNREADABLE LINES ARE COUNTED, NOT SKIPPED. A torn line is a run this gate did
            # not examine, so it has to stop the gate reporting MET: `_counted` keeps the
            # number and `_completed` is the sibling that drops it. I-01 is the row where
            # exactly this count was computed and thrown away.
            #
            # AND A RUN THAT STARTED AND NEVER ENDED IS COUNTED THE SAME WAY [K-08].
            # `_counted` drops every start line before any rule sees it, so a SIGKILLed run
            # reached no rule, appeared in no field of the payload, and the gate answered
            # MET / exit 0 / `cannot_check: null` over a history `log` was already reporting
            # `unfinished: 1` for. The scan rides on the pass that has to happen anyway —
            # which is A-20's whole point, and `_counted`'s docstring's one condition is met
            # here: this pass is EXHAUSTED by the `list()` below, so no run that ends further
            # down the file is reported unfinished.
            #
            # `report()` IS NOT CALLED. `show` prints a banner from this; a gate's answer is
            # the payload and the page, and R-4 gives stdout to the payload alone. The count
            # reaches the reader as a field and as a `cannot_check` clause instead.
            scan = _InFlightScan(log.parent / ".incomplete")
            records = list(_counted(log, bad, scan))
            unfinished = len(scan.open)
    except OSError as exc:
        # THE HISTORY IS THERE AND CANNOT BE READ [K-23, L-01]. This used to raise out of the
        # command: a raw traceback, nothing on stdout, and exit 1 — which is L-81's code for
        # *a rule was checked and your controls were violated*, about a file nothing ever
        # opened. `show` crashes the same way and inherits no handler, but only `gate`
        # reserves exit 1 for that meaning. The question WAS formed and the finding is that
        # the history could not be read, so this carries a payload and exits 2, exactly as
        # the missing-history branch below does.
        said = exc.strerror or str(exc)
        _diag(f"gate: {log} could not be read: {said}")
        result = policy.assessment(
            loaded,
            [],
            policy_path=hashing._posix(args.policy),
            history=hashing._posix(log),
            read_error=said,
        )
    else:
        if present:
            result = policy.assessment(
                loaded,
                records,
                policy_path=hashing._posix(args.policy),
                history=hashing._posix(log),
                unreadable=bad[0],
                unfinished=unfinished,
            )
        else:
            result = policy.assessment(
                loaded,
                [],
                policy_path=hashing._posix(args.policy),
                history=hashing._posix(log),
                found=False,
            )
            _diag(f"gate: no run history at {log}")

    if args.format == "json":
        # NOTHING ELSE ON STDOUT [ADR-0017 R-4]. Both diagnostics above go to stderr, so a caller
        # parses what it is handed instead of stripping a line first.
        print(json.dumps(policy.payload(result), indent=2))
    else:
        for line in policy.render(result):
            print(line)
    return result.exit_code


def _check(args: argparse.Namespace) -> int:
    """ADR-0011. Exit 1 on a finding, so it can gate a build — which is the point of it.

    Also exit 1 on a file that could not be parsed: that file was NOT checked, and a gate
    that greens on "not checked" is the shape this package exists to catch.
    """
    root = pathlib.Path(args.root) if args.root else pathlib.Path(active().root)
    if not root.is_dir():
        # [ADR-0017 R-15]. THE DIRECTORY NAMED IS NOT THERE, so this package has not answered
        # — it could not start. Silence on stdout is the signal that says so, and it is the
        # only thing separating this from the exit 2 below, which IS an answer.
        print(f"check: {root} is not a directory", file=sys.stderr)
        return 2
    report = check_mod.scan(root)
    if args.format == "json":
        # [ADR-0017 R-4]. The payload and nothing else: the render lines below are suppressed
        # rather than printed alongside, and the diagnostic under `examined_nothing` is on
        # stderr already, where a caller that parses stdout never meets it.
        print(json.dumps(check_mod.payload(report, root), indent=2))
    else:
        for line in check_mod.render(report, root):
            print(line)
    if report.examined_nothing:
        # A-08. 2 is COULD NOT CHECK, which is what this is: a wrong path or a directory with
        # no entry point in it. A CI job must be able to tell that from a finding, and from a
        # clean sweep — three outcomes, three codes.
        print(f"check: {report.examined_nothing}", file=sys.stderr)
        return 2
    return 0 if report.ok else 1


def _prune(args: argparse.Namespace) -> int:
    """`prune`, the only command in this package that removes a file it did not just write.

    DERIVED FROM THE HISTORY BEING READ, like `_report_in_flight`: `--log somewhere/else`
    prunes the markers belonging to THAT history. The directory is `.incomplete` beside it,
    which is `Project.resolved_incomplete_dir`'s rule, spelled here from the same path the
    banner printed rather than from the ambient project — so what the banner complained
    about is what this clears.

    NO CONFIRMATION PROMPT, deliberately. The README already documents `rm -r` on this
    directory as safe, and this command deletes a strict subset of what that `rm` deletes;
    adding friction to the safer of the two tools would be theatre rather than caution.
    `--dry-run` is there for a user who wants to look first, and what makes this safe is the
    containment check and the RUNNING skip, not a question nobody reads.
    """
    path = pathlib.Path(args.log) if args.log else active().resolved_run_log()
    try:
        older = prune_mod.parse_age(args.older_than) if args.older_than else None
    except ValueError as exc:
        # A MESSAGE, NOT A TRACEBACK, and exit 2 like every other usage failure here.
        print(f"  runprov prune: {exc}", file=sys.stderr)
        return 2
    return _forget(
        path.parent / ".incomplete",
        older,
        other_hosts=args.other_hosts,
        dry=args.dry_run,
    )


def _show(args: argparse.Namespace, path: pathlib.Path) -> int:
    """`show`, which reads the history and renders it. It writes nothing, by design.

    With no target it is the PROJECT page -- every script, what it expects, what it writes,
    and every artifact on record with the run that produced it. That is the question a
    developer has three weeks in: does this already exist, and what did it come from.

    With a target it is one page per matching run, oldest last, because the last one is the
    state you are in.
    """
    bad = [0]
    if args.exit_code and not (args.stale or args.rehash):
        # A GATE THAT CHECKED NOTHING would exit 0 having looked at no artifact, which is
        # the reassuring lie this package refuses -- `verify` has a guard for the same
        # shape. Nothing to gate on is a usage error, not a pass.
        raise UsageError("--exit-code needs --stale or --rehash; there is nothing to gate on")
    if args.exit_code and args.target:
        # REFUSED RATHER THAN OVERLOADED. `show <target>` already returns 1 for "nothing
        # matched your target", so accepting both would make one exit code mean that AND
        # "an artifact is stale" -- and the reader could not tell which. The staleness
        # column belongs to the project page anyway (see the note below).
        raise UsageError(
            "--exit-code applies to the project page; `show <target>` already exits 1 for "
            "'nothing matched', and one code cannot mean both"
        )
    if args.target and (args.stale or args.rehash):
        # ANNOUNCED, not ignored. These are parsed for every `show` but only applied to the
        # project page, because the staleness column belongs to the ARTIFACT INDEX and a
        # target renders runs. Accepting a flag and doing nothing is the silence this
        # package refuses everywhere else -- a reader who passed it is entitled to know it
        # had no effect rather than to conclude the artifacts are fine.
        flags = " ".join(f for f, on in (("--stale", args.stale), ("--rehash", args.rehash)) if on)
        print(
            f"# NOTE: {flags} applies to the project page and is ignored with a target.\n"
            f"#   `python -m runprov show` with no target renders the artifact index, which "
            f"is where\n#   the staleness column lives.",
            file=sys.stderr,
        )
    if args.target:
        # A TARGET IS A FILTER, and now genuinely is one: `select` fills its four buckets
        # in a single streaming pass, so what is held is what matches -- a handful of runs,
        # not a hundred thousand. `--limit` is passed IN rather than sliced off the result,
        # so a bucket cannot grow past it on the way.
        # J-12. `hits[0]` IS HOW MANY MATCHED; `matched` below is how many survived `--limit`.
        # The deque inside `select` forgets the rest, so the count has to come out with it.
        hits = [0]
        matched = select(_counted(path, bad), args.target, limit=args.limit or None, matching=hits)
        if not matched:
            # [ADR-0017 R-15]. A TARGET THAT MATCHED NOTHING IS AN ANSWER AND EXITS 1, so it
            # serialises. Found by measuring rather than by reasoning: the two success paths and
            # the missing-history path were wired and this one was not, so `show nosuch
            # --format json` exited 1 with EMPTY stdout — which under R-15 is the signal for
            # *the invocation was wrong*, and the invocation was fine. Every sibling emits a
            # payload at exit 1: `report` for STALE, `check` for a finding, `log` with
            # `matched: false`.
            #
            # THE SAME SHAPE AS A MATCH, with `matched: 0` and an empty `runs` list, because a
            # consumer should not need a second shape to read a negative answer — and `matched`
            # beside `runs` is what makes the empty list legible.
            if args.format == "json":
                print(json.dumps(show_mod.payload_runs(path, args.target, [], bad[0]), indent=2))
            print(
                f"nothing in {path} matches {args.target!r}.\n"
                f"  A target is a script name, a run_uid prefix, a run_id or an artifact "
                f"path.\n  `python -m runprov show` with no target lists every script this "
                f"project has run.",
                file=sys.stderr,
            )
            return 1
        views = [run_view(r) for r in matched]
        if args.format == "yaml":
            # J-08. RENDERED, NOT ANSWERED. R-3's amendment: *"`show --format yaml` … is a
            # rendering of a page, not an answer."* Moving the truncations out of `run_view` for
            # the payload's sake would otherwise have rewritten this format, which shipped in
            # 0.6.0, without an entry — I-24's defect, arriving sideways through a fix for
            # something else.
            sys.stdout.write(_yaml_doc([show_mod.for_display(v) for v in views]))
        elif args.format == "json":
            # [ADR-0017 R-4]. The payload alone on stdout; the tally below is on stderr, where
            # it already was, and now the payload carries `unreadable` too so a consumer meets
            # that fact without reading a banner it is told not to parse.
            print(
                json.dumps(
                    show_mod.payload_runs(
                        path, args.target, views, bad[0], hits[0], args.limit or None
                    ),
                    indent=2,
                )
            )
        else:
            sys.stdout.write("\n".join(render_run(v) for v in views))
        # J-12. `N of M` ONLY WHEN THEY DIFFER, so an untruncated page prints the sentence it
        # has printed since 0.6.0 and a truncated one stops claiming the narrowed count is the
        # whole answer. Measured against the tag: five matching runs and `--limit 2` said
        # *"2 run(s) matching 'build'"*, which a reader takes for a script that ran twice.
        #
        # A STATEMENT RATHER THAN A CONDITIONAL EXPRESSION, and the first version of this was
        # the bug: with `head if cond else other + clause`, the `+ clause` binds to the `else`
        # arm ALONE, so the unreadable-lines count disappeared from exactly the truncated page
        # this row exists to fix. One defect traded for another, in the same line.
        head = (
            f"# {len(matched)} of {hits[0]} run(s) matching {args.target!r} from {path}"
            if len(matched) != hits[0]
            else f"# {len(matched)} run(s) matching {args.target!r} from {path}"
        )
        print(
            head + (f"; {bad[0]} unreadable line(s) skipped" if bad[0] else ""),
            file=sys.stderr,
        )
        return 0

    # MARKERS BEFORE THE PASS THAT READS THE HISTORY, which is A-05's ordering and is why
    # this is constructed here rather than beside the banner it prints. `_counted` feeds it
    # every record as the page streams, so the `started` lines the page drops are recovered
    # in the pass already happening instead of by reading the whole file again (A-20).
    scan = _InFlightScan(path.parent / ".incomplete")
    view = project_view(_counted(path, bad, scan))
    # OFF unless asked. The page is consulted many times a day and reading the filesystem
    # is the one thing here that can cost what the work costs; `--stale` is a stat per
    # input, `--rehash` reads them.
    # A SECOND pass over the file rather than a second copy in memory. Re-reading 91 MB
    # costs seconds; holding it costs hundreds of megabytes, and only one of those grows
    # without bound as the project does.
    states = (
        staleness(_counted(path, [0]), rehash=args.rehash) if (args.stale or args.rehash) else None
    )
    if args.format == "yaml":
        sys.stdout.write(_yaml_doc({**view, "state": states} if states else view))
    elif args.format == "json":
        # THE MARKER SCAN IS READ HERE RATHER THAN PRINTED. `scan.report()` below writes the
        # unfinished runs to stderr and nothing else ever saw them; `pending()` is idempotent
        # by construction — its own docstring is about exactly that — so reading it here and
        # letting `report()` print afterwards states one fact twice rather than computing it
        # twice. H1-6 is the row where a second computation of one fact disagreed with the first.
        print(
            json.dumps(
                show_mod.payload_project(path, view, states, scan.pending(), bad[0]), indent=2
            )
        )
    else:
        # WRITELINES, not write: `render_project_lines` yields the page one line at a
        # time precisely so the whole of it is never a single value. Joining here would
        # rebuild the string the generator exists to avoid.
        sys.stdout.writelines(render_project_lines(view, states))
    # RUNS WITH NO ENDING ON RECORD, before the tally, because "this page may be describing
    # a job that is still writing" changes how everything above it should be read. Reported
    # on the PROJECT page only, for the same reason the staleness column is: it is a fact
    # about the project rather than about one run. The pairing was collected above, as the
    # page streamed; this only prints it.
    scan.report()

    tally = ""
    if states:
        counts = collections.Counter(states.values())
        tally = "; " + ", ".join(f"{n} {k}" for k, n in sorted(counts.items()))
    print(
        f"# {view['runs']} run(s), {len(view['scripts'])} script(s), "
        f"{len(view['artifacts'])} artifact(s) from {path}{tally}"
        + (f"; {bad[0]} unreadable line(s) skipped" if bad[0] else ""),
        file=sys.stderr,
    )
    if args.exit_code:
        # `?` IS NOT A FAILURE, and that is the whole reason it is a separate word. It means
        # the check could not be made -- a missing sidecar, a directory input the stat check
        # cannot speak for, a digest the run never recorded -- and failing a gate on it would
        # make every project with one directory input permanently red. It is on the summary
        # line above, where a reader sees it and can reach for `--rehash`.
        #
        # NOTHING CHECKED still exits 0 here, unlike `verify`. `show` reads the HISTORY, so
        # an empty project page means the project has no runs, which is a true and unalarming
        # answer -- not `verify`'s "these files exist and none of them carries a pin".
        failing = sorted(k for k, v in (states or {}).items() if v in (STALE, GONE, MODIFIED))
        if failing:
            shown = ", ".join(failing[:5]) + (
                f", and {len(failing) - 5} more" if len(failing) > 5 else ""
            )
            print(f"# FAILING ({len(failing)}): {shown}", file=sys.stderr)
            return 1
    return 0


def _chain(args: argparse.Namespace) -> int:
    """`chain` — ADR-0016. Whether the history has been edited since it was written.

    ITS OWN SUBCOMMAND, and R-8 records why it is not a flag on `verify`: that command's
    docstring states it deliberately never touches the history, because the pin lives in the
    artifact's bytes so a committed result stays checkable by someone holding the repository and
    nothing else. Answering a second question there would make one exit code mean two things.

    The three codes are the package's, unchanged: 0 checked and intact, 1 checked and BROKEN, 2
    COULD NOT CHECK — no history, unreadable, or nothing in it chained yet.
    """
    # G-06. `resolved_run_log()`, LIKE EVERY OTHER READER. This was the only one of the nine
    # that read `active().run_log`, which is `None` in any project that does not pass
    # `run_log=` explicitly — so in the layout this package documents, `log` and `lineage`
    # answered and `chain` said "no history configured and none named" and exited 2 over an
    # intact history. The command's own default, dead, on the projects it was written for.
    #
    # AND NO `is None` GUARD BEHIND IT: the resolved path is never `None`, so the arm would be
    # unreachable, and a path that does not exist is already answered one layer down —
    # `verify` catches the `OSError` and the report reads `CANNOT CHECK: no history to read.`
    # with exit 2, the same answer it gives for a directory or a file it may not open.
    log = pathlib.Path(args.log) if args.log else active().resolved_run_log()
    report = chain_mod.verify(log)
    if args.format == "json":
        # ONE BUILDER, TWO RENDERERS (ADR-0017 R-1). This used to name every field here, so a
        # field added to `Report` had to be remembered in two places; `chain_mod.payload` walks
        # the structure instead, and a test asserts the walk covers it.
        print(json.dumps(chain_mod.payload(report, pathlib.Path(log)), indent=2))
    else:
        for line in chain_mod.render(report, pathlib.Path(log)):
            print(line)
    if report.status == chain_mod.BROKEN:
        return 1
    return 0 if report.status == chain_mod.INTACT else CANNOT_CHECK


def _verify(args: argparse.Namespace) -> int:
    """`verify`, and it deliberately never touches the history.

    The pin is in the artifact, which is the whole point of putting it there: a committed
    result can be checked by someone who has the repository and nothing else — no sidecar,
    no `runs.jsonl`, no `configure()`. Requiring the history here would have made the check
    depend on the one file the pin exists to survive.
    """
    root = pathlib.Path(args.root) if args.root else active().root
    if getattr(args, "log", None):
        print(
            "# NOTE: --log is accepted for uniformity and ignored by `verify`, which reads "
            "the pin\n#   inside each artifact and never the history — that is why the pin "
            "is in the bytes.",
            file=sys.stderr,
        )
    report = verify([pathlib.Path(p) for p in args.paths] or [root], root)

    if args.format == "json":
        sys.stdout.write(json.dumps(verify_mod.payload(report), indent=2) + "\n")
    else:
        sys.stdout.write(render_report(report))

    seen, pinned = report["artifacts_seen"], report["artifacts_pinned"]
    # J-18. BOTH "NOTHING CHECKED" SENTENCES COME FROM THE FOLD, and so does the second
    # branch's condition. They were composed here and the payload carried only the counters,
    # so the sentence a reader sees and the conclusion a consumer must derive were two
    # different things with the precedence visible in only one of them. J-04 is this defect in
    # `chain`'s renderer, found in the same audit.
    inability = verify_mod.cannot_check(report)
    # A-17, AND BEFORE EVERY EARLY RETURN. A directory this checker could not read is the
    # one thing that changes what a clean result means: `os.walk` swallows those failures,
    # so a pinned artifact inside it did not exist as far as this report was concerned.
    #
    # It matters MOST in the `not pinned` branch below, which is exactly where the first
    # placement of this notice never ran — an unreadable directory holding every pin in the
    # project reported `NOTHING CHECKED: no pins found` and said nothing about why.
    for name in report.get("directories_unreadable") or []:
        print(
            f"# COULD NOT READ {name} — anything pinned inside it was NOT checked",
            file=sys.stderr,
        )

    if not pinned:
        # NOT zero. A gate that goes green having checked nothing is worse than no gate,
        # because someone will trust it -- the same rule as `git_status_captured: false`.
        print(
            f"# NOTHING CHECKED: {inability}.\n"
            f"#   This is 'we could not look', not 'nothing is wrong'. A pin is written by "
            f"`run.header()`\n"
            f"#   or `run.open_output()`; an artifact produced without one cannot be "
            f"verified from its bytes.",
            file=sys.stderr,
        )
        return CANNOT_CHECK

    # The skipped count is REPORTED, never merely applied. A checker that quietly narrows
    # what it looked at reads as "everything is fine" when it means "I did not look there".
    skipped = report["directories_skipped"]
    print(
        f"# {pinned} pinned artifact(s) of {seen} file(s) under {root}"
        + (
            f" ({skipped} build/vcs/venv director{'y' if skipped == 1 else 'ies'} not walked)"
            if skipped
            else ""
        )
        + f": {report['ok']} OK, {report['stale']} STALE, {report['gone']} GONE, "
        f"{report['unverifiable']} UNVERIFIABLE"
        + (f", {report['altered']} ALTERED" if report.get("altered") else "")
        + (
            f"; {report['partial_pins']} pin(s) declare they may understate the run"
            if report.get("partial_pins")
            else ""
        ),
        file=sys.stderr,
    )
    # DEBRIS IS SAID, not silently skipped. `_atomic` leaves one of these only when a run
    # died between opening the temporary file and renaming it over the destination, so it is
    # the one visible trace of a crash mid-write -- and a reader who finds an unexplained
    # dotfile in a results directory should be told what it is rather than left to guess.
    if report.get("write_debris"):
        n = report["write_debris"]
        print(
            f"# {n} file(s) left behind by a run that died while writing a record"
            f" (*{TEMP_SUFFIX}). Nothing reads them; they are safe to delete.",
            file=sys.stderr,
        )
    # WHAT `OK` MEANS, every time it is printed. The caveat was in the README and nowhere a
    # user of this command would meet it, so `verify` printed OK and exited 0 over an
    # artifact with a fabricated row appended -- true, since the INPUTS were untouched, and
    # read by everyone as "this file is intact". A CI gate built on this command, which the
    # README endorses, would not catch a hand-edited result. One line, on the output that
    # makes the claim.
    if report["ok"]:
        print(
            "# OK = the inputs each artifact pins still hash the same, AND — for artifacts "
            "carrying a `body` digest — that the artifact itself has not been edited since "
            f"it was written ({report.get('body_checked', 0)} of {pinned} could be asked; "
            "one written before that field existed carries no digest to check).",
            file=sys.stderr,
        )
    # ALTERED IS SAID SEPARATELY AND LOUDLY. It is not a result that went out of date; it
    # is a file somebody changed after it was written, and the person reading this needs to
    # know which of the two they are looking at before they rebuild anything.
    if report.get("altered"):
        print(
            f"# {report['altered']} artifact(s) ALTERED: the body no longer hashes to what "
            f"the artifact's own pin says it was written as. That is an edit after the fact, "
            f"not a stale input — rebuilding would destroy whatever was changed.",
            file=sys.stderr,
        )
    if report["stale"] or report["gone"] or report.get("altered"):
        return 1
    if report.get("paths_absent"):
        # J-24. ITS OWN HEADLINE, because "NOTHING CHECKED" would be FALSE here: this branch is
        # reached with artifacts verified and one of the caller's paths missing, and a sentence
        # that overstates what went wrong is the defect I-16 and G-16 are both about.
        #
        # THE EXIT CODE MOVES, 0 -> 2, and that is the row. Taylor ruled on 2026-10-01 on the
        # measured evidence: README's own contract for these codes says 2 covers *"no such
        # file"* and that `verify` answers the same three codes, and a gate that passes while a
        # named artifact is missing is this module's own "AND IT WILL NOT PASS HAVING CHECKED
        # NOTHING" broken for the subset of the request it could not look at.
        print(
            f"# NOT CHECKED: {inability}.\n"
            f"#   You named {'it' if len(report['paths_absent']) == 1 else 'them'} and "
            f"{'it is' if len(report['paths_absent']) == 1 else 'they are'} not there, so "
            f"nothing was verified about\n"
            f"#   {'it' if len(report['paths_absent']) == 1 else 'them'}. A run that was "
            f"supposed to produce this file and did not is the case\n"
            f"#   this exists to stop passing silently.",
            file=sys.stderr,
        )
        return CANNOT_CHECK

    if inability is not None:
        # THE OTHER WAY TO CHECK NOTHING. The guard above catches "no artifact carries a
        # pin"; this catches "every pin was unreadable" -- an input outside the root, an
        # escaped name, a digest the run never recorded. Zero comparisons were made either
        # way, and `FAILING = (STALE, GONE)` excluded UNVERIFIABLE, so the zero-pins case
        # exited 1 while the zero-checks case exited 0. This module's own docstring says
        # "Green must mean checked" and "AND IT WILL NOT PASS HAVING CHECKED NOTHING".
        #
        # A MIXED report still exits 0 deliberately: one artifact verified is a real answer,
        # and the count of the rest is on the summary line above where a reader sees it.
        # `ok` includes the NONE REGISTERED pin -- a run stating it read nothing is a
        # checkable claim that checks out, which is why this tests `ok` and not the entries.
        print(
            f"# NOTHING CHECKED: {inability}.\n"
            f"#   Every pin was UNVERIFIABLE -- see the reasons above. This is 'we could "
            f"not look', not\n"
            f"#   'nothing is wrong', and a gate that goes green on it is worse than no "
            f"gate.",
            file=sys.stderr,
        )
        return CANNOT_CHECK
    return 0


def _prog() -> str:
    """How the user actually invoked this, for usage and every error message.

    There are two ways in and they need different names. `prog` was hardcoded to
    `python -m runprov`, so the console script -- the one `uvx runprov` and `pipx run
    runprov` resolve, and the only one they CAN resolve -- printed usage telling the reader
    to type something else, and `runprov: error:` messages named an invocation they had not
    used. Leaving `prog` unset is not the fix either: argparse would derive it from
    `sys.argv[0]`, which under `-m` is the path to this file, so the module form would
    advertise `__main__.py`.

    `-m` sets `sys.argv[0]` to this file's path, which is what distinguishes the two.
    """
    entry = pathlib.Path(sys.argv[0]).name if sys.argv else ""
    return "python -m runprov" if entry == "__main__.py" else "runprov"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog=_prog())
    # `--version` before anything else, because it is the first thing a bug report asks for
    # and it was the one thing this CLI could not answer: `runprov --version` exited 2 with
    # "the following arguments are required: cmd". Read from the package rather than from
    # installed metadata, so a source checkout that was never `pip install`ed still answers.
    from . import __version__

    ap.add_argument("--version", action="version", version=f"runprov {__version__}")
    sub = ap.add_subparsers(dest="cmd", required=True)
    lg = sub.add_parser("log", help="read the continuous run history")
    lg.add_argument("--log", default=None, help="path to runs.jsonl (default: the project's)")
    # `text` IS THE HUMAN FORMAT ON EVERY SUBCOMMAND. It was spelled `timeline` here and
    # `text` on `show`, `verify` and `lineage`, so `--format text` -- learned on any one of
    # those -- was a hard ERROR here, and no format name at all was accepted by all four.
    # `timeline` stays accepted so nothing that works today stops working, and `metavar`
    # hides it so there is one name to learn. The renderer already treats anything that is
    # not `yaml` or `jsonl` as the human format, which is why this is the whole change.
    lg.add_argument(
        "--format",
        choices=("text", "timeline", "yaml", "jsonl", "json"),
        default="text",
        metavar="{text,yaml,jsonl,json}",
    )
    lg.add_argument("--limit", type=int, default=0, help="show only the last N runs")
    lg.add_argument("--script", default="", help="filter by script name")
    lg.add_argument("--run-id", default="", help="filter by run id")
    lg.add_argument("--failed", action="store_true", help="only runs that failed")
    lg.add_argument(
        "--unreadable",
        action="store_true",
        help="print ONLY the lines that will not parse, with their line numbers, and stop",
    )
    im = sub.add_parser(
        "impact", help="what was derived from this file, and in what order it rebuilds"
    )
    im.add_argument("target", help="a path, or a sha256 digest")
    im.add_argument("--depth", type=int, default=None, help="stop after this many hops")
    im.add_argument("--log", default=None, help="path to runs.jsonl (default: the project's)")
    im.add_argument("--format", choices=("text", "json"), default="text")
    df = sub.add_parser("diff", help="what changed between two runs, and what cannot be compared")
    df.add_argument("a", help="a run: run_uid prefix, run_id, script name or artifact path")
    df.add_argument(
        "b",
        nargs="?",
        default=None,
        help="the other run; omit to compare the last TWO runs matching the first",
    )
    df.add_argument("--log", default=None, help="path to runs.jsonl (default: the project's)")
    df.add_argument("--format", choices=("text", "json"), default="text")
    rs = sub.add_parser(
        "resources", help="what a run consumed, as a request you can size a cluster job with"
    )
    rs.add_argument("script", nargs="?", default=None, help="a script name; omit for the last run")
    rs.add_argument(
        "--format",
        choices=("text", "tsv", "slurm", "k8s", "json"),
        default="text",
        help="tsv uses Snakemake's benchmark columns; slurm and k8s render a request; "
        "json is the measurement itself",
    )
    rs.add_argument(
        "--margin",
        type=float,
        # J-16. `None`, NOT 1.5, SO THAT *NOT ASKED* IS DISTINGUISHABLE FROM *ASKED FOR 1.5*.
        # With a concrete default there is no way to tell them apart, and the note below would
        # have to print on every `--format json` invocation or none. `_log_selectors` states
        # the same rule for `log`'s flags: a default is a value, and `null` is nobody asked.
        default=None,
        help="multiply the measured floor by this before rendering a request (default 1.5)",
    )
    rs.add_argument("--log", default=None, help="path to runs.jsonl (default: the project's)")
    rp = sub.add_parser("report", help="one artifact, one page, for a quality file")
    rp.add_argument("artifact", help="the artifact to report on")
    rp.add_argument("--log", default=None, help="path to runs.jsonl (default: the project's)")
    rp.add_argument("--format", choices=("text", "json"), default="text")
    ck = sub.add_parser("check", help="which entry points open files and record nothing (ADR-0011)")
    ck.add_argument(
        "root", nargs="?", default=None, help="directory to sweep (default: the project root)"
    )
    ck.add_argument("--format", choices=("text", "json"), default="text")
    ln = sub.add_parser("lineage", help="reconstruct the run DAG by joining on digests")
    ln.add_argument("--log", default=None, help="path to runs.jsonl (default: the project's)")
    ln.add_argument("--format", choices=("text", "json"), default="text")
    sh = sub.add_parser("show", help="the notebook: one page per project, or per run")
    sh.add_argument(
        "target",
        nargs="?",
        help="a script name, run_uid prefix, run_id or artifact path; omit for the project",
    )
    sh.add_argument("--log", default=None, help="path to runs.jsonl (default: the project's)")
    sh.add_argument("--format", choices=("text", "yaml", "json"), default="text")
    sh.add_argument("--limit", type=int, default=0, help="show only the last N matching runs")
    sh.add_argument(
        "--stale",
        action="store_true",
        help="check each artifact against the inputs of the run that made it (one stat each)",
    )
    sh.add_argument(
        "--rehash",
        action="store_true",
        help="with --stale, re-derive digests instead of comparing size and mtime (slower)",
    )
    sh.add_argument(
        "--exit-code",
        action="store_true",
        help="with --stale/--rehash, exit 1 if any artifact is STALE, GONE or MODIFIED",
    )
    # THE ONLY THING `show` DOES THAT IS NOT LOOKING, and it is applied AFTER the page has
    # been rendered, in `main`, rather than inside `_show`. That keeps the promise in
    # `_show`'s own docstring — "it writes nothing, by design" — true of the function as
    # well as of the prose, and it means the reader sees the markers counted on the page
    # before the ones that describe nothing running are cleared out from under them.
    sh.add_argument(
        "--forget-markers",
        action="store_true",
        help="after the page, remove in-flight markers that describe nothing running",
    )
    ex = sub.add_parser("exec", help="run a non-Python command AS a recorded run")
    ex.add_argument("--name", default=None, help="the step name (default: the program's)")
    ex.add_argument("--input", action="append", default=[], help="repeatable")
    ex.add_argument("--output", action="append", default=[], help="repeatable")
    ex.add_argument("--provenance", default=None, help="where the sidecar goes")
    ex.add_argument("--capture", default=None, help="tee the command's output to this file")
    ex.add_argument("command", nargs=argparse.REMAINDER, help="-- then the command to run")
    vf = sub.add_parser(
        "verify",
        help="do artifacts still match the INPUTS they pin? (not: is the artifact unedited)",
    )
    vf.add_argument("paths", nargs="*", help="artifacts or directories (default: the root)")
    vf.add_argument("--root", default=None, help="what pinned names are relative to")
    # ACCEPTED SO `runprov <cmd> --log X` stays uniform across the subcommands, and
    # ANNOUNCED because `verify` genuinely cannot use it: it reads the pin inside the
    # artifact and nothing else, which is the whole reason the pin is in the bytes. Declared
    # with SUPPRESS and never read, it was a flag that did nothing and said nothing.
    vf.add_argument("--log", default=None, help=argparse.SUPPRESS)
    vf.add_argument("--format", choices=("text", "json"), default="text")
    ch = sub.add_parser(
        "chain",
        help="has the run history been edited since it was written? (ADR-0016)",
    )
    ch.add_argument("log", nargs="?", default=None, help="the history (default: the project's)")
    ch.add_argument("--format", choices=("text", "json"), default="text")
    cp = sub.add_parser(
        "capture",
        help="run a Python script AS a recorded run, with no changes to the script",
    )
    cp.add_argument("script", help="the .py file to run")
    cp.add_argument("rest", nargs=argparse.REMAINDER, help="arguments passed to the script")
    cp.add_argument(
        "--name", default=None, help="the script name in the record (default: its stem)"
    )
    cp.add_argument("--provenance", default=None, help="where to write the sidecar")
    cp.add_argument("--log", default=None, help=argparse.SUPPRESS)
    ex2 = sub.add_parser("export", help="the record in RO-Crate or W3C PROV, for other tools")
    ex2.add_argument(
        "sidecar",
        nargs="?",
        default=None,
        help="one run's sidecar (.prov.json). Omit to export the whole history.",
    )
    ex2.add_argument("--format", choices=EXPORT_FORMATS, default="ro-crate")
    ex2.add_argument("--log", default=None, help="path to runs.jsonl (default: the project's)")
    ex2.add_argument(
        "-o",
        "--out",
        default=None,
        help="write here instead of stdout; a directory gets the format's own filename",
    )
    pr = sub.add_parser("prune", help="remove in-flight markers that describe nothing running")
    pr.add_argument("--log", default=None, help="path to runs.jsonl (default: the project's)")
    pr.add_argument(
        "--older-than",
        default=None,
        metavar="AGE",
        help="only markers this old or older: 30d, 12h, 90m (default: every eligible one)",
    )
    pr.add_argument(
        "--other-hosts",
        action="store_true",
        help="also remove markers from OTHER hosts, whose liveness cannot be checked here",
    )
    pr.add_argument("--dry-run", action="store_true", help="say what would go; remove nothing")
    gt = sub.add_parser(
        "gate",
        help="did the recorded runs meet a written policy? (ADR-0018)",
        # R-5's OTHER HALF: THE RULE SET IS NOT TYPED HERE. A policy's author needs the names and
        # what each one asks, and the registry is the only list of them — so `--help` reads it.
        # A list in this epilog would be the second copy R-5 exists to forbid, and the one most
        # likely to go stale, because nothing fails when a help text is wrong.
        epilog="rules (the names a policy may use):\n"
        + "".join(f"  {name:<22}{got.asks}\n" for name, got in sorted(policy.rules().items()))
        + "\neach rule in the policy carries a `why`, which is required and is printed beside "
        "its verdict",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    gt.add_argument("--policy", required=True, help="a .json or .toml policy file")
    gt.add_argument("--log", default=None, help="path to runs.jsonl (default: the project's)")
    gt.add_argument(
        "--emit-policy",
        action="store_true",
        help="validate the policy and print its normalised form; reads no history (ADR-0018 R-14)",
    )
    gt.add_argument("--format", choices=("text", "json"), default="text")
    args = ap.parse_args(argv)

    if args.cmd == "exec":
        return _exec(args)

    if args.cmd == "chain":
        return _chain(args)
    if args.cmd == "verify":
        return _verify(args)

    # BEFORE THE "no run history" CHECK, because markers outlive the history they name: a
    # deleted `runs.jsonl`, a run started with no `provenance=`, a `--log` pointed at a path
    # that was never written. Those are precisely the markers nothing else will ever clear.
    if args.cmd == "capture":
        return _capture(args)

    if args.cmd == "export":
        return _export(args)

    if args.cmd == "prune":
        return _prune(args)

    # BEFORE the run-history lookup below, like `capture` and for the same reason: this reads
    # SOURCE, not records. A project with no history yet is exactly the one worth asking.
    if args.cmd == "check":
        return _check(args)

    if args.cmd == "report":
        return _report(args)

    if args.cmd == "resources":
        return _resources(args)

    if args.cmd == "diff":
        return _diff(args)

    if args.cmd == "impact":
        return _impact(args)

    # BEFORE the run-history lookup below, and the order is the decision rather than the layout.
    # THE POLICY IS THE QUESTION: a gate whose policy cannot be read has nothing to ask, while a
    # gate whose history is missing has a question and cannot answer it. Looking for the history
    # first would report a missing history for an invocation that never had a question — and the
    # two are different exit-2 classes under ADR-0017 R-15, one silent and one carrying a payload.
    if args.cmd == "gate":
        return _gate(args)

    path = pathlib.Path(args.log) if args.log else active().resolved_run_log()
    if not path.is_file():
        # BEFORE "nothing recorded here", because a marker beside a MISSING history is not
        # nothing recorded — it is a run that started and never got to write one, which is
        # the opposite finding and the more alarming of the two.
        scan = _report_in_flight(path)
        # WHERE THE RECORDS ACTUALLY WENT, WHEN A MARKER KNOWS. Every marker carries the
        # `history_destination()` of the run that wrote it, and beside a missing history that
        # is the one piece of evidence in the room. Without it this branch printed two things
        # that are FALSE for a project with a custom `sink=`: "nothing has been recorded here
        # yet", when a great deal was recorded — into a database — and "pass --log that path",
        # when a sink has no path to pass. The operator was sent looking for a file that will
        # never exist, by the command holding the answer.
        elsewhere = sorted(
            {
                str(m["history"])
                for m in scan.markers.values()
                if m.get("history") and not _same_destination(str(m["history"]), path)
            }
        )
        # NAMED, NOT INTERPRETED. Whether the destination is a file to pass to `--log` or a
        # sink with nothing to read is a distinction the reader can make from the string and
        # this command cannot make safely — `JsonlSink(/x/y.jsonl)` and a bare `DbSink` are
        # both possible, and guessing wrong sends them looking a second time.
        if elsewhere:
            found = (
                "  A MARKER BESIDE THIS PATH SAYS OTHERWISE: the run that left it recorded to\n"
                + "".join(f"    {d}\n" for d in elsewhere)
                + "  If that names a file, pass it to --log. If it names a sink, the records\n"
                "  are not on this filesystem and there is nothing here for --log to read."
            )
        else:
            found = (
                "  Nothing has been recorded here yet, which is a different thing from a run\n"
                "  that was not recorded, and worth telling apart.\n"
                "  This CLI cannot see what your scripts passed to configure(): with no --log\n"
                "  it reads the DEFAULT path above. If configure(run_log=...) sent the history\n"
                "  somewhere else, pass --log that path — the runs are not missing, this is the\n"
                "  wrong file to look in."
            )
        print(
            f"no run history at {path}\n"
            f"  It is created by the first recorded run — `with Run(..., provenance=...)`.\n"
            + found,
            file=sys.stderr,
        )
        # THE MARKERS ARE STILL THERE EVEN THOUGH THE HISTORY IS NOT, so the flag must work
        # here too — and this is the path where the directory is most likely to be the only
        # thing left. Accepting `--forget-markers` and silently doing nothing here would be
        # the silent no-op this package refuses everywhere else. After the message, for the
        # same reason it runs after the page: what a reader was told is what was true when
        # they were told it.
        # [ADR-0017 R-15]. A NAMED HISTORY THAT IS NOT THERE IS AN ANSWER, not a bad
        # invocation: the command ran, looked where it was told, and its finding is that there
        # is nothing to read. `chain` has said so in JSON since 0.6.0 while the commands
        # sharing THIS guard said it only in prose — one condition, one exit code, two classes,
        # which is the split R-15 was decided to end.
        #
        # ONE SITE FOR THREE COMMANDS: `log`, `show` and `lineage` all arrive here. The builder
        # is LOOKED UP rather than branched to, and that is a coverage fact as much as a style
        # one — an `elif args.cmd == "lineage"` has a false arm that nothing can reach, because
        # `show` is the only other command here and it has no `json` to be asked for yet. A
        # branch that cannot be taken is not a branch that is untested, and writing `show`'s
        # arm before `show` has a payload would be writing code no test can reach.
        #
        # A MISSING KEY RAISES RATHER THAN GUESSING. If `show` gains `--format json` without
        # gaining an entry here, this raises `KeyError` where the alternative — a default arm —
        # would hand a `show` caller a `lineage` payload. R-15's ratchet names `show` as
        # outstanding, so the two fail together.
        if getattr(args, "format", None) == "json":
            # J-06. `scan.pending()`, NOT `[]`. The scan is six lines above and was built
            # precisely because a marker beside a missing history is the finding here; the
            # payload was the one reader of this branch that could not see it.
            print(json.dumps(_NO_HISTORY_ANSWER[args.cmd](path, args, scan.pending()), indent=2))
        if args.cmd == "show" and args.forget_markers:
            _forget(path.parent / ".incomplete")
        return CANNOT_CHECK

    # `show` STREAMS rather than materialising, and it is the command that most needs to:
    # it is the page a developer opens many times a day over a history that only grows.
    # Measured on a 100,000-run, 91 MB history: 392 MB held to build the page, against 3 MB
    # to stream it. Every other command here genuinely needs all the records at once.
    # A MESSAGE, NOT A TRACEBACK — the reason `UsageError` exists. It was caught inside
    # `exec` only, so the first `show` usage error printed a stack over its own message, and
    # a second copy of the same `except` was the wrong repair. Exit 2 matches `exec`'s usage
    # failures and argparse's own, and is distinct from 1, which is a finding about the data.
    if args.cmd in ("show", "log"):
        try:
            code = _show(args, path) if args.cmd == "show" else _log(args, path)
        except UsageError as exc:
            print(f"  runprov {args.cmd}: {exc}", file=sys.stderr)
            return 2
        if args.cmd == "show" and args.forget_markers:
            # `show`'s EXIT CODE IS A FINDING ABOUT THE DATA and this must not overwrite it:
            # `--stale --exit-code` returning 1 means an artifact is stale, and a prune that
            # went perfectly cannot turn that into a pass. A file that would not unlink is
            # reported in prose, on the line that names it.
            _forget(path.parent / ".incomplete")
        return code

    # LINEAGE, and it STREAMS like everything else here. The comment that stood in this
    # place said it "genuinely needs every record at once ... there is nothing to stream
    # past", which was a claim about the records when the join is over DIGESTS -- and it is
    # the kind of confident comment that stops the next person looking. It walks the history
    # twice and holds an index, not a copy. Not guarded by an `if`, because the subparser is
    # required and every other command has returned by here.
    counted: list[int] = [0]
    names: dict[str, str] = {}
    g = _lineage(path, counted, names)
    total, bad = g["runs"], counted[0]
    if args.format == "json":
        # [ADR-0017 R-5] I-18. `lineage` has no module of its own — it is built here — so its
        # schema constant lives with the code that produces it rather than being invented at
        # the emission site, which is where a second, differing copy would eventually appear.
        # ONE BUILDER FOR BOTH STATES (J-21). This used to assemble the dict here while the
        # no-history answer assembled its own, and they differed by `path` within one commit.
        # `cannot_check` is present and null, [ADR-0017 R-8]: leaving the key out would make a
        # consumer tell *it looked and could* from *this rendering does not report that* by an
        # absence, which is the inference R-8 exists to remove.
        sys.stdout.write(json.dumps(_lineage_answer(path, g, bad), indent=2) + "\n")
    else:
        sys.stdout.write(_render_lineage(g, names) + "\n")
    print(
        f"# {total} record(s) from {path}" + (f"; {bad} unreadable line(s) skipped" if bad else ""),
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
