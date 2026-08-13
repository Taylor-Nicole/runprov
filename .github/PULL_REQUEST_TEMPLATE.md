<!-- Thank you for this. CONTRIBUTING.md has the detail; this is the short form. -->

## What this changes, and what went wrong without it

<!-- The commit style here is "what the defect was", not "what the change is" — the git log
     is the record of why the code looks like this. A sentence naming the failure is worth
     more than a paragraph naming the fix. -->

## How you know it works

<!-- The bar is measurement, not confidence. For a bug fix, the strongest evidence is a
     test that FAILS on the current main and passes with your change:

         git archive HEAD | tar -x -C /tmp/before
         cp tests/test_runprov.py /tmp/before/tests/
         cd /tmp/before && python -m pytest -k your_test    # must fail

     If your change is about performance or memory, give the numbers and say what machine
     and what state they came from — a benchmark on a cold cache is a different claim. -->

## Checklist

- [ ] `python ci.py lint` passes (ruff + mypy `--strict`, both pinned).
- [ ] `python ci.py test` passes — coverage is **100% statement and branch**, and that is
      the gate rather than a target.
- [ ] `python ci.py build` passes, if you touched packaging or anything the sdist ships.
- [ ] **No `content_digest` value moved.** A changed digest re-pins every artifact at once,
      which is the failure this package exists to end. If a digest must change, say so
      explicitly here — it is a format change and belongs to a version bump.
- [ ] The commit is signed off (`git commit -s`) — the DCO check requires it, see DCO.txt.

## Anything you are unsure about

<!-- Genuinely welcome. "I could not work out how to test this" is a fine thing to write. -->
