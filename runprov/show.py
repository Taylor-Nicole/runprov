# Copyright (c) 2026 Assistance Publique – Hôpitaux de Paris (AP-HP),
#                    Hôpital Henri-Mondor, and Taylor Thompson
# Licensed under the CeCILL-B Free Software License Agreement — see LICENSE.
# https://cecill.info/
"""The lab notebook the history already contained: one view per run, one per project.

`log` is a timeline and `lineage` is a graph, and neither answers the question a developer
actually has three weeks in — **which script expects which input, and does the thing I need
already exist?** In a project with many data sources that question is answered today by
opening several files and reconstructing it, which is the work this is meant to remove.

WHAT IT IS FOR, concretely. Building an artifact can take hours, so the expensive mistake is
not a lost record, it is REBUILDING something that was already correct because nobody could
tell. The project view is an index of what has been made, by what, from what, and whether it
still holds — so the answer to "do I need to run this again" is a page rather than an
excavation.

A READER AND NOTHING ELSE. It adds no field, writes no file and changes no record; it reads
`runs.jsonl` and renders it. That is deliberate: a view that could alter what it displays
would be a view you have to trust, and the record is the thing being trusted here.

TEXT AND YAML, not HTML. The audience runs this over ssh in a terminal beside the work, many
times a day, and a browser is a worse place to read it from than the shell it was launched
in. YAML because the fields are the same ones `log --format yaml` emits, so anything that
reads one reads the other.
"""

from __future__ import annotations

import collections
import json
import pathlib
import typing

#: How many characters of a digest identify an artifact version to a human. The same 16 the
#: pin uses, so a digest read here can be matched against a digest read in an artifact.
SHORT = 16

#: How many distinct input versions to name before summarising. A script that has read forty
#: versions of one file has a fact worth stating and a list not worth printing.
VERSIONS_SHOWN = 3


def _short(entry: dict[str, typing.Any]) -> str:
    """The digest a human compares. Content first, like the pin — see `hashing.pin_digest`."""
    return str(
        entry.get("content_sha256") or entry.get("sha256") or entry.get("sha256_tree") or "-"
    )[:SHORT]


def _name(entry: dict[str, typing.Any]) -> str:
    return str(entry.get("path", "?"))


def run_view(record: dict[str, typing.Any]) -> dict[str, typing.Any]:
    """One run, flattened to what a person asks about it."""
    code = {
        "commit": record.get("git_commit") or "none",
        "dirty": bool(record.get("git_code_dirty")),
        # `is False`, never falsy: a run that PREDATES the field does not know the answer,
        # and rendering "clean" for it would be the reassuring lie the flag exists to stop.
        "unknown": record.get("git_status_captured") is False,
    }
    return {
        "run": record.get("script", "?"),
        "status": record.get("status", "ok"),
        "started": record.get("started_utc", "?"),
        "finished": record.get("finished_utc", "?"),
        "run_id": record.get("run_id", "?"),
        "run_uid": str(record.get("run_uid", ""))[:12],
        "generation": record.get("generation", "?"),
        "script_file": record.get("script_file"),
        "code": code,
        "command": record.get("command"),
        "cwd": record.get("cwd"),
        "parameters": record.get("parameters") or {},
        "inputs": [{"path": _name(i), "digest": _short(i)} for i in record.get("inputs") or []],
        "outputs": [
            {"path": _name(o), "digest": _short(o), "kind": o.get("kind", "file")}
            for o in record.get("outputs") or []
        ],
        "notes": record.get("notes") or {},
        "seeds": record.get("seeds") or [],
        "failure": record.get("failure"),
        "terminal_log": (record.get("terminal_log") or {}).get("path"),
    }


