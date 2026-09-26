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

#: R-5. The payload's own version, named HERE rather than in `__main__`, because it names THIS
#: module's report and `--format json` is only one rendering of it. `chain.SCHEMA` states the
#: same argument, and G-24(a) states the cost of not stating it: a literal in the emitter and a
#: second literal in its test is one sentence asserted against its own copy, so the payload was
#: free to be versioned wrongly with the suite green.
SCHEMA = "runprov.report.v1"


def _rule(title: str) -> str:
    """A titled rule. There is no untitled form: the first version had one, nothing called
    it, and a branch nothing reaches is a shape nobody tests."""
    return f"── {title} " + "─" * max(0, WIDTH - len(title) - 4)


def _kv(key: str, value: typing.Any, pad: int = 13) -> str:  # noqa: ANN401 - anything printable
    return f"  {key.ljust(pad)} {value}"


class Match(typing.NamedTuple):
    """Which run produced an artifact, and what the answer could not settle.

    `recorded` is the digest the named run itself recorded for that path; `elsewhere` is the
    single other run whose recorded output matches the bytes actually on disk, when there is
    exactly one. Both are None/empty in the ordinary case, and `page()` prints nothing extra.

    RETURNED RATHER THAN STASHED. The prototype for D-03 passed these through a module-level
    global; two readers of one artifact would then have raced, and a second caller of
    `find_run` would have seen the first one's answer.
    """

    record: dict[str, typing.Any] | None
    recorded: str = ""
    elsewhere: dict[str, typing.Any] | None = None
    elsewhere_path: str | None = None
    #: The output ENTRY this record holds for this path, kept whole. `recorded` is that entry
    #: through `pin_digest`, which is the right thing to PRINT and the wrong thing to compare —
    #: it applies a precedence the record may not have used. A comparison has to know which key
    #: the record actually carries, so the entry travels rather than a digest chosen for it.
    entry: dict[str, typing.Any] | None = None

    @property
    def also_elsewhere(self) -> bool:
        """Exactly one OTHER run recorded the bytes now on disk, at a different path.

        NAMED FOR WHAT IT TESTS. It was `disagrees`, whose docstring said "the named run
        recorded a digest for this path, and it is not what is there now" — a sentence about a
        comparison this expression never performs. The name asserted the fix; the code asserted
        the other fact. I-02.
        """
        return self.elsewhere is not None


def find_run(
    history: typing.Iterable[dict[str, typing.Any]],
    artifact: str,
    digest: str | None = None,
) -> dict[str, typing.Any] | None:
    """The LAST run that wrote `artifact`, or None. `find_match` for the rest of the answer.

    KEPT AS A THIN WRAPPER because callers and tests reach this name and the record is what
    almost all of them want; `find_match` carries what D-03 needs on top.
    """
    return find_match(history, artifact, digest).record


