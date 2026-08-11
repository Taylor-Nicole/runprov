# Copyright (c) 2026 Assistance Publique – Hôpitaux de Paris (AP-HP),
#                    Hôpital Henri-Mondor, and Taylor Thompson
# Licensed under the CeCILL-B Free Software License Agreement — see LICENSE.
# https://cecill.info/
"""`python -m runprov log` — read the continuous history back.

The history is ONE file, created once and appended to forever: every run this project has
ever recorded, in order, one JSON object per line. That is the same thing the predecessor's
`transformation_log.yml` was for, and the reason it is JSONL rather than YAML is that the
predecessor's file **stopped being readable**. Its writer appended `---` documents into a
file that began as a list, so `yaml.safe_load_all` raises at line 14,575 and eleven repair
scripts exist to heal it — one of which is itself a step in the pipeline it documents.

JSONL cannot fail that way. Each line stands alone: a corrupt line costs one record, never
the file, and a reader can always skip it and say so.

Because a log nobody reads is a log nobody checks, this renders it back:

    python -m runprov log                       the timeline, newest last
    python -m runprov log --format yaml         the transformation-log shape
    python -m runprov log --failed              only the runs that died
    python -m runprov log --script build_labels --limit 5
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys
import typing

from .project import active


def _load(path: pathlib.Path) -> tuple[list[dict[str, typing.Any]], int]:
    """Returns (records, unreadable_line_count). A bad line is COUNTED, never dropped."""
    rows, bad = [], 0
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if not line.strip():
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            bad += 1
    return rows, bad


def _timeline(rows: list[dict[str, typing.Any]]) -> str:
    out = []
    for r in rows:
        status = r.get("status", "ok")
        mark = "  " if status == "ok" else "!!"
        out.append(f"{mark} {r.get('started_utc', '?'):20} {r.get('script', '?')}")
        out.append(
            f"     run_id     {r.get('run_id', '?')}   generation {r.get('generation', '?')}"
        )
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
    return "\n".join(out)


def _yaml(rows: list[dict[str, typing.Any]]) -> str:
    """The shape of the transformation log this replaces, generated from the real record.

    Deliberately the same field names — `step`, `input`, `output`, `script`, `run_command`,
    `date` — so anyone who reads the old file can read this one. The difference is where the
    values come from: these were observed and hashed, not typed by hand.
    """

    def q(value: object) -> str:
        """ALWAYS json.dumps. Never a cleverer rule.

        The first version quoted only when it spotted `:`, `#`, a newline or a quote — and
        emitted `cwd: ?` for a record that predates the cwd field. A bare `?` opens a YAML
        complex key, so `yaml.safe_load` died at line 10 of the rendered file. That is the
        SAME defect as the log this replaces: hand-rolled serialisation that is correct for
        the values its author happened to think of.

        JSON strings are valid YAML scalars (YAML 1.2 is a JSON superset), so quoting
        unconditionally is both simpler and total. `test_rendered_yaml_parses_with_nasty_values`
        holds the line.
        """
        return json.dumps("" if value is None else str(value))

    def structure(value: object) -> str:
        """A nested value, in JSON flow style — which IS valid YAML, and total.

        `params:` and `summary:` are mappings in the old log, not scalars, so `q()` cannot
        render them: it stringifies, and `inplace: "False"` is a different fact from
        `inplace: false`. The alternative to flow style is emitting block YAML, which means
        writing an indenter — a second hand-rolled serialiser, in the function whose
        docstring above explains why the first one broke the file it replaces.

        This is total for the same reason `q()` is. Every value here arrived via
        `json.loads` of a history line, so `json.dumps` of it cannot raise; `default=str`
        is belt-and-braces for a caller passing hand-built rows (the tests do).
        """
        return json.dumps(value, default=str, ensure_ascii=False)

    out = [
        "# GENERATED by `python -m runprov log --format yaml`. Do not hand-edit —",
        "# the source of truth is the append-only run history, and every path below",
        "# carries the SHA-256 that was measured when the file was read or written.",
        "#",
        "# Field names are the transformation log's, so anything that could read that file",
        "# can read this one. Two deliberate differences: every scalar is QUOTED, so `date`",
        "# loads as an ISO-8601 string rather than as a bare YAML timestamp, and a key is",
        "# omitted when the run did not use the feature rather than filled with a default.",
        "",
    ]
    for r in rows:
        ins = [i.get("path", "?") for i in (r.get("inputs") or [])]
        outs = [o.get("path", "?") for o in (r.get("outputs") or [])]
        out.append(f"- step: {q(r.get('script', '?'))}")
        # `script` is the FILE; `step` is the logical name. The old log had one field for
        # both and they disagreed. The fallback here is deliberately not `step`: a record
        # written before `script_file` reached the history cannot support that claim, and
        # a populated-looking `script:` that names something other than what ran is the
        # exact defect this field replaces.
        out.append(
            f"  script: {q(r.get('script_file') or 'not recorded (this run predates script_file)')}"
        )
        out.append(f"  date: {q(r.get('started_utc', '?'))}")
        out.append(f"  input: {q(', '.join(ins) or 'none registered')}")
        out.append(f"  output: {q(', '.join(outs) or 'none registered')}")
        out.append(f"  run_command: {q(r.get('command', '?'))}")
        out.append(f"  cwd: {q(r.get('cwd', '?'))}")
        out.append(f"  run_id: {q(r.get('run_id', '?'))}")
        out.append(f"  generation: {q(r.get('generation', '?'))}")
        out.append(f"  git_commit: {q(r.get('git_commit', '?'))}")
        out.append(f"  status: {q(r.get('status', 'ok'))}")
        # `params` and `summary` are the old log's names for these. What changed is where
        # the values come from: `params` is what argparse actually parsed rather than a
        # re-typed prose copy, and `summary` holds `run.note()` values — typed numbers —
        # where the old log had a `description:` paragraph somebody wrote by hand.
        if r.get("parameters"):
            out.append(f"  params: {structure(r['parameters'])}")
        if r.get("notes"):
            out.append(f"  summary: {structure(r['notes'])}")
        # The content-addressed environment file, under the name the old log used for the
        # per-invocation pip freeze it replaces — one file per DISTINCT environment rather
        # than 87 timestamped copies holding 8 distinct contents. Omitted, not defaulted:
        # snapshots are opt-in, and a project that never configured them has no such file.
        snap = r.get("environment_snapshot") or {}
        if snap.get("path"):
            out.append(f"  requirements_file: {q(snap['path'])}")
        # The old log's `terminal_log_file`, under its own name. `terminal_log_capture` has
        # no counterpart there and is emitted beside it deliberately: the old field was a
        # path and nothing else, so a reader could not tell a log that saw everything from
        # one written by a mechanism blind to subprocesses.
        term = r.get("terminal_log") or {}
        if term.get("path"):
            out.append(f"  terminal_log_file: {q(term['path'])}")
            out.append(f"  terminal_log_capture: {q(term.get('capture', 'unknown'))}")
            # Same reason as `capture`, one step further: this log stops before its run
            # does, because an inner capture still held the descriptors. Without the flag
            # a reader sees a log whose last line predates `finished_utc` and reads it as
            # truncation. Emitted only when true — a caveat on every entry is noise.
            if term.get("out_of_order"):
                out.append("  terminal_log_out_of_order: true")
        if r.get("inputs"):
            out.append("  input_sha256:")
            for i in r["inputs"]:
                out.append(
                    "    - "
                    + q(str(i.get("sha256") or "MISSING")[:64] + "  " + str(i.get("path", "")))
                )
        if r.get("outputs"):
            out.append("  output_sha256:")
            for o in r["outputs"]:
                out.append(
                    "    - "
                    + q(str(o.get("sha256") or "MISSING")[:64] + "  " + str(o.get("path", "")))
                )
        out.append("")
    return "\n".join(out)


def _lineage(rows: list[dict[str, typing.Any]]) -> dict[str, typing.Any]:
    """Reconstruct the run DAG. JOIN ON THE DIGEST, not the path (L1).

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

    def digest(io_: dict[str, typing.Any]) -> str | None:
        d = io_.get("content_sha256") or io_.get("sha256") or io_.get("sha256_tree")
        return str(d) if d else None

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

    no_uid = sum(1 for r in rows if not r.get("run_uid"))
    usable = rows

    # digest -> [(finished, run_uid)], oldest first
    produced: dict[str, list[tuple[str, str]]] = {}
    for r in usable:
        when = str(r.get("finished_utc") or r.get("started_utc") or "")
        for o in r.get("outputs") or []:
            d = digest(o)
            if d:
                produced.setdefault(d, []).append((when, address(r)))
    for v in produced.values():
        v.sort()

    edges: list[tuple[str, str]] = []
    resolvable = ambiguous = orphan = 0
    for r in usable:
        started = str(r.get("started_utc") or "")
        for i in r.get("inputs") or []:
            d = digest(i)
            candidates = [c for c in produced.get(d, []) if d] if d else []
            # rule 2: only a producer that had FINISHED. `<=` because a stage may write and
            # a later stage in the same second may read -- second resolution is the reason
            # the ad-hoc run id collides in the first place.
            me = address(r)
            before = [c for c in candidates if c[0] <= started and c[1] != me]
            if not before:
                orphan += 1
                continue
            edges.append((before[-1][1], me))
            resolvable += 1
    return {
        "runs": len(rows),
        "records_without_uid": no_uid,
        "resolvable": resolvable,
        "ambiguous": ambiguous,
        "orphan": orphan,
        "edges": edges,
    }