def project_view(records: list[dict[str, typing.Any]]) -> dict[str, typing.Any]:
    """The notebook: every script, what it reads, what it writes, and what exists now.

    Built per SCRIPT rather than per run, because "which inputs does this expect" is a
    question about the script and the history is the only place the answer was ever written
    down. A script that has read three different files across ten runs has three answers,
    and all three are worth seeing at once.
    """
    scripts: dict[str, dict[str, typing.Any]] = {}
    artifacts: dict[str, dict[str, typing.Any]] = {}

    for rec in records:
        name = rec.get("script", "?")
        s = scripts.setdefault(
            name,
            {
                "runs": 0,
                "ok": 0,
                "failed": 0,
                "first": rec.get("started_utc", "?"),
                "last": rec.get("started_utc", "?"),
                "script_file": rec.get("script_file"),
                "inputs": collections.defaultdict(set),
                "outputs": collections.defaultdict(set),
                "note_keys": set(),
                "parameters": set(),
            },
        )
        s["runs"] += 1
        s["ok" if rec.get("status", "ok") == "ok" else "failed"] += 1
        s["last"] = rec.get("started_utc", s["last"])
        s["script_file"] = rec.get("script_file") or s["script_file"]
        s["note_keys"].update((rec.get("notes") or {}).keys())
        s["parameters"].update((rec.get("parameters") or {}).keys())

        for i in rec.get("inputs") or []:
            s["inputs"][_name(i)].add(_short(i))
        for o in rec.get("outputs") or []:
            s["outputs"][_name(o)].add(_short(o))
            # LAST WRITER WINS, and the records are in order, so this ends up naming the run
            # that actually produced what is on disk now.
            artifacts[_name(o)] = {
                "by": name,
                "digest": _short(o),
                "kind": o.get("kind", "file"),
                "when": rec.get("started_utc", "?"),
                "status": rec.get("status", "ok"),
            }

    for s in scripts.values():
        s["inputs"] = {k: sorted(v) for k, v in sorted(s["inputs"].items())}
        s["outputs"] = {k: sorted(v) for k, v in sorted(s["outputs"].items())}
        s["note_keys"] = sorted(s["note_keys"])
        s["parameters"] = sorted(s["parameters"])

    return {
        "runs": len(records),
        "scripts": dict(sorted(scripts.items())),
        "artifacts": dict(sorted(artifacts.items())),
    }


# ------------------------------------------------------------------ rendering
def _rule(title: str, width: int = 78) -> str:
    return f"── {title} " + "─" * max(0, width - len(title) - 4)


def _kv(key: str, value: object, pad: int = 12) -> str:
    return f"  {key:<{pad}} {value}"


def render_run(view: dict[str, typing.Any]) -> str:
    """One run, as a page. The order is the order the questions get asked in."""
    mark = "ok" if view["status"] == "ok" else view["status"].upper()
    out = [_rule(f"{view['run']}  [{mark}]"), ""]
    out.append(_kv("started", view["started"]))
    out.append(_kv("finished", view["finished"]))
    out.append(_kv("run_id", f"{view['run_id']}   generation {view['generation']}"))
    if view["run_uid"]:
        out.append(_kv("run_uid", view["run_uid"]))

    code = view["code"]
    state = (
        "DIRTY"
        if code["dirty"]
        else ("UNKNOWN — git status did not run" if code["unknown"] else "clean")
    )
    out.append(_kv("code", f"{code['commit']}  ({state})"))
    if view["script_file"]:
        out.append(_kv("script", view["script_file"]))
    if view["cwd"]:
        out.append(_kv("cwd", view["cwd"]))
    if view["command"]:
        out.append(_kv("command", view["command"]))

    if view["failure"]:
        f = view["failure"]
        out += ["", _rule("failure"), ""]
        out.append(_kv("type", f.get("type", "?")))
        out.append(_kv("message", str(f.get("message", ""))[:400]))

    if view["parameters"]:
        out += ["", _rule("parameters"), ""]
        pad = max(len(k) for k in view["parameters"])
        out += [_kv(k, json.dumps(v, default=str), pad) for k, v in view["parameters"].items()]

    for label in ("inputs", "outputs"):
        if view[label]:
            out += ["", _rule(f"{label} ({len(view[label])})"), ""]
            for e in view[label]:
                kind = "" if e.get("kind", "file") == "file" else f"  [{e['kind']}]"
                out.append(f"  {e['digest']}  {e['path']}{kind}")

    if view["notes"]:
        out += ["", _rule("notes"), ""]
        pad = max(len(k) for k in view["notes"])
        out += [_kv(k, json.dumps(v, default=str), pad) for k, v in view["notes"].items()]

    if view["seeds"]:
        out += ["", _kv("seeds", view["seeds"])]
    if view["terminal_log"]:
        out += ["", _kv("terminal", view["terminal_log"])]
    return "\n".join(out) + "\n"


