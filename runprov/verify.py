# Copyright (c) 2026 Assistance Publique – Hôpitaux de Paris (AP-HP),
#                    Hôpital Henri-Mondor, and Taylor Thompson
# Licensed under the CeCILL-B Free Software License Agreement — see LICENSE.
# https://cecill.info/
"""Does each artifact still match what it was made from? The half that was missing.

The package's claim is that a result can be **invalidated** when its inputs change. Every
piece needed for that was already here — the pin is deterministic, the digests are in the
artifact, the names are root-relative — and nothing read them back. The claim was a
property of the FORMAT and not of the PRODUCT: `log` and `lineage` tell you what happened,
and neither tells you whether what happened is still true. A checker that lives in another
repository is a checker most users do not have.

WHAT IT CHECKS, precisely: for every input listed in an artifact's pin, re-derive that
input's digest now and compare it to the digest the artifact carries. It does NOT re-run
anything, does not compare the artifact against itself, and cannot tell you the artifact is
correct — only whether the things it was made from still hash the way they did when it was
made. That is the question staleness actually is.

TRANSITIVITY COMES FREE, and it is the property worth having. Because a pin lands in the
artifact and a downstream step registers that artifact as an input, a changed root shows up
at every level that depends on it — and a grandchild stays stale after the root is restored,
because the child was never rebuilt. Nothing here implements that; it falls out of the pin
being in the bytes.

WHAT IT REFUSES TO GUESS. Three shapes are reported UNVERIFIABLE rather than assumed good,
because each is a case where a comparison would be meaningless and a green result would be
a lie: a name outside the project root (`<external>/…`, deliberately not a path), a name
carrying an escape from `_safe_for_pin` (a file named `a\\nb` and a file named `a<LF>b`
render identically, so there is no unambiguous way back), and an input the run recorded no
digest for (`MISSING`). Green must mean checked.

AND IT WILL NOT PASS HAVING CHECKED NOTHING. Zero pins found is a non-zero exit with a
message saying so. "We could not look" rendering as the reassuring answer is the failure
this package refuses everywhere else — `git_status_captured: false` exists for the same
reason — and a CI gate that goes green over a directory whose artifacts carry no pins is
worse than no gate, because someone will trust it.
"""

from __future__ import annotations

import json
import os
import pathlib
import re
import typing

from .hashing import PIN_DIGEST_CHARS, PIN_SIDECAR_SUFFIX, describe, pin_digest

# The first line of every pin, and the only thing that identifies one. Matched as a
# SUBSTRING so the caller's comment marker -- `# `, `## `, `; `, whatever the format needs
# -- is whatever precedes it on that line, rather than something this reader has to know.
ANCHOR = "provenance — this artifact and what produced it"

# Pins are written at the top of an artifact (`open_output` writes the header first), so
# reading the whole file to find one would mean reading every byte of a 50 GB BAM to learn
# it has no pin. Bounded, and stated: a pin further in than this is not found.
SCAN_BYTES = 1 << 16

#: How far into a file the FIRST pin block may begin before the file stops counting as an
#: artifact. See `read_pins` for the failure this closes.
PIN_STARTS_WITHIN = 4

#: Directories `collect()` does not walk. None of them holds artifacts a run produced, and
#: two of them actively produce false positives: a virtualenv contains this package's own
#: source and the wheel METADATA, both of which quote the pin format. Counted, never
#: silently dropped -- `verify` reports how many files it skipped, because a checker that
#: quietly narrows what it looked at is the failure this package exists to refuse.
SKIP_DIRS = frozenset(
    {
        ".bzr",
        ".git",
        ".hg",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".svn",
        ".tox",
        ".venv",
        "__pycache__",
        "build",
        "dist",
        "env",
        "node_modules",
        "site-packages",
        "venv",
    }
)

# Against the pin's own rendering: four spaces, the digest, TWO spaces, the name. The
# two-space separator is what allows a name to contain single spaces.
_ENTRY = re.compile(rf"^ {{4}}([0-9a-f]{{{PIN_DIGEST_CHARS}}}|MISSING) {{2}}(.+)$")
#: BOTH SPELLINGS, and the old one is not deprecated so much as wrong: artifacts written
#: before 2026-08-19 say `sha256:` where the value is a CONTENT digest. A reader that
#: accepted only the new word would report every one of them as unpinned, which is a worse
#: outcome than the mislabelling it corrects.
_COUNT = re.compile(r"^ {2}inputs \((\d+)\), (?:sha256|content digest):$")
_FIELD = re.compile(r"^ {2}(script|generation|commit) +: (.*)$")
# TWO LEADING SPACES, like every other body line. Without them this never matched, so a
# pin stating NONE REGISTERED fell through to "a pin with no entries" and read as
# UNVERIFIABLE -- turning the one case where a run explicitly says it read nothing into
# the case where we do not know what it read. Found by the test, not by review.
_NONE = "  inputs     : NONE REGISTERED"

