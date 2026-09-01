# Copyright (c) 2026 Assistance Publique – Hôpitaux de Paris (AP-HP),
#                    Hôpital Henri-Mondor, and Taylor Thompson
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
import shlex
import signal
import subprocess
import sys
import typing

from . import prune as prune_mod
from . import show as show_mod
from .hashing import PIN_DIGEST_CHARS
from .project import active
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
from .verify import GONE, STALE, render_report, verify

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
    """
    shown = raw[:UNREADABLE_SHOWN]
    body = "".join(
        c if c.isprintable() or c == " " else c.encode("unicode_escape").decode() for c in shown
    )
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
    return "\n".join(out) + "\n"


def _yaml(rows: list[dict[str, typing.Any]]) -> str:
    """The shape of the transformation log this replaces, generated from the real record.

    Deliberately the same field names — `step`, `input`, `output`, `script`, `run_command`,
    `date` — so anyone who reads the old file can read this one. The difference is where the
    values come from: these were observed and hashed, not typed by hand.
    """

    return _yaml_header() + "".join(_yaml_entry(r) for r in rows)


def _lineage(
    source: pathlib.Path | typing.Sequence[dict[str, typing.Any]],
    bad: list[int] | None = None,
    scripts: dict[str, str] | None = None,
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
    bad = total = shown = n_failed = 0

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

    def emit(r: dict[str, typing.Any]) -> None:
        nonlocal shown, n_failed
        shown += 1
        n_failed += r.get("status") == "failed"
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
        # it passes, and a banner emitted per record is not a banner.
        sys.stdout.write(_yaml_header())

    for rec in _stream(path):
        if rec is None:
            bad += 1
            continue
        if _is_start(rec):
            # NOT A RUN THAT HAPPENED. `log` streams raw rather than through `_load` or
            # `_counted` — it is the one reader that must not materialise — so the filter
            # those two apply has to be repeated here. Without it every completed run
            # printed an empty entry before its real one and the tally said 6 of 6 for
            # three runs.
            continue
        total += 1
        # THE NAME IS TESTED ON ITS OWN, before `--failed` narrows anything. Combined,
        # `--script build --failed` over a project whose `build` never failed reported
        # "nothing matched 'build'" and exited 1 — which is false twice over: the name was
        # right, and no failures is the good answer.
        if named_hit(rec):
            named_hits += 1
        if not matches(rec):
            continue
        if keep is not None:
            keep.append(rec)
        else:
            emit(rec)

    if keep is not None:
        for rec in keep:
            emit(rec)

    print(
        f"# {shown} of {total} run(s) from {path}"
        + (f"; {n_failed} FAILED" if n_failed else "")
        + (f"; {bad} unreadable line(s) skipped" if bad else ""),
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
        matched = select(_counted(path, bad), args.target, limit=args.limit or None)
        if not matched:
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
            sys.stdout.write(_yaml_doc(views))
        else:
            sys.stdout.write("\n".join(render_run(v) for v in views))
        print(
            f"# {len(matched)} run(s) matching {args.target!r} from {path}"
            + (f"; {bad[0]} unreadable line(s) skipped" if bad[0] else ""),
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
        sys.stdout.write(json.dumps(report, indent=2) + "\n")
    else:
        sys.stdout.write(render_report(report))

    seen, pinned = report["artifacts_seen"], report["artifacts_pinned"]
    if not pinned:
        # NOT zero. A gate that goes green having checked nothing is worse than no gate,
        # because someone will trust it -- the same rule as `git_status_captured: false`.
        print(
            f"# NOTHING CHECKED: {seen} file(s) examined under {root}, none carries a pin.\n"
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
        + (
            f"; {report['partial_pins']} pin(s) declare they may understate the run"
            if report.get("partial_pins")
            else ""
        ),
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
            "# OK = the inputs each artifact pins still hash the same. It does NOT mean the "
            "artifact itself is unedited — for that, `runprov show --stale --rehash`.",
            file=sys.stderr,
        )
    if report["stale"] or report["gone"]:
        return 1
    if not report["ok"]:
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
            f"# NOTHING CHECKED: {pinned} pinned artifact(s) under {root}, and not one "
            f"could be compared.\n"
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
        choices=("text", "timeline", "yaml", "jsonl"),
        default="text",
        metavar="{text,yaml,jsonl}",
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
    sh.add_argument("--format", choices=("text", "yaml"), default="text")
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
    args = ap.parse_args(argv)

    if args.cmd == "exec":
        return _exec(args)

    if args.cmd == "verify":
        return _verify(args)

    # BEFORE THE "no run history" CHECK, because markers outlive the history they name: a
    # deleted `runs.jsonl`, a run started with no `provenance=`, a `--log` pointed at a path
    # that was never written. Those are precisely the markers nothing else will ever clear.
    if args.cmd == "prune":
        return _prune(args)

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
                if m.get("history") and str(m["history"]) != str(path)
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
        sys.stdout.write(json.dumps(g, indent=2) + "\n")
    else:
        sys.stdout.write(_render_lineage(g, names) + "\n")
    print(
        f"# {total} record(s) from {path}" + (f"; {bad} unreadable line(s) skipped" if bad else ""),
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
