# Copyright (c) 2026 Assistance Publique – Hôpitaux de Paris (AP-HP),
#                    Hôpital Henri-Mondor, and Taylor Thompson
# SPDX-License-Identifier: BSD-3-Clause
# Licensed under the BSD 3-Clause License — see LICENSE.
"""The record, in formats other people's tools already read. See ADR-0009.

WHY THIS EXISTS. Everything this package writes is its own: `runs.jsonl`, the sidecar, the
pin, `transformation_log.yml`. That is right for a format whose job is to be readable by a
person and greppable in three years — and it means a repository, a registry or a reviewer has
to be taught to read it, which most of them will not be. Two formats they already read:

  RO-CRATE 1.1  a JSON-LD packaging convention built on schema.org. Zenodo and WorkflowHub
                ingest it, which makes this the format that matters at DEPOSIT: the record
                arrives with the data instead of being described in a README nobody parses.
  PROV-JSON     the W3C provenance data model, in the JSON serialisation defined by `The
                PROV-JSON Serialization` -- a W3C MEMBER SUBMISSION of 24 April 2013, which
                is neither a Note nor a Recommendation. This said "its own note" until
                2026-09-11; `/TR/prov-json/` is a 404, and the distinction is one a reviewer
                in this field makes without effort.
                Entities, activities, agents, and the four relations between them.
                Fewer tools consume it and it is the one a reviewer recognises, because it is
                the vocabulary the field agreed on.

NOTHING IS REPLACED. Export is READ-ONLY and additive: it reads records this package already
wrote and emits a second description beside them. `runs.jsonl`, the sidecars, the YAML twins
and `transformation_log.yml` are untouched and remain the record of truth — a derived view
that could rewrite its source would be a strange thing for a provenance tool to ship.

BOTH SCOPES, because they answer different questions. A WHOLE HISTORY is what you deposit:
every run, every artifact, the lineage between them. ONE RUN, from its sidecar, is what you
attach to a submitted artifact — and it is the only one available to somebody holding a file
and its sidecar, with no history to read.

NO DEPENDENCIES, and that is why PROV-JSON rather than PROV-O in Turtle or RDF/XML: those want
an RDF library. Both formats here are plain JSON, and `json` is in the standard library.
"""

from __future__ import annotations

__all__: list[str] = []

import json
import typing

#: The RO-Crate version this conforms to, named rather than implied. A crate that does not
#: say which version it follows is one a validator has to guess about.
RO_CRATE_CONTEXT = "https://w3id.org/ro/crate/1.1/context"
RO_CRATE_SPEC = "https://w3id.org/ro/crate/1.1"

#: The prefixes PROV-JSON needs. `prov` is the model itself; `runprov` is where this
#: package's own identifiers live, so nothing it invents can collide with a standard term.
PROV_PREFIX = {"prov": "http://www.w3.org/ns/prov#", "runprov": "https://runprov.invalid/ns#"}

FORMATS = ("ro-crate", "prov")


def _files(records: typing.Iterable[dict[str, typing.Any]]) -> dict[str, dict[str, typing.Any]]:
    """Every distinct file any run read or wrote, keyed by the path the record spells.

    KEYED ON THE RECORDED SPELLING, which is why T-08 mattered: two runs that named one file
    two ways would appear here as two files, and the lineage between them would not join.
    Since every recorded path is `_posix`, one file is one key on every platform.
    """
    out: dict[str, dict[str, typing.Any]] = {}
    for rec in records:
        for role in ("inputs", "outputs"):
            for entry in rec.get(role, []) or []:
                path = entry.get("path")
                if not path:
                    continue
                # THE FULLEST DESCRIPTION WINS. A history line trims an input to path and
                # digests; the sidecar keeps `size_bytes` and `mtime_utc`. Exporting a mix of
                # both should give the reader the better of the two, not whichever came last.
                if len(entry) > len(out.get(path, {})):
                    out[path] = entry
    return out


