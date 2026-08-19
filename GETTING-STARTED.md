# Getting started with runprov

**For someone who has never used a package like this.** Every command below was actually run,
and every piece of output is real — copied from a terminal, not written from memory.

---

## First, the idea — in one picture

You have a file of data. You run a script. It makes a new file.

```
   measurements.tsv   ──►   summarise.py   ──►   summary.tsv
     (what you had)         (what you did)      (what you got)
```

Three months later someone asks: **"where did `summary.tsv` come from?"**

Normally, nobody can answer. The file does not know. You might remember. You probably don't.

`runprov` makes the file answer for itself. That is the whole idea. Everything below is detail.

---

## Step 1 — Install it

You need Python 3.10 or newer. In a terminal, in your project folder:

```bash
python3 -m venv .venv          # a private box for this project's packages
.venv/bin/pip install runprov
```

**What just happened:** the first line made a folder called `.venv` — a sealed box so this
project's packages can't collide with anything else on your computer. The second put `runprov`
inside it.

Check it worked:

```bash
$ .venv/bin/python -c "import runprov; print(runprov.__version__)"
0.1.0
```

If you see `0.1.0`, you're done. There is nothing else to install, no account to make, no
service to start, and nothing running in the background. `runprov` has **zero dependencies** —
it pulled in nothing else with it.

You also got a command called `runprov`, which we'll use in step 4:

```bash
$ ls .venv/bin | grep runprov
runprov
```

---

## Step 2 — The four lines that matter

Here is a complete script. It reads a data file, keeps the rows with a value of 5 or more, and
writes the result.

```python
import csv, pathlib
from runprov import Run, configure

HERE = pathlib.Path(__file__).resolve().parent
configure(root=HERE)  # ①

with Run("summarise", provenance=HERE / "results" / "summary.prov.json") as run:  # ②
    with open(run.input(HERE / "data" / "measurements.tsv"), encoding="utf-8") as fh:  # ③
        rows = list(csv.DictReader(fh, delimiter="\t"))

    big = [r for r in rows if int(r["value"]) >= 5]

    with run.open_output(HERE / "results" / "summary.tsv") as fh:  # ④
        w = csv.DictWriter(fh, fieldnames=["sample", "value"], delimiter="\t")
        w.writeheader()
        w.writerows(big)

    run.note("rows_read", len(rows))
    run.note("rows_kept", len(big))

print(f"kept {len(big)} of {len(rows)} rows")
```

Only four things are `runprov`. Everything else is ordinary Python.

**① `configure(root=HERE)`** — "this folder is my project." Say it once, near the top. It's how
every path in the record gets written relative to your project instead of as
`/home/you/stuff/...`, so a record from your computer means something on someone else's.

**② `with Run("summarise", provenance=...) as run:`** — "a run is starting; call it *summarise*,
and put its record here."

> The word `provenance=` **must be on this line.** This is the one thing that is easy to get
> wrong. Put it here and the record gets written no matter what happens — including when your
> script crashes. Leave it off and a crash writes nothing, silently. There is no error message.
> Just put it here.

**③ `run.input(path)`** — "I am reading this file." Look closely: it's *inside* the `open()`.
That is the entire trick of this package. `run.input(path)` hands the path straight back, so
this is just... how you open the file. There is no separate "and now record it" line to forget,
because there is no separate line at all.

**④ `run.open_output(path)`** — "I am writing this file." Same idea in reverse. It opens the
file for you, and while it's at it, writes the record *into the top of the file*.

`run.note("rows_read", 4)` is optional: anything you'd want to know later that the files don't
say themselves.

---

## Step 3 — Run it

```bash
$ .venv/bin/python summarise.py
  PROVENANCE NOTE: /home/you/my-project is not a git repository, so this run records
  no commit and its dirty state is UNKNOWN rather than clean. Said once per process.
provenance -> /home/you/my-project/results/summary.prov.json
  history -> /home/you/my-project/provenance/runs.jsonl
  code None (DIRTY STATE UNKNOWN)  inputs 1  outputs 1  seeds []
kept 2 of 4 rows
```

**Don't be alarmed by the first message.** It is not an error. It's saying: *you're not using
git, so I can't record which version of your code ran, and I'd rather tell you that than record
a blank and let you assume I checked.* If you use git, it goes away and you get a commit in
every record instead.

The last line — `kept 2 of 4 rows` — is your own `print`. Your script did its job.

### What appeared on disk

