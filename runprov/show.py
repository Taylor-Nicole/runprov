# Copyright (c) 2026 Assistance Publique – Hôpitaux de Paris (AP-HP),
#                    Hôpital Henri-Mondor, and Taylor Thompson
# Licensed under the CeCILL-B Free Software License Agreement — see LICENSE.
# https://cecill.info/
"""The lab notebook the history already contained: one view per run, one per project.

`log` is a timeline and `lineage` is a graph, and neither answers the question a developer
actually has three weeks in — **which script expects which input, and does the thing I need
already exist?** In a project with many data sources that question is answered today by
opening several files and reconstructing it, which is the work this is meant to remove.

WHAT IT IS FOR, concretely. Building an artifact can take hours, so the expensive mistake is
not a lost record, it is REBUILDING something that was already correct because nobody could
tell. The project view is an index of what has been made, by what, from what, and whether it
still holds — so the answer to "do I need to run this again" is a page rather than an
excavation.

A READER AND NOTHING ELSE. It adds no field, writes no file and changes no record; it reads
`runs.jsonl` and renders it. That is deliberate: a view that could alter what it displays
would be a view you have to trust, and the record is the thing being trusted here.

TEXT AND YAML, not HTML. The audience runs this over ssh in a terminal beside the work, many
times a day, and a browser is a worse place to read it from than the shell it was launched
in. YAML because the fields are the same ones `log --format yaml` emits, so anything that
reads one reads the other.
"""

from __future__ import annotations

import collections
import json
import pathlib
import typing

from .hashing import describe, moved_since, pin_digest

#: How many characters of a digest identify an artifact version to a human. The same 16 the
#: pin uses, so a digest read here can be matched against a digest read in an artifact.
SHORT = 16

#: How many distinct input versions to name before summarising. A script that has read forty
#: versions of one file has a fact worth stating and a list not worth printing.
VERSIONS_SHOWN = 3


def _short(entry: dict[str, typing.Any]) -> str:
    """The digest a human compares. Content first, like the pin — see `hashing.pin_digest`."""
    return str(
        entry.get("content_sha256") or entry.get("sha256") or entry.get("sha256_tree") or "-"
    )[:SHORT]


def _name(entry: dict[str, typing.Any]) -> str:
    return str(entry.get("path", "?"))


def run_view(record: dict[str, typing.Any]) -> dict[str, typing.Any]:
    """One run, flattened to what a person asks about it."""
    code = {
        "commit": record.get("git_commit") or "none",
        "dirty": bool(record.get("git_code_dirty")),
        # `is False`, never falsy: a run that PREDATES the field does not know the answer,
        # and rendering "clean" for it would be the reassuring lie the flag exists to stop.
        "unknown": record.get("git_status_captured") is False,
    }
    return {
        "run": record.get("script", "?"),
        "status": record.get("status", "ok"),
        "started": record.get("started_utc", "?"),
        "finished": record.get("finished_utc", "?"),
        "run_id": record.get("run_id", "?"),
        "run_uid": str(record.get("run_uid", ""))[:12],
        "generation": record.get("generation", "?"),
        "script_file": record.get("script_file"),
        "code": code,
        "command": record.get("command"),
        "cwd": record.get("cwd"),
        "parameters": record.get("parameters") or {},
        "inputs": [{"path": _name(i), "digest": _short(i)} for i in record.get("inputs") or []],
        "outputs": [
            {"path": _name(o), "digest": _short(o), "kind": o.get("kind", "file")}
            for o in record.get("outputs") or []
        ],
        "notes": record.get("notes") or {},
        "seeds": record.get("seeds") or [],
        "failure": record.get("failure"),
        "terminal_log": (record.get("terminal_log") or {}).get("path"),
    }


