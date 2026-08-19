"""Tests for the `runprov` package.

Every test IMPORTS `runprov` and exercises the real objects. That is not a stylistic
preference — ADR-015 recorded a fix as Accepted whose class had zero construction sites for
its entire life, and the test covering it *reimplemented* the fix, so it could never fail.
A test that does not import its subject proves only that the test is self-consistent.

This file is deliberately free of anything HCV- or repository-specific, so it can be lifted
verbatim into a standalone runprov repository. The tests that check this repository's shim
live next door in `test_provenance_shim.py`.
"""

from __future__ import annotations

import argparse
import ast
import builtins
import concurrent.futures
import contextlib
import dataclasses
import errno
import gc
import gzip
import hashlib
import importlib
import importlib.metadata
import importlib.util
import inspect
import io
import itertools
import json
import logging
import os
import pathlib
import re
import shutil
import signal
import subprocess
import sys
import tarfile
import tempfile
import textwrap
import threading
import time
import types
import zipfile

import pytest

REPO = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

import runprov  # noqa: E402
import runprov.__main__ as cli  # noqa: E402
import runprov._report  # noqa: E402
import runprov.environment  # noqa: E402
import runprov.terminal  # noqa: E402
import runprov.watch  # noqa: E402

# ------------------------------------------------- what the platform can be asked to build
#
# L-26. `test.yml` runs this suite on windows-latest, and 22 tests here build a fixture
# Windows cannot build: a FIFO (`os.mkfifo` does not exist), a symlink (blocked without
# Developer Mode or admin, which a GitHub runner does not grant), an unreadable file
# (`chmod(0o000)` clears the read-only bit and nothing more, so the file stays readable),
# or `fcntl`. Every one of them fails in SETUP, before it asserts anything -- so the
# "on Linux 3.10-3.13, macOS and Windows" claim was made by a job that could not have
# been green.
#
# PROBED, NOT ASKED. `sys.platform == "win32"` is the wrong condition twice over: symlinks
# work on a Windows machine with Developer Mode on, and `chmod(0o000)` does not deny read
# TO ROOT, so the unreadable-file tests fail inside a root container on Linux -- which is
# most Docker CI -- and a platform check would have called that Windows-only. Asking the
# filesystem answers for the machine actually running.


#: CPython <=3.12 raises `RuntimeError("Symlink loop from ...")` when `resolve()` walks a
#: cyclic link; **3.13 returns the path unchanged instead**. Four tests asserted the exception
#: as their premise and so failed on 3.13 while the package itself behaved correctly — the
#: premise was version-specific, the OUTCOME each test exists for is not. Found by the one CI
#: run that ever executed, and confirmed locally against 3.13.15 (L-104).
#:
#: The guards in `run.py` still catch `RuntimeError` and must keep doing so: this is a
#: relaxation on the newer version, not a removal on the older ones.
_RESOLVE_RAISES_ON_LOOP = sys.version_info < (3, 13)


def _assert_symlink_loop(path: pathlib.Path) -> None:
    """The premise of the loop tests, asserted in a way that is true on every version."""
    try:
        path.resolve()
        raised = False
    except RuntimeError as exc:
        raised = "Symlink loop" in str(exc)
    assert raised == _RESOLVE_RAISES_ON_LOOP, (
        f"resolve()-on-a-loop behaviour changed for this interpreter "
        f"({sys.version_info.major}.{sys.version_info.minor}); update _RESOLVE_RAISES_ON_LOOP "
        f"and check that run.py's RuntimeError guards are still needed for the versions that "
        f"do raise"
    )


def _can_symlink() -> bool:
    with tempfile.TemporaryDirectory() as d:
        try:
            (pathlib.Path(d) / "l").symlink_to(pathlib.Path(d) / "t")
        except (OSError, NotImplementedError):
            return False
        return True


def _chmod_denies_read() -> bool:
    """Whether `chmod(0o000)` actually makes a file unreadable HERE. False on Windows, and
    false for root on POSIX."""
    with tempfile.TemporaryDirectory() as d:
        f = pathlib.Path(d) / "f"
        f.write_text("x", encoding="utf-8")
        try:
            f.chmod(0o000)
            f.read_text(encoding="utf-8")
        except OSError:
            return True
        finally:
            f.chmod(0o644)
        return False


requires_fifo = pytest.mark.skipif(
    not hasattr(os, "mkfifo"), reason="os.mkfifo does not exist on this platform (Windows)"
)
requires_symlinks = pytest.mark.skipif(
    not _can_symlink(),
    reason="cannot create a symlink here (Windows without Developer Mode or admin)",
)
requires_unreadable_files = pytest.mark.skipif(
    not _chmod_denies_read(),
    reason="chmod(0o000) does not deny read here (Windows, or running as root)",
)
requires_fcntl = pytest.mark.skipif(
    importlib.util.find_spec("fcntl") is None, reason="fcntl is POSIX-only"
)


# --------------------------------------------------------------------- hashing
def test_content_digest_ignores_volatile_header_stamps(tmp_path):
    a = tmp_path / "a.tsv"
    b = tmp_path / "b.tsv"
    a.write_text("# built_utc: 2026-01-01T00:00:00Z\nid\tvalue\nx\t1\n")
    b.write_text("# built_utc: 2099-12-31T23:59:59Z\nid\tvalue\nx\t1\n")
    assert runprov.sha256(a) != runprov.sha256(b)  # raw hashes differ
    assert runprov.content_digest(a) == runprov.content_digest(b)


def test_content_digest_ignores_volatile_json_keys(tmp_path):
    """The half that was missing at first — and it made every provenance JSON, which is
    itself a declared output, permanently unable to hash the same twice."""
    a = tmp_path / "a.json"
    b = tmp_path / "b.json"
    # ADR-029 R2 scoped this to runprov's OWN records, so the fixture carries the marker.
    # Before that scoping the rule fired on any *.json and erased a user's legitimate
    # `mtime_utc` from its digest — see
    # `test_volatile_stripping_applies_only_to_runprovs_own_records`.
    a.write_text(
        json.dumps({"schema": "runprov.run.v2", "started_utc": "2026-01-01T00:00:00Z", "n": 5})
    )
    b.write_text(
        json.dumps({"schema": "runprov.run.v2", "started_utc": "2099-12-31T23:59:59Z", "n": 5})
    )
    assert runprov.content_digest(a) == runprov.content_digest(b)


def test_content_digest_still_sees_a_real_change(tmp_path):
    """The stripping must not be so eager that it hides data. This is the failure mode
    that would make the whole mechanism silently useless."""
    a = tmp_path / "a.tsv"
    b = tmp_path / "b.tsv"
    a.write_text("# built_utc: 2026-01-01T00:00:00Z\nid\tvalue\nx\t1\n")
    b.write_text("# built_utc: 2026-01-01T00:00:00Z\nid\tvalue\nx\t2\n")
    assert runprov.content_digest(a) != runprov.content_digest(b)


def test_content_digest_of_gzip_ignores_the_compression_mtime(tmp_path):
    """A .gz rewritten from identical data hashes differently every time, because the
    gzip header stores an mtime. One artifact was reported CHANGED on every run for this."""
    import gzip
    import time

    a, b = tmp_path / "a.gz", tmp_path / "b.gz"
    a.write_bytes(gzip.compress(b"same payload", mtime=1))
    time.sleep(0.01)
    b.write_bytes(gzip.compress(b"same payload", mtime=999_999))
    assert a.read_bytes() != b.read_bytes()
    assert runprov.content_digest(a) == runprov.content_digest(b)


def test_describe_records_both_hashes(tmp_path):
    p = tmp_path / "x.tsv"
    p.write_text("# built_utc: 2026-01-01T00:00:00Z\nid\n1\n")
    rec = runprov.describe(p)
    assert rec["kind"] == "file"
    assert rec["sha256"] != rec["content_sha256"], (
        "both hashes must be present and they answer different questions"
    )


def test_describe_hashes_a_directory_as_a_tree(tmp_path):
    d = tmp_path / "dir"
    (d / "sub").mkdir(parents=True)
    (d / "a.txt").write_text("a")
    (d / "sub" / "b.txt").write_text("b")
    rec = runprov.describe(d)
    assert rec["kind"] == "directory" and rec["n_files"] == 2
    before = rec["sha256_tree"]
    (d / "sub" / "b.txt").write_text("changed")
    assert runprov.describe(d)["sha256_tree"] != before


# --------------------------------------------------------------------- the project
@pytest.fixture(autouse=True)
def _reset_once_per_process_warnings():
    """Both "say this once" flags, reset before every test.

    They are module globals by design — a run that says the same structural thing on every
    one of fifty steps is noise, and that is the point of them. In a test suite the same
    design makes assertions ORDER-DEPENDENT: whichever test runs first sees the message and
    the rest see nothing, so a test can pass alone and fail in the suite, or worse pass in
    the suite and stop testing anything. Reset here rather than in each test, because the
    hazard belongs to the flags and not to the tests that happen to notice it.
    """
    runprov.run._IMPLICIT_WARNED = False
    runprov.run._NO_REPO_WARNED = False


def _project(tmp_path) -> runprov.Project:
    return runprov.Project(
        root=tmp_path,
        run_log=tmp_path / "runs.jsonl",
        run_id=lambda: "test_run",
        generation=lambda: "test_gen",
    )


def test_run_log_defaults_under_the_root_not_to_this_repository(tmp_path):
    """A misconfigured install must write somewhere obvious, never append to a history
    it does not belong to."""
    p = runprov.Project(root=tmp_path)
    assert p.resolved_run_log() == tmp_path / "provenance" / "runs.jsonl"
    assert REPO not in p.resolved_run_log().parents


def test_detect_root_finds_the_git_toplevel(tmp_path):
    """Self-contained on purpose. The first version asserted against the AMBIENT
    repository and failed the moment the package was extracted into a directory where
    `git init` had not been run yet — a test that measures its surroundings rather than
    its subject. It builds its own repository instead."""
    import subprocess

    repo = tmp_path / "proj"
    (repo / "deep" / "nested").mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    assert runprov.detect_root(repo / "deep" / "nested") == repo.resolve()


def test_detect_root_falls_back_to_the_directory_itself_outside_git(tmp_path):
    """No git, no crash, and no silent claim about a repository that is not there."""
    d = tmp_path / "plain"
    d.mkdir()
    assert runprov.detect_root(d) == d.resolve()


def test_git_returns_none_instead_of_raising(tmp_path):
    """Provenance capture that can abort a run gets deleted from the run."""
    assert runprov.git(REPO, "not-a-git-subcommand") is None
    assert runprov.git(tmp_path / "does-not-exist", "rev-parse", "HEAD") is None


# --------------------------------------------------------------------- Run
def test_run_input_returns_the_path_so_registering_is_the_easy_path(tmp_path):
    src = tmp_path / "in.tsv"
    src.write_text("id\n1\n")
    run = runprov.Run("t", project=_project(tmp_path))
    assert run.input(src) == src  # usable as `open(run.input(p))`
    assert run.record["inputs"][0]["path"] == str(src)
    assert run.record["inputs"][0]["sha256"]


def test_run_records_a_registered_output_that_was_never_written(tmp_path):
    """A stage that reports success having produced nothing is the defect; dropping the
    entry here is what would hide it."""
    run = runprov.Run("t", project=_project(tmp_path))
    run.output(tmp_path / "never.tsv")
    prov = run.write(tmp_path / "t_provenance.json")
    rec = json.loads(prov.read_text(encoding="utf-8"))
    assert rec["outputs"][0]["kind"] == "MISSING"


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="POSIX FIFO needed to make one")
@requires_fifo
def test_an_output_that_cannot_be_hashed_is_recorded_and_does_not_destroy_the_run(tmp_path, capsys):
    """The whole record used to be lost for ONE awkward output, and that is the opposite of
    this package's job.

    `describe()` refuses a FIFO by design rather than blocking on it, and the raise left
    `write()`, left `_finish()`, and was swallowed by `__exit__`'s catch-all: no sidecar, no
    history line, exit 0 — with `provenance=` AND a `with` block, the two things documented
    to guarantee a record. A Snakemake `pipe()` output, a bash process substitution, a
    container-root-owned file or an output deleted after `exists()` all reach it.

    The asserts are deliberately about THE REST of the record. That the bad output is
    labelled matters less than that the good output, the input, the note and the history
    line all survive it — losing those was the defect.
    """
    proj = _project(tmp_path)
    src = tmp_path / "in.tsv"
    src.write_text("id\n1\n", encoding="utf-8")
    fifo = tmp_path / "stream.out"
    os.mkfifo(fifo)

    with runprov.Run("t", project=proj, provenance=tmp_path / "t_prov.json") as run:
        run.input(src)
        run.output(fifo)
        (good := run.output(tmp_path / "real.tsv")).write_text("id\n2\n", encoding="utf-8")
        run.note("rows", 1)

    rec = json.loads((tmp_path / "t_prov.json").read_text(encoding="utf-8"))
    outs = {pathlib.Path(o["path"]).name: o for o in rec["outputs"]}

    assert rec["status"] == "ok", "the run succeeded; an unhashable output is not a failure"
    assert rec["notes"] == {"rows": 1} and len(rec["inputs"]) == 1
    assert outs["real.tsv"]["sha256"], "the GOOD output must still be hashed and recorded"
    assert outs["stream.out"]["kind"] == "UNHASHABLE"
    assert "sha256" not in outs["stream.out"], "no digest may be invented for it"

    history = (tmp_path / "runs.jsonl").read_text(encoding="utf-8").strip().splitlines()
    assert len(history) == 1, "the run must reach the append-only history, not vanish from it"
    assert json.loads(history[0])["notes"] == {"rows": 1}

    # Announced, unlike MISSING: this file is present and ordinary-looking, so nothing but
    # the warning tells the user its digest is absent.
    assert "UNHASHABLE" in capsys.readouterr().err
    assert good.exists()


def test_run_hashes_outputs_at_write_time_not_at_registration(tmp_path):
    """Callers do `df.to_csv(run.output(p))`, so the file does not exist at call time.
    Eager hashing silently dropped every output whose writer had not yet run."""
    run = runprov.Run("t", project=_project(tmp_path))
    out = run.output(tmp_path / "late.tsv")
    out.write_text("id\n1\n")
    rec = json.loads(run.write(tmp_path / "t_provenance.json").read_text(encoding="utf-8"))
    assert rec["outputs"][0]["kind"] == "file" and rec["outputs"][0]["sha256"]


def test_run_appends_one_line_per_run_to_the_history(tmp_path):
    proj = _project(tmp_path)
    for _ in range(3):
        runprov.Run("t", project=proj).write(tmp_path / "t_provenance.json")
    lines = (tmp_path / "runs.jsonl").read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 3
    first = json.loads(lines[0])
    assert first["run_id"] == "test_run" and first["generation"] == "test_gen"
    assert first["provenance_path"].endswith("t_provenance.json"), (
        "without this an archiver can find every artifact except the run's own provenance"
    )


def test_run_resolves_the_calling_script_not_the_package_file(tmp_path):
    """The bug promotion would otherwise have introduced. `_provenance.py` computed
    script_sha256 from its OWN directory, which held only while it sat beside its callers;
    moving the module would have turned it into None for every script, silently."""
    run = runprov.Run("t", project=_project(tmp_path))
    resolved = pathlib.Path(run.record["code"]["script_file"])
    assert resolved == pathlib.Path(__file__).resolve()
    assert run.record["code"]["script_sha256"] == runprov.sha256(resolved)
    assert resolved.parent != pathlib.Path(runprov.run.__file__).parent


def test_run_records_which_root_it_believed_it_had(tmp_path):
    run = runprov.Run("t", project=_project(tmp_path))
    assert run.record["code"]["project_root"] == str(tmp_path)


# --------------------------------------------------------------------- failure recording
def test_a_crashed_run_still_leaves_a_record(tmp_path):
    """THE defect this package was written to replace, which it shipped with for one
    commit. The predecessor appended only on success, so its "300 runs" was 300 COMPLETED
    runs with an unknown denominator. A `with` block closes it."""
    proj = _project(tmp_path)
    with pytest.raises(RuntimeError):
        with runprov.Run("crashy", project=proj, provenance=tmp_path / "crashy.json") as run:
            run.output(tmp_path / "result.tsv")
            raise RuntimeError("the step failed halfway, as steps do")
    rec = json.loads((tmp_path / "crashy.json").read_text(encoding="utf-8"))
    assert rec["status"] == "failed"
    assert rec["failure"]["type"] == "RuntimeError"
    assert "failed halfway" in rec["failure"]["message"]
    assert "Traceback" in rec["failure"]["traceback"]
    assert rec["outputs"][0]["kind"] == "MISSING", (
        "what the step did NOT produce is the most useful thing in a failure record"
    )


def test_the_failure_is_re_raised_never_swallowed(tmp_path):
    """A provenance module that hides an exception is strictly worse than one that
    records nothing."""
    with pytest.raises(ZeroDivisionError):
        with runprov.Run("t", project=_project(tmp_path), provenance=tmp_path / "t.json"):
            _ = 1 / 0


def test_a_failed_run_is_greppable_in_the_history(tmp_path):
    proj = _project(tmp_path)
    with pytest.raises(ValueError):
        with runprov.Run("bad", project=proj, provenance=tmp_path / "bad.json"):
            raise ValueError("nope")
    runprov.Run("good", project=proj).write(tmp_path / "good.json")
    rows = [
        json.loads(x)
        for x in (tmp_path / "runs.jsonl").read_text(encoding="utf-8").strip().splitlines()
    ]
    assert [r["status"] for r in rows] == ["failed", "ok"]
    assert rows[1]["failure"] is None


def test_a_successful_with_block_writes_once_not_twice(tmp_path):
    proj = _project(tmp_path)
    with runprov.Run("t", project=proj, provenance=tmp_path / "t.json") as run:
        run.write(tmp_path / "t.json")  # explicit write, the historic style
    assert len((tmp_path / "runs.jsonl").read_text(encoding="utf-8").strip().splitlines()) == 1


def test_status_defaults_to_ok_on_the_plain_write_path(tmp_path):
    run = runprov.Run("t", project=_project(tmp_path))
    rec = json.loads(run.write(tmp_path / "t.json").read_text(encoding="utf-8"))
    assert rec["status"] == "ok"


def test_registering_a_missing_input_names_the_script_and_the_path(tmp_path):
    """A bare FileNotFoundError out of os.stat names neither, and reads as a bug in the
    caller's own file rather than a registration that could not be honoured."""
    run = runprov.Run("stepname", project=_project(tmp_path))
    with pytest.raises(FileNotFoundError) as e:
        run.input(tmp_path / "nope.tsv")
    assert "stepname" in str(e.value) and "nope.tsv" in str(e.value)


def test_concurrent_appends_do_not_interleave(tmp_path):
    """Measured on this repository's history: median line 2,032 bytes, max 7,274, and 65
    of 1,908 lines exceed the 4,096-byte POSIX atomic-append bound. Sequential chains hid
    this; a parallel pipeline would not."""
    import concurrent.futures as cf

    proj = runprov.Project(
        root=tmp_path, run_log=tmp_path / "runs.jsonl", run_id=lambda: "r", generation=lambda: "g"
    )
    big = {"padding": "x" * 9000}  # forces a line well over the atomic bound

    def one(i):
        runprov.Run(f"s{i}", big, project=proj).write(tmp_path / f"p{i}.json")

    with cf.ThreadPoolExecutor(max_workers=8) as ex:
        list(ex.map(one, range(24)))
    lines = (tmp_path / "runs.jsonl").read_text(encoding="utf-8").strip().splitlines()
    # The count first, and with the number in the message: Windows CI failed here with
    # 23 == 24 and the interesting fact was the missing ONE, not the assertion text.
    assert len(lines) == 24, f"{24 - len(lines)} record(s) lost to interleaving"
    for ln in lines:
        json.loads(ln)  # every line must still be valid JSON
    assert len(lines[0]) > 4096


# --------------------------------------------------------------------- the continuous log
NASTY = [
    {
        "script": "s1",
        "started_utc": "2026-01-01T00:00:00Z",
        "command": "/usr/bin/python x.py --flag 'a: b' #hash",
        "cwd": None,
        "run_id": "r",
        "generation": "g",
        "git_commit": "abc",
        "status": "ok",
        "inputs": [{"path": "a: b/c#d.tsv", "sha256": "f" * 64}],
        "outputs": [{"path": "- out.tsv", "sha256": None}],
    },
    # No cwd key at all — a record written before the field existed. THIS is what broke it:
    # the renderer emitted `cwd: ?`, a bare YAML complex-key indicator, and the whole file
    # stopped parsing at line 10.
    {
        "script": "s2",
        "started_utc": "2026-01-02T00:00:00Z",
        "status": "failed",
        "failure": {"type": "ValueError", "message": "on: colon\nand a newline"},
    },
    {
        "script": "yes",
        "command": "true",
        "status": "ok",
        "generation": "2026-08-07",
        "run_id": "0755",
        "cwd": "/tmp/dir with spaces/é",
    },
]


def test_the_rendered_separator_leads_each_entry_rather_than_trailing_it():
    """L-74. Splitting `_yaml`/`_timeline` into per-record functions moved the blank line
    from between the entries to after each one, which is not the same place: the banner lost
    its separation from the first record, and both renderers grew a trailing blank line
    because the last entry emits a separator it has nothing to separate from.

    Only whitespace — both forms parse — but it is a real byte delta from a refactor, and a
    golden-file diff or a downstream `diff` against pre-refactor output fails for a reason
    that has nothing to do with content. Asserted here in both directions, so neither the
    leading blank nor the trailing one can come back unnoticed."""
    recs = [
        {"script": "a", "started_utc": "2026-01-01T00:00:00Z", "status": "ok"},
        {"script": "b", "started_utc": "2026-01-02T00:00:00Z", "status": "ok"},
    ]
    y, timeline = cli._yaml(recs), cli._timeline(recs)

    assert y.endswith('status: "ok"\n'), "the last entry must end the output"
    assert not y.endswith("\n\n"), "no trailing blank line"
    assert '\n\n- step: "a"' in y, "the banner is separated from the first entry"
    assert 'status: "ok"\n\n- step: "b"' in y, "and the entries from each other"

    assert not timeline.startswith("\n"), "the first record must not be preceded by a blank"
    assert not timeline.endswith("\n\n"), "no trailing blank line"
    assert "\n\n   2026-01-02" in timeline, "records are still separated"

    # The property that makes the placement correct rather than merely different: N records
    # carry N-1 separators, so rendering one record is one record.
    one = cli._timeline(recs[:1])
    assert one.count("\n\n") == 0 and cli._timeline(recs).count("\n\n") == 1

    yaml = pytest.importorskip("yaml")
    assert len(yaml.safe_load(y)) == 2, "and it is still one YAML list of two runs"


def test_the_streaming_renderers_lay_out_exactly_what_the_batch_ones_do(tmp_path, capsys):
    """L-74. Two producers of the same bytes: `log` streams entry by entry while `_yaml` and
    `_timeline` build the whole string. The streaming path is the one that cannot see the
    last record — which is why the separator leads — so it is the one that has to be
    compared against the batch output rather than assumed equal to it."""
    recs = [
        {"script": "a", "started_utc": "2026-01-01T00:00:00Z", "status": "ok", "run_id": "r1"},
        {"script": "b", "started_utc": "2026-01-02T00:00:00Z", "status": "ok", "run_id": "r2"},
    ]
    log = tmp_path / "h.jsonl"
    log.write_text("".join(json.dumps(r) + "\n" for r in recs), encoding="utf-8")

    assert cli.main(["log", "--log", str(log), "--format", "yaml"]) == 0
    assert capsys.readouterr().out == cli._yaml(recs)

    assert cli.main(["log", "--log", str(log)]) == 0
    assert capsys.readouterr().out == cli._timeline(recs)


def test_rendered_yaml_parses_with_nasty_values():
    """The renderer must not hand-roll quoting. The predecessor's log is unreadable —
    `yaml.safe_load_all` raises at line 14,575 — because its writer was correct only for
    the values its author thought of, and this renderer reproduced that defect exactly
    before this test existed."""
    yaml = pytest.importorskip("yaml")
    docs = yaml.safe_load(cli._yaml(NASTY))
    assert len(docs) == 3
    assert docs[0]["step"] == "s1"
    assert docs[2]["run_id"] == "0755", "an octal-looking id must stay a string"
    assert docs[2]["generation"] == "2026-08-07", "a date-looking value must stay a string"
    assert docs[0]["cwd"] == "" and "cwd" in docs[1]


def test_rendered_yaml_keeps_the_field_names_of_the_log_it_replaces():
    """`step`, `input`, `output`, `run_command`, `date` — so anyone who can read the old
    transformation log can read this one. What changed is where the values come from."""
    yaml = pytest.importorskip("yaml")
    d = yaml.safe_load(cli._yaml(NASTY))[0]
    for k in (
        "step",
        "date",
        "input",
        "output",
        "run_command",
        "cwd",
        "run_id",
        "generation",
        "git_commit",
        "status",
        "input_sha256",
        "output_sha256",
    ):
        assert k in d, k


# The four fields the transformation log carried that the rendered view did not. Each is
# recorded — `script_file` and the env snapshot in the sidecar, `parameters` and `notes` in
# the history line — so this is a rendering gap, not a capture gap.
COMPAT = [
    {
        "script": "annotate_segmentation_status_fuzzy",
        "script_file": "src/segmentation/fuzzy/annotate_segmentation_status_fuzzy.py",
        "started_utc": "2026-01-19T17:48:00Z",
        "status": "ok",
        # Nested, and with the value types the old log used: a list, a bool, a null.
        "parameters": {
            "inputs": ["data/a.csv"],
            "column": "sequence",
            "inplace": False,
            "min_frac": 0.2,
            "robust_min_frac_classes": None,
        },
        "notes": {"n_rows": 469747, "passed": 5001, "failed": 0},
        "environment_snapshot": {
            "path": "provenance/environments/env-4d1ef6e1838eef07.txt",
            "sha256": "4d1ef6e1838eef07" + "f" * 48,
        },
    },
    # A run recorded before these fields existed. The renderer must not invent them.
    {"script": "older_step", "started_utc": "2025-07-28T15:27:00Z", "status": "ok"},
]


def test_rendered_yaml_names_the_script_file_and_never_guesses_it():
    """The old log's `script:` was typed by hand, and in the source project it names a
    DIFFERENT file from the one `run_command:` runs — `proteins_ns5b_domains/…` against the
    `…_3utr/v65/…` that actually ran. Emitting the observed path is the whole point.

    The failure mode to guard is the tempting one: falling back to the step name when
    `script_file` is absent. That reproduces the defect being replaced — a `script:` field
    naming something that is not what ran — while looking populated.
    """
    yaml = pytest.importorskip("yaml")
    a, b = yaml.safe_load(cli._yaml(COMPAT))
    assert a["script"] == "src/segmentation/fuzzy/annotate_segmentation_status_fuzzy.py"
    assert a["step"] == "annotate_segmentation_status_fuzzy", "step stays the logical name"
    assert "script" in b, "the key must be present — a reader must not KeyError on old runs"
    assert b["script"] != b["step"], "an unrecorded script_file must never render as the step"
    assert "not recorded" in b["script"]


def test_rendered_yaml_carries_params_and_notes_under_the_old_key_names():
    """`params` and `summary` are where the old log put these, and both are real mappings
    there rather than prose. Types must survive: a bool that arrives as the string "False"
    is a different fact."""
    yaml = pytest.importorskip("yaml")
    a = yaml.safe_load(cli._yaml(COMPAT))[0]
    assert a["params"]["column"] == "sequence"
    assert a["params"]["inputs"] == ["data/a.csv"]
    assert a["params"]["inplace"] is False, "a bool must not be stringified"
    assert a["params"]["min_frac"] == 0.2, "a number must not be stringified"
    assert a["params"]["robust_min_frac_classes"] is None
    assert a["summary"]["n_rows"] == 469747


def test_rendered_yaml_omits_the_keys_a_run_simply_did_not_use():
    """Two different absences, deliberately rendered differently. `script:` should always
    have been knowable, so its absence is announced. Snapshots are opt-in and off by
    default, so a project that never configured them has no requirements file at all —
    stamping every entry with `requirements_file: "not configured"` is noise, not a record.
    """
    yaml = pytest.importorskip("yaml")
    a, b = yaml.safe_load(cli._yaml(COMPAT))
    assert a["requirements_file"] == "provenance/environments/env-4d1ef6e1838eef07.txt"
    assert "requirements_file" not in b
    assert "params" not in b and "summary" not in b


def test_rendered_yaml_still_parses_with_nasty_params_and_notes():
    """`q()` is total because it always json.dumps. Nested values must inherit that, not a
    second hand-rolled rule — the predecessor's log is unreadable for exactly this reason."""
    yaml = pytest.importorskip("yaml")
    rows = [
        {
            "script": "s",
            "parameters": {"a: b": "#c\nd", "quote": '"', "brace": "}{", "tab": "\t"},
            "notes": {"empty": {}, "list": [1, None, True], "unicode": "é—"},
        }
    ]
    d = yaml.safe_load(cli._yaml(rows))[0]
    assert d["params"]["a: b"] == "#c\nd"
    assert d["summary"]["list"] == [1, None, True]
    assert d["summary"]["unicode"] == "é—"


def test_history_line_carries_the_script_file_and_the_env_snapshot(tmp_path, monkeypatch):
    """The renderer can only show what the history holds. `_append_history` carried
    `script_sha256` but not the path it hashed, and the tracked-package subset but not the
    content-addressed snapshot — so both facts existed in the sidecar and died there."""
    monkeypatch.chdir(tmp_path)
    log = tmp_path / "runs.jsonl"
    runprov.configure(root=tmp_path, run_log=log, env_snapshot_dir=tmp_path / "envs")
    with runprov.Run("s", provenance=tmp_path / "p.json"):
        pass
    line = json.loads(log.read_text(encoding="utf-8").splitlines()[-1])
    assert line["script_file"], "the path, not only its hash"
    assert line["script_file"].endswith(".py")
    assert line["environment_snapshot"]["path"].endswith(".txt")


def test_the_timeline_shows_the_full_invocation():
    out = cli._timeline(NASTY)
    assert "/usr/bin/python x.py --flag 'a: b' #hash" in out
    assert "!!" in out and "ValueError" in out, "a failed run must be visible at a glance"


def test_an_unreadable_line_is_counted_not_dropped(tmp_path):
    """One corrupt line costs one record, never the file. That is the whole reason this is
    JSONL: the predecessor's writer could make the entire history unparseable."""
    p = tmp_path / "runs.jsonl"
    p.write_text(
        json.dumps({"script": "a"})
        + "\n"
        + "{not json at all\n"
        + json.dumps({"script": "b"})
        + "\n"
    )
    rows, bad = cli._load(p)
    assert [r["script"] for r in rows] == ["a", "b"]
    assert bad == 1


def test_the_history_is_one_file_appended_forever(tmp_path):
    """Created once by the first write, never rewritten — the continuity the old log had,
    which is the property worth keeping."""
    proj = _project(tmp_path)
    log = tmp_path / "runs.jsonl"
    for i in range(4):
        runprov.Run(f"s{i}", project=proj).write(tmp_path / f"p{i}.json")
        assert len(log.read_text(encoding="utf-8").strip().splitlines()) == i + 1
    first = json.loads(log.read_text(encoding="utf-8").splitlines()[0])
    assert first["script"] == "s0", "the first record must survive every later append"


def test_cli_log_reports_a_missing_history_rather_than_printing_nothing(tmp_path, capsys):
    assert cli.main(["log", "--log", str(tmp_path / "nope.jsonl")]) == 1
    assert "no run history" in capsys.readouterr().err


def test_cli_log_filters(tmp_path, capsys):
    p = tmp_path / "runs.jsonl"
    p.write_text("\n".join(json.dumps(r) for r in NASTY) + "\n")
    cli.main(["log", "--log", str(p), "--failed"])
    out = capsys.readouterr()
    assert "s2" in out.out and "s1" not in out.out
    assert "1 of 3 run(s)" in out.err and "1 FAILED" in out.err


# --------------------------------------------------------------------- the invocation
def test_the_recorded_command_is_re_runnable(tmp_path):
    """It recorded `step_review.py` — the BASENAME. The log this replaces did better:
    `/home/.../envs/hcv_genotyping_env/bin/python3 src/preprocessing/x.py --inputs ...`,
    which names the interpreter, and in a mamba/uv layout with several venvs that is most
    of the answer to 'why did this run differ'."""
    run = runprov.Run("t", project=_project(tmp_path))
    cmd = run.record["command"]
    assert sys.executable in cmd, "which interpreter ran it"
    assert run.record["argv"] == sys.argv
    assert run.record["cwd"] == str(pathlib.Path.cwd()), (
        "a relative script path in the command means nothing without this"
    )


def test_the_command_survives_arguments_that_need_quoting(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "argv", ["s.py", "--x", "a b", "--y", "it's; rm -rf /"])
    run = runprov.Run("t", project=_project(tmp_path))
    import shlex

    assert shlex.split(run.record["command"])[1:] == sys.argv


def test_the_invocation_reaches_the_history(tmp_path):
    proj = _project(tmp_path)
    runprov.Run("t", project=proj).write(tmp_path / "p.json")
    rec = json.loads((tmp_path / "runs.jsonl").read_text(encoding="utf-8"))
    assert sys.executable in rec["command"] and rec["cwd"]


# --------------------------------------------------------------------- the pin
def test_header_is_identical_across_two_runs_over_the_same_inputs(tmp_path):
    """THE regression this exists to prevent. The first version embedded the run id — a
    UTC stamp under another name — and two identical runs over identical inputs both
    reported 80 artifacts CHANGED, forever."""
    src = tmp_path / "in.tsv"
    src.write_text("id\n1\n")
    proj = _project(tmp_path)
    r1 = runprov.Run("t", project=proj)
    r1.input(src)
    r2 = runprov.Run("t", project=proj)
    r2.input(src)
    assert r1.header() == r2.header()


def test_header_carries_no_timestamp_and_no_run_id(tmp_path):
    src = tmp_path / "in.tsv"
    src.write_text("id\n1\n")
    run = runprov.Run("t", project=_project(tmp_path))
    run.input(src)
    text = run.header()
    assert "test_run" not in text, "a run id embeds a UTC stamp; it is a timestamp"
    assert run.record["started_utc"] not in text
    assert not re.search(r"\d{4}-\d{2}-\d{2}", text)


def test_header_pins_the_content_digest_not_the_raw_hash(tmp_path):
    """They must agree or an artifact downstream of a timestamped one changes forever."""
    src = tmp_path / "in.tsv"
    src.write_text("# built_utc: 2026-01-01T00:00:00Z\nid\n1\n")
    run = runprov.Run("t", project=_project(tmp_path))
    run.input(src)
    assert runprov.content_digest(src)[:16] in run.header()
    assert runprov.sha256(src)[:16] not in run.header()


def test_header_changes_when_an_input_changes(tmp_path):
    src = tmp_path / "in.tsv"
    src.write_text("id\n1\n")
    proj = _project(tmp_path)
    r1 = runprov.Run("t", project=proj)
    r1.input(src)
    before = r1.header()
    src.write_text("id\n2\n")
    r2 = runprov.Run("t", project=proj)
    r2.input(src)
    assert r2.header() != before


def test_header_names_the_absence_of_a_commit_rather_than_printing_none(tmp_path):
    """The first artifact of a new project is written before its first commit, so this is
    the pin a reader sees FIRST. `commit: None` is a value to interpret; say what it means."""
    run = runprov.Run("t", project=_project(tmp_path))  # tmp_path is not a git repo
    assert run.record["code"]["git_commit_short"] is None
    assert "commit     : NONE — no commit to name (yet)" in run.header()


def test_header_says_so_when_nothing_was_registered(tmp_path):
    """'Derived from nothing' and 'reads bypass run.input()' look identical in an
    artifact unless one of them says so."""
    run = runprov.Run("t", project=_project(tmp_path))
    assert "NONE REGISTERED" in run.header()


def test_header_comment_prefix_is_configurable(tmp_path):
    run = runprov.Run("t", project=_project(tmp_path))
    assert run.header(comment="// ").startswith("// provenance")


# --------------------------------------------------------------------- environment
def test_environment_snapshot_is_content_addressed_not_per_run(tmp_path):
    """The measured failure it replaces: the old pipeline wrote one timestamped freeze per
    invocation — 87 files holding 8 distinct environments, 1.4 MB, and 'did the
    environment change?' answerable only by diffing across filenames."""
    proj = runprov.Project(
        root=tmp_path,
        run_log=tmp_path / "runs.jsonl",
        env_snapshot_dir=tmp_path / "env",
        run_id=lambda: "r",
        generation=lambda: "g",
    )
    for i in range(5):
        runprov.Run("t", project=proj).write(tmp_path / f"t{i}.json")
    files = list((tmp_path / "env").iterdir())
    assert len(files) == 1, "five runs in one environment must produce ONE file"
    assert files[0].name.startswith("env-") and files[0].name.endswith(".txt")


def test_environment_snapshot_filename_is_the_digest_of_its_body(tmp_path):
    rec = runprov.write_snapshot(tmp_path)
    body = pathlib.Path(rec["path"]).read_text(encoding="utf-8")
    assert pathlib.Path(rec["path"]).name == f"env-{runprov.environment.digest(body)[:16]}.txt"
    assert rec["sha256"] == runprov.environment.digest(body)


def test_environment_snapshot_reports_reuse(tmp_path):
    """`reused: true` is the signal that the environment has not moved since an earlier
    run — the question the 87 timestamped files could not answer cheaply."""
    first = runprov.write_snapshot(tmp_path)
    second = runprov.write_snapshot(tmp_path)
    assert first["reused"] is False and second["reused"] is True
    assert first["path"] == second["path"]


def test_environment_snapshot_is_off_unless_configured(tmp_path):
    run = runprov.Run("t", project=_project(tmp_path))
    run.write(tmp_path / "t.json")
    assert "snapshot" not in run.record["environment"]
    assert run.environment_snapshot() is None


def test_installed_packages_reads_the_running_interpreter(tmp_path):
    """`importlib.metadata`, not `subprocess(['pip','freeze'])`. The dead helper in the
    sibling froze the pip on PATH, which in a mamba-plus-uv layout need not be the
    interpreter running the script."""
    pkgs = runprov.installed_packages()
    assert pkgs and "pytest" in {k.lower() for k in pkgs}
    assert all(v for v in pkgs.values()), "a version-less entry must read UNKNOWN, not ''"
    assert list(pkgs) == sorted(pkgs, key=str.lower)


def test_snapshot_body_records_the_interpreter_not_only_the_packages(tmp_path):
    """The same versions on a different Python are a different environment."""
    body = pathlib.Path(runprov.write_snapshot(tmp_path)["path"]).read_text(encoding="utf-8")
    assert f"# python   : {sys.version.split()[0]}" in body


# --------------------------------------------------------------------- licence notices
# THESE TESTS WERE CLAIMED IN COMMIT deff1ae AND DID NOT EXIST. An edit silently failed to
# apply, the suite still passed, and the pass count was read as confirmation. That is the
# defect this repository is entirely about -- a check asserted in prose and absent in fact
# -- committed by the person writing the checks. Added properly, and each one was made to
# FAIL before being kept.
def test_every_module_carries_the_copyright_header():
    """CeCILL-B art. 5.3.4 CREDITS requires the intellectual-property notice to travel
    with the software. A header on five files out of six is the same as no policy."""
    mods = sorted(pathlib.Path(runprov.__file__).parent.glob("*.py"))
    assert len(mods) >= 6, f"expected the whole package, found {[m.name for m in mods]}"
    for m in mods:
        # The notice spans two lines now, so join before looking. A test that reads only
        # line 0 would silently stop checking the moment the line wrapped.
        head = "\n".join(m.read_text(encoding="utf-8").split("\n", 5)[:4])
        assert head.startswith("# Copyright (c)"), m.name
        # BOTH holders. The institution holds the economic rights under CPI art. L113-9;
        # the author holds the droit moral, which French law does not permit transferring.
        # Naming one misstates the position, and a test for "some copyright line" would
        # pass on a half-finished edit -- which is how this file lost these tests once.
        assert "AP-HP" in head, m.name
        assert "Hôpital Henri-Mondor" in head, m.name
        assert "Taylor Thompson" in head, m.name
        assert "CeCILL-B" in head, m.name


def test_the_licence_text_ships_with_the_package():
    """Not a link to it. A licence a user cannot read from the artifact they received is
    not a licence they have been given."""
    pkg = pathlib.Path(runprov.__file__).parent
    lic = next((p for p in (pkg.parent / "LICENSE", pkg / "LICENSE") if p.is_file()), None)
    if lic is None:
        pytest.skip("installed wheel: LICENSE lives in dist-info, not beside the package")
    text = lic.read_text(encoding="utf-8")
    assert "CeCILL-B FREE SOFTWARE LICENSE AGREEMENT" in text
    assert "5.3.4 CREDITS" in text, "the attribution obligation must be present"


def test_citation_metadata_exists_and_names_the_affiliation():
    """A prior review named the absence of this as a reviewer's first question: how do I
    cite this, and who are the authors with their affiliations."""
    pkg = pathlib.Path(runprov.__file__).parent
    cff = next(
        (p for p in (pkg.parent / "CITATION.cff", pkg / "CITATION.cff") if p.is_file()), None
    )
    if cff is None:
        pytest.skip("installed wheel: CITATION.cff is not packaged")
    text = cff.read_text(encoding="utf-8")
    assert "cff-version:" in text and "license: CECILL-B" in text
    assert "AP-HP" in text and "Hôpital Henri-Mondor" in text
    assert "0000-0000-0000-0000" not in text, "never ship a placeholder ORCID"


# --------------------------------------------------------------------- robustness review
def test_the_package_ships_pep561_type_information():
    """Without `py.typed` every annotation in this package is INVISIBLE to a downstream
    type checker. "Fully typed" then means typed for us and untyped for every user."""
    marker = pathlib.Path(runprov.__file__).parent / "py.typed"
    assert marker.is_file(), "PEP 561 marker missing; consumers get no types"


def test_every_record_declares_its_schema(tmp_path):
    """A provenance format with no version cannot be read defensively by anything -- a
    script, a dashboard, or an agent reading the history has to guess from which keys
    happen to be present."""
    sink = runprov.MemorySink()
    proj = runprov.Project(root=tmp_path, sink=sink, run_id=lambda: "r", generation=lambda: "g")
    run = runprov.Run("t", project=proj)
    assert run.record["schema"] == runprov.SCHEMA == "runprov.run.v2"
    run.write(tmp_path / "p.json")
    # The history line declares its OWN name. It asserted `== runprov.SCHEMA` until
    # 2026-08-11, which is how one string came to label two different shapes (C7): the
    # sidecar is the full record, the history line is a flattened summary, and a consumer
    # branching on the marker -- the only reason the field exists -- would have applied the
    # wrong reader to one of them.
    assert sink.records[0]["schema"] == runprov.HISTORY_SCHEMA == "runprov.history.v2"
    assert runprov.SCHEMA != runprov.HISTORY_SCHEMA


def test_the_pin_is_identical_on_two_machines_for_an_input_outside_the_root(tmp_path):
    """The pin embedded an ABSOLUTE path for an out-of-root input, so the same data pinned
    on two machines produced two different pins -- the machine-dependence class the
    encoding defect belonged to. The hash identifies the input; the path is only a label."""
    external = tmp_path / "elsewhere"
    external.mkdir()
    (external / "x.tsv").write_text("id\n1\n", encoding="utf-8")
    header_a = _header_for(tmp_path / "proj_a", external / "x.tsv")
    header_b = _header_for(tmp_path / "proj_b", external / "x.tsv")
    assert header_a == header_b
    assert str(external) not in header_a
    assert "<external>/x.tsv" in header_a


def _header_for(root: pathlib.Path, src: pathlib.Path) -> str:
    root.mkdir(parents=True, exist_ok=True)
    proj = runprov.Project(
        root=root, sink=runprov.MemorySink(), run_id=lambda: "r", generation=lambda: "g"
    )
    run = runprov.Run("t", project=proj)
    run.input(src)
    return run.header()


def _dir_input_history(tmp_path):
    """A run reading one DIRECTORY and one plain file, writing one artifact."""
    proj = _project(tmp_path)
    d = tmp_path / "refdir"
    d.mkdir()
    (d / "a.txt").write_text("original\n", encoding="utf-8")
    (tmp_path / "plain.tsv").write_text("id\n1\n", encoding="utf-8")
    with runprov.Run("s", project=proj, provenance=tmp_path / "p.json") as run:
        run.input(d)
        run.input(tmp_path / "plain.tsv")
        (out := run.output(tmp_path / "art.tsv")).write_text("x\n", encoding="utf-8")
    assert out.is_file()
    return [json.loads(x) for x in (tmp_path / "runs.jsonl").read_text().splitlines()]


def _one_state(rows, **kw):
    return next(iter(runprov.show.staleness(rows, **kw).values()))


def test_staleness_does_not_keep_the_records_it_streams(tmp_path, monkeypatch):
    """L-37. The loop streams — one history line at a time, never the file — and then kept
    the WHOLE record per artifact, which put the history back in memory by the side door:
    parameters, notes, every input and output, the git state and the terminal log, retained
    so four values could be read back later. Measured on 40,000 runs: 4.4 MB with 2,000
    distinct artifacts, 78.1 MB with 40,000.

    One artifact per run is not a pathological shape — it is what a script writing one
    output does, which over the years this history is meant to survive is the normal case.

    MEASURED DURING THE WALK, for the reason L-04's test documents: `producer` is a local
    that dies with the frame, so counting after `staleness` returns proves nothing and the
    retaining version passes."""
    import gc
    import weakref

    class Rec(dict):  # plain dicts cannot be weak-referenced; a subclass can
        pass

    seen: list[weakref.ref] = []
    held: list[int] = []
    n = 200

    def stream():
        for i in range(n):
            rec = Rec(
                script="s",
                run_uid=f"{i:032x}",
                cwd=str(tmp_path),
                provenance_path=str(tmp_path / f"p{i}.json"),
                parameters={"pad": "x" * 200},
                outputs=[{"path": f"out_{i}.tsv", "sha256": "1" * 64}],
            )
            seen.append(weakref.ref(rec))
            yield rec
            del rec
            if i == n - 1:
                gc.collect()
                held.append(sum(1 for w in seen if w() is not None))

    runprov.show.staleness(stream())
    assert held[0] <= 2, f"{held[0]} of {n} whole records held to read four fields from them"


def test_the_sidecar_cache_survives_not_keeping_the_records(tmp_path, monkeypatch):
    """The cache was keyed on `id(rec)`, which worked ONLY because every record was being
    kept alive — CPython reuses an id once an object is freed, so the moment the records
    stopped being retained two runs could collide on one id and the second would silently
    read the first's sidecar. Keyed on the run instead: two runs writing the same sidecar
    path with different uids must not share an answer."""
    monkeypatch.chdir(tmp_path)
    proj = _project(tmp_path)
    (tmp_path / "in.tsv").write_text("id\n1\n", encoding="utf-8")

    # Two runs, the SAME provenance path — the second overwrites the first's sidecar.
    for k in range(2):
        with runprov.Run(f"s{k}", project=proj, provenance=tmp_path / "shared.json") as run:
            run.input(tmp_path / "in.tsv")
            (tmp_path / f"out_{k}.tsv").write_text(f"{k}\n", encoding="utf-8")
            run.output(tmp_path / f"out_{k}.tsv")

    rows = [json.loads(x) for x in (tmp_path / "runs.jsonl").read_text().splitlines()]
    states = {pathlib.Path(k).name: v for k, v in runprov.show.staleness(rows).items()}
    # The FIRST run's sidecar was overwritten, so its artifact cannot be judged and says so.
    # The second's is still its own and verifies. Sharing one cache entry would give both the
    # same answer, which is the confident nonsense `_sidecar`'s run_uid check exists to stop.
    assert states["out_0.tsv"] == runprov.show.UNKNOWN, states
    assert states["out_1.tsv"] == runprov.show.CURRENT, states


def _shared_input_history(tmp_path, artifacts=6, inputs=3):
    """`artifacts` runs, each reading ALL of `inputs` shared files. Returns the records."""
    proj = _project(tmp_path)
    (tmp_path / "data").mkdir(exist_ok=True)
    shared = []
    for i in range(inputs):
        (p := tmp_path / "data" / f"in_{i}.tsv").write_text(f"id\n{i}\n", encoding="utf-8")
        shared.append(p)
    for k in range(artifacts):
        with runprov.Run(f"s{k}", project=proj, provenance=tmp_path / f"p{k}.json") as run:
            for p in shared:
                run.input(p)
            (tmp_path / f"out_{k}.tsv").write_text("x\n", encoding="utf-8")
            run.output(tmp_path / f"out_{k}.tsv")
    return [json.loads(x) for x in (tmp_path / "runs.jsonl").read_text().splitlines()]


def test_rehash_reads_each_distinct_file_once_across_the_whole_page(tmp_path, monkeypatch):
    """L-20. The stat path deliberately caches — `detailed[key]`, with a docstring and a test
    saying "the sidecar is read per RUN, not per artifact". The rehash path had no cache of
    any kind, so a shared input was re-read once per artifact that used it. Measured on 200
    artifacts drawing from 10 shared inputs: 2,200 reads over 210 distinct files, one input
    read 200 times.

    `--rehash` is documented as the answer for anyone who cannot accept the stat check's
    resolution limit — the mode you reach for when correctness matters — so it is the worst
    place for the redundancy. On 20-byte fixtures it is syscall overhead; on a 2 GB input
    shared by 300 artifacts it is 600 GB of reads for one page."""
    monkeypatch.chdir(tmp_path)
    rows = _shared_input_history(tmp_path, artifacts=6, inputs=3)

    calls = []
    real = runprov.show._digest_now
    monkeypatch.setattr(
        runprov.show, "_digest_now", lambda p: (calls.append(pathlib.Path(p)), real(p))[1]
    )
    states = runprov.show.staleness(rows, rehash=True)

    assert set(states.values()) == {runprov.show.CURRENT}, "the premise: nothing has moved"
    assert len(calls) == len(set(calls)), (
        f"{len(calls)} reads over {len(set(calls))} distinct files — one each is the point"
    )
    assert len(set(calls)) == 3 + 6, "three shared inputs plus one artifact per run"


def test_the_rehash_cache_does_not_change_what_is_reported(tmp_path, monkeypatch):
    """A memo that alters the verdict is worse than the cost it saves: every artifact sharing
    a moved input must still go STALE, from a single re-read of it."""
    monkeypatch.chdir(tmp_path)
    rows = _shared_input_history(tmp_path, artifacts=4, inputs=2)
    assert set(runprov.show.staleness(rows, rehash=True).values()) == {runprov.show.CURRENT}

    (tmp_path / "data" / "in_0.tsv").write_text("id\n0\n1\n2\n", encoding="utf-8")
    states = runprov.show.staleness(rows, rehash=True)
    assert set(states.values()) == {runprov.show.STALE}, f"all four, from one re-read: {states}"


def test_a_rewritten_directory_input_is_not_reported_as_current(tmp_path, monkeypatch):
    """L-16. `moved_since` returns None for anything that is not a plain file, and None
    means "did not move" — so a directory input read as EVIDENCE OF FRESHNESS. A directory's
    size and mtime belong to its inode: they move when an entry is added or removed and stay
    exactly where they were when a file inside is edited in place.

    `?`, not `current` and not STALE. We did not look, and saying so is the argument this
    package makes everywhere else. `--rehash` re-derives the tree hash and gets it right."""
    monkeypatch.chdir(tmp_path)
    rows = _dir_input_history(tmp_path)
    (tmp_path / "refdir" / "a.txt").write_text("TOTALLY DIFFERENT\n", encoding="utf-8")

    assert _one_state(rows) == runprov.show.UNKNOWN, "stat cannot speak for a directory"
    assert _one_state(rows, rehash=True) == runprov.show.STALE, "rehash can, and does"


def test_a_definite_finding_still_beats_the_unknown_from_a_directory(tmp_path, monkeypatch):
    """`?` is the absence of a finding, so anything established must outrank it — otherwise
    adding a directory input would silently hide a moved file or a hand-edited artifact."""
    monkeypatch.chdir(tmp_path)

    rows = _dir_input_history(tmp_path)
    (tmp_path / "plain.tsv").write_text("id\n1\n2\n3\n4\n", encoding="utf-8")
    assert _one_state(rows) == runprov.show.STALE, "a moved FILE input is still STALE"

    shutil.rmtree(tmp_path / "refdir")
    for name in ("runs.jsonl", "p.json", "art.tsv", "plain.tsv"):
        (tmp_path / name).unlink()
    rows = _dir_input_history(tmp_path)
    (tmp_path / "art.tsv").write_text("EDITED BY HAND ENTIRELY\n", encoding="utf-8")
    assert _one_state(rows) == runprov.show.MODIFIED, "a rewritten ARTIFACT is still MODIFIED"


def test_the_history_carries_kind_when_it_is_a_finding(tmp_path):
    """L-15. `kind` is how an entry says MISSING (registered, never written), UNHASHABLE
    (present, no digest) or directory. `_append_history` dropped it, so
    `staleness`'s `entry.get("kind") == "MISSING"` guard was dead for exactly the records
    `show` reads — the history — and an artifact the run never wrote could read as `current`
    once something else created that path.

    `"file"` is still omitted, deliberately: it is the ordinary case, this file is appended
    to forever, and `moved_since` already reads an absent kind as a file. Writing it would
    add a constant to every entry of every line to say "nothing to report"."""
    proj = _project(tmp_path)
    (tmp_path / "in.tsv").write_text("id\n1\n", encoding="utf-8")
    (tmp_path / "dir").mkdir()
    (tmp_path / "dir" / "a.txt").write_text("a\n", encoding="utf-8")

    with runprov.Run("s", project=proj, provenance=tmp_path / "p.json") as run:
        run.input(tmp_path / "in.tsv")
        run.input(tmp_path / "dir")
        run.output(tmp_path / "never.tsv")  # registered and never written
        (ok := run.output(tmp_path / "real.tsv")).write_text("x\n", encoding="utf-8")

    rec = json.loads((tmp_path / "runs.jsonl").read_text(encoding="utf-8").splitlines()[0])
    by_name = {pathlib.Path(e["path"]).name: e for e in rec["inputs"] + rec["outputs"]}
    assert by_name["never.tsv"]["kind"] == "MISSING", "a run that produced nothing must say so"
    assert by_name["dir"]["kind"] == "directory", "a directory input cannot be stat-checked"
    assert "kind" not in by_name["in.tsv"], "an ordinary file adds no key to a forever file"
    assert "kind" not in by_name["real.tsv"]
    assert ok.name == "real.tsv"


def _run_thrice(tmp_path, **projkw):
    """Three real runs under one project. Returns the project."""
    proj = runprov.Project(
        root=tmp_path,
        run_log=tmp_path / "provenance" / "runs.jsonl",
        run_id=lambda: "r",
        generation=lambda: "g",
        **projkw,
    )
    (tmp_path / "in.tsv").write_text("id\tv\n1\ta\n", encoding="utf-8")
    for i, mode in enumerate(("a", "b", "c")):
        with runprov.Run(
            "summarise", {"mode": mode}, project=proj, provenance=tmp_path / f"p{i}.prov.json"
        ) as run:
            run.input(tmp_path / "in.tsv")
            with run.open_output(tmp_path / f"out_{i}.tsv") as fh:
                fh.write("id\tv\n1\ta\n")
            run.note("rows_kept", i * 10)
    return proj


def test_a_project_keeps_a_readable_transformation_log_beside_the_history(tmp_path):
    """The file this package exists because of, written the way that file should have been.
    `transformation_log.yml` is what a person opens to read the story of a project — and the
    predecessor's copy is also the file that stopped parsing at line 14,547 of 24,300 and
    grew eight repair scripts around it.

    So: a VIEW, appended in step with the history, never the record of truth. Every scalar
    is quoted, which is exactly the defect that killed the original — it quoted only what
    its author thought needed quoting, and one hand-typed `Note:` ended the file."""
    yaml = pytest.importorskip("yaml")
    _run_thrice(tmp_path)

    log = tmp_path / "provenance" / "transformation_log.yml"
    assert log.is_file(), "written beside runs.jsonl without being asked for"

    entries = yaml.safe_load(log.read_text(encoding="utf-8"))
    assert isinstance(entries, list) and len(entries) == 3, "one entry per run, in one list"
    assert [e["step"] for e in entries] == ["summarise"] * 3
    assert [e["params"]["mode"] for e in entries] == ["a", "b", "c"], "in order, all three"
    # TYPED, not stringified: `params` and `summary` are structures, and `date` is a string
    # rather than a bare YAML timestamp. Both are why every scalar is quoted.
    assert entries[2]["summary"] == {"rows_kept": 20}
    assert isinstance(entries[0]["date"], str)

    history = (tmp_path / "provenance" / "runs.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(history) == 3, "the record of truth is unchanged and still one line per run"


def test_the_transformation_log_says_it_is_not_the_record(tmp_path):
    """A banner that names the wrong origin is a small lie in the first line of the file.
    This one is maintained continuously; `log --format yaml` renders on demand. A reader
    asking how the file in front of them came to exist is asking a real question."""
    _run_thrice(tmp_path)
    head = (tmp_path / "provenance" / "transformation_log.yml").read_text(encoding="utf-8")
    assert "MAINTAINED by runprov" in head and "one entry appended per run" in head
    assert "VIEW" in head and "record of truth is the run history" in head
    assert "log --format yaml" in head, "and how to rebuild it"


def test_each_artifact_gets_both_a_json_and_a_yaml_sidecar(tmp_path):
    """The JSON is what `verify` and every other tool reads; the YAML is what a person opens
    beside an artifact. Written from the same record in the same call, so they cannot drift
    — which is the failure mode a `--format` flag on a reader would have instead."""
    yaml = pytest.importorskip("yaml")
    _run_thrice(tmp_path)

    for i in range(3):
        js, yml = tmp_path / f"p{i}.prov.json", tmp_path / f"p{i}.prov.yml"
        assert js.is_file() and yml.is_file(), f"both for run {i}"
        assert (
            yaml.safe_load(yml.read_text(encoding="utf-8"))["run_uid"]
            == json.loads(js.read_text(encoding="utf-8"))["run_uid"]
        ), "the same run, from the same record"


def test_the_yaml_sidecar_does_not_eat_a_compound_suffix(tmp_path):
    """`summary.prov.json` -> `summary.prov.yml`, and `calls.v2.json` -> `calls.v2.yml`.
    `Path.with_suffix` would turn `calls.v2.json` into `calls.yml`, eating `.v2` — the
    compound-suffix bug `_sidecar_name` already had to learn once."""
    proj = _project(tmp_path)
    with runprov.Run("s", project=proj, provenance=tmp_path / "calls.v2.json"):
        pass
    assert (tmp_path / "calls.v2.yml").is_file(), sorted(p.name for p in tmp_path.iterdir())
    assert not (tmp_path / "calls.yml").exists(), "the version segment survives"


def test_both_extra_views_can_be_turned_off(tmp_path):
    """They are conveniences, and a convenience that cannot be declined is a tax. Turning
    them off must leave the record itself untouched."""
    proj = _run_thrice(tmp_path, write_transformation_log=False, write_yaml_sidecar=False)
    assert not (tmp_path / "provenance" / "transformation_log.yml").exists()
    assert not (tmp_path / "p0.prov.yml").exists()
    assert (tmp_path / "p0.prov.json").is_file(), "the record is not a view"
    assert len((tmp_path / "provenance" / "runs.jsonl").read_text().splitlines()) == 3
    assert proj.write_transformation_log is False


def test_a_supplied_sink_is_the_whole_story(tmp_path):
    """The extension point exists so a lab can send records to one shared database, and
    quietly writing a YAML file next to it would be this package deciding where somebody
    else's records live."""
    sink = runprov.MemorySink()
    proj = runprov.Project(root=tmp_path, sink=sink, run_id=lambda: "r", generation=lambda: "g")
    with runprov.Run("s", project=proj, provenance=tmp_path / "p.prov.json"):
        pass
    assert len(sink.records) == 1
    assert not (tmp_path / "provenance" / "transformation_log.yml").exists()


def test_a_second_write_does_not_double_count_the_run(tmp_path):
    """One run is one history line, or every count taken from the history is wrong."""
    sink = runprov.MemorySink()
    proj = runprov.Project(root=tmp_path, sink=sink, run_id=lambda: "r", generation=lambda: "g")
    run = runprov.Run("t", project=proj)
    run.write(tmp_path / "a.json")
    run.write(tmp_path / "b.json")
    assert len(sink.records) == 1
    assert (tmp_path / "a.json").is_file() and (tmp_path / "b.json").is_file()


def test_writing_a_second_path_does_not_cost_the_one_the_constructor_named(tmp_path):
    """L-02. `write()`'s docstring calls a second path "fine and sometimes useful" — a
    promise of TWO records, not a swap of one for the other. `__exit__` corrected only the
    LAST path written, so `write(other)` inside the block left the `provenance=` file never
    created at all: the path every document calls the guarantee, the only one armed for a run
    that dies, absent because the caller also wrote somewhere else."""
    log, proj = _sinked(tmp_path)
    with runprov.Run("t", project=proj, provenance=tmp_path / "primary.json") as run:
        run.write(tmp_path / "second.json")

    assert (tmp_path / "primary.json").is_file(), "the constructor's sidecar must exist"
    assert (tmp_path / "second.json").is_file(), "and so must the one write() named"
    assert len(log.records) == 1, "two sidecars are still one run and one history line"


def test_every_sidecar_a_run_wrote_carries_the_final_status(tmp_path):
    """The other half. A sidecar written mid-block holds an optimistic snapshot: the status
    is not known until `__exit__`. Correcting only the last one left the earlier files saying
    `ok` beside a run that failed — two sidecars for one run, disagreeing, with nothing on
    either to say which was corrected."""
    log, proj = _sinked(tmp_path)
    with contextlib.suppress(ValueError):
        with runprov.Run("t", project=proj, provenance=tmp_path / "primary.json") as run:
            run.write(tmp_path / "second.json")
            raise ValueError("boom")

    for name in ("primary.json", "second.json"):
        rec = json.loads((tmp_path / name).read_text(encoding="utf-8"))
        assert rec["status"] == "failed", f"{name} still claims the run succeeded"
        assert rec["failure"]["type"] == "ValueError"
    assert log.records[0]["status"] == "failed"


def test_one_file_named_two_ways_is_one_sidecar(tmp_path):
    """Spelling is not identity. `provenance=` and a later `write()` naming the same file
    through a different spelling must not count as two, or the run reports two sidecars where
    there is one and writes the same bytes twice."""
    log, proj = _sinked(tmp_path)
    (tmp_path / "out").mkdir()
    with runprov.Run("t", project=proj, provenance=tmp_path / "out" / "p.json") as run:
        run.write(tmp_path / "out" / ".." / "out" / "p.json")

    assert (tmp_path / "out" / "p.json").is_file()
    assert len(run._written_paths) == 1, f"counted {len(run._written_paths)} paths for one file"
    assert len(log.records) == 1


@requires_symlinks
def test_a_sidecar_path_through_a_symlink_loop_does_not_end_the_run(tmp_path):
    """L-02, the guard under it. `Path.resolve()` raises `RuntimeError` for a cyclic link,
    not `OSError` — so an `except OSError` around it reads as careful and is not, which is
    L-01's defect one exception family over. A run whose `provenance=` points through such a
    link must still record; the sidecar is what is lost, and it says so."""
    (tmp_path / "a").symlink_to(tmp_path / "b")
    (tmp_path / "b").symlink_to(tmp_path / "a")
    _assert_symlink_loop(tmp_path / "a")  # the premise, asserted rather than assumed

    log, proj = _sinked(tmp_path)
    with runprov.Run("t", project=proj, provenance=tmp_path / "a" / "p.json"):
        pass
    assert len(log.records) == 1, "the history line survives a sidecar path that cannot resolve"


def test_a_second_write_does_not_double_the_outputs_inside_the_record(tmp_path):
    """The other half of the test above, and the half that was wrong.

    Counting history LINES stayed correct while what was inside them did not: `outputs` was
    appended to on every `write()`, so a second call — which `write()`'s own docstring calls
    legitimate, for a caller wanting the record at a second path — recorded one artifact
    twice. Inside a `with` block the history line is deferred to `__exit__`, so it reads the
    already-doubled list and the doubling reaches the append-only history, where any tally
    over artifacts produced is then silently inflated for the life of the file.
    """
    sink = runprov.MemorySink()
    proj = runprov.Project(root=tmp_path, sink=sink, run_id=lambda: "r", generation=lambda: "g")

    run = runprov.Run("t", project=proj)
    (out := run.output(tmp_path / "o.tsv")).write_text("id\n1\n", encoding="utf-8")
    run.write(tmp_path / "a.json")
    run.write(tmp_path / "b.json")  # the second path this method's docstring invites

    for name in ("a.json", "b.json"):
        rec = json.loads((tmp_path / name).read_text(encoding="utf-8"))
        paths = [o["path"] for o in rec["outputs"]]
        assert paths == [str(out)], f"{name} recorded {len(paths)} entries for one file"

    # The history line is the one that is permanent, and inside a `with` block it is
    # deferred to `__exit__` — so it read the list AFTER every write() had appended to it.
    sink2 = runprov.MemorySink()
    proj2 = runprov.Project(root=tmp_path, sink=sink2, run_id=lambda: "r", generation=lambda: "g")
    with runprov.Run("t", project=proj2, provenance=tmp_path / "c.json") as run2:
        (out2 := run2.output(tmp_path / "p.tsv")).write_text("id\n1\n", encoding="utf-8")
        run2.write(tmp_path / "d.json")

    assert len(sink2.records) == 1, "still exactly one line for the run"
    assert [o["path"] for o in sink2.records[0]["outputs"]] == [str(out2)], (
        "the append-only history must not carry one artifact twice"
    )


def test_a_custom_sink_receives_the_records(tmp_path):
    """The one extension point: where records go. A lab pointing many pipelines at one
    store must not have to fork the package."""
    sink = runprov.MemorySink()
    proj = runprov.Project(root=tmp_path, sink=sink, run_id=lambda: "r", generation=lambda: "g")
    for i in range(3):
        runprov.Run(f"s{i}", project=proj).write(tmp_path / f"p{i}.json")
    assert [r["script"] for r in sink.records] == ["s0", "s1", "s2"]
    assert not (tmp_path / "provenance" / "runs.jsonl").exists(), (
        "a custom sink must REPLACE the default, not write to both"
    )


def test_a_sink_without_append_is_refused_at_configuration(tmp_path):
    """At configure(), not at the end of a two-hour run when the work is already done."""

    class NotASink:
        pass

    with pytest.raises(TypeError, match="RecordSink"):
        runprov.configure(root=tmp_path, sink=NotASink())
    runprov.configure(root=tmp_path)  # restore a sane active project


def test_content_digest_is_streamed_not_read_whole(tmp_path):
    """The module docstring promised streaming for multi-GB inputs. It was true of
    `sha256` and false of `content_digest`, which is the one called on every input."""
    import ast
    import inspect

    # Parse and drop the docstring before looking. The first version of this test matched
    # the prose in the docstring -- which mentions `read_text()` precisely because it
    # explains why the function no longer calls it -- and failed on a correct
    # implementation. A test that reads comments is testing the comments.
    tree = ast.parse(textwrap.dedent(inspect.getsource(runprov.content_digest)))
    fn = tree.body[0]
    assert isinstance(fn, ast.FunctionDef)
    body = fn.body[1:] if ast.get_docstring(fn) else fn.body
    code = "\n".join(ast.unparse(node) for node in body)
    assert "read_text()" not in code and "read_bytes()" not in code, (
        "the spelling half — cheap, and it CANNOT see the defect class: `fh.read()`, "
        "`list(fh)` and `''.join(fh)` all load the whole file and all pass this line. "
        "The property is asserted below."
    )
    # and it still agrees with the whole-file definition it replaced
    p = tmp_path / "big.tsv"
    p.write_text("# built_utc: X\n" + "".join(f"{i}\ta\n" for i in range(50000)), encoding="utf-8")
    import hashlib

    body = "\n".join(
        ln for ln in p.read_text(encoding="utf-8").splitlines() if not runprov.VOLATILE.match(ln)
    )
    assert runprov.content_digest(p) == hashlib.sha256(body.encode()).hexdigest()


# ===================================================================== coverage: the CLI
def test_cli_skips_blank_lines_in_the_history(tmp_path):
    p = tmp_path / "runs.jsonl"
    p.write_text(json.dumps({"script": "a"}) + "\n\n   \n", encoding="utf-8")
    rows, bad = cli._load(p)
    assert [r["script"] for r in rows] == ["a"] and bad == 0


def test_cli_timeline_shows_seeds_when_a_run_declared_them():
    out = cli._timeline([{"script": "s", "seeds": [42, 43]}])
    assert "seeds      [42, 43]" in out


def test_the_timeline_prints_the_same_digest_width_as_the_pin():
    """L-78. The timeline's `[:16]` was never asserted — timeline tests check the script
    name, run_id, seeds and counts, never the rendered digest — so narrowing it to `[:8]`
    left the whole suite green.

    Sixteen is the width `show`, the PIN and `verify` all agree on, precisely so a digest
    read in one view can be matched by eye against a digest read in another. A drift breaks
    that silently: nothing errors, the columns simply stop lining up with the artifact's own
    pin, and the reader concludes the file is not the one they made.

    So the timeline now slices with `SHORT` rather than a third literal 16, and the
    agreement between `SHORT` and `PIN_DIGEST_CHARS` is asserted rather than assumed —
    two constants that must be equal, in different modules, are a drift waiting to happen."""
    digest = "0123456789abcdef" + "f" * 48
    assert len(digest) == 64, "the premise: a full sha256, which is what a record carries"

    out = cli._timeline(
        [{"script": "s", "inputs": [{"path": "in.tsv", "sha256": digest}],
          "outputs": [{"path": "out.tsv", "sha256": digest}]}]
    )  # fmt: skip

    assert out.count("0123456789abcdef") == 2, "both the input and the output line"
    assert "0123456789abcdeff" not in out, "17 characters would be a wider handle"
    assert runprov.show.SHORT == runprov.hashing.PIN_DIGEST_CHARS, (
        f"the views disagree: show renders {runprov.show.SHORT} characters and the pin "
        f"carries {runprov.hashing.PIN_DIGEST_CHARS} — a digest read in one cannot be "
        f"matched against a digest read in the other"
    )


def test_cli_filters_by_run_id(tmp_path, capsys):
    p = tmp_path / "runs.jsonl"
    p.write_text(
        "\n".join(
            json.dumps(r) for r in [{"script": "a", "run_id": "x"}, {"script": "b", "run_id": "y"}]
        ),
        encoding="utf-8",
    )
    cli.main(["log", "--log", str(p), "--run-id", "y"])
    out = capsys.readouterr()
    assert "b" in out.out and "1 of 2" in out.err


def test_cli_limit_takes_the_last_n(tmp_path, capsys):
    p = tmp_path / "runs.jsonl"
    p.write_text("\n".join(json.dumps({"script": f"s{i}"}) for i in range(5)), encoding="utf-8")
    cli.main(["log", "--log", str(p), "--limit", "2"])
    out = capsys.readouterr().out
    assert "s4" in out and "s3" in out and "s0" not in out


def test_cli_yaml_and_jsonl_formats(tmp_path, capsys):
    p = tmp_path / "runs.jsonl"
    p.write_text(json.dumps({"script": "a", "started_utc": "T"}) + "\n", encoding="utf-8")
    cli.main(["log", "--log", str(p), "--format", "yaml"])
    assert "- step:" in capsys.readouterr().out
    cli.main(["log", "--log", str(p), "--format", "jsonl"])
    assert json.loads(capsys.readouterr().out.strip())["script"] == "a"


def test_cli_rejects_an_unknown_step():
    with pytest.raises(SystemExit):
        cli.main(["log", "--format", "nonsense"])


# ============================================================ coverage: the environment
def test_a_distribution_with_unreadable_metadata_is_counted_not_dropped(monkeypatch, tmp_path):
    """A snapshot silently missing an entry reads as complete, which is the worst outcome
    for a record whose only job is to be complete."""

    class Broken:
        _path = "/somewhere/broken.dist-info"

        @property
        def metadata(self):
            raise RuntimeError("unreadable")

    class Nameless:
        _path = "/somewhere/nameless.dist-info"
        metadata = {"Name": "   "}
        version = "1.0"

    import importlib.metadata as md

    monkeypatch.setattr(md, "distributions", lambda: [Broken(), Nameless()])
    bad = []
    pkgs = runprov.installed_packages(bad)
    assert pkgs == {} and len(bad) == 2
    assert "UNREADABLE: 2" in runprov.environment.render(pkgs, len(bad))


def test_a_version_that_cannot_be_read_is_recorded_as_unknown(monkeypatch):
    class NoVersion:
        metadata = {"Name": "thing"}
        version = None

    import importlib.metadata as md

    monkeypatch.setattr(md, "distributions", lambda: [NoVersion()])
    assert runprov.installed_packages() == {"thing": "UNKNOWN"}


# ================================================================= coverage: the hashing
def test_content_digest_of_a_missing_path_is_none(tmp_path):
    assert runprov.content_digest(tmp_path / "nope") is None


def test_a_corrupt_gzip_falls_back_to_the_raw_hash(tmp_path):
    p = tmp_path / "broken.gz"
    p.write_bytes(b"not actually gzip at all")
    assert runprov.content_digest(p) == runprov.sha256(p)


def test_a_file_of_only_volatile_lines_hashes_without_crashing(tmp_path):
    p = tmp_path / "stamps.tsv"
    p.write_text("# built_utc: A\n# generated_utc: B\n", encoding="utf-8")
    q = tmp_path / "other.tsv"
    q.write_text("# built_utc: C\n# generated_utc: D\n", encoding="utf-8")
    assert runprov.content_digest(p) == runprov.content_digest(q)


def test_binary_content_falls_back_to_the_raw_hash(tmp_path):
    p = tmp_path / "blob.bin"
    p.write_bytes(bytes(range(256)) * 4)
    assert runprov.content_digest(p) == runprov.sha256(p)


# ================================================================= coverage: the project
def test_git_on_a_path_that_is_not_a_directory_returns_none(tmp_path):
    f = tmp_path / "afile"
    f.write_text("x", encoding="utf-8")
    assert runprov.git(f, "rev-parse", "HEAD") is None


def test_default_run_id_and_generation_read_the_environment(monkeypatch):
    monkeypatch.setenv("RUNPROV_RUN_ID", "chain_123")
    monkeypatch.setenv("RUNPROV_GENERATION", "gen_x")
    assert runprov.default_run_id() == "chain_123"
    assert runprov.default_generation() == "gen_x"
    monkeypatch.delenv("RUNPROV_RUN_ID")
    monkeypatch.delenv("RUNPROV_GENERATION")
    # unset: labelled ad-hoc on purpose, so a hand-run script and a chain stage are not
    # indistinguishable in the history
    assert runprov.default_run_id().startswith("adhoc_")
    assert runprov.default_generation() == "(default)"


def test_active_builds_a_project_when_none_was_configured(monkeypatch):
    monkeypatch.setattr(runprov.project, "_ACTIVE", None)
    assert isinstance(runprov.active(), runprov.Project)


# ===================================================================== coverage: the Run
def test_seeds_and_notes_are_recorded(tmp_path):
    run = runprov.Run("t", project=_project(tmp_path))
    run.seeds([1, 2, 3])
    run.note("n_rows", 5)
    assert run.record["seeds"] == [1, 2, 3] and run.record["notes"] == {"n_rows": 5}


def test_module_records_where_an_import_resolved_from(tmp_path, capsys):
    """A commit is not sufficient provenance when two installables share a name."""
    run = runprov.Run("t", project=_project(tmp_path))
    run.module(json)
    rec = run.record["modules"][0]
    assert rec["module"] == "json" and rec["sha256"]
    assert rec["inside_project"] is False
    cap = capsys.readouterr()
    assert "resolved OUTSIDE" in cap.err and cap.out == ""


def test_module_handles_something_without_a_file(tmp_path):
    run = runprov.Run("t", project=_project(tmp_path))
    run.module(sys)  # a built-in has no __file__
    assert run.record["modules"][0]["resolved_file"] is None


def test_the_same_output_registered_twice_is_recorded_once(tmp_path):
    run = runprov.Run("t", project=_project(tmp_path))
    p = run.output(tmp_path / "one.tsv")
    run.output(tmp_path / "one.tsv")
    p.write_text("x", encoding="utf-8")
    rec = json.loads(run.write(tmp_path / "prov.json").read_text(encoding="utf-8"))
    assert len(rec["outputs"]) == 1


def test_an_unwritable_snapshot_directory_warns_and_does_not_break_the_run(tmp_path, capsys):
    blocker = tmp_path / "blocked"
    blocker.write_text("I am a file, not a directory", encoding="utf-8")
    proj = runprov.Project(
        root=tmp_path,
        sink=runprov.MemorySink(),
        env_snapshot_dir=blocker / "sub",
        run_id=lambda: "r",
        generation=lambda: "g",
    )
    run = runprov.Run("t", project=proj)
    rec = run.environment_snapshot()
    assert rec is not None and "error" in rec
    cap = capsys.readouterr()
    assert "could not write environment snapshot" in cap.err and cap.out == ""


def test_a_dirty_tree_is_announced(tmp_path, capsys):
    """The warning is the point: a commit that does not identify what ran must say so."""
    import subprocess

    repo = tmp_path / "r"
    (repo / "src").mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "t@e.org"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "T"], check=True)
    (repo / "src" / "a.py").write_text("x = 1\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", "one"], check=True)
    (repo / "src" / "a.py").write_text("x = 2\n", encoding="utf-8")
    runprov.Run("t", project=runprov.Project(root=repo, sink=runprov.MemorySink()))
    cap = capsys.readouterr()
    assert "CODE is modified relative to git_commit" in cap.err and cap.out == ""


# =========================================================== terminal capture (the tee)
def test_capture_records_a_subprocess_and_still_reaches_the_terminal(tmp_path, monkeypatch):
    """THE POINT OF fd-LEVEL CAPTURE, and the reason python-level would not do.

    A subprocess writes to file descriptor 1 directly and never touches `sys.stdout`, so a
    tee that swaps the Python stream records an EMPTY FILE THAT LOOKS LIKE A LOG. This
    repository's main consumer is a harness that wraps sibling scripts as subprocesses, so
    that failure would be silent and total.

    The second assertion is the one that keeps this honest: capture must COPY, never
    divert. If the child's output stopped reaching the real stdout, this package would be
    doing the thing `_report.py` exists to prevent.

    The ordinary-python half is written through a dup of fd 1 rather than with `print()`,
    because under pytest's capture `sys.stdout` is pytest's own buffer and never reaches
    fd 1 at all — a `print()` here would prove nothing about fd capture either way. The
    python-level path is covered separately, where `print()` IS the right instrument.
    """
    monkeypatch.chdir(tmp_path)
    log = tmp_path / "t.log"
    runprov.configure(root=tmp_path, run_log=tmp_path / "runs.jsonl")
    sink = tmp_path / "real_stdout.txt"
    with open(sink, "w", encoding="utf-8") as fh:
        saved = os.dup(1)
        os.dup2(fh.fileno(), 1)  # the "terminal" for this test, so we can read it back
        try:
            with runprov.Run("s", provenance=tmp_path / "p.json", terminal_log=log) as run:
                subprocess.run([sys.executable, "-c", "print('FROM-A-CHILD')"], check=True)
                with os.fdopen(os.dup(1), "w", closefd=True) as direct:
                    direct.write("FROM-PYTHON\n")
        finally:
            os.dup2(saved, 1)
            os.close(saved)
    body = log.read_text(encoding="utf-8")
    assert "FROM-A-CHILD" in body, "a python-level tee cannot see this — fd capture must"
    assert "FROM-PYTHON" in body
    assert run.record["terminal_log"]["capture"] == "fd"
    passed_through = sink.read_text(encoding="utf-8")
    assert "FROM-A-CHILD" in passed_through and "FROM-PYTHON" in passed_through, (
        "capture must TEE, never divert — the output still belongs to the caller"
    )


def test_the_captured_log_is_hashed_after_the_capture_stops(tmp_path, monkeypatch):
    """Hashing before the tee stops pins a PREFIX of the log. The recorded digest must be
    the digest of the finished file, or the one artifact describing the run is pinned to
    something that never existed on disk."""
    monkeypatch.chdir(tmp_path)
    log = tmp_path / "t.log"
    runprov.configure(root=tmp_path, run_log=tmp_path / "runs.jsonl")
    with runprov.Run("s", provenance=tmp_path / "p.json", terminal_log=log) as run:
        print("x" * 5000, flush=True)
    rec = run.record["terminal_log"]
    assert rec["sha256"] == runprov.sha256(log), "the recorded hash must match the FINAL file"
    assert rec["bytes"] == log.stat().st_size


def test_python_level_capture_says_it_cannot_see_subprocesses(tmp_path, monkeypatch):
    """The fallback must ANNOUNCE itself, in the record and not only on stderr. An empty
    log means two different things and only the mechanism distinguishes them."""
    monkeypatch.chdir(tmp_path)
    log = tmp_path / "t.log"
    cap = runprov.Capture(log)
    # The module reference inside runprov.terminal, NOT `os.dup2` itself. `runprov.terminal.os`
    # IS the os module, so patching an attribute on it patches it for the whole interpreter —
    # which broke pytest's own capture teardown when this test was first written.
    monkeypatch.setattr(runprov.terminal, "os", _NoDup2())
    cap.start()
    print("VISIBLE-TO-PYTHON")
    rec = cap.stop()
    assert cap.mode == "python"
    assert "VISIBLE-TO-PYTHON" in log.read_text(encoding="utf-8")
    assert "subprocesses is NOT included" in rec["note"]
    assert rec["capture"] == "python"


def test_a_capture_that_cannot_open_its_file_does_not_touch_the_streams(tmp_path, capsys):
    """A provenance addition must never cost the caller its stdout. If the log cannot be
    opened, nothing is swapped and the run proceeds."""
    blocker = tmp_path / "afile"
    blocker.write_text("not a directory", encoding="utf-8")
    before_out, before_err = sys.stdout, sys.stderr
    cap = runprov.Capture(blocker / "sub" / "t.log")
    cap.start()
    assert cap.mode == "none"
    assert sys.stdout is before_out and sys.stderr is before_err, "streams must be untouched"
    assert cap.stop()["capture"] == "none"
    assert "terminal capture off" in capsys.readouterr().err


def test_terminal_log_false_overrides_a_configured_directory(tmp_path, monkeypatch):
    """The per-Run switch must be able to say NO, not only yes — a step that streams data
    to stdout must be able to opt out of a project-wide default."""
    monkeypatch.chdir(tmp_path)
    runprov.configure(
        root=tmp_path, run_log=tmp_path / "runs.jsonl", terminal_log_dir=tmp_path / "logs"
    )
    with runprov.Run("on", provenance=tmp_path / "a.json") as on:
        pass
    with runprov.Run("off", provenance=tmp_path / "b.json", terminal_log=False) as off:
        pass
    assert on.record["terminal_log"]["path"].endswith(".log")
    assert "_" in pathlib.Path(on.record["terminal_log"]["path"]).name, "named <script>_<run_id>"
    assert "terminal_log" not in off.record


def test_a_caller_supplied_log_is_registered_without_any_capture(tmp_path, monkeypatch):
    """The primitive under the automatic capture. It takes no stream, so it is the shape
    for a harness that already collects a child's output, or a `| tee` in a Makefile."""
    monkeypatch.chdir(tmp_path)
    runprov.configure(root=tmp_path, run_log=tmp_path / "runs.jsonl")
    log = tmp_path / "made_by_the_shell.log"
    log.write_text("output the caller collected\n", encoding="utf-8")
    before = sys.stdout
    with runprov.Run("s", provenance=tmp_path / "p.json") as run:
        assert run.terminal_log(log) == log
        assert sys.stdout is before, "registering a log must not touch any stream"
    assert run.record["terminal_log"]["capture"] == "caller"
    hashed = [o for o in run.record["outputs"] if o["path"] == str(log)]
    assert hashed and hashed[0]["sha256"], "it is hashed like any other artifact"


def test_the_history_and_the_yaml_view_carry_the_terminal_log(tmp_path, monkeypatch):
    """`terminal_log_file` is the old log's field name, and the mechanism travels beside
    it — the old field was a bare path and could not say what it had been able to see."""
    yaml = pytest.importorskip("yaml")
    monkeypatch.chdir(tmp_path)
    runlog = tmp_path / "runs.jsonl"
    runprov.configure(root=tmp_path, run_log=runlog, terminal_log_dir=tmp_path / "logs")
    with runprov.Run("s", provenance=tmp_path / "p.json"):
        print("recorded", flush=True)
    line = json.loads(runlog.read_text(encoding="utf-8").splitlines()[-1])
    assert line["terminal_log"]["capture"] == "fd"
    d = yaml.safe_load(cli._yaml([line]))[0]
    assert d["terminal_log_file"].endswith(".log")
    assert d["terminal_log_capture"] == "fd"


class _RaisingFile:
    """A real file-like object whose writes fail — a full disk, or a vanished mount.

    A fake, not a mock: it has the interface and the behaviour, so the code under test
    takes the same path it would take in production. Nothing asserts that a method was
    called; the assertions are about what the run RECORDED, which is the only thing that
    matters to a reader of the history.
    """

    def __init__(self, exc=None):
        # Constructed in the body, not the default: a call in a default argument is
        # evaluated once at import and shared by every instance.
        self.exc = exc or OSError("no space left on device")
        self.closed = False

    def write(self, *_a):
        raise self.exc

    def flush(self):
        raise self.exc

    def close(self):
        self.closed = True
        raise self.exc


def test_a_log_that_cannot_be_written_still_lets_the_output_through(tmp_path, monkeypatch):
    """The priority when the disk fails is not ambiguous: the run's output belongs to the
    caller and the copy is the expendable half. A capture that ate stdout on a full disk
    would be the `_report.py` defect with extra steps."""
    monkeypatch.chdir(tmp_path)
    sink = tmp_path / "real.txt"
    cap = runprov.Capture(tmp_path / "t.log")
    with open(sink, "w", encoding="utf-8") as fh:
        saved = os.dup(1)
        os.dup2(fh.fileno(), 1)
        try:
            cap.start()
            cap._fh = _RaisingFile()  # the disk fills AFTER the capture started
            os.write(1, b"STILL-REACHES-THE-TERMINAL\n")
            cap.stop()
        finally:
            os.dup2(saved, 1)
            os.close(saved)
    assert "STILL-REACHES-THE-TERMINAL" in sink.read_text(encoding="utf-8")


def test_when_neither_mechanism_works_capture_is_off_and_says_so(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    log = tmp_path / "t.log"
    cap = runprov.Capture(log)
    monkeypatch.setattr(runprov.terminal, "os", _NoDup2())
    monkeypatch.setattr(runprov.terminal, "_flush_std", _raise_runtime)
    cap.start()
    assert cap.mode == "none"
    assert "terminal capture off" in capsys.readouterr().err
    assert cap.stop()["capture"] == "none"


def test_a_capture_whose_log_vanished_records_it_as_MISSING(tmp_path):
    """Registered and absent is a finding, exactly as it is for any other output."""
    cap = runprov.Capture(tmp_path / "gone.log")
    cap.mode = "fd"
    assert cap.describe()["kind"] == "MISSING"


def test_a_process_still_holding_the_output_open_is_reported_not_hung(tmp_path, monkeypatch):
    """A provenance module must never hang the run it is describing. If a child inherited
    the captured fd and never exits, the join times out, the log is declared possibly
    short, and the run continues."""
    monkeypatch.chdir(tmp_path)
    cap = runprov.Capture(tmp_path / "t.log")
    cap.start()

    class _Stuck:
        def join(self, timeout=None):
            return None

        def is_alive(self):
            return True

    cap._thread = _Stuck()
    rec = cap.stop()
    assert "did not finish" in rec["error"]


def test_stopping_a_capture_never_raises_into_the_run(tmp_path, monkeypatch, capsys):
    """`stop()` runs while an exception may already be in flight. It must not become the
    failure the caller sees — the same rule `_persist` follows."""
    monkeypatch.chdir(tmp_path)
    cap = runprov.Capture(tmp_path / "t.log")
    cap.start()
    monkeypatch.setattr(runprov.terminal, "_flush_std", _raise_runtime)
    rec = cap.stop()
    assert "stopping capture failed" in rec["error"]
    assert "WARNING" in capsys.readouterr().err
    cap._restore_fds()
    cap._close_saved()


def test_a_run_whose_capture_cannot_start_or_stop_is_still_recorded(tmp_path, monkeypatch, capsys):
    """Capture is an ADDITION to the record, never a precondition for it. Both ends are
    isolated, so a broken tee costs the log and never the run."""
    monkeypatch.chdir(tmp_path)
    log = tmp_path / "runs.jsonl"
    runprov.configure(root=tmp_path, run_log=log)

    class _ExplodingCapture:
        def __init__(self, path):
            raise RuntimeError("cannot even construct")

    monkeypatch.setattr(runprov.run, "Capture", _ExplodingCapture)
    with runprov.Run("a", provenance=tmp_path / "a.json", terminal_log=tmp_path / "a.log"):
        pass
    assert "could not start" in capsys.readouterr().err
    assert json.loads(log.read_text(encoding="utf-8").splitlines()[-1])["status"] == "ok"

    class _ExplodingStop:
        def __init__(self, path):
            self.path, self.mode, self.error = path, "fd", None

        def start(self):
            return None

        def stop(self):
            raise RuntimeError("stop blew up")

    monkeypatch.setattr(runprov.run, "Capture", _ExplodingStop)
    with runprov.Run("b", provenance=tmp_path / "b.json", terminal_log=tmp_path / "b.log") as run:
        pass
    assert "could not stop cleanly" in capsys.readouterr().err
    assert "terminal_log" not in run.record
    assert json.loads(log.read_text(encoding="utf-8").splitlines()[-1])["status"] == "ok"


def test_a_capture_returning_no_description_leaves_the_record_alone(tmp_path, monkeypatch):
    """`stop()` returns None when its own teardown failed. The record must then carry no
    `terminal_log` key at all rather than a null one — absent and 'null' read differently."""
    monkeypatch.chdir(tmp_path)
    runprov.configure(root=tmp_path, run_log=tmp_path / "runs.jsonl")

    class _NoDescription:
        def __init__(self, path):
            self.path, self.mode, self.error = path, "fd", None

        def start(self):
            return None

        def stop(self):
            return None

    monkeypatch.setattr(runprov.run, "Capture", _NoDescription)
    with runprov.Run("s", provenance=tmp_path / "p.json", terminal_log=tmp_path / "s.log") as run:
        pass
    assert "terminal_log" not in run.record


def test_the_python_tee_survives_a_stream_that_refuses_to_write(tmp_path):
    """`_StreamTee` must pass through first and copy second, so a broken copy target cannot
    cost the caller its output."""
    out = io.StringIO()
    tee = runprov.terminal._StreamTee(out, _RaisingFile())
    assert tee.write("kept\n") == 5
    assert out.getvalue() == "kept\n"
    assert tee.encoding == out.encoding  # delegation, not reimplementation


def test_flush_and_write_helpers_never_raise(monkeypatch):
    """Both run while fds are mid-swap, where reporting a failure would write to the very
    stream that just failed."""
    monkeypatch.setattr(sys, "stdout", _RaisingFile())
    runprov.terminal._flush_std()  # must not raise
    runprov.terminal._write_all(-1, b"nowhere")  # a closed fd: returns, does not raise


def test_write_all_completes_a_short_write(monkeypatch):
    """`os.write` may write fewer bytes than asked. Losing the tail of a line to that would
    corrupt exactly the evidence this file exists to keep."""
    seen = []

    class _ShortWriter:
        def __getattr__(self, name):
            return getattr(os, name)

        def write(self, fd, data):
            seen.append(bytes(data))
            return 1 if len(data) > 1 else len(data)

    monkeypatch.setattr(runprov.terminal, "os", _ShortWriter())
    runprov.terminal._write_all(1, b"abc")
    assert b"".join(d[:1] for d in seen) == b"abc", "every byte must be written exactly once"


def test_write_all_gives_up_when_the_target_refuses_everything(monkeypatch):
    """A writer returning 0 forever would spin the mirror thread. It must terminate."""

    class _Zero:
        def __getattr__(self, name):
            return getattr(os, name)

        def write(self, fd, data):
            return 0

    monkeypatch.setattr(runprov.terminal, "os", _Zero())
    runprov.terminal._write_all(1, b"abc")  # returns rather than looping


def test_restoring_fds_tolerates_a_closed_descriptor(tmp_path):
    """Nothing useful remains to be done about a failed restore, and raising would replace
    the run's own outcome."""
    cap = runprov.Capture(tmp_path / "t.log")
    cap._saved = {1: -1, 2: -1}  # never-valid descriptors
    cap._restore_fds()
    cap._close_saved()
    assert cap._saved == {}


def test_the_pump_stops_when_its_pipe_disappears(tmp_path):
    """The read end going away mid-run is process teardown, not an error to report — there
    is nothing left to mirror and nothing to report to."""
    cap = runprov.Capture(tmp_path / "t.log")
    rfd, wfd = os.pipe()
    os.close(rfd)
    os.close(wfd)
    cap._pump(rfd, 1)  # a closed fd: returns rather than raising


def test_every_half_of_the_capture_works_without_the_other(tmp_path):
    """Each piece holds an optional collaborator, and each `is not None` guard had only
    ever been true. A guard whose false branch nobody has executed is a guard nobody has
    checked — and these are the states reached when the log file could not be opened, which
    is exactly when the rest must keep working.
    """
    # the pump, mirroring with NO file to copy into
    cap = runprov.Capture(tmp_path / "unused.log")
    rfd, wfd = os.pipe()
    sink = tmp_path / "mirror.txt"
    with open(sink, "wb") as fh:
        os.write(wfd, b"MIRRORED-ANYWAY\n")
        os.close(wfd)
        cap._fh = None
        cap._pump(rfd, fh.fileno())
    assert sink.read_bytes() == b"MIRRORED-ANYWAY\n", "no file to copy into, mirror still runs"

    # stop() in fd mode with no thread ever started
    lone = runprov.Capture(tmp_path / "none.log")
    lone.mode = "fd"
    assert lone.stop()["kind"] == "MISSING"

    # closing when there is nothing open
    runprov.Capture(tmp_path / "x.log")._close_file()

    # the python tee with no copy target: pure passthrough
    out = io.StringIO()
    assert runprov.terminal._StreamTee(out, None).write("through\n") == 8
    assert out.getvalue() == "through\n"


def _raise_runtime(*_a, **_k):
    raise RuntimeError("simulated failure")


class _NoDup2:
    """The `os` module with `dup2` broken — a machine whose fds 1 and 2 cannot be moved.

    A real object standing in for a real module, rather than a mock: everything except the
    one call under test behaves exactly as it does in production, so the fallback is
    exercised against the genuine `os.pipe`, `os.dup` and `os.close`.
    """

    def __getattr__(self, name):
        return getattr(os, name)

    def dup2(self, *a, **k):
        raise OSError("dup2 unavailable on this machine")


# ===================================== council batch: a run must not hang, crash, or lie
NO_FIFO = pytest.mark.skipif(
    not hasattr(os, "mkfifo"),
    reason="Windows has no FIFOs, so the hazard cannot exist there and neither can the test",
)


@NO_FIFO
@requires_fifo
def test_registering_a_fifo_fails_instead_of_hanging(tmp_path):
    """R15. `open()` on a FIFO blocks until a writer appears, so registering one hangs the
    run FOREVER — with no message, in provenance capture, before the work starts. A
    provenance module that hangs the run it is describing is the worst failure available to
    it: worse than a wrong record, because there is no record and no process either.

    Measured before this: the probe needed SIGALRM to escape.
    """
    fifo = tmp_path / "pipe"
    os.mkfifo(fifo)
    with pytest.raises(ValueError, match="not a regular file"):
        runprov.describe(fifo)


@NO_FIFO
@requires_fifo
def test_a_run_refuses_a_fifo_input_by_name(tmp_path, monkeypatch):
    """The same guard where a caller meets it, and it must say WHICH script and WHICH path
    — the bare hang gave neither."""
    monkeypatch.chdir(tmp_path)
    runprov.configure(root=tmp_path, run_log=tmp_path / "runs.jsonl")
    fifo = tmp_path / "pipe"
    os.mkfifo(fifo)
    with runprov.Run("s", provenance=tmp_path / "p.json") as run:
        with pytest.raises(ValueError) as e:
            run.input(fifo)
    assert "s:" in str(e.value) and "pipe" in str(e.value)


def test_a_corrupt_gzip_does_not_escape_content_digest(tmp_path):
    """R16. `zlib.error` is not an OSError, so a truncated or corrupt `.gz` raised straight
    out of `content_digest` — inside provenance capture, on a registered input. The except
    clause listed OSError, EOFError and BadGzipFile and missed the one decompression
    actually raises."""
    p = tmp_path / "bad.gz"
    p.write_bytes(b"\x1f\x8b\x08\x00" + b"garbagegarbage")
    assert runprov.content_digest(p) == runprov.sha256(p), "it must fall back to the raw hash"


def test_a_non_finite_note_still_produces_strict_json(tmp_path, monkeypatch):
    """R11. `json.dumps` emits bare `NaN`/`Infinity`, which are NOT JSON. Every strict
    parser rejects the file — so a single NaN metric makes the sidecar AND the history line
    unreadable to anything that is not Python."""
    monkeypatch.chdir(tmp_path)
    log = tmp_path / "runs.jsonl"
    runprov.configure(root=tmp_path, run_log=log)
    prov = tmp_path / "p.json"
    with runprov.Run("s", provenance=prov) as run:
        run.note("auroc", float("nan"))
        run.note("loss", float("inf"))

    def strict(text):
        return json.loads(
            text, parse_constant=lambda c: (_ for _ in ()).throw(ValueError(f"non-JSON: {c}"))
        )

    rec = strict(prov.read_text(encoding="utf-8"))
    strict(log.read_text(encoding="utf-8").splitlines()[-1])
    assert rec["notes"]["auroc"] == "NaN" and rec["notes"]["loss"] == "Infinity", (
        "the VALUE must survive as a string — dropping it would lose the measurement"
    )


# Was `sys.platform == "win32"`, which is the wrong question: root on POSIX also traverses
# a 0o000 directory, so inside a root container this ran and could not fail.
@requires_unreadable_files
def test_an_unreadable_subdirectory_is_reported_not_silently_dropped(tmp_path):
    """R8. `rglob` skips a directory it cannot enter and says nothing, so the tree hash
    changed while the tree did not — the silent-skip class, in the function whose job is
    saying what a run read."""
    d = tmp_path / "tree"
    (d / "sub").mkdir(parents=True)
    (d / "sub" / "f").write_text("x", encoding="utf-8")
    (d / "ok").write_text("y", encoding="utf-8")
    before = runprov.describe(d)
    # The CLEAN reading first, and it is what makes the count falsifiable: asserting only
    # `== 1` on the broken tree passes against a hardcoded 1. Mutation-tested.
    assert before["n_unreadable_dirs"] == 0
    os.chmod(d / "sub", 0o000)
    try:
        after = runprov.describe(d)
    finally:
        # 0o700, not 0o755: restoring only what the test removed. S103 flags the broader
        # mask, and it is right to — a test has no business widening permissions.
        os.chmod(d / "sub", 0o700)
    assert after.get("n_unreadable_dirs") == 1, "the count must appear in the record"
    assert after["sha256_tree"] != before["sha256_tree"], "it genuinely hashes less"
    assert "sub" in " ".join(after.get("unreadable_dirs", [])), "and name what it could not read"


# ================================================ I5: the quickstart must teach the fix
def _readme() -> str:
    """The shipped README, in either layout.

    `runprov/README.md` here; `README.md` at the root of the extracted standalone. Both are
    the SAME shipped file, and a test that silently skipped when it found neither would be
    the silent-skip class — so a missing README is a failure, not a pass.
    """
    for cand in (REPO / "runprov" / "README.md", REPO / "README.md"):
        if cand.is_file():
            return cand.read_text(encoding="utf-8")
    raise AssertionError(f"no shipped README found under {REPO}")


def _first_python_block(text: str) -> str:
    m = re.search(r"```python\n(.*?)```", text, re.S)
    assert m, "the README has no python block; the quickstart is the first one"
    return m.group(1)


#: Internal working documents. They stay in the repository — the audience is the person they
#: address — and they must not travel inside a release. L-08.
_NOT_FOR_DISTRIBUTION = ("PUBLISHING.md", "LICENSING.md")


#: Written to disk verbatim, so it is a RAW string: `\t` and `\n` have to survive into the
#: generated file as escape sequences rather than becoming real characters here, which would
#: break the f-string across lines.
_SMK_STEP = r"""import csv, pathlib
rows = list(csv.DictReader(open("declared.tsv"), delimiter="\t"))
label = list(csv.DictReader(open("lookup.csv")))[0]["label"]   # UNDECLARED read
pathlib.Path("out.tsv").write_text(f"label\t{label}\nn\t{len(rows)}\n", encoding="utf-8")
"""

_SMK_FILE = """rule build:
    input: "declared.tsv"
    output: "out.tsv"
    shell: "python step.py"
"""


def test_a_workflow_engine_cannot_see_an_undeclared_read(tmp_path):
    """WHY.md and the README both claim a workflow engine records what a rule DECLARED while
    this records the read where it happens. That is a claim about somebody else's tool, in
    the section a reviewer reads first, so it is measured rather than asserted — and it is
    measured HERE so it cannot quietly become false when Snakemake changes.

    Skips unless `snakemake` is on PATH (or `RUNPROV_SNAKEMAKE` points at it), which is the
    honest state on a machine that does not have it: an unrunnable comparison is not a
    passing one. Measured first on **9.25.2**, 2026-08-18.

    Note what is NOT claimed. Snakemake hashes — its metadata carries `input_checksums` with
    a sha256 — so "workflow engines do not hash" would be false. It hashes what was declared.
    And runprov does not see the undeclared read either; the difference is that its
    declaration sits inside the read expression rather than in a separate file. The test
    asserts exactly that pair, so neither half can be quoted without the other."""
    smk = os.environ.get("RUNPROV_SNAKEMAKE") or shutil.which("snakemake")
    if not smk:  # pragma: no cover - absent on CI and on most machines
        pytest.skip("snakemake is not on PATH; set RUNPROV_SNAKEMAKE to run this comparison")

    (tmp_path / "declared.tsv").write_text("sample\tvalue\na\t9\nb\t2\n", encoding="utf-8")
    (tmp_path / "lookup.csv").write_text("code,label\n1,ALPHA\n", encoding="utf-8")
    (tmp_path / "step.py").write_text(_SMK_STEP, encoding="utf-8")
    (tmp_path / "Snakefile").write_text(_SMK_FILE, encoding="utf-8")

    def snakemake():
        return subprocess.run([smk, "--cores", "1"], capture_output=True, text=True, cwd=tmp_path)

    first = snakemake()
    assert first.returncode == 0, first.stderr
    out = tmp_path / "out.tsv"
    assert "ALPHA" in out.read_text(encoding="utf-8"), "the premise: the script read the lookup"

    # The undeclared input changes. Nothing else does.
    (tmp_path / "lookup.csv").write_text("code,label\n1,OMEGA\n", encoding="utf-8")
    os.utime(tmp_path / "lookup.csv", (time.time() + 5, time.time() + 5))
    second = snakemake()
    assert second.returncode == 0, second.stderr
    assert "Nothing to be done" in second.stdout + second.stderr, (
        "the claim is that the engine sees no reason to re-run; it apparently did re-run, "
        "so the section in WHY.md and the README needs remeasuring against this version"
    )
    assert "ALPHA" in out.read_text(encoding="utf-8"), "and the artifact is stale meanwhile"

    # THE PREMISE, asserted rather than assumed, and it is not optional: without this the
    # test passes against a script that never reads the lookup at all — nothing changed that
    # the engine tracks, so "Nothing to be done" is true for the wrong reason and the stale
    # ALPHA is a coincidence. Forcing the rule proves the read is real and that the earlier
    # value was genuinely out of date. (Found by mutation: deleting the read left this green.)
    forced = subprocess.run(
        [smk, "--cores", "1", "--forceall"], capture_output=True, text=True, cwd=tmp_path
    )
    assert forced.returncode == 0, forced.stderr
    assert "OMEGA" in out.read_text(encoding="utf-8"), (
        "the script does not actually depend on the undeclared file, so this experiment "
        "demonstrates nothing"
    )

    # It DOES hash — what was declared. Saying otherwise would be false and checkable.
    meta = [
        json.loads(f.read_text(encoding="utf-8"))
        for f in (tmp_path / ".snakemake" / "metadata").rglob("*")
        if f.is_file()
    ]
    checksums = {k: v for m in meta for k, v in (m.get("input_checksums") or {}).items()}
    assert any(v.startswith("sha256:") for v in checksums.values()), (
        f"Snakemake no longer records input checksums; WHY.md says it does: {meta}"
    )
    assert "lookup.csv" not in checksums, "the undeclared read is what it cannot see"

    # And the artifact carries nothing about its own origins — claim 2, same experiment.
    assert "declared.tsv" not in out.read_text(encoding="utf-8")


@requires_fcntl  # a POSIX signal number is the premise
@pytest.mark.parametrize(("signame", "signum"), [("TERM", 15), ("KILL", 9), ("HUP", 1), ("INT", 2)])
def test_exec_returns_what_a_shell_returns_for_a_signal_killed_child(
    tmp_path, monkeypatch, signame, signum
):
    """Council C-25. `subprocess` reports a signal-killed child as `-N`, and
    `raise SystemExit(-15)` reaches the OS as `-15 & 0xFF` = 241 — so `exec` returned 241
    where `sh -c` returns 143, and 247 for SIGKILL where a shell says 137, which is the number
    an OOM check looks for.

    Worse than merely different: IT COLLIDED. A child killed by SIGTERM and a child that
    exited 241 both produced 241, two states a shell keeps apart. `exec`'s whole promise is
    "returns the command's own exit code, so it composes without changing what failure means".

    Parametrised over four signals because a single one cannot show the arithmetic is 128+N
    rather than a constant."""
    monkeypatch.chdir(tmp_path)
    runprov.configure(root=tmp_path, run_log=tmp_path / "h.jsonl")
    code = cli.main(
        [
            "exec",
            "--name",
            "k",
            "--",
            "/bin/sh",
            "-c",
            f"kill -{signame} $$",
        ]
    )
    assert code == 128 + signum, f"a shell returns {128 + signum} for SIG{signame}"

    rec = json.loads((tmp_path / "h.jsonl").read_text(encoding="utf-8").strip())
    assert rec["status"] == "failed"
    assert f"SIG{signame}" in rec["failure"]["message"], (
        "and the record must NAME the signal — 'exited -15' is unreadable and wrong: the "
        "process did not exit -15, it was killed"
    )
    assert rec["notes"]["signal"] == {"number": signum, "name": f"SIG{signame}"}
    assert rec["notes"]["exit_code"] == -signum, "the RAW wait status is still recorded"


def test_exec_does_not_confuse_a_signal_with_an_exit_code_that_looks_like_one(
    tmp_path, monkeypatch
):
    """The collision, asserted directly. Before the fix, SIGTERM and `exit 241` were both 241.
    A shell keeps them apart and so must this."""
    monkeypatch.chdir(tmp_path)
    runprov.configure(root=tmp_path, run_log=tmp_path / "h.jsonl")
    killed = cli.main(
        [
            "exec",
            "--name",
            "a",
            "--",
            "/bin/sh",
            "-c",
            "kill -TERM $$",
        ]
    )
    exited = cli.main(
        [
            "exec",
            "--name",
            "b",
            "--",
            "/bin/sh",
            "-c",
            "exit 241",
        ]
    )
    assert killed == 143 and exited == 241, f"{killed} vs {exited} — these must differ"

    recs = [json.loads(x) for x in (tmp_path / "h.jsonl").read_text().splitlines() if x]
    by = {r["script"]: r for r in recs}
    assert "signal" in by["a"]["notes"] and "signal" not in by["b"]["notes"], (
        "and only one of them was killed by a signal"
    )


def test_exec_does_not_read_a_windows_crash_code_as_a_signal(tmp_path, monkeypatch):
    """A Windows child that crashes yields a large negative NTSTATUS (e.g. -1073741819), which
    is not a signal number. Without a floor, `128 + 1073741819` would be invented and the
    message would name a signal that does not exist.

    Cannot be produced on this platform, so `subprocess.run` is substituted — the one place in
    this suite where a mock is right, because the value comes from an OS this machine is not."""
    monkeypatch.chdir(tmp_path)
    runprov.configure(root=tmp_path, run_log=tmp_path / "h.jsonl")

    class FakePopen:
        """Popen-shaped, because `_exec` starts the child with Popen so it can forward a
        signal to it (C-28). Patching `subprocess.run` stopped intercepting when that
        changed, and the test failed loudly rather than silently — which is the good case."""

        pid = 424242

        def __init__(self, *a, **k):
            self._killed = False
            self.args = a[0] if a else []

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def communicate(self, *a, **k):
            return (None, None)

        def kill(self):
            self._killed = True

        def wait(self, timeout=None):
            return -1073741819

        def poll(self):
            return -1073741819

    monkeypatch.setattr(cli.subprocess, "Popen", FakePopen)
    code = cli.main(["exec", "--name", "w", "--", "anything"])
    rec = json.loads((tmp_path / "h.jsonl").read_text(encoding="utf-8").strip())
    assert "signal" not in rec["notes"], "an NTSTATUS is not a signal"
    assert "killed by" not in rec["failure"]["message"]
    assert code != 0


def test_exec_names_a_signal_the_platform_has_no_name_for(tmp_path, monkeypatch):
    """A real-time signal has a number and no `signal.Signals` member. It must still be
    recorded as a signal rather than crashing the reporting path on `ValueError`."""
    monkeypatch.chdir(tmp_path)
    runprov.configure(root=tmp_path, run_log=tmp_path / "h.jsonl")

    class FakePopen:
        pid = 424243

        def __init__(self, *a, **k):
            self._killed = False
            self.args = a[0] if a else []

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def communicate(self, *a, **k):
            return (None, None)

        def kill(self):
            self._killed = True

        def wait(self, timeout=None):
            return -35

        def poll(self):
            return -35

    monkeypatch.setattr(cli.subprocess, "Popen", FakePopen)
    assert cli.main(["exec", "--name", "rt", "--", "anything"]) == 163
    rec = json.loads((tmp_path / "h.jsonl").read_text(encoding="utf-8").strip())
    assert rec["notes"]["signal"] == {"number": 35, "name": "signal 35"}


@pytest.mark.parametrize("cmd", ["log", "show", "verify", "lineage"])
def test_every_subcommand_accepts_format_text(tmp_path, monkeypatch, cmd):
    """L-25. `log` called the human format `timeline` while `show`, `verify` and `lineage`
    called it `text`, so `--format text` — learned on any one of those — was a hard ERROR on
    `log`, and NO format name at all was accepted by all four.

    Asserted through the real parser: argparse exits 2 on an unknown choice, so a rejected
    format is indistinguishable here from any other usage error, which is exactly what the
    user hit."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "in.tsv").write_text("a\n", encoding="utf-8")
    runprov.configure(root=tmp_path, run_log=tmp_path / "h.jsonl")
    with runprov.Run("s", provenance=tmp_path / "p.json") as run:
        run.input(tmp_path / "in.tsv")
        with run.open_output(tmp_path / "out.tsv") as fh:
            fh.write("x\n")

    argv = [cmd, "--format", "text"]
    if cmd in ("log", "lineage"):
        argv += ["--log", str(tmp_path / "h.jsonl")]
    code = cli.main(argv)
    assert code != 2, f"`{cmd} --format text` was rejected by the parser"


def test_the_old_timeline_spelling_still_works(tmp_path, monkeypatch, capsys):
    """Removing it would break every script that already passes it, so it stays accepted and
    is merely hidden from the help. `text` and `timeline` must render the SAME BYTES — an
    alias that quietly produced different output would be worse than the inconsistency it
    replaces."""
    monkeypatch.chdir(tmp_path)
    runprov.configure(root=tmp_path, run_log=tmp_path / "h.jsonl")
    with runprov.Run("s", provenance=tmp_path / "p.json") as run:
        run.note("a", 1)

    rendered = []
    for spelling in ("text", "timeline"):
        capsys.readouterr()
        assert cli.main(["log", "--log", str(tmp_path / "h.jsonl"), "--format", spelling]) == 0
        rendered.append(capsys.readouterr().out)

    assert rendered[0] == rendered[1], "the alias must render exactly what the new name does"
    assert rendered[0].strip(), "and it must render something, or this proves nothing"


def _exec_and_signal(tmp_path, *, to_group):
    """Start `runprov exec` on a long child, signal it, and report what happened.

    A real subprocess, because the whole subject is what a SIGNAL does to a process tree.

    READINESS AND SURVIVAL ARE BOTH SIGNALLED BY FILES, not by matching process names. A
    `pgrep` pattern matches any shell that happens to carry the string — including the one
    running the test — which produced both a false orphan and a killed test runner while this
    was being written. The child touches `started` and then, if it is still alive after the
    parent should have gone, touches `survived`."""
    started = tmp_path / "started"
    survived = tmp_path / "survived"
    script = f"touch {started}; sleep 3; touch {survived}; sleep 30"

    env = {**os.environ, "PYTHONPATH": str(REPO)}
    proc = subprocess.Popen(
        [sys.executable, "-m", "runprov", "exec", "--name", "t", "--", "/bin/sh", "-c", script],
        env=env,
        cwd=tmp_path,
        start_new_session=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
    )
    deadline = time.monotonic() + 30
    while not started.exists() and time.monotonic() < deadline:
        time.sleep(0.05)
    assert started.exists(), "the child never started; the test proves nothing"

    if to_group:
        os.killpg(os.getpgid(proc.pid), signal.SIGTERM)  # what `scancel` does
    else:
        proc.send_signal(signal.SIGTERM)  # SIGTERM to runprov alone
    _, err = proc.communicate(timeout=60)

    # The child would touch `survived` about 3s after starting. Wait past that: if it is
    # gone, the file never appears.
    time.sleep(5)
    return proc.returncode, err, survived.exists()


@pytest.mark.parametrize("child_ignores_sigterm", [False, True])
def test_a_terminated_exec_forwards_the_signal_and_escalates(
    tmp_path, monkeypatch, capsys, child_ignores_sigterm
):
    """Council C-27 + C-28, IN PROCESS. The two subprocess tests below prove the real
    behaviour, but they run runprov as a CHILD, so coverage cannot see the forwarding code at
    all — the same blind spot the audit hook had. This drives the logic directly.

    Both arms matter: a child that dies on SIGTERM must not be SIGKILLed, and one that
    ignores it must be, because leaving it running is the orphan this exists to prevent."""
    monkeypatch.chdir(tmp_path)
    sent: list[tuple[int, int]] = []

    class FakePopen:
        pid = 999001

        def __init__(self, *a, **k):
            self._killed = False
            self.args = a[0] if a else []

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def communicate(self, *a, **k):
            return (None, None)

        def kill(self):
            self._killed = True

        def wait(self, timeout=None):
            if timeout is None:
                raise runprov.run.Terminated(signal.SIGTERM)  # the signal-turned-exception
            if child_ignores_sigterm and not self._killed:
                raise subprocess.TimeoutExpired("cmd", timeout)
            return -signal.SIGTERM

        def poll(self):
            return None if (child_ignores_sigterm and not self._killed) else -signal.SIGTERM

    def fake_killpg(pid, sig):
        sent.append((pid, sig))
        if sig == signal.SIGKILL:
            FakePopen._killed = True

    monkeypatch.setattr(cli.subprocess, "Popen", FakePopen)
    monkeypatch.setattr(cli.os, "killpg", fake_killpg)

    code = cli.main(["exec", "--name", "t", "--", "/bin/sh", "-c", "sleep 40"])

    assert code == 128 + signal.SIGTERM, "a number, not a traceback"
    assert "terminated by SIGTERM" in capsys.readouterr().err
    signals = [s for _, s in sent]
    assert signal.SIGTERM in signals, "the child must be told first"
    if child_ignores_sigterm:
        assert signal.SIGKILL in signals, "and killed if it will not go"
    else:
        assert signal.SIGKILL not in signals, "but not killed if it already went"


@requires_fcntl  # POSIX signals and process groups are the whole subject
def test_a_scancel_shaped_kill_gives_a_number_not_a_traceback(tmp_path):
    """Council C-27. `Terminated` is a `BaseException` on purpose — so an ordinary
    `except Exception:` around a pipeline step cannot swallow a kill — but nothing in the CLI
    caught it either, so SIGTERM to the process group printed a RAW PYTHON TRACEBACK and
    exited 1. Measured before the fix.

    This is the commonest way a cluster job actually ends: SLURM's time limit is
    SIGTERM-then-SIGKILL and `scancel` is SIGTERM, which `run.py`'s own signal docstring
    already calls out. The RECORD was always correct; only the CLI's answer was not.

    128+N, matching a signal-killed child and matching a shell, so a wrapper inspecting the
    code sees the same number whether the signal hit the child or hit runprov."""
    code, err, _ = _exec_and_signal(tmp_path, to_group=True)

    assert "Traceback" not in err, f"a traceback is not an answer:\n{err[-400:]}"
    assert code == 128 + signal.SIGTERM, f"a shell would say 143, got {code}"


@requires_fcntl
def test_a_killed_exec_does_not_orphan_its_child(tmp_path):
    """Council C-28. SIGTERM to `runprov exec` killed the wrapper and left the child running
    — measured: the child outlived the runprov that started it. On a cluster that is work
    still running after the job that owns it is gone, invisible to the scheduler that thinks
    it reclaimed the node.

    The child runs in its own session and the signal is forwarded to its GROUP, because
    `sh -c` usually has children of its own."""
    code, _, child_survived = _exec_and_signal(tmp_path, to_group=False)

    assert not child_survived, "the child outlived runprov"
    assert code == 128 + signal.SIGTERM


def test_the_pin_tables_cannot_be_mutated_by_a_caller(tmp_path, monkeypatch):
    """Council C-26. `PIN_UNSAFE` was a plain dict and the ONLY mutable container in the whole
    public API, so any library sharing the interpreter could rewrite it.

    For THAT table it is harmless, and the test says so rather than implying otherwise:
    nothing branches on it — clearing it entirely changes no behaviour — because it is
    documentation rendered into a refusal message.

    `PIN_ALTERNATIVE` is the one where it bites, and it is NOT exported, which is why the
    reviewer missed it. Measured: `.zzz` opened with `comment="// "` writes a sidecar before
    a row is added and an in-band pin after. BOTH halves are needed — the row and a matching
    caller comment — which is narrower than it first looks, but the artifact's own bytes
    change and nothing announces it."""
    with pytest.raises(TypeError):
        runprov.PIN_UNSAFE[".tsv"] = "x"
    with pytest.raises(TypeError):
        del runprov.PIN_UNSAFE[".nwk"]
    with pytest.raises(TypeError):
        runprov.run.PIN_ALTERNATIVE[".zzz"] = ("// ", "")

    assert runprov.PIN_UNSAFE[".nwk"].startswith("Newick"), "still readable"
    assert dict(runprov.PIN_UNSAFE), "and still convertible, which is what a caller needs"


def test_the_unsafe_table_is_documentation_and_nothing_branches_on_it(tmp_path, monkeypatch):
    """The half that stops a future reader re-discovering it. `PIN_UNSAFE` earns its place by
    explaining a refusal, not by causing one — `PIN_BINARY` and `PIN_INLINE` decide. Asserted
    against a COPY, since the real table is now frozen."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(runprov.run, "PIN_UNSAFE", {})  # emptied entirely
    runprov.configure(root=tmp_path, run_log=tmp_path / "h.jsonl")

    with runprov.Run("s", provenance=tmp_path / "p.json") as run:
        with run.open_output(tmp_path / "t.nwk") as fh:
            fh.write("(a,b);\n")

    body = (tmp_path / "t.nwk").read_text(encoding="utf-8")
    assert not body.startswith("#"), "still no in-band pin, with the table empty"
    assert (tmp_path / "t.nwk.prov.txt").is_file(), "still a sidecar — the route is unchanged"


def test_the_two_to_yamls_are_both_reachable_and_disagree(tmp_path):
    """Council C-24 / ledger L-47. `runprov.to_yaml` and `runprov.show.to_yaml` are different
    functions with the same name, both reachable (`import runprov` binds `runprov.show`), both
    accepting a record dict, neither raising on the other's input — so the wrong import yields
    a plausible file of the WRONG SHAPE rather than an error.

    Which name changes is the author's decision (L-47, `needs-your-decision`), so this test
    does not assert a rename. It pins the DIVERGENCE, so that whoever settles the name has the
    behaviour written down, and so that the more dangerous half cannot be forgotten: only the
    package-level one normalises with `_jsonable`.

    That normalisation is not cosmetic. It is exactly the failure `runprov.to_yaml`'s own
    comment cites: a manifest reading `n_exact_matches: "<scalar 118>"` beside a sidecar
    reading `118`, for one run."""

    class Scalarish:  # a numpy scalar's shape, without the dependency
        def item(self):
            return 118

        def __repr__(self):
            return "<scalar 118>"

    record = {"script": "s", "notes": {"n": Scalarish()}}

    packaged = runprov.to_yaml([record])
    assert "118" in packaged and "<scalar" not in packaged, (
        "the package-level renderer normalises first, which is why the manifest and the "
        "sidecar agree"
    )

    low_level = runprov.show.to_yaml(record)
    assert "<scalar" in low_level, (
        "and the low-level one does NOT — it must be handed something already normalised. If "
        "this ever passes, the divergence is closed and the docstrings should stop warning."
    )
    assert packaged != low_level, "different shapes, same name, both reachable"


def test_the_verify_module_is_not_shadowed_by_a_function(tmp_path):
    """Council C-24, second half. `runprov.verify` is a MODULE, and `runprov.verify.verify` is
    the function inside it. Exporting a `verify` FUNCTION from `__init__` later would shadow
    the module for anyone who had already imported it — `import runprov.verify` does not
    restore the attribute once `sys.modules` holds it, so working code breaks with
    `AttributeError: 'function' object has no attribute 'verify'`.

    Reproduced on a throwaway copy during the audit. This guard costs nothing and fails the
    moment that export is added, which is the only cheap moment to notice."""
    import runprov.verify

    assert isinstance(runprov.verify, types.ModuleType), "runprov.verify must stay a module"
    assert callable(runprov.verify.verify), "and the function must stay reachable through it"


@requires_symlinks
def test_a_symlinked_subdirectory_is_counted_not_silently_dropped(tmp_path):
    """Council C-23. `os.walk` does not follow a directory symlink, which is deliberate and
    documented in SECURITY.md — following one would pull an unbounded amount of somebody
    else's disk into a hash. **The silence was the defect.** `_dirs` was discarded, so a
    linked subtree appeared in NO count, and adding a whole new file inside it left
    `sha256_tree` IDENTICAL. Measured before the fix.

    The code's own `skipped` counter makes the argument: "dropping it without a word is the
    same silent skip as the unreadable directory above." `data/ref -> /shared/references` is
    the ordinary layout in the domain this package came from, so this is the common case."""
    tree = tmp_path / "tree"
    elsewhere = tmp_path / "elsewhere"
    tree.mkdir()
    elsewhere.mkdir()
    (tree / "a.txt").write_text("a\n", encoding="utf-8")
    (elsewhere / "one.txt").write_text("e\n", encoding="utf-8")
    (tree / "linkdir").symlink_to(elsewhere, target_is_directory=True)

    before = runprov.describe(tree)
    assert before["n_symlinked_dirs_not_followed"] == 1
    assert [pathlib.Path(x).name for x in before["symlinked_dirs_not_followed"]] == ["linkdir"]

    # The premise the count exists to disclose: the subtree really is outside the hash.
    (elsewhere / "two.txt").write_text("brand new\n", encoding="utf-8")
    after = runprov.describe(tree)
    assert after["sha256_tree"] == before["sha256_tree"], (
        "not following is the documented behaviour; if this changes, the count is the wrong fix"
    )
    assert after["n_symlinked_dirs_not_followed"] == 1, "and the omission is still stated"


def test_a_tree_with_no_symlinked_directories_says_nothing_about_them(tmp_path):
    """Omitted, not defaulted — the rule every optional field in the record follows. A count
    that appears on every tree is noise, and the named list is what a reader acts on."""
    tree = tmp_path / "plain"
    tree.mkdir()
    (tree / "f.txt").write_text("x\n", encoding="utf-8")

    rec = runprov.describe(tree)
    assert rec["n_symlinked_dirs_not_followed"] == 0
    assert "symlinked_dirs_not_followed" not in rec


@requires_symlinks
def test_the_two_symlink_asymmetries_are_what_they_are(tmp_path):
    """Both surprised the reviewer and neither is changed: a symlinked FILE inside the tree IS
    hashed, and a symlink to a directory passed as the TOP-LEVEL path IS followed — it is what
    the caller named. Only a directory link found DURING the walk is left out.

    Asserted so that changing either becomes a decision rather than a side effect."""
    tree = tmp_path / "tree"
    elsewhere = tmp_path / "elsewhere"
    tree.mkdir()
    elsewhere.mkdir()
    (elsewhere / "one.txt").write_text("e\n", encoding="utf-8")
    (tree / "linkfile.txt").symlink_to(elsewhere / "one.txt")
    (tree / "linkdir").symlink_to(elsewhere, target_is_directory=True)

    inside = runprov.describe(tree)
    assert inside["n_files"] == 1, "the symlinked FILE is followed and counted"
    assert inside["n_symlinked_dirs_not_followed"] == 1, "the symlinked DIRECTORY is not"

    top = runprov.describe(tree / "linkdir")
    assert top["kind"] == "directory" and top["symlink"] is True
    assert top["n_files"] == 1, "a link named AS the target is followed — bounded by the ask"


def test_an_unhonoured_snapshot_request_is_recorded_not_swallowed(tmp_path, monkeypatch, capsys):
    """Council C-22. `environment_snapshot()` returned None, recorded nothing and warned
    nothing when no directory was configured — while the caller was unambiguously asking for a
    snapshot.

    `tool()`, ten methods up in the same file, states the governing principle: "A tool that is
    NOT found is recorded with `found: false` rather than omitted. 'We looked and it was not
    there' is a fact about the run; silence is not." The same argument applies to a request
    that could not be honoured, and silence left a record indistinguishable from one where
    nobody asked."""
    monkeypatch.chdir(tmp_path)
    runprov.configure(root=tmp_path, run_log=tmp_path / "h.jsonl")  # no env_snapshot_dir

    with runprov.Run("s", provenance=tmp_path / "p.json") as run:
        assert run.environment_snapshot() is None, "the return contract is unchanged"
    err = capsys.readouterr().err

    asked = run.record["environment"]["snapshot_requested"]
    assert asked["written"] is False
    assert "env_snapshot_dir" in asked["reason"], "and the record says WHY, not just that"
    assert "NO ENVIRONMENT SNAPSHOT" in err
    assert "configure(env_snapshot_dir=" in err, "the warning must name the fix"

    rec = json.loads((tmp_path / "p.json").read_text(encoding="utf-8"))
    assert rec["environment"]["snapshot_requested"]["written"] is False, "and it reaches disk"


def test_a_run_nobody_asked_records_no_snapshot_request(tmp_path, monkeypatch, capsys):
    """The half that keeps it usable. `write()` takes the automatic path only when a directory
    IS configured, so it can never meet the unconfigured case — and a run where nobody asked
    for a snapshot must say nothing about snapshots at all. Omitted, not defaulted, like every
    other optional field in the record."""
    monkeypatch.chdir(tmp_path)
    runprov.configure(root=tmp_path, run_log=tmp_path / "h.jsonl")
    with runprov.Run("s", provenance=tmp_path / "p.json") as run:
        run.note("a", 1)

    assert "snapshot_requested" not in run.record["environment"]
    assert "NO ENVIRONMENT SNAPSHOT" not in capsys.readouterr().err


def test_a_snapshot_that_can_be_written_is_not_reported_as_refused(tmp_path, monkeypatch, capsys):
    """And the configured case must be untouched: the request is honoured, so there is no
    unhonoured request to record."""
    monkeypatch.chdir(tmp_path)
    runprov.configure(
        root=tmp_path, run_log=tmp_path / "h.jsonl", env_snapshot_dir=tmp_path / "envs"
    )
    with runprov.Run("s", provenance=tmp_path / "p.json") as run:
        got = run.environment_snapshot()

    assert got is not None and "snapshot" in run.record["environment"]
    assert "snapshot_requested" not in run.record["environment"]
    assert "NO ENVIRONMENT SNAPSHOT" not in capsys.readouterr().err


def test_project_fields_are_keyword_only(tmp_path):
    """Council C-21. `Project` is a frozen dataclass with 16 fields and, without
    `kw_only=True`, it accepted POSITIONAL arguments — so field order would become API the day
    this is published, and inserting a field beside its logical neighbour would be a breaking
    change rather than an addition.

    This is not hypothetical. `warn_unregistered_reads` was added at index 5 on 2026-08-19,
    shifting `env_snapshot_dir` 5→6 and `terminal_log_dir` 6→7. A caller who had written the
    positional form would now bind a `Path` to a BOOL field and get `env_snapshot_dir = None`,
    with NO error — indices 1/2 and 6/7 are all `Path | None`, and 3/4/5 are all `bool`. A
    silent mis-binding in the object that decides where every record goes."""
    with pytest.raises(TypeError, match="positional"):
        runprov.Project(tmp_path, tmp_path / "h.jsonl")  # type: ignore[misc]

    assert runprov.Project(root=tmp_path).root == tmp_path, "keywords are unaffected"

    # And the property that outlives this test: order is free to change because nothing can
    # depend on it. Asserted as the RULE rather than as a list of names, so adding a field is
    # not a test edit.
    import dataclasses

    for field in dataclasses.fields(runprov.Project):
        assert field.kw_only, f"{field.name} is positional; inserting a field would break callers"


def test_a_run_armed_but_never_written_says_so_at_exit(tmp_path, monkeypatch, capsys):
    """Council C-20. The THIRD shape that records nothing, and the only one that records
    nothing on a CLEAN finish: `provenance=` supplied, no `with`, no `write()`. The README's
    table covered the two that lose a crash; this one loses everything, always, silently.

    It is the likelier mistake now rather than a rarer one — every page presses `provenance=`
    on the constructor as "the switch that makes `__exit__` write", so the half left to forget
    is the `with`. And the artifact is still written, still carrying a pin that names a record
    which does not exist.

    Driven through `_warn_unpersisted` directly rather than by ending the interpreter, because
    a test cannot exit the process it runs in. The wiring — that `atexit` calls this — is
    asserted separately below."""
    monkeypatch.chdir(tmp_path)
    runprov.configure(root=tmp_path, run_log=tmp_path / "h.jsonl")

    run = runprov.Run("noctx", provenance=tmp_path / "p.json")  # armed, never written
    run.note("x", 1)
    with run.open_output(tmp_path / "out.tsv") as fh:
        fh.write("data\n")
    capsys.readouterr()

    runprov.run._warn_unpersisted()
    err = capsys.readouterr().err
    assert "NOTHING WAS RECORDED" in err and "noctx" in err
    assert "with Run(" in err, "the warning must show the shape that works"

    assert not (tmp_path / "p.json").exists(), "the premise: nothing was recorded"
    assert (tmp_path / "out.tsv").is_file(), "while the artifact IS on disk, carrying a pin"


@pytest.mark.parametrize(
    "shape", ["with_block", "manual_write", "no_provenance", "crash_inside_with"]
)
def test_a_run_that_did_record_is_not_warned_about(tmp_path, monkeypatch, capsys, shape):
    """The half that decides whether this is usable. A warning that fires on correct code is
    worse than none — it is the "permanently red check" this package cites more than any other
    failure. Every shape that reaches disk must be silent, including the crash path, which
    records precisely because it is armed."""
    monkeypatch.chdir(tmp_path)
    runprov.configure(root=tmp_path, run_log=tmp_path / "h.jsonl")
    prov = tmp_path / "p.json"

    if shape == "with_block":
        with runprov.Run("s", provenance=prov) as run:
            run.note("a", 1)
    elif shape == "manual_write":
        run = runprov.Run("s", provenance=prov)
        run.note("a", 1)
        run.write(prov)
    elif shape == "no_provenance":
        # BOUND TO A NAME, deliberately. `_UNPERSISTED` is a WeakSet, so an unreferenced Run
        # is collected before the warning runs and the assertion passes for the wrong reason —
        # measured: with the `provenance is not None` condition removed, this test still
        # passed. Holding the reference is what makes it test the condition.
        held = runprov.Run("s")  # never promised a record, so never warned about
        held.note("a", 1)
    else:
        with contextlib.suppress(ValueError), runprov.Run("s", provenance=prov) as run:
            raise ValueError("boom")

    capsys.readouterr()
    runprov.run._warn_unpersisted()
    assert "NOTHING WAS RECORDED" not in capsys.readouterr().err


def test_the_exit_warning_is_actually_wired_to_atexit(tmp_path, monkeypatch):
    """A warning nothing calls is not a warning. Asserted by watching the registration, since
    the alternative is ending the interpreter."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(runprov.run, "_ATEXIT_REGISTERED", False)
    registered = []
    monkeypatch.setattr(runprov.run.atexit, "register", lambda fn: registered.append(fn))
    runprov.configure(root=tmp_path, run_log=tmp_path / "h.jsonl")

    runprov.Run("a", provenance=tmp_path / "a.json")
    runprov.Run("b", provenance=tmp_path / "b.json")
    assert registered == [runprov.run._warn_unpersisted], (
        "registered once, and it is the right function"
    )

    # HELD, for the same reason: a WeakSet drops an unreferenced Run, so without this the
    # assertion below is satisfied by garbage collection rather than by the condition.
    unarmed = runprov.Run("c")  # no provenance: promises nothing, so it must not be tracked
    assert unarmed not in runprov.run._UNPERSISTED
    assert all(r.provenance_path is not None for r in runprov.run._UNPERSISTED), (
        "only runs that were armed to record may be warned about"
    )


def test_every_path_argument_accepts_a_plain_string(tmp_path, monkeypatch):
    """Council C-19. `provenance=` was annotated `pathlib.Path | None` while `input`,
    `output`, `write`, `code`, `open_output`, `pin_sidecar` and `terminal_log` all take
    `str | pathlib.Path` — and so does `_sidecar_name`, which is what consumes it. A plain
    string worked at runtime and mypy rejected it, on THE MOST LOAD-BEARING ARGUMENT IN THE
    PACKAGE: the one that decides whether a crash is recorded.

    With `py.typed` shipped, every typed consumer met that error on the flagship call, and
    the tempting way to silence it is to delete the argument — which is precisely the shape
    that records nothing.

    Asserted at RUNTIME here; the annotation itself is checked by the signature test below,
    because `mypy` on the package is a separate gate and a runtime test cannot see a type."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "in.tsv").write_text("a\n", encoding="utf-8")
    (tmp_path / "t.log").write_text("log\n", encoding="utf-8")
    runprov.configure(root=tmp_path, run_log=tmp_path / "h.jsonl")

    with runprov.Run("s", provenance="p.json", script_path="s.py") as run:
        run.input("in.tsv")
        run.terminal_log("t.log")
        with run.open_output("out.tsv") as fh:
            fh.write("x\n")

    assert (tmp_path / "p.json").is_file(), "a str provenance must still write the sidecar"
    assert isinstance(run.provenance_path, pathlib.Path), "and be normalised on the way in"
    assert run.record["status"] == "ok"


def test_the_path_arguments_are_annotated_the_same_way(tmp_path):
    """The annotation is the actual defect — the runtime always worked — so it is the
    annotation that gets asserted. `mypy` runs over the package in a separate gate and would
    not notice a NARROWING here, because a narrower type is self-consistent: it only breaks
    the consumer, which the package's own suite never type-checks."""
    import inspect

    sig = inspect.signature(runprov.Run.__init__)
    for name in ("provenance", "script_path", "terminal_log"):
        annotation = str(sig.parameters[name].annotation)
        assert "str" in annotation, (
            f"Run(..., {name}=) is annotated {annotation!r}, which rejects a plain string "
            f"while every sibling path argument accepts one"
        )

    for method in (
        "input",
        "output",
        "write",
        "code",
        "open_output",
        "pin_sidecar",
        "terminal_log",
    ):
        params = inspect.signature(getattr(runprov.Run, method)).parameters
        first = next(p for n, p in params.items() if n not in ("self",))
        assert "str" in str(first.annotation), f"{method}() stopped accepting a str"


@pytest.mark.parametrize(
    "call",
    ["note", "seeds", "output", "input", "tool", "code", "module", "terminal_log"],
)
def test_recording_after_the_block_is_refused_not_swallowed(tmp_path, monkeypatch, call):
    """Council C-18. Everything called after `__exit__` was accepted, mutated `run.record`,
    returned normally and reached no file — silently. Measured: `note`, `seeds`, `output` and
    `input` after the block left the live record holding values the sidecar and history did
    not, with not one word said.

    It matters more than "do not do that", because THIS PACKAGE'S OWN DOCS send callers out
    here: `to_yaml`'s docstring says "AFTER the block, deliberately" and shows
    `MANIFEST.write_text(runprov.to_yaml(run.record))`. A reader taught the object is live
    after the block will reasonably call `run.note(...)` too.

    Parametrised across every recording method, because guarding some and not others is how
    this survives a fix."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "f.txt").write_text("x\n", encoding="utf-8")
    runprov.configure(root=tmp_path, run_log=tmp_path / "h.jsonl")
    with runprov.Run("s", provenance=tmp_path / "p.json") as run:
        pass

    attempt = {
        "note": lambda: run.note("late", 1),
        "seeds": lambda: run.seeds([7]),
        "output": lambda: run.output(tmp_path / "late.txt"),
        "input": lambda: run.input(tmp_path / "f.txt"),
        "tool": lambda: run.tool("ls"),
        "code": lambda: run.code(tmp_path / "f.txt"),
        "module": lambda: run.module(runprov),
        "terminal_log": lambda: run.terminal_log(tmp_path / "f.txt"),
    }[call]

    with pytest.raises(RuntimeError, match="after the `with` block closed"):
        attempt()


def test_reading_the_record_after_the_block_is_still_the_documented_shape(tmp_path, monkeypatch):
    """The other half, and the reason this refuses RECORDING rather than all access: the docs
    hand the reader `runprov.to_yaml(run.record)` after the block on purpose. Breaking that
    to fix the silent loss would trade one defect for another."""
    monkeypatch.chdir(tmp_path)
    runprov.configure(root=tmp_path, run_log=tmp_path / "h.jsonl")
    with runprov.Run("s", provenance=tmp_path / "p.json") as run:
        run.note("inside", "yes")

    assert run.record["notes"] == {"inside": "yes"}, "the record is readable out here"
    rendered = runprov.to_yaml(run.record)
    assert "inside" in rendered, "and renderable, which is what the docstring demonstrates"


def test_a_late_write_cannot_make_the_sidecar_and_the_history_disagree(tmp_path, monkeypatch):
    """Council C-18, second half. `write()` after the block re-persists the SIDECAR while the
    history line, already appended, is not corrected — so one `run_uid` had two persisted
    records disagreeing about notes, seeds and outputs. That breaks the invariant `to_yaml`'s
    own docstring states: "Two renderings of the same record must not disagree, which is the
    whole claim here."

    Fixed at the root rather than patched: if nothing can CHANGE the record after exit, a
    later `write()` re-persists an identical one and the two files agree by construction.
    `write()` itself stays legal — the manual shape depends on it."""
    monkeypatch.chdir(tmp_path)
    runprov.configure(root=tmp_path, run_log=tmp_path / "h.jsonl")
    with runprov.Run("s", provenance=tmp_path / "p.json") as run:
        run.note("inside", "yes")

    run.write(tmp_path / "p.json")  # legal, and must now be a no-op in content terms

    side = json.loads((tmp_path / "p.json").read_text(encoding="utf-8"))
    hist = json.loads((tmp_path / "h.jsonl").read_text(encoding="utf-8").strip())
    assert side["run_uid"] == hist["run_uid"], "the premise: one run, two persisted records"
    for field in ("notes", "seeds"):
        assert side[field] == hist[field], f"{field} disagree: {side[field]} vs {hist[field]}"
    assert len(side["outputs"]) == len(hist["outputs"])


def test_a_run_refuses_to_be_entered_twice(tmp_path, monkeypatch):
    """Council C-17. Re-entering the same `Run` was allowed and recorded only the first pass:
    two blocks, ONE history line, and the second block's output left on disk with nothing
    describing it. With no input registered in the second block it was completely silent.

    Worse than under-recording, the SIDECAR came out internally inconsistent — it listed the
    second block's input and not its output, describing a run that never happened.

    Raising does not breach "provenance must not kill the run": it fires at ENTRY, before the
    second block does any work, so there is no unrecorded work to lose. That is the whole
    difference from the silent under-recording it replaces."""
    monkeypatch.chdir(tmp_path)
    runprov.configure(root=tmp_path, run_log=tmp_path / "h.jsonl")
    run = runprov.Run("step", provenance=tmp_path / "p.json")

    with run:
        with run.open_output(tmp_path / "out1.tsv") as fh:
            fh.write("first\n")

    with pytest.raises(RuntimeError, match="already been used as a context manager"):
        with run:
            with run.open_output(tmp_path / "out2.tsv") as fh:  # pragma: no cover - refused
                fh.write("second\n")

    assert not (tmp_path / "out2.tsv").exists(), (
        "refused at entry, so the second block's work never happens — an artifact with no "
        "record is exactly what this prevents"
    )
    lines = [x for x in (tmp_path / "h.jsonl").read_text(encoding="utf-8").splitlines() if x]
    assert len(lines) == 1, "the first pass is still recorded, exactly once"
    assert (tmp_path / "out1.tsv").is_file(), "and its work is untouched"


def test_the_message_says_what_to_do_instead(tmp_path, monkeypatch):
    """`for f in files: with run:` is the plausible misreading, so the error has to name both
    correct shapes rather than only refusing."""
    monkeypatch.chdir(tmp_path)
    runprov.configure(root=tmp_path, run_log=tmp_path / "h.jsonl")
    run = runprov.Run("step", provenance=tmp_path / "p.json")
    with run:
        pass
    with pytest.raises(RuntimeError) as caught:
        with run:
            pass  # pragma: no cover - refused
    message = str(caught.value)
    assert "Run per iteration" in message and "around the whole loop" in message
    assert "step" in message, "and it must name WHICH run, for a script with several"


def test_a_run_used_without_a_context_manager_is_unaffected(tmp_path, monkeypatch):
    """The manual shape stays legal. The guard is about re-ENTRY, not about entering at all,
    and `write()` without a `with` is a documented (if weaker) way to use this."""
    monkeypatch.chdir(tmp_path)
    runprov.configure(root=tmp_path, run_log=tmp_path / "h.jsonl")
    run = runprov.Run("step", provenance=tmp_path / "p.json")
    run.write(tmp_path / "p.json")
    run.write(tmp_path / "p2.json")  # twice, deliberately: only __enter__ is guarded
    assert (tmp_path / "p.json").is_file() and (tmp_path / "p2.json").is_file()


@requires_unreadable_files
def test_write_does_not_confirm_a_sidecar_it_could_not_write(tmp_path, monkeypatch, capsys):
    """Council C-16. `write()` called `_persist(p)` and DISCARDED the bool that `_persist`'s
    own docstring says means "the record is on disk", then printed `provenance -> <path>`
    unconditionally. Measured with a read-only sidecar directory: the warning fired, the
    confirmation followed it, and `write()` returned a path to a file that does not exist.

    Not fully silent — the warning does precede it — but a wrapper parsing the
    `provenance -> ` line, or a caller archiving the returned path, is handed something
    absent. And the run is NOT lost: the history is written by a different path, so the
    message names which half exists instead of implying both do."""
    monkeypatch.chdir(tmp_path)
    locked = tmp_path / "locked"
    locked.mkdir()
    runprov.configure(root=tmp_path, run_log=tmp_path / "runs.jsonl")

    # LOCKED BEFORE THE RUN STARTS. Locking it afterwards leaves the sidecar `__exit__`
    # already wrote sitting there, and the test then asserts against a file that exists for
    # an unrelated reason — which is how a test passes while proving nothing.
    locked.chmod(0o500)  # readable, not writable
    try:
        run = runprov.Run("step", provenance=locked / "o.prov.json")
        with run:
            with run.open_output(tmp_path / "out.tsv") as fh:
                fh.write("x\n")
        capsys.readouterr()
        returned = run.write(locked / "o.prov.json")
        err = capsys.readouterr().err
        exists = (locked / "o.prov.json").exists()
    finally:
        locked.chmod(0o700)

    assert not exists, "the premise: the sidecar really is absent"
    assert "could not write the provenance sidecar" in err, "the warning still fires"
    assert "NOT WRITTEN" in err, "and the confirmation must not claim otherwise"
    assert f"provenance -> {locked / 'o.prov.json'}" not in err, (
        "the success line must not appear for a file that was never written"
    )
    # The history survives a sidecar failure, and that is worth knowing rather than guessing.
    assert (tmp_path / "runs.jsonl").is_file()
    assert returned == locked / "o.prov.json", "the return is still the path it tried"


def test_write_still_confirms_a_sidecar_it_did_write(tmp_path, monkeypatch, capsys):
    """The other half, and the one that keeps the fix honest: the ordinary path must still
    read as a plain confirmation. A message that hedges on every run is no better than one
    that lies on the rare one."""
    monkeypatch.chdir(tmp_path)
    runprov.configure(root=tmp_path, run_log=tmp_path / "runs.jsonl")
    run = runprov.Run("step", provenance=tmp_path / "o.prov.json")
    with run:
        with run.open_output(tmp_path / "out.tsv") as fh:
            fh.write("x\n")

    err = capsys.readouterr().err
    assert f"provenance -> {tmp_path / 'o.prov.json'}" in err
    assert "NOT WRITTEN" not in err
    assert (tmp_path / "o.prov.json").is_file()


def test_terminal_log_anchors_like_its_siblings(tmp_path, monkeypatch, capsys):
    """Council C-15. `terminal_log()` skipped `_anchor()`, so a relative path was resolved at
    `write()` time against the cwd the run STARTED in. A run built in `a/` that then `chdir`s
    to `b/` recorded the name `step.log` carrying the digest of **a/step.log** — the file in
    the old directory, not the one the caller would open.

    Verbatim the defect `_anchor`'s own docstring says it exists to kill: "The record named a
    file that did not exist and carried the hash of a different one — no error, no warning,
    and nothing downstream able to tell." It survived in the one method that predates the
    guard.

    The silence is asserted too, and it is the sharp end: because the cwd-moved notice is
    raised BY `_anchor`, a run that called only this method said nothing at all."""
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    (tmp_path / "a" / "step.log").write_text("LOG FROM A\n", encoding="utf-8")
    (tmp_path / "b" / "step.log").write_text("LOG FROM B\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path / "a")
    runprov.configure(root=tmp_path, run_log=tmp_path / "h.jsonl")

    run = runprov.Run("step", provenance=tmp_path / "p.json")
    monkeypatch.chdir(tmp_path / "b")
    returned = run.terminal_log("step.log")
    run.write(tmp_path / "p.json")

    assert returned == pathlib.Path(str(returned)).resolve(), "anchored, not the raw string"
    assert returned == (tmp_path / "b" / "step.log").resolve(), (
        "it must return an anchored absolute, like input() and output(), so that "
        "open(run.terminal_log(P)) and the record cannot disagree"
    )
    entry = run.record["outputs"][0]
    assert entry["sha256"] == runprov.sha256(tmp_path / "b" / "step.log"), (
        f"the record carries the digest of the WRONG file: {entry}"
    )
    assert "NOTICE" in capsys.readouterr().err, (
        "and the cwd-moved notice must fire — it is raised by _anchor, so skipping _anchor "
        "made this case completely silent"
    )
    assert run.record["terminal_log"]["path"] == str(returned), "the field agrees too"


def test_terminal_log_left_alone_when_the_cwd_does_not_move(tmp_path, monkeypatch, capsys):
    """The ordinary case must not have acquired a notice: anchoring is only visible when the
    cwd actually moved, and a package that warns on every correct run trains its users to
    ignore it."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "step.log").write_text("x\n", encoding="utf-8")
    runprov.configure(root=tmp_path, run_log=tmp_path / "h.jsonl")
    run = runprov.Run("step", provenance=tmp_path / "p.json")
    capsys.readouterr()

    # THE SAME ANSWER `output()` GIVES, which is the actual property — not "absolute".
    # `_anchor` deliberately leaves a path relative while the cwd is where the run started,
    # because a relative path is the portable one and rewriting it would make records from
    # two machines stop comparing equal.
    assert run.terminal_log("step.log") == run.output("step.log")
    run.write(tmp_path / "p.json")
    assert "NOTICE" not in capsys.readouterr().err


def test_no_stderr_never_means_the_callers_stdout(tmp_path, monkeypatch, capsys):
    """Council C-13. `print(..., file=None)` falls back to STDOUT by CPython's own rule, and
    `sys.stderr is None` is the documented state under `pythonw.exe`, embedded interpreters
    and windowed frozen builds. Measured: a run put 725 bytes of provenance chatter into the
    caller's data channel and 0 into stderr — the exact defect `_report.py`'s docstring opens
    with (`python step.py > result.tsv` showing provenance lines inside the data), arriving
    through the fallback rather than through a bare `print`.

    Silence is the right degradation. The RECORD is still written to disk; the only thing
    lost is a message about it."""
    monkeypatch.chdir(tmp_path)
    runprov.configure(root=tmp_path, run_log=tmp_path / "h.jsonl")
    monkeypatch.setattr(runprov._report.sys, "stderr", None)

    with runprov.Run("step", provenance=tmp_path / "p.json") as run:
        with run.open_output(tmp_path / "out.tsv") as fh:
            fh.write("real\tdata\n")

    captured = capsys.readouterr()
    assert captured.out == "", f"provenance leaked into the data channel: {captured.out!r}"
    assert (tmp_path / "out.tsv").is_file(), "and the work still happened"
    assert (tmp_path / "p.json").is_file(), "and the record was still written"
    assert run.record["status"] == "ok"


def test_an_unwritable_stderr_does_not_destroy_the_run(tmp_path, monkeypatch):
    """Council C-14, and the more serious half. `_write` caught `UnicodeEncodeError` alone,
    so an stderr that cannot be WRITTEN TO killed the run: measured with
    `python step.py 2>/dev/full` (ENOSPC on every write) — exit 120, **no artifact, no
    sidecar, no history line**. The reporting mechanism destroyed the run it was describing,
    which is the failure that same docstring names as the reason this module exists.

    A full disk, a closed pipe (`... | head`, EPIPE) and a detached console all reach it. A
    fake stream rather than `/dev/full`, so the guard is tested everywhere and not only on
    Linux.

    Note what is NOT claimed: the process may still exit 120, because CPython flushes stderr
    at shutdown and that flush fails independently of anything here — `python -c
    "sys.stderr.write('x')" 2>/dev/full` exits 120 too. What a library controls is whether
    the user's work and record survive."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "in.tsv").write_text("x\n", encoding="utf-8")
    runprov.configure(root=tmp_path, run_log=tmp_path / "h.jsonl")

    class FullDisk:
        encoding = "utf-8"

        def write(self, _text):
            raise OSError(28, "No space left on device")

        def flush(self):
            raise OSError(28, "No space left on device")

    monkeypatch.setattr(runprov._report.sys, "stderr", FullDisk())

    with runprov.Run("step", provenance=tmp_path / "p.json") as run:
        run.input(tmp_path / "in.tsv")
        with run.open_output(tmp_path / "out.tsv") as fh:
            fh.write("real\tdata\n")

    assert (tmp_path / "out.tsv").is_file(), "the artifact must survive an unwritable stderr"
    assert (tmp_path / "p.json").is_file(), "and the sidecar"
    assert (tmp_path / "h.jsonl").is_file(), "and the history line"
    assert run.record["status"] == "ok"


def test_a_stderr_that_cannot_encode_still_degrades_rather_than_dying(tmp_path, monkeypatch):
    """The pre-existing guard, kept honest: its fallback re-encodes and writes AGAIN, and
    that second write can fail exactly like the first. Both paths must end in silence rather
    than in an exception."""
    monkeypatch.chdir(tmp_path)
    runprov.configure(root=tmp_path, run_log=tmp_path / "h.jsonl")

    class HostileConsole:
        encoding = "ascii"

        def write(self, text):
            if any(ord(ch) > 127 for ch in text):
                raise UnicodeEncodeError("ascii", text, 0, 1, "hostile")
            raise OSError(28, "and the fallback fails too")

        def flush(self):
            pass

    monkeypatch.setattr(runprov._report.sys, "stderr", HostileConsole())
    runprov._report.diagnostic("an em dash — and a naïve café")  # must not raise

    with runprov.Run("step", provenance=tmp_path / "p.json") as run:
        with run.open_output(tmp_path / "out.tsv") as fh:
            fh.write("x\n")
    assert (tmp_path / "p.json").is_file()


@pytest.mark.parametrize(
    ("field", "make"),
    [
        ("script", lambda inject: {"name": inject}),
        ("generation", lambda inject: {"generation": inject}),
    ],
)
def test_no_field_in_the_pin_can_forge_an_entry(tmp_path, monkeypatch, field, make):
    """Council C-12. `_safe_for_pin` was applied to input FILENAMES only, while the header
    also interpolates `script`, `generation` and `commit` — all caller- or
    environment-supplied. `RUNPROV_GENERATION` carrying a newline produced a pin listing an
    input nobody read, and `verify` then reported GONE and exited 1 **forever**, over a file
    that never existed. That is the permanently-red check this package cites more than any
    other failure, reachable by a scheduler putting a job description in a variable.

    Both spellings of the injection are tried, because escaping one field and not the others
    is exactly how this got here."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "real.tsv").write_text("a\tb\n1\t2\n", encoding="utf-8")
    inject = "v1\n#     0000000000000000  NEVER_READ.tsv"
    kw = make(inject)

    proj = runprov.Project(
        root=tmp_path,
        run_log=tmp_path / "h.jsonl",
        generation=(lambda: kw["generation"]) if "generation" in kw else (lambda: "g"),
    )
    run = runprov.Run(kw.get("name", "step"), project=proj, provenance=tmp_path / "p.json")
    run.input(tmp_path / "real.tsv")
    with run.open_output(tmp_path / "out.tsv") as fh:
        fh.write("x\n")
    run.write(tmp_path / "p.json")

    body = (tmp_path / "out.tsv").read_text(encoding="utf-8")
    # The injected text must survive as TEXT on the field's own line — escaped, not dropped.
    # (Dropping it would be a different defect: a record that quietly omits what it was told.)
    assert "\\n" in body, "the newline must be escaped, and the value kept"
    forged = [
        row
        for row in body.splitlines()
        if "NEVER_READ.tsv" in row and not row.lstrip("# ").startswith(("script", "generation"))
    ]
    assert not forged, f"the {field} field forged a line in the pin: {forged}"

    # The property that actually matters: the reader agrees the pin has ONE input.
    pins = runprov.verify.read_pins(tmp_path / "out.tsv")
    named = [name for pin in pins for _, name in pin["entries"]]
    assert named == ["real.tsv"], f"{field} injected a phantom input: {named}"
    assert all(pin["declared"] == 1 for pin in pins), "and the count still matches the entries"


def test_one_unusable_value_costs_that_value_and_not_the_record(tmp_path, monkeypatch):
    """Council C-07, and the worst defect found in the whole audit. `_jsonable` returned the
    object unchanged at its last line, leaving `str()` to the record's `default=str` — which
    runs OUTSIDE every guard here. An object whose `__str__` raises therefore destroyed the
    ENTIRE record: no sidecar, no history line, exit 0, one line on stderr. The user believes
    the run was recorded.

    Same rule as the history being JSONL rather than one document: damage costs the damaged
    thing, never everything around it. A bad KEY counts too — `default=` does not apply to
    keys, so `json.dumps` raises on them before any handler is consulted."""
    monkeypatch.chdir(tmp_path)
    runprov.configure(root=tmp_path, run_log=tmp_path / "h.jsonl")

    class Boom:
        def __repr__(self):
            raise RuntimeError("boom")

        def __str__(self):
            raise RuntimeError("boom")

    class Unhashable:
        def __hash__(self):
            return 7

    with runprov.Run("s", provenance=tmp_path / "p.json") as run:
        run.note("bad_value", Boom())
        run.note("bad_key", {Unhashable(): 1})
        run.note("fine", 42)

    rec = json.loads((tmp_path / "p.json").read_text(encoding="utf-8"))
    assert rec["notes"]["fine"] == 42, "the good notes must survive the bad one"
    assert "UNSERIALISABLE" in rec["notes"]["bad_value"], rec["notes"]["bad_value"]
    assert "Boom" in rec["notes"]["bad_value"], "and it must name the type, to be findable"

    line = json.loads((tmp_path / "h.jsonl").read_text(encoding="utf-8").strip())
    assert line["notes"]["fine"] == 42, "the HISTORY line must survive too, not just the sidecar"
    assert (tmp_path / "p.yml").is_file(), "and the YAML view, which renders the same record"


def test_configure_refuses_a_project_and_fields_together(tmp_path):
    """Council C-08. `configure(existing, write_yaml_sidecar=False)` returned `existing` and
    DISCARDED every keyword without a word — a silent no-op, which is this package's
    characteristic defect and the one shape it must not ship.

    Raising rather than merging: a caller who passes both has two ideas of the project in one
    call, and choosing one for them would be a guess."""
    proj = runprov.Project(root=tmp_path)
    with pytest.raises(TypeError, match="Project OR its fields"):
        runprov.configure(proj, write_yaml_sidecar=False)

    # Both single forms keep working — the fix must not cost the ordinary calls.
    assert runprov.configure(write_yaml_sidecar=False).write_yaml_sidecar is False
    assert runprov.configure(proj).root == tmp_path


def test_the_pin_does_not_call_a_content_digest_a_sha256(tmp_path, monkeypatch):
    """Council C-09. The header said `sha256:` over a value that is `pin_digest` — the
    CONTENT digest, which strips volatile build stamps. A reader who runs `sha256sum` on the
    input gets a different number and concludes the pin is broken. In a package whose thesis
    is that its claims can be checked, that was the wrong word in the one place it invites
    checking.

    Both spellings must still READ, because artifacts written before this change say the old
    word and reporting them as unpinned would be worse than the mislabelling."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "in.tsv").write_text("a\tb\n1\t2\n", encoding="utf-8")
    runprov.configure(root=tmp_path, run_log=tmp_path / "h.jsonl")
    with runprov.Run("s", provenance=tmp_path / "p.json") as run:
        run.input(tmp_path / "in.tsv")
        with run.open_output(tmp_path / "out.tsv") as fh:
            fh.write("x\n")

    body = (tmp_path / "out.tsv").read_text(encoding="utf-8")
    assert "content digest:" in body
    assert "sha256:" not in body, "the word must not appear over a value that is not one"

    # The premise, asserted rather than assumed: the pinned value really is NOT the sha256.
    pinned = next(row for row in body.splitlines() if "in.tsv" in row).split()[1]
    assert runprov.content_digest(tmp_path / "in.tsv").startswith(pinned)
    assert not runprov.sha256(tmp_path / "in.tsv").startswith(pinned), (
        "if these ever coincide the test proves nothing; pick an input with a volatile stamp"
    )

    # An artifact carrying the OLD word still verifies.
    old = tmp_path / "old.tsv"
    old.write_text(body.replace("content digest:", "sha256:"), encoding="utf-8")
    assert runprov.verify.read_pins(old), "pre-2026-08-19 artifacts must still be readable"


def test_verify_says_what_OK_means_every_time_it_says_OK(tmp_path, monkeypatch, capsys):
    """Council C-10. `verify` printed `OK` and exited 0 over an artifact with a fabricated
    row appended — true, because its INPUTS were untouched, and read by everyone as "this
    file is intact". The caveat existed in the README and nowhere a user of the command would
    meet it, and the README endorses building a CI gate on exactly this command."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "in.tsv").write_text("a\n", encoding="utf-8")
    runprov.configure(root=tmp_path, run_log=tmp_path / "h.jsonl")
    with runprov.Run("s", provenance=tmp_path / "p.json") as run:
        run.input(tmp_path / "in.tsv")
        with run.open_output(tmp_path / "out.tsv") as fh:
            fh.write("x\n")
    capsys.readouterr()

    with open(tmp_path / "out.tsv", "a", encoding="utf-8") as fh:
        fh.write("FABRICATED\n")  # the artifact is tampered; the inputs are not

    assert cli.main(["verify", str(tmp_path / "out.tsv"), "--root", str(tmp_path)]) == 0
    err = capsys.readouterr().err
    assert "OK" in err, "the premise: it still reports OK, which is what makes this a trap"
    assert "does NOT mean the artifact itself is unedited" in err
    assert "--rehash" in err, "and it must name the command that WOULD catch this"


def test_an_unregistered_read_is_noticed_and_recorded(tmp_path, monkeypatch, capsys):
    """L-107. `run.input(p)` makes registration the ordinary way to open a file; it cannot
    make it the only way. A plain `open(p)` left the read absent from the record and from the
    pin, and NOTHING SAID SO — a record that looks complete while being incomplete, which is
    the shape of the defect this package replaced.

    Recorded as well as warned: a warning is ephemeral and a field is checkable three years
    later by someone who was not there."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "in.tsv").write_text("a\tb\n1\t2\n", encoding="utf-8")
    (tmp_path / "data" / "lookup.csv").write_text("code,label\n1,X\n", encoding="utf-8")
    runprov.configure(root=tmp_path, run_log=tmp_path / "h.jsonl")

    with runprov.Run("misses", provenance=tmp_path / "p.json") as run:
        with open(run.input(tmp_path / "data" / "in.tsv"), encoding="utf-8") as fh:
            fh.read()
        with open(tmp_path / "data" / "lookup.csv", encoding="utf-8") as fh:
            fh.read()  # UNREGISTERED — the whole point
        with run.open_output(tmp_path / "out.tsv") as fh:
            fh.write("x\n")

    assert run.record["unregistered_reads"] == ["data/lookup.csv"], (
        "the missed read must be named, relative to the project root"
    )
    err = capsys.readouterr().err
    assert "UNREGISTERED READ" in err and "data/lookup.csv" in err
    assert "run.input(path)" in err, "the warning must say what to do about it"

    # It reaches the HISTORY, not just the live object: a field nobody can read later would
    # be no better than the warning.
    rec = json.loads((tmp_path / "h.jsonl").read_text(encoding="utf-8").strip())
    assert rec["unregistered_reads"] == ["data/lookup.csv"]


def test_a_run_that_registers_everything_says_nothing_at_all(tmp_path, monkeypatch, capsys):
    """The half that decides whether the feature is usable. A check that fires on a correct
    run is noise, and noise is the failure the deterministic-pin decision exists to prevent:
    a permanently red check teaches its audience to ignore it.

    The registered OUTPUT is the trap here — outputs are described in `write()`, which runs
    after the check, so an early version reported every output as an unregistered read."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "in.tsv").write_text("a\n", encoding="utf-8")
    (tmp_path / "cfg.json").write_text('{"k": 1}\n', encoding="utf-8")
    runprov.configure(root=tmp_path, run_log=tmp_path / "h.jsonl")

    with runprov.Run("clean", provenance=tmp_path / "p.json") as run:
        with open(run.input(tmp_path / "in.tsv"), encoding="utf-8") as fh:
            fh.read()
        with open(run.input(tmp_path / "cfg.json"), encoding="utf-8") as fh:
            fh.read()
        with run.open_output(tmp_path / "out.tsv") as fh:
            fh.write("x\n")

    assert "unregistered_reads" not in run.record, "omitted, not defaulted, when there are none"
    assert "UNREGISTERED READ" not in capsys.readouterr().err


def test_the_provenance_files_are_never_reported_as_unregistered(tmp_path, monkeypatch):
    """The package's own records are not the user's data. Reporting the history, the YAML
    view or a sidecar would make every run noisy about files the user never opened."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "in.tsv").write_text("a\n", encoding="utf-8")
    runprov.configure(root=tmp_path)  # default provenance/ dir, YAML view and sidecar on

    with runprov.Run("s", provenance=tmp_path / "out.prov.json") as run:
        run.input(tmp_path / "in.tsv")
        with run.open_output(tmp_path / "out.tsv") as fh:
            fh.write("x\n")
    with runprov.Run("s2", provenance=tmp_path / "out2.prov.json") as run2:
        run2.input(tmp_path / "in.tsv")

    for r in (run.record, run2.record):
        assert "unregistered_reads" not in r, r.get("unregistered_reads")


def test_the_watcher_can_be_turned_off(tmp_path, monkeypatch, capsys):
    """A step may deliberately read files it does not want recorded. Off means silent in the
    record as well as on the terminal."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "secret.tsv").write_text("x\n", encoding="utf-8")
    runprov.configure(root=tmp_path, run_log=tmp_path / "h.jsonl", warn_unregistered_reads=False)

    with runprov.Run("quiet", provenance=tmp_path / "p.json") as run:
        with open(tmp_path / "secret.tsv", encoding="utf-8") as fh:
            fh.read()

    assert "unregistered_reads" not in run.record
    assert "UNREGISTERED READ" not in capsys.readouterr().err


def test_the_unregistered_filter_excludes_what_is_not_data(tmp_path):
    """`watch.unregistered` is pure, so the filter is testable directly rather than through a
    run. Each exclusion below cost a false positive in an earlier version or would have."""
    root = tmp_path
    (root / "sub").mkdir()
    (root / "sub" / "keep.tsv").write_text("x", encoding="utf-8")
    (root / "code.py").write_text("x", encoding="utf-8")
    (root / "site-packages").mkdir()
    (root / "site-packages" / "thing.tsv").write_text("x", encoding="utf-8")
    (root / "registered.tsv").write_text("x", encoding="utf-8")
    outside = tmp_path.parent / "outside.tsv"
    outside.write_text("x", encoding="utf-8")

    got = runprov.watch.unregistered(
        opened=[
            str(root / "sub" / "keep.tsv"),
            str(root / "code.py"),  # code, not data
            str(root / "site-packages" / "thing.tsv"),  # not the user's
            str(root / "registered.tsv"),  # already in the record
            str(outside),  # outside the project
            str(root / "gone.tsv"),  # does not exist
            str(root / "sub"),  # a directory
        ],
        registered=[str(root / "registered.tsv")],
        root=root,
    )
    assert got == ["sub/keep.tsv"]

    # The `exclude` arm: a whole directory the package owns, such as the snapshot store.
    (root / "snapshots").mkdir()
    (root / "snapshots" / "env.tsv").write_text("x", encoding="utf-8")
    assert runprov.watch.unregistered(
        opened=[str(root / "snapshots" / "env.tsv"), str(root / "sub" / "keep.tsv")],
        registered=[],
        root=root,
        exclude=[root / "snapshots"],
    ) == ["sub/keep.tsv"], "an excluded directory's contents are ours, not the user's data"


def test_the_hook_itself_ignores_everything_it_should(monkeypatch):
    """The hook body CALLED DIRECTLY, because coverage cannot trace it otherwise: CPython
    runs audit-hook callbacks with tracing suppressed, so the integration tests above prove
    it works while leaving its branches unmeasured. Calling it as a plain function measures
    the logic; the integration tests prove the real hook is wired to real opens. Both are
    needed — neither substitutes for the other.

    Every branch here is a way to be wrong about what a file is."""

    class FakeRun:
        def __init__(self):
            self._opened = set()

    w = runprov.watch._Watcher()
    run = FakeRun()

    w._hook("open", ("/tmp/x.tsv",))  # nobody listening yet
    assert run._opened == set()

    w._active.append(run)  # attach without installing a real process-wide hook
    w._hook("exec", ("/tmp/other.tsv",))  # not an open
    assert run._opened == set()

    w._hook("open", (3,))  # a file DESCRIPTOR, not a name
    assert run._opened == set()

    w._hook("open", ("/tmp/x.tsv",))
    w._hook("open", (b"/tmp/y.tsv",))  # bytes are a path too
    w._hook("open", (pathlib.Path("/tmp/z.tsv"),))  # and so is os.PathLike
    assert run._opened == {"/tmp/x.tsv", "/tmp/y.tsv", "/tmp/z.tsv"}

    monkeypatch.setattr(runprov.watch, "WATCH_MAX_PATHS", 3)
    w._hook("open", ("/tmp/past-the-cap.tsv",))
    assert "/tmp/past-the-cap.tsv" not in run._opened, "the cap must hold"


def test_installing_the_hook_is_idempotent(monkeypatch):
    """`sys.addaudithook` cannot be undone — the interpreter offers no API — so installing
    one per run would leave a process with hundreds, each firing on every open, and nothing
    could remove them. Exactly one, ever."""
    calls = []
    monkeypatch.setattr(runprov.watch.sys, "addaudithook", lambda h: calls.append(h))
    w = runprov.watch._Watcher()
    w.install()
    w.install()
    w.attach(object())
    assert len(calls) == 1, f"installed {len(calls)} hooks; it must be exactly one"


def test_detaching_a_run_that_was_never_attached_is_harmless():
    """Teardown must not be able to raise: `_note_unregistered_reads` detaches, and it runs
    on the failure path too."""
    runprov.watch._Watcher().detach(object())  # must not raise


def test_the_watcher_remembers_a_bounded_number_of_paths(tmp_path, monkeypatch):
    """`WATCH_MAX_PATHS` bounds MEMORY, not usefulness: a run opening more distinct files
    than this has a provenance problem the first names already describe. Asserted with a real
    run rather than by poking the set, and the bound gets its own sanity range so shrinking it
    to 1 cannot satisfy this test."""
    assert 100 <= runprov.watch.WATCH_MAX_PATHS <= 100_000
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(runprov.watch, "WATCH_MAX_PATHS", 5)
    runprov.configure(root=tmp_path, run_log=tmp_path / "h.jsonl")
    for i in range(20):
        (tmp_path / f"f{i}.tsv").write_text("x", encoding="utf-8")

    with runprov.Run("many", provenance=tmp_path / "p.json") as run:
        for i in range(20):
            with open(tmp_path / f"f{i}.tsv", encoding="utf-8") as fh:
                fh.read()

    assert len(run._opened) <= 5, "the hook must stop collecting at the bound"


def test_the_exported_name_count_in_why_md_is_the_real_one():
    """WHY.md is the positioning document — the file you hand someone who asks how this is
    different — and its differentiator bullet cited **514 statements** and **21 exported
    names** while the package had 2,149 and 27. A stale number is bad anywhere; in the
    sentence claiming smallness it is the evidence.

    The statement counts are not asserted here — coverage measures those, and pinning them in
    a test would mean editing it on every commit that adds a line. The name count is
    different: `__all__` is the public surface, it changes rarely, and a claim about it is
    checkable in one expression."""
    why = _repo_root() / "WHY.md"
    if not why.is_file():  # pragma: no cover - shipped, but a bare tree may not have it
        pytest.skip("WHY.md not present")
    text = why.read_text(encoding="utf-8")
    assert f"two of the {len(runprov.__all__)} exported names" in text, (
        f"WHY.md does not say there are {len(runprov.__all__)} exported names"
    )
    # And the claim beside it: the quickstart really does use two of them.
    block = _first_python_block(_readme())
    used = {n for n in runprov.__all__ if re.search(rf"\b{n}\b", block)}
    assert used == {"Run", "configure"}, f"the quickstart now uses {sorted(used)}"


def test_the_internal_drafts_are_not_packaged_and_not_linked():
    """L-08. Both shipped inside the 0.1.0 sdist AND were linked from the README, which is
    the `Description` PyPI freezes at upload.

    They are addressed to one person — "that decision, and the account it happens under, are
    yours" — and both keep superseded reasoning on purpose: PUBLISHING.md recommends MIT, and
    the licence has been CeCILL-B since 2026-08-07, which the same file says a section later.
    Read as a project's position rather than as somebody's notes, that is a package whose
    release procedure argues for a licence it does not use.

    Checked against the include LIST rather than a built tarball, so it costs nothing and
    fails in the same run that introduces it; `ci.py build` unpacks the real sdist and checks
    the other direction — that everything named IS present."""
    # AN UNPACKED SDIST IS NOT THE REPOSITORY, and the guard below assumes it is. `PKG-INFO`
    # sits at the sdist root and never in the repo, so it is the one reliable way to tell them
    # apart. Without this the suite the TARBALL SHIPS fails on its own tests — for exactly the
    # audience the sdist exists for, a packager who runs them — and neither `ci.py build` nor
    # the release workflow could see it, because nothing ran the shipped suite from the
    # tarball. Found by the pre-release council; introduced by the L-08 exclusion itself.
    if (_repo_root() / "PKG-INFO").is_file():  # pragma: no cover - only true inside an sdist
        pytest.skip("unpacked sdist: these files are excluded from it by design")

    pyproject = (_repo_root() / "pyproject.toml").read_text(encoding="utf-8")
    sdist = pyproject[pyproject.index("[tool.hatch.build.targets.sdist]") :]
    include = sdist[sdist.index("include = [") : sdist.index("]", sdist.index("include = ["))]
    readme = _readme()
    for name in _NOT_FOR_DISTRIBUTION:
        assert (_repo_root() / name).is_file(), f"{name} is gone; excluding it was not deleting it"
        assert f'"{name}"' not in include, f"{name} is in the sdist include list"
        assert f"]({name})" not in readme and f"/blob/main/{name})" not in readme, (
            f"the README links {name}, and the README is the description PyPI freezes"
        )
    # The list is an EXPLICIT allowlist, so this test would also pass on an empty one.
    assert '"README.md"' in include and '"LICENSE"' in include, "the list is still a real list"

    # And nothing that DOES ship may send a reader to a file that no longer does — the sdist
    # exists for a packager rebuilding from source, with no repository in reach.
    shipped = [
        n
        for n in ("CITATION.cff", "CONTRIBUTING.md", "SECURITY.md", "WHY.md", "CHANGELOG.md")
        if (_repo_root() / n).is_file()
    ] + [str(f.relative_to(_repo_root())) for f in sorted((_repo_root() / "runprov").glob("*.py"))]
    dangling = [
        (n, name)
        for n in shipped
        for name in _NOT_FOR_DISTRIBUTION
        if name in (_repo_root() / n).read_text(encoding="utf-8")
    ]
    assert not dangling, f"shipped file(s) cite a file that is not in the sdist: {dangling}"


def test_the_readme_has_no_relative_links_because_it_is_the_pypi_page():
    """The README becomes `Description` in METADATA, and PyPI does not rewrite relative
    links: they resolve against `https://pypi.org/project/runprov/` and dead-end.

    This is a test rather than a review note because the description **cannot be edited
    after upload** — the same immutability PUBLISHING.md argues for versions applies to the
    prose inside them, so the only remedy for a dead link is a new release. A pure `#anchor`
    is fine: PyPI renders the headings it points at.
    """
    bad = [
        m.group(0)
        for m in re.finditer(r"\[[^\]]+\]\(([^)]+)\)", _readme())
        if not m.group(1).startswith(("http://", "https://", "#"))
    ]
    assert not bad, (
        f"{len(bad)} link(s) that 404 on the PyPI page: {bad}. Use the absolute "
        f"https://github.com/…/blob/main/ form; a relative path only works on GitHub."
    )


def test_the_readme_quickstart_teaches_the_shape_that_actually_records():
    """I5, and it is the defect with the widest blast radius in the package's history.

    The README's front page taught `run = Run(...)` … `run.write(PROV)` for the package's
    whole life. Someone integrating it copied that shape, their script died halfway, and the
    record was lost — the exact defect the package exists to eliminate, taught by its own
    quickstart. The second shape is worse because it looks like the fix: `__exit__` only
    writes when `provenance=` reached the CONSTRUCTOR, so adding `with` while leaving
    `write()` at the end buys nothing.

    Parsed rather than grepped. A substring check for "provenance" passes on the very
    sentence that CONDEMNS the shape, so it would be green while the quickstart taught the
    defect — an unfalsifiable check, in the test guarding against unfalsifiable teaching.
    """
    tree = ast.parse(_first_python_block(_readme()))

    def is_run(node):
        return isinstance(node, ast.Call) and getattr(node.func, "id", None) == "Run"

    bound = [
        item.context_expr
        for node in ast.walk(tree)
        if isinstance(node, ast.With)
        for item in node.items
        if is_run(item.context_expr)
    ]
    every = [n for n in ast.walk(tree) if is_run(n)]

    assert bound, "the quickstart must construct Run inside a `with`"
    assert len(bound) == len(every), (
        "every Run in the quickstart must be bound by `with`; a bare `run = Run(...)` "
        "records nothing when the body raises"
    )
    for call in bound:
        assert any(k.arg == "provenance" for k in call.keywords), (
            "`with Run(...)` WITHOUT provenance= is the shape that looks like the fix and "
            "is not: __exit__ has nowhere to write, so a crash still records nothing"
        )


def test_the_constructor_documents_the_trap_and_not_merely_the_argument():
    """The other half of I5. Naming `provenance` in the signature is not documenting it:
    the argument is not guessable — two plausible shapes are silent and one is correct — so
    the docstring has to say what goes wrong, not just what the parameter is called."""
    doc = inspect.getdoc(runprov.Run) or ""
    assert "provenance:" in doc, "the constructor's Args must name provenance"

    # THE ENTRY, not the whole docstring. Checking the docstring as a whole passed while the
    # entry said only "where the sidecar goes" — the class intro happens to mention the trap
    # a dozen lines earlier, so the assertion was satisfied by text a reader looking up this
    # parameter never reaches. Mutation-tested: gutting the entry now fails.
    entry = doc[doc.index("provenance:") :]
    nxt = re.search(r"\n {0,8}\w[\w_]*:", entry)  # the next Args key at the same indent
    entry = (entry[: nxt.start()] if nxt else entry).lower()

    assert "raises" in entry or "crash" in entry, "the entry must say what happens on failure"
    assert "records nothing" in entry or "silent" in entry, (
        "it must state the CONSEQUENCE — that the run is lost — not merely the mechanism"
    )


def test_the_package_docstring_teaches_the_shape_that_actually_records():
    """The third door into I5, and the one that stayed open after the other two were shut.

    `test_the_readme_quickstart_teaches_the_shape_that_actually_records` guards the README
    and the test above guards `Run`'s own docstring, and both were green while
    `runprov/__init__.py` still opened with `run = Run(...)` … `run.write(PROV)` — the first
    row of the README's own "records nothing" table, in the module docstring, which is what
    `help(runprov)` and `pydoc runprov` print and therefore the first thing anyone reads
    after `import runprov`. The README was fixed when the shape cost someone a run; the
    package docstring was not, so the two disagreed and the wrong one was reached first.

    Parsed, not grepped, for the reason the README test gives: a substring check for
    "provenance" passes on the prose that CONDEMNS the shape.
    """
    doc = inspect.getdoc(runprov) or ""

    # The indented example, taken as the maximal blank-or-indented run after the summary.
    # Slicing to the first `with` would beg the question -- the defect under test is a
    # docstring with NO `with` in it at all, which would then yield an empty block and pass.
    lines, block = doc.splitlines(), []
    for line in lines[next(i for i, ln in enumerate(lines) if ln.startswith("    ")) :]:
        if line.strip() and not line.startswith("    "):
            break
        block.append(line)
    tree = ast.parse(textwrap.dedent("\n".join(block)))

    def is_run(node):
        return isinstance(node, ast.Call) and getattr(node.func, "id", None) == "Run"

    bound = [
        item.context_expr
        for node in ast.walk(tree)
        if isinstance(node, ast.With)
        for item in node.items
        if is_run(item.context_expr)
    ]
    every = [n for n in ast.walk(tree) if is_run(n)]

    assert every, "the package docstring must show how to construct a Run"
    assert len(bound) == len(every), (
        "every Run in the package docstring must be bound by `with`; a bare `run = Run(...)` "
        "records nothing when the body raises, and this docstring is what help() prints"
    )
    for call in bound:
        assert any(k.arg == "provenance" for k in call.keywords), (
            "`with Run(...)` WITHOUT provenance= is the shape that looks like the fix and is "
            "not: __exit__ has nowhere to write, so a crash still records nothing"
        )
    # `.write` ON THE RUN, not any `.write`: the first spelling of this caught the
    # `fh.write(run.header())` that the example is supposed to teach. The trap is the run's
    # own write(), so the receiver has to be checked, and a bare `run = Run(...)` binding is
    # checked alongside the `with ... as` one because that is the shape being excluded.
    receivers = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.With):
            for item in node.items:
                if is_run(item.context_expr) and isinstance(item.optional_vars, ast.Name):
                    receivers.add(item.optional_vars.id)
        elif isinstance(node, ast.Assign) and is_run(node.value):
            receivers.update(t.id for t in node.targets if isinstance(t, ast.Name))
    assert not any(
        isinstance(n, ast.Attribute)
        and n.attr == "write"
        and getattr(n.value, "id", None) in receivers
        for n in ast.walk(tree)
    ), "run.write() here is the trap: write() INSTEAD OF provenance= is what loses the run"


# ======================================================== I4: the Run-subclass shim
def _write_shim(tmp_path, name="shimmod"):
    """A REAL module defining a real Run subclass — the obvious refactor, in a real file.

    Not a stand-in: the defect only exists because `inspect.stack()` sees an actual frame
    from an actual file, so anything short of a real module on disk would test something
    else. This is what `scripts/audit/_provenance.py` is in the host project.
    """
    src = tmp_path / f"{name}.py"
    src.write_text(
        textwrap.dedent(
            """
            import runprov

            class ShimRun(runprov.Run):
                def __init__(self, script, params=None, **kw):
                    super().__init__(script, params, **kw)
            """
        ),
        encoding="utf-8",
    )
    sys.path.insert(0, str(tmp_path))
    try:
        return importlib.import_module(name), src
    finally:
        sys.path.remove(str(tmp_path))


def test_a_run_subclass_records_its_caller_not_the_shim(tmp_path, monkeypatch):
    """I4. A `Run` factory or subclass is the obvious refactor, and it made every script
    record THE FACTORY as `script_file` — because a shim frame is the first frame outside
    the package and the stack walk had no way to know it was a shim.

    Live in the host project: `extract_runprov` recorded
    `scripts/audit/_provenance.py`. The consequence reaches the rendered log, where the
    `script:` field then names something other than what ran — the exact defect that field
    was added to replace.
    """
    monkeypatch.chdir(tmp_path)
    mod, shim_src = _write_shim(tmp_path)
    runprov.configure(root=tmp_path, run_log=tmp_path / "runs.jsonl")
    with mod.ShimRun("s", provenance=tmp_path / "p.json") as run:
        pass
    recorded = pathlib.Path(run.record["code"]["script_file"]).resolve()
    assert recorded != shim_src.resolve(), "the shim must not be recorded as the script"
    assert recorded == pathlib.Path(__file__).resolve(), "the CALLER is the script"


def test_a_subclass_used_in_its_own_file_still_records_that_file(tmp_path):
    """The fix must not overshoot. If a script defines its own `Run` subclass AND uses it,
    that file IS the script — skipping it outright would record `script_file: null` for
    exactly the self-contained script the walk exists to find.

    Run as a REAL entry point, in a subprocess. Importing it and calling in from here would
    put this test file on the stack as a genuine outer caller, and then the right answer is
    debatable rather than known — which would make the test's verdict an artefact of how the
    test was built. `python selfcontained.py` is the case the rule is written for.
    """
    src = tmp_path / "selfcontained.py"
    src.write_text(
        textwrap.dedent(
            f"""
            import json, pathlib, sys
            sys.path.insert(0, {str(REPO)!r})
            import runprov

            class LocalRun(runprov.Run):
                pass

            runprov.configure(root=pathlib.Path({str(tmp_path)!r}),
                              run_log=pathlib.Path({str(tmp_path / "runs.jsonl")!r}))
            with LocalRun("s", provenance=pathlib.Path({str(tmp_path / "p.json")!r})) as r:
                print(json.dumps(r.record["code"]["script_file"]))
            """
        ),
        encoding="utf-8",
    )
    out = subprocess.run(
        [sys.executable, str(src)], capture_output=True, text=True, cwd=tmp_path, check=True
    )
    got = json.loads(out.stdout.splitlines()[0])
    assert got is not None, "a self-contained script must not record script_file: null"
    assert pathlib.Path(got).resolve() == src.resolve()


def test_a_subclass_whose_module_has_no_file_is_simply_not_preferred_against(tmp_path, monkeypatch):
    """A `Run` subclass defined in a notebook cell, an `exec`'d string, or a REPL has a
    `__module__` with no `__file__`. There is no path to prefer against, so the walk
    proceeds exactly as it would have — the shim rule must degrade to the old behaviour
    rather than raise or record nothing."""
    monkeypatch.chdir(tmp_path)
    runprov.configure(root=tmp_path, run_log=tmp_path / "runs.jsonl")
    # A class object, so N806 does not apply the way ruff reads it — bound to a lowercase
    # name to keep the linter honest rather than silenced with a noqa.
    dynamic_cls = type("Dynamic", (runprov.Run,), {"__module__": "not_an_imported_module"})
    assert runprov.run._subclass_files(dynamic_cls) == frozenset()
    with dynamic_cls("s", provenance=tmp_path / "p.json") as run:
        pass
    assert run.record["code"]["script_file"].endswith("test_runprov.py")


def test_the_caller_file_falls_back_to_none_when_every_frame_is_internal(monkeypatch):
    monkeypatch.setattr(runprov.run.inspect, "stack", lambda: [])
    assert runprov.run._caller_file() is None


# ============================================================ coverage: the FIVE branches
# statement coverage said 100% while these five conditions had never been evaluated both
# ways. That is the ledger's own meta-finding arriving a second time: the C0a state machine
# was broken in four ways with 91/91 green, because a line that RAN is not a behaviour that
# was CHECKED. Each test below is named for the behaviour, not for the branch.


def test_a_with_block_with_no_provenance_and_no_write_records_nothing(
    tmp_path, monkeypatch, capsys
):
    """The one KNOWN gap in the state machine, which had no test — so nothing would have
    noticed if it silently changed. `_finish` reaches its history clause with `_written`
    false and `provenance_path` None, writes no sidecar, and appends no line.

    This is documented in the remediation ledger as "confirmed unchanged" and pinned here
    for the first time. It is deliberately a CHARACTERISATION test: it asserts the current
    behaviour so that changing it becomes a decision rather than an accident.

    THE SECOND ASSERTION IS THE LOAD-BEARING ONE. "No line was written" is also true when
    the append CRASHED — with no `write()` there is no `finished_utc`, so an
    `_append_history` reached by mistake raises KeyError, `__exit__` swallows it as a
    capture failure, and the file is equally absent. Mutation-tested: removing the
    `_deferred_history is not None` guard leaves the first assertion green and this one
    red. A test that cannot tell "correctly recorded nothing" from "died trying" is the
    unfalsifiable shape this repository keeps finding.
    """
    monkeypatch.chdir(tmp_path)
    log = tmp_path / "runs.jsonl"
    runprov.configure(root=tmp_path, run_log=log)
    capsys.readouterr()
    with runprov.Run("silent") as run:
        run.note("did_work", True)
    err = capsys.readouterr().err
    assert not log.exists(), "no provenance= and no write() records NOTHING — including here"
    assert "provenance capture failed" not in err, (
        "it must record nothing BY DESIGN, not by crashing on the way to recording"
    )


def test_a_module_whose_resolved_file_is_not_a_file_records_no_hash(tmp_path, monkeypatch):
    """`module()`'s own comment claims "no sha256 key means the resolved path is not a
    readable file". That claim had never been executed — a namespace package resolves to a
    directory, and hashing it would raise inside provenance capture."""
    monkeypatch.chdir(tmp_path)
    runprov.configure(root=tmp_path, run_log=tmp_path / "runs.jsonl")
    fake = types.ModuleType("nspkg")
    fake.__file__ = str(tmp_path / "nspkg")  # a directory, not a file
    (tmp_path / "nspkg").mkdir()
    with runprov.Run("m", provenance=tmp_path / "p.json") as run:
        run.module(fake)
    rec = run.record["modules"][0]
    assert "sha256" not in rec, "an unhashable resolved path must not invent a hash"
    assert rec["resolved_file"].endswith("nspkg")


def test_a_module_inside_the_project_is_recorded_without_a_warning(tmp_path, monkeypatch, capsys):
    """The quiet half of `module()`. Only the OUTSIDE case was tested, so the warning had
    never been shown not to fire — a warning that always fires is the same defect as one
    that never does."""
    monkeypatch.chdir(tmp_path)
    runprov.configure(root=tmp_path, run_log=tmp_path / "runs.jsonl")
    src = tmp_path / "inside.py"
    src.write_text("x = 1\n", encoding="utf-8")
    fake = types.ModuleType("inside")
    fake.__file__ = str(src)
    with runprov.Run("m", provenance=tmp_path / "p.json") as run:
        capsys.readouterr()
        run.module(fake)
        err = capsys.readouterr().err
    rec = run.record["modules"][0]
    assert rec["inside_project"] is True and "sha256" in rec
    assert "resolved OUTSIDE" not in err, "a module inside the project must not warn"


def test_a_status_line_whose_path_is_empty_yields_no_path():
    """`if p:` inside the rename split had only ever been true. git reports an untracked
    directory as `?? pkg/`, and `rstrip("/")` on a bare `/` leaves the empty string — a
    path that must be dropped rather than recorded as ''."""
    restored, paths = runprov.project._split_status("?? /")
    assert paths == [], "an empty component must not become a recorded path"
    assert restored == "?? /"


def test_an_unreadable_distribution_is_skipped_when_no_collector_is_passed(monkeypatch):
    """`installed_packages()` takes `unreadable` optionally, and every test had passed one.
    With none, an unnameable distribution must still be skipped rather than keyed on ''."""
    import importlib.metadata as md

    class Broken:
        _path = "/nowhere/broken.dist-info"
        version = "1.0"

        @property
        def metadata(self):
            raise RuntimeError("unreadable metadata")

    monkeypatch.setattr(md, "distributions", lambda: [Broken()])
    assert runprov.environment.installed_packages() == {}, "no collector, no crash, no '' key"


# =================================================================== coverage: the sinks
def test_a_sink_that_cannot_write_warns_instead_of_raising(tmp_path, capsys):
    """A sink that can abort a run gets removed from the run, and then nothing is
    recorded at all."""
    blocker = tmp_path / "afile"
    blocker.write_text("not a directory", encoding="utf-8")
    runprov.JsonlSink(blocker / "sub" / "runs.jsonl").append({"a": 1})
    cap = capsys.readouterr()
    assert "could not append to run history" in cap.err and cap.out == ""


def test_the_posix_lock_path_is_exercised(monkeypatch, tmp_path):
    """The MIRROR of the Windows test, and it exists because 100% coverage turned out to
    be platform-dependent: on Linux the fcntl branch runs and msvcrt is faked, and on
    Windows exactly the reverse, so each platform missed the other's lines and the
    Windows runner failed at 99.12%. Faking BOTH on BOTH makes the number mean the same
    thing everywhere -- which is the only way a coverage gate is worth having."""
    calls = []

    class FakeFcntl:
        LOCK_EX = 2
        LOCK_UN = 8

        @staticmethod
        def flock(fd, op):
            calls.append(op)

    monkeypatch.setitem(sys.modules, "fcntl", FakeFcntl)
    p = tmp_path / "runs.jsonl"
    runprov.JsonlSink(p).append({"a": 1})
    assert calls == [FakeFcntl.LOCK_EX, FakeFcntl.LOCK_UN]
    assert json.loads(p.read_text(encoding="utf-8"))["a"] == 1


def test_the_windows_lock_path_is_exercised(monkeypatch, tmp_path):
    """The msvcrt branch cannot run on this platform, so the BRANCH is tested with a stand
    -in module. That is not the same as testing Windows -- CI does that on a real runner --
    but it does prove the code selects and releases the right lock, which is what failed
    when 24 concurrent appends produced 23 lines."""
    calls = []

    class FakeMsvcrt:
        LK_LOCK = 1
        LK_UNLCK = 0

        @staticmethod
        def locking(fd, mode, nbytes):
            calls.append(mode)

    monkeypatch.setitem(sys.modules, "msvcrt", FakeMsvcrt)
    monkeypatch.setattr(sys, "platform", "win32")

    real_import = builtins.__import__

    def no_fcntl(name, *a, **k):
        if name == "fcntl":
            raise ImportError("no fcntl on this pretend platform")
        return real_import(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", no_fcntl)
    p = tmp_path / "runs.jsonl"
    runprov.JsonlSink(p).append({"a": 1})
    assert calls == [FakeMsvcrt.LK_LOCK, FakeMsvcrt.LK_UNLCK]
    assert json.loads(p.read_text(encoding="utf-8"))["a"] == 1


def test_no_locking_available_says_so(monkeypatch, tmp_path, capsys):
    real_import = builtins.__import__

    def nothing(name, *a, **k):
        if name in ("fcntl", "msvcrt"):
            raise ImportError("neither")
        return real_import(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", nothing)
    monkeypatch.setattr(sys, "platform", "win32")
    runprov.JsonlSink(tmp_path / "runs.jsonl").append({"a": 1})
    cap = capsys.readouterr()
    assert "no file locking available" in cap.err and cap.out == ""


def test_cli_filters_by_script(tmp_path, capsys):
    p = tmp_path / "runs.jsonl"
    p.write_text(
        "\n".join(json.dumps({"script": n}) for n in ("build", "train", "build")),
        encoding="utf-8",
    )
    cli.main(["log", "--log", str(p), "--script", "build"])
    out = capsys.readouterr()
    assert "train" not in out.out and "2 of 3" in out.err


def test_git_returns_none_when_the_binary_is_absent(monkeypatch, tmp_path):
    """`git` may simply not be installed. Provenance capture that raises there would
    abort the run it is supposed to be describing."""

    def no_git(*a, **k):
        raise FileNotFoundError("git: command not found")

    monkeypatch.setattr(runprov.project.subprocess, "run", no_git)
    assert runprov.git(tmp_path, "rev-parse", "HEAD") is None


# ============================================== C0a / C0b — the two wrong-record defects
def test_a_failure_after_write_is_not_recorded_as_ok(tmp_path):
    """WRONG RECORD, and the worst kind. `__exit__` set status=failed and then declined to
    persist it because `_written` was already True, so a script that dies in teardown, a
    final assertion, an atexit flush or a `finally` reported SUCCESS. The suite blessed the
    exact shape: test_a_successful_with_block_writes_once_not_twice writes inside the
    block. A record that has become false must be corrected, not skipped."""
    sink = runprov.MemorySink()
    proj = runprov.Project(root=tmp_path, sink=sink, run_id=lambda: "r", generation=lambda: "g")
    with pytest.raises(RuntimeError):
        with runprov.Run("step", project=proj, provenance=tmp_path / "p.json") as run:
            run.write(tmp_path / "p.json")
            raise RuntimeError("the real work blew up after the record was written")

    rec = json.loads((tmp_path / "p.json").read_text(encoding="utf-8"))
    assert rec["status"] == "failed"
    assert rec["failure"]["type"] == "RuntimeError"
    assert "blew up" in rec["failure"]["message"]
    # The history must not claim success either, and must not double-count the run.
    assert [r["status"] for r in sink.records] == ["failed"], sink.records


def test_the_pin_does_not_depend_on_registration_order(tmp_path):
    """THE determinism guarantee. `header()` rendered inputs in registration order and
    `Path.glob()` is filesystem order, so `for p in DIR.glob("*.tsv"): run.input(p)` gave a
    different pin on a different machine -- the 80-artifacts-CHANGED regression the
    method's own docstring exists to prevent, through a different door. The guard test
    registered in the same order both times and could not fail on this."""
    for name in ("aa.tsv", "zz.tsv", "mm.tsv", "bb.tsv"):
        (tmp_path / name).write_text(name, encoding="utf-8")
    proj = runprov.Project(
        root=tmp_path,
        sink=runprov.MemorySink(),
        run_id=lambda: "r",
        generation=lambda: "g",
    )

    def pin(order: list[str]) -> str:
        run = runprov.Run("t", project=proj)
        for name in order:
            run.input(tmp_path / name)
        return run.header()

    forward = ["aa.tsv", "bb.tsv", "mm.tsv", "zz.tsv"]
    assert pin(forward) == pin(list(reversed(forward)))
    assert pin(forward) == pin(["mm.tsv", "aa.tsv", "zz.tsv", "bb.tsv"])


def test_the_pin_deduplicates_a_repeatedly_registered_input(tmp_path):
    """Outputs were deduplicated and inputs were not, so a data-dependent read loop
    produced a different pin from identical data."""
    src = tmp_path / "x.tsv"
    src.write_text("id\n1\n", encoding="utf-8")
    proj = runprov.Project(
        root=tmp_path,
        sink=runprov.MemorySink(),
        run_id=lambda: "r",
        generation=lambda: "g",
    )
    once = runprov.Run("t", project=proj)
    once.input(src)
    thrice = runprov.Run("t", project=proj)
    for _ in range(3):
        thrice.input(src)
    assert once.header() == thrice.header()
    assert thrice.header().count("x.tsv") == 1


def test_the_sidecar_still_records_every_registration(tmp_path):
    """Deduplication belongs to the PIN, not to the record. How many times a script opened
    a file is a fact about the run, and the sidecar is where facts about the run live."""
    src = tmp_path / "x.tsv"
    src.write_text("id\n1\n", encoding="utf-8")
    run = runprov.Run("t", project=_project(tmp_path))
    for _ in range(3):
        run.input(src)
    assert len(run.record["inputs"]) == 3


# ===================================================== X3 — the sink guard, made real
def test_a_sink_whose_append_takes_the_wrong_arguments_is_refused(tmp_path):
    """`runtime_checkable` checks attribute PRESENCE, not signature -- so the guard whose
    docstring promised failure "at configuration, not at the end of a two-hour run"
    accepted anything with an `append` attribute, including a bare list. Verified before
    the fix: isinstance([], RecordSink) is True."""

    class WrongArity:
        def append(self) -> None:  # no record parameter
            pass

    with pytest.raises(TypeError, match="append"):
        runprov.configure(root=tmp_path, sink=WrongArity())
    runprov.configure(root=tmp_path)


def test_a_plain_list_IS_a_valid_sink(tmp_path):
    """The council called `configure(sink=[])` a hole. It is not, and I wrote a test
    demanding it be refused before checking: `list.append(record)` satisfies the protocol
    honestly, and a caller who HOLDS the list gets every record -- that is exactly what
    MemorySink is. Refusing it would reject a legitimate use to catch a typo.

    What was genuinely broken is narrower: the guard's message promised a signature check
    it never performed, so wrong-arity and non-callable `append` sailed through and blew up
    at the end of the run instead. Those now raise at configuration."""
    records: list[dict] = []
    proj = runprov.configure(root=tmp_path, sink=records)
    runprov.Run("t", project=proj).write(tmp_path / "p.json")
    assert len(records) == 1 and records[0]["script"] == "t"
    runprov.configure(root=tmp_path)


def test_an_append_that_is_not_callable_is_refused(tmp_path):
    class NotCallable:
        append = 3

    with pytest.raises(TypeError, match="append"):
        runprov.configure(root=tmp_path, sink=NotCallable())
    runprov.configure(root=tmp_path)


# ===================== provenance capture must never replace the failure it is recording
def test_a_failing_sidecar_write_does_not_replace_the_users_exception(tmp_path, capsys):
    """The worst failure available to this package. The run raised RuntimeError; __exit__
    then tried to persist under an unwritable path, and NotADirectoryError propagated in
    its place -- so the diagnostic the user needed was destroyed BY the thing recording it,
    and nothing was recorded either. `sinks.py` already states the rule for sinks ("a sink
    that can abort a run gets removed from the run"); the sidecar path never honoured it."""
    blocker = tmp_path / "blocked"
    blocker.write_text("a file, not a directory", encoding="utf-8")
    sink = runprov.MemorySink()
    proj = runprov.Project(root=tmp_path, sink=sink, run_id=lambda: "r", generation=lambda: "g")
    with pytest.raises(RuntimeError, match="THE REAL FAILURE"):
        with runprov.Run("f", project=proj, provenance=blocker / "sub" / "p.json"):
            raise RuntimeError("THE REAL FAILURE the user needs to see")
    # and the failure must still reach the history, which does not need the filesystem
    assert [r["status"] for r in sink.records] == ["failed"]
    assert "could not write" in capsys.readouterr().err


def test_an_unserialisable_note_does_not_lose_the_run(tmp_path):
    """A tuple-keyed dict is what a per-class confusion matrix looks like, and
    `default=str` does not apply to KEYS -- so json.dumps raised, the sidecar was never
    written, and the run vanished. Serialise before touching the filesystem.

    THE MEASUREMENT NOW SURVIVES. This used to assert that the note was replaced wholesale
    by an `UNSERIALISABLE` marker, which recorded the run and threw the confusion matrix
    away. `_jsonable` stringifies the KEY instead, so the numbers are still there and still
    attributable -- degrading the part that cannot encode rather than the value containing
    it, which is the same rule one level down."""
    sink = runprov.MemorySink()
    proj = runprov.Project(root=tmp_path, sink=sink, run_id=lambda: "r", generation=lambda: "g")
    run = runprov.Run("g", project=proj)
    run.note("confusion", {(1, "a"): 0.9})
    run.write(tmp_path / "p.json")
    assert len(sink.records) == 1, "the run must be recorded even if a note cannot encode"
    rec = json.loads((tmp_path / "p.json").read_text(encoding="utf-8"))
    assert list(rec["notes"]["confusion"].values()) == [0.9], "the measurement survives"
    assert "1" in next(iter(rec["notes"]["confusion"])), "under a stringified key"
    assert sink.records[0]["notes"] == rec["notes"], "and the HISTORY agrees with the sidecar"


# ================= regressions introduced by the deferred-history change, found by review
def _sinked(tmp_path):
    sink = runprov.MemorySink()
    return sink, runprov.Project(
        root=tmp_path, sink=sink, run_id=lambda: "r", generation=lambda: "g"
    )


def test_the_sidecar_and_the_history_agree_without_a_provenance_kwarg(tmp_path):
    """SPLIT BRAIN. The exit-time correction was gated on `provenance_path is not None`,
    but the path actually written is known independently. Before: history `failed`,
    sidecar `ok` -- and the sidecar is the file a human opens. Two contradictory answers
    with no rule for which wins is worse than the single wrong answer it replaced."""
    sink, proj = _sinked(tmp_path)
    with pytest.raises(RuntimeError):
        with runprov.Run("x", project=proj) as run:  # no provenance=
            run.write(tmp_path / "p.json")
            raise RuntimeError("boom")
    assert [r["status"] for r in sink.records] == ["failed"]
    assert json.loads((tmp_path / "p.json").read_text(encoding="utf-8"))["status"] == "failed"


def test_one_run_is_one_history_line_even_across_write_then_with(tmp_path):
    """`_written` means "a sidecar exists", which is not "the history has this run".
    Conflating them appended twice, and both lines carry the same run_id -- so any tally
    over runs.jsonl was silently inflated."""
    sink, proj = _sinked(tmp_path)
    run = runprov.Run("y", project=proj)
    run.write(tmp_path / "p.json")
    with run:
        run.write(tmp_path / "p.json")
    assert len(sink.records) == 1


def test_one_run_is_one_history_line_however_often_write_is_called(tmp_path):
    """The `_history_appended` guard, which is what this has always been about: `write()` is
    callable as often as you like and must still append exactly one line.

    It used to make that point with two `with` blocks. Re-entry now raises (see
    `test_a_run_refuses_to_be_entered_twice`), which guarantees the same property more
    strongly — there cannot be a second block — so the guard is exercised the way it is
    actually reachable: repeated `write()` inside one block, and again after it."""
    sink, proj = _sinked(tmp_path)
    run = runprov.Run("z", project=proj, provenance=tmp_path / "p.json")
    with run:
        run.write(tmp_path / "p.json")
        run.write(tmp_path / "p.json")
    run.write(tmp_path / "p.json")
    assert len(sink.records) == 1


def test_a_clean_sys_exit_is_not_a_failure(tmp_path):
    """`raise SystemExit(main())` is how a CLI ends. Treating any non-None exc_type as a
    failure poisons the exact query the deferral was built to make trustworthy."""
    sink, proj = _sinked(tmp_path)
    with pytest.raises(SystemExit):
        with runprov.Run("q", project=proj, provenance=tmp_path / "p.json") as run:
            run.write(tmp_path / "p.json")
            raise SystemExit(0)
    assert [r["status"] for r in sink.records] == ["ok"]
    assert json.loads((tmp_path / "p.json").read_text(encoding="utf-8"))["status"] == "ok"


def test_a_nonzero_sys_exit_is_still_a_failure(tmp_path):
    sink, proj = _sinked(tmp_path)
    with pytest.raises(SystemExit):
        with runprov.Run("q", project=proj, provenance=tmp_path / "p.json"):
            raise SystemExit(2)
    assert [r["status"] for r in sink.records] == ["failed"]


def test_a_failure_inside_the_exit_capture_is_reported_not_raised(tmp_path, capsys):
    """The last-resort net. `_persist` and the sink already swallow individually; this
    covers anything else that could throw while an exception is in flight -- the one
    moment when replacing the exception destroys the diagnostic AND the record."""
    _, proj = _sinked(tmp_path)
    run = runprov.Run("t", project=proj, provenance=tmp_path / "p.json")

    def boom() -> None:
        raise RuntimeError("capture itself broke")

    run._finish = boom  # type: ignore[method-assign]
    with pytest.raises(ValueError, match="the user's failure"):
        with run:
            raise ValueError("the user's failure")
    assert "provenance capture failed during exit" in capsys.readouterr().err


def test_encoding_skips_a_section_that_is_not_a_dict(tmp_path):
    """`parameters` is recorded verbatim from the caller, so it is not guaranteed to be a
    mapping. The degrade path must not assume it is."""
    _, proj = _sinked(tmp_path)
    run = runprov.Run("t", project=proj)
    run.record["parameters"] = ["not", "a", "dict"]
    run.note("bad", {(1, 2): "tuple key"})
    run.write(tmp_path / "p.json")
    rec = json.loads((tmp_path / "p.json").read_text(encoding="utf-8"))
    assert rec["parameters"] == ["not", "a", "dict"]
    assert rec["notes"]["bad"] == {"(1, 2)": "tuple key"}, "the key is stringified, not dropped"


def test_a_config_that_points_at_itself_still_produces_a_record(tmp_path):
    """L-01. A config node holding a `parent` back-reference is how most hierarchical config
    libraries represent a tree, and `parameters` is documented as "what argparse actually
    parsed" — so this value arrives by the ordinary route. Unguarded, `_jsonable` recursed
    until `RecursionError`, which is a `RuntimeError` and so fell past the degrade path's
    `except (TypeError, ValueError)`. The run then exited 0 having written NO history line
    and NO sidecar: the one failure this package exists to prevent, arriving through its own
    sanitiser. The cycle must cost the caller that one field and nothing else."""
    proj = _project(tmp_path)
    log = tmp_path / "runs.jsonl"
    cyc = {"name": "cfg"}
    cyc["parent"] = cyc
    with runprov.Run("s", {"cfg": cyc}, project=proj, provenance=tmp_path / "p.json") as run:
        run.note("ok", 1)

    assert (tmp_path / "p.json").exists(), "the sidecar must survive a cyclic parameter"
    rec = json.loads(log.read_text(encoding="utf-8").splitlines()[0])
    assert rec["parameters"]["cfg"]["parent"] == "<circular reference>"
    assert rec["parameters"]["cfg"]["name"] == "cfg", "the rest of the node is still recorded"
    assert rec["notes"]["ok"] == 1, "an unrelated note is untouched"


def test_shared_structure_is_recorded_twice_and_is_not_a_cycle(tmp_path):
    """The guard tracks the ids on the CURRENT PATH, not every id seen. One dict passed as
    two parameters is ordinary sharing — a config section reused by two steps — and both
    sightings must record the value. A `seen` set that never forgets would call the second
    one a cycle and silently blank a field that is perfectly serialisable."""
    proj = _project(tmp_path)
    log = tmp_path / "runs.jsonl"
    shared = {"a": 1}
    with runprov.Run(
        "s",
        {"x": shared, "y": shared, "z": [shared, shared]},
        project=proj,
        provenance=tmp_path / "p.json",
    ):
        pass
    rec = json.loads(log.read_text(encoding="utf-8").splitlines()[0])
    assert rec["parameters"] == {"x": {"a": 1}, "y": {"a": 1}, "z": [{"a": 1}, {"a": 1}]}


def test_a_parameter_nested_past_the_cap_is_named_rather_than_lost(tmp_path):
    """Depth alone lost the record too, with no cycle anywhere: 2,000 nested dicts is a
    `RecursionError` in the same place. The cap states itself in the record — the rule the
    NaN branch already follows, and the reason `git_other_files_omitted` exists."""
    proj = _project(tmp_path)
    log = tmp_path / "runs.jsonl"
    deep = "leaf"
    for _ in range(2000):
        deep = {"k": deep}
    with runprov.Run("deep", {"d": deep}, project=proj, provenance=tmp_path / "p.json"):
        pass

    assert (tmp_path / "p.json").exists()
    body = log.read_text(encoding="utf-8")
    assert f"<nested beyond {runprov.run.JSONABLE_MAX_DEPTH} levels>" in body
    walk, levels = json.loads(body.splitlines()[0])["parameters"]["d"], 0
    while isinstance(walk, dict):
        walk, levels = walk["k"], levels + 1
    assert walk == f"<nested beyond {runprov.run.JSONABLE_MAX_DEPTH} levels>"
    # One less than the cap, and the arithmetic is worth pinning: `parameters` is itself the
    # first level the walk descends, so a value reached at `parameters["d"]` keeps 99 of its
    # own levels. Asserting the exact number is what caught this off by one when the guard
    # was written -- a cap whose stated size is not the size it applies is the kind of
    # almost-true number this package exists to refuse.
    assert levels == runprov.run.JSONABLE_MAX_DEPTH - 1, (
        f"the walk stopped after {levels} levels, not at the stated cap"
    )


def test_a_value_that_recurses_while_being_stringified_costs_one_entry_not_the_run(tmp_path):
    """L-01, second half. `_jsonable`'s depth cap cannot help here: the value is a plain
    object, so it passes through untouched and the recursion happens inside `json.dumps`'s
    `default=str`. The degrade path is what must catch it — and it caught only `TypeError`
    and `ValueError`, so a `RecursionError` (a `RuntimeError`) fell past a two-name `except`
    that had been correct for years and lost the entire record.

    Written because removing `RecursionError` from `SERIALISATION_ERRORS` left every other
    test in this file green: the cap above hides the raise from the guard below, so the two
    halves of the fix have to be tested apart."""

    class Recursive:
        def __str__(self) -> str:
            return str(self)

    proj = _project(tmp_path)
    log = tmp_path / "runs.jsonl"
    with runprov.Run("s", project=proj, provenance=tmp_path / "p.json") as run:
        run.note("fine", 1)
        run.note("bad", Recursive())

    assert (tmp_path / "p.json").exists(), "one bad note must not cost the sidecar"
    rec = json.loads(log.read_text(encoding="utf-8").splitlines()[0])
    assert rec["notes"]["bad"].startswith("UNSERIALISABLE")
    assert rec["notes"]["fine"] == 1, "the entries beside it are untouched"


def _consumer(tmp_path) -> pathlib.Path:
    """Write a script that emits DATA on stdout and records provenance while doing it.

    This is the shape the defect was reported in and the shape a pipeline step actually
    has: the artifact goes to stdout, the provenance goes to a sidecar.
    """
    (tmp_path / "in.tsv").write_text("id\tvalue\n1\t2\n", encoding="utf-8")
    script = tmp_path / "step.py"
    script.write_text(
        textwrap.dedent(f"""
            import pathlib, sys
            sys.path.insert(0, {str(REPO)!r})
            import runprov

            root = pathlib.Path({str(tmp_path)!r})
            proj = runprov.Project(
                root=root,
                run_log=root / "runs.jsonl",
                run_id=lambda: "r",
                generation=lambda: "g",
            )
            run = runprov.Run("step", project=proj)
            run.input(root / "in.tsv")
            out = run.output(root / "out.tsv")
            out.write_text("x", encoding="utf-8")
            run.module(runprov)
            sys.stdout.write("id\\tvalue\\n1\\t2\\n")   # THE DATA, and nothing else may join it
            run.write(root / "prov.json")
        """),
        encoding="utf-8",
    )
    return script


def test_a_consumer_that_writes_data_to_stdout_gets_nothing_from_runprov_on_stdout(
    tmp_path, monkeypatch
):
    """`python step.py > result.tsv` must produce result.tsv and NOTHING else.

    Byte-exact, not "does not contain 'provenance'": the point is that stdout is the
    caller's channel and the library has no business on it at all.
    """
    import subprocess

    monkeypatch.delenv("RUNPROV_QUIET", raising=False)  # the summary must be ON for this
    proc = subprocess.run(
        [sys.executable, str(_consumer(tmp_path))], capture_output=True, timeout=120
    )
    assert proc.returncode == 0, proc.stderr.decode("utf-8", "replace")
    assert proc.stdout.splitlines() == [b"id\tvalue", b"1\t2"], (
        f"stdout must be the caller's data and only the caller's data; got {proc.stdout!r}"
    )
    # ... and the provenance still SAID something. Silence would be the other failure.
    err = proc.stderr.decode("utf-8", "replace")
    assert "provenance -> " in err and "resolved OUTSIDE" in err


def test_the_dirty_tree_warning_reaches_stderr_and_never_the_data(tmp_path, monkeypatch):
    """The load-bearing message: the recorded commit does not identify what ran.

    Checked through a real redirect, because this is the one message that must survive
    every future attempt to make the package quieter.
    """
    import subprocess

    monkeypatch.delenv("RUNPROV_QUIET", raising=False)
    repo = tmp_path / "r"
    (repo / "src").mkdir(parents=True)
    for cmd in (
        ["git", "init", "-q", str(repo)],
        ["git", "-C", str(repo), "config", "user.email", "t@e.org"],
        ["git", "-C", str(repo), "config", "user.name", "T"],
    ):
        subprocess.run(cmd, check=True)
    (repo / "src" / "a.py").write_text("x = 1\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", "one"], check=True)
    (repo / "src" / "a.py").write_text("x = 2\n", encoding="utf-8")  # now dirty

    proc = subprocess.run([sys.executable, str(_consumer(repo))], capture_output=True, timeout=120)
    assert proc.returncode == 0, proc.stderr.decode("utf-8", "replace")
    err = proc.stderr.decode("utf-8", "replace")
    assert "CODE is modified relative to git_commit" in err
    assert "src/a.py" in err, "the warning must name the files, not just announce itself"
    assert proc.stdout.splitlines() == [b"id\tvalue", b"1\t2"]


def test_quiet_cannot_silence_a_warning(tmp_path, monkeypatch, capsys):
    """The invariant that makes the switch safe to have at all: a knob that can turn off a
    provenance warning is the knob that gets turned off. `RUNPROV_QUIET` reaches
    `summary()` and nothing else."""
    monkeypatch.setenv("RUNPROV_QUIET", "1")
    run = runprov.Run("t", project=_project(tmp_path))
    run.module(json)  # resolves outside the project -> a diagnostic
    run.record["status"] = "failed"
    run.record["failure"] = {"type": "Boom", "message": "it broke", "traceback": ""}
    run.write(tmp_path / "prov.json")
    cap = capsys.readouterr()
    assert cap.out == ""
    assert "resolved OUTSIDE" in cap.err
    assert "RUN FAILED" in cap.err
    assert "provenance -> " not in cap.err  # the summary, and only the summary, is hidden


def test_the_summary_is_on_by_default_and_a_falsey_value_keeps_it_on(tmp_path, monkeypatch, capsys):
    """The other state. `RUNPROV_QUIET=0` meaning *quiet* is exactly the surprise that
    gets a switch blamed for a missing message."""
    for value in (None, "", "0", "false", "NO", " off "):
        if value is None:
            monkeypatch.delenv("RUNPROV_QUIET", raising=False)
        else:
            monkeypatch.setenv("RUNPROV_QUIET", value)
        run = runprov.Run("t", project=_project(tmp_path))
        run.write(tmp_path / "prov.json")
        cap = capsys.readouterr()
        assert cap.out == "", f"RUNPROV_QUIET={value!r} must not put anything on stdout"
        assert "provenance -> " in cap.err, f"RUNPROV_QUIET={value!r} must not silence"
        assert "seeds []" in cap.err


def test_no_library_module_calls_bare_print():
    """The regression guard, and it is structural on purpose.

    Eight call sites drifted onto stdout one at a time; asserting the behaviour of the
    eight that exist today does not stop the ninth. `_report.py` is the ONE place allowed
    to call `print`, and `__main__.py` is a CLI whose rendered log IS its output.

    This sees CALLS, not behaviour — a `sys.stdout.write` would slip past it — which is why
    the subprocess tests above exist as well. It is the cheap half of a pair.

    Parsed rather than grepped. The regex version flagged a COMMENT: a note in
    `terminal.py` explaining that a bare `print()` resolves `sys.stdout` at call time,
    which is exactly the kind of comment this module should contain. A guard whose price is
    the explanations gets paid by deleting the explanations, so it reads the tree instead —
    strictly stronger, since a string or comment was never a call in the first place.
    """
    offenders = {}
    for mod in sorted((REPO / "runprov").glob("*.py")):
        if mod.name in ("_report.py", "__main__.py"):
            continue
        tree = ast.parse(mod.read_text(encoding="utf-8"))
        hits = [
            f"{mod.name}:{node.lineno}"
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "print"
        ]
        if hits:
            offenders[mod.name] = hits
    assert offenders == {}, (
        f"library modules must emit through runprov._report, not print(): {offenders}"
    )


def test_a_posix_locking_failure_is_announced(monkeypatch, tmp_path, capsys):
    """The notice sat inside the win32-only branch, so a POSIX `flock` that raised -- NFS,
    CIFS, a container without the syscall -- degraded to an unlocked append in SILENCE.
    Those filesystems are the entire justification for having the lock, so that was the
    one case that most deserved announcing and the one case that could not."""
    real_import = builtins.__import__

    def no_fcntl(name, *a, **k):
        if name == "fcntl":
            raise ImportError("pretend NFS")
        return real_import(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", no_fcntl)
    monkeypatch.setattr(sys, "platform", "linux")
    p = tmp_path / "runs.jsonl"
    runprov.JsonlSink(p).append({"a": 1})
    assert json.loads(p.read_text(encoding="utf-8"))["a"] == 1
    assert "no file locking available" in capsys.readouterr().err


def _repo_with(tmp_path, subdir):
    """A REAL git repository with one committed module under `subdir`, plus one data file.

    Real rather than a stub, for the reason the file docstring gives: the defect lives in
    what `git status` reports for a layout, and a faked git can only report what the author
    of the fake already believed.
    """
    import subprocess

    repo = tmp_path / "integration"
    (repo / subdir).mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "t@e.org"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "T"], check=True)
    (repo / subdir / "mod.py").write_text("x = 1\n", encoding="utf-8")
    (repo / "data.tsv").write_text("id\n1\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", "one"], check=True)
    return repo


def test_uncommitted_code_outside_the_default_code_paths_is_not_reported_as_clean(tmp_path):
    """I2. `code_paths` was the PATHSPEC of `git status`, so a project laid out any other
    way -- `pkg/` rather than `src/` -- edited uncommitted code and got `git_code_dirty:
    false`, an empty `git_dirty_code_files`, and a commit hash that looks like it names what
    ran. The most consequential boolean in the record failed toward the reassuring answer.
    The whole tree is read now and `code_paths` only WIDENS what counts."""
    repo = _repo_with(tmp_path, "pkg")
    (repo / "pkg" / "mod.py").write_text("x = 2\n", encoding="utf-8")  # uncommitted edit
    proj = runprov.Project(root=repo, sink=runprov.MemorySink())  # the DEFAULT code_paths
    code = runprov.Run("t", project=proj).record["code"]
    assert code["git_code_dirty"] is True, "an edited module is not a clean tree"
    assert any("pkg/mod.py" in line for line in code["git_dirty_code_files"])
    assert code["git_dirty_outside_code_paths"] == code["git_dirty_code_files"], (
        "and the record must say the configured scope missed it, or the next reader "
        "cannot tell a bad scope from a clean tree"
    )
    assert code["git_tree_dirty"] is True
    assert code["code_paths"] == list(runprov.DEFAULT_CODE_PATHS)


def test_the_dirty_other_files_list_is_capped_and_says_how_much_it_dropped(tmp_path):
    """L-57. `git_other_files_omitted == 0` is the `0 == 0` shape: the neighbouring fixture
    dirties TWO files against a cap of 50, so the cap and its counter are asserted with no
    tail to omit. Raising `OTHER_FILES_KEPT` to 5000 left the whole suite green.

    The cap exists because "a repository with a 273 MB report tree can have thousands of
    untracked files, and a provenance record must not become one". Removing it makes every
    history line as large as the dirty tree — unbounded growth in the append-only file,
    invisible until a real repository hits it. `git_other_files_omitted` is the field that
    would say so, and it was only ever observed at zero.

    This dirties `OTHER_FILES_KEPT + 5` files, so the list is capped AND the counter is
    non-zero — which is the pair the record needs to stay honest about its own truncation."""
    extra = 5
    repo = _repo_with(tmp_path, "pkg")
    # AT THE ROOT, not inside a new directory: `git status --porcelain` collapses a wholly
    # untracked directory into ONE `?? churn/` line, so 55 files there produce 55 files and
    # a single status line -- and the cap would never be reached. The record is built from
    # status LINES, so the fixture has to produce that many of them.
    for i in range(runprov.project.OTHER_FILES_KEPT + extra):
        (repo / f"out_{i:04d}.tsv").write_text(f"{i}\n", encoding="utf-8")

    code = runprov.Run("t", project=runprov.Project(root=repo, sink=runprov.MemorySink())).record[
        "code"
    ]
    kept = code["git_dirty_other_files"]
    assert code["git_code_dirty"] is False, "churn under `churn/` is not code"
    assert code["git_tree_dirty"] is True
    assert len(kept) == runprov.project.OTHER_FILES_KEPT, (
        f"kept {len(kept)} lines; the cap is {runprov.project.OTHER_FILES_KEPT} and a record "
        f"that grows with the dirty tree is the thing the cap exists to prevent"
    )
    assert code["git_other_files_omitted"] > 0, "silently truncating is the defect, not the cap"
    # The count is what was DROPPED, not what was seen: kept + omitted is the whole list, so
    # a reader can reconstruct the size of the omission rather than guess at it.
    assert len(kept) + code["git_other_files_omitted"] == code["git_other_changes"]


def test_the_dirty_other_files_cap_is_small_enough_to_be_a_cap(tmp_path):
    """The bound the test above cannot state, and the reason it cannot. That test sizes its
    fixture from `OTHER_FILES_KEPT`, so raising the constant raises the fixture with it and
    the mutation `OTHER_FILES_KEPT = 5000` passes — it proves the cap is APPLIED, never that
    it caps anything. Measured: with the cap at 5000 the whole suite stays green.

    So the value gets its own assertion, exactly as `_CARRY` did. The cap exists because "a
    repository with a 273 MB report tree can have thousands of untracked files, and a
    provenance record must not become one" — a cap of 5000 satisfies the mechanism and
    abandons the purpose, letting every history line grow with the dirty tree."""
    assert 0 < runprov.project.OTHER_FILES_KEPT <= 200, (
        f"OTHER_FILES_KEPT is {runprov.project.OTHER_FILES_KEPT}; a record that keeps that "
        f"many change lines per run is the unbounded growth the cap was added to stop"
    )


def test_data_churn_alone_still_does_not_report_the_code_as_dirty(tmp_path):
    """The other half, and the reason `git_code_dirty` is not simply the whole-tree state:
    a pipeline dirties its own output tree on every run, and a boolean that is red forever
    is a boolean everyone learns to ignore. The churn is still LISTED -- an integer with no
    file list was the only trace the `pkg/` case ever left."""
    repo = _repo_with(tmp_path, "pkg")
    (repo / "data.tsv").write_text("id\n1\n2\n", encoding="utf-8")
    (repo / "reports").mkdir()
    (repo / "reports" / "out.parquet").write_bytes(b"\x00")
    code = runprov.Run("t", project=runprov.Project(root=repo, sink=runprov.MemorySink())).record[
        "code"
    ]
    assert code["git_code_dirty"] is False
    assert code["git_tree_dirty"] is True, "the tree IS modified and the record must say so"
    assert code["git_other_changes"] == 2, "unchanged meaning: the whole-tree change count"
    assert sorted(code["git_dirty_other_files"]) == [" M data.tsv", "?? reports/"]
    assert code["git_other_files_omitted"] == 0
    # v1 -> v2 at ADR-029: `content_sha256` is computed differently (volatile stripping
    # now reaches inside gzip, spans block boundaries and is scoped to runprov's own
    # records) and `sha256_tree` gained a separator. This guard exists to stop a GRATUITOUS
    # bump, and the rule it enforces is "bump when a field changes meaning" — which one
    # now has. It is updated, not deleted: the next bump still has to justify itself.
    assert runprov.SCHEMA == "runprov.run.v2", "bumped by ADR-029: content_sha256 changed meaning"


def test_a_renamed_makefile_outside_code_paths_still_counts_as_code():
    """Both ends of a rename, a basename with no informative suffix, and a blank line."""
    state = runprov.project.classify_status(
        "R  Makefile -> build/Makefile\n\n M docs/notes.md", ("src",)
    )
    assert state.captured is True
    assert state.code == ("R  Makefile -> build/Makefile",)
    assert state.outside_code_paths == state.code
    assert state.other == (" M docs/notes.md",)


def test_the_status_parser_restores_the_leading_space_git_strip_ate():
    """`git()` ends with `stdout.strip()`, so the FIRST porcelain line arrives one character
    short: ` M pkg/mod.py` becomes `M pkg/mod.py`. Parsing from a fixed column 3 then yields
    `kg/mod.py` -- which matches no code path and ends in no known suffix, so the very first
    changed file in every run would have been classified as not-code. It also reads as a
    STAGED change when it is an unstaged one."""
    assert runprov.project._split_status("M pkg/mod.py") == (" M pkg/mod.py", ["pkg/mod.py"])
    assert runprov.project._split_status(" M pkg/mod.py") == (" M pkg/mod.py", ["pkg/mod.py"])


def test_the_status_parser_reads_both_ends_of_a_rename_and_unquotes_a_quoted_path():
    line = "R  old/a.py -> pkg/b.py"
    assert runprov.project._split_status(line) == (line, ["old/a.py", "pkg/b.py"])
    quoted = ' M "src/caf\\303\\251.py"'
    assert runprov.project._split_status(quoted) == (quoted, ["src/caf\\303\\251.py"])
    assert runprov.project._split_status("?? pkg/") == ("?? pkg/", ["pkg"])
    assert runprov.project._split_status("??") == ("??", []), "unparseable names no path"
    assert runprov.project.classify_status("??", ("src",)).code == ("??",), (
        "and a line the parser cannot read is counted as CODE, never quietly as churn"
    )
    assert runprov.project.looks_like_code("pkg/deep/mod.py") is True
    assert runprov.project.looks_like_code("reports/audit/LICENSE") is False


def test_a_record_made_where_git_could_not_run_is_not_a_record_of_a_clean_tree(tmp_path, capsys):
    """`git()` returns None on ANY failure -- no repository, no git binary, the 20 s timeout
    -- and `bool(None)` is False, so the two facts were written identically. A companion
    boolean rather than a nullable `git_code_dirty`: `None` is falsy, so every consumer
    already written as `if rec["git_code_dirty"]` would go on reading "we could not look"
    as "clean", which is the coercion that caused this."""
    blind_root = tmp_path / "not_a_repo"
    blind_root.mkdir()
    blind = runprov.Run(
        "t", project=runprov.Project(root=blind_root, sink=runprov.MemorySink())
    ).record["code"]
    seen = runprov.Run(
        "t", project=runprov.Project(root=_repo_with(tmp_path, "src"), sink=runprov.MemorySink())
    ).record["code"]

    assert (seen["git_status_captured"], seen["git_code_dirty"]) == (True, False)
    assert (blind["git_status_captured"], blind["git_code_dirty"]) == (False, False)
    assert (blind["git_status_captured"], blind["git_code_dirty"]) != (
        seen["git_status_captured"],
        seen["git_code_dirty"],
    ), "a consumer must be able to tell 'we could not look' from 'verified clean'"
    # The wording differs by situation now — an anomaly is a WARNING, not being under
    # version control is a NOTE said once — but both must carry the same claim, which is
    # that unknown is not clean. The record above is the contract; this is the human line.
    assert "UNKNOWN rather than clean" in capsys.readouterr().err


def test_a_status_that_fails_inside_a_real_repository_is_recorded_as_unknown(
    tmp_path, monkeypatch, capsys
):
    """The case the record could never express: git is installed, the repository is there,
    the commit is real -- and `git status` did not complete. Everything else in the record
    looks authoritative, which is exactly why the one thing that failed must be stated."""
    repo = _repo_with(tmp_path, "src")
    real = runprov.project.subprocess.run

    def flaky(cmd, **kwargs):
        if "status" in tuple(cmd):
            raise runprov.project.subprocess.TimeoutExpired(list(cmd), 20)
        return real(cmd, **kwargs)

    monkeypatch.setattr(runprov.project.subprocess, "run", flaky)
    run = runprov.Run("t", project=runprov.Project(root=repo, sink=runprov.MemorySink()))
    code = run.record["code"]
    assert code["git_commit"] is not None, "git works and the repository is real"
    assert code["git_status_captured"] is False
    assert code["git_code_dirty"] is False, "and on its own this now means nothing"
    run.write(tmp_path / "p.json")
    assert "(DIRTY STATE UNKNOWN)" in capsys.readouterr().err


def test_the_history_destination_is_recorded_printed_and_carried_in_the_line(tmp_path, capsys):
    """The console printed the SIDECAR path and never the HISTORY path, so a project whose
    `configure(run_log=...)` had not been imported appended to a second runs.jsonl under a
    different root and produced identical-looking output. The README promises one continuous
    history; a promise nothing states per run cannot be checked."""
    log = tmp_path / "elsewhere" / "runs.jsonl"
    proj = runprov.Project(root=tmp_path, run_log=log, run_id=lambda: "r", generation=lambda: "g")
    run = runprov.Run("t", project=proj)
    run.write(tmp_path / "p.json")

    assert run.record["history"]["destination"] == str(log)
    sidecar = json.loads((tmp_path / "p.json").read_text(encoding="utf-8"))
    assert sidecar["history"]["destination"] == str(log)
    line = json.loads(log.read_text(encoding="utf-8").splitlines()[0])
    assert line["history_destination"] == str(log), "an archived line must name its own file"
    assert line["project_source"] == "argument"
    assert f"history -> {log}" in capsys.readouterr().err


def test_the_history_destination_names_a_sink_that_has_no_path(tmp_path):
    assert runprov.Project(root=tmp_path).history_destination() == str(
        tmp_path / "provenance" / "runs.jsonl"
    )
    jsonl = runprov.JsonlSink(tmp_path / "h.jsonl")
    assert runprov.Project(root=tmp_path, sink=jsonl).history_destination() == (
        f"JsonlSink({tmp_path / 'h.jsonl'})"
    )
    assert runprov.Project(root=tmp_path, sink=runprov.MemorySink()).history_destination() == (
        "MemorySink"
    )


def test_the_implicit_project_warning_fires_for_the_forgetful_script_and_not_otherwise(
    tmp_path, monkeypatch, capsys
):
    """The warning names the ONE condition in which a history can split behind your back:
    a Run built when nothing in the process ever called `configure()`. A script that forgot
    to import its project's paths module is in exactly that state and in no other."""
    monkeypatch.setattr(runprov.project, "_ACTIVE", None)
    monkeypatch.setattr(runprov.project, "_CONFIGURED", False)
    monkeypatch.setattr(runprov.run, "_IMPLICIT_WARNED", False)
    monkeypatch.chdir(tmp_path)
    assert runprov.is_configured() is False

    forgot = runprov.Run("script_that_forgot_the_import")
    out = capsys.readouterr().err
    assert "no configure() has run" in out
    assert forgot.record["history"]["destination"] in out, "and it names the file it will use"
    assert forgot.record["history"]["project_source"] == "implicit"

    runprov.Run("same_process_again")
    assert "no configure() has run" not in capsys.readouterr().err, (
        "once per process: a warning repeated per Run is scrollback, not a warning"
    )

    monkeypatch.setattr(runprov.run, "_IMPLICIT_WARNED", False)
    override = runprov.Project(root=tmp_path, sink=runprov.MemorySink())
    assert runprov.Run("t", project=override).record["history"]["project_source"] == "argument"
    runprov.configure(root=tmp_path, sink=runprov.MemorySink())
    assert runprov.is_configured() is True
    assert runprov.Run("t").record["history"]["project_source"] == "configured"
    assert "no configure() has run" not in capsys.readouterr().err, (
        "a per-call override and a configured project are both deliberate; neither warns"
    )


def test_the_timeline_tells_an_unknown_dirty_state_from_a_clean_one_and_names_the_history(
    tmp_path, capsys
):
    """The CLI is a consumer too. If `python -m runprov log` renders both as a blank space,
    the distinction exists only for whoever reads raw JSON."""
    p = tmp_path / "runs.jsonl"
    p.write_text(
        json.dumps({"script": "blind", "git_status_captured": False, "history_destination": str(p)})
        + "\n"
        + json.dumps({"script": "seen", "git_status_captured": True})
        + "\n",
        encoding="utf-8",
    )
    cli.main(["log", "--log", str(p)])
    out = capsys.readouterr().out
    assert out.count("DIRTY STATE UNKNOWN (git status did not run)") == 1
    assert f"history    {p}" in out


def test_a_diagnostic_survives_a_console_that_cannot_encode_it(monkeypatch, capsys):
    """These messages contain em dashes, and stderr on Windows uses the console code page.
    Measured: fine under utf-8 and cp1252, `UnicodeEncodeError` under cp932 and ascii -- so
    on a Japanese or stripped-down console a run would die INSIDE provenance capture, at
    the moment it was trying to report something. Windows CI surfaced it as a mojibake
    byte (0x97, the cp1252 em dash) before it could surface as a crash."""

    class NarrowStderr(io.TextIOWrapper):
        pass

    buf = io.TextIOWrapper(io.BytesIO(), encoding="ascii", newline="")
    monkeypatch.setattr(sys, "stderr", buf)
    runprov._report.diagnostic("timeout — every git_* field means 'we could not look'")
    buf.flush()
    written = buf.buffer.getvalue().decode("ascii")
    assert "every git_* field" in written, "the warning must still arrive"
    assert "—" not in written


@NO_FIFO
@requires_fifo
def test_a_non_regular_file_inside_a_tree_is_counted_not_silently_dropped(tmp_path):
    """The other half of R8, in the same shape. A FIFO inside a directory cannot be hashed
    — opening it is the hang `describe` refuses at the top — but dropping it without a word
    leaves the tree hash describing a population nobody stated."""
    d = tmp_path / "tree"
    d.mkdir()
    (d / "real").write_text("x", encoding="utf-8")
    os.mkfifo(d / "pipe")
    rec = runprov.describe(d)
    assert rec["n_files"] == 1 and rec["n_skipped_nonregular"] == 1
    assert "pipe" in " ".join(rec["skipped_nonregular"])
    assert rec["sha256_tree"], "the readable half is still hashed"


# ================================================== council batch C + the packaging pair
# UNIT tests first, then INTEGRATION tests that drive a whole run and read the artifacts
# back. Fakes where a real object can be built (files, streams, a mutating file); a spy
# only where the property IS an interaction — the stat/hash ORDER in R10 cannot be observed
# from the record alone, because a stable file gives identical output either way.


class _MutatingFile:
    """A fake `sha256` that changes the file WHILE it is being hashed.

    The TOCTOU window is a race, and a race cannot be reproduced by waiting for it. Standing
    in for the hash function and mutating from inside is the only way to make the window
    deterministic — and it is a fake, not a mock: it computes and returns a real digest, so
    everything downstream behaves exactly as in production.
    """

    def __init__(self, path, grow_to=b"much longer content than before"):
        self.path, self.grow_to, self.calls = path, grow_to, 0

    def __call__(self, path, *a, **k):
        self.calls += 1
        digest = runprov.hashing.sha256.__wrapped__(path, *a, **k) if False else None
        import hashlib

        digest = hashlib.sha256(pathlib.Path(path).read_bytes()).hexdigest()
        if pathlib.Path(path) == self.path:
            self.path.write_bytes(self.grow_to)  # the file moves under us
        return digest


def test_the_history_carries_both_identities_of_an_input(tmp_path, monkeypatch):
    """C3. The pin uses `content_sha256`; the history used `sha256` alone. The same input
    therefore had two identities and nothing could join them — a reader holding a pinned
    artifact could not find the run that produced it without rehashing every candidate."""
    monkeypatch.chdir(tmp_path)
    log = tmp_path / "runs.jsonl"
    runprov.configure(root=tmp_path, run_log=log)
    src = tmp_path / "in.tsv"
    src.write_text("# built_utc: 2026-01-01\nid\tv\nx\t1\n", encoding="utf-8")
    with runprov.Run("s", provenance=tmp_path / "p.json") as run:
        run.input(src)
    entry = json.loads(log.read_text(encoding="utf-8").splitlines()[-1])["inputs"][0]
    assert entry["sha256"] == runprov.sha256(src)
    assert entry["content_sha256"] == runprov.content_digest(src), (
        "the history must carry the identity the PIN uses, or the two cannot be joined"
    )
    assert entry["sha256"] != entry["content_sha256"], "this file has a volatile stamp"


def test_an_in_repo_relative_path_pins_by_its_repo_path(tmp_path, monkeypatch):
    """C4. `_pin_name` called `relative_to` on the raw string, so a RELATIVE in-repo path
    raised ValueError and pinned as `<external>/name` — announcing a file as foreign to the
    very repository holding it."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "x.tsv").write_text("a\n", encoding="utf-8")
    run = runprov.Run("s", project=runprov.Project(root=tmp_path))
    # POSIX separators on EVERY platform: a pin is embedded in a committed artifact, so a
    # backslash form on Windows and a slash form on Linux would be two pins for one input.
    assert run._pin_name("data/x.tsv") == "data/x.tsv"
    assert run._pin_name(str(tmp_path / "data" / "x.tsv")) == "data/x.tsv"
    assert run._pin_name("/etc/passwd").startswith("<external>/"), "genuinely outside stays so"

    # THE CASE THAT MAKES THE cwd JOIN LOAD-BEARING, and the one a naive test misses:
    # a relative path resolves against the LIVE cwd, which is the run's cwd right up until
    # the script chdirs. Then `Path("data/x.tsv").resolve()` silently means somewhere else.
    # The run's own cwd is captured at construction and is the only correct basis.
    # Mutation-tested: without the join, this pins as <external>/x.tsv.
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    assert run._pin_name("data/x.tsv") == "data/x.tsv", (
        "a relative input must pin against the RUN's cwd, not wherever the script has since "
        "chdir'd to"
    )


def test_the_sidecar_and_the_history_do_not_share_one_schema_name(tmp_path, monkeypatch):
    """C7. One string, `runprov.run.v1`, labelled two different shapes. A consumer branching
    on it — which the field exists for — would apply the wrong reader to one of them."""
    monkeypatch.chdir(tmp_path)
    log = tmp_path / "runs.jsonl"
    runprov.configure(root=tmp_path, run_log=log)
    prov = tmp_path / "p.json"
    with runprov.Run("s", provenance=prov):
        pass
    side = json.loads(prov.read_text(encoding="utf-8"))
    hist = json.loads(log.read_text(encoding="utf-8").splitlines()[-1])
    assert side["schema"] == runprov.SCHEMA == "runprov.run.v2"
    assert hist["schema"] == runprov.HISTORY_SCHEMA == "runprov.history.v2"
    assert side["schema"] != hist["schema"], "two shapes must not answer to one name"


def test_a_file_that_moves_while_being_hashed_is_recorded_as_unstable(tmp_path, monkeypatch):
    """R10. `size_bytes` was stat-ed BEFORE hashing, so a file written while it was read
    recorded a size that never went with that digest — and nothing said so."""
    p = tmp_path / "growing.tsv"
    p.write_bytes(b"short")
    monkeypatch.setattr(runprov.hashing, "sha256", _MutatingFile(p))
    rec = runprov.hashing.describe(p)
    assert rec["unstable_during_hash"] is True, "a file that moved under the hash must say so"
    assert rec["size_bytes"] == len(b"much longer content than before"), (
        "the recorded size must be the post-hash stat, not a stale pre-hash one"
    )


def test_size_is_stat_ed_after_the_hash_not_before(tmp_path):
    """R10, the ORDER — and the one property in this batch that a record cannot show, since
    a stable file yields identical output either way. A spy is the right instrument here and
    a fake is not: the claim is about the sequence of calls, not about a value."""
    from unittest import mock

    p = tmp_path / "f.tsv"
    p.write_text("x\n", encoding="utf-8")
    calls: list[str] = []
    real_stat = pathlib.Path.stat
    with (
        mock.patch.object(
            runprov.hashing, "sha256", side_effect=lambda *a, **k: calls.append("hash") or "0" * 64
        ),
        mock.patch.object(
            pathlib.Path,
            "stat",
            autospec=True,
            side_effect=lambda self, *a, **k: (calls.append("stat"), real_stat(self))[1],
        ),
    ):
        runprov.hashing.describe(p)
    assert "hash" in calls and calls.index("hash") < len(calls) - 1, (
        "at least one stat must follow the hash — that is what makes size_bytes match it"
    )


def test_registering_an_input_after_the_pin_is_written_warns(tmp_path, monkeypatch, capsys):
    """R14. `header()` renders the pin from the inputs registered SO FAR. Called early, it
    embeds a pin that understates its own artifact — and the artifact then claims, in its own
    body, to be derived from less than it was. Nothing can detect that after the fact, so it
    has to be said at the moment it happens."""
    monkeypatch.chdir(tmp_path)
    runprov.configure(root=tmp_path, run_log=tmp_path / "runs.jsonl")
    a, b = tmp_path / "a.tsv", tmp_path / "b.tsv"
    for f in (a, b):
        f.write_text("x\n", encoding="utf-8")
    with runprov.Run("s", provenance=tmp_path / "p.json") as run:
        run.input(a)
        run.header()
        capsys.readouterr()
        run.input(b)
        err = capsys.readouterr().err
    assert "PROVENANCE WARNING" in err and "pin" in err.lower()
    assert "b.tsv" in err, "it must name the input that arrived too late"


def test_no_warning_when_every_input_precedes_the_pin(tmp_path, monkeypatch, capsys):
    """The other half — a warning that always fires is the same defect as one that never
    does, and this is the ordinary correct usage."""
    monkeypatch.chdir(tmp_path)
    runprov.configure(root=tmp_path, run_log=tmp_path / "runs.jsonl")
    a = tmp_path / "a.tsv"
    a.write_text("x\n", encoding="utf-8")
    with runprov.Run("s", provenance=tmp_path / "p.json") as run:
        run.input(a)
        capsys.readouterr()
        run.header()
        err = capsys.readouterr().err
    assert "pin" not in err.lower()


# ---------------------------------------------------------------- INTEGRATION
def test_a_whole_run_end_to_end_reads_back_consistently(tmp_path, monkeypatch):
    """INTEGRATION. One real run: real files, a real subprocess, a real sidecar, a real
    append-only history, and the rendered YAML view — then every artifact is read back and
    cross-checked against the others. The unit tests above each pin one field; this asserts
    they still agree once the whole thing has run.
    """
    monkeypatch.chdir(tmp_path)
    log = tmp_path / "runs.jsonl"
    runprov.configure(
        root=tmp_path,
        run_log=log,
        terminal_log_dir=tmp_path / "logs",
        env_snapshot_dir=tmp_path / "envs",
    )
    src = tmp_path / "in.tsv"
    src.write_text("# built_utc: 2026-01-01\nid\tv\nx\t1\n", encoding="utf-8")
    out = tmp_path / "out.tsv"
    prov = tmp_path / "out_provenance.json"

    with runprov.Run("integration", {"k": 6}, provenance=prov) as run:
        text = pathlib.Path(run.input(src)).read_text(encoding="utf-8")
        subprocess.run([sys.executable, "-c", "print('child ran')"], check=True)
        with open(run.output(out), "w", encoding="utf-8") as fh:
            fh.write(run.header())
            fh.write(text)
        run.note("rows", 1)
        run.seeds([42])

    side = json.loads(prov.read_text(encoding="utf-8"))
    hist = json.loads(log.read_text(encoding="utf-8").splitlines()[-1])

    # the two records describe the same run, under DIFFERENT schema names (C7)
    assert side["run_id"] == hist["run_id"] and side["schema"] != hist["schema"]
    # the input is joinable across both by either identity (C3)
    assert hist["inputs"][0]["content_sha256"] == side["inputs"][0]["content_sha256"]
    # the pin inside the artifact names the input by its REPO-RELATIVE path (C4)
    body = out.read_text(encoding="utf-8")
    assert "in.tsv" in body and "<external>" not in body
    # the pin is deterministic: no timestamp, no run id
    assert side["run_id"] not in body
    # the terminal log caught the SUBPROCESS, and the record says by which mechanism
    assert "child ran" in pathlib.Path(side["terminal_log"]["path"]).read_text(encoding="utf-8")
    assert side["terminal_log"]["capture"] == "fd"
    # the output is hashed, present, and not MISSING
    assert side["outputs"][0]["sha256"] == runprov.sha256(out)
    # and the whole history line renders in the transformation-log shape
    yaml = pytest.importorskip("yaml")
    doc = yaml.safe_load(cli._yaml([hist]))[0]
    assert doc["step"] == "integration" and doc["params"] == {"k": 6}
    assert doc["summary"] == {"rows": 1}
    assert doc["requirements_file"].endswith(".txt")


# ============================================ ADR-029: the digest migration (R1 R2 R3 R9)
@pytest.mark.parametrize("offset", [-1, 0, 1])
def test_a_volatile_stamp_spanning_a_block_boundary_is_still_stripped(tmp_path, offset):
    """R1. The substitution ran per 8,192-line block, so a `"started_utc": "..."` split
    across a boundary was never matched — and the artifact oscillated forever, which is the
    one thing `content_digest` exists to prevent.

    THE PLACEMENT IS DERIVED, NOT TYPED. The original fixture padded with a literal 8,190
    lines, which puts the key at the START of block two — the whole match inside one block,
    so `_CARRY` was never needed and the R1 defect it is named for went unguarded. Measured:
    with `_CARRY` cut to 8, the digest is stable at every pad count from 8,185 to 8,194
    EXCEPT 8,189 — the one where the key lands on the last line of a block. The fixture sat
    at 8,190 and missed it by a line.

    So the pad count is computed from `chunk_lines` (the file opens with `{` and the schema
    line, then the pads, then the key), and the case is run either side of the boundary as
    well as on it. A change to `chunk_lines`, to the block loop, or to `_CARRY` now moves
    the fixture with it instead of silently stepping past the case."""
    # `{`, `"schema"`, then `pads` lines, then the `"started_utc":` key line. For that key
    # to be the LAST line of the first block, pads = chunk_lines - 3.
    chunk_lines = 8192
    pads = chunk_lines - 3 + offset

    def build(stamp):
        p = tmp_path / f"big_{pads}_{stamp[:4]}.json"
        # It must be RUNPROV'S OWN record, because R2 scopes the stripping to those. The
        # two fixes interact and this test failed until it said so — which is the scoping
        # working, not a defect.
        pad = "".join(f'  "pad{i}": {i},\n' for i in range(pads))
        p.write_text(
            '{\n  "schema": "runprov.run.v2",\n' + pad + f'  "started_utc":\n    "{stamp}"\n}}\n',
            encoding="utf-8",
        )
        return p

    a, b = build("2026-01-01T00:00:00Z"), build("2099-12-31T23:59:59Z")
    assert runprov.content_digest(a) == runprov.content_digest(b), (
        f"two runs differing only in a stamp at block-boundary offset {offset:+d} "
        f"must pin identically"
    )


def test_the_carry_window_covers_the_longest_volatile_match(tmp_path):
    """The bound behind the test above, stated rather than assumed. `_CARRY` is how much of
    the previous block is held back so a match straddling the boundary can still be seen, so
    it has to exceed the longest thing `VOLATILE_JSON` can match. A `_CARRY` smaller than
    that is the R1 defect returning, and the parametrised test above only catches it at the
    offsets it happens to try."""
    longest = len('  "environment_snapshot_started_utc":\n    "2026-01-01T00:00:00.000000+00:00",')
    assert runprov.hashing._CARRY > longest * 4, (
        f"_CARRY is {runprov.hashing._CARRY}; a volatile key/value pair can run to about "
        f"{longest} characters and the window must clear it with room for the next one"
    )


def test_volatile_stripping_applies_only_to_runprovs_own_records(tmp_path):
    """R2. `VOLATILE_JSON` fired on ANY `*.json`, so a user's data file with a legitimate
    key named `mtime_utc` had it erased from its digest — two genuinely different datasets
    collided. The stripping exists for the records runprov writes, which carry timestamps
    it puts there itself; it has no business rewriting somebody's data."""
    a, b = tmp_path / "data_a.json", tmp_path / "data_b.json"
    a.write_text('{"sample": "S1", "mtime_utc": "REAL DATA A", "n": 1}', encoding="utf-8")
    b.write_text('{"sample": "S1", "mtime_utc": "REAL DATA B", "n": 1}', encoding="utf-8")
    assert runprov.content_digest(a) != runprov.content_digest(b), (
        "a user's data must not be erased by a rule about runprov's own timestamps"
    )

    # ...and runprov's OWN record still has its stamps stripped, or every sidecar oscillates
    def record(stamp):
        p = tmp_path / f"rec_{stamp[:4]}.json"
        p.write_text(
            f'{{"schema": "runprov.run.v2", "script": "s", "started_utc": "{stamp}", "n": 1}}',
            encoding="utf-8",
        )
        return p

    assert runprov.content_digest(record("2026-01-01")) == runprov.content_digest(
        record("2099-12-31")
    ), "runprov's own record must still be stable across runs"


def test_a_gzipped_artifact_with_a_build_stamp_is_stable(tmp_path):
    """R3. `.gz` was decompressed and hashed raw, with no volatile stripping — so a gzipped
    artifact carrying `# built_utc:` differed on every run, and so did everything pinning
    it. The exact oscillation the plain-text path was written to stop."""

    def build(stamp):
        p = tmp_path / f"a_{stamp[:4]}.tsv.gz"
        with gzip.open(p, "wt", encoding="utf-8") as fh:
            fh.write(f"# built_utc: {stamp}\nid\tvalue\nx\t1\n")
        return p

    assert runprov.content_digest(build("2026-01-01")) == runprov.content_digest(
        build("2099-12-31")
    )


def test_a_gzipped_binary_still_falls_back_to_the_raw_digest(tmp_path):
    """The half that keeps R3 from becoming R13's mistake: a gzipped BINARY must not be
    decoded as text. It falls back to hashing the decompressed bytes."""
    p = tmp_path / "b.bin.gz"
    with gzip.open(p, "wb") as fh:
        fh.write(bytes(range(256)) * 4)
    assert runprov.content_digest(p), "it must produce a digest rather than raising"


def test_the_tree_hash_does_not_depend_on_the_order_the_filesystem_yields(tmp_path, monkeypatch):
    """L-29. `files.sort()` is what makes `sha256_tree` reproducible, and removing it left
    the ENTIRE suite green — the two existing tests could not see it. One hashes a two-file
    tree and only checks the digest CHANGES when a file changes; the other uses a single-file
    tree, where there is no order to get wrong.

    Without the sort the digest is a property of the filesystem's readdir order, so the same
    untouched directory hashes differently after a copy, a remount, or on another machine —
    and `verify` reports STALE for a tree nobody touched. That is the permanently-red check
    this module exists to end."""
    # NAMES CHOSEN SO FULL-PATH ORDER AND BASENAME ORDER DISAGREE: by path `a/z.txt` comes
    # before `b.txt`, by basename it comes after. A first fixture used `a.txt`, `sub/b.txt`,
    # `z.txt`, where the two orders coincide — so sorting by the wrong key passed, which is
    # the fixture-too-small shape this audit keeps finding.
    d = tmp_path / "tree"
    (d / "a").mkdir(parents=True)
    (d / "a" / "z.txt").write_text("z", encoding="utf-8")
    (d / "b.txt").write_text("b", encoding="utf-8")
    (d / "m.txt").write_text("m", encoding="utf-8")

    forwards = runprov.describe(d)["sha256_tree"]

    # The SAME tree, walked in the opposite order. `os.walk` yields whatever the filesystem
    # gives it; nothing guarantees that is stable across machines or copies.
    real_walk = runprov.hashing.os.walk
    monkeypatch.setattr(
        runprov.hashing.os,
        "walk",
        lambda *a, **k: (
            (root, list(reversed(dirs)), list(reversed(files)))
            for root, dirs, files in real_walk(*a, **k)
        ),
    )
    backwards = runprov.describe(d)["sha256_tree"]
    assert backwards == forwards, "the tree digest changed when only the walk order did"


def test_the_tree_hash_is_the_sorted_relpath_and_digest_stream(tmp_path):
    """The other half, and the stronger one: an INDEPENDENT recomputation. Order-invariance
    alone would also hold for a digest that ignored names entirely, so this pins the actual
    construction — sorted relative POSIX paths, each followed by its file digest, each
    NUL-terminated."""
    d = tmp_path / "tree"
    (d / "a").mkdir(parents=True)
    (d / "a" / "z.txt").write_text("z", encoding="utf-8")
    (d / "b.txt").write_text("b", encoding="utf-8")

    # Sorted as PATHS, which is what the code does — `Path.__lt__` compares parts, and that
    # is not always the same as comparing the rendered strings. Same fixture as above, so
    # sorting by basename instead of by path produces a different stream and fails here.
    expected = hashlib.sha256()
    for full in sorted([d / "a" / "z.txt", d / "b.txt"]):
        expected.update(full.relative_to(d).as_posix().encode() + b"\0")
        expected.update(runprov.sha256(full).encode() + b"\0")
    assert runprov.describe(d)["sha256_tree"] == expected.hexdigest()


def test_the_tree_hash_separates_a_name_from_its_digest(tmp_path):
    """R9. `name || hex-digest` with no separator is not injective by construction. The
    review called the second preimage 'trivial'; it is not — it needs a preimage attack on
    SHA-256, and one could not be built. The separator is free, so the argument goes away
    rather than being defended."""
    d = tmp_path / "t"
    d.mkdir()
    (d / "f").write_text("x", encoding="utf-8")
    digest = runprov.describe(d)["sha256_tree"]
    naive = hashlib.sha256()
    naive.update(b"f")
    naive.update(runprov.sha256(d / "f").encode())
    assert digest != naive.hexdigest(), "the separator must actually be in the stream"


def test_the_schema_marker_moved_because_content_sha256_changed_meaning(tmp_path):
    """ADR-029 step 2. The marker is bumped when a field changes MEANING, never when one is
    added — and `content_sha256` is computed differently after this migration. A consumer
    comparing a v1 digest against a v2 digest is comparing two different questions."""
    assert runprov.SCHEMA == "runprov.run.v2"
    assert runprov.HISTORY_SCHEMA == "runprov.history.v2"


def test_content_digest_really_streams_measured_not_grepped(tmp_path):
    """C14. The test above asserts `read_text()` and `read_bytes()` do not appear — two
    spellings, not the property. `fh.read()`, `list(fh)` and `"".join(fh)` each load the
    whole file and each pass it, so the check could not see its own defect class.

    THE PROPERTY OF STREAMING IS SCALE INVARIANCE: peak memory is bounded by the BLOCK, not
    by the file, so tripling the input must not triple the peak. That is what is measured
    here, and it is the second version of this test.

    The first compared peak against a fraction of one file's size (`peak < size / 4`) and
    was GREEN LOCALLY AND RED IN CI: 1.9 MB peak for a 7.4 MB file, missing an arbitrary
    threshold by 2%. The implementation was streaming correctly the whole time — a block of
    8,192 Python `str` objects costs far more than the bytes it holds, and how much more
    depends on the interpreter and the platform. A threshold tuned on one machine is a test
    of that machine.

    Comparing two sizes cancels the constant out. A whole-file implementation grows with the
    file and fails; a streaming one does not care.
    """
    import tracemalloc

    def peak_for(lines: int) -> tuple[int, int]:
        p = tmp_path / f"f{lines}.tsv"
        p.write_text(
            "# built_utc: 2026-01-01T00:00:00Z\n"
            + "".join(f"{i}\tvalue{i}\n" for i in range(lines)),
            encoding="utf-8",
        )
        tracemalloc.start()
        try:
            assert runprov.content_digest(p)
            _, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
        return peak, p.stat().st_size

    small_peak, small_size = peak_for(150_000)
    large_peak, large_size = peak_for(600_000)

    assert large_size > small_size * 3, "the two fixtures must actually differ in size"
    assert large_peak < small_peak * 1.5, (
        f"peak grew with the file — {small_peak / 1e6:.1f} MB for {small_size / 1e6:.1f} MB "
        f"vs {large_peak / 1e6:.1f} MB for {large_size / 1e6:.1f} MB. Streaming means the "
        f"peak is bounded by the block, not by the input."
    )


def test_open_output_registers_pins_and_forces_utf8_in_one_call(tmp_path, monkeypatch):
    """A1. Pinning currently takes THREE things a caller must remember separately —
    `run.output(p)`, `fh.write(run.header())`, and `encoding="utf-8"` — and measured on the
    host project, 63 of 155 `run.output()` sites sit in scripts that write no pin at all.

    A default nobody has to remember is the only kind that gets used.
    """
    monkeypatch.chdir(tmp_path)
    runprov.configure(root=tmp_path, run_log=tmp_path / "runs.jsonl")
    src = tmp_path / "in.tsv"
    src.write_text("id\tv\nx\t1\n", encoding="utf-8")
    out = tmp_path / "out.tsv"

    with runprov.Run("s", provenance=tmp_path / "p.json") as run:
        run.input(src)
        with run.open_output(out) as fh:
            fh.write("id\tv\nx\t1\n")

    body = out.read_text(encoding="utf-8")
    assert body.startswith("# provenance"), "the pin must be the first thing in the artifact"
    assert "in.tsv" in body, "and it must name what the artifact was made from"
    assert body.rstrip().endswith("x\t1"), "the caller's content follows it"
    assert [o["path"] for o in run.record["outputs"]] == [str(out)], "registered exactly once"


def test_open_output_writes_utf8_whatever_the_locale_says(tmp_path, monkeypatch):
    """The encoding half, and it is not decoration: `header()` contains an em dash, so
    without an explicit UTF-8 the artifact is written in the machine's locale encoding —
    189 bytes under UTF-8, 187 under cp1252, a different SHA-256 for the same artifact, and
    `UnicodeEncodeError` outright under ascii."""
    monkeypatch.chdir(tmp_path)
    runprov.configure(root=tmp_path, run_log=tmp_path / "runs.jsonl")
    out = tmp_path / "o.tsv"
    with runprov.Run("s", provenance=tmp_path / "p.json") as run:
        with run.open_output(out) as fh:
            assert fh.encoding.lower().replace("-", "") == "utf8"
            fh.write("café\n")
    assert "café" in out.read_bytes().decode("utf-8")


def test_open_output_takes_the_comment_marker_for_the_format(tmp_path, monkeypatch):
    """A pin is a comment, and `#` is not a comment everywhere. A caller writing an artifact
    whose reader would choke on `#` must be able to say so rather than skip the pin."""
    monkeypatch.chdir(tmp_path)
    runprov.configure(root=tmp_path, run_log=tmp_path / "runs.jsonl")
    out = tmp_path / "o.sql"
    with runprov.Run("s", provenance=tmp_path / "p.json") as run:
        with run.open_output(out, comment="-- ") as fh:
            fh.write("select 1;\n")
    assert out.read_text(encoding="utf-8").startswith("-- provenance")


def test_open_output_still_records_a_crash_as_a_failed_run(tmp_path, monkeypatch):
    """The ergonomic path must not quietly lose the property the package exists for. A body
    that dies mid-write still leaves a record saying so."""
    monkeypatch.chdir(tmp_path)
    log = tmp_path / "runs.jsonl"
    runprov.configure(root=tmp_path, run_log=log)
    prov = tmp_path / "p.json"
    with pytest.raises(RuntimeError):
        with runprov.Run("s", provenance=prov) as run:
            with run.open_output(tmp_path / "o.tsv") as fh:
                fh.write("partial\n")
                raise RuntimeError("died halfway")
    rec = json.loads(prov.read_text(encoding="utf-8"))
    assert rec["status"] == "failed" and rec["failure"]["type"] == "RuntimeError"


def test_open_output_closes_the_handle_if_the_pin_cannot_be_written(tmp_path, monkeypatch):
    """An artifact HALF-written by this helper would be worse than one it refused to open:
    a file containing a truncated pin and nothing else still looks like an artifact. If
    rendering the pin fails, the handle closes and the exception reaches the caller."""
    monkeypatch.chdir(tmp_path)
    runprov.configure(root=tmp_path, run_log=tmp_path / "runs.jsonl")
    out = tmp_path / "o.tsv"
    opened = []

    real_open = open

    def spy_open(*a, **k):  # a real handle, remembered so the test can check it closed
        fh = real_open(*a, **k)
        opened.append(fh)
        return fh

    with runprov.Run("s", provenance=tmp_path / "p.json") as run:
        monkeypatch.setattr(
            type(run), "header", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no pin"))
        )
        monkeypatch.setattr(runprov.run, "open", spy_open, raising=False)
        with pytest.raises(RuntimeError, match="no pin"):
            run.open_output(out)

    assert out.read_text(encoding="utf-8") == "", "nothing may reach a half-pinned artifact"
    # THE ASSERTION THAT MAKES IT FALSIFIABLE. "the file is empty" is also true when the
    # handle leaked -- the write simply never happened. Mutation-tested: dropping the
    # `fh.close()` leaves the emptiness check green and this one red.
    assert opened and all(fh.closed for fh in opened), "the handle must not leak"


# ================================================= C8 + L1: a unique address, and lineage
def test_every_run_has_a_unique_address_that_the_pin_never_sees(tmp_path, monkeypatch):
    """C8. `run_id` is a CHAIN id — 30 stages of one pass share it — and the ad-hoc fallback
    collides at one-second resolution. So no run had a unique address, and an edge in a
    lineage graph could not say WHICH run it came from.

    `run_uid` is uuid4, in the sidecar and the history. It must never reach the pin: a uuid
    is a timestamp wearing a different name, and embedding one made two identical runs over
    identical inputs both report 80 artifacts CHANGED.
    """
    monkeypatch.chdir(tmp_path)
    log = tmp_path / "runs.jsonl"
    runprov.configure(root=tmp_path, run_log=log, run_id=lambda: "chain_shared")
    src = tmp_path / "in.tsv"
    src.write_text("x\n", encoding="utf-8")
    uids = []
    for i in range(3):
        prov = tmp_path / f"p{i}.json"
        with runprov.Run("stage", provenance=prov) as run:
            run.input(src)
            pin = run.header()
        rec = json.loads(prov.read_text(encoding="utf-8"))
        uids.append(rec["run_uid"])
        assert rec["run_id"] == "chain_shared", "the chain id is deliberately shared"
        assert rec["run_uid"] not in pin, "a uuid in the pin destabilises every artifact"
    assert len(set(uids)) == 3, "three runs of one chain must have three distinct addresses"
    hist = [json.loads(x) for x in log.read_text(encoding="utf-8").splitlines()]
    assert len({h["run_uid"] for h in hist}) == 3


def _hist(tmp_path, rows):
    p = tmp_path / "h.jsonl"
    p.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    return p


def _rec(uid, script, started, ins=(), outs=()):
    return {
        "schema": "runprov.history.v2",
        "run_uid": uid,
        "run_id": "r",
        "script": script,
        "started_utc": started,
        "finished_utc": started,
        "status": "ok",
        "inputs": [{"path": p, "sha256": s, "content_sha256": s} for p, s in ins],
        "outputs": [{"path": p, "sha256": s, "content_sha256": s} for p, s in outs],
    }


def test_lineage_joins_on_the_digest_not_the_path(tmp_path):
    """L1. Matching a consumer's input to a producer's output BY PATH is a heuristic: the
    same path is rewritten by many runs over a project's life, so every read has as many
    candidate producers as there were writes. Joining on the DIGEST is a fact — that byte
    sequence was produced exactly where it was produced."""
    rows = [
        _rec("A", "build", "2026-01-01T00:00:00Z", outs=[("data/x.tsv", "a" * 64)]),
        # the SAME PATH, different content, later — a path join cannot tell these apart
        _rec("B", "build", "2026-01-02T00:00:00Z", outs=[("data/x.tsv", "b" * 64)]),
        _rec("C", "train", "2026-01-03T00:00:00Z", ins=[("data/x.tsv", "a" * 64)]),
    ]
    g = cli._lineage([json.loads(x) for x in _hist(tmp_path, rows).read_text().splitlines()])
    assert g["ambiguous"] == 0
    assert ("A", "C") in g["edges"], "C read A's bytes, not B's — the digest says so"
    assert ("B", "C") not in g["edges"]


def test_a_producer_that_finished_after_the_consumer_started_is_not_one(tmp_path):
    """Two runs writing identical content is ordinary — a rebuild that reproduces. The
    digest alone then has two candidates, and time settles it: an artifact cannot have been
    read from a run that had not finished writing it."""
    rows = [
        _rec("EARLY", "build", "2026-01-01T00:00:00Z", outs=[("x", "c" * 64)]),
        _rec("READER", "use", "2026-01-02T00:00:00Z", ins=[("x", "c" * 64)]),
        _rec("LATER", "build", "2026-01-03T00:00:00Z", outs=[("x", "c" * 64)]),
    ]
    g = cli._lineage([json.loads(x) for x in _hist(tmp_path, rows).read_text().splitlines()])
    assert ("EARLY", "READER") in g["edges"]
    assert ("LATER", "READER") not in g["edges"], "a run cannot read from its own future"
    assert g["ambiguous"] == 0


def test_the_latest_producer_wins_when_several_finished_in_time(tmp_path):
    """L-32. The rule is "the latest such producer wins", and no fixture had TWO producers
    that both finished before the consumer started — the neighbouring test has exactly one
    valid candidate, so `before[-1]` and `before[0]` are the same element. Inverting the
    tie-break left the suite green.

    Inverted, a rebuild that reproduces byte-identical output makes every downstream edge
    point at the ORIGINAL run rather than the one that actually produced the bytes on disk:
    a lineage graph that is confidently wrong about provenance, which is worse than one that
    reports ambiguity. The docstring claims `ambiguous` is 0 "by construction rather than by
    luck" — this is the construction.

    THE NAMES RUN OPPOSITE TO THE TIMES on purpose. `before` is sorted over
    `(finished, address)` pairs, so naming the earlier producer `ZZZ` and the later one
    `AAA` means picking by address instead of by time also fails here. A fixture whose two
    orderings agree would pass for either rule."""
    rows = [
        _rec("ZZZ", "build", "2026-01-01T00:00:00Z", outs=[("x", "c" * 64)]),
        _rec("AAA", "rebuild", "2026-01-02T00:00:00Z", outs=[("x", "c" * 64)]),
        _rec("READER", "use", "2026-01-03T00:00:00Z", ins=[("x", "c" * 64)]),
    ]
    g = cli._lineage([json.loads(x) for x in _hist(tmp_path, rows).read_text().splitlines()])

    assert ("AAA", "READER") in g["edges"], "the LATER producer made the bytes on disk"
    assert ("ZZZ", "READER") not in g["edges"], "the superseded run is not the source"
    assert g["resolvable"] == 1 and g["ambiguous"] == 0, (
        "one edge, chosen deterministically — not two, and not an ambiguity report"
    )


def test_an_input_nobody_produced_is_an_orphan_and_is_counted(tmp_path):
    """An orphan is not a failure — a corpus downloaded outside the history is legitimately
    one. It is a COUNT, because 'every edge resolved' over a graph with no edges is the
    empty-set claim this project keeps finding."""
    rows = [_rec("X", "use", "2026-01-01T00:00:00Z", ins=[("outside.tsv", "d" * 64)])]
    g = cli._lineage([json.loads(x) for x in _hist(tmp_path, rows).read_text().splitlines()])
    assert g["orphan"] == 1 and g["resolvable"] == 0 and g["edges"] == []


def test_lineage_reports_records_that_cannot_take_part(tmp_path):
    """A record with no `run_uid` predates C8 and cannot be an endpoint. Counted rather than
    dropped: 1,310 entries in the real history predate run_id and the same will be true of
    this field, so a graph that silently excluded them would understate its own coverage."""
    rows = [
        {
            "script": "ancient",
            "started_utc": "2025-01-01T00:00:00Z",
            "outputs": [{"path": "x", "sha256": "e" * 64}],
        },
        _rec("NEW", "use", "2026-01-01T00:00:00Z", ins=[("x", "e" * 64)]),
    ]
    g = cli._lineage([json.loads(x) for x in _hist(tmp_path, rows).read_text().splitlines()])
    assert g["records_without_uid"] == 1, "the count must be reported, not hidden"
    # AND IT STILL PARTICIPATES. Requiring `run_uid` made the rule non-retroactive, and L1's
    # whole claim is that it works retroactively by a reader rule alone — measured, all 2,196
    # records in the real history predate the field, so insisting on it produced a graph of
    # 0 edges over the entire corpus. run_uid ADDRESSES; the digest RESOLVES.
    assert g["resolvable"] == 1 and g["orphan"] == 0
    assert g["edges"][0][1] == "NEW"
    assert g["edges"][0][0].startswith("derived:"), "an old record gets a derived address"


def test_the_lineage_cli_renders_and_counts(tmp_path, capsys):
    rows = [
        _rec("A", "build", "2026-01-01T00:00:00Z", outs=[("x", "f" * 64)]),
        _rec("B", "use", "2026-01-02T00:00:00Z", ins=[("x", "f" * 64)]),
    ]
    p = _hist(tmp_path, rows)
    assert cli.main(["lineage", "--log", str(p)]) == 0
    out = capsys.readouterr().out
    assert "build" in out and "use" in out and "1 edge" in out


def test_lineage_handles_an_output_with_no_digest_and_a_truncated_render(tmp_path, capsys):
    """Three states the graph must survive: an output recorded as MISSING (registered, never
    written — it has no digest and can produce no edge), a run whose edges exceed what the
    text view prints, and the JSON view a consumer would actually parse."""
    rows = [
        {
            "schema": "runprov.history.v2",
            "run_uid": "P",
            "script": "p",
            "started_utc": "2026-01-01T00:00:00Z",
            "finished_utc": "2026-01-01T00:00:00Z",
            "inputs": [],
            # MISSING: registered and never written, so no digest to join on
            "outputs": [{"path": "gone.tsv", "kind": "MISSING"}]
            + [{"path": f"o{i}.tsv", "sha256": f"{i:064d}"} for i in range(210)],
        }
    ]
    rows += [
        {
            "schema": "runprov.history.v2",
            "run_uid": "C",
            "script": "c",
            "started_utc": "2026-01-02T00:00:00Z",
            "finished_utc": "2026-01-02T00:00:00Z",
            "inputs": [{"path": f"o{i}.tsv", "sha256": f"{i:064d}"} for i in range(210)],
            "outputs": [],
        }
    ]
    g = cli._lineage(rows)
    assert g["resolvable"] == 210, "the MISSING output must produce no edge and no crash"
    p = _hist(tmp_path, rows)

    assert cli.main(["lineage", "--log", str(p)]) == 0
    text = capsys.readouterr().out

    # L-76. COUNT the lines, do not just look for the footnote. `"more edge(s) not shown" in
    # text` is true of a cap of 2 as easily as 200, so a silent shrink turned a usable graph
    # into two lines plus a note while the assertion that was meant to guard the truncation
    # stayed green.
    shown = cli.EDGES_SHOWN
    assert text.count(" -> ") == shown, (
        f"{text.count(' -> ')} edges printed, expected {shown} — the cap moved and the "
        f"footnote below would still have read as correct"
    )
    assert f"... {210 - shown} more edge(s) not shown" in text, (
        "the omission is stated with its SIZE; a reader cannot size it from a bare footnote"
    )
    assert "210 edge(s)" in text, "and the true total is still reported"
    # The count above reads `EDGES_SHOWN`, so it adapts to the constant and cannot see the
    # constant CHANGE -- verified: shrinking the cap to 2 leaves it green. Same shape as
    # L-57's cap, and the same remedy: the value gets its own bound. A handful of edges is
    # not a graph view, and printing tens of thousands is not a terminal view.
    assert 50 <= cli.EDGES_SHOWN <= 2000, (
        f"EDGES_SHOWN is {cli.EDGES_SHOWN}; below ~50 the text view stops being a graph, "
        f"above ~2000 it stops being readable in a terminal"
    )

    assert cli.main(["lineage", "--log", str(p), "--format", "json"]) == 0
    parsed = json.loads(capsys.readouterr().out)
    assert parsed["resolvable"] == 210 and parsed["ambiguous"] == 0


# ============================================== R7: a torn line must cost ONE record only
def test_a_record_appended_after_a_torn_line_survives(tmp_path):
    """R7. A process SIGKILLed mid-append leaves a line with no terminator — measured, 5 of
    12 trials. `O_APPEND` then positions the NEXT write at that fragment's end, so the new
    record is concatenated onto it and BOTH are unreadable. The torn one was lost anyway;
    the next one is collateral, and it is the one that had nothing wrong with it.

    Worse, `_load` counts the result as ONE unreadable line, so the reader understates the
    loss by exactly the record it did not know it had destroyed.
    """
    p = tmp_path / "h.jsonl"
    p.write_bytes(b'{"script": "A"}\n{"script": "B", "pad": "xxxx')  # B torn by a kill
    runprov.JsonlSink(p).append({"script": "C"})

    parsed, unreadable = [], 0
    for ln in p.read_text(encoding="utf-8").splitlines():
        try:
            parsed.append(json.loads(ln)["script"])
        except (json.JSONDecodeError, KeyError):
            unreadable += 1
    assert "C" in parsed, "the NEW record must survive a torn predecessor"
    assert "A" in parsed
    assert unreadable == 1, "exactly one record is lost — the one that was actually torn"


def test_the_transformation_log_heals_a_torn_entry_the_same_way(tmp_path):
    """The YAML view is appended forever too, so it inherits the same hazard and the same
    repair: a process killed mid-append leaves a fragment, and `O_APPEND` would concatenate
    the next entry onto it and lose both. One newline closes the fragment."""
    yaml = pytest.importorskip("yaml")
    p = tmp_path / "t.yml"
    p.write_bytes(b'- step: "A"\n  date: "2026-01-01T00:00:00Z"\n- step: "B"\n  date: "2026')
    runprov.sinks.YamlLogSink(p).append({"script": "C", "started_utc": "2026-01-02T00:00:00Z"})

    body = p.read_text(encoding="utf-8")
    assert body.count('- step: "C"') == 1
    assert '  date: "2026- step: "C"' not in body, "the new entry is not glued to the torn one"

    # AND THE HONEST HALF, asserted rather than hoped for: the torn entry leaves an
    # unterminated quoted scalar, so the WHOLE document stops parsing — not just the entry
    # that was torn. That is the difference between this file and the history, and it is the
    # entire reason `runs.jsonl` is the record of truth: a torn line there costs one line.
    # Testing this pins the trade-off in place, so nobody later reads the repair above and
    # concludes the YAML is as durable as the JSONL.
    with pytest.raises(yaml.YAMLError):
        yaml.safe_load(body)


def test_a_transformation_log_that_cannot_be_written_does_not_stop_the_run(tmp_path, capsys):
    """A VIEW is never the reason a record is lost. The sink swallows and says so, exactly as
    `JsonlSink` does — `sinks.py`'s own rule is that a sink which can abort a run gets
    removed from the run."""
    blocked = tmp_path / "as_a_directory.yml"
    blocked.mkdir()
    runprov.sinks.YamlLogSink(blocked).append({"script": "s", "started_utc": "2026-01-01"})
    assert "could not append to the transformation log" in capsys.readouterr().err


def test_a_yaml_sidecar_that_cannot_be_written_does_not_cost_the_json_one(tmp_path, capsys):
    """Same rule one level down. `_persist` returns "the record is on disk", and the record
    is the JSON; the YAML twin failing is worth a line on stderr and nothing more."""
    proj = _project(tmp_path)
    run = runprov.Run("s", project=proj)
    (tmp_path / "p.prov.yml").mkdir()  # the twin's path is occupied by a directory
    assert run.write(tmp_path / "p.prov.json") == tmp_path / "p.prov.json"

    assert (tmp_path / "p.prov.json").is_file(), "the record itself is written"
    assert json.loads((tmp_path / "p.prov.json").read_text(encoding="utf-8"))["script"] == "s"
    assert "could not write the YAML sidecar" in capsys.readouterr().err


def test_a_normal_append_gains_no_blank_line(tmp_path):
    """The healing must be invisible in the ordinary case. A stray blank line every append
    would make the history grow at twice the rate and read as corruption."""
    p = tmp_path / "h.jsonl"
    sink = runprov.JsonlSink(p)
    for i in range(3):
        sink.append({"n": i})
    body = p.read_text(encoding="utf-8")
    assert body.count("\n\n") == 0 and len(body.splitlines()) == 3
    assert [json.loads(x)["n"] for x in body.splitlines()] == [0, 1, 2]


def test_the_first_record_in_a_new_history_has_no_leading_newline(tmp_path):
    """An empty file has no torn tail to separate from, and a leading blank line would be
    an unreadable 'record' in every history's first position."""
    p = tmp_path / "fresh.jsonl"
    runprov.JsonlSink(p).append({"n": 1})
    assert p.read_bytes().startswith(b'{"n": 1}')
    assert len(p.read_text(encoding="utf-8").splitlines()) == 1


def test_the_reader_counts_the_torn_fragment_it_could_not_read(tmp_path):
    """The loss must be visible. `_load` counts unreadable lines rather than dropping them,
    and after healing that count is the true number of records lost."""
    p = tmp_path / "h.jsonl"
    p.write_bytes(b'{"script": "A"}\n{"torn')
    runprov.JsonlSink(p).append({"script": "C"})
    rows, bad = cli._load(p)
    assert [r["script"] for r in rows] == ["A", "C"]
    assert bad == 1, "the fragment is counted, not silently skipped"


# ================================================ input shapes the package had never met
CONTROL_CHARS_IN_NAMES = pytest.mark.skipif(
    sys.platform == "win32",
    reason="Windows refuses a filename containing \\n or \\r (OSError 22), so the fixture "
    "cannot be built and the hazard cannot exist there. The escaper itself is unit-tested "
    "on every platform by test_the_pin_escaper_neutralises_control_characters.",
)


def test_the_pin_escaper_neutralises_control_characters():
    """The escaping logic, as a pure function, on EVERY platform.

    The two tests below build a file whose NAME contains a control character, which Windows
    will not do. Skipping them there would leave the escaper untested on Windows even though
    the code runs there -- so the rule is checked directly, and only the filesystem fixture
    is skipped.
    """
    safe = runprov.Run._safe_for_pin
    assert safe("ordinary/name.tsv") == "ordinary/name.tsv", "an ordinary name is untouched"
    assert safe("a.tsv\n#  0000  FORGED.tsv") == "a.tsv\\n#  0000  FORGED.tsv"
    assert safe("b.tsv\rHIDDEN") == "b.tsv\\rHIDDEN"
    assert safe("c\ttab.tsv") == "c\\ttab.tsv"
    assert safe("d\x7fdel.tsv") == "d\\x7fdel.tsv"
    for bad in ("\n", "\r", "\x7f"):
        assert bad not in safe(f"x{bad}y"), f"{bad!r} must not survive into an artifact"
    # An accented name is NOT a control character and must survive intact -- escaping it
    # would mangle every legitimate non-ASCII filename in the corpus.
    assert safe("café.tsv") == "café.tsv"


@CONTROL_CHARS_IN_NAMES
def test_a_newline_in_a_filename_cannot_forge_a_pin_entry(tmp_path, monkeypatch):
    """THE PIN IS A LINE-ORIENTED FORMAT, and a filename may contain a newline.

    Measured before this was fixed: registering a file literally named

        a.tsv\\n#     0000000000000000  NEVER_READ.tsv

    produced a pin whose body read

        #   inputs (1), sha256:
        #     2d711642b726b044  a.tsv
        #     0000000000000000  NEVER_READ.tsv

    -- an artifact claiming, IN ITS OWN BODY, to derive from a file that was never read,
    with a digest nobody computed. The count said 1 and the list showed 2.

    A pin is the artifact's claim about what made it. A name that can forge an entry makes
    the claim worthless, so control characters are escaped and the name stays on one line.
    """
    monkeypatch.chdir(tmp_path)
    runprov.configure(root=tmp_path, run_log=tmp_path / "runs.jsonl")
    evil = tmp_path / "a.tsv\n#     0000000000000000  NEVER_READ.tsv"
    evil.write_text("x\n", encoding="utf-8")

    with runprov.Run("victim", provenance=tmp_path / "p.json") as run:
        run.input(evil)
        pin = run.header()

    body = [ln for ln in pin.splitlines() if ln.startswith("#     ")]
    assert len(body) == 1, f"one input must render as exactly one line, got {body}"
    assert "NEVER_READ" in body[0], "the real name is kept, escaped, not discarded"
    assert "\\n" in body[0], "the newline must be escaped, not literal"
    assert "inputs (1)" in pin, "the count and the listing must agree"


@CONTROL_CHARS_IN_NAMES
def test_a_carriage_return_cannot_overwrite_the_pin_either(tmp_path, monkeypatch):
    """A lone `\\r` rewrites the line in any terminal or editor that honours it, so a name
    can hide what precedes it without containing a newline at all."""
    monkeypatch.chdir(tmp_path)
    runprov.configure(root=tmp_path, run_log=tmp_path / "runs.jsonl")
    sneaky = tmp_path / "b.tsv\rHIDDEN"
    sneaky.write_text("x\n", encoding="utf-8")
    with runprov.Run("v", provenance=tmp_path / "p.json") as run:
        run.input(sneaky)
        pin = run.header()
    assert "\r" not in pin, "no raw control character may reach the artifact"
    assert "\\r" in pin


@requires_symlinks
def test_a_symlink_records_that_it_is_one_and_what_it_points_at(tmp_path, monkeypatch):
    """A record that cannot tell a file from a link to it is incomplete in a way that
    matters: the link can be repointed afterwards, and every hash in the record stays valid
    while describing different bytes. The digest is the TARGET's, which is correct -- that
    is what was read -- but the record must say so."""
    monkeypatch.chdir(tmp_path)
    runprov.configure(root=tmp_path, run_log=tmp_path / "runs.jsonl")
    real = tmp_path / "real.tsv"
    real.write_text("id\tv\nx\t1\n", encoding="utf-8")
    link = tmp_path / "link.tsv"
    link.symlink_to(real)

    rec = runprov.describe(link)
    assert rec["sha256"] == runprov.sha256(real), "the bytes read are the target's"
    assert rec["symlink"] is True
    assert pathlib.Path(rec["symlink_target"]).name == "real.tsv"
    assert runprov.describe(real).get("symlink") is False, "a plain file says so too"


@requires_symlinks
def test_a_broken_symlink_is_refused_by_name(tmp_path, monkeypatch):
    """`stat()` on a dangling link raises a bare FileNotFoundError naming neither the run
    nor what was wrong with it. Registering it must say both."""
    monkeypatch.chdir(tmp_path)
    runprov.configure(root=tmp_path, run_log=tmp_path / "runs.jsonl")
    dangling = tmp_path / "dangling.tsv"
    dangling.symlink_to(tmp_path / "gone.tsv")
    with runprov.Run("s", provenance=tmp_path / "p.json") as run:
        with pytest.raises(FileNotFoundError) as e:
            run.input(dangling)
    msg = str(e.value)
    assert "s:" in msg and "dangling.tsv" in msg
    # A PHRASE THE PATH CANNOT SUPPLY. `"symlink" in msg.lower()` passed even with the
    # distinction removed, because pytest's tmp_path is named after the test —
    # `test_a_broken_symlink_is_refused_by0` — and the path is in the message. The
    # assertion was satisfied by its own fixture. Mutation-tested.
    assert "whose target does not exist" in msg, "it must say WHY it does not exist"
    assert "gone.tsv" in msg, "and name the target it points at"


def test_the_pin_escaper_keeps_legitimate_non_ascii_while_escaping_the_rest():
    """The escaper must be SURGICAL. Its first implementation ran the whole name through
    `unicode_escape` whenever any character offended, so a name containing both an accent
    and a newline came out with the accent mangled too — `café` becoming `caf\\xe9` in a
    committed artifact, for a corpus that has accented filenames.

    Escape the offending character; leave every other one alone.
    """
    safe = runprov.Run._safe_for_pin
    assert safe("café.tsv") == "café.tsv"
    assert safe("café\nx.tsv") == "café\\nx.tsv", "the accent survives, the newline does not"
    assert safe("naïve\ttab.tsv") == "naïve\\ttab.tsv"

    # A lone surrogate OUTSIDE the surrogateescape range (U+DC80-U+DCFF). These do not come
    # from an undecodable byte -- they arrive from a caller that built the string itself --
    # but they are equally unencodable, so they must not reach an artifact either.
    # The surrogateescape range (U+DC80-U+DCFF): an undecodable BYTE from a POSIX
    # filename. It renders as the byte that was actually on disk, which is the useful
    # thing to see. Checked here so the branch is exercised on filesystems that
    # cannot hold such a name at all -- macOS and Windows both refuse to create one.
    assert safe("bad\udcff name.tsv") == "bad\\xff name.tsv"
    assert safe("x\ud800y.tsv") == "x\\ud800y.tsv"
    assert "\ud800" not in safe("x\ud800y.tsv").encode("utf-8", "strict").decode("utf-8")


def _fs_allows_non_utf8_names(where: pathlib.Path) -> bool:
    """Can THIS filesystem hold a filename that is not valid UTF-8?

    Probed rather than guessed from `sys.platform`: it is a property of the filesystem, not
    the operating system. Linux/ext4 stores filenames as arbitrary bytes; APFS rejects them
    with OSError 92 (Illegal byte sequence) and Windows stores UTF-16, so neither can build
    the fixture. An ext4 volume mounted elsewhere would behave like Linux, and a probe gets
    that right where a platform check would not.
    """
    try:
        # INSIDE the try, deliberately. On Windows `os.fsdecode` itself raises
        # UnicodeDecodeError on an invalid byte -- filenames there are UTF-16 -- so building
        # the name is already part of what is being probed. With this line outside the try,
        # the Windows job failed in the probe that exists to decide whether to skip.
        probe = where / os.fsdecode(b"\xff_probe")
        probe.write_text("x", encoding="utf-8")
        probe.unlink()
        return True
    except (OSError, UnicodeError, ValueError):
        return False


def test_a_filename_that_is_not_utf8_cannot_break_the_callers_write(tmp_path, monkeypatch):
    """A POSIX filename is BYTES, not text. Python decodes an undecodable one with
    `surrogateescape`, so the name carries lone surrogates — and `fh.write(run.header())`
    with `encoding="utf-8"`, which this package insists on everywhere, then raises

        UnicodeEncodeError: 'utf-8' codec can't encode character '\\udcff'

    Measured before this was fixed: registering such a file KILLED THE CALLER'S WRITE. The
    artifact was never created, and the failure came from inside provenance capture — the
    same class as a FIFO hanging the run, and the reason `_report._write` degrades rather
    than raising.
    """
    if not _fs_allows_non_utf8_names(tmp_path):
        pytest.skip(
            "this filesystem cannot store a non-UTF-8 filename, so the hazard "
            "cannot exist here; the escaper itself is unit-tested above"
        )
    monkeypatch.chdir(tmp_path)
    runprov.configure(root=tmp_path, run_log=tmp_path / "runs.jsonl")
    odd = tmp_path / os.fsdecode(b"bad\xff name.tsv")
    odd.write_text("x\n", encoding="utf-8")

    out = tmp_path / "artifact.tsv"
    with runprov.Run("s", provenance=tmp_path / "p.json") as run:
        run.input(odd)
        with run.open_output(out) as fh:  # the ordinary write path
            fh.write("id\tv\nx\t1\n")

    body = out.read_text(encoding="utf-8")
    assert "bad" in body and "name.tsv" in body, "the name is recorded, not dropped"
    assert "\\xff" in body, "the undecodable byte is shown as an escape"
    assert out.stat().st_size > 0


# ---------------------------------------------------------------- CALLING SHAPES
# Ten shapes the package had never been called in. Eight were already correct — a Run in a
# thread, eight concurrent Runs in threads, Runs in fork- and spawn-started children, a
# forked child capturing inside a capturing parent, a subprocess's output, `python -O`, and
# LIFO-nested captures. The two below were not.


def _out_of_order_captures(tmp_path, sink):
    """Start A, start B, stop A, stop B — with every write going through fd 1.

    Returns what reached the real stdout. `sink` stands in for the terminal: under pytest
    `sys.stdout` is pytest's buffer and never reaches fd 1, so a `print()` here would
    prove nothing about descriptors either way.

    Both Runs are CONSTRUCTED inside the redirect, because capture starts in
    `Run.__init__` and not in `__enter__`. Building them outside leaves their saved
    descriptors pointing at pytest's stdout, and the redirect then clobbers a live pipe —
    an earlier draft of this helper did exactly that and produced three failures that
    looked convincingly like the defect under test.
    """
    with open(sink, "w", encoding="utf-8") as fh:
        saved1, saved2 = os.dup(1), os.dup(2)
        os.dup2(fh.fileno(), 1)
        os.dup2(fh.fileno(), 2)
        try:
            a = runprov.Run("A", provenance=tmp_path / "a.json", terminal_log=tmp_path / "a.log")
            b = runprov.Run("B", provenance=tmp_path / "b.json", terminal_log=tmp_path / "b.log")
            a.__enter__()
            b.__enter__()
            os.write(1, b"BOTH-LIVE\n")
            a.__exit__(None, None, None)  # OUT OF ORDER, on purpose
            os.write(1, b"ONLY-B-LIVE\n")
            b.__exit__(None, None, None)
            os.write(1, b"AFTER-BOTH\n")
        finally:
            # Unconditional, so a failure of the thing under test cannot take pytest's own
            # streams down with it — without this a regression here loses the test report.
            os.dup2(saved1, 1)
            os.dup2(saved2, 2)
            os.close(saved1)
            os.close(saved2)
    return a, b, sink.read_text(encoding="utf-8")


def test_stopping_captures_out_of_order_does_not_destroy_stdout(tmp_path, monkeypatch):
    """THE DEFECT: fds 1 and 2 are process-global, so unwinding them in the wrong order
    leaves fd 1 pointing into a pipe nobody reads.

    Capture B, started inside A, saves *A's pipe* as its "original" — that is what fd 1
    held when B started. Stopping A first and B second therefore reinstalls A's pipe over
    fd 1 after A has stopped reading it. Measured before the fix: `AFTER-BOTH` and every
    subsequent write in the process vanished, with no error anywhere. That is precisely
    what `terminal.py`'s own docstring calls worse than recording nothing.
    """
    monkeypatch.chdir(tmp_path)
    runprov.configure(root=tmp_path, run_log=tmp_path / "runs.jsonl")
    _a, _b, passed = _out_of_order_captures(tmp_path, tmp_path / "real_stdout.txt")

    assert "AFTER-BOTH" in passed, (
        "stdout was destroyed by the out-of-order unwind — fd 1 was left pointing into an "
        "orphaned pipe, so everything written after both captures ended was lost"
    )
    assert "BOTH-LIVE" in passed and "ONLY-B-LIVE" in passed, "capture tees, never diverts"


def test_an_out_of_order_stop_is_recorded_not_merely_survived(tmp_path, monkeypatch):
    """Surviving it is not enough. The outer log stops early while its descriptors stay
    installed, so a reader comparing the log against the run's own start/finish times finds
    output that ends before the run did. The record has to explain that, or the discrepancy
    reads as a truncated log."""
    monkeypatch.chdir(tmp_path)
    runprov.configure(root=tmp_path, run_log=tmp_path / "runs.jsonl")
    a, b, _passed = _out_of_order_captures(tmp_path, tmp_path / "real_stdout.txt")

    outer = a.record["terminal_log"]
    assert outer.get("out_of_order") is True
    assert "after this run finished" in outer["note"]
    assert "out_of_order" not in b.record["terminal_log"], (
        "the INNER capture unwound normally and must not be flagged"
    )


def test_the_outer_log_keeps_what_was_written_while_it_was_live(tmp_path, monkeypatch):
    """The outer capture's file cannot be closed the instant `stop()` is called.

    Its pump thread may not have been scheduled yet, so bytes written while it was live are
    still in a chain of pipes. Measured without the bounded drain: the outer log recorded
    **none** of `BOTH-LIVE`, which was written while that capture was the only one the
    caller had asked for. It must equally not contain what came after it stopped.
    """
    monkeypatch.chdir(tmp_path)
    runprov.configure(root=tmp_path, run_log=tmp_path / "runs.jsonl")
    _a, _b, _passed = _out_of_order_captures(tmp_path, tmp_path / "real_stdout.txt")

    outer = (tmp_path / "a.log").read_text(encoding="utf-8", errors="replace")
    inner = (tmp_path / "b.log").read_text(encoding="utf-8", errors="replace")
    assert "BOTH-LIVE" in outer, "written while the outer capture was live — it belongs there"
    assert "ONLY-B-LIVE" not in outer, "written after it stopped — it does not"
    assert "BOTH-LIVE" in inner and "ONLY-B-LIVE" in inner
    assert "AFTER-BOTH" not in inner


def test_nested_captures_unwound_in_order_are_unaffected(tmp_path, monkeypatch):
    """The ordinary nesting — inner closed first — already worked, and the fix must not buy
    the out-of-order case at its expense. Neither record is flagged, and the terminal keeps
    every line."""
    monkeypatch.chdir(tmp_path)
    runprov.configure(root=tmp_path, run_log=tmp_path / "runs.jsonl")
    sink = tmp_path / "real_stdout.txt"
    with open(sink, "w", encoding="utf-8") as fh:
        saved1, saved2 = os.dup(1), os.dup(2)
        os.dup2(fh.fileno(), 1)
        os.dup2(fh.fileno(), 2)
        try:
            # Constructed here, not before the redirect — see `_out_of_order_captures`.
            a = runprov.Run("A", provenance=tmp_path / "a.json", terminal_log=tmp_path / "a.log")
            with a:
                os.write(1, b"OUTER-ONLY\n")
                b = runprov.Run(
                    "B", provenance=tmp_path / "b.json", terminal_log=tmp_path / "b.log"
                )
                with b:
                    os.write(1, b"NESTED\n")
                os.write(1, b"OUTER-AGAIN\n")
            os.write(1, b"AFTER-BOTH\n")
        finally:
            os.dup2(saved1, 1)
            os.dup2(saved2, 2)
            os.close(saved1)
            os.close(saved2)

    passed = sink.read_text(encoding="utf-8")
    for line in ("OUTER-ONLY", "NESTED", "OUTER-AGAIN", "AFTER-BOTH"):
        assert line in passed, f"{line} never reached the terminal"
    assert "out_of_order" not in a.record["terminal_log"]
    assert "out_of_order" not in b.record["terminal_log"]
    outer = (tmp_path / "a.log").read_text(encoding="utf-8", errors="replace")
    assert "OUTER-AGAIN" in outer, "the outer capture must resume recording after the inner"
    assert "NESTED" in outer, "the inner capture mirrors THROUGH the outer one"


def test_an_output_registered_before_a_chdir_is_not_hashed_somewhere_else(tmp_path, monkeypatch):
    """THE DEFECT: a record holds ONE `cwd`, and `write()` resolved pending outputs against
    whatever directory the script had reached by then.

    Measured before the fix: `output("rel.tsv")` followed by `os.chdir(sub)` and a relative
    write produced `path: "rel.tsv"` with `cwd:` the ORIGINAL directory and the sha256 of
    `sub/rel.tsv`. The record named a file that did not exist and carried the digest of a
    different one — silently, with nothing downstream able to detect it.

    MISSING is the honest answer here: the script said it would write `rel.tsv` beside the
    run's cwd and did not.
    """
    monkeypatch.chdir(tmp_path)
    runprov.configure(root=tmp_path, run_log=tmp_path / "runs.jsonl")
    sub = tmp_path / "sub"
    sub.mkdir()

    with runprov.Run("s", provenance=tmp_path / "p.json") as run:
        run.output("rel.tsv")
        os.chdir(sub)
        pathlib.Path("rel.tsv").write_text("written under the NEW cwd\n", encoding="utf-8")
    os.chdir(tmp_path)

    out = run.record["outputs"][0]
    assert out["kind"] == "MISSING", (
        "the file beside the recorded cwd was never written; hashing the one under the new "
        "cwd puts a digest in the record that its own path does not name"
    )
    assert "sha256" not in out


def test_a_path_registered_after_a_chdir_is_recorded_so_that_it_resolves(tmp_path, monkeypatch):
    """The other half: registering *after* a chdir must still hash the file the caller
    means, and must record it so that `cwd + path` finds it. A relative spelling cannot do
    that — the record's `cwd` is no longer the directory it belongs to — so the path is
    stored absolute."""
    monkeypatch.chdir(tmp_path)
    runprov.configure(root=tmp_path, run_log=tmp_path / "runs.jsonl")
    sub = tmp_path / "sub"
    sub.mkdir()
    (sub / "src.tsv").write_text("payload\n", encoding="utf-8")

    with runprov.Run("s", provenance=tmp_path / "p.json") as run:
        os.chdir(sub)
        run.input("src.tsv")
        run.output("made.tsv")
        pathlib.Path("made.tsv").write_text("out\n", encoding="utf-8")
    os.chdir(tmp_path)

    rec = run.record
    got = rec["inputs"][0]
    assert got["sha256"] == runprov.sha256(sub / "src.tsv"), "it hashed what the caller meant"
    for entry in (got, rec["outputs"][0]):
        assert (pathlib.Path(rec["cwd"]) / entry["path"]).exists(), (
            f"{entry['path']} does not resolve against the record's own cwd"
        )


def test_an_ordinary_relative_path_is_still_recorded_relative(tmp_path, monkeypatch):
    """The anchoring must be invisible when nothing moves. Rewriting every relative output
    into an absolute path would make each record machine-specific and every artifact
    comparison across two checkouts fail on the paths alone."""
    monkeypatch.chdir(tmp_path)
    runprov.configure(root=tmp_path, run_log=tmp_path / "runs.jsonl")
    (tmp_path / "in.tsv").write_text("a\n", encoding="utf-8")

    with runprov.Run("s", provenance=tmp_path / "p.json") as run:
        run.input("in.tsv")
        run.output("out.tsv")
        pathlib.Path("out.tsv").write_text("b\n", encoding="utf-8")

    assert run.record["inputs"][0]["path"] == "in.tsv"
    assert run.record["outputs"][0]["path"] == "out.tsv"
    assert run.record["outputs"][0]["kind"] == "file"


def test_a_capture_whose_stop_failed_does_not_poison_the_next_one(tmp_path, monkeypatch):
    """One failed teardown must not disable teardown for the rest of the process.

    `stop()` can raise before it touches the live stack at all — `_flush_std` runs first.
    A capture left on that stack is indistinguishable from one still holding the
    descriptors, so every capture started afterwards finds itself "not innermost", defers
    its own teardown to an owner that will never unwind it, and never restores fd 1. The
    process would lose terminal capture permanently after a single failure, which is the
    kind of degradation nothing reports.
    """
    monkeypatch.chdir(tmp_path)
    runprov.configure(root=tmp_path, run_log=tmp_path / "runs.jsonl")

    broken = runprov.Capture(tmp_path / "broken.log")
    broken.start()
    monkeypatch.setattr(runprov.terminal, "_flush_std", _raise_runtime)
    assert "stopping capture failed" in broken.stop()["error"]
    monkeypatch.undo()
    broken._restore_fds()
    broken._close_saved()

    assert runprov.terminal._LIVE == [], (
        "a capture that failed to stop is still on the live stack, so the next capture "
        "will defer its teardown to it and never restore the descriptors"
    )
    # And prove it by using the next one, rather than trusting the list.
    sink = tmp_path / "real_stdout.txt"
    with open(sink, "w", encoding="utf-8") as fh:
        saved1, saved2 = os.dup(1), os.dup(2)
        os.dup2(fh.fileno(), 1)
        os.dup2(fh.fileno(), 2)
        try:
            with runprov.Run(
                "after", provenance=tmp_path / "p.json", terminal_log=tmp_path / "a.log"
            ):
                os.write(1, b"DURING\n")
            os.write(1, b"AFTER\n")
        finally:
            os.dup2(saved1, 1)
            os.dup2(saved2, 2)
            os.close(saved1)
            os.close(saved2)
    body = sink.read_text(encoding="utf-8")
    assert "DURING" in body and "AFTER" in body
    assert "DURING" in (tmp_path / "a.log").read_text(encoding="utf-8", errors="replace")


class _RunawayClock:
    """A clock that always advances, so the drain's deadline is reached rather than its
    idle condition. Substituted for the module's `time` reference — patching `time` itself
    would reach every other user of it in the process, including pytest's own teardown."""

    def __init__(self):
        self.now = 0.0
        self.slept = 0

    def monotonic(self):
        self.now += 0.4
        return self.now

    def sleep(self, _seconds):
        self.slept += 1


def test_the_drain_gives_up_rather_than_waiting_for_a_pump_that_never_idles(tmp_path, monkeypatch):
    """The out-of-order path waits for the mirror thread to finish what is already in the
    pipe. That wait is a heuristic on a byte count, so it MUST be bounded: a capture whose
    inner run keeps printing would otherwise hold the outer run's exit open indefinitely —
    a provenance module hanging the run it describes, which is the FIFO defect again.

    The count is made to keep moving, so the only way out is the deadline.
    """
    monkeypatch.chdir(tmp_path)
    cap = runprov.Capture(tmp_path / "t.log")
    clock = _RunawayClock()
    monkeypatch.setattr(runprov.terminal, "time", clock)

    ticking = itertools.count()
    # `_seen` is an instance attribute, so the property goes on the CLASS with
    # raising=False. A property is a data descriptor and therefore still wins over the
    # instance value `__init__` already set.
    monkeypatch.setattr(type(cap), "_seen", property(lambda _s: next(ticking)), raising=False)
    cap._quiesce(limit=1.0, idle=0.0)

    assert clock.slept >= 1, "it must actually have waited, not fallen straight through"
    assert clock.now <= 1.0 + 0.4 * 2, "and it must have stopped at the deadline"


def test_tearing_down_a_capture_that_has_no_pump_thread_restores_the_descriptors(tmp_path):
    """`_teardown_fds` guards `self._thread is not None`, and the guard has to hold: the
    descriptors are what the caller's output depends on, and skipping the restore because
    there is no thread to join would be the swallow-stdout failure with extra steps."""
    cap = runprov.Capture(tmp_path / "t.log")
    sink = tmp_path / "sink.txt"
    with open(sink, "w", encoding="utf-8") as fh:
        before = os.dup(1)
        cap._saved = {1: os.dup(1)}  # saved, but no pipe and no pump was ever started
        os.dup2(fh.fileno(), 1)
        try:
            cap._teardown_fds()
            os.write(1, b"RESTORED\n")
        finally:
            os.dup2(before, 1)
            os.close(before)
    assert cap._saved == {}, "the saved descriptor must be released, not leaked"
    assert "RESTORED" not in sink.read_text(encoding="utf-8"), (
        "fd 1 was left pointing at the sink — the restore was skipped"
    )


def test_the_drain_stops_as_soon_as_the_pump_is_idle(tmp_path, monkeypatch):
    """The other half of the bound. A drain that always runs to its deadline would add two
    seconds to every out-of-order stop while looking correct in every assertion about
    CONTENT — mutation testing found exactly that hole: removing the early return changed
    no observable output, only the wait.
    """
    monkeypatch.chdir(tmp_path)
    cap = runprov.Capture(tmp_path / "t.log")
    clock = _RunawayClock()
    monkeypatch.setattr(runprov.terminal, "time", clock)

    cap._quiesce(limit=100.0, idle=0.0)  # `_seen` never moves — the pump is idle

    assert clock.slept <= 2, "it kept waiting after the byte count had settled"
    assert clock.now < 10.0, "it ran toward the deadline instead of returning when idle"


def test_the_yaml_view_flags_a_log_that_stops_before_its_run_does(tmp_path, monkeypatch):
    """`out_of_order` has to survive into the YAML view, for the same reason `capture` does.

    The old `transformation_log.yml` carried a bare path, so a reader could not tell a log
    that saw everything from one that could not. This is that gap one step further: without
    the flag, a log whose last line predates `finished_utc` reads as truncated, and the
    natural conclusion — the run was killed — is wrong.
    """
    yaml = pytest.importorskip("yaml")
    monkeypatch.chdir(tmp_path)
    runprov.configure(root=tmp_path, run_log=tmp_path / "runs.jsonl")
    a, b, _passed = _out_of_order_captures(tmp_path, tmp_path / "real_stdout.txt")

    rendered = cli._yaml([a.record])
    assert "terminal_log_out_of_order: true" in rendered
    assert yaml.safe_load(rendered)[0]["terminal_log_out_of_order"] is True
    assert "out_of_order" not in cli._yaml([b.record]), (
        "the ordinary case must not carry the caveat"
    )


# ------------------------------------------------------- CONSUMER COMPATIBILITY (v1)
# `tests/fixtures/history_v1.jsonl` is real v1 output, produced by the v1 code at 609299b.
# See tests/fixtures/README.md — a compatibility fixture written from memory tests memory.

FIXTURES = pathlib.Path(__file__).parent / "fixtures"


def _v1_rows():
    return [json.loads(line) for line in (FIXTURES / "history_v1.jsonl").read_text().splitlines()]


def test_the_v1_fixture_really_is_v1(tmp_path):
    """Guards the guard. If a regenerated fixture ever came out as v2, every test below
    would keep passing while testing nothing at all — the shape this codebase has been
    caught by before, where a check cannot tell its subject from its absence."""
    rows = _v1_rows()
    assert [r["schema"] for r in rows] == ["runprov.run.v1"] * 2
    assert all("run_uid" not in r for r in rows), "v1 predates run_uid"
    assert all("script_file" not in r for r in rows), "v1 predates script_file"
    out = [o for r in rows for o in (r.get("outputs") or []) if "sha256" in o]
    assert out and all("content_sha256" not in o for o in out), "v1 has no content digest"


def test_todays_cli_reads_a_v1_history(tmp_path, capsys):
    """The record format moved to v2 in ADR-029 and the histories in the field are v1. A
    reader that needs its own schema version is not a reader of the history, it is a reader
    of the present."""
    rc = cli.main(["log", "--log", str(FIXTURES / "history_v1.jsonl")])
    seen = capsys.readouterr()
    body = seen.out + seen.err  # the "n of m run(s)" header is a diagnostic, not the report
    assert rc == 0
    assert "v1_producer" in body and "v1_failed" in body
    assert "2 of 2 run(s)" in body, "neither v1 record may be skipped as unreadable"
    assert "1 FAILED" in body and "ValueError: deliberate" in body
    assert "in.tsv" in body and "out.tsv" in body, "v1 inputs and outputs must render"


def test_the_yaml_view_of_a_v1_record_says_what_it_cannot_know(tmp_path):
    """v1 has no `script_file`, and the YAML view's `script:` is that field. Filling it with
    the step name would produce a populated-looking path that names something other than
    what ran — which is the exact defect the old transformation log had."""
    yaml = pytest.importorskip("yaml")
    rendered = cli._yaml(_v1_rows())
    docs = yaml.safe_load(rendered)
    assert docs[0]["step"] == "v1_producer"
    assert "not recorded" in docs[0]["script"], "it must not invent a path"
    assert docs[0]["input"] == "in.tsv" and docs[0]["output"] == "out.tsv"


def test_lineage_joins_across_the_v1_v2_schema_boundary(tmp_path, monkeypatch):
    """THE DEFECT: an edge from a v1 producer to a v2 consumer was reported as an ORPHAN.

    The join preferred `content_sha256` and fell back to `sha256`. A v1 output carries only
    the raw hash; a v2 input of the SAME BYTES carries both, and the content digest is taken
    over normalised text so it never equals the raw one. The two sides were therefore keyed
    differently, and every edge crossing an upgrade vanished — reported not as unknown but
    as "produced by no recorded run", which is a false statement rather than a missing one.

    This is the ordinary case for anyone adopting v2: the history is v1 and the new runs
    are not.
    """
    monkeypatch.chdir(tmp_path)
    runlog = tmp_path / "runs.jsonl"
    runlog.write_text((FIXTURES / "history_v1.jsonl").read_text(), encoding="utf-8")
    runprov.configure(root=tmp_path, run_log=runlog)

    # A REAL v2 run reading the v1 producer's actual artifact — not a hand-copied digest.
    artifact = tmp_path / "out.tsv"
    artifact.write_bytes((FIXTURES / "artifact_v1.tsv").read_bytes())
    with runprov.Run("v2_consumer", provenance=tmp_path / "c.json") as run:
        run.input(artifact)

    rows = [json.loads(x) for x in runlog.read_text(encoding="utf-8").splitlines()]
    got = cli._lineage(rows)
    assert got["resolvable"] == 1, "the v1 -> v2 edge must resolve"
    assert got["ambiguous"] == 0
    producer, consumer = got["edges"][0]
    assert producer.startswith("derived:"), "a v1 producer has no uid and is addressed"
    assert consumer == run.record["run_uid"]
    assert got["records_without_uid"] == 2, "and the v1 records are counted, not dropped"


def test_lineage_joins_the_other_way_too_a_v2_producer_read_by_a_v1_consumer(tmp_path):
    """The reverse direction, which is a DIFFERENT half of the join.

    v1-produces/v2-consumes is fixed by probing every digest the consumer has. This one is
    fixed by INDEXING every digest the producer has: a v2 output keyed only under its
    preferred `content_sha256` is invisible to a v1 consumer, which has nothing but the raw
    hash to look it up by. Mutation testing found this gap — the producer-side breadth
    survived every test until this fixture existed.

    Both records are real: today's code wrote `shared.tsv`, then the v1 code at 609299b read
    it, in that order, so the producer-finished-before-consumer-started rule holds honestly
    rather than by an edited timestamp.
    """
    rows = [json.loads(x) for x in (FIXTURES / "history_v2_then_v1.jsonl").read_text().splitlines()]
    assert [r["schema"] for r in rows] == ["runprov.history.v2", "runprov.run.v1"], (
        "the fixture must be a v2 producer followed by a v1 consumer, in that order"
    )
    got = cli._lineage(rows)
    assert got["resolvable"] == 1, "a v1 consumer must find a v2 producer"
    assert got["orphan"] == 0
    producer, consumer = got["edges"][0]
    assert producer == rows[0]["run_uid"]
    assert consumer.startswith("derived:"), "the v1 consumer has no uid of its own"


# ------------------------------------------------------------- CONTAINER FORMATS
# Measured across csv, tsv, txt, json, xlsx (openpyxl AND xlsxwriter), parquet, feather,
# pickle, npy, npz, gz, zip, tar and sqlite: every one is stable under `content_digest`
# except the archives below, which embed a write time the way the gzip header does.


def _zip(path, entries, **kw):
    with zipfile.ZipFile(path, "w") as z:
        for name, data in entries:
            z.writestr(zipfile.ZipInfo(name, kw.get("when", (1999, 1, 1, 0, 0, 0))), data)
    return path


BASE = [("a.csv", "id,v\na,1\n"), ("b.txt", "hello\n")]


def test_a_zip_rewritten_from_identical_files_has_one_content_digest(tmp_path):
    """A zip stores a modification time PER ENTRY, so an archive rebuilt from unchanged
    files is a different byte sequence every time. That is the gzip-header defect one
    container up, and it reaches further than it looks: `.xlsx`, `.docx`, `.odt`, `.whl`
    and `.npz` are all zips. Measured: two `df.to_excel()` calls a second apart produced
    different digests for the same two rows, so a spreadsheet regenerated from unchanged
    data made everything pinning it differ — the oscillation this module exists to end.
    """
    early = _zip(tmp_path / "early.zip", BASE, when=(1999, 1, 1, 0, 0, 0))
    later = _zip(tmp_path / "later.zip", BASE, when=(2026, 8, 12, 9, 30, 0))
    assert runprov.sha256(early) != runprov.sha256(later), "the BYTES differ; that is the point"
    assert runprov.content_digest(early) == runprov.content_digest(later)


def test_the_raw_hash_still_separates_what_the_content_digest_calls_equal(tmp_path):
    """Both questions keep their own answer. `content_digest` says "same content"; `sha256`
    still says "not the same file", and every record carries both."""
    early = _zip(tmp_path / "early.zip", BASE, when=(1999, 1, 1, 0, 0, 0))
    later = _zip(tmp_path / "later.zip", BASE, when=(2026, 8, 12, 9, 30, 0))
    rec_a, rec_b = runprov.describe(early), runprov.describe(later)
    assert rec_a["content_sha256"] == rec_b["content_sha256"]
    assert rec_a["sha256"] != rec_b["sha256"]


@pytest.mark.parametrize(
    ("what", "entries"),
    [
        ("content changed", [("a.csv", "id,v\na,2\n"), ("b.txt", "hello\n")]),
        ("entry renamed", [("a2.csv", "id,v\na,1\n"), ("b.txt", "hello\n")]),
        ("entry removed", [("a.csv", "id,v\na,1\n")]),
        ("entry added", [*BASE, ("c.txt", "x\n")]),
        # The one a name-plus-digest concatenation without separators would miss.
        ("contents swapped between names", [("a.csv", "hello\n"), ("b.txt", "id,v\na,1\n")]),
    ],
)
def test_a_zip_digest_still_sees_a_real_change(tmp_path, what, entries):
    """A digest that ignores the right things must still catch everything else — the
    failure mode of "normalise harder" is a hash that cannot tell two files apart."""
    ref = _zip(tmp_path / "ref.zip", BASE)
    other = _zip(tmp_path / "other.zip", entries)
    assert runprov.content_digest(ref) != runprov.content_digest(other), what


def test_the_order_entries_were_written_in_is_not_content(tmp_path):
    """Entry order is a property of the writer, not of the archive's content."""
    ref = _zip(tmp_path / "ref.zip", BASE)
    flipped = _zip(tmp_path / "flipped.zip", list(reversed(BASE)))
    assert runprov.content_digest(ref) == runprov.content_digest(flipped)


def test_a_directory_entry_is_not_an_empty_file(tmp_path):
    """Both contribute a name and no bytes. Without the type flag they would collide, and
    an archive that lost a file to a directory of the same name would hash unchanged."""
    as_file = _zip(tmp_path / "f.zip", [("thing", "")])
    as_dir = tmp_path / "d.zip"
    with zipfile.ZipFile(as_dir, "w") as z:
        z.writestr(zipfile.ZipInfo("thing/", (1999, 1, 1, 0, 0, 0)), "")
    assert runprov.content_digest(as_file) != runprov.content_digest(as_dir)


def _core_xml(created, title="t"):
    return (
        '<?xml version="1.0"?><cp:coreProperties xmlns:dcterms="http://purl.org/dc/terms/">'
        f"<dc:title>{title}</dc:title>"
        f'<dcterms:created xsi:type="dcterms:W3CDTF">{created}</dcterms:created>'
        f'<dcterms:modified xsi:type="dcterms:W3CDTF">{created}</dcterms:modified>'
        "</cp:coreProperties>"
    )


def test_the_ooxml_write_timestamp_is_not_content(tmp_path):
    """Zip entry mtimes were only half of it. Measured on real openpyxl output: the ten
    entries of two `.xlsx` files written a second apart were byte-identical except
    `docProps/core.xml`, whose `dcterms:created` and `dcterms:modified` carry the write
    time — inside the entry, where normalising the container cannot reach it."""
    a = _zip(
        tmp_path / "a.xlsx",
        [("xl/w.xml", "<r/>"), ("docProps/core.xml", _core_xml("2026-08-12T08:41:32Z"))],
    )
    b = _zip(
        tmp_path / "b.xlsx",
        [("xl/w.xml", "<r/>"), ("docProps/core.xml", _core_xml("2026-08-12T08:41:33Z"))],
    )
    assert runprov.content_digest(a) == runprov.content_digest(b)


def test_the_rest_of_the_ooxml_properties_are_still_content(tmp_path):
    """Only the two timestamp elements are dropped. A changed title is a changed document,
    and blanking the whole part would have hidden it."""
    a = _zip(
        tmp_path / "a.xlsx", [("docProps/core.xml", _core_xml("2026-01-01T00:00:00Z", "before"))]
    )
    b = _zip(
        tmp_path / "b.xlsx", [("docProps/core.xml", _core_xml("2026-01-01T00:00:00Z", "after"))]
    )
    assert runprov.content_digest(a) != runprov.content_digest(b)


def test_the_ooxml_rule_reaches_only_the_part_the_spec_names(tmp_path):
    """ADR-029 R2 in a new place: a rule that rewrites bytes must know exactly whose bytes.
    The JSON stamp rule once fired on any `*.json` and erased a user's legitimate
    `mtime_utc`, colliding two different datasets. This one is pinned to the single path
    the OOXML specification fixes, so a user's own `core.xml` keeps its timestamps."""
    a = _zip(tmp_path / "a.zip", [("data/core.xml", _core_xml("2026-01-01T00:00:00Z"))])
    b = _zip(tmp_path / "b.zip", [("data/core.xml", _core_xml("2026-06-06T00:00:00Z"))])
    assert runprov.content_digest(a) != runprov.content_digest(b), (
        "a `core.xml` that is not the OOXML part is ordinary user data"
    )


def test_an_oversized_core_xml_is_streamed_and_says_so_by_differing(tmp_path):
    """The substitution needs the part whole, so it is bounded. `docProps/core.xml` is a
    handful of elements by specification; an entry claiming that name and megabytes of body
    is not that part, and is hashed as ordinary bytes rather than read into memory."""
    big = "<x>" + "p" * (1 << 20) + "</x>"
    a = _zip(tmp_path / "a.xlsx", [("docProps/core.xml", _core_xml("2026-01-01T00:00:00Z") + big)])
    b = _zip(tmp_path / "b.xlsx", [("docProps/core.xml", _core_xml("2026-06-06T00:00:00Z") + big)])
    assert runprov.content_digest(a) != runprov.content_digest(b)


def _tar(path, members):
    with tarfile.open(path, "w") as t:
        for name, body, when in members:
            info = tarfile.TarInfo(name)
            data = body.encode()
            info.size, info.mtime, info.uid, info.gid = len(data), when, 1000, 1000
            t.addfile(info, io.BytesIO(data))
    return path


def test_a_tar_carries_mtime_uid_and_gid_and_none_of_them_are_content(tmp_path):
    """Same argument as the zip, plus ownership: the same tree packed by two people on two
    machines is the same content, and a digest that disagreed would report every transfer
    as a change."""
    a = _tar(tmp_path / "a.tar", [("m.txt", "one\n", 1000000000)])
    b = tmp_path / "b.tar"
    with tarfile.open(b, "w") as t:
        info = tarfile.TarInfo("m.txt")
        info.size, info.mtime, info.uid, info.gid = 4, 1786524101, 501, 20
        t.addfile(info, io.BytesIO(b"one\n"))
    assert runprov.sha256(a) != runprov.sha256(b)
    assert runprov.content_digest(a) == runprov.content_digest(b)


def test_a_tar_digest_sees_content_and_link_targets(tmp_path):
    """A symlink's TARGET is its content — two archives whose links point elsewhere are not
    the same archive, and a link contributes no bytes for a naive digest to notice."""
    a = _tar(tmp_path / "a.tar", [("m.txt", "one\n", 1)])
    b = _tar(tmp_path / "b.tar", [("m.txt", "two\n", 1)])
    assert runprov.content_digest(a) != runprov.content_digest(b)

    def linked(path, target):
        with tarfile.open(path, "w") as t:
            info = tarfile.TarInfo("l")
            info.type, info.linkname = tarfile.SYMTYPE, target
            t.addfile(info)
            d = tarfile.TarInfo("dir")
            d.type = tarfile.DIRTYPE
            t.addfile(d)
        return path

    assert runprov.content_digest(linked(tmp_path / "l1.tar", "here")) != runprov.content_digest(
        linked(tmp_path / "l2.tar", "elsewhere")
    )


def test_a_tar_member_that_cannot_be_streamed_falls_back_to_the_raw_hash(tmp_path, monkeypatch):
    """`extractfile` returns None for a regular member it cannot open as a stream.

    Inventing a marker for it would put a content digest in the record for an archive
    nobody actually read — and two archives differing only inside unreadable members would
    then be certified identical. The raw hash of the container is the honest answer, and it
    is the fallback a corrupt gzip or zip already takes.
    """
    a = _tar(tmp_path / "a.tar", [("m.txt", "one\n", 1)])
    b = _tar(tmp_path / "b.tar", [("m.txt", "two\n", 1)])
    assert runprov.content_digest(a) != runprov.content_digest(b)

    monkeypatch.setattr(tarfile.TarFile, "extractfile", lambda self, m: None)
    assert runprov.content_digest(a) == runprov.sha256(a)
    assert runprov.content_digest(a) != runprov.content_digest(b), (
        "falling back must not make two different archives agree"
    )


def test_a_zip_directory_and_an_empty_file_cannot_collide(tmp_path):
    """No type flag is written beside the name, because a zip directory IS a name ending in
    `/`. The separator has to carry the weight instead: without the NUL, entries `a` and
    `b` build the same byte string as a single entry `ab`."""
    empty_file = _zip(tmp_path / "f.zip", [("thing", "")])
    as_dir = tmp_path / "d.zip"
    with zipfile.ZipFile(as_dir, "w") as z:
        z.writestr(zipfile.ZipInfo("thing/", (1999, 1, 1, 0, 0, 0)), "")
    assert runprov.content_digest(empty_file) != runprov.content_digest(as_dir)

    split = _zip(tmp_path / "split.zip", [("a/", ""), ("b/", "")])
    joined = _zip(tmp_path / "joined.zip", [("a/b/", "")])
    assert runprov.content_digest(split) != runprov.content_digest(joined), (
        "two names must not concatenate into one"
    )


def test_a_truncated_archive_falls_back_to_the_raw_hash(tmp_path):
    """A container that announces itself and then cannot be read must not take the run down
    from inside provenance — the same rule the corrupt-gzip path follows."""
    good = _zip(tmp_path / "good.zip", BASE)
    broken = tmp_path / "broken.zip"
    broken.write_bytes(good.read_bytes()[:-40] + b"\0" * 40)
    if zipfile.is_zipfile(broken):  # only meaningful if it still LOOKS like a zip
        assert runprov.content_digest(broken) == runprov.sha256(broken)


def test_the_order_members_were_added_in_is_not_tar_content(tmp_path):
    """As for the zip: member order is the writer's business, not the archive's content."""
    one = _tar(tmp_path / "one.tar", [("a.txt", "A\n", 1), ("b.txt", "B\n", 1)])
    two = _tar(tmp_path / "two.tar", [("b.txt", "B\n", 1), ("a.txt", "A\n", 1)])
    assert runprov.sha256(one) != runprov.sha256(two)
    assert runprov.content_digest(one) == runprov.content_digest(two)


def test_a_tar_name_and_a_link_target_cannot_be_confused_for_each_other(tmp_path):
    """A CONSTRUCTED collision, which is what makes the separator load-bearing rather than
    ornamental.

    Each member contributes `name`, a one-byte type flag, a NUL, then its payload. Drop
    that NUL and these two archives build the identical byte string:

        name "a",  link -> "Lb"    ->  "a"  "L" "Lb"
        name "aL", link -> "b"     ->  "aL" "L" "b"

    ADR-029's R9 row called the equivalent zip case a "trivial second preimage" and was
    corrected — for digests that needs a preimage attack on SHA-256. Here the collision is
    in the ENCODING and takes two lines to write, which is the version of that argument
    that actually holds.

    Note which separator this pins. An earlier draft put a NUL after the NAME and claimed
    this same collision for it; that was wrong, because the flag's own NUL already
    terminates the name field. The redundant byte was removed rather than left with a
    plausible-looking justification.
    """

    def linked(path, name, target):
        with tarfile.open(path, "w") as t:
            info = tarfile.TarInfo(name)
            info.type, info.linkname = tarfile.SYMTYPE, target
            t.addfile(info)
        return path

    a = linked(tmp_path / "a.tar", "a", "Lb")
    b = linked(tmp_path / "b.tar", "aL", "b")
    assert runprov.content_digest(a) != runprov.content_digest(b)


# ----------------------------------------------------------- CONDA-FAMILY PREFIXES
# Installation was verified under pip (wheel and sdist), uv, and mamba. What was NOT
# right was what the snapshot RECORDED there.


def _conda_prefix(root, packages, *, corrupt=(), stray=()):
    """A prefix laid out the way conda, mamba, micromamba and pixi all lay one out."""
    meta = root / "conda-meta"
    meta.mkdir(parents=True, exist_ok=True)
    for name, version, build in packages:
        body = json.dumps({"name": name, "version": version, "build": build})
        (meta / f"{name}-{version}-{build}.json").write_text(body, encoding="utf-8")
    for filename in corrupt:
        (meta / filename).write_text("{not json", encoding="utf-8")
    for filename in stray:
        (meta / filename).write_text("{}", encoding="utf-8")
    return root


PKGS = [("samtools", "1.21", "h50ea8bc_0"), ("blast", "2.16.0", "h66d330f_4")]


def test_a_conda_environment_is_not_only_its_python_packages(tmp_path):
    """THE GAP. `importlib.metadata` sees Python distributions, and a conda-family
    environment is mostly not those. Measured on a bare `mamba create -p env python=3.12`:
    conda installed **27** packages and the snapshot recorded **8**.

    The nineteen it missed were libgcc, openssl, sqlite, icu, ncurses, tk and the rest — and
    in this package's own target setting that is exactly where `samtools`, `blast` and
    `mmseqs2` live. A snapshot claiming to describe the environment that produced a result,
    silently missing every non-Python tool it depended on.
    """
    got = runprov.environment.conda_packages(_conda_prefix(tmp_path, PKGS))
    assert got == {"blast": "2.16.0=h66d330f_4", "samtools": "1.21=h50ea8bc_0"}


def test_a_prefix_with_no_conda_meta_is_simply_not_a_conda_prefix(tmp_path):
    """An ordinary venv must be unaffected — no section, no header line, and no field in
    the record about a manager it has never met."""
    assert runprov.environment.conda_packages(tmp_path) == {}
    assert "conda" not in runprov.environment.render({"a": "1"}, 0, {})


def test_an_unreadable_conda_record_is_recovered_from_its_filename(tmp_path):
    """`name-version-build.json` is the layout by construction, so a corrupt record still
    identifies its package. Dropping it would understate the environment, which is the one
    thing a snapshot must never do."""
    root = _conda_prefix(tmp_path, PKGS, corrupt=("mmseqs2-15.6f452-pl5321h6a68c12_0.json",))
    got = runprov.environment.conda_packages(root)
    assert got["mmseqs2"] == "15.6f452=pl5321h6a68c12_0"
    assert len(got) == 3


def test_a_conda_record_that_names_nothing_is_skipped_not_guessed(tmp_path):
    """A corrupt file whose NAME does not carry the layout either. There is nothing to
    recover, and inventing an entry would be worse than omitting one."""
    root = _conda_prefix(tmp_path, PKGS, corrupt=("garbage.json",))
    assert set(runprov.environment.conda_packages(root)) == {"blast", "samtools"}


def test_the_snapshot_digest_moves_when_a_conda_package_moves(tmp_path):
    """The whole point of the snapshot is answering "did the environment change?". Before
    this, upgrading samtools changed nothing a record could see."""
    pip = {"pandas": "3.0.5"}
    before = runprov.environment.conda_packages(_conda_prefix(tmp_path / "a", PKGS))
    after = runprov.environment.conda_packages(
        _conda_prefix(tmp_path / "b", [("samtools", "1.22", "h50ea8bc_0"), PKGS[1]])
    )
    d = runprov.environment.digest
    assert d(runprov.environment.render(pip, 0, before)) != d(
        runprov.environment.render(pip, 0, after)
    )


def test_where_the_prefix_lives_is_not_part_of_the_environment(tmp_path):
    """Two identical environments installed at different paths ARE the same environment.
    A prefix in the body would make the digest machine-specific and defeat the content
    addressing the file is named by — the same defect as an absolute path in a fixture."""
    here = runprov.environment.conda_packages(_conda_prefix(tmp_path / "opt" / "envs" / "x", PKGS))
    there = runprov.environment.conda_packages(_conda_prefix(tmp_path / "home" / "y", PKGS))
    body = runprov.environment.render({"pandas": "3.0.5"}, 0, here)
    assert body == runprov.environment.render({"pandas": "3.0.5"}, 0, there)
    assert str(tmp_path) not in body


def test_the_snapshot_record_counts_what_conda_put_there(tmp_path, monkeypatch):
    """Through `write_snapshot`, so the wiring is tested and not just the helper — the
    ADR-015 lesson that a fix has to be proved reachable."""
    monkeypatch.setattr(sys, "prefix", str(_conda_prefix(tmp_path / "pfx", PKGS)))
    rec = runprov.environment.write_snapshot(tmp_path / "envs")
    assert rec["n_conda_packages"] == 2
    body = pathlib.Path(rec["path"]).read_text(encoding="utf-8")
    assert "samtools=1.21=h50ea8bc_0" in body
    assert "# conda    : 2 package(s)" in body

    monkeypatch.setattr(sys, "prefix", str(tmp_path / "plain"))
    plain = runprov.environment.write_snapshot(tmp_path / "envs")
    assert "n_conda_packages" not in plain
    assert plain["sha256"] != rec["sha256"], "the two environments are not the same one"


def test_conda_packages_are_ordered_by_name_not_by_filename(tmp_path):
    """The same rule `installed_packages` follows, and for the same reason.

    Entries are read from a sorted glob, which orders whole FILENAMES by byte — so `R-4.4`
    sorts before `arrow-1.3` because `R` is 0x52 and `a` is 0x61. The snapshot is keyed by
    the digest of its body, so an ordering that depends on how names happen to be spelled
    would give two identical environments two different digests, and every record pointing
    at `env-<sha16>` would disagree about which environment ran.
    """
    root = _conda_prefix(tmp_path, [("R", "4.4.2", "h1b0"), ("arrow", "1.3.0", "py312")])
    got = runprov.environment.conda_packages(root)
    assert list(got) == ["arrow", "R"], "sorted case-insensitively by NAME"
    body = runprov.environment.render({}, 0, got).splitlines()
    assert body.index("arrow=1.3.0=py312") < body.index("R=4.4.2=h1b0")


# --------------------------------------------------- PARITY WITH THE SCRIPTS IT REPLACES
# The predecessor's scripts do three things this package has to keep doing: configure
# logging to a file and to stdout, write a per-run YAML manifest, and run `json_safe()`
# over pandas/numpy scalars before dumping. Each is tested here against what runprov offers.


class _Scalar:
    """A numpy-style scalar: NOT an int, but exposes `.item()`.

    A fake rather than numpy, because this package has no dependencies and its test suite
    must not acquire one to prove a duck-typing rule. `numpy.int64` behaves exactly this
    way — it subclasses neither `int` nor `bool`, which is the whole reason the bug existed.
    """

    def __init__(self, value):
        self._value = value

    def item(self):
        return self._value

    def __str__(self):
        return f"<scalar {self._value}>"


class _NotAScalar:
    """`.item()` exists and raises — a numpy ARRAY with more than one element."""

    def item(self):
        raise ValueError("can only convert an array of size 1 to a Python scalar")

    def __str__(self):
        return "[0 1 2]"


def test_a_numpy_style_count_is_recorded_as_a_number_not_as_a_string(tmp_path, monkeypatch):
    """THE DEFECT. `numpy.float64` subclasses `float` and survived; `numpy.int64` and
    `numpy.bool_` subclass NEITHER `int` NOR `bool`, so they fell through to the record's
    `default=str`.

    Measured: `run.note("n", df["v"].sum())` recorded the STRING `"6"`. A count recorded as
    text compares unequal to `6` in every downstream check, sorts as text, and renders
    quoted in the YAML view — and `df.nunique()`, `.sum()` and `(s > 1).any()` are the
    ordinary spellings, so this is the common case rather than an exotic one.

    Duck-typed on `.item()`, which is the rule the predecessor's own `json_safe()` used.
    """
    monkeypatch.chdir(tmp_path)
    runprov.configure(root=tmp_path, run_log=tmp_path / "runs.jsonl")
    with runprov.Run("s", provenance=tmp_path / "p.json") as run:
        run.note("count", _Scalar(6))
        run.note("flag", _Scalar(True))
        run.note("nested", {"a": _Scalar(7), "b": [_Scalar(8)]})

    got = json.loads((tmp_path / "p.json").read_text(encoding="utf-8"))["notes"]
    assert got["count"] == 6 and isinstance(got["count"], int)
    assert got["flag"] is True
    assert got["nested"] == {"a": 7, "b": [8]}


def test_a_real_bool_stays_a_bool_and_is_not_taken_for_a_scalar(tmp_path, monkeypatch):
    """`bool` is a subclass of `int`, and the ordering of the branches is what keeps a
    plain `True` from being reported as `1`."""
    monkeypatch.chdir(tmp_path)
    runprov.configure(root=tmp_path, run_log=tmp_path / "runs.jsonl")
    with runprov.Run("s", provenance=tmp_path / "p.json") as run:
        run.note("t", True)
        run.note("f", False)
        run.note("i", 1)
    got = json.loads((tmp_path / "p.json").read_text(encoding="utf-8"))["notes"]
    assert got["t"] is True and got["f"] is False and got["i"] == 1


def test_something_whose_item_raises_is_recorded_not_dropped(tmp_path, monkeypatch):
    """An array is not a scalar, and `.item()` on one raises. It must degrade to the
    string it degraded to before, never take the caller's run down from inside a note."""
    monkeypatch.chdir(tmp_path)
    runprov.configure(root=tmp_path, run_log=tmp_path / "runs.jsonl")
    with runprov.Run("s", provenance=tmp_path / "p.json") as run:
        run.note("arr", _NotAScalar())
    got = json.loads((tmp_path / "p.json").read_text(encoding="utf-8"))["notes"]
    assert got["arr"] == "[0 1 2]"


def test_to_yaml_writes_a_manifest_for_one_run_without_pyyaml(tmp_path, monkeypatch):
    """The predecessor wrote a per-run `*_manifest_*.yml` with `yaml.safe_dump`. That
    capability is kept, and kept dependency-free: the renderer is the same one behind
    `log --format yaml`, so the manifest and the history view cannot drift apart."""
    yaml = pytest.importorskip("yaml")
    monkeypatch.chdir(tmp_path)
    runprov.configure(root=tmp_path, run_log=tmp_path / "runs.jsonl")
    (tmp_path / "in.tsv").write_text("a\n", encoding="utf-8")
    with runprov.Run("validate", {"version": "v1"}, provenance=tmp_path / "p.json") as run:
        run.input("in.tsv")
        with run.open_output("out.tsv") as fh:
            fh.write("b\n")
        run.note("n_exact_matches", _Scalar(118))

    manifest = tmp_path / "manifest.yml"
    manifest.write_text(runprov.to_yaml(run.record), encoding="utf-8")

    doc = yaml.safe_load(manifest.read_text(encoding="utf-8"))[0]
    assert doc["step"] == "validate"
    assert doc["params"] == {"version": "v1"}
    assert doc["summary"] == {"n_exact_matches": 118}, "and the count is a NUMBER"
    assert doc["status"] == "ok"
    assert doc["input"] == "in.tsv" and doc["output"] == "out.tsv"
    assert any("in.tsv" in line for line in doc["input_sha256"])


def test_to_yaml_takes_one_record_or_many(tmp_path, monkeypatch):
    """One run is a manifest; a list is the history view. Same function, so a manifest can
    never disagree with `log --format yaml` about the same run."""
    yaml = pytest.importorskip("yaml")
    monkeypatch.chdir(tmp_path)
    runprov.configure(root=tmp_path, run_log=tmp_path / "runs.jsonl")
    with runprov.Run("a", provenance=tmp_path / "a.json") as one:
        pass
    with runprov.Run("b", provenance=tmp_path / "b.json") as two:
        pass
    assert len(yaml.safe_load(runprov.to_yaml(one.record))) == 1
    assert [d["step"] for d in yaml.safe_load(runprov.to_yaml([one.record, two.record]))] == [
        "a",
        "b",
    ]


def test_an_object_with_no_item_method_is_still_recorded(tmp_path, monkeypatch):
    """The duck-typing must not become a requirement. Anything without `.item()` keeps the
    behaviour it always had — recorded as its string, never dropped and never fatal."""
    monkeypatch.chdir(tmp_path)
    runprov.configure(root=tmp_path, run_log=tmp_path / "runs.jsonl")
    somewhere = tmp_path / "x.tsv"
    with runprov.Run("s", provenance=tmp_path / "p.json") as run:
        run.note("path", somewhere)
        run.note("set", {"b", "a"})
    got = json.loads((tmp_path / "p.json").read_text(encoding="utf-8"))["notes"]
    # `str(Path)`, never a hardcoded literal: a POSIX spelling makes this fail on Windows
    # for the separator rather than for the behaviour, which is what it did.
    assert got["path"] == str(somewhere)
    assert isinstance(got["set"], str)


def test_python_capture_reports_the_logging_handlers_it_cannot_see(tmp_path, monkeypatch):
    """THE SUBTLE HALF of the python-level fallback, and the one that costs a well-behaved
    script the most.

    `logging.StreamHandler(sys.stdout)` stores the stream OBJECT it was handed. Swapping
    `sys.stdout` cannot reach it, so a handler installed before the capture keeps writing to
    the original stream — while a bare `print()`, which resolves `sys.stdout` at call time,
    is captured. Measured in one block: the log held `PRINTED` and not `LOGGED`.

    A script that routes everything through `logging` — which is the well-behaved thing to
    do, and what the scripts this package replaces do — therefore gets a log that looks
    complete and holds almost nothing. Reported in the record, not repaired: rebinding
    another library's handlers from inside a provenance module is the overreach
    `_report.py` exists to prevent.
    """
    monkeypatch.chdir(tmp_path)
    runprov.configure(root=tmp_path, run_log=tmp_path / "runs.jsonl")
    monkeypatch.setattr(runprov.terminal, "os", _NoDup2())  # force the python fallback

    logger = logging.getLogger("runprov-test-prebound")
    logger.setLevel(logging.INFO)
    logger.handlers.clear()
    logger.addHandler(logging.StreamHandler(sys.stdout))
    try:
        with runprov.Run("s", provenance=tmp_path / "p.json", terminal_log=tmp_path / "t.log") as r:
            logger.info("LOGGED")
            print("PRINTED", flush=True)
    finally:
        logger.handlers.clear()

    rec = r.record["terminal_log"]
    assert rec["capture"] == "python"
    assert rec["prebound_stream_handlers"] >= 1
    assert "NOT included" in rec["note"]
    body = (tmp_path / "t.log").read_text(encoding="utf-8", errors="replace")
    assert "PRINTED" in body, "a bare print resolves sys.stdout at call time and IS seen"
    assert "LOGGED" not in body, "the handler holds the old object — which is the point"


def test_counting_the_handlers_never_takes_the_capture_down(tmp_path, monkeypatch):
    """It is diagnostic. A logging configuration that cannot be walked — a handler list
    replaced by a framework, a proxy that raises on attribute access — must degrade to "I
    do not know" rather than fail a capture that is already falling back.

    Broken LOCALLY, on one real logger. An earlier draft replaced `logging.Logger.manager`
    with a raising fake and killed the whole pytest session: the logging plugin walks that
    same manager at session finish. Same shape as the `monkeypatch.setattr(os, "dup2")`
    incident this suite already carries a fake for — patching shared machinery to test one
    function reaches every other user of it.
    """
    broken = logging.getLogger("runprov-test-unwalkable")
    monkeypatch.setattr(broken, "handlers", 0)  # not iterable; `list()` raises TypeError
    assert runprov.terminal._prebound_handlers(sys.stdout, sys.stderr) == 0


def test_a_numpy_style_NaN_is_still_named_after_unwrapping(tmp_path, monkeypatch):
    """The two rules have to compose, and the order matters.

    `numpy.float64("nan").item()` is a Python NaN, which `json.dumps` writes as a bare
    `NaN` — not JSON, and rejected by every strict parser, which is the defect `_jsonable`
    was written for. Unwrapping the scalar without re-applying that rule would reintroduce
    it through the new path. An AUROC on a class with no positives IS this value, and it
    arrives from numpy.
    """
    monkeypatch.chdir(tmp_path)
    runprov.configure(root=tmp_path, run_log=tmp_path / "runs.jsonl")
    with runprov.Run("s", provenance=tmp_path / "p.json") as run:
        run.note("auroc", _Scalar(float("nan")))
        run.note("ratio", _Scalar(float("inf")))

    body = (tmp_path / "p.json").read_text(encoding="utf-8")
    assert ": NaN" not in body, "bare NaN is not JSON"
    got = json.loads(body)["notes"]
    assert got["auroc"] == "NaN" and got["ratio"] == "Infinity"


def test_to_yaml_finds_the_script_file_in_a_sidecar_record_too(tmp_path, monkeypatch):
    """The history line flattens `script_file` to the top level; the sidecar keeps it under
    `code`. Reading only the flat one made `to_yaml(run.record)` report a run from a real
    `.py` file as having none, while `log --format yaml` — the same renderer, the same run,
    read from the history — printed the path. Two views of one run must not disagree."""
    yaml = pytest.importorskip("yaml")
    monkeypatch.chdir(tmp_path)
    runprov.configure(root=tmp_path, run_log=tmp_path / "runs.jsonl")
    script = tmp_path / "step.py"
    script.write_text("x = 1\n", encoding="utf-8")

    with runprov.Run("s", provenance=tmp_path / "p.json", script_path=script) as run:
        pass

    from_record = yaml.safe_load(runprov.to_yaml(run.record))[0]
    history = [json.loads(x) for x in (tmp_path / "runs.jsonl").read_text().splitlines()]
    from_history = yaml.safe_load(cli._yaml(history))[0]
    assert from_record["script"] == str(script)
    assert from_record["script"] == from_history["script"], "the two views must agree"


def test_the_distribution_declares_a_console_script():
    """`python -m runprov` is one entry point; the console script is another, and only the
    second is reachable by `uvx runprov` or `pipx run runprov`.

    Asserted against pyproject rather than against an installed environment, so the suite
    stays runnable from a source checkout — `ci.py build` checks the built artifact, which
    is the half that catches a wheel where the entry point did not survive packaging.
    """
    body = (REPO / "pyproject.toml").read_text(encoding="utf-8")
    assert "[project.scripts]" in body
    assert 'runprov = "runprov.__main__:main"' in body


def test_main_can_be_called_with_no_argv_the_way_a_console_script_calls_it():
    """A console script calls `main()` with no arguments. If the signature required argv,
    every `runprov ...` invocation would die with a TypeError while `python -m runprov`
    kept working — the entry point is generated at install time and never type-checked."""
    assert inspect.signature(cli.main).parameters["argv"].default is None
    with pytest.raises(SystemExit):
        cli.main([])  # argparse exits 2 on a missing subcommand; it must not TypeError


# ------------------------------------------------- REBUILDING THE ENVIRONMENT LATER
# A package list describes an environment; it does not say how to rebuild one. `uv sync`,
# `mamba env create` and `poetry install` are different commands over different files, so
# the record has to name which one applies and pin the file it applies to.


def _prefix_with(tmp_path, cfg=None, conda=False):
    root = tmp_path / "prefix"
    root.mkdir(parents=True, exist_ok=True)
    if cfg is not None:
        (root / "pyvenv.cfg").write_text(cfg, encoding="utf-8")
    if conda:
        (root / "conda-meta").mkdir(exist_ok=True)
    return root


def test_a_uv_created_venv_says_so_and_says_which_uv(tmp_path):
    """uv stamps its own version into `pyvenv.cfg`, which is the most reliable marker
    available — no subprocess, no PATH lookup, and it survives the environment being
    copied. `virtualenv` does the same; a plain `venv` writes neither, and that absence is
    how a plain venv is recognised."""
    cfg = "home = /usr/bin\nversion = 3.12.13\nuv = 0.11.8\n"
    got = runprov.environment.manager(_prefix_with(tmp_path, cfg=cfg), env={})
    assert got["detected"] == ["uv", "venv"]
    assert got["evidence"]["pyvenv.cfg:uv"] == "0.11.8"

    plain = runprov.environment.manager(
        _prefix_with(tmp_path / "b", cfg="home = /usr/bin\n"), env={}
    )
    assert plain["detected"] == ["venv"], "a plain venv stamps no tool version"


def test_overlapping_layouts_are_reported_as_several_not_resolved_to_one(tmp_path):
    """A uv-created venv inside a conda prefix is an ordinary thing in this field, and a
    single answer would have to be wrong about one of them."""
    got = runprov.environment.manager(
        _prefix_with(tmp_path, cfg="uv = 0.11.8\n", conda=True),
        env={"CONDA_PREFIX": "/opt/mamba/envs/hcv", "MAMBA_EXE": "/opt/bin/mamba"},
    )
    assert got["detected"] == ["conda", "conda-family", "mamba", "uv", "venv"]


def test_an_environment_NAME_is_recorded_but_never_a_path(tmp_path):
    """The name is what a human asks for six months later. The paths beside it carry a
    username and a machine layout, and this dict goes into a record that gets committed
    and shared — so only the fact that they were set is kept."""
    got = runprov.environment.manager(
        _prefix_with(tmp_path),
        env={
            "CONDA_DEFAULT_ENV": "hcv-genotyping",
            "CONDA_PREFIX": "/home/someone/mambaforge/envs/hcv-genotyping",
            "VIRTUAL_ENV": "/home/someone/proj/.venv",
        },
    )
    assert got["evidence"]["CONDA_DEFAULT_ENV"] == "hcv-genotyping"
    assert got["evidence"]["CONDA_PREFIX"] == "set"
    assert got["evidence"]["VIRTUAL_ENV"] == "set"
    assert "someone" not in json.dumps(got), "no path may reach the record"


def test_an_unreadable_pyvenv_cfg_still_proves_a_venv(tmp_path, monkeypatch):
    """It is evidence, not a parser. A prefix with an unreadable `pyvenv.cfg` is still a
    venv layout, and reporting nothing would be a worse answer than reporting less."""
    root = _prefix_with(tmp_path, cfg="uv = 1.0\n")

    def _boom(*a, **k):
        raise OSError("permission denied")

    monkeypatch.setattr(pathlib.Path, "read_text", _boom)
    got = runprov.environment.manager(root, env={})
    assert got["detected"] == ["venv"]
    assert got["evidence"]["pyvenv.cfg"] == "present but unreadable"


def test_the_lock_file_is_hashed_so_the_record_names_a_fixed_thing(tmp_path):
    """`uv.lock` changes every time a dependency does. A record naming it without pinning
    its content names a moving target — the same defect as a pin that lists a path and not
    a digest."""
    (tmp_path / "uv.lock").write_text("version = 1\n", encoding="utf-8")
    (tmp_path / "requirements.txt").write_text("pandas>=3\n", encoding="utf-8")
    got = runprov.environment.lockfiles(tmp_path)
    assert [d["name"] for d in got] == ["uv.lock", "requirements.txt"]
    assert got[0]["sha256"] == runprov.sha256(tmp_path / "uv.lock")
    assert runprov.environment.lockfiles(tmp_path / "empty") == []


def test_lock_files_are_archived_content_addressed_and_reused(tmp_path):
    """Hashing says which lock it was; ARCHIVING means the run is still rebuildable after
    that file has moved on. Content-addressed for the same reason the package snapshot is:
    an unchanged lock collapses to one copy however many runs reference it."""
    (tmp_path / "uv.lock").write_text("version = 1\n", encoding="utf-8")
    envs = tmp_path / "envs"

    first = runprov.environment.archive_lockfiles(tmp_path, envs)
    assert first[0]["reused"] is False
    archived = pathlib.Path(first[0]["path"])
    assert archived.read_text(encoding="utf-8") == "version = 1\n"
    assert archived.name.startswith("lock-") and archived.name.endswith("uv.lock")

    again = runprov.environment.archive_lockfiles(tmp_path, envs)
    assert again[0]["reused"] is True, "an unchanged lock must not be copied twice"

    (tmp_path / "uv.lock").write_text("version = 2\n", encoding="utf-8")
    moved = runprov.environment.archive_lockfiles(tmp_path, envs)
    assert moved[0]["reused"] is False and moved[0]["path"] != first[0]["path"]


def test_a_lock_that_cannot_be_archived_is_recorded_not_fatal(tmp_path, monkeypatch):
    """Provenance must never become the reason the work did not happen."""
    (tmp_path / "uv.lock").write_text("version = 1\n", encoding="utf-8")

    def _boom(*a, **k):
        raise OSError("read-only file system")

    monkeypatch.setattr(pathlib.Path, "write_bytes", _boom)
    got = runprov.environment.archive_lockfiles(tmp_path, tmp_path / "envs")
    assert "read-only file system" in got[0]["error"]
    assert got[0]["sha256"], "the digest is still recorded — only the copy failed"


def test_a_run_records_how_its_environment_could_be_rebuilt(tmp_path, monkeypatch):
    """End to end, through `Run`, so the wiring is tested and not just the helpers."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "uv.lock").write_text("version = 1\n", encoding="utf-8")
    runprov.configure(
        root=tmp_path, run_log=tmp_path / "runs.jsonl", env_snapshot_dir=tmp_path / "envs"
    )
    with runprov.Run("step", provenance=tmp_path / "p.json") as run:
        pass

    env = run.record["environment"]
    # The SHAPE, not a verdict. An empty `detected` is a real answer -- a bare system
    # Python built by no tool at all, which is exactly what CI runs -- and asserting that
    # something was found here made the test a statement about the runner rather than
    # about the code. Detection itself is tested against constructed prefixes above.
    assert isinstance(env["manager"]["detected"], list)
    assert isinstance(env["manager"]["evidence"], dict)
    assert [d["name"] for d in env["lockfiles"]] == ["uv.lock"]
    assert env["snapshot"]["lockfiles"][0]["reused"] is False
    assert pathlib.Path(env["snapshot"]["lockfiles"][0]["path"]).is_file()


def _git_repo(root):
    """A real repository, because the question is what git's object database contains and
    a fake would only test the fake. No commit is needed: `git add` stores the blob, which
    is exactly the condition being detected."""
    root.mkdir(parents=True, exist_ok=True)
    if subprocess.run(["git", "init", "-q", str(root)], capture_output=True).returncode != 0:
        pytest.skip("git is not available")
    return root


def test_a_lock_git_already_stores_is_not_copied_again(tmp_path):
    """THE COST THIS AVOIDS. Measured on the project this came from: `uv.lock` is 1.1 MB
    and the snapshot directory is tracked, so archiving unconditionally committed a second
    copy of a file git already versions — once per lock change, into a repository that is a
    publication artifact.

    The digest still pins WHICH lock. Git is the archive, and the recorded blob id makes it
    one command to get the bytes back.
    """
    root = _git_repo(tmp_path / "proj")
    lock = root / "uv.lock"
    lock.write_text("version = 1\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(root), "add", "uv.lock"], check=True, capture_output=True)

    got = runprov.environment.archive_lockfiles(root, root / "envs")
    assert got[0]["archived"] is False
    assert got[0]["note"] == "git already stores it"
    assert len(got[0]["git_blob"]) == 40
    assert got[0]["sha256"], "the digest is still recorded — only the copy is skipped"
    assert not (root / "envs").exists(), "nothing was written"


def test_a_lock_with_uncommitted_edits_is_still_archived(tmp_path):
    """Being tracked is the wrong question; having these BYTES is the right one. A
    tracked lock with uncommitted edits is not stored yet, and that is precisely when the
    copy is worth making — the run used those bytes and nothing else has them."""
    root = _git_repo(tmp_path / "proj")
    lock = root / "uv.lock"
    lock.write_text("version = 1\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(root), "add", "uv.lock"], check=True, capture_output=True)
    lock.write_text("version = 2\n", encoding="utf-8")  # edited, not added

    got = runprov.environment.archive_lockfiles(root, root / "envs")
    assert got[0]["archived"] is True
    assert pathlib.Path(got[0]["path"]).read_text(encoding="utf-8") == "version = 2\n"


def test_outside_a_repository_the_lock_is_archived(tmp_path):
    """No git, no archive — so runprov archives. `hash-object` works outside a repository
    and `cat-file` cannot, which is the fallback that makes the check safe to run
    anywhere."""
    (tmp_path / "uv.lock").write_text("version = 1\n", encoding="utf-8")
    got = runprov.environment.archive_lockfiles(tmp_path, tmp_path / "envs")
    assert got[0]["archived"] is True
    assert runprov.environment._already_in_git(tmp_path, tmp_path / "uv.lock") is None
    # And when `hash-object` itself cannot answer — no such file, or no git at all — the
    # answer is "not stored", which archives. Never "stored", which would drop the copy.
    assert runprov.environment._already_in_git(tmp_path, tmp_path / "absent.lock") is None


# ------------------------------------------------------- INPUT SHAPES, THE REMAINDER
# A path near PATH_MAX (~3,900 chars), a directory containing a symlink LOOP, one file
# reached by three spellings, and NFC-vs-NFD filenames were all probed and all already
# correct. The two below were not.


def test_an_input_replaced_after_registration_is_flagged(tmp_path, monkeypatch):
    """THE UNCLOSED HALF OF R10. `unstable_during_hash` catches a file rewritten WHILE it
    is being read. Nothing caught one rewritten a second later — so a run pinned
    `sha256: abc…` and finished beside a file that no longer held those bytes, with the
    record asserting in good faith something no longer true of anything on disk.

    A checker comparing the pin to the file then reports a difference that is real and is
    NOT the run's fault, which is the kind of red check that teaches people to ignore
    checks. The record now says which side moved.
    """
    monkeypatch.chdir(tmp_path)
    runprov.configure(root=tmp_path, run_log=tmp_path / "runs.jsonl")
    src = tmp_path / "in.tsv"
    src.write_text("original\n", encoding="utf-8")

    with runprov.Run("s", provenance=tmp_path / "p.json") as run:
        run.input(src)
        recorded = run.record["inputs"][0]["sha256"]
        src.write_text("replaced, and a different length\n", encoding="utf-8")

    entry = run.record["inputs"][0]
    assert entry["changed_after_registration"] == "size"
    assert entry["sha256"] == recorded, "the digest must stay what the run actually READ"
    assert entry["sha256"] != runprov.sha256(src)


def test_an_input_deleted_after_registration_is_flagged_as_gone(tmp_path, monkeypatch):
    """Gone is not the same as changed, and a reader needs to tell them apart: one means
    the bytes moved on, the other that there is nothing left to compare against."""
    monkeypatch.chdir(tmp_path)
    runprov.configure(root=tmp_path, run_log=tmp_path / "runs.jsonl")
    src = tmp_path / "in.tsv"
    src.write_text("original\n", encoding="utf-8")
    with runprov.Run("s", provenance=tmp_path / "p.json") as run:
        run.input(src)
        src.unlink()
    assert run.record["inputs"][0]["changed_after_registration"] == "gone"


def test_an_unchanged_input_is_not_flagged(tmp_path, monkeypatch):
    """The guard that keeps the flag meaningful. A check that fires on every run says
    nothing, and this one runs a `stat` per input on every run."""
    monkeypatch.chdir(tmp_path)
    runprov.configure(root=tmp_path, run_log=tmp_path / "runs.jsonl")
    (tmp_path / "in.tsv").write_text("steady\n", encoding="utf-8")
    (tmp_path / "tree").mkdir()
    (tmp_path / "tree" / "a.txt").write_text("a\n", encoding="utf-8")
    with runprov.Run("s", provenance=tmp_path / "p.json") as run:
        run.input(tmp_path / "in.tsv")
        run.input(tmp_path / "tree")
    assert all("changed_after_registration" not in e for e in run.record["inputs"])


def test_moved_since_reports_a_time_only_change(tmp_path):
    """Size is the cheap signal; mtime is the one that catches a rewrite of the SAME
    length, which is what a corrected value in a fixed-width table looks like."""
    p = tmp_path / "x.tsv"
    p.write_text("aaaa\n", encoding="utf-8")
    rec = runprov.describe(p)
    p.write_text("bbbb\n", encoding="utf-8")  # identical length
    os.utime(p, (0, 0))  # and an unmistakably different mtime
    assert runprov.hashing.moved_since(rec) == "mtime"


def test_a_relative_input_is_rechecked_against_the_recorded_cwd_not_the_current_one(
    tmp_path, monkeypatch, capsys
):
    """A `chdir` between registering an input and finishing the run is ordinary, and it used
    to point this check at whatever sat at the same relative spelling in the new directory.

    Both directions are asserted because both were wrong and they fail oppositely. The
    output loop in `write()` had always anchored to `record["cwd"]`; the input loop ten
    lines above it had not, so the two disagreed inside one method.
    """
    work, other = tmp_path / "work", tmp_path / "other"
    work.mkdir(), other.mkdir()
    (work / "ref.fa").write_text(">a\nACGT\n", encoding="utf-8")
    proj = _project(tmp_path)

    # FALSE POSITIVE: a file nobody touched, reported "gone" forever in the record.
    monkeypatch.chdir(work)
    run = runprov.Run("stage", project=proj)
    run.input("ref.fa")
    monkeypatch.chdir(other)  # the same relative name does not exist here
    rec = json.loads(run.write(tmp_path / "p1.json").read_text(encoding="utf-8"))
    assert "changed_after_registration" not in rec["inputs"][0], (
        "the input never moved; the check moved"
    )
    assert "CHANGED" not in capsys.readouterr().err

    # FALSE NEGATIVE, and the worse one: the input genuinely IS rewritten, the stat lands on
    # a path that does not exist, FileNotFoundError reads as "gone"... which was then ALSO
    # wrong in the other direction. Anchored, the real rewrite is what gets reported.
    monkeypatch.chdir(work)
    run2 = runprov.Run("stage", project=proj)
    run2.input("ref.fa")
    (work / "ref.fa").write_text(">a\nACGTACGTACGT\n", encoding="utf-8")
    monkeypatch.chdir(other)
    rec2 = json.loads(run2.write(tmp_path / "p2.json").read_text(encoding="utf-8"))
    assert rec2["inputs"][0]["changed_after_registration"] == "size"
    assert "CHANGED" in capsys.readouterr().err


def test_moved_since_does_not_claim_a_change_it_cannot_see(tmp_path):
    """It answers from a `stat`, so it must not overstate. A file that became unreadable
    is a fact, but it is not evidence the CONTENT moved — and a directory entry carries no
    size to compare."""
    p = tmp_path / "x.tsv"
    p.write_text("a\n", encoding="utf-8")
    rec = runprov.describe(p)
    assert runprov.hashing.moved_since(rec) is None

    d = tmp_path / "tree"
    d.mkdir()
    (d / "a.txt").write_text("a\n", encoding="utf-8")
    assert runprov.hashing.moved_since(runprov.describe(d)) is None, "a tree has no size"
    assert runprov.hashing.moved_since({}) is None, "and an entry with no path is not a claim"


@requires_unreadable_files
def test_an_input_that_exists_but_cannot_be_read_names_the_script(tmp_path, monkeypatch):
    """`exists()` is TRUE for a file with no read permission — `stat` works, `open` does
    not — so the missing-input check passes and the failure surfaces from inside `sha256`
    as a bare PermissionError naming neither the script nor the fact that provenance
    raised it. The same treatment the missing-input case already had."""
    monkeypatch.chdir(tmp_path)
    runprov.configure(root=tmp_path, run_log=tmp_path / "runs.jsonl")
    locked = tmp_path / "locked.tsv"
    locked.write_text("x\n", encoding="utf-8")
    os.chmod(locked, 0o000)
    if os.access(locked, os.R_OK):  # root, or a filesystem without permission bits
        pytest.skip("this user can read a mode-000 file, so the hazard cannot arise here")
    try:
        with pytest.raises(OSError) as caught:
            with runprov.Run("the_script", provenance=tmp_path / "p.json") as run:
                run.input(locked)
    finally:
        os.chmod(locked, 0o644)
    assert "the_script" in str(caught.value)
    assert "could not be read" in str(caught.value)


@requires_unreadable_files
def test_moved_since_is_silent_when_the_stat_itself_fails(tmp_path):
    """A `stat` can fail for reasons other than the file being gone — here, a parent
    directory that can no longer be entered. Reporting "changed" from that would be
    inventing evidence: nothing was observed about the content at all."""
    box = tmp_path / "box"
    box.mkdir()
    p = box / "x.tsv"
    p.write_text("a\n", encoding="utf-8")
    rec = runprov.describe(p)
    # 0o700, not 0o755: the restore only has to give this test's own user the directory
    # back, and a wider mask is what the linter is right to object to.
    os.chmod(box, 0o000)
    if os.access(p, os.R_OK):  # root, or a filesystem without permission bits
        os.chmod(box, 0o700)
        pytest.skip("this user can traverse a mode-000 directory")
    try:
        assert runprov.hashing.moved_since(rec) is None
    finally:
        os.chmod(box, 0o700)


# ------------------------------------------- INPUT SHAPES UNDER THE CALLING SHAPES
# The cross product. One file hashed by eight concurrent Runs gives one digest, and a
# subprocess that rewrites a registered input is caught by `changed_after_registration` —
# both already correct. The two below were silent.


def test_a_subprocess_that_rewrites_a_registered_input_is_caught(tmp_path, monkeypatch):
    """The post-registration check under a real calling shape rather than a constructed
    one. A stage that shells out to a tool which rewrites its own input in place is an
    ordinary pipeline, and the digest recorded is the one that was read."""
    monkeypatch.chdir(tmp_path)
    runprov.configure(root=tmp_path, run_log=tmp_path / "runs.jsonl")
    victim = tmp_path / "victim.tsv"
    victim.write_text("before\n", encoding="utf-8")

    with runprov.Run("s", provenance=tmp_path / "p.json") as run:
        run.input(victim)
        subprocess.run(
            [
                sys.executable,
                "-c",
                f"import pathlib;pathlib.Path({str(victim)!r}).write_text('AFTER, longer\\n')",
            ],
            check=True,
        )
    assert run.record["inputs"][0]["changed_after_registration"] == "size"


def test_one_input_hashed_by_many_threads_gives_one_digest(tmp_path, monkeypatch):
    """Hashing is a read, and eight concurrent readers of one file must not disagree —
    a digest that depended on who else was reading would be worthless."""
    monkeypatch.chdir(tmp_path)
    runprov.configure(root=tmp_path, run_log=tmp_path / "runs.jsonl")
    shared = tmp_path / "shared.tsv"
    shared.write_text("x" * 100_000 + "\n", encoding="utf-8")
    seen: list[str] = []

    def work(i):
        with runprov.Run(f"t{i}", provenance=tmp_path / f"t{i}.json") as run:
            run.input(shared)
            seen.append(run.record["inputs"][0]["sha256"])

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as ex:
        list(ex.map(work, range(8)))
    assert len(seen) == 8 and len(set(seen)) == 1


def test_a_cwd_moving_under_a_run_is_announced_once(tmp_path, monkeypatch, capsys):
    """`_anchor` already records the ABSOLUTE path when the cwd has moved, so the record
    names the file actually read rather than the one the script meant. What it did not do
    is say so — and two very different things reach this branch:

    * a script that deliberately `chdir`s, where absolute paths are merely surprising —
      they are machine-specific, so records from two machines stop comparing equal;
    * ANOTHER THREAD moving the process-global cwd, where the file registered may not be
      the one intended at all, and nothing else would ever hint at it.

    Once per run, not once per path: a script that chdirs and then registers thirty files
    has made one decision, not thirty.
    """
    monkeypatch.chdir(tmp_path)
    (tmp_path / "here").mkdir()
    (tmp_path / "there").mkdir()
    for where in ("here", "there"):
        (tmp_path / where / "a.tsv").write_text(f"{where}\n", encoding="utf-8")
        (tmp_path / where / "b.tsv").write_text(f"{where}\n", encoding="utf-8")
    runprov.configure(root=tmp_path, run_log=tmp_path / "runs.jsonl")

    os.chdir(tmp_path / "here")
    try:
        with runprov.Run("s", provenance=tmp_path / "p.json") as run:
            os.chdir(tmp_path / "there")
            run.input("a.tsv")
            run.input("b.tsv")
    finally:
        os.chdir(tmp_path)

    err = capsys.readouterr().err
    assert err.count("the working directory has moved") == 1, "once per run, not per path"
    assert "ANOTHER THREAD" in err, "the race is the reading that costs the most"
    for entry in run.record["inputs"]:
        assert pathlib.Path(entry["path"]).is_absolute()
        assert pathlib.Path(entry["path"]).read_text(encoding="utf-8") == "there\n"


def test_two_runs_writing_one_sidecar_say_so(tmp_path, monkeypatch, capsys):
    """A sidecar is ONE run's record. Two runs sharing a path leaves the file describing
    whichever finished last, while the artifact beside it came from the other — and the
    only clue is an unfamiliar `run_id`, which nobody checks. The history keeps both, so
    nothing is lost; what was missing is being told."""
    monkeypatch.chdir(tmp_path)
    runprov.configure(root=tmp_path, run_log=tmp_path / "runs.jsonl")
    shared = tmp_path / "shared.json"

    with runprov.Run("first", provenance=shared):
        pass
    capsys.readouterr()
    with runprov.Run("second", provenance=shared) as second:
        pass

    err = capsys.readouterr().err
    assert "already written by ANOTHER RUN" in err
    assert json.loads(shared.read_text(encoding="utf-8"))["run_uid"] == second.record["run_uid"]
    lines = [json.loads(x) for x in (tmp_path / "runs.jsonl").read_text().splitlines()]
    assert {r["script"] for r in lines} == {"first", "second"}, "the history keeps both"


def test_one_run_rewriting_its_own_sidecar_is_not_a_collision(tmp_path, monkeypatch, capsys):
    """The guard that keeps the warning meaningful. `write()` inside a `with` block, then
    `__exit__` correcting the same file, is the documented and correct spelling — warning
    on it would fire on ordinary use and be tuned out within a day."""
    monkeypatch.chdir(tmp_path)
    runprov.configure(root=tmp_path, run_log=tmp_path / "runs.jsonl")
    with runprov.Run("s", provenance=tmp_path / "p.json") as run:
        run.write(tmp_path / "p.json")
        run.write(tmp_path / "p.json")  # twice, explicitly: still ONE run
    assert "already written by ANOTHER RUN" not in capsys.readouterr().err


def test_two_spellings_of_one_sidecar_path_still_collide(tmp_path, monkeypatch, capsys):
    """Keyed by the FILE, not by how it was spelled. `p.json` and `./sub/../p.json` are one
    file, and a collision that a caller can hide by writing the path differently is a check
    that reports on punctuation."""
    monkeypatch.chdir(tmp_path)
    runprov.configure(root=tmp_path, run_log=tmp_path / "runs.jsonl")
    (tmp_path / "sub").mkdir()

    with runprov.Run("first", provenance=tmp_path / "p.json"):
        pass
    capsys.readouterr()
    with runprov.Run("second", provenance=tmp_path / "sub" / ".." / "p.json"):
        pass
    assert "already written by ANOTHER RUN" in capsys.readouterr().err


# ------------------------------------------------------ CALLING SHAPES, THE REMAINDER
# `SystemExit(0)` records ok, `SystemExit(1)` and KeyboardInterrupt record failed, a Run
# nested inside another records both, and entering one Run twice appends one history line
# — all probed and all already correct. The abandoned Run was not.


class _Interrupted(BaseException):
    """A BaseException that is not an Exception — the family KeyboardInterrupt belongs to.

    Used INSTEAD of raising a real `KeyboardInterrupt` in-process. Python re-raises SIGINT
    at interpreter exit when a KeyboardInterrupt was involved, so a test that raises one
    kills the test runner's whole process group: measured, the suite reported 286 passed
    and the process still died by signal 2, taking the shell that invoked it with it.

    `__exit__` classifies on the exit CODE of a SystemExit and on nothing else, so this
    class reaches the identical branch. The real type is exercised in a subprocess below,
    where its exit convention is contained.
    """


@pytest.mark.parametrize(
    ("raiser", "status", "kind"),
    [
        (SystemExit, "ok", None),  # SystemExit() — code None
        (lambda: SystemExit(0), "ok", None),
        (lambda: SystemExit(1), "failed", "SystemExit"),
        (_Interrupted, "failed", "_Interrupted"),
    ],
)
def test_the_baseexception_family_is_classified(tmp_path, monkeypatch, raiser, status, kind):
    """`SystemExit` and `KeyboardInterrupt` derive from BaseException, not Exception, and
    `raise SystemExit(main())` is how every script in the consuming project ends. Treating
    them as clean would record an interrupted run as successful; treating them all as
    failure would poison `grep '"status": "failed"'`, which is the query the whole deferral
    was built to make trustworthy. The exit CODE is what separates them."""
    monkeypatch.chdir(tmp_path)
    runprov.configure(root=tmp_path, run_log=tmp_path / "runs.jsonl")
    with contextlib.suppress(BaseException), runprov.Run("s", provenance=tmp_path / "p.json"):
        raise raiser()
    rec = json.loads((tmp_path / "p.json").read_text(encoding="utf-8"))
    assert rec["status"] == status
    assert (rec.get("failure") or {}).get("type") == kind


def test_a_real_keyboard_interrupt_is_recorded_as_failed(tmp_path):
    """The real type, in a SUBPROCESS — because that is the only place its exit convention
    can be observed without imposing it on the test runner. Ctrl-C during a long stage is
    the most ordinary way a run ends early, and recording it as `ok` would be the worst
    possible lie this package could tell."""
    script = tmp_path / "step.py"
    script.write_text(
        "import pathlib, sys\n"
        f"sys.path.insert(0, {str(REPO)!r})\n"
        "import runprov\n"
        f"T = pathlib.Path({str(tmp_path)!r})\n"
        "runprov.configure(root=T, run_log=T / 'runs.jsonl')\n"
        "with runprov.Run('interrupted', provenance=T / 'p.json'):\n"
        "    raise KeyboardInterrupt\n",
        encoding="utf-8",
    )
    done = subprocess.run([sys.executable, str(script)], capture_output=True, text=True)
    assert done.returncode != 0
    rec = json.loads((tmp_path / "p.json").read_text(encoding="utf-8"))
    assert rec["status"] == "failed"
    assert rec["failure"]["type"] == "KeyboardInterrupt"


def test_a_run_that_is_never_entered_does_not_keep_capturing(tmp_path, monkeypatch):
    """Capture starts in `__init__` — deliberately, so a caller using `write()` without a
    `with` block is still recorded. The cost is that a Run built and abandoned had fds 1
    and 2 dup2'd to its pipe for the life of the process, with every line the program
    printed afterwards accumulating in ITS log.

    Nothing looked wrong: the tee holds, so output still reached the terminal. The log just
    ended up describing a run that never happened, plus everything that came after it.
    """
    monkeypatch.chdir(tmp_path)
    runprov.configure(root=tmp_path, run_log=tmp_path / "runs.jsonl")
    log = tmp_path / "ab.log"
    live_before = len(runprov.terminal._LIVE)

    def build_and_drop():
        runprov.Run("abandoned", provenance=tmp_path / "ab.json", terminal_log=log)

    build_and_drop()
    gc.collect()

    assert len(runprov.terminal._LIVE) == live_before, "the capture must not stay live"
    settled = log.stat().st_size
    print("AFTER-THE-ABANDONED-RUN", flush=True)
    assert log.stat().st_size == settled, "and its log must stop growing"
    assert not (tmp_path / "ab.json").exists(), "an abandoned run records nothing, as before"


def test_releasing_an_abandoned_capture_twice_is_harmless(tmp_path):
    """The finalizer also fires for a run that WAS closed properly, and at interpreter
    shutdown. Stopping an already-stopped capture must restore nothing and close nothing
    twice — and it must not raise, because a finalizer that raises prints an
    unhandled-exception notice from deep inside the interpreter and helps nobody."""
    cap = runprov.Capture(tmp_path / "t.log")
    cap.start()
    cap.stop()
    runprov.run._release_abandoned(cap)
    runprov.run._release_abandoned(cap)


def test_a_run_nested_inside_another_records_both(tmp_path, monkeypatch):
    """A harness that wraps a step in its own Run is the obvious thing to write, and both
    passes are real runs with their own inputs and outputs."""
    monkeypatch.chdir(tmp_path)
    runprov.configure(root=tmp_path, run_log=tmp_path / "runs.jsonl")
    with runprov.Run("outer", provenance=tmp_path / "o.json"):
        with runprov.Run("inner", provenance=tmp_path / "i.json"):
            pass
    got = [json.loads(x)["script"] for x in (tmp_path / "runs.jsonl").read_text().splitlines()]
    assert sorted(got) == ["inner", "outer"]


def test_entering_one_run_twice_appends_one_history_line(tmp_path, monkeypatch):
    """The history counts RUNS. A second `with` on the same object is the same run, and
    appending twice would inflate every tally taken over runs.jsonl."""
    monkeypatch.chdir(tmp_path)
    runprov.configure(root=tmp_path, run_log=tmp_path / "runs.jsonl")
    run = runprov.Run("twice", provenance=tmp_path / "p.json")
    with run:
        pass
    # Re-entry is refused now rather than tolerated-and-under-recorded, so the property this
    # test was written for — one history line per Run — holds by construction. Asserting the
    # refusal AND the count keeps both halves of the original intent.
    with pytest.raises(RuntimeError, match="already been used"):
        with run:
            pass  # pragma: no cover - refused
    got = [json.loads(x) for x in (tmp_path / "runs.jsonl").read_text().splitlines()]
    assert sum(1 for r in got if r["script"] == "twice") == 1


# ============================== `verify`: the half that reads the pin back and checks it
def _chain(tmp_path, monkeypatch):
    """A two-step chain, so the inherited pin is real rather than hand-written."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "data").mkdir()
    (tmp_path / "results").mkdir()
    src = tmp_path / "data" / "in.tsv"
    src.write_text("id\tv\n1\ta\n", encoding="utf-8")
    proj = _project(tmp_path)
    for name, a, b in (
        ("step1", src, tmp_path / "results" / "mid.tsv"),
        ("step2", tmp_path / "results" / "mid.tsv", tmp_path / "results" / "final.tsv"),
    ):
        with runprov.Run(name, project=proj, provenance=b.with_suffix(".prov.json")) as run:
            text = pathlib.Path(run.input(a)).read_text(encoding="utf-8")
            with run.open_output(b) as fh:
                fh.write(text)
    return src, tmp_path / "results"


def test_verify_passes_a_chain_nobody_has_touched(tmp_path, monkeypatch):
    _, results = _chain(tmp_path, monkeypatch)
    rep = runprov.verify.verify([results], tmp_path)
    assert rep["ok"] == 2 and rep["stale"] == 0 and rep["gone"] == 0
    assert rep["artifacts_pinned"] == 2, "the .prov.json sidecars carry no pin and must not count"


def test_verify_reports_staleness_transitively_through_an_inherited_pin(tmp_path, monkeypatch):
    """The property the pin-in-artifact design buys, and the reason every block is read.

    `final.tsv` copies `mid.tsv`'s lines through, so it carries step1's pin as well as its
    own. Changing the ROOT input must therefore surface on the grandchild too — reading
    only the first block would check one generation and silently ignore a claim the
    artifact is making in its own bytes.
    """
    src, results = _chain(tmp_path, monkeypatch)
    src.write_text("id\tv\n1\tCHANGED\n", encoding="utf-8")

    rep = runprov.verify.verify([results], tmp_path)
    assert rep["stale"] == 2, "both the child and the grandchild pin the changed root"

    final = next(a for a in rep["artifacts"] if a["artifact"].endswith("final.tsv"))
    assert final["pins"] == 2 and final["scripts"] == ["step2", "step1"]
    bad = [i for i in final["inputs"] if i["status"] != "OK"]
    assert [(i["name"], i["via"]) for i in bad] == [("data/in.tsv", "step1")], (
        "the failing claim must name the step that made it, not the artifact's own step"
    )


def test_verify_separates_a_missing_input_from_a_changed_one(tmp_path, monkeypatch):
    """Different repairs: a stale artifact is rebuilt, a gone input is FOUND. Collapsing
    them printed `1 STALE` over an artifact whose own line said GONE."""
    _, results = _chain(tmp_path, monkeypatch)
    (results / "mid.tsv").unlink()
    rep = runprov.verify.verify([results / "final.tsv"], tmp_path)
    assert rep["gone"] == 1 and rep["stale"] == 0
    assert [i["status"] for i in rep["artifacts"][0]["inputs"]] == ["GONE", "OK"]


def test_verify_refuses_to_guess_rather_than_reporting_a_green_it_cannot_justify(tmp_path):
    """Three shapes where a comparison would be meaningless. Green must mean CHECKED."""
    root = tmp_path
    (root / "real.tsv").write_text("x\n", encoding="utf-8")
    cases = [
        ("<external>/elsewhere.tsv", "outside"),
        ("odd\\nname.tsv", "escaped"),
        ("real.tsv", "recorded no digest"),  # via the MISSING digest below
    ]
    digests = ["0" * 16, "0" * 16, "MISSING"]
    for (name, expected), sha in zip(cases, digests, strict=True):
        got = runprov.verify.check_input(sha, name, root)
        assert got["status"] == "UNVERIFIABLE", name
        assert expected in got["reason"]


@requires_fifo
def test_verify_reports_an_input_it_cannot_hash_now_as_unverifiable(tmp_path):
    """`describe` refuses a FIFO rather than blocking. That is not evidence of a change,
    and calling it STALE would be the overstatement the hashing module avoids."""
    os.mkfifo(tmp_path / "pipe.tsv")
    got = runprov.verify.check_input("0" * 16, "pipe.tsv", tmp_path)
    assert got["status"] == "UNVERIFIABLE" and "regular file" in got["reason"]


def test_verify_catches_a_pin_that_declares_more_inputs_than_it_carries(tmp_path):
    """Three surviving entries agreeing proves nothing about the fourth. A truncated or
    hand-edited pin is a finding about the PIN, so it lands on the artifact."""
    art = tmp_path / "a.tsv"
    art.write_text(
        f"# {runprov.verify.ANCHOR}\n"
        "#   script     : s\n"
        "#   inputs (3), sha256:\n"
        f"#     {'0' * 16}  gone_one.tsv\n"
        "id\n1\n",
        encoding="utf-8",
    )
    rep = runprov.verify.verify_artifact(art, tmp_path)
    assert rep["status"] == "STALE"
    assert rep["pin_truncated"] == ["s: declares 3 input(s), carries 1"]


def test_verify_accepts_a_pin_that_states_it_read_nothing(tmp_path):
    """NONE REGISTERED is a checkable claim and it checks out. An artifact with a pin and
    no entries and no such statement is NOT the same thing."""
    stated, silent = tmp_path / "stated.tsv", tmp_path / "silent.tsv"
    stated.write_text(
        f"# {runprov.verify.ANCHOR}\n#   inputs     : NONE REGISTERED. Either this\n",
        encoding="utf-8",
    )
    silent.write_text(f"# {runprov.verify.ANCHOR}\n#   script     : s\n", encoding="utf-8")
    assert runprov.verify.verify_artifact(stated, tmp_path)["status"] == "OK"
    assert runprov.verify.verify_artifact(silent, tmp_path)["status"] == "UNVERIFIABLE"


def test_verify_reads_a_pin_written_with_any_comment_marker(tmp_path):
    """The marker is whatever precedes the anchor, because `header(comment=...)` is the
    caller's to choose — `## ` for a VCF, `; ` elsewhere. A reader that assumed `# ` would
    verify only the formats it happened to know."""
    src = tmp_path / "in.tsv"
    src.write_text("x\n", encoding="utf-8")
    proj = _project(tmp_path)
    run = runprov.Run("s", project=proj)
    run.input(src)
    for marker in ("## ", "; ", ""):
        art = tmp_path / f"a{len(marker)}.vcf"
        art.write_text(run.header(marker) + "##fileformat=VCFv4.2\n", encoding="utf-8")
        rep = runprov.verify.verify_artifact(art, tmp_path)
        assert rep["status"] == "OK", f"marker {marker!r} -> {rep}"


def test_verify_does_not_read_a_whole_binary_looking_for_a_pin(tmp_path):
    """Bounded, and stated: a pin further in than SCAN_BYTES is not found. Reading every
    byte of a 50 GB BAM to learn it has no pin is the cost that gets a checker removed."""
    art = tmp_path / "big.bin"
    art.write_bytes(b"\x00" * (runprov.verify.SCAN_BYTES + 64) + runprov.verify.ANCHOR.encode())
    assert runprov.verify.read_pins(art) == []
    assert runprov.verify.verify_artifact(art, tmp_path)["status"] == "NO PIN"


def test_verify_treats_an_undecodable_file_as_unpinned(tmp_path):
    """Does not raise. A non-UTF-8 file cannot contain the anchor, and a pin recovered from
    a mis-decoded artifact would carry digests we could not trust anyway.

    Split from the unreadable-file case below (L-26) so that this half — which needs nothing
    of the platform — still runs where `chmod(0o000)` denies nothing."""
    latin = tmp_path / "l.tsv"
    latin.write_bytes(f"# {runprov.verify.ANCHOR}\n".encode("cp1252"))  # em dash -> 0x97
    assert runprov.verify.read_pins(latin) == []


@requires_unreadable_files
def test_verify_treats_an_unreadable_file_as_unpinned(tmp_path):
    """The other half: an OSError on open is not an exception the caller should have to
    handle, because a file we cannot read cannot be shown to carry a pin either way."""
    locked = tmp_path / "locked.tsv"
    locked.write_text("x\n", encoding="utf-8")
    locked.chmod(0o000)
    try:
        assert runprov.verify.read_pins(locked) == []
    finally:
        locked.chmod(0o644)


def test_verify_collects_files_directories_and_neither(tmp_path):
    """Sorted and deduped: naming a file AND its parent must not check it twice, and two
    runs of the same check must report in the same order."""
    (tmp_path / "d").mkdir()
    (tmp_path / "d" / "b.tsv").write_text("b\n", encoding="utf-8")
    (tmp_path / "a.tsv").write_text("a\n", encoding="utf-8")
    got, skipped = runprov.verify.collect(
        [tmp_path / "d", tmp_path / "d" / "b.tsv", tmp_path / "a.tsv", tmp_path / "nope.tsv"]
    )
    assert [p.name for p in got] == ["a.tsv", "b.tsv"]
    assert skipped == 0


def test_verify_does_not_treat_a_file_that_MENTIONS_the_pin_format_as_an_artifact(tmp_path):
    """Found by running `runprov verify` with no arguments in a project with a virtualenv.

    The anchor was matched anywhere in the first 64 KiB, so the checker reported this
    package's own `verify.py` (which holds the anchor as a constant), `run.py` (which
    renders it), their `.pyc` files, and the installed wheel's METADATA — and METADATA
    embeds the README's EXAMPLE pin, so it invented a `GONE` for `data/labels.tsv`, a path
    that exists only in documentation. A gate that fails because the docs describe the
    format is worse than no gate.

    A pinned artifact declares itself at the TOP. A file that merely talks about pins does
    not, and that is the whole discriminator.
    """
    doc = tmp_path / "explaining.md"
    doc.write_text(
        "# How the pin works\n\nHere is what one looks like:\n\n"
        "```\n"
        f"# {runprov.verify.ANCHOR}\n"
        "#   script     : build_labels\n"
        "#   inputs (1), sha256:\n"
        f"#     {'0' * 16}  data/labels.tsv\n"
        "```\n",
        encoding="utf-8",
    )
    assert runprov.verify.read_pins(doc) == [], "prose about the format is not a pin"
    assert runprov.verify.verify_artifact(doc, tmp_path)["status"] == "NO PIN"

    # And the tolerance is real: a shebang above a hand-placed pin still counts.
    script = tmp_path / "generated.txt"
    script.write_text(f"#!/usr/bin/env cat\n# {runprov.verify.ANCHOR}\n#   script : s\n", "utf-8")
    assert len(runprov.verify.read_pins(script)) == 1


def test_verify_does_not_walk_build_and_vcs_directories_but_counts_what_it_skipped(tmp_path):
    """A bare `verify` in a real project walked `.venv`, which is where the false positives
    above came from — and 898 files to check 1 artifact. Skipping is right; skipping
    SILENTLY is the failure this package refuses everywhere else, so the count is reported.

    A path named explicitly is still examined: the list is about what a bare `verify`
    walks, not a claim that those files cannot be checked.
    """
    (tmp_path / "results").mkdir()
    (tmp_path / "results" / "out.tsv").write_text("x\n", encoding="utf-8")
    for skipped_dir in (".git", ".venv", "__pycache__", "node_modules", "thing.egg-info"):
        d = tmp_path / skipped_dir
        d.mkdir()
        (d / "noise.tsv").write_text("noise\n", encoding="utf-8")

    found, skipped = runprov.verify.collect([tmp_path])
    assert [p.name for p in found] == ["out.tsv"]
    assert skipped == 5, "five DIRECTORIES pruned, counted rather than dropped"

    named, _ = runprov.verify.collect([tmp_path / ".venv" / "noise.tsv"])
    assert [p.name for p in named] == ["noise.tsv"], "an explicit path is always examined"


def test_the_skip_count_does_not_descend_into_what_it_skipped(tmp_path, monkeypatch):
    """L-43, half one. The count used to be `rglob("*")` over each pruned tree — walking
    exactly what `SKIP_DIRS` exists not to walk, so the pruning saved the pin reads and not
    the traversal. The cost was proportional to the size of the tree the function had just
    declared it would not look at, and that tree is the one thing on a machine guaranteed to
    be large. Measured on 2,000 real files beside an 18,000-entry `.venv`: 180 ms against
    71 ms; on this package's own repo, 60 ms against 3 ms.

    It also counted DIRECTORIES AS FILES, because `rglob("*")` yields both."""
    (tmp_path / "results").mkdir()
    (tmp_path / "results" / "out.tsv").write_text("x\n", encoding="utf-8")
    venv = tmp_path / ".venv" / "lib" / "site-packages" / "pkg"
    venv.mkdir(parents=True)
    for i in range(50):
        (venv / f"m{i}.py").write_text("x\n", encoding="utf-8")

    walked: list[str] = []
    real_walk = runprov.verify.os.walk
    monkeypatch.setattr(
        runprov.verify.os,
        "walk",
        lambda p, *a, **k: (walked.append(str(p)), real_walk(p, *a, **k))[1],
    )
    found, skipped = runprov.verify.collect([tmp_path])

    assert [p.name for p in found] == ["out.tsv"]
    assert skipped == 1, "ONE directory pruned, not the 54 entries inside it"
    assert not any(".venv" in w for w in walked), f"it descended into a skipped tree: {walked}"


def test_the_skip_count_reaches_both_the_report_and_the_summary(tmp_path, capsys):
    """L-43, half two, and the reason halves one and three survived: the string
    `files_skipped` appeared NOWHERE in this suite, so the number carrying the module's
    honesty claim could be zeroed — or wrong by every subdirectory — without a test noticing.

    The docstring's whole argument is that the count is REPORTED, never merely applied: a
    checker that quietly narrows what it looked at reads as "everything is fine" when it
    means "I did not look there"."""
    monkey = tmp_path / "results"
    monkey.mkdir()
    runprov.configure(root=tmp_path, run_log=tmp_path / "provenance" / "runs.jsonl")
    (tmp_path / "in.tsv").write_text("id\n1\n", encoding="utf-8")
    with runprov.Run("s", {}, provenance=tmp_path / "p.json") as run:
        run.input(tmp_path / "in.tsv")
        with run.open_output(monkey / "out.tsv") as fh:
            fh.write("id\n1\n")
    (tmp_path / ".venv").mkdir()
    (tmp_path / ".venv" / "noise.tsv").write_text("noise\n", encoding="utf-8")

    rc = runprov.__main__.main(
        ["verify", str(tmp_path), "--root", str(tmp_path), "--format", "json"]
    )
    out, err = capsys.readouterr()
    assert rc == 0
    assert json.loads(out)["directories_skipped"] == 1, "it survives into the JSON report"
    assert "1 build/vcs/venv directory not walked" in err, f"and onto the summary line: {err}"


def test_verify_cli_exits_non_zero_and_says_so_when_it_checked_nothing(tmp_path, capsys):
    """A gate that goes green having checked nothing is worse than no gate, because
    someone will trust it. The same rule as `git_status_captured: false`."""
    (tmp_path / "plain.tsv").write_text("id\n1\n", encoding="utf-8")
    rc = runprov.__main__.main(["verify", str(tmp_path), "--root", str(tmp_path)])
    err = capsys.readouterr().err
    assert rc == 1
    assert "NOTHING CHECKED" in err and "not 'nothing is wrong'" in err


def test_verify_reads_the_pin_write_json_embeds(tmp_path, monkeypatch, capsys):
    """L-18. JSON has no comment syntax, so `write_json` puts the pin in a top-level KEY —
    the same `pin_digest` values, stored as data so a consumer does not have to parse prose
    out of a string. `read_pins` knew only the text anchor, so `verify` called a directory of
    `write_json` artifacts unpinned and exited 1 with NOTHING CHECKED. Two features added in
    the same session that could not see each other."""
    monkeypatch.chdir(tmp_path)
    runprov.configure(root=tmp_path, run_log=tmp_path / "provenance" / "runs.jsonl")
    (tmp_path / "in.tsv").write_text("id\n1\n", encoding="utf-8")
    with runprov.Run("s", {}, provenance=tmp_path / "p.json") as run:
        run.input(tmp_path / "in.tsv")
        run.write_json(tmp_path / "calls.json", {"variants": [1, 2, 3]})

    rc = runprov.__main__.main(["verify", str(tmp_path), "--root", str(tmp_path)])
    assert "1 OK" in capsys.readouterr().err
    assert rc == 0

    # And it goes STALE when the input moves, or it would be a pin that never fails.
    (tmp_path / "in.tsv").write_text("id\n1\n2\n3\n", encoding="utf-8")
    rc = runprov.__main__.main(["verify", str(tmp_path), "--root", str(tmp_path)])
    assert "1 STALE" in capsys.readouterr().err
    assert rc == 1


def test_the_json_pin_is_found_by_shape_not_by_key_name(tmp_path, monkeypatch, capsys):
    """`write_json(key=...)` is documented — "Pass a `key` your readers ignore" — so a checker
    that only knew `_provenance` would silently stop recognising pins the moment anyone used
    that parameter."""
    monkeypatch.chdir(tmp_path)
    runprov.configure(root=tmp_path, run_log=tmp_path / "provenance" / "runs.jsonl")
    (tmp_path / "in.tsv").write_text("id\n1\n", encoding="utf-8")
    with runprov.Run("s", {}, provenance=tmp_path / "p.json") as run:
        run.input(tmp_path / "in.tsv")
        run.write_json(tmp_path / "calls.json", {"variants": [1]}, key="__prov__")

    rc = runprov.__main__.main(["verify", str(tmp_path), "--root", str(tmp_path)])
    assert "1 OK" in capsys.readouterr().err and rc == 0


def test_an_ordinary_json_document_is_not_mistaken_for_a_pin(tmp_path):
    """Shape-matching has to be narrow or every config file in the tree becomes an artifact —
    which is the false-positive class that made `verify` invent a GONE for a path existing
    only in the README. A nested object is not a pin unless it carries `script` AND a list of
    two-element input pairs."""
    for body in (
        '{"config": {"script": "run.sh", "inputs": "not-a-list"}}',
        '{"config": {"inputs": [["abc", "x.tsv"]]}}',  # no `script`
        '{"config": {"script": "s", "inputs": [{"path": "x"}]}}',  # not pairs
        '{"config": {"name": "ordinary", "values": [1, 2]}}',
    ):
        (p := tmp_path / "doc.json").write_text(body, encoding="utf-8")
        assert runprov.verify.read_pins(p) == [], body


def test_verify_hashes_each_distinct_input_once_however_many_artifacts_pin_it(
    tmp_path, monkeypatch
):
    """L-19. `check_input` re-derived every pinned input's digest once per artifact that
    pinned it, so `verify` was quadratic in (artifacts x shared inputs). The inputs a
    scientific artifact pins are the expensive files — a reference genome, a BAM, a 40 GB
    matrix — and a fan-out of N artifacts from one input meant N full reads of it. Measured
    on 2,000 artifacts sharing 20 inputs: 40,000 reads over 20 distinct files, 22.5 s.

    COUNTED, not timed. The defect is a number of reads, and asserting on the number is
    exact where a stopwatch would be a test of the machine — which is the flaw the flaky
    timing test in the perf section has."""
    monkeypatch.chdir(tmp_path)
    runprov.configure(root=tmp_path, run_log=tmp_path / "provenance" / "runs.jsonl")
    (tmp_path / "data").mkdir()
    shared = []
    for i in range(3):
        (p := tmp_path / "data" / f"in_{i}.tsv").write_text(f"id\n{i}\n", encoding="utf-8")
        shared.append(p)

    for k in range(5):  # five artifacts, each pinning all three inputs
        with runprov.Run(f"s{k}", {}, provenance=tmp_path / f"p{k}.json") as run:
            for p in shared:
                run.input(p)
            with run.open_output(tmp_path / f"out_{k}.tsv") as fh:
                fh.write("id\n1\n")

    calls = []
    real = runprov.verify.describe
    monkeypatch.setattr(runprov.verify, "describe", lambda p, *a, **kw: (
        calls.append(pathlib.Path(p)), real(p, *a, **kw))[1])  # fmt: skip

    report = runprov.verify.verify([tmp_path], tmp_path)
    assert report["ok"] == 5 and report["stale"] == 0, "the premise: five artifacts verify"
    assert len(calls) == len(set(calls)) == 3, (
        f"{len(calls)} reads over {len(set(calls))} distinct inputs — one each is the point"
    )


def test_the_verify_cache_does_not_change_what_is_reported(tmp_path, monkeypatch):
    """A memo that alters the verdict is worse than the cost it saves. Two artifacts sharing
    one input must BOTH go stale when it moves, from a single re-read."""
    monkeypatch.chdir(tmp_path)
    runprov.configure(root=tmp_path, run_log=tmp_path / "provenance" / "runs.jsonl")
    (src := tmp_path / "ref.tsv").write_text("id\n1\n", encoding="utf-8")
    for k in range(2):
        with runprov.Run(f"s{k}", {}, provenance=tmp_path / f"p{k}.json") as run:
            run.input(src)
            with run.open_output(tmp_path / f"out_{k}.tsv") as fh:
                fh.write("id\n1\n")

    assert runprov.verify.verify([tmp_path], tmp_path)["ok"] == 2
    src.write_text("id\n1\n2\n3\n", encoding="utf-8")
    report = runprov.verify.verify([tmp_path], tmp_path)
    assert report["stale"] == 2 and report["ok"] == 0, "both, from one re-read"


def test_an_unreadable_input_is_reported_for_every_artifact_that_pins_it(tmp_path, monkeypatch):
    """Exceptions are cached too, so a failed read is not retried per artifact — but the
    finding still has to reach every artifact's report, or caching would have turned a
    per-artifact fact into a one-off."""
    monkeypatch.chdir(tmp_path)
    runprov.configure(root=tmp_path, run_log=tmp_path / "provenance" / "runs.jsonl")
    (src := tmp_path / "ref.tsv").write_text("id\n1\n", encoding="utf-8")
    for k in range(2):
        with runprov.Run(f"s{k}", {}, provenance=tmp_path / f"p{k}.json") as run:
            run.input(src)
            with run.open_output(tmp_path / f"out_{k}.tsv") as fh:
                fh.write("id\n1\n")

    src.unlink()  # GONE, which every artifact pinning it must report
    report = runprov.verify.verify([tmp_path], tmp_path)
    assert report["gone"] == 2, "the finding belongs to each artifact, not to the cache"


def test_a_json_pin_past_the_scan_bound_is_not_found_and_does_not_raise(tmp_path):
    """The bound is the same one the text reader has, and it is stated rather than hidden: a
    pin further into the file than `SCAN_BYTES` is not found. `write_json` writes the pin
    first, so this only happens to a file somebody else assembled — and there the head cuts
    through an object, `raw_decode` fails on the truncation, and the file must read as
    unpinned rather than raise out of the checker."""
    # The first key's value must be an OBJECT, so the key regex matches and `raw_decode` is
    # actually attempted on a value the head cuts through. A first draft padded with a long
    # STRING instead: the regex never matched, the loop never ran, and the test passed
    # without reaching the guard it was written for. Coverage caught that, not the assertion.
    big = json.dumps({"big": {"padding": "x" * (runprov.verify.SCAN_BYTES * 2)}})[:-1]
    pin = '"_provenance": {"script": "s", "inputs": [["abcdef0123456789", "in.tsv"]]}'
    (p := tmp_path / "late.json").write_text(f"{big}, {pin}}}", encoding="utf-8")

    assert json.loads(p.read_text(encoding="utf-8"))["_provenance"]["script"] == "s", (
        "the premise: the document is valid JSON and does carry a pin"
    )
    assert runprov.verify.read_pins(p) == [], "past the bound, so not found — and no raise"


def _unverifiable_artifact(tmp_path, monkeypatch, name="out.tsv"):
    """An artifact whose only pinned input sits OUTSIDE the project root, so the pin reads
    `<external>/…` — deliberately not a path, and so not comparable."""
    proj = tmp_path / "proj"
    proj.mkdir(exist_ok=True)
    (tmp_path / "outside.tsv").write_text("outside\n", encoding="utf-8")
    monkeypatch.chdir(proj)
    runprov.configure(root=proj, run_log=proj / "provenance" / "runs.jsonl")
    with runprov.Run("s", {}, provenance=proj / f"{name}.prov.json") as run:
        run.input(tmp_path / "outside.tsv")
        with run.open_output(proj / name) as fh:
            fh.write("id\n1\n")
    return proj


def test_verify_exits_non_zero_when_every_pin_was_unverifiable(tmp_path, monkeypatch, capsys):
    """L-17. The existing guard catches "no artifact carries a pin". This catches the other
    way to check nothing: every pin was unreadable — an input outside the root, an escaped
    name, a digest the run never recorded. Zero comparisons happened either way, but
    `FAILING = (STALE, GONE)` excluded UNVERIFIABLE, so the zero-pins case exited 1 and the
    zero-checks case exited 0. `verify.py`'s own docstring says "Green must mean checked"
    and "AND IT WILL NOT PASS HAVING CHECKED NOTHING"."""
    proj = _unverifiable_artifact(tmp_path, monkeypatch)
    rc = runprov.__main__.main(["verify", str(proj), "--root", str(proj)])
    err = capsys.readouterr().err
    assert "0 OK" in err and "1 UNVERIFIABLE" in err, "the premise: nothing was comparable"
    assert rc == 1, "a gate that goes green having compared nothing is worse than no gate"
    assert "NOTHING CHECKED" in err and "not one" in err


def test_verify_still_passes_when_something_was_actually_checked(tmp_path, monkeypatch, capsys):
    """The other side, or the fix would turn every mixed report red. One artifact verified
    is a real answer, and the count of the rest is on the summary line where a reader sees
    it."""
    proj = _unverifiable_artifact(tmp_path, monkeypatch)
    (proj / "in2.tsv").write_text("id\n1\n", encoding="utf-8")
    with runprov.Run("s2", {}, provenance=proj / "p2.json") as run:
        run.input(proj / "in2.tsv")
        with run.open_output(proj / "out2.tsv") as fh:
            fh.write("id\n2\n")

    rc = runprov.__main__.main(["verify", str(proj), "--root", str(proj)])
    err = capsys.readouterr().err
    assert "1 OK" in err and "1 UNVERIFIABLE" in err
    assert rc == 0, "something was checked, and the unverifiable count is reported"


def test_verify_cli_reports_text_and_json_and_sets_the_exit_code(tmp_path, monkeypatch, capsys):
    src, results = _chain(tmp_path, monkeypatch)

    assert runprov.__main__.main(["verify", str(results), "--root", str(tmp_path)]) == 0
    assert "OK" in capsys.readouterr().out

    src.write_text("id\tv\n1\tCHANGED\n", encoding="utf-8")
    assert runprov.__main__.main(["verify", str(results), "--root", str(tmp_path)]) == 1
    text = capsys.readouterr()
    assert "STALE" in text.out and "via step1" in text.out

    assert (
        runprov.__main__.main(["verify", str(results), "--root", str(tmp_path), "--format", "json"])
        == 1
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload["stale"] == 2 and payload["root"] == str(tmp_path)


def test_verify_cli_defaults_to_the_active_project_root(tmp_path, monkeypatch, capsys):
    """No path and no --root: the project's own root. `verify` never reads the history —
    the pin is in the artifact precisely so a checker needs nothing else."""
    _chain(tmp_path, monkeypatch)
    runprov.configure(root=tmp_path, run_log=tmp_path / "elsewhere.jsonl")
    assert runprov.__main__.main(["verify"]) == 0
    assert not (tmp_path / "elsewhere.jsonl").exists(), "no history was consulted"
    assert "2 pinned artifact(s)" in capsys.readouterr().err


def test_verify_renders_nothing_for_an_empty_report():
    assert runprov.verify.render({"artifacts": []}) == ""


def test_a_pin_entry_of_the_wrong_digest_width_is_not_an_entry(tmp_path):
    """L-77. `_ENTRY`'s exact-length hex group was unguarded: relaxed to `[0-9a-f]+`, the
    whole suite stayed green. No test ever fed `read_pins` a body line whose hex field was
    longer or shorter than `PIN_DIGEST_CHARS`.

    The width is the shared contract between `header()` and this reader — the module
    docstring's whole argument for `pin_digest` being ONE function is that the two must not
    drift. A relaxed reader accepts a truncated or hand-edited pin as valid and then compares
    16 characters of the recorded digest against a different number of characters, which
    reads as STALE or, worse, as OK.

    A 64-character field is the realistic case: it is a full `sha256`, which is exactly what
    someone pasting "the digest" by hand would reach for."""
    src = tmp_path / "kept.tsv"
    src.write_text("x\n", encoding="utf-8")
    good = runprov.hashing.pin_digest(runprov.describe(src))
    assert len(good) == runprov.hashing.PIN_DIGEST_CHARS, "the premise: the pin width"

    art = tmp_path / "a.tsv"
    art.write_text(
        f"# {runprov.verify.ANCHOR}\n"
        "#   script     : s\n"
        "#   inputs (3), sha256:\n"
        f"#     {good}  kept.tsv\n"
        f"#     {'a' * 64}  full_sha256.tsv\n"
        f"#     {'b' * 8}  truncated.tsv\n"
        "id\n1\n",
        encoding="utf-8",
    )

    rep = runprov.verify.verify_artifact(art, tmp_path)
    assert [i["name"] for i in rep["inputs"]] == ["kept.tsv"], (
        f"only the correctly-sized entry is one: {[i['name'] for i in rep['inputs']]}"
    )
    # And the pin does not pass quietly: it DECLARED three and carries one, which is the
    # truncation finding -- a reader that silently accepted the other two would report OK.
    assert rep["pin_truncated"], "declaring 3 and carrying 1 is a fact about the pin"
    assert rep["status"] != "OK"


def test_verify_walks_past_a_comment_line_that_is_not_part_of_the_pin(tmp_path):
    """An artifact's own comments sit beside the pin, and a reader that stopped at the
    first unrecognised marker line would drop every entry after them.

    The truncation note is rendered here too: it is a fact about the pin rather than about
    any input, so it has no per-input line to appear on and would otherwise print nowhere.
    """
    src = tmp_path / "kept.tsv"
    src.write_text("x\n", encoding="utf-8")
    art = tmp_path / "a.tsv"
    art.write_text(
        f"# {runprov.verify.ANCHOR}\n"
        "#   script     : s\n"
        "# generated by the lab pipeline, which comments its own output\n"
        "#   inputs (2), sha256:\n"
        f"#     {runprov.hashing.pin_digest(runprov.describe(src))}  kept.tsv\n"
        "id\n1\n",
        encoding="utf-8",
    )
    rep = runprov.verify.verify_artifact(art, tmp_path)
    assert [i["name"] for i in rep["inputs"]] == ["kept.tsv"], "the entry after the comment"
    assert rep["status"] == "STALE" and rep["pin_truncated"]

    rendered = runprov.verify.render({"artifacts": [rep]})
    assert "!! pin s: declares 2 input(s), carries 1" in rendered


def test_the_cli_names_the_invocation_the_reader_actually_used(monkeypatch, capsys):
    """Two ways in, two names. Hardcoding one meant the console script — the only form
    `uvx runprov` and `pipx run runprov` can resolve — printed usage telling the reader to
    type something else, and every `error:` line named an invocation they had not used.

    Leaving `prog` unset is not the fix: argparse derives it from `sys.argv[0]`, which
    under `-m` is this module's file, so the module form would advertise `__main__.py`.
    """
    for argv0, expected in (
        ("/usr/lib/python3.12/runprov/__main__.py", "python -m runprov"),
        ("/home/x/.venv/bin/runprov", "runprov"),
        ("", "runprov"),
    ):
        monkeypatch.setattr(sys, "argv", [argv0] if argv0 else [])
        with pytest.raises(SystemExit):
            runprov.__main__.main(["--help"])
        out = capsys.readouterr().out
        assert out.startswith(f"usage: {expected} "), f"{argv0!r} -> {out.splitlines()[0]}"
        assert "__main__.py" not in out


# ================= the pin is not a comment everywhere, and one format fails SILENTLY
def test_a_format_that_cannot_hold_a_pin_gets_one_BESIDE_it(tmp_path):
    """Newick is why the guard exists: a pinned tree PARSES, and Biopython read a 3-taxon
    tree back with 6 terminals, three of them harvested from the pin's own prose.

    Refusing outright was the first fix and it was half of one — it protected the artifact
    and gave up the property that makes the pin worth having, which is that an artifact can
    say what it was made from after the run's sidecar has been overwritten. The file is now
    written untouched and the pin goes next to it.
    """
    proj = _project(tmp_path)
    src = tmp_path / "aln.fa"
    src.write_text(">a\nACGT\n", encoding="utf-8")
    with runprov.Run("tree", project=proj, provenance=tmp_path / "p.json") as run:
        run.input(src)
        with run.open_output(tmp_path / "out.nwk") as fh:
            fh.write("(HCV1a:0.1,HCV1b:0.2,HCV2a:0.3);\n")

    tree = (tmp_path / "out.nwk").read_text(encoding="utf-8")
    assert tree == "(HCV1a:0.1,HCV1b:0.2,HCV2a:0.3);\n", "not one byte added to the artifact"
    assert "provenance" not in tree

    side = tmp_path / ("out.nwk" + runprov.run.PIN_SIDECAR_SUFFIX)
    assert side.is_file()
    body = side.read_text(encoding="utf-8")
    assert "provenance for: out.nwk" in body, "a separated sidecar must still name its artifact"
    assert "aln.fa" in body, "and carry the pin it would have written inside"

    rec = json.loads((tmp_path / "p.json").read_text(encoding="utf-8"))
    names = sorted(pathlib.Path(o["path"]).name for o in rec["outputs"])
    assert names == ["out.nwk", "out.nwk.prov.txt"], "the sidecar is an artifact, so it is hashed"


def _sidecar_pinned(tmp_path):
    """An artifact whose pin lives BESIDE it, plus its input. Returns (artifact, sidecar)."""
    proj = _project(tmp_path)
    (tmp_path / "in.tsv").write_text("id\n1\n", encoding="utf-8")
    with runprov.Run("make", project=proj, provenance=tmp_path / "p.json") as run:
        run.input(tmp_path / "in.tsv")
        art = run.output(tmp_path / "out.png")
        art.write_bytes(b"\x89PNG\r\n\x1a\n fake")
        side = run.pin_sidecar(art)
    return art, side


def test_a_sidecar_whose_artifact_was_deleted_is_GONE_not_OK(tmp_path):
    """L-03. A `.prov.txt` speaks FOR the file beside it; it is not that file. Checked as
    though it were, every input still hashed correctly — they do, nothing touched them — so a
    deleted artifact reported `OK` and exited 0. The pin surviving separately is the whole
    point of a sidecar, and it was being read as evidence the artifact is fine."""
    art, side = _sidecar_pinned(tmp_path)
    report = runprov.verify.verify([tmp_path], tmp_path)
    assert report["ok"] == 1 and report["gone"] == 0, "the premise: it passes while present"

    art.unlink()
    report = runprov.verify.verify([tmp_path], tmp_path)
    assert report["gone"] == 1 and report["ok"] == 0
    entry = next(r for r in report["artifacts"] if r["status"] == runprov.verify.GONE)
    assert entry["artifact"] == str(art), "the report names the artifact, not its sidecar"
    assert "no longer there" in entry["reason"]
    assert side.is_file(), "the sidecar itself is untouched — it is evidence, not the finding"


def test_the_verify_report_names_the_artifact_even_when_the_pin_is_beside_it(tmp_path):
    """The passing case was wrong too: the row read `out.png.prov.txt`, so a reader checking
    `out.png` never saw `out.png` in the report at all. Where the pin happens to live is the
    checker's business, not the reader's."""
    art, _ = _sidecar_pinned(tmp_path)
    report = runprov.verify.verify([tmp_path], tmp_path)
    names = [r["artifact"] for r in report["artifacts"]]
    assert str(art) in names
    assert not any(n.endswith(runprov.run.PIN_SIDECAR_SUFFIX) for n in names), names
    assert runprov.verify.render(report).count("out.png.prov.txt") == 0


def test_the_sidecar_suffix_has_one_definition(tmp_path):
    """The writer appends it and the verifier strips it. Two copies of the string would
    drift, and the drift would be silent: sidecars would still be written and would simply
    stop being recognised as speaking for anything."""
    assert runprov.run.PIN_SIDECAR_SUFFIX is runprov.hashing.PIN_SIDECAR_SUFFIX


def _repo_root():
    return pathlib.Path(__file__).resolve().parent.parent


def _release_tree(
    tmp_path, *, version="0.1.0", citation_version=None, heading="Unreleased", date_released=False
):
    """A four-file tree `ci.py release-check` can read, and nothing else.

    `ROOT` is `ci.py`'s own directory, so copying the script beside these fixtures is what
    lets the real check run against a version state this repository does not have.
    """
    shutil.copy(_repo_root() / "ci.py", tmp_path / "ci.py")
    (tmp_path / "runprov").mkdir()
    (tmp_path / "pyproject.toml").write_text(f'version = "{version}"\n', encoding="utf-8")
    (tmp_path / "runprov/__init__.py").write_text(f'__version__ = "{version}"\n', encoding="utf-8")
    cff = f"version: {citation_version or version}\n"
    if date_released:
        cff += "date-released: 2026-08-18\n"
    (tmp_path / "CITATION.cff").write_text(cff, encoding="utf-8")
    (tmp_path / "CHANGELOG.md").write_text(f"## [{heading}] — {version}\n", encoding="utf-8")
    return tmp_path


def _release_check(tree, tag=None):
    env = {**os.environ, "RUNPROV_RELEASE_TAG": tag} if tag else os.environ
    return subprocess.run(
        [sys.executable, str(tree / "ci.py"), "release-check"],
        capture_output=True,
        text=True,
        env=env,
        cwd=tree,
    )


def test_release_check_passes_on_an_untagged_tree_whose_copies_agree(tmp_path):
    """L-34/L-68. The baseline, so the failures below are the check firing rather than the
    check being broken."""
    r = _release_check(_release_tree(tmp_path))
    assert r.returncode == 0, r.stdout + r.stderr
    assert "release-check ok" in r.stdout and "untagged" in r.stdout


def test_release_check_refuses_a_tag_that_disagrees_with_the_version(tmp_path):
    """L-34. `git tag v0.2.0` on a tree that still says 0.1.0 uploaded 0.1.0, and 0.1.0
    could never be re-uploaded to correct it. Nothing in the release path compared the two."""
    r = _release_check(_release_tree(tmp_path, version="0.1.0"), tag="v0.2.0")
    assert r.returncode != 0
    assert "does not match" in r.stdout + r.stderr


def test_release_check_refuses_a_tag_while_the_changelog_says_unreleased(tmp_path):
    """L-36. A tag means released; `[Unreleased]` means it is not. Publishing with both is
    how a version reaches PyPI with no dated section to attribute it to."""
    r = _release_check(_release_tree(tmp_path, heading="Unreleased"), tag="v0.1.0")
    assert r.returncode != 0
    assert "[Unreleased]" in r.stdout + r.stderr


def test_release_check_refuses_a_released_version_with_no_citation_year(tmp_path):
    """L-36. `CITATION.cff` exists so a reader does not have to guess a citation — and
    without `date-released` the citation it hands them has no year in it."""
    tree = _release_tree(tmp_path, heading="0.1.0 — 2026-08-18", date_released=False)
    r = _release_check(tree)
    assert r.returncode != 0
    assert "date-released" in r.stdout + r.stderr

    (tree / "CITATION.cff").write_text(
        "version: 0.1.0\ndate-released: 2026-08-18\n", encoding="utf-8"
    )
    ok = _release_check(tree, tag="v0.1.0")
    assert ok.returncode == 0, ok.stdout + ok.stderr


def test_release_check_refuses_copies_that_disagree(tmp_path):
    """L-68. The four copies agree today, which is exactly when to assert it: drift is
    otherwise found at release, in the one build that cannot be re-uploaded."""
    r = _release_check(_release_tree(tmp_path, version="0.1.0", citation_version="0.9.9"))
    assert r.returncode != 0
    assert "disagree" in r.stdout + r.stderr and "CITATION.cff" in r.stdout + r.stderr


def test_release_check_reports_a_version_it_cannot_find(tmp_path):
    """A silent pass on an unreadable file would be worse than no check: the message has to
    name the file, because the patterns here are the thing most likely to rot."""
    tree = _release_tree(tmp_path)
    (tree / "CITATION.cff").write_text("# nothing here\n", encoding="utf-8")
    r = _release_check(tree)
    assert r.returncode != 0
    assert "no version found in CITATION.cff" in r.stdout + r.stderr


def test_build_runs_the_release_check_first(tmp_path):
    """The guard is only a guard if `build` calls it — and `ci.py build` takes minutes, so
    this reads the wiring rather than running it. Deleting the call is the whole regression."""
    src = (_repo_root() / "ci.py").read_text(encoding="utf-8")
    assert re.search(r"def build\(\) -> None:\n    release_check\(\)", src), (
        "ci.py build must call release_check() before it builds anything"
    )


def _ci_module():
    spec = importlib.util.spec_from_file_location("_ci_under_test", _repo_root() / "ci.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_the_coverage_floor_is_on_unless_it_is_asked_off(monkeypatch):
    """L-26. The floor has to be OFF for the Windows leg — 22 tests skip there, so their
    lines go unmeasured and 100% is unreachable by construction. That is exactly the shape
    of change that quietly becomes permanent, so: on by default, off only for the literal
    value, and every other value including the empty string an Actions expression produces
    for a leg that sets nothing leaves it on."""
    ci = _ci_module()
    monkeypatch.delenv("RUNPROV_COVERAGE_FLOOR", raising=False)
    assert "--cov-fail-under=100" in ci._coverage_args()
    for value in ("", "no", "0", "false", "OFFF"):
        monkeypatch.setenv("RUNPROV_COVERAGE_FLOOR", value)
        assert "--cov-fail-under=100" in ci._coverage_args(), f"{value!r} must not lower it"
    for value in ("off", "OFF", "Off"):
        monkeypatch.setenv("RUNPROV_COVERAGE_FLOOR", value)
        assert "--cov-fail-under=100" not in ci._coverage_args()
        assert "--cov=runprov" in ci._coverage_args(), "coverage is still MEASURED, just not gated"


def test_only_the_windows_leg_turns_the_coverage_floor_off():
    """L-26. If a second leg picked this up the gate would be gone on Linux and the suite
    would still be green — which is the failure mode this whole audit keeps finding."""
    yaml = pytest.importorskip("yaml")
    wf = _repo_root() / ".github/workflows/test.yml"
    if not wf.is_file():  # pragma: no cover - not shipped in the sdist
        pytest.skip("test.yml not present")
    matrix = yaml.safe_load(wf.read_text(encoding="utf-8"))["jobs"]["test"]["strategy"]["matrix"]
    off = [leg for leg in matrix["include"] if leg.get("coverage_floor") == "off"]
    assert [leg["os"] for leg in off] == ["windows-latest"], (
        f"exactly one leg may lower the floor; found {off}"
    )
    assert matrix["os"] == ["ubuntu-latest"], "the base matrix must carry no floor override"


def test_the_publish_workflow_declares_least_privilege(tmp_path):
    """L-34. With no top-level `permissions`, every job inherits the repository default —
    write-all on older repositories — so `build` and `test` ran a release with push rights
    they never needed. A job-level block REPLACES the top-level one, which is why `publish`
    has to repeat `contents: read` beside the id-token it needs for trusted publishing."""
    yaml = pytest.importorskip("yaml")
    wf = _repo_root() / ".github/workflows/publish.yml"
    if not wf.is_file():  # pragma: no cover - not shipped in the sdist
        pytest.skip("publish.yml not present")
    d = yaml.safe_load(wf.read_text(encoding="utf-8"))
    assert d["permissions"] == {"contents": "read"}
    assert d["jobs"]["publish"]["permissions"] == {"id-token": "write", "contents": "read"}


def test_every_copy_of_the_version_agrees():
    """L-68. The version is written in FOUR places — `pyproject.toml`, `__init__.__version__`,
    `CITATION.cff` and the CHANGELOG's top heading — and nothing failed if they drifted. They
    agree today, which is exactly when to assert it: a mismatch is discovered at release, in
    the one build that cannot be re-uploaded, and it makes the citation cite a version that
    was never published."""
    root = _repo_root()
    for name in ("pyproject.toml", "runprov/__init__.py", "CITATION.cff", "CHANGELOG.md"):
        if not (root / name).is_file():  # pragma: no cover - sdist ships them; a bare tree may not
            pytest.skip(f"{name} not present")

    found = {
        "pyproject.toml": re.search(
            r'^version = "([^"]+)"', (root / "pyproject.toml").read_text(encoding="utf-8"), re.M
        ),
        "__init__.py": re.search(
            r'^__version__ = "([^"]+)"',
            (root / "runprov/__init__.py").read_text(encoding="utf-8"),
            re.M,
        ),
        "CITATION.cff": re.search(
            r"^version: (\S+)", (root / "CITATION.cff").read_text(encoding="utf-8"), re.M
        ),
        "CHANGELOG.md": re.search(
            # See `_VERSION_SOURCES` in ci.py: the version is in the BRACKETS for a real
            # release and after the dash only for `[Unreleased]`. Reading the field after the
            # dash unconditionally captured the DATE from a released heading.
            r"^## \[(?:Unreleased\] — )?(\d[^\]\s]*)",
            (root / "CHANGELOG.md").read_text(encoding="utf-8"),
            re.M,
        ),
    }
    missing = [k for k, m in found.items() if m is None]
    assert not missing, f"no version found in {missing}"
    versions = {k: m.group(1) for k, m in found.items()}
    assert len(set(versions.values())) == 1, f"the copies disagree: {versions}"
    assert versions["__init__.py"] == runprov.__version__


def test_the_pre_commit_ruff_matches_the_one_the_gate_enforces():
    """L-69. The hook pinned ruff v0.6.9 against a gate on 0.16.2, ten minor versions apart —
    and with `--fix` that is worse than a skew: the hook REWRITES code to satisfy an old ruff,
    then CI rejects it with the new one, so the tool that exists to catch a failure early
    creates one."""
    root = _repo_root()
    if not (root / ".pre-commit-config.yaml").is_file():  # pragma: no cover - not in the sdist
        pytest.skip(".pre-commit-config.yaml not present")

    hook = re.search(
        r"ruff-pre-commit\n\s+rev: v(\S+)", (root / ".pre-commit-config.yaml").read_text("utf-8")
    )
    gate = re.search(r'"ruff==(\S+?)"', (root / "pyproject.toml").read_text(encoding="utf-8"))
    assert hook and gate, "could not find both pins"
    assert hook.group(1) == gate.group(1), (
        f"pre-commit runs ruff {hook.group(1)} while the gate enforces {gate.group(1)}; with "
        f"`--fix` the hook will rewrite code that CI then rejects"
    )


def _readme_headings():
    """(level, text) for every ATX heading, in document order."""
    out = []
    for line in (_repo_root() / "README.md").read_text(encoding="utf-8").splitlines():
        if line.startswith("#"):
            hashes = len(line) - len(line.lstrip("#"))
            if line[hashes : hashes + 1] == " ":
                out.append((hashes, line[hashes + 1 :].strip()))
    return out


#: The predecessor log the README's central worked example measures. NOT in this repository
#: and it must never be: it is 2.1 MB of somebody's real research history. Point
#: `RUNPROV_PREDECESSOR_LOG` at it to check the README's numbers against the actual file.
_PREDECESSOR_LOG_SHA = "bc0a72ca0dcea3fc25c32fbdd960349e66f44f31a852b440bd102c9a722e3c71"


def test_the_predecessor_log_numbers_are_reproducible_from_the_named_file():
    """L-49. The README's worked example cited measurements against a file it never
    identified — and SEVERAL copies of that file exist, with different line counts, different
    entry counts and different failure offsets. A reviewer holding a different copy produced
    three confident, wrong corrections to the section. That is precisely the failure this
    package exists to prevent, in its own front matter.

    So the file is now named and pinned by digest in the README, and this re-derives every
    figure from it. SKIPPED when the file is not reachable, which is the honest state on any
    machine but one: an unreachable file is not a passing test, and the digest check means a
    different copy skips rather than silently 'passing' against the wrong numbers."""
    raw = os.environ.get("RUNPROV_PREDECESSOR_LOG")
    if not raw:  # pragma: no cover - set only where the file exists
        pytest.skip("set RUNPROV_PREDECESSOR_LOG to the predecessor transformation_log.yml")
    path = pathlib.Path(raw)
    if not path.is_file():  # pragma: no cover - the drive it lives on is removable
        pytest.skip(f"{path} is not reachable")
    data = path.read_bytes()
    if hashlib.sha256(data).hexdigest() != _PREDECESSOR_LOG_SHA:  # pragma: no cover
        pytest.skip("a DIFFERENT copy of the log — this is the defect the row is about")

    readme = (_repo_root() / "README.md").read_text(encoding="utf-8")
    assert _PREDECESSOR_LOG_SHA in readme, "the README must name the digest it measured"
    section = readme[
        readme.index("It is JSONL rather than YAML") : readme.index("Read it back with the CLI")
    ]

    lines = data.decode("utf-8", "replace").splitlines()
    entries = [i for i, line in enumerate(lines) if line.startswith("- step:")]
    yaml = pytest.importorskip("yaml")

    measured = {
        "bytes": len(data),
        "lines": len(lines),
        "entries": len(entries),
        "documents": sum(1 for line in lines if line.rstrip() == "---"),
        "longest_entry": max(
            b - a for a, b in zip(entries, [*entries[1:], len(lines)], strict=True)
        ),
    }
    assert measured == {
        "bytes": 2_147_154,
        "lines": 24_300,
        "entries": 295,
        "documents": 252,
        "longest_entry": 6_544,
    }, measured
    # Measured, then required to be WHAT THE README SAYS — IN CONTEXT. Asserting the literals
    # alone would leave the prose free to drift away from the file it names, which is the
    # row's defect one level up; asserting a bare `"295" in section` is not enough either,
    # since the same digits appear in the recovery claim and a wrong entry count survived it.
    for name, phrase in {
        "bytes": f"bytes   {measured['bytes']:,}",
        "lines": f"{measured['lines']:,} lines)",
        "entries": f"**{measured['entries']} entries**",
        "documents": f"**{measured['documents']}** of them",
        "longest_entry": f"**{measured['longest_entry']:,}\nlines**",
    }.items():
        assert phrase in section, f"the README does not state {name} as {phrase!r}"

    # The two failure offsets, which are the numbers a reader is most likely to check.
    with pytest.raises(yaml.YAMLError) as single:
        yaml.safe_load(data.decode("utf-8", "replace"))
    assert "line 14547" in str(single.value).replace(",", "")
    with pytest.raises(yaml.YAMLError) as stream:
        list(yaml.safe_load_all(data.decode("utf-8", "replace")))
    assert "line 14554" in str(stream.value).replace(",", "")

    # "A tolerant line-wise recovery gets 293 of 295 entries back" — the claim that carries
    # the argument for JSONL, so it is derived here rather than quoted.
    recovered = 0
    for a, b in zip(entries, [*entries[1:], len(lines)], strict=True):
        block = "\n".join(line for line in lines[a:b] if line.rstrip() != "---")
        try:
            recovered += bool(yaml.safe_load(block))
        except yaml.YAMLError:
            pass
    assert recovered == 293, recovered
    assert f"**{recovered} of {measured['entries']}**" in section, (
        "the recovery claim must state both halves, and both must be the measured ones"
    )
    for offset in (14547, 14554):
        assert f"{offset:,}" in section, f"the README does not state the failure at {offset:,}"


def test_every_statement_of_the_repair_script_count_agrees():
    """L-59 was fixed once and drifted twice. The predecessor's `fix_transformation_log_*.py`
    scripts are cited in eight places across four files as the reason this package exists,
    and the number has been written as eleven, nine and eight — three values for one fact, in
    a package whose subject is claims that stopped being true.

    The COUNT cannot be checked from here: the scripts live in the sibling project, outside
    this repository. What can be checked is that every statement of it agrees, which is the
    same treatment the four version copies get — a fact nothing verified is at least not
    allowed to contradict itself."""
    pat = re.compile(r"(\w+)\s+(?:`fix_transformation_log_\*\.py`\s+)?(?:repair|heal) scripts")
    root = _repo_root()
    found = {}
    for name in [
        "README.md",
        "WHY.md",
        "CHANGELOG.md",
        *sorted(str(p.relative_to(root)) for p in (root / "runprov").glob("*.py")),
    ]:
        f = root / name
        if not f.is_file():  # pragma: no cover - WHY.md is not in the sdist
            continue
        for m in pat.finditer(f.read_text(encoding="utf-8").replace("\n", " ")):
            found.setdefault(m.group(1).lower(), []).append(name)
    assert found, "the citation is gone entirely; if that is deliberate, delete this test"
    assert len(found) == 1, f"one fact, {len(found)} different numbers: {found}"


def test_no_cli_subcommand_is_documented_inside_another_ones_section():
    """L-70. `runprov exec` is a top-level subcommand and sat as an H3 CHILD of "The
    notebook: `show`" — a section about a read-only viewer. A reader scanning the rendered
    outline for `exec` finds it filed under something it has nothing to do with, and PyPI
    renders that outline from the same file, permanently, at upload.

    Asserted structurally rather than by line number: any heading that introduces
    `runprov <sub>` must not sit under the section of a DIFFERENT subcommand."""
    subs = ["log", "lineage", "show", "exec", "verify"]
    named, current = [], None
    for level, text in _readme_headings():
        here = [s for s in subs if f"runprov {s}`" in text or f"`{s}`" in text]
        if level <= 2:
            current = here[0] if here else None
        elif here and current and here[0] != current:
            named.append((current, here[0], text))
    assert not named, (
        f"a subcommand is documented inside another's section: {named} — promote the "
        f"heading to H2 so the outline matches `python -m runprov --help`"
    )


def test_the_sections_that_are_not_about_show_are_not_filed_under_it():
    """L-70, the other half: `tool()` and `code()` are RECORDING APIs, and both were H3
    children of the viewer's section too. They name no subcommand, so the structural check
    above cannot see them — this one names them, because being specific about the three
    headings that moved is better than a rule that would have to guess."""
    levels = {text: level for level, text in _readme_headings()}
    for heading in (
        "What code actually ran",
        "The work that is not Python",
        "A pipeline with no Python in it: `runprov exec`",
    ):
        assert heading in levels, f"{heading!r} is gone; if it was renamed, update this test"
        assert levels[heading] == 2, f"{heading!r} is not about `show` and must not nest under it"
    # And the two that ARE about `show` stay under it, so this cannot be satisfied by
    # flattening every heading in the file.
    assert levels["When did I add that column?"] == 3
    assert levels["Do I need to run this again?"] == 3


def test_the_readme_documents_the_in_band_allowlist_exactly():
    """L-60. The format table's in-band row read "TSV, CSV, BED, YAML, Markdown, SQL" and
    omitted GFF3 — while the allowlist includes `.gff3` and the prose 800 lines further down
    says `#` IS a comment in a GFF3. So the document contradicted itself, and a
    bioinformatics reader checking the table was told their GFF3 gets a sidecar when the pin
    goes in the file.

    A prose list of a constant drifts the moment the constant changes, which is what
    happened. This asserts the two agree, so the next suffix added to `PIN_INLINE` fails
    here until the README learns about it."""
    readme = pathlib.Path(__file__).resolve().parent.parent / "README.md"
    if not readme.is_file():  # pragma: no cover - the sdist ships it; a bare checkout may not
        pytest.skip("README.md not present")
    row = next(
        line for line in readme.read_text(encoding="utf-8").splitlines()
        if line.startswith("| in-band |")
    )  # fmt: skip
    documented = set(re.findall(r"`(\.[a-z0-9]+)`", row))
    assert documented == set(runprov.run.PIN_INLINE), (
        f"README in-band row and PIN_INLINE disagree — "
        f"only in the code: {sorted(set(runprov.run.PIN_INLINE) - documented)}, "
        f"only in the README: {sorted(documented - set(runprov.run.PIN_INLINE))}"
    )


def test_the_binary_refusal_names_the_mode_not_a_comment_syntax(tmp_path):
    """L-50. The message came from `PIN_UNSAFE`, and 25 of the 40 binary suffixes are not in
    that table — `.pkl` `.pt` `.rds` `.feather` and the rest — so they fell to its default and
    told a caller writing a pickle that "the format is not known to accept a `#` comment".

    True and irrelevant: the handle is TEXT, so the format could accept `#` gladly and this
    would still be the wrong call. A message naming the wrong cause sends someone looking for
    a pin setting that does not exist."""
    proj = _project(tmp_path)
    run = runprov.Run("s", project=proj)
    outside = sorted(set(runprov.run.PIN_BINARY) - set(runprov.run.PIN_UNSAFE))
    assert len(outside) > 20, f"the premise: most binary suffixes are not in PIN_UNSAFE ({outside})"

    for suffix in (".pkl", ".pt", ".rds", ".feather", ".png"):
        with pytest.raises(ValueError) as caught:
            with run.open_output(tmp_path / f"f{suffix}"):
                pass
        message = str(caught.value)
        assert "TEXT handle" in message, f"{suffix}: {message}"
        assert "not known to accept" not in message, f"{suffix} still blames the comment syntax"
        assert "pin_sidecar" in message, f"{suffix}: the message must name the way that works"


def test_the_history_carries_the_omitted_count_beside_the_code_digest(tmp_path, monkeypatch):
    """L-51. Past `imported_code_max` the digest covers only the kept PREFIX of the sorted
    file list, so it silently changes meaning — and the history carried the digest without
    the one number that reveals it. Two runs whose digests differ would look like a code
    change when the truth may be that a 201st file appeared and pushed a different 200 into
    the hash. A digest whose scope is unstated is not a digest."""
    monkeypatch.chdir(tmp_path)
    proj = runprov.Project(
        root=tmp_path, run_log=tmp_path / "runs.jsonl", run_id=lambda: "r",
        generation=lambda: "g", imported_code_max=2,
    )  # fmt: skip
    for i in range(5):
        (tmp_path / f"m{i}.R").write_text(f"# {i}\n", encoding="utf-8")

    with runprov.Run("s", project=proj, provenance=tmp_path / "p.json") as run:
        for i in range(5):
            run.code(tmp_path / f"m{i}.R")

    line = json.loads((tmp_path / "runs.jsonl").read_text(encoding="utf-8").splitlines()[0])
    ic = line["imported_code"]
    assert ic["count"] == 5 and ic["omitted"] == 3, ic
    assert ic["digest"], "the digest is still there — it is its SCOPE that needed stating"


def _nest(n):
    v = "leaf"
    for _ in range(n):
        v = {"k": v}
    return v


def _stack_depth():
    n, f = 0, sys._getframe()
    while f is not None:
        n, f = n + 1, f.f_back
    return n


def test_the_yaml_view_survives_a_record_json_accepts():
    """L-72. `to_yaml` recurses once per nesting level, so `show <target> --format yaml` died
    with an uncaught `RecursionError` on a record `json.loads` accepts without complaint. The
    READER was narrower than the writer, in a module whose founding story is a log that
    stopped being readable because one record defeated its reader.

    THE DEPTHS ARE NOT ASSERTED, and that is the lesson rather than a weakening. The first
    version of this test required the VALUE to survive at depth 5,000 — true on CPython 3.12,
    false on 3.10, because past the cap the remainder goes to `json.dumps`, which recurses on
    a stack already `YAML_MAX_DEPTH` frames deep. How much it can still take is a property of
    the interpreter. So the guarantee is the one that can hold everywhere: it renders, it
    parses, and it never raises."""
    yaml = pytest.importorskip("yaml")
    for depth in (50, 900, 1_000, 5_000):
        rendered = runprov.show.to_yaml(_nest(depth))
        assert yaml.safe_load(rendered) is not None, f"depth {depth} rendered unparseable YAML"

    # Well inside the cap the value is present in BLOCK form, so the flow-style path is not
    # quietly swallowing everything.
    shallow = yaml.safe_load(runprov.show.to_yaml(_nest(50)))
    for _ in range(50):
        shallow = shallow["k"]
    assert shallow == "leaf"

    # And past the cap the tail is either the value or the stated marker — never a silent
    # truncation, and never an exception.
    deep = yaml.safe_load(runprov.show.to_yaml(_nest(5_000)))
    for _ in range(runprov.show.YAML_MAX_DEPTH):
        deep = deep["k"]
    tail = json.dumps(deep)
    assert "leaf" in tail or runprov.show.YAML_TOO_DEEP in tail, tail[:200]


def test_the_yaml_view_says_so_when_even_flow_style_is_too_deep():
    """The fallback needs a fallback. `json.dumps` past the cap is itself recursive, so on
    CPython 3.10 a 1,000-deep record raised inside it — the same RecursionError the cap exists
    to prevent, one frame further down, on the version `requires-python` names as the floor
    and which nothing here had ever run.

    SKIPPED WHERE THE BRANCH CANNOT BE REACHED. CPython 3.12's C encoder does not consume
    Python frames — measured: it renders a 5,000-deep structure with a 60-frame budget — so
    there is no honest way to force it there, and pretending otherwise would mean testing a
    mock instead of the thing. The probe below decides, so this asserts real behaviour on
    3.10 and 3.11 and says nothing it cannot support on 3.12."""
    yaml = pytest.importorskip("yaml")
    original = sys.getrecursionlimit()
    budget = runprov.show.YAML_MAX_DEPTH + 60

    def with_small_budget(fn):
        # Measured from where we stand, so the limit is never set below the stack in use.
        sys.setrecursionlimit(_stack_depth() + budget)
        try:
            return fn()
        finally:
            sys.setrecursionlimit(original)

    def probe():
        try:
            json.dumps(_nest(5_000))
        except RecursionError:
            return True
        return False

    if not with_small_budget(probe):  # pragma: no cover - CPython 3.12 and later
        pytest.skip("this interpreter's json encoder does not consume Python frames")

    rendered = with_small_budget(lambda: runprov.show.to_yaml(_nest(5_000)))
    assert runprov.show.YAML_TOO_DEEP in rendered, "it must SAY it stopped, not stop silently"
    assert yaml.safe_load(rendered) is not None, "and what it does emit still has to parse"


def test_binary_formats_are_still_refused_because_the_MODE_is_wrong(tmp_path):
    """Not about the pin at all: this method opens in text mode, so a caller cannot write a
    PNG or a BAM through the handle whatever happens to the provenance. The message points
    at the two calls that do work."""
    proj = _project(tmp_path)
    run = runprov.Run("s", project=proj)
    for name in (
        "x.bam",
        "x.cram",
        "x.parquet",
        "x.h5",
        "x.npy",
        "x.xlsx",
        "x.gz",
        "x.bgz",
        "x.zst",
        "x.zip",
        "x.png",
        "x.pdf",
    ):
        with pytest.raises(ValueError, match="cannot open") as caught:
            run.open_output(tmp_path / name)
        assert "run.output(path)" in str(caught.value)
        assert "run.pin_sidecar(p)" in str(caught.value)


def test_every_text_format_that_cannot_hold_a_pin_is_written_clean_with_a_sidecar(tmp_path):
    """One rule for all of them, so a format nobody thought about behaves like the ones
    that were. The artifact is byte-exact and the pin is beside it."""
    proj = _project(tmp_path)
    src = tmp_path / "in.tsv"
    src.write_text("x\n", encoding="utf-8")
    run = runprov.Run("s", project=proj)
    run.input(src)
    payload = "PAYLOAD\n"
    for name in (
        "x.fastq",
        "x.fq",
        "x.fasta",
        "x.fna",
        "x.vcf",
        "x.sam",
        "x.nwk",
        "x.svg",
        "x.xml",
        "x.html",
        "x.json",
        "x.jsonl",
        "x.geojson",
        "x.ipynb",
        "x.tex",
    ):
        with run.open_output(tmp_path / name) as fh:
            fh.write(payload)
        assert (tmp_path / name).read_text(encoding="utf-8") == payload, name
        assert (tmp_path / (name + runprov.run.PIN_SIDECAR_SUFFIX)).is_file(), name


def test_a_format_specific_marker_is_offered_only_when_the_caller_names_it(tmp_path, capsys):
    """`;` is the legacy Pearson FASTA comment. Measured: Biopython reads it without
    complaint and `samtools faidx` REJECTS the file outright. That is a real trade, so it is
    available and it is never made on the caller's behalf — the default stays the sidecar.

    A marker that does NOT help is not a way in. `##` is VCF's own meta-line marker and was
    measured to leave `bcftools` reporting `unknown file type`, so a VCF gets a sidecar
    however it is asked for.
    """
    proj = _project(tmp_path)
    src = tmp_path / "in.tsv"
    src.write_text("x\n", encoding="utf-8")
    run = runprov.Run("s", project=proj)
    run.input(src)

    with run.open_output(tmp_path / "legacy.fasta", comment="; ") as fh:
        fh.write(">seq1\nACGT\n")
    body = (tmp_path / "legacy.fasta").read_text(encoding="utf-8")
    assert body.startswith("; provenance"), "the caller asked for it, so it is in-band"
    assert not (tmp_path / "legacy.fasta.prov.txt").exists(), "and not also beside it"
    err = capsys.readouterr().err
    assert "`samtools faidx` REJECTS" in err, "the trade is stated at the moment it is made"

    # The default, and the VCF case: sidecar regardless of the marker offered.
    with run.open_output(tmp_path / "plain.fasta") as fh:
        fh.write(">seq1\nACGT\n")
    assert (tmp_path / "plain.fasta.prov.txt").is_file()
    with run.open_output(tmp_path / "calls.vcf", comment="## ") as fh:
        fh.write("##fileformat=VCFv4.2\n")
    assert (tmp_path / "calls.vcf").read_text(encoding="utf-8").startswith("##fileformat")
    assert (tmp_path / "calls.vcf.prov.txt").is_file()


def test_pin_sidecar_is_callable_for_a_file_another_library_wrote(tmp_path):
    """The route for a figure or a BAM: `output()` registers it, the library writes it, and
    the pin goes beside it. This is what the binary refusal points at."""
    proj = _project(tmp_path)
    src = tmp_path / "in.tsv"
    src.write_text("x\n", encoding="utf-8")
    with runprov.Run("plot", project=proj, provenance=tmp_path / "p.json") as run:
        run.input(src)
        png = run.output(tmp_path / "panel.png")
        png.write_bytes(b"\x89PNG\r\n\x1a\n binary bytes")
        side = run.pin_sidecar(png)

    assert side == tmp_path / "panel.png.prov.txt"
    assert (tmp_path / "panel.png").read_bytes().startswith(b"\x89PNG"), "artifact untouched"
    assert "in.tsv" in side.read_text(encoding="utf-8")
    rec = json.loads((tmp_path / "p.json").read_text(encoding="utf-8"))
    assert sorted(pathlib.Path(o["path"]).name for o in rec["outputs"]) == [
        "panel.png",
        "panel.png.prov.txt",
    ]


def test_write_json_puts_the_pin_in_the_document_as_structure(tmp_path):
    """JSON has no comments but it has structure, and a top-level key is a place every
    parser will read and none will choke on. It cannot go through a file handle — it means
    serialising the whole document — so it is its own method, and opt-in because it changes
    the caller's schema."""
    proj = _project(tmp_path)
    src = tmp_path / "in.tsv"
    src.write_text("x\n", encoding="utf-8")
    with runprov.Run("s", project=proj, provenance=tmp_path / "p.json") as run:
        run.input(src)
        out = run.write_json(tmp_path / "summary.json", {"variants": 12, "sample": "A"})

    doc = json.loads(out.read_text(encoding="utf-8"))
    assert doc["variants"] == 12 and doc["sample"] == "A", "the payload is untouched"
    pin = doc["_provenance"]
    assert pin["script"] == "s"
    # STRUCTURE, not prose: a consumer reading digests must not have to parse a comment.
    assert pin["inputs"] == [[runprov.hashing.pin_digest(run.record["inputs"][0]), "in.tsv"]]


def test_write_json_refuses_to_restructure_or_overwrite(tmp_path):
    """A JSON array has nowhere to put a key, and wrapping it in an object would change what
    the document IS rather than annotate it. An existing key is the caller's."""
    proj = _project(tmp_path)
    run = runprov.Run("s", project=proj)
    with pytest.raises(TypeError, match="needs a mapping"):
        run.write_json(tmp_path / "a.json", [1, 2, 3])
    with pytest.raises(ValueError, match="already in the payload"):
        run.write_json(tmp_path / "b.json", {"_provenance": "mine"})


def test_open_output_still_pins_the_formats_that_can_hold_one(tmp_path):
    """The guard must not become a reason to stop pinning. A `#` block is valid in these,
    and GFF/GTF is the one bioinformatics format where the pin genuinely works."""
    proj = _project(tmp_path)
    src = tmp_path / "in.tsv"
    src.write_text("x\n", encoding="utf-8")
    run = runprov.Run("s", project=proj)
    run.input(src)
    # NO `.json` HERE. It sat in this list asserting that a JSON file can carry a `#` pin,
    # which is false -- the suite was pinning the corruption in place. YAML, TOML and INI
    # are the text formats where `#` really IS a comment.
    for name in ("a.tsv", "a.csv", "a.txt", "a.md", "a.gff3", "a.gtf", "a.bed", "a.yaml"):
        with run.open_output(tmp_path / name) as fh:
            fh.write("payload\n")
        assert "provenance" in (tmp_path / name).read_text(encoding="utf-8"), name
        assert not (tmp_path / (name + ".prov.txt")).exists(), f"{name}: in-band, so no sidecar"


# ============================ a kill is how a long run actually ends, and it recorded nothing
_KILLED_SCRIPT = """
import pathlib, sys, time, runprov
root = pathlib.Path(sys.argv[1])
proj = runprov.Project(root=root, run_log=root / "runs.jsonl",
                       run_id=lambda: "r", generation=lambda: "g")
with runprov.Run("slow", {"n": 1}, project=proj, provenance=root / "p.json") as run:
    src = root / "in.tsv"
    src.write_text("id\\n1\\n", encoding="utf-8")
    run.input(src)
    run.output(root / "never.tsv")
    run.note("started", True)
    print("READY", flush=True)
    time.sleep(60)
"""


@pytest.mark.skipif(not hasattr(signal, "SIGTERM"), reason="POSIX signal needed")
def test_a_sigterm_records_the_run_instead_of_vanishing(tmp_path):
    """SLURM's time limit, `scancel` and `docker stop` are all SIGTERM, and every one of
    them used to leave no sidecar and no history line — a run indistinguishable from one
    that never started, which is the silence this package exists to end.

    A real process, really signalled: the mechanism is a signal handler and a fake cannot
    show that the handler reaches `__exit__` through the interpreter's own delivery.
    """
    script = tmp_path / "slow.py"
    script.write_text(_KILLED_SCRIPT, encoding="utf-8")
    proc = subprocess.Popen(
        [sys.executable, str(script), str(tmp_path)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env={**os.environ, "PYTHONPATH": str(REPO)},
    )
    try:
        assert proc.stdout is not None
        assert proc.stdout.readline().strip() == "READY", "the run must be open before we kill"
        proc.send_signal(signal.SIGTERM)
        proc.wait(timeout=30)
    finally:
        proc.kill()

    rec = json.loads((tmp_path / "p.json").read_text(encoding="utf-8"))
    assert rec["status"] == "failed"
    assert rec["failure"]["type"] == "Terminated"
    assert "SIGTERM" in rec["failure"]["message"]
    assert rec["notes"] == {"started": True}, "everything up to the signal is still recorded"
    assert rec["inputs"][0]["sha256"], "the input it had read"
    assert rec["outputs"][0]["kind"] == "MISSING", "and the output it never got to write"
    assert rec["signals"]["SIGTERM"] == "armed"

    history = (tmp_path / "runs.jsonl").read_text(encoding="utf-8").strip().splitlines()
    assert len(history) == 1 and json.loads(history[0])["status"] == "failed"


def test_a_termination_is_not_swallowed_by_a_broad_except(tmp_path):
    """`except Exception:` around a step is ordinary. If it caught a termination the step
    would report success for work the OS had already stopped, so `Terminated` sits outside
    `Exception` exactly as `KeyboardInterrupt` does."""
    assert issubclass(runprov.Terminated, BaseException)
    assert not issubclass(runprov.Terminated, Exception)

    caught = None
    try:
        try:
            raise runprov.Terminated(signal.SIGTERM)
        except Exception:
            caught = "swallowed"
    except BaseException as exc:
        caught = type(exc).__name__
    assert caught == "Terminated"
    assert runprov.Terminated(signal.SIGTERM).signum == signal.SIGTERM


def test_it_will_not_take_a_signal_handler_the_caller_installed(tmp_path):
    """A script with its own SIGTERM handler has decided what termination means for it.
    Overriding that to improve a log would be provenance changing the run it observes."""
    original = signal.getsignal(signal.SIGTERM)
    signal.signal(signal.SIGTERM, lambda *a: None)
    try:
        with runprov.Run("s", project=_project(tmp_path), provenance=tmp_path / "p.json") as run:
            assert run.record["signals"]["SIGTERM"] == "not armed — the caller has its own handler"
        assert signal.getsignal(signal.SIGTERM) is not signal.SIG_DFL, "theirs is untouched"
    finally:
        signal.signal(signal.SIGTERM, original)


def test_the_handler_is_removed_when_the_block_ends(tmp_path):
    """Left installed, a signal arriving during teardown would raise INSIDE `_finish` —
    the mechanism for recording a termination would be what lost the record."""
    assert signal.getsignal(signal.SIGTERM) is signal.SIG_DFL, "test precondition"
    with runprov.Run("s", project=_project(tmp_path), provenance=tmp_path / "p.json") as run:
        # `==`, not `is`: each access to a bound method builds a new object.
        assert signal.getsignal(signal.SIGTERM) == run._on_signal
    assert signal.getsignal(signal.SIGTERM) is signal.SIG_DFL


def test_a_run_in_a_worker_thread_says_it_could_not_arm_rather_than_dying(tmp_path):
    """`signal.signal` is main-thread-only. Provenance must not be the reason a worker
    dies, and the record states the gap rather than implying coverage."""
    seen = {}

    def work():
        with runprov.Run("w", project=_project(tmp_path), provenance=tmp_path / "w.json") as run:
            seen.update(run.record["signals"])

    t = threading.Thread(target=work)
    t.start()
    t.join(timeout=30)
    assert seen["SIGTERM"] == "not armed — not the main thread"
    assert json.loads((tmp_path / "w.json").read_text(encoding="utf-8"))["status"] == "ok"


def test_a_signal_absent_on_the_platform_is_recorded_as_absent(tmp_path, monkeypatch):
    """SIGHUP does not exist on Windows. 'absent' and 'not armed' are different facts and
    the record keeps them apart."""
    monkeypatch.delattr(signal, "SIGHUP", raising=False)
    with runprov.Run("s", project=_project(tmp_path), provenance=tmp_path / "p.json") as run:
        assert run.record["signals"]["SIGHUP"] == "absent on this platform"


@pytest.mark.skipif(not hasattr(signal, "SIGTERM"), reason="POSIX signal needed")
def test_the_handler_raises_into_the_block_it_guards(tmp_path):
    """The subprocess test above proves the whole path end to end, but it runs in another
    interpreter. This one signals THIS process, so the raise is exercised where it can be
    seen: delivered by the interpreter, unwinding a real `with`, into a real `__exit__`.
    """
    assert signal.getsignal(signal.SIGTERM) is signal.SIG_DFL, "test precondition"
    with pytest.raises(runprov.Terminated) as caught:
        with runprov.Run("s", project=_project(tmp_path), provenance=tmp_path / "p.json") as run:
            run.note("reached", True)
            os.kill(os.getpid(), signal.SIGTERM)
            time.sleep(1)  # the raise lands at the next bytecode boundary, not here

    assert caught.value.signum == signal.SIGTERM and caught.value.name == "SIGTERM"
    rec = json.loads((tmp_path / "p.json").read_text(encoding="utf-8"))
    assert rec["status"] == "failed" and rec["failure"]["type"] == "Terminated"
    assert rec["notes"] == {"reached": True}
    assert signal.getsignal(signal.SIGTERM) is signal.SIG_DFL, "and it cleaned up after itself"


def test_content_digest_streams_files_whose_LINES_are_long(tmp_path):
    """The sibling of the scale-invariance test above, and the case it could not see.

    That one triples the LINE COUNT at a fixed line length; a block bounded only by lines
    passes it while remaining unbounded in bytes. The shape that defeats such a block is not
    a big file but a file with few big lines — an unwrapped FASTA (`seqtk seq -l0`, most
    assemblers, any single-sequence download), a minified JSON, a one-line dump. Measured
    before the byte bound, on 8,192 contigs of ~30 kb: **607 MB peak for a 246 MB file**.
    Under a SLURM memory cgroup that is an OOM kill inside provenance capture — and an OOM
    kill is SIGKILL, the one ending nothing can record.

    Same method as its sibling, for the same reason: comparing two sizes cancels out the
    per-object constant that made an absolute threshold a test of one machine.
    """
    import tracemalloc

    def peak_for(records: int, width: int) -> tuple[int, int]:
        p = tmp_path / f"g{records}x{width}.fa"
        with open(p, "w", encoding="utf-8") as fh:
            for i in range(records):
                fh.write(f">contig{i}\n" + "ACGT" * width + "\n")
        tracemalloc.start()
        try:
            assert runprov.content_digest(p)
            _, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
        return peak, p.stat().st_size

    # Same line COUNT, four times the line LENGTH: only a byte bound can hold the peak.
    small_peak, small_size = peak_for(2000, 2_000)
    large_peak, large_size = peak_for(2000, 8_000)

    assert large_size > small_size * 3, "the two fixtures must actually differ in size"
    assert large_peak < small_peak * 1.5, (
        f"peak grew with the LINE LENGTH — {small_peak / 1e6:.1f} MB for "
        f"{small_size / 1e6:.1f} MB vs {large_peak / 1e6:.1f} MB for {large_size / 1e6:.1f} "
        f"MB. A block bounded only by line count is unbounded in bytes."
    )


def test_the_byte_bound_does_not_move_any_digest(tmp_path):
    """The bound is a memory change and must not be a FORMAT change. A digest that moved
    would re-pin every artifact at once — the '80 artifacts CHANGED' failure this module
    exists to prevent, arriving through an optimisation.

    Pinned by construction rather than by a stored constant: the same bytes hashed at four
    block sizes, including one smaller than a single line, must agree.
    """
    p = tmp_path / "mixed.txt"
    p.write_text(
        "# built_utc: 2026-01-01T00:00:00Z\n"
        + "short\n\n"
        + "x" * 40_000
        + "\n"
        + "".join(f"row{i}\tv{i}\n" for i in range(5_000))
        + "tail-without-newline",
        encoding="utf-8",
    )
    digests = set()
    for chunk_bytes in (1 << 23, 1 << 16, 4096, 64):
        with open(p, encoding="utf-8") as fh:
            digests.add(
                runprov.hashing._stream_digest(
                    fh, is_json=False, chunk_lines=8192, chunk_bytes=chunk_bytes
                )
            )
    assert len(digests) == 1, f"the block size changed the digest: {digests}"
    assert runprov.content_digest(p) in digests


# ===================== a symlinked data directory is the normal layout, not a foreign file
@requires_symlinks
def test_a_symlinked_directory_inside_the_root_pins_as_repository_data(tmp_path):
    """`data/ -> /mnt/bigdisk/data` is the standard bioinformatics layout, and every
    Nextflow or Snakemake work directory stages its inputs as symlinks.

    `resolve()` followed every link, so real repository data pinned as `<external>/x.tsv` —
    wrong twice: it announces a file as foreign to the repository holding it, and
    `<external>/` is deliberately not a path, so `verify` calls it UNVERIFIABLE. Those
    inputs were unpinnable AND uncheckable.
    """
    root, big = tmp_path / "repo", tmp_path / "bigdisk"
    (root / "results").mkdir(parents=True)
    big.mkdir()
    (big / "reads.tsv").write_text("id\tseq\n1\tACGT\n", encoding="utf-8")
    (root / "data").symlink_to(big, target_is_directory=True)

    proj = runprov.Project(root=root, run_log=root / "runs.jsonl", run_id=lambda: "r")
    with runprov.Run("s", project=proj, provenance=root / "p.json") as run:
        run.input(root / "data" / "reads.tsv")
        with run.open_output(root / "results" / "out.tsv") as fh:
            fh.write("done\n")

    pin = (root / "results" / "out.tsv").read_text(encoding="utf-8")
    assert "data/reads.tsv" in pin, pin
    assert "<external>" not in pin, "repository data must not pin as foreign"

    # And it is now checkable, which is the half that `<external>/` made impossible.
    rep = runprov.verify.verify([root / "results"], root)
    assert rep["ok"] == 1 and rep["unverifiable"] == 0


@requires_symlinks
def test_a_root_reached_through_a_link_still_resolves(tmp_path):
    """The other direction, and why `resolve()` is kept as the second attempt: macOS `/tmp`
    is `/private/tmp`, and plenty of clusters mount home directories through a link. There
    the spelled path is not under the spelled root and only resolving finds the relation."""
    real = tmp_path / "real_root"
    (real / "data").mkdir(parents=True)
    (real / "data" / "in.tsv").write_text("x\n", encoding="utf-8")
    link = tmp_path / "via_link"
    link.symlink_to(real, target_is_directory=True)

    # The project names the LINK; the input is given by its REAL path.
    proj = runprov.Project(root=link, run_log=link / "runs.jsonl", run_id=lambda: "r")
    run = runprov.Run("s", project=proj)
    run.input(real / "data" / "in.tsv")
    assert "data/in.tsv" in run.header() and "<external>" not in run.header()


def test_a_path_that_is_genuinely_outside_still_pins_as_external(tmp_path):
    """The guard must not turn `<external>` into a category nothing reaches: a file outside
    the root is a real thing to record, and the pin says so rather than inventing a name."""
    root = tmp_path / "repo"
    root.mkdir()
    outside = tmp_path / "elsewhere.tsv"
    outside.write_text("x\n", encoding="utf-8")
    proj = runprov.Project(root=root, run_log=root / "runs.jsonl", run_id=lambda: "r")
    run = runprov.Run("s", project=proj)
    run.input(outside)
    assert "<external>/elsewhere.tsv" in run.header()


@requires_symlinks
def test_a_dotdot_through_a_link_declines_the_cheap_answer(tmp_path):
    """`link/../x` normalises to the parent of the LINK, while on disk it means the parent
    of its TARGET. The cheap normalisation would name a file that is not the one hashed, so
    a path containing `..` skips it and only the resolved form is used."""
    root = tmp_path / "repo"
    (root / "sub").mkdir(parents=True)
    target = tmp_path / "target"
    (target / "deep").mkdir(parents=True)
    (tmp_path / "sibling.tsv").write_text("x\n", encoding="utf-8")
    (root / "sub" / "link").symlink_to(target / "deep", target_is_directory=True)

    proj = runprov.Project(root=root, run_log=root / "runs.jsonl", run_id=lambda: "r")
    run = runprov.Run("s", project=proj)
    # Spelled: repo/sub/link/../../sibling.tsv. Normalised that is repo/sibling.tsv, which
    # is NOT the file on disk — the real one sits beside `target`, outside the root.
    run.input(root / "sub" / "link" / ".." / ".." / "sibling.tsv")
    header = run.header()
    assert "<external>/sibling.tsv" in header, header
    assert "repo/sibling.tsv" not in header


@requires_symlinks
def test_a_symlink_loop_pins_as_external_instead_of_killing_the_run(tmp_path):
    """`resolve()` on a loop raises RuntimeError, which is NOT an OSError — so it was not
    caught here, escaped `_pin_name`, escaped `header()`, and killed the run at the moment
    it tried to describe itself.

    A loop is a misconfigured mount or a broken staging step: a fact about the inputs worth
    recording, not a reason to lose the record. It pins as external, which is exactly what
    "we could not place this under the root" means.
    """
    root, outside = tmp_path / "repo", tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    (outside / "a").symlink_to(outside / "b")
    (outside / "b").symlink_to(outside / "a")

    # The premise is VERSION-DEPENDENT — see `_assert_symlink_loop`. Asserting the
    # exception outright was wrong: 3.13 returns the path unchanged, so this test failed
    # there while the outcome below (the run survives, the path pins as external) held.
    _assert_symlink_loop(outside / "a" / "x.tsv")

    proj = runprov.Project(root=root, run_log=root / "runs.jsonl", run_id=lambda: "r")
    run = runprov.Run("s", project=proj)
    run.record["inputs"].append({"path": str(outside / "a" / "x.tsv"), "sha256": "0" * 64})
    assert "<external>/x.tsv" in run.header()


# ============== provenance must not change the program it observes: tracked packages
def _tracking(tmp_path, *mods):
    return runprov.Project(
        root=tmp_path,
        run_log=tmp_path / "runs.jsonl",
        run_id=lambda: "r",
        generation=lambda: "g",
        tracked_packages=mods,
    )


def test_constructing_a_run_does_not_import_the_packages_it_tracks(tmp_path):
    """`__import__(mod).__version__` imported numpy, pandas, scipy and sklearn whether or
    not the script used them, so the provenance object CHANGED THE PROGRAM IT OBSERVED.

    Not merely latency, which is the part that is easy to see: numpy and MKL fix their
    thread-pool configuration at import time and libraries install `warnings` filters at
    import time, so the run was measurably different because it was traced — and the record
    said nothing about that. Measured on one `Run()` in an env holding all four: 0.633 s and
    +135 MB RSS before, 0.336 s and +3 MB after, with identical recorded versions.

    A module whose import has a visible side effect stands in for all of them, because the
    side effect is the point rather than the identity of any particular package.
    """
    pkg = tmp_path / "sideeffecty"
    pkg.mkdir()
    (pkg / "__init__.py").write_text(
        "import builtins\nbuiltins._RUNPROV_SIDE_EFFECT = True\n__version__ = '9.9'\n",
        encoding="utf-8",
    )
    sys.path.insert(0, str(tmp_path))
    try:
        assert not hasattr(builtins, "_RUNPROV_SIDE_EFFECT"), "precondition"
        run = runprov.Run("s", project=_tracking(tmp_path, "sideeffecty"))
        assert not hasattr(builtins, "_RUNPROV_SIDE_EFFECT"), (
            "constructing a Run imported a tracked package and ran its module-level code"
        )
        assert "sideeffecty" not in sys.modules
        # Importable but neither imported nor an installed distribution, so there is
        # nothing to READ — and reading is now the only thing this is allowed to do.
        assert run.record["environment"]["packages"] == {"sideeffecty": None}
    finally:
        sys.path.remove(str(tmp_path))
        sys.modules.pop("sideeffecty", None)
        if hasattr(builtins, "_RUNPROV_SIDE_EFFECT"):
            del builtins._RUNPROV_SIDE_EFFECT


def test_an_already_imported_package_is_read_from_the_object_the_script_holds(tmp_path):
    """`sys.modules` first: if the script imported it, the object it actually has is the
    truth. An editable install, a `sys.path` shim or a vendored copy can disagree with any
    metadata — which is the whole reason `module()` exists."""
    mod = types.ModuleType("pretend_pkg")
    mod.__version__ = "3.2.1-from-the-object"
    sys.modules["pretend_pkg"] = mod
    try:
        run = runprov.Run("s", project=_tracking(tmp_path, "pretend_pkg"))
        assert run.record["environment"]["packages"] == {"pretend_pkg": "3.2.1-from-the-object"}
    finally:
        del sys.modules["pretend_pkg"]


def test_a_tracked_package_whose_distribution_has_another_name_is_still_found(tmp_path):
    """`sklearn` ships as `scikit-learn`, and it is the most-used tracked package in
    science. Looking the IMPORT name up in metadata would have reported it absent, so the
    module-to-distribution map is what makes reading a viable replacement for importing.

    A REAL `.dist-info` on `sys.path`, not a patched map, and not a package that happens to
    be installed here. The first version of this test used `_pytest` → `pytest` and passed
    while proving nothing: `_pytest` defines `__version__`, so it was answered from
    `sys.modules` and the map was never consulted. Depending on some dev-only dependency
    instead would make the test a statement about this machine's environment — a distro
    packager running `pytest` with only the `test` extra would not have it.
    """
    site = tmp_path / "site"
    info = site / "my_dist-4.5.6.dist-info"
    info.mkdir(parents=True)
    (info / "METADATA").write_text(
        "Metadata-Version: 2.1\nName: my-dist\nVersion: 4.5.6\n", encoding="utf-8"
    )
    # What maps the import name to the distribution name; `sklearn`'s case exactly.
    (info / "top_level.txt").write_text("mymod\n", encoding="utf-8")

    sys.path.insert(0, str(site))
    importlib.invalidate_caches()
    try:
        assert "mymod" not in sys.modules, "it must be resolved by the MAP, not by import"
        assert importlib.metadata.packages_distributions().get("mymod") == ["my-dist"]
        run = runprov.Run("s", project=_tracking(tmp_path, "mymod"))
        assert run.record["environment"]["packages"] == {"mymod": "4.5.6"}
    finally:
        sys.path.remove(str(site))
        importlib.invalidate_caches()


def test_an_absent_tracked_package_records_none_rather_than_failing_the_run(tmp_path):
    """None IS the record. A tracked package that is absent is a fact about the
    environment, and it must never abort somebody's run."""
    run = runprov.Run("s", project=_tracking(tmp_path, "no_such_package_anywhere", "runprov"))
    pkgs = run.record["environment"]["packages"]
    assert pkgs["no_such_package_anywhere"] is None
    assert pkgs["runprov"] == runprov.__version__, "and a name that IS a distribution resolves"


def test_unreadable_distribution_metadata_records_none_rather_than_raising(tmp_path, monkeypatch):
    """No metadata is a reason to record None, never a reason to fail the run being
    described — the same rule the rest of this module follows."""

    def boom():
        raise RuntimeError("metadata unreadable")

    monkeypatch.setattr(importlib.metadata, "packages_distributions", boom)
    run = runprov.Run("s", project=_tracking(tmp_path, "no_such_package_anywhere", "runprov"))
    pkgs = run.record["environment"]["packages"]
    assert pkgs["no_such_package_anywhere"] is None
    # With no map, the import name is tried as a distribution name directly, which is why
    # losing the map degrades the answer rather than the run.
    assert pkgs["runprov"] == runprov.__version__


# ================================ the shipped example must actually run, or it is a lie
def test_the_shipped_example_runs_and_produces_a_verifiable_artifact(tmp_path):
    """`examples/summarise.py` is the only COMPLETE script the package ships, so it is what
    a reader runs first. An example that has quietly stopped working teaches the reader that
    the package does not work — and nothing else in the suite would notice, because every
    other test builds its own fixtures rather than using the one users copy.

    Copied to a temp directory and run as a SUBPROCESS: the example configures the project
    root as its own parent, so running it in place would write a history into this
    repository, and importing it would not exercise the `__main__` path a reader uses.
    """
    shutil.copytree(REPO / "examples", tmp_path / "examples")
    proc = subprocess.run(
        [sys.executable, str(tmp_path / "examples" / "summarise.py")],
        capture_output=True,
        text=True,
        env={**os.environ, "PYTHONPATH": str(REPO)},
        cwd=tmp_path,
    )
    assert proc.returncode == 0, proc.stderr
    assert "kept 3 of 4 rows" in proc.stdout

    out = tmp_path / "examples" / "results" / "summary.tsv"
    body = out.read_text(encoding="utf-8")
    assert body.startswith("# provenance"), "the artifact must carry its own pin"
    assert "examples/data/measurements.tsv" in body, "and name what it was made from"

    # The default history path is the one the CLI reads with no --log, which is the whole
    # reason the example does not set `run_log=`.
    history = tmp_path / "provenance" / "runs.jsonl"
    assert history.is_file(), "the example must write where `runprov log` looks by default"
    rec = json.loads(history.read_text(encoding="utf-8").strip())
    assert rec["status"] == "ok"
    assert rec["notes"] == {
        "columns": ["sample", "value"],
        "rows_read": 4,
        "rows_kept": 3,
    }, "the example records the schema it wrote — see the note() convention in the README"
    assert rec["parameters"]["threshold"] == 5, "argparse types must survive into the record"

    report = runprov.verify.verify([out], tmp_path)
    assert report["ok"] == 1 and report["stale"] == 0


def test_the_front_page_block_runs_as_printed(tmp_path):
    """L-71. The README says of the block on its front page: "It is also shipped as
    `examples/summarise.py`, and a test runs it, so it cannot quietly stop working." The
    test ran the SHIPPED FILE, which is a different file — a fuller variant with `main()`,
    repo-relative defaults and an extra note. So the guarantee was made about the thirty
    lines the reader is looking at and held for a file they are not.

    Rather than forcing the two to be one file — the paste-and-run block and the complete
    example are not the same document — this executes the block ITSELF, extracted from the
    README, in a directory with nothing else in it. The sentence is now true of both."""
    readme = (_repo_root() / "README.md").read_text(encoding="utf-8")
    block = re.search(r"```python\n(.*?)```", readme, re.S)
    assert block, "the front-page python block is gone; the claim above it must go too"
    src = block.group(1)
    assert "with Run(" in src and "run.open_output(" in src, (
        f"the first python block is no longer the whole script: {src[:120]!r}"
    )
    (tmp_path / "summarise.py").write_text(src, encoding="utf-8")
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "measurements.tsv").write_text(
        "sample\tvalue\na\t9\nb\t2\nc\t7\nd\t5\n", encoding="utf-8"
    )

    proc = subprocess.run(
        [sys.executable, str(tmp_path / "summarise.py")],
        capture_output=True,
        text=True,
        env={**os.environ, "PYTHONPATH": str(REPO)},
        cwd=tmp_path,
    )
    assert proc.returncode == 0, proc.stderr

    # The three claims the block makes about itself, in the order it makes them.
    out = tmp_path / "results" / "summary.tsv"
    assert out.is_file(), "`open_output` must create the directory the block never makes"
    body = out.read_text(encoding="utf-8")
    assert body.startswith("# provenance"), "the artifact carries its own pin"
    assert "data/measurements.tsv" in body, "and names what it was made from"
    assert len(body.strip().splitlines()) - body.count("#") == 4, "header + the 3 kept rows"

    history = tmp_path / "provenance" / "runs.jsonl"
    assert history.is_file(), "`configure(root=...)` must write where the CLI looks"
    rec = json.loads(history.read_text(encoding="utf-8").strip())
    assert rec["status"] == "ok"
    assert rec["notes"] == {"rows_read": 4, "rows_kept": 3}
    assert (tmp_path / "results" / "summary.prov.json").is_file(), "provenance= writes a sidecar"


def test_the_cli_answers_version_which_is_the_first_thing_a_bug_report_asks(capsys):
    """`runprov --version` exited 2 with "the following arguments are required: cmd",
    because the subcommand was required before anything could be parsed.

    Read from the package rather than from installed metadata, so a source checkout that
    was never `pip install`ed still answers — which is the case a contributor is in.
    """
    with pytest.raises(SystemExit) as exit_code:
        runprov.__main__.main(["--version"])
    assert exit_code.value.code == 0
    assert capsys.readouterr().out.strip() == f"runprov {runprov.__version__}"


def test_nothing_is_tracked_until_the_project_asks_for_it(tmp_path):
    """The default was the source project's ML stack, so every record of every OTHER kind
    of run carried `{"numpy": null, "pandas": null, "scipy": null, "sklearn": null}` —
    forever, on every history line. Each null is truthful, and nobody asked the question.

    A field populated by assumption rather than by observation is prose provenance one
    level down, which is the thing this package exists to replace.
    """
    assert runprov.DEFAULT_TRACKED == ()
    run = runprov.Run("s", project=_tracking(tmp_path))
    assert run.record["environment"]["packages"] == {}

    # The facts that ARE universal were never in this list and are still unconditional.
    env = run.record["environment"]
    assert env["python"] and env["platform"] and "hostname" in env

    # And asking is one argument, with `None` still meaning "asked for, not present".
    # TWO absent names, deliberately: the module-to-distribution map is built once per run
    # and reused, and with a single absent package that reuse never happens. The old ML-stack
    # default had four names and covered it by accident, which is not coverage.
    asked = runprov.Run(
        "s", project=_tracking(tmp_path, "runprov", "definitely_not_here", "nor_is_this_one")
    )
    assert asked.record["environment"]["packages"] == {
        "runprov": runprov.__version__,
        "definitely_not_here": None,
        "nor_is_this_one": None,
    }


# ============ a warning that fires forever on a condition nobody can change is ignored
def test_not_being_a_repository_is_said_once_and_briefly(tmp_path, capsys, monkeypatch):
    """Two situations printed the same four-line alarm: a project simply not under version
    control, and a repository whose `git status` did not run. The first is how a great many
    people work and will be true of every run they ever make.

    Repeating an alarm on every run for a permanent condition the reader cannot act on is
    the permanently-red check this package refuses everywhere else — it trains people to
    stop reading warnings, including the ones that matter.
    """
    monkeypatch.setattr(runprov.run, "_NO_REPO_WARNED", False)
    assert not runprov.project.is_repository(tmp_path), "precondition: no repo here"

    proj = _project(tmp_path)
    for i in range(3):
        with runprov.Run(f"s{i}", project=proj, provenance=tmp_path / f"p{i}.json") as run:
            pass

    err = capsys.readouterr().err
    assert err.count("not a git repository") == 1, "once per process, not once per run"
    assert "PROVENANCE WARNING" not in err, "a way of working is a NOTE, not a warning"
    # The thing that must never be lost, in the note and in the record.
    assert "UNKNOWN rather than clean" in err
    assert run.record["code"]["git_status_captured"] is False
    assert run.record["code"]["git_commit"] is None


def test_a_repository_whose_git_did_not_run_stays_loud_every_time(tmp_path, capsys, monkeypatch):
    """The other half. A missing binary, a corrupt index or the 20 s timeout is a surprise,
    it is wrong right now, and quietening it would hide the one case worth shouting about.
    """
    (tmp_path / ".git").mkdir()  # a repository by the only check that does not need git
    assert runprov.project.is_repository(tmp_path)
    monkeypatch.setattr(runprov.project, "git", lambda *a, **k: None)

    proj = _project(tmp_path)
    for i in range(3):
        with runprov.Run(f"s{i}", project=proj, provenance=tmp_path / f"p{i}.json"):
            pass

    err = capsys.readouterr().err
    assert err.count("this IS a repository") == 3, "an anomaly is reported on every run"
    assert err.count("PROVENANCE WARNING") == 3


def test_is_repository_sees_a_worktree_and_a_parent_repository(tmp_path):
    """`.git` is a directory in an ordinary clone and a FILE in a worktree or submodule, and
    a project root can sit below the repository top level. Checked on the filesystem because
    the reason this is asked is usually that git did not work."""
    clone = tmp_path / "clone"
    (clone / "deep" / "nested").mkdir(parents=True)
    (clone / ".git").mkdir()
    assert runprov.project.is_repository(clone)
    assert runprov.project.is_repository(clone / "deep" / "nested"), "found by walking up"

    worktree = tmp_path / "worktree"
    worktree.mkdir()
    (worktree / ".git").write_text("gitdir: /elsewhere/.git/worktrees/wt\n", encoding="utf-8")
    assert runprov.project.is_repository(worktree), ".git as a FILE is still a repository"


def test_the_dirty_file_list_is_capped_and_says_how_many_it_left_out(tmp_path, capsys, monkeypatch):
    """The list was every dirty line, so a working tree with hundreds of modified files
    buried the sentence that matters under hundreds of lines of stderr. A silently shortened
    list is a different claim from a short one, so the remainder is counted — and the full
    set is in the record either way."""
    files = tuple(f" M src/mod{i}.py" for i in range(25))
    monkeypatch.setattr(
        runprov.run,
        "classify_status",
        lambda *a, **k: runprov.project.DirtyState(captured=True, code=files),
    )
    with runprov.Run("s", project=_project(tmp_path), provenance=tmp_path / "p.json") as run:
        pass

    err = capsys.readouterr().err
    shown = [ln for ln in err.splitlines() if ln.strip().startswith("M src/")]
    assert runprov.run.DIRTY_FILES_SHOWN == 10
    assert len(shown) == 10, "the terminal gets a readable number of lines"
    assert "… and 15 more (all of them are in the record)" in err
    assert len(run.record["code"]["git_dirty_code_files"]) == 25, "the record keeps them all"


def test_svg_and_json_are_written_clean_and_would_have_broken_in_band(tmp_path):
    """`.svg` and `.json` are TEXT, and that is what made them the trap: `open_output` used
    to write them happily and the result did not look damaged until something parsed it.
    Found by instrumenting a real matplotlib script that saves a figure as `.svg`.

    Asserted against what the PARSERS do rather than against a table, because the table is
    the thing under test: the artifact this method produces must parse, and the in-band pin
    it declines to write must not.
    """
    import xml.etree.ElementTree as ET

    proj = _project(tmp_path)
    src = tmp_path / "in.tsv"
    src.write_text("x\n", encoding="utf-8")
    run = runprov.Run("s", project=proj)
    run.input(src)

    cases = {
        "fig.svg": ('<svg xmlns="http://www.w3.org/2000/svg"><rect/></svg>\n', ET.parse),
        "data.json": ('{"a": 1}\n', lambda q: json.loads(pathlib.Path(q).read_text())),
    }
    for name, (payload, parse) in cases.items():
        with run.open_output(tmp_path / name) as fh:
            fh.write(payload)
        artifact = tmp_path / name
        assert artifact.read_text(encoding="utf-8") == payload, "byte-exact"
        parse(artifact)  # must not raise
        assert (tmp_path / (name + runprov.run.PIN_SIDECAR_SUFFIX)).is_file()

        # What the sidecar buys: the same bytes with the pin in them do not parse.
        broken = tmp_path / f"broken_{name}"
        broken.write_text(run.header() + payload, encoding="utf-8")
        with pytest.raises(Exception):  # noqa: B017 - two libraries, two error types
            parse(broken)


def test_a_format_nobody_listed_gets_the_SAFE_outcome(tmp_path):
    """THE INVERSION, and the reason for it.

    The rule used to be "pin in-band unless the suffix is on a list of known-unsafe
    formats", so a format nobody had thought of got a `#` written into it. Three rounds of
    review each found another one nobody had thought of — Newick, where a pinned tree
    PARSED and came back with three phantom taxa; SVG and JSON, found by instrumenting a
    real plotting script; then pickle. Each fix added a row and left the default intact.

    A guard whose default is to corrupt is not a guard. The question is now "is this format
    known to take a `#` comment?", so anything unrecognised is written byte-exact with the
    pin beside it — safe for a format that has not been invented yet.
    """
    proj = _project(tmp_path)
    src = tmp_path / "in.tsv"
    src.write_text("x\n", encoding="utf-8")
    run = runprov.Run("s", project=proj)
    run.input(src)

    payload = "PAYLOAD\n"
    for name in ("a.weirdext", "a.qza2", "a.h5adx", "a.loomx", "a.no_such_format", "a"):
        assert pathlib.Path(name).suffix.lower() not in runprov.run.PIN_INLINE
        with run.open_output(tmp_path / name) as fh:
            fh.write(payload)
        assert (tmp_path / name).read_text(encoding="utf-8") == payload, f"{name}: byte-exact"
        assert (tmp_path / (name + runprov.run.PIN_SIDECAR_SUFFIX)).is_file(), name


def test_executable_scripts_are_not_pinned_in_band_even_though_hash_is_a_comment(tmp_path):
    """`#` IS a comment in Python and shell, and they are still not on the allowlist: their
    first line can be a shebang, and a pin above it stops the file being executable.
    "Is `#` a comment" is not the same question as "is line 1 free"."""
    proj = _project(tmp_path)
    src = tmp_path / "in.tsv"
    src.write_text("x\n", encoding="utf-8")
    run = runprov.Run("s", project=proj)
    run.input(src)
    for name in ("generated.py", "wrapper.sh", "tool.pl", "helper.rb"):
        with run.open_output(tmp_path / name) as fh:
            fh.write("#!/usr/bin/env python3\nprint('hi')\n")
        assert (tmp_path / name).read_text(encoding="utf-8").startswith("#!"), name
        assert (tmp_path / (name + runprov.run.PIN_SIDECAR_SUFFIX)).is_file(), name


def test_pickle_and_friends_are_refused_with_the_message_a_caller_needs(tmp_path):
    """Naming a binary format buys a better MESSAGE, not the protection — the allowlist
    already sends anything unrecognised to the sidecar. A caller holding a `pickle.dump`
    needs to be told they cannot write it through a text handle, not that the pin moved."""
    proj = _project(tmp_path)
    run = runprov.Run("s", project=proj)
    for name in (
        "model.pkl",
        "model.pickle",
        "model.joblib",
        "t.feather",
        "t.arrow",
        "d.sqlite",
        "x.nc",
        "x.mat",
        "i.tiff",
        "i.jpg",
        "m.pt",
        "m.safetensors",
        "a.h5ad",
        "a.rds",
    ):
        with pytest.raises(ValueError, match="cannot open") as caught:
            run.open_output(tmp_path / name)
        assert "run.pin_sidecar(p)" in str(caught.value), name


# =========== `show`: the notebook the history already contained, per run and per project
def _history(tmp_path, monkeypatch):
    """Two scripts, a chain, two versions of one input, and one failure."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "data").mkdir()
    (tmp_path / "out").mkdir()
    src = tmp_path / "data" / "in.tsv"
    proj = _project(tmp_path)

    for value in ("a", "b"):  # the SAME input path at two different digests
        src.write_text(f"id\tv\n1\t{value}\n", encoding="utf-8")
        with runprov.Run(
            "build", {"mode": value}, project=proj, provenance=tmp_path / "p.json"
        ) as r:
            r.input(src)
            with r.open_output(tmp_path / "out" / "mid.tsv") as fh:
                fh.write(f"{value}\n")
            r.note("rows", 1)

    with runprov.Run("report", {}, project=proj, provenance=tmp_path / "q.json") as r:
        r.input(tmp_path / "out" / "mid.tsv")
        (tmp_path / "out" / "final.txt").write_text("done\n", encoding="utf-8")
        r.output(tmp_path / "out" / "final.txt")

    with contextlib.suppress(ValueError):
        with runprov.Run("report", {}, project=proj, provenance=tmp_path / "r.json") as r:
            r.input(tmp_path / "out" / "mid.tsv")
            raise ValueError("boom")

    return [json.loads(x) for x in (tmp_path / "runs.jsonl").read_text().splitlines()]


def test_the_project_page_says_which_script_expects_which_input(tmp_path, monkeypatch):
    """The question three weeks in, and the one `log` and `lineage` do not answer: which
    script expects which input, and does the thing I need already exist?

    Built per SCRIPT rather than per run, because that is what the question is about. A file
    read at two different digests is two answers and both are worth seeing.
    """
    rows = _history(tmp_path, monkeypatch)
    view = runprov.show.project_view(rows)

    assert view["runs"] == 4
    assert sorted(view["scripts"]) == ["build", "report"]

    build = view["scripts"]["build"]
    assert build["runs"] == 2 and build["failed"] == 0
    versions = next(iter(build["inputs"].values()))
    assert len(versions) == 2, "one path read at two digests is two versions, not one"
    assert build["parameters"] == ["mode"] and build["note_keys"] == ["rows"]

    report = view["scripts"]["report"]
    assert report["runs"] == 2 and report["failed"] == 1, "a failed run is still a run"

    # The artifact index: what exists, and what made it.
    names = {pathlib.Path(p).name for p in view["artifacts"]}
    assert {"mid.tsv", "final.txt"} <= names
    made_by = {pathlib.Path(p).name: a["by"] for p, a in view["artifacts"].items()}
    assert made_by["mid.tsv"] == "build" and made_by["final.txt"] == "report"

    # L-52. LAST WRITER WINS, and until now nothing checked it: `mid.tsv` is written twice
    # and BOTH writes come from `build`, so `made_by` above reads the same either way and
    # first-writer-wins passed the whole suite.
    #
    # ON THE DIGEST, not on `when`. The two runs can finish inside one second, so their
    # timestamps may be equal and an assertion on them would be true whichever entry
    # survived — the `0 == 0` shape this audit keeps finding. The digests always differ,
    # because the runs wrote different bytes.
    entry = next(a for p, a in view["artifacts"].items() if pathlib.Path(p).name == "mid.tsv")
    writes = [runprov.show._short(o) for r in rows for o in r.get("outputs") or []
              if pathlib.Path(o["path"]).name == "mid.tsv"]  # fmt: skip
    assert len(writes) == 2 and writes[0] != writes[1], "the premise: two different writes"
    assert entry["digest"] == writes[-1], (
        "the index must name the run that produced what is on disk NOW; showing a superseded "
        "run's digest beside a file that no longer has those bytes is authoritative and wrong"
    )


def test_the_project_page_reports_when_a_script_first_and_last_ran(tmp_path):
    """L-56. `first` and `last` were never asserted, so `s["last"] = s["last"]` — making
    `last` permanently equal `first` — left the whole suite green. "When did this last run"
    is one of the few facts on the page a reader cannot get anywhere else in a glance, and
    a page that always shows the first run's time as the last is silently wrong on every
    line of it.

    SYNTHETIC RECORDS, with times chosen here. The `_history` fixture's runs complete inside
    one second and genuinely share a `started_utc` — measured — so on that fixture `first`
    and `last` are equal for the right reason, and any assertion comparing them is true
    whichever value survived. That is the `0 == 0` shape, and it is why this row existed."""
    rows = [
        {"script": "build", "status": "ok", "started_utc": "2026-01-01T00:00:00Z"},
        {"script": "other", "status": "ok", "started_utc": "2026-02-02T00:00:00Z"},
        {"script": "build", "status": "ok", "started_utc": "2026-03-03T00:00:00Z"},
        {"script": "build", "status": "ok", "started_utc": "2026-04-04T00:00:00Z"},
    ]
    view = runprov.show.project_view(rows)

    build = view["scripts"]["build"]
    assert build["first"] == "2026-01-01T00:00:00Z", "the first run of this script, not of the log"
    assert build["last"] == "2026-04-04T00:00:00Z", "the last run of this script"
    assert build["first"] != build["last"], "the premise: this fixture can tell them apart"

    # A script that ran once has first == last, which is correct and is NOT what the
    # mutation produces -- stating it stops a future fix from special-casing the wrong thing.
    once = view["scripts"]["other"]
    assert once["first"] == once["last"] == "2026-02-02T00:00:00Z"

    # And it reaches the page, which is where a reader meets it.
    page = runprov.show.render_project(view)
    assert "last 2026-04-04T00:00:00Z    first 2026-01-01T00:00:00Z" in page, page


def test_the_project_page_is_rendered_without_needing_a_second_document(tmp_path, monkeypatch):
    """One page. The point is not to open several files and reconstruct the history."""
    rows = _history(tmp_path, monkeypatch)
    text = runprov.show.render_project(runprov.show.project_view(rows))
    for expected in ("build", "report", "expects:", "writes:", "artifacts on record"):
        assert expected in text, expected
    assert "FAILED" in text, "a script with a failed run must say so on the project page"


def test_a_run_page_carries_what_a_person_asks_about_a_run(tmp_path, monkeypatch):
    rows = _history(tmp_path, monkeypatch)
    view = runprov.show.run_view(rows[0])
    text = runprov.show.render_run(view)
    for expected in ("started", "run_id", "parameters", "inputs (1)", "outputs", "notes"):
        assert expected in text, expected
    assert view["parameters"] == {"mode": "a"}
    assert view["inputs"][0]["digest"] and len(view["inputs"][0]["digest"]) == runprov.show.SHORT


def test_a_failed_run_page_leads_with_the_failure(tmp_path, monkeypatch):
    rows = _history(tmp_path, monkeypatch)
    failed = next(r for r in rows if r.get("status") == "failed")
    text = runprov.show.render_run(runprov.show.run_view(failed))
    assert "[FAILED]" in text and "ValueError" in text and "boom" in text


def test_show_finds_a_run_by_script_uid_run_id_or_artifact(tmp_path, monkeypatch):
    """Four things one argument can mean. A developer asking about `build` and one asking
    about `out/mid.tsv` are asking the same question and should not have to say which kind
    of name they are holding."""
    rows = _history(tmp_path, monkeypatch)
    uid = rows[0]["run_uid"]
    # FED A GENERATOR, not a list, for every one of the four kinds. `_show` hands `select`
    # the streaming reader, and the four-pass implementation was only ever tested with a
    # list -- so the guard that made it work with a generator could be deleted and the suite
    # stayed green while `show <uid>`, `show <run_id>` and `show <artifact>` all answered a
    # confident "nothing matches" for targets that exist. Three quarters of the command.
    assert len(runprov.show.select(iter(rows), "build")) == 2
    assert len(runprov.show.select(iter(rows), uid[:8])) == 1
    assert len(runprov.show.select(iter(rows), rows[0]["run_id"])) == 4, "run_id is a CHAIN id"
    assert len(runprov.show.select(iter(rows), "mid.tsv")) == 4, "written twice, read twice"
    assert runprov.show.select(iter(rows), "no_such_thing") == []


def test_select_takes_the_LAST_n_when_it_is_given_a_limit(tmp_path, monkeypatch):
    """`--limit` moved into `select` so a bucket cannot grow past it -- which only helps if
    it still means the same thing. Last N, because the last run is the state you are in."""
    rows = _history(tmp_path, monkeypatch)
    both = runprov.show.select(iter(rows), "build")
    one = runprov.show.select(iter(rows), "build", limit=1)
    # ON `parameters`, NOT `started_utc`. The two build runs finish inside one second, so the
    # timestamps are equal and an assertion on them is true whichever run survives -- the
    # `0 == 0` shape, which is what left `show --limit` unguarded in the first place. `mode`
    # is "a" then "b", so it can only pass for the right one.
    assert [r["parameters"]["mode"] for r in both] == ["a", "b"], "the premise"
    assert len(one) == 1 and one[0]["parameters"]["mode"] == "b"


def test_selecting_a_target_does_not_hold_the_records_that_do_not_match(tmp_path, monkeypatch):
    """The property the four-pass version claimed in a comment and did not have: it held the
    whole history to answer about one run -- 438 MB at 100,000 runs, and the same on the path
    where NOTHING matches, to print "nothing matches".

    ON WEAKREFS, NOT ON MEMORY. The obvious test measures `tracemalloc` peak at two history
    sizes, and the first draft of it did. It passed alone and FAILED under `--cov`, because
    coverage allocates per line executed and the walk is the thing being sized -- so the test
    was reading the instrumentation. Threading the bound to accommodate that would have made
    it a test of the harness, which is exactly the flaw the flaky timing test one section down
    has. A weakref answers the real question directly: is the record still reachable after
    `select` has walked past it? Deterministic, 200 records rather than 200,000, and it cannot
    be moved by an allocator, a coverage run or a loaded machine.
    """
    import gc
    import weakref

    class Rec(dict):  # plain dicts cannot be weak-referenced; a subclass can
        pass

    rows = _history(tmp_path, monkeypatch)
    seen: list[weakref.ref] = []
    held: list[int] = []

    def stream(n, script):
        """Counts, AT THE LAST YIELD, how many earlier records are still reachable.

        Measured from inside the walk deliberately. Checking after `select` returns proves
        nothing: `records = list(records)` binds a local that dies with the frame, so every
        record is collectable by then and the materialising version passes. The question is
        what is held WHILE the history is being walked, which is the only moment at which
        holding it costs anything.
        """
        for i in range(n):
            rec = Rec(rows[i % len(rows)], run_uid=f"{i:032x}", script=script)
            seen.append(weakref.ref(rec))
            yield rec
            del rec  # the generator's own reference; the consumer's is the one under test
            if i == n - 1:
                gc.collect()
                held.append(sum(1 for w in seen if w() is not None))

    assert runprov.show.select(stream(200, "not_the_target"), "target") == []
    # A streaming walk holds the record it is looking at and nothing behind it. Measured: 1.
    # The bound is 2 rather than 1 so that a consumer which happens to keep the previous
    # record alive for one more step is not a failure -- the finding this guards is 200, and
    # the distance between 2 and 200 is the whole point. Anything above a couple of records
    # means the walk is accumulating, which is the defect.
    assert held[0] <= 2, f"{held[0]} of 200 non-matching records held during the walk"

    # The other half, or the assertion above would pass on a `select` that returns nothing at
    # all: a record that DOES match is retained, because it is the answer.
    seen.clear()
    held.clear()
    got = runprov.show.select(stream(200, "target"), "target")
    assert len(got) == 200
    assert held == [200], "matches must be kept — they are what was asked for"


def test_the_yaml_view_quotes_every_scalar_so_a_typed_colon_cannot_break_it(tmp_path, monkeypatch):
    """The predecessor's log dies at line 14,554 of 24,300 on an unquoted `Note:` inside a
    hand-written description. A quoting rule with exceptions is correct until someone types
    a colon, so there are no exceptions."""
    yaml = pytest.importorskip("yaml")
    rows = _history(tmp_path, monkeypatch)
    rows[0]["notes"]["description"] = "Recomputed p_l/p_o. Note: qval and log2_fc are NOT modified"
    rows[0]["notes"]["worse"] = "a: b\n- item\n#c\t\"q\" 's' ---"

    rendered = runprov.show.to_yaml(runprov.show.project_view(rows))
    back = yaml.safe_load(rendered)
    assert back["runs"] == 4

    one = runprov.show.to_yaml([runprov.show.run_view(rows[0])])
    parsed = yaml.safe_load(one)
    assert parsed[0]["notes"]["description"].startswith("Recomputed")
    assert parsed[0]["notes"]["worse"] == rows[0]["notes"]["worse"]


def test_show_writes_nothing(tmp_path, monkeypatch):
    """A view that could alter what it displays is a view you have to trust, and the record
    is the thing being trusted."""
    rows = _history(tmp_path, monkeypatch)
    before = {p: p.read_bytes() for p in sorted(tmp_path.rglob("*")) if p.is_file()}
    runprov.show.render_project(runprov.show.project_view(rows))
    runprov.show.render_run(runprov.show.run_view(rows[0]))
    runprov.show.to_yaml(runprov.show.project_view(rows))
    after = {p: p.read_bytes() for p in sorted(tmp_path.rglob("*")) if p.is_file()}
    assert before == after


def test_show_cli_renders_both_pages_and_says_when_nothing_matches(tmp_path, monkeypatch, capsys):
    _history(tmp_path, monkeypatch)
    log = str(tmp_path / "runs.jsonl")

    assert runprov.__main__.main(["show", "--log", log]) == 0
    assert "project notebook" in capsys.readouterr().out

    # L-55. WHICH run, not just that one rendered. `"inputs (1)"` is true of either build
    # run, so `matched[: args.limit]` -- first-N instead of last-N -- passed. `log --limit`
    # has been properly guarded all along (`test_cli_limit_takes_the_last_n` asserts s4/s3 in
    # and s0 out); this was the untested half of the same flag.
    #
    # `--limit` is documented as "the last N matching runs", and the reason is stated:
    # "the last one is the state you are in". Showing the FIRST N hands a reader the oldest
    # run of a chain while the summary line says N of M -- a page that is wrong about which
    # run it is describing, with nothing on it to say so.
    assert runprov.__main__.main(["show", "build", "--log", log, "--limit", "1"]) == 0
    out = capsys.readouterr().out
    assert "inputs (1)" in out
    assert 'mode "b"' in out, f"--limit 1 rendered the FIRST build run, not the last: {out}"
    assert 'mode "a"' not in out, "only one run should be on the page"

    # Without the flag, both are there -- so the assertion above is about `--limit` and not
    # about the fixture only ever having one run to show.
    assert runprov.__main__.main(["show", "build", "--log", log]) == 0
    both = capsys.readouterr().out
    assert 'mode "a"' in both and 'mode "b"' in both

    assert runprov.__main__.main(["show", "--log", log, "--format", "yaml"]) == 0
    assert '"scripts"' in capsys.readouterr().out

    assert runprov.__main__.main(["show", "build", "--log", log, "--format", "yaml"]) == 0
    assert '"run"' in capsys.readouterr().out

    assert runprov.__main__.main(["show", "nope", "--log", log]) == 1
    err = capsys.readouterr().err
    assert "nothing in" in err and "script name, a run_uid prefix" in err


def test_the_same_run_renders_identically_from_the_record_and_from_the_history(tmp_path):
    """L-46. `runprov.to_yaml(run.record)` and `log --format yaml` are documented as "this
    same function" over one run and over a history. They disagreed: the live record rendered
    `git_commit: "?"` where the history rendered `git_commit: ""`, because the history line
    FLATTENS it from `code.git_commit_short` while the record keeps it nested.

    `script_file` had already taught the renderer this lesson and carries a both-shapes read
    with a comment explaining why; `git_commit` did not. Two views of one run disagreeing is
    the failure this renderer exists to prevent.

    COMPARES THE WHOLE RENDERING rather than the one field, so the next field with the same
    asymmetry fails here instead of being found by a reviewer."""
    proj = _project(tmp_path)
    (tmp_path / "in.tsv").write_text("id\n1\n", encoding="utf-8")
    with runprov.Run("s", {"m": "a"}, project=proj, provenance=tmp_path / "p.json") as run:
        run.input(tmp_path / "in.tsv")
        run.note("rows", 1)

    # AFTER the block, which is what `to_yaml`'s own docstring instructs: `__exit__` is where
    # outputs are hashed and the status becomes known.
    from_record = runprov.to_yaml(run.record)
    rows = [json.loads(x) for x in (tmp_path / "runs.jsonl").read_text().splitlines()]
    from_history = runprov.to_yaml(rows)

    body = lambda s: [ln for ln in s.splitlines() if ln.startswith(("- ", "  "))]  # noqa: E731
    assert body(from_record) == body(from_history), (
        "the same renderer, the same run, two sources — and they disagree:\n"
        + "\n".join(
            f"  record: {a}\n  history: {b}"
            # strict=False deliberately: when this FAILS the two may differ in length,
            # and a diagnostic that raises instead of printing is no diagnostic.
            for a, b in zip(body(from_record), body(from_history), strict=False)
            if a != b
        )
    )
    assert any("git_commit" in ln for ln in body(from_record)), "the field is actually rendered"


def test_the_yaml_emitter_handles_empty_and_scalar_shapes(tmp_path):
    """Small shapes it must not choke on, since a record can legitimately hold any of them."""
    yaml = pytest.importorskip("yaml")
    for obj in ({}, [], {"a": {}}, {"a": []}, [1, 2], "bare", 3, None, True, 1.5):
        rendered = runprov.show.to_yaml(obj)
        yaml.safe_load(rendered)  # must not raise
    assert yaml.safe_load(runprov.show.to_yaml({"a": [{"b": 1}]}))["a"][0]["b"] == 1


def test_the_views_survive_a_record_that_is_missing_almost_everything():
    """A renderer meets records written by OLDER versions, and by runs that did nothing.
    Every field here is optional in some real record, so every one is absent in this one.

    The history is append-only and never rewritten, which is exactly why a reader has to
    cope with the shapes it already contains rather than the shape it wishes for.
    """
    bare = {"script": "minimal", "status": "ok"}
    view = runprov.show.run_view(bare)
    text = runprov.show.render_run(view)
    assert "minimal" in text and "[ok]" in text
    for absent in ("script ", "cwd ", "command ", "seeds", "terminal"):
        assert absent not in text, f"{absent!r} has no value and must not be printed"

    project = runprov.show.render_project(runprov.show.project_view([bare]))
    assert "minimal" in project
    assert "expects:" not in project and "writes:" not in project
    assert "artifacts on record" not in project, "nothing was produced, so there is no index"


def test_a_run_page_shows_seeds_and_the_terminal_log_when_there_are_any():
    rec = {
        "script": "train",
        "status": "ok",
        "run_uid": "abcdef123456789",
        "script_file": "src/train.py",
        "cwd": "/w",
        "command": "python src/train.py",
        "seeds": [20250131],
        "terminal_log": {"path": "logs/train.log"},
        "git_code_dirty": True,
    }
    text = runprov.show.render_run(runprov.show.run_view(rec))
    assert "seeds" in text and "20250131" in text
    assert "logs/train.log" in text
    assert "DIRTY" in text, "a dirty tree is the most consequential line on the page"


def test_the_run_page_shows_a_usable_run_uid_handle():
    """L-75. `run_uid` on the run page was never asserted — neither that it appears nor how
    much of it. Two mutations survived the whole suite: truncating to `[:6]`, and never
    printing the line at all.

    The width is not cosmetic. `select()` accepts a PREFIX of the uid, so the page's job is
    to show a handle long enough to be unique — and it is also how a reader ties a page back
    to the `.prov.json` sidecar beside an artifact. Six hex characters is 24 bits: a
    thousand-run project has a coin-flip chance of a collision, and the page would be
    handing out an ambiguous identifier while looking exactly as authoritative.
    """
    uid = "abcdef123456789abc"
    view = runprov.show.run_view({"script": "train", "status": "ok", "run_uid": uid})
    text = runprov.show.render_run(view)

    assert view["run_uid"] == uid[:12], f"the view carries 12 characters, not {view['run_uid']!r}"
    assert "abcdef123456" in text, "the handle must be ON the page, not merely in the view"
    assert "abcdef1234567" not in text, "13 characters would be a different, wider handle"

    # The handle is a genuine prefix of the real uid, so `show <handle>` finds this run.
    assert uid.startswith(view["run_uid"])
    rows = [{"script": "train", "run_uid": uid}, {"script": "other", "run_uid": "999" * 6}]
    assert runprov.show.select(iter(rows), view["run_uid"]) == [rows[0]], (
        "the identifier the page prints must be one the reader can hand back to `show`"
    )


def test_many_versions_of_one_input_are_counted_rather_than_listed():
    """A script that has read forty versions of one file has a fact worth stating and a list
    not worth printing. The count is the finding."""
    many = [
        {
            "script": "s",
            "status": "ok",
            # The digest must differ in its FIRST characters: `_short` takes 16, and a
            # fixture varying only the tail collapses to one version and tests nothing.
            "inputs": [{"path": "data/in.tsv", "sha256": f"{i}" + "a" * 63}],
            "outputs": [],
        }
        for i in range(runprov.show.VERSIONS_SHOWN + 2)
    ]
    text = runprov.show.render_project(runprov.show.project_view(many))
    assert f"[{runprov.show.VERSIONS_SHOWN + 2} versions]" in text
    assert "0" + "a" * 15 not in text, "the digests are summarised, not listed"

    few = many[: runprov.show.VERSIONS_SHOWN]
    lean = runprov.show.render_project(runprov.show.project_view(few))
    assert "0" + "a" * 15 in lean, "a handful of versions IS worth naming"


def test_an_artifact_from_a_failed_run_is_flagged_on_the_project_page():
    """`MISSING` and a partial write both come from failed runs, and an index that lists
    them beside good artifacts without saying so is the reassuring answer again."""
    rec = {
        "script": "s",
        "status": "failed",
        "started_utc": "2026-01-01T00:00:00Z",
        "inputs": [],
        "outputs": [{"path": "out/half.tsv", "sha256": "a" * 64, "kind": "file"}],
    }
    text = runprov.show.render_project(runprov.show.project_view([rec]))
    assert "from a FAILED run" in text


# ============ the staleness column: "do I need to run this again", in one page
def _staleable(tmp_path, monkeypatch):
    """A project whose artifacts can each be pushed into a different state."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "data").mkdir()
    (tmp_path / "out").mkdir()
    src = tmp_path / "data" / "in.tsv"
    src.write_text("id\tv\n1\ta\n", encoding="utf-8")
    ref = tmp_path / "data" / "ref.tsv"
    ref.write_text("r\n", encoding="utf-8")
    proj = _project(tmp_path)

    with runprov.Run("build", project=proj, provenance=tmp_path / "out" / "mid.prov.json") as r:
        r.input(src)
        with r.open_output(tmp_path / "out" / "mid.tsv") as fh:
            fh.write("a\n")
    with runprov.Run("aux", project=proj, provenance=tmp_path / "out" / "aux.prov.json") as r:
        r.input(ref)
        (tmp_path / "out" / "aux.bin").write_bytes(b"\x00\x01")
        r.output(tmp_path / "out" / "aux.bin")
    with runprov.Run("temp", project=proj, provenance=tmp_path / "out" / "t.prov.json") as r:
        r.input(ref)
        (tmp_path / "out" / "gone.txt").write_text("x\n", encoding="utf-8")
        r.output(tmp_path / "out" / "gone.txt")

    return [json.loads(x) for x in (tmp_path / "runs.jsonl").read_text().splitlines()]


def _state_of(states, name):
    return next(v for k, v in states.items() if pathlib.Path(k).name == name)


def _rebuilt_from_a_different_input(tmp_path, monkeypatch):
    """One artifact path, written twice, by two runs reading DIFFERENT inputs.

    `_staleable` gives every artifact exactly one producing run, so the overwrite semantics
    of `staleness`'s producer map are never exercised there — which is L-53. This is the
    smallest history that can tell first-writer-wins from last-writer-wins.
    """
    monkeypatch.chdir(tmp_path)
    (tmp_path / "data").mkdir()
    (tmp_path / "out").mkdir()
    first = tmp_path / "data" / "first.tsv"
    second = tmp_path / "data" / "second.tsv"
    first.write_text("one\n", encoding="utf-8")
    second.write_text("two\n", encoding="utf-8")
    proj = _project(tmp_path)

    for name, src in (("build", first), ("rebuild", second)):
        with runprov.Run(
            name, project=proj, provenance=tmp_path / "out" / f"{name}.prov.json"
        ) as r:
            r.input(src)
            with r.open_output(tmp_path / "out" / "shared.tsv") as fh:
                fh.write(f"from {name}\n")

    return [json.loads(x) for x in (tmp_path / "runs.jsonl").read_text().splitlines()]


def test_staleness_checks_the_run_that_wrote_the_artifact_last(tmp_path, monkeypatch):
    """L-53. The same defect as L-52, one function along: `staleness`'s producer map is also
    last-writer-wins and was also unguarded, because `_staleable` gives every artifact
    exactly one producing run.

    First-writer-wins checks the artifact against the inputs of an OLD run, so a file rebuilt
    from new inputs reads `current` — the inputs of a superseded run have not moved, and they
    are not the ones it was made from. That is the reassuring lie the `?` state exists to
    avoid, arriving through the state that looks most trustworthy."""
    rows = _rebuilt_from_a_different_input(tmp_path, monkeypatch)
    assert _state_of(runprov.show.staleness(rows), "shared.tsv") == "current", "the premise"

    # The LATEST producer's input. Bigger, so the size check sees it without waiting a second.
    (tmp_path / "data" / "second.tsv").write_text("two, and then some more\n", encoding="utf-8")
    assert _state_of(runprov.show.staleness(rows), "shared.tsv") == "STALE", (
        "the artifact was made from second.tsv, and second.tsv moved"
    )


def test_a_superseded_runs_input_does_not_make_an_artifact_stale(tmp_path, monkeypatch):
    """The converse, and the sharper half: touching the input of the run that was REPLACED
    must change nothing. Without it, a test could pass by treating every historical input of
    a path as current — reporting STALE for work that is perfectly up to date, which sends a
    developer to rebuild something that does not need it."""
    rows = _rebuilt_from_a_different_input(tmp_path, monkeypatch)

    (tmp_path / "data" / "first.tsv").write_text("one, edited long afterwards\n", encoding="utf-8")
    assert _state_of(runprov.show.staleness(rows), "shared.tsv") == "current", (
        "first.tsv is not what the artifact on disk was made from"
    )


def test_the_artifact_index_says_which_artifacts_need_rebuilding(tmp_path, monkeypatch):
    """The question the page exists to answer in one glance, when a build costs hours.

    Answered from the HISTORY rather than from the in-artifact pin, which is why it works
    for `aux.bin` — a binary that could never hold a pin at all.
    """
    rows = _staleable(tmp_path, monkeypatch)
    assert set(runprov.show.staleness(rows).values()) == {"current"}

    time.sleep(1.1)  # mtime has one-second resolution; see `moved_since`
    (tmp_path / "data" / "in.tsv").write_text("id\tv\n1\tCHANGED\n", encoding="utf-8")
    (tmp_path / "out" / "gone.txt").unlink()
    (tmp_path / "out" / "aux.bin").write_bytes(b"hand edited, longer than before")

    states = runprov.show.staleness(rows)
    assert _state_of(states, "mid.tsv") == "STALE", "its input moved — rebuilding differs"
    assert _state_of(states, "gone.txt") == "GONE"
    assert _state_of(states, "aux.bin") == "MODIFIED", (
        "the ARTIFACT changed, not its inputs — a different repair, so a different word"
    )


def test_the_cheap_check_reads_no_input_bytes(tmp_path, monkeypatch):
    """A page consulted many times a day must not cost what the build costs. The default is
    one `stat` per input; only `--rehash` reads them."""
    rows = _staleable(tmp_path, monkeypatch)
    opened = []
    real_open = pathlib.Path.open

    def watched(self, *a, **k):
        opened.append(self)
        return real_open(self, *a, **k)

    monkeypatch.setattr(pathlib.Path, "open", watched)
    runprov.show.staleness(rows)
    names = {p.name for p in opened}
    assert "in.tsv" not in names and "ref.tsv" not in names, "no input was read"
    assert any(n.endswith(".prov.json") for n in names), "only the sidecars, for their stats"


def test_rehash_catches_what_a_stat_cannot(tmp_path, monkeypatch):
    """`moved_since` compares size and mtime, so a rewrite inside one second that preserves
    the byte count is invisible to it. That limit is documented rather than hidden, and
    `--rehash` is the answer for anyone who cannot accept it."""
    rows = _staleable(tmp_path, monkeypatch)
    src = tmp_path / "data" / "in.tsv"
    stat = src.stat()
    src.write_text("id\tv\n1\tZ\n", encoding="utf-8")  # SAME length as "1\ta\n"... no: force it
    src.write_bytes(b"id\tv\n1\tz\n")
    os.utime(src, (stat.st_atime, stat.st_mtime))  # and put the mtime back

    assert _state_of(runprov.show.staleness(rows), "mid.tsv") == "current", (
        "the stat check cannot see this, and says so by being documented, not by guessing"
    )
    assert _state_of(runprov.show.staleness(rows, rehash=True), "mid.tsv") == "STALE"


def test_an_artifact_whose_INPUT_is_gone_says_GONE_not_STALE(tmp_path, monkeypatch):
    """L-31. The existing `GONE` assertion deletes the ARTIFACT, so it is answered by the
    earlier `not target.exists()` branch and never reaches `GONE if why == "gone" else
    STALE` inside the input loop. That line was executed on every page and its VALUE was
    never observed: collapsing it to `STALE` left the suite green.

    The module docstring makes the distinction load-bearing — "a stale artifact is rebuilt,
    a gone input is FOUND" — and they are genuinely different repairs. Reported as STALE, the
    page tells you to rebuild an artifact whose input no longer exists, so the rebuild fails
    for a reason the page had the information to state."""
    rows = _staleable(tmp_path, monkeypatch)

    # The INPUT, with its artifact left exactly where it is.
    (tmp_path / "data" / "ref.tsv").unlink()
    assert (tmp_path / "out" / "aux.bin").is_file(), "the premise: the artifact is still there"

    states = runprov.show.staleness(rows)
    assert _state_of(states, "aux.bin") == "GONE", (
        "an input that is not there cannot be compared, and rebuilding will not find it"
    )
    assert _state_of(states, "mid.tsv") == "current", "the other artifact is unaffected"


def test_rehash_reports_MODIFIED_for_an_artifact_someone_rewrote(tmp_path, monkeypatch):
    """L-30. `MODIFIED` was asserted only on the STAT path — a different function — so
    `_by_digest`'s final line could return `CURRENT` unconditionally and every rehash test
    stayed green. That regression makes the THOROUGH check report `current` for a
    hand-edited artifact, while `--rehash` is documented as the answer for anyone who cannot
    accept the stat check's one-second resolution. A user reaching for the stricter mode
    would get a worse answer than the default, and be told it was stricter.

    STALE and MODIFIED are different repairs, which is why they are different words: a stale
    artifact is rebuilt from moved inputs, a modified one was overwritten by something that
    is not this pipeline and rebuilding it silently discards whatever that was."""
    rows = _staleable(tmp_path, monkeypatch)
    assert _state_of(runprov.show.staleness(rows, rehash=True), "mid.tsv") == "current", (
        "the premise: nothing has moved yet"
    )

    # The ARTIFACT, not its input. Its inputs are untouched, so `STALE` would be wrong.
    (tmp_path / "out" / "mid.tsv").write_text("EDITED BY HAND ENTIRELY\n", encoding="utf-8")
    assert _state_of(runprov.show.staleness(rows, rehash=True), "mid.tsv") == "MODIFIED"


def test_a_moved_input_outranks_a_rewritten_artifact_under_rehash(tmp_path, monkeypatch):
    """When both are true the input wins, and that ordering is the useful one: rebuilding
    fixes a moved input, and cannot fix an artifact something else is writing. Asserting it
    also stops `MODIFIED` being reported for an artifact that a rebuild would legitimately
    replace anyway."""
    rows = _staleable(tmp_path, monkeypatch)
    (tmp_path / "out" / "mid.tsv").write_text("EDITED BY HAND\n", encoding="utf-8")
    (tmp_path / "data" / "in.tsv").write_text("id\tv\n1\tCHANGED\n", encoding="utf-8")
    assert _state_of(runprov.show.staleness(rows, rehash=True), "mid.tsv") == "STALE"


def test_a_missing_or_overwritten_sidecar_reports_unknown_not_current(tmp_path, monkeypatch):
    """The stat fields live in the sidecar because the history line trims them. A sidecar
    that is gone, or that a later run has overwritten, cannot answer for THIS run — and
    `current` would be the reassuring lie the whole package refuses."""
    rows = _staleable(tmp_path, monkeypatch)
    (tmp_path / "out" / "mid.prov.json").unlink()
    assert _state_of(runprov.show.staleness(rows), "mid.tsv") == "?"

    # Overwritten by a different run: same path, different run_uid.
    doc = json.loads((tmp_path / "out" / "aux.prov.json").read_text(encoding="utf-8"))
    doc["run_uid"] = "a-completely-different-run"
    (tmp_path / "out" / "aux.prov.json").write_text(json.dumps(doc), encoding="utf-8")
    assert _state_of(runprov.show.staleness(rows), "aux.bin") == "?", (
        "digests from one run against stats from another would be confident nonsense"
    )


def test_a_registered_output_that_was_never_written_is_gone(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    proj = _project(tmp_path)
    with runprov.Run("s", project=proj, provenance=tmp_path / "p.json") as r:
        r.output(tmp_path / "never.tsv")
    rows = [json.loads(x) for x in (tmp_path / "runs.jsonl").read_text().splitlines()]
    assert _state_of(runprov.show.staleness(rows), "never.tsv") == "GONE"


def test_rehash_reports_unknown_when_an_input_cannot_be_read(tmp_path, monkeypatch):
    rows = _staleable(tmp_path, monkeypatch)
    (tmp_path / "data" / "in.tsv").unlink()
    assert _state_of(runprov.show.staleness(rows, rehash=True), "mid.tsv") == "?"


def test_show_cli_takes_stale_and_rehash_and_tallies_them(tmp_path, monkeypatch, capsys):
    rows = _staleable(tmp_path, monkeypatch)
    log = str(tmp_path / "runs.jsonl")
    time.sleep(1.1)
    (tmp_path / "data" / "in.tsv").write_text("id\tv\n1\tCHANGED\n", encoding="utf-8")

    assert runprov.__main__.main(["show", "--log", log, "--stale"]) == 0
    seen = capsys.readouterr()
    assert "STALE" in seen.out and "current" in seen.out
    assert "STALE" in seen.err, "the summary line tallies the states"

    assert runprov.__main__.main(["show", "--log", log, "--rehash", "--format", "yaml"]) == 0
    payload = capsys.readouterr().out
    assert '"state"' in payload and "STALE" in payload

    # Without the flag the page is instant and carries no column.
    assert runprov.__main__.main(["show", "--log", log]) == 0
    assert "STALE" not in capsys.readouterr().out
    assert len(rows) == 3


def test_a_record_with_no_sidecar_path_reports_unknown(tmp_path):
    """A run recorded before `provenance_path` existed, or one that never wrote a sidecar,
    cannot supply the stat fields — and there is nothing to fall back to but honesty."""
    rec = {
        "script": "s",
        "status": "ok",
        "cwd": str(tmp_path),
        "inputs": [{"path": "in.tsv", "sha256": "a" * 64}],
        "outputs": [{"path": "out.tsv", "sha256": "b" * 64, "kind": "file"}],
    }
    (tmp_path / "out.tsv").write_text("x\n", encoding="utf-8")
    assert runprov.show.staleness([rec]) == {"out.tsv": "?"}


def test_two_artifacts_from_one_run_read_that_run_s_sidecar_once(tmp_path, monkeypatch):
    """The sidecar is read per RUN, not per artifact. A step writing forty files must not
    open and parse the same JSON forty times on a page consulted many times a day."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "out").mkdir()
    src = tmp_path / "in.tsv"
    src.write_text("x\n", encoding="utf-8")
    with runprov.Run("many", project=_project(tmp_path), provenance=tmp_path / "p.json") as r:
        r.input(src)
        for i in range(3):
            with r.open_output(tmp_path / "out" / f"o{i}.tsv") as fh:
                fh.write("y\n")

    rows = [json.loads(x) for x in (tmp_path / "runs.jsonl").read_text().splitlines()]
    reads = []
    real = pathlib.Path.read_text

    def watched(self, *a, **k):
        reads.append(self.name)
        return real(self, *a, **k)

    monkeypatch.setattr(pathlib.Path, "read_text", watched)
    states = runprov.show.staleness(rows)
    assert len(states) == 3 and set(states.values()) == {"current"}
    assert reads.count("p.json") == 1, f"the sidecar was read {reads.count('p.json')} times"


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="POSIX FIFO needed to make one")
@requires_fifo
def test_rehash_reports_unknown_when_the_ARTIFACT_cannot_be_read(tmp_path, monkeypatch):
    """`describe` refuses a FIFO rather than blocking on it, and an artifact that cannot be
    hashed now is a question that cannot be answered — not an artifact that is current."""
    rows = _staleable(tmp_path, monkeypatch)
    aux = tmp_path / "out" / "aux.bin"
    aux.unlink()
    os.mkfifo(aux)
    assert _state_of(runprov.show.staleness(rows, rehash=True), "aux.bin") == "?"


# ================= a sidecar per run: the record beside the artifact is not overwritten
def test_a_sidecar_per_run_keeps_every_run_beside_the_artifact(tmp_path, monkeypatch):
    """`provenance=` names ONE path, so the tenth run leaves one sidecar and the nine
    before it are gone. The append-only history still holds all ten -- no RECORD is lost --
    but the file beside the artifact answers only for the last run, and "when did this
    column appear" is a question about the ones that were overwritten.
    """
    monkeypatch.chdir(tmp_path)
    (tmp_path / "out").mkdir()
    src = tmp_path / "in.tsv"
    src.write_text("id\n1\n", encoding="utf-8")
    proj = dataclasses.replace(_project(tmp_path), sidecar_per_run=True)

    for i in range(3):
        if i:
            time.sleep(1.05)  # the stamp has one-second resolution
        with runprov.Run(
            "build", {"pass": i}, project=proj, provenance=tmp_path / "out" / "summary.prov.json"
        ) as r:
            r.input(src)
            with r.open_output(tmp_path / "out" / "summary.tsv") as fh:
                fh.write(f"pass {i}\n")

    found = sorted((tmp_path / "out").glob("*.prov.json"))
    assert len(found) == 3, "three runs, three sidecars, none overwritten"

    passes = [json.loads(p.read_text(encoding="utf-8"))["parameters"]["pass"] for p in found]
    assert passes == [0, 1, 2], "sorted by NAME is sorted by TIME, which is why time is first"

    uids = {json.loads(p.read_text(encoding="utf-8"))["run_uid"] for p in found}
    assert len(uids) == 3, "each sidecar describes its own run"


def test_the_stamp_goes_before_the_whole_compound_suffix(tmp_path, monkeypatch):
    """`Path("summary.prov.json").suffix` is `.json` and its stem is `summary.prov`, so the
    obvious insertion gives `summary.prov.<stamp>.json` -- which no longer matches
    `*.prov.json`, the glob every reader uses to find these. Measured by writing three and
    watching the glob return nothing."""
    monkeypatch.chdir(tmp_path)
    proj = dataclasses.replace(_project(tmp_path), sidecar_per_run=True)
    with runprov.Run("s", project=proj, provenance=tmp_path / "a.prov.json") as run:
        pass
    name = run.provenance_path.name
    assert name.endswith(".prov.json"), name
    assert name.startswith("a.20"), name
    assert list(tmp_path.glob("*.prov.json")) == [run.provenance_path]

    # A plain single suffix still behaves.
    with runprov.Run("s", project=proj, provenance=tmp_path / "b.json") as run2:
        pass
    assert run2.provenance_path.name.endswith(".json")
    assert run2.provenance_path.name.startswith("b.20")


def test_without_the_flag_the_path_is_exactly_what_the_caller_named(tmp_path, monkeypatch):
    """Default OFF: a caller who names a path gets that path, and a Makefile or a downstream
    reader pointing at it keeps working."""
    monkeypatch.chdir(tmp_path)
    target = tmp_path / "fixed.prov.json"
    with runprov.Run("s", project=_project(tmp_path), provenance=target) as run:
        pass
    assert run.provenance_path == target and target.is_file()


def test_per_run_sidecars_make_staleness_answerable_for_older_runs(tmp_path, monkeypatch):
    """The reason this matters beyond tidiness. `show --stale` reads the PRODUCING run's
    sidecar for its stat fields, and reports `?` when a later run has overwritten it. With
    one sidecar per run, the older runs can still answer."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "out").mkdir()
    src = tmp_path / "in.tsv"
    src.write_text("id\n1\n", encoding="utf-8")
    proj = dataclasses.replace(_project(tmp_path), sidecar_per_run=True)

    for i in range(2):
        if i:
            time.sleep(1.05)
        with runprov.Run(
            "build", project=proj, provenance=tmp_path / "out" / f"o{i}.prov.json"
        ) as r:
            r.input(src)
            with r.open_output(tmp_path / "out" / f"o{i}.tsv") as fh:
                fh.write("x\n")

    rows = [json.loads(x) for x in (tmp_path / "runs.jsonl").read_text().splitlines()]
    states = runprov.show.staleness(rows)
    assert set(states.values()) == {"current"}, "both runs can still answer for themselves"
    assert "?" not in states.values()


# ================ the code that RAN: first-party modules the run actually imported
def _forget_src():
    """Drop every `src*` module before and after a test that imports one.

    Each test builds its own `src/` under its own tmp_path, so a cached `src` package from
    an earlier test carries a `__path__` pointing at a directory this one never made --
    and `import src.helper` then fails for a reason that has nothing to do with the code
    under test. Order-dependence in a suite is a bug in the suite.
    """
    for name in [m for m in sys.modules if m == "src" or m.startswith("src.")]:
        del sys.modules[name]
    importlib.invalidate_caches()


def _imported_in_order(tmp_path, monkeypatch, names):
    """Build `src/<name>.py` for each name, import them IN THE GIVEN ORDER, run, and return
    the run's `code.imported` section.

    The import order is the point. `sys.modules` is insertion-ordered, so a set built by
    walking it comes out in the order things were imported — which is the order the sort
    exists to erase.
    """
    (tmp_path / "src").mkdir(exist_ok=True)
    (tmp_path / "src" / "__init__.py").write_text("", encoding="utf-8")
    for n in names:
        (tmp_path / "src" / f"{n}.py").write_text(f"NAME = {n!r}\n", encoding="utf-8")
    proj = _project(tmp_path)
    monkeypatch.syspath_prepend(str(tmp_path))
    _forget_src()
    try:
        for n in names:
            importlib.import_module(f"src.{n}")
        with runprov.Run("s", project=proj, provenance=tmp_path / f"{names[0]}.json"):
            pass
        rec = json.loads((tmp_path / f"{names[0]}.json").read_text(encoding="utf-8"))
        return rec["code"]["imported"]
    finally:
        _forget_src()


def test_the_imported_code_list_is_sorted_not_in_import_order(tmp_path, monkeypatch):
    """L-54. `sorted()` is what makes two runs over unchanged code produce the same list and
    therefore the same `digest`, and removing it left the whole suite green — both existing
    digest tests compare two runs INSIDE ONE PROCESS, where `sys.modules` insertion order is
    already stable, so sorting is never the reason they agree.

    The fixture imports `zeta` BEFORE `alpha`, so insertion order and alphabetical order
    disagree. Without that they coincide and `list(seen.items())` passes — the same
    fixture-cannot-see-it shape as L-29's sort key."""
    monkeypatch.chdir(tmp_path)
    imported = _imported_in_order(tmp_path, monkeypatch, ["zeta", "alpha"])

    paths = [f["path"] for f in imported["files"]]
    assert {"src/zeta.py", "src/alpha.py"} <= set(paths), paths
    assert paths == sorted(paths), (
        f"the list is in import order, not sorted: {paths} — the digest is then a function "
        f"of which module happened to be imported first"
    )


def test_the_code_digest_is_the_same_whichever_order_the_modules_were_imported(
    tmp_path, monkeypatch
):
    """The property the sort exists for, stated directly. `imported.digest` is the one field
    answering "did any first-party code change between these two runs" in a single string
    comparison. Without a deterministic order it becomes a function of import order, so two
    runs of the SAME unchanged code — from different entry points, or after an import moved
    — report a code change that did not happen. That is the false-positive twin of the
    failure `script_sha256` could not see, and it is worse than a missed change: it teaches
    a reader that the field is noise."""
    monkeypatch.chdir(tmp_path)
    forwards = _imported_in_order(tmp_path, monkeypatch, ["alpha", "zeta"])
    backwards = _imported_in_order(tmp_path, monkeypatch, ["zeta", "alpha"])

    assert forwards["digest"] is not None
    assert forwards["digest"] == backwards["digest"], (
        "the same code, imported in a different order, reported a different code digest"
    )


def _project_with_module(tmp_path, body="X = 1\n"):
    (tmp_path / "src").mkdir(exist_ok=True)
    (tmp_path / "src" / "__init__.py").write_text("", encoding="utf-8")
    (tmp_path / "src" / "helper.py").write_text(body, encoding="utf-8")
    return _project(tmp_path)


def test_the_modules_a_run_imported_are_hashed_not_only_the_entry_script(tmp_path, monkeypatch):
    """`git_commit` identifies the code only when the tree is clean, and in development it
    never is. `script_sha256` pins the entry point and nothing it calls. So a run whose
    numbers moved because `src/helper.py` moved recorded a commit, a clean-looking entry
    script, and no trace of the file that did it.
    """
    proj = _project_with_module(tmp_path)
    monkeypatch.syspath_prepend(str(tmp_path))
    _forget_src()
    import src.helper  # noqa: F401 - importing it is the point

    try:
        with runprov.Run("s", project=proj, provenance=tmp_path / "p.json"):
            pass
        rec = json.loads((tmp_path / "p.json").read_text(encoding="utf-8"))
        imported = rec["code"]["imported"]
        paths = {f["path"] for f in imported["files"]}
        assert "src/helper.py" in paths, paths
        assert all(len(f["sha256"]) == 64 for f in imported["files"])
        assert imported["count"] == len(imported["files"]) and imported["omitted"] == 0
    finally:
        _forget_src()


def test_the_digest_moves_when_a_DEPENDENCY_moves_and_the_entry_script_does_not(
    tmp_path, monkeypatch
):
    """The whole point, and the case `script_sha256` cannot see."""
    proj = _project_with_module(tmp_path, "X = 1\n")
    monkeypatch.syspath_prepend(str(tmp_path))
    _forget_src()
    import src.helper

    try:
        with runprov.Run("s", project=proj, provenance=tmp_path / "a.json"):
            pass
        first = json.loads((tmp_path / "a.json").read_text(encoding="utf-8"))

        (tmp_path / "src" / "helper.py").write_text("X = 2  # changed\n", encoding="utf-8")
        importlib.reload(src.helper)
        with runprov.Run("s", project=proj, provenance=tmp_path / "b.json"):
            pass
        second = json.loads((tmp_path / "b.json").read_text(encoding="utf-8"))
    finally:
        _forget_src()

    assert first["code"]["imported"]["digest"] != second["code"]["imported"]["digest"]
    assert first["code"]["script_sha256"] == second["code"]["script_sha256"], (
        "the entry script did not change, which is exactly why this field was needed"
    )


def test_dependencies_installed_inside_the_root_are_not_this_project_s_code(tmp_path, monkeypatch):
    """A virtualenv inside the repository is under the root and is NOT the project's code.
    Hashing site-packages on every run would cost far more than it says, and `packages` plus
    the environment snapshot already answer for it."""
    proj = _project_with_module(tmp_path)
    vendored = tmp_path / ".venv" / "lib" / "site-packages" / "thirdparty"
    vendored.mkdir(parents=True)
    (vendored / "__init__.py").write_text("Y = 1\n", encoding="utf-8")
    monkeypatch.syspath_prepend(str(vendored.parent))
    monkeypatch.syspath_prepend(str(tmp_path))
    _forget_src()
    sys.modules.pop("thirdparty", None)
    import src.helper  # noqa: F401
    import thirdparty  # noqa: F401

    try:
        with runprov.Run("s", project=proj, provenance=tmp_path / "p.json"):
            pass
        paths = {
            f["path"]
            for f in json.loads((tmp_path / "p.json").read_text(encoding="utf-8"))["code"][
                "imported"
            ]["files"]
        }
        assert "src/helper.py" in paths
        assert not any("site-packages" in p for p in paths), paths
    finally:
        _forget_src()
        sys.modules.pop("thirdparty", None)


def test_the_history_line_carries_the_summary_and_not_every_file(tmp_path, monkeypatch):
    """The history is appended FOREVER. Fifty modules per line would multiply it, so it
    carries one digest and a count -- enough to answer "did any first-party code change
    between these two runs", which is the question the history is asked."""
    proj = _project_with_module(tmp_path)
    monkeypatch.syspath_prepend(str(tmp_path))
    _forget_src()
    import src.helper  # noqa: F401

    try:
        with runprov.Run("s", project=proj, provenance=tmp_path / "p.json"):
            pass
    finally:
        _forget_src()

    line = json.loads((tmp_path / "runs.jsonl").read_text(encoding="utf-8").strip())
    # `omitted` joined `count` and `digest` for L-51: past `imported_code_max` the digest
    # covers only the kept prefix, so its SCOPE has to travel with it. The file LIST still
    # belongs in the sidecar -- that is what this test is about.
    assert sorted(line["imported_code"]) == ["count", "digest", "omitted"]
    assert "files" not in line["imported_code"], "the list belongs in the sidecar"
    sidecar = json.loads((tmp_path / "p.json").read_text(encoding="utf-8"))
    assert line["imported_code"]["digest"] == sidecar["code"]["imported"]["digest"]


def test_the_list_is_capped_and_says_how_many_it_left_out(tmp_path, monkeypatch):
    """A record must not become the repository it describes."""
    proj = dataclasses.replace(_project_with_module(tmp_path), imported_code_max=2)
    # FOUR modules against a cap of two, so there is a tail to leave out. With two of each
    # the assertion below passes on `0 == 0` and proves nothing.
    for n in ("a", "b", "c"):
        (tmp_path / "src" / f"{n}.py").write_text(f"{n.upper()} = 1\n", encoding="utf-8")
    monkeypatch.syspath_prepend(str(tmp_path))
    _forget_src()
    import src.a
    import src.b
    import src.c
    import src.helper  # noqa: F401

    try:
        with runprov.Run("s", project=proj, provenance=tmp_path / "p.json"):
            pass
    finally:
        _forget_src()

    imported = json.loads((tmp_path / "p.json").read_text(encoding="utf-8"))["code"]["imported"]
    assert len(imported["files"]) == 2
    assert imported["omitted"] == imported["count"] - 2 > 0


def test_hashing_imported_code_can_be_turned_off(tmp_path, monkeypatch):
    proj = dataclasses.replace(_project_with_module(tmp_path), hash_imported_code=False)
    monkeypatch.chdir(tmp_path)
    with runprov.Run("s", project=proj, provenance=tmp_path / "p.json"):
        pass
    rec = json.loads((tmp_path / "p.json").read_text(encoding="utf-8"))
    assert "imported" not in rec["code"]
    # All three None, not the key absent: the history line's shape is fixed so a reader never
    # has to ask whether a missing key means "no code" or "an older record". `omitted` joined
    # the other two for L-51.
    assert json.loads((tmp_path / "runs.jsonl").read_text().strip())["imported_code"] == {
        "count": None,
        "digest": None,
        "omitted": None,
    }


def test_a_module_whose_file_vanished_does_not_fail_the_run(tmp_path, monkeypatch):
    """Provenance must never be the reason a run dies -- and a module can outlive its file
    (a temp module, an editable install that moved, a notebook cell)."""
    proj = _project_with_module(tmp_path)
    monkeypatch.syspath_prepend(str(tmp_path))
    _forget_src()
    import src.helper  # noqa: F401

    (tmp_path / "src" / "helper.py").unlink()
    try:
        with runprov.Run("s", project=proj, provenance=tmp_path / "p.json"):
            pass
    finally:
        _forget_src()
    imported = json.loads((tmp_path / "p.json").read_text(encoding="utf-8"))["code"]["imported"]
    assert "src/helper.py" not in {f["path"] for f in imported["files"]}


def test_two_names_for_one_module_file_are_hashed_once(tmp_path, monkeypatch):
    """`sys.modules` can hold the same file under several names — a package and its alias,
    `__main__` and the module it also imports. Hashing it twice would make the digest depend
    on how a module happened to be reached rather than on what the code is."""
    proj = _project_with_module(tmp_path)
    monkeypatch.syspath_prepend(str(tmp_path))
    _forget_src()
    import src.helper

    monkeypatch.setitem(sys.modules, "an_alias_for_helper", src.helper)

    # L-79. COUNT THE READS, not the entries. `paths.count(...) == 1` is what the `seen`
    # dict gives for free -- `seen[rel] = sha256(p)` overwrites, so there is one entry
    # whichever way -- and the guard this test is named for is `if rel in seen: continue`,
    # whose job is to avoid HASHING the file a second time. Deleting it left the suite green.
    #
    # The cost is per RUN, in a code path that executes at the end of every recorded run: an
    # aliased module is hashed once per alias, so a package imported under two names doubles
    # its share of the work, quietly, forever.
    hashed: list[str] = []
    real_sha256 = runprov.run.sha256
    monkeypatch.setattr(
        runprov.run, "sha256", lambda p, *a, **kw: (hashed.append(str(p)), real_sha256(p))[1]
    )
    try:
        with runprov.Run("s", project=proj, provenance=tmp_path / "p.json"):
            pass
    finally:
        _forget_src()

    files = json.loads((tmp_path / "p.json").read_text(encoding="utf-8"))["code"]["imported"][
        "files"
    ]
    paths = [f["path"] for f in files]
    assert paths.count("src/helper.py") == 1, paths

    reads = [p for p in hashed if p.endswith("helper.py")]
    assert len(reads) == 1, (
        f"helper.py was read {len(reads)} times for {len(reads)} names of one file — the "
        f"digest is the same either way, so the extra read buys nothing and costs a run"
    )


def test_a_failure_while_hashing_imports_is_recorded_not_raised(tmp_path, monkeypatch):
    """Provenance must never be the reason a run dies. A partial answer that says it is
    partial beats a lost record."""
    monkeypatch.chdir(tmp_path)

    def boom(self):
        raise RuntimeError("sys.modules exploded")

    monkeypatch.setattr(runprov.Run, "_imported_code", boom)
    with runprov.Run("s", project=_project(tmp_path), provenance=tmp_path / "p.json"):
        pass
    rec = json.loads((tmp_path / "p.json").read_text(encoding="utf-8"))
    assert rec["code"]["imported"] == {"error": "sys.modules exploded"}
    assert rec["status"] == "ok", "the RUN succeeded; only the capture of it did not"


# ============ the work that is not Python: external tools, and scripts in other languages
def test_tool_records_which_binary_and_what_version(tmp_path, monkeypatch):
    """For a pipeline whose real work is subprocesses, the Python environment answers almost
    nothing: `packages` lists what pip installed, and the thing that made the BAM is not in
    it. The PATH says WHICH one, when a conda env and /usr/bin both have a `samtools`."""
    monkeypatch.chdir(tmp_path)
    fake = tmp_path / "bin"
    fake.mkdir()
    tool = fake / "faketool"
    tool.write_text("#!/bin/sh\necho 'faketool 9.9.9'\n", encoding="utf-8")
    tool.chmod(0o755)
    monkeypatch.setenv("PATH", str(fake), prepend=os.pathsep)

    with runprov.Run("s", project=_project(tmp_path), provenance=tmp_path / "p.json") as run:
        got = run.tool("faketool")

    assert got["found"] is True
    assert got["version"] == "faketool 9.9.9"
    assert got["path"] == str(tool)
    assert len(got["sha256"]) == 64, "the binary itself, for two builds calling themselves 9.9.9"

    rec = json.loads((tmp_path / "p.json").read_text(encoding="utf-8"))
    assert rec["tools"][0]["name"] == "faketool"
    line = json.loads((tmp_path / "runs.jsonl").read_text(encoding="utf-8").strip())
    assert line["tools"] == {"faketool": "faketool 9.9.9"}, "the history carries name -> version"


def test_a_tool_that_is_absent_is_recorded_as_absent(tmp_path, monkeypatch):
    """ "We looked and it was not there" is a fact about the run. Silence is not."""
    monkeypatch.chdir(tmp_path)
    with runprov.Run("s", project=_project(tmp_path), provenance=tmp_path / "p.json") as run:
        got = run.tool("definitely_not_on_this_path")
    assert got == {"name": "definitely_not_on_this_path", "found": False}
    assert json.loads((tmp_path / "p.json").read_text(encoding="utf-8"))["tools"] == [got]


def test_a_tool_that_hangs_or_cannot_run_is_recorded_never_raised(tmp_path, monkeypatch):
    """Provenance must not be the reason a pipeline stops. `--version` on an unknown binary
    is not free and not always harmless, which is why this is bounded and guarded."""
    monkeypatch.chdir(tmp_path)
    fake = tmp_path / "bin"
    fake.mkdir()
    slow = fake / "slowtool"
    slow.write_text("#!/bin/sh\nsleep 5\n", encoding="utf-8")
    slow.chmod(0o755)
    monkeypatch.setenv("PATH", str(fake), prepend=os.pathsep)

    with runprov.Run("s", project=_project(tmp_path), provenance=tmp_path / "p.json") as run:
        got = run.tool("slowtool", timeout=0.4)

    assert got["found"] is True and got["version"] is None
    assert "Timeout" in got["version_error"], got
    assert json.loads((tmp_path / "p.json").read_text(encoding="utf-8"))["status"] == "ok"


def test_a_version_printed_to_stderr_is_still_a_version(tmp_path, monkeypatch):
    """Plenty of tools print their version to stderr, and a non-zero exit does not mean they
    failed to tell us — `samtools --version` is the canonical example."""
    monkeypatch.chdir(tmp_path)
    fake = tmp_path / "bin"
    fake.mkdir()
    noisy = fake / "noisytool"
    noisy.write_text("#!/bin/sh\necho 'noisytool 2.1' >&2\nexit 1\n", encoding="utf-8")
    noisy.chmod(0o755)
    monkeypatch.setenv("PATH", str(fake), prepend=os.pathsep)

    with runprov.Run("s", project=_project(tmp_path), provenance=tmp_path / "p.json") as run:
        got = run.tool("noisytool")
    assert got["version"] == "noisytool 2.1" and got["exit_code"] == 1


def test_a_version_banner_that_is_not_utf8_does_not_kill_the_run(tmp_path, monkeypatch):
    """L-14. A version banner is not required to be UTF-8 — a latin-1 `ç` in a vendor's
    copyright line is enough. Strict decoding raised `UnicodeDecodeError`, which is a
    `ValueError` and so walked past the `(OSError, SubprocessError)` guard, left `tool()`,
    and reached the caller. The run was then recorded `status: failed` with a decode error:
    a record that says the WORK failed when only the description of it did. `tool()`'s own
    docstring says provenance must not be the reason a pipeline stops.

    Fifth instance of the wrong-exception-family defect — see L-98's test."""
    monkeypatch.chdir(tmp_path)
    fake = tmp_path / "bin"
    fake.mkdir()
    noisy = fake / "latin1tool"
    # `ç` and `Ã` as raw latin-1 bytes: valid output, invalid UTF-8.
    noisy.write_bytes(b'#!/bin/sh\nprintf "latin1tool 2.1 \xc3(c) Fran\xe7ois\\n"\nexit 0\n')
    noisy.chmod(0o755)
    monkeypatch.setenv("PATH", str(fake), prepend=os.pathsep)

    with runprov.Run("s", project=_project(tmp_path), provenance=tmp_path / "p.json") as run:
        got = run.tool("latin1tool")

    rec = json.loads((tmp_path / "p.json").read_text(encoding="utf-8"))
    assert rec["status"] == "ok", "the run did not fail; only its version probe was awkward"
    assert got["version"] is not None, "a replaceable byte is not a reason to lose the version"
    assert got["version"].startswith("latin1tool 2.1"), got["version"]
    assert "�" in got["version"], "the undecodable bytes are replaced, not dropped"
    assert got["exit_code"] == 0


def test_exec_runs_a_command_whose_version_banner_is_not_utf8(tmp_path, monkeypatch, capfd):
    """The consequence that makes L-14 more than cosmetic: `_exec` probes `argv[0]` with
    `tool()` BEFORE running anything, so the decode error meant the wrapped command never
    executed at all — a wrapper that stopped the pipeline it was meant to record."""
    monkeypatch.chdir(tmp_path)
    runprov.configure(root=tmp_path, run_log=tmp_path / "runs.jsonl")
    fake = tmp_path / "bin"
    fake.mkdir()
    tool = fake / "latin1echo"
    tool.write_bytes(
        b"#!/bin/sh\n"
        b'if [ "$1" = "--version" ]; then printf "latin1echo 1.0 Fran\xe7ois\\n"; exit 0; fi\n'
        b'echo "DID RUN: $*"\n'
    )
    tool.chmod(0o755)
    monkeypatch.setenv("PATH", str(fake), prepend=os.pathsep)

    rc = runprov.__main__.main(
        ["exec", "--name", "e", "--provenance", str(tmp_path / "p.json"), "--", "latin1echo", "x"]
    )
    assert rc == 0
    assert "DID RUN: x" in capfd.readouterr().out, "the command must actually execute"


def test_code_registers_a_script_in_another_language_into_the_same_digest(tmp_path, monkeypatch):
    """An R script or a shell wrapper is code that ran, and `sys.modules` will never know
    about it — the interpreter that ran it was a subprocess. Declared, then treated
    identically, so "did any code change" stays ONE comparison across languages."""
    monkeypatch.chdir(tmp_path)
    r_script = tmp_path / "fit.R"
    r_script.write_text('cat("fitting\\n")\n', encoding="utf-8")

    with runprov.Run("s", project=_project(tmp_path), provenance=tmp_path / "a.json") as run:
        returned = run.code(r_script)
    assert returned == r_script, "it returns the path, so registering is how you pass it"

    first = json.loads((tmp_path / "a.json").read_text(encoding="utf-8"))["code"]["imported"]
    assert "fit.R" in {f["path"] for f in first["files"]}

    r_script.write_text('cat("fitting differently\\n")\n', encoding="utf-8")
    with runprov.Run("s", project=_project(tmp_path), provenance=tmp_path / "b.json") as run:
        run.code(r_script)
    second = json.loads((tmp_path / "b.json").read_text(encoding="utf-8"))["code"]["imported"]
    assert first["digest"] != second["digest"], "an R change moves the code digest"


@requires_symlinks
def test_one_unresolvable_module_does_not_erase_the_whole_code_section(tmp_path, monkeypatch):
    """L-98, found by sweeping every narrow `except` around a `resolve()` after L-97.

    `Path.resolve()` raises `RuntimeError` on a symlink loop, not `OSError`. Both guards in
    `_imported_code` say "skip this one" — a `continue` and a `contextlib.suppress` — and
    neither named `RuntimeError`, so it escaped to `_record_imported_code`'s catch-all and
    the ENTIRE section became `{"error": ...}`. One module behind a looped path erased every
    other module and every file declared with `code()`.

    Fourth instance of the same defect: L-01 (`RecursionError` past `TypeError, ValueError`),
    L-97 (`RuntimeError` past `OSError`), the `_pin_name` guard which had already learned it,
    and this. The lesson is not about symlinks; it is that a guard naming the obvious
    exception reads as careful and is not.
    """
    monkeypatch.chdir(tmp_path)
    (tmp_path / "real.py").write_text("X = 1\n", encoding="utf-8")
    (tmp_path / "fit.R").write_text('cat("hi")\n', encoding="utf-8")
    (tmp_path / "a").symlink_to(tmp_path / "b")
    (tmp_path / "b").symlink_to(tmp_path / "a")
    _assert_symlink_loop(tmp_path / "a" / "mod.py")  # the premise, asserted not assumed

    good = types.ModuleType("goodmod_l98")
    good.__file__ = str(tmp_path / "real.py")
    bad = types.ModuleType("badmod_l98")
    bad.__file__ = str(tmp_path / "a" / "mod.py")
    monkeypatch.setitem(sys.modules, "goodmod_l98", good)
    monkeypatch.setitem(sys.modules, "badmod_l98", bad)

    with runprov.Run("s", project=_project(tmp_path), provenance=tmp_path / "p.json") as run:
        run.code(tmp_path / "fit.R")

    imported = json.loads((tmp_path / "p.json").read_text(encoding="utf-8"))["code"]["imported"]
    assert "error" not in imported, f"one bad path erased the section: {imported}"
    paths = {f["path"] for f in imported["files"]}
    assert "real.py" in paths, "a good module must survive a neighbour that cannot resolve"
    assert "fit.R" in paths, "and so must a file the caller explicitly declared"


def test_declared_code_outside_the_project_root_is_still_recorded(tmp_path, monkeypatch):
    """L-27. `relative_to(root)` raises for a path outside the tree, and inside a bare
    `suppress` that dropped the entry entirely — so `run.code("/opt/pipeline/fit.R")`
    recorded NOTHING while returning the path, with no warning. Tracking a script that lives
    outside the tree is exactly what `code()` was added for.

    Discovery may skip what it cannot place; an EXPLICIT call may not. Recorded as
    `<external>/<name>`, the same spelling the pin uses for an input outside the root and for
    the same reason: an absolute path would put this machine's layout into a digest whose
    whole purpose is to compare across machines."""
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    (script := outside / "fit.R").write_text('cat("hi")\n', encoding="utf-8")
    root = tmp_path / "project"
    root.mkdir()
    monkeypatch.chdir(root)
    proj = runprov.Project(
        root=root, run_log=root / "runs.jsonl", run_id=lambda: "r", generation=lambda: "g"
    )

    with runprov.Run("s", project=proj, provenance=root / "p.json") as run:
        run.code(script)

    imported = json.loads((root / "p.json").read_text(encoding="utf-8"))["code"]["imported"]
    assert [f["path"] for f in imported["files"]] == ["<external>/fit.R"]
    assert imported["digest"], "and it reaches the digest, which is the point of declaring it"


def test_a_relative_provenance_path_is_readable_from_anywhere(tmp_path, monkeypatch):
    """L-42. `_sidecar` opened the recorded `provenance_path` verbatim while `staleness`
    resolves every artifact path against the run's recorded cwd — so a run that used the
    natural relative spelling, `provenance="out/mid.prov.json"`, was readable only from its
    own working directory.

    Measured before the fix: `current` from there, `?` from anywhere else, for every
    artifact at once. A page whose answers depend on the reader's shell is not a page."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "out").mkdir()
    (tmp_path / "in.tsv").write_text("id\n1\n", encoding="utf-8")
    proj = _project(tmp_path)
    with runprov.Run("b", project=proj, provenance="out/mid.prov.json") as run:
        run.input(tmp_path / "in.tsv")
        with run.open_output(tmp_path / "out" / "mid.tsv") as fh:
            fh.write("a\n")

    rows = [json.loads(x) for x in (tmp_path / "runs.jsonl").read_text().splitlines()]
    assert rows[0]["provenance_path"] == "out/mid.prov.json", "the premise: a relative path"
    assert _state_of(runprov.show.staleness(rows), "mid.tsv") == "current"

    monkeypatch.chdir(tmp_path.parent)  # any other directory
    assert _state_of(runprov.show.staleness(rows), "mid.tsv") == "current", (
        "the same history read from elsewhere must give the same answer"
    )


def test_sidecar_per_run_applies_to_write_as_well_as_the_kwarg(tmp_path):
    """L-45. `sidecar_per_run` was honoured only for the `provenance=` kwarg, so two
    `write()` calls to one path overwrote each other exactly as they did before the setting
    existed — and overwriting is the thing it exists to prevent. Two of the three documented
    ways to get a sidecar ignored it."""
    proj = runprov.Project(
        root=tmp_path, run_log=tmp_path / "runs.jsonl", run_id=lambda: "r",
        generation=lambda: "g", sidecar_per_run=True,
    )  # fmt: skip
    for i in range(2):
        runprov.Run(f"w{i}", project=proj).write(tmp_path / "out.prov.json")

    written = sorted(p.name for p in tmp_path.iterdir() if p.name.endswith(".prov.json"))
    assert len(written) == 2, f"one run overwrote the other: {written}"
    assert all(n.startswith("out.") and n.endswith(".prov.json") for n in written), written
    assert "out.prov.json" not in written, "the stamp goes in, or the setting did nothing"


def test_the_constructors_sidecar_is_not_stamped_twice(tmp_path):
    """The interaction the fix has to avoid. `__init__` stamps `provenance_path`, and
    `_finish()` hands that same path back to `write()` at exit — so stamping unconditionally
    there produces `summary.<t>.<uid>.<t>.<uid>.prov.json`."""
    proj = runprov.Project(
        root=tmp_path, run_log=tmp_path / "runs.jsonl", run_id=lambda: "r",
        generation=lambda: "g", sidecar_per_run=True,
    )  # fmt: skip
    with runprov.Run("a", project=proj, provenance=tmp_path / "summary.prov.json"):
        pass

    names = [p.name for p in tmp_path.iterdir() if p.name.endswith(".prov.json")]
    assert len(names) == 1, names
    assert names[0].count(".prov.json") == 1 and names[0].count("Z.") == 1, (
        f"stamped twice: {names[0]}"
    )


@requires_symlinks
def test_declared_code_that_becomes_unresolvable_is_skipped_not_fatal(tmp_path, monkeypatch):
    """The guard under L-27's fix, and it is reachable rather than defensive. `code()`
    validates the file at REGISTRATION; the record is built at EXIT, and a staging step can
    replace a path in between. `Path.resolve()` then raises `RuntimeError` for a symlink
    loop — not `OSError`, which is L-97 and L-98's lesson — and losing the whole code section
    over one declared file would be L-98 again.

    Skipping one entry is the intent. The run, the record and the other declared files all
    survive."""
    monkeypatch.chdir(tmp_path)
    (good := tmp_path / "keep.R").write_text('cat("keep")\n', encoding="utf-8")
    staged = tmp_path / "staged"
    staged.mkdir()
    (doomed := staged / "fit.R").write_text('cat("fit")\n', encoding="utf-8")

    proj = _project(tmp_path)
    with runprov.Run("s", project=proj, provenance=tmp_path / "p.json") as run:
        run.code(good)
        run.code(doomed)
        # The staging directory is replaced by a symlink loop before the run ends.
        doomed.unlink()
        staged.rmdir()
        (tmp_path / "staged").symlink_to(tmp_path / "loop")
        (tmp_path / "loop").symlink_to(tmp_path / "staged")
        _assert_symlink_loop(doomed)  # the premise, asserted rather than assumed

    imported = json.loads((tmp_path / "p.json").read_text(encoding="utf-8"))["code"]["imported"]
    assert "error" not in imported, f"one bad path cost the whole section: {imported}"
    assert "keep.R" in {f["path"] for f in imported["files"]}, "the other declared file survives"


def test_code_is_recorded_without_a_with_block(tmp_path, monkeypatch):
    """L-05. `__exit__` was the only place the code section was built, so `run.code(...)`
    followed by `run.write(...)` — the documented manual shape — recorded nothing at all
    while `code()` returned the path, so the call looked like it had worked. An R script
    declared by a script that does not use a `with` block simply was not in the record."""
    monkeypatch.chdir(tmp_path)
    (r_script := tmp_path / "fit.R").write_text('cat("hi\\n")\n', encoding="utf-8")

    run = runprov.Run("s", project=_project(tmp_path))
    run.code(r_script)
    run.write(tmp_path / "p.json")

    imported = json.loads((tmp_path / "p.json").read_text(encoding="utf-8"))["code"]["imported"]
    assert "fit.R" in {f["path"] for f in imported["files"]}


def test_declared_code_survives_hash_imported_code_being_off(tmp_path, monkeypatch):
    """The other shape, and the worse one. `hash_imported_code=False` turns off DISCOVERY —
    the sweep of `sys.modules` that costs a hash per first-party file. It was also dropping
    everything the caller had explicitly DECLARED, which is the opposite of a cost control:
    an explicit call is the one thing that cannot be inferred. A Python setting silently
    removed an R script from the record."""
    monkeypatch.chdir(tmp_path)
    (r_script := tmp_path / "fit.R").write_text('cat("hi\\n")\n', encoding="utf-8")
    # A REAL first-party module under the root, imported for the duration. Without one, the
    # discovery half is invisible: an empty tmp_path yields nothing to discover, so a version
    # that ignores the setting entirely produces the same `{"fit.R"}` and the test passes for
    # the wrong reason. Found by mutating the setting away and watching this stay green.
    proj = _project_with_module(tmp_path)
    proj = dataclasses.replace(proj, hash_imported_code=False)
    monkeypatch.syspath_prepend(str(tmp_path))
    _forget_src()
    import src.helper  # noqa: F401 - importing it is the point

    try:
        with runprov.Run("s", project=proj, provenance=tmp_path / "p.json") as run:
            run.code(r_script)
        imported = json.loads((tmp_path / "p.json").read_text(encoding="utf-8"))["code"]["imported"]
        paths = {f["path"] for f in imported["files"]}
        assert paths == {"fit.R"}, f"declared code only, discovery still off: {paths}"
    finally:
        _forget_src()


def test_hash_imported_code_off_and_nothing_declared_records_no_section(tmp_path, monkeypatch):
    """The setting still has to mean something. With discovery off and nothing declared there
    is no code section at all — otherwise "off" would have become "walk anyway"."""
    monkeypatch.chdir(tmp_path)
    proj = runprov.Project(
        root=tmp_path,
        run_log=tmp_path / "runs.jsonl",
        run_id=lambda: "r",
        generation=lambda: "g",
        hash_imported_code=False,
    )
    with runprov.Run("s", project=proj, provenance=tmp_path / "p.json"):
        pass
    rec = json.loads((tmp_path / "p.json").read_text(encoding="utf-8"))
    assert rec["code"].get("imported") is None


def test_code_refuses_a_file_it_cannot_read(tmp_path, monkeypatch):
    """Same treatment as `input()`: code that cannot be hashed at registration is a loud
    failure, not a silently absent line in the record."""
    monkeypatch.chdir(tmp_path)
    with runprov.Run("s", project=_project(tmp_path), provenance=tmp_path / "p.json") as run:
        with pytest.raises(OSError, match="cannot register code"):
            run.code(tmp_path / "no_such_script.R")


def test_code_is_not_confused_with_input(tmp_path, monkeypatch):
    """A new column in a data file and a rewritten model are the same event to a reader who
    only has one list, so they are two lists."""
    monkeypatch.chdir(tmp_path)
    data = tmp_path / "in.tsv"
    data.write_text("a\n1\n", encoding="utf-8")
    script = tmp_path / "step.sh"
    script.write_text("echo hi\n", encoding="utf-8")

    with runprov.Run("s", project=_project(tmp_path), provenance=tmp_path / "p.json") as run:
        run.input(data)
        run.code(script)

    rec = json.loads((tmp_path / "p.json").read_text(encoding="utf-8"))
    assert [pathlib.Path(i["path"]).name for i in rec["inputs"]] == ["in.tsv"]
    assert "step.sh" in {f["path"] for f in rec["code"]["imported"]["files"]}
    assert "step.sh" not in {pathlib.Path(i["path"]).name for i in rec["inputs"]}


# ========== `runprov exec`: a subprocess recorded as a run, for pipelines with no Python
def test_exec_records_a_shell_command_as_a_run(tmp_path, monkeypatch, capsys):
    """`tool()` and `code()` are calls a caller has to make, and a Makefile or a Snakefile
    has no Python to put them in. So the recording is something you put IN FRONT."""
    monkeypatch.chdir(tmp_path)
    runprov.configure(root=tmp_path, run_log=tmp_path / "runs.jsonl")
    (tmp_path / "in.tsv").write_text("b\n2\na\n1\n", encoding="utf-8")

    rc = runprov.__main__.main(
        [
            "exec",
            "--name",
            "sorter",
            "--input",
            "in.tsv",
            "--output",
            "out.tsv",
            "--provenance",
            str(tmp_path / "p.json"),
            "--",
            "sort",
            "in.tsv",
            "-o",
            "out.tsv",
        ]
    )
    capsys.readouterr()
    assert rc == 0 and (tmp_path / "out.tsv").is_file()

    rec = json.loads((tmp_path / "p.json").read_text(encoding="utf-8"))
    assert rec["status"] == "ok" and rec["notes"]["exit_code"] == 0
    assert rec["parameters"]["argv"] == ["sort", "in.tsv", "-o", "out.tsv"]
    assert rec["tools"][0]["name"] == "sort" and rec["tools"][0]["found"] is True
    assert [pathlib.Path(i["path"]).name for i in rec["inputs"]] == ["in.tsv"]
    assert [pathlib.Path(o["path"]).name for o in rec["outputs"]] == ["out.tsv"]
    assert rec["outputs"][0]["sha256"], "the artifact the tool wrote is hashed like any other"


def test_exec_reports_a_missing_input_as_a_message_not_a_traceback(tmp_path, monkeypatch, capsys):
    """L-82. `input()` raises for a path that is not there, deliberately — a registered input
    is hashed and pinned, so it must exist at registration. Through the CLI that arrived as a
    raw `FileNotFoundError` and a stack, which no other user error here does: a mistyped
    `--input` is a usage mistake, and a traceback buries the message that says what to fix."""
    monkeypatch.chdir(tmp_path)
    runprov.configure(root=tmp_path, run_log=tmp_path / "runs.jsonl")
    rc = runprov.__main__.main(["exec", "--input", "no_such.tsv", "--", "/bin/echo", "hi"])
    err = capsys.readouterr().err
    assert rc == 2, "a usage mistake, distinct from the wrapped command's own exit code"
    assert "cannot register input no_such.tsv" in err, err
    assert "Traceback" not in err and "FileNotFoundError" not in err


def test_show_says_when_stale_and_rehash_are_ignored(tmp_path, monkeypatch, capsys):
    """L-48. Both flags are parsed for every `show` and applied only to the project page,
    because the staleness column belongs to the ARTIFACT INDEX and a target renders runs.
    Accepting a flag and doing nothing is the silence this package refuses everywhere else —
    a reader who passed it is entitled to know it had no effect, rather than to conclude the
    artifacts are fine."""
    rows = _history(tmp_path, monkeypatch)
    log = str(tmp_path / "runs.jsonl")
    assert rows

    assert runprov.__main__.main(["show", "build", "--log", log, "--stale"]) == 0
    err = capsys.readouterr().err
    assert "--stale applies to the project page and is ignored with a target" in err, err

    # The project page still applies it, and says nothing.
    assert runprov.__main__.main(["show", "--log", log, "--stale"]) == 0
    assert "is ignored with a target" not in capsys.readouterr().err


def test_verify_says_when_log_is_ignored(tmp_path, monkeypatch, capsys):
    """The other half of L-48. `--log` is accepted so `runprov <cmd> --log X` stays uniform
    across subcommands, but `verify` genuinely cannot use it: it reads the pin inside each
    artifact and never the history, which is the whole reason the pin is in the bytes.
    Declared with `SUPPRESS` and never read, it was a flag that did nothing and said
    nothing."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "plain.tsv").write_text("id\n1\n", encoding="utf-8")
    runprov.__main__.main(["verify", str(tmp_path), "--root", str(tmp_path), "--log", "x.jsonl"])
    assert "--log is accepted for uniformity and ignored by `verify`" in capsys.readouterr().err

    runprov.__main__.main(["verify", str(tmp_path), "--root", str(tmp_path)])
    assert "--log is accepted" not in capsys.readouterr().err, "silent when not passed"


def test_exec_passes_a_separator_through_to_the_command(tmp_path, monkeypatch, capfd):
    """L-13. `nargs=REMAINDER` hands back the `--` that separates runprov's own flags from
    the command, so one has to come off — but every `--` was being dropped, which rewrites
    the command itself. `git log -- src/` means "what follows are paths"; `find . -- -x`
    protects a leading dash. Both ran as something else, and `parameters.argv` recorded the
    mangled list as though it were what ran, which is the one thing this wrapper exists to
    get right: the record equals what executed."""
    monkeypatch.chdir(tmp_path)
    runprov.configure(root=tmp_path, run_log=tmp_path / "runs.jsonl")

    rc = runprov.__main__.main(
        ["exec", "--name", "e", "--provenance", str(tmp_path / "p.json"),
         "--", "/bin/echo", "a", "--", "b"]
    )  # fmt: skip
    # capfd, NOT capsys: the child writes to the real file descriptor, which is the whole
    # point of a wrapper that runs somebody else's program -- capsys only sees Python's own
    # sys.stdout and reports an empty string here.
    out = capfd.readouterr().out
    assert rc == 0
    assert "a -- b" in out, f"the separator reached the command: {out!r}"

    rec = json.loads((tmp_path / "p.json").read_text(encoding="utf-8"))
    assert rec["parameters"]["argv"] == ["/bin/echo", "a", "--", "b"]


def test_exec_accepts_a_command_with_no_leading_separator(tmp_path, monkeypatch, capfd):
    """The leading `--` is optional — argparse only inserts it when the user typed it — so
    stripping position 0 unconditionally would eat the program name instead."""
    monkeypatch.chdir(tmp_path)
    runprov.configure(root=tmp_path, run_log=tmp_path / "runs.jsonl")

    rc = runprov.__main__.main(
        ["exec", "--name", "e", "--provenance", str(tmp_path / "p.json"), "/bin/echo", "hi"]
    )
    assert rc == 0 and "hi" in capfd.readouterr().out
    rec = json.loads((tmp_path / "p.json").read_text(encoding="utf-8"))
    assert rec["parameters"]["argv"] == ["/bin/echo", "hi"]


def test_exec_returns_the_commands_own_exit_code_and_records_the_failure(tmp_path, monkeypatch):
    """It has to compose in a Makefile or a Snakemake `shell:` without changing what failure
    means — so the command's code is returned, and the run is recorded as failed."""
    monkeypatch.chdir(tmp_path)
    runprov.configure(root=tmp_path, run_log=tmp_path / "runs.jsonl")

    rc = runprov.__main__.main(
        [
            "exec",
            "--name",
            "boom",
            "--provenance",
            str(tmp_path / "p.json"),
            "--",
            "sh",
            "-c",
            "exit 3",
        ]
    )
    assert rc == 3, "the tool's exit code, not 1 and not 0"
    rec = json.loads((tmp_path / "p.json").read_text(encoding="utf-8"))
    assert rec["status"] == "failed"
    assert rec["failure"]["type"] == "CommandFailedError"
    assert rec["notes"]["exit_code"] == 3


def test_exec_records_a_program_that_does_not_exist(tmp_path, monkeypatch):
    """A missing tool is a fact about the run, and it must not be a traceback."""
    monkeypatch.chdir(tmp_path)
    runprov.configure(root=tmp_path, run_log=tmp_path / "runs.jsonl")
    rc = runprov.__main__.main(
        ["exec", "--provenance", str(tmp_path / "p.json"), "--", "definitely_not_a_program"]
    )
    assert rc == 1
    rec = json.loads((tmp_path / "p.json").read_text(encoding="utf-8"))
    assert rec["status"] == "failed"
    assert "exec_error" in rec["notes"]
    assert rec["tools"][0]["found"] is False, "and the tool is recorded as absent"


def test_exec_without_a_command_explains_itself(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    runprov.configure(root=tmp_path, run_log=tmp_path / "runs.jsonl")
    assert runprov.__main__.main(["exec"]) == 2
    err = capsys.readouterr().err
    assert "runprov exec needs a command" in err and "--input" in err


def test_exec_defaults_the_name_and_the_sidecar_path(tmp_path, monkeypatch, capsys):
    """No `--name` and no `--provenance`: the program's own name, and a sidecar under the
    project's provenance directory carrying the run id, so two runs do not collide."""
    monkeypatch.chdir(tmp_path)
    runprov.configure(root=tmp_path, run_log=tmp_path / "runs.jsonl")
    assert runprov.__main__.main(["exec", "--", "sh", "-c", "true"]) == 0
    capsys.readouterr()
    written = list((tmp_path / "provenance").glob("sh_*.json"))
    assert len(written) == 1, written
    assert json.loads(written[0].read_text(encoding="utf-8"))["script"] == "sh"


def test_exec_can_tee_the_commands_output_to_a_file(tmp_path, monkeypatch, capsys):
    """The command inherits this process's stdout, so `--capture` -- which works at file
    descriptor level -- sees a SUBPROCESS's output, which Python-level capture cannot."""
    monkeypatch.chdir(tmp_path)
    runprov.configure(root=tmp_path, run_log=tmp_path / "runs.jsonl")
    log = tmp_path / "cmd.log"
    rc = runprov.__main__.main(
        [
            "exec",
            "--provenance",
            str(tmp_path / "p.json"),
            "--capture",
            str(log),
            "--",
            "sh",
            "-c",
            "echo from-a-subprocess",
        ]
    )
    capsys.readouterr()
    assert rc == 0
    assert "from-a-subprocess" in log.read_text(encoding="utf-8")
    rec = json.loads((tmp_path / "p.json").read_text(encoding="utf-8"))
    assert rec["terminal_log"]["path"] == str(log)


# ============================ NFS and Lustre: the filesystems the lock exists for
#: Point this at a directory on a REAL network filesystem and the tests below run there.
#:
#:     RUNPROV_NETWORK_FS_DIR=/mnt/lustre/scratch/you python -m pytest -k network_fs
#:
#: They are skipped otherwise, loudly and by name, because the alternative is a suite that
#: reports success for the one environment it never entered. `flock` on NFS depends on the
#: server, the protocol version and whether lockd is running; none of that can be
#: discovered from a laptop, and asserting it from one would be a claim about a machine
#: that is not the machine that matters.
NETWORK_FS = os.environ.get("RUNPROV_NETWORK_FS_DIR")


def _append_many(target, n, size, tag):
    """One process appending `n` records of about `size` bytes through the real sink."""
    sink = runprov.JsonlSink(pathlib.Path(target))
    for i in range(n):
        sink.append({"tag": tag, "i": i, "pad": "x" * size})


@requires_fcntl
def test_concurrent_appends_survive_when_locking_is_UNAVAILABLE(tmp_path, monkeypatch):
    """The NFS case, forced rather than waited for.

    `flock` on NFS depends on the server, the protocol version and whether lockd is running.
    When it is not there, `fcntl.flock` raises and this package degrades to an unlocked
    `O_APPEND` -- which is exactly the configuration a cluster hands you. So the degraded
    path is tested directly instead of hoping the lock is always available.

    What must hold with NO lock at all: every record still lands, and any line that did tear
    costs ONE record rather than the file. That is the JSONL promise, and it is the reason
    the format was chosen over the YAML it replaces.
    """
    import fcntl

    def no_locks(*_a, **_k):
        raise OSError(errno.ENOLCK, "No locks available")

    monkeypatch.setattr(fcntl, "flock", no_locks)

    target = tmp_path / "runs.jsonl"
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(lambda t: _append_many(target, 20, 4000, t), range(8)))

    rows, bad = runprov.__main__._load(target)
    assert len(rows) + bad == 160, f"records lost outright: {len(rows)} + {bad} torn"
    assert bad == 0 or len(rows) >= 160 - bad, "a torn line must cost one record, never more"


@requires_fcntl
def test_the_downgrade_to_no_locking_is_ANNOUNCED(tmp_path, monkeypatch, capsys):
    """The notice used to sit inside a win32-only branch, so a POSIX `flock` that raised --
    an NFS or CIFS mount, a container without the syscall -- degraded silently. Those
    filesystems are the lock's entire justification, so that was the one case that most
    deserved announcing and the one case that could not."""
    import fcntl

    monkeypatch.setattr(
        fcntl, "flock", lambda *_a, **_k: (_ for _ in ()).throw(OSError(errno.ENOLCK, "nope"))
    )
    runprov.JsonlSink(tmp_path / "h.jsonl").append({"a": 1})
    err = capsys.readouterr().err
    assert "no file locking available" in err
    assert "may interleave" in err, "the consequence, not only the fact"
    assert json.loads((tmp_path / "h.jsonl").read_text(encoding="utf-8"))["a"] == 1


def test_a_torn_line_costs_one_record_not_the_file(tmp_path):
    """What a network filesystem can actually do to an append, written directly. A YAML
    document that loses a line stops parsing; JSONL loses one record and says so."""
    target = tmp_path / "runs.jsonl"
    sink = runprov.JsonlSink(target)
    sink.append({"i": 0})
    with target.open("a", encoding="utf-8") as fh:
        fh.write('{"i": 1, "trunc')  # an interleaved half-write, no newline
    sink.append({"i": 2})

    rows, bad = runprov.__main__._load(target)
    assert bad == 1, "the torn line is COUNTED, never silently dropped"
    assert [r["i"] for r in rows] == [0, 2], "the records either side are intact"


@pytest.mark.skipif(not NETWORK_FS, reason="set RUNPROV_NETWORK_FS_DIR to a real NFS/Lustre path")
def test_network_fs_concurrent_appends_do_not_lose_records():
    """THE REAL TEST, on the filesystem that matters. Run it on the cluster:

        RUNPROV_NETWORK_FS_DIR=/mnt/lustre/scratch/you python -m pytest -k network_fs

    Processes rather than threads, because on a cluster the writers are separate jobs and
    a thread pool shares one file description -- which is precisely the sharing that hides
    the bug. 8 x 20 records of ~4 KB, which straddles the 4,096-byte bound below which
    POSIX guarantees an atomic O_APPEND and above which it does not.
    """
    root = pathlib.Path(NETWORK_FS)
    root.mkdir(parents=True, exist_ok=True)
    target = root / f"runprov_nfs_{os.getpid()}.jsonl"
    try:
        with concurrent.futures.ProcessPoolExecutor(max_workers=8) as pool:
            list(pool.map(_append_many, [target] * 8, [20] * 8, [4000] * 8, range(8)))

        rows, bad = runprov.__main__._load(target)
        assert bad == 0, f"{bad} torn line(s) on {root} — locking is not holding there"
        assert len(rows) == 160, f"{len(rows)} of 160 records survived on {root}"
        assert len({(r["tag"], r["i"]) for r in rows}) == 160, "and none was duplicated"
    finally:
        target.unlink(missing_ok=True)


@pytest.mark.skipif(not NETWORK_FS, reason="set RUNPROV_NETWORK_FS_DIR to a real NFS/Lustre path")
@requires_fcntl
def test_network_fs_reports_whether_locking_is_actually_available():
    """Not an assertion, a MEASUREMENT: does `flock` work on this mount at all?

    It is allowed to fail -- plenty of NFS exports have no lockd -- and the point is that
    the answer is printed rather than assumed. A run on such a mount degrades to an
    unlocked append and says so, which is the behaviour the tests above pin down.
    """
    import fcntl

    probe = pathlib.Path(NETWORK_FS) / f"runprov_lockprobe_{os.getpid()}"
    probe.write_text("x", encoding="utf-8")
    try:
        with probe.open("a") as fh:
            try:
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
                available = True
                detail = ""
            except OSError as exc:
                available = False
                detail = f"{type(exc).__name__}: {exc}"
        print(f"\nflock on {NETWORK_FS}: {'AVAILABLE' if available else 'UNAVAILABLE'} {detail}")
    finally:
        probe.unlink(missing_ok=True)


# ==================== performance regressions: shapes, never absolute numbers
# Every test here compares TWO measurements of the same operation at different sizes. An
# absolute threshold is a test of the machine that set it -- this suite already learned that
# once, when `peak < size / 4` passed locally and failed in CI by 2% on a 7.4 MB file. A
# ratio cancels the machine out: a linear implementation stays linear on a slow disk too.
def _elapsed(fn, *a, **k):
    start = time.perf_counter()
    fn(*a, **k)
    return time.perf_counter() - start


def test_appending_to_the_history_does_not_get_slower_as_it_grows(tmp_path):
    """The history is appended FOREVER, so an append that reads the file first is a defect
    that only shows up in year two. A 2,000-record history must cost the same per append as
    an empty one."""
    fresh = runprov.JsonlSink(tmp_path / "fresh.jsonl")
    grown = runprov.JsonlSink(tmp_path / "grown.jsonl")
    for i in range(2000):
        grown.append({"i": i, "pad": "x" * 500})

    rec = {"i": -1, "pad": "x" * 500}
    empty_cost = min(_elapsed(fresh.append, rec) for _ in range(20))
    grown_cost = min(_elapsed(grown.append, rec) for _ in range(20))

    assert grown_cost < empty_cost * 8 + 5e-4, (
        f"appending to a 2,000-record history cost {grown_cost * 1e6:.0f}us against "
        f"{empty_cost * 1e6:.0f}us for an empty one — the append is reading the file"
    )


def _big_view(n_artifacts, n_scripts=40):
    return {
        "runs": 100_000,
        "scripts": {
            f"step_{i:02d}": {
                "runs": 100, "ok": 100, "failed": 0, "first": "A", "last": "B",
                "script_file": f"src/step_{i:02d}.py",
                "inputs": {f"data/in_{j}.tsv": ["a" * 16] for j in range(3)},
                "outputs": {f"results/out_{j}.tsv": ["b" * 16] for j in range(3)},
                "note_keys": ["rows"], "parameters": ["mode"],
            }
            for i in range(n_scripts)
        },
        "artifacts": {
            f"results/out_{k:06d}.tsv": {
                "by": f"step_{k % 40:02d}", "digest": "c" * 16,
                "kind": "file", "when": "W", "status": "ok",
            }
            for k in range(n_artifacts)
        },
    }  # fmt: skip


def test_the_project_page_is_never_built_as_one_string(tmp_path):
    """L-39. The page was accumulated as a list of every line and joined at the end, so
    rendering cost about six times the text it produced — 57.7 MB of peak to emit 9.6 MB at
    100,000 artifacts. The waste scales with the artifact count, which is exactly the part
    of this page that grows over the years the history is meant to survive.

    `log` already learned this: `_timeline_entry` was split out so each record could be
    written as it streamed. Same reasoning, and here the unbounded half is the artifacts."""
    import tracemalloc

    view = _big_view(20_000)
    text = runprov.show.render_project(view)

    class Sink(io.TextIOBase):
        def writelines(self, lines):
            for _ in lines:
                pass

    tracemalloc.start()
    Sink().writelines(runprov.show.render_project_lines(view))
    peak = tracemalloc.get_traced_memory()[1]
    tracemalloc.stop()

    assert len(text) > 1_000_000, "the premise: this page is megabytes of text"
    assert peak < len(text) / 4, (
        f"rendering held {peak / 1e6:.1f} MB to write {len(text) / 1e6:.1f} MB of text — "
        f"the page is still being built before any of it is printed"
    )


def test_the_streamed_page_is_byte_for_byte_the_page(tmp_path, monkeypatch):
    """A renderer split for memory must render the same thing, or the fix is a rewrite. The
    string form is kept as a thin wrapper over the generator for exactly this reason: there
    is one implementation, so the two cannot drift."""
    rows = _history(tmp_path, monkeypatch)
    view = runprov.show.project_view(rows)
    states = runprov.show.staleness(rows)
    joined = "".join(runprov.show.render_project_lines(view, states))
    assert joined == runprov.show.render_project(view, states)
    assert joined.endswith("\n") and "project notebook" in joined
    assert all(ln.endswith("\n") for ln in runprov.show.render_project_lines(view, states)), (
        "lines carry their own newline so a caller can writelines() them unchanged"
    )


def test_lineage_counts_each_unreadable_line_once(tmp_path, capsys):
    """`lineage` walks the history TWICE (L-38), and `bad` accumulates across both — so a
    history with two torn lines reported "4 unreadable line(s) skipped". `log` and `show`
    read once and said 2, so the three commands disagreed about the same file.

    A reader who cannot trust the count of what was lost is worse off than one given no
    count at all, because the number looks like evidence. Introduced by the streaming
    rewrite and found by using the tool rather than by a ledger row."""
    p = tmp_path / "runs.jsonl"
    good = lambda i: json.dumps({"script": f"s{i}", "started_utc": f"2026-01-0{i}T00:00:00Z"})  # noqa: E731
    p.write_text(
        "\n".join([good(1), '{"script": "TORN", "pad": "xxx', good(2), "not json at all"]) + "\n",
        encoding="utf-8",
    )

    assert runprov.__main__.main(["lineage", "--log", str(p)]) == 0
    err = capsys.readouterr().err
    assert "2 unreadable line(s) skipped" in err, err
    assert "4 unreadable" not in err, "each damaged line is counted once, not once per pass"

    # And it agrees with the readers that walk the file once.
    assert runprov.__main__.main(["log", "--log", str(p)]) == 0
    assert "2 unreadable line(s) skipped" in capsys.readouterr().err


def test_lineage_does_not_hold_the_records_it_walks(tmp_path):
    """L-38. The comment here said lineage "genuinely needs every record at once ... there is
    nothing to stream past". That was a claim about the RECORDS, and the join is over
    DIGESTS: it needs an index, not a copy of the history. Measured at 40,000 chained runs,
    166 MB against 58 MB, for byte-identical output.

    The confident comment is the reason this survived review — it told the next person not to
    look. Counting reachable records DURING the walk is the same technique L-04 and L-37 use,
    and for the same reason: the list would die with the frame and prove nothing after."""
    import gc
    import weakref

    class Rec(dict):
        pass

    seen: list[weakref.ref] = []
    held: list[int] = []
    n = 200

    def stream():
        prev = None
        for i in range(n):
            d = f"{i:064x}"
            rec = Rec(
                script=f"s{i}",
                run_uid=f"{i:032x}",
                # MONOTONIC, not `i % 60`: rule 2 says a producer must have finished before
                # its consumer started, so a wrapping clock makes some producers look later
                # than the runs that read them and silently drops those edges.
                started_utc=f"2026-08-01T{i // 3600:02d}:{i // 60 % 60:02d}:{i % 60:02d}Z",
                finished_utc=f"2026-08-01T{i // 3600:02d}:{i // 60 % 60:02d}:{i % 60:02d}Z",
                parameters={"pad": "x" * 300},
                inputs=([{"path": "a", "sha256": prev}] if prev else []),
                outputs=[{"path": "a", "sha256": d}],
            )
            prev = d
            seen.append(weakref.ref(rec))
            yield rec
            del rec
            if i == n - 1:
                gc.collect()
                held.append(sum(1 for w in seen if w() is not None))

    # Two passes, so the source must be re-iterable — a generator would give an empty second
    # pass. This shim hands `_lineage` a fresh generator on each `__iter__`, which is exactly
    # the contract `records()` depends on.
    class Reiterable:
        def __iter__(self):
            return stream()

    cli = runprov.__main__
    g = cli._lineage(Reiterable())
    assert g["runs"] == n and g["resolvable"] == n - 1, g
    assert held[0] <= 2, f"{held[0]} of {n} records held to join on digests"


def test_the_lineage_json_gains_no_new_key(tmp_path):
    """`lineage --format json` is a documented output somebody parses. The script-name map
    that replaced the records is an OUT-PARAMETER precisely so it does not land in that
    document — putting it there would trade a memory problem for a bigger file, adding one
    entry per run to the thing a consumer reads."""
    cli = runprov.__main__
    rows = [
        {
            "script": "a",
            "run_uid": "u1",
            "started_utc": "2026-01-01T00:00:00Z",
            "finished_utc": "2026-01-01T00:00:00Z",
            "outputs": [{"path": "x", "sha256": "d1"}],
        },
        {
            "script": "b",
            "run_uid": "u2",
            "started_utc": "2026-01-01T00:00:01Z",
            "inputs": [{"path": "x", "sha256": "d1"}],
        },
    ]
    names: dict[str, str] = {}
    g = cli._lineage(rows, None, names)
    assert set(g) == {"runs", "records_without_uid", "resolvable", "ambiguous", "orphan", "edges"}
    assert g["edges"] == [("u1", "u2")] and g["resolvable"] == 1
    assert names == {"u1": "a", "u2": "b"}, "the names come back beside the graph, not inside it"
    assert "a [u1" in cli._render_lineage(g, names), "and the text view still names the scripts"


def test_appending_to_the_transformation_log_does_not_get_slower_as_it_grows(tmp_path):
    """The same rule as the history beside it, and the reason this file is APPENDED rather
    than regenerated. Rewriting the whole YAML after each run would read the entire history
    to write one entry — O(n) per run and O(n^2) over a project, which is the cost that
    surfaces in year two, on precisely the long-lived record this file is for."""
    fresh = runprov.sinks.YamlLogSink(tmp_path / "fresh.yml")
    grown = runprov.sinks.YamlLogSink(tmp_path / "grown.yml")
    rec = {"script": "s", "started_utc": "2026-01-01T00:00:00Z", "parameters": {"pad": "x" * 400}}
    for i in range(2000):
        grown.append({**rec, "run_id": f"r{i}"})

    empty_cost = min(_elapsed(fresh.append, rec) for _ in range(20))
    grown_cost = min(_elapsed(grown.append, rec) for _ in range(20))

    assert grown_cost < empty_cost * 8 + 5e-4, (
        f"appending to a 2,000-entry transformation log cost {grown_cost * 1e6:.0f}us "
        f"against {empty_cost * 1e6:.0f}us for an empty one — it is rewriting the file"
    )


def test_the_project_page_does_a_fixed_amount_of_work_per_record(tmp_path):
    """L-40. This REPLACES a wall-clock ratio test (`large < small * 9`), which was the only
    timing test in this section with no absolute slack term and the only one that flaked.

    THE SHAPE WAS UNFIXABLE, NOT THE CONSTANTS, and that was established by measurement
    rather than assumed. On 8 cores under 12-way load the original failed twice in eight runs
    at 16.7x and 9.1x. Adding the missing floor, raising the repeats from 3 to 7 and scaling
    the fixtures up did not fix it. Widening the sizes from 4x to 10x made it worse — ratios
    ranged 1.79 to 104.97 — because `min()` over repeats can find a clean run for the SMALL
    measurement, which fits inside a scheduling quantum, and cannot for the large one. The
    ratio then inflates without bound. At 4x sizing linear is 4x and quadratic is 16x, so the
    bound must sit between them, and under contention the noise alone spans that entire gap:
    no threshold separates the two hypotheses. Both reviewers proposed widening to ~12x,
    which would have stopped catching quadratic altogether (16x > 12x).

    A ratio is also blind to a CONSTANT FACTOR by construction: a change that doubles the
    cost at every size leaves it exactly where it was. Simulated during the audit, a 2x
    regression scored 5.58 against a threshold of 9 and passed the whole performance section.
    Reproduced here: a 2x-per-entry regression passes the ratio test and fails this one.

    COUNTING IS DETERMINISTIC. Two counters, and between them they cover what the clock was
    supposed to:

      * `records consumed` catches re-walking the input, which is the classic quadratic —
        the page must touch each record exactly once, however many it is given;
      * `_short`/`_name` calls per record catches extra work per entry — exactly 6.00 at
        every size, because they run once per input and once per output.

    Neither can be moved by CPU load, an allocator or a slow disk, which is what the section
    header has always asked for: shapes, never absolute numbers."""
    per_record = 6  # 3 inputs+outputs x 2 helpers, per record — see the docstring

    def calls(n):
        rows = [
            {
                "script": f"s{i % 20}",
                "status": "ok",
                "started_utc": "2026-01-01T00:00:00Z",
                "inputs": [{"path": f"in/{i % 50}.tsv", "sha256": f"{i:064x}"}],
                "outputs": [{"path": f"out/{i}.tsv", "sha256": f"{i:064x}", "kind": "file"}],
            }
            for i in range(n)
        ]
        counted = [0]
        consumed = [0]

        def once():
            """The records, yielded one at a time and COUNTED. An implementation that walks
            the history twice — or that indexes back into it — consumes more than `n`."""
            for r in rows:
                consumed[0] += 1
                yield r

        real_short, real_name = runprov.show._short, runprov.show._name
        runprov.show._short = lambda e: (counted.__setitem__(0, counted[0] + 1), real_short(e))[1]
        runprov.show._name = lambda e: (counted.__setitem__(0, counted[0] + 1), real_name(e))[1]
        try:
            runprov.show.project_view(once())
        finally:
            runprov.show._short, runprov.show._name = real_short, real_name
        return counted[0], consumed[0]

    for n in (500, 2000, 8000):
        ops, seen = calls(n)
        assert seen == n, (
            f"the page consumed {seen} records to render {n} — it is walking the history "
            f"more than once, which is the classic quadratic"
        )
        assert ops == per_record * n, (
            f"{ops / n:.2f} operations per record at n={n}, not {per_record} — "
            f"the page is doing more work per run than it used to"
        )


def test_reading_a_pin_does_not_read_the_whole_artifact(tmp_path):
    """`verify` opens every candidate file in a tree. It reads a bounded prefix looking for
    the anchor, so a 50 GB BAM costs the same as a 1 KB TSV -- and a regression here turns
    a check into a full-corpus read."""
    small = tmp_path / "small.bin"
    small.write_bytes(b"\x00" * 4096)
    large = tmp_path / "large.bin"
    large.write_bytes(b"\x00" * (64 * 1024 * 1024))

    small_cost = min(_elapsed(runprov.verify.read_pins, small) for _ in range(5))
    large_cost = min(_elapsed(runprov.verify.read_pins, large) for _ in range(5))

    assert large_cost < small_cost * 20 + 5e-3, (
        f"a 64 MB file cost {large_cost * 1e3:.1f}ms against {small_cost * 1e3:.1f}ms for "
        f"4 KB — read_pins is reading past SCAN_BYTES ({runprov.verify.SCAN_BYTES} bytes)"
    )


def test_content_digest_memory_does_not_grow_with_the_file(tmp_path):
    """The property the whole streaming design exists for, guarded as a ratio. Both the line
    COUNT and the line LENGTH are varied, because a block bounded by only one of them looks
    streamed until the other moves -- which is how the byte bound came to be needed."""
    import tracemalloc

    def peak(records, width):
        p = tmp_path / f"f{records}x{width}.txt"
        with open(p, "w", encoding="utf-8") as fh:
            for i in range(records):
                fh.write(f"{i}\t" + "y" * width + "\n")
        tracemalloc.start()
        try:
            assert runprov.content_digest(p)
            return tracemalloc.get_traced_memory()[1]
        finally:
            tracemalloc.stop()

    # ABOVE both block bounds (8,192 lines and 8 MiB), or all three fixtures fit in ONE
    # block and the test measures a single read three times -- 4x the lines took 4.0x the
    # memory, which looked like a regression and was a fixture too small to stream.
    base = peak(20_000, 500)
    more_lines = peak(80_000, 500)
    longer_lines = peak(20_000, 2_000)

    assert more_lines < base * 2, f"4x the LINES took {more_lines / base:.1f}x the memory"
    assert longer_lines < base * 2, f"4x the LINE LENGTH took {longer_lines / base:.1f}x"


def test_constructing_a_run_does_not_scale_with_the_history(tmp_path, monkeypatch):
    """A `Run` reads nothing of the existing history at construction, and must not start:
    the 2,000th step of a pipeline must cost what the first one did."""
    monkeypatch.chdir(tmp_path)
    proj = _project(tmp_path)
    log = tmp_path / "runs.jsonl"
    with log.open("w", encoding="utf-8") as fh:
        for i in range(3000):
            fh.write(json.dumps({"i": i, "pad": "x" * 600}) + "\n")

    grown = min(_elapsed(runprov.Run, "s", project=proj) for _ in range(5))
    log.unlink()
    empty = min(_elapsed(runprov.Run, "s", project=proj) for _ in range(5))

    assert grown < empty * 3 + 5e-3, (
        f"construction cost {grown * 1e3:.1f}ms with a 3,000-record history against "
        f"{empty * 1e3:.1f}ms with none — something is reading it"
    )


def _project_view_peak(runs, artifacts, scripts=20):
    """Peak allocation while aggregating `runs` records naming `artifacts` distinct outputs.

    The two are separated on purpose. `project_view`'s memory is driven by the size of the
    AGGREGATE — distinct scripts and distinct artifact paths — and not by how many records
    streamed past to build it. A fixture that moves both at once cannot tell which.
    """
    import tracemalloc

    def stream():
        for i in range(runs):
            yield {
                "script": f"s{i % scripts}",
                "status": "ok",
                "started_utc": "2026-01-01T00:00:00Z",
                # `run_uid` IS PRESENT, and it is per-run and distinct. Without it the
                # records here were unlike every real record, and a mutation that hoarded
                # one value per run into the aggregate accumulated 20,000 `None`s -- a few
                # bytes each, under the bound, invisible. The fixture has to carry the
                # fields a regression would plausibly hoard.
                "run_uid": f"{i:032x}",
                "inputs": [{"path": f"in/{i % 50}.tsv", "sha256": f"{i:064x}"}],
                "outputs": [{"path": f"out/{i % artifacts}.tsv", "sha256": f"{i:064x}"}],
            }

    tracemalloc.start()
    try:
        view = runprov.show.project_view(stream())
        assert view["runs"] == runs
        return tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()


def test_the_project_page_does_not_grow_with_the_number_of_RUNS(tmp_path):
    """L-41. This test was called "does not hold the history in memory", which is a stronger
    claim than it checks — and its fixture pinned artifacts at `i % 100` and scripts at 20,
    so the aggregate was CONSTANT BY CONSTRUCTION and the measurement flat whatever `n` was:
    0.12 MB at 5,000 runs, 0.11 MB at 20,000, 0.11 MB at 80,000. The same numbers would
    appear for an implementation that held everything except the artifact index.

    What it genuinely guards is worth keeping and is now what it is named for: nothing
    proportional to the RUN COUNT survives the aggregation. The artifact count is held fixed
    so that is the only variable, and the growth it cannot see is asserted by the test below
    rather than left to be discovered at 100,000 artifacts.

    Measured on a realistic 100,000-run, 91 MB history: materialising every record cost
    392 MB, streaming it costs 3. `project_view` consumes an ITERABLE and counts as it goes;
    asking for `len(records)` would materialise the history this exists not to hold."""
    small, large = _project_view_peak(5_000, 1_000), _project_view_peak(20_000, 1_000)
    assert large < small * 2, (
        f"4x the runs took {large / small:.1f}x the memory at a FIXED artifact count — "
        f"project_view is keeping something per record"
    )


def test_the_project_page_grows_with_the_ARTIFACT_count_and_only_linearly(tmp_path):
    """The other half, and the one that was invisible. The aggregate IS an index of every
    distinct artifact, so it grows — that is the design, not a defect, and the cost is
    stated here rather than found later: 3.35 MB at 5,000 artifacts, 13.29 MB at 20,000,
    53.59 MB at 80,000, which is linear and is what a project writing one artifact per run
    actually pays.

    Asserting it BOTH grows and grows only linearly is what makes this a test rather than a
    number: a fixture that could not see the growth would satisfy the upper bound trivially,
    which is exactly how the test above came to certify more than it checked."""
    small, large = _project_view_peak(20_000, 5_000), _project_view_peak(80_000, 20_000)
    assert large > small * 2, (
        f"4x the artifacts took only {large / small:.1f}x the memory — the fixture is not "
        f"exercising the artifact index, so the bound below proves nothing"
    )
    assert large < small * 6, (
        f"4x the artifacts took {large / small:.1f}x the memory — the index is growing "
        f"faster than the thing it indexes"
    )


def test_reading_the_history_streams_rather_than_slurping(tmp_path):
    """`_stream` yields a record at a time. The first version did
    `read_text().splitlines()`, holding the whole file AND a list of every line before one
    record was parsed -- two full copies of a file whose design is that it never stops
    growing. Same defect `content_digest` had, in the function that meets the biggest file.
    """
    import tracemalloc

    target = tmp_path / "runs.jsonl"
    with target.open("w", encoding="utf-8") as fh:
        for i in range(20_000):
            fh.write(json.dumps({"i": i, "pad": "x" * 900}) + "\n")
    size = target.stat().st_size
    assert size > 15_000_000, "the fixture must be big enough for slurping to show"

    tracemalloc.start()
    try:
        count = sum(1 for rec in runprov.__main__._stream(target) if rec is not None)
        peak = tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()

    assert count == 20_000
    assert peak < size / 4, (
        f"peak {peak / 1e6:.0f} MB against a {size / 1e6:.0f} MB file — it is reading it whole"
    )


def test_the_streaming_peak_is_bounded_by_the_largest_RECORD_not_the_file(tmp_path):
    """L-73. The test above varies the record COUNT at a fixed record size, so it cannot see
    that `_stream`'s peak is bounded by the largest RECORD rather than by a constant — the
    exact blind spot `test_content_digest_streams_files_whose_LINES_are_long` was written to
    close for `content_digest`, left open for the history reader one module over.

    `_stream`'s docstring promises "every line of the history, parsed, one at a time", which
    is true and is NOT the same as bounded. A three-run history containing one enormous note
    costs twice that note, however few runs there are.

    Measured, and the ratio is stable rather than a fixture artefact: 2.01x the largest
    record at 5 MB and 2.01x at 20 MB. The bound asserted here is 3x, which states the real
    guarantee with room for the parse, and would fail a reader that held the whole file."""
    import tracemalloc

    # ONE big record, ISOLATED. A first draft used two adjacent large records to make
    # buffering visible and measured 4.0x rather than 2.0x -- because the consumer still
    # holds the record it is on while the next line is read and parsed, so two large records
    # side by side are both alive at once. That is a true and looser bound; the tight one is
    # per-record, and this test asserts the tight one. See `_stream` for both numbers.
    target = tmp_path / "runs.jsonl"
    with target.open("w", encoding="utf-8") as fh:
        fh.write(json.dumps({"i": 0, "pad": "small"}) + "\n")
        fh.write(json.dumps({"i": 1, "note": "y" * 5_000_000}) + "\n")
        fh.write(json.dumps({"i": 2, "pad": "small"}) + "\n")

    lines = target.read_text(encoding="utf-8").splitlines()
    largest = max(len(ln) for ln in lines)
    assert len(lines) == 3, "the premise: a SHORT history, so record count cannot explain it"

    tracemalloc.start()
    try:
        count = sum(1 for rec in runprov.__main__._stream(target) if rec is not None)
        peak = tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()

    assert count == 3
    assert peak < largest * 3, (
        f"peak {peak / 1e6:.1f} MB for a largest record of {largest / 1e6:.1f} MB — the "
        f"reader is holding more than the record it is on"
    )


def test_an_unreadable_line_is_still_counted_when_streaming(tmp_path):
    """The count is what stops a corrupt line from being silently dropped, and it has to
    survive the switch from a list to a generator."""
    target = tmp_path / "runs.jsonl"
    target.write_text('{"i": 0}\nnot json at all\n\n{"i": 1}\n', encoding="utf-8")
    assert list(runprov.__main__._stream(target)) == [{"i": 0}, None, {"i": 1}]
    rows, bad = runprov.__main__._load(target)
    assert [r["i"] for r in rows] == [0, 1] and bad == 1


def test_show_reports_unreadable_lines_it_streamed_past(tmp_path, capsys):
    """`bad` is accumulated as the stream runs, so the summary can still say what it could
    not read — a reader must not be told 'N runs' when it was N plus something broken."""
    target = tmp_path / "runs.jsonl"
    target.write_text(
        json.dumps({"script": "s", "status": "ok", "outputs": [], "inputs": []}) + "\ntorn{\n",
        encoding="utf-8",
    )
    assert runprov.__main__.main(["show", "--log", str(target)]) == 0
    assert "1 unreadable line(s) skipped" in capsys.readouterr().err


def test_log_limit_holds_only_the_limit_not_the_history(tmp_path):
    """`--limit N` keeps a deque of N and nothing else. The 99,900 runs before them do not
    need to be in memory to be skipped.

    A ratio, not a threshold: 4x the history behind the same `--limit` must not cost 4x the
    memory, because what is kept is N either way.
    """
    import tracemalloc

    def peak_for(n):
        target = tmp_path / f"h{n}.jsonl"
        with target.open("w", encoding="utf-8") as fh:
            for i in range(n):
                fh.write(
                    json.dumps(
                        {
                            "script": "s",
                            "status": "ok",
                            "i": i,
                            "pad": "x" * 700,
                            "inputs": [],
                            "outputs": [],
                        }
                    )
                    + "\n"
                )
        args = argparse.Namespace(
            log=str(target), format="jsonl", limit=5, script="", run_id="", failed=False
        )
        tracemalloc.start()
        try:
            assert runprov.__main__._log(args, target) == 0
            return tracemalloc.get_traced_memory()[1]
        finally:
            tracemalloc.stop()

    small, large = peak_for(5_000), peak_for(20_000)
    assert large < small * 2, (
        f"4x the history cost {large / small:.1f}x the memory behind the same --limit"
    )


def test_log_never_writes_to_the_history(tmp_path, capsys):
    """`--limit` is a VIEW over the history, not a trim of it. `log` reads; it has never
    written. The records it does not show are exactly where they were, and the next command
    without `--limit` shows them again."""
    target = tmp_path / "runs.jsonl"
    with target.open("w", encoding="utf-8") as fh:
        for i in range(50):
            fh.write(
                json.dumps({"script": f"s{i}", "status": "ok", "inputs": [], "outputs": []}) + "\n"
            )
    before, size = hashlib.sha256(target.read_bytes()).hexdigest(), target.stat().st_size

    assert runprov.__main__.main(["log", "--log", str(target), "--limit", "3"]) == 0
    shown = capsys.readouterr()
    assert shown.out.count("run_id") == 3, "three shown"
    assert "3 of 50 run(s)" in shown.err, "and it says what it did not show"

    assert hashlib.sha256(target.read_bytes()).hexdigest() == before
    assert target.stat().st_size == size

    assert runprov.__main__.main(["log", "--log", str(target)]) == 0
    assert capsys.readouterr().out.count("run_id") == 50, "all 50 are still there"


def test_log_streams_every_format_and_the_yaml_header_appears_once(tmp_path, capsys):
    """Streaming means writing each record as it passes, so the banner must be emitted by
    the command rather than by the renderer -- once, not once per run."""
    yaml = pytest.importorskip("yaml")
    target = tmp_path / "runs.jsonl"
    with target.open("w", encoding="utf-8") as fh:
        for i in range(4):
            fh.write(
                json.dumps(
                    {
                        "script": f"s{i}",
                        "status": "ok",
                        "started_utc": "2026-01-01",
                        "command": "c",
                        "inputs": [],
                        "outputs": [],
                    }
                )
                + "\n"
            )

    assert runprov.__main__.main(["log", "--log", str(target), "--format", "yaml"]) == 0
    out = capsys.readouterr().out
    assert out.count("# GENERATED by") == 1, "the banner is not per record"
    assert len(yaml.safe_load(out)) == 4

    assert runprov.__main__.main(["log", "--log", str(target), "--format", "jsonl"]) == 0
    assert len(capsys.readouterr().out.strip().splitlines()) == 4


def test_log_filters_compose_with_limit_while_streaming(tmp_path, capsys):
    """The filters are applied as records stream past, and `--limit` takes the last N of
    what SURVIVED them — not the last N of the file."""
    target = tmp_path / "runs.jsonl"
    with target.open("w", encoding="utf-8") as fh:
        for i in range(30):
            fh.write(
                json.dumps(
                    {
                        "script": "wanted" if i % 3 == 0 else "other",
                        "status": "failed" if i % 2 == 0 else "ok",
                        "run_id": "chain" if i > 20 else "old",
                        "inputs": [],
                        "outputs": [],
                    }
                )
                + "\n"
            )

    assert (
        runprov.__main__.main(
            [
                "log",
                "--log",
                str(target),
                "--script",
                "wanted",
                "--failed",
                "--limit",
                "2",
                "--format",
                "jsonl",
            ]
        )
        == 0
    )
    seen = capsys.readouterr()
    rows = [json.loads(x) for x in seen.out.strip().splitlines()]
    assert len(rows) == 2
    assert all(r["script"] == "wanted" and r["status"] == "failed" for r in rows)
    assert "2 of 30 run(s)" in seen.err and "2 FAILED" in seen.err


def test_log_counts_a_torn_line_it_streamed_past(tmp_path, capsys):
    """The count is what stops a corrupt line from being silently dropped, and it has to
    survive `log` becoming a stream — a reader must not be told "N runs" when it was N plus
    something unreadable."""
    target = tmp_path / "runs.jsonl"
    target.write_text(
        json.dumps({"script": "a", "status": "ok", "inputs": [], "outputs": []})
        + "\n"
        + "{torn, not json\n"
        + json.dumps({"script": "b", "status": "ok", "inputs": [], "outputs": []})
        + "\n",
        encoding="utf-8",
    )
    assert runprov.__main__.main(["log", "--log", str(target), "--format", "jsonl"]) == 0
    seen = capsys.readouterr()
    assert len(seen.out.strip().splitlines()) == 2, "the two good records still render"
    assert "2 of 2 run(s)" in seen.err
    assert "1 unreadable line(s) skipped" in seen.err


def test_our_own_teardown_is_not_refused_by_the_after_exit_guard(tmp_path, monkeypatch):
    """The after-exit guard fired on `__exit__`'s OWN calls and the record was never written.

    `_in_context` is already False by the time `_finish` runs -- deliberately, because
    `_note_unregistered_reads` relies on it to avoid hashing every first-party file twice --
    so a guard reading `_in_context` alone cannot tell our teardown from a caller reaching in
    after the block. `_finish` -> `write` -> `environment_snapshot` and
    `_record_imported_code` -> `code` are all guarded methods called from `__exit__`.

    Measured: runs.jsonl did not exist at all, and the only trace was a WARNING on stderr.
    The mechanism for refusing a call that reaches no file became the reason nothing reached
    a file.
    """
    monkeypatch.chdir(tmp_path)
    log = tmp_path / "runs.jsonl"
    runprov.configure(root=tmp_path, run_log=log, env_snapshot_dir=tmp_path / "envs")
    with runprov.Run("s", provenance=tmp_path / "p.json"):
        pass

    assert log.is_file(), "the history line was never written"
    line = json.loads(log.read_text(encoding="utf-8").splitlines()[-1])
    assert line["environment_snapshot"]["path"].endswith(".txt")
    assert (tmp_path / "p.json").is_file()
