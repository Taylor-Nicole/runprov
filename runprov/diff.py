# Copyright (c) 2026 Assistance Publique – Hôpitaux de Paris (AP-HP),
#                    Hôpital Henri-Mondor, and Taylor Nicole Thompson
# SPDX-License-Identifier: BSD-3-Clause
# Licensed under the BSD 3-Clause License — see LICENSE.
"""Why is today different from last month. ADR-0014, T-30.

THE QUESTION ASKED MOST OFTEN, and the one nothing surfaced. Same script, same command,
different numbers — and answering it meant opening two records side by side and reading. Every
field needed is already recorded, including the step argument digests ADR-0010 built expressly
so that "did this function see the same inputs?" would be answerable.

A DIFFERENCE AND AN INCOMPARABILITY ARE NOT THE SAME ANSWER, and that is the whole design.
Two records can be laid side by side and still not support a comparison: a run on 3.11 could
not see what a run on 3.12 saw, a dirty tree's commit does not name the code that ran, a
`getrusage` peak and a cgroup peak are different quantities. A diff that does not know this
reports the change of an INTERPRETER as five functions appearing.

INCOMPLETE IS NOT INCOMPARABLE either, and collapsing them throws away the useful half. A run
with an unregistered read did not record all its inputs — but a changed input it DID record is
a true finding. What such a record can never support is the word `unchanged`.

So each dimension carries its own precondition, and two facts give four states:

    complete   + differences -> changed
    complete   + none        -> unchanged, and it says what it examined
    incomplete + differences -> changed, and not fully comparable
    incomplete + none        -> NOT COMPARABLE, never "unchanged"

The last cell is the point: a record that could not see all its inputs must never be the
source of "nothing changed".
"""

from __future__ import annotations

__all__: list[str] = []

import typing

#: The dimensions, in the order a reader wants them: whether it finished, what went in, what
#: came out, what the code was, what it was asked to do, what happened inside, what it cost.
#: Ordered here rather than sorted, so the output reads like an explanation rather than an
#: alphabet.
#:
#: AND `compare()` NOW READS IT, which it did not. ADR-0014's sketch promised `--only` and the
#: constant was left with no reader at all — a promise made in a ratified decision and quietly
#: not kept, found sitting here by an Audit D reviewer. An unread constant does not merely
#: waste a line: this one was also WRONG, because `status` was added to `compare()` by D-02
#: and never added here, so the one statement of "these are the dimensions" had been false
#: since. The order now lives in the only place that claims to hold it, and a dimension
#: `compare()` builds that is missing from this tuple raises rather than being dropped.
DIMENSIONS = (
    # FIRST, because it is the precondition on reading any of the others: "the outputs did not
    # change" means something different when one of the two runs died partway.
    "status",
    "inputs",
    "outputs",
    "code",
    "parameters",
    "packages",
    "steps",
    "resources",
)

#: R-5. The payload's own version, named HERE rather than in `__main__`, because it names THIS
#: module's answer and `--format json` is only one rendering of it. `chain.SCHEMA` and
#: `report.SCHEMA` state the same argument, and G-24(a) states the cost of not stating it: a
#: literal in the emitter and a second literal in its test is one sentence asserted against its
#: own copy, so the payload was free to be versioned wrongly with the suite green.
SCHEMA = "runprov.diff.v1"

#: A recorded value that is NOT a digest. Audit B, A-06: `_value_digest` writes
#: `"UNDIGESTIBLE:<type>"` for anything it will not canonicalise, so two entirely different
#: DataFrames both record `UNDIGESTIBLE:DataFrame` and compare EQUAL. An empty string is the
#: same hole one step along — `_pairs` maps an `UNHASHABLE` or `MISSING` entry to `""`, and
#: `"" == ""` reads as unchanged.
#:
#: Equal markers are not equal values. A dimension holding one cannot support `unchanged`,
#: which is the four-state table this module was written to enforce, applied to itself.
_NOT_A_DIGEST = "UNDIGESTIBLE"


def _undigested(*values: typing.Any) -> bool:  # noqa: ANN401 - whatever the record holds
    """True when any value is a marker rather than a digest, at any depth."""
    for value in values:
        if isinstance(value, str) and (not value or value.startswith(_NOT_A_DIGEST)):
            return True
        if isinstance(value, (list, tuple)) and _undigested(*value):
            return True
        if isinstance(value, dict) and _undigested(*value.values()):
            return True
    return False


CHANGED, UNCHANGED, INCOMPARABLE = "changed", "unchanged", "not comparable"