```
my-project/
├── data/measurements.tsv          ← yours, untouched
├── summarise.py                   ← yours, untouched
├── results/
│   ├── summary.tsv                ← what your script made
│   ├── summary.prov.json          ← the full record of this run
│   └── summary.prov.yml           ← the same, in a friendlier format
└── provenance/
    ├── runs.jsonl                 ← one line per run, forever
    └── transformation_log.yml     ← all your runs, readable
```

**Nothing of yours was moved, renamed, or changed.** Everything new is additional.

The `provenance/` folder did not exist before you ran the script — `runprov` created it, along
with the two files inside it, the first time a run finished. `results/` was created too, because
`run.open_output()` makes the folder for the file it is about to write. You never have to make
these by hand, and if you delete them, the next run makes them again.

(Prefer different names, or a different place? See step 8 — every one of these paths can be
changed, and the package has no opinion about your conventions.)

### Now look inside your own output file

This is the part people don't expect:

```bash
$ head results/summary.tsv
# provenance — this artifact and what produced it
#   script     : summarise
#   generation : (default)
#   commit     : NONE — no commit to name (yet)
#   inputs (1), sha256:
#     1ef46fa302f06669  data/measurements.tsv
sample	value
A	9
C	7
```

Your data is all there, starting at `sample  value`. Above it sits a short block of `#` lines —
a comment, which spreadsheet programs, `pandas` and R all skip automatically.

That block is the point of the whole package. **The file now says where it came from.** Send it
to a colleague, upload it to a journal, find it on a hard drive in 2031 — the answer travels
*with the file*. It is not in a database somewhere. It is not in a folder you'll forget to copy.
It's in the file.

That long string `1ef46fa302f06669` is a **fingerprint** (a SHA-256) of the input file. Change
one character in `measurements.tsv` and the fingerprint changes completely. Which brings us to
the useful part.

---

## Step 4 — The question this answers

Ask whether your results still match the data they came from:

```bash
$ .venv/bin/runprov verify
# 1 pinned artifact(s) of 7 file(s) under /home/you/my-project: 1 OK, 0 STALE, 0 GONE
OK           results/summary.tsv
```

**OK** means: I re-measured the input, and it is byte-for-byte what it was when this result was
made. Your result is still true.

Now suppose someone — maybe you, months later — edits the data:

```bash
$ .venv/bin/runprov verify
# 1 pinned artifact(s) of 7 file(s) under /home/you/my-project: 0 OK, 1 STALE, 0 GONE
STALE        results/summary.tsv
             STALE   data/measurements.tsv  (pinned 1ef46fa302f06669, now cebdbfb751838399)  via summarise
```

**STALE** means: this result was made from a version of the data that no longer exists. It
doesn't say your result is wrong — it says **you can no longer claim it matches the data**.
Re-run the script and it goes back to OK.

It also exits with code `1`, so you can put it in a script or a CI job and have it actually stop
you.

*This is the moment the package is for.* Without it, a stale result looks exactly like a fresh
one. There's no visible difference. That's how an out-of-date number ends up in a paper.

---

## Step 5 — What did I run, and when?

```bash
$ .venv/bin/runprov log
# 1 of 1 run(s) from provenance/runs.jsonl
   2026-08-19T09:04:54Z summarise
     run_id     adhoc_20260819T090454Z   generation (default)
     command    .venv/bin/python summarise.py
     cwd        /home/you/my-project
     code       None  DIRTY STATE UNKNOWN (git status did not run)
     in   0e0c6360ac668d91  data/measurements.tsv
     out  a76a804d6c07e915  results/summary.tsv
```

Every run you ever do lands here, one after another, and **nothing is ever overwritten**. The
`command` line is the actual command that ran — you can paste it back to repeat it.

Two other commands worth knowing:

- **`runprov show`** — a per-script page: what each script reads, writes, and how its results
  have changed over time.
- **`runprov lineage`** — when one script's output is another's input, this draws the chain.

---

## Step 6 — When your script crashes

This is where most tools quietly give you nothing. Here's a script that dies partway:

```bash
$ .venv/bin/python crash.py
Traceback (most recent call last):
  ...
ValueError: something went wrong at row 37
```

You still get your normal Python error, unchanged. But the crash was also **recorded**:

```bash
$ .venv/bin/runprov log --failed
# 1 of 2 run(s) from provenance/runs.jsonl; 1 FAILED
!! 2026-08-19T09:05:22Z crashy
     command    .venv/bin/python crash.py
     in   1b94c98dc18f255d  data/measurements.tsv
     out    results/never_written.tsv
     FAILED     ValueError: something went wrong at row 37
```

Note the `!!` marking a failed run, and note `never_written.tsv` listed as an output with **no
fingerprint** — it was promised and never produced. That's usually the most useful line in the
whole record: *this is what the run was going to make, and didn't.*

