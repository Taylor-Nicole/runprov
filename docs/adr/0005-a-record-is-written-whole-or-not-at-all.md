# 5. A record is written whole or not at all

Date: 2026-09-01 · Status: accepted · Ledger: T-04 · Decided by the author, not the applier

## Context

Every provenance write in this package was `pathlib.Path.write_text`. That call truncates the
destination and then fills it, so the file passes through a state in which it holds neither
the old contents nor the new. A process that stops in that window leaves a **prefix**.

This was not found by reading. `tools/torture.py` patches `Path.write_text`, enumerates the
write sites a real run actually reaches — rather than listing them, which is how a check stops
covering what it names — and tears each in turn. Measured over a two-step pipeline:

| site | file | 0% | 50% | 90% |
|---|---|---|---|---|
| 2 | `.incomplete/<uid>.json` | 0 of 218 B | 109 of 219 B | 197 of 219 B |
| 3 | `out/step1.prov.json` | 0 of 2 689 B | 1 345 of 2 691 B | 2 421 of 2 691 B |
| 4 | `out/step1.prov.yml` | 0 of 2 355 B | 1 178 of 2 357 B | 2 121 of 2 357 B |
| 5 | `.incomplete/<uid>.json` | 0 of 218 B | 109 of 219 B | 197 of 219 B |
| 6 | `out/step2.prov.json` | 0 of 2 698 B | 1 350 of 2 700 B | 2 430 of 2 700 B |
| 7 | `out/step2.prov.yml` | 0 of 2 364 B | 1 183 of 2 366 B | 2 129 of 2 366 B |

Six of the six sites the package owns. The seventh site in that run is the pipeline's own
`in.tsv` and is not this project's to fix.

**Nothing was broken.** Every reader survived every torn tree: `verify`, `show`, `log`,
`lineage` and `prune` stayed inside the exit-code contract over ninety of them and not one
printed a traceback. `sinks.py` — the history and the transformation log — was already correct,
appending under an exclusive lock with its own torn-line repair and an `fsync`. The finding is
not that a site was careless; it is that the one file somebody had thought hard about was the
one that was right, and the others had never been asked the question.

What was at stake is narrower than "the record is corrupt" and worse than it sounds: the crash
could destroy **the record that was already there**. A reader can be taught that a half file is
unreadable. Nothing recovers a whole one that was truncated to make room for a write that never
finished — and the event that does this is precisely the event a provenance record exists to
survive.

## Decision

One helper, `runprov/_atomic.py`, and every provenance write goes through it.

1. Write a temporary file **beside the destination** — `os.replace` is atomic only within one
   filesystem, and `tempfile.gettempdir()` is frequently a different one, including on this
   hospital's cluster and on every CI runner this project uses.
2. `fsync` the file **before** the rename, so the bytes are durable before the name points at
   them.
3. `os.replace` onto the destination. POSIX guarantees this is atomic: a concurrent reader sees
   the old file or the new one, and a crash at any instant leaves one of the two.
4. `fsync` the **directory** afterwards, because a rename that is not itself synced can be lost
   although the data it points at was written. This is the step usually skipped, and skipping
   it makes the guarantee "atomic but not durable" — not the one claimed above. It is best
   effort: Windows cannot open a directory as a file, some network filesystems refuse the sync,
   and neither is a reason to fail a write that has already succeeded.

Three behaviours of `write_text` are preserved deliberately, because changing them silently
would be a second defect wearing the first one's clothes:

* **Through a symlink, not over it.** `open(path, "w")` follows a link; a bare `os.replace`
  would destroy it and leave a real file where somebody had deliberately put a pointer.
* **The destination's own permissions.** A rename brings the temporary file's mode with it, so
  a record somebody had restricted would quietly re-open on the next run.
* **The same exceptions at the same moments**, so every caller's existing `except OSError` still
  catches exactly what it caught before.

`sinks.py` is exempt, with its reason recorded beside the exemption: rewriting a
forever-growing history to append one line is the wrong shape by a wide margin, and that file
already does the careful thing.

## Consequences

* A crash leaves the previous record intact and, at worst, one debris file named
  `.<name>.<uid>.runprov-tmp`. `verify` counts debris, does not read a pin out of it, and
  **says so** — it is the only visible trace that a run died mid-write, which is a thing the
  reader wants to be told rather than a thing to hide.
* One extra `fsync` per record and one per directory. The history sink already fsyncs per
  append, so this is not a new class of cost.
* `verify.collect` returns a third value. It is not part of `__all__`; the four call sites in
  the suite were updated.
* Torturing the package requires the harness to patch the new seam. It did not, at first, and
  reported "0 findings" over a census line reading "0 of them runprov's" — a harness that has
  lost sight of its subject prints exactly what one that found nothing prints. `part_a` now
  **voids** the run when it sees none of the package's writes.

## Alternatives considered

**Leave it, and rely on the readers.** They do handle a torn file correctly, and that was the
argument for doing nothing. It answers the wrong question: the readers protect you from a
*damaged* record, and nothing protects you from a *destroyed* one.

**`fsync` the file and skip the directory.** Cheaper, and the atomicity claim still holds. But
then "the record survives a crash" is true only of the data and not of the name that reaches
it, and an ADR that promises durability has to mean the whole sentence.

**Write to `tempfile.mkstemp()` and move.** Silently degrades to a copy across filesystems,
which is exactly the non-atomic write this replaces — and it would degrade on the machines this
package was written for.

**Make the callers do it.** Six call sites, each one line, is how six subtly different versions
of a thing get written. It is also how the seventh site, added later, gets none.