def find_match(
    history: typing.Iterable[dict[str, typing.Any]],
    artifact: str,
    digest: str | None = None,
) -> Match:
    """The LAST run that wrote `artifact`, and what the match could not settle.

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
    recorded_for_path = ""
    entry_for_path: dict[str, typing.Any] | None = None
    # KEYED BY RUN, NOT BY PATH. D-06 of Audit D: keying on the path got the uniqueness rule
    # wrong in BOTH directions. Two runs that wrote byte-identical bytes to the SAME recorded
    # path — a deterministic pipeline re-run, which is every re-run of a correct pipeline —
    # collapsed to one key, so `len(...) == 1` passed and the LAST-appended was named, with its
    # command and its commit, for an artifact the reader had found somewhere else. And one run
    # that wrote identical bytes to TWO paths counted as two, so a moved artifact that a single
    # run plainly produced was reported NOT FOUND.
    #
    # `run_uid` is the run's own identity and is what the question is actually about. `id()` is
    # the fallback for a hand-built mapping in a test, which is a real caller here.
    by_digest: dict[typing.Any, tuple[str, dict[str, typing.Any]]] = {}
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
                # WHAT THIS RECORD SAYS THESE BYTES WERE, kept so the caller can compare it
                # with what they are. Taken with `pin_digest`'s own precedence rather than a
                # fourth re-derivation of it — a checker that recomputes that precedence
                # independently is a checker that can disagree with the pin it is checking.
                recorded_for_path = (hashing.pin_digest(out) if isinstance(out, dict) else "") or ""
                # AND THE ENTRY ITSELF, for I-02: `pin_digest` applies a precedence, and a
                # comparison has to know which key the record actually carries.
                entry_for_path = out if isinstance(out, dict) else None
            elif (
                isinstance(out, dict)
                and digest
                and any(
                    out.get(key) == digest for key in ("sha256", "content_sha256", "sha256_tree")
                )
            ):
                run_key = record.get("run_uid") or record.get("run_id") or id(record)
                by_digest[run_key] = (_resolve(str(name), base) if name else "", record)
    if by_path is not None:
        # D-03 of Audit D. THE PATH WINS, AND THE DISAGREEMENT IS REPORTED RATHER THAN RESOLVED.
        # The branch assigned unconditionally, so when the path-matching record's OWN recorded
        # digest for that path contradicted the bytes on disk, `find_run` held proof the record
        # did not describe them and returned it anyway — discarding an unambiguous digest hit.
        # The page then paired one run's script, command and commit with another run's inputs
        # (those come from the artifact's own pin), reported OK, and exited 0: the internally
        # contradictory page A-05 was filed about, by a third road.
        #
        # PREFERRING THE DIGEST THERE WAS TRIED AND IS WORSE. A skeptic broke it in one attempt:
        # run A writes `results/out.bin`, run B records an EMPTY `archive/placeholder.bin`, an
        # interrupted rewrite truncates A's output to nothing — and the remedy names B, which
        # never touched that path, on an empty-file collision. Degenerate collisions (empty
        # results, header-only CSVs, `.done` sentinels) are the class C-01 was filed over.
        #
        # RETURNING NOTHING IS WORSE STILL: the disagreement condition also holds when the
        # artifact was simply EDITED IN PLACE, where this record IS the producer and the page
        # correctly reports ALTERED. Blanking the run there would strip the producer from every
        # altered page — the one place "who wrote this, and when" matters most.
        #
        # So the strongest identity stays the headline and the reader is handed the rest: this
        # is the package's own rule, report what you cannot tell rather than guess.
        other = [(p, r) for p, r in by_digest.values() if r is not by_path]
        one = other[0] if len(other) == 1 else (None, None)
        return Match(
            by_path,
            recorded=recorded_for_path,
            elsewhere=one[1],
            elsewhere_path=one[0],
            entry=entry_for_path,
        )
    if len(by_digest) == 1:
        return Match(next(iter(by_digest.values()))[1])
    # Nothing matched, or the bytes sit at two paths and identify no single run.
    return Match(None)


def _resolve(name: str, base: str | None = None) -> str:
    """A path in one comparable form, anchored to `base` when it is relative."""
    try:
        path = pathlib.Path(name)
        if base and not path.is_absolute():
            path = pathlib.Path(base) / path
        return str(path.resolve())
    except (OSError, ValueError):  # a name that is not a usable path compares as itself
        return name


class Run(typing.NamedTuple):
    """The run that produced the artifact, IN THE RECORD'S OWN NAMES.

    `started_utc` and `finished_utc`, never `started` and `finished`: those two are the
    page's COLUMN LABELS, and a label is a rendering choice. Keeping the record's spelling
    here is what lets a question asked of the history be asked of this structure and of
    anything derived from it, instead of a reader having to learn a second vocabulary for
    the same seven facts.
    """

    script: str | None
    status: str | None
    started_utc: str | None
    finished_utc: str | None
    run_id: str | None
    command: str | None
    cwd: str | None


class BytesDiffer(typing.NamedTuple):
    """The named run's own digest for this path is not what is on disk. ONE FACT, ONLY.

    I-02 of Audit I. This used to carry a second, independent fact as well — that the bytes now
    on disk were recorded as an output by exactly one other run — and the two were reported
    together or not at all. Fusing them meant the finding fired when the SECOND was true and the
    first was never tested: `Match.disagrees` was `recorded and elsewhere is not None`, which
    never compares `recorded` with `now`. Measured on a publish-by-copy history — a `cp` in a
    Makefile, the case `find_match`'s own docstring cites — the page accused an untouched file
    of having changed. The other half is `WrittenElsewhere` below.

    `how` NAMES THE KEY THE COMPARISON WAS MADE ON, because the two sides must be the same
    quantity or it is not a comparison. `hashing.describe` writes BOTH `sha256` and
    `content_sha256` for every regular file and they differ for any text ending in a newline —
    every `.tsv` this package writes. `recorded` used to come from `pin_digest`, which prefers
    `content_sha256`, while `now` was `hashing.sha256(file)`: two algorithms, so the two numbers
    printed side by side were digests of the SAME unchanged bytes. Measured across the
    cross-version corpus, all six released wheels: 18 of 18 outputs carry the two digests
    differing. The comparison now reads the record's own key and hashes the file that way.
    """

    recorded: str
    now: str
    how: str


class WrittenElsewhere(typing.NamedTuple):
    """The bytes now on disk were recorded as an output by exactly one OTHER run.

    D-03's other half, and independent of `BytesDiffer`: it is true of a file that was published
    by copying — where nothing has changed and the digests agree — and it is false of a file
    edited in place, where they disagree and no other run holds the new bytes. Reporting either
    one only when the other also held is what I-02 was filed about.
    """

    path: str
    script: str | None


class Tool(typing.NamedTuple):
    """The runprov that wrote the record, and whether it can be followed back to code. U-01."""

    version: str | None
    source: str | None
    identifies_code: bool | None
    commit: str | None


class Environment(typing.NamedTuple):
    """The interpreter the run used. See `Method.environment` for when this is there at all."""

    python: str | None
    platform: str | None


class Method(typing.NamedTuple):
    """Which version of the method ran, and whether it can be got back.

    `tool` is None for a record written before 0.3.0, and `environment` is None for EVERY
    history — that field is a whitelist projection and stays in the sidecar beside the
    artifact, which is the right trade for a file appended to forever. Both nulls are doing
    real work: "this run recorded no tool block" and "the history does not carry this" are
    findings, and the page says both of them in words rather than going quiet.
    """

    git_commit: str | None
    git_status_captured: bool | None
    git_code_dirty: bool | None
    tool: Tool | None
    environment: Environment | None


class Input(typing.NamedTuple):
    """One pinned input, in `verify.check_input`'s own names.

    `name` and `pinned` are read rather than guessed for the reason the rendering already
    records: the first version of that line invented `path` and `want` and printed `OK ? ?`
    for a perfectly good input — a page that looks filled in and says nothing.
    """

    name: str | None
    status: str | None
    pinned: str | None
    found: str | None


class Observation(typing.NamedTuple):
    """What the run was ABLE to observe. ADR-0010, and the page's own blind-spot section.

    None is a record with no `observation` block at all — a run from before that block
    existed, which is a different fact from a run that observed nothing, and the page
    already refuses to conflate them.

    `unregistered_watch_truncated` is C-06: when the watch hit its cap the unregistered list
    is a SAMPLE rather than the answer, and a reader who cannot see this count reads an
    empty list as "nothing was missed" on the one page consulted to judge one artifact.
    """

    steps: typing.Any
    packages_recorded: typing.Any
    unregistered_watch_truncated: int | None


class Body(typing.NamedTuple):
    """Everything the page states once it has found the run that produced the artifact.

    ONE OPTIONAL RATHER THAN SIX, and that is null-versus-absent made structural. When no run
    is found this page stops after the verdict: it does not look for a method, an input list
    or an observation block and then fail to find them — it never looks. Six separate Nones
    would spell "looked and found none" six times over, about questions the page did not ask;
    one absent `Body` spells "this page does not reach here", and `Limits.run_not_found` is
    the fact that says why.
    """

    run: Run
    bytes_differ: BytesDiffer | None
    written_elsewhere: WrittenElsewhere | None
    method: Method
    inputs: tuple[Input, ...]
    observation: Observation | None
    unregistered_reads: tuple[str, ...]


class Limits(typing.NamedTuple):
    """What this page cannot tell you — the FACT, never the prose.

    `_limits()` is mostly standing text. "It records; it does not audit" is true of every
    report this package has ever printed, and a caveat true of everything carries no
    information about anything; it belongs in the page and in the documentation, not in a
    structure. Its one CONDITIONAL clause is the exception, and it is a statement about this
    report: no run record was found, so everything except the pin and the verdict is absent
    rather than clean. A reader who could not see that reads a page missing four sections as
    a page with nothing to report.
    """

    run_not_found: bool


class Report(typing.NamedTuple):
    """One artifact's page, as facts. The text is a rendering of THIS, and so is the JSON.

    THE ARGUMENT IS `Page`'S OWN, GENERALISED. The status was returned rather than matched
    back out of the rendered text because the first version of the CLI handler decided its
    exit code by string-matching its own output, which made the wording load-bearing —
    rephrasing a line would silently change what the command returned to a build. Everything
    else on this page was still only prose, so every other fact on it was one rewording away
    from the same defect, and there was nothing for a second rendering to be derived FROM.
    """

    artifact: pathlib.Path
    verdict: str
    reason: str | None
    body: Body | None

    @property
    def limits(self) -> Limits:
        """DERIVED, so the page's caveat about itself cannot disagree with the page."""
        return Limits(run_not_found=self.body is None)


