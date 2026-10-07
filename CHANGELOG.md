# Changelog

Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions follow
[semantic versioning](https://semver.org/), with the record-format promise in the README
taking precedence over the Python API while this is `0.x`.

Entries state what was **measured**, not what was improved. A fix with no number beside it
is a fix nobody checked.

## [Unreleased]

### Fixed — `open_output()` compares the marker it will WRITE, so `comment=None` and a `str`-Enum no longer destroy the artifact

**Audit O, product-1 and product-1b. Two defects in one expression, each of them an unparseable
artifact that `runprov verify` reported as OK, exit 0.** Both are the Newick class, arriving
through the writer-7 repair that closed the Newick class.

1. **`comment=None` routed the pin IN-BAND for every suffix OUTSIDE the allowlist.**
   `PIN_INLINE.get(suffix)` is `None` there, so `comment == known` compared `None` against `None`
   and said yes — inverting the safe default onto exactly the formats nobody had thought of.
   Measured: a `.nwk` holding `(A,B,C);` came out with a first line of
   `Noneprovenance — this artifact and what produced it`, **no sidecar**, a 3-taxon tree reading
   back with 4 taxa, and `runprov verify t.nwk` saying **OK, exit 0**. Reachable from
   `json.loads(cfg).get("comment")` and from an `argparse` default, both of which `mypy --strict`
   passes because both are `Any` — so the `comment: str` annotation was not what held this.
2. **A `str`-Enum marker compared equal and rendered as its own name.**
   `class Marker(str, Enum): HASH = "# "` is `==` to the declared marker on every interpreter, so
   it routed in-band — but `header()` interpolates `f"{comment}"`, and that is `'# '` on 3.10 and
   `'Marker.HASH'` on 3.11, 3.12, 3.13 and 3.14. **Measured on all five.** The `.yaml` became a
   `ScannerError`, with no sidecar and `verify` OK, exit 0 — and the SAME call wrote different
   bytes on different interpreters, which is the rule `newline=""` is non-negotiable for in this
   very method. No annotation and no checker refuses it, because a `str`-Enum **is** a `str`.

**The fix is to compare the RENDERING and to route the rendering onward**, so the value compared
is the value written. It is not a type blocklist: a legitimate `str` subclass equal to `"# "`
renders `"# "` and still pins in-band, measured. An `__eq__`-always-True object is closed by the
same line.

**What changes for a caller.** A caller passing a non-`str` marker — `None`, a `str`-Enum member,
an object whose `__eq__` says yes — now gets the **sidecar** and an untouched artifact, where
before it got a corrupt one. `open_output(p)` and `open_output(p, comment="# ")` are byte-identical
to before. One divergence is NOT closed and is stated rather than hidden: for a `str`-Enum marker,
3.10 still pins in-band (its rendering really is `"# "`) while 3.11+ writes a sidecar. Both
outcomes are correct and both artifacts parse; closing the divergence itself would mean refusing a
marker by type, which this method's own comments refuse for the reason a blocklist always fails.

**Held by a DERIVED guard rather than by the three suffixes that were reported.**
`test_a_suffix_outside_the_allowlist_takes_the_sidecar_whatever_the_marker_renders_as` walks
`(PIN_UNSAFE ∪ PIN_ALTERNATIVE) − PIN_INLINE − PIN_BINARY ∪ {".zzz"}` — **25 suffixes × 10
sentinels = 250 pairs** — because the obvious cheap widening, adding `None` to the existing loop's
marker tuple, is **green over the live defect and cannot fail**: every suffix that loop reaches is
IN the table. The derived set is also the only form that exercises `.svg`, `.html`, `.vcf`,
`.ipynb` and `.fa`, and it is the one that goes red. **Seven released histories unmoved: 84 corpus
rows over 7 trees and 12 recipes, base-against-base 0 of 84 before anything was believed, then
base against this fix 0 of 84.**

### Changed — `open_output()` demands the comment marker the format is known to take, and `comment` is keyword-only

**Audit N, writer-7. This changes what an existing call does, and the change is deliberate.**

Until now `PIN_INLINE` was a set of 17 suffixes, so any string at all was accepted as the pin's
comment marker for them, while `PIN_ALTERNATIVE`'s 7 suffixes demanded the exact marker. The
asymmetry had no reason behind it and it corrupted artifacts. **Measured on one `.yaml`: eight
different second arguments — `'w'`, `'DATA '`, `''`, `'note: '`, `'// '`, `'-- '`, `'%'` and
`';'` — each wrote an in-band pin that left the file unparseable (`ScannerError`), and
`runprov verify` reported all eight as OK, exit 0.** The pin was readable in every case; the
artifact was destroyed. That is the Newick class the allowlist inversion exists to prevent,
arriving through the path advertised as the safe default.

**What changes for a caller.** `PIN_INLINE` is now a mapping of suffix to the marker that suffix
is known to take (`"# "` for all 17 today). A `comment=` that is not that marker gets the
**sidecar**, exactly as an unrecognised suffix does, and the artifact is written untouched. Only
calls that were already producing a corrupt artifact behave differently; `open_output(p)` and
`open_output(p, comment="# ")` are byte-identical to before, and `verify` still reads every
record written by every released version identically (84 corpus rows, 7 trees, unchanged).

**And `comment` is keyword-only, because the signature invited the mistake.**
`open_output(path, comment="# ")` reads like `open(path, mode)`, so `open_output(p, "w")` is now
a `TypeError` at the call site rather than a corrupt file later. Verified by AST over every
tracked `.py` file that **no call anywhere in this repository passed a second positional
argument**; `record_header` follows `comment` and became keyword-only with it, and nothing passed
that positionally either. `header()` keeps its positional marker, because there the marker is the
subject of the call — it is the by-hand escape hatch `open_output`'s own docstring points at.

**Refusing the file modes by name was considered and refused**, and `open_output`'s own comment
had already refused it: *"every round of review found another one nobody had thought of … a guard
whose default is to corrupt is not a guard."* A mode blocklist catches `'w'` and misses the other
seven. **R-9 is not engaged**: no field is renamed, no digest convention moves, and the record's
shape is untouched — what changes is which of two already-documented destinations the pin goes to.

**The note now says which of the two reasons it was.** For a format with no comment line,
*"cannot hold an in-band pin"* is the whole story; for a `.yaml` asked for `'DATA '` it is false,
so the note names the marker and adds *".yaml does take one behind `'# '`."*


### Added — `runprov gate`: a policy, checked against the history that was actually recorded

T-34, ADR-0018. `check` is the static half — *could this code fail to record*. This is the other
half: *did the runs that actually happened meet the rules this project set for itself?* An
accredited laboratory has to show documented controls and evidence they were met; the evidence was
always here and there was no way to state the control, so the control lived in a lab manual, in
prose, checked by a person remembering to look.

```bash
runprov gate --policy policy.json --log runs.jsonl
```

**The rule set is derived from a registry rules register into**, never a hand-typed list in the
parser, the docs and the tests. There are **three checked statements** of it — the registry, the
ADR's table and the README's table — and a test holds each of the other two to the registry.
`--help` builds its rule list from the registry, so that copy cannot disagree at all; the
README's table is checked cell by cell, including that each row's *cannot answer when* is the
first clause of the sentence the rule itself prints, compared by EQUALITY rather than by prefix,
because a prefix was satisfied by an empty cell.

**Seven rules:** `clean_tree`, `no_unregistered_reads`, `outputs_pin_inputs`, `commit_recorded`,
`environment_captured`, `finished_ok` and `inputs_verify` — **prose, and the registry overrides
it.** Nothing reads this list: it is release notes, the fourth statement of the rule set and the
only unchecked one, which is why the entry above says three and not four. `runprov gate --help`
is the derived list a policy's author should read. Every one names what it cannot see, and
two exist mostly to say so: `git_tree_dirty` is `False` both for a clean tree and for a run that
never looked, and `unregistered_reads` is empty both for a run that missed nothing and for one
whose watch hit its cap. A rule reading only the first field of either pair would report an
unexamined run as a pass.

**Three exit codes, and the third is the design.** 0 every rule checked and met, 1 a rule
violated, **2 a rule that could not be checked.** A violation outranks an inability — knowing part
of a list is knowing a breach — and **one unreadable line in the history turns a met gate into
exit 2**, because that line is a run the gate did not examine and a gate that greens on *not
examined* is the shape this package exists to catch. Measured: the same policy over the same
history exits 0, and exits 2 after one appended line that is not JSON.

**The policy file is JSON or TOML, chosen by the suffix, and every rule carries a required
`why`.** JSON parses with nothing installed on every supported version; `.toml` needs `tomllib`
(3.11+) or `pip install runprov[toml]` below it, and `dependencies = []` is unchanged. The `why` is
a field and not a comment because **a comment cannot be checked and does not travel** — it is
printed beside the verdict on the page and in the payload, so an assessor reads why a control
exists next to whether it was met. The file is read strictly: a key this version does not
understand is refused rather than ignored, because a rule under a mistyped name is a control its
author believes is in force and the gate never applies.

**`inputs_verify` is the one rule that leaves the record**, and R-9 was clarified before it was
written: *the source* means source code, not the filesystem. It re-hashes what each run declared,
on the record's own digest key, comparing all 256 bits rather than the 16 characters a pin
carries. A declared input that is gone is `CANNOT_CHECK` naming the path and never a violation —
the file may have been archived, and an accusation cannot be withdrawn from a record.

**`no_unregistered_reads` now names the setting that switches its watch off.**
`warn_unregistered_reads=False` — documented, and recommended for a step that deliberately reads
files it does not want recorded — skips the watch altogether, so such a run records a full
`observation` block and neither field and the rule answers MET over real unregistered reads. The
setting is PROJECT-level, so one step's exemption silences the watch for every run that shares the
project. **The verdict is deliberately unchanged and the limit is written down instead**, in the
sentence the gate prints under `cannot see` and in ADR-0018, whose paragraph presented the watch's
own `except` arm as the only route to that state. The field that would let the rule answer
properly cannot be back-filled: requiring it would turn every clean pre-0.8.0 history from MET to
CANNOT_CHECK for ever, which is worse than the defect, so it gets its own ADR.

**`environment_captured` reads the snapshot the record NAMES, and opens no file.** A run whose
environment snapshot could not be written records `environment_snapshot: {"error": …}` and is
still marked `packages_recorded: "snapshot"` — the mark comes from the key's presence — so a run
that printed *WARNING: could not write environment snapshot* and recorded no package at all
answered MET over `packages: {}`, which this rule's own *cannot see* sentence calls
indistinguishable from nobody having asked. An unwritable snapshot directory in CI turned the
environment control into a no-op for every run after it. The rule now answers CANNOT_CHECK naming
the error, and CANNOT_CHECK when the record marks a snapshot and names no package count in it.
**No snapshot file is opened** — the record attests the count and the digest, and the test deletes
the file and takes the verdict again to prove it. Measured on all seven released histories: no
verdict moves, because every one of them names `n_packages: 2`.

**A breach and an inability are two lists and two words.** Each rule row used to carry one
`reasons` list holding both, and the page printed every member of it under the word `finding` — so
*the run recorded no outputs, so there is nothing to ask* was rendered as an accusation beside a
real breach. A person can tell the two apart by reading them; a consumer cannot, and
`len(reasons)` was 2 where `violated` was 1. `reasons` is now the breaches alone and `blocked`
carries what could not be answered, which is the word `runprov diff` already uses for an
incomparability in its own field. The page prints them under `finding` and `not checked`.

**And the gate's summary no longer loses a run to a breach of the same rule.** `cannot_check` was
`null` over a history where a run was not checked, whenever a violation of the SAME rule outranked
it: measured with one rule `outputs_pin_inputs`, one run with an output and no input, one with no
outputs — the payload carried `not_checked: 1` and `cannot_check: null` together. The sentence is
built from the rows' own counts now rather than from each rule's folded outcome, and **it names
runs**: *not every run was checked against 1 rule(s): outputs_pin_inputs (1 of 2 run(s))*, where
before it counted only rules and said nothing about how many runs each left unanswered. No exit
code moves — measured zero change on all seven released histories.

**A run that started and never ended is named, and never changes the verdict.** `gate` used to
drop every `runprov.start.v1` line before any rule saw it, so a history holding a SIGKILLed run
answered `MET`, exit 0 and `cannot_check: null` — *looked and found nothing missing* — about a run
it never examined, while `runprov log` reported `unfinished: 1` over the same file. The payload
now carries `unfinished` and says so in `cannot_check`. **The exit code deliberately does not
move**: the start line is append-only and `runprov prune` clears markers and not history, so
failing the gate on it would fail a project for ever over one power cut — measured, including
after two further successful runs — and would fail a gate because another job is merely running.
The limit that remains is stated rather than hidden: a consumer keying only on the exit code still
greens over a lost run, and must read `unfinished` or `cannot_check` to see it.

**A history that is there and cannot be read is an answer, not a traceback.** `chmod 000` on a run
log used to raise out of the command: a raw traceback, **zero bytes on stdout and exit 1**, which
is this package's code for *a rule was checked and your controls were violated* — about a file
nothing ever opened a line of — while silence on stdout is its signal for *this command never had
a question*. It now carries the payload with the failure in its own `read_error` field, names it
in `cannot_check`, and exits 2. `found` stays `true`, because the history IS there: *not there*
and *there and unreadable* are different findings. On a project whose data lives on an external
drive this is not hypothetical — an EIO mid-read is an `OSError` on the same path, so a failing
disk reported a policy violation.

**A recorded path is resolved against the RUN'S OWN `cwd`, never the gate process's**, exactly as
`describe`'s own staleness check resolves it, and the per-invocation digest cache is keyed on the
resolved path. `run.input(p)` records the spelling it was handed, so a relative registration is
recorded relative — and this was measured, not reasoned: over a corpus history carrying
`data/m.tsv`, the same records answered `2 met, 1 not checked` from the tree they describe,
`0 met, 0 violated, 3 not checked` from one directory up, and **`0 met, 3 violated` — exit 1,
naming three files the gate never opened** — from an unrelated project holding the same names.
All seven released histories now answer identically from every working directory, and none of
their verdicts moved.

**A declared DIRECTORY is `CANNOT_CHECK` by design, and the sentence now says which tool
re-walks a tree.** `runprov verify` owns the tree comparison, its ordering key and the Windows
casefold precedence; a second implementation inside a policy rule is how the two come to
disagree. Measured: every one of the seven released histories reaches this through `data/refs`,
and before this the guard tested a `kind` the writer has never emitted, so all of them were told
*could not be read: Is a directory* instead.

`runprov gate --format json` carries the same answer, versioned `"schema": "runprov.gate.v1"`,
with each rule's `evaluated`, `met`, `violated` and `not_checked` counts beside its outcome. A
policy that cannot be read prints nothing at all, which is how a consumer tells a gate that could
not conclude from one that never had a question.

### Added — the gate payload carries the policy, and `--emit-policy` answers about the file alone

ADR-0018 R-13 and R-14, both from one ruling by Taylor on an open question, and the question's own
answer shaped the first. **The payload already contained every field of the normalised policy** —
each rule row carries `rule` and `why`, which is exactly what the parser returns — so the key adds
a shape rather than data: the control as a separable object, archivable and diffable without being
reassembled from the verdicts that carry it. It is **projected from those same rows and never
parsed a second time**, so the two views cannot disagree.

`runprov gate --policy <file> --emit-policy` validates a policy and prints its normalised form,
through the same reader the gate uses — so a policy that emits is a policy the gate accepts. It
**opens no history**, proved by exiting 0 with a `--log` that does not exist, and announces that
`--log` was not read rather than accepting a flag and ignoring it. Versioned
`"schema": "runprov.policy.v1"`, deliberately not the gate's, because it carries no verdict, no
count and no run. **Exit 0 usable, 2 not, and there is no 1:** a policy cannot carry a finding.

### Changed — the recommended interpreter is 3.12 or later

ADR-0018 R-12. The floor stays at **3.10** and nothing is withdrawn. `sys.monitoring` (PEP 669)
arrives in 3.12, so automatic step observation is unreachable below it and `steps` is **not
comparable** across that boundary — 3.11 buys `tomllib` in the standard library and nothing else.
Stated in both READMEs with its reason, because a bare version preference is the first thing a
reader discounts.

### Fixed — ADR-0017 said its own feature was not built, through the release that shipped it

T-33's two closing conditions, which 0.7.0 went out without.

**ADR-0017 was still `Proposed — T-33. Specification for a feature that is not built.`** The
feature reaches all ten answering commands and shipped on 2026-10-01. The ADR and the index row
now say `Accepted`.

**And a guard for exactly this already existed, which is the part worth reading.**
`test_every_adr_is_listed_in_the_adr_index` asserts that no ADR is still proposed while code
implements it — and it decides *implements* from a module's **top docstring**, deliberately, so a
passing mention of a future decision in a mid-file comment is not read as an implementation. T-33
cited ADR-0017 in function docstrings and comments across ten commands and in **no module
docstring**, so the guard never had `0017` in its set. It was not wrong; it was uninformed, and a
guard cannot tell that from satisfied. The **ten** modules that define a payload schema now name
it, and **a new test enforces the convention the old guard depends on** — deriving the set from
the parser, so a further answering command is covered the day its schema appears.

> **CORRECTED [L-15]: this said *nine*.** `policy.py` makes ten, and it was already in the tree —
> the same staleness as the sentence above it, in the entry whose whole subject is a count that
> went stale unwatched. **The number beside it was a `>= 9` FLOOR, which is G-11's shape: a floor
> is one below the truth the moment anything is added, and it cannot say so.** The floor is
> replaced by a derivation — every command the parser offers `--format json` must be covered by
> some module's schema constant — so the guard is red the moment a command answers in JSON with
> no schema constant, rather than two commands later.

**ADR-0017 also had no `test_every_answer_requirement_has_a_test`**, which ADR-0013 and ADR-0016
both have and which the backlog named as T-33's own closing condition. All 16 requirements were
cited on the day it was written — which is exactly the state that rots unwatched.

Copying the sibling guard would have been the obvious mistake and it is now asserted against:
`test_every_chain_requirement_has_a_test` matches `**R-n.**`, with the bold closing after the
number. That is ADR-0017's convention for R-1 … R-14 — but **R-15 and R-16, both added by later
audits, bold the whole requirement sentence**. A copied guard would have found 14 of 16 and
passed while ignoring the two newest requirements in the document: the scope pattern, in the
guard written to stop it. The pattern here matches both conventions, and a floor assertion fails
if it ever finds only 14.


### Fixed — six recorded paths were spelled the platform's way, and on Windows nothing joined

Audit L, L-06 and L-07. **This changes VALUES a Windows consumer may already have persisted, and
that is the whole reason it is here rather than silent.** No key is renamed and no schema moves:
R-9 governs the name a consumer looks up, and every name is untouched.

**What was wrong.** `_posix` is this package's one spelling for a recorded path — forward slashes
on every platform, because a record is read on a different machine from the one that wrote it.
Six fields across two modules still used `str()`:

| schema | field | module |
|---|---|---|
| `runprov.verify.v1` | `artifact` (three sites: no pin, gone, and the ordinary result) | `verify.py` |
| `runprov.verify.v1` | `root` | `verify.py` |
| `runprov.run.v1` | `cwd` | `run.py` |
| `runprov.run.v1` | `code.project_root` | `run.py` |

**`artifact` is a DECLARED name collision** — `runprov.verify.v1` and `runprov.report.v1` share it
on purpose, so that a consumer can join the two — and `report` has always spelled its half
`_posix`. Measured on `PureWindowsPath`: `verify` emitted `C:\Users\…`, `report` emitted
`C:/Users/…`, and the join matched **nothing**. The collision was declared, both halves were
checked for the NAME, and nothing asked whether they agreed on the VALUE.

**`cwd` became joined-on during this cycle**, which is why it moved now: `inputs_verify` anchors a
run's declared inputs to the `cwd` the run recorded, so a path a rule resolves against was spelled
one way by the machine that wrote it and another by the machine that checks it.
`code.project_root` is the other half of that pair.

**ON WINDOWS THIS IS A BEHAVIOUR CHANGE, NOT ONLY A REPAIR.** A Windows consumer that stored the
old `artifact`, `root`, `cwd` or `code.project_root` strings and compares them literally will stop
matching: `C:\data\out.tsv` is now `C:/data/out.tsv`. Records written earlier are not rewritten,
so a history spanning this change carries both spellings — compare with `pathlib.PurePath` on both
sides, or re-spell the stored value with `as_posix()`. On POSIX nothing changes at all, which is
exactly why the suite could not see this: `str(PosixPath(...))` and `_posix(...)` are the same
string, so no content assertion on Linux distinguishes the repair from its absence. The guard is
structural instead — `test_no_recorded_path_is_spelled_with_a_bare_str` now derives its scope
rather than matching names that end in `path`, and the six sites are inside it.

**And five assertions in the suite moved in the same commit**, comparing these fields against the
platform's native spelling. Leaving them would have reproduced `df4964b` exactly: seven jobs
green, Windows red, and a local gate that structurally cannot see the class.


### Fixed — a SEVENTH recorded path was spelled the platform's way, and the guard could not see it

Audit M, `guard-1` and `guard-2`. **Three parts, and every two-of-three combination was measured
RED, so they land in one commit.** No key is renamed, no digest convention moves, and R-9 is not
engaged — this is L-06's disposition applied to the field next door.

**`code.script_file` (`runprov.run.v1`, `run.py`) used `str()`**, twenty-two lines below the
`code.project_root` L-07 repaired in the same `"code"` dict and in no list this package keeps. On
Windows one record carried `C:/Users/lab` for `project_root` beside `C:\Users\lab\scripts\align.py`
for `script_file`. The ground for the repair is `_posix`'s own docstring — *the one spelling every
recorded path uses, forward slashes on every platform* — and the permanence of a history line, **not
a consumer:** derived by AST, **no comparison anywhere in the package reads this field**, and the
only one in the suite is a membership test for v1 records. So by L-27's rule the spelling was free
to change, which is what made it safe rather than what made it necessary.

**On Windows this moves a value where the field is absolute**, which is the `_caller_file` route; a
caller that passes `script_path=` records what it was given, unresolved, and that is unchanged.
Records written earlier are not rewritten. **Measured on the seven released histories: nothing
moves** — all seven record `script_file` relative (`corpus_scenario.py`), and twelve commands over
each of the seven trees are byte-identical before and after.

**And the guard was blind to the SHAPE as well as to the name.**
`test_no_recorded_path_is_spelled_with_a_bare_str` tested the TOP node of a keyed value: measured,
`verify.py`'s `_posix(path)` reverted to `str(path)` is RED naming the site, and the identical
revert written `str(path) if path else ""` left the **whole suite** at exit 0 — which is exactly the
shape `script_file` had. The scan now unwraps the arms of a conditional expression and the operands
of a boolean value before it looks for the call, with **one probe line per arm** so the widening
cannot become a no-op. A deeper walk of the value was measured and rejected: it reports the
legitimate `_posix(str(...))` inside `hashing.py`'s `onerror` as an offender and names
`script_file` under the wrong key, which is a report that misnames its own finding.

**The offender arm cannot be the demonstration for a single-site field, and the guard now says so.**
Its scope is the declared table united with the names the package actually spells `_posix`, and the
declared table is held equal to the derived one — so reverting the one site such a field has takes
the name off the derived side in the same stroke, and the EQUALITY assert fails first. Verified on
`project_root`: the revert is red accusing the table, and it never names the line. On POSIX
`str(PosixPath(...))` and `_posix(...)` are the same string, so no content assertion on this
platform distinguishes any of this from its absence; the hosted matrix is the instrument, exactly as
for L-06 and L-07.

**Six stale `file.py:line` citations are replaced by names** — four pointing at the `cwd` string
equality, which the commit that wrote *line 2165* is the commit that moved to 2178, and two in
`diff.py` pointing thirteen and twenty-six lines wide of the history projection's own fields. They
now name `Run._anchor` and `Run._append_history`. C-03 already decided this class: a line number in
a comment can be held by nothing, a function name can.


### Fixed — `symlink_target` was the fourth undocumented recorded path, inside the funnel itself

Audit M, M-01. `hashing.describe` records `os.readlink`'s answer, and `os.readlink` answers in the
platform's spelling: on Windows one record carried `out/link.tsv` for `path` and
`..\real\target.tsv` for `symlink_target`, **six lines apart, in the function whose docstring is
the invariant** — *`_posix` … applied here, at `describe`, because every input and every output goes
through it; one funnel, one rule, and no list of call sites to keep extending.* It is now
`_posix(link_target)`. No key is renamed and R-9 is not engaged. The one reader in the tree takes
`pathlib.Path(...).name` of it, which both spellings answer the same way.

**No released history can move:** none of the seven corpus trees registers a symlink, so no record
any released wheel wrote carries this field at all — which is also why the cross-version harness
could not have caught it.

**Both existing guards missed it, for two different reasons, and that is the part worth reading.**
The scope guard missed it because the name was in no list and the recorded value contains **no
`Call` node at all** to test. `test_inside_describe_every_str_is_wrapped_in_posix` missed it because
there is no `str(...)` here to find — its own docstring asserts that `describe` contains exactly
**one** `str(` call, which is true, and is exactly why asking *is there a bare `str`* harder could
never have found this.

**So the new guard asks about the VALUE instead, and derives what a path is from the operating
system.** `_posix` is replaced by a marking version, `describe` is run over a real file, a real
symlink and a real directory, and **every recorded string that names something on disk must carry
the mark.** There is no list of path-producing functions in it, so the next value that arrives from
a call nobody here has heard of is caught on the day it is recorded rather than in the audit after
it. It is also the only guard in this family that can fail on Linux: a mark is not a separator.
Measured — dropping `_posix` from `symlink_target` is red naming the field, while the two existing
guards stay green on that mutation.


### Fixed — `show` called a status-less record `ok` while the gate could not check it

Audit M, the second row the field-comparison table surfaced. Five readers spelled
`record.get("status", "ok")` — `show <target>`'s view, the artifact block, `render_yaml`'s entry,
`show`'s per-script counters and `log`'s text page — while `policy._finished_ok` spelled
`record.get("status")` and answered CANNOT_CHECK. **One history gave a reader two answers,
chosen by which command they typed.**

**The gate's default is the right one, and it is the one that stayed.** ADR-0018 R-3: an
unevaluable rule is never a pass. `_finished_ok`'s own docstring states the other half — *A
MISSING STATUS IS NOT A FAILURE* — and `diff._status` reaches the same answer in its own words,
*a missing `status` BLOCKS rather than compares*. So absence is neither verdict, and
`show.status_of` is where that third state is now named. `show.py` already stated the principle
four lines above the line that broke it, about the field next door: *a run that PREDATES the field
does not know the answer, and rendering "clean" for it would be the reassuring lie the flag exists
to stop.*

**Two of the five sites had nowhere to put a third state, and routing them naively would have
turned the lie into an accusation** — which is the part worth reading, because it is what the row
as filed would have produced:

* `log`'s page marks `!!` for anything that is not `ok` and then prints `FAILED <type>:
  <message>` thirty lines below. A status-less record would have been accused of failing with an
  empty message. It now prints `status  NOT RECORDED` instead, and only a RECORDED non-ok status
  reaches the failure clause.
* `show --format json`'s per-script tally has two buckets. An unknown status now increments
  **neither**, and `runs` beside them is what says so — `runs: 1, ok: 0, failed: 0`. **This adds no
  key to `runprov.show.v1`,** and it makes the tally agree with `log --format json`'s own `failed`,
  which has always counted explicit failures only.

**Behaviour change, stated:** for a record carrying no `status`, `show <target>` and `show
--format json` now report `unknown` rather than `ok`; the per-script `ok` count no longer includes
it; and `log`'s page marks it `!!` with a `NOT RECORDED` line. A record this package wrote is
unaffected — the only status-less line it writes is `runprov.start.v1`, which every reader drops
by name before any of this. No released history moves: twelve commands over each of the seven
corpus trees are byte-identical.

**And `export.py`'s RO-Crate `actionStatus` is the opposite invention and is NOT changed here.** It
is `CompletedActionStatus if rec.get("status") == "ok" else FailedActionStatus`, so a status-less
record is exported as an accusation — in a published interchange format, two paragraphs above the
same function's honest `runprov:status: null`. It is named in the guard's docstring so a reader does
not take the green as covering it; changing a field a third party reads is a decision of its own.

**The guard derives the permitted default from the writer rather than listing sites**: the module
that ASSIGNS `status` is found by AST, its verdict vocabulary with it, and no other module may
supply one of those words as a `.get` default. `run.py`'s history projection keeps its default and
is the one exemption, with the reason at the line — a KeyError raised in the writer loses the
record to protect it. Five mutations, each red and each naming its site, including the gate itself
defaulting to a pass.


### Fixed — five more renderers forged lines on the page a person reads

Audit M, escape-1/2/3 and two the seven findings did not name. K-21 escaped two renderers and
L-03 escaped five more at their emission points; **the hand-written list of renderers has now
been wrong four times**, and this is the fourth. The five that were still forgeable:

* **`verify.render_report`** — the artifact's **own filename, read off the disk**. A newline in
  it prints a standalone `OK           out/clean.tsv` verdict line inside a page whose real
  verdict is `STALE` and whose exit code is 1. Reproduced. **This is the page the shipped
  action prints:** `action.yml` runs `python -m runprov verify $root` inside
  `echo "::group::runprov verify"`, and `.pre-commit-hooks.yaml` runs it too. The other three
  channels the row named are not live — `inputs[].name` is already escaped at write time by
  `run._safe_for_pin`, and `via` and `scripts` go through `{!r}`.
* **`check.render`** — a `.py` file whose NAME carries a newline forges this page's own
  all-clear sentence, *no entry point opens files without recording them*, as a standalone line
  while the page is reporting a finding and exiting 1. The forged name must **end** in `.py` or
  the walk never sees it.
* **`impact.render`** — through **`step.script`**, not `outputs[].path`. `script` is a
  caller-supplied string needing no file to exist, so of the three this is the only one that
  reaches the page on **every** platform; the other two need a filename POSIX allows and NTFS
  does not. A forged line in a rebuild order is an instruction to rebuild something that was
  never recorded.
* **`chain.render`** — in **none** of the findings, and the most reachable route in the family:
  its first line is `# chain — {path}`, interpolating **one `sys.argv` argument**, so it forges a
  bare line on **stdout** with no record, no history and no file, on every platform. Escaped at
  each of its three returns, because there is no single join to escape at.
* **`__main__._render_lineage`** — every edge line interpolates a script name.

**The payload side is untouched, measured rather than asserted.** Seven released corpus trees
× fifteen recipes — every `--format json`, `yaml`, the export and the twelve text pages — are
**byte-identical**, 105 of 105 rows unmoved, after the instrument itself was fixed: the `chain`
rows are digests over a history that records the tree's own absolute path, so a per-run temporary
directory made them disagree on the **baseline**.

**Escaping is not censoring:** in all five the forged text still reaches the reader as
`\nGATE: MET (exit 0)\n` on the line it belongs to. Only its power to start a new line is gone.


### Fixed — stderr is escaped too, in three different shapes, because one would not do

Audit M, escape-4. L-23 escaped `gate`'s four stderr diagnostics and `_diag`'s docstring
declared that narrow scope deliberately — *a package-wide stderr transform is a different
decision*. This is that decision, taken in the three shapes the sites actually need. **The
package-wide `printable(message)` it was first filed as does not work, three ways:**

1. **It does not import.** `_report` is the one stderr emission point, and `terminal` imports
   `_report`, so `_report` importing `terminal` raised `ImportError: cannot import name
   'diagnostic' from partially initialized module 'runprov._report'` and the package did not
   come up at all. So `printable` and `printable_lines` **moved to `_report`**, down the import
   graph to the module that imports nothing from the package. Nine modules import them from
   there now; `terminal.py` keeps the comment saying why they left.
2. **It collapses every multi-line diagnostic.** A four-line `AUTO-DETECTED project:` note came
   out as one line of `\n` literals, and at least eight `diagnostic()` callers pass embedded
   newlines on purpose — which is exactly what `printable_lines`'s own docstring names as *the
   one way to get this wrong*.
3. **The whole suite was GREEN with that regression installed**, so the suite was no evidence of
   safety here.

**The three shapes:**

* **`_report._write`, per line.** Every `diagnostic()` call in `run.py` (27) and `sinks.py` (3)
  and every `summary()` line arrive here; each is split on its own separator and each line
  escaped. `\r`, `\x1b[2K` and stray control bytes are gone from the whole writer-side stderr
  surface. **A NEWLINE IS NOT CLOSED HERE AND CANNOT BE**, and the docstring says so: by this
  point the message is one string, and a forged separator is indistinguishable from a
  deliberate one. Multi-line notes still arrive as multiple lines — measured.
* **`run.py`'s `UNREGISTERED READ` warning, per field.** It names paths the run actually opened,
  and a newline is legal in one on POSIX. Reproduced: an unregistered read of
  `data/sneak.csv\nGATE: MET (exit 0)\nx` put a bare `GATE: MET (exit 0)` on stderr **from the
  writer, with no CLI involved**. Escaping `shown` at the site is the only place that half can
  be closed.
* **`__main__`'s in-flight banner, per line of the list it already joins.** Its `script` comes
  from a `.incomplete/*.json` marker **or from a `runprov.start.v1` line in the history with no
  marker file at all** — reproduced, platform-neutral, and invisible to both escaping oracles:
  the property guard's `_acted_on` whitelists `\n` by construction, and no corpus recipe
  produces an unfinished run.

**The other 51 `print(..., file=sys.stderr)` sites in `__main__` are NOT routed through one
transform.** Several are deliberately multi-line, and a single transform over them is the
regression above in a second place. 105 of 105 released-corpus rows are byte-identical.


### Fixed — the renderer set is DERIVED now, and the derivation found a fifth missed renderer

Audit M, escape-5, escalated to high. L-03 wrote a property test to retire the hand-written
list of renderers and then **attacked it through a declared list of record FIELDS**, so the list
moved out of the production code and into the probe. Six renderers stayed forgeable behind a
green suite, and **two of the test's own legs were refusal pages** — `verify` 255 bytes and
`check` 194 bytes, with `carries=False` — so reverting either renderer's escaping was green.

**The derivation.** `test_every_page_main_prints_reaches_an_escaping_chokepoint` reads
`__main__`'s own syntax: every expression reaching `print`, a `sys.stdout`/`sys.stderr` write or
a `for` loop that prints its target, resolved through `"\n".join(...)`, `+ "\n"`, a
comprehension and one local assignment. The payload side excludes itself — an expression whose
subtree calls `.dumps` is not a page — and the only names typed are the six **serialisers**
(RO-Crate/PROV, the Kubernetes manifest, the sbatch header, the YAML document and its banner),
each **held by equality against the derivation**, so an exemption that stops naming a printed
function is red too. Twenty-one renderers, fifteen of them text.

**And it immediately found the fifth miss: `prune.render`**, in none of Audit M's seven
findings, in no hand list, and as reachable as `chain` —
`prune --log 'prov\nGATE: MET (exit 0)\nx/history.jsonl' --dry-run` forges a bare line on
stderr with no marker, no history and no file, on every platform. It is also the renderer no
behavioural leg may ever reach, because `prune` DELETES; the structural half is the only thing
that can hold it.

**A newline-aware oracle, which is the one thing `_acted_on` cannot be.** `_acted_on`'s test is
`not (c.isprintable() or c in " \n")` — **`\n` is whitelisted by construction**, because it is
the page's own separator — so every newline-only forgery in this family reported `acted_on: []`.
The second oracle counts lines at the chokepoint: a line handed to `printable_lines` that
contains a newline is a line the page emits and the renderer did not count. It records them
**per renderer**, and three floors hold the legs' own liveness: twelve carriers, no carrier
without a renderer, and five named renderers that must each be reached.

**Three channels the fixture had none of**, two needing no file and so running everywhere: a
forged `script` on a run `impact` and `lineage` both print; an orphan `runprov.start.v1` line
with no record and no marker, which is the in-flight banner's only channel; and an artifact
FILENAME plus a `.py` filename carrying the forgery, behind a **probe** (not `sys.platform`) with
the reason stated — on a host whose names cannot hold a control character the defect cannot
exist.

**Nine mutations, each red and each naming its own site**, including the two vacuity
regressions: write the forged artifact without a pin and the guard names `verify <tree>` as a leg
that asserts nothing; take the orphan start line out and it names
`runprov.__main__.report`. And **the docstring's `THE PROPERTY NEEDS NO EXEMPTIONS` is
withdrawn**: it has three, and they are now written down.

**Two of the row's claims did not reproduce, and both are recorded rather than quietly dropped.**
The *under-detecting oracle* — `_FORGED_LINE not in splitlines()` never fires for a mid-line
forgery — is wrong: `str.splitlines` splits on `\r` too, so the clause goes red for all five
reverted renderers. The stronger position-independent form was added in front of it anyway. And
the artifact-FILENAME leg needs the **newline alone**: with the full four-line forgery in the
name, the sidecar's `provenance for:` line pushes `PIN_ANCHOR` past `PIN_STARTS_WITHIN` and
`verify` reports NO PIN, which would have made the leg vacuous in a new way.


### Fixed — `--format yaml` wrote surrogate escapes, which are legal JSON and illegal YAML

Audit M, escape-6, escalated to high: **the only one of the escaping family that was a wrong
record ON DISK.** `show._q` called `json.dumps` at the default `ensure_ascii=True` while its
sibling `_structure` twenty lines below passed `ensure_ascii=False` — **the only `ensure_ascii`
in the whole tree** — so one module rendered the same data two ways at the character level,
against ADR-0017 R-1.

**Above the BMP, `ensure_ascii=True` emits a surrogate pair, and a surrogate escape is legal
JSON and illegal YAML.** Measured end to end: `yaml.CSafeLoader` raises `ScannerError: found
invalid Unicode character escape code`, and `yaml.SafeLoader` returns a string that **does not
equal the record**. So `log --format yaml`, `show --format yaml` and the on-disk
`prov/*.prov.yml` and `prov/transformation_log.yml` carried a path that is not the path, and
libyaml refused the file outright. **No reprint fixes that once the original filename is gone**
— it is the precise failure the README blames the predecessor's log for. `ensure_ascii=False`
now on every arm of the YAML path: `_q`, `render_yaml`'s keys and its deep-nesting fallback, and
all three arms of `_scalar`.

**And it was held by nothing, in two independent ways.** `_q`'s docstring named
`test_rendered_yaml_parses_with_nasty_values` as holding the line. That test calls
`yaml.safe_load` — the **pure-Python** loader, which never raises on a surrogate escape and
silently hands back lone surrogates — and `NASTY`'s highest character was `é`, which is **inside
the BMP** and escapes to `\u00e9`, a form YAML accepts. A fixture that could not reach the case
and a loader that could not refuse it. `NASTY` now carries U+1F9AC, folded into an existing row
rather than added as a fourth because two sibling tests read its length, and the new guard
asserts the loaded value **equals** the record's string through `yaml.CSafeLoader`, skipped with
its reason where libyaml is absent. *It parsed* is the assertion that was green over this for a
whole release.

Three mutations, each red: `_q` back to the default, `_scalar`'s string arm back to the default,
and `NASTY` back to `é` — the last one failing with the reason rather than a `StopIteration`.
**No released history moves:** seven corpus trees × fifteen recipes, 105 of 105 rows
byte-identical. The README's pinned `hcv_genotyping/transformation_log.yml` is the
**predecessor's** hand-written log and nothing in `show.py` can move it.


### Fixed — WHY.md's deleted statement split is guarded by its own deletion notice

Audit M, guard-3. L-08 deleted three hand-typed size figures from WHY.md and left a
**literal-string tripwire on one of them**, so the split returns freely with fresh numbers and
the suite stays green. **The filed remedy — L-26's shape, a regex — is refuted three ways, each
measured:**

* `about ([\d,]+) (record|statement)` matches **README.md's `about 5,500 statements`**, which is
  the one gated figure in this tree and the one `ci.py` asserts against coverage's own totals. It
  also matches plausible future prose: *a history of about 3,000 records*.
* it misses a reworded return — *roughly 3,400 lines that record* is the same claim and the same
  drift, and no pattern tight enough to avoid the false positive above catches it.
* **L-26's instrument transplanted literally is RED AT HEAD.** `[\d,]+ (?:statements|branches)`
  finds `**514 statements**`, which WHY.md keeps on purpose as a dated fact about 2026-08-18 and
  explains in the same paragraph. A regex cannot tell a live claim from a dated historical one,
  and WHY.md legitimately holds one of each.

**So the guard is the positive form.** WHY.md documents its own deletion — *THE STATEMENT SPLIT
THAT STOOD HERE IS DELETED [L-08]* — and that paragraph was guarded by nothing. Its presence can
pass, cannot false-positive on prose, and fails exactly when somebody rewrites the bullet, which
is the only route the split can return by. **It is a tripwire and not an instrument**, and the
test says so: it measures no figure. Measured red by rewriting the bullet with fresh numbers —
the mutation the old literal tripwire stays green for.

### Changed — `printable`'s docstring now says the non-breaking spaces are escaped, and why

Audit M, escape-7, **REFUSED as a code change.** `printable` escapes U+00A0 and U+202F, which
French typography puts before `:` `;` `!` `?` and which Word and LibreOffice insert
automatically, so a French laboratory's policy `why` renders `contrôle\xa0: chaque run`. Every
measurement in the row reproduces and **none of them is a defect**: the contract is *a space
survives and nothing else non-printable does*, and the accented letters, CJK and em dash the
docstring promises all pass.

**The remedy — widen the survivor set to `unicodedata.category(c) == "Zs"` — has a coupling it
did not mention.** The escaping guard's oracle is `c.isprintable() or c in " \n"`, so letting
the Zs category survive in production would make the first `why` a French laboratory writes with
a non-breaking space turn that guard **RED over a legitimate character**. Today the suite is
green only because no corpus page contains one. Widening the set means widening the oracle in the
same commit, and neither is worth a legible space. The docstring now records the behaviour, the
reason, and the coupling; the code is unchanged.


## [0.7.0] — 2026-10-01

### Added — `runprov report --format json`, and `report` gained a structure to serialise

ADR-0017, the first row of T-33. `report` was the only one of the seven answering commands
with nothing to serialise: `Page` was `lines: list[str]` and a status, so its facts became
prose inside the builder. It is now a builder returning a `Report` and a renderer turning that
into the page, with the payload and the page both derived from it — one builder, two
renderings, which is the whole point: this project has shipped two renderings of one answer
disagreeing twice in one audit, once as a cause the text asserted and the JSON did not carry,
once as two findings the text dropped while the JSON reported them.

The payload carries what the page could not check, not only what it found: `bytes_differ`,
`observation.unregistered_watch_truncated`, the full `unregistered_reads` list, and
`limits.run_not_found`. A null is *looked and found none* and an absent key is *this did not
look* — with no run record there is no method, input or observation section on the page, and
none in the payload. Fields keep the record's own names (`started_utc`, not the page's
`started` label), and the `?` a reader sees for a fact the record does not carry is supplied by
the renderer rather than stored, so a consumer can never mistake an absence for a script named
`?`. The exit code is unchanged by the format.