OK = "OK"
STALE = "STALE"
GONE = "GONE"
UNVERIFIABLE = "UNVERIFIABLE"
NO_PIN = "NO PIN"

#: Every state this checker can report. `show` imports the four it shares — see the note on
#: its own vocabulary — and the two modules differ by exactly the two states only one source
#: of evidence can support: `MODIFIED` needs the artifact's own recorded digest, which lives
#: in the history and never in a pin; `NO PIN` needs the bytes.
STATES = frozenset({OK, STALE, GONE, UNVERIFIABLE, NO_PIN})

#: Statuses that mean the artifact cannot be trusted as current. `GONE` is included
#: deliberately: an input that no longer exists cannot be compared, but it is not a neutral
#: absence either -- the artifact can no longer be re-derived, which is a finding.
FAILING = (STALE, GONE)


#: A top-level JSON key whose value could be a `output_json` pin: `"name": {`, at the start
#: of the document. Not anchored to the default key name, because `output_json(key=...)` lets
#: a caller choose one their readers ignore — a checker that only knew `_provenance` would
#: silently stop recognising pins the moment somebody used that documented parameter.
_JSON_KEY = re.compile(r'"([^"\\]{1,64})"\s*:\s*\{')


def _json_pin(head: str) -> dict[str, typing.Any] | None:
    """A `output_json` pin, read from the structure rather than from prose. None if absent.

    `output_json` embeds the pin as a top-level KEY because JSON has no comment syntax — the
    same `pin_digest` values as an in-band pin, stored as data so a consumer does not have to
    parse prose out of a string. `read_pins` knew only the text anchor, so `verify` reported a
    directory of `output_json` artifacts as carrying no pin at all and exited 1 with NOTHING
    CHECKED. Two features added in the same session that could not see each other.

    BOUNDED like the rest of this reader: only `head` is examined, and only top-level keys
    near the start of it. `output_json` writes the pin first (`{key: pin, **payload}`), so a
    pin that is not in the first `SCAN_BYTES` is a file this function is right not to trust.

    Identified by SHAPE, not by name: a mapping carrying `script` and a list of
    `[digest, name]` pairs under `inputs`. That is what makes a caller's own `key=` work, and
    it is also what stops an ordinary JSON document with a `"config": {...}` key being read as
    a pin.
    """
    if not head.lstrip().startswith("{"):
        return None
    decoder = json.JSONDecoder()
    for m in _JSON_KEY.finditer(head):
        try:
            value, _ = decoder.raw_decode(head, m.end() - 1)
        except ValueError:  # guards-ok: a value truncated by SCAN_BYTES, or not JSON at all
            continue
        if not isinstance(value, dict) or "script" not in value:
            continue
        entries = value.get("inputs")
        if not isinstance(entries, list):
            continue
        pairs = [
            (str(e[0]), str(e[1])) for e in entries if isinstance(e, (list, tuple)) and len(e) == 2
        ]
        if len(pairs) != len(entries):
            continue
        fields = {k: str(v) for k, v in value.items() if k in ("script", "generation", "commit")}
        # `declared` is the length of the list itself, not a separate count. The in-band pin
        # states a number so a truncated block can be caught; a JSON array cannot be
        # truncated without the document failing to parse, so the two agree by construction.
        return {"entries": pairs, "declared": len(pairs), "fields": fields}
    return None