class Page(typing.NamedTuple):
    """The rendered page, and the report it was rendered from.

    The status is RETURNED rather than read back out of the rendered text. The first version
    of the CLI handler decided its exit code by string-matching its own output, which makes
    the wording load-bearing: rephrasing a line would silently change what the command
    returns to a build. It reads off `Report` now rather than being carried beside the lines,
    because two copies of one verdict are two things that can disagree.
    """

    lines: list[str]
    report: Report

    @property
    def status(self) -> str:
        return self.report.verdict

    @property
    def ok(self) -> bool:
        return self.status == verify_mod.OK


def _or(value: typing.Any, fill: typing.Any) -> typing.Any:  # noqa: ANN401 - anything printable
    """`fill` for a fact the record does not carry. THE FILL IS A RENDERING CHOICE.

    `page()` used to spell these as `run.get("script", "?")`, in the builder, and that was
    harmless while a page was the only thing built: a reader sees `?` and reads "not
    recorded". It stops being harmless the moment the same facts are serialised, because
    then the FIELD'S VALUE is the string `?` and no consumer can tell it from a script
    genuinely called `?` — an absence rendered as a finding, which is the one direction this
    package treats as unrecoverable. So the structure carries the absence, and this supplies
    the word for it, in the same division of labour `_kv` already makes between a fact and
    the width of the column it is printed in.
    """
    return fill if value is None else value


