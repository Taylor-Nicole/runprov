# Copyright (c) 2026 Taylor Thompson
# Licensed under the CeCILL-B Free Software License Agreement — see LICENSE.
# https://cecill.info/
"""The full installed package set, captured CONTENT-ADDRESSED rather than per run.

What the old pipeline did, and what it cost
-------------------------------------------
The sibling project captured a pip freeze per script invocation into
`requirements_<script>_v1_<timestamp>.txt`. Measured on disk:

    87 files   32 of 55 steps covered   8 DISTINCT CONTENTS   1.4 MB

So 87 files hold 8 environments. 79 of them are byte-identical copies of a neighbour, and
the question anyone actually has — *did the environment change between these two runs?* —
takes a diff across 87 timestamped filenames to answer.

Naming a snapshot by its own hash inverts that. Identical environments collapse to ONE
file, every run references it, and "did it change?" is string equality on a digest that is
already in the provenance record. Eight files, and a changed environment announces itself.

Two further differences from the original, both deliberate
----------------------------------------------------------
* **`importlib.metadata`, not `subprocess("pip freeze")`.** The dead shared helper in
  `log_transformation.py` ran bare `["pip", "freeze"]` — the pip on PATH, which in a
  mamba-plus-uv layout with several venvs is not reliably the interpreter running the
  script. (The per-script copies got this right with `sys.executable -m pip`; the shared
  one, the one that was never called, did not.) Reading the running interpreter's own
  metadata cannot disagree with itself, needs no subprocess, and works where pip is absent.
* **It records what it could not read.** A distribution with no version is reported, not
  skipped. A snapshot silently missing an entry is worse than one that says so.
"""

from __future__ import annotations

import hashlib
import pathlib
import platform
import sys


def installed_packages(unreadable: list | None = None) -> dict[str, str]:
    """`{name: version}` for the RUNNING interpreter. Never raises.

    `unreadable` collects distributions whose metadata could not be read, so the count can
    be written into the snapshot rather than vanishing.
    """
    import importlib.metadata as md

    out: dict[str, str] = {}
    for dist in md.distributions():
        try:
            name = (dist.metadata["Name"] or "").strip()
        except Exception:  # guards-ok: a distribution with unreadable metadata has no
            # name to key on, so it cannot be recorded as an entry. It is counted instead
            # -- see `unreadable` in the snapshot header, which is what stops this from
            # being a silent omission.
            name = ""
        if not name:
            if unreadable is not None:
                unreadable.append(str(getattr(dist, "_path", "?")))
            continue
        # UNKNOWN rather than omitted: a package present but unreadable is a fact about
        # the environment, and dropping it makes the snapshot quietly wrong.
        out[name] = (dist.version or "UNKNOWN").strip()
    return {k: out[k] for k in sorted(out, key=str.lower)}


def render(packages: dict[str, str], unreadable: int = 0) -> str:
    """Requirements-style text, sorted, with the interpreter recorded in the header.

    The package list alone is not the environment: the same versions on a different Python
    are a different environment, and the old per-run files did not say which they were.
    """
    head = [
        "# environment snapshot — content-addressed; the filename IS the digest of this body",
        f"# python   : {sys.version.split()[0]}",
        f"# platform : {platform.platform()}",
        f"# packages : {len(packages)}",
    ]
    if unreadable:
        # Stated, never dropped. A snapshot quietly missing entries reads as complete.
        head.append(f"# UNREADABLE: {unreadable} distribution(s) had no readable name")
    body = [f"{n}=={v}" for n, v in packages.items()]
    return "\n".join(head + body) + "\n"


def digest(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def write_snapshot(directory: pathlib.Path) -> dict:
    """Write `env-<sha16>.txt` into `directory` if it is not already there.

    Returns the record embedded in the run: path, digest, package count, and whether this
    run created the file or found an identical one. `reused: true` is the useful signal —
    it means the environment has not moved since some earlier run.
    """
    bad: list = []
    pkgs = installed_packages(bad)
    text = render(pkgs, len(bad))
    d = digest(text)
    directory = pathlib.Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"env-{d[:16]}.txt"
    existed = path.is_file()
    if not existed:
        path.write_text(text)
    return {
        "path": str(path),
        "sha256": d,
        "n_packages": len(pkgs),
        "n_unreadable": len(bad),
        "python": sys.version.split()[0],
        "reused": existed,
    }