Measured: the text is byte-identical over 26 fixtures covering every branch of the page, with
one asserted exception — a record carrying an explicit JSON `null` for a scalar field printed
the word `None` and now prints `?`. A test walks the payload's own leaves and asserts each one
moves the page when it changes, so a field can no longer reach one rendering and not the other.

### Added — `runprov diff --format json`, the option ADR-0014 specified and never got

ADR-0017, the second row of T-33. ADR-0014's own sketch promised `[--format text|json]` for this
command; neither that nor `--only` was built, and a reviewer later found the `DIMENSIONS`
constant sitting in `diff.py` with no reader — and **wrong**, because `status` had been added to
the comparison and never added to it.

`compare()` already returned the whole answer, so this row is a wrapper rather than the refactor
`report` needed: `Comparison(a, b, dimensions)` adds the four facts the table's header states, in
the record's own names, and the exit code is now read off it instead of folded beside the
renderer. `DIMENSIONS` holds the reading order, which it had only claimed to; a dimension it does
not name raises rather than being silently dropped.

The payload carries what the comparison could not support, not only what moved: `blocked` is why
a dimension cannot support the word *unchanged*, `examined` is the scope it was looked at over,
and `verdict` and `settled` are on every dimension. **A payload with only `differences` would
hand a consumer an empty list for a dimension that could not be compared at all**, which reads as
*nothing changed* — the defect ADR-0014 exists to prevent, delivered to the readers least able to
notice it. Nothing in this payload is ever absent: unlike `report`, `diff` answers every dimension
for every pair of records, so `blocked: null` has exactly one meaning. The `?` a reader sees for a
fact the record does not carry is supplied by the renderer, so a consumer cannot mistake it for a
script named `?`. The exit code is unchanged by the format, over a settled comparison, a changed
one and a blocked one.

Measured: the table is byte-identical across the refactor over 20 record pairs covering every
branch of the renderer, 520 of 521 lines, with one asserted exception — a record carrying an
explicit JSON `null` for `script`, `run_id` or `started_utc` printed the word `None` in the header
and now prints `?`. A guard walks the structure's own leaves in both directions and asserts each
one moves the table when it changes; the one place a rendering says less than the structure holds
is `examined` on a row that does not claim `unchanged`, and that asymmetry is asserted as a set
rather than left to be found.

