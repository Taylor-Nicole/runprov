---
title: 'runprov: recording what a script read, in a form that needs no software to check'
tags:
  - Python
  - provenance
  - reproducibility
  - research software
  - data lineage
authors:
  - name: Taylor Thompson
    orcid: 0009-0004-0091-0319
    affiliation: "1, 2"
affiliations:
  - name: Plateforme GenoBioMICS, DMU Biologie-Pathologie, Hôpital Henri-Mondor, AP-HP, Créteil, France
    index: 1
  - name: INSERM Unité U955, équipe Pawlotsky, Créteil, France
    index: 2
date: 10 September 2026
bibliography: paper.bib
---

<!-- DRAFT. NOT SUBMITTED.
     Prose, reviewer preparation and the trimming history live in
     `runprov_paper/PAPER-DRAFT-joss.md`; this file is the extract JOSS builds from.

     THE AUTHOR LIST IS NOT SETTLED. It carries one name because JOSS asks for contribution
     to the SOFTWARE, and because question 2 of the letter to the DRCI asks who should figure
     where — that answer decides this block. If Christophe Rodriguez becomes an author here,
     the sentence thanking him in the Acknowledgements must come out: one does not thank a
     co-author.

     Affiliations are word-for-word `CITATION.cff`'s. If one is edited, edit the other.
     The National Reference Center is named in the Acknowledgements as the funder of the
     post, not as an affiliation; see `runprov_paper/REFERENCES.md`.
-->

> *"Let the seal … be set upon these lines, and they shall never be filched from him, nor
> shall evil ever be changed with their good."*
> — Theognis 19–23, trans. J. M. Edmonds, *Elegy and Iambus* I

### Summary

`runprov` records what a Python script actually read, wrote and executed, and writes that
record in two forms that require no software to read: a YAML log whose digests are in
`sha256sum`'s own format, and a comment header inside the artifact itself. A command,
`runprov verify`, re-derives every recorded digest and reports whether a result still follows
from the inputs it was made from — or has itself been edited — reading only the artifact.

The package has no runtime dependencies and adoption is one call per file: `run.input(p)`
**returns the path it was given**, so it is written on the way to the `open` already there:

```python
with open(ROOT / "data/measurements.tsv") as fh:             # before
with open(run.input(ROOT / "data/measurements.tsv")) as fh:  # after: registered and hashed
```

That decision is what makes the record hard to falsify by accident: registration is not a
declaration alongside the code, where the two drift apart, but sits *in* the expression that
opens the file. Outputs take the same shape — `run.open_output(p)` returns a handle and
writes the pin into the artifact's first bytes — and a read that bypasses registration is
still noticed: a CPython audit hook records it as `unregistered_reads`, so an omission appears
in the record rather than as a silence.

### Statement of need

Six months on, the question *"which data made this, with which code,
and does it still hold?"* is frequently unanswerable. A hand-maintained log describes what an
author believed they did; it cannot describe what the program did. The failure that produced
this package was of that kind: a step logged the hash of its own output and nothing about the
corpus it was drawn from, so its input moved four times without the record being able to say
so.

Workflow engines address this for pipelines already formalised into rules. They cannot address
the exploratory phase, where the shape of the analysis is the unknown and the numbers that
reach a manuscript are produced. `runprov` is adoptable there and composes
with an engine afterwards: `runprov exec` returns the wrapped command's exit code, so it drops
inside a Snakemake rule or a Nextflow process.

### State of the field

**Provenance written into a scientific artifact is not new** — philology calls the device a
*sphragis* — and the modern instances are the ones a reviewer will raise, so they are conceded
here in full. SAM/BAM headers have carried `@PG` — program, version and exact command line, chained
through `PP` — for over a decade; `@SQ M5` records the MD5 of the reference sequence; VCF has
`##source`. Those record *what ran*, not *what was read*: with no digest of the inputs beyond
the reference, a `@PG` chain cannot be re-derived to decide whether a result still holds. They
come with no checker.

**And they are per format, which in practice is the binding limit:** these conventions cover
alignment and variant files and nothing else. Across the six analysis repositories of the
virology platform this package was written for, of **903,463** data artifacts **none** is in a
format carrying an in-band provenance convention — zero SAM, BAM, CRAM, VCF or BCF. That
corpus is GenBank flat files (692,435), CSV (201,183), PNG (4,212), JSON, HTML, FASTA, TSV and
parquet; the count, its exclusions and the command producing it are in `CENSUS.md`. A group whose
pipelines end in BAM would count differently: `@PG` is not inadequate, but its coverage is a
property of the formats a group happens to use — and here it is zero.

**Nor is the general problem new.** Sumatra [@sumatra] and noWorkflow [@noworkflow] are both
actively maintained and address it directly; noWorkflow captures substantially more, including
internal dataflow, by instrumenting the abstract syntax tree. `recipy` patches libraries to
the same observational end. DVC versions data rather than recording what a script read.

**MLflow is frequently suggested and answers a different question.** It records what a user
declares — `log_param`, `log_metric`, `log_artifact` — with framework-specific autologging;
it does not observe arbitrary file reads, and
`log_artifact` copies a file *into* the tracking store rather than describing it where it
lies. That is the inverse of this design and the right one for comparing many training runs.
The two compose.

The contribution here is narrower, and is a property of the **record** rather than the
capture:

> The record does not depend on the tool that wrote it.

Sumatra keeps records in a Berkeley DB (pickled Python objects) or SQLite; noWorkflow in
SQLite; recipy in MongoDB — each a store to be queried.
`runprov`'s history is a UTF-8 YAML log and a JSON-lines file, and its per-artifact pin is a
comment block inside the artifact. The consequence is testable: with the package uninstalled
and no Python present, `cat` reads the history, `grep` finds every run that touched a file,
and `sha256sum -c` verifies the recorded digests.

On restricted infrastructure — an air-gapped machine, a five-year-old result, a departed
author — a record that needs its own software to be read carries that software's lifetime as
an expiry date. This package trades capability for
durability, deliberately.

### Acknowledgements

This work was carried out within the National Reference Center for Viral Hepatitis B, C and D
(Hôpital Henri-Mondor, AP-HP, Créteil), designated by Santé publique France; the first
author's post is funded by that centre's budget. We thank Professor Christophe Rodriguez,
thesis director, and the whole Plateforme GenoBioMICS team (DMU Biologie-Pathologie) for the
infrastructure and working context in which this tool was written, and Professor Jean-Michel
Pawlotsky's team at INSERM U955 for the setting its requirements came from — the failure that
produced it was met in that group's own pipeline.