class Dimension(typing.NamedTuple):
    """One dimension's answer: what moved, what was examined, and what limits the conclusion."""

    name: str
    differences: list[str]
    examined: str
    blocked: str | None  # why this dimension cannot support "unchanged", if it cannot
    #: REPORTED BUT NEVER DECISIVE. A cost is not part of "is this the same run": two runs of
    #: one script never agree on wall time, so letting timing settle the exit code is the same
    #: defect A-07 named — a gate that cannot pass — arriving through a threshold instead of an

    @property
    def verdict(self) -> str:
        if self.differences:
            return CHANGED
        return INCOMPARABLE if self.blocked else UNCHANGED

    @property
    def settled(self) -> bool:
        """True only when this dimension was fully comparable AND found nothing.

        The exit code is built from this, so a gate cannot go green while half the comparison
        was impossible — the vacuous pass, in a new place.
        """
        return self.verdict == UNCHANGED


class Side(typing.NamedTuple):
    """WHO is being compared, in the record's own names. ADR-0017 R-9, R-12.

    The header states four facts about each run and nothing else, so this carries four. It is
    deliberately not the whole record: a structure holding a fact no rendering states is a
    field a reader of either rendering cannot find, and `run_uid` — which the CLI resolves an
    address through and then never prints — is the one that kept asking to be let in.

    THE ABSENCES ARE ABSENCES HERE. `render` used to read `record.get("script", "?")`, which
    is harmless while a table is the only thing built and stops being harmless the moment the
    same facts are serialised: the FIELD'S VALUE is then the string `?` and no consumer can
    tell it from a script genuinely called `?`. `report` paid for that lesson one row ago.
    """

    script: str | None
    run_id: str | None
    started_utc: str | None
    status: str | None


class Comparison(typing.NamedTuple):
    """One comparison: who, what moved, and what the comparison could not support.

    THE TOP LEVEL IS NOT JUST A LIST OF DIMENSIONS, because the table's own header is not:
    every `blocked` message in this module says "A" or "B", and a payload carrying those
    sentences without saying which runs A and B are would be describing two runs a consumer
    cannot name.

    AND THERE IS NO OPTIONAL BLOCK, which is the shape `report` has and this does not.
    `compare()` returns every dimension for every pair of records — a dimension that cannot be
    compared is INCOMPARABLE, never missing — so `blocked: null` here is always *looked, and
    nothing stops this dimension supporting "unchanged"*, and there is no second reading in
    which it means *this version did not look*. That is the whole of ADR-0014 held as a shape
    rather than as prose, and it is why R-8's null-versus-absent needs no judgement call here.
    """

    a: Side
    b: Side
    dimensions: list[Dimension]

    @property
    def settled(self) -> bool:
        """Exit 0 means comparable AND identical, in every dimension. ADR-0014 clause 4.

        READ OFF THE STRUCTURE, not folded in the CLI handler, for `Page.status`'s reason one
        command along: a verdict computed beside the thing it describes is a second copy of
        it, and two copies of one verdict are two things that can disagree. A consumer of the
        payload that recomputes this from `differences` alone gets a different answer from the
        command it is reading — an incomparable dimension is non-zero and has no differences.
        """
        return all(d.settled for d in self.dimensions)


def _obs(record: typing.Mapping[str, typing.Any], key: str) -> typing.Any:  # noqa: ANN401
    return (record.get("observation") or {}).get(key)


def _pairs(record: typing.Mapping[str, typing.Any], key: str) -> dict[str, str]:
    """`{name: digest}` for inputs or outputs, from either the sidecar or the history shape."""
    out: dict[str, str] = {}
    for entry in record.get(key) or []:
        if isinstance(entry, dict):
            name = entry.get("path") or entry.get("name")
            digest = entry.get("sha256") or entry.get("content_sha256") or entry.get("digest")
            if name:
                out[str(name)] = str(digest or "")
    return out


def _compare_maps(a: dict[str, str], b: dict[str, str], label: str) -> list[str]:
    lines = []
    for name in sorted(set(a) | set(b)):
        if name not in a:
            lines.append(f"{name}  ADDED  {b[name][:16]}")
        elif name not in b:
            lines.append(f"{name}  REMOVED")
        elif a[name] != b[name]:
            lines.append(f"{name}  {a[name][:16] or '?'} -> {b[name][:16] or '?'}")
    return lines


