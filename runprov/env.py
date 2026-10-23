# Copyright (c) 2026 Assistance Publique – Hôpitaux de Paris (AP-HP),
#                    Hôpital Henri-Mondor, and Taylor Nicole Thompson
# SPDX-License-Identifier: BSD-3-Clause
# Licensed under the BSD 3-Clause License — see LICENSE.
"""Read a recorded environment, say what can be checked, and compare two. ADR-0026.

**NOT `environment.py`, WHICH CAPTURES.** That module writes the snapshot and the record; this one
reads them. The names are four characters apart and the split is the command: `runprov env` is
this file, and nothing in the package types `runprov environment`. Said here because an
unqualified name has cost this package before — four functions called `render`, one of which
produced *"a plausible environment snapshot whose body was a verify report"*.

**ONE IDENTITY, THREE CONSUMERS, AND EVERY LATER FEATURE IS A SOURCE** [ADR-0026 R-1, R-10].
`Identity` is what an environment is, together with **how each part of it was obtained**;
`compare` takes two of them. `--digest` prints one, `--can-check` makes one describe itself, and
`--compare` passes two. A published digest string, a merged group of histories and **the
environment a rebuild has just created** are three more sources, and none of them is a second
comparison — which matters because a rebuild verifies by asking this question, and two
implementations of *are these the same packages* would drift by whole cases rather than details.

**ONE ANSWER, TWO RENDERINGS** [ADR-0017]: `payload` builds and both renderers read it, so the
JSON is the same answer rather than a second one, and the provenance of every field travels into
both — a consumer that cannot tell a recorded digest from a recomputed one cannot tell what the
answer is evidence of.

**THE RECORDER'S OWN VERSION IS THE EXPECTED DIFFERENCE** [ADR-0026 R-6]. Measured across nine
released corpus versions: their package-set digests are nine distinct values and **exactly one**
once `runprov` is excluded. A reviewer who installs runprov to check a record differs by one
package every time, so the comparison names that case in words rather than leaving them to read a
correct rebuild as a failure.
"""

from __future__ import annotations

import dataclasses
import pathlib
import sys
import typing

from . import environment, pkgset
from ._report import printable_lines
from .diff import Dimension

__all__: list[str] = []

#: THE SHAPE'S NAME, CARRIED IN THE ANSWER [ADR-0017 R-5]. A consumer that cannot tell which
#: shape it is holding is being promised nothing, and the version is in the name so a later key
#: set is a new shape rather than a silent change to this one.
SCHEMA = "runprov.env.v1"

#: THE RESERVED OPERAND NAMING THIS ENVIRONMENT [ADR-0026 R-5]. A bare word rather than a flag,
#: because an operand must be passable as the VALUE of one — `--compare --here` is read by
#: argparse as a flag with no value, which is the kind of wart that becomes permanent. A run
#: actually called this makes the word mean two things, and the CLI refuses rather than choosing.
HERE = "here"

#: THE FAMILY A PUBLISHED IDENTITY BELONGS TO, derived from the algorithm's own name so the two
#: cannot disagree. An operand in this family is an identity ATTEMPT and is refused as one — a
#: `runprov.pkgset.v99:…` string read as a run name would answer *no run for that* and send a
#: reviewer looking for a run they never had, which is the inference [ADR-0026 R-5] forbids.
IDENTITY_FAMILY = pkgset.ALGORITHM.rsplit(".", 1)[0] + "."

#: HOW A FIELD WAS OBTAINED, WHICH IS NEVER THE SAME QUESTION AS WHAT IT IS [ADR-0010]. A digest
#: read from a record and one recomputed from a body on this machine are the same number and not
#: the same evidence: the first is what the run asserted, the second is what the file still says.
#: `absent` carries the reason, because *"could not look"* and *"there is nothing"* are two
#: answers everywhere else in this package and collapsing them here would be the one place they
#: are confused.
RECORDED = "recorded"
RECOMPUTED = "recomputed from the snapshot body"
OBSERVED = "observed in this environment"


