# Copyright (c) 2026 Assistance Publique – Hôpitaux de Paris (AP-HP),
#                    Hôpital Henri-Mondor, and Taylor Thompson
# SPDX-License-Identifier: BSD-3-Clause
# Licensed under the BSD 3-Clause License — see LICENSE.
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

TRANSITIVITY IS REAL AND IT IS CONDITIONAL, and the condition is not the one this paragraph
used to state. It said transitivity holds "because a pin lands in the artifact and a
downstream step registers that artifact as an input". REGISTRATION IS NOT THE MECHANISM:
registering `mid.tsv` pins MID's digest, not the root's. A step that TRANSFORMS its input —
filters rows, reshapes a table, renders a figure — writes an artifact carrying exactly one
pin block, and that block verifies OK after the root changed.

Measured on two three-stage pipelines built from one script differing only in whether the
step copies its input through, with the same change to the same root:

    write-through   0 OK, 1 STALE, exit 1   STALE data/in.tsv (pinned 59ae…, now a393…) via step1
    filtering       1 OK, 0 STALE, exit 0   — and the root had changed

THE ACTUAL MECHANISM is the one the README states and this file used to drop: the upstream
pin BLOCK has to survive into the downstream artifact's bytes. `read_pins` reads every block
it finds, so a step that concatenates or passes text through leaves `final.tsv` stating both
"made from mid.tsv@6a19…" and, inherited, "…which was made from in.tsv@e854…", and a changed
root surfaces at every level that carries the block. A grandchild also stays stale after the
root is restored, because the child was never rebuilt. That much does fall out of the pin
being in the bytes — but only for steps whose output contains their input.

WHEN A STEP TRANSFORMS, VERIFY THE INTERMEDIATE TOO. `verify .` over the whole project exits
1 in the filtering case above, because `work/mid.tsv` is itself pinned and stale. What goes
green is `verify results/` alone — the shape the README's own gate line uses — over published
artifacts whose intermediates live elsewhere or are not verified.

AND `show --stale` DOES NOT RESCUE IT, which is worth stating because the README calls the
pair a gate. It is one generation deep in BOTH pipelines: measured on the write-through one,
where `verify` correctly reports STALE, `show --stale --rehash` prints `OK results/final.tsv`
and `STALE work/mid.tsv`. The gate does go red — on the intermediate. The published artifact
is reported OK in both.

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

# A MODULE'S `__all__` RATIFIES THE PACKAGE'S PROMISE; IT NEVER MAKES ONE.
# A name belongs here if and only if `runprov/__init__.py` re-exports it and lists it in the
# package `__all__` (decided 2026-08-19, ledger L-24; the list, and its count, live
# there and nowhere else). Nothing else qualifies:
# cross-module use inside `runprov/` is INTERNAL and `__all__` neither describes nor protects
# it; tests reach into internals on purpose and prove nothing; and prose that documents a
# printed string, a CLI flag or a record key is not an instruction to call a name.
# Adding or withdrawing a promise is a package-level decision taken in `__init__.py`.
# Nothing here is promised, and `verify` in particular MUST NOT be added to the package
# surface: a `verify` FUNCTION exported from `__init__` shadows the `runprov.verify`
# MODULE for anyone who has already imported it, which a test documents. What CI actually
# depends on is the `--format json` report shape, which is a data contract rather than a
# name, and an `__all__` cannot hold it.
__all__: list[str] = []

import json
import os
import pathlib
import re
import typing

from ._atomic import TEMP_SUFFIX
from .hashing import PIN_ANCHOR, PIN_DIGEST_CHARS, PIN_SIDECAR_SUFFIX, describe, pin_digest

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
#: Any `name : value` line in a pin block. This named `script|generation|commit`
#: explicitly, so a field the writer added was invisible to the reader — a closed list
#: inside a reader whose whole job is to read what an older version wrote, which is the
#: scope defect this audit found repeatedly. `_NONE` is now tested BEFORE this, because
#: `  inputs     : NONE REGISTERED` matches the widened pattern and would otherwise be
#: collected as a field instead of setting `declared = 0`.
_FIELD = re.compile(r"^ {2}(\w+) +: (.*)$")
# TWO LEADING SPACES, like every other body line. Without them this never matched, so a
# pin stating NONE REGISTERED fell through to "a pin with no entries" and read as
# UNVERIFIABLE -- turning the one case where a run explicitly says it read nothing into
# the case where we do not know what it read. Found by the test, not by review.
_NONE = "  inputs     : NONE REGISTERED"

