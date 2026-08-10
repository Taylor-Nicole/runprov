# Copyright (c) 2026 Assistance Publique – Hôpitaux de Paris (AP-HP),
#                    Hôpital Henri-Mondor, and Taylor Thompson
# Licensed under the CeCILL-B Free Software License Agreement — see LICENSE.
# https://cecill.info/
"""`Run` — one pass of one script, recorded so it can be reconstructed or invalidated.

The interface is the point
-------------------------
`run.input(p)` RETURNS the path. So the natural way to open a file is also the registering
way, and a read that skips registration is a visible omission rather than the default:

    df = pd.read_csv(run.input(path))          # registered
    df = pd.read_csv(path)                     # not — and a checker can see the difference

The predecessor this replaces took provenance as prose — `{"input": f"{a}, {b}"}` — so it
recorded what the author believed the step read. 248 files imported it. Universal adoption
did not make the record true, because the interface could not tell the difference between
a description and a fact.

What is deliberately excluded from `header()` is documented on that method: no timestamp,
no run id. Both were tried; both made every pinned artifact differ on every run.
"""

from __future__ import annotations

import datetime as dt
import inspect
import json
import os
import pathlib
import platform
import shlex
import sys
import traceback
import types
import typing

from .environment import write_snapshot
from .hashing import describe, sha256
from .project import Project, active, git

# The record format, named and versioned. A consumer -- a script, a dashboard, an agent
# reading the history -- can branch on this instead of guessing from which keys happen to
# be present. Bump it when a field changes meaning, never when one is added.
SCHEMA = "runprov.run.v1"


def _caller_file() -> pathlib.Path | None:
    """The script that constructed the Run — not this package's own file.

    `_provenance.py` computed `script_sha256` as `Path(__file__).parent / f"{script}.py"`,
    which held only while it sat in the same directory as its callers. Promoting the module
    breaks that silently: the hash becomes None for every script and nothing says so. Walk
    the stack instead, skipping frames inside this package.
    """
    here = pathlib.Path(__file__).resolve().parent
    for fr in inspect.stack()[1:]:
        f = pathlib.Path(fr.filename).resolve()
        if here not in f.parents and f.is_file():
            return f
    return None