**Sixteen mutations derived from this row's production diff, one per hunk, and one survived.**
M04 swapped `record.get("started_utc")` for `record.get("finished_utc")` in `_side` and the whole
suite stayed green. It is not an equivalent mutant: the mutated header prints
`A  s.py  r1  2026-01-01T23:59:59Z` where the true one prints `00:00:00Z`, and the payload emits
that finish time **under the key `started_utc`**. It survived because **not one `diff` fixture in
the suite carried `finished_utc`** — the mutated read returned `None` for every record the tests
build, so the header printed `?` either way, and the only assertion on `Side` was the all-`None`
case where the two keys cannot differ by construction. Against a record this package actually
writes — `report`, `show` and `export` all read `finished_utc` — every dimension would compare
correctly, every verdict would be right, and the line above them would be false. **That is the
defect class Audit H exists to catch, and no assertion about a verdict can see it.** Closed by a
test that fixes the fixture shape as much as the read: a record carrying both stamps, different,
with the start reaching the header and the payload and the finish reaching neither.

### Added — `runprov impact --format json`, and a blind spot of zero says so

ADR-0017, the third row of T-33. R-12 already named this command's builder, and it was accurate:
`Chain(digest, seeds, steps, unregistered, runs_examined, watch_drops)` with
`Step(depth, address, script, outputs)` was there to be serialised, so this row is a wrapper
rather than the refactor `report` needed. `payload()` derives from `Chain._asdict()`, so a field
added to the structure reaches it without anyone remembering; what wants guarding is somebody
reinstating a hand-written list.

**One defect found while scoping it, and it was already shipped.** The truncation predicate was
written twice — `not chain.steps and chain.seeds` in `render` to choose the sentence, and
`chain.seeds and not chain.steps` in `__main__` to choose the exit code: the same question, the
operands reversed, in two files. A-09 and C-07 were that one defect found twice and fixed twice
where each was found, which is how the second copy came to exist. They agreed, nothing held them
together, and this row's payload would have been the third. It is now `Chain.truncated`, derived
once and read by all three.

**The page and the payload differ in four places, every one an abbreviation in the safe
direction** — the machine reader is told more than the person, never less. `seeds` reaches the
page as a count, because a person rebuilding needs how many runs read the bytes and a consumer
needs which ones. `steps[].address` is on no page in any state, because depth and script are what
a person rebuilds by while the address is what `runprov show` takes. `runs_examined` is quoted
only inside a blind-spot sentence, so with both counters zero the scope leaves with the lines that
cited it, while the payload keeps it — nothing missed over nothing examined is not a clean bill.
And the page prints `digest[:16]`. **Naming those four as a set is what makes a fifth fail a test
instead of reaching a consumer.**

Measured: 15 mutations derived from this row's production diff, one per hunk, with the no-op
control surviving. The derived guard was controlled in both directions — dropping `watch_drops`
from the payload fails the payload-completeness half naming `watch_drops`, and dropping `depth`
from the page's step line fails the page-completeness half naming `depth`. Exit codes are
identical across formats over all three states, not only the one that answers.

**The digest asymmetry needed its own test, and that is the finding worth recording.** The guard
marks a field as stated when some change to it moves the page, and every substitute the
perturbation draws differs from the first character — so `digest` was reported as stated,
correctly, while 48 of its 64 characters reach no page at all. **A perturbation that changes the
front of a string cannot detect a rendering that shows only the front.** That is the scope pattern
one level in: not the guard's logic, but the range its substitutions cover, failing to reach what
it reports on.

### Changed — ADR-0017 states what silence means for a path that is not there

The open question Audit J closed with, ruled by Taylor on 2026-10-01 against the measurement
rather than a reading of the rule. R-15 says silence on stdout means the invocation was wrong,
and could not settle one case on its own: a command told to read a path that does not exist —
has it answered, or was it mis-invoked?

Measured across every command that takes a path, the package was doing three things, and **two
were already ruled**:

| the absent path is | behaviour | commands | ruled by |
|---|---|---|---|
| the **history** | answer | all nine that read one | R-15 / I-21, 2026-09-29 |
| the **single subject** (`nargs=1` or `?`) | silence | `report`, `impact`, `check`, `export` | the `silent_by_design` bucket |
| a member of a **list** (`nargs="*"`) | answer, naming the absent | `verify` | J-24, 2026-10-01 |

**Only `check` versus `verify` was open** — both are *the tree I was told to scan is not there*,
and they behaved oppositely. The ruling is the arity, not taste: `verify <paths>` takes a list,
so `verify good.tsv typo.tsv` is partly answerable and J-24 requires a gate to see the absent
member; a command that must answer in the mixed case cannot sensibly fall silent when the list
happens to hold one absent path. `check <root>` takes one root and has no mixed case — with
nothing to walk it never started, which is the distinction the README already drew for it:
*"how a consumer tells a sweep that could not conclude from a command that could not start."*

**The cost is stated rather than hidden: a consumer must know a command's arity to predict
whether stdout will parse.** That was weighed against changing released output — giving `check` a
payload, or reversing J-24 — and an arity is at least discoverable from `--help` and stable,
where the alternatives move behaviour somebody may already depend on.

Nothing in the package changed. What changed is that the rule is written down and checked: a
test declares each command's path subject (a judgement), reads its arity from the parser (a
fact), and asserts the behaviour the two predict — so a command whose `nargs` changes is held to
the other clause of the rule on the day it changes. It also asserts the half that is not about
stdout at all: **every one of these names the path on one channel or the other.** Silence is a
signal to a consumer and never a reason to tell the person nothing, which is I-16's row.

Two of the five controls measured nothing on their first run and were rewritten. One mutated an
exit-code branch to try to silence `verify`, but `_verify` writes its payload at the top of the
function, unconditionally — so that clause is structural for `verify` and no branch change can
break it. The other hardcoded today's arities in place of reading the parser, which is a no-op;
changing the parser's `nargs` is what proves the expectation follows it.

### Fixed — one name meant two things across the commands, and now a check says so

Audit J, J-36 — the cross-command guard the audit closed by naming as the next thing to write.
Nine of its thirty-five rows were one command's vocabulary drifting from its sibling's, and
every one was fixed per command. This is the check that notices the class.

**It found five collisions on its first run, four with incompatible types** — a consumer
deserialising generically breaks on each:

| key | commands | types |
|---|---|---|
| `matched` | `log` / `show <target>` | **bool** vs **int** |
| `artifacts` | `impact` / `verify` | int vs list |
| `ok` | `check` / `verify` | bool vs int |
| `runs` | `lineage` / `show <target>` | int vs list |
| `unreadable` | six commands / `chain` | int vs list |

**`matched` was the one defect and it is gone.** It was the only collision *invented by both
payload builders* rather than owned by a structure, so R-9 bound neither side: `log`'s tri-state
*did a named target hit anything* is `matched_any`, and `show`'s count is `matching` — `log`'s
own word for the same number. Both were unreleased.

The other four are **declared, with reasons, and not defects.** R-12 makes each payload its
command's own structure and R-9 forbids renaming a field on the way out, so where two structures
legitimately own the same word the collision is a consequence of two rules this project chose:
`impact.Chain.artifacts` and `check.Report.ok` are `@property`, `lineage`'s `runs` is one of the
join's own counters, and `chain` *names* the unreadable lines because it walks them while every
other command can only count them. What the guard refuses is an **undeclared** one — a new field
quietly making a third meaning for a name a consumer has already learned.

It also carries the checkable half of the scope vocabulary, which is J-12 and J-14 as an
invariant rather than two fixes: **`shown` and `matching` are one pair.** A payload that reports
a window must report what it narrowed from, so a third command growing `--limit` is held to it on
the day it does.

Stated limits: two fields with the same type and different meanings pass — type disagreement is
the mechanically checkable half, and the table carries the other half as prose.

One line was deleted from the guard because a control refuted the comment beside it. The comment
called `isinstance(value, bool)` *"the single most consequential line"*, reasoning that `bool` is
a subclass of `int`; true of `isinstance`, irrelevant here, because `type(True).__name__` is
already `"bool"`. Removing it failed nothing. The judgement that *is* load-bearing — a `null` is
an answer and not a type — now has the control.

### Fixed — `runprov resources --margin` is accepted and ignored in silence no longer

Audit J, J-16. `--margin` scales a REQUEST for a scheduler. `--format slurm` and `--format k8s`
apply it and print the multiplier on the line it was applied to; `tsv`, `json` and the default
page report the MEASUREMENT, and passing it there changed nothing and said nothing — output
byte-identical with and without the flag. **The row named two formats; the default text view
ignores it too, which makes three.**

The treatment is the one `verify` gives `--log`: accepted for uniformity and *said* to be
ignored, naming the two formats where it does apply. `log --unreadable` is refused instead, and
the difference is whether an honest "ignored" semantics exists — here it does, because the margin
applies to a request and these formats are not one.

`--margin` now defaults to `None`, which is the mechanism rather than a detail: with
`default=1.5` there is no way to tell *not asked* from *asked for 1.5*, so the note would have to
print on every `--format json` invocation or on none of them. `_log_selectors` states the same
rule for `log`'s flags — a default is a value, and absence is what says nobody asked. The
multiplier applied when nobody asks is still 1.5, asserted.

### Fixed — `verify`'s in-process dicts are guarded against carrying a published schema

Audit J, J-28, and the reviewer's own measurement is the finding: they added `"schema": SCHEMA`
inside `verify()` and **the mutation survived the full suite at rc 0**, with the published payload
byte-identical — `payload` spreads the report after its own `schema` key, so the duplicate
collapses onto the same value. Sound reasoning, defended by nothing.

**And the reasoning as written was wrong twice, which this row found while building the guard.**
The docstring said putting `schema` in the return value "would have handed it to every caller
that never emits JSON, including `report`, which reads this result". Measured: `verify()` has
exactly **one** caller, `__main__._verify`, and that caller is the JSON emitter — so the stated
harm had no instance. And `report` does not read that result at all; it reads `verify_artifact`,
whose dict is `artifact`, `inputs`, `status`.

The property is right and now names its real subject: the cross-command consumer is
`verify_artifact`'s dict, which `report` folds into an answer versioned `runprov.report.v1`, so a
`runprov.verify.v1` key inside would claim a shape `report` does not promise. Both dicts are
guarded, and the harm is asserted at the consumer — `runprov.verify.v1` appears nowhere in
`report`'s answer.

### Fixed — ADR-0017's R-15 and R-16 now state `export`'s exclusion

Audit J, J-26. R-3's table excludes `export` with a reason — *"two standard vocabularies
already, and this would be a third"* — and R-15 and R-16, written later, did not mention it,
though R-15's scope reads *whenever THIS PACKAGE answers*. Read literally that required a
`runprov.export.v1` object on the stdout of the one command whose purpose is to speak RO-Crate
and PROV: a scope gap rather than a violation, and the kind that is resolved by whoever reads it
next rather than by the rule.

The exclusion is stated in all three places now, and the test side needed no change — which is
the point. `_cli_json_commands()` reads the parsers, so `export` is absent by construction rather
than by anyone remembering. A ratchet asserts that, and it is not a tautology: it fails the day
`export` is given a `json` format, which is exactly when the exclusion has to be re-read. The
reason the ADR gives is checked too, against `EXPORT_FORMATS` itself.

### Fixed — `runprov log --limit N` reported "nothing failed" over a history with a failure

Audit J, J-14. `failed` sat beside `total` and was counted inside `emit`, which runs once per
record that survives `--limit`. Over four runs whose oldest failed:

    $ runprov log --limit 2
    # 2 of 4 run(s) from runs.jsonl          <- no FAILED clause at all
    payload: shown 2, total 4, failed 0

A truncation answered *nothing failed* about a history with a failure in it — and because the
clause is conditional, the reader was not even shown a zero to doubt.

**`total` is not the right denominator either, and that is why `matching` exists.** `total` is
the history's size, and it has to be: it is the only thing separating *your selector matched
nothing* from *the history is empty*, which `_log_answer`'s own docstring argues for. Scoping
`failed` to it would report another script's failure to someone who asked about this one —
measured, `log --script other` over a history whose `build` failed would say `1 FAILED`.

So `failed` is scoped to the SELECTION: past the selectors, before the limit. Nothing in the
payload stated that number, so a consumer could not tell what `failed` was out of; `matching`
states it, and the page names it in the FAILED clause — `1 FAILED of 3 matching` — only when it
differs from `shown`, so an ordinary page prints the clause it has printed since 0.6.0.

This is the gap J-12 closed in `show` an hour earlier with `matched` beside `shown`: the same
command family, the same flag, three scopes and two names. `log` now states all three — `total`
for the history, `matching` for the selection, `shown` for the window.

### Fixed — `runprov show <target> --limit N` reported the narrowed count as the whole answer

Audit J, J-12. `payload_runs` set `matched` to `len(views)` — the list **after** `--limit`
truncated it — and nothing named the truncation: no `limit`, no `shown`, no `selectors`. `log`'s
sibling payload, built the same week behind the same flag, gets this right with `shown` +
`total` + `selectors.limit`.

**The text was wrong too, which the row did not claim.** Measured against the v0.6.0 tag itself,
five runs of one script:

    $ runprov show build --limit 2
    # 2 run(s) matching 'build'

A person reads that as a script that ran twice. So this is a false statement about somebody's
project in **released** output, and the tag is the oracle for it rather than the suite. The page
now says `# 2 of 5 run(s) matching 'build'` when the two differ, and prints the 0.6.0 sentence
byte for byte when they do not.

**The count had to come out of `select`, because `--limit` destroys it:** each bucket is a
`deque(maxlen=limit)`, so a run that matched and fell off the front is gone before the caller
sees the list. It travels in an out-counter — this module's own idiom, the way
`_counted(path, bad)` accumulates — and deliberately **not** as a named-tuple return:
`len(select(...))` appears in five existing assertions and a two-field tuple has `len() == 2`, so
the assertion expecting two matches would have gone on passing for an entirely different reason.
A silent false pass is worse than a break.

The counter is **per bucket**, not a sum over the four. The resolution is an `elif` chain, so a
target that is both a script name on one run and a `run_id` on another fills two buckets while
only the higher-priority one is the answer; summing would report both.

**And the guard that should have caught this did not read the wrapper.** `show`'s R-2 guard
perturbs the view of one run and reads `render_run`, so every field of `payload_runs` outside
`runs[]` was unguarded and `matched` could carry the wrong number with the whole suite green.
`log`'s guard covers its wrapper. One command was fixed and its sibling left, because the fix was
written per command rather than per class — which is the asymmetry this audit keeps finding. The
wrapper's fields are now asserted as a set, so a new one refuses until someone classifies it.

### Fixed — `runprov log` could not tell an empty history from one holding only a start

Audit J, J-13. `_log_answer`'s docstring argues for `shown` beside `total` because *"an empty
`records` list means one thing after a selector that matched nothing and another over a history
with nothing in it"*. There is a third state, and it was the one the payload could not express.
Over a history holding one start line and no record, against an **empty** history:

    total 0, shown 0, failed 0, unreadable 0, records [], matched null, cannot_check null

Equal in every content field — only `path` differed, and `path` echoes the argument. So *nothing
has ever run here* and *a run began and never came back* were one answer. `show` tells the same
two files apart, so this was a per-command gap rather than a missing capability.

The payload carries `unfinished` now, and the stderr tally names it too — conditionally, beside
the existing `FAILED` and `unreadable line(s) skipped` clauses, so a history with nothing
unfinished prints exactly the line it always has.

**It is paired, not counted, and that is the substance of the fix.** Every run leaves a start
line **and** a record — measured: two completed runs give a four-line history — so a raw count
of start lines is the number of runs that *began*, and over a healthy three-run project it would
have read `3`. The counter retires a start when its `run_uid`'s record goes past. That costs
almost nothing in the one reader whose design forbids materialising, because a run's start and
its record are appended adjacently: the pending set holds one uid at a time in the ordinary case,
and only grows for runs that are genuinely unfinished. It holds uids, never records.

**A count and not a list**, deliberately. `show`'s `in_flight` carries each run with its liveness
because it reads the `.incomplete` markers as well; `log` reads the history and nothing else, so
what it can honestly report is how many runs have no ending **on record** — permanent,
append-only evidence that says nothing about whether a process is still alive. Naming it
`in_flight` would have promised the half it cannot see.

### Fixed — `runprov resources --format json` said "nothing was unavailable" where nothing was asked

Audit J, J-11. `payload`'s own docstring states the rule: *"`null` against a figure here means
nothing measured it"*. Every field obeyed it except `unavailable`, which was `[]` — so it was
the one field needing a special case in a consumer, and the one whose value was a true statement
about a **different** state: a run that *was* measured and had nothing unavailable.

**The row's premise was wrong and is recorded as wrong.** It said the no-measurement payload was
*"byte-identical to a cluster run where every mechanism answered"*. Built and measured, those two
differ in **twelve** fields — every figure, `script`, `run_id`, `mean_cores`, `source` and
`cannot_check`. The defect was never a collision between two payloads; it was one field inside
one payload disagreeing with the rule the other eleven follow.

**The row's second half is refuted.** It said `source: null` duplicates what `Measurement`'s
closed vocabulary already spells `"none"`. They are two facts: `"none"` is a run that *was*
measured and whose mechanisms all declined — `wall_seconds` present, `cannot_check` null — and
`null` is a run nothing measured at all. Both states are built and asserted, so the refutation
rests on a measurement rather than on a reading.

One existing assertion moved with it, and the line above it was already the argument: it pinned
`unavailable == []` directly beneath *"every figure null, so nothing here can be read as a
measurement of zero"* — the same exemption the payload made, repeated in the test that should
have caught it.

### Changed — `runprov verify` names a path that is not there, and no longer passes over it

Audit J, J-24. **The exit code moves, 0 to 2, and that is the point of the entry.** `verify`
takes any number of paths, and with one good path beside a mistyped one it reported `1 OK` and
exited **0**, naming the missing file on neither channel:

    $ runprov verify out.tsv typo.tsv --root .
    # 1 pinned artifact(s) of 1 file(s) under .: 1 OK, 0 STALE, 0 GONE, 0 UNVERIFIABLE
    OK           out.tsv  ['r']
    rc=0

The README endorses `verify results/` as a CI step, so a pipeline that silently stopped
producing an artifact passed its own gate. `collect` dropped the path with a bare `continue` —
absent from the file list, from every count, and from both channels — against its own
docstring: *"Asking about one by name is an answerable question and it gets answered."*

A-17 is the same defect one category over, for a directory that could not be listed, and its
comment is the description: *"A pinned artifact inside it simply did not exist as far as the
report was concerned, and the run exited 0."*

It now exits 2 and says which paths it could not examine, in `paths_absent` and on the page,
with its own headline — *NOT CHECKED*, because *NOTHING CHECKED* would overstate it when an
artifact was verified. **A finding still outranks it:** `STALE`, `GONE` or `ALTERED` keeps exit
1, since something checked and wrong is a stronger statement than something not looked at.

The filed row was the narrower half — that a mistyped path and an empty directory produced
**byte-identical output on both channels**. They no longer do, and route 1's own sentence is
asserted rather than assumed, because the first version of this fix deleted that clause while
reordering the verdict and an empty directory fell through to route 2's sentence instead. A
direct measurement of the four states caught that; the suite did not.

`collect` returns a `Collected` named tuple now. Five tests broke on the old 4-tuple's arity
and none on the meaning of the change, which is what positional unpacking costs a call site
that does not care; the next category added costs nothing.

A dangling symlink named on the command line is one of these, deliberately and in one category:
the link is there and the artifact is not, the repair is the same either way, and a broken link
the walk merely came across is still not something the caller asked about.

**Two statements of the exit-1 family were wrong and are corrected.** README's `report`
paragraph still said *"1 when one is `STALE` or `GONE`"* — the fourth copy of that list, missed
yesterday because J-19's guard was scoped by hand to the exit-code table's row alone, inside the
fix for a defect caused by a hand-written scope. The guard now reads both statements, located
by what they claim rather than by position.

### Changed — `runprov report` exits 2 for `NO PIN` and `UNVERIFIABLE`, matching `verify`

**A behaviour change on a documented path, and deliberately so** — Audit I, I-10, decided by
Taylor. `report` folded five verdicts into two codes (`0 if result.ok else 1`), so `NO PIN` and
`UNVERIFIABLE` came back as **1** — *checked and wrong* — while `verify` reported **2** for the
same artifact in the same second. Measured, back to back on one file: `verify nopin.tsv` exited 2
with *"NOTHING CHECKED … this is 'we could not look', not 'nothing is wrong'"*, and
`report nopin.tsv` exited 1.

ADR-0007 was written to refuse exactly this: *"Collapsing them turns 'your provenance is not
running at all' into 'your results are stale', and sends somebody to re-run a pipeline over a
problem that re-running cannot touch."* `check` was brought into line in A-08 — "three outcomes,
three codes" — and this command was missed, **though its own docstring already promised 2 for an
absent artifact**. So the third code was already this command's contract, applied to one state out
of the three that deserve it.

**What moves:** `NO PIN` and `UNVERIFIABLE`, from 1 to 2. `OK` stays 0 and `STALE`/`GONE` stay 1.
A gate written `runprov report x || abort` is unaffected — both codes are non-zero. A gate written
`if rc == 1` will stop treating *could not check* as *checked and wrong*, which is the correction.

**Derived, not enumerated.** The mapping reads `verify.OK` and `verify.FAILING` — the package's own
name for *checked and wrong* — so a verdict that is neither falls to 2. A sixth verdict added to
`verify` therefore defaults to the safe answer rather than being folded silently into "wrong", and
the test asserts the same expression rather than a second copy of the list.

Measured over all four verdicts an artifact can have, `report` and `verify` now return the same
code for the same file: OK 0/0, STALE 1/1, UNVERIFIABLE 2/2, NO PIN 2/2.

### Changed — `report` exits 1 for an `ALTERED` artifact, matching `verify`

Audit J, J-19. Measured across every state `verify` has a word for, before the fix:

| state | `report` | `verify` |
|---|---|---|
| `OK` | 0 | 0 |
| `STALE` | 1 | 1 |
| `GONE` | 1 | 1 |
| **`ALTERED`** | **2** | **1** |
| `NO PIN` | 2 | 2 |
| `UNVERIFIABLE` | 2 | 2 |

`report` said **2, could not check** about a file whose body digest had been compared and did
not match — the strongest finding this checker makes, reported as an inability. Two commands
answering about one artifact in the same second with different codes is what ADR-0007 exists to
refuse, and it is J-18's defect in the other direction.

**The cause was one incomplete tuple.** `FAILING = (STALE, GONE)` is the package's own name for
*checked and something IS wrong*, and `report` derives its exit code from it deliberately, so
that a verdict nobody anticipated falls to the safe answer instead of being folded into "wrong".
`ALTERED` is not an unanticipated verdict; it is decided before any input is consulted, and it
was missing. Widening the tuple fixes it in one line and cannot disturb `verify_artifact`, whose
`FAILING` branch reads INPUT statuses — and an input entry carries only `OK`, `STALE`, `GONE` or
`UNVERIFIABLE`, which a test now asserts rather than assumes.