def _q(value: typing.Any) -> typing.Any:  # noqa: ANN401 - anything printable
    """The page's own fill. See `_or`."""
    return _or(value, "?")


def build(
    artifact: pathlib.Path,
    root: pathlib.Path,
    history: typing.Iterable[dict[str, typing.Any]] = (),
) -> Report:
    """The facts, with nothing worded yet. `render_page` turns them into the page.

    THE ONE BUILDER. Every rendering of this command is a function of what this returns, so
    a fact can be reworded, re-ordered or dropped from one rendering without any other
    rendering quietly disagreeing about the artifact — which is the defect this project has
    now shipped twice, once as a cause the text asserted and the JSON did not carry, and
    once as two findings the text dropped while the JSON reported them.
    """
    result = verify_mod.verify_artifact(artifact, root)
    # THE DIGEST OF THE BYTES ON DISK NOW, so a moved or renamed artifact still finds its run,
    # and two files sharing a basename cannot be confused for one another. A-05.
    try:
        digest: str | None = hashing.sha256(artifact)
    except OSError:
        digest = None
    match = find_match(history, str(artifact), digest)
    run = match.record

    body = None
    if run is not None:
        body = Body(
            run=Run(
                script=run.get("script"),
                status=run.get("status"),
                started_utc=run.get("started_utc"),
                finished_utc=run.get("finished_utc"),
                run_id=run.get("run_id"),
                command=run.get("command"),
                cwd=run.get("cwd"),
            ),
            bytes_differ=_differs(match.entry, artifact),
            written_elsewhere=_elsewhere(match),
            method=_method(run),
            # THE ARTIFACT'S OWN PIN, not the run's outputs — `verify_artifact` read them and
            # already re-derived every digest. Nothing here is computed a second time.
            inputs=tuple(_input(entry) for entry in result.get("inputs") or []),
            observation=_observation(run),
            unregistered_reads=tuple(run.get("unregistered_reads") or []),
        )
    return Report(
        artifact=artifact,
        verdict=result.get("status", "?"),
        reason=result.get("reason"),
        body=body,
    )