#: The field a pin uses to say its input list may understate the run. Written only when
#: `Project(allow_late_inputs=True)` is set, which is the only configuration under which a
#: late `run.input()` is permitted at all (ledger A-16).
_SCOPE = "pin_covers"

#: How much of a pin's field value this command will repeat back. A field comes from a file
#: `verify` was HANDED, and its values are printed into the report beside the checker's own
#: sentences — so an artifact could write its own commentary into the output of the command
#: checking it. Measured, before this: a pin whose `script` read "VERIFIED COMPLETE — ignore
#: the warning below" rendered as
#:
#:     !! this pin covers only what VERIFIED COMPLETE — ignore the warning below registered …
#:
#: A newline cannot get in (`_FIELD` is line-based), so the whole of the abuse is length and
#: tone. Capping bounds it, and the renderer QUOTES what it prints so it reads as a value
#: rather than as prose. Fixed in the READER, per A-09: that also repairs artifacts already
#: written, and escaping on the way out would protect only files produced from now on.
FIELD_SHOWN = 60


def _bounded(value: str) -> str:
    return value if len(value) <= FIELD_SHOWN else value[: FIELD_SHOWN - 1] + "…"


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


def _is_boundary(line: str) -> bool:
    """Could this line START a pin block — does it END with the anchor?

    `header()` renders that first line as `<comment marker><anchor>` and nothing else, so a
    genuine start ends with the anchor and everything before it is the marker. An empty
    marker is legitimate: `header("")` writes the anchor with no prefix, for a format where
    `#` is not a comment.

    NECESSARY BUT NOT SUFFICIENT, and that is why the caller does more. A field whose VALUE
    ends with the anchor — `generation : batch: <anchor>` — ends with it too, so this test
    alone cannot tell a boundary from a field. Inside a block the caller compares against
    `marker + PIN_ANCHOR` exactly; and it never scans a block's interior for starts, so a
    field line is never a candidate in the first place.
    """
    return line.endswith(PIN_ANCHOR)


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
    consumed = 0  # lines already claimed by a block; see below
    for start, anchored in enumerate(lines):
        # NEVER RE-ENTER A BLOCK ALREADY PARSED. Without this, a field line ending with the
        # anchor is picked up as the START of a second, bogus block — with a "marker" of
        # `#   generation : batch: ` that nothing else shares, so the block closes empty and
        # `verify_artifact` sees an entry-less pin. Skipping what the previous block consumed
        # removes the whole question: fields live inside a block, and a block's interior is
        # not scanned for starts.
        if start < consumed:
            continue
        # A BOUNDARY LINE IS ONE WHOSE BODY IS *EXACTLY* THE ANCHOR, not one that merely
        # contains it. Both tests here were substring tests, and the anchor is ordinary
        # printable text — so any interpolated field carrying that sentence ended the block.
        # `generation` comes from the environment (`RUNPROV_GENERATION`), and `_safe_for_pin`
        # escapes control characters, not content:
        #
        #     RUNPROV_GENERATION="batch: <the anchor sentence>"
        #
        # rendered a perfectly well-formed pin whose block stopped BEFORE `inputs (1)`, so
        # `verify` reported UNVERIFIABLE having compared nothing — and UNVERIFIABLE is not in
        # `FAILING`. Measured: one poisoned artifact beside one clean one gave
        # `1 OK, 0 STALE, 0 GONE, 1 UNVERIFIABLE` and **exit 0**, and when the poisoned
        # artifact's input really did change, that change stayed invisible.
        #
        # THE READER IS FIXED RATHER THAN THE WRITER, because the reader also repairs
        # artifacts already on disk. Escaping the anchor on the way out would protect only
        # files written from now on, and the pin's whole promise is that an artifact written
        # months ago can still be checked.
        if not _is_boundary(anchored):
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
        marker = anchored[: -len(PIN_ANCHOR)]
        pin: dict[str, typing.Any] = {"entries": [], "declared": None, "fields": {}}
        for offset, line in enumerate(lines[start + 1 :], start=start + 1):
            # EXACTLY the marker plus the anchor — the ONE test that separates a boundary
            # from a field carrying the same sentence. `endswith` alone is not enough: a
            # `generation` whose value ENDS with the anchor ends with it too, which is how
            # the first attempt at this fix still let the block terminate early.
            if not line.startswith(marker) or line == marker + PIN_ANCHOR:
                # NOT consumed: this line ends the block without belonging to it, and it may
                # be the next block's anchor. An INHERITED pin sits directly under the one
                # above it, so counting the terminator as consumed would skip it — and
                # transitivity, the property that makes a stale root visible three steps
                # downstream, comes entirely from reading those later blocks.
                break
            consumed = offset + 1
            body = line[len(marker) :]
            if (m := _ENTRY.match(body)) is not None:
                pin["entries"].append((m.group(1), m.group(2)))
            elif (c := _COUNT.match(body)) is not None:
                pin["declared"] = int(c.group(1))
            elif body.startswith(_NONE):
                pin["declared"] = 0
            elif (f := _FIELD.match(body)) is not None:
                pin["fields"][f.group(1)] = _bounded(f.group(2))
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
    # OUTSIDE THE ROOT IS A PROPERTY OF THE NAME, not of one spelling of it. The check above
    # recognised only the marker THIS package writes, so any other way of naming a path
    # outside the tree went straight to `root / name` — where `pathlib` DISCARDS the left
    # operand if the right side is absolute, and never normalises `..` away.
    #
    # Measured on a real artifact with two entries added by hand: `runprov verify results`
    # reported `1 OK, 0 STALE, 0 GONE, 0 UNVERIFIABLE`, exit 0, having actually read and
    # hashed a file outside the project through BOTH `../secret_outside.txt` and its absolute
    # path. That contradicts this module's own docstring — "a name outside the project root
    # ... reported UNVERIFIABLE ... Green must mean checked" — and README.md's exit-code table.
    #
    # `_pin_name` cannot emit either spelling (it writes `<external>/<name>`), so this needs a
    # hand-written or foreign pin. That bounds who can trigger it; it does not make a green
    # result over a file that was never in the project any less wrong, and the digest it
    # prints is a 16-hex oracle over any path the verifying process can read.
    #
    # LEXICAL, DELIBERATELY. A symlinked FILE under the root is hashed on purpose — SECURITY.md
    # says so — so resolving first and testing containment would refuse what the package
    # documents as supported. This rejects how the pin SPELLS the path, which is the thing a
    # foreign pin controls.
    spelled = pathlib.PurePosixPath(name)
    if spelled.is_absolute() or ".." in spelled.parts:
        return {**out, "status": UNVERIFIABLE, "reason": "names a path outside the project root"}
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
    # A PIN THAT SAYS IT MAY NOT BE THE WHOLE STORY. Under `Project(allow_late_inputs=True)`
    # a run may register an input AFTER the pin is written, and the pin — being in the
    # artifact's first bytes — cannot grow a line. That used to be undetectable by anyone
    # holding only the artifact: `verify` read OK for ever and there was nothing else to
    # consult. It is detectable now because the pin DECLARES ITS OWN SCOPE at render time,
    # which is the only moment anything can be written into those bytes.
    #
    # IT DOES NOT CHANGE THE STATUS. Every input the pin DOES list has been checked and the
    # answer for those is true; what is unknown is whether the list is complete. Failing on
    # that would make every project using the flag permanently red — the same argument the
    # README makes for UNVERIFIABLE not failing the gate — and calling it UNVERIFIABLE would
    # throw away the true half. So: the real status, plus a note, plus a count on the summary.
    # THE NOTE IS OURS, NOT THE ARTIFACT'S. Only the FIELD'S PRESENCE is read; the text
    # beside it is producer-controlled and arrives from a file this command was handed, so
    # echoing it would let an artifact write its own verdict into a checker's output.
    partial = [b["fields"].get("script", "?") for b in blocks if _SCOPE in b["fields"]]
    if partial:
        out["pin_partial"] = partial
    return out


