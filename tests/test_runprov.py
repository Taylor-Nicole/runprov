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

import builtins
import io
import json
import pathlib
import re
import sys
import textwrap

import pytest

REPO = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

import runprov  # noqa: E402
import runprov.__main__ as cli  # noqa: E402
import runprov._report  # noqa: E402
import runprov.environment  # noqa: E402


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
    a.write_text(json.dumps({"started_utc": "2026-01-01T00:00:00Z", "n": 5}))
    b.write_text(json.dumps({"started_utc": "2099-12-31T23:59:59Z", "n": 5}))
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
    assert run.record["schema"] == runprov.SCHEMA == "runprov.run.v1"
    run.write(tmp_path / "p.json")
    assert sink.records[0]["schema"] == runprov.SCHEMA


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
    assert "read_text()" not in code and "read_bytes()" not in code
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


def test_the_caller_file_falls_back_to_none_when_every_frame_is_internal(monkeypatch):
    monkeypatch.setattr(runprov.run.inspect, "stack", lambda: [])
    assert runprov.run._caller_file() is None


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
    assert runprov.SCHEMA == "runprov.run.v1", "no field changed meaning, so no bump"


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