def _files(
    a: typing.Mapping[str, typing.Any], b: typing.Mapping[str, typing.Any], key: str
) -> Dimension:
    """Inputs or outputs. INCOMPLETE when either run knows it did not see everything."""
    left, right = _pairs(a, key), _pairs(b, key)
    blocked = None
    # A-06. An entry the run could not hash records an empty digest, and `"" == ""` is not
    # agreement — it is two absences meeting. `_compare_maps` already prints `'?'` for one on
    # the DIFFERING branch, so the case was seen and handled only where it does no harm.
    missing = sorted({n for n, d in left.items() if not d} | {n for n, d in right.items() if not d})
    if missing:
        blocked = f"no digest recorded for {', '.join(missing[:3])}"
    for record, side in ((a, "A"), (b, "B")):
        if record.get("unregistered_reads"):
            n = len(record["unregistered_reads"])
            blocked = f"{side} recorded {n} read(s) that bypassed registration"
        elif _obs(record, "unregistered_watch_truncated"):
            # C-06 of Audit C. A-11 made the watcher SAY when it went blind and then nothing
            # asked. Both runs hitting the cap record `unregistered_reads: []` — the reads
            # that bypassed registration were dropped before they could be reported — so this
            # printed `inputs unchanged (3 vs 3)` and exited 0 over a census both runs knew
            # was incomplete. That is the vacuous pass ADR-0014 was written against, reached
            # through the very field added to prevent it.
            #
            # HERE AND NOT IN `_steps`' mark list, where the row's finding pointed: this is a
            # bound on observed FILE READS, and putting it there would qualify the wrong
            # dimension while leaving this one green.
            blocked = (
                f"{side}'s watch dropped at least "
                f"{_obs(record, 'unregistered_watch_truncated')} path(s), so its inputs are "
                "a partial census"
            )
        elif record.get("pin_partial"):
            blocked = f"{side}'s pin states it may understate the run"
    return Dimension(key, _compare_maps(left, right, key), f"{len(left)} vs {len(right)}", blocked)


def _imported_of(record: typing.Mapping[str, typing.Any]) -> dict[str, typing.Any]:
    """The hashed first-party code block, from EITHER record shape.

    THE HISTORY FLATTENS IT and the sidecar does not: `run.py` writes a top-level
    `imported_code`, while the live record keeps `code.imported`. Reading only one shape is
    how A-04 made `packages` compare `{}` against `{}` on every real history, and how A-03
    crashed `steps` — twice in one audit, in this same function's neighbours. `diff` is fed
    history records; the unit tests build sidecar-shaped ones; both must work.
    """
    flat = record.get("imported_code")
    if isinstance(flat, dict):
        return flat
    nested = (record.get("code") or {}).get("imported")
    return nested if isinstance(nested, dict) else {}


def _code(a: typing.Mapping[str, typing.Any], b: typing.Mapping[str, typing.Any]) -> Dimension:
    """The project's commit AND the digest of the first-party code that actually ran.

    A DIRTY TREE IS WHAT BREAKS THE COMMIT, not anything about the package: the commit is
    recorded, and the files that ran differ from it, so a commit-to-commit line would name a
    change that is not the change.

    AND THE COMMIT IS NOT THE CODE, which is D-04 of Audit D. `git status` reports neither a
    gitignored module nor a script outside the repository, so `git_code_dirty` is False, the
    commits match, and this reported `unchanged` — while the same history line carried a
    different `imported_code.digest`. `run.code()` on an out-of-repo script is the documented
    purpose of that API, and README.md says of this digest, in as many words, that it "answers
    *did any first-party code change between these two runs*" — the question this command
    exists to ask. ADR-0014's own precondition table already named `imported_code.omitted`; the
    ADR contemplated the field and the implementation dropped it.

    EXACTLY ONE SIDE CARRYING A DIGEST BLOCKS rather than compares. `imported_code` entered the
    history WITHOUT a `HISTORY_SCHEMA` bump, so two records can legitimately share
    `runprov.history.v2` and disagree about whether the field exists — and `hash_imported_code`
    can be off on one machine and on the other. Comparing there would report a code change for
    every pair straddling 2026-08-13, which is a confident wrong answer rather than a refusal.

    `omitted` IS A NOTE AND NOT A BLOCK (Taylor's decision, 2026-09-17). Past
    `imported_code_max` the digest covers only the kept prefix, so equality means the first N
    files agree — a real qualification, which goes in `examined` where the scope of a
    comparison belongs. Blocking on it would stop any project with more than 200 first-party
    modules from ever exiting 0, which is the gate-that-cannot-pass this package has now built
    three times; `configure(imported_code_max=…)` is there for anyone who wants strictness.
    """
    left, right = a.get("git_commit"), b.get("git_commit")
    blocked = None
    for record, side in ((a, "A"), (b, "B")):
        if record.get("git_status_captured") is False:
            blocked = f"{side}'s dirty state is unknown — git status did not run"
        elif record.get("git_code_dirty"):
            blocked = f"{side} ran from a dirty tree, so its commit does not name the code"
    differences = [f"commit  {left or '?'} -> {right or '?'}"] if left != right else []

    mine, theirs = _imported_of(a), _imported_of(b)
    one, two = str(mine.get("digest") or ""), str(theirs.get("digest") or "")
    examined = f"{left or 'none'} / {right or 'none'}"
    if one and two:
        if one != two:
            differences.append(f"first-party code  {one[:16]} -> {two[:16]}")
        examined += f", code {one[:8]} / {two[:8]}"
        scope = [
            f"{side}'s code digest covers {kept.get('count')} of "
            f"{(kept.get('count') or 0) + (kept.get('omitted') or 0)} files"
            for kept, side in ((mine, "A"), (theirs, "B"))
            if kept.get("omitted")
        ]
        if scope:
            examined += " — " + "; ".join(scope)
    elif one or two:
        which = "A" if one else "B"
        blocked = blocked or (
            f"only {which} recorded a digest of its first-party code, so the code itself "
            "cannot be compared"
        )
        examined += f", code {which} only"
    return Dimension("code", differences, examined, blocked)