def to_ro_crate(
    records: typing.Sequence[dict[str, typing.Any]], name: str = "runprov record"
) -> dict[str, typing.Any]:
    """An RO-Crate 1.1 metadata document describing these runs and their files.

    THE SHAPE IS THE STANDARD'S, not this package's. A crate is a flat `@graph` of entities
    that refer to each other by `@id`, with two required descriptors at the top: the metadata
    file itself, and the root Dataset. Each run becomes a `CreateAction` — schema.org's verb
    for "something made something" — with `object` for what it consumed, `result` for what it
    produced and `instrument` for what did it. That is the closest true mapping; a
    `SoftwareApplication` per script is named as the instrument rather than invented as an
    agent, because the script is a tool and the person is not in the record.
    """
    graph: list[dict[str, typing.Any]] = [
        {
            "@id": "ro-crate-metadata.json",
            "@type": "CreativeWork",
            "conformsTo": {"@id": RO_CRATE_SPEC},
            "about": {"@id": "./"},
        }
    ]
    files = _files(records)
    root_parts = [{"@id": p} for p in sorted(files)]
    actions = [{"@id": f"#run-{r.get('run_uid', i)}"} for i, r in enumerate(records)]
    graph.append(
        {
            "@id": "./",
            "@type": "Dataset",
            "name": name,
            "description": (
                "Provenance recorded by runprov: what each script actually read, wrote and "
                "ran. The record of truth is the runprov history beside this file; this is a "
                "derived view."
            ),
            "hasPart": root_parts,
            "mentions": actions,
        }
    )
    for path in sorted(files):
        entry = files[path]
        node: dict[str, typing.Any] = {"@id": path, "@type": "File"}
        if entry.get("sha256"):
            # `sha256`, NOT `content_digest`. A crate is read by tools that will check it
            # against the bytes on disk, and `content_digest` deliberately ignores line
            # endings -- true of the DATA and false of the file, which is the wrong promise
            # to make to a validator.
            node["sha256"] = entry["sha256"]
        if isinstance(entry.get("size_bytes"), int):
            node["contentSize"] = entry["size_bytes"]
        if entry.get("mtime_utc"):
            node["dateModified"] = entry["mtime_utc"]
        if entry.get("kind") and entry["kind"] != "file":
            # MISSING and UNHASHABLE are findings, and a crate that dropped them would be a
            # tidier description of a worse situation.
            node["description"] = f"runprov: {entry['kind']}"
        graph.append(node)
    for i, rec in enumerate(records):
        script = rec.get("script", "?")
        instrument = f"#script-{script}"
        graph.append(
            {
                "@id": f"#run-{rec.get('run_uid', i)}",
                "@type": "CreateAction",
                "name": script,
                "startTime": rec.get("started_utc"),
                "endTime": rec.get("finished_utc"),
                "actionStatus": (
                    "http://schema.org/CompletedActionStatus"
                    if rec.get("status") == "ok"
                    else "http://schema.org/FailedActionStatus"
                ),
                "instrument": {"@id": instrument},
                "object": [
                    {"@id": e["path"]} for e in rec.get("inputs", []) or [] if e.get("path")
                ],
                "result": [
                    {"@id": e["path"]} for e in rec.get("outputs", []) or [] if e.get("path")
                ],
            }
        )
        graph.append({"@id": instrument, "@type": "SoftwareApplication", "name": script})
    # DEDUPLICATED BY `@id`, because two runs of one script name the same instrument and a
    # crate with a repeated `@id` is invalid rather than merely redundant.
    seen: dict[str, dict[str, typing.Any]] = {}
    for node in graph:
        seen.setdefault(node["@id"], node)
    return {"@context": RO_CRATE_CONTEXT, "@graph": list(seen.values())}


