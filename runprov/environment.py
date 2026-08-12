# Copyright (c) 2026 Assistance Publique – Hôpitaux de Paris (AP-HP),
#                    Hôpital Henri-Mondor, and Taylor Thompson
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
import json
import os
import pathlib
import platform
import sys
import typing


def installed_packages(unreadable: list[str] | None = None) -> dict[str, str]:
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
    # (lower, exact) so a case-variant pair cannot tie and be broken by sys.path scan
    # order, which is unsorted. A tie here would change the rendered body, hence the
    # digest, hence the env-<sha16> filename referenced from every provenance record.
    return {k: out[k] for k in sorted(out, key=lambda s: (s.lower(), s))}


def conda_packages(prefix: pathlib.Path | None = None) -> dict[str, str]:
    """`{name: version=build}` for what conda, mamba, micromamba or pixi installed.

    THE GAP THIS CLOSES. `importlib.metadata` sees Python distributions. A conda-family
    environment is mostly NOT Python distributions: measured on a bare `mamba create -p env
    python=3.12`, conda installed **27** packages and the snapshot recorded **8** — the
    nineteen missing ones being libgcc, openssl, sqlite, icu, ncurses, tk and the rest.

    In this package's own target setting that list is where the tools live. A genotyping
    pipeline's `samtools`, `blast` and `mmseqs2` come from conda, have no dist-info, and
    were therefore absent from every snapshot claiming to describe the environment that
    produced a result — the silent omission this module says in its own docstring it exists
    to avoid.

    Read from `$PREFIX/conda-meta/*.json`, which is a filesystem read and not a subprocess.
    That is the same argument as for `importlib.metadata`: `conda list` needs a `conda` on
    PATH that may not be the one owning this interpreter, and in a mamba-plus-uv layout it
    reliably is not. The directory belongs to the prefix the interpreter is running from,
    so it cannot disagree with itself.

    MEASURED on conda and mamba, which share one prefix layout. micromamba and pixi build
    prefixes in that same format and so are covered by construction rather than by test --
    stated that way round on purpose, because neither was run here. Nothing depends on
    WHICH tool wrote the directory: the rule is "this prefix has a conda-meta".
    """
    root = (prefix or pathlib.Path(sys.prefix)) / "conda-meta"
    out: dict[str, str] = {}
    if not root.is_dir():
        return out
    for f in sorted(root.glob("*.json")):
        try:
            meta = json.loads(f.read_text(encoding="utf-8"))
            name, version, build = meta["name"], meta["version"], meta.get("build", "")
        except (OSError, ValueError, KeyError):
            # The filename is `name-version-build.json` by construction, so an unreadable
            # record still identifies its package. Dropping it would understate the
            # environment, which is the one thing a snapshot must never do.
            parts = f.stem.rsplit("-", 2)
            if len(parts) != 3:
                continue
            name, version, build = parts
        out[str(name)] = f"{version}={build}" if build else str(version)
    return {k: out[k] for k in sorted(out, key=lambda s: (s.lower(), s))}


def render(
    packages: dict[str, str],
    unreadable: int = 0,
    conda: dict[str, str] | None = None,
) -> str:
    """Requirements-style text, sorted, with the interpreter recorded in the header.

    The package list alone is not the environment: the same versions on a different Python
    are a different environment, and the old per-run files did not say which they were.
    """
    conda = conda or {}
    head = [
        "# environment snapshot — content-addressed; the filename IS the digest of this body",
        f"# python   : {sys.version.split()[0]}",
        f"# platform : {platform.platform()}",
        f"# packages : {len(packages)}",
    ]
    if conda:
        head.append(f"# conda    : {len(conda)} package(s) from this prefix's conda-meta")
    if unreadable:
        # Stated, never dropped. A snapshot quietly missing entries reads as complete.
        head.append(f"# UNREADABLE: {unreadable} distribution(s) had no readable name")
    body = [f"{n}=={v}" for n, v in packages.items()]
    if conda:
        # Conda's own `--export` spelling, `name=version=build`, and under a heading, so a
        # reader can tell which manager put a package there. NO PREFIX PATH appears here:
        # two identical environments installed at different locations are the same
        # environment, and a path in the body would make the digest machine-specific and
        # defeat the content addressing this file is named by.
        body += ["# --- conda-meta (conda / mamba / micromamba / pixi) ---"]
        body += [f"{n}={v}" for n, v in conda.items()]
    return "\n".join(head + body) + "\n"


