# Changelog

Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions follow
[semantic versioning](https://semver.org/), with the record-format promise in the README
taking precedence over the Python API while this is `0.x`.

Entries state what was **measured**, not what was improved. A fix with no number beside it
is a fix nobody checked.

## [Unreleased]

### Added

* **`runprov chain` — has the run history been edited since it was written?** (T-32, ADR-0016.)
  Every history line now carries `prev`, the sha256 of the line before it, so an edit — or a
  deletion or reordering anywhere but the very end — breaks every link after it. Nothing
  detected that before: change line 40 and `log`, `show`, `diff`, `impact` and `report` all
  repeat the new value with no sign anything moved.

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
  any single byte anywhere in a history and the chain never stays silent — 3 192 cases over four
  shapes (intact, torn mid-write, mixed-version, written before the chain existed), zero
  escapes.** `attested` is a count of edges that hold, so it can never exceed the lines it had.

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