def _status(a: typing.Mapping[str, typing.Any], b: typing.Mapping[str, typing.Any]) -> Dimension:
    """Did each run SUCCEED. Audit D, D-02.

    `compare()` had seven dimensions and none of them read `status` or `failure`, though the
    history projection carries both deliberately — `run.py`'s own comment there says
    `grep '"status": "failed"' runs.jsonl` is the whole query. So a script that wrote its
    table and THEN raised — a post-processing step blowing up after the output is on disk —
    produced a record with identical inputs, outputs, parameters, packages and commit to its
    successful predecessor, and `runprov diff` reported every dimension `unchanged` and exited
    0. The traceback was sitting in the same history line the diff had just read.

    WORSE, AND THE REASON A BARE `!=` IS NOT THE FIX: when BOTH runs crash identically, the
    statuses match, so an inequality test settles and `runprov diff <script>` reports a clean
    green comparison forever. Hence the per-side evidence lines — a `failed` side is a finding
    whether or not the other side agrees with it.

    NOT A POISONED COMPARISON. Marking every dimension incomparable when a run failed was
    prototyped and rejected: `code`, `parameters` and `packages` are recorded at START and are
    fully known for a crashed run, and blanking them contradicts ADR-0014's own amendment that
    INCOMPLETE is not INCOMPARABLE — it would lose the half that is still true.

    A missing `status` BLOCKS rather than compares. `compare()` is pure and public and is fed
    hand-built mappings; and `running` can reach a mid-run sidecar, where it describes a run
    that has not finished rather than one that failed.
    """
    left, right = a.get("status"), b.get("status")
    if not left or not right:
        return Dimension(
            "status",
            [],
            f"{left or '<absent>'} vs {right or '<absent>'}",
            "one of the two records does not say whether the run finished",
        )
    if left == RUNNING or right == RUNNING:
        return Dimension(
            "status", [], f"{left} vs {right}", "a run that has not finished cannot be compared"
        )
    lines = []
    if left != right:
        lines.append(f"status  {left} -> {right}")
    # THE EVIDENCE, PER SIDE, so failed-vs-failed is a finding too. `failure` alone would not
    # do: a record can be `failed` with the failure block truncated or absent, and `status` is
    # the field the history projection guarantees.
    for record, side, state in ((a, "A", left), (b, "B", right)):
        if state != OK_STATUS:
            failure = record.get("failure") or {}
            first = str(failure.get("message", "")).splitlines() or [""]
            detail = (
                f"{failure.get('type', '?')}: {first[0][:60]}"
                if failure
                else "no failure block recorded"
            )
            lines.append(f"{side} {state}: {detail}")
    return Dimension("status", lines, f"{left} vs {right}", None)


def _parameters(
    a: typing.Mapping[str, typing.Any], b: typing.Mapping[str, typing.Any]
) -> Dimension:
    left, right = a.get("parameters") or {}, b.get("parameters") or {}
    lines = []
    for key in sorted(set(left) | set(right)):
        if left.get(key) != right.get(key):
            lines.append(f"{key}  {left.get(key, '<absent>')!r} -> {right.get(key, '<absent>')!r}")
    return Dimension("parameters", lines, f"{len(left)} vs {len(right)}", None)