**The same list was written three times and only the copy with no claim to be the contract was
right.** `verify.FAILING` said two states; the README's exit-code table said *"a stale or gone
artifact"*; `_verify`'s own exit-1 branch said `stale or gone or altered`. The README row now
names all three, and a guard reads the branch's condition out of the source and compares it with
the tuple, so the two cannot drift apart again. `ALTERED` had been missed by an enumeration
before: Audit B found it absent from `verify.STATES` after it was added.

**And the test that should have caught it was green, for a reason its own docstring described.**
It claimed to be *"DERIVED FROM `verify`'s OWN VOCABULARY, NOT A HAND-WRITTEN MAP"* — and the
map was derived while the state space was four fixture names in a loop, with no `ALTERED` among
them. A derived expectation over a hand-listed set of states is the scope pattern wearing the
words of its remedy. The state space is now `verify.STATES`, so a seventh verdict fails by name.

### Fixed — `runprov show --format json` said no run was unfinished while its own text named one

Audit J, J-06. Over a history that is not there, with a marker beside it, one invocation
reported two contradictory things in the same second:

    stderr   # 1 run(s) STARTED with no ending recorded:
             #   INTERRUPTED  long-job  2026-10-01T12:14:46Z  pid 2314806
    stdout   "in_flight": []

The branch calls `_report_in_flight` six lines before building the payload, precisely because a
marker beside a missing history is not *nothing recorded* — it is a run that started and never
got to write one, which is the more alarming of the two findings and the reason the scan is
there at all. The scan was then discarded, because `payload_no_history` had `in_flight` wired
to `[]` and no parameter to take it.

`[]` is not a neutral default here. Per the README's rule, `null` is *looked and found none* and
an absent key is *this page did not look*; `[]` is the list-shaped form of the first. This page
had looked and found one. **So the most alarming state the command has was the one its JSON
reported as clean**, while the text — correct since A-15 — said otherwise directly above it.

Both liveness states are covered, not only the alarming one: a marker whose process is still
alive reads RUNNING and one whose process is gone reads INTERRUPTED, and the payload said `[]`
for both. `log` and `lineage` share that lookup and take the scan without using it, because
`in_flight` is the project page's own fact and a join over `run_uid` has no such field in any
state.

### Fixed — one history named two ways read as two destinations

Audit J, J-35, found while reproducing J-06 in the six lines above it. The same branch compared
a marker's recorded history with the path it was asked about **as strings**, so one file
answered differently depending on how it was typed:

    --log /abs/dir/runs.jsonl   "Nothing has been recorded here yet"
    --log runs.jsonl            "A MARKER BESIDE THIS PATH SAYS OTHERWISE: the run that left it
                                 recorded to /abs/dir/runs.jsonl"
    --log ./runs.jsonl          the same, a third spelling of the same file

The last two tell the operator the records went somewhere else, name the path they just passed,
and advise *"If that names a file, pass it to --log"*. The marker always stores an absolute path
and a relative `--log` is the ordinary way to type one, so the misleading answer was the one a
person was most likely to get.

The comparison resolves both sides now, which also makes a symlinked results directory — the
ordinary case on a cluster — read as the one file it is. The message itself is untouched and
asserted intact for the two states it exists for: a run that really did record to a different
file, and one that recorded to a `sink` that is not a path at all. A fix that silenced it would
have been worse than the defect, because for a `sink=` project no other output of this command
reveals where the records went.

### Fixed — `runprov verify --format json` carried the counters and not the conclusion

Audit J, J-18. ADR-0017's R-16 table told a consumer to read *could not check* off a count
relationship — `artifacts_seen > 0` with `artifacts_pinned = 0`. Measured, that is **false in
three of the four states this command exits 2 in**, twice from the other side of the inequality:

| state | `artifacts_seen` | `artifacts_pinned` | R-16 as written |
|---|---|---|---|
| a file carrying no pin | > 0 | 0 | holds |
| an empty directory | 0 | 0 | fails `> 0` |
| a path that is not there | 0 | 0 | fails `> 0` |
| every pin UNVERIFIABLE | > 0 | > 0 | fails both |

A consumer applying it literally read *verify could check* in three states out of four. The ADR
called this entry *"the weakest of the five"*; it was not weak, it was wrong in the majority of
its states, and the fourth route — every pin unreadable — it did not describe at all.

**The true relationship was not something to document instead**, which is why this is a field.
It is two clauses over five counters whose order matters: a report with one STALE artifact has
`ok == 0` as well, so a consumer testing `ok == 0` reports a FINDING as an inability — the
inversion the rule exists to prevent — and the precedence that stops it lived in the CLI's
branch order where nothing published it. `verify.cannot_check()` is now that fold, the payload
carries its result, and both of the command's *NOTHING CHECKED* sentences are printed from it
instead of composed beside it.

Additive: every counter keeps its name, its value and its place, `cannot_check` is appended, and
both pages are byte-identical. Four controls; dropping the exit-1 clause, silencing the second
route, dropping the key and letting the page recompose its sentence are each caught, and two of
them by the derived R-15 and R-16 checks without an edit to those tests.

Two statements about this command were also corrected rather than quietly dropped: the test
suite's copy of R-16 said `verify`'s vocabulary was `verdict: "NO PIN"` — a key its payload has
never had — while the table six hundred lines below carried the count pair.

### Fixed — `runprov chain`'s page and its JSON named different routes to one verdict

Audit J, J-04. The page and the payload agreed on the verdict and disagreed on its reason.
`render` decided its headline from `lines == 0`, then from nothing being chained, then from the
status; the fold behind `status` and `cannot_check` tested the edge statuses and a lost
terminator FIRST. Over a history with nothing chained **and** a damaged line both applied, and
one report was described two ways — the page saying *"2 line(s), none of them chained."* while
`--format json` said *"a line lost its terminator and merged with the next"*. Reachable from a
single truncated byte, and from a torn first line in front of a pre-chain history.

J-01 unified `status` with `cannot_check` and left the renderer as an unreconciled third fold.
The order is now the renderer's — if nothing is chained there are no claims to check, so the
edge statuses are consequences — and the page PRINTS the fold's reason rather than composing
its own copy of the sentence.

Measured: `status` moved in **0 of 18,432** constructed states (every subset of the seven edge
statuses x four line counts x four `chained_from` values x three `merged` x three `unreadable`),
because every route under `BROKEN` returns `CANNOT_CHECK` — so only which reason is named
changed, and `cannot_check` has not been released. The page is byte-identical over all eight
real histories in the test's corpus, including the two that disagreed.

`BROKEN` keeps its precedence, and a `BROKEN` verdict cannot appear under a *CANNOT CHECK*
headline for a reason the table proves rather than the fixture: all 88 of R-25's `BROKEN`
outcomes require a claim, and the first line carrying one sets `chained_from`.

**One reason's wording moved, in the payload only.** `cannot_check` for an absent or empty
history read *"there is no history to read"* and now reads *"no history to read"* — the page's
own wording, taken up by the fold because the page is output 0.6.0 shipped and this field is
not. Single-sourcing means a reword here now rewords the page, so both released sentences are
pinned by a test; before this fix nothing anywhere pinned *"CANNOT CHECK: no history to read."*

### Fixed — `runprov report --log <a path that is not there>` said nothing at all

Audit I, I-16. Measured across the five commands that read a history: `impact`, `diff` and `log`
exit 2 and name the path on stderr, `chain` prints *"CANNOT CHECK: no history to read"* as its
verdict — and `report` said **nothing on either stream**. It printed a page reading *"NOT FOUND in
the run history supplied"*, which a reader takes as *there is no such run* rather than *the file
you named is not there*.

It now says so, naming the path and what it is reporting from instead: the artifact's pin.

**The exit code is deliberately NOT moved, and the sibling comparison that suggests moving it is
unfair.** `impact`, `diff`, `log` and `chain` cannot answer without a history — it is their
subject. `report` can: the pin is self-contained and travels with the file, which is ADR-0007's
second hook and the reason the command works on a copy someone emailed you. `limits.run_not_found`
already carries the absence as a fact, and a test demonstrates R-8's null-versus-absent rule
through exactly this route. Exiting 2 would remove a documented capability to fix a silence.

Whether the code should move as well is a behaviour change on a documented path, and is recorded
as open rather than taken.

### Fixed — `runprov chain` describes a claimless line by who wrote it, not by where it sits

Audit H, H1-1/H1-2/H1-3. R-25's rule 4 decides `UNCHAINED` by POSITION, so every claimless
line at the front of a file was described as predating the chain whatever wrote it. An append
that could not take the file lock writes no claim by design (ADR-0016 R-22), so a project whose
early runs landed on NFS, CIFS or a container without `flock` has such a block written by the
CURRENT release. Measured: one unlocked run then one locked one printed `2 line(s) predate the
chain` over two lines stating `0.6.0`, on an `INTACT`, exit 0 page.

Those lines now get a sentence that says what is true of them — a release that can chain wrote
no claim, which the lock explains and a hand-inserted line would too — and the early return
that says *"written before the chain existed; the next run to append will anchor it"* now fires
only where the whole of its stated precondition holds. It also **stopped discarding
`unreadable` and `merged`**: a destroyed record boundary was reported by `--format json` and
invisible in the text. Verdicts and exit codes are unchanged everywhere, and a genuine
pre-chain history reads exactly as it did.

### Fixed — a break says where to look, and stops naming a cause the file cannot know

Audit H, H1-4. `BROKEN` printed *"LINE N-1 IS WHAT CHANGED"*, in capitals, over a mid-file
**deletion** and over a **reorder** — naming a line that is byte-for-byte what the original
held. Measured on a six-line history: delete line 3 and it accused line 2; swap lines 3 and 4
and it accused lines 2, 3 and 4 in turn. Deletion and reordering are two of the three threats
the feature names, and both take the same path an edit takes, so nothing distinguished them.

The sentence keeps the part a reader acts on — the discrepancy is at the boundary **below** the
named line, not in it — and drops the choice of cause, which an edit, a deletion, a reorder and
an insertion all leave looking identical. Both digests are still printed whole, and an in-place
edit is still named as a possible cause.

### Fixed — the `UNCLAIMED` sentence offers only the causes the record cannot exclude

Audit H, H1-10, against ADR-0016 R-32 as amended. The sentence offered *"a run still in flight
has not written its completion record yet"* unconditionally. Measured on a real 0.6.0 run killed
mid-flight: **it writes a chained start line**, so over a line naming a chain-capable release
that explanation is ruled out by the file's own bytes. The clause is now printed only where the
line names no version and none resolves from its run — G-05's case, which is the one it is true
of. Verdict and exit code unchanged; what changes is a reader no longer chasing something the
record denies.

### Fixed — a coverage gap names the version it was judged by

Audit H, H1-6. The `GAP` sentence resolved the writer's version a second time, from the line's
raw `tool.version` alone, while the judgement had already resolved it from the run (ADR-0016
R-29). A record whose own version field is not a string was judged on `0.5.0` and reported as
*"1 line(s) written by runprov 12345"*, and `Link.wrote` — annotated `str | None`, and published
by `--format json` — held the integer. One expression answers both now.

Also removed, both unreachable and both noted by the same pass: *"a version it does not name"*,
which a `GAP` edge can never print because rule 5 fires only once a version has resolved, and
`Link.detail`'s non-`BROKEN` arm, whose only caller iterates the `BROKEN` edges. `_findings`'s
docstring no longer claims its sentences come out in ADR-0016 R-25's order; they come out
weakest-claim-first, and it now says so.

### Fixed — `show --format json` shipped the page's display values, and said `state` two ways

Audit J, J-07, J-08 and J-09. Three defects in one command's payload, all of them R-8 or R-9.

**Display values had leaked into the payload.** `run_view` held a 12-character `run_uid` where
`log --format json` carries all 32 for the same record; a 16-character digest where the record has
64, **with the key it came from computed and then discarded** — so a consumer could not tell a
`content_sha256` prefix from a `sha256` or `sha256_tree` one, which is the distinction I-27 exists
for; and the strings `"-"` and `"none"` where the record says `null`, both truthy.
`_recorded`'s own docstring records a filed defect caused by comparing against that dash, and the
payload was shipping it to every consumer at once.

The abbreviations now live in the renderers and the view holds facts, which is R-1's shape: one
builder, two renderings, the renderings free to say less. **The staleness comparison is untouched**
— `_recorded_pair` still truncates, because that is what the comparison and the eye both use, and
both of its sides truncate.

**`state` meant one thing in two spellings.** It was `null` on the project page when nobody asked
and the key was ABSENT on a target page, which also had not asked — two readings of R-8 for one
key, ten lines apart in one module, each cited in an assertion. R-8 already assigns *this page did
not look* to an absent key, so the null said nothing new; and `--stale` over a project with no
artifacts yields `{}`, so *asked, and found none* had its own encoding already. `state` is absent
unless asked.

**AND A DRIFT IN RELEASED OUTPUT WAS CAUGHT BEFORE IT SHIPPED, by diffing against the tag.**
`--format yaml` renders the same view and shipped in 0.6.0. Moving the truncations out of that view
rewrote it — uid 12 → 32, `"none"` → `null`, digest 16 → 64, a key added — and **every one of
`show`'s tests passed.** A second pass then showed it still differed by KEY ORDER, two lines per
entry, because the entry dict was built digest-first where 0.6.0 put `path` first. R-3's amendment
is what settles the design — *"`show --format yaml` … is a rendering of a page, not an answer"* — so
the yaml renders through a display view and is byte-identical to 0.6.0's. A test now pins that,
because nothing did.

### Changed — `runprov diff` exits 1 for a named address that matched nothing, as L-81 says

Audit J, J-25. **`diff` contradicted a contract this package had already ratified.** L-81, decided
2026-09-01 and quoted at the top of `__main__.py`, puts *"a named target or filter that matched
nothing"* in the **1** family — *checked, and something IS wrong* — and reserves **2** for *could
not check*. `show <target>` and `log --script` both implemented it. `diff` returned 2, so a
consumer reading L-81 got the wrong answer for one of the ten commands.

This is a behaviour change on a documented path and Taylor ruled it on 2026-09-30, on the evidence
that L-81 already answered it rather than on a preference between conventions.

**`diff`'s other three refusals stay at 2**, and the line is L-81's own: an address naming a single
run, two addresses resolving to one run, and no history at all are all cases where no comparison
could be FORMED. **Matching one run is not matching nothing** — that distinction is the whole of
L-81's sentence.

**And nothing asserted what `diff`'s four refusals SAY.** An Audit J reviewer replaced all four
reasons with the literal `"could not check"`, left stderr untouched so the page still named the
route, and the entire suite stayed green — J-01's finding one command over, since `chain` gained a
reason and a test naming each of its four routes while `diff` had the same shape and nothing
equivalent. The four are now asserted as a distinct set, and each is asserted to name its own
route, because four unique strings all saying the wrong thing would satisfy a set check.

A test asserts the three target-taking commands agree on L-81's 1 family together, so the
agreement is the assertion rather than three facts that happen to line up.

### Fixed — a truncated `impact` walk exits 2 and now says so in the payload