def render_project(view: dict[str, typing.Any]) -> str:
    """The notebook page: what each script reads and writes, then what exists now."""
    out = [_rule(f"project notebook — {view['runs']} run(s), {len(view['scripts'])} script(s)")]

    for name, s in view["scripts"].items():
        status = f"{s['runs']} run(s)"
        if s["failed"]:
            status += f", {s['failed']} FAILED"
        out += ["", f"  {name}    {status}", f"    last {s['last']}    first {s['first']}"]
        if s["script_file"]:
            out.append(f"    file {s['script_file']}")

        if s["inputs"]:
            out.append("    expects:")
            for path, versions in s["inputs"].items():
                # The COUNT of distinct versions is the fact; the list of forty digests is
                # not. A file read at two different digests is the thing worth noticing.
                extra = (
                    f"   [{len(versions)} versions]"
                    if len(versions) > VERSIONS_SHOWN
                    else f"   {' '.join(versions)}"
                )
                out.append(f"      {path}{extra}")
        if s["outputs"]:
            out.append("    writes:")
            for path in s["outputs"]:
                out.append(f"      {path}")
        if s["parameters"]:
            out.append(f"    params:  {', '.join(s['parameters'])}")
        if s["note_keys"]:
            out.append(f"    notes:   {', '.join(s['note_keys'])}")

    if view["artifacts"]:
        out += ["", _rule(f"artifacts on record ({len(view['artifacts'])})"), ""]
        for path, a in view["artifacts"].items():
            flag = "" if a["status"] == "ok" else f"  <- from a {a['status'].upper()} run"
            kind = "" if a["kind"] == "file" else f"  [{a['kind']}]"
            out.append(f"  {a['digest']}  {path}{kind}")
            out.append(f"  {' ' * SHORT}  by {a['by']}  {a['when']}{flag}")
    return "\n".join(out) + "\n"


def to_yaml(obj: object, indent: int = 0) -> str:
    """A nested structure as YAML, with EVERY scalar quoted.

    The same rule as `__main__._yaml`, and for the same measured reason: the predecessor's
    log quoted only what its author thought needed quoting, and one hand-written
    `Note:` inside a description is where `yaml.safe_load_all` dies on the real file — line
    14,554 of 24,300. A quoting rule with exceptions is correct until someone types a colon.
    """
    pad = "  " * indent
    if isinstance(obj, dict):
        if not obj:
            return pad + "{}\n"
        out = []
        for k, v in obj.items():
            if isinstance(v, (dict, list)) and v:
                out.append(f"{pad}{json.dumps(str(k))}:\n{to_yaml(v, indent + 1)}")
            else:
                out.append(f"{pad}{json.dumps(str(k))}: {_scalar(v)}\n")
        return "".join(out)
    if isinstance(obj, list):
        if not obj:
            return pad + "[]\n"
        out = []
        for v in obj:
            if isinstance(v, (dict, list)) and v:
                body = to_yaml(v, indent + 1)
                out.append(f"{pad}-\n{body}")
            else:
                out.append(f"{pad}- {_scalar(v)}\n")
        return "".join(out)
    return pad + _scalar(obj) + "\n"


def _scalar(v: object) -> str:
    """`json.dumps` unconditionally. JSON scalars are valid YAML, so this is total."""
    if isinstance(v, bool) or v is None or isinstance(v, (int, float)):
        return json.dumps(v)
    if isinstance(v, (dict, list)):
        return json.dumps(v, default=str)
    return json.dumps(str(v))


def select(records: list[dict[str, typing.Any]], target: str) -> list[dict[str, typing.Any]]:
    """Runs matching `target`: a script name, a run_uid prefix, a run_id, or an artifact path.

    Four things one argument can mean, resolved by trying each and returning what matched.
    A developer asking about `build_labels` and a developer asking about `results/x.tsv` are
    asking the same question -- what happened here -- and should not have to say which kind
    of name they happen to be holding.
    """
    by_script = [r for r in records if r.get("script") == target]
    if by_script:
        return by_script
    by_uid = [r for r in records if str(r.get("run_uid", "")).startswith(target)]
    if by_uid:
        return by_uid
    by_run_id = [r for r in records if r.get("run_id") == target]
    if by_run_id:
        return by_run_id
    return [
        r
        for r in records
        if any(
            _name(e) == target or pathlib.Path(_name(e)).name == target
            for e in (r.get("outputs") or []) + (r.get("inputs") or [])
        )
    ]
