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

import os
import pathlib
import re
import typing

from .hashing import PIN_DIGEST_CHARS, describe, pin_digest

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
_COUNT = re.compile(r"^ {2}inputs \((\d+)\), sha256:$")
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

#: Statuses that mean the artifact cannot be trusted as current. `GONE` is included
#: deliberately: an input that no longer exists cannot be compared, but it is not a neutral
#: absence either -- the artifact can no longer be re-derived, which is a finding.
FAILING = (STALE, GONE)


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


def check_input(want: str, name: str, root: pathlib.Path) -> dict[str, typing.Any]:
    """One pinned input, re-derived and compared. See the module docstring on refusals."""
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
    try:
        got = pin_digest(describe(target))
    except (OSError, ValueError) as exc:  # unreadable now, or a FIFO where a file was
        return {**out, "status": UNVERIFIABLE, "reason": str(exc)}
    return {**out, "status": OK if got == want else STALE, "found": got}


def verify_artifact(path: pathlib.Path, root: pathlib.Path) -> dict[str, typing.Any]:
    """One artifact: every pin in it, every input in those, and a status for the whole."""
    blocks = read_pins(path)
    if not blocks:
        return {"artifact": str(path), "status": NO_PIN, "inputs": []}

    checked, truncated, scripts = [], [], []
    for pin in blocks:
        # Which step's claim this input belongs to. An inherited pin names an UPSTREAM
        # script, so without this a stale grandparent reads as a defect in the artifact's
        # own step and the reader goes looking in the wrong place.
        via = pin["fields"].get("script", "?")
        scripts.append(via)
        checked += [{**check_input(s, n, root), "via": via} for s, n in pin["entries"]]

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
        "artifact": str(path),
        "status": status,
        "inputs": checked,
        "pins": len(blocks),
        "scripts": scripts,
    }
    if truncated:
        out["pin_truncated"] = truncated
    return out


def collect(paths: typing.Iterable[pathlib.Path]) -> tuple[list[pathlib.Path], int]:
    """Files to examine, and how many were skipped. See `SKIP_DIRS` for what and why.

    Sorted, so two runs of the same check report in the same order — the same reason the
    pin itself is sorted. Duplicates collapse: naming a file and its parent directory must
    not double-count it.

    A path named EXPLICITLY is always examined, even inside a skipped directory: the skip
    list is about what a bare `verify` should walk, not a claim that those files cannot be
    checked. Asking about one by name is an answerable question and it gets answered.
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
                # Counted before pruning, so the report can say what it did not look at.
                skipped += sum(1 for _ in (here / d).rglob("*"))
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
    results = [verify_artifact(p, root) for p in examined]
    pinned = [r for r in results if r["status"] != NO_PIN]
    return {
        "root": str(root),
        "artifacts_seen": len(results),
        "files_skipped": skipped,
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