def read_pins(path: pathlib.Path) -> list[dict[str, typing.Any]]:
    """EVERY pin block in an artifact's first `SCAN_BYTES`, in the order they appear.

    Every, not the first, and that is where transitivity comes from. A step that reads an
    upstream artifact and copies its lines through carries the upstream pin in its own
    bytes — so `final.tsv` states both "I was made from mid.tsv@6a19…" and, inherited,
    "…which was made from in.tsv@e854…". Reading only the first block would check one
    generation and silently ignore a claim the artifact is making about its grandparent.

    Decoded with `errors="replace"`, so a file that is not UTF-8 does not raise — it simply
    fails to contain the anchor and reads as unpinned. That is the honest outcome rather
    than a guess: every file `runprov` writes is UTF-8 by construction, and a pin recovered
    from a mis-decoded artifact would be a pin whose digests we could not trust anyway.
    """
    try:
        with path.open("rb") as fh:
            head = fh.read(SCAN_BYTES).decode("utf-8", errors="replace")
    except OSError:  # guards-ok: unreadable reads as unpinned; the caller reports the path
        return []

    if (json_pin := _json_pin(head)) is not None:
        return [json_pin]

    lines = head.splitlines()
    blocks: list[dict[str, typing.Any]] = []
    for start, anchored in enumerate(lines):
        if ANCHOR not in anchored:
            continue
        # A PINNED ARTIFACT DECLARES ITSELF AT THE TOP; a file that merely MENTIONS the
        # format is not an artifact. Without this the anchor was matched anywhere in the
        # first 64 KiB, so `runprov verify` over a project reported this package's own
        # `verify.py` (which holds the anchor as a constant), `run.py` (which renders it),
        # their `.pyc` files, and the wheel's METADATA -- and METADATA embeds the README's
        # EXAMPLE pin, so the check invented a GONE for `data/labels.tsv`, a path that
        # exists only in documentation. A gate that fails because the docs describe the
        # format is worse than no gate.
        #
        # A few lines of tolerance rather than exactly line 1: `open_output` writes the pin
        # first, but a caller placing `header()` by hand may put a shebang or an encoding
        # declaration above it. An INHERITED pin further down is still read -- that is where
        # transitivity comes from -- it just cannot be the one that makes the file count.
        if not blocks and start >= PIN_STARTS_WITHIN:
            continue
        marker = anchored[: anchored.index(ANCHOR)]
        pin: dict[str, typing.Any] = {"entries": [], "declared": None, "fields": {}}
        for line in lines[start + 1 :]:
            if not line.startswith(marker) or ANCHOR in line:
                break
            body = line[len(marker) :]
            if (m := _ENTRY.match(body)) is not None:
                pin["entries"].append((m.group(1), m.group(2)))
            elif (c := _COUNT.match(body)) is not None:
                pin["declared"] = int(c.group(1))
            elif (f := _FIELD.match(body)) is not None:
                pin["fields"][f.group(1)] = f.group(2)
            elif body.startswith(_NONE):
                pin["declared"] = 0
            # A marker line matching none of these ends the block only once the entries are
            # complete. An EMPTY marker -- `header(comment="")` -- makes every following
            # line of the artifact "start with" it, so without this the reader would walk
            # the whole data section looking for entries it has already found.
            if pin["declared"] is not None and len(pin["entries"]) >= pin["declared"]:
                break
        blocks.append(pin)
    return blocks


def check_input(
    want: str,
    name: str,
    root: pathlib.Path,
    cache: dict[pathlib.Path, str | Exception] | None = None,
) -> dict[str, typing.Any]:
    """One pinned input, re-derived and compared. See the module docstring on refusals.

    `cache` MEMOISES THE DIGEST BY PATH for the lifetime of one `verify()` call, and it is
    the difference between linear and quadratic. The inputs a scientific artifact pins are
    the expensive files -- a reference genome, a BAM, a 40 GB matrix -- and a fan-out of N
    artifacts from one input meant N full reads of it. Measured on 2,000 artifacts sharing
    20 inputs: 40,000 reads over 20 distinct files, 22.5 s, of which almost all was
    re-hashing the same twenty files two thousand times each. The cost begins the moment
    more than one artifact shares an input, which is the normal case, and it is invisible at
    the tens-of-files scale a test suite uses.

    Safe because a path's digest cannot change during a single check -- and if it could,
    re-reading it would make the report self-inconsistent rather than more accurate: two
    artifacts pinning the same input would disagree about what is on disk now.

    Exceptions are cached too, so an unreadable input is reported once per artifact from one
    failed read rather than re-attempted for each.
    """
    out: dict[str, typing.Any] = {"name": name, "pinned": want}
    if name.startswith("<external>/"):
        return {**out, "status": UNVERIFIABLE, "reason": "outside the project root when pinned"}
    if "\\" in name:
        return {**out, "status": UNVERIFIABLE, "reason": "escaped name — no unambiguous path"}
    if want == "MISSING":
        return {**out, "status": UNVERIFIABLE, "reason": "the run recorded no digest for it"}

    target = root / name
    if not target.exists():
        return {**out, "status": GONE, "reason": "the pinned input is no longer there"}
    if cache is not None and target in cache:
        got_or_exc = cache[target]
    else:
        try:
            got_or_exc = pin_digest(describe(target))
        except (OSError, ValueError) as exc:  # unreadable now, or a FIFO where a file was
            got_or_exc = exc
        if cache is not None:
            cache[target] = got_or_exc
    if isinstance(got_or_exc, Exception):
        return {**out, "status": UNVERIFIABLE, "reason": str(got_or_exc)}
    return {**out, "status": OK if got_or_exc == want else STALE, "found": got_or_exc}


