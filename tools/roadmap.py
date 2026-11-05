#!/usr/bin/env python3
"""Write `ROADMAP.md` from the decision records, the changelog and the version. Nothing by hand.

WHY IT IS GENERATED AND NOT WRITTEN. This project kept a hand-written `ROADMAP-0.9.0.md` in the
audit repository. It went stale on the day the version it planned shipped, which is what a
hand-kept list does: a feature's state is already recorded in three places — a `Status:` line in
its decision record, a dated section in `CHANGELOG.md`, and a tag — and a fourth copy is a fourth
thing to forget.

So there is no list. There is a view, and the view is rebuilt from the three places that are
updated as part of doing the work rather than as a chore afterwards.

WHAT IT CANNOT KNOW, PRINTED IN THE FILE ITSELF. It reports what the records CLAIM. A record
saying `Accepted` over code nobody wrote appears here as accepted, and only a few records name the
release that shipped them — so "accepted" mixes shipped-long-ago with built-but-unreleased, and
the file says so rather than implying a tidier picture than exists.

NOTHING IS LEFT OVER. Every decision record must yield a row, and one that does not is REPORTED in
the file by name rather than skipped. A roadmap that silently drops the records it cannot parse is
how a roadmap comes to look complete.
"""

from __future__ import annotations

import pathlib
import re
import sys
import typing

ROOT = pathlib.Path(__file__).resolve().parents[1]
ADR = ROOT / "docs" / "adr"
OUT = ROOT / "ROADMAP.md"

#: THE SEPARATOR IS WRITTEN AS ESCAPES, NOT AS THE CHARACTERS THEMSELVES. Two heading
#: spellings exist in the records -- `# 10. Title` and `# 0001 - Title` with an em dash --
#: so both dashes must be matched; but a literal en or em dash in source is an ambiguous
#: character the linter refuses, and rightly: it is indistinguishable from a hyphen on
#: screen, which is exactly how a regex comes to match something other than it appears to.
TITLE = re.compile("^#\\s*(\\d+)\\s*[.\u2013\u2014-]\\s*(.+?)\\s*$", re.M)
STATUS = re.compile(r"^-\s*\*\*Status:\*\*\s*(.+?)\s*$", re.M)
WORD = re.compile(r"\*{0,2}\s*([A-Za-z]+)")
SHIPPED = re.compile(r"shipped in\s*\*{0,2}(\d+\.\d+\.\d+)", re.I)
TROW = re.compile(r"\b(T-\d+)\b")
RELEASE = re.compile(r"^## \[(\d+\.\d+\.\d+)\][^\n]*?(\d{4}-\d{2}-\d{2})", re.M)
UNRELEASED = re.compile(r"^## \[Unreleased\]\s*$", re.M)
HEADING = re.compile(r"^### (.+?)\s*$", re.M)
VERSION = re.compile(r'^version\s*=\s*"([^"]+)"', re.M)


def _records() -> tuple[list[dict[str, typing.Any]], list[str]]:
    rows: list[dict[str, typing.Any]] = []
    unreadable: list[str] = []
    for path in sorted(p for p in ADR.glob("*.md") if p.name.lower() != "readme.md"):
        text = path.read_text(encoding="utf-8", errors="replace")
        title, status = TITLE.search(text), STATUS.search(text)
        if not title or not status:
            unreadable.append(path.name)
            continue
        word = WORD.match(status.group(1))
        if not word:
            unreadable.append(path.name)
            continue
        trow = TROW.search(status.group(1))
        ship = SHIPPED.search(status.group(1))
        rows.append(
            {
                "adr": int(title.group(1)),
                "title": title.group(2),
                "status": word.group(1).strip().lower(),
                "shipped": ship.group(1) if ship else None,
                "row": trow.group(1) if trow else None,
            }
        )
    return rows, unreadable


def render() -> str:
    changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    found = VERSION.search((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    if not found:
        raise SystemExit(
            'REFUSE: pyproject.toml carries no `version = "..."` line, so this view cannot say '
            "where the package is and would print a roadmap with a hole where its subject goes"
        )
    version = found.group(1)
    releases = RELEASE.findall(changelog)
    rows, unreadable = _records()

    unreleased = ""
    hit = UNRELEASED.search(changelog)
    if hit:
        tail = changelog[hit.end() :]
        nxt = re.search(r"^## \[", tail, re.M)
        unreleased = tail[: nxt.start()] if nxt else tail
    pending = HEADING.findall(unreleased)

    out: list[str] = []
    w = out.append
    w("# Roadmap")
    w("")
    w("<!-- GENERATED FILE — DO NOT EDIT. Rebuild with `python ci.py roadmap`. -->")
    w("")
    w("Every line below is derived: the decision records in `docs/adr/` say what each feature is")
    w("and what state it is in, `CHANGELOG.md` says what has been released and when, and")
    w("`pyproject.toml` says where the package is now. There is no hand-kept list to go stale,")
    w("because a hand-kept roadmap goes stale on the day its release ships.")
    w("")
    w("**This reports what the records claim.** A record marked accepted over code nobody wrote")
    w("appears here as accepted; holding records against the tree is a different job, done by the")
    w("citation guards and the gate.")
    w("")
    w("## Where the package is")
    w("")
    w(f"- Current version: **{version}**")
    if releases:
        last_v, last_d = releases[0]
        w(f"- Last release: **{last_v}** on {last_d}")
    w(f"- Released versions on record: {len(releases)}")
    if pending:
        w(f"- Sections awaiting the next release: **{len(pending)}**")
    w("")

    if pending:
        w("## In the next release")
        w("")
        w("Dated sections of `CHANGELOG.md` are the per-release record; these are the entries that")
        w("have landed and are not yet in one.")
        w("")
        for heading in pending:
            w(f"- {heading}")
        w("")

    buckets: dict[str, list[dict[str, typing.Any]]] = {}
    for r in rows:
        buckets.setdefault("shipped" if r["shipped"] else r["status"], []).append(r)

    w("## Features, by the state their decision record declares")
    w("")
    stated = sum(1 for r in rows if r["shipped"])
    w(f"{stated} of {len(rows)} records name the release that shipped them, so *accepted* below")
    w("mixes shipped-long-ago with built-but-unreleased. The changelog and the tags are where")
    w("shipping is actually recorded.")
    w("")
    for bucket in sorted(buckets, key=lambda b: (b == "shipped", b)):
        w(f"### {bucket.title()} — {len(buckets[bucket])}")
        w("")
        for r in sorted(buckets[bucket], key=lambda r: r["adr"]):
            tag = f" ({r['row']})" if r["row"] else ""
            ship = f" — shipped in {r['shipped']}" if r["shipped"] else ""
            w(f"- **ADR-{r['adr']:04d}**{tag} {r['title']}{ship}")
        w("")

    if unreadable:
        w("## Records this view could not read")
        w("")
        w("Named rather than dropped: a roadmap that silently omits what it cannot parse is how a")
        w("roadmap comes to look complete.")
        w("")
        for name in unreadable:
            w(f"- `{name}`")
        w("")
    else:
        w(f"Every one of the {len(rows)} decision records yielded a row; none was dropped.")
        w("")
    return "\n".join(out).rstrip() + "\n"


def main() -> int:
    if not ADR.is_dir():
        print(f"REFUSE: no decision records at {ADR}; this view has no source", file=sys.stderr)
        return 1
    text = render()
    OUT.write_text(text, encoding="utf-8")
    print(
        f"roadmap ok — wrote {OUT.name}, {len(text.splitlines())} line(s), derived from "
        f"{len(list(ADR.glob('*.md')))} record(s)"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
