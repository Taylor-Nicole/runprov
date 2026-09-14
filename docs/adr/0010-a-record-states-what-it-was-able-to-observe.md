# 10. A record states what it was able to observe

Date: 2026-09-11, amended 2026-09-14 · Status: **accepted** (stage one shipped `f21ddfc`; stage two specified below) · Ledger: T-25

## Context

T-25 asks for function-level capture: which values a function received and returned, so a
difference inside a script is visible the way a difference between files already is. The
obvious implementation is noWorkflow's — rewrite the abstract syntax tree — and it is not
available here, because `WHY.md` and the JOSS draft both state the trade in as many words:
**provenance must not change the program it observes.** A test section carries that title.

Two mechanisms remain, and they do not cover the same Python versions:

| | rewrites source? | versions | cost |
|---|---|---|---|
| `sys.settrace` / `setprofile` | no | all | severe overhead, and it collides with `coverage`, which this project runs at 100% |
| **`sys.monitoring`** (PEP 669) | no | **3.12+** | low overhead; `CALL` / `RETURN` events |
| **a decorator, `@run.step`** | no | all | one line per function, and it appears in the diff |

That table is the whole problem. `requires-python = ">=3.10"`, so a feature built on
`sys.monitoring` is present for some users of the same version of this package and absent for
others — **and the difference does not announce itself**.

### The failure that makes this an ADR rather than a patch

Two runs of the same script, same inputs, same package version, one on 3.11 and one on 3.12.
The 3.11 record contains no step observations. A reader comparing them concludes *"nothing
changed inside the script"*.

What is true is *"nothing was looked at inside the script"*. This package exists because those
two produce the same green, and it has caught the same shape four times already: a census that
reported zero because it enumerated nothing; a `twine check` that passed because it could not
render; a guard whose scope stopped covering what it named; a mutation that missed its anchor
and looked like a survivor. **A capability difference that is invisible in the record is that
defect, introduced by the feature meant to detect it.**

### And the general case, which is bigger than Python's version

The author's field needs the same thing of every dependency, not only of the interpreter: a
result that differs because `pandas` changed minor version is a real finding, and one that is
untraceable if the record does not say which `pandas` ran.

Measured today, this is what a default record carries:

```json
"environment": {
  "python": "3.12.13",
  "platform": "Linux-5.15.0-186-generic-x86_64-with-glibc2.35",
  "packages": {},
  "manager": {"detected": ["uv", "venv"], "evidence": {"pyvenv.cfg:uv": "0.11.8"}},
  "lockfiles": []
}
```

`packages` is **empty by default** — `DEFAULT_TRACKED = ()`, on the stated grounds that "a
package list is a GUESS about somebody's domain". The full set is available, but only if
`env_snapshot_dir` is configured: `environment_snapshot()` then writes a content-addressed
`env-<hash>.txt` and names it in the record.

So the stack *is* traceable, and it is **opt-in and silent when off**. That is the same defect
one level up: a record with `"packages": {}` does not distinguish *"no tracked package was
installed"* from *"nobody asked"*.

## Decision

**One package, one API, two backends — and the record says which one ran.**

1. **`@run.step` is the primary mechanism.** It works on every supported version, it is in the
   code where a reviewer sees it in the diff, and it cannot be bypassed by launching
   differently — the same argument that makes `run.input()` the shape of this package.

2. **`sys.monitoring` is an optional amplifier on 3.12+**, never a replacement. It records
   calls the author did not decorate, labelled **observed** rather than **declared** — exactly
   the distinction `unregistered_reads` already draws for files that were read without being
   registered.

3. **Every record carries an `observation` block naming what was available**, whether or not
   the feature is used:

   ```json
   "observation": {
     "steps": "declared",            // "declared" | "declared+observed" | "none"
     "auto_backend": null,           // "sys.monitoring" when active
     "auto_available": false,        // capability, not choice: 3.12+ and the extra installed
     "packages_recorded": "none"     // "none" | "tracked" | "snapshot"
   }
   ```

   `auto_available` is the field that matters. It separates *not observed* from *could not
   observe*, which is the whole point of this ADR, and it is recorded even when nothing else
   in this feature is in use.

4. **Comparison across unlike records refuses, loudly.** Two records whose `observation`
   blocks differ may be compared on files — that half is unchanged and version-independent —
   but any report about steps says so rather than reporting an absence as an agreement.

5. **The digest rule is stated in advance, because it is where this could quietly lie.**
   Files have bytes; arguments are arbitrary objects, which is where noWorkflow reaches for
   SQLite — the design this package rejects, since then the record depends on the tool again.

   | value | recorded as |
   |---|---|
   | `bytes`, `str`, `int`, `float`, `bool`, `None` | canonical encoding, digested |
   | `list` / `tuple` / `dict` of the above | canonical JSON, keys sorted, digested |
   | anything with `__runprov_digest__` | its answer — the existing extension protocol |
   | **everything else** | **`UNDIGESTIBLE`, with the type name and nothing more** |

   Never a `repr`: unstable across runs and across versions, and it would be recorded as
   though it were stable. Never a pickle: not reproducible across versions, and it would put
   executable bytes inside a provenance record. The last row is the one that keeps this
   honest, and it is the same behaviour as the fifteen formats `open_output` refuses rather
   than pretending to pin.

6. **Step results are reported separately from `verify`'s four verdicts.** `OK` / `STALE` /
   `GONE` / `ALTERED` answer *"does this artifact still follow from its inputs?"*. A step
   digest answers *"did this function see what it saw last time?"* — a different question
   about a different subject, and a fifth value would blur both.