#: The record keys a comparison can be made on, each with the function that computes the same
#: quantity from the file. ORDERED AS `pin_digest` ORDERS THEM, so the digest a reader sees
#: quoted elsewhere is the one compared here — but the choice is made on what the record
#: CARRIES, never on a precedence applied to it.
#:
#: `sha256_tree` is deliberately absent. `build` hashes the artifact with `hashing.sha256`,
#: which raises on a directory, so `digest` is None and this block is unreachable for one. An
#: arm that cannot fire reads as a case that can happen.
_COMPARABLE_AS = (("content_sha256", hashing.content_digest), ("sha256", hashing.sha256))


def _differs(entry: dict[str, typing.Any] | None, artifact: pathlib.Path) -> BytesDiffer | None:
    """Does the record's own digest for this path disagree with the file? I-02.

    LIKE FOR LIKE, OR IT IS NOT A COMPARISON. The record decides how the file is hashed: if it
    carries `content_sha256` the file is content-digested, if it carries `sha256` the file is
    hashed raw. The previous version took `recorded` from `pin_digest` — which prefers
    `content_sha256` — and `now` from `hashing.sha256`, so the two sides were different
    quantities and could not be equal for any ordinary text file.

    NOT `show._digest_now`, WHICH LOOKS LIKE THE FUNCTION FOR THIS AND IS NOT. That applies
    TODAY's precedence to the file while the record carries whatever it carries, so over a
    v1-shaped record (raw `sha256` only) it compares a content digest against a raw one and
    flags an untouched file. Reusing it would have moved this defect rather than fixed it.

    AND NOTHING IS RE-PINNED. This reads the record and hashes the file; `pin_digest`'s
    precedence is untouched. Changing THAT would move a digest already recorded, which is one
    of the two irreversible classes this project names.

    `None` means *looked, and they agree* — or that the record's own key could not be asked of
    this file, which `content_digest` reports by returning None for anything it will not
    canonicalise. Neither is a finding.
    """
    for key, how in _COMPARABLE_AS:
        was = (entry or {}).get(key)
        if not was:
            continue
        now = how(artifact)
        if now is None:
            # THE RECORD'S OWN KEY CANNOT BE ASKED OF THIS FILE. `content_digest` returns None
            # for anything that is not a regular file — a directory, or a path that has since
            # been removed. Falling through to the next key would compare two different
            # quantities, which is the defect this function exists to remove; and inventing a
            # disagreement from a digest that could not be computed would accuse a file nobody
            # could hash. Not looked, so not a finding.
            return None
        # COMPARED WHOLE, DISPLAYED SHORT. Truncating before the comparison would let two
        # different digests agree on their first sixteen characters and pass.
        if str(was) != str(now):
            width = hashing.PIN_DIGEST_CHARS
            return BytesDiffer(str(was)[:width], str(now)[:width], key)
        return None
    return None


