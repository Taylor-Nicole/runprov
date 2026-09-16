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

#: The dimensions, in the order a reader wants them: what went in, what the code was, what it
#: was asked to do, what happened inside, what it cost. Ordered here rather than sorted, so the
#: output reads like an explanation rather than an alphabet.
DIMENSIONS = ("inputs", "outputs", "code", "parameters", "packages", "steps", "resources")

CHANGED, UNCHANGED, INCOMPARABLE = "changed", "unchanged", "not comparable"


class Dimension(typing.NamedTuple):
    """One dimension's answer: what moved, what was examined, and what limits the conclusion."""

    name: str
    differences: list[str]
    examined: str
    blocked: str | None  # why this dimension cannot support "unchanged", if it cannot

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
    for record, side in ((a, "A"), (b, "B")):
        if record.get("unregistered_reads"):
            n = len(record["unregistered_reads"])
            blocked = f"{side} recorded {n} read(s) that bypassed registration"
        elif record.get("pin_partial"):
            blocked = f"{side}'s pin states it may understate the run"
    return Dimension(key, _compare_maps(left, right, key), f"{len(left)} vs {len(right)}", blocked)


def _code(a: typing.Mapping[str, typing.Any], b: typing.Mapping[str, typing.Any]) -> Dimension:
    """The project's commit — NOT `tool`, which is about runprov's own code.

    A DIRTY TREE IS WHAT BREAKS THIS, not anything about the package: the commit is recorded,
    and the files that ran differ from it, so a commit-to-commit line would name a change that
    is not the change.
    """
    left, right = a.get("git_commit"), b.get("git_commit")
    blocked = None
    for record, side in ((a, "A"), (b, "B")):
        if record.get("git_status_captured") is False:
            blocked = f"{side}'s dirty state is unknown — git status did not run"
        elif record.get("git_code_dirty"):
            blocked = f"{side} ran from a dirty tree, so its commit does not name the code"
    differences = [f"commit  {left or '?'} -> {right or '?'}"] if left != right else []
    return Dimension("code", differences, f"{left or 'none'} / {right or 'none'}", blocked)


def _parameters(
    a: typing.Mapping[str, typing.Any], b: typing.Mapping[str, typing.Any]
) -> Dimension:
    left, right = a.get("parameters") or {}, b.get("parameters") or {}
    lines = []
    for key in sorted(set(left) | set(right)):
        if left.get(key) != right.get(key):
            lines.append(f"{key}  {left.get(key, '<absent>')!r} -> {right.get(key, '<absent>')!r}")
    return Dimension("parameters", lines, f"{len(left)} vs {len(right)}", None)


def _packages(a: typing.Mapping[str, typing.Any], b: typing.Mapping[str, typing.Any]) -> Dimension:
    """`packages_recorded` decides this. `{}` on one side and 47 on the other is a change of
    CONFIGURATION, not of environment, and reporting it as the second is the defect."""
    how_a, how_b = _obs(a, "packages_recorded"), _obs(b, "packages_recorded")
    blocked = None
    if how_a != how_b:
        blocked = f"A recorded packages as {how_a!r}, B as {how_b!r}"
    left = (a.get("environment") or {}).get("packages") or {}
    right = (b.get("environment") or {}).get("packages") or {}
    lines = []
    for name in sorted(set(left) | set(right)):
        if left.get(name) != right.get(name):
            lines.append(f"{name}  {left.get(name, '<absent>')} -> {right.get(name, '<absent>')}")
    return Dimension("packages", lines, f"{len(left)} vs {len(right)}", blocked)


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
    left = {s.get("step"): s for s in (a.get("steps") or []) if isinstance(s, dict)}
    right = {s.get("step"): s for s in (b.get("steps") or []) if isinstance(s, dict)}
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
    return Dimension("steps", lines, f"{len(left)} vs {len(right)} declared", blocked)


def _resources(a: typing.Mapping[str, typing.Any], b: typing.Mapping[str, typing.Any]) -> Dimension:
    """ADR-0013: a cgroup peak and a `getrusage` peak are different quantities.

    "312 MiB -> 8 GiB" across a source change is a laptop moving to Slurm and starting to
    measure what the scheduler enforces — not a run that needed twenty-five times more.
    """
    left, right = a.get("resources") or {}, b.get("resources") or {}
    blocked = None
    if not left or not right:
        blocked = "one of the two runs predates the resources block"
    elif left.get("source") != right.get("source"):
        blocked = (
            f"A measured by {left.get('source')!r}, B by {right.get('source')!r} — "
            "different quantities"
        )
    lines = []
    for key in ("max_rss_bytes", "cpu_seconds", "wall_seconds"):
        one, two = left.get(key), right.get(key)
        if one is not None and two is not None and one != two:
            lines.append(f"{key}  {one} -> {two}")
    return Dimension(
        "resources", lines, f"{left.get('source', '—')} / {right.get('source', '—')}", blocked
    )


def compare(
    a: typing.Mapping[str, typing.Any], b: typing.Mapping[str, typing.Any]
) -> list[Dimension]:
    """Every dimension, each with its own precondition. Pure, so the table is testable."""
    dims = [
        _files(a, b, "inputs"),
        _files(a, b, "outputs"),
        _code(a, b),
        _parameters(a, b),
        _packages(a, b),
        _steps(a, b),
        _resources(a, b),
    ]
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


def render(
    a: typing.Mapping[str, typing.Any], b: typing.Mapping[str, typing.Any], dims: list[Dimension]
) -> list[str]:
    """The three states, spelled out. `unchanged` always says what it examined."""
    out = [
        f"A  {a.get('script', '?')}  {a.get('run_id', '?')}  {a.get('started_utc', '?')}",
        f"B  {b.get('script', '?')}  {b.get('run_id', '?')}  {b.get('started_utc', '?')}",
        "",
    ]
    for d in dims:
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