## Amendment, 2026-09-14 — the automatic backend has two levels, and they are different features

Stage one (`@run.step`) shipped in `f21ddfc`. Building stage two forced two questions the
original decision did not answer. Both are settled here rather than in the code.

### Measured first, because "it records everything" was an assertion

Reading **500 lines of TSV** with the standard library, under `sys.monitoring`:

```
1 505 Python calls observed
1 504 of them inside csv.py
    1 in the caller's own code
```

**The cap does not save this.** At 1 000 entries it would fill on `csv.py` internals and stop
before recording a single one of the author's functions — a record that honestly announces
its truncation and contains the wrong thing. **Scope is the fix, not the cap.** Filtering to
code under `project.root`, the same run yields **4 calls**, all the caller's.

The scope is therefore DERIVED from the project root, never a list of modules to maintain.

### Anonymous frames are not steps

`sum(int(r["value"]) for r in rows)` compiles the generator expression into its own code
object, so the interpreter reports two calls for that one line: `summarise`, and
`summarise.<locals>.<genexpr>`. It is genuinely a call and it is not a step: it has no name
the author wrote, and it describes Python's compilation of a line rather than the line.

**Filtered, and derived rather than listed:** a code object whose `co_qualname` contains `<`
is anonymous by construction — `<genexpr>`, `<lambda>`, `<listcomp>`. No set of names to fall
out of date.

### Two levels, because they answer two questions at two prices

| mode | what an observed entry says | cost |
|---|---|---|
| **`census`** — default on 3.12+ | *this function of mine ran, N times* | one counter per call |
| **`arguments`** — opt-in | *and these are the distinct argument sets it saw* | a frame materialised and values digested, per call |

`sys.monitoring`'s `PY_START` callback receives `(code, instruction_offset)` — **it reports
that a call began, not what was passed.** Arguments require reaching into the running frame
with `sys._getframe()` and reading `f_locals` on every call. That is buildable and it is a
different feature at a different price, which is why it is a MODE and not a default.

**`census` is the default for a reason beyond cost.** A declared step says *the author said
this matters*; an observed step says only *the interpreter noticed this*. Those are different
claims, and if both carried argument digests they would look identical in the record while
meaning different things — the distinction surviving only in a metadata field. Keeping
observed entries structurally different — a count, not a digest — puts it in the data. Same
reasoning as `unregistered_reads` being its own key rather than quietly mixed into `inputs`.

It also gives the feature an honest shape: a census tells you **which functions to decorate**.
It complements `@run.step` instead of competing with it.

### In `arguments` mode, distinct signatures rather than calls

Recording one entry per call reintroduces the volume problem inside the scope. So a function
records its call count and the **set of distinct argument-set digests**, with a count each:

```json
"work.py:summarise": {"calls": 400, "signatures": {"a1b2c3d4e5f60718": 399, "…": 1}}
```

Four hundred calls, two distinct input sets. *"Did this function see the same inputs as last
time?"* is then a comparison of two small sets, and the record's size is bounded by variety
rather than by volume.

### What the record says about which backend ran

`observation.auto_backend` is `"sys.monitoring"` or `null`; `observation.auto_mode` is
`"off"`, `"census"` or `"arguments"`. **A refusal is recorded, not silent:** `sys.monitoring`
has six tool slots and `coverage` uses one, so `use_tool_id` can fail — in which case the
mode is recorded as `off` with the reason, rather than a record that looks like a run where
nothing happened to be called.

### The implementation constraint this creates

Version-conditional code cannot reach 100 % coverage on any single interpreter, which is the
L-104 problem in a new place. The backend therefore takes its monitoring interface **as an
argument** rather than reaching for `sys.monitoring` directly, so both paths are exercised on
every leg with a stub. A capability read at module scope would have forced a coverage
exemption on three legs instead of a test.

## Consequences

* A 3.11 user gets the decorator and a record that **says** automatic capture was unavailable.
  Nothing silently differs.
* `sys.monitoring` records everything, which is also its problem: a loop of ten thousand calls
  floods a history designed to be read with `cat`. It needs a cap, and the cap needs to be
  recorded when it bites — a truncated record that does not say it was truncated is this same
  defect a third time.
* `observation.packages_recorded` makes the empty `packages: {}` legible. It does not change
  the default, which stays `()` for the reason already given, but it stops the default from
  being indistinguishable from an unasked question.
* One more field in every record, forever, for a feature most runs will not use. That is the
  price of the capability being legible, and it is four short keys.

## Alternatives considered

**Rewrite the AST, like noWorkflow.** Rejected: it would retract a published claim, and the
claim is the honest one. noWorkflow captures more and says so; this says less and says that.

**Ship two packages, one per Python range.** Rejected. It splits the user base and doubles
maintenance for one feature, and it does not solve the actual problem — two records from two
packages are exactly as hard to compare as two records from two backends, with an extra
version number to confuse it.

**`sys.settrace`.** Rejected on measurement grounds before taste: it fights `coverage`, and
this project's gate requires 100% branch coverage. A feature that cannot be tested under the
project's own gate is not a feature.

**Make `sys.monitoring` the primary mechanism on 3.12+ and the decorator a fallback.**
Rejected. It inverts the package's argument: the automatic path is the one that fails silently
when it is not there, and the explicit path is the one that cannot be bypassed. The same
reasoning rejects `smt run` as this package's adoption model.

**Do nothing, and keep `run.note()`.** Still the honest baseline, and it is what exists today.
This ADR is not implemented until someone needs the finer grain badly enough to accept the
`UNDIGESTIBLE` row as a real and frequent answer.