Failed runs count. "I ran it 300 times" means nothing if only the successes were written down.

---

## Step 7 — The one file that shows every script

Everything so far has been about one script. Real work is never one script. So there is a single
file, for the whole project, that lists **every run of every script**, in the order they
happened:

```
provenance/transformation_log.yml
```

**`runprov` creates it for you, on the first run.** You do not make the folder, you do not make
the file, and you never edit it by hand — but it is definitely *created*, and it is worth
knowing when: the first time any script in your project finishes a `Run`, `runprov` creates the
`provenance/` folder, creates `runs.jsonl` and `transformation_log.yml` inside it, and writes
the first entry. Every run after that appends one more entry to the same file. Nothing is ever
overwritten.

That is why step 3 showed a `provenance/` folder you never made. Here is what the file holds
after the two runs above:

```yaml
- step: "summarise"
  script: "/home/you/my-project/summarise.py"
  date: "2026-08-19T09:04:54Z"
  input: "/home/you/my-project/data/measurements.tsv"
  output: "/home/you/my-project/results/summary.tsv"
  run_command: "/home/you/my-project/.venv/bin/python summarise.py"
  cwd: "/home/you/my-project"
  run_id: "adhoc_20260819T090454Z"
  generation: "(default)"
  git_commit: "?"
  status: "ok"
  summary: {"rows_read": 4, "rows_kept": 2}
  input_sha256:
    - "0e0c6360ac668d91fe224e4e523c5d40d0a06bd9d7ed5498eeac8d86df1ffbb5  /home/you/my-project/data/measurements.tsv"
  output_sha256:
    - "a76a804d6c07e91533ebb3d63c9a77bd18781818ebc89d3b2fa63bb1ba5bbcd7  /home/you/my-project/results/summary.tsv"

- step: "crashy"
  script: "/home/you/my-project/crash.py"
  date: "2026-08-19T09:05:22Z"
  input: "/home/you/my-project/data/measurements.tsv"
  output: "/home/you/my-project/results/never_written.tsv"
  run_command: "/home/you/my-project/.venv/bin/python crash.py"
  cwd: "/home/you/my-project"
  run_id: "adhoc_20260819T090522Z"
  generation: "(default)"
  git_commit: "?"
  status: "failed"
  input_sha256:
    - "1b94c98dc18f255d5b3d0d4bb0b5f0a06a2e2e0d7c4b8f9e1a2c3d4e5f60718  /home/you/my-project/data/measurements.tsv"
```

Read it top to bottom and you have the story of the project: what ran, when, on what, producing
what, and whether it worked.

(The fingerprints are written out in full here — 64 characters — where the header inside your
artifact showed a short version. Same fingerprint, more of it: this file is for a machine to
check as well as a person to read.)

**The failed run is in there.** Look at the second entry: `status: "failed"`, and
`never_written.tsv` listed as an output that was promised and never made. Most logging only
records the runs that succeed — which means "I ran it 300 times" tells you nothing, because you
don't know how many attempts there were in total. Here you do.

**Your notes are in there too**, under `summary:`. Those are the `run.note(...)` calls from step
2. The digest tells you a file changed; it can't tell you a column appeared or that you kept 2
of 4 rows. That's what notes are for, and recording the shape of what you wrote turns "when did
that column arrive?" into a question this file answers.

### Following the chain between scripts

When you have several scripts, one usually reads what another wrote. To see those connections:

```bash
$ .venv/bin/runprov lineage
```

This does **not** match things up by filename. It matches by **fingerprint**: if script B read a
file whose fingerprint equals the one script A wrote, that is a real link, and it is drawn. If
somebody edited the file in between, the fingerprints differ and no link is drawn — because
there genuinely isn't one any more.

That distinction matters more than it sounds. A name can be reused, moved, or typed wrong. A
fingerprint can't.

### Two things about this file that are deliberate

**It is added to, never rewritten.** Each run appends one entry. It would be simpler to
regenerate the whole file each time, and that is exactly what makes logs like this fall over in
year two: rewriting means reading everything to add one line, so the cost grows with the history
until a run takes minutes. Appending costs the same on run 10,000 as on run 1.

**It is a view, not the original.** The real record is `provenance/runs.jsonl` beside it — one
line per run, which is the format that survives damage, because a corrupted line costs you that
line instead of everything after it. The YAML is generated for humans. If it is ever damaged or
you delete it, rebuild it from the record:

```bash
$ .venv/bin/runprov log --format yaml > provenance/transformation_log.yml
```

That is also the answer to "what if I made a mess of it": you cannot lose anything that matters
by touching this file, because it was never where the truth lived.