def project_view(
    records: typing.Iterable[dict[str, typing.Any]],
) -> dict[str, typing.Any]:
    """The notebook: every script, what it reads, what it writes, and what exists now.

    Built per SCRIPT rather than per run, because "which inputs does this expect" is a
    question about the script and the history is the only place the answer was ever written
    down. A script that has read three different files across ten runs has three answers,
    and all three are worth seeing at once.
    """
    scripts: dict[str, dict[str, typing.Any]] = {}
    artifacts: dict[str, dict[str, typing.Any]] = {}
    # COUNTED as they stream past, because `records` may be a generator and a generator has
    # no length -- and asking for one would materialise the history this exists not to hold.
    seen = 0

    for rec in records:
        seen += 1
        name = rec.get("script", "?")
        s = scripts.setdefault(
            name,
            {
                "runs": 0,
                "ok": 0,
                "failed": 0,
                "first": rec.get("started_utc", "?"),
                "last": rec.get("started_utc", "?"),
                "script_file": rec.get("script_file"),
                "inputs": collections.defaultdict(set),
                "outputs": collections.defaultdict(set),
                "note_keys": set(),
                "parameters": set(),
            },
        )
        s["runs"] += 1
        s["ok" if rec.get("status", "ok") == "ok" else "failed"] += 1
        s["last"] = rec.get("started_utc", s["last"])
        s["script_file"] = rec.get("script_file") or s["script_file"]
        s["note_keys"].update((rec.get("notes") or {}).keys())
        s["parameters"].update((rec.get("parameters") or {}).keys())

        for i in rec.get("inputs") or []:
            s["inputs"][_name(i)].add(_short(i))
        for o in rec.get("outputs") or []:
            s["outputs"][_name(o)].add(_short(o))
            # LAST WRITER WINS, and the records are in order, so this ends up naming the run
            # that actually produced what is on disk now.
            artifacts[_name(o)] = {
                "by": name,
                "digest": _short(o),
                "kind": o.get("kind", "file"),
                "when": rec.get("started_utc", "?"),
                "status": rec.get("status", "ok"),
            }

    for s in scripts.values():
        s["inputs"] = {k: sorted(v) for k, v in sorted(s["inputs"].items())}
        s["outputs"] = {k: sorted(v) for k, v in sorted(s["outputs"].items())}
        s["note_keys"] = sorted(s["note_keys"])
        s["parameters"] = sorted(s["parameters"])

    return {
        "runs": seen,
        "scripts": dict(sorted(scripts.items())),
        "artifacts": dict(sorted(artifacts.items())),
    }


# ------------------------------------------------------- the transformation-log view
# Moved here from `__main__` so a SINK can render a run without importing the CLI --
# `project` imports `sinks`, so `sinks` importing `__main__` would be a cycle. The
# direction is the right one anyway: `__main__` is an entry point that happens to hold
# renderers, and `show` is the module whose job is rendering.


def _q(value: object) -> str:
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


def _structure(value: object) -> str:
    """A nested value, in JSON flow style — which IS valid YAML, and total.

    `params:` and `summary:` are mappings in the old log, not scalars, so `_q()` cannot
    render them: it stringifies, and `inplace: "False"` is a different fact from
    `inplace: false`. The alternative to flow style is emitting block YAML, which means
    writing an indenter — a second hand-rolled serialiser, in the function whose
    docstring above explains why the first one broke the file it replaces.

    This is total for the same reason `_q()` is. Every value here arrived via
    `json.loads` of a history line, so `json.dumps` of it cannot raise; `default=str`
    is belt-and-braces for a caller passing hand-built rows (the tests do).
    """
    return json.dumps(value, default=str, ensure_ascii=False)


def _yaml_header(origin: str = "GENERATED by `python -m runprov log --format yaml`") -> str:
    """The banner, emitted once. Separated so `log --format yaml` can stream its entries.

    `origin` because the same entries are produced two ways and a banner that names the
    wrong one is a small lie in the first line of the file: `log --format yaml` renders on
    demand, while `YamlLogSink` appends one entry per run for the life of the project. A
    reader who wants to know how the file in front of them came to exist is asking a real
    question, and the answer differs.
    """
    return "\n".join(
        [
            f"# {origin}. Do not hand-edit —",
            "# the source of truth is the append-only run history, and every path below",
            "# carries the SHA-256 that was measured when the file was read or written.",
            "#",
            "# Field names are the transformation log's, so anything that could read that file",
            "# can read this one. Two deliberate differences: every scalar is QUOTED, so `date`",
            "# loads as an ISO-8601 string rather than as a bare YAML timestamp, and a key is",
            "# omitted when the run did not use the feature rather than filled with a default.",
            "",
        ]
    )