@dataclasses.dataclass(frozen=True)
class Field:
    """One part of an identity: its value, how it was obtained, or why there is none."""

    value: str | None
    how: str | None = None
    absent: str | None = None

    def __post_init__(self) -> None:
        #: THE TWO STATES ARE EXCLUSIVE BY CONSTRUCTION. A field carrying both a value and a
        #: reason it is missing would let a reader believe either one, and the reader that
        #: believed the wrong one would have no way to find out.
        if (self.value is None) == (self.absent is None):
            raise ValueError("a field states a value with its provenance, or why there is none")

    def __str__(self) -> str:
        return self.value if self.value is not None else f"<{self.absent}>"


@dataclasses.dataclass(frozen=True)
class Identity:
    """What an environment is, and how each part of that was obtained.

    `packages` IS THE DETAIL LEVEL AND IT IS OFTEN ABSENT, which is the honest shape rather than
    an inconvenience [ADR-0024 R-11, corrected]. Naming *which* packages differ needs the snapshot
    body, and the feature's headline case — two records compared off-host — may have neither body
    on disk. So the digest is the floor, the parsed map is the detail, and a comparison says which
    of the two it had.
    """

    label: str
    package_set: Field
    interpreter: Field
    host_digest: Field
    package_count: int | None = None
    packages: dict[str, str] | None = None
    algorithms: tuple[str, ...] = ()

    def answerable(self) -> list[tuple[str, bool, str]]:
        """What can be checked from what is here: one row per subject, and never two states.

        THIS IS THE PREFLIGHT [ADR-0020 R-13, ADR-0024 R-23], and it is this object describing
        itself rather than a second walk of the record. A preflight that measured the subject
        separately could report a question answerable while the comparison refuses it — one
        sentence bound to two instruments, which this project has already paid for once.
        """
        rows = [
            ("package set", self.package_set.value is not None, str(self.package_set)),
            ("interpreter", self.interpreter.value is not None, str(self.interpreter)),
            ("host digest", self.host_digest.value is not None, str(self.host_digest)),
            (
                "named packages",
                self.packages is not None,
                f"{len(self.packages)} entries"
                if self.packages is not None
                else "<no readable snapshot body, so a difference can be counted but not named>",
            ),
        ]
        return rows


def _package_set_from_record(
    snapshot: typing.Mapping[str, typing.Any], body: str | None
) -> tuple[Field, tuple[str, ...]]:
    """The package-set digest of a recorded environment, recomputed from the body if need be.

    THREE ABSENCES, AND THEY CALL FOR THREE DIFFERENT ACTIONS [ADR-0024 R-20]. A record written
    before the field existed is repaired by re-recording; one whose canonicaliser refused the body
    is repaired by fixing the environment, and the writer's own reason says so; one carrying a
    digest only under a form this build does not implement is a version skew. Looked up **by
    algorithm name**, so a future form is never read as this one.

    AND A RECORD THAT DISAGREES WITH ITS OWN SIDECAR IS A REFUSAL, NOT A PREFERENCE. Where both
    the recorded digest and the body are in hand they must agree; if they do not, neither is
    evidence and saying which one to believe would be an invention.
    """
    offered = snapshot.get("package_sets")
    offered = offered if isinstance(offered, dict) else {}
    algorithms = tuple(sorted(offered))
    recorded = offered.get(pkgset.ALGORITHM)
    recomputed = None
    if body is not None:
        try:
            recomputed = pkgset.package_set_digest(body)
        except pkgset.PackageSetError as exc:
            return Field(None, absent=f"the snapshot body cannot be reduced: {exc}"), algorithms

    if isinstance(recorded, str) and recorded:
        if recomputed is not None and recomputed != recorded:
            return (
                Field(
                    None,
                    absent=(
                        f"the record claims {recorded[:16]} and its own snapshot body hashes to "
                        f"{recomputed[:16]}, so neither is evidence"
                    ),
                ),
                algorithms,
            )
        return Field(recorded, how=RECORDED), algorithms
    if recomputed is not None:
        return Field(recomputed, how=RECOMPUTED), algorithms
    why = snapshot.get("package_sets_unavailable")
    if isinstance(why, str) and why:
        return Field(None, absent=f"capture could not compute one: {why}"), algorithms
    if algorithms:
        return (
            Field(
                None,
                absent=f"recorded only as {list(algorithms)}, and this build computes "
                f"{pkgset.ALGORITHM}",
            ),
            algorithms,
        )
    return Field(None, absent="recorded before a package-set digest was written"), algorithms


