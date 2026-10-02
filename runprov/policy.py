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

import types
import typing

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
    judge: typing.Callable[[typing.Mapping[str, typing.Any]], Verdict]


_REGISTRY: dict[str, Rule] = {}


def rule(
    name: str, *, asks: str, reads: tuple[str, ...], blind: str
) -> typing.Callable[
    [typing.Callable[[typing.Mapping[str, typing.Any]], Verdict]],
    typing.Callable[[typing.Mapping[str, typing.Any]], Verdict],
]:
    """Register a rule. The decorator IS the registry's only entry point.

    A duplicate name RAISES at import rather than overwriting: two rules under one name means a
    policy naming it gets whichever was imported last, which is a silent change of control.
    """

    def register(
        judge: typing.Callable[[typing.Mapping[str, typing.Any]], Verdict],
    ) -> typing.Callable[[typing.Mapping[str, typing.Any]], Verdict]:
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
    name: str, records: typing.Iterable[typing.Mapping[str, typing.Any]]
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
    tally = {MET: 0, VIOLATED: 0, CANNOT_CHECK: 0}
    reasons: list[str] = []
    for record in records:
        verdict = got.judge(record)
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
def _clean_tree(record: typing.Mapping[str, typing.Any]) -> Verdict:
    """THE RULE R-6 IS ABOUT, which is why it is the first one built.

    `git_tree_dirty` is `False` both for a clean tree and for a run that never looked, and
    `git_status_captured` is the field that separates them. A rule reading only the first would
    report *clean* for every run outside a repository — a control that passes because nothing was
    examined, which is the exact shape this ADR's third requirement exists to refuse.
    """
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
def _no_unregistered_reads(record: typing.Mapping[str, typing.Any]) -> Verdict:
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
def _outputs_pin_inputs(record: typing.Mapping[str, typing.Any]) -> Verdict:
    """THE LIMIT IS NAMED RATHER THAN GUESSED AT, and it is the honest half of this rule.

    A record showing outputs and no inputs is either a generator that truly read nothing or a run
    that read without registering. The HISTORY cannot separate them — `no_unregistered_reads` is
    the rule that can, which is why these two belong in a policy together and why this one does
    not pretend to subsume it.
    """
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
def _commit_recorded(record: typing.Mapping[str, typing.Any]) -> Verdict:
    """THE SAME TWO-FIELD SHAPE AS `clean_tree`, and for the same reason.

    `git_commit` is `None` both for a run outside a repository and for one in a repository with no
    commit to name. Only `git_status_captured` separates *we could not look* from *there was
    nothing there*, and a rule reading the commit alone would report every run on an unversioned
    machine as a violation — an accusation rather than a finding.
    """
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
def _environment_captured(record: typing.Mapping[str, typing.Any]) -> Verdict:
    """`packages: {}` CANNOT BE TOLD FROM *NOBODY ASKED*, which ADR-0010 states in those words.

    So the verdict comes from `observation.packages_recorded` — a closed vocabulary of `snapshot`,
    `tracked` and `none` — rather than from the dict's emptiness. `none` is a VIOLATION and not an
    inability: the record positively says nothing was captured, which is an answer.
    """
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
def _finished_ok(record: typing.Mapping[str, typing.Any]) -> Verdict:
    """A MISSING STATUS IS NOT A FAILURE, which is the distinction `show`'s INTERRUPTED exists for.

    A `start` line with no ending means the run is in flight or was killed before it could say
    anything — and a policy that read that as a violation would turn every machine that lost power
    into a non-compliant one, permanently, in a record nobody can amend.
    """
    status = record.get("status")
    if status is None:
        return Verdict(CANNOT_CHECK, "the record carries no status, so the run recorded no ending")
    if status == "ok":
        return Verdict(MET)
    failure = record.get("failure")
    named = f": {failure}" if failure else ""
    return Verdict(VIOLATED, f"the run finished with status {status!r}{named}")