def _yaml_entry(r: dict[str, typing.Any]) -> str:
    """ONE run in the transformation-log shape. Split out of `_yaml` so that
    `log --format yaml` can write each record as it streams rather than building every
    entry before printing any of them."""
    out: list[str] = []
    ins = [i.get("path", "?") for i in (r.get("inputs") or [])]
    outs = [o.get("path", "?") for o in (r.get("outputs") or [])]
    out.append(f"- step: {_q(r.get('script', '?'))}")
    # `script` is the FILE; `step` is the logical name. The old log had one field for
    # both and they disagreed. The fallback here is deliberately not `step`: a record
    # written before `script_file` reached the history cannot support that claim, and
    # a populated-looking `script:` that names something other than what ran is the
    # exact defect this field replaces.
    # BOTH SHAPES. The history line flattens this to the top level; the SIDECAR keeps
    # it nested under `code`, and reading only the flat one made `to_yaml(run.record)`
    # report a fresh run as having no script file while `log --format yaml` -- the same
    # renderer, over the same run, from the history -- printed the path. Two views of
    # one run disagreeing is the failure this renderer exists to prevent.
    script_file = r.get("script_file") or (r.get("code") or {}).get("script_file")
    # The fallback states the absence and NOT a cause: it read "this run predates
    # script_file", which is true of a v1 record and false of a `python -c` invocation
    # that simply has no file. A populated-looking `script:` naming the wrong thing is
    # the exact defect this field replaces, and so is a confident wrong explanation.
    out.append(f"  script: {_q(script_file or 'not recorded (no script file for this run)')}")
    out.append(f"  date: {_q(r.get('started_utc', '?'))}")
    out.append(f"  input: {_q(', '.join(ins) or 'none registered')}")
    out.append(f"  output: {_q(', '.join(outs) or 'none registered')}")
    out.append(f"  run_command: {_q(r.get('command', '?'))}")
    out.append(f"  cwd: {_q(r.get('cwd', '?'))}")
    out.append(f"  run_id: {_q(r.get('run_id', '?'))}")
    out.append(f"  generation: {_q(r.get('generation', '?'))}")
    out.append(f"  git_commit: {_q(r.get('git_commit', '?'))}")
    out.append(f"  status: {_q(r.get('status', 'ok'))}")
    # `params` and `summary` are the old log's names for these. What changed is where
    # the values come from: `params` is what argparse actually parsed rather than a
    # re-typed prose copy, and `summary` holds `run.note()` values — typed numbers —
    # where the old log had a `description:` paragraph somebody wrote by hand.
    if r.get("parameters"):
        out.append(f"  params: {_structure(r['parameters'])}")
    if r.get("notes"):
        out.append(f"  summary: {_structure(r['notes'])}")
    # The content-addressed environment file, under the name the old log used for the
    # per-invocation pip freeze it replaces — one file per DISTINCT environment rather
    # than 87 timestamped copies holding 8 distinct contents. Omitted, not defaulted:
    # snapshots are opt-in, and a project that never configured them has no such file.
    snap = r.get("environment_snapshot") or {}
    if snap.get("path"):
        out.append(f"  requirements_file: {_q(snap['path'])}")
    # The old log's `terminal_log_file`, under its own name. `terminal_log_capture` has
    # no counterpart there and is emitted beside it deliberately: the old field was a
    # path and nothing else, so a reader could not tell a log that saw everything from
    # one written by a mechanism blind to subprocesses.
    term = r.get("terminal_log") or {}
    if term.get("path"):
        out.append(f"  terminal_log_file: {_q(term['path'])}")
        out.append(f"  terminal_log_capture: {_q(term.get('capture', 'unknown'))}")
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
                + _q(str(i.get("sha256") or "MISSING")[:64] + "  " + str(i.get("path", "")))
            )
    if r.get("outputs"):
        out.append("  output_sha256:")
        for o in r["outputs"]:
            out.append(
                "    - "
                + _q(str(o.get("sha256") or "MISSING")[:64] + "  " + str(o.get("path", "")))
            )
    out.append("")
    return "\n".join(out) + "\n"


