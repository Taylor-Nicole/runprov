# Copyright (c) 2026 Assistance Publique – Hôpitaux de Paris (AP-HP),
#                    Hôpital Henri-Mondor, and Taylor Nicole Thompson
# SPDX-License-Identifier: BSD-3-Clause
# Licensed under the BSD 3-Clause License — see LICENSE.
"""One artifact, one page, for a quality file.

THE QUESTION THIS ANSWERS is the one an accreditation assessor asks, and it is not the one
`log` or `show` are shaped for: *for this reported result, show me which data and which
version of the method produced it, and show me the record could not have drifted from what
ran.* `log` is a timeline, `show` is a notebook page per run or per script, and `verify` is a
verdict. An assessor wants those three joined, about ONE file, on a page that can be printed
and filed beside the result.

IT IS A DERIVED VIEW AND NOTHING MORE. Every fact on the page is read from the artifact's own
pin and the run history — nothing is computed here that is not already recorded, and no field
exists that `show` and `verify` cannot also produce. A report that could say something the
record does not would be a second source of truth, which is the thing this package exists to
remove.

WHAT IT SAYS IT CANNOT TELL YOU, on the page itself rather than in documentation nobody reads
beside it. A quality document that overstates is worse than none, because it is the version
that gets cited; and the limits are not incidental — "it records, it does not audit" is the
package's own boundary and belongs where the claim is made.
"""

from __future__ import annotations

__all__: list[str] = []

import json
import pathlib
import typing

from . import hashing
from . import verify as verify_mod

#: The width of the rules, matching `show`'s so a printed page from either looks like the
#: same document.
WIDTH = 78


def _rule(title: str) -> str:
    """A titled rule. There is no untitled form: the first version had one, nothing called
    it, and a branch nothing reaches is a shape nobody tests."""
    return f"── {title} " + "─" * max(0, WIDTH - len(title) - 4)


def _kv(key: str, value: typing.Any, pad: int = 13) -> str:  # noqa: ANN401 - anything printable
    return f"  {key.ljust(pad)} {value}"


def find_run(
    history: typing.Iterable[dict[str, typing.Any]],
    artifact: str,
    digest: str | None = None,
) -> dict[str, typing.Any] | None:
    """The LAST run that wrote `artifact`, or None.

    The last rather than the first: a file rewritten by a later run is described by that run,
    and a page naming the first would describe bytes that are no longer there. Runs are
    appended in order, so the last match is the most recent.

    MATCHED ON THE DIGEST FIRST, THEN THE WHOLE PATH — never the basename. Audit B, A-05: this
    compared `pathlib.Path(name).name`, so `sample_01/summary.csv` and `sample_99/summary.csv`
    were the same artifact. Reproduced: a page for sample 1 named the sample-99 script, its
    command and its commit, exited 0, and listed sample 1's inputs underneath — internally
    contradictory and filed with a quality record. Per-sample output directories are the
    ordinary layout in this package's own field, so the defect fires on the common case.

    The digest comes first because it is the identity this package actually believes in:
    `lineage` joins on digests rather than paths for the reason L1 records — a path is
    rewritten by many runs over a project's life. It also finds the run for a file that has
    since been MOVED, which a path comparison cannot.

    There is deliberately NO basename fallback. Finding nothing is a state this page already
    renders honestly; finding the wrong run is not recoverable by a reader.
    """
    want = _resolve(artifact)
    # COLLECTED SEPARATELY, THEN RANKED. C-01 of Audit C: the two kinds of match were both
    # last-wins inside ONE loop, so a run that wrote BYTE-IDENTICAL bytes at a different path
    # later in the history overrode — and DESTROYED — the exact-path match for the artifact the
    # reader actually named. That is A-05 returning by another road: a basename collision became
    # a content collision, and unlike the old code this one discarded a correct answer.
    #
    # An exact recorded path is the strongest identity available, so it wins outright. The
    # digest is the FALLBACK, for an artifact that has been moved since — the case it was added
    # for. And a digest matching outputs at two or more DISTINCT paths identifies nothing, so it
    # returns None: this page renders "no run found" honestly, and naming the wrong run does not.
    by_path = None
    by_digest: dict[str, dict[str, typing.Any]] = {}
    for record in history:
        # AGAINST THE RECORDED CWD, NEVER THE CURRENT ONE. A record holds the path as the run
        # saw it, which is often relative; resolving it here would anchor it to wherever the
        # reader happens to stand. The suite already holds this rule for inputs
        # (`test_a_relative_input_is_rechecked_against_the_recorded_cwd_not_the_current_one`)
        # and the first version of this fix did not carry it over.
        base = record.get("cwd")
        for out in record.get("outputs") or []:
            name = out.get("path") if isinstance(out, dict) else out
            if name and _resolve(str(name), base) == want:
                by_path = record
            elif (
                isinstance(out, dict)
                and digest
                and any(
                    out.get(key) == digest for key in ("sha256", "content_sha256", "sha256_tree")
                )
            ):
                by_digest[_resolve(str(name), base) if name else ""] = record
    if by_path is not None:
        return by_path
    if len(by_digest) == 1:
        return next(iter(by_digest.values()))
    return None  # nothing matched, or the bytes sit at two paths and identify no single run