Audit J, J-20. `impact --depth 0` empties `steps` while `seeds` stays non-empty, and C-07 is the
row that made that exit 2 rather than 0 — *"a pre-overwrite guard written as `runprov impact ref.fa
--depth 0 || abort` goes green and the reference is overwritten"*. The payload did not follow: it
was built ABOVE the fold that decides the exit code, so it printed `truncated: true` beside
`cannot_check: null` and exited 2. A payload asserting nothing went wrong, attached to a code
saying something did — which R-15's own guard calls worse than no payload at all.

**Two defects, and the second is the one worth recording.** `impact.payload` accepted a
`cannot_check` argument and **hardcoded `None` on the path where a chain exists**, so the reason was
honoured only in the no-walk state it was added for. A caller could pass one on this path and be
silently ignored, and the call site read as correct. A parameter accepted and dropped is worse than
one not offered.

The reason is read off `chain.truncated` — the same property the exit code reads twelve lines
below — because two expressions for one fact is H1-6, and here the fact is the verdict.

**And R-15's guard has stopped hand-listing what it checks.** This state was in neither of its
buckets, which is why it hid: `impact` had two entries and a third exit-2 state nobody had
enumerated. The guard's reason-check was also scoped by a name prefix with a hand-written count
beside it, and **that count broke the moment this row added a sixth state** — the stale-list
pattern inside the check written to avoid it. The scope is derived from R-16's table now, which
widens the reason assertion from **two commands to seven**: `chain`, `log`, `lineage`, `resources`
and `show` were unchecked for content and are not now.

### Fixed — `lineage --format json` said nothing about lines it could not read, and its two shapes differed

Audit J, J-21. Two defects in a payload that **shipped in 0.6.0**, fixed together on Taylor's
ruling of 2026-09-30. Both additive: `path` and `unreadable` are added, `schema` and `cannot_check`
keep the places they took since the tag, and the join's own six counters keep their names and their
order.

**A history where NOT ONE line parses was byte-identical to an empty one.** The count of lines the
reader could not use reached **stderr alone**, and R-4 tells a caller that stdout is the payload and
nothing else — so a consumer obeying the rules read a wholly corrupt history as a history with
nothing in it. Measured: three unreadable lines now give `unreadable: 3` where an empty file gives
`0`. **`show` was fixed for exactly this the day before and `lineage` was not**, which is what
fixing a defect per command costs when the commands share it.

**And the two shapes differed by `path`**, present only when the command could NOT answer — so key
presence depended on whether it succeeded, which is the inference R-8 exists to remove, and the
answered payload could not say which history it described. `lineage` was the only one of the three
commands sharing the missing-history guard whose no-history answer was **hand-written as a second
dict** instead of calling the answered path's builder; `log` and `show` both share theirs, and
`lineage` drifted within one commit. There is one builder now.

**A guard covers all of them, not two.** `test_a_cannot_check_payload_has_the_same_keys_as_a_real_one`
asserted this for `impact` and `diff` while its docstring claimed one shape per command; the new
test derives the command list from the parser, so a command cannot join the family and be skipped.
`report` and `verify` are excluded by measurement rather than omission — neither has a
cannot-check state with a different shape, because for a verdict command the inability IS the
answer (R-16).

### Added — `chain`'s payload names which route reached `CANNOT_CHECK`

Audit J, J-01. `chain --format json` said `status: "CANNOT_CHECK"` and nothing more, though it
reaches that verdict by four routes with different fixes: an edge that is `UNCHECKABLE`, `GAP` or
`UNCLAIMED`; a line that lost its terminator and merged with the next (G-03); a history with
nothing chained in it; and no history to read. The text page names each in words. `cannot_check`
now carries it, and is `null` for every other verdict.

**ADDITIVE ON A PAYLOAD THAT SHIPPED in 0.6.0** — the key SET, which is what a consumer reads
by. `cannot_check` is appended last and nothing is renamed or removed.

**CORRECTED 2026-09-30 by Audit J (J-02).** This entry first said *"every key 0.6.0 emitted keeps
its place, nothing reordered"*. That is false: measured against the tag, **8 of `chain`'s 10 keys
have moved** since 0.6.0 — `status` 2→8, `lines` 3→2, `attested` 4→9, `chained_from` 5→4,
`translated` 6→5, `merged` 7→6, `unreadable` 8→7, `edges` 9→3. The reorder is `abeff47`'s, not this
commit's, and **the I-24 entry below already records it with these exact two lists** — so this
section contradicted itself 190 lines apart and the newer statement was the wrong one. The reorder
is unreleased, so no consumer has met it; both statements would have shipped together.

The test cited as proof asserts the CURRENT order, which is a useful drift ratchet going forward
but is not what its comment claimed; its wording is corrected too. `chain.payload`'s own docstring
was honest about this all along — *"Key ORDER differs from the hand-written version and nothing
depends on it"* — so the code and this entry disagreed, and the code was right.

**The row as filed was wrong, and the correction is the part worth keeping.** It said the payload
"distinguishes none of" the four routes. Measured, it distinguishes **all** of them — `lines`,
`chained_from`, `merged` and the edge statuses all travel, and each route is a different
combination. Unlike I-08, no input is private. What was missing is the **conclusion**: naming the
route means reimplementing the verdict's fold including its precedence, and the precedence is the
part that cannot be derived, because when two routes apply at once only the fold's order decides
which is reported. That is I-08's shape one level up — the inputs travelled and the conclusion
did not.

**One fold, two readers.** `status` and `cannot_check` are both read off a single `_verdict`
property rather than computed separately. The precedence is the fact here, so two folds in the
same order would be two chances to get it wrong with no way to notice — H1-6 is the row where one
fact resolved a second way disagreed with the first. A test asserts the order directly, including
that `BROKEN` still beats everything so a reason never appears beside an accusation.

### Changed — every exit-2 state that is an ANSWER now carries its payload

ADR-0017 R-15, decided 2026-09-29, applied to the commands that predated it. Five states where a
command formed its question, read what it was given, and could not answer now serialise instead
of printing to stderr alone: `impact` with no history, and `diff` with no history, with one
address naming fewer than two runs, with a named address matching nothing, and with two addresses
resolving to the same run. Each carries `cannot_check` with the reason, and `diff` carries
`settled: false` beside it — the same fact its exit code carries, so a consumer keying on
`settled` needs no special case.

**Three states deliberately stay silent, and the test now says so rather than leaving it to
look like an omission.** `report` with an artifact that is not there, `impact` with a target that
is neither a file nor a digest, and `diff` with an empty run address are all cases where the
command could not FORM its question. R-15 reserves an empty stdout for exactly that, and it is
the only signal separating *I could not answer* from *you mistyped this*. `check` with a
directory that does not exist established the precedent when it shipped.

**The ratchet that tracked this work was wrong in both directions, and that is worth recording.**
It named four states; two of those were already correct, and three of `diff`'s five were missing
from it entirely. A hand-written list of states goes stale exactly as a hand-written list of
fields does, and the enumeration it was standing in for lives in the code.

**`impact` and `diff` both promise in their own docstrings that nothing is absent from their
payloads.** A `cannot_check` object that dropped a field would break that where a consumer can
least cope — the run where something already went wrong — so a test compares the two shapes' key
sets. It caught `Chain.unreadable` missing from the new branch on the first attempt: I-01's
field, added to the structure long after, invisible to every other test because nothing built
that branch.

### Added — `runprov show --format json`, and T-33 is complete

ADR-0017, the seventh and last row of T-33. **All ten commands that answer a question now offer
`--format json`**, which is R-3 satisfied rather than scheduled; the test that tracked the
remaining work has counted down to zero and now asserts the rule outright.

**The shape was decided by R-3's amendment, not chosen.** *"`json` under R-5 is an ANSWER … The
same holds for `show --format yaml`, which is a rendering of a page, not an answer."* So this is
not the YAML view with a schema bolted on, and the difference is worth two facts: `unreadable`
and `in_flight` previously reached **stderr only**. A consumer obeying R-4 — stdout is the
payload and nothing else — could reach neither, and would read a page assembled from a torn
history as complete, or one describing a run that is still writing as finished. `in_flight` is a
list rather than a count, because the question it answers is *which*.

**Two shapes, and the absences are R-8.** With no target, `target` is `null` and the object
carries `project`, `state` and `in_flight`. With a target it carries `matched` and `runs`, and
those three keys are absent rather than null: a target page never builds the artifact index,
never computes staleness — the flags are refused with a note — and never scans for markers. An
absent key is *this page did not look*; `null` would claim it looked at three things and found
nothing in them.

**The payload embeds the view the renderer is given rather than re-deriving it**, which is a
stronger guarantee than the perturbation guards its siblings carry: those assert that two
renderings agree, and this leaves no second expression that could drift.

**Both non-zero exits carry the payload.** A target that matched nothing exits 1, and a missing
history exits 2; each is an answer, and R-15 requires both to serialise. The first was found by
measuring rather than reasoning — it exited 1 with empty stdout, which under R-15 is the signal
for *the invocation was wrong*, and the invocation was fine. This row also supplies the entry
that removes a deliberate `KeyError` from the shared missing-history guard, left there when
`log` was built because it was unreachable until `show` gained this flag.

### Added — `runprov resources --format json`, the measurement rather than a request

ADR-0017, the sixth row of T-33. R-12 named this command's builder and was right twice over:
`Measurement` existed, and it already carried `source` and `unavailable` — the two things R-7
requires to travel with a number were here before the rendering was.

**`source` is the field that matters.** ADR-0013 R-5 is the row establishing that a cgroup peak
and a `getrusage` peak are different quantities; a figure without its source is one a consumer
can compare wrongly and never know it. `unavailable` lists what could not be obtained and why,
in the same object as what could.

**`--margin` is deliberately absent.** It scales a REQUEST for a scheduler, and this payload is
the measurement; a consumer applying its own headroom needs the number that was measured, not
one already multiplied by somebody's default. `--format slurm` and `--format k8s` remain the
renderings that produce requests.

**Both exit-2 states serialise, and they are different answers.** A history that is not there,
and a history that was read and holds no run carrying the block. Each gives every figure `null`
with `cannot_check` saying which — never a shape that could be read as a measurement of zero,
because this handler's own docstring says a renderer printing a request from no measurement
"would be the worst possible output, because it looks exactly like a measured one".

**Four page/payload asymmetries, named as a set** — more than any sibling has. `max_vms_bytes`,
`io_read_bytes`, `io_write_bytes` and `io_self_only` reach the payload and no state of the text
view: a person sizing a job reads peak RSS, since virtual size counts address space a process
reserved and never touched. All four are the safe direction, and naming them as a set is what
makes a fifth fail a test rather than be discovered by a consumer.

### Added — `runprov log --format json`, the ANSWER rather than the records

ADR-0017, the fifth row of T-33. R-3's amendment already drew the line this implements:
`--format jsonl` is the RECORDS, one per line, as stored, and `json` is a single object
carrying the schema, the question's result, and what could not be established. A consumer
asking *what did this command find* and one asking *give me the records* are asking different
things, and only the first has anywhere to put a qualification.

It carries `shown` beside `total`, because an empty `records` list means one thing after a
`--script` that matched nothing and another over a history with nothing in it — the same
distinction `check.examined` exists for. `matched` is a **tri-state**: `true`, `false`, or
`null` when nothing was NAMED, since `--script` and `--run-id` name a record while `--failed`
selects a class and no failed runs is the good answer. `selectors` normalises argparse's own
defaults, which are values rather than absences — `--script` defaults to `""` and `--limit` to
`0`, and a payload saying `"script": ""` claims a script named empty string.

**This is the one format that materialises, by construction.** An answer is a single object, so
it cannot be written until the last record has gone past; `jsonl`, `yaml` and `text` still write
each record as it streams and remain what a 100,000-run history should be read with. The
docstring and the README both say so rather than leaving it to be found on a large file.

**`lineage` gained R-15 compliance from the same line.** `log`, `show` and `lineage` share one
missing-history guard, and it was the guard that was silent rather than the commands: a named
history that is not there is an ANSWER, and both now say so in JSON with `cannot_check` carrying
the reason. `lineage`'s ordinary payload gained `cannot_check: null` in the same pass, because a
key present at exit 2 and absent at exit 0 forces exactly the inference R-8 exists to remove.

### Added — `runprov check --format json`, and a sweep that scanned nothing says so

ADR-0017, the fourth row of T-33. R-12 already named this command's builder and it was
accurate: `check.Report(examined, entry_points, flagged, unparseable)` existed and only the
rendering was missing. `check` is the command a CI job runs, so it is the one whose answer is
most often read by something that is not a person — and until now that something had to parse
the prose of a page whose wording this file has changed more than once.

**`examined` is the field that matters and it is why R-12 named it.** A sweep that parsed no
Python and a sweep that parsed 647 files and found nothing produce the *same empty* `flagged`
list, and A-08 is the row where that produced a green gate over a CI typo. The payload carries
`examined`, `entry_points`, `ok` and `examined_nothing` — the last a REASON in words rather
than a flag, because "no Python file was found here" and "files parsed, none is an entry point"
are different mistakes with different fixes. `null` there means the sweep did check something,
which is R-8's *looked and found none* rather than *this did not look*.

**Exit 2 carries the payload, and a missing directory does not.** This is the first command
built under R-15, decided the same day: a sweep that could not conclude is an ANSWER and
serialises; a directory that is not there means the command could not start, so stdout stays
empty. Both exit 2, and that difference is the only thing separating them for a consumer —
which is the whole of why R-15 exists, `argparse` having taken exit 2 for usage errors first.

The exit code is unchanged by the format. A guard walks the payload's own leaves and asserts
each one moves the page when it changes, so a field cannot reach one rendering and not the
other, and the two ratchets that count the remaining `--format json` work both shrink by one.

### Fixed — `verify` and `lineage` say which shape their JSON is, as the other four already did

Audit I, I-18. ADR-0017 R-5 requires every JSON payload to carry
`"schema": "runprov.<command>.v1"`, so a consumer can tell which shape it has without inferring
it from which keys happen to be present. `verify --format json` and `lineage --format json`
shipped in 0.6.0 emitting a raw dict with no such key, while `report`, `diff`, `impact` and
`chain` have carried one since they were written.

**R-3's own amendment lists both commands as done, which is how it went unnoticed.** The rule
was applied from a list of the commands that answered in JSON when it was written, and two more
grew the format afterwards — the scope pattern in a rule rather than in a check. Both sides are
now derived instead: a test reads the commands that offer `--format json` out of the parser by
AST, reads the commands whose schema is actually asserted out of the test file's own
`runprov.<name>.v1` literals, and fails naming any command in the first set and not the second.
A seventh JSON command fails it on the day it is added. `export` is outside R-5 and not by
omission — its formats are `ro-crate` and `prov`, which carry their own schemas.

**Additive on a payload that has shipped.** Nothing is renamed, reordered or removed; one key is
added at the front of each object, so a consumer parsing 0.6.0's output keeps working. `verify`'s
key is added by a `payload()` wrapper rather than inside `verify()` itself, because the dict that
function returns is an in-process result other code reads — including `report`, which has a
schema of its own — and only the published shape needs to say which shape it is.

### Changed — `runprov chain --format json`: two changes to a payload that had already shipped

Audit I, I-24, and the row is about the CHANGELOG rather than about the code. `chain --format
json` shipped in 0.6.0 on 2026-09-23. **Three** commits changed its output afterwards and none
said so here — the only released output this project has modified without an entry, which is
exactly what a changelog exists to stop.

**CORRECTED 2026-09-30 by Audit J (J-27): this entry said TWO, and its own enumeration was the
stale list.** The one it missed is the earliest and the only one that changed a VALUE rather than a
shape: **`7354671` (2026-09-24) moved `edges[].wrote` from `null` to a version string** on every
`UNCHAINED` edge, and on `UNCLAIMED` edges. Measured with the tagged module loaded by path:
`v0.6.0` gives `wrote = None` on all six edges of the 0.5.0 corpus history, HEAD gives `'0.5.0'` —
**18 value changes across the cross-version corpus**, on the 0.3.0, 0.4.0 and 0.5.0 histories,
type `NoneType -> str`.

`7354671`'s commit message disclosed it; its CHANGELOG entry did not, and closed *"a genuine
pre-chain history reads exactly as it did"* — the three genuine pre-chain corpus histories are
precisely the ones whose JSON moved. **No schema bump is needed**: `Link.wrote` is declared
`str | None` and documented as *"the runprov that wrote it, when the line says so"*, so 0.6.0 was
under-populating against its own stated meaning and HEAD populates it correctly. The defect was the
silence, and this row existed to end exactly that.

**And the lesson is about this row's own shape.** I-24 enumerated the changes as a hand-written
list and it went stale in six days — the failure mode this project's entries call out for
hand-written FIELD lists, now met for a list of commits. Audit J found the same shape four times in
two days: a list of fields, of states, of guards, and of commits.

**Key order moved** (`abeff47`, 2026-09-24). The payload was hand-written in `__main__.py` and
is now derived from the `Report` structure, so the keys come out in the structure's order:

```
0.6.0    schema, path, status, lines, attested, chained_from, translated, merged, unreadable, edges
now      schema, path, lines, edges, chained_from, translated, merged, unreadable, status, attested
```

**The key SET is identical** — measured, nothing added and nothing removed by that commit — and
a JSON object is unordered, so no conforming consumer can notice. It is recorded anyway: a
reader comparing two files byte for byte sees a difference, and "you should not have depended on
that" is a thing to be able to look up rather than to be told.

**`edges[].could_chain` added** (`a0a48ab`). A boolean on every edge, saying whether that line
could have carried a `prev` claim at all. Without it the page's diagnosis reached no consumer:
two histories differing only in a version string print opposite pages — *"written before the
chain existed"* against *"they do NOT predate the chain"* — and produced payloads identical but
for `edges[].wrote`, because the threshold that separates them is private, in no document, and
`null` on every 0.1.0 record. **Additive**: nothing renamed, reordered or removed, so a consumer
parsing 0.6.0's output keeps working.

Both are `runprov.chain.v1` still, and correctly: R-11 versions the MEANING of a field, and no
field changed meaning. The README now documents this command, which it did not before — see
I-23.

## [0.6.0] — 2026-09-23

### Added

* **`runprov chain` — has the run history been edited since it was written?** (T-32, ADR-0016.)
  Every history line now carries `prev`, the sha256 of the line before it, so an edit — or a
  deletion or reordering anywhere but the very end — breaks the link that vouches for it.
  Nothing detected that before: change line 40 and `log`, `show`, `diff`, `impact` and
  `report` all repeat the new value with no sign anything moved.

  **The newest line is attested by nothing until the next run appends**, because a line cannot
  contain its own digest, and for the same reason a truncation of the tail cannot be seen from
  the file alone. The report says both, every time, rather than printing a coverage figure it
  cannot support.

  **Tamper-evident, not tamper-proof, and the command says so.** Each digest is public, so
  whoever can edit the file can also append a forged line or rewrite from a point and re-chain.
  What this catches is retroactive editing by someone who did not re-chain — the realistic case,
  where a number looks wrong and someone opens the history in an editor to fix a "typo".

  **Checkable with `sha256sum` and nothing else.** That is why `prev` is a plain top-level
  string and why the digest is over the line's bytes as written rather than any canonical form.
  The nine-line shell recipe is in the ADR and in the suite: it agrees with the package, and it
  reports an edit to line 3 as line 4 breaking — the line *after* the one that changed, which
  is the counter-intuitive part the report states outright.

  A history written before the chain is **anchored by the next append**, not left behind: one
  run is enough to make editing the last pre-chain line detectable. The report always states
  how many links it checked against how many lines exist, because "INTACT" over a file whose
  chain covers three of nine hundred lines would be the vacuous pass this project keeps fixing.
  A torn or corrupt line is `COULD NOT CHECK`, never an accusation.

  **The verdict is per EDGE, not per line, and that is what makes it trustworthy.** A line
  carries two separate facts — *is my claim about my predecessor correct*, and *are my own bytes
  attested by my successor* — and every wrong answer this feature produced came from conflating
  them. The unit is the adjacent pair, each pair resolved through a decision table over four
  enumerated inputs, and the file's verdict is the worst edge and nothing else. **Measured: flip
  any single byte the report says is attested and the chain never stays silent — 2 152 positions
  over four shapes (intact, torn mid-write, mixed-version, written before the chain existed),
  every one of the 255 possible substitutions at each, 548 760 cases, zero escapes.**
  `attested` is a count of edges that hold, so it can never exceed the lines it had.

  **That sentence used to read "3 192 cases … zero escapes" and it was not true, which is worth
  recording rather than quietly restating.** The gate that produced it skipped every `\n` byte,
  and the one escape that existed was at a `\n`: overwrite the terminator between two attested
  lines and the two records merge into a single unreadable line while the verdict stayed
  `INTACT` and the exit code stayed 0 — both runs gone from `log`, `show` and `report`, every
  byte of both still on disk. Two of its four shapes were also already non-`INTACT` before any
  flip, so nothing could escape them and the assertion held for free. **A destroyed record
  terminator is now `COULD NOT CHECK`, exit 2, named on both renderings** — and the page says
  *this is not evidence of an edit*, because what the two merged records used to say cannot be
  read back, and because an `fsck` zero-filling a block it cannot recover joins two lines
  exactly as a hand edit would. What the report states is the narrow claim it can stand behind:
  a truncating crash cannot produce this. An honest crash is untouched and still costs a
  project nothing.

  Three consequences a reader will meet. A CRLF translation is a **per-line** fact, so one stray
  carriage return from a `core.autocrlf=true` checkout no longer discards every finding in the
  file. The version that wrote a line is resolved from the **run**, not the line, because no
  released version writes a `tool` block into a start line — half of every history. And a line
  whose predecessor is unreadable answers `COULD NOT CHECK` rather than `BROKEN`: a tear that
  happened later cannot be told from an edit, and saying so is the honest answer.

  **A line that made no chain claim is reported `UNCLAIMED`, never as tampering.** It is
  decisive — exit 2, so it can never be read as a clean bill — and it is named on the report
  with the causes it cannot tell apart: an append that could not take the file lock writes no
  claim by design, a run still in flight has not written its completion record yet, and so
  would a line inserted by hand. Both innocent cases produce files in which every record is
  present and every byte is as written, and nothing in this package can clear a finding, so
  calling them a break made the only remedy *editing the history*. What this gives up is
  stated rather than hidden: a line appended at the very end by someone who did not compute
  `prev` is now exit 2 instead of exit 1. A splice anywhere else still breaks the chain at the
  next link, and no history that was `BROKEN` becomes `INTACT`.

  **`chain --format json` — the payload, named here because nothing else names it.** It is the
  package's only output carrying a `schema` key, `runprov.chain.v1`, and its shape changed
  twice while this entry was being written — gaining `merged` and then `unreadable` — with no
  published description of any of it. Nothing breaks: the command is unreleased and ADR-0017
  R-11 states the payload is not a stable API on its first release. But a versioned string with
  nothing written under it invites one round of "v1 meant something else last month", so:

  | key | what it is |
  |---|---|
  | `schema` | `runprov.chain.v1` |
  | `path` | the history read, always POSIX-spelled so a Windows record reads the same |
  | `status` | `INTACT`, `BROKEN` or `CANNOT_CHECK` — the file's verdict, the worst edge |
  | `lines` | lines in the file |
  | `attested` | edges that `HOLDS`; never negative, never more than `lines` |
  | `chained_from` | the first line carrying a claim, or `null` if none does |
  | `translated` | how many lines have a CRLF-translated terminator |
  | `merged` | lines whose record boundary was destroyed, so two records read as one |
  | `unreadable` | lines that carry no readable record |
  | `edges` | one object per edge: `line`, `status`, `claimed`, `computed`, `wrote` |

  **`edges` carries every edge, not a selection.** It replaced four hand-picked buckets, and
  three mutations of those survived the suite because the only test of them used a clean file
  where every bucket was empty. An edge's `status` is one of `HOLDS`, `HOLDS_TRIVIAL`,
  `UNCHAINED`, `GAP`, `UNCHECKABLE` or `UNCLAIMED`; `claimed` is the digest that line claims
  for its predecessor and `computed` the digest of the predecessor's bytes as they sit, so a
  reader can run `sha256sum` against the two without parsing a sentence. `status` and the exit
  code say the same thing: **0** `INTACT`, **1** `BROKEN`, **2** `CANNOT_CHECK`. A missing
  history still emits a well-formed payload — `"lines": 0`, `"edges": []`, exit 2.


### Fixed

* **The source distribution no longer carries the path of the machine that built it.**
  `tests/corpus/` holds records from real runs of every released version, and 40 of those files
  named this repository's absolute path in `command`, `argv[0]` and `code.script_file` —
  `_normalise` rewrote the corpus tree's own root but never the scenario script's, which lives
  outside the tree it normalises. One test also carried the build host's name in a sample of
  expected output. **The wheel was never affected**: it contains package code and nothing else.

  **0.5.0's sdist on PyPI carries both and cannot be replaced** — 32 files with the path, one
  with the hostname. Nothing else leaked: no username, no credentials, no data.

  The scanner that should have caught it matched a hand-written list of prefixes — `/home/`,
  `/Users/`, `/tmp/` and three more — and this repository lives under `/mnt/`. It now asks
  whether a path is **absolute at all**, which is a property of the string rather than a guess
  about where people keep their code. A second check sweeps every tracked file for the
  repository's own resolved path and the running host's name, both read off the machine at test
  time, because both of these leaks sat outside the corpus trees the first scanner walks.

## [0.5.0] — 2026-09-17

### Added

* **`runprov impact <file>` — what was derived from these bytes, and in what order it rebuilds
  (T-31, ADR-0015).** `verify` answers that per artifact but only for artifacts you already
  thought to name; `lineage` holds the DAG and walks it backwards. This is the forward
  direction, reported with depth because the answer is an **order** — a set of nine filenames
  does not say which to rebuild first.

  **It answers what *did* derive, never what *will* break**, and that gap is the whole of its
  honesty: a script that never imported runprov, a read that bypassed registration, a pruned
  history. **An empty result reads "no recorded run read these bytes", never "nothing depends
  on this"** — one is a fact about the history, the other a claim about the world, and the
  second would be a green light to overwrite a reference. The blind spots print on every
  answer, not only the empty one.

  Connectivity comes from the single walk `lineage` already makes: the digest rule (index on
  `content_sha256`, `sha256` **and** `sha256_tree`, because preferring one and falling back
  compares two different keys and invents orphans) is not copied — `_lineage` fills two
  out-parameters during the passes it already makes, the convention `bad` and `scripts`
  already follow.

* **`runprov diff` — why is today different from last month (T-30, ADR-0014).** The question
  asked most often, and answering it used to mean opening two records side by side. Everything
  needed was already recorded, including the step argument digests ADR-0010 built expressly so
  that *"did this function see the same inputs?"* would be answerable.

  **A difference and an incomparability are not the same answer, and that is the whole design.**
  A run on 3.11 could not see what a run on 3.12 saw; a dirty tree's commit does not name the
  code that ran; a `getrusage` peak and a cgroup peak are different quantities. A diff that does
  not know this reports a change of *interpreter* as five functions appearing. So comparability
  is decided **per dimension**, with three verdicts rather than two, and `unchanged` always
  states what it was examined over.

  **Incomplete is not incomparable either** — a run with an unregistered read can still report
  an input that moved; what it can never support is the word `unchanged`. Exit 0 means
  comparable *and* identical in every dimension.

  `runprov diff align` compares the last two runs of a script; two addresses compare two runs.
  It refuses to compare a run with itself, and never compares a run with its own in-flight
  start marker — which the first version did, reporting that the schema, the packages and the
  observation block all differed, because one of the two was not a finished run.

### Added

* **A cross-version record corpus — the suite can now read records it did not write.** Every
  other test in this project builds a record with the same code that reads it, which leaves one
  failure structurally invisible: **a record written by last year's version that this year's
  version reads differently, or refuses.** `tests/corpus/` closes it. Each released wheel is
  installed from PyPI, one fixed scenario is run under it, and the resulting tree is kept.

  **It is a measurement, not a fixture somebody typed.** The artifacts and the environment
  snapshot are copied byte for byte and never edited — the artifact carries the pin whose body
  digest `verify` re-derives, and the snapshot's filename *is* the digest of its body, so a
  corpus that had to be rewritten to be moved would be measuring the rewriter. Only the record
  files are normalised, and only to replace an absolute root; measured first, not assumed —
  with relative paths the sole absolute values reaching a record are `cwd` and `command`.

  **Verified by reinstating the defect it was built for.** Putting Audit C's C-03 sort key back
  turns the directory-pinning artifact STALE in all four captured versions at once. Seven more
  mutations — the pin's body field, the history schema, the steps shape, `content_digest`, the
  manifest, one artifact byte, and an edit to the scenario itself — are all caught. One of them
  found a real hole while it was being written: `verify` reporting OK does not prove the body
  was *checked*, so `body_checked` is now asserted beside the verdict.

  `data/refs` is a directory whose subdirectory names are chosen rather than illustrative:
  `panel/`, `panel.old/` and `panel-v2/` sort in **opposite** orders under a string key and a
  parts key, because `/` is 0x2f while `.` is 0x2e and `-` is 0x2d. Flat filenames sort
  identically under both, so a corpus built from those would have *looked* like it covered
  C-03 and would not.

  The version list is derived from the directory, the command list from the parser, and a
  released version that is never captured fails the suite one release later — because this
  project's record on remembered rules is seven misses for the scope pattern and three for
  `README-pypi.md`, so the harness may not depend on one.

### Fixed — three smaller ones from the same pass

* **An archived lock file was reused by NAME, not by content.** The name is derived from the
  digest, so an existing file at it is not evidence about its bytes: a torn write from before
  atomic writes leaves a prefix under a name claiming a digest it does not have, and every
  later run sees the name, records `reused: true`, and never rewrites it. The record then
  asserts a `sha256` that does not describe the file it points at. Same defect and same remedy
  as the environment snapshot, one function along, applied there and not here.

* **`runprov report`'s digest fallback counted distinct PATHS, not distinct RUNS.** Two runs
  writing byte-identical bytes to the same recorded path — every re-run of a deterministic
  pipeline — collapsed to one, so the ambiguity guard passed and the newest run was named for
  an artifact found somewhere else. And one run that wrote identical bytes to two paths counted
  as two, so a moved artifact a single run plainly produced was reported NOT FOUND. Wrong in
  both directions from one key; the question is "which run", so runs are counted.

* **`runprov impact` called a sum of per-run counts a floor on paths.** Each run records the
  distinct paths its watch dropped; adding those across runs gives drop EVENTS, and a hundred
  runs dropping the same 2 000 system paths printed "at least 200 000 path(s)". The number is
  fine and the sentence about it was not — the same overstatement removed per-run, arriving
  through the reader added to fix it.

### Fixed — `runprov report` says when the bytes are not the bytes that run recorded

* **A page could pair one run's identity with another run's inputs, and report OK.** The
  path-matching branch of `find_run` assigned unconditionally, so when the named record's own
  recorded digest for that path contradicted the bytes on disk, it had proof the record did not
  describe them and returned it anyway — discarding an unambiguous digest match. The run block
  came from that record and the "inputs it was made from" block from the artifact's own pin,
  i.e. from a different run. Three commands gave three answers for one file: `report` named
  v1, `verify` named v2, `show --stale` said MODIFIED. Reached by publish-by-copy (a staged
  result copied to a stable name) or by restoring a good copy over a bad run.

  **The path match stays the named run and the disagreement is now printed**: *this run
  recorded X for this path; the file now hashes Y; those bytes are the output Z recorded by run
  W*. Both digests are recorded facts and the comparison is the one `show --stale` already
  performs — nothing is inferred. Two alternatives were prototyped and are worse: preferring
  the digest names a run that never touched the path when an interrupted rewrite leaves an
  empty file colliding with some other empty output, and returning no run strips the producer
  from every ALTERED page, which is where "who wrote this" matters most.

  The exit code does not move. `report` exits on `verify`'s verdict, and `verify` is right —
  the pin is internally consistent and its inputs still hash correctly. What is wrong is which
  history record attached to the file, which is a property of the history. The precedent is the
  UNREGISTERED block, the loudest line on the page, which has never moved the exit code either.

### Fixed — `runprov diff` compares the code that ran, not only the commit

* **A changed analysis script passed the gate when the commit did not move.** `git status`
  reports neither a gitignored module nor a script outside the repository, so `git_code_dirty`
  was False, the commits matched, and `code` reported `unchanged` — while the same history line
  carried a different `imported_code.digest`. `run.code()` on an out-of-repo script is the
  documented purpose of that API, and the README says of that digest, in as many words, that it
  answers *did any first-party code change between these two runs*. ADR-0014's own precondition
  table already named `imported_code.omitted`; the implementation was handed the field and did
  not look at it. Reproduced end to end: edit a `run.code()` script between two runs of a clean
  tree and the command exited 0. It exits 1 and names both digests.

  **Exactly one side carrying a digest blocks rather than compares.** `imported_code` entered
  the history without a `HISTORY_SCHEMA` bump, so two records legitimately share
  `runprov.history.v2` and disagree about whether the field exists — and `hash_imported_code`
  can be off on one machine and on elsewhere. Comparing there would report a code change for
  every pair straddling that date. Neither side carrying one is agreement, not a refusal.

  **A truncated digest is a note, not a block.** Past `imported_code_max` the digest covers the
  kept prefix, which the page now states; blocking on it would stop any project with more than
  200 first-party modules from ever exiting 0. `configure(imported_code_max=…)` is there for
  anyone who wants strictness.

### Fixed — cost can fail the gate again, because the noise model was the defect

* **`runprov diff` could never fail on `resources` — including when the two figures were not
  the same quantity.** A `getrusage` peak compared against a cgroup peak printed `NOT
  COMPARABLE — different quantities` and exited **0**, which ADR-0014 clause 4 forbids and
  which that ADR's rejected-alternatives section refuses by name.

  The cause was three attempts at the wrong problem. Every pair of runs differs in cost, so
  letting cost decide the exit code made a gate that could never pass; the fix reached for was
  to declare the whole dimension non-decisive, and that swallowed incomparability along with
  noise. **The defect was the noise model.** Measured over twelve identical runs on one
  machine: `wall_seconds` spreads 111% but only 0.035 s, while `max_rss_bytes` spreads 1%.
  **Time noise is absolute; memory noise is relative** — so a relative band alone could never
  absorb sub-second wall jitter, which is why it kept failing and kept being worked around.

  A difference is now material only when it clears **both** a 5% band and an absolute floor
  (0.5 s, 0.5 s, 8 MiB — roughly 14x and 33x the measured noise). With that, cost settles on
  exactly the same terms as every other dimension: jitter produces no difference at all, a real
  regression exits 1, and an incomparable pair exits non-zero. Nothing is exempt, and ADR-0014
  is amended with the measurement rather than with an exception.

  Two runs that **both** predate the `resources` block are now comparable and agree — they
  measured nothing, in the same sense in which two runs that both declared no steps agree.
  Blocking on absence would have made every history written by 0.1.0–0.3.0 permanently
  non-zero, which is the gate-that-cannot-pass arriving a third time.

* **A zero-step comparison keeps its truncation reason.** `steps` reports `0 vs 0` both for two
  runs that declared none and for a run whose observation was cut off, and the branch that
  distinguishes them could drop that reason with the suite green — `unchanged`, settled, gate
  passed over a census both runs knew was partial.

### Fixed — `runprov diff` says when a run did not finish

* **A run that CRASHED compared `unchanged` against one that succeeded, and exited 0.**
  `compare()` had seven dimensions and none of them read `status` or `failure`, though the
  history carries both deliberately. A script that writes its table and then raises — a
  post-processing step blowing up after the output is already on disk — records identical
  inputs, outputs, parameters, packages and commit to its predecessor, so every dimension
  reported `unchanged` while the traceback sat in the same history line the diff had just
  read, and `runprov log` printed `FAILED RuntimeError: …` from that very line.

  **A bare inequality would not have fixed it:** when both runs crash identically the statuses
  agree, so `runprov diff <script>` reported a clean green comparison **forever**. The new
  `status` dimension therefore emits a per-side evidence line — `B failed: RuntimeError: …` —
  whenever either side did not finish, and `render` marks the header lines `[failed]` so the
  fact is visible at a glance where `show` already puts it.

  An absent `status`, or a `running` one from a mid-run sidecar, BLOCKS rather than compares:
  a run that has not finished cannot be said to match or differ. Poisoning every dimension
  when a run failed was prototyped and rejected — `code`, `parameters` and `packages` are
  recorded at start and fully known for a crashed run, and blanking them would contradict
  ADR-0014's own amendment that incomplete is not incomparable.

### Changed — one recorded digest moves, and only on Windows

* **`sha256_tree` for a directory input pinned by 0.1.0–0.4.0 ON WINDOWS has moved. POSIX
  records are unchanged.** This is the one exception to this project's standing rule that the
  tree hash does not move, and it is written here because the previous two attempts at this
  were not.

  `PurePath.__lt__` compares a **case-folded** key on Windows and only there, so the released
  versions hashed `README.md` and `data/` in one order on Windows and another on Linux — the
  same tree, two digests, which is a digest that cannot answer the only question it is asked.
  Fixing that necessarily moves one platform: **there is no sort key that equals both the
  folded and the unfolded order.** Measured over a real project tree, 69 of 339 directories
  (20%) order differently under the two, which is any tree mixing a capitalised and a
  lowercase entry.

  **`runprov verify` recognises the old order and says so.** A directory pinned that way
  reports `STALE` with the reason *"this matches the tree digest a pre-0.5.0 run recorded ON
  WINDOWS, where the order was case-folded; the contents are unchanged — re-pin"*. Still
  STALE, deliberately: the pin no longer identifies the tree under the digest this version
  computes and re-pinning is required, so `OK` would be a green over a record that needs
  action. A genuinely changed directory still gets a bare `STALE` with no excuse attached.

  The check is not weakened by the fallback. The hashed stream is `name\0digest\0` per file
  with fixed-width digests, so it determines the **ordered** (name, digest) list — a stream
  matching in folded order has the same names and the same file digests as the pinned tree,
  i.e. it is that tree. Both digests come from one walk, so a 40 GB reference directory is not
  re-read to answer a question about its order.

### Fixed

* **Two audits over the same code, 32 distinct defects, and the second was aimed at the
  first's repairs.** Audit B put 30 read-only reviewers over the package and found 21; four
  MORE were then introduced by the fixes themselves, caught only by tests written before them.
  So Audit C put 30 more over the diff of the repairs, each told what a change *claimed* to do:
  11 further defects, three high. The rate is the finding worth recording — a fix is as likely
  to be wrong as the code it fixes, and only a reader who did not write it has caught either.

  **`sha256_tree` moved and nothing said so.** A fix meant to stop the directory digest
  depending on the machine changed it on Linux and macOS instead: `PurePath.__lt__` compares
  part-wise, the sort it replaced compared the rendered string, and `/` competes with `.` and
  `-` there. Every directory input pinned before it verified STALE with nothing on disk
  touched. The digest is back where it was, pinned by its literal value in the suite.

  **Three commands could never exit 0.** `diff` marked `steps` NOT COMPARABLE for every pair
  of history records (the history stores a count, the sidecar stores the list), and smoothed
  every resource figure except `max_rss_bytes` — the one that actually jitters between two
  identical runs. `impact --depth 0` truncated its walk to nothing and exited 0, the code that
  means *checked, and nothing is wrong*, so a pre-overwrite guard went green over a file three
  artifacts derive from. It exits 2 — COULD NOT CHECK — which is what a truncated walk is.

  **`runprov report` named the wrong run.** Matching on content before path meant a
  byte-identical file written later somewhere else did not merely win, it destroyed a correct
  exact-path match. Path wins; a digest match decides only when it is unique.

  **A checkpointed sidecar was left saying `running` on a run that succeeded**, with a
  phantom double-stamped twin beside it holding the real record — because the rule "do not
  re-stamp a name already stamped" was written as a list of one name, and a later change
  routed a second name through it. The exclusion is derived now. That is the third time this
  package has shipped the same shape of defect, and the rule is in the ledger.

  **Bounds that announce themselves, and readers that listen.** The audit-hook watch counted
  open *events* rather than distinct paths, so one reference file read a thousand times past
  the cap recorded a thousand lost files — an overstatement in the permanent record. It counts
  distinct paths, saturating, and says "at least". The field it writes was read by nothing:
  `diff`, `report` and `impact` all still judged completeness from `unregistered_reads` alone,
  which is EMPTY when the watch went blind. All three consult it now, and `diff` refuses
  `unchanged` over a census either run knew was partial.

  Also: drive-RELATIVE names (`C:data/x`) escaped the project root on Windows where
  drive-qualified ones no longer did; a pinned artifact's temporary file sat at the umask
  default for the whole write block, so a result restricted to 0600 was group-readable for as
  long as the run took; and an environment snapshot written by a pre-fix version on Windows
  was reported `reused: true` forever, its recorded sha256 not describing its own bytes.

  Every fix carries a regression test, and each was mutation-checked in a copied tree as a
  positive control: twelve mutations, twelve caught.

## [0.4.0] — 2026-09-16

### Added

* **`resources` — what a run actually consumed, and `runprov resources` to render it (T-29,
  ADR-0013).** To put a pipeline on a cluster you must declare `--mem` and `--time` before you
  have run it there, and `#SBATCH --mem=64G` is a statement of BELIEF — a hand-maintained log
  in a different domain. Every record now carries the measurement; the command renders it as
  Snakemake benchmark columns, a Slurm preamble, or Kubernetes `requests`/`limits`.

  **It is a floor, and it says so.** Slurm and Kubernetes enforce against the **cgroup** —
  every process concurrently, plus page cache — while `RUSAGE_CHILDREN` is the high-water mark
  of the largest **single** child: measured, three children holding ~150 MiB at once report
  162 MiB, not 450. A figure pasted into `--mem=` without that caveat gets the job OOM-killed.
  When the run owns a cgroup (Slurm step, container) it reads `memory.peak`, which **is** the
  enforced quantity, and the record names which mechanism answered. Outside one it refuses the
  ambient group: on a workstation that is the desktop session, 8 138 MiB.

  Units are canonical in the record — bytes and seconds — because the two targets disagree in
  ways that corrupt a number silently: Kubernetes memory in `M` rather than `Mi` is a 4.8%
  error that reads like a typo, and its CPU is a **rate** in millicores, not a core count.

  **The specification is checked, not remembered.** ADR-0013 numbers sixteen requirements and
  `test_every_resource_requirement_has_a_test` derives that list from the ADR, failing if any
  has no test citing it.