class Run:
    """Collects provenance for a single run.

    Args:
        script: the name recorded, and the key a history is grouped by.
        params: the parameters that shaped the result. Recorded verbatim.
        project: where this is happening. Defaults to the configured project.
        script_path: override the auto-detected caller file (wrappers, notebooks).
    """

    def __init__(
        self,
        script: str,
        params: dict[str, typing.Any] | None = None,
        *,
        project: Project | None = None,
        script_path: pathlib.Path | None = None,
        provenance: pathlib.Path | None = None,
    ) -> None:
        self.project = project or active()
        root = self.project.root
        dirty = git(root, "status", "--porcelain", "--", *self.project.code_paths)
        everything = git(root, "status", "--porcelain")

        sp = pathlib.Path(script_path) if script_path else _caller_file()
        self.record: dict[str, typing.Any] = {
            "schema": SCHEMA,
            "script": script,
            "run_id": self.project.run_id(),
            "generation": self.project.generation(),
            "started_utc": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            # THE FULL INVOCATION, re-runnable as written. This recorded only the
            # basename -- `step_review.py` -- which says neither which interpreter ran it
            # nor from where, and the predecessor this package replaces did better:
            # `/home/.../envs/hcv_genotyping_env/bin/python3 src/preprocessing/x.py
            #  --inputs data/...`. That names the environment, which for a mamba/uv layout
            # with several venvs is most of the answer to "why did this run differ".
            "command": " ".join(shlex.quote(a) for a in [sys.executable, *sys.argv]),
            "argv": list(sys.argv),
            # A relative script path in `command` means nothing without this.
            "cwd": str(pathlib.Path.cwd()),
            "parameters": params or {},
            "code": {
                # The detected root, recorded. A module that can be wrong about which
                # repository it is in must at least say which one it chose.
                "project_root": str(root),
                "git_commit": git(root, "rev-parse", "HEAD"),
                "git_commit_short": git(root, "rev-parse", "--short", "HEAD"),
                "git_branch": git(root, "rev-parse", "--abbrev-ref", "HEAD"),
                "git_code_dirty": bool(dirty),
                "git_dirty_code_files": (dirty.splitlines() if dirty else []),
                "git_other_changes": len(everything.splitlines()) if everything else 0,
                "script_file": str(sp) if sp else None,
                "script_sha256": sha256(sp) if sp and sp.is_file() else None,
            },
            "environment": {
                "python": sys.version.split()[0],
                "platform": platform.platform(),
                "hostname": platform.node(),
                "cpu_count": os.cpu_count(),
                "packages": self._versions(),
            },
            "seeds": [],
            "inputs": [],
            "outputs": [],
        }
        self._pending: list[pathlib.Path] = []
        self.provenance_path = pathlib.Path(provenance) if provenance else None
        self._written = False
        # Inside a `with` block the run is NOT over when write() is called -- the work can
        # still fail afterwards. The sidecar is written eagerly (a caller may want it on
        # disk), and the HISTORY line is deferred to __exit__, the only moment the final
        # status is known. Without this, write() inside the block appended `status: ok`
        # and __exit__ declined to correct it, so a script dying in teardown, in a final
        # assertion or in a `finally` was greppable as a SUCCESS.
        self._in_context = False
        self._deferred_history: pathlib.Path | None = None
        if dirty:
            print(
                "  PROVENANCE WARNING: CODE is modified relative to git_commit; the "
                "commit does not identify what ran:"
            )
            for line in dirty.splitlines():
                print(f"    {line}")

    # ------------------------------------------------------------- failure recording
    def __enter__(self) -> Run:
        """Use `with Run(..., provenance=P) as run:` so a CRASH still leaves a record.

        Without this, `write()` is the last line of a script and a step that dies halfway
        records nothing at all — which is precisely the defect this package was written to
        replace. The predecessor appended only on success, so its "300 runs" was 300
        *completed* runs with an unknown denominator. Building the replacement with the
        same hole and a better interface would have been the funnier version of the same
        mistake, and it shipped that way for one commit.
        """
        self._in_context = True
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: types.TracebackType | None,
        # Literal[False], not bool, and mypy is right to insist. A `bool` return says
        # "this context manager MAY swallow exceptions". It must never. The type says so
        # now, so no caller has to read the body to find out.
    ) -> typing.Literal[False]:
        if exc_type is not None:
            self.record["status"] = "failed"
            self.record["failure"] = {
                "type": exc_type.__name__,
                "message": str(exc)[:2000],
                # Tail, not head: the frames nearest the failure are the informative ones.
                "traceback": "".join(traceback.format_exception(exc_type, exc, tb))[-4000:],
            }
        self._in_context = False
        if self.provenance_path is not None and not self._written:
            self.write(self.provenance_path)
        elif self._written and self.provenance_path is not None:
            # Already on disk, and the status may have just changed under it. Rewrite so
            # the sidecar carries the truth rather than the optimistic snapshot.
            self._persist(self.provenance_path)
        if self._deferred_history is not None:
            path, self._deferred_history = self._deferred_history, None
            self._append_history(path)
        return False  # NEVER swallow. A provenance module that hides an exception is
        # strictly worse than one that records nothing.

    def _versions(self) -> dict[str, typing.Any]:
        out = {}
        for mod in self.project.tracked_packages:
            try:
                out[mod] = __import__(mod).__version__
            except Exception:  # guards-ok: None IS the record — "this package was not
                # importable in the run's environment" is a fact worth keeping, and a
                # tracked package that is absent must not abort somebody's run
                out[mod] = None
        return out

    # ---------------------------------------------------------------- registration
    def input(self, path: str | pathlib.Path) -> pathlib.Path:
        """Hash and record a read. RETURNS the path, so registering is the easy path."""
        p = pathlib.Path(path)
        if not p.exists():
            # A bare FileNotFoundError from inside os.stat names neither the script nor
            # the fact that provenance registration raised it. The read was going to fail
            # anyway; failing here with the context is strictly more useful.
            raise FileNotFoundError(
                f"{self.record['script']}: cannot register input {p} — it does not exist. "
                f"A registered input is hashed and pinned, so it must be present at "
                f"registration time. Register it after producing it, or check the path."
            )
        self.record["inputs"].append(describe(p))
        return p

    def output(self, path: str | pathlib.Path) -> pathlib.Path:
        """Register a write. Hashed in `write()`, not here.

        Callers do `df.to_csv(run.output(p))`, so the file does not exist yet at call
        time. Hashing eagerly silently dropped every output whose writer had not yet run.
        """
        p = pathlib.Path(path)
        self._pending.append(p)
        return p

    def environment_snapshot(
        self, directory: str | pathlib.Path | None = None
    ) -> dict[str, typing.Any] | None:
        """Capture the FULL installed package set, content-addressed.

        `environment.packages` records only the tracked subset -- enough to explain a
        numerical difference in the usual case, useless when the cause is a package nobody
        thought to track. This is the whole set, written as `env-<sha16>.txt`, so two runs
        in the same environment reference one file and a changed environment is visible as
        a changed digest rather than as a diff across timestamped filenames.

        Called automatically by `write()` when the project configures a directory.
        """
        d = directory or self.project.env_snapshot_dir
        if d is None:
            return None
        try:
            rec = write_snapshot(pathlib.Path(d))
        except Exception as exc:  # never let provenance capture break a run
            print(f"  WARNING: could not write environment snapshot: {exc}")
            rec = {"error": str(exc)}
        self.record["environment"]["snapshot"] = rec
        return rec

    def seeds(self, seeds: typing.Iterable[int]) -> None:
        self.record["seeds"] = [int(s) for s in seeds]

    def note(self, key: str, value: typing.Any) -> None:  # noqa: ANN401
        """Any, deliberately: a note is whatever number or string the script wants recorded."""
        self.record.setdefault("notes", {})[key] = value

    def module(self, module: types.ModuleType) -> None:
        """Record where an imported module actually RESOLVED from.

        A commit is not sufficient provenance when two installables share a name: a PEP 660
        finder can resolve an import to a sibling project's copy, and `sys.path.insert`
        will not override it. A record naming this repository's commit then attributes the
        run to code that never executed. Recording the resolved `__file__` and its hash is
        what makes that case detectable at all.
        """
        f = getattr(module, "__file__", None)
        rec: dict[str, typing.Any] = {
            "module": getattr(module, "__name__", str(module)),
            "resolved_file": f,
        }
        if f:  # guards-ok: a module with no __file__ (builtin, namespace package) is
            # recorded with resolved_file: null rather than skipped — the absence is
            # the finding, since a builtin cannot be the vendored copy this guards against
            fp = pathlib.Path(f)
            rec["inside_project"] = str(self.project.root) in str(fp.resolve())
            if fp.is_file():  # guards-ok: no sha256 key means the resolved path is not
                # a readable file, which resolved_file already states. Hashing a
                # non-existent path would raise inside provenance capture instead.
                rec["sha256"] = sha256(fp)
            if not rec["inside_project"]:
                print(
                    f"  PROVENANCE WARNING: {rec['module']} resolved OUTSIDE the project root: {f}"
                )
        self.record.setdefault("modules", []).append(rec)

    # ---------------------------------------------------------------- the pin
    def header(self, comment: str = "# ") -> str:
        """The PIN, as a comment block to embed in the artifact ITSELF.

        A sidecar provenance file is overwritten by the next run, so a committed artifact
        and the record describing it drift apart. The failure that produced this method: a
        locked holdout recorded the hash of its own body and nothing about the corpus it
        was drawn from; its input then moved four times without the lock being able to say
        so, and three fabricated labels sat inside a locked evaluation set.

        **A lock is only as good as the thing it pins. An artifact that cannot say what it
        was made from cannot be invalidated when that thing changes.**

        Excluded on purpose, both learned by breaking it:

        * **No timestamp.** Every artifact would differ on every run for no reason.
        * **No run id** — a run id embeds a UTC stamp, so it is a timestamp wearing a
          different name. The first version carried one and two identical runs over
          identical inputs both reported 80 artifacts CHANGED. `run_id` answers *which
          pass wrote this*; the pin answers *what it was made from*. Only the second
          belongs in the artifact; the sidecar already carries the first.

        Input hashes are the opposite: they change only when an input changes, which is
        exactly when the artifact should read as CHANGED.
        """
        c = comment
        code = self.record["code"]
        # NOT `None`. The first artifact a new project produces is written before its
        # first commit, and a pin reading "commit: None" is a value a reader has to
        # interpret. Say what it means: there is no commit to name.
        commit = code["git_commit_short"] or "NONE — no commit to name (yet)"
        lines = [
            f"{c}provenance — this artifact and what produced it",
            f"{c}  script     : {self.record['script']}",
            f"{c}  generation : {self.record['generation']}",
            f"{c}  commit     : {commit}"
            + (
                "  (CODE DIRTY — the commit does not identify what ran)"
                if code["git_code_dirty"]
                else ""
            ),
        ]
        # SORTED AND DEDUPED, and that is the determinism guarantee rather than a tidy-up.
        # This rendered inputs in REGISTRATION order, and `Path.glob()` returns filesystem
        # order -- so the ordinary `for p in DIR.glob("*.tsv"): run.input(p)` produced a
        # different pin on a different machine while the data was identical. That is the
        # 80-artifacts-CHANGED regression this method exists to prevent, arriving through a
        # different door. Duplicates collapse for the same reason: a data-dependent read
        # loop must not move the pin.
        #
        # The SIDECAR still records every registration, in order. How many times a script
        # opened a file is a fact about the run, and facts about the run live there.
        pinned: dict[tuple[str, str], None] = {}
        for i in self.record["inputs"]:
            # Content digest FIRST: two runs over the same data must pin identically.
            sha = str(
                i.get("content_sha256") or i.get("sha256") or i.get("sha256_tree") or "MISSING"
            )[:16]
            pinned.setdefault((sha, self._pin_name(i.get("path", "?"))), None)
        ins = sorted(pinned)
        if ins:
            lines.append(f"{c}  inputs ({len(ins)}), sha256:")
            for sha, name in ins:
                lines.append(f"{c}    {sha}  {name}")
        else:
            # NOT silence. "Derived from nothing" and "reads bypass run.input()" look
            # identical in an artifact unless one of them says so.
            lines.append(
                f"{c}  inputs     : NONE REGISTERED. Either this artifact is "
                f"derived from nothing, or its reads bypass run.input()."
            )
        return "\n".join(lines) + "\n"

    def _pin_name(self, raw: str) -> str:
        """The label a pinned input carries INSIDE an artifact.

        Relative to the project root where possible; otherwise `<external>/<name>` rather
        than the absolute path. An absolute path embeds this machine's directory layout in
        a committed artifact, so the same data pinned on two machines produced two
        different pins -- the same machine-dependence class as the encoding defect. The
        full path stays in the sidecar, where it is information rather than a comparison
        key.
        """
        try:
            return str(pathlib.Path(raw).relative_to(self.project.root))
        except (ValueError, TypeError):
            return f"<external>/{pathlib.Path(raw).name}"

    # ---------------------------------------------------------------- finish
    def write(self, path: str | pathlib.Path) -> pathlib.Path:
        """Hash the registered outputs, write the record, append to the history."""
        p = pathlib.Path(path)
        if (
            self.project.env_snapshot_dir is not None
            and "snapshot" not in self.record["environment"]
        ):
            self.environment_snapshot()
        seen: set[pathlib.Path] = set()
        for q in self._pending:
            if q in seen:
                continue
            seen.add(q)
            if q.exists():
                self.record["outputs"].append(describe(q))
            else:
                # A registered output that was never written is a FINDING, not an
                # omission. Dropping it here is how a stage reports success having
                # produced nothing.
                self.record["outputs"].append(
                    {"path": str(q), "kind": "MISSING", "note": "registered but never written"}
                )
        self.record.setdefault("status", "ok")
        self.record["finished_utc"] = dt.datetime.now(dt.timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
        already = self._written
        self._written = True
        self._persist(p)
        if self._in_context:
            self._deferred_history = p  # see __init__; __exit__ appends it once
        elif already:
            # A second write() rewrites the sidecar deliberately (a caller may want the
            # record at two paths) but must NOT append a second history line: one run is
            # one line, or every count taken from the history is wrong.
            print("  (history already recorded for this run; sidecar rewritten only)")
        else:
            self._append_history(p)

        if self.record.get("status") == "failed":
            print(
                f"  RUN FAILED — recorded: {self.record['failure']['type']}: "
                f"{self.record['failure']['message'][:120]}"
            )
        print(f"provenance -> {p}")
        print(
            f"  code {self.record['code']['git_commit_short']}"
            f"{' (CODE DIRTY)' if self.record['code']['git_code_dirty'] else ''}"
            f"  inputs {len(self.record['inputs'])}"
            f"  outputs {len(self.record['outputs'])}"
            f"  seeds {self.record['seeds']}"
        )
        return p

    def _persist(self, p: pathlib.Path) -> None:
        """Write the sidecar. Split out of write() so __exit__ can correct a record that
        has become false without appending a second history line for one run."""
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(self.record, indent=2, default=str), encoding="utf-8")

    def _append_history(self, prov_path: pathlib.Path) -> None:
        """Append the summary to the project's sink — one line per run, forever.

        Each sidecar holds only the LATEST run of a script; the history holds every run
        ever. It is the one thing an experiment tracker gives you that per-run files do
        not, and keeping it as a git-tracked JSONL rather than a service means it is
        diffable, travels with the repository, and needs nothing a reviewer cannot run.

        Where it goes is the project's business (`Project.sink`), not this method's.
        """
        r = self.record
        summary = {
            "schema": SCHEMA,
            "script": r["script"],
            "run_id": r["run_id"],
            "generation": r["generation"],
            # So `grep '"status": "failed"' runs.jsonl` is the whole query. A history of
            # successes only cannot tell you how often a step fails.
            "status": r.get("status", "ok"),
            "failure": r.get("failure"),
            "started_utc": r["started_utc"],
            "finished_utc": r["finished_utc"],
            "command": r["command"],
            "cwd": r["cwd"],
            "parameters": r["parameters"],
            "seeds": r["seeds"],
            "git_commit": r["code"]["git_commit_short"],
            "git_code_dirty": r["code"]["git_code_dirty"],
            "script_sha256": r["code"]["script_sha256"],
            "packages": r["environment"]["packages"],
            "inputs": [
                {"path": i["path"], "sha256": i.get("sha256") or i.get("sha256_tree")}
                for i in r["inputs"]
            ],
            "outputs": [
                {"path": o["path"], "sha256": o.get("sha256") or o.get("sha256_tree")}
                for o in r["outputs"]
            ],
            # Where the FULL record is. Without this an archiver reading the history can
            # find every artifact a run produced EXCEPT its own provenance.
            "provenance_path": str(prov_path),
            "notes": r.get("notes", {}),
        }
        self.project.resolved_sink().append(summary)