# ------------------------------------------------------------------ staleness
#: What the artifact index can say about a file that is on disk now.
CURRENT = "current"
STALE = "STALE"
GONE = "GONE"
MODIFIED = "MODIFIED"
UNKNOWN = "?"


def _resolve(path: str, cwd: str | None) -> pathlib.Path:
    """A recorded path against the cwd of the run that recorded it, never the current one."""
    p = pathlib.Path(path)
    return p if p.is_absolute() or not cwd else pathlib.Path(cwd) / p


def _sidecar(rec: dict[str, typing.Any]) -> dict[str, typing.Any] | None:
    """The producing run's full record, or None if it cannot be had.

    The history line trims an input to path and digests -- deliberately, because it is
    appended forever and the THREE fields `describe()` also carries (`symlink`, `size_bytes`,
    `mtime_utc`) cost 19.3% of the file. Measured 2026-08-18 by re-serialising a real
    2,453-record history with 17,149 input/output entries: 6.39 MB against 7.62 MB.
    (An earlier note here said "four extra fields cost 15.6%". The 15.6% is real but belongs
    to a different measurement -- the YAML rendering growing 5.14 MB to 5.94 MB when four
    FIELDS were added to it -- and had been borrowed for a quantity it does not describe.)
    The SIDECAR keeps the whole `describe()` output, including `size_bytes` and `mtime_utc`,
    which is what makes a stat-only check possible at all.

    The run_uid is checked, and that is not paranoia: a sidecar is overwritten by the next
    run that writes to the same path, so the file sitting there may describe a DIFFERENT
    run. Comparing digests from one run against stats from another would produce confident
    nonsense, so a mismatch reports `?` instead.
    """
    path = rec.get("provenance_path")
    if not path:
        return None
    # AGAINST THE RUN'S RECORDED CWD, exactly as `staleness` resolves every artifact path.
    # This opened the recorded string verbatim, so a run that used the natural relative
    # spelling -- `provenance="out/mid.prov.json"` -- was readable only from its own working
    # directory. Measured: `current` from there, `?` from anywhere else, for every artifact
    # at once. A page whose answers depend on the reader's shell is not a page.
    try:
        doc = json.loads(_resolve(str(path), rec.get("cwd")).read_text(encoding="utf-8"))
    except (OSError, ValueError):  # guards-ok: no sidecar is "cannot tell", not "current"
        return None
    if rec.get("run_uid") and doc.get("run_uid") != rec.get("run_uid"):
        return None
    return doc if isinstance(doc, dict) else None