def from_record(
    record: typing.Mapping[str, typing.Any], label: str, root: pathlib.Path | None = None
) -> Identity:
    """An identity from a run record, in either of the two shapes a record is written in.

    BOTH SPELLINGS, BECAUSE THE RECORD HAS TWO AND THEY ARE RENAMED RATHER THAN NESTED: the
    history projection writes a top-level `environment_snapshot` and the sidecar keeps
    `environment.snapshot`. A reader written against one is blind to the other, which `diff` found
    the hard way one command along.

    THE BODY IS READ WHEN IT IS THERE AND ITS ABSENCE IS NOT AN ERROR. A pruned sidecar lowers the
    detail level from *named packages* to *a digest*; it does not stop the comparison, and the
    output says which it had.
    """
    flat = record.get("environment_snapshot")
    nested = (record.get("environment") or {}).get("snapshot")
    snapshot = flat if isinstance(flat, dict) else nested if isinstance(nested, dict) else {}
    body, packages = None, None
    where = snapshot.get("path")
    if isinstance(where, str) and where:
        candidate = pathlib.Path(where)
        if root is not None and not candidate.is_absolute():
            candidate = root / candidate
        try:
            body = candidate.read_text(encoding="utf-8")
        except OSError:  # guards-ok: a pruned sidecar lowers the detail level, it is not an error
            body = None
    if body is not None:
        try:
            pip, conda = pkgset.parse(body)
            packages = {**pip, **{f"conda:{n}": v for n, (v, _) in conda.items()}}
        except pkgset.PackageSetError:  # guards-ok: the digest field below states the reason
            packages = None
    package_set, algorithms = _package_set_from_record(snapshot, body)
    python = snapshot.get("python") or (record.get("environment") or {}).get("python")
    host = snapshot.get("sha256")
    return Identity(
        label=label,
        package_set=package_set,
        interpreter=(
            Field(python, how=RECORDED)
            if isinstance(python, str) and python
            else Field(None, absent="the record names no interpreter version")
        ),
        host_digest=(
            Field(host, how=RECORDED)
            if isinstance(host, str) and host
            else Field(None, absent="the record names no snapshot digest")
        ),
        package_count=snapshot.get("n_packages"),
        packages=packages,
        algorithms=algorithms,
    )


def from_here() -> Identity:
    """An identity for the environment this process is running in. Nothing is written.

    THE SOURCE A REVIEWER IN A CONTAINER USES, and the reason it renders the body rather than
    reading a record: there is no record here. `_render_snapshot` is reused rather than
    reimplemented so that what is compared is what capture would have written.
    """
    bad: list[str] = []
    pip_map = environment.installed_packages(bad)
    conda = environment.conda_packages()
    body = environment._render_snapshot(pip_map, len(bad), conda)
    try:
        pip, conda_parsed = pkgset.parse(body)
        packages: dict[str, str] | None = {
            **pip,
            **{f"conda:{n}": v for n, (v, _) in conda_parsed.items()},
        }
        package_set = Field(pkgset.package_set_digest(body), how=OBSERVED)
    except pkgset.PackageSetError as exc:
        packages, package_set = (
            None,
            Field(None, absent=f"this environment cannot be reduced: {exc}"),
        )
    return Identity(
        label="this environment",
        package_set=package_set,
        interpreter=Field(sys.version.split()[0], how=OBSERVED),
        host_digest=Field(environment.digest(body), how=OBSERVED),
        package_count=len(pip_map),
        packages=packages,
        algorithms=(pkgset.ALGORITHM,),
    )


def from_identity_string(text: str) -> Identity:
    """An identity from a published `runprov.pkgset.v1:<hex>` string, and nothing else.

    THE JOURNAL REVIEWER'S SOURCE [ADR-0024 R-16]: a paper prints this line, and the reviewer has
    the line and a record. Everything other than the package set is absent, with that stated — an
    identity string asserts one thing, so the comparison answers one dimension and says plainly
    that the other two had nothing to compare.
    """
    algorithm, _, value = text.partition(":")
    if algorithm != pkgset.ALGORITHM or not value:
        raise ValueError(
            f"{text!r} is not a published identity this build can read: it must be "
            f"{pkgset.ALGORITHM}:<hex>, and the algorithm is part of the string precisely so a "
            f"digest computed under another form is never read as this one"
        )
    return Identity(
        label=text,
        package_set=Field(value, how="quoted"),
        interpreter=Field(None, absent="an identity string carries no interpreter version"),
        host_digest=Field(None, absent="an identity string carries no host digest"),
        algorithms=(algorithm,),
    )


