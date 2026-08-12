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

import ast
import builtins
import gzip
import hashlib
import importlib
import inspect
import io
import itertools
import json
import os
import pathlib
import re
import subprocess
import sys
import tarfile
import textwrap
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


def test_a_second_write_does_not_double_count_the_run(tmp_path):
    """One run is one history line, or every count taken from the history is wrong."""
    sink = runprov.MemorySink()
    proj = runprov.Project(root=tmp_path, sink=sink, run_id=lambda: "r", generation=lambda: "g")
    run = runprov.Run("t", project=proj)
    run.write(tmp_path / "a.json")
    run.write(tmp_path / "b.json")
    assert len(sink.records) == 1
    assert (tmp_path / "a.json").is_file() and (tmp_path / "b.json").is_file()


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


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="chmod(0o000) does not deny directory traversal on Windows, so the condition "
    "under test cannot be created there",
)
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


def test_an_unserialisable_note_does_not_lose_the_run(tmp_path, capsys):
    """A tuple-keyed dict is what a per-class confusion matrix looks like, and
    `default=str` does not apply to KEYS -- so json.dumps raised, the sidecar was never
    written, and the run vanished. Serialise before touching the filesystem."""
    sink = runprov.MemorySink()
    proj = runprov.Project(root=tmp_path, sink=sink, run_id=lambda: "r", generation=lambda: "g")
    run = runprov.Run("g", project=proj)
    run.note("confusion", {(1, "a"): 0.9})
    run.write(tmp_path / "p.json")
    assert len(sink.records) == 1, "the run must be recorded even if a note cannot encode"
    assert "could not serialise" in capsys.readouterr().err
    rec = json.loads((tmp_path / "p.json").read_text(encoding="utf-8"))
    assert rec["notes"]["confusion"].startswith("UNSERIALISABLE")


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


def test_one_run_is_one_history_line_across_two_with_blocks(tmp_path):
    sink, proj = _sinked(tmp_path)
    run = runprov.Run("z", project=proj, provenance=tmp_path / "p.json")
    with run:
        run.write(tmp_path / "p.json")
    with run:
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
    assert rec["notes"]["bad"].startswith("UNSERIALISABLE")


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

    This sees literals, not behaviour — a `sys.stdout.write` would slip past it — which is
    why the subprocess tests above exist as well. It is the cheap half of a pair.
    """
    offenders = {}
    for mod in sorted((REPO / "runprov").glob("*.py")):
        if mod.name in ("_report.py", "__main__.py"):
            continue
        hits = [
            f"{mod.name}:{n}"
            for n, line in enumerate(mod.read_text(encoding="utf-8").splitlines(), 1)
            if re.search(r"(?<![\w.])print\s*\(", line)
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
    assert "dirty state is UNKNOWN, not clean" in capsys.readouterr().err


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
def test_a_volatile_stamp_spanning_a_block_boundary_is_still_stripped(tmp_path):
    """R1. The substitution ran per 8,192-line block, so a `"started_utc": "..."` split
    across a boundary was never matched — and the artifact oscillated forever, which is the
    one thing `content_digest` exists to prevent."""

    def build(stamp):
        p = tmp_path / f"big_{stamp[:4]}.json"
        # It must be RUNPROV'S OWN record, because R2 scopes the stripping to those. The
        # two fixes interact and this test failed until it said so — which is the scoping
        # working, not a defect.
        pad = "".join(f'  "pad{i}": {i},\n' for i in range(8190))
        p.write_text(
            '{\n  "schema": "runprov.run.v2",\n' + pad + f'  "started_utc":\n    "{stamp}"\n}}\n',
            encoding="utf-8",
        )
        return p

    a, b = build("2026-01-01T00:00:00Z"), build("2099-12-31T23:59:59Z")
    assert runprov.content_digest(a) == runprov.content_digest(b), (
        "two runs differing only in a boundary-spanning stamp must pin identically"
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
    assert "more edge(s) not shown" in text, "the text view must say what it truncated"

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
    assert "predates script_file" in docs[0]["script"]
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