def staleness(
    records: typing.Iterable[dict[str, typing.Any]], *, rehash: bool = False
) -> dict[str, str]:
    """For each artifact: is it still what the run that made it would make now?

    ANSWERED FROM THE HISTORY, not from the pin, and that is the point. `verify` reads the
    provenance block inside an artifact, so it can only speak about formats that can hold
    one. This walks the run that produced the artifact and re-checks THAT run's inputs, so
    it answers for a BAM, a parquet, a pickle and a figure exactly as well as for a TSV.

    A STAT BY DEFAULT, not a re-hash: `moved_since` compares the size and mtime the record
    already holds, which is one `stat` per input and no reading at all -- because a page
    consulted many times a day must not cost what the build costs. `rehash=True` re-derives
    every digest, has neither limit below, and costs what reading every input costs.

    THE STAT CHECK HAS TWO LIMITS, and both are stated because a checker that narrows what
    it looked at without saying so is the failure this package refuses:

      1. RESOLUTION. `mtime_utc` is recorded to the second, so a rewrite inside the same
         second that preserves the byte count is invisible.
      2. DIRECTORIES CANNOT BE STAT-CHECKED AT ALL. A directory's size and mtime belong to
         its inode: they move when an entry is added or removed and stay exactly where they
         were when a file inside is edited in place. So an artifact with a directory input
         reports `?` rather than `current` -- measured, a reference directory whose only
         file was rewritten end to end used to report `current`, while `--rehash` on the
         same history reported STALE. A definite finding still wins: a moved FILE input is
         STALE and a rewritten artifact is MODIFIED regardless.

    The states answer different questions and imply different repairs:

        current    the artifact is there and its inputs have not moved
        STALE      an input moved -- rebuilding would produce something else
        MODIFIED   the ARTIFACT is not the bytes recorded (rehash only) -- someone or
                   something else wrote it, which is not the same as stale
        GONE       the artifact, or an input it needs, is not there any more
        ?          it cannot be told: the sidecar with the stat fields is missing or has
                   been overwritten by a later run, or an input is a directory the stat
                   check cannot speak for. Saying `current` there would be the reassuring
                   lie this package exists to refuse -- use `--rehash` for a real answer
    """
    # FOUR FIELDS, NOT THE RECORD. This loop streams -- it reads one history line at a time
    # and never holds the file -- and then kept the WHOLE record per artifact, which put the
    # history back in memory by the side door: parameters, notes, every input and output, the
    # git state and the terminal log, retained so that four values could be read back later.
    # Measured on 40,000 runs: 4.4 MB with 2,000 distinct artifacts, 78.1 MB with 40,000.
    #
    # And one artifact per run is not a pathological shape; it is what a script that writes
    # one output does, which over the years this history is meant to survive is the normal
    # case rather than the exception.
    #
    # The keys are spelled exactly as the record spells them, so `_sidecar` and `_by_digest`
    # read this dict without knowing it is not a record. `inputs` only under `rehash`,
    # because that is the only path that looks at it.
    producer: dict[str, dict[str, typing.Any]] = {}
    for rec in records:
        for o in rec.get("outputs") or []:
            producer[_name(o)] = {
                "entry": o,
                "cwd": rec.get("cwd"),
                "run_uid": rec.get("run_uid"),
                "provenance_path": rec.get("provenance_path"),
                **({"inputs": rec.get("inputs")} if rehash else {}),
            }

    # KEYED ON THE RUN, not on `id(rec)`. The old key worked only BECAUSE every record was
    # being kept alive: CPython reuses an id once an object is freed, so the moment the
    # records above stopped being retained, two different runs could collide on one id and
    # the second would silently read the first's sidecar. The uid and the sidecar path are
    # stable and are already here. Records with neither share a key and share the answer
    # `None`, which is what `_sidecar` returns for them anyway.
    detailed: dict[tuple[str | None, str | None], dict[str, typing.Any] | None] = {}
    # ONE DIGEST MEMO FOR THE WHOLE PAGE, beside the sidecar memo above and for the same
    # reason: the artifacts on a page share their inputs, so the redundancy is across
    # artifacts rather than inside one. See `_by_digest`.
    digests: dict[pathlib.Path, str | None] = {}
    out: dict[str, str] = {}
    for path, made in producer.items():
        entry = made["entry"]
        cwd = made.get("cwd")
        base = pathlib.Path(cwd) if cwd else None
        target = _resolve(path, cwd)
        if entry.get("kind") == "MISSING" or not target.exists():
            out[path] = GONE
            continue

        if rehash:
            out[path] = _by_digest(made, entry, target, base, digests)
            continue

        key = (made.get("run_uid"), made.get("provenance_path"))
        if key not in detailed:
            detailed[key] = _sidecar(made)
        doc = detailed[key]
        if doc is None:
            out[path] = UNKNOWN
            continue

        state = CURRENT
        # An input the STAT CHECK CANNOT SPEAK FOR. `moved_since` returns None for anything
        # that is not a plain file, and None means "did not move" -- so a DIRECTORY input
        # read as evidence of freshness. A directory's own size and mtime belong to its
        # inode: they move when an entry is added or removed and stay exactly where they
        # were when a file inside is edited in place. Measured: a reference directory whose
        # only file was rewritten end to end reported `current`, while `--rehash` on the
        # same history correctly reported STALE.
        #
        # `?`, not `current` and not STALE. We did not look, and saying so is the whole
        # argument this package makes elsewhere -- `verify` reports UNVERIFIABLE for the
        # same reason, and `git_status_captured: false` exists for it too. `--rehash` is
        # the answer for anyone who needs a real one, and it re-derives the tree hash.
        uncheckable = False
        for i in doc.get("inputs") or []:
            if i.get("kind") not in ("file", None):
                uncheckable = True
                continue
            why = moved_since(i, base=base)
            if why:
                state = GONE if why == "gone" else STALE
                break
        if state is CURRENT:
            # THE ARTIFACT ITSELF, from the sidecar's own output entry. Without this, a file
            # someone edited by hand read as `current` because its INPUTS had not moved --
            # true, and not the question the reader is asking.
            mine = next((o for o in doc.get("outputs") or [] if _name(o) == _name(entry)), None)
            if mine is not None and moved_since(mine, base=base):
                state = MODIFIED
        # AFTER the artifact check, so a definite finding still wins: MODIFIED is something
        # we established, and `?` is the absence of one.
        if state is CURRENT and uncheckable:
            state = UNKNOWN
        out[path] = state
    return out


