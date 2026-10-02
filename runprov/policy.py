# Copyright (c) 2026 Assistance Publique – Hôpitaux de Paris (AP-HP),
#                    Hôpital Henri-Mondor, and Taylor Nicole Thompson
# SPDX-License-Identifier: BSD-3-Clause
# Licensed under the BSD 3-Clause License — see LICENSE.
"""Rules a project sets for itself, checked against the history it already wrote. T-34.

`check` is static: it reads source and answers *could this code fail to record*. This answers
the other half — *did the runs that actually happened meet the rules this project set?* — and so
it reads only the history and never the source.

THE SPECIFICATION IS `docs/adr/0018-a-policy-is-checked-against-the-history-not-remembered.md`,
and it is deliberately not cited by number in this docstring yet.
`test_every_adr_is_listed_in_the_adr_index` reads a module's TOP docstring to decide which
decisions are BUILT and asserts none of those is still proposed. That decision is still proposed
because this feature is half-built, so the citation belongs in the LAST build commit — which is
exactly the closing condition T-33 shipped without, and the guard enforcing the right order
rather than merely describing it.

THE REGISTRY IS THE ONLY LIST OF RULES. R-5: a rule registers itself here, and the parser, the
documentation and the tests all read this one dict. Three places to update is two places to
forget, and that is the scope pattern this codebase has now found ten times.
"""

from __future__ import annotations

import importlib
import json
import pathlib
import types
import typing

from . import hashing

#: A rule's three answers. `CANNOT_CHECK` is the whole design (R-2, R-3): *violated* and
#: *unverifiable* are different findings, and a policy engine that collapses them produces the
#: vacuous green several ledger rows already exist about.
MET = "MET"
VIOLATED = "VIOLATED"
CANNOT_CHECK = "CANNOT_CHECK"
OUTCOMES = (MET, VIOLATED, CANNOT_CHECK)


class Verdict(typing.NamedTuple):
    """One rule's answer about one run, and why — never a bare bool.

    `reason` is REQUIRED for anything but `MET`, because a gate that says VIOLATED without
    saying what it read sends a reader to the history to guess, and a gate that says
    CANNOT_CHECK without saying what was missing is indistinguishable from a bug.
    """

    outcome: str
    reason: str | None = None


class Context(typing.NamedTuple):
    """What a rule may consult BESIDES the record — declared, so a verdict's inputs are readable.

    Five of the six rules never touch this, and that is the point of passing it to all of them
    rather than letting one reach for a module global: a rule's inputs are its `reads` plus this,
    both written down, so *what could this answer have depended on* is answerable from the
    signature. R-9 is the boundary it must not cross — the source is off limits; the data the
    history declares is not.

    `digests` MEMOISES ONE `assess`, for the reason `verify.check_input` documents in the same
    words: a fan-out of N runs declaring one reference genome is N full reads of it otherwise.
    Keyed by `(path, key)` AND NOT BY PATH ALONE, because `sha256` and `content_sha256` are two
    different quantities taken from the same bytes, and a cache that forgot which one it holds
    would compare a raw digest against a content one — I-02, which is the defect this rule's
    like-for-like comparison exists to avoid.

    IT IS PER-CALL AND DELIBERATELY NOT A MODULE-LEVEL `lru_cache`. A digest cache that outlived
    one `assess` would answer a later question with an earlier run's bytes, which is the exact
    opposite of what this command exists to establish.
    """

    digests: dict[tuple[str, str], str | None] | None = None


class Rule(typing.NamedTuple):
    """One question the record already answers, and what the question cannot see.

    `reads` IS NOT DOCUMENTATION. R-10 says a rule may only assert over fields that exist, and
    this is how that is checked: a test compares every rule's `reads` against the keys a real
    record carries, so a rule inventing a field fails rather than silently evaluating `None`.

    `blind` IS R-6, in the rule's own words. A rule asserting *no unregistered reads* can only
    speak for runs whose watch did not hit its cap; one asserting *the tree was clean* can only
    speak for runs whose git status was captured at all. A rule that reads a field without
    reading that field's own blindness mark is the C-06 defect rewritten as a policy, and it is
    the single most likely way this feature goes quietly wrong.
    """

    name: str
    asks: str
    reads: tuple[str, ...]
    blind: str
    judge: typing.Callable[[typing.Mapping[str, typing.Any], Context], Verdict]