def _packages_of(record: typing.Mapping[str, typing.Any]) -> dict[str, str]:
    """The tracked packages, from EITHER record shape. Audit B, A-04.

    The history is a projection, and it FLATTENS this field: `run.py:3513` writes a top-level
    `packages`, while the sidecar keeps `environment.packages`. `_diff` is fed history records
    — and this read only the sidecar shape, so `left` and `right` were always `{}`, the
    dimension always reported `unchanged (0 vs 0)`, and it always counted as settled for the
    exit code. Measured on a real 3 556-line history whose lines carry pyyaml, openpyxl and
    forty others: diff could not see a single one of them.

    A package version moving is the most common cause of "same script, different numbers",
    which is the question this command exists to answer.
    """
    flat = record.get("packages")
    if isinstance(flat, dict):
        return {str(k): str(v) for k, v in flat.items()}
    nested = (record.get("environment") or {}).get("packages")
    return {str(k): str(v) for k, v in nested.items()} if isinstance(nested, dict) else {}


def _packages(a: typing.Mapping[str, typing.Any], b: typing.Mapping[str, typing.Any]) -> Dimension:
    """`packages_recorded` decides this. `{}` on one side and 47 on the other is a change of
    CONFIGURATION, not of environment, and reporting it as the second is the defect."""
    how_a, how_b = _obs(a, "packages_recorded"), _obs(b, "packages_recorded")
    blocked = None
    if how_a != how_b:
        blocked = f"A recorded packages as {how_a!r}, B as {how_b!r}"
    left, right = _packages_of(a), _packages_of(b)
    lines = []
    for name in sorted(set(left) | set(right)):
        if left.get(name) != right.get(name):
            lines.append(f"{name}  {left.get(name, '<absent>')} -> {right.get(name, '<absent>')}")
    return Dimension("packages", lines, f"{len(left)} vs {len(right)}", blocked)


def _steps_of(
    record: typing.Mapping[str, typing.Any],
) -> dict[typing.Any, dict[str, typing.Any]] | None:
    """The step entries when the record carries them, or None when it carries only a count."""
    steps = record.get("steps")
    if isinstance(steps, list):
        return {s.get("step"): s for s in steps if isinstance(s, dict)}
    return None


def _step_count(record: typing.Mapping[str, typing.Any]) -> int:
    steps = record.get("steps")
    if isinstance(steps, int):
        return steps
    return len(steps) if isinstance(steps, list) else 0


def _steps(a: typing.Mapping[str, typing.Any], b: typing.Mapping[str, typing.Any]) -> Dimension:
    """ADR-0010's own example, and the reason `observation` exists.

    A run on 3.11 could not see what a run on 3.12 saw. Reporting that as five functions
    appearing describes a change of interpreter as a change of code.
    """
    blocked = None
    if _obs(a, "auto_available") != _obs(b, "auto_available"):
        blocked = (
            f"A auto_available={_obs(a, 'auto_available')}, "
            f"B auto_available={_obs(b, 'auto_available')} — different interpreters saw "
            "different amounts"
        )
    else:
        for record, side in ((a, "A"), (b, "B")):
            for mark in ("steps_truncated", "observed_truncated", "auto_stopped_after_calls"):
                if _obs(record, mark):
                    blocked = f"{side} is truncated ({mark}), so an absence is not an absence"
    # THE HISTORY CARRIES A COUNT, THE SIDECAR CARRIES THE LIST. Audit B, A-03: this iterated
    # whatever it was given, so `runprov diff` raised `TypeError: 'int' object is not iterable`
    # for any pair where either run declared a step — every project that adopted `@run.step`,
    # which is the feature the digests exist for. `run.py:3472` writes `len(r["steps"])` and
    # omits the key when zero; the comment there says THE COUNT, NOT THE LIST in capitals.
    #
    # A count is still a comparison, just a weaker one: "2 steps vs 3" is a real finding, and
    # "2 vs 2" is NOT grounds for `unchanged`, because two different steps count the same.
    # That is exactly the `blocked` state this module already has, so counts use it rather
    # than a new shape.
    left, right = _steps_of(a), _steps_of(b)
    if left is None or right is None:
        na, nb = _step_count(a), _step_count(b)
        # C-02 of Audit C. Blocking on EVERY count pair made `steps` incomparable for every
        # history-versus-history diff — which is every diff this command actually performs, since
        # `_diff` is fed history records — so `runprov diff` still could never exit 0. That
        # recreates A-07: fixed in the same commit, for the same command, three findings apart.
        #
        # A count is weak evidence, not an absence of evidence. Two runs that BOTH declared no
        # steps agree completely: the key is omitted when zero, so "nothing declared" is a
        # recorded fact rather than an unreadable one. Two EQUAL NON-ZERO counts are the only
        # case a count cannot settle — two different steps count the same — and only that case
        # is blocked.
        if na != nb:
            return Dimension("steps", [f"count  {na} -> {nb}"], f"{na} vs {nb} counted", blocked)
        if na == 0:
            return Dimension("steps", [], "0 vs 0 declared", blocked)
        return Dimension(
            "steps",
            [],
            f"{na} vs {nb} counted",
            blocked or "the history records a COUNT of steps, not their digests",
        )
    lines = []
    for name in sorted(set(left) | set(right), key=str):
        one, two = left.get(name), right.get(name)
        if one is None or two is None:
            lines.append(f"{name}  {'ADDED' if one is None else 'REMOVED'}")
        elif (one.get("args"), one.get("kwargs"), one.get("returned")) != (
            two.get("args"),
            two.get("kwargs"),
            two.get("returned"),
        ):
            lines.append(f"{name}  argument or result digest changed")
        elif _undigested(
            one.get("args"), one.get("kwargs"), one.get("returned")
        ):  # A-06: equal markers, not equal values
            blocked = blocked or (
                f"{name} recorded a value that could not be digested, so equality here "
                "means two markers matched, not two values"
            )
    return Dimension("steps", lines, f"{len(left)} vs {len(right)} declared", blocked)