def _elsewhere(match: Match) -> WrittenElsewhere | None:
    """The other run holding these bytes, or None. D-03's second half, on its own."""
    if not match.also_elsewhere:
        return None
    return WrittenElsewhere(
        # `_posix`, NEVER `str()`. A recorded path spelled the platform's way is read on the
        # other one — the guard `test_no_recorded_path_is_spelled_with_a_bare_str` caught this
        # the moment it was written, which is the guard doing exactly its job.
        path=hashing._posix(match.elsewhere_path) if match.elsewhere_path else "",
        script=(match.elsewhere or {}).get("script"),
    )


def _method(run: dict[str, typing.Any]) -> Method:
    """Which version of the method ran. The THREE git fields, not the sentence they make."""
    return Method(
        git_commit=run.get("git_commit"),
        git_status_captured=run.get("git_status_captured"),
        git_code_dirty=run.get("git_code_dirty"),
        tool=_tool(run),
        environment=_environment(run),
    )


def _tool(run: dict[str, typing.Any]) -> Tool | None:
    """The runprov that wrote this record, or None for a record from before 0.3.0.

    None rather than a `Tool` of nulls, because the page says two different sentences here
    and they must not read alike: a tool block with nothing in it is a runprov that recorded
    itself badly, and no tool block at all is a run from before it recorded itself. U-01.
    """
    tool = run.get("tool")
    if not tool:
        return None
    return Tool(
        version=tool.get("version"),
        source=tool.get("source"),
        identifies_code=tool.get("identifies_code"),
        commit=tool.get("commit"),
    )


def _environment(run: dict[str, typing.Any]) -> Environment | None:
    """The interpreter, or None — which is EVERY history.

    The history does not carry `environment`: it is a whitelist projection and that field
    stays in the sidecar, which is the right trade for a file appended forever. Saying so is
    not padding — a page that silently omitted the interpreter would read as though the run
    had not recorded one, and "not on this page" is a different fact from "not recorded".
    """
    env = run.get("environment") or {}
    if not env:
        return None
    return Environment(python=env.get("python"), platform=env.get("platform"))


def _input(entry: dict[str, typing.Any]) -> Input:
    """One checked input, in `check_input`'s names."""
    return Input(
        name=entry.get("name"),
        status=entry.get("status"),
        pinned=entry.get("pinned"),
        found=entry.get("found"),
    )


def _observation(run: dict[str, typing.Any]) -> Observation | None:
    """What the run was able to observe, or None for a record carrying no such block."""
    obs = run.get("observation") or {}
    if not obs:
        return None
    return Observation(
        steps=obs.get("steps"),
        packages_recorded=obs.get("packages_recorded"),
        unregistered_watch_truncated=obs.get("unregistered_watch_truncated"),
    )