def to_prov_json(records: typing.Sequence[dict[str, typing.Any]]) -> dict[str, typing.Any]:
    """A PROV-JSON document: entities, activities, agents, and what relates them.

    FOUR RELATIONS AND NO MORE, because those four are what this package can honestly assert:
    `used` (a run read a file), `wasGeneratedBy` (a run wrote one), `wasAssociatedWith` (a run
    was run by something) and `wasDerivedFrom` (an output came from an input OF THAT RUN).

    THE DERIVATION IS PER-RUN AND THAT IS THE LIMIT WORTH KNOWING. `wasDerivedFrom` here means
    "this output was produced by a run that read this input", not "this value came from that
    value". PROV allows the finer claim and this package cannot make it: it records what was
    opened, not what was used. Saying more in a standard vocabulary than the record supports
    would be the exact failure this project exists to refuse -- and it would be harder to
    catch, because it would be well-formed.
    """
    entities: dict[str, typing.Any] = {}
    activities: dict[str, typing.Any] = {}
    agents: dict[str, typing.Any] = {}
    used: dict[str, typing.Any] = {}
    generated: dict[str, typing.Any] = {}
    associated: dict[str, typing.Any] = {}
    derived: dict[str, typing.Any] = {}

    for path, entry in _files(records).items():
        node: dict[str, typing.Any] = {"prov:label": path, "runprov:path": path}
        if entry.get("sha256"):
            node["runprov:sha256"] = entry["sha256"]
        if isinstance(entry.get("size_bytes"), int):
            node["runprov:size_bytes"] = entry["size_bytes"]
        entities[f"runprov:file/{path}"] = node

    for i, rec in enumerate(records):
        act = f"runprov:run/{rec.get('run_uid', i)}"
        activities[act] = {
            "prov:label": rec.get("script", "?"),
            "prov:startTime": rec.get("started_utc"),
            "prov:endTime": rec.get("finished_utc"),
            "runprov:status": rec.get("status"),
            "runprov:run_id": rec.get("run_id"),
        }
        agent = f"runprov:script/{rec.get('script', '?')}"
        agents[agent] = {"prov:type": "prov:SoftwareAgent", "prov:label": rec.get("script", "?")}
        associated[f"{act}/assoc"] = {"prov:activity": act, "prov:agent": agent}
        ins = [e["path"] for e in rec.get("inputs", []) or [] if e.get("path")]
        outs = [e["path"] for e in rec.get("outputs", []) or [] if e.get("path")]
        for n, path in enumerate(ins):
            used[f"{act}/used/{n}"] = {"prov:activity": act, "prov:entity": f"runprov:file/{path}"}
        for n, path in enumerate(outs):
            generated[f"{act}/gen/{n}"] = {
                "prov:activity": act,
                "prov:entity": f"runprov:file/{path}",
            }
        for a, out in enumerate(outs):
            for b, src in enumerate(ins):
                derived[f"{act}/der/{a}-{b}"] = {
                    "prov:generatedEntity": f"runprov:file/{out}",
                    "prov:usedEntity": f"runprov:file/{src}",
                    "prov:activity": act,
                }

    doc: dict[str, typing.Any] = {"prefix": PROV_PREFIX}
    # EMPTY SECTIONS ARE OMITTED, not written as `{}`. A PROV document that declares it has
    # entities and lists none says something different from one that does not mention them.
    for key, section in (
        ("entity", entities),
        ("activity", activities),
        ("agent", agents),
        ("used", used),
        ("wasGeneratedBy", generated),
        ("wasAssociatedWith", associated),
        ("wasDerivedFrom", derived),
    ):
        if section:
            doc[key] = section
    return doc


def render(
    records: typing.Sequence[dict[str, typing.Any]], fmt: str, name: str = "runprov record"
) -> str:
    """One of `FORMATS`, as the text to write. Raises `ValueError` for anything else."""
    if fmt == "ro-crate":
        return json.dumps(to_ro_crate(records, name), indent=2, sort_keys=False) + "\n"
    if fmt == "prov":
        return json.dumps(to_prov_json(records), indent=2, sort_keys=False) + "\n"
    raise ValueError(f"unknown export format {fmt!r}; choose from {', '.join(FORMATS)}")


def default_filename(fmt: str) -> str:
    """What the format's own convention calls the file.

    `ro-crate-metadata.json` IS THE SPEC'S NAME and is not negotiable: a crate is recognised
    by that filename sitting beside the data. PROV-JSON has no such convention, so the name
    only has to say what it is.
    """
    return "ro-crate-metadata.json" if fmt == "ro-crate" else "prov.json"