_REGISTRY: dict[str, Rule] = {}


def rule(
    name: str, *, asks: str, reads: tuple[str, ...], blind: str
) -> typing.Callable[
    [typing.Callable[[typing.Mapping[str, typing.Any], Context], Verdict]],
    typing.Callable[[typing.Mapping[str, typing.Any], Context], Verdict],
]:
    """Register a rule. The decorator IS the registry's only entry point.

    A duplicate name RAISES at import rather than overwriting: two rules under one name means a
    policy naming it gets whichever was imported last, which is a silent change of control.
    """

    def register(
        judge: typing.Callable[[typing.Mapping[str, typing.Any], Context], Verdict],
    ) -> typing.Callable[[typing.Mapping[str, typing.Any], Context], Verdict]:
        if name in _REGISTRY:  # pragma: no cover - a programming error, asserted by a test
            raise ValueError(f"two rules registered as {name!r}")
        _REGISTRY[name] = Rule(name, asks, reads, blind, judge)
        return judge

    return register


def rules() -> types.MappingProxyType[str, Rule]:
    """Every registered rule, read-only.

    A PROXY RATHER THAN A COPY, so a caller that mutates what it is given fails instead of
    editing a private dict through a door nobody meant to leave open — `pending()` in `__main__`
    is the row where a method that read like a query consumed its receiver.
    """
    return types.MappingProxyType(_REGISTRY)


def assess(
    name: str,
    records: typing.Iterable[typing.Mapping[str, typing.Any]],
    context: Context | None = None,
) -> dict[str, typing.Any]:
    """One rule over many runs, with the count it was evaluated against. R-4.

    `evaluated` IS BESIDE THE OUTCOME AND IS NOT DECORATION: *no violations* over a log matching
    zero runs is `check`'s A-08 defect one level up, where a sweep that parsed no files printed a
    clean bill and exited 0. A caller can tell *every run met this* from *this was asked of
    nothing* only if the count travels.

    THE WORST OUTCOME DECIDES, in the order VIOLATED, CANNOT_CHECK, MET — and that order is the
    fact rather than an implementation detail. A violation among runs that could not all be
    checked is still a violation; an unchecked run among passes is not a pass.
    """
    got = _REGISTRY[name]
    #: ONE CACHE PER CALL, built here rather than demanded of the caller, so a caller that knows
    #: nothing about digests still gets the memoisation and no caller can hand in a stale one.
    context = Context() if context is None else context
    if context.digests is None:
        context = context._replace(digests={})
    tally = {MET: 0, VIOLATED: 0, CANNOT_CHECK: 0}
    reasons: list[str] = []
    for record in records:
        verdict = got.judge(record, context)
        tally[verdict.outcome] += 1
        if verdict.outcome != MET and verdict.reason and verdict.reason not in reasons:
            reasons.append(verdict.reason)
    evaluated = sum(tally.values())
    if tally[VIOLATED]:
        outcome = VIOLATED
    elif tally[CANNOT_CHECK] or not evaluated:
        outcome = CANNOT_CHECK
    else:
        outcome = MET
    return {
        "rule": name,
        "asks": got.asks,
        "blind": got.blind,
        "outcome": outcome,
        "evaluated": evaluated,
        "met": tally[MET],
        "violated": tally[VIOLATED],
        "cannot_check": tally[CANNOT_CHECK],
        #: EMPTY over a clean pass, and a LIST because several runs can fail differently and a
        #: reader repairs each one separately.
        "reasons": reasons,
        #: R-4 again, as a sentence rather than an inference: a rule asked of nothing is not a
        #: rule that passed, and this is the field that says so without a consumer doing
        #: arithmetic on the three tallies.
        "asked_of_nothing": not evaluated,
    }


@rule(
    "clean_tree",
    asks="the working tree was clean when the run started",
    reads=("git_tree_dirty", "git_status_captured"),
    blind="a run whose git status could not be captured at all — outside a repository, or with "
    "no git on PATH; the record says so and this rule reports it rather than reading the "
    "absence as clean",
)
def _clean_tree(record: typing.Mapping[str, typing.Any], context: Context) -> Verdict:
    """THE RULE R-6 IS ABOUT, which is why it is the first one built.

    `git_tree_dirty` is `False` both for a clean tree and for a run that never looked, and
    `git_status_captured` is the field that separates them. A rule reading only the first would
    report *clean* for every run outside a repository — a control that passes because nothing was
    examined, which is the exact shape this ADR's third requirement exists to refuse.
    """
    del context  # this rule reads the record only
    if not record.get("git_status_captured"):
        return Verdict(CANNOT_CHECK, "git status was not captured, so the tree state is unknown")
    if record.get("git_tree_dirty"):
        return Verdict(VIOLATED, "the working tree had uncommitted changes when the run started")
    return Verdict(MET)