def _by_digest(
    rec: dict[str, typing.Any],
    entry: dict[str, typing.Any],
    target: pathlib.Path,
    base: pathlib.Path | None,
    digests: dict[pathlib.Path, str | None],
) -> str:
    """The thorough check: re-derive every digest. No stat resolution limit, and no cheap.

    `digests` MEMOISES BY PATH across the whole page, mirroring the `detailed` sidecar cache
    one level up. The stat path deliberately caches -- and its docstring and test say so --
    while this path had no cache of ANY kind, so a shared input was re-read once per artifact
    that used it. Measured on 200 artifacts drawing from 10 shared inputs: 2,200 reads over
    210 distinct files, with one input read 200 times.

    `--rehash` is documented as the answer for anyone who cannot accept the stat check's
    one-second resolution -- the mode you reach for when correctness matters -- so it is the
    worst place for the redundancy. On 20-byte fixtures it is pure syscall overhead; on a
    2 GB input shared by 300 artifacts it is 600 GB of reads for one page.
    """

    # REQUIRED, not an optional convenience. A `digests=None` default would be a branch no
    # caller takes -- `staleness` always has a memo to pass -- and an unreachable guard is
    # the thing this audit keeps finding. The coverage gate caught it as line 331.
    def now(p: pathlib.Path) -> str | None:
        if p not in digests:
            digests[p] = _digest_now(p)
        return digests[p]

    for i in rec.get("inputs") or []:
        fresh = now(_resolve(_name(i), str(base) if base else None))
        if fresh is None:
            return UNKNOWN
        if fresh != _short(i):
            return STALE
    fresh = now(target)
    if fresh is None:
        return UNKNOWN
    # Not stale: the ARTIFACT is not what was recorded. A different repair entirely.
    return CURRENT if fresh == _short(entry) else MODIFIED


def _digest_now(path: pathlib.Path) -> str | None:
    try:
        return pin_digest(describe(path))
    except (OSError, ValueError):  # guards-ok: unreadable now is "cannot tell", not "same"
        return None


# ------------------------------------------------------------------ rendering
def _rule(title: str, width: int = 78) -> str:
    return f"── {title} " + "─" * max(0, width - len(title) - 4)


def _kv(key: str, value: object, pad: int = 12) -> str:
    return f"  {key:<{pad}} {value}"