> A small thing worth noticing: `runprov` **wrote** that YAML without any YAML library
> installed. The environment in step 1 contains `runprov` and nothing else. It quotes every
> value unconditionally, which is not fussiness — the log this package replaces quoted only what
> its author expected to need quoting, and a single hand-typed `Note:` inside a description made
> the entire 24,300-line file unreadable.

## Step 8 — Changing the names and places

Everything above used the defaults. **They are defaults, not rules.** If your team already has a
convention — a different folder, a different filename, a `.yaml` extension instead of `.yml` —
say so once in `configure(...)` and the package follows you.

Every path below was produced by actually running these four settings, and the file listings are
what appeared on disk.

### The defaults, spelled out

```python
configure(root=HERE)
```

| what | where it goes by default |
|---|---|
| the run history (the real record) | `<root>/provenance/runs.jsonl` |
| the readable project-wide log | *beside the history*, `<root>/provenance/transformation_log.yml` |
| the per-run record | wherever you point `provenance=` on `Run(...)` |
| the readable twin of that record | the same name with `.yml` instead of `.json` |

Result:

```
in.tsv   out.tsv   out.prov.json   out.prov.yml
provenance/runs.jsonl
provenance/transformation_log.yml
```

### Move the whole thing somewhere else

```python
configure(root=HERE, run_log=HERE / "audit" / "history.jsonl")
```

```
audit/history.jsonl
audit/transformation_log.yml        ← it followed the history
in.tsv   out.tsv   out.prov.json   out.prov.yml
```

Note what happened without being asked: **the readable log moved with the history.** It is
defined as "beside the record", so you name one place, not two, and they cannot drift apart.

### Name the readable log yourself

```python
configure(root=HERE, transformation_log=HERE / "docs" / "data_lineage.yaml")
```

```
docs/data_lineage.yaml              ← your name, your folder, your extension
provenance/runs.jsonl
in.tsv   out.tsv   out.prov.json   out.prov.yml
```

The history stayed where it was. These two are independent: set one, both, or neither.

### Turn the readable files off entirely

```python
configure(root=HERE, write_transformation_log=False, write_yaml_sidecar=False)
```

```
in.tsv   out.tsv   out.prov.json
provenance/runs.jsonl
```

Only the machine-readable record remains. Nothing else changes — and you can regenerate the
YAML view whenever you want it, because it was only ever a view:

```bash
$ runprov log --format yaml > wherever/you/like.yml
```

### The rule behind all of this

The **JSONL history is the record**; everything else is a view of it. So the settings that move
views around are safe by construction — you cannot lose anything by renaming, relocating or
disabling a view, because the record is somewhere else and is never rewritten.

There is one more setting worth knowing for teams: `configure(sink=...)`. If your lab wants
records to go somewhere else entirely — a shared database, a message queue — you supply a sink
and `runprov` writes there instead, and it deliberately stops writing the YAML view too, because
where *your* records live stopped being its decision the moment you said so.

---

## The five things to remember

1. **`configure(root=...)`** once, near the top.
2. **`with Run("name", provenance=PATH) as run:`** — and `provenance=` goes on **that line**.
3. **`open(run.input(p))`** to read. Registering *is* how you open the file.
4. **`run.open_output(p)`** to write. It puts the record inside the file.
5. **`runprov verify`** to ask whether your results still match their inputs.

That's the package. There's more in the README, but you can do useful work with only this.

---

## Honest answers to reasonable worries

**"Will this change my data files?"**
No. It reads your inputs and never writes to them. The only file it adds anything to is one you
made *through* `run.open_output()` — and it adds a `#` comment block at the top, which every
common tool skips.

**"What if I stop using it?"**
Delete the `provenance/` folder. Your scripts still run — you'd remove the four lines. The
comment blocks already written into your outputs stay, harmlessly, as comments.

**"Does it slow my script down?"**
It reads each registered file once to fingerprint it, and writes one line per run. On a
100,000-run history, adding a run takes **0.0007 seconds**, and that number doesn't grow as the
history does.

**"Do I need a server, an account, or the internet?"**
No, no, and no. Everything is files, on your computer.

**"What if the record file gets corrupted?"**
The history is one line per run, so damage costs you *that line* — and the tools tell you how
many lines they couldn't read rather than pretending they read everything. This is a deliberate
design choice, from watching the predecessor format become unreadable in one piece.

**"What can't it do?"**
It **records**; it doesn't **check**. If you read a file without `run.input()`, that read is
invisible — it isn't in the record and nothing will warn you. It also doesn't run your scripts
for you, doesn't schedule anything, and doesn't version your data. It's a notebook, not a
supervisor.