@rule(
    "no_unregistered_reads",
    asks="the run opened no data file it did not register",
    reads=("unregistered_reads", "observation.unregistered_watch_truncated"),
    blind="a run whose record carries no `observation` block at all — written by a version "
    "before the watch existed; and, named here because the record cannot close it, a run whose "
    "watch itself raised, which warns on stderr and leaves both fields unset exactly as a clean "
    "run does",
)
def _no_unregistered_reads(record: typing.Mapping[str, typing.Any], context: Context) -> Verdict:
    """R-6'S OWN EXAMPLE: the field that qualifies the answer is consulted, not just the answer.

    `unregistered_reads` is ABSENT from a clean run rather than empty, so absence is the ordinary
    good case — and that is why the `observation` block's presence is what makes this evaluable.
    A record without one came from a version that did not watch, and reading its silence as
    *nothing was missed* would be the vacuous green over the whole of history.

    A VIOLATION OUTRANKS THE TRUNCATION, and the order is the fact. When the watch hit its cap AND
    still caught something, at least one read was certainly missed — knowing part of a list is
    knowing a violation. The reverse, an empty list after the cap bit, is the C-06 defect and is
    never a pass.
    """
    del context  # this rule reads the record only
    if record.get("observation") is None:
        return Verdict(CANNOT_CHECK, "the record carries no observation block, so nothing watched")
    missed = record.get("unregistered_reads") or []
    if missed:
        return Verdict(
            VIOLATED, f"{len(missed)} file(s) were read without being registered: {missed[0]}"
        )
    dropped = (record.get("observation") or {}).get("unregistered_watch_truncated")
    if dropped:
        return Verdict(
            CANNOT_CHECK,
            f"the watch lost at least {dropped} path(s) to its cap, so an empty list no longer "
            "means none were missed",
        )
    return Verdict(MET)


@rule(
    "outputs_pin_inputs",
    asks="every run that produced an output declared what it read",
    reads=("outputs", "inputs"),
    blind="a run that recorded no outputs, which this rule has nothing to ask about — and a run "
    "that genuinely read nothing, which is indistinguishable in the history from one that read "
    "something and failed to say so",
)
def _outputs_pin_inputs(record: typing.Mapping[str, typing.Any], context: Context) -> Verdict:
    """THE LIMIT IS NAMED RATHER THAN GUESSED AT, and it is the honest half of this rule.

    A record showing outputs and no inputs is either a generator that truly read nothing or a run
    that read without registering. The HISTORY cannot separate them — `no_unregistered_reads` is
    the rule that can, which is why these two belong in a policy together and why this one does
    not pretend to subsume it.
    """
    del context  # this rule reads the record only
    outputs = record.get("outputs")
    if outputs is None:
        return Verdict(CANNOT_CHECK, "the record does not say what this run produced")
    if not outputs:
        return Verdict(CANNOT_CHECK, "the run recorded no outputs, so there is nothing to ask")
    if record.get("inputs"):
        return Verdict(MET)
    return Verdict(
        VIOLATED, f"{len(outputs)} output(s) recorded and no input declared for any of them"
    )


@rule(
    "commit_recorded",
    asks="the run names the commit it ran from",
    reads=("git_commit", "git_status_captured"),
    blind="a run outside a repository or with no git on PATH, where the absence of a commit is "
    "not a missing one — the record says which through `git_status_captured`",
)
def _commit_recorded(record: typing.Mapping[str, typing.Any], context: Context) -> Verdict:
    """THE SAME TWO-FIELD SHAPE AS `clean_tree`, and for the same reason.

    `git_commit` is `None` both for a run outside a repository and for one in a repository with no
    commit to name. Only `git_status_captured` separates *we could not look* from *there was
    nothing there*, and a rule reading the commit alone would report every run on an unversioned
    machine as a violation — an accusation rather than a finding.
    """
    del context  # this rule reads the record only
    if not record.get("git_status_captured"):
        return Verdict(CANNOT_CHECK, "git status was not captured, so no commit could be recorded")
    if record.get("git_commit"):
        return Verdict(MET)
    return Verdict(VIOLATED, "git status was captured and the record names no commit")