#: WHY A DIFFERENCE IS EXPECTED, AND IT IS THE RECORDER [ADR-0024 R-17]. Measured over nine
#: released corpus versions: their package-set digests are nine distinct values and exactly ONE
#: once this name is excluded. Nothing is excluded by default — runprov is in that environment
#: and the digest says so — but the comparison names the case and names the flag, because a
#: verifier whose commonest correct outcome reads as a failure teaches its users to ignore it.
RECORDER = "runprov"


def _named_difference(
    a: typing.Mapping[str, str], b: typing.Mapping[str, str]
) -> tuple[list[str], set[str]]:
    """The three sets a reviewer can act on, and the canonical names that moved.

    THREE SETS AND NOT A DIGEST PAIR [ADR-0024 R-11]: *only in A*, *only in B*, and *a different
    version*. Two digests tell a reader that they failed; these tell them what to install.
    """
    lines, moved = [], set()
    for name in sorted(set(a) | set(b)):
        left, right = a.get(name), b.get(name)
        if left == right:
            continue
        moved.add(name)
        if left is None:
            lines.append(f"only in B   {name} {right}")
        elif right is None:
            lines.append(f"only in A   {name} {left}")
        else:
            lines.append(f"version     {name}  {left} -> {right}")
    return lines, moved


def _package_dimension(a: Identity, b: Identity, ignore: frozenset[str]) -> Dimension:
    """The one dimension the exit code is keyed on [ADR-0024 R-10]."""
    if a.package_set.value is None or b.package_set.value is None:
        why = "; ".join(
            f"{side.label}: {side.package_set.absent}"
            for side in (a, b)
            if side.package_set.value is None
        )
        return Dimension("package set", [], "nothing to compare", why)

    named = a.packages is not None and b.packages is not None
    left = {n: v for n, v in (a.packages or {}).items() if n not in ignore}
    right = {n: v for n, v in (b.packages or {}).items() if n not in ignore}
    if ignore and named:
        #: AN IGNORE RECOMPUTES THE DIGEST OVER WHAT IS LEFT rather than comparing the recorded
        #: digests and subtracting: a digest covers the whole set, so there is nothing to
        #: subtract from it, and comparing recorded digests under an `--ignore` would silently
        #: ignore nothing at all.
        same = left == right
    else:
        same = a.package_set.value == b.package_set.value

    if same:
        if ignore and named:
            #: THE DIGEST IS NOT QUOTED UNDER AN IGNORE, AND THAT IS NOT A DETAIL. A recorded
            #: digest covers the whole set, the ignored name with it, so printing it beside
            #: *"ignoring runprov"* would offer a value that does not stand for what was
            #: compared — a reader could quote it as the agreed identity of two environments
            #: that disagree. What agreed is the named set minus the exclusions, and that is
            #: what the line says.
            return Dimension(
                "package set",
                [],
                f"{len(left)} package(s) agree, ignoring {sorted(ignore)}; the recorded digests "
                f"cover the ignored name and so are not quoted here",
                None,
            )
        return Dimension("package set", [], f"{a.package_set.value[:16]} on both", None)

    if not named:
        #: THE DETAIL LEVEL IS DECLARED, NOT OMITTED [ADR-0024 R-11, R-17]. Without both bodies
        #: the packages cannot be named AND the recorder diagnosis cannot be made, and silence
        #: about an unavailable diagnosis reads exactly like the diagnosis coming back negative.
        missing = ", ".join(side.label for side in (a, b) if side.packages is None)
        return Dimension(
            "package set",
            [f"digest      {a.package_set.value[:16]} -> {b.package_set.value[:16]}"],
            f"digests only; no readable snapshot body for {missing}, so the packages cannot be "
            f"named and whether the only difference is {RECORDER} itself cannot be determined",
            None,
        )

    lines, moved = _named_difference(left, right)
    examined = f"{len(left)} vs {len(right)} package(s)"
    if moved == {RECORDER}:
        examined = (
            f"{examined} — THE ONLY DIFFERENCE IS {RECORDER} ITSELF, which is in the record "
            f"because {RECORDER} wrote it. Re-run with `--ignore {RECORDER}` to compare the "
            f"work's environment"
        )
    return Dimension("package set", lines, examined, None)


