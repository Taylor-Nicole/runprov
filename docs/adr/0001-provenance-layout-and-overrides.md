# 0001 — Where records are written, and how a team changes it

- **Status:** Accepted
- **Date:** 2026-08-19
- **Applies to:** `runprov/project.py` (`Project`, `configure`), `runprov/sinks.py`

## Context

A recording tool creates files in someone else's project. That is an intrusion, and it needs a
defensible answer to three questions a team will ask on day one:

1. **What appears, and when?** A folder a user did not create is alarming if it is unexplained.
2. **Can we name it ourselves?** Groups arrive with conventions already in place — `audit/`,
   `metadata/`, `.yaml` rather than `.yml`, a lineage file the wiki already links to. A package
   that ignores those conventions is one a team has to fight.
3. **What is safe to delete?** If a user cannot answer this, they will either keep everything
   forever out of fear, or delete the wrong thing.

The predecessor answered none of these: its log path was effectively fixed, and nine repair
scripts grew around the single file it insisted on.

## Decision

**One record, several views. The record is `runs.jsonl`; everything else is a view of it.**

On the first completed run, `runprov` creates what it needs:

| artefact | default location | kind |
|---|---|---|
| run history | `<root>/provenance/runs.jsonl` | **the record** |
| project-wide readable log | *beside the history*: `<root>/provenance/transformation_log.yml` | view |
| per-run record | wherever `provenance=` on `Run(...)` points | record |
| per-run readable twin | same path, `.yml` instead of `.json` | view |

Each is overridable, through `configure(...)` / `Project`:

| setting | effect |
|---|---|
| `root=` | the project. Every recorded path is relative to it |
| `run_log=` | move the history. **The readable log follows it** |
| `transformation_log=` | name and place the readable log independently |
| `write_transformation_log=False` | no project-wide YAML |
| `write_yaml_sidecar=False` | no per-run YAML twin |
| `sink=` | send records somewhere else entirely (see below) |

Three properties are deliberate:

**The readable log is defined as "beside the history", not as a fixed path.** Moving the history
moves it. A team names one location, not two, and the pair cannot drift apart — the failure mode
where a project's history and its human-readable log describe different runs.

**A supplied `sink=` disables the YAML view.** If a lab routes records to a shared database, the
package stops writing a YAML file next to it. Continuing would mean deciding where someone
else's records live after they have said where.

**Views are safe to lose.** Deleting, renaming or disabling any view costs nothing, because the
record is append-only and elsewhere, and the view regenerates:
`python -m runprov log --format yaml`. This is why the record is JSONL and the view is YAML, and
not the reverse: a damaged line in JSONL costs that line, while a damaged block in a single YAML
document costs everything after it — which is precisely how the predecessor's log died.

## Consequences

- A project gets a `provenance/` folder it did not create. Mitigated by: it is announced on
  first run, it is documented as step 3 of the getting-started guide, and it is one folder.
- Two files hold the same runs in different formats, so a reader must know which is
  authoritative. Mitigated by: the YAML's own banner says it is a view and names the command
  that rebuilds it.
- `run_log` and `transformation_log` interact (one follows the other unless set). This is
  convenient and slightly surprising; it is stated in `Project`'s field comments, in the
  getting-started guide, and asserted by tests.

## Alternatives considered

**A fixed, non-configurable layout.** Simplest to document and to support. Rejected: teams have
conventions that predate this package, and a tool that cannot fit them is a tool that does not
get adopted. Universal adoption of an unfittable interface is exactly the failure this package
was written in response to.

**YAML as the record, with no JSONL.** One file, human-readable, no duplication. Rejected on
evidence: the predecessor did this, and one hand-typed `Note:` inside a description made a
24,300-line file unparseable, spawning nine repair scripts. Measured on that file:
`yaml.safe_load` fails at line 14,547 and `safe_load_all` at 14,554.

**Regenerating the readable log on every run instead of appending.** Simpler and always
consistent. Rejected on cost: rewriting means reading the entire history to add one entry — O(n)
per run, O(n²) over a project — which surfaces in year two on exactly the long-lived history
this exists for. Appending costs the same at run 10,000 as at run 1.

**No readable log at all; regenerate on demand.** Tempting, and nearly right. Rejected because
the file people actually read is the one that is already there. On-demand generation means the
project's story exists only when someone remembers to ask for it.

## Verification

The four modes below were run, and these are the resulting file listings:

```
configure(root=D)                                   in.tsv  out.tsv  out.prov.json  out.prov.yml
                                                    provenance/runs.jsonl
                                                    provenance/transformation_log.yml

configure(root=D, run_log=D/"audit/history.jsonl")  audit/history.jsonl
                                                    audit/transformation_log.yml   ← followed
                                                    in.tsv  out.tsv  out.prov.json  out.prov.yml

configure(root=D,                                   docs/data_lineage.yaml         ← renamed
          transformation_log=D/"docs/data_lineage.yaml")
                                                    provenance/runs.jsonl
                                                    in.tsv  out.tsv  out.prov.json  out.prov.yml

configure(root=D, write_transformation_log=False,   in.tsv  out.tsv  out.prov.json
          write_yaml_sidecar=False)                 provenance/runs.jsonl
```
