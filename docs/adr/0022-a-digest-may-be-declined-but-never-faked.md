# 22. A digest may be declined, but never faked

- **Status:** Accepted — built 2026-10-11, **not yet released** (`[Unreleased]`). The last open item of the 2026-10-09 council. Decided by Taylor on 2026-10-11: both shapes — a project threshold **and** a per-call override — for 0.9.0, with 0.8.0 tagged first.
- **Applies to:** `runprov/project.py` (`digest_max_bytes`, `parse_size`), `runprov/run.py` (`input`, `output`, `_digest_decision`), `runprov/hashing.py` (`describe`)

## Context

**The state before this.** `Run.input()` always digested, and so did `write()`'s output loop. There was no
size-only mode, no cache and no opt-out, while the stated audience is genomics.

**The measurement.** Measured on this host, 2 GiB of random bytes on an external SSD:

| read | throughput | extrapolated to 50 GB |
|---|---|---|
| **cold, off the disk** | **263 MiB/s** | **~194 s** |
| warm, page cache | 815 MiB/s | ~63 s |

**The first registration of a large file is cold by definition**, so the cost that matters is
the one over three minutes, **per registration**. The council's original figure — 702 MB/s,
~71 s — was the warm half of a 3× spread. *Neither number is a property of this package; the
condition is part of the measurement.*

**The asymmetry, in the package's own words.** `README.md` already names that exact cost, with that exact file size, **for the
checker**: *"reading every byte of a 50 GB BAM to learn it has no pin is the cost that gets a
checker deleted."* So `verify` bounds its pin scan to the first 64 KiB, and the recorder paid a
strictly larger version of the same cost unconditionally. The case was considered, in print, one
command over.

**The sweep, which changed the design.** An AST sweep of all 24 modules — 20 282 lines, 38 real calls to `describe` / `sha256` /
`content_digest` / `pin_digest` — found **three** sites of the shape *a per-file read whose cost
scales with the user's data, with no bound and no escape*: `input()`, `write()`'s output loop,
and the directory-tree digest whose own comment measures *"a 40 GB reference directory"*.
A `grep` for the same thing reported fourteen sites, ten of which were the word `describe()` in
prose — **a sweep in the wrong unit is a finding about the sweep.**

## Decision

**R-1.** `Project.digest_max_bytes` sets a size at or above which a file is **recorded but not
read**. `None`, the default, is the previous behaviour exactly: everything is digested, whatever
it costs. No existing run changes.

**R-2.** `Run.input(path, *, digest=None)` and `Run.output(path, *, digest=None)` override it per
call, **in both directions**: `digest=True` reads a file over the limit, `digest=False` declines
one under it. A threshold a caller cannot override is not a default but a policy, and the one
input a reviewer needs hashed is exactly the one that happens to be large. Keyword-only, so no
existing positional call changes meaning.

**R-3.** **The decision is made in exactly one place** — `Run._digest_decision` — and both
recording sites call it. the sweep is why this is a method and not a copied `if`: the rule applies
twice on the recording path, and an escape added to one site would be one rule with two
implementations, drifting by a whole case.

**R-4.** `describe(path, *, digest=False)` owns the **shape** of the answer and `run.py` owns the
**policy**. A second function assembling a stat-only entry of its own would be one record
described twice.

**R-5.** An undigested entry says so **positively**: `digested: false`, plus a `note` giving the
size and the limit, or naming the caller's choice. Omitted in the ordinary case, because *absent*
and *false* are different statements and only one of them is a measurement. **`kind` is
untouched** — a directory is still a directory — see R-8.

**R-6.** A size is an `int` of bytes, or a string carrying its **own explicit unit**
(`"1GiB"`, `"500MB"`). Both unit families are honoured because this project writes `64 KiB` in
its own documentation, and guessing would be it inventing a fact about the caller's
configuration. **A bare numeric string is refused**: `"1000000"` is a value a caller believes
they have set, and bytes-or-megabytes is not something to guess. This is `parse_age`'s rule
applied to a second magnitude.

**R-7.** The value is parsed at `configure()`, so a bad one fails there rather than at the end
of a long run, and nothing downstream ever sees a string.

## Consequences

**R-8.** **The digest-based answers degrade and name the reason; the stat-based one keeps
working.** `verify` reports `UNVERIFIABLE … (the run recorded no digest for it)` and exits 2 —
*we could not look*, a different code from *this artifact is wrong* — and `show --rehash` agrees.
`show --stale` is a **stat** check: it compares `size_bytes` and `mtime_utc` through
`moved_since` and never read a digest for any input, so an undigested entry is exactly as
checkable there as a digested one.

That is why R-5 keeps `kind` as the file's type. `show`'s non-rehash path decides an input is
*uncheckable* from `kind not in ("file", None)`; spelling the skip as a new `kind` would have
made the stat check report `?` and **discard an answer it can still give**. Measured both ways
before choosing.

**R-9.** **This is not a cache, and must never become one.** A `(size, mtime, inode)` cache
would record a digest the run did not compute, which ADR-0013 — *what a run consumed, measured
not declared* — refuses. The difference is the whole of this decision: an undigested entry
reports UNVERIFIABLE, and a cached one would report OK on a hash nobody took. **A digest may be
declined. It may not be invented.**

**R-10.** The threshold is a **per-file** size, and a directory's `st_size` is its inode's, not
its content's — so a registered directory is digested whatever the threshold says, and
`digest=False` is the escape for one. Stated rather than fixed by summing the tree: a walk that
happens only to decide whether to walk is a surprise.

## Alternatives considered

**A size-only mode as the default.** Rejected: it makes every existing record weaker without
anyone asking, and the package's value is that a record says what was actually measured.

**A `(size, mtime, inode)` cache.** Rejected by Taylor and by R-9.

**An escape on `input()` alone**, as the council filed it. Rejected by the sweep: outputs are the
user's data too, and a run that writes a 50 GB BAM pays the same cost as one that reads it.