def render_page(report: Report) -> list[str]:
    """The page, from the report AND NOTHING ELSE.

    The signature is the guarantee, and it is the half of the consistency rule that reading
    output cannot check: a renderer handed only the structure cannot state a fact the
    structure does not hold. Both defects this split exists to prevent are what happens when
    a rendering is free to reach past the structure — one asserted a cause its sibling
    rendering did not carry, the other silently dropped two findings its sibling reported.
    """
    out = [_rule(f"provenance report — {report.artifact.name}"), ""]
    out.append(_kv("artifact", report.artifact))
    out.append(_kv("verdict", report.verdict))
    if report.reason:
        out.append(_kv("", f"({report.reason})"))

    body = report.body
    if body is None:
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
        out += ["", *_limits(report.limits)]
        return out

    run = body.run
    out += ["", _rule("the run that produced it"), ""]
    out.append(_kv("script", _q(run.script)))
    out.append(_kv("status", _q(run.status)))
    out.append(_kv("started", _q(run.started_utc)))
    out.append(_kv("finished", _q(run.finished_utc)))
    out.append(_kv("run_id", _q(run.run_id)))
    out.append(_kv("command", _q(run.command)))
    out.append(_kv("cwd", _q(run.cwd)))
    differ = body.bytes_differ
    if differ is not None:
        # D-03 of Audit D. THE LOUDEST LINE ON THE PAGE WHEN IT IS PRESENT, because everything
        # above it describes a run and everything below describes the artifact, and this says
        # the two may not belong together. Both digests are recorded facts and the comparison
        # is the one `show --stale` already performs; nothing here is inferred.
        #
        # THE EXIT CODE DOES NOT MOVE (Taylor's decision, 2026-09-17). `report` exits on
        # `verify`'s verdict, and `verify` is right: the artifact's pin is internally
        # consistent and the inputs it names still hash correctly. What is wrong is which
        # HISTORY RECORD attached to it, which is a property of the history and not of the
        # file. Making `report` fail where `verify` passes would leave two commands disagreeing
        # about one artifact. The precedent is the UNREGISTERED block below, which is the
        # loudest thing on this page and never moves the exit code either.
        out.append(
            _kv(
                "BYTES DIFFER", f"this run recorded {differ.recorded} for this path ({differ.how});"
            )
        )
        out.append(_kv("", f"the file now hashes {differ.now} the same way"))
    if body.written_elsewhere is not None:
        # A SECOND BLOCK, because it is a second fact. It is true of a file published by
        # copying, where nothing changed and the block above is silent; and false of a file
        # edited in place, where that block fires and no other run holds the new bytes.
        also = body.written_elsewhere
        out.append(_kv("ALSO WRITTEN", f"these bytes are the output {also.path}"))
        out.append(_kv("", f"recorded by the run {_q(also.script)}"))

    method = body.method
    out += ["", _rule("the method, and whether it can be got back"), ""]
    commit = method.git_commit or "none recorded"
    if method.git_status_captured is False:
        state = "UNKNOWN — git status did not run"
    elif method.git_code_dirty:
        state = "DIRTY — the code that ran matches no commit"
    else:
        state = "clean"
    out.append(_kv("code", f"{commit}  ({state})"))

    tool = method.tool
    if tool is not None:
        how = _q(tool.source)
        if not tool.identifies_code:
            how += ", DOES NOT IDENTIFY THE CODE"
        elif tool.commit:
            how += f" {str(tool.commit)[:12]}"
        out.append(_kv("recorded by", f"runprov {_q(tool.version)}  ({how})"))
    else:
        # A RECORD FROM BEFORE 0.3.0. Saying so is the point of U-01: the alternative is a
        # page that is silent about its own provenance and looks complete.
        out.append(_kv("recorded by", "not recorded — this run predates the `tool` block"))

    env = method.environment
    if env is not None:
        out.append(_kv("python", _q(env.python)))
        out.append(_kv("platform", _q(env.platform)))
    else:
        out.append(_kv("environment", "in the sidecar beside the artifact, not in the history"))

    out += ["", _rule(f"inputs it was made from ({len(body.inputs)})"), ""]
    if not body.inputs:
        out.append(_kv("", "none pinned"))
    for entry in body.inputs:
        line = f"  {_q(entry.status):<12} {_q(entry.pinned)}  {_q(entry.name)}"
        if entry.status != verify_mod.OK and entry.found:
            line += f"   (now {entry.found})"
        out.append(line)

    obs = body.observation
    steps: typing.Any = None
    packages: typing.Any = None
    truncated: int | None = None
    if obs is not None:
        steps, packages, truncated = (
            obs.steps,
            obs.packages_recorded,
            obs.unregistered_watch_truncated,
        )
    out += ["", _rule("what the run was able to observe"), ""]
    out.append(_kv("steps", _or(steps, "not recorded")))
    out.append(_kv("packages", _or(packages, "not recorded")))
    unregistered = body.unregistered_reads
    if unregistered:
        # THE MOST IMPORTANT LINE ON THE PAGE WHEN IT IS PRESENT, so it is not buried in a
        # count: these files were read and are NOT in the pin above.
        #
        # THE COUNT IS THE WHOLE LIST AND THE LINES ARE THE FIRST TEN, which is the one place
        # a rendering of this report deliberately says less than the report holds. The count
        # is what makes that legible rather than silent, and a machine reader is handed all of
        # them — the asymmetry runs in the safe direction and is asserted, not assumed.
        out.append(
            _kv("UNREGISTERED", f"{len(unregistered)} file(s) were read and are NOT pinned:")
        )
        out += [f"               {p}" for p in unregistered[:10]]
    if truncated:
        # C-06 of Audit C. It belongs HERE above all: the list printed above is the page's
        # answer to "was anything read that is not pinned?", and when the watch hit its cap
        # that list is a sample rather than the answer. Without this line an empty
        # UNREGISTERED reads as "nothing was missed" on the one page a reader consults to
        # judge a single artifact — the conflation the `observation` block exists to end.
        out.append(
            _kv(
                "WATCH TRUNCATED",
                f"at least {truncated} further path(s) were "
                "dropped; the line above is a sample, not a census",
            )
        )

    out += ["", *_limits(report.limits)]
    return out