## [0.3.0] — 2026-09-16

### Added

* **`runprov report <artifact>` — one artifact, one page, for a quality file.** The question
  an accreditation assessor asks is not the one `log`, `show` or `verify` are shaped for:
  *for this reported result, show me which data and which version of the method produced it,
  and show me the record could not have drifted.* This joins those three about ONE file, on a
  page that can be printed and filed beside the result. It exits on the verdict, so a quality
  gate can call it.

  A **derived view and nothing more** — every fact is read from the artifact's own pin and the
  run history, and no field exists that `show` and `verify` cannot also produce. **The limits
  are printed on the page**, not left in a manual: what it cannot tell you is not incidental,
  and a quality document that overstates is worse than none because it is the one that gets
  cited.

* **`show` now names the runprov that wrote each run**, completing U-01. The record gained the
  field; no view showed it, so only a reader who already suspected something would find it.
  This also required the `tool` block to travel into the history, which is a whitelist
  projection — a field not named there never reaches `show` or `log`, and "which runs were
  made by which version" is a question about the project over time.

* **`runprov check` — the checker this README promised and this package did not ship (T-27,
  ADR-0011).** It reads source, so it answers the case `watch.py` states it can never see: a
  script that never imports `runprov`, whose code therefore never runs. Exit 1 on a finding so
  it can gate a build, and exit 1 on a file it could not parse, because that file was not
  checked. It never imports or executes your code, so it works on a pipeline that has never
  heard of runprov.

  **Three rules, each forced by a measurement on real pipelines rather than chosen.** Scope is
  derived (without excluding this package, the first prototype flagged seven of runprov's own
  modules). The subject is an **entry point**, found from `if __name__ == "__main__"` rather
  than a path convention — 59 flagged files became 30, because a library function that opens a
  file is called *by* analysis code and is not analysis code. And reaching runprov is resolved
  **transitively** through the project's own modules: 30 became **2**, because a platform
  adopts a library by wrapping it — one real repository has 3 files importing runprov and 96
  reaching it through a single internal `provenance.py`.

### Fixed

* **`observation.steps` never reported `declared+observed`, the value it was introduced for
  (T-26).** It was assigned the literal `"declared"` inside `_add_step` and nothing else ever
  wrote it, so a run with one `@run.step` and five observed calls summarised as `"declared"` —
  and a run with observed calls and no decorated ones reported `"none"`, saying nothing was
  seen inside a script that was watched from start to finish. Nothing was lost, because
  `observed` and `auto_mode` both held the truth; what was wrong is that the one field meant
  to carry the declared-versus-observed distinction was the one field that did not.

  Found by smoke-testing the **published** 0.2.0 wheel, not by the suite. The value is now
  **derived at seal time** from `bool(record["steps"])` and `bool(record["observed"])` rather
  than assigned by whichever code path happened to run, and the enum gains the fourth value
  ADR-0010 never named: `none` | `declared` | `observed` | `declared+observed`.

### Added

* **Every record now names the runprov that wrote it, and says whether that name identifies
  the code (U-01).** A new top-level `tool` block, present with nothing configured:

  ```json
  "tool": {"name": "runprov", "version": "0.2.0", "source": "index", "identifies_code": true}
  ```

  **The package was failing its own thesis, and a consumer found it rather than a reviewer.**
  ~1 000 records said `runprov: 0.1.0` — one string covering every commit the package had —
  so an artifact could not say what produced it. Measured while fixing it, and worse than
  reported: that much appeared only because they had set `tracked_packages=("runprov",)`.
  `packages` is `()` by default, so the ordinary record did not name this package at all.

  `source` is derived from **PEP 610**: no `direct_url.json` means an index install, and a
  PyPI filename is never reused, so name + version is exact; `vcs_info.commit_id` gives the
  commit for a git install; anything else is `local` and identifies nothing. A live checkout
  is asked of git directly and **wins over the metadata**, which goes stale — this
  repository's own `direct_url.json` still names a drive mount that no longer exists. A dirty
  checkout records its commit and `identifies_code: false`, because the files that ran match
  no commit that exists.

  Adding a field needs no schema bump; the format promise already says so.

## [0.2.0] — 2026-09-15

### Added

* **`progress` — the run narrates itself, configured once instead of in every script.** Each
  registered read and each artifact written, with elapsed time, a path relative to the project
  root, and the same short digest the pin carries:

  ```
  [00:00] read  data/m1.tsv  1541e29a8301ba21
  [00:01] wrote out.tsv      c533232884b32c60
  ```

  It answers both questions a long run is asked — *what has it done* and *is it still going* —
  from events runprov already observes, so no script configures anything.

  **On when stderr is a terminal, off otherwise**, because a pipe, a file or a job runner's
  log has nobody watching and the lines are somebody else's noise. `configure(progress="on")`
  and `"off"` force it, `RUNPROV_QUIET` silences it like every other routine message, and it
  never touches stdout — that channel belongs to the caller's data. Capped at 200 lines per
  run, and it says once when the cap bites.

* **A heartbeat, for the silence between events.** After `configure(heartbeat=…)` seconds
  with nothing happening, a narrated run says it is still there and names what it last did:

  ```
  [00:02] still running — last: read data/m1.tsv
  ```

  **Silence, not a metronome:** every registered read, write or step resets it, so a run
  producing events steadily never beats at all. `heartbeat=0` disables it and builds no
  thread; the count reaches the record as `observation.heartbeats`.

  The thread is a daemon *and* stopped explicitly — a non-daemon thread keeps a finished
  process alive, a daemon one killed at shutdown can raise from inside a module being torn
  down. It is stopped at the top of `__exit__`, **before** the record is assembled: signals
  reach the main thread only, so on a `SIGTERM` it would otherwise go on printing "still
  running" while the run unwound. stderr is now serialised by a lock, re-created after
  `os.fork()` — a child inheriting a lock held by a thread that no longer exists deadlocks on
  its first message.

* **Automatic observation is bounded, which is what makes it safe as a default.** Measured on
  call-bound code against `auto_steps="off"`: `census` **6.4×**, `arguments` **21×**, and
  unbounded, because every call in the process pays for the callback whether or not it is in
  scope. The observer now turns itself off after **50 000 calls** and records
  `observation.auto_stopped_after_calls`. A run of 1 040 000 calls pays 268 ms once and then
  runs at full speed, where the uncapped cost extrapolates to about six seconds.

* **`@run.step` — function-level provenance (T-25, ADR-0010).** A digest says a *file*
  changed; this says an *argument* changed, which is the difference inside a script that
  `verify` cannot see because `verify`'s subject is the artifact. Records the digests of what
  a decorated function received and returned, including calls that raised.

  **Declared, not observed, and that is the design.** noWorkflow captures more by
  instrumenting the abstract syntax tree, at the cost of changing the program it observes;
  this package has a test section headed *provenance must not change the program it observes*
  and states that trade in `WHY.md`. The decorator is the same argument as `run.input()`: it
  is in the code, so a reviewer sees it in the diff and it cannot be bypassed by launching
  differently.

* **A digest rule for Python values that refuses rather than guesses.** Type-tagged canonical
  forms for scalars and containers of them — `json.dumps` renders `True`, `1` and `1.0`
  identically and would call a changed argument unchanged — `__runprov_digest__` for anything
  that offers it, and `UNDIGESTIBLE:<type>` for everything else. **Never a `repr`**, which is
  unstable across runs and would be recorded as though it were not; **never a pickle**, which
  is irreproducible and would put executable bytes inside a provenance record.

* **An `observation` block in every record.** Names what the run was ABLE to observe, present
  whether or not the feature is used. Without it a record with no steps cannot be told apart
  from a record made where steps could not be observed, and a reader comparing two runs on two
  interpreters concludes "nothing changed inside the script" when the truth is "nothing was
  looked at" — this package's own defect class, arriving through the feature meant to catch
  it. `auto_available` is the field that carries that distinction; `packages_recorded` does
  the same for `"packages": {}`, which until now could not be told from nobody asking.

* **Steps are capped at 1000 per run, and the cap says so.** `observation.steps_truncated`
  counts what was dropped. A truncated record that does not announce the truncation is the
  same defect one level down.

* **Automatic call observation on Python 3.12+ (ADR-0010 stage two).** `sys.monitoring`
  counts calls into the project's own code, with no decorator and no opt-in: on an
  interpreter that can do it the default is `census`, below it the default is `off`, and
  `observation.auto_mode` says which — the capability decides and the record states it.

  **Scope, not a cap, is what makes it usable.** Measured: reading 500 lines of TSV with the
  standard library produces **1 505 Python calls, 1 504 of them inside `csv.py`**. A cap of a
  thousand fills on those and stops before recording one function the author wrote. Filtering
  to code under the project root gives **4**, all theirs. Compiler-generated frames —
  `<genexpr>`, `<lambda>` — are excluded by a property rather than a list of names.

  **`configure(auto_steps="arguments")`** additionally digests the distinct argument sets each
  function saw: four hundred calls with two distinct inputs record two signatures, so *"did
  this function see the same inputs as last time?"* is a comparison of two small sets. It
  costs a frame read per call, which is why it is asked for rather than assumed.

  An observed entry is a **count**, a declared one a **digest** — structurally different,
  because "the interpreter noticed this" and "the author said this matters" are different
  claims and the difference belongs in the data. A refused tool slot (`coverage` holds one) is
  recorded as `auto_refused`, never as an empty record.

### Tooling

* **`ruff` 0.16.2 → 0.16.6, `mypy` 2.3.0 → 2.3.1, `build` 1.5.0 → 1.6.0.** Applied by hand
  across all seven pin sites rather than by merging Dependabot's pull requests, which edit
  `pyproject.toml` alone: Dependabot does not read workflow `run:` lines, and the
  `.pre-commit-config.yaml` pin is a `rev:` it cannot match either. Nothing new was reported
  by either tool — `ruff check`, `ruff format --check` and `mypy --strict` all clean at the
  new versions.

Nothing else yet. `release_check` refuses a `v*` tag while this heading says `[Unreleased]`, so
dating it is part of cutting a release rather than something to remember separately.

## [0.1.0] — 2026-09-11

The first published release. Everything below is what it contains.

The release path was rehearsed end to end against TestPyPI before this tag existed
(run `34598547070`): built, tested on Python 3.10–3.13, published by Trusted Publishing,
then installed into a clean environment from the index and exercised — `verify` returning
`OK`, then `STALE` naming the input that changed. That rehearsal found and fixed a missing
coverage exemption that would have failed this very tag at 99.72% and published nothing.

### The three properties it exists for

* **Registration is the ergonomic path.** `run.input(p)` returns the path, so the natural
  way to open a file is the recorded way and a skipped read is a visible omission.
