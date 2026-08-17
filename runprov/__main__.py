# Copyright (c) 2026 Assistance Publique – Hôpitaux de Paris (AP-HP),
#                    Hôpital Henri-Mondor, and Taylor Thompson
# Licensed under the CeCILL-B Free Software License Agreement — see LICENSE.
# https://cecill.info/
"""`python -m runprov log` — read the continuous history back.

The history is ONE file, created once and appended to forever: every run this project has
ever recorded, in order, one JSON object per line. That is the same thing the predecessor's
`transformation_log.yml` was for, and the reason it is JSONL rather than YAML is that the
predecessor's file **stopped being readable**. Its writer appended `---` documents into a
file that began as a list. Measured on the real 24,300-line file: `safe_load` dies at line
14,547 on those `---` documents, and `safe_load_all` dies at 14,554 on something else
entirely -- an unquoted `Note:` inside a hand-written description. Eleven repair scripts
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

import argparse
import collections
import json
import pathlib
import shlex
import subprocess
import sys
import typing

from .project import active
from .run import Run
from .show import (
    _yaml_entry,
    _yaml_header,
    project_view,
    render_project,
    render_run,
    run_view,
    select,
    staleness,
)
from .show import to_yaml as _yaml_doc
from .verify import render, verify


def _stream(path: pathlib.Path) -> typing.Iterator[dict[str, typing.Any] | None]:
    """Every line of the history, parsed, one at a time. `None` marks a line that would not.

    STREAMED, because the history is appended forever and this is the one place that reads
    all of it. The first version did `read_text().splitlines()`, which holds the whole file
    as one string AND a list of every line before a single record is parsed -- two full
    copies of a file whose entire design is that it never stops growing. Measured on a
    realistic 100,000-run history of 91 MB: 488 MB of peak memory to read it.

    That is the same defect `content_digest` had and for the same reason: a convenient
    whole-file read, in the function that meets the biggest file.
    """
    with path.open(encoding="utf-8", errors="replace") as fh:
        for line in fh:
            if not line.strip():
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                yield None


def _load(path: pathlib.Path) -> tuple[list[dict[str, typing.Any]], int]:
    """Returns (records, unreadable_line_count). A bad line is COUNTED, never dropped.

    Materialises what `_stream` yields, for the callers that genuinely need every record at
    once. `log --limit N` does not, and does not use this.
    """
    rows, bad = [], 0
    for rec in _stream(path):
        if rec is None:
            bad += 1
        else:
            rows.append(rec)
    return rows, bad


def _timeline(rows: typing.Iterable[dict[str, typing.Any]]) -> str:
    return "".join(_timeline_entry(r) for r in rows)


def _timeline_entry(r: dict[str, typing.Any]) -> str:
    """ONE run, rendered alone. Split out of `_timeline` so `log` can write each record
    as it streams past instead of building every line before printing any of them."""
    out: list[str] = []
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
        out.append(f"     in   {str(i.get('sha256') or '')[:16]}  {i.get('path', '?')}")
    for o in outs:
        out.append(f"     out  {str(o.get('sha256') or '')[:16]}  {o.get('path', '?')}")
    if status != "ok":
        f = r.get("failure") or {}
        out.append(f"     FAILED     {f.get('type', '?')}: {str(f.get('message', ''))[:160]}")
    if r.get("history_destination"):
        out.append(f"     history    {r['history_destination']}")
    if r.get("seeds"):
        out.append(f"     seeds      {r['seeds']}")
    out.append("")
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

    def records() -> typing.Iterator[dict[str, typing.Any]]:
        """A FRESH pass over the history. Called exactly twice — see the two PASS comments
        below. Returning a new iterator each time is the whole contract: handing back a
        part-consumed one would make pass two silently empty."""
        if isinstance(source, pathlib.Path):
            return _counted(source, bad if bad is not None else [0])
        return iter(source)

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
    for a, b in g["edges"][:200]:
        out.append(f"  {by_uid.get(a, '?')} [{str(a)[:8]}] -> {by_uid.get(b, '?')} [{str(b)[:8]}]")
    if len(g["edges"]) > 200:
        out.append(f"  ... {len(g['edges']) - 200} more edge(s) not shown")
    out.append("")
    out.append(f"{len(g['edges'])} edge(s)")
    return "\n".join(out)


class CommandFailedError(Exception):
    """The wrapped command exited non-zero.

    Raised INSIDE the `with` so `__exit__` records the run as failed, then caught so the
    wrapper exits with the COMMAND's code rather than a Python traceback. Named without a
    leading underscore because it reaches the terminal: `__exit__` prints the exception
    type, and `_CommandFailedError: sort exited 2` reads like a leaked internal when it is in
    fact the whole message.
    """


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
    provenance = (
        pathlib.Path(args.provenance)
        if args.provenance
        else pathlib.Path(project.root) / "provenance" / f"{name}_{project.run_id()}.json"
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
            for i in args.input:
                run.input(i)
            for o in args.output:
                run.output(o)
            try:
                # Inheriting this process's stdout and stderr, so the command still writes
                # to the terminal it was launched from -- and so `--capture`, which works at
                # file-descriptor level, sees a subprocess's output too.
                returncode = subprocess.run(argv, check=False).returncode
            except OSError as exc:
                run.note("exec_error", f"{type(exc).__name__}: {exc}")
                raise CommandFailedError(str(exc)) from exc
            run.note("exit_code", returncode)
            if returncode != 0:
                raise CommandFailedError(f"{argv[0]} exited {returncode}")
    except CommandFailedError as exc:
        print(f"  runprov exec: recorded a FAILED run — {exc}", file=sys.stderr)
        return returncode if returncode else 1
    return returncode


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
    keep: collections.deque[dict[str, typing.Any]] | None = (
        collections.deque(maxlen=args.limit) if args.limit else None
    )
    bad = total = shown = n_failed = 0

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
            sys.stdout.write(_timeline_entry(r))

    if args.format == "yaml":
        # ONCE, by the command rather than the renderer: streaming writes each record as
        # it passes, and a banner emitted per record is not a banner.
        sys.stdout.write(_yaml_header())

    for rec in _stream(path):
        if rec is None:
            bad += 1
            continue
        total += 1
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
    return 0


def _counted(path: pathlib.Path, bad: list[int]) -> typing.Iterator[dict[str, typing.Any]]:
    """The history, streamed, with unreadable lines counted into `bad` rather than dropped."""
    for rec in _stream(path):
        if rec is None:
            bad[0] += 1
        else:
            yield rec


def _show(args: argparse.Namespace, path: pathlib.Path) -> int:
    """`show`, which reads the history and renders it. It writes nothing, by design.

    With no target it is the PROJECT page -- every script, what it expects, what it writes,
    and every artifact on record with the run that produced it. That is the question a
    developer has three weeks in: does this already exist, and what did it come from.

    With a target it is one page per matching run, oldest last, because the last one is the
    state you are in.
    """
    bad = [0]
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

    view = project_view(_counted(path, bad))
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
        sys.stdout.write(render_project(view, states))
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
    return 0


def _verify(args: argparse.Namespace) -> int:
    """`verify`, and it deliberately never touches the history.

    The pin is in the artifact, which is the whole point of putting it there: a committed
    result can be checked by someone who has the repository and nothing else — no sidecar,
    no `runs.jsonl`, no `configure()`. Requiring the history here would have made the check
    depend on the one file the pin exists to survive.
    """
    root = pathlib.Path(args.root) if args.root else active().root
    report = verify([pathlib.Path(p) for p in args.paths] or [root], root)

    if args.format == "json":
        sys.stdout.write(json.dumps(report, indent=2) + "\n")
    else:
        sys.stdout.write(render(report))

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
        return 1

    # The skipped count is REPORTED, never merely applied. A checker that quietly narrows
    # what it looked at reads as "everything is fine" when it means "I did not look there".
    skipped = report["files_skipped"]
    print(
        f"# {pinned} pinned artifact(s) of {seen} file(s) under {root}"
        + (f" ({skipped} skipped in build/vcs/venv dirs)" if skipped else "")
        + f": {report['ok']} OK, {report['stale']} STALE, {report['gone']} GONE, "
        f"{report['unverifiable']} UNVERIFIABLE",
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
        return 1
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
    lg.add_argument("--format", choices=("timeline", "yaml", "jsonl"), default="timeline")
    lg.add_argument("--limit", type=int, default=0, help="show only the last N runs")
    lg.add_argument("--script", default="", help="filter by script name")
    lg.add_argument("--run-id", default="", help="filter by run id")
    lg.add_argument("--failed", action="store_true", help="only runs that failed")
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
    ex = sub.add_parser("exec", help="run a non-Python command AS a recorded run")
    ex.add_argument("--name", default=None, help="the step name (default: the program's)")
    ex.add_argument("--input", action="append", default=[], help="repeatable")
    ex.add_argument("--output", action="append", default=[], help="repeatable")
    ex.add_argument("--provenance", default=None, help="where the sidecar goes")
    ex.add_argument("--capture", default=None, help="tee the command's output to this file")
    ex.add_argument("command", nargs=argparse.REMAINDER, help="-- then the command to run")
    vf = sub.add_parser("verify", help="do artifacts still match the inputs they pin?")
    vf.add_argument("paths", nargs="*", help="artifacts or directories (default: the root)")
    vf.add_argument("--root", default=None, help="what pinned names are relative to")
    vf.add_argument("--log", default=None, help=argparse.SUPPRESS)  # unused; keeps --log uniform
    vf.add_argument("--format", choices=("text", "json"), default="text")
    args = ap.parse_args(argv)

    if args.cmd == "exec":
        return _exec(args)

    if args.cmd == "verify":
        return _verify(args)

    path = pathlib.Path(args.log) if args.log else active().resolved_run_log()
    if not path.is_file():
        print(
            f"no run history at {path}\n"
            f"  It is created by the first recorded run — `with Run(..., provenance=...)`.\n"
            f"  Nothing has been recorded here yet, which is a different thing from a run\n"
            f"  that was not recorded, and worth telling apart.\n"
            f"  This CLI cannot see what your scripts passed to configure(): with no --log\n"
            f"  it reads the DEFAULT path above. If configure(run_log=...) sent the history\n"
            f"  somewhere else, pass --log that path — the runs are not missing, this is the\n"
            f"  wrong file to look in.",
            file=sys.stderr,
        )
        return 1

    # `show` STREAMS rather than materialising, and it is the command that most needs to:
    # it is the page a developer opens many times a day over a history that only grows.
    # Measured on a 100,000-run, 91 MB history: 392 MB held to build the page, against 3 MB
    # to stream it. Every other command here genuinely needs all the records at once.
    if args.cmd == "show":
        return _show(args, path)

    if args.cmd == "log":
        return _log(args, path)

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