@rule(
    "environment_captured",
    asks="the run recorded the packages it ran with",
    reads=("packages", "observation.packages_recorded"),
    blind="a record with no `observation` block, which cannot say whether packages were recorded "
    "at all — `packages: {}` alone is indistinguishable from nobody having asked, which is the "
    "whole reason ADR-0010 added the field this rule reads",
)
def _environment_captured(record: typing.Mapping[str, typing.Any], context: Context) -> Verdict:
    """`packages: {}` CANNOT BE TOLD FROM *NOBODY ASKED*, which ADR-0010 states in those words.

    So the verdict comes from `observation.packages_recorded` — a closed vocabulary of `snapshot`,
    `tracked` and `none` — rather than from the dict's emptiness. `none` is a VIOLATION and not an
    inability: the record positively says nothing was captured, which is an answer.
    """
    del context  # this rule reads the record only
    observation = record.get("observation")
    if observation is None:
        return Verdict(CANNOT_CHECK, "the record carries no observation block")
    recorded = observation.get("packages_recorded")
    if recorded is None:
        return Verdict(
            CANNOT_CHECK, "the record does not say whether packages were recorded at all"
        )
    if recorded == "none":
        return Verdict(VIOLATED, "the run recorded no packages")
    return Verdict(MET)


@rule(
    "finished_ok",
    asks="the run reached its end and recorded success",
    reads=("status", "failure"),
    blind="a line carrying no status — a `start` with no ending is a run still going or one "
    "killed before it could record, and neither is a failure this rule may assert",
)
def _finished_ok(record: typing.Mapping[str, typing.Any], context: Context) -> Verdict:
    """A MISSING STATUS IS NOT A FAILURE, which is the distinction `show`'s INTERRUPTED exists for.

    A `start` line with no ending means the run is in flight or was killed before it could say
    anything — and a policy that read that as a violation would turn every machine that lost power
    into a non-compliant one, permanently, in a record nobody can amend.
    """
    del context  # this rule reads the record only
    status = record.get("status")
    if status is None:
        return Verdict(CANNOT_CHECK, "the record carries no status, so the run recorded no ending")
    if status == "ok":
        return Verdict(MET)
    failure = record.get("failure")
    named = f": {failure}" if failure else ""
    return Verdict(VIOLATED, f"the run finished with status {status!r}{named}")


#: THE RECORD'S OWN KEY, paired with the function that computes THAT quantity. I-02's sentence:
#: like for like, or it is not a comparison. The choice is made on what the entry CARRIES and
#: never on today's precedence — a v1-shaped entry holding only a raw `sha256`, compared against
#: a content digest, reports every untouched text file as changed, which is the defect `report`
#: was repaired for.
#:
#: `report._COMPARABLE_AS` IS THE SIBLING OF THIS AND IS DELIBERATELY NOT SHARED. It omits
#: `sha256_tree` for a reason local to its caller — `build` hashes with `hashing.sha256`, which
#: raises on a directory, so that arm is unreachable there and an arm that cannot fire reads as a
#: case that can happen. This rule is handed whatever the history declared, directories included,
#: and answers for one differently below. Two callers whose reasons differ must not share one
#: constant: the next widening would be made for one of them and inherited by the other.
_COMPARABLE_AS: tuple[tuple[str, typing.Callable[[pathlib.Path], str | None]], ...] = (
    ("content_sha256", hashing.content_digest),
    ("sha256", hashing.sha256),
)


def _comparable_on(entry: typing.Mapping[str, typing.Any]) -> tuple[str, str, typing.Any] | None:
    """The one key this entry can be re-checked on, with the function that recomputes it.

    `None` means the entry carries no digest this rule knows how to take again — not that it is
    wrong. The caller turns that into `CANNOT_CHECK`, because a rule that cannot recompute a
    quantity has not found a violation of it.
    """
    for key, how in _COMPARABLE_AS:
        was = entry.get(key)
        if was:
            return key, str(was), how
    return None


def _and_more(reasons: list[str]) -> str:
    """One sentence for a list of them: the first, and how many more there were."""
    if len(reasons) == 1:
        return reasons[0]
    return f"{reasons[0]} (and {len(reasons) - 1} more)"