def verify_artifact(
    path: pathlib.Path,
    root: pathlib.Path,
    cache: dict[pathlib.Path, str | Exception] | None = None,
) -> dict[str, typing.Any]:
    """One artifact: every pin in it, every input in those, and a status for the whole.

    A `.prov.txt` IS NOT THE ARTIFACT, it speaks for the file beside it. Checked as though it
    were, a sidecar whose artifact had been deleted reported `OK` and exited 0 — the whole
    point of the pin surviving separately, inverted into a green result over a file that is
    not there. It is also the wrong name to print: the report said `out.png.prov.txt` and
    never mentioned `out.png`, so even the passing case named a file the user did not make.
    """
    blocks = read_pins(path)
    if not blocks:
        return {"artifact": str(path), "status": NO_PIN, "inputs": []}

    speaks_for = None
    if path.name.endswith(PIN_SIDECAR_SUFFIX):
        speaks_for = path.with_name(path.name[: -len(PIN_SIDECAR_SUFFIX)])
        if not speaks_for.exists():
            # GONE, not UNVERIFIABLE: nothing here is ambiguous. The sidecar names its
            # artifact, the artifact is absent, and that is a finding a checker exists to
            # make. GONE is in FAILING, so the exit code follows.
            return {
                "artifact": str(speaks_for),
                "status": GONE,
                "reason": f"the artifact is no longer there; its pin survives in {path.name}",
                "inputs": [],
                "pins": len(blocks),
                "scripts": [b["fields"].get("script", "?") for b in blocks],
            }

    checked, truncated, scripts = [], [], []
    for pin in blocks:
        # Which step's claim this input belongs to. An inherited pin names an UPSTREAM
        # script, so without this a stale grandparent reads as a defect in the artifact's
        # own step and the reader goes looking in the wrong place.
        via = pin["fields"].get("script", "?")
        scripts.append(via)
        checked += [{**check_input(s, n, root, cache), "via": via} for s, n in pin["entries"]]

        # A pin saying `inputs (4)` above three entries has been truncated or edited, and
        # the three that survived agreeing proves nothing about the fourth. A fact about
        # the PIN, so it is reported on the artifact rather than against any input in it.
        declared, found = pin["declared"], len(pin["entries"])
        if declared is not None and found != declared:
            truncated.append(f"{via}: declares {declared} input(s), carries {found}")

    statuses = {c["status"] for c in checked}
    declared_none = all(p["declared"] == 0 for p in blocks)
    if statuses & set(FAILING) or truncated:
        status = STALE if (statuses & {STALE} or truncated) else GONE
    elif statuses == {OK} or (not checked and declared_none):
        # `declared == 0` is the NONE REGISTERED pin: the run stated it read nothing, which
        # is a checkable claim and it checks out. An EMPTY entry list with no such
        # statement is not the same thing and falls through to unverifiable below.
        status = OK
    else:
        status = UNVERIFIABLE

    out: dict[str, typing.Any] = {
        # The ARTIFACT's name, not the sidecar's. A reader checking `out.png` should see
        # `out.png` in the report; where the pin happened to live is the checker's business.
        "artifact": str(speaks_for if speaks_for is not None else path),
        "status": status,
        "inputs": checked,
        "pins": len(blocks),
        "scripts": scripts,
    }
    if truncated:
        out["pin_truncated"] = truncated
    return out