def _render_lineage(rows: list[dict[str, typing.Any]], g: dict[str, typing.Any]) -> str:
    by_uid: dict[str, dict[str, typing.Any]] = {}
    for r in rows:
        uid = r.get("run_uid")
        key = (
            str(uid)
            if uid
            else "derived:"
            + "|".join(str(r.get(k, "")) for k in ("script", "started_utc", "provenance_path"))
        )
        by_uid[key] = r
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
        ra, rb = by_uid.get(a, {}), by_uid.get(b, {})
        out.append(
            f"  {ra.get('script', '?')} [{str(a)[:8]}] -> {rb.get('script', '?')} [{str(b)[:8]}]"
        )
    if len(g["edges"]) > 200:
        out.append(f"  ... {len(g['edges']) - 200} more edge(s) not shown")
    out.append("")
    out.append(f"{len(g['edges'])} edge(s)")
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m runprov")
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
    args = ap.parse_args(argv)

    path = pathlib.Path(args.log) if args.log else active().resolved_run_log()
    if not path.is_file():
        print(
            f"no run history at {path}\n"
            f"  It is created by the first `run.write(...)`. Nothing has been recorded "
            f"here yet — which is a different thing from a run that was not recorded, "
            f"and worth telling apart.",
            file=sys.stderr,
        )
        return 1

    rows, bad = _load(path)
    total = len(rows)

    if args.cmd == "lineage":
        g = _lineage(rows)
        if args.format == "json":
            sys.stdout.write(json.dumps(g, indent=2) + "\n")
        else:
            sys.stdout.write(_render_lineage(rows, g) + "\n")
        print(
            f"# {total} record(s) from {path}"
            + (f"; {bad} unreadable line(s) skipped" if bad else ""),
            file=sys.stderr,
        )
        return 0
    if args.script:
        rows = [r for r in rows if r.get("script") == args.script]
    if args.run_id:
        rows = [r for r in rows if r.get("run_id") == args.run_id]
    if args.failed:
        rows = [r for r in rows if r.get("status") == "failed"]
    if args.limit:
        rows = rows[-args.limit :]

    if args.format == "yaml":
        sys.stdout.write(_yaml(rows))
    elif args.format == "jsonl":
        for r in rows:
            sys.stdout.write(json.dumps(r) + "\n")
    else:
        sys.stdout.write(_timeline(rows))

    n_failed = sum(1 for r in rows if r.get("status") == "failed")
    print(
        f"# {len(rows)} of {total} run(s) from {path}"
        + (f"; {n_failed} FAILED" if n_failed else "")
        + (f"; {bad} unreadable line(s) skipped" if bad else ""),
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