* **The pin lives in the artifact**, not only in a sidecar.
* **The pin is deterministic** — no timestamp, no run id, so two identical runs over
  identical inputs do not report every artifact as changed.

### Added

- `Run`, `Project`/`configure`, `describe`, `sha256`, `content_digest`, and an append-only
  JSONL history with `flock` (and a `msvcrt` path on Windows).
- `python -m runprov log | lineage | verify`, and a `runprov` console script — `uvx runprov`
  and `pipx run runprov` resolve the second and cannot reach the first.
- **`show`** — the notebook the history already contained. `show` with no argument is the
  PROJECT page: every script, the inputs it expects (with a digest per distinct version it
  has read), the outputs it writes, its parameter and note keys, and an index of every
  artifact on record with the run that produced it. `show <target>` is one page per run,
  where the target may be a script name, a `run_uid` prefix, a `run_id` or an artifact path.
  Text or `--format yaml`. **It writes nothing** — a reader over `runs.jsonl`, asserted by a
  test that compares every byte on disk before and after.
- **`runprov exec -- <command>`** — a subprocess recorded as a run, for pipelines driven
  from a Makefile, a Snakefile or a shell script where there is no Python to hold a
  `run.tool()` call. Records the resolved tool and its version, the argv, declared inputs
  and outputs (hashed), and the exit code; **returns the command's own exit code** so it
  composes without changing what failure means. A non-zero exit is recorded as a failed run,
  and a missing program is recorded rather than raised. `--capture` tees the command's
  output at file-descriptor level.
- **`run.tool(name)` and `run.code(path)`** — the work that is not Python. `tool()` records
  which binary resolved, its `sha256`, and the version it reports (stdout *or* stderr, since
  `samtools --version` uses the latter and exits non-zero); it is bounded by a timeout,
  never raises, and records `found: false` rather than omitting an absent tool. `code()`
  registers an R script, shell wrapper or Snakefile that `sys.modules` can never see, and
  hashes it into the SAME code digest as the Python — so "did any code change" is one
  comparison across languages. The history line carries `tools` as `name → version`.
- **Every run hashes the project's own modules that it imported.** `git_commit` identifies
  the code only when the tree is clean and `script_sha256` pins the entry point alone, so a
  run whose result changed because a helper module changed had no trace of it. Measured:
  editing `src/utils/stats.py` moves the imported-code digest while `script_sha256` stays
  put. Read at exit so lazy imports count; scoped under the project root, with virtualenvs
  and build trees inside the root excluded. The history line carries one digest and a count
  (the per-file list is in the sidecar, since the history is appended forever);
  `hash_imported_code=False` turns it off and `imported_code_max` caps it.
- **A documented `note()` convention** for the questions a digest cannot answer. A digest
  says an artifact changed; it cannot say a column appeared, and runprov will not open your
  files to find out. `run.note("columns", list(df.columns))` makes "when did that column
  arrive" a dated answer from `runprov show <script>`. Keys worth standardising — `columns`,
  `n_rows`, `dtypes`, `model`/`temperature`/`prompt_sha256`, `tool_version` — are listed in
  the README, and `examples/summarise.py` now records its own schema.
- **`Project(sidecar_per_run=True)`** — a sidecar per run instead of one the next run
  overwrites: `summary.prov.json` becomes `summary.<utc>.<run_uid8>.prov.json`. Sortable by
  name because the time comes first, unique because the run_uid follows, and the stamp goes
  before the WHOLE compound suffix so `*.prov.json` still matches — inserting before `.json`
  alone silently breaks that glob. Default off, so a caller who names a path still gets it.
  It also makes `show --stale` answerable for older runs.
- **`show --stale` / `--rehash`** — a staleness column on the artifact index, answering
  "do I need to run this again" in one page. Computed from the HISTORY rather than the
  in-artifact pin, so it works for binaries that cannot hold one. `current` / `STALE` (an
  input moved) / `MODIFIED` (the artifact itself changed) / `GONE` / `?`. Off by default;
  `--stale` is one `stat` per input and reads no input bytes, taking the sizes and mtimes
  from the producing run's sidecar and reporting `?` when that sidecar is absent or has
  been overwritten by a later run.
- **`verify`** — re-derives every input a pin names and compares. Until it existed,
  invalidation was a property of the format and not of the product: everything needed was
  in the artifact and nothing read it back. Reads the artifact and nothing else — no
  history, no sidecar, no `configure()`. Transitive through inherited pins, measured on a
  two-step chain: changing the root reports **2 stale artifacts**, and `via` names the step
  whose claim failed rather than the artifact's own. `GONE` is counted apart from `STALE`
  (a stale artifact is rebuilt; a missing input is found), and zero pins found is a
  **non-zero exit** rather than a green check over nothing. The pin-digest precedence is
  one function shared with `header()`, so the checker cannot disagree with what wrote it.
- `log --format yaml`, and `runprov.to_yaml()` for a per-run manifest, both from one
  renderer and **without pyyaml**.
- **Terminal capture** (`terminal_log`), tee-never-divert, at file-descriptor level so a
  subprocess's output is seen; falls back to Python level and says so in the record.
- **Environment snapshots**, content-addressed as `env-<sha16>.txt`, plus `conda-meta`
  packages, environment-manager detection with its evidence, and lock files hashed and
  archived.
- **Failure recording.** `with Run(..., provenance=PROV)` records `status: "failed"` with
  the exception type, message and traceback tail, and every registered-but-unproduced output
  as `MISSING`.
- **First-run warning noise.** "Not a git repository" and "this repository's `git status`
  failed" printed the same four-line alarm. The first is how many people work and is true
  of every run they will ever make — repeating an alarm for a condition the reader cannot
  act on is the permanently-red check this package refuses elsewhere. It is now one note,
  once per process; a repository whose git actually failed stays loud on every run. The
  dirty-file list is capped at 10 with the remainder counted. **No record changes.**
- **Nothing is tracked until the project asks.** `tracked_packages` defaulted to this
  project's own stack, so a run touching none of it recorded
  `{"numpy": null, "pandas": null, "scipy": null, "sklearn": null}` on every history line,
  forever. Every null was truthful and nobody had asked — a field populated by assumption
  rather than observation, which is the failure the package exists to replace. There is no
  domain-neutral list; the universal facts (interpreter, platform, environment manager,
  lock files) were never in it and are still unconditional. `configure(tracked_packages=…)`
  is one line, and `run.module(mod)` is the sharper tool for what this is usually reached
  for.
- **`verify` no longer mistakes documentation for an artifact.** The anchor was matched
  anywhere in the first 64 KiB, so a bare `runprov verify` in a project with a virtualenv
  reported this package's own `verify.py` and `run.py`, their `.pyc` files, and the wheel
  METADATA — and METADATA embeds the README's *example* pin, so it invented a `GONE` for
  `data/labels.tsv`, a path that exists only in prose. A pinned artifact declares itself at
  the top; a file that merely mentions the format does not. Build, VCS and virtualenv
  directories are no longer walked, and the count of files skipped is **reported** rather
  than silently applied. Measured on one demo project: 898 files and 5 false findings →
  18 files, 1 artifact, 1 OK.
- **`examples/format_compatibility.py`** — writes an artifact in each of 60 formats through
  runprov and reads it back with that format's real library, reporting pin placement, parse,
  hash and digest stability. Skips (loudly) any format whose library is absent, so it is not
  in the test suite. Measured with every optional library installed: **60 round-tripped,
  0 failed, 0 skipped**. Without them it says so rather than passing quietly — on a bare
  install it reports `18 format(s) round-tripped, 0 failed, 42 skipped for a missing
  library`. Including Pickle, joblib,
  cloudpickle, Parquet, Feather, HDF5, AnnData `.h5ad`, Zarr, NetCDF, `.xlsx`, BAM, CRAM,
  bgzipped VCF, R `.rds`, ONNX, safetensors, PyTorch `.pt`, `.npy`, `.npz`, `.mat`, PNG, TIFF, gzip and
  SQLite. It also
  documents two honest limits: gzip is byte-unstable but content-stable (which is what
  `content_digest` is for), and SciPy `.mat` plus CRAM move on every run — `.mat` writes
  `Created on: <date>` into its header, and CRAM differs across two writes of identical
  records with an identical reference path.
- **A complete, runnable example.** `examples/summarise.py` is standard-library only and
  is executed by the suite, so it cannot quietly stop working — the README had **zero**
  whole scripts in 41 KB, every one a fragment with undefined names.
- **`runprov --version`.** It previously exited 2 with "the following arguments are
  required: cmd", which is the first thing a bug report asks for.
- **The quickstart no longer configures the CLI into failure.** It set
  `run_log=<root>/reports/runs.jsonl`, which is precisely what makes a bare
  `runprov log` report that nothing has been recorded; the default
  `<root>/provenance/runs.jsonl` is where the CLI looks.
- `CODE_OF_CONDUCT.md`, issue and pull-request templates.
- **Constructing a `Run` no longer imports the packages it tracks.** `_versions` did
  `__import__(mod).__version__`, so building a provenance object imported numpy, pandas,
  scipy and sklearn whether or not the script used them — and numpy/MKL fix their
  thread-pool configuration at import time, so the run was measurably different because it
  was traced. Versions are now read from `sys.modules` (what the script actually holds)
  then from distribution metadata, via `packages_distributions()` so `sklearn` still
  resolves through `scikit-learn`. Measured on one `Run()` with all four installed and none
  imported: **RSS +135 MB → +3 MB**, and the recorded versions are identical. A tracked
  package that is neither imported nor installed now records `None` rather than being
  imported to find out.
- **A symlinked directory inside the root pins as repository data, not as `<external>/`.**
  `_pin_name` resolved every link, so `data/ -> /mnt/bigdisk/data` — the standard layout,
  and every Nextflow/Snakemake work directory, which stages inputs as symlinks — announced
  real repository data as foreign, and `verify` then called it `UNVERIFIABLE` because
  `<external>/` is deliberately not a path. Those inputs were unpinnable *and* uncheckable.
  The spelled form is tried first and is also the more stable one: `/mnt/bigdisk/…` is this
  machine's mount layout, `data/…` is what the repository looks like everywhere. `resolve()`
  is kept as the second attempt, for a root reached through a link (macOS `/tmp`, cluster
  homes); a path containing `..` skips the first attempt, since `link/../x` normalises to
  the parent of the link and means the parent of its target. **This changes pin text for
  symlinked layouts**, which is why it lands before the first release rather than after.
- **A symlink loop no longer kills the run describing it.** `resolve()` raises
  `RuntimeError` on a loop, which is not an `OSError` and was caught by nothing: it escaped
  `_pin_name`, escaped `header()`, and took the run with it. Now pinned as external, which
  is what "we could not place this under the root" means.
- **`content_digest` blocks are bounded by bytes, not only by line count.** "Streamed" was
  true only of files whose *lines* are short, so the shape that defeated it was not a big
  file but a file with few big lines — an unwrapped FASTA, a minified JSON, a one-line
  dump. Peak RSS for one call: **607 MB → 43 MB** on 8,192 contigs of ~30 kb (246 MB file);
  unchanged on ordinary short-line text. Verified across 26 file shapes that **no digest
  moves** — a moved digest would re-pin every artifact at once. A single line longer than
  the block is still read whole, which is stated rather than implied.
- **Termination recording.** SIGTERM and SIGHUP raise `Terminated` (a `BaseException`, so a
  broad `except Exception:` cannot swallow one) and take the same path — so SLURM's time
  limit, `scancel` and `docker stop` leave a record instead of nothing. Verified against a
  real signalled process. Never replaces a handler the caller installed, and does not arm
  outside the main thread; the record states which, per signal. **SIGKILL and SIGSTOP
  cannot be caught by any program** and still leave nothing, which is stated rather than
  worked around.
- **`open_output` refuses formats a `#` pin would corrupt** (`PIN_UNSAFE`). Newick is why:
  a pinned tree *parses*, and Biopython 1.85 read a 3-taxon tree back with **6 terminals**,
  three of them harvested from the pin's own prose. FASTQ, FASTA, VCF, SAM and the binary
  formats are refused for reasons recorded per suffix — VCF and SAM reject the pin **even
  with the format's own marker**. `output()` remains the way to record an artifact that
  cannot hold a pin. **`.svg`, `.xml`, `.html`, `.json`, `.jsonl`, `.geojson`, `.ipynb`
  and `.tex` were added after instrumenting a real matplotlib script**: they are TEXT,
  so `open_output` wrote them happily and the damage only appeared when a parser
  touched it (`ET.parse` at line 1 column 1; `json.loads` at char 0). `.json` had been
  in the suite's list of formats a pin CAN go into, so a passing test was holding the
  corruption in place.
- **In-band pinning is an ALLOWLIST.** It was a denylist, so a format nobody had thought of
  got a `#` written into it — and three rounds of review each found another: Newick, then
  SVG and JSON, then pickle. Each fix added a row and left the default intact. Now only
  suffixes known to take a `#` comment are pinned in-band and everything else gets a
  sidecar, so an unrecognised format gets the safe outcome. `.py`/`.sh` are excluded on
  purpose: a pin above a shebang stops the file being executable. `.sql` and `.tex` pin
  in-band when the caller names their real marker (`-- `, `% `).
- **A sidecar pin for every format that cannot hold one in-band.** `open_output` no longer
  refuses a text format: it writes the artifact byte-exact and puts the pin in
  `<artifact>.prov.txt`, registered and hashed. Verified with the real tools — `samtools
  faidx` indexes the FASTA, Biopython reads it, `pd.read_json(lines=True)` sees 2 rows not
  3. `run.pin_sidecar(p)` does the same for a file another library wrote (a figure, a BAM).
  Two measured opt-ins: `comment="; "` for FASTA (Biopython reads it, `samtools faidx`
  rejects it — stated on stderr as the trade is made) and `run.output_json()`, which embeds
  the pin as a top-level key. **JSONL gets no opt-in**: a leading provenance line makes
  pandas read 3 rows for a 2-row file, which is the Newick failure again.

### Fixed for long histories

- **Reading the history streamed instead of slurped.** `_load` did
  `read_text().splitlines()` — the whole file as one string *and* a list of every line,
  before one record was parsed. Measured on a 100,000-run, 91 MB history: **488 MB → 392 MB**
  just from streaming the read.
- **`log` streams too, in every format.** Each record is written as it passes and dropped;
  `--limit N` keeps a deque of N and nothing else. Measured on the 91 MB history:
  **1.2 s, 24 MB**, the same at any length. The YAML banner moved to the command so it is
  emitted once rather than per record. `lineage` still materialises, because a graph
  joining outputs to inputs across the whole history has nothing to stream past.
- **`show` no longer materialises the history at all.** It consumes a stream and counts as
  it goes, so the project page costs **1.6 s and 33 MB** on that same 91 MB history instead
  of holding 392 MB. `--stale` takes a second pass over the file rather than a second copy
  in memory. Guarded by ratio tests: 4x the runs must not cost 4x the memory.

### Tested

- **NFS / Lustre.** The degraded path is tested unconditionally by forcing `flock` to raise
  `ENOLCK`: every record lands, the downgrade is announced, and a torn line costs one record
  rather than the file. The REAL test is opt-in and pointed at your mount with
  `RUNPROV_NETWORK_FS_DIR`, appending from 8 separate processes; a second one reports
  whether `flock` works there at all. Skipped by name when the variable is unset.
- **Performance regressions, as ratios rather than thresholds.** History append does not
  slow as the file grows; `project_view` is linear in run count; `read_pins` does not read
  past `SCAN_BYTES`; `content_digest` peak memory is flat against both line count and line
  length; `Run()` construction does not scale with the history. An absolute number is a test
  of the machine that set it — this suite learned that when `peak < size / 4` passed locally
  and failed in CI by 2%.

### Fixed, with what each was measured to be

- **A filename could forge a pin entry.** A crafted name containing a newline wrote an extra
  line into the `# provenance` block embedded in an artifact. Control characters, lone
  surrogates and undecodable bytes are escaped; `café` is left alone.
- **An undecodable filename killed the caller's write** — `UnicodeEncodeError` from inside
  provenance capture, so the artifact was never created.
- **Two terminal captures stopped out of order destroyed the process's stdout.** File
  descriptors 1 and 2 are process-global; restoring them in the wrong order reinstalled a
  pipe whose reader had gone, and every later write in the process vanished.
- **A `chdir` mid-run recorded a digest its own path did not name** — the record named a
  file that did not exist and carried the hash of a different one.
- **An input replaced after registration was undetectable**, so a run could pin `sha256:
  abc…` and finish beside a file that no longer had those bytes.
- **`.xlsx`, `.zip` and `.tar` rebuilt from unchanged data hashed differently every time.**
  Measured across fourteen formats: csv, tsv, txt, json, parquet, feather, pickle, npy,
  npz, sqlite and gz — eleven — were already stable; the three archives were not. Detection is by content,
  so `.docx`, `.odt`, `.whl` and `.npz` are covered too.
- **`lineage` lost every edge crossing the v1/v2 schema boundary**, reporting a real
  producer as "produced by no recorded run". Tested against records generated by the v1 code
  itself.
- **A `numpy.int64` was recorded as the string `"6"`.** It subclasses neither `int` nor
  `bool`; `numpy.float64` survived because it subclasses `float`.
- **A `Run` built and never entered kept capturing for the life of the process**, with every
  line the program printed afterwards accumulating in its log.
- **The published artifact was the one nothing verified.** The release workflow built with a
  bare `python -m build`; the full check ran only on pushes to `main`, never on a tag.
- The pin used the platform path separator (found by Windows CI); a diagnostic could kill
  the run it described; a corrupt gzip escaped `content_digest`; and a `.gz` rewritten from
  identical bytes never hashed the same twice.

### The defaults follow one rule, and it is written down

- **`Project`'s docstring now states the rule the defaults follow: record everything
  observed, guess nothing.** A review read `hash_imported_code=True` against
  `DEFAULT_TRACKED=()` as a contradiction — one doing work nobody asked for while the other
  argued "record nothing until asked". The axis is not more against less, it is OBSERVED
  against GUESSED: the modules that were imported are a fact about the run, and a package
  list is a guess about somebody's domain. The old `tracked_packages` default recorded
  `{"numpy": null, "pandas": null, …}` on every line of every history — four truthful
  answers to a question nobody posed.
- **The cost the review measured was not where it said.** It reported `hash_imported_code`
  at a 4.9x exit cost and read that as the price of hashing. Measured on 86 modules with 40
  under the root: the walk alone **17.97 ms**, hashing all 41 files **0.43 ms**. 96% of it
  was asking the filesystem at every exit whether each stdlib and site-packages module lives
  under the project root — a question whose answer cannot change for a module already
  imported.