#: How much a resource figure has to move before it is a FINDING rather than noise. Audit B,
#: A-07: `wall_seconds` and `cpu_seconds` are continuous measurements recorded to microseconds,
#: so exact inequality made `resources` report a change for every pair of runs ever compared —
#: the dimension was never `unchanged`, `settled` was never true, and `runprov diff` could
#: therefore never exit 0. A gate that cannot pass is a gate nobody keeps.
#:
#: A ratio rather than an absolute, because these span microseconds to hours; 5% is chosen to
#: be well below anything a reader would call a difference and well above clock jitter. It is
#: a DISPLAY threshold only — the record keeps every digit.
#: The two terminal states a run can reach, and the one it wears while still going. Read from
#: `run.py` rather than re-spelled: a diff that invents its own vocabulary for the record's
#: states is a diff that disagrees with `log` and `show` about the same line.
OK_STATUS = "ok"
RUNNING = "running"

RESOURCE_NOISE = 0.05

#: AND AN ABSOLUTE FLOOR PER FIGURE, because a relative band alone cannot work for time.
#: D-01 of Audit D, measured over twelve IDENTICAL runs on one machine, same input:
#:
#:     wall_seconds   spread 111.6 %   but only 0.035 s
#:     cpu_seconds    spread  21.5 %   but only 0.052 s
#:     max_rss_bytes  spread   1.0 %          0.24 MiB
#:
#: **Time noise is ABSOLUTE; memory noise is RELATIVE.** That is why the 5 % band worked for
#: memory and could never work for wall time — on a short run, a few milliseconds of scheduler
#: jitter is a doubling. A-07 saw the symptom (a gate that could never pass) and C-09 tried a
#: wider relative band; neither fixed the model, and the third attempt was to stop letting cost
#: decide the exit code at all, which made five documents false at once.
#:
#: A difference is material only if it clears the band AND the floor. These floors are ~14x
#: and ~33x the noise measured above, which is loose enough that ordinary jitter never fires
#: and tight enough that anything a person would call a regression does.
RESOURCE_FLOOR = {
    "wall_seconds": 0.5,
    "cpu_seconds": 0.5,
    "max_rss_bytes": 8 * 1024 * 1024,
}


def _materially(key: str, one: typing.Any, two: typing.Any) -> bool:  # noqa: ANN401
    """True when two resource figures differ by more than measurement noise."""
    if one == two:
        return False
    try:
        a, b = float(one), float(two)
    except (TypeError, ValueError):
        return True  # not numbers: any difference is a real one
    # C-09 of Audit C. The first version exempted `max_rss_bytes` on the grounds that a
    # high-water mark is "a count, not a continuous reading". It is not a count of anything
    # stable: `ru_maxrss` moves by tens to hundreds of kibibytes between two identical
    # executions — allocator behaviour, ASLR, whatever the interpreter imported first — so the
    # one figure the exemption protected was the one that jitters most.
    #
    # BOTH TESTS, D-01 of Audit D. A relative band alone cannot absorb time jitter (see
    # `RESOURCE_FLOOR`), and an absolute floor alone would let a 10 % regression on an
    # eight-hour job through. Material means the difference is large next to the measurement
    # AND large in its own units.
    gap = abs(a - b)
    largest = max(abs(a), abs(b))
    relative = largest == 0 or gap / largest > RESOURCE_NOISE
    return relative and gap > RESOURCE_FLOOR.get(key, 0.0)


