---
name: Feature request
about: Something the package should record, or read back, and does not
title: ''
labels: enhancement
assignees: ''
---

**An honest warning before you spend time on this:** the answer to a feature request is
often "not soon". The package has no runtime dependencies and a deliberately small surface,
and both of those get harder to keep with every addition. That is not a reason to stay
quiet — knowing what people need is how the scope gets decided — it is a reason not to be
surprised.

## What are you trying to find out about a run?

<!-- The QUESTION, not the API. "Which commit produced this figure" is a question;
     "add a `commit` argument" is a guess at the answer, and often not the best one. -->

## What do you do today instead

<!-- Including "nothing, I gave up" — that is a useful data point. -->

## Would this belong in the record, or in a tool that reads it?

<!-- The record format is the contract; the CLI is one reader of it. Plenty of good ideas
     are a script over `runs.jsonl` rather than a change to what is written. -->

## Environment, if it is relevant

- runprov version:
- What runs your pipeline (plain scripts / Snakemake / Nextflow / Make / a scheduler):