def _input_still_matches(entry: typing.Mapping[str, typing.Any], context: Context) -> Verdict:
    """One declared input, re-read from disk. The three answers, for that one file."""
    raw = entry.get("path")
    if not raw:
        return Verdict(CANNOT_CHECK, "an input entry carries no path")
    #: A DECLARED DIRECTORY IS NOT RE-WALKED HERE, and the limit is named rather than approximated.
    #: Reproducing `sha256_tree` means reproducing the ordering key A-14 and C-03 were both about,
    #: and then deciding whether a record's `sha256_tree_casefolded` or its `sha256_tree` is
    #: authoritative for a tree written by an older version on Windows. `verify` makes that
    #: decision and is its only reader; a second implementation of it inside a policy rule is how
    #: the two would come to disagree.
    if entry.get("kind") == "tree" or entry.get("sha256_tree"):
        return Verdict(
            CANNOT_CHECK,
            f"{raw} was declared as a directory, which `runprov verify` re-walks and this rule "
            "does not",
        )
    comparable = _comparable_on(entry)
    if comparable is None:
        return Verdict(CANNOT_CHECK, f"{raw} carries no digest this rule can take again")
    key, was, how = comparable
    seen = context.digests
    if seen is not None and (raw, key) in seen:
        now = seen[(raw, key)]
    else:
        try:
            #: THE ADR'S OWN CASE, AND IT IS AN INABILITY RATHER THAN A VIOLATION. R-9's
            #: clarification says so in those words: a declared input that is gone is a
            #: `CANNOT_CHECK` naming the path. The alternative accuses a run of changing a file
            #: that may simply have been archived, in a record nobody can amend.
            if not pathlib.Path(raw).exists():
                return Verdict(
                    CANNOT_CHECK,
                    f"{raw} is no longer on disk, so what it holds now cannot be compared with "
                    "what the run recorded",
                )
            now = how(pathlib.Path(raw))
        except OSError as exc:
            return Verdict(CANNOT_CHECK, f"{raw} could not be read: {exc.strerror or exc}")
        if seen is not None:
            seen[(raw, key)] = now
    if now is None:
        return Verdict(
            CANNOT_CHECK, f"{raw} is not a file whose {key} can be taken, so nothing was compared"
        )
    if now != was:
        width = hashing.PIN_DIGEST_CHARS
        #: COMPARED WHOLE, DISPLAYED SHORT — `report`'s sentence, for the same reason. Truncating
        #: before the comparison lets two different digests agree on sixteen characters and pass.
        return Verdict(
            VIOLATED,
            f"{raw} no longer matches the {key} the run recorded ({was[:width]} -> {now[:width]})",
        )
    return Verdict(MET)


@rule(
    "inputs_verify",
    asks="every input the run declared still hashes to what it recorded",
    reads=("inputs",),
    blind="a declared input that is no longer on disk, or a run that declared none at all — and, "
    "named here because the record cannot close it, a history read on a machine other than the "
    "one that wrote it, where the recorded path is absolute and names nothing even though the "
    "file itself still exists somewhere",
)
def _inputs_verify(record: typing.Mapping[str, typing.Any], context: Context) -> Verdict:
    """THE ONE RULE THAT LEAVES THE RECORD, which is why R-9 had to be clarified before it.

    Taylor's ruling, 2026-10-02: *the source* means SOURCE CODE — the thing `check` parses — not
    the filesystem. Data files are not source, and *does the evidence still match the files* is
    the question an accredited laboratory asks first. So this rule re-hashes, and the cost is
    stated rather than hidden: with it in a policy, `gate` scales with the DATA and not with the
    history.

    IT IS ALSO THE RULE THAT FORCED THE `judge` CONTRACT TO TAKE A SECOND ARGUMENT, and the
    reason is the cache and not a root. The recorded `path` is ABSOLUTE, so there is nothing to
    resolve it against — I had planned a `root` and dropped it when the record settled the
    question: a root would be a second answer to *where is this file* competing with the one the
    history already gives, and R-9's clarification states the behaviour for a path that names
    nothing, which is `CANNOT_CHECK`. What a root WOULD buy — re-checking a history on a machine
    that did not write it — is not in ADR-0018, and an unspecified option is how a feature grows
    a flag nobody asked for. The rule's `blind` text names that limit instead.

    NOT `verify.check_input`, AND THE REASON IS WORTH STATING. It asks exactly this question, and
    `pin_digest(describe(f))` is exactly `content_sha256[:16]`, so the two conventions ARE
    compatible after truncation. But truncating throws away 48 characters of a digest the history
    already holds at full width: a pin is 16 characters because it lives in an artifact's own
    bytes, and a gate has no such constraint. It compares all 256 bits.
    """
    declared = record.get("inputs")
    if declared is None:
        return Verdict(CANNOT_CHECK, "the record does not say what this run read")
    if not declared:
        return Verdict(CANNOT_CHECK, "the run declared no inputs, so there is nothing to re-hash")
    changed: list[str] = []
    unknown: list[str] = []
    for entry in declared:
        verdict = _input_still_matches(entry, context)
        if verdict.outcome == VIOLATED:
            changed.append(verdict.reason or "")
        elif verdict.outcome == CANNOT_CHECK:
            unknown.append(verdict.reason or "")
    #: EVERY ENTRY IS RE-READ BEFORE THE VERDICT IS FORMED, and this is deliberately not
    #: short-circuited on the first mismatch. The question a laboratory asks is *which* of the
    #: declared files no longer match, and a gate naming one and stopping sends the reader back
    #: for a second run to find the next. The MET case reads all of them anyway, so the only
    #: saving forgone is in the case that has already failed.
    if changed:
        return Verdict(VIOLATED, _and_more(changed))
    if unknown:
        return Verdict(CANNOT_CHECK, _and_more(unknown))
    return Verdict(MET)


