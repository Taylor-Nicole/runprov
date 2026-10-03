# Copyright (c) 2026 Assistance Publique – Hôpitaux de Paris (AP-HP),
#                    Hôpital Henri-Mondor, and Taylor Nicole Thompson
# SPDX-License-Identifier: BSD-3-Clause
# Licensed under the BSD 3-Clause License — see LICENSE.
"""Rules a project sets for itself, checked against the history it already wrote. T-34.

`check` is static: it reads source and answers *could this code fail to record*. This answers
the other half — *did the runs that actually happened meet the rules this project set?* — and so
it reads only the history and never the source.

ADR-0018 IS THE SPECIFICATION, and the citation landed in the last build commit rather than the
first. `test_every_adr_is_listed_in_the_adr_index` reads a module's TOP docstring to decide which
decisions are BUILT and asserts none of those is still proposed — so naming the number here while
the feature was half-built would have turned that guard red, and the number arrived with the
twelfth requirement. ADR-0017 is the reason the ordering is worth stating: its status read *a
feature that is not built* through the release that shipped it, because **eight** modules cited it
in function docstrings and comments and **none in a module docstring**, which is the one place the
guard looks.

EIGHT IS THE RELEASED COUNT AND IT IS CHECKABLE FOREVER [K-38]: `git archive v0.7.0` and look, a
tag cannot move. This sentence said nine, which is the count AFTER the repair that followed —
measured on the wrong side of the fix it describes, which is the same error as taking a figure
from the instrument you are correcting.

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
from .terminal import printable

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
    Keyed by `(resolved path, key)` AND NOT BY PATH ALONE, because `sha256` and `content_sha256`
    are two different quantities taken from the same bytes, and a cache that forgot which one it
    holds would compare a raw digest against a content one — I-02, which is the defect this rule's
    like-for-like comparison exists to avoid.

    THE *RESOLVED* PATH AND NEVER THE RECORDED SPELLING [K-17]. Two runs in two projects that
    each declared `data/m.tsv` write the same spelling and mean different files: a cache keyed on
    the spelling answers the second run with the first one's bytes and names a real file as
    altered with nothing on disk touched. Measured: `1 met, 1 violated`, exit 1, over two
    projects sharing one history and the gate run from one project's own root.

    IT IS PER-CALL AND DELIBERATELY NOT A MODULE-LEVEL `lru_cache`. A digest cache that outlived
    one `assess` would answer a later question with an earlier run's bytes, which is the exact
    opposite of what this command exists to establish.

    **BUT A CALLER THAT HANDS IN A `Context` OWNS ITS FRESHNESS [K-31].** This paragraph used to
    say *IT IS PER-CALL* as an absolute and `assess`'s own comment said *no caller can hand in a
    stale one*; both were false. `assess` replaces `digests` only when it is `None`, so a cache
    passed in is used verbatim — reproduced: zero files read, and an unchanged file reported
    VIOLATED, which is a false accusation and the one answer a gate must never give.

    That behaviour is DELIBERATE and asserted by
    `test_the_digest_cache_is_one_assess_wide_and_no_wider`, because `assessment()` needs one
    cache across every rule in a policy rather than one per rule; what was wrong was the promise
    written beside it. So: a library caller that reuses a `Context` between calls is reusing
    digests, and the entries have to still describe the disk.
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

    **AND THE EM DASH IN `blind` IS LOAD-BEARING, WHICH IS A CONSTRAINT ON THE NEXT RULE WRITTEN
    HERE [K-39].** The README's rule table documents what each rule cannot see, and
    `test_the_readme_rule_table_is_the_registry_and_not_a_copy_of_it` holds its third column
    EQUAL to `blind.split(" — ")[0]` — the first clause of this sentence. The comparison was
    `startswith`, which an EMPTY cell satisfies, and a one-character one, and another rule's
    opening words: four of the seven texts begin *"a run "*. So a `blind` written as one long
    clause with no em dash gives the README a cell it has to carry whole, and the split is where
    a rule says *this much is the documented limit, the rest is the detail*.
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
    #: nothing about digests still gets the memoisation. **AND A CALLER THAT HANDS ONE IN OWNS
    #: ITS FRESHNESS [K-31].** This said *no caller can hand in a stale one*, which is false, and
    #: the failure mode is the one output a gate must never produce: `digests` is replaced only
    #: when it is `None`, so a `Context(digests={(path, key): "deadbeef" * 8})` is used verbatim,
    #: **zero files are read**, and an unchanged file reports VIOLATED. Reproduced, including the
    #: zero reads — patching `content_digest` to raise still gave VIOLATED.
    #:
    #: **THE CODE IS RIGHT AND THE SENTENCE WAS WRONG, which is why only the sentence moved.**
    #: Replacing `digests` unconditionally would break
    #: `test_the_digest_cache_is_one_assess_wide_and_no_wider`, which asserts the opposite
    #: behaviour deliberately — *a caller that supplies one gets it used rather than replaced* —
    #: and `assessment()` depends on exactly that to read each file ONCE across every rule in a
    #: policy instead of once per rule. `assess`, `Context` and `digests` are all public, so the
    #: old sentence told the next caller that reusing a context is impossible and that nobody
    #: need guard against it.
    context = Context() if context is None else context
    if context.digests is None:
        context = context._replace(digests={})
    tally = {MET: 0, VIOLATED: 0, CANNOT_CHECK: 0}
    reasons: list[str] = []
    blocked: list[str] = []
    for record in records:
        verdict = got.judge(record, context)
        tally[verdict.outcome] += 1
        #: A BREACH AND AN INABILITY GO TO DIFFERENT LISTS [K-10]. They used to share one, and
        #: the page printed every member of it under the word `finding` — so *the run recorded no
        #: outputs, so there is nothing to ask* was rendered as an accusation, which is Audit H's
        #: "a true verdict wearing a false sentence" in new code. A person can tell the two apart
        #: by reading them; a CONSUMER cannot, because `reasons` is `list[str]` and `len(reasons)`
        #: was 2 where `violated` was 1. The outcome is already in hand here, which is what makes
        #: this a split rather than a second pass.
        #:
        #: `blocked` IS ADR-0014's WORD FOR THIS AND THE BORROWING IS DELIBERATE. `diff` keeps an
        #: incomparability in its own field under that name rather than mixing it into the
        #: differences, which is this same decision one command over. A DECLARED collision, in
        #: R-9/R-12's sense: `diff.Dimension.blocked` is one `str | None` about a dimension and
        #: this is a `list[str]` about a rule's runs, so the two agree on the MEANING and not on
        #: the type. **THE DECLARATION HAS MOVED TO WHERE THE CHECK READS IT [K-16].** It was
        #: written here because `test_no_undeclared_name_means_two_things_across_the_commands`
        #: read top-level keys only and could not see a nested one; that guard now walks every
        #: leaf, so `blocked` is a row in its `_SHARED_NAMES` table and a reason in a comment is
        #: no longer the only thing standing between this name and the next audit.
        if verdict.reason:
            into = reasons if verdict.outcome == VIOLATED else blocked
            if verdict.reason not in into:
                into.append(verdict.reason)
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
        #: `not_checked` AND NOT `cannot_check`, AND THE REASON IS WHO OWNS THE NAME. ADR-0017
        #: R-16 gives `cannot_check` to the COMMAND, as the one sentence saying what it could not
        #: see; **R-16's own table is the list of which commands carry it that way, and that
        #: table is the citation rather than a number repeated here [K-35].** This sentence said
        #: *six* twice over — the count was copied out of ADR-0017's prose, which was itself two
        #: stale (J-18 had moved `verify` into that family and `chain` was never counted), so a
        #: reader who re-counted stopped trusting the paragraph. Measured from live output, eight
        #: shipped commands carry it: `log`, `lineage`, `impact`, `diff`, `resources`, `show`,
        #: `verify` and `chain`. A number restated beside a table is a copy that rots, and this
        #: docstring has now been wrong about this one twice. A COUNT under the same name one
        #: level down
        #: would put a string and an int behind one key in a single document — J-36's defect, which
        #: is resolved by asking which structure owns the word rather than by renaming whichever is
        #: more convenient. The gate's cross-command guard cannot see nested keys, so this is a
        #: finding the rule produced rather than one a check caught.
        "not_checked": tally[CANNOT_CHECK],
        #: EMPTY over a clean pass, and a LIST because several runs can fail differently and a
        #: reader repairs each one separately. **VIOLATIONS ONLY** since K-10: `len(reasons)` is
        #: now a number a consumer can compare with `violated`.
        "reasons": reasons,
        #: WHY A RUN COULD NOT BE ANSWERED FOR, which is a different finding from a breach and is
        #: now a different field. R-3 is that the two are different answers; this is that applied
        #: to the sentences as well as to the verdict.
        "blocked": blocked,
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
    "run does; and — the ordinary route, not the defensive one — a run configured with "
    "`warn_unregistered_reads=False`, which is documented and recommended for a step that "
    "deliberately reads files it does not want recorded: the watch is never attached, so the "
    "record is silent exactly as a clean run's is, and the setting is PROJECT-level, so one "
    "step's exemption silences it for every run that shares the project",
)
def _no_unregistered_reads(record: typing.Mapping[str, typing.Any], context: Context) -> Verdict:
    """R-6'S OWN EXAMPLE: the field that qualifies the answer is consulted, not just the answer.

    `unregistered_reads` is ABSENT from a clean run rather than empty, so absence is the ordinary
    good case — and that is why the `observation` block's presence is what makes this evaluable.
    A record without one came from a version that did not watch, and reading its silence as
    *nothing was missed* would be the vacuous green over the whole of history.

    AND THE `observation` BLOCK'S PRESENCE IS NOT ENOUGH, WHICH IS K-18 AND IS NOT FIXED HERE.
    `warn_unregistered_reads=False` skips `attach()` altogether and the run still records a full
    `observation` block, so this rule answers MET for a run that performed real unregistered reads
    — reproduced, with a documented and package-recommended setting, which is the most ordinary
    route there is to the vacuous green this ADR exists to refuse. **The record carries no field
    saying whether the watch ran**, so R-10's remedy applies and it is a record change with its own
    ADR, not an inference here.

    **DELIBERATELY NOT FIXED BEFORE 0.8.0, and the reason is measured rather than preferred.** The
    field cannot be back-filled, so a rule requiring it turns every clean pre-0.8.0 history from
    MET to CANNOT_CHECK for ever — worse than the defect — and a check placed before the violation
    arm turns a real VIOLATED finding into CANNOT_CHECK on all seven corpus histories. What landed
    now is the honest half: the limit is named in `blind`, where R-6 requires it, and ADR-0018's
    paragraph no longer presents the watch's `except` arm as the only way to reach this state.

    A VIOLATION OUTRANKS THE TRUNCATION, and the order is the fact. When the watch hit its cap AND
    still caught something, at least one read was certainly missed — knowing part of a list is
    knowing a violation. The reverse, an empty list after the cap bit, is the C-06 defect and is
    never a pass.
    """
    del context  # this rule reads the record only
    if record.get("observation") is None:
        return Verdict(CANNOT_CHECK, "the record carries no observation block, so nothing watched")
    #: `or []` IS REDUNDANT HERE AND IT STAYS, WITH THE REASON [K-07]. `if missed:` guards the
    #: only use and `len()` is never reached with `None`, so deleting it changes nothing and a
    #: mutation that deletes it survives the whole suite. That makes it an EQUIVALENT mutant, and
    #: an equivalent mutant is a finding about the design rather than about the test — so the
    #: design is stated instead of the code being changed: this is the HOUSE IDIOM for this field
    #: and it is load-bearing at two of its three sites. `__main__.py` does
    #: `len(record.get("unregistered_reads") or [])` and `report.py` does `tuple(... or [])`, and
    #: `len(None)` and `tuple(None)` both raise. Deleting it would make this the one reader of
    #: three that spells the field differently, which is how two readers of one field come to
    #: disagree about what its absence means.
    #:
    #: AND `FEATURE_WORKFLOW.md`'s EQUIVALENT-MUTANT RULE DOES NOT REACH IT. That rule is about
    #: two DERIVATIONS of one quantity — `PurePosixPath(n).parts` against `n.split("/")`, where
    #: the pair can silently disagree — not about a defensive default whose other arm is
    #: unreachable. Without that distinction every `or []` and `or {}` in the package is a future
    #: audit row.
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
    reads=("packages", "observation.packages_recorded", "environment_snapshot"),
    blind="a record with no `observation` block, which cannot say whether packages were recorded "
    "at all — `packages: {}` alone is indistinguishable from nobody having asked, which is the "
    "whole reason ADR-0010 added the field this rule reads; and a snapshot whose write FAILED, "
    "which the record marks `snapshot` all the same",
)
def _environment_captured(record: typing.Mapping[str, typing.Any], context: Context) -> Verdict:
    """`packages: {}` CANNOT BE TOLD FROM *NOBODY ASKED*, which ADR-0010 states in those words.

    So the verdict comes from `observation.packages_recorded` — a closed vocabulary of `snapshot`,
    `tracked` and `none` — rather than from the dict's emptiness. `none` is a VIOLATION and not an
    inability: the record positively says nothing was captured, which is an answer.

    AND A `snapshot` MARK IS NOT A SNAPSHOT [K-19]. `_observed_packages` returns `"snapshot"` on
    the mere PRESENCE of the key, and a capture that raised stores `{"error": str(exc)}` under it
    — so a run that printed *WARNING: could not write environment snapshot* and recorded no
    package at all answered MET, over `packages: {}`, which this rule's own `blind` text says is
    indistinguishable from nobody having asked. An unwritable snapshot directory in CI turned the
    environment control into a no-op for every run after it. Reproduced live with `chmod 500`.

    THE RECORD'S OWN `environment_snapshot` IS WHAT ANSWERS IT, AND NO FILE IS OPENED. The row as
    filed sent an applier to `environment["snapshot"]`, which is `None` in every HISTORY record —
    `run.py` flattens the block to a top-level `environment_snapshot` and `environment.snapshot`
    exists only in the sidecar, which `gate` never reads. **An applier following the row would
    have read `None` on every record**, which is a guard that is uninformed looking exactly like
    one that is satisfied. The flattened field carries the snapshot's digest, its path AND its
    package count, so the count is in hand and R-9 need not be bent at all.

    THE WIDER CLAIM THIS ROW CARRIED IS WITHDRAWN, refuted twice independently: *every released
    wheel answers MET over `packages: {}`* is true and is not a defect, because those records
    carry `n_packages: 2` beside the digest. They did record what they ran with. MET is the
    correct answer for all seven.
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
    #: ONLY WHERE THE MARK IS THE WHOLE OF THE EVIDENCE. A `tracked` record carries the packages
    #: themselves, and a `snapshot` record that ALSO tracked some is not relying on the snapshot
    #: to say what it ran with — so neither needs the snapshot consulted, and widening this to
    #: every record would make the rule ask for a field older writers never wrote.
    if recorded == "snapshot" and not record.get("packages"):
        #: `or {}` IS THE HOUSE IDIOM AND IT IS LOAD-BEARING HERE: a record marking a snapshot and
        #: carrying none reaches the count test below and answers the same way, rather than adding
        #: an arm no writer of this package can produce — the mark comes from the key's presence.
        snapshot = record.get("environment_snapshot") or {}
        if snapshot.get("error"):
            return Verdict(
                CANNOT_CHECK,
                f"the environment snapshot could not be written ({snapshot['error']}), so no "
                "package was recorded",
            )
        if not snapshot.get("n_packages"):
            return Verdict(
                CANNOT_CHECK,
                "the record marks a snapshot and names no package count in it, so `packages: {}` "
                "cannot be told from nobody having asked",
            )
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
    #: THE TYPE AND THE MESSAGE, NEVER THE WHOLE BLOCK. `failure` is a mapping of `type`,
    #: `message` and `traceback`, and interpolating it whole put an escaped multi-line traceback
    #: inside one line of a report a laboratory files — found by reading the page rather than by a
    #: test, which is the argument for rendering a command once by hand before believing it.
    #: THE TRACEBACK IS STILL IN THE RECORD and `show` is where a reader goes for it; a verdict's
    #: reason is one sentence or it stops being one.
    if isinstance(failure, dict):
        said = ": ".join(str(failure[key]) for key in ("type", "message") if failure.get(key))
    else:
        #: A STRING IS ACCEPTED TOO — AND THE PREMISE THIS ARM WAS WRITTEN ON IS FALSE [K-02].
        #: It said *a record written by a version before the block existed carries one*. No such
        #: version existed: `git show v0.1.0|v0.4.0|v0.7.0:runprov/run.py` shows every released
        #: version writing `failure` as a `{type, message, traceback}` MAPPING. So the string
        #: shape is not a backward-compatibility arm, and a fixture pinning one would be a
        #: fixture asserting a state this package's writer never emits — the defect K-20 was
        #: filed for.
        #:
        #: THE EMPTY-`said` BRANCH BELOW IS UNREACHABLE FOR THE SAME REASON [K-03]. `run.py` sets
        #: `status = "failed"` and that mapping in ONE block, and `failure["type"]` is
        #: `exc_type.__name__`, which is never empty — so a record this package wrote can never
        #: produce the dangling colon the conditional avoids.
        #:
        #: **BOTH ARMS STAY, AS TOLERANCE FOR A RECORD THIS PACKAGE DID NOT WRITE:** a
        #: hand-edited history, a line from another tool, a record repaired by a person reading
        #: it. A rule that met one of those and reported *the run finished with status 'failed'*
        #: with no reason, or with a sentence trailing off after a colon, would be a finding a
        #: reader cannot act on. What they are not is history, and the arm every real record DOES
        #: carry is asserted by name in
        #: `test_the_rules_agree_with_records_this_package_really_wrote`.
        said = str(failure) if failure else ""
    return Verdict(
        VIOLATED, f"the run finished with status {status!r}" + (f": {said}" if said else "")
    )


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