def _simple_dimension(
    name: str, a: Field, b: Field, labels: tuple[str, str], *, digest: bool
) -> Dimension:
    """A dimension over one recorded string, reported and never decisive.

    REPORTED INDEPENDENTLY, WHICH IS THE WHOLE POINT [ADR-0024 R-9]. A reviewer in a container
    expects the host to differ and must not be told the environment does not match because of it,
    so these two answer on their own lines and the exit code does not consult them.
    """
    if a.value is None or b.value is None:
        why = "; ".join(
            f"{label}: {field.absent}"
            for label, field in zip(labels, (a, b), strict=True)
            if field.value is None
        )
        return Dimension(name, [], "nothing to compare", why)
    #: A DIGEST IS TRUNCATED AND A VERSION IS NOT. Sixteen characters identify a digest and
    #: nothing else reads it; an interpreter version is a value a person compares by eye, and
    #: `3.13.0rc1+local` cut at sixteen would be a different version silently. The flag is
    #: per-dimension rather than a rule about lengths, because the two are different KINDS.

    def short(value: str) -> str:
        return value[:16] if digest else value

    if a.value == b.value:
        return Dimension(name, [], f"{short(a.value)} on both", None)
    return Dimension(name, [f"{short(a.value)} -> {short(b.value)}"], "differs", None)


@dataclasses.dataclass(frozen=True)
class Comparison:
    """Three independent verdicts, and the exit code of the one that answers the question."""

    a: Identity
    b: Identity
    dimensions: list[Dimension]
    ignored: frozenset[str]
    ignored_matched_nothing: frozenset[str]

    @property
    def packages(self) -> Dimension:
        return self.dimensions[0]

    def exit_code(self) -> int:
        """0 the package sets agree · 1 they differ · 2 the comparison could not be made.

        KEYED ON THE PACKAGE SET ALONE [ADR-0024 R-10]. An interpreter or a host difference is a
        fact about where the reviewer is standing, not about whether the environment was rebuilt
        correctly, and letting either move this number would make every container comparison fail.
        """
        if self.packages.blocked:
            return 2
        return 1 if self.packages.differences else 0


def compare(a: Identity, b: Identity, ignore: typing.Iterable[str] = ()) -> Comparison:
    """Compare two identities. ADR-0024 R-9, R-10, R-22.

    THE ONLY COMPARISON IN THE PACKAGE, AND DELIBERATELY SO. The rebuild specified for 0.11.0
    verifies what a
    manager built by asking this question of the environment it created; a second implementation
    there would be two halves of one rule, which this project has measured to drift by whole cases
    rather than by details. A new source returns an `Identity`; nothing here changes.
    """
    wanted = frozenset(pkgset.normalise(name) for name in ignore)
    present = set(a.packages or {}) | set(b.packages or {})
    return Comparison(
        a=a,
        b=b,
        dimensions=[
            _package_dimension(a, b, wanted),
            _simple_dimension(
                "interpreter", a.interpreter, b.interpreter, (a.label, b.label), digest=False
            ),
            _simple_dimension(
                "host digest", a.host_digest, b.host_digest, (a.label, b.label), digest=True
            ),
        ],
        ignored=wanted,
        #: AN IGNORE THAT MATCHED NOTHING IS REPORTED, NEVER SILENTLY SATISFIED. A misspelled name
        #: would otherwise read as a successful exclusion, which is the shape of every selector
        #: failure this project has recorded: it fails toward a clean-looking answer.
        ignored_matched_nothing=frozenset(wanted - present) if present else wanted,
    )


