# Copyright (c) 2026 Assistance Publique – Hôpitaux de Paris (AP-HP),
#                    Hôpital Henri-Mondor, and Taylor Nicole Thompson
# SPDX-License-Identifier: BSD-3-Clause
# Licensed under the BSD 3-Clause License — see LICENSE.
"""The canonical package set of an environment, and its digest. ADR-0025.

**THIS MODULE IMPORTS NOTHING BUT THE STANDARD LIBRARY, AND THAT IS A REQUIREMENT RATHER THAN A
HABIT** [ADR-0025 R-6]. A reviewer, a journal referee or an external auditor verifies an
environment somewhere other than where it was recorded — in a container, on a cluster, on a
laptop years later — and the thing they need to run must be copyable out of an installed runprov
or an unpacked sdist and runnable on its own. So this file is the ONLY implementation of the
canonical form, the rest of the package imports it, and a guard asserts its imports are stdlib.

**WHY A SECOND DIGEST EXISTS AT ALL.** `environment.write_snapshot` digests the whole rendered
body, and that body carries `# platform : …`. Measured: two identical package sets on
x86_64/glibc2.35 and aarch64/glibc2.39 digest differently, with the platform header as the only
differing line. So the existing digest answers *has this environment changed on this host* and
**cannot** answer *are these the same packages* — and `diff.compare` already knows it, reporting
a differing snapshot digest as `blocked` rather than `changed` for exactly this reason. This
module answers the second question, and only the second question. **Where that digest is kept
in the record, and where it is compared, is a separate and not-yet-built decision**, deliberately
unnamed here: a module's docstring citing a decision is a claim that the decision is implemented.

**NAMED `package_set_digest` AND NOT `digest`.** `environment.digest` already exists and means
the other thing, and this package has paid for an unqualified `render` before: four functions of
that name, one of which produced "a plausible environment snapshot whose body was a verify
report". One word, two meanings, no exception raised.
"""

from __future__ import annotations

import hashlib
import re
import sys
import typing

#: THE ALGORITHM NAME, WHICH IS ALSO THE KEY THE DIGEST IS FILED UNDER IN THE RECORD
#: [ADR-0025 R-4]. It is a frozen public contract once released, like `runprov.gate.v1`
#: and `runprov.policy.v1`: a change to a single byte of what `canonical_text` produces is
#: `runprov.pkgset.v2` under its own key, never a redefinition of this one. A digest quoted in a
#: paper must still be checkable when the algorithm has moved on, and it can only be if the paper
#: and the record both say which algorithm they mean.
ALGORITHM = "runprov.pkgset.v1"

#: `__all__` RATIFIES THE PACKAGE PROMISE AND NEVER MAKES ONE [ADR-0003]. The canonicaliser's
#: functions are reachable as `runprov.pkgset.<name>` and are INTERNAL by that decision's own
#: definition — *"cross-module use inside `runprov/` is internal"* — so none of them is listed
#: here. Promoting `package_set_digest` to `runprov.__all__` would be a package-surface decision
#: taken in `__init__.py`, not one this module may take by writing a longer list.
#:
#: `main` IS LISTED BECAUSE IT IS THE AUDITOR'S ENTRY POINT and the one name whose audience is
#: outside the package: the copied-out file is run, not imported.
__all__ = ["main"]

#: The separator `environment._render_snapshot` writes between pip and conda entries. The split
#: is taken from the BODY and never from this host's `conda-meta` [ADR-0020 R-7]: a
#: classification that needs the original machine cannot run where a verification is needed.
CONDA_SEPARATOR = "# --- conda-meta (conda / mamba / micromamba / pixi) ---"

#: `name==version`, the pip half of a snapshot body.
_PIP_ENTRY = re.compile(r"^([A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?)==(.+)$")

#: `name=version=build`, conda's own `--export` spelling, with the build optional.
_CONDA_ENTRY = re.compile(r"^([^#\s=]+)=([^=]+)(?:=(.*))?$")

#: PEP 503: runs of `-`, `_` and `.` collapse to one `-`, then the whole name lowercases.
_SEPARATORS = re.compile(r"[-_.]+")


class PackageSetError(ValueError):
    """A snapshot body this module refuses to reduce to a digest.

    EVERY RAISE SITE IS A REFUSAL AND NOT A FALLBACK, which is the whole design of this module:
    a canonicaliser that recovers from input it does not understand produces a digest that is
    confidently wrong, and nothing downstream can tell. Its callers map this to exit 2 — *could
    not look* — never to exit 1 [ADR-0007, ADR-0025 R-2].
    """


def normalise(name: str) -> str:
    """The PEP 503 canonical spelling of a distribution name."""
    return _SEPARATORS.sub("-", name).lower()