def page(
    artifact: pathlib.Path,
    root: pathlib.Path,
    history: typing.Iterable[dict[str, typing.Any]] = (),
) -> Page:
    """The page. Separate from printing so the wording itself is testable."""
    report = build(artifact, root, history)
    return Page(render_page(report), report)


def _plain(value: typing.Any) -> typing.Any:  # noqa: ANN401 - the structure, whatever it holds
    """A `Report` as JSON-able dicts and lists, DERIVED FROM `_fields` RATHER THAN RE-TYPED.

    A serialiser that names each field is a second spelling of the structure, and two hand-kept
    spellings of one thing drift in the direction this repository has now found nine times: a
    field added to the structure is simply missing from the payload, silently, on the rendering
    nobody reads. Walking `_asdict()` cannot miss one.

    A path goes out POSIX-spelled, for the reason every recorded path in this package does: a
    record is read on a different machine from the one that wrote it, and
    `str(WindowsPath("data/a.tsv"))` is not what a Linux reader is holding.
    """
    if isinstance(value, tuple) and hasattr(value, "_asdict"):
        return {key: _plain(item) for key, item in value._asdict().items()}
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    if isinstance(value, pathlib.Path):
        return hashing._posix(value)
    return value


def payload(report: Report) -> dict[str, typing.Any]:
    """The report as one object, for a reader that is not a person.

    EVERYTHING THE PAGE STATES, INCLUDING WHAT IT COULD NOT CHECK. `bytes_differ`,
    `observation`, `unregistered_reads` and `limits` are the qualifications, and a payload
    carrying only findings would let a consumer read "could not check" as "nothing wrong" —
    the vacuous green this package exists to refuse. Two audits running have found three
    defects each of exactly that class.

    THE RUN-DEPENDENT KEYS ARE ABSENT RATHER THAN NULL when no run was found, and that is the
    null-versus-absent distinction doing real work rather than a tidiness. A null is *looked
    and found none*; an absent key is *this did not look*. With no run there is no method to
    describe, no observation block to read and no input section on the page at all — the page
    stops after the verdict, and so does this. What a reader must never have to INFER from a
    missing key is the finding itself, so the finding is stated positively and always:
    `limits.run_not_found`.

    `body` IS FLATTENED rather than nested, and it is the one field that is. It exists so that
    six optional blocks can be one optional block — the page either reaches them all or reaches
    none of them — and that is a fact about how the structure is held, not about the artifact.
    A reader of this payload sees the page's own sections at the top level.
    """
    head = {key: _plain(value) for key, value in report._asdict().items() if key != "body"}
    out: dict[str, typing.Any] = {"schema": SCHEMA, **head}
    if report.body is not None:
        out.update(_plain(report.body))
    out["limits"] = _plain(report.limits)
    return out


def _limits(limits: Limits) -> list[str]:
    """What the page cannot tell you — on the page, not in a manual beside it.

    STANDING PROSE PLUS ONE CONDITIONAL CLAUSE, and only the clause is a fact about this
    report. `Limits` carries that fact; these words are this rendering's way of saying it.
    """
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
    if limits.run_not_found:
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