def collect(paths: typing.Iterable[pathlib.Path]) -> tuple[list[pathlib.Path], int]:
    """Files to examine, and how many DIRECTORIES were skipped. See `SKIP_DIRS` for why.

    Sorted, so two runs of the same check report in the same order — the same reason the
    pin itself is sorted. Duplicates collapse: naming a file and its parent directory must
    not double-count it.

    A path named EXPLICITLY is always examined, even inside a skipped directory: the skip
    list is about what a bare `verify` should walk, not a claim that those files cannot be
    checked. Asking about one by name is an answerable question and it gets answered.

    DIRECTORIES, NOT FILES, and that fixes three defects in one line. The count used to be
    `rglob("*")` over each pruned tree, which:

      1. WALKED EXACTLY WHAT `SKIP_DIRS` EXISTS NOT TO WALK. The pruning saved the pin reads
         and not the traversal, so the cost was proportional to the size of the tree this
         function had just declared it would not look at — and that tree is the one thing on
         the machine guaranteed to be large. Measured on 2,000 real files beside an 18,000
         entry `.venv`: 0.109 s against 0.007 s. On NFS, where per-entry latency is ~100x
         local, it is the difference between a cheap gate and an unusable one.
      2. COUNTED DIRECTORIES AS FILES. `rglob("*")` yields both, so a number labelled
         "files" over-reported by every subdirectory in the tree.
      3. Was asserted NOWHERE, which is how 1 and 2 survived.

    Reporting what was skipped is right — a checker that quietly narrows what it looked at
    reads as "everything is fine" when it means "I did not look there". It does not have to
    be exact to the file to serve that: "3 directories not walked" tells a reader the shape
    of the omission, and costs nothing to know.
    """
    found: set[pathlib.Path] = set()
    skipped = 0
    for p in paths:
        if not p.is_dir():
            if p.exists():
                found.add(p)
            continue
        for dirpath, dirnames, filenames in os.walk(p):
            here = pathlib.Path(dirpath)
            pruned = [d for d in dirnames if d in SKIP_DIRS or d.endswith(".egg-info")]
            for d in pruned:
                # Counted as we prune, without descending. `os.walk` never enters it.
                skipped += 1
                dirnames.remove(d)
            found.update(here / name for name in filenames if (here / name).is_file())
    return sorted(found), skipped


def verify(paths: typing.Iterable[pathlib.Path], root: pathlib.Path) -> dict[str, typing.Any]:
    """Every artifact under `paths`, checked against `root`. Unpinned files are counted.

    They are counted rather than listed because a directory of results usually holds many
    files that were never meant to carry a pin, and a report that names each one buries the
    finding. The COUNT still has to be visible: it is the difference between "everything
    checks out" and "nothing was checked".
    """
    examined, skipped = collect(paths)
    # ONE CACHE FOR THE WHOLE REPORT. Shared inputs are the normal case -- a fan-out of
    # N artifacts from one reference file meant N full reads of it -- so the memo has to
    # live across artifacts, not inside one.
    cache: dict[pathlib.Path, str | Exception] = {}
    results = [verify_artifact(p, root, cache) for p in examined]
    pinned = [r for r in results if r["status"] != NO_PIN]
    return {
        "root": str(root),
        "artifacts_seen": len(results),
        "directories_skipped": skipped,
        "artifacts_pinned": len(pinned),
        # GONE is counted apart from STALE even though both fail the check. They are
        # different repairs -- a stale artifact is rebuilt, a gone input is FOUND -- and
        # collapsing them printed "1 STALE" over an artifact whose line said GONE.
        "ok": sum(1 for r in pinned if r["status"] == OK),
        "stale": sum(1 for r in pinned if r["status"] == STALE),
        "gone": sum(1 for r in pinned if r["status"] == GONE),
        "unverifiable": sum(1 for r in pinned if r["status"] == UNVERIFIABLE),
        "artifacts": pinned,
    }


def render(report: dict[str, typing.Any]) -> str:
    """The text view: one line per artifact, then one line per input that is not OK.

    An OK input is not listed. A check whose passing output is proportional to the size of
    the corpus is a check whose output nobody reads, and the count in the summary already
    says how many passed.
    """
    out = []
    for art in report["artifacts"]:
        out.append(f"{art['status']:12} {art['artifact']}")
        for note in art.get("pin_truncated", []):
            out.append(f"             !! pin {note}")
        for i in art["inputs"]:
            if i["status"] == OK:
                continue
            detail = i.get("reason") or f"pinned {i['pinned']}, now {i.get('found', '?')}"
            out.append(f"             {i['status']:12} {i['name']}  ({detail})  via {i['via']}")
    return "\n".join(out) + ("\n" if out else "")