def _resources(a: typing.Mapping[str, typing.Any], b: typing.Mapping[str, typing.Any]) -> Dimension:
    """ADR-0013: a cgroup peak and a `getrusage` peak are different quantities.

    "312 MiB -> 8 GiB" across a source change is a laptop moving to Slurm and starting to
    measure what the scheduler enforces — not a run that needed twenty-five times more.
    """
    left, right = a.get("resources") or {}, b.get("resources") or {}
    blocked = None
    if bool(left) != bool(right):
        # EXACTLY ONE SIDE, not "either side". D-01 of Audit D: this blocked whenever the
        # block was missing, and 0.1.0 through 0.3.0 wrote no `resources` block at all — so
        # every pair of records from those versions was permanently incomparable and their
        # histories could never exit 0. The gate that cannot pass, which is A-07's own defect,
        # arriving a third time. The message was also literally false there: "one of the two
        # runs predates" when both did.
        #
        # Two runs that BOTH predate the block agree completely about cost in the only sense
        # available — neither measured it — which is C-02's reasoning about two runs that both
        # declared no steps, applied here.
        which = "A" if not left else "B"
        blocked = f"{which} predates the resources block, so there is nothing to compare it to"
    elif left.get("source") != right.get("source"):
        blocked = (
            f"A measured by {left.get('source')!r}, B by {right.get('source')!r} — "
            "different quantities"
        )
    lines = []
    for key in ("max_rss_bytes", "cpu_seconds", "wall_seconds"):
        one, two = left.get(key), right.get(key)
        if one is None or two is None or not _materially(key, one, two):
            continue
        lines.append(f"{key}  {one} -> {two}")
    return Dimension(
        "resources",
        lines,
        "neither run measured cost"
        if not left and not right
        else f"{left.get('source', '—')} / {right.get('source', '—')}",
        blocked,
    )


def compare(
    a: typing.Mapping[str, typing.Any], b: typing.Mapping[str, typing.Any]
) -> list[Dimension]:
    """Every dimension, each with its own precondition. Pure, so the table is testable.

    THE READING ORDER IS `DIMENSIONS`' AND NOT THIS FUNCTION'S. It used to be both: the
    constant said it held the order and the list below actually held it, which is how the
    constant came to be missing `status` with nothing red. Sorting through it means the
    sentence and the behaviour are one thing, and a dimension built here that the constant
    does not name raises out of `.index` rather than being quietly dropped — the direction a
    filter would fail in, and the one that loses a finding.
    """
    dims = sorted(
        (
            _files(a, b, "inputs"),
            _files(a, b, "outputs"),
            _code(a, b),
            _parameters(a, b),
            _packages(a, b),
            _steps(a, b),
            _resources(a, b),
            _status(a, b),
        ),
        key=lambda d: DIMENSIONS.index(d.name),
    )
    # A SCHEMA CHANGE POISONS EVERY DIGEST-BEARING DIMENSION AT ONCE. `content_sha256` changed
    # meaning at v1 -> v2, so the same field name holds two different quantities and a
    # per-dimension precondition would have to repeat itself seven times to say so.
    if a.get("schema") != b.get("schema"):
        why = f"schema differs: {a.get('schema')!r} vs {b.get('schema')!r}"
        dims = [
            d._replace(blocked=d.blocked or why) if d.name in ("inputs", "outputs", "steps") else d
            for d in dims
        ]
    return dims


def _side(record: typing.Mapping[str, typing.Any]) -> Side:
    """The four facts the header states, read once. The record's own names, R-9."""
    return Side(
        script=record.get("script"),
        run_id=record.get("run_id"),
        started_utc=record.get("started_utc"),
        status=record.get("status"),
    )


def build(a: typing.Mapping[str, typing.Any], b: typing.Mapping[str, typing.Any]) -> Comparison:
    """The comparison, as facts. The table is a rendering of THIS, and so is the payload.

    ONE BUILDER, TWO RENDERERS (ADR-0017 R-1), and the split is here rather than in the CLI
    because the alternative is what this project has now shipped twice about one object: a
    second emitter written beside the first, drifting invisibly because nobody reads both.
    `compare()` is kept as it is — pure, public and the thing ADR-0017's own table names —
    and this is the wrapper the header needed.
    """
    return Comparison(_side(a), _side(b), compare(a, b))