def digest(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def write_snapshot(directory: pathlib.Path) -> dict[str, typing.Any]:
    """Write `env-<sha16>.txt` into `directory` if it is not already there.

    Returns the record embedded in the run: path, digest, package count, and whether this
    run created the file or found an identical one. `reused: true` is the useful signal —
    it means the environment has not moved since some earlier run.
    """
    bad: list[str] = []
    pkgs = installed_packages(bad)
    conda = conda_packages()
    text = render(pkgs, len(bad), conda)
    d = digest(text)
    directory = pathlib.Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"env-{d[:16]}.txt"
    existed = path.is_file()
    if not existed:
        path.write_text(text, encoding="utf-8")
    rec: dict[str, typing.Any] = {
        "path": str(path),
        "sha256": d,
        "n_packages": len(pkgs),
        "n_unreadable": len(bad),
        "python": sys.version.split()[0],
        "reused": existed,
    }
    if conda:
        # Only when there are any, so a plain venv's record does not carry a field about a
        # manager it has never met. Its presence is itself the statement "this interpreter
        # lives in a conda-family prefix".
        rec["n_conda_packages"] = len(conda)
    return rec


#: Lock and requirement files, in the order a reader should trust them: a resolved lock
#: pins exact versions and hashes, a `requirements.txt` may be a loose declaration. Fixed
#: list rather than a glob, so what is captured is reviewable and cannot quietly widen.
LOCKFILES = (
    "uv.lock",
    "poetry.lock",
    "pdm.lock",
    "pixi.lock",
    "conda-lock.yml",
    "Pipfile.lock",
    "environment.yml",
    "environment.yaml",
    "requirements.txt",
    "requirements.lock",
    "requirements-dev.lock",
)

#: Environment variables that NAME an environment. Values are recorded because a name is
#: the thing a human asks for -- "which env was that?" -- and none of these is a path.
_NAME_VARS = ("CONDA_DEFAULT_ENV", "HATCH_ENV_ACTIVE", "PIXI_ENVIRONMENT_NAME")

#: Variables whose PRESENCE identifies a manager. The values are paths, and paths carry
#: usernames and machine layout, so only the fact that they are set is recorded.
_MANAGER_VARS = {
    "CONDA_PREFIX": "conda",
    "MAMBA_EXE": "mamba",
    "MAMBA_ROOT_PREFIX": "mamba",
    "PIXI_PROJECT_ROOT": "pixi",
    "POETRY_ACTIVE": "poetry",
    "PDM_PROJECT_ROOT": "pdm",
    "HATCH_ENV_ACTIVE": "hatch",
    "RYE_HOME": "rye",
    "UV_PROJECT_ENVIRONMENT": "uv",
    "VIRTUAL_ENV": "venv",
}


def manager(
    prefix: pathlib.Path | None = None, env: dict[str, str] | None = None
) -> dict[str, typing.Any]:
    """Which tool built the environment this interpreter is running in, and the EVIDENCE.

    A package list says what is installed. It does not say how to rebuild it, and "rebuild
    it" is the question anyone asks six months later. `uv sync`, `mamba env create` and
    `poetry install` are different commands over different files, so the answer starts with
    which one applies.

    Detected from what is ON DISK and in the environment, never by running anything:

    * `pyvenv.cfg` in the prefix — written by `venv`, `virtualenv` and `uv`, and **uv
      stamps its own version into it**, which is the most reliable marker there is;
    * `conda-meta/` in the prefix — conda, mamba, micromamba and pixi all create it;
    * a small set of environment variables, listed above.

    Returns the evidence beside the verdict, and a LIST rather than one name, because the
    layouts overlap for real: a uv-created venv inside a conda prefix is an ordinary thing
    in this field, and a single answer would have to be wrong about one of them.

    Paths are deliberately absent from the result. `VIRTUAL_ENV` and `CONDA_PREFIX` hold
    absolute paths carrying a username and a machine layout, and this dict goes into a
    record that gets committed and shared.
    """
    prefix = pathlib.Path(sys.prefix) if prefix is None else prefix
    env = dict(os.environ) if env is None else env
    detected: list[str] = []
    evidence: dict[str, typing.Any] = {}

    cfg = prefix / "pyvenv.cfg"
    if cfg.is_file():
        detected.append("venv")
        try:
            for line in cfg.read_text(encoding="utf-8", errors="replace").splitlines():
                key, _, value = line.partition("=")
                key, value = key.strip().lower(), value.strip()
                # `uv` and `virtualenv` write their own version; plain `venv` writes
                # neither, which is itself how a plain venv is recognised.
                if key in ("uv", "virtualenv") and value:
                    detected.append(key)
                    evidence[f"pyvenv.cfg:{key}"] = value
        except OSError:  # guards-ok: an unreadable pyvenv.cfg still proves a venv layout,
            # which is the part that was already recorded above.
            evidence["pyvenv.cfg"] = "present but unreadable"

    if (prefix / "conda-meta").is_dir():
        detected.append("conda-family")
        evidence["conda-meta"] = True

    for var, name in _MANAGER_VARS.items():
        if env.get(var):
            detected.append(name)
            evidence[var] = "set"  # never the value: it is a path

    for var in _NAME_VARS:
        if env.get(var):
            evidence[var] = env[var]

    # Sorted and deduplicated so two runs in one environment produce one answer regardless
    # of dict iteration or which marker was seen first.
    return {"detected": sorted(set(detected)), "evidence": evidence}


def lockfiles(root: pathlib.Path) -> list[dict[str, typing.Any]]:
    """Every lock or requirements file at the project root, hashed.

    This is the half that makes an environment reproducible rather than merely described.
    The snapshot says which versions were installed; the lock says how to install them
    again, and a digest says WHICH lock — the file changes as the project moves, and a
    record naming `uv.lock` without pinning its content names a moving target.
    """
    from .hashing import sha256

    out: list[dict[str, typing.Any]] = []
    for name in LOCKFILES:
        path = root / name
        if path.is_file():
            out.append({"name": name, "sha256": sha256(path), "bytes": path.stat().st_size})
    return out


def archive_lockfiles(root: pathlib.Path, directory: pathlib.Path) -> list[dict[str, typing.Any]]:
    """Copy each lock file into `directory`, named by its own digest.

    Hashing a lock file records which one it was; copying it means the run can still be
    rebuilt after that file has moved on. Content-addressed for the same reason the package
    snapshot is: an unchanged lock collapses to one copy no matter how many runs reference
    it, and `reused: true` says the environment's DECLARATION has not moved either.
    """
    directory = pathlib.Path(directory)
    out: list[dict[str, typing.Any]] = []
    for rec in lockfiles(root):
        target = directory / f"lock-{rec['sha256'][:16]}-{rec['name']}"
        rec = dict(rec, path=str(target), reused=target.is_file())
        if not rec["reused"]:
            try:
                directory.mkdir(parents=True, exist_ok=True)
                target.write_bytes((root / rec["name"]).read_bytes())
            except OSError as exc:  # guards-ok: a snapshot that could not be archived is
                # recorded as such. Failing the caller's run because a copy failed would
                # make provenance the reason the work did not happen.
                rec["error"] = str(exc)
        out.append(rec)
    return out