def render_run(view: dict[str, typing.Any]) -> str:
    """One run, as a page. The order is the order the questions get asked in."""
    mark = "ok" if view["status"] == "ok" else view["status"].upper()
    out = [_rule(f"{view['run']}  [{mark}]"), ""]
    out.append(_kv("started", view["started"]))
    out.append(_kv("finished", view["finished"]))
    out.append(_kv("run_id", f"{view['run_id']}   generation {view['generation']}"))
    if view["run_uid"]:
        out.append(_kv("run_uid", view["run_uid"]))

    code = view["code"]
    state = (
        "DIRTY"
        if code["dirty"]
        else ("UNKNOWN — git status did not run" if code["unknown"] else "clean")
    )
    out.append(_kv("code", f"{code['commit']}  ({state})"))
    if view["script_file"]:
        out.append(_kv("script", view["script_file"]))
    if view["cwd"]:
        out.append(_kv("cwd", view["cwd"]))
    if view["command"]:
        out.append(_kv("command", view["command"]))

    if view["failure"]:
        f = view["failure"]
        out += ["", _rule("failure"), ""]
        out.append(_kv("type", f.get("type", "?")))
        out.append(_kv("message", str(f.get("message", ""))[:400]))

    if view["parameters"]:
        out += ["", _rule("parameters"), ""]
        pad = max(len(k) for k in view["parameters"])
        out += [_kv(k, json.dumps(v, default=str), pad) for k, v in view["parameters"].items()]

    for label in ("inputs", "outputs"):
        if view[label]:
            out += ["", _rule(f"{label} ({len(view[label])})"), ""]
            for e in view[label]:
                kind = "" if e.get("kind", "file") == "file" else f"  [{e['kind']}]"
                out.append(f"  {e['digest']}  {e['path']}{kind}")

    if view["notes"]:
        out += ["", _rule("notes"), ""]
        pad = max(len(k) for k in view["notes"])
        out += [_kv(k, json.dumps(v, default=str), pad) for k, v in view["notes"].items()]

    if view["seeds"]:
        out += ["", _kv("seeds", view["seeds"])]
    if view["terminal_log"]:
        out += ["", _kv("terminal", view["terminal_log"])]
    return "\n".join(out) + "\n"


def render_project(view: dict[str, typing.Any], states: dict[str, str] | None = None) -> str:
    """The notebook page as one string. A thin wrapper over `render_project_lines`.

    Kept because a caller that wants the whole page as a value is a reasonable thing to be,
    and because every test that asserts on this page asserts on a string. `_show` writes the
    lines instead -- see the generator below for why.
    """
    return "".join(render_project_lines(view, states))


def render_project_lines(
    view: dict[str, typing.Any], states: dict[str, str] | None = None
) -> typing.Iterator[str]:
    """The notebook page, ONE LINE AT A TIME: what each script reads and writes, then what
    exists now.

    A GENERATOR, because the page was built as a list of every line and joined at the end --
    so rendering it cost about six times the text it produced, measured at both 5,000 and
    100,000 artifacts (57.7 MB of peak to emit 9.6 MB). The waste scales with the artifact
    count, which is exactly the part of this page that grows over the years the history is
    meant to survive.

    `log` already learned this: `_timeline_entry` was split out of `_timeline` so each record
    could be written as it streamed rather than accumulating every line before printing any.
    The same reasoning applies here and the artifact section is the half that is unbounded.

    Yields lines WITH their newlines, so a caller can hand the iterator straight to
    `writelines` without rejoining what this exists not to join.
    """

    def line(s: str = "") -> str:
        return s + "\n"

    yield line(_rule(f"project notebook — {view['runs']} run(s), {len(view['scripts'])} script(s)"))

    for name, s in view["scripts"].items():
        status = f"{s['runs']} run(s)"
        if s["failed"]:
            status += f", {s['failed']} FAILED"
        yield line()
        yield line(f"  {name}    {status}")
        yield line(f"    last {s['last']}    first {s['first']}")
        if s["script_file"]:
            yield line(f"    file {s['script_file']}")

        if s["inputs"]:
            yield line("    expects:")
            for path, versions in s["inputs"].items():
                # The COUNT of distinct versions is the fact; the list of forty digests is
                # not. A file read at two different digests is the thing worth noticing.
                extra = (
                    f"   [{len(versions)} versions]"
                    if len(versions) > VERSIONS_SHOWN
                    else f"   {' '.join(versions)}"
                )
                yield line(f"      {path}{extra}")
        if s["outputs"]:
            yield line("    writes:")
            for path in s["outputs"]:
                yield line(f"      {path}")
        if s["parameters"]:
            yield line(f"    params:  {', '.join(s['parameters'])}")
        if s["note_keys"]:
            yield line(f"    notes:   {', '.join(s['note_keys'])}")

    if view["artifacts"]:
        yield line()
        yield line(_rule(f"artifacts on record ({len(view['artifacts'])})"))
        yield line()
        for path, a in view["artifacts"].items():
            flag = "" if a["status"] == "ok" else f"  <- from a {a['status'].upper()} run"
            kind = "" if a["kind"] == "file" else f"  [{a['kind']}]"
            # The column a reader is actually scanning for: do I need to run this again.
            mark = f"{states.get(path, '')!s:<9}" if states else ""
            yield line(f"  {mark}{a['digest']}  {path}{kind}")
            yield line(f"  {' ' * (SHORT + len(mark))}  by {a['by']}  {a['when']}{flag}")