def _or(value: typing.Any) -> typing.Any:  # noqa: ANN401 - anything printable
    """`?` for a fact the record does not carry. THE FILL IS A RENDERING CHOICE.

    `report._or`'s argument, one command along: a `?` that reaches the structure is an absence
    a consumer cannot tell from a finding, and that is the direction this package treats as
    unrecoverable. So the structure carries `None` and this supplies the word for it.
    """
    return "?" if value is None else value


def _plain(value: typing.Any) -> typing.Any:  # noqa: ANN401 - the structure, whatever it holds
    """A `Comparison` as JSON-able dicts and lists, DERIVED FROM `_fields` RATHER THAN RE-TYPED.

    A serialiser that names each field is a second spelling of the structure, and two hand-kept
    spellings of one thing drift in the direction this repository has now found nine times: a
    field added to the structure is simply missing from the payload, silently, on the rendering
    nobody reads. Walking `_asdict()` cannot miss one.

    `verdict` AND `settled` ARE ADDED RATHER THAN WALKED, because they are computed properties
    and not fields — and R-10 allows them because they compute nothing the record does not
    already hold. They are not a convenience either. `settled` is what the exit code is built
    from, so a consumer that re-derives a verdict from `differences` alone gets a different
    answer from the command it is reading: an incomparable dimension has no differences and is
    still non-zero, which is the entire subject of ADR-0014.
    """
    if isinstance(value, Dimension):
        return {
            **{name: _plain(item) for name, item in value._asdict().items()},
            "verdict": value.verdict,
            "settled": value.settled,
        }
    if isinstance(value, tuple) and hasattr(value, "_asdict"):
        return {name: _plain(item) for name, item in value._asdict().items()}
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    return value


def payload(comparison: Comparison) -> dict[str, typing.Any]:
    """The comparison as one object, for a reader that is not a person. R-1, R-5, R-7, R-9.

    EVERYTHING THE TABLE STATES, INCLUDING WHAT IT COULD NOT COMPARE. `blocked` is why a
    dimension cannot support the word *unchanged* and `examined` is the scope over which it was
    looked at; a payload carrying only `differences` would let a consumer read NOT COMPARABLE
    as *nothing changed*, which is the exact defect ADR-0014 exists to prevent, delivered to
    the readers least able to notice it. Every one of this module's preconditions was added
    because somebody could otherwise not tell *not measured* from *measured as zero*.

    NOTHING IS ABSENT HERE, and that is a fact about `diff` rather than a shortcut. `compare()`
    answers every dimension for every pair of records, so `blocked: null` always means *looked,
    and nothing stops this one supporting "unchanged"*. R-8's other reading — a key absent
    because this version did not look — has nowhere to arise, because an incomparability is a
    value in this command and never a silence.

    THE TABLE IS THE ONE THAT SAYS LESS, in the safe direction: `examined` is printed only on
    an `unchanged` row, so a changed or incomparable dimension carries its scope here and not
    on the page. That asymmetry is asserted in its own test rather than left to be discovered.
    """
    return {
        "schema": SCHEMA,
        **{name: _plain(value) for name, value in comparison._asdict().items()},
        "settled": comparison.settled,
    }


def render(comparison: Comparison) -> list[str]:
    """The three states, spelled out. `unchanged` always says what it examined.

    IT TAKES THE COMPARISON AND NOTHING ELSE, which is the half of R-2 that reading output
    cannot check: a renderer handed nothing but the structure cannot state a fact the
    structure does not hold.
    """

    # `[failed]` ON THE HEADER LINES, not only in the dimension. D-02: when both runs failed
    # the statuses agree, and a reader scanning the table sees seven `unchanged` rows with no
    # hint that neither run finished. `show` already puts the state in exactly this position.
    def _who(run: Side, side: str) -> str:
        mark = "" if run.status == OK_STATUS else f"  [{run.status or 'status not recorded'}]"
        return f"{side}  {_or(run.script)}  {_or(run.run_id)}  {_or(run.started_utc)}{mark}"

    out = [_who(comparison.a, "A"), _who(comparison.b, "B"), ""]
    for d in comparison.dimensions:
        if d.verdict == UNCHANGED:
            # WHAT WAS EXAMINED, because "no difference found" and "nothing examined" print the
            # same word otherwise — this project's own recurring failure.
            out.append(f"{d.name:<12}unchanged  ({d.examined})")
            continue
        if d.verdict == INCOMPARABLE:
            out.append(f"{d.name:<12}NOT COMPARABLE — {d.blocked}")
            continue
        out.append(f"{d.name:<12}{len(d.differences)} change(s)")
        out += [f"              {line}" for line in d.differences]
        if d.blocked:
            out.append(f"              (and not fully comparable — {d.blocked})")
    return out