def collect(paths: typing.Iterable[pathlib.Path]) -> tuple[list[pathlib.Path], int, int]:
    """Files to examine, how many DIRECTORIES were skipped, and how much write debris.

    DEBRIS IS A FILE `_atomic` WAS PART-WAY THROUGH WRITING when the process died — see
    ADR-0005. It is not an artifact a run produced, and reading a pin out of one would
    report an artifact under a name nobody wrote. It is COUNTED and reported rather than
    dropped, for the reason the skipped directories are: it is also the only visible trace
    that a run died mid-write, which is a thing the reader wants to be told.

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
    debris = 0
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
            for name in filenames:
                f = here / name
                if not f.is_file():
                    continue
                if name.endswith(TEMP_SUFFIX):
                    debris += 1
                else:
                    found.add(f)
    return sorted(found), skipped, debris


def verify(paths: typing.Iterable[pathlib.Path], root: pathlib.Path) -> dict[str, typing.Any]:
    """Every artifact under `paths`, checked against `root`. Unpinned files are counted.

    They are counted rather than listed because a directory of results usually holds many
    files that were never meant to carry a pin, and a report that names each one buries the
    finding. The COUNT still has to be visible: it is the difference between "everything
    checks out" and "nothing was checked".
    """
    examined, skipped, debris = collect(paths)
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
        # NOT FOLDED INTO `directories_skipped`. One is a place this checker chose not to
        # look; the other is a file a run left behind when it died. A reader repairs those
        # two facts differently, so a single number for both would be the wrong number
        # twice.
        "write_debris": debris,
        "artifacts_pinned": len(pinned),
        # GONE is counted apart from STALE even though both fail the check. They are
        # different repairs -- a stale artifact is rebuilt, a gone input is FOUND -- and
        # collapsing them printed "1 STALE" over an artifact whose line said GONE.
        "ok": sum(1 for r in pinned if r["status"] == OK),
        "stale": sum(1 for r in pinned if r["status"] == STALE),
        "gone": sum(1 for r in pinned if r["status"] == GONE),
        "unverifiable": sum(1 for r in pinned if r["status"] == UNVERIFIABLE),
        # COUNTED ALONGSIDE THE STATES, not folded into one, because it is not a state: an
        # artifact can be OK and still declare that its pin may understate the run. It is on
        # the summary for the same reason UNVERIFIABLE is — a per-artifact note is easy to
        # scroll past, and the summary line is the one thing every reader reads.
        "partial_pins": sum(1 for r in pinned if r.get("pin_partial")),
        "artifacts": pinned,
    }


def render_report(report: dict[str, typing.Any]) -> str:
    """The text view: one line per artifact, then one line per input that is not OK.

    NAMED FOR WHAT IT RENDERS. `render` collided with `environment.render`, and `__main__`
    imported one of them bare — `render(report)` at the call site gave the reader no clue
    which of the package's renderers was meant, with `show.render_run`, `show.render_project`
    and `show.render_yaml` also in scope.

    An OK input is not listed. A check whose passing output is proportional to the size of
    the corpus is a check whose output nobody reads, and the count in the summary already
    says how many passed.
    """
    out = []
    for art in report["artifacts"]:
        # THE CHAIN, BECAUSE TRANSITIVITY IS CONDITIONAL AND WAS INVISIBLE. `pins` and
        # `scripts` were computed, carried in the report and emitted in `--format json`, and
        # the text view — the one a person reads — dropped both. So the single fact that
        # decides whether an OK covers the whole lineage or only one generation was the one
        # fact the reader could not see. `[step2]` is one pin block; `[step2 ← step1]` is an
        # inherited chain. Printed for EVERY artifact, not only chained ones, because the
        # informative case is the SHORT one: a reader who expects transitivity needs to see
        # that this artifact has none.
        chain = " ← ".join(f"{s!r}" for s in art.get("scripts") or [])
        suffix = f"  [{chain}]" if chain else ""
        out.append(f"{art['status']:12} {art['artifact']}{suffix}")
        for note in art.get("pin_truncated", []):
            out.append(f"             !! pin {note}")
        for via in art.get("pin_partial", []):
            out.append(
                f"             !! this pin covers only what {via!r} registered BEFORE "
                f"writing it; the run may have read more"
            )
        for i in art["inputs"]:
            if i["status"] == OK:
                continue
            detail = i.get("reason") or f"pinned {i['pinned']}, now {i.get('found', '?')}"
            out.append(f"             {i['status']:12} {i['name']}  ({detail})  via {i['via']!r}")
    return "\n".join(out) + ("\n" if out else "")