def payload(comparison: Comparison) -> dict[str, typing.Any]:
    """The comparison as data. ADR-0017: one builder, two renderings, one answer.

    THE PROVENANCE OF EVERY FIELD TRAVELS WITH IT. A consumer that cannot tell a recorded digest
    from one recomputed on the reading machine cannot tell what the answer is evidence of.
    """

    def side(identity: Identity) -> dict[str, typing.Any]:
        return {
            "label": identity.label,
            "package_set": {
                "value": identity.package_set.value,
                "how": identity.package_set.how,
                "absent": identity.package_set.absent,
                "algorithm": pkgset.ALGORITHM,
                "algorithms_recorded": list(identity.algorithms),
            },
            "interpreter": {"value": identity.interpreter.value, "how": identity.interpreter.how},
            "host_digest": {"value": identity.host_digest.value, "how": identity.host_digest.how},
            "package_count": identity.package_count,
            "packages_named": identity.packages is not None,
        }

    return {
        "schema": SCHEMA,
        #: THE ONE FIELD A CONSUMER TESTS FOR [ADR-0017 R-16]: null when the question was put,
        #: and the reason when it could not be. It is the package-set dimension's own `blocked`
        #: rather than a second sentence about the same fact, because two spellings of one
        #: inability is how the pair this project already measured came to disagree.
        "cannot_check": comparison.packages.blocked,
        "a": side(comparison.a),
        "b": side(comparison.b),
        "dimensions": [
            {
                "name": d.name,
                "differences": list(d.differences),
                "examined": d.examined,
                "blocked": d.blocked,
            }
            for d in comparison.dimensions
        ],
        "ignored": sorted(comparison.ignored),
        "ignored_matched_nothing": sorted(comparison.ignored_matched_nothing),
        "exit_code": comparison.exit_code(),
    }


def refused(reason: str, label: str = "?") -> dict[str, typing.Any]:
    """The payload for an invocation that could not put the question. ADR-0017 R-15.

    **AN ANSWER AND NOT A BAD INVOCATION.** It looked where it was told and there was nothing to
    read, so a consumer parsing `--format json` gets the same shape with `cannot_check` set,
    rather than empty output and a message on a stream it is not reading.

    BUILT THROUGH THE REAL BUILDER, which is the only way the two shapes cannot drift apart. Two
    identities that know nothing produce a blocked package dimension, so the key set here is the
    key set there **by construction** — and `lineage` is in this corpus precisely because a pair
    of hand-written shapes was allowed to diverge.
    """
    nothing = Field(None, absent=reason)
    empty = Identity(label, nothing, nothing, nothing)
    answer = payload(compare(empty, empty))
    answer["cannot_check"] = reason
    return answer


def render(comparison: Comparison) -> list[str]:
    """The comparison as lines. Three verdicts, each on its own, and the reason beneath it."""
    out = [f"A  {comparison.a.label}", f"B  {comparison.b.label}", ""]
    for dimension in comparison.dimensions:
        verdict = (
            "CANNOT COMPARE"
            if dimension.blocked
            else ("DIFFERS" if dimension.differences else "SAME")
        )
        out.append(f"{dimension.name:<12} {verdict}   ({dimension.examined})")
        out += [f"    {line}" for line in dimension.differences]
        if dimension.blocked:
            out.append(f"    {dimension.blocked}")
    if comparison.ignored:
        out += ["", f"ignored: {sorted(comparison.ignored)}"]
    if comparison.ignored_matched_nothing:
        out.append(
            f"    WARNING: {sorted(comparison.ignored_matched_nothing)} matched no package on "
            f"either side, so nothing was excluded for it"
        )
    #: EVERY PAGE THIS PACKAGE PRINTS GOES THROUGH ONE TRANSFORM [ADR-0018 R-8, Audit M escape-5].
    #: A package name, a script name and a label all reach these lines from a RECORD, and a record
    #: is a file that travels: a name carrying an escape sequence would otherwise be handed to the
    #: terminal by the one command a reviewer runs on a stranger's record.
    return printable_lines(out)


def render_answerable(identity: Identity) -> list[str]:
    """What can be checked from what is here. ADR-0020 R-13, ADR-0024 R-23."""
    out = [f"{identity.label}", ""]
    for subject, present, detail in identity.answerable():
        out.append(f"{subject:<16} {'yes' if present else 'NO ':<4} {detail}")
    return printable_lines(out)