def _input_still_matches(
    entry: typing.Mapping[str, typing.Any],
    context: Context,
    base: pathlib.Path | None = None,
) -> Verdict:
    """One declared input, re-read from disk. The three answers, for that one file.

    `base` IS THE RUN'S OWN RECORDED `cwd` AND NEVER THE GATE PROCESS'S [K-17]. `run.input(p)`
    records `hashing._posix(p)` verbatim, so a relative registration is recorded relative — which
    is what this project's own corpus generator writes, and therefore what every released wheel's
    history carries. Resolved against wherever the gate happens to be standing, the same record
    answers `2 met, 1 not checked` from the tree it describes and `0 met, 0 violated, 3 not
    checked` from one directory up with nothing on disk changed, and in a tree holding same-named
    files it answers VIOLATED about three files it never opened.

    THE IDIOM IS `hashing.moved_since`'s, DELIBERATELY AND NOT BY COINCIDENCE. That function
    anchors the same recorded spelling on the same recorded `cwd` and reads an absent `kind` as a
    file, and its docstring records what re-deriving the answer cost the first time. A second
    expression for *where is this file* is how the two come to disagree, which is I-02's lesson
    and K-17's.
    """
    raw = entry.get("path")
    if not raw:
        return Verdict(CANNOT_CHECK, "an input entry carries no path")
    #: A DECLARED DIRECTORY IS NOT RE-WALKED HERE, and the limit is named rather than approximated.
    #: Reproducing `sha256_tree` means reproducing the ordering key A-14 and C-03 were both about,
    #: and then deciding whether a record's `sha256_tree_casefolded` or its `sha256_tree` is
    #: authoritative for a tree written by an older version on Windows. `verify` makes that
    #: decision and is its only reader; a second implementation of it inside a policy rule is how
    #: the two would come to disagree.
    #:
    #: THE TEST IS `moved_since`'s AND THE OLD ONE WAS DEAD [K-20]. It asked `kind == "tree"` or a
    #: `sha256_tree` key; the writer folds a directory's tree digest into `sha256` and spells the
    #: kind `directory`, so no record this package has ever written reached this arm — measured
    #: over all seven corpus versions, where `data/refs` fell through to the OSError arm and told
    #: the reader *could not be read: Is a directory* instead. Exit 2 for a declared directory is
    #: intended (Taylor, 2026-10-03) and R-7's own column now says so; what was wrong was the
    #: sentence, which sent a reader to a permissions problem rather than to `runprov verify`.
    if entry.get("kind") not in ("file", None):
        return Verdict(
            CANNOT_CHECK,
            f"{raw} was declared as a directory, which `runprov verify` re-walks and this rule "
            "does not",
        )
    comparable = _comparable_on(entry)
    if comparable is None:
        return Verdict(CANNOT_CHECK, f"{raw} carries no digest this rule can take again")
    key, was, how = comparable
    spelled = pathlib.Path(raw)
    target = spelled if spelled.is_absolute() or base is None else base / spelled
    #: KEYED ON THE RESOLVED PATH, which is the half of this fix a reading would miss. Two runs
    #: whose `cwd` differ and whose recorded spelling is the same are two different files, and a
    #: cache keyed on the spelling hands the second one the first one's digest.
    cached = (target.as_posix(), key)
    seen = context.digests
    if seen is not None and cached in seen:
        now = seen[cached]
    else:
        try:
            #: THE ADR'S OWN CASE, AND IT IS AN INABILITY RATHER THAN A VIOLATION. R-9's
            #: clarification says so in those words: a declared input that is gone is a
            #: `CANNOT_CHECK` naming the path. The alternative accuses a run of changing a file
            #: that may simply have been archived, in a record nobody can amend.
            if not target.exists():
                return Verdict(
                    CANNOT_CHECK,
                    f"{raw} is no longer on disk, so what it holds now cannot be compared with "
                    "what the run recorded",
                )
            now = how(target)
        except OSError as exc:
            return Verdict(CANNOT_CHECK, f"{raw} could not be read: {exc.strerror or exc}")
        if seen is not None:
            seen[cached] = now
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
    "one that wrote it, where the recorded path names nothing even though the file itself still "
    "exists somewhere; and a declared DIRECTORY, which `runprov verify` re-walks and this rule "
    "does not, so a project registering one gets exit 2 from this rule by design",
)
def _inputs_verify(record: typing.Mapping[str, typing.Any], context: Context) -> Verdict:
    """THE ONE RULE THAT LEAVES THE RECORD, which is why R-9 had to be clarified before it.

    Taylor's ruling, 2026-10-02: *the source* means SOURCE CODE — the thing `check` parses — not
    the filesystem. Data files are not source, and *does the evidence still match the files* is
    the question an accredited laboratory asks first. So this rule re-hashes, and the cost is
    stated rather than hidden: with it in a policy, `gate` scales with the DATA and not with the
    history.

    IT IS ALSO THE RULE THAT FORCED THE `judge` CONTRACT TO TAKE A SECOND ARGUMENT, and the
    reason is the cache and not a root.

    **THE PREMISE THIS WAS BUILT ON WAS FALSE AND K-17 IS THE CORRECTION.** It read: *the
    recorded `path` is ABSOLUTE, so there is nothing to resolve it against* — and `run.input(p)`
    records `hashing._posix(p)` verbatim, so a relative registration is recorded relative. This
    project's own corpus generator calls `run.input("data/m.tsv")`, so every released wheel's
    history carries relative paths and the dropped anchor was resolving them against the GATE
    PROCESS'S working directory. One run that happened to pass an absolute path was measured and
    generalised from.

    THE ANCHOR IS THE RECORD'S OWN `cwd` AND NOT A `root` FLAG, which is the part of the original
    reasoning that survives: a root would be a second answer to *where is this file* competing
    with the one the history already gives, while `cwd` IS the answer the history gives and is
    present in every corpus version. No record change was needed. What a root WOULD buy —
    re-checking a history on a machine that did not write it — is still not in ADR-0018, and the
    rule's `blind` text names that limit instead.

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
    #: THE RUN'S OWN `cwd`, read off the record exactly as `hashing.moved_since` takes it. A
    #: record written before the field existed, or by a writer that omits it, leaves this `None`
    #: and the recorded spelling is then taken as it stands — which is the old behaviour for
    #: exactly the records that cannot do better, and not for the ones that can.
    recorded_cwd = record.get("cwd")
    base = pathlib.Path(recorded_cwd) if recorded_cwd else None
    changed: list[str] = []
    unknown: list[str] = []
    for entry in declared:
        verdict = _input_still_matches(entry, context, base)
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


#: THE ANSWER'S SHAPE, VERSIONED. ADR-0017 R-5: a consumer branches on this and not on what it
#: finds, and the command is `gate` even though the module is `policy` — the schema names the
#: question that was asked, not the file that answered it.
SCHEMA = "runprov.gate.v1"


class Assessment(typing.NamedTuple):
    """One policy over one history: every rule's verdict, and what the gate could not see.

    THE FACTS ARE FIELDS AND THE CONCLUSIONS ARE PROPERTIES, which is `check.Report`'s shape and
    for its reason: `outcome` and `cannot_check` are computed once, here, and read by both
    renderings. H1-6 is the row where a second computation of one fact disagreed with the first.

    `found` IS A FIELD AND NOT AN INFERENCE FROM `runs == 0`. A history that is not there and a
    history that is there and empty are different findings — the first is a path to fix, the
    second is a project that has recorded nothing yet — and ADR-0017 R-8 is the rule that they
    must not be collapsed.
    """

    #: The policy file this was read from, and the history it was applied to. Named because the
    #: payload is evidence a laboratory files: *which controls, against which runs*.
    #:
    #: `policy_path` AND NOT `source`, AND THE WORD WAS ALREADY TAKEN [K-12]. ADR-0017 R-12's own
    #: table defines `source` for the twelve payloads as *which MECHANISM answered* — `resources`
    #: carries `source: "getrusage"`, and ADR-0013 R-5 is the row establishing that a cgroup peak
    #: and a `getrusage` peak are different quantities. Four shipped payloads use it that way;
    #: these two new ones used it for a FILE PATH. All the values are strings, so no type-based
    #: guard can ever see the collision — the cross-command guard's own docstring says that is the
    #: half it cannot see — and ADR-0017 R-9 forbids renaming a field once it is uploaded. So this
    #: rename had one window and this is it.
    #:
    #: THE SIBLING STAYS `history`, so the payload reads `policy_path` / `history` rather than
    #: `policy_path` / `history_path`. `history` is what every other command in the package calls
    #: the same file and R-9 governs it: renaming it here to match a word invented today would
    #: trade a frozen vocabulary for a local symmetry.
    policy_path: str
    history: str
    found: bool
    #: THE HISTORY IS THERE AND COULD NOT BE READ, which is a third state beside `found` and not
    #: either of its two [K-23]. Before this, an `OSError` from the read raised out of `_gate`:
    #: a raw traceback, zero bytes on stdout and **exit 1**, which under L-81 says *a rule was
    #: checked and your controls were violated*. Both signals were false — the invocation HAD a
    #: question, so R-15's silence wrongly said it did not. On this project it is not
    #: hypothetical: an EIO from a failing external drive is an `OSError` on the same path, so a
    #: dying disk reported a policy violation.
    #:
    #: ITS OWN FIELD AND NOT A REUSE OF `found` OR `unreadable`, measured: `found=False` prints
    #: *there is no run history at …* about a file that is right there and collapses the R-8
    #: distinction `found` exists to keep, and `unreadable` prints *N line(s) could not be read*
    #: about a file nothing ever opened a line of.
    read_error: str | None
    runs: int
    #: LINES THE HISTORY COULD NOT GIVE UP. A torn line is a run the gate did not examine, so it
    #: has to stop the gate saying MET — counted rather than skipped, which is what `_counted`
    #: exists for.
    unreadable: int
    #: RUNS THAT STARTED AND HAVE NO ENDING ON RECORD — `log`'s own fact under `log`'s own name
    #: [K-08]. A run entered and the process killed leaves `runprov.start.v1` with no record, and
    #: `_counted` drops every start line before any rule sees it. So `gate` said `1 run / MET /
    #: exit 0 / cannot_check null` while `log` said `unfinished: 1` over the same file, and
    #: `cannot_check: null` — documented as *looked and found nothing missing* — asserted the
    #: opposite of what happened.
    #:
    #: **IT IS NAMED AND IT NEVER CHANGES THE VERDICT.** Taylor's ruling, 2026-10-03, after a
    #: skeptic measured the cost of the arm this row originally asked for: the start line is
    #: append-only and `runprov prune` does not help, so reading this in `outcome` pins a project
    #: at exit 2 for ever after one power cut, in a record nobody can amend — and
    #: `_finished_ok`'s own docstring already argues against exactly that shape. It also fails a
    #: gate because another job is merely in progress. **The residual limit is stated rather than
    #: hidden: a consumer keying only on the exit code still greens over a lost run, and must
    #: read `unfinished` or `cannot_check` to see it.**
    unfinished: int
    rules: tuple[dict[str, typing.Any], ...]

    @property
    def outcome(self) -> str:
        """The worst of the rules', and never MET over a history that was not whole. R-2, R-3.

        THE ORDER IS VIOLATED, CANNOT_CHECK, MET, the same order `assess` uses one level down and
        for the same reason: a violation among runs that could not all be examined is still a
        violation, and an unexamined run among passes is not a pass.

        `read_error` IS HERE AND `unfinished` IS DELIBERATELY NOT [K-08, K-23], and the
        difference is permanence. An unreadable history is a fact about THIS invocation: fix the
        mode, replace the drive, and the next run answers. An unpaired start line is a fact about
        an APPEND-ONLY record — nothing can ever remove it, `runprov prune` clears markers and not
        history, so reading it here would fail a project's gate for ever over one power cut.
        Taylor's ruling, 2026-10-03: name it in `cannot_check`, never in the verdict.
        """
        if any(row["outcome"] == VIOLATED for row in self.rules):
            return VIOLATED
        if not self.found or self.read_error or self.unreadable or not self.rules:
            return CANNOT_CHECK
        if any(row["outcome"] == CANNOT_CHECK for row in self.rules):
            return CANNOT_CHECK
        return MET

    @property
    def cannot_check(self) -> str | None:
        """What the gate could not see, in one sentence, or `None`. ADR-0017 R-16.

        `None` MEANS LOOKED AND FOUND NOTHING MISSING — R-8's distinction, not *did not look*.

        IT IS INDEPENDENT OF `outcome`, deliberately. A history with one violation AND two
        unexaminable rules is a VIOLATED gate that still could not see everything, and a reader
        repairing the violation needs to know the rest was not cleared. Tying this to the outcome
        would hide the second half behind the first.
        """
        parts = []
        if not self.found:
            parts.append(f"there is no run history at {self.history}, so no run was examined")
        if self.read_error:
            parts.append(
                f"the history at {self.history} is there and could not be read "
                f"({self.read_error}), so no run was examined"
            )
        if self.unreadable:
            parts.append(
                f"{self.unreadable} line(s) of the history could not be read, so the runs they "
                "describe were not examined"
            )
        if self.unfinished:
            parts.append(
                f"{self.unfinished} run(s) started with no ending on record, so they were not "
                "examined — a run still going, or one killed before it could record"
            )
        #: BUILT FROM THE ROWS' OWN COUNTS AND NOT FROM THEIR AGGREGATE OUTCOME [K-09]. It used
        #: to list the rules whose aggregate was CANNOT_CHECK — and `assess` folds a rule to
        #: VIOLATED as soon as one run breaches it, so a rule with one breach and one unexamined
        #: run vanished from the one field a consumer is pointed at. Reproduced: one rule
        #: `outputs_pin_inputs`, run A with an output and no input, run B with no outputs — the
        #: payload carried `not_checked: 1` and `cannot_check: null` together. The docstring
        #: above already claimed to handle exactly this, which was true at the RULE level and
        #: false at the RUN level: a docstring that is not a contract.
        #:
        #: AND IT NAMES RUNS. The old sentence counted rules and said nothing about how many runs
        #: each one left unanswered, so a reader could not tell *this rule saw nothing* from *this
        #: rule saw all but one*. `asked_of_nothing` is carried alongside because a rule asked of
        #: no run has `not_checked == 0` and is the vacuous case R-4 exists for — reading the
        #: counts alone would have dropped it.
        blind = [row for row in self.rules if row["not_checked"] or row["asked_of_nothing"]]
        if blind:
            named = ", ".join(
                f"{row['rule']} (no run to check)"
                if row["asked_of_nothing"]
                else f"{row['rule']} ({row['not_checked']} of {row['evaluated']} run(s))"
                for row in blind
            )
            parts.append(f"not every run was checked against {len(blind)} rule(s): {named}")
        return "; ".join(parts) or None

    @property
    def exit_code(self) -> int:
        """0 met, 1 violated, 2 could not check. R-2, and L-81's vocabulary unchanged.

        DERIVED FROM `outcome` RATHER THAN RE-DECIDED. `report`'s I-10 is the row where a second
        expression folded five verdicts into two exit codes, and the repair was to derive the code
        from the verdict the command had already formed.
        """
        return {MET: 0, VIOLATED: 1, CANNOT_CHECK: 2}[self.outcome]


def assessment(
    policy: typing.Mapping[str, typing.Any],
    records: typing.Iterable[typing.Mapping[str, typing.Any]],
    *,
    policy_path: str,
    history: str,
    found: bool = True,
    unreadable: int = 0,
    unfinished: int = 0,
    read_error: str | None = None,
) -> Assessment:
    """One policy over one history. R-1, R-4, and R-11's `why` carried into the answer.

    ONE `Context` FOR EVERY RULE IN THE POLICY, which is the whole reason the cache is a parameter
    and not a global: a reference file declared by twenty runs and read by three rules is hashed
    ONCE per invocation. Per-rule caches would be three reads of it, and a global would be a
    digest held across invocations.

    THE `why` TRAVELS BESIDE THE VERDICT. R-11's substance is that a control's rationale is a
    required field rather than a comment — this is where that becomes true of the answer, so an
    assessor reads *why this control exists* next to *whether it was met*, in both renderings.
    """
    seen = list(records)
    context = Context(digests={})
    return Assessment(
        policy_path=policy_path,
        history=history,
        found=found,
        read_error=read_error,
        runs=len(seen),
        unreadable=unreadable,
        unfinished=unfinished,
        rules=tuple(
            {**assess(entry["rule"], seen, context), "why": entry["why"]}
            for entry in policy["rules"]
        ),
    )


def payload(result: Assessment) -> dict[str, typing.Any]:
    """The assessment as one object, for `--format json`. ADR-0017 R-1, R-5, R-10, R-12.

    DERIVED FROM `Assessment`'s OWN FIELDS, so a field added to the structure reaches the payload
    without anyone remembering to add it. The three computed properties are added explicitly,
    exactly as `check.payload` adds `ok` and `examined_nothing`: `_fields` does not reach a
    property, and they are READ here rather than recomputed.
    """
    return {
        "schema": SCHEMA,
        **result._asdict(),
        #: R-13: THE CONTROL AS A SEPARABLE OBJECT, and it is a PROJECTION of the rows above
        #: rather than a second parse of the file. Every field of the normalised policy was
        #: already in this payload — each rule row carries `rule` and `why`, which is exactly what
        #: `load()` returns — so what this adds is not data but a shape: the policy can be
        #: archived, diffed and filed without being reassembled from the verdicts carrying it.
        #:
        #: BUILT FROM `_RULE_KEYS`, the same constant the parser accepts, so the projection cannot
        #: come to name a field a policy may not carry. Re-reading the file here would be the
        #: defect: H1-6 is the row where one fact computed twice disagreed with itself.
        #:
        #: THIS IS THE CONTROL *AS APPLIED* AND NOT THE CONTROL [K-13]. `--emit-policy`'s
        #: `runprov.policy.v1` is the document a laboratory files: it names itself and carries
        #: the file it came from. This object is the same rules inside the ANSWER, so a filed
        #: result carries what it was judged against — and it deliberately carries no `schema`
        #: and no `policy_path`, because the payload around it already carries both and putting
        #: them here would be the duplication R-13 says it avoided. `{"rules": [...]}` is what a
        #: policy FILE may contain, not an unversioned invention: `_checked` refuses a `schema`
        #: key in one.
        "policy": {"rules": [{key: row[key] for key in _RULE_KEYS} for row in result.rules]},
        "outcome": result.outcome,
        "cannot_check": result.cannot_check,
        "exit_code": result.exit_code,
    }


#: A NORMALISED POLICY IS ITS OWN SHAPE and must not wear the gate's version: it carries no
#: verdict, no count and no history, so a consumer that branched on `runprov.gate.v1` and met one
#: would find every field it expected missing. R-14.
POLICY_SCHEMA = "runprov.policy.v1"


def policy_payload(
    policy: typing.Mapping[str, typing.Any], *, policy_path: str
) -> dict[str, typing.Any]:
    """A policy file, validated and normalised, as one object. R-14, ADR-0017 R-5.

    `policy_path` TRAVELS WITH IT for the same reason it travels with an assessment: this is
    evidence a laboratory files, and *which controls* is half of what makes it evidence.

    IT WAS `source` AND THAT WORD MEANS SOMETHING ELSE IN THIS PACKAGE [K-12]: ADR-0017 R-12
    gives it to *which mechanism answered*, which is what `resources.source` carries. See
    `Assessment.policy_path`.
    """
    return {"schema": POLICY_SCHEMA, "policy_path": policy_path, **policy}


def render_policy(policy: typing.Mapping[str, typing.Any], *, policy_path: str) -> list[str]:
    """The same policy as a page. R-14, and ADR-0017 R-1 again: one structure, two renderings.

    IT SAYS THE POLICY IS USABLE RATHER THAN LEAVING THAT TO BE INFERRED. Reaching this function
    means `load()` accepted the file, every rule in it is registered in this version, and every
    one carries a `why` — three facts a reader would otherwise have to deduce from the absence of
    an error, which is the inference this package refuses everywhere else.
    """
    lines = [f"POLICY {printable(policy_path)}", ""]
    for entry in policy["rules"]:
        #: ESCAPED, BECAUSE A `why` IS WHATEVER THE POLICY SAID [K-21]. A multi-line rationale —
        #: a paragraph, which is what a laboratory writes — forged whole lines on this page, and
        #: through `--emit-policy` it printed verdicts for a rule the policy does not contain over
        #: runs this command promises never to read.
        lines.append(f"  {printable(entry['rule']):<22}{printable(entry['why'])}")
    lines.append("")
    lines.append(
        f"{len(policy['rules'])} rule(s), every one registered in this version and carrying a "
        "`why`. No run was examined."
    )
    return lines


def render(result: Assessment) -> list[str]:
    """The same assessment as a page. ADR-0017 R-1: one builder, two renderings.

    EVERYTHING HERE IS READ OFF THE STRUCTURE AND NOTHING IS RECOMPUTED. J-04 is the row where a
    page and a payload named different routes to one verdict because each worked it out for itself;
    the repair was to make the page a rendering of the answer rather than a second answer.

    THE INABILITY IS PRINTED EVEN WHEN THE GATE FAILED, because `cannot_check` is independent of
    the outcome: a reader fixing the violation has to know the rest was not cleared either.
    """
    #: EVERY FIELD THAT CAME FROM A RECORD OR A POLICY IS ESCAPED ON THE WAY TO THE PAGE [K-21].
    #: A newline in one of them forges whole lines, including a line reading exactly
    #: `GATE: MET (exit 0)` inside a VIOLATED report — and a CI log scraped for `GATE:` reads the
    #: forged one first. Both routes were reproduced against a real invocation: a declared input
    #: path containing a newline (legal on POSIX, stored verbatim) and a policy's `why`. **And an
    #: innocent multi-line `why` garbles the page identically**, so this is not adversarial-only.
    #:
    #: WHAT IS NOT ESCAPED IS WHAT CAME FROM THIS SOURCE FILE: `outcome`, `rule`, `asks` and
    #: `blind` are the registry's own strings, written here and validated against it by the
    #: parser. Escaping them would say the registry is untrusted, which is a different and false
    #: claim. The JSON payload is untouched: `json.dumps` escapes already, and a payload carrying
    #: pre-escaped text would hand a consumer a string that is not the one in the record.
    lines = [
        f"POLICY {printable(result.policy_path)}",
        f"  history  {printable(result.history)}"
        + ("" if result.found else "  (NOT FOUND)")
        + f"  —  {result.runs} run(s) examined"
        + (f", {result.unreadable} line(s) unreadable" if result.unreadable else ""),
        "",
    ]
    for row in result.rules:
        lines.append(f"{row['outcome']:<13}{row['rule']} — {row['asks']}")
        lines.append(f"  why          {printable(row['why'])}")
        #: R-4 ON THE PAGE AND NOT ONLY IN THE PAYLOAD. *No violations* over a selection of zero
        #: runs is the vacuous green this requirement exists to refuse, and a reader who has to
        #: ask for JSON to see the count is a reader who will not ask.
        lines.append(
            f"  evaluated    {row['evaluated']} run(s): {row['met']} met, "
            f"{row['violated']} violated, {row['not_checked']} not checked"
            + ("  — ASKED OF NOTHING" if row["asked_of_nothing"] else "")
        )
        for reason in row["reasons"]:
            lines.append(f"  finding      {printable(reason)}")
        #: A DIFFERENT WORD, BECAUSE IT IS A DIFFERENT ANSWER [K-10]. These used to print under
        #: `finding` beside the breaches, so *the run recorded no outputs, so there is nothing to
        #: ask* was labelled an accusation. `cannot see` below is the rule's standing blind spot;
        #: this is what it could not answer about THESE runs.
        for reason in row["blocked"]:
            lines.append(f"  not checked  {printable(reason)}")
        lines.append(f"  cannot see   {row['blind']}")
        lines.append("")
    if result.cannot_check:
        lines.append(f"COULD NOT CHECK: {printable(result.cannot_check)}")
    lines.append(f"GATE: {result.outcome} (exit {result.exit_code})")
    return lines
