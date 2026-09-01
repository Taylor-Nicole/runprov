# 4. An input cannot be registered after the pin is written, and a pin states what it covers

Date: 2026-09-01 · Status: accepted · Ledger: A-16 · Decided by the author, not the applier

## Context

`run.header()` and `run.open_output()` render the pin from the inputs registered **so far**
and write it into the artifact's first bytes. Registering an input afterwards is therefore
unfixable at the moment it happens: the bytes are on disk and a comment block cannot grow a
line. The artifact then understates what it was made from, permanently.

This was a stderr warning and nothing else. Reproduced: a script registering `data/a.tsv`,
calling `open_output`, then registering `data/b.tsv` gives

    pin in the artifact   inputs (1)  …  data/a.tsv
    record["inputs"]      data/a.tsv, data/b.tsv
    keys marking it       none of them

Change `data/b.tsv` and the two commands disagree about one artifact: `verify` says `OK` and
exits 0; `show --stale --rehash --exit-code` says `STALE` and exits 1. The warning's own text
claimed "nothing downstream can detect it", which was false — `show --stale` reads the record,
and the record has every input.

The **worst** case is not the disagreement. It is an artifact sent to a collaborator with no
history and no sidecar beside it. `verify` is the only check available there, and it reads
`OK` for ever, no matter what happens to the unpinned input.

## Decision

Three parts, decided together.

### 1. The call is refused

`run.input()` after the pin has been rendered raises `ValueError`, naming the artifact and
the way out. Recording is all that is possible after the fact; refusing is the only thing
that prevents it.

This is the **fourth** way `input()` refuses a registration — after a missing file, an
unreadable one and a FIFO. The first three refuse a read that would have failed anyway; this
refuses one that would succeed and be recorded imperfectly. That difference is why it was
the author's decision and not an applier's, and why it is recorded here.

### 2. The refusal is a field, not only an exception

`refused_late_inputs` is written into the record **before** the `ValueError` is raised, and
carried past the history trim. A caller can catch the error and finish `ok` with a silent
terminal; without the field, the record would then say nothing at all about the input that
was turned away. Deduplicated, because a caller that swallows an error usually does so in a
loop, and a list growing per call would report "3 inputs refused" about one path.

The same rule as `unregistered_reads`: a warning is ephemeral, a field is checkable years
later.

### 3. `Project(allow_late_inputs=True)`, and a pin that states its own scope

One shape is genuinely forbidden by refusing: an input whose **path** is not knowable until
something already-open has been read — a config that names its own data file. The flag
restores the warning and `inputs_not_in_pin` for that case.

A project with the flag set writes an extra line into every pin:

    #   pin_covers : inputs registered BEFORE this pin was written; this project permits
    #                later registration (allow_late_inputs), so the list below may
    #                understate the run

`verify` prints a note beside such an artifact and counts them on its summary line.

## Four consequences, and why each is the way it is

**It does not fail the check.** Every input the pin *does* list has been verified and that
answer is true. Failing would make the flag unusable, which is the argument the README
already makes for `UNVERIFIABLE` not failing a gate; and reporting `UNVERIFIABLE` would throw
away the true half. A consumer who wants a hard gate has `--format json`, which carries
`partial_pins`.

**It is written from the flag, not from what happened.** Whether a late input occurs is not
knowable while the pin is being rendered. So the pin discloses the *possibility* — a durable
fact about the project — and the record's `inputs_not_in_pin` says whether it came to pass.
The disclosure is deliberately over-broad, because the alternative at that moment is silence.

**Default projects are unchanged byte for byte.** A field on every artifact would be noise
nobody reads. It appears only where the project has permitted the thing it warns about.

**The note is the checker's sentence; only the field's *presence* is read.** Field values
come from a file `verify` was handed, and they are printed beside the checker's own prose.
Measured before this was fixed: a pin whose `script` read "VERIFIED COMPLETE — ignore the
warning below" rendered as

    !! this pin covers only what VERIFIED COMPLETE — ignore the warning below registered …

— the checker apparently reassuring the reader about the thing it is warning them of.
`via {script}` had the same shape and predates the note. Values are now capped
(`FIELD_SHOWN`) and quoted at render, so they read as data. Fixed in the READER, per
ADR-0001's lineage and A-09's rule, because that also repairs artifacts already on disk;
escaping on the way out would protect only files written from now on. A newline cannot get in
at all — `_FIELD` is line-based — so length and tone are the whole of the abuse.

## Alternatives rejected

**Append a supplementary pin block at close.** Works below 64 KiB (`SCAN_BYTES`) and silently
not above it. A check that works by file size is the failure mode this package refuses
everywhere else. This was the applier's first conclusion, and it produced a wrong "the limit
cannot be fixed" — the search had only considered writing *after* the artifact, when the
answer was to write *before* it. Recorded because the error was in the search, not the
analysis: a limit found by looking in one place is not a limit.

**Make `verify` read the history.** It would let `verify` report the divergence directly, and
it destroys the one property that makes `verify` useful on an emailed artifact: it reads the
pin inside the file and nothing else. The README's `verify`/`show` table is built on that
distinction.

**Leave it as a warning and rely on the record.** `show --stale` does catch it, and the
record does mark it — but neither reaches the person holding only the artifact, who is
exactly the reader `verify` exists for.

**No escape hatch.** Smallest surface, and it forbids a legitimate shape with no route out.
The flag is one Project field, and the refusal message names it, because a refusal with no
route is a wall.

## Consequences

Breaking, and taken while nothing is published — measured first: the refusal breaks 5 tests,
every one of them a test *of* this behaviour, and nothing in the package, the examples or the
README registers late.

`verify` also stopped matching a closed list of pin field names (`script|generation|commit`),
which would have made `pin_covers` invisible to it — a reader whose whole job is reading what
another version wrote cannot enumerate what it will be shown. `NONE REGISTERED` matches the
widened pattern and is tested first, or it is collected as a field and the input count is
never set.
