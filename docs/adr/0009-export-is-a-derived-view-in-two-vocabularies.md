# 9. Export is a derived view, in two vocabularies and two scopes

Date: 2026-09-04 · Status: accepted · Ledger: T-20 · Decided by the author, not the applier

## Context

Everything this package writes is its own: `runs.jsonl`, the sidecar, the in-band pin,
`transformation_log.yml`. That is the right choice for a format whose job is to be read by a
person and grepped three years later, and it has a cost — a repository, a registry or a
reviewer has to be taught to read it, and most of them will not be.

It is also the question a JOSS reviewer asks in one line: *"why not use existing standards?"*
The answer worth being able to give is not an argument. It is **we emit them**.

## Decision

**`runprov export`, in two formats and two scopes, writing nothing this package owns.**

| format | what it is | why this one |
|---|---|---|
| **RO-Crate 1.1** | JSON-LD over schema.org | Zenodo and WorkflowHub ingest it. This is the format that matters at **deposit**: the record arrives with the data rather than being described in a README nobody parses. |
| **PROV-JSON** | the W3C provenance model, in the JSON serialisation its own note defines | Entities, activities, agents and the relations between them — the vocabulary the field agreed on, and the one a **reviewer** recognises. |

| scope | what it answers |
|---|---|
| **whole history** (default) | what you deposit: every run, every artifact, the lineage joining them |
| **one run**, from its sidecar | what you attach to a submitted artifact — and the only scope available to somebody holding a file and its sidecar, with no history to read |

The second scope is not a convenience. It is the same case the in-band pin exists for, and a
tool that could only export a whole project would have nothing to offer it.

**Export is read-only and additive.** `runs.jsonl`, the sidecars, the YAML twins and
`transformation_log.yml` are untouched and remain the record of truth. A derived view that
could rewrite its source would be a strange thing for a provenance tool to ship, so a test
asserts that every byte in the project is identical before and after an export.

**PROV-JSON rather than PROV-O in Turtle or RDF/XML**, and the reason is the dependency
budget: those want an RDF library. Both formats here are plain JSON, and `json` is in the
standard library — so the zero-dependency claim survives the feature.

## Consequences

* **The lineage joins only because every recorded path is spelled one way.** A file is keyed
  in both graphs by the path the record gives it; two runs naming one file two ways would
  produce two nodes and no edge. That is T-08 paying for itself in a place it was not written
  for.
* **`wasDerivedFrom` is per-run, and that is the limit worth knowing.** It means *"this output
  was produced by a run that read this input"*, not *"this value came from that value"*. PROV
  allows the finer claim; this package cannot make it, because it records what was opened and
  not what mattered. Saying more in a standard vocabulary than the record supports would be
  the exact failure this project exists to refuse — and **harder to catch, because it would
  be well-formed**.
* A run that only started (`runprov.start.v1`) is left out. In a crate it would assert an
  activity that produced nothing, which is not what a reader would understand by it.
* `ro-crate-metadata.json` is not a preference. A crate is recognised by that filename sitting
  beside the data, so `-o <dir>` uses it.
* A missing field is **omitted**, never emitted as null: a validator reads `null` as "known to
  be nothing", and a thin history line simply does not know.

## Alternatives considered

**Only RO-Crate.** It is the one with real consumers, and it is schema.org — good at *what
exists*, weaker at *what happened*. PROV is the model built for the second, and a reviewer
asking about provenance semantics is asking about PROV.

**Only PROV.** More precise and consumed by fewer things people actually use. Deposit is the
concrete use, and deposit means RO-Crate.

**Emit RO-Crate with PROV-O terms embedded.** Legitimate, and the most faithful thing a
mature implementation would do. It also multiplies what has to be right in one document, and
neither format is validated here beyond its own tests. Two documents that are each simple
and honest beat one that is clever and unverified.

**Make it the native format.** No: `runs.jsonl` is append-only, greppable and readable by a
person under time pressure. JSON-LD is none of those, and the record of truth should be the
one you can read when everything else has failed.