- Two repairs: the verdict is **memoised** per `(root, __file__)`, and `os.path.realpath` +
  `startswith` replaces `Path.resolve()` + `relative_to`, because `relative_to` signals
  "not under the root" by RAISING and that is the common case (~150 exceptions built and
  thrown to compute ~150 no's). End to end: **55.3 ms → 12.2 ms** for a one-run process, and
  **1.6 ms** for every run after the first.
- **`runprov exec` follows `sidecar_per_run` instead of contradicting it.** It hardcoded
  `{name}_{run_id}.json` — per-run naming reached by a second route — while the library
  default overwrites. Two entry points, the same decision, opposite answers, and a project
  that had chosen one got the other depending on which door it came through.

### An unrequested file is announced, and the fourteen methods have families

- **`open_output` created a second file and said nothing.** Where the format cannot hold a
  comment — JSON, FASTA, JSONL, Newick, SVG and 18 others — the pin goes BESIDE the artifact
  as `<name>.prov.txt`, which is correct and was silent: a caller who wrote one `.json`
  found two files in their results directory. The surprise arrives twice, because the
  sidecar is also what `verify` reports as the pinned artifact. It now says which file was
  created, that `verify` will name it, and — for JSON — that `output_json()` keeps it to one
  file. A NOTE, not a warning: nothing is wrong, and it is the extra FILE that has to be
  visible. The in-band TRADE a few lines away already announced itself; the larger of the
  two surprises did not.
- **`Run`'s docstring now groups its fourteen methods into REGISTER / WRITE / ANNOTATE /
  FINISH.** They were listed alphabetically and nowhere else, so the rule for choosing among
  four ways of registering one artifact was invisible at the call site: `open_output` and
  `output_json` both write a JSON result correctly, produce provenance of different shapes,
  and only one of them can be checked from the artifact alone. The boundary that matters is
  the last one — `write()` is NOT in the WRITE family, because those write YOUR DATA and it
  writes THE RECORD. A test asserts every method has a line and that this boundary holds,
  since prose rots and a method added without one is a method with no stated family.

### A run that is killed outright is on record

Reported by the first real consumer: *anything runprov writes only at the end is lost by
every interrupted long job, and the artifacts on disk look identical to a successful run's.*

- **Measured before anything was built.** `SIGINT`, `SIGTERM` and `SIGHUP` were already
  covered — one history line each — because the signal handler raises and `__exit__` runs.
  `SIGKILL` was not: artifact on disk, **zero** history lines, no sidecar. So the gap was
  SIGKILL-class only: the OOM killer, `kill -9`, a power loss, a node failure. For an
  8-hour job the OOM killer is the likeliest ending there is.
- **A `runprov.start.v1` line is appended when the block is entered**, so the record of a
  run exists before the run can be killed. A start whose `run_uid` never gets a matching
  record is the finding, and it is permanent — the history is append-only. This is the
  "unknown denominator" the package criticises its predecessor for, closed for the one
  ending that runs no code.
- **`<history>/.incomplete/<run_uid>.json`** is written beside it and deleted at exit: an
  index of what is unfinished *now*, which the history cannot answer because it does not
  know what is alive. `show` is driven by the history and enriched by the markers, so the
  finding survives deleting the directory.
- **A marker is not a death certificate.** It exists for the whole of every run, so
  `RUNNING` is the ordinary state and not a finding; `INTERRUPTED` is; and a marker from
  another host reads `?` rather than being guessed at, since `os.kill(pid, 0)` there would
  answer about whichever local process holds that number.
- Every reader drops the `started` lines — `_load` and `_counted` for `show`/`lineage`, and
  `log` separately because it streams raw — so nothing counts a completed run twice. The
  YAML view declines them too: it is a narrative of completed runs.
- **The public surface is written down**, in `docs/public-surface.txt`, and a test holds the
  package to it. Reported from downstream: an upgrade broke a project, and a survey of this
  history for `feat!` and `BREAKING` found nothing — the three commits responsible were typed
  `refactor:`. Two withdrew ten names from `__all__`; the third renamed `Run.write_json` to
  `Run.output_json`, which `__all__` cannot see at all, because a promised class carries its
  methods. **A convention that records intent cannot see a breakage the author did not
  intend**, so the check is on the surface rather than on the commit message: removing or
  renaming anything forces an edit to that file, in the diff, where it can be noticed.
- **The CLI has one exit-code contract**: `0` checked and nothing wrong, `1` checked and
  something is wrong, `2` could not check or the invocation did not describe one. `1` used to
  mean "no match", "stale artifacts", "nothing was checked" and "no history file" in
  different commands, so a CI job could not tell a failing gate from a gate that never ran.
  `verify`'s no-pins case and every missing-history case move from `1` to `2`, and
  `log --script X` with no match moves from `0` to `1`, matching `show <target>`.
- **A run with no `provenance=` writes neither — and no marker either.** That shape records
  nothing by design, and a start line there would mean constructing a `Run` created a
  history file. The marker had no such guard: an unarmed run created `<history>/.incomplete/`
  and wrote into it, so a REPL experiment left a directory behind and, killed, left a
  permanent marker that made one `runprov show` print "1 run(s) STARTED with no ending
  recorded" directly above "Nothing has been recorded here yet". The marker directory indexes
  RECORDED runs that have no ending yet; a run with no record has no ending to be missing.
- **An input registered after the pin was rendered is now REFUSED.** The pin lives in the
  artifact's first bytes and cannot grow a line, so a later registration leaves the artifact
  understating what it was made from while its own `runprov verify` reads OK for ever — and
  the person holding only that artifact has no way to learn otherwise. Recording it is all
  that is possible afterwards; refusing is the only thing that prevents it. The refusal is
  recorded as `refused_late_inputs` BEFORE it is raised, so catching the error does not erase
  it. `Project(allow_late_inputs=True)` restores the warning for the one shape this forbids:
  an input whose path is not knowable until something already-open has been read.
- **A pin written under `allow_late_inputs` declares its own scope**, as a `pin_covers` field
  in the artifact's own bytes. That is the only thing that reaches someone holding just the
  artifact — no history, no sidecar — and it works at any file size because it is written
  when the pin is rendered rather than appended afterwards. `verify` notes it per artifact and
  counts it on the summary line, without failing: the inputs the pin does list are verified.
  Default projects are unchanged byte for byte.
- **`verify` reads any `name : value` field in a pin**, not the three it was told about. The
  reader's whole job is to read what another version wrote, and a closed list made a field the
  writer added invisible.
- **An input registered after the pin was rendered is now IN THE RECORD**, as
  `inputs_not_in_pin`, in the sidecar and the history and omitted when empty. It used to
  print a stderr warning and record nothing, so the sidecar listed N inputs, the artifact's
  pin listed M < N, and no field anywhere marked the divergence — which is the opposite of
  the rule `unregistered_reads` follows for the same class of problem. The warning also
  claimed "nothing downstream can detect it"; `show --stale` detects it and always did, and
  the warning now says which command can and which cannot.
- **A checkpoint no longer claims the run succeeded.** `run.write(PROV)` inside the block is
  the only way to get inputs, outputs and notes onto disk before a SIGKILL — `__exit__` is
  where they are persisted and SIGKILL never reaches it — so the README recommends the call.
  It used to leave `"status": "ok"` and a `finished_utc`: measured, a job checkpointed at
  hour 7 and then killed had a sidecar claiming success sitting beside the artifact, while
  the history said it never ended. A checkpoint now records `"status": "running"` and
  `"finished_utc": null`, and the ending corrects it to `ok` or `failed`.
- Sealing the record moved from `write()` to `_finish`, because one exit branch writes no
  file at all — when the caller has already written the constructor's path themselves — and
  a status stamped inside `write()` was never stamped there. Sealing is an act of ENDING a
  run, not of writing a file.
- **The README states what a SIGKILL does NOT recover**: everything registered during the
  run, unless it was checkpointed. Plus two smaller limits — liveness is same-host only and
  pids get reused, and a kill in the microseconds before the first line records nothing.

### You can see the lines a reader skipped

- **`log --unreadable`** prints the lines that will not parse, with their line numbers, and
  stops. The count was already there — `3 unreadable line(s) skipped` — and was actionable
  only as a number: it says something is wrong in a file that may hold 100,000 lines, and
  nothing about where. Control characters are escaped, because the reason a line will not
  parse is often that something wrote bytes into it and printing those raw hands the
  terminal whatever corrupted the file. Bounded at 200 characters, with what was cut stated.
- **It reads, and there is no repair command.** JSONL loses the bad line and counts it,
  which is the whole reason for the format, so damage costs exactly the damaged lines. A
  command that rewrote `runs.jsonl` would contradict the claim the package is built on and
  add a new way to lose data. The corruptible file is the YAML view, and regenerating it
  from the record of truth is already the heal story.
- **Exit 0 even when it finds something.** `log` already returns 1 for "no history at that
  path", and a second meaning on that code is a decision of its own, not a side effect of
  this one.
- Refuses `--limit`, `--script`, `--run-id`, `--failed` and `--format yaml|jsonl` with exit
  2: those select and render RECORDS, and a line that will not parse has none.

### `__all__` is 17 names, down from 27

- **Five more withdrawn on 2026-08-20**, for a different reason from the first five. Those
  were mechanism, or documentation rendered into a message, or defaults `Project` already
  supplies. These are names **nobody was ever told to call**: `installed_packages`,
  `write_snapshot`, `Capture`, `MemorySink` and `git` each had ZERO references in README,
  GETTING-STARTED, WHY and every ADR — checked, not assumed — while the features they belong
  to are documented entirely as configuration and record fields (`Project(env_snapshot_dir=…)`,
  `terminal_log`, `sink=`).
- `git` in particular: 6 internal call sites, no documented caller, and a bare `git` in an
  importing namespace collides with GitPython's top-level module. Its contract is *swallow
  every exception, return None, 20s timeout* — provenance capture, not a general-purpose
  runner. ADR-0003 left this open because a module cannot decide a package promise;
  withdrawing it closes the question without touching a single call site.
- All ten withdrawn names still exist on their own modules: `runprov.project.git`,
  `runprov.terminal.Capture`, `runprov.sinks.MemorySink`, and so on. **Withdrawn, not
  deleted**, and a test pins that.

### Every module now declares its public surface

- **Eleven of twelve modules declared no `__all__`**, so 101 top-level names were importable
  but unpromised and would have been frozen BY USE rather than by decision at the first
  upload — the same defect `__all__` itself had one level up. The rule adopted, recorded as
  **ADR-0003**: a module's `__all__` **ratifies** the package's promise and never makes one.
  A name belongs in a module list if and only if `__init__.py` promises it, with exactly one
  other source of publicness — `[project.scripts]`, which binds `runprov.__main__:main`.
  `_report.py` declares nothing: the underscore in the module name is already the statement.
  A test asserts both directions, since a missing name and an invented one are different
  mistakes.
- **Five renames landed first**, because a name recorded in two surfaces is twice the work
  to change: `environment.render` → `_render_snapshot` and `verify.render` →
  `render_report` (two different functions sharing a word, and neither raised on the other's
  input — a `verify` report rendered as a plausible environment snapshot);
  `verify.ANCHOR` → `hashing.PIN_ANCHOR` (the one sentence identifying a pin, written as a
  literal in the writer and held as a separate constant in the reader); and the deletion of
  `show.SHORT` (a second spelling of `PIN_DIGEST_CHARS`, with a test that only asserted the
  two agreed) and `run.SERIALISATION_ERRORS` (dead — a promise about an `except` clause that
  no longer exists).

### One vocabulary for both checkers

- **`show`'s states are now `verify`'s states**, imported rather than restated: `current`
  became `OK` and `?` became `UNVERIFIABLE`. They were the same five ideas spelled two ways
  — plus a case split nobody chose, one lowercase word among four uppercase ones — so the
  two commands disagreed in print about artifacts they agreed about in fact. `verify` owns
  the four shared definitions, `show` imports them and adds `MODIFIED` (only the history
  holds the artifact's own digest), `verify` keeps `NO PIN` (only the bytes can be missing
  one). There is no second spelling left to drift.
- The artifact-index column is now DERIVED from the vocabulary rather than written as a
  number. It was a hand-written 9, which fitted `MODIFIED` and not `UNVERIFIABLE` — and
  since a `:<` field pads but never truncates, the long state did not merely misalign the
  column, it ran INTO the digest: `UNVERIFIABLEa4c3ed04a95a3da1`. Widening it to 12 by hand
  would have left the same defect, one character narrower; `STATE_COLUMN` is the longest
  state plus one, so the next state added widens the column with it.
- **`--format yaml`'s `state:` values change with it**, which is the one thing here a
  machine consumer would notice. Nothing has been published, so nothing is broken.
- Two docstring errors fixed in the same pass. `staleness` described `MODIFIED` as
  "(rehash only)" — it is reachable from the STAT path too, which the code four screens
  below has always done and which is now measured in the test suite.

### The two staleness checkers now compose into a gate

- **`show --stale --exit-code`.** The package shipped two commands that answer "is this
  result still good?", and **the one that finds more problems was the one that exits 0**.
  Measured on a throwaway project: with the input changed, `verify` said STALE and exited 1
  while `show --stale` said STALE and exited 0; with the input restored and the RESULT
  hand-edited, `verify` said `OK` and exited 0 while `show --rehash` said MODIFIED — and
  exited 0. And over a BAM, which cannot carry a pin, `verify` could say nothing at all.

  The considered fix was to merge them into one engine. It was designed, reviewed by five
  independent adversarial lenses, and **abandoned on the evidence**: a pin lists the run's
  INPUTS, so the artifact's own digest is not in it and cannot be — the pin lives inside the
  file it would describe. An engine merging the two could not close the tampering case,
  because the evidence `verify` reads does not contain the answer. The two commands share
  about 12 executable lines of ~400; the rest is genuinely different work.

  So the fix is composition, and the README now documents it as the gate:

      python -m runprov verify results/ && \
        python -m runprov show --stale --rehash --exit-code

  `?` does not fail the gate — it means the check could not be made, and failing on it would
  make any project with one directory input permanently red. Two shapes are refused with
  exit 2 so exit 1 keeps one meaning: no `--stale`/`--rehash` (nothing to gate on), and a
  `show <target>` argument (which already exits 1 for "nothing matched"). `verify`'s exit
  codes are unchanged, and the exit-code VOCABULARY question is a separate open decision.

### Fixed before it could gate

- **`show --rehash` called an artifact MODIFIED that it had never digested.** `_short`
  returns `-` for an entry with no digest — right to print, and it was being compared
  against. Today's digest is not `-`, so anything the run recorded as `kind: UNHASHABLE`
  (a FIFO, a socket, a device) came back MODIFIED once the path became readable. The line
  read `MODIFIED -  pipe.out  [UNHASHABLE]`: a definite finding, the absent digest and the
  reason it is absent, contradicting each other on one line. The same comparison ran on
  INPUTS, where it reported STALE. Both now report `?` with the reason, which is what
  `verify` has always said for the same condition. Harmless only for as long as `show`
  exits 0 — which is about to change.

### Named before anyone depended on it

Nothing here is a rename to a user, because there are no users yet. It is written down
because these were the last names to settle and the reasoning belongs with the release:

- `run.write_json()` is **`run.output_json()`**. It shared a verb with `write()`, which
  writes the provenance record rather than your data; `output`, `open_output` and
  `output_json` now all mean "your data" and `write` alone means "the record".
- `show.to_yaml()` is **`show.render_yaml()`**, so there is one obvious name and not two
  reachable functions with the same one.
- `--format timeline` is **`--format text`** on every subcommand, spelled the same way
  everywhere. `timeline` still works and renders the identical bytes; it is simply not
  advertised.
- `__all__` is **22 names, down from 27**. `VOLATILE`, `VOLATILE_JSON`, `PIN_UNSAFE`,
  `default_run_id` and `default_generation` left the public surface — the first three are
  mechanism, the last two are defaults `Project` already supplies. All five still exist on
  their own modules (`runprov.run.PIN_UNSAFE`, and so on); they are no longer promises.

### The record in somebody else's vocabulary

- **`runprov export`** emits **RO-Crate 1.1** (JSON-LD over schema.org — what Zenodo and
  WorkflowHub ingest) and **W3C PROV-JSON** (entities, activities, agents), over the **whole
  history** or over **one run from its sidecar alone**. The second scope is the case the
  in-band pin exists for: somebody holding a file and its sidecar, with no history to read.
- **It writes nothing this package owns.** `runs.jsonl`, the sidecars, the YAML twins and
  `transformation_log.yml` are untouched and remain the record of truth; a test asserts every
  byte in the project is identical before and after an export.
- **PROV-JSON rather than PROV-O in Turtle**, because those want an RDF library and this
  package has no dependencies. Both formats are plain JSON.
- **One limit, stated rather than glossed:** PROV's `wasDerivedFrom` here means "this output
  was produced by a run that read this input", not "this value came from that value". PROV
  allows the finer claim and this package cannot make it. Over-claiming in a standard
  vocabulary would be harder to catch than over-claiming in our own, because it would be
  well-formed. **ADR-0009.**

### Recording a script nobody changed

- **`runprov capture script.py`** runs an unmodified script — no `import runprov`, no
  `run.input`, nothing — and records every data file the interpreter saw it read or write.
  In-process via `runpy`, under the audit hook this package already installed and had been
  using only to print a warning. A file the run wrote is an **output** even if it also read
  it; the audit event has carried the mode all along (PEP 578 passes `(path, mode, flags)`)
  and this hook read only the first of the three.
- **It is a rung below `run.input()`, not a replacement,** and the README says so: an observed
  record lists what was opened, not what mattered, and nothing in it is pinned into an
  artifact. The records are the same format, so nothing is wasted at the migration.
- `runprov exec` remains the sibling for non-Python steps: a subprocess has its own
  interpreter and its own hooks, so it can record a command and never see inside it.
  **ADR-0008.**

### An artifact answers for itself

- **`verify` can now say "this file has not been edited since it was written."** It could
  only ever say the artifact's INPUTS were unchanged, and it printed that caveat every time
  it passed. A test existed whose premise was the gap — it appended a fabricated row, asserted
  a passing exit code, and called that *the trap*. **That test now asserts the opposite:
  same fixture, same tampering, `ALTERED` and exit 1.**
- **`ALTERED` is its own state, not a flavour of `STALE`,** because they are opposite
  repairs: a stale artifact is rebuilt, an altered one was edited by somebody and rebuilding
  it destroys the edit.
- **The artifact is published once, complete.** The body streams into a temporary file and is
  hashed as it goes; the pin's `body` field is patched there and one rename publishes it, so
  the file never exists carrying a digest that is wrong. ADR-0005's rule applied to the
  artifact. Measured against the obvious alternative — writing the body then copying it under
  a finished header — that would have cost **1.7×** on a 210 MB artifact (0.76 s against
  0.45 s) and bought nothing.
- **An artifact with no `body` digest is not a finding.** Everything written before this, and
  everything produced any other way, reports "cannot tell" — and the summary says **how many
  could be asked at all**, because `0 ALTERED` over files carrying no digest says nothing and
  reads exactly like `nothing was tampered with`.
- **Cost, stated:** an artifact does not exist at its final path until its handle is closed.
  A script that writes and re-reads an artifact *inside* the same `with` block will not find
  it; after the block, nothing changes. `_seal` publishes anything a caller left open, so
  forgetting `close()` costs the pin's accuracy at worst, never the file. **ADR-0006.**

### A gate other projects can adopt

- **`.pre-commit-hooks.yaml` and `action.yml`.** The exit-code contract has been right since
  the CLI gained it and unreachable without somebody remembering to type the command. Two
  pre-commit hooks and a one-line GitHub Action make a stale or altered result fail a build.
  Both pass the exit code through rather than collapsing it: 1 is a finding about the data,
  2 is "could not check", and they need different repairs.

### A record is written whole or not at all

- **Every provenance write is atomic.** The sidecar, its YAML twin, the in-flight marker, the
  pinned JSON artifact and the environment snapshot were each a plain `Path.write_text`, which
  truncates the destination and then fills it. Measured with `tools/torture.py`, tearing each
  write at 0%, 50% and 90%: **every site left a prefix on
  disk** — 1 345 of 2 691 bytes of a sidecar, 109 of 219 of a marker. (A later review found
  the census had reported "six of six" while enumerating only what one two-step pipeline
  reached: an eighth site, `archive_lockfiles`, spells its write `Path.write_bytes` and was
  invisible to both the guard and the harness. It is atomic now, and both instruments cover
  `write_bytes`. See ADR-0005's correction note.) Worse than a damaged new
  record, the crash destroyed the whole OLD one. They go through `runprov/_atomic.py` now —
  temp beside the destination, `fsync`, `os.replace`, `fsync` the directory — so a crash at any
  instant leaves either the previous record or the new one. **ADR-0005.** `sinks.py` was
  already correct and is unchanged.
- **The readers were already right about torn files**, and this is the number that says so:
  `verify`, `show`, `log`, `lineage` and `prune` were run over ~90 torn trees and 63
  byte-mutation cases — ~600 invocations — and every one stayed inside the exit-code contract
  with no traceback. Nothing was broken; what was missing was that a crash could take the
  record that was already there.
- **`verify` counts and reports write debris.** A crash between the temporary file and the
  rename leaves `.<name>.<uid>.runprov-tmp`. It is not read as an artifact, and it is not
  silently skipped either: it is the only visible trace that a run died mid-write.

### The Windows leg, run for the first time in three weeks

The hosted matrix had not run since 2026-08-12, a hundred commits earlier. When it came back
the Windows leg **aborted at 66% with exit 15 and no pytest summary**, and had been doing so
unreadably for as long as it had been failing. Three findings came out of it.

- **`os.kill(pid, 0)` IS NOT A LIVENESS CHECK ON WINDOWS, and `show` was using it as one.**
  `signal.CTRL_C_EVENT` is 0 and CPython special-cases it, so that call is
  `GenerateConsoleCtrlEvent(CTRL_C_EVENT, pid)` — **it sends a Ctrl-C to a console process
  group** and returns success. Two consequences, and the first is the serious one: `runprov
  show`, reading a marker left by a job that died last week, interrupted whatever live
  process now held that number. And since it never raised, every dead run was reported
  RUNNING — `INTERRUPTED`, the finding the whole page exists for, was unreachable on
  Windows. Measured on the runner: it raised for nothing, and it killed two rounds of the
  probe sent to measure it. `show` now asks `OpenProcess` + a zero-timeout wait, and signals
  no one. An unexpected error is reported `?` rather than guessed as RUNNING.
- **Every recorded path was spelled the platform's way.** Seven sites — `describe()` and the
  line that overwrote it, both terminal-log fields, the environment snapshot, the
  provenance path, the refused-late-inputs list. A record written on Windows said
  `data\a.tsv` where the same run on Linux said `data/a.tsv`, so `show --stale` keyed its
  answers `{'results\final.tsv': 'OK'}` for a reader asking about `results/final.tsv` and
  found nothing. The pin and the directory hash had each already made this choice, with
  their own note saying why; the record — which is mostly paths — had not. One rule now,
  `hashing._posix`, at the funnel, with an AST guard so the eighth site cannot be added
  quietly.
- **`run.open_output()` no longer translates line endings.** `csv` writes its own `\r\n`,
  and without `newline=""` Windows translated the `\n` of that pair as well: the README's
  own front-page block produced an artifact with a blank line after every row. It also means
  one script over one input now writes the same bytes, and therefore the same `sha256`, on
  every platform.

Four test guards named the wrong predicate. `hasattr(signal, "SIGTERM")` is true on Windows,
where `os.kill` is `TerminateProcess` — so the test that signals its own process **killed the
runner**, which is the exit 15 and the missing summary. `_RESOLVE_RAISES_ON_LOOP =
sys.version_info < (3, 13)` asked the version, two lines under a comment reading *"PROBED, NOT
ASKED"*. Both are probes now, along with four more: a POSIX shell, real file modes, directory
handles, and whether `link/..` traverses the link.

### Known and deliberate

- **Line endings are not content.** `content_digest` ignores a trailing newline and CRLF vs
  LF. Measured: making them significant moves 91.11% of artifacts in the project this came
  from. `sha256` is recorded beside it and preserves exact bytes.
- **A `Run` names one script.** A pipeline of five scripts is five runs, joined by the
  history and by `lineage`.
- **Records contain absolute paths and a hostname.** See `SECURITY.md`.

### Verified

774 tests, 100% statement *and* branch coverage.

**THE WHOLE MATRIX IS GREEN, run `33575026376`, 2026-09-02.** Seven jobs: `lint`, `build`,
ubuntu 3.10/3.11/3.12/3.13, macOS 3.12 and Windows 3.12. (The first ever green matrix was
`33561447357` the day before; these are the figures after the split-role review.)

| leg | passed | skipped |
|---|---|---|
| ubuntu 3.12 | 769 | 5 |
| macOS 3.12 | 768 | 6 |
| windows 3.12 | 722 | 50 |

Before this, macOS and Windows had run green **once**, on 2026-08-12 (run `31592997325`,
commit `faa47a54`), and a hundred commits landed in between: the matrix had not seen `show`,
`exec`, `verify`, the transformation-log sink, the 3.13 fix or anything since. When it came
back it was red on both, and the Windows leg could not say why — it aborted at 66% with exit
15 and no summary. See the entry above for what was behind that, and README, *What has
actually been run*.

Widening from one interpreter to four found two defects a 3.12-only gate could not, and both
are the same shape: a test asserting something true of the interpreter rather than of the
code.

- `show --format yaml`'s depth guard hands the remainder to `json.dumps`, which is itself
  recursive, so a 1,000-deep record rendered on 3.12 and raised `RecursionError` on 3.10 —
  the version `requires-python` names as the floor. A reader narrower than its writer.
- CPython 3.13 changed `Path.resolve()`: a symlink loop **returns the path unchanged** rather
  than raising `RuntimeError`. Four tests asserted that exception as their premise and failed
  on 3.13 while the package behaved correctly — the run survives and the path pins as
  external. The premise is now expressed once, in a form true on every version, and
  `run.py`'s `RuntimeError` guards stay: a relaxation on the newer version, not a removal on
  the older ones.

Windows 3.12 runs the same suite **minus 22 tests and without the coverage floor**, and the
distinction is the point: those 22 build a fixture Windows cannot build — a FIFO
(`os.mkfifo` does not exist), a symlink (blocked without Developer Mode or admin), or a file
`chmod(0o000)` genuinely makes unreadable — so they skip, their lines go unmeasured, and
100% stops being reachable there by construction. They are skipped by PROBE rather than by
platform name, which also fixes the reverse error: `chmod(0o000)` denies nothing to root
either, so those tests could not fail inside a root container and a `win32` check called
that a pass.

This section previously read "on Linux 3.10–3.13, macOS and Windows", which the Windows job
could not have supported: all 22 failed in setup. What is verified from Linux is that the
suite has no failures when those four constructs are unavailable; whether Windows agrees
about path separators, line endings and open-file deletion is answered by the job, not from
here.

Coverage is a floor, not the argument: every fix above was mutation-tested — the defect
reintroduced, the suite required to fail.