def parse(body: str) -> tuple[dict[str, str], dict[str, tuple[str, str]]]:
    """The pip and conda entries of a snapshot body, keyed by canonical name.

    THE CLASSIFICATION IS TOTAL [ADR-0025 R-2]. Every non-blank line is exactly one of: the conda
    separator; a conda entry, after it; a pip entry, before it; or an annotation, which is any
    other line beginning with `#`. **A line matching none of those raises**, because a
    canonicaliser that silently drops a line it does not understand reports a digest for a
    smaller environment than the one it was given.

    AND A COLLISION RAISES RATHER THAN MERGING [ADR-0025 R-3]. Normalisation maps
    `zope.interface` and `zope_interface` onto one name, and `installed_packages()` keys its map
    by the raw metadata `Name`, so a body can legitimately carry both. Measured: keeping the last
    of the two made the digest depend on **line order**, producing two values for one environment
    and contradicting this module's own order-invariance. Two versions for one canonical name is
    information, and picking one invents an answer.
    """
    pip: dict[str, str] = {}
    conda: dict[str, tuple[str, str]] = {}
    in_conda = False
    for raw in body.split("\n"):
        line = raw.rstrip("\r")
        if not line.strip():
            continue
        if line == CONDA_SEPARATOR:
            in_conda = True
            continue
        if line.startswith("#"):
            continue  # an annotation: the header, or a comment a renderer added
        found = (_CONDA_ENTRY if in_conda else _PIP_ENTRY).match(line)
        if found is None:
            raise PackageSetError(
                f"a snapshot body line is neither a package entry nor an annotation, so this "
                f"environment cannot be reduced to a digest without dropping it: {line!r}"
            )
        name = normalise(found.group(1))
        if in_conda:
            _refuse_collision("conda", name, conda.get(name), found.group(2), conda)
            conda[name] = (found.group(2), found.group(3) or "")
        else:
            _refuse_collision("pip", name, pip.get(name), found.group(2), pip)
            pip[name] = found.group(2)
    return pip, conda


def _refuse_collision(
    kind: str,
    name: str,
    already: str | tuple[str, str] | None,
    version: str,
    _seen: typing.Mapping[str, object],
) -> None:
    """Refuse a second entry for one canonical name. ADR-0025 R-3.

    SEPARATE FROM `parse` SO BOTH HALVES CALL ONE COPY. The pip and conda branches are the two
    places this can happen, and a rule written twice in one function is the shape that drifts.
    """
    if already is None:
        return
    seen = already[0] if isinstance(already, tuple) else already
    raise PackageSetError(
        f"two {kind} entries normalise to the canonical name {name!r} with versions {seen!r} and "
        f"{version!r}. Keeping either one would make this digest depend on the order the lines "
        f"happen to be in, so it is refused: a published digest cannot be order-dependent"
    )


def canonical_text(body: str) -> str:
    """The canonical text of a snapshot body's package set. ADR-0024 R-4.

    THE FIRST LINE IS A DOMAIN SEPARATOR, so this digest can never collide with a digest of
    anything else in this package — a record, an artifact, a history line — and so that a reader
    holding a bare hex string can be told which algorithm produced it.

    THE INTERPRETER IS DELIBERATELY ABSENT [ADR-0025 R-5]. It is recorded in its own field and
    compared on its own. A digest covering the package set AND the interpreter could not say
    which of them moved, which is the defect this module exists to repair; committing it again at
    a smaller scale would be the same mistake with a quieter failure.
    """
    pip, conda = parse(body)
    lines = [ALGORITHM]
    lines += [f"pip {name} {version}" for name, version in sorted(pip.items())]
    lines += [f"conda {name} {v} {b}" for name, (v, b) in sorted(conda.items())]
    return "\n".join(lines) + "\n"


def package_set_digest(body: str) -> str:
    """The SHA-256 of `canonical_text(body)`, hex, 64 characters."""
    return hashlib.sha256(canonical_text(body).encode("utf-8")).hexdigest()


def main(argv: list[str] | None = None) -> int:
    """Print the package-set digest of each snapshot body named on the command line.

    THE AUDITOR'S ENTRY POINT, and the reason this module has one at all. Copy this single file
    out of an installed runprov and run `python pkgset.py env-*.txt` in a container that has no
    runprov in it. The exit code follows ADR-0007: **0** every file was reduced to a digest, **2**
    one of them could not be — never 1, because this command makes no comparison and so has no
    finding to report.
    """
    paths = sys.argv[1:] if argv is None else argv
    if not paths:
        print(f"usage: pkgset.py <snapshot file> ...    # {ALGORITHM}", file=sys.stderr)
        return 2
    for path in paths:
        try:
            with open(path, encoding="utf-8") as handle:
                print(f"{ALGORITHM}:{package_set_digest(handle.read())[:16]}  {path}")
        except (OSError, PackageSetError, UnicodeDecodeError) as exc:
            print(f"{path}: {exc}", file=sys.stderr)
            return 2
    return 0


if __name__ == "__main__":  # pragma: no cover - the copied-out auditor path, probed by a test
    raise SystemExit(main())
