# Hand-off: where the prompt cycle lives, and what runs here

This is runprov's entry point into the working cycle. **It is a POINTER, not a copy**, and
that is the whole design — see "Why there is only one standing prompt" below.

## The cycle, in order

1. **`python3 tools/prompt-graph.py`** — in the audit repository. Prints the frontier: which
   working ledger's `## Next action` nothing has started from yet.
2. **The standing prompt** — `HANDOFF-PROMPT.md` in the audit repository, section
   `## The standing prompt`. Read it from the file; never from memory.
3. **A working ledger** — written in the audit repository *before* the work, not after.

The audit repository is a sibling of this one, not a dependency. **Nothing in runprov imports,
reads or requires it**, and runprov builds, tests and releases without it — which is why the
guard on this document checks sibling paths only when the sibling is present, and says so.

## Why there is only one standing prompt, and no second prompt-graph

**One rule, one definition.** Two copies of a rule agree the week they are typed. This project
has the measurement: the standing prompt once drifted *six lessons* behind the copy being
emitted, which is why the hand-off now reads the file instead of recalling it. A second
standing prompt in this repository would recreate exactly that, and the drift is silent —
each copy reads complete on its own.

**And a second prompt-graph would be red on arrival.** `prompt-graph.py`, in the audit
repository, derives its nodes from documents carrying a `## Next action`, and refuses when it
finds none rather than
printing an empty frontier that looks like a clean one. runprov has no such documents: the
narrative lives in the audit repository by design, because this repository is a published
artifact and its history is read by strangers. A check that cannot pass teaches its audience
to ignore it, so there is none here.

**The anti-duplication rule is enforced from THIS side**, in
`tests/test_runprov.py`, because **runprov has a gate and the audit repository does not**. If
anyone pastes the standing prompt into this repository, that test goes red.

## What this repository holds, and what checks it

Everything below is in runprov and runs here:

| surface | what it is |
|---|---|
| `ci.py` | the gate: lint, test, build. Green before every commit. |
| `tests/test_runprov.py` | the suite, and the guards. 100% branch coverage, enforced. |
| `docs/adr/` | the decisions, one file each. Read the one governing any file you touch. |
| `docs/claims-baseline.txt` | the figures shipped documents claim, ratcheted. |
| `docs/public-surface.txt` | the names this package promises. |
| `CHANGELOG.md` | what each release changed, per version. |
| `.github/workflows/test.yml` | the matrix. A green local gate is not a green matrix. |

## The two rules that outlive any hand-off

**No assistant-attribution trailers on any commit, in either repository.** Not
`Co-Authored-By:`, not `Claude-Session:`, not any other. Pre-test every message with
`python3 ci.py check-commit-msg <path>` — it takes a path, not the text — and then verify the
committed object. A trailer that reaches a branch with a pull request is permanent.

**A green gate is evidence that the guards which exist pass, and nothing more.** A line that
runs under test is not thereby defended. The deliverable is a check that goes red when the
property breaks, and that has been *seen* going red.