class PolicyError(Exception):
    """A policy file that cannot be used, with a sentence naming what to fix.

    NOT `__main__.UsageError`, AND THE IMPORT DIRECTION IS THE WHOLE REASON. This module must not
    import the command module: `check`, `verify` and `report` are importable without a CLI, and a
    policy parser that dragged `__main__` in would make `import runprov.policy` build an argument
    parser. The command catches this and turns it into its own exit 2 — which is L-81's code for
    *could not check*, and a malformed policy is exactly that: nothing was examined.
    """


#: THE KEYS A RULE ENTRY MAY CARRY, and there are two. `why` is required by R-11 and is not a
#: comment: a comment cannot be checked and does not travel, while a field reaches the page and
#: the payload beside the verdict, where an assessor reads it.
_RULE_KEYS = ("rule", "why")


def _toml_parser() -> types.ModuleType:
    """The TOML parser this interpreter has, or a `PolicyError` naming its own fix. R-11.

    `importlib.import_module` RATHER THAN TWO GUARDED `import` STATEMENTS, so all three arms are
    reachable on one interpreter: a test replaces the loader and the *3.10 without the extra* arm
    is exercised on 3.12. A version-guarded `import` would leave a branch only one leg of the
    matrix could enter, and a branch only CI can reach is a branch whose failure nobody reads.

    IT IS ALSO THE ONLY WAY TO REACH `tomllib` AT ALL AT THIS FLOOR, which is the reason that
    needs no argument. `requires-python` is `>=3.10` and the suite's `_TOO_YOUNG` guard refuses a
    static import of any stdlib module younger than the declared floor — it names `tomllib` and
    `(3, 11)` explicitly. A bare `import tomllib` is therefore not something this package may
    write until the floor moves, whatever the `try` around it says.

    `tomllib` FIRST. On 3.11+ it is the standard library and `tomli` is the same parser under its
    original name; preferring an installed extra would let the version a user happens to have
    decide how their policy is parsed.
    """
    for name in ("tomllib", "tomli"):
        try:
            return importlib.import_module(name)
        except ModuleNotFoundError:
            continue
    raise PolicyError(
        "a .toml policy needs a TOML parser, and this interpreter has none: `tomllib` is in the "
        "standard library from Python 3.11, and on 3.10 the extra supplies the same parser — "
        "`pip install runprov[toml]`. A .json policy needs nothing and works on every supported "
        "version."
    )