def to_yaml(obj: object, indent: int = 0) -> str:
    """A nested structure as YAML, with EVERY scalar quoted.

    The same rule as `__main__._yaml`, and for the same measured reason: the predecessor's
    log quoted only what its author thought needed quoting, and one hand-written
    `Note:` inside a description is where `yaml.safe_load_all` dies on the real file — line
    14,554 of 24,300. A quoting rule with exceptions is correct until someone types a colon.
    """
    pad = "  " * indent
    if isinstance(obj, dict):
        if not obj:
            return pad + "{}\n"
        out = []
        for k, v in obj.items():
            if isinstance(v, (dict, list)) and v:
                out.append(f"{pad}{json.dumps(str(k))}:\n{to_yaml(v, indent + 1)}")
            else:
                out.append(f"{pad}{json.dumps(str(k))}: {_scalar(v)}\n")
        return "".join(out)
    if isinstance(obj, list):
        if not obj:
            return pad + "[]\n"
        out = []
        for v in obj:
            if isinstance(v, (dict, list)) and v:
                body = to_yaml(v, indent + 1)
                out.append(f"{pad}-\n{body}")
            else:
                out.append(f"{pad}- {_scalar(v)}\n")
        return "".join(out)
    return pad + _scalar(obj) + "\n"


def _scalar(v: object) -> str:
    """`json.dumps` unconditionally. JSON scalars are valid YAML, so this is total."""
    if isinstance(v, bool) or v is None or isinstance(v, (int, float)):
        return json.dumps(v)
    if isinstance(v, (dict, list)):
        return json.dumps(v, default=str)
    return json.dumps(str(v))


def select(
    records: typing.Iterable[dict[str, typing.Any]],
    target: str,
    limit: int | None = None,
) -> list[dict[str, typing.Any]]:
    """Runs matching `target`: a script name, a run_uid prefix, a run_id, or an artifact path.

    Four things one argument can mean, resolved by trying each and returning what matched.
    A developer asking about `build_labels` and a developer asking about `results/x.tsv` are
    asking the same question -- what happened here -- and should not have to say which kind
    of name they happen to be holding.

    ONE PASS, FOUR BUCKETS. The four kinds used to be four comprehensions over a list, which
    meant `list(records)` first -- and `records` is the history generator, so the whole file
    was held to answer a question about one run: 445 MB at 100,000 runs, 2.0 GB at 500,000,
    and the same on the path where NOTHING matches, to print "nothing matches". The comment
    that stood here claimed the opposite ("only what matches is held").

    The buckets are disjoint by construction and a lower-priority one is only ever returned
    when every higher one is empty, so filling all four in one pass returns exactly what four
    ordered passes returned. `limit` bounds each bucket to the last N, which is the same
    slice the caller used to take afterwards -- taken here so a bucket cannot grow past it.
    """
    kinds: tuple[collections.deque[dict[str, typing.Any]], ...] = tuple(
        collections.deque(maxlen=limit) for _ in range(4)
    )
    by_script, by_uid, by_run_id, by_path = kinds
    for r in records:
        if r.get("script") == target:
            by_script.append(r)
        elif str(r.get("run_uid", "")).startswith(target):
            by_uid.append(r)
        elif r.get("run_id") == target:
            by_run_id.append(r)
        elif any(
            _name(e) == target or pathlib.Path(_name(e)).name == target
            for e in (r.get("outputs") or []) + (r.get("inputs") or [])
        ):
            by_path.append(r)
    for bucket in kinds:
        if bucket:
            return list(bucket)
    return []