def _resolve(name: str, base: str | None = None) -> str:
    """A path in one comparable form, anchored to `base` when it is relative."""
    try:
        path = pathlib.Path(name)
        if base and not path.is_absolute():
            path = pathlib.Path(base) / path
        return str(path.resolve())
    except (OSError, ValueError):  # a name that is not a usable path compares as itself
        return name


class Page(typing.NamedTuple):
    """The page, and the verdict it is about.

    The status is RETURNED rather than read back out of the rendered text. The first version
    of the CLI handler decided its exit code by string-matching its own output, which makes
    the wording load-bearing: rephrasing a line would silently change what the command
    returns to a build.
    """

    lines: list[str]
    status: str

    @property
    def ok(self) -> bool:
        return self.status == verify_mod.OK


def page(
    artifact: pathlib.Path,
    root: pathlib.Path,
    history: typing.Iterable[dict[str, typing.Any]] = (),
) -> Page:
    """The page. Separate from printing so the wording itself is testable."""
    result = verify_mod.verify_artifact(artifact, root)
    status = result.get("status", "?")
    # THE DIGEST OF THE BYTES ON DISK NOW, so a moved or renamed artifact still finds its run,
    # and two files sharing a basename cannot be confused for one another. A-05.
    try:
        digest: str | None = hashing.sha256(artifact)
    except OSError:
        digest = None
    run = find_run(history, str(artifact), digest)

    out = [_rule(f"provenance report — {artifact.name}"), ""]
    out.append(_kv("artifact", artifact))
    out.append(_kv("verdict", status))
    if result.get("reason"):
        out.append(_kv("", f"({result['reason']})"))

    if run is None:
        # SAID, NOT OMITTED. A page that simply left the run section out would read as though
        # the artifact had no producer rather than as though none was FOUND, and those are
        # different facts — the second is usually a history that was not passed in.
        out += [
            "",
            _rule("the run that produced it"),
            "",
            _kv("", "NOT FOUND in the run history supplied."),
            _kv("", "The pin above still stands on its own; the rest of this page cannot."),
        ]
        out += ["", *_limits(found_run=False)]
        return Page(out, status)

    out += ["", _rule("the run that produced it"), ""]
    out.append(_kv("script", run.get("script", "?")))
    out.append(_kv("status", run.get("status", "?")))
    out.append(_kv("started", run.get("started_utc", "?")))
    out.append(_kv("finished", run.get("finished_utc", "?")))
    out.append(_kv("run_id", run.get("run_id", "?")))
    out.append(_kv("command", run.get("command", "?")))
    out.append(_kv("cwd", run.get("cwd", "?")))

    out += ["", _rule("the method, and whether it can be got back"), ""]
    commit = run.get("git_commit") or "none recorded"
    if run.get("git_status_captured") is False:
        state = "UNKNOWN — git status did not run"
    elif run.get("git_code_dirty"):
        state = "DIRTY — the code that ran matches no commit"
    else:
        state = "clean"
    out.append(_kv("code", f"{commit}  ({state})"))

    tool = run.get("tool")
    if tool:
        how = tool.get("source", "?")
        if not tool.get("identifies_code"):
            how += ", DOES NOT IDENTIFY THE CODE"
        elif tool.get("commit"):
            how += f" {str(tool['commit'])[:12]}"
        out.append(_kv("recorded by", f"runprov {tool.get('version', '?')}  ({how})"))
    else:
        # A RECORD FROM BEFORE 0.3.0. Saying so is the point of U-01: the alternative is a
        # page that is silent about its own provenance and looks complete.
        out.append(_kv("recorded by", "not recorded — this run predates the `tool` block"))

    # THE HISTORY DOES NOT CARRY `environment` — it is a whitelist projection and that field
    # stays in the sidecar, which is the right trade for a file appended forever. Saying so is
    # not padding: a page that silently omitted the interpreter would read as though the run
    # had not recorded one, and "not on this page" is a different fact from "not recorded".
    env = run.get("environment") or {}
    if env:
        out.append(_kv("python", env.get("python", "?")))
        out.append(_kv("platform", env.get("platform", "?")))
    else:
        out.append(_kv("environment", "in the sidecar beside the artifact, not in the history"))

    inputs = result.get("inputs") or []
    out += ["", _rule(f"inputs it was made from ({len(inputs)})"), ""]
    if not inputs:
        out.append(_kv("", "none pinned"))
    for entry in inputs:
        # THE KEYS ARE `name` AND `pinned`, read from `verify_artifact` rather than guessed:
        # the first version of this line invented `path` and `want` and printed "OK ? ?" for
        # a perfectly good input — a page that looks filled in and says nothing.
        mark = entry.get("status", "?")
        digest = str(entry.get("pinned", "")) or "?"
        line = f"  {mark:<12} {digest}  {entry.get('name', '?')}"
        if entry.get("status") != verify_mod.OK and entry.get("found"):
            line += f"   (now {entry['found']})"
        out.append(line)

    obs = run.get("observation") or {}
    unregistered = run.get("unregistered_reads") or []
    out += ["", _rule("what the run was able to observe"), ""]
    out.append(_kv("steps", obs.get("steps", "not recorded")))
    out.append(_kv("packages", obs.get("packages_recorded", "not recorded")))
    if unregistered:
        # THE MOST IMPORTANT LINE ON THE PAGE WHEN IT IS PRESENT, so it is not buried in a
        # count: these files were read and are NOT in the pin above.
        out.append(
            _kv("UNREGISTERED", f"{len(unregistered)} file(s) were read and are NOT pinned:")
        )
        out += [f"               {p}" for p in unregistered[:10]]
    if obs.get("unregistered_watch_truncated"):
        # C-06 of Audit C. It belongs HERE above all: the list printed above is the page's
        # answer to "was anything read that is not pinned?", and when the watch hit its cap
        # that list is a sample rather than the answer. Without this line an empty
        # UNREGISTERED reads as "nothing was missed" on the one page a reader consults to
        # judge a single artifact — the conflation the `observation` block exists to end.
        out.append(
            _kv(
                "WATCH TRUNCATED",
                f"at least {obs['unregistered_watch_truncated']} further path(s) were "
                "dropped; the line above is a sample, not a census",
            )
        )

    out += ["", *_limits(found_run=True)]
    return Page(out, status)


def _limits(*, found_run: bool) -> list[str]:
    """What the page cannot tell you — on the page, not in a manual beside it."""
    lines = [
        _rule("what this page cannot tell you"),
        "",
        "  It records; it does not audit. A pinned input was READ by the run — this page",
        "  cannot say it was the input that mattered, or that the method was correct.",
        "",
        "  A verdict of OK means every pinned input still hashes to what it did. It does",
        "  not mean everything the run read was pinned: a read that bypassed registration",
        "  is reported above when it was seen, and cannot be seen at all for a script that",
        "  never imported runprov.",
    ]
    if not found_run:
        lines += [
            "",
            "  No run record was found for this artifact, so everything except the pin and",
            "  the verdict is absent rather than clean.",
        ]
    return lines


def render(artifact: pathlib.Path, root: pathlib.Path, log: pathlib.Path | None) -> Page:
    """Read the history if there is one, and build the page."""
    history: list[dict[str, typing.Any]] = []
    if log is not None and log.is_file():
        for line in log.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except ValueError:
                continue  # a torn line is not a reason to refuse the page
            if isinstance(record, dict):
                history.append(record)
    return page(artifact, root, history)