def load(path: str | pathlib.Path) -> dict[str, typing.Any]:
    """A policy file, parsed, checked against the registry, and normalised. R-11.

    JSON OR TOML, DECIDED BY THE SUFFIX AND NOT BY SNIFFING THE CONTENT. A file that is tried as
    one format and then the other reports the second parser's error for a typo in the first, which
    sends the reader to the wrong line of their own file.

    EVERYTHING THIS RAISES NAMES THE FILE AND THE FIX. A policy is a document a laboratory is
    audited against; *invalid policy* is not a finding anybody can act on.

    THE TWO FORMATS RETURN THE SAME STRUCTURE, which is R-1's shape one level down: one checked
    representation, two ways of writing it, and a test asserts the same policy written both ways
    loads equal.
    """
    path = pathlib.Path(path)
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise PolicyError(f"{path} could not be read: {exc.strerror or exc}") from None
    except UnicodeDecodeError:
        raise PolicyError(f"{path} is not UTF-8 text, so it is neither JSON nor TOML") from None
    suffix = path.suffix.lower()
    if suffix == ".json":
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise PolicyError(f"{path} is not valid JSON: {exc}") from None
    elif suffix == ".toml":
        parser = _toml_parser()
        try:
            parsed = parser.loads(raw)
        except parser.TOMLDecodeError as exc:
            raise PolicyError(f"{path} is not valid TOML: {exc}") from None
    else:
        raise PolicyError(
            f"{path} is neither a .json nor a .toml policy "
            f"({suffix or 'the name carries no suffix'}), and the suffix is what chooses the parser"
        )
    return _checked(parsed, path)


def _checked(parsed: object, path: pathlib.Path) -> dict[str, typing.Any]:
    """The parsed file against the registry, or a `PolicyError` naming the one thing wrong.

    READ STRICTLY, AND THAT IS THE POINT RATHER THAN PEDANTRY. A policy key this version does not
    understand is refused instead of ignored: `rule = "clean_tree"` under a mistyped table name is
    a control the author believes is in force and the gate never applies — a control that was lost
    to a typo, which is the whole failure this feature exists to prevent and would be an
    especially bitter way to produce it.

    THE REGISTRY DECIDES WHICH NAMES EXIST. R-5: there is no list of rules here, and an unknown
    name is reported with the names that do exist, because *unknown rule* without them sends the
    reader to the source of a package they installed.
    """
    if not isinstance(parsed, dict):
        raise PolicyError(
            f"{path} is a {type(parsed).__name__} at the top level; a policy is a table with a "
            "`rules` list"
        )
    unknown = sorted(set(parsed) - {"rules"})
    if unknown:
        raise PolicyError(
            f"{path} carries settings this version does not understand: {', '.join(unknown)}. "
            "They are refused rather than ignored, because a rule under a mistyped name is a "
            "control its author believes is in force and the gate never applies"
        )
    declared = parsed.get("rules")
    if not isinstance(declared, list) or not declared:
        raise PolicyError(
            f"{path} must carry a non-empty `rules` list; an empty policy would pass every "
            "history, which is the vacuous green in a file"
        )
    registered = rules()
    chosen: list[dict[str, str]] = []
    seen: set[str] = set()
    for index, entry in enumerate(declared, start=1):
        where = f"{path} rule {index}"
        if not isinstance(entry, dict):
            raise PolicyError(
                f"{where} is a {type(entry).__name__}; each rule is a table carrying `rule` and "
                "`why`"
            )
        extra = sorted(set(entry) - set(_RULE_KEYS))
        if extra:
            raise PolicyError(
                f"{where} carries keys a rule does not take: {', '.join(extra)}. A rule takes "
                f"{' and '.join(_RULE_KEYS)}"
            )
        name = entry.get("rule")
        if not isinstance(name, str) or not name.strip():
            raise PolicyError(f"{where} does not name a rule")
        name = name.strip()
        if name not in registered:
            raise PolicyError(
                f"{where} names `{name}`, which is not a rule this version has. The rules are: "
                f"{', '.join(sorted(registered))}"
            )
        if name in seen:
            raise PolicyError(
                f"{where} names `{name}` a second time, and two reasons for one rule leave no "
                "answer to which of them the gate applied"
            )
        why = entry.get("why")
        if not isinstance(why, str) or not why.strip():
            raise PolicyError(
                f"{where} (`{name}`) carries no `why`. R-11: a comment cannot be checked and does "
                "not travel, so the reason a control exists is a required field that reaches the "
                "report beside the verdict"
            )
        seen.add(name)
        #: THE AUTHOR'S ORDER IS KEPT. A policy read back in a different order from the one it was
        #: written in is a document whose diff stops being the evidence that it did not loosen.
        chosen.append({"rule": name, "why": why.strip()})
    return {"rules": chosen}
