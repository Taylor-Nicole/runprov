# Copyright (c) 2026 Assistance Publique – Hôpitaux de Paris (AP-HP),
#                    Hôpital Henri-Mondor, and Taylor Thompson
# Licensed under the CeCILL-B Free Software License Agreement — see LICENSE.
"""Remove in-flight markers that no longer describe anything running.

WHY THIS EXISTS AT ALL, given that the README already says `rm -r provenance/.incomplete` is
safe. Nothing in this package removed a marker except the run that wrote it, and there was no
TTL, so on a machine that has had a few hundred SIGKILLs they accumulate for ever. The banner
is capped now, so they no longer bury the page — but a directory that only grows is still a
directory that only grows, and telling a user to reach for `rm -r` is telling them to use a
blunter tool than the one that should have shipped.

**THIS DELETES LESS THAN THE `rm` IT REPLACES, AND THAT IS THE ENTIRE JUSTIFICATION.** A
command inside this package that removes files must be narrower than a human's `rm`, or it is
not worth having:

* it only ever unlinks *inside* the resolved `.incomplete` directory, checked per file, so a
  symlink planted there cannot make it delete something else;
* it only unlinks files that PARSE AS OUR MARKERS — a `*.json` whose contents are a JSON
  object carrying a `run_uid`. Anything else in that directory is another tool's, or a
  human's, and is counted and left alone;
* it removes only markers it can positively call **INTERRUPTED**. `rm -r` removes the lot.
  A run that is RUNNING on this host keeps its marker, and so does one whose host is not
  this one — a pid on a compute node says nothing here, `show` prints that as `?`, and
  turning "cannot tell" into a deletion is the guess this package exists to refuse. For a
  run started without `provenance=` the marker is the ONLY evidence there is: that shape
  appends no history line by design, so a wrong deletion is unrecoverable rather than
  merely inconvenient. `--other-hosts` is how a user overrides the other-host half of
  that, and it is an assertion THEY make, not one this command makes for them.

WHAT IT IS NOT ALLOWED TO TOUCH: the history. `runs.jsonl` is the append-only record and is
what makes an interruption permanent; the markers are a cheap index over "what is unfinished
NOW" that is documented as transient. Pruning changes how fast `show` can answer, and changes
no finding. That is exactly why the operation is safe, and it is also the reason it lives in
its own module rather than in `show.py`: `show` writes nothing, and folding a deletion into
the renderer would make that promise untrue in the code as well as in the docs.

NOT A PUBLIC API. Reached through `runprov prune` and `runprov show --forget-markers`.
"""

from __future__ import annotations

# A MODULE'S `__all__` RATIFIES THE PACKAGE'S PROMISE; IT NEVER MAKES ONE — see ADR 0003.
# Nothing here is promised. This is a CLI operation, one week old, and a user who wants it
# from Python has `pathlib.Path.unlink` and the same three rules written above.
__all__: list[str] = []

import datetime as dt
import json
import pathlib
import typing

from .show import INTERRUPTED, liveness
from .show import RUNNING as RUNNING_STATE

#: The stamp `run.py` writes into every marker's `started_utc`. Parsed rather than trusted as
#: a sort key here because `--older-than` needs a real instant, not an ordering.
_STAMP = "%Y-%m-%dT%H:%M:%SZ"

#: Suffixes accepted by `--older-than`. Deliberately small and deliberately not `M`/`y`: a
#: month is not a fixed number of seconds and a year is worse, and a duration flag that
#: silently means 30 days by "1M" is the kind of approximation this package does not make.
_UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}


class Plan(typing.NamedTuple):
    """What a prune WOULD do, separated from doing it.

    Computed first and in full so that `--dry-run` and the real run share one decision
    procedure. A dry run that walked a different code path would be a preview of something
    other than what happens, which is the failure mode a preview exists to prevent.

    The four counts are not decoration: each names a distinct reason a file in that directory
    survived, and a user staring at "removed 0" needs to know which of them it was.
    """

    remove: list[pathlib.Path]
    #: Alive on this host. Never removed — see the module docstring.
    running: int
    #: From another host, so liveness cannot be checked here. Kept unless `other_hosts`.
    untellable: int
    #: Younger than `--older-than`.
    too_new: int
    #: A marker whose `started_utc` will not parse, under `--older-than`. We cannot say how
    #: old it is, so we do not remove it: "we could not look" is not a licence to delete.
    undateable: int
    #: A file in `.incomplete` that is not one of our markers. Not ours, not touched.
    foreign: int


def parse_age(text: str) -> float:
    """`"30d"` → seconds. Raises `ValueError` with a message meant for a user, not a log.

    A bare number is REFUSED rather than assumed to be seconds. `--older-than 30` is far more
    likely to mean thirty days than thirty seconds, and guessing which would make the
    difference between deleting nothing and deleting everything.
    """
    t = text.strip().lower()
    if len(t) < 2 or t[-1] not in _UNITS:
        raise ValueError(
            f"--older-than {text!r}: expected a number and a unit, one of "
            f"{'/'.join(sorted(_UNITS))} — for example 30d, 12h, 90m"
        )
    try:
        n = float(t[:-1])
    except ValueError:
        raise ValueError(f"--older-than {text!r}: {t[:-1]!r} is not a number") from None
    if n < 0:
        raise ValueError(f"--older-than {text!r}: a negative age selects nothing")
    return n * _UNITS[t[-1]]


def _age_seconds(rec: dict[str, typing.Any], now: dt.datetime) -> float | None:
    stamp = rec.get("started_utc")
    if not isinstance(stamp, str):
        return None
    try:
        started = dt.datetime.strptime(stamp, _STAMP).replace(tzinfo=dt.timezone.utc)
    except ValueError:
        return None
    return (now - started).total_seconds()


#: How many names a plan prints before summarising. Same rule and same number as the in-flight
#: banner's `MARKERS_SHOWN`: a preview that scrolls the reason for the preview off the screen
#: has stopped being one. Not imported from `__main__`, which imports this module.
LISTED = 10


def plan(
    directory: pathlib.Path,
    *,
    older_than: float | None = None,
    other_hosts: bool = False,
    now: dt.datetime | None = None,
) -> Plan:
    """Decide what to remove, touching nothing.

    THE CONTAINMENT CHECK IS PER FILE AND IT IS THE POINT. `glob` returns names under the
    directory, but a name under a directory is not a path inside it: `.incomplete/x.json` can
    be a symlink to anywhere, and unlinking it removes the link rather than the target — so
    the danger is not deletion of the target but that the resolved parent is somewhere this
    command has no business walking. Resolving each candidate and requiring its parent to BE
    the resolved directory is the same rule `verify` applies to a pinned name, and it is
    cheap enough to apply to every file every time.
    """
    now = now or dt.datetime.now(dt.timezone.utc)
    remove: list[pathlib.Path] = []
    running = untellable = too_new = undateable = foreign = 0
    try:
        here = directory.resolve()
    except (OSError, RuntimeError):  # guards-ok: an unresolvable directory prunes nothing
        return Plan([], 0, 0, 0, 0, 0)
    if not directory.is_dir():
        return Plan([], 0, 0, 0, 0, 0)
    for f in sorted(directory.glob("*.json")):
        try:
            if f.resolve().parent != here or not f.is_file():
                foreign += 1
                continue
            rec = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, RuntimeError, ValueError):  # guards-ok: unreadable is not ours
            foreign += 1
            continue
        # NOT A DICT IS NOT OURS. A history line that is valid JSON but not an object crashed
        # every reader before A-10; the same input reaching a DELETE loop is a worse version
        # of that bug, so the type is checked before `.get` is called on it.
        if not isinstance(rec, dict) or not rec.get("run_uid"):
            foreign += 1
            continue
        # POSITIVELY INTERRUPTED, OR IT STAYS. Three answers come back and only one of them
        # is "this run is definitely over": RUNNING is a live pid here, and `?` is a marker
        # from another host, where a pid means nothing and `os.kill(pid, 0)` would answer
        # about whichever local process happens to hold that number. An earlier draft of
        # this loop removed `?` — deleting evidence about a job that may well still be
        # running on a compute node, on the strength of not having looked.
        state = liveness(rec)
        if state != INTERRUPTED:
            if state == RUNNING_STATE:
                running += 1
                continue
            if not other_hosts:
                untellable += 1
                continue
        if older_than is not None:
            age = _age_seconds(rec, now)
            if age is None:
                undateable += 1
                continue
            if age < older_than:
                too_new += 1
                continue
        remove.append(f)
    return Plan(remove, running, untellable, too_new, undateable, foreign)


def apply(p: Plan) -> tuple[int, list[str]]:
    """Unlink what the plan chose. Returns how many went, and one message per failure.

    `missing_ok`, because the run that owns a marker may reach `__exit__` between the plan
    and this loop and remove it itself. That is the ordinary race and not a failure: the file
    is gone, which is what was wanted.

    FAILURES ARE COLLECTED, NOT RAISED. A read-only file in the middle of the list must not
    stop the other 400 from being removed, and the user needs to be told which one it was.
    """
    gone = 0
    problems: list[str] = []
    for f in p.remove:
        try:
            f.unlink(missing_ok=True)
            gone += 1
        except OSError as exc:  # guards-ok: one bad file does not stop the rest
            problems.append(f"could not remove {f.name}: {exc}")
    return gone, problems


def render(p: Plan, gone: int | None, directory: pathlib.Path) -> str:
    """One block saying what happened, WHICH files, and for every survivor why it survived.

    `gone is None` renders the dry run. The two share this function so a preview cannot
    describe the operation differently from the operation.

    THE NAMES ARE PRINTED, capped at `LISTED`. A preview that says only "would remove 412"
    is not a preview — the user cannot check it against anything — and a `--dry-run` nobody
    can check is worse than no `--dry-run`, because it looks like diligence.
    """
    verb = f"would remove {len(p.remove)}" if gone is None else f"removed {gone}"
    out = [f"# {verb} in-flight marker(s) from {directory}"]
    out += [f"#     {f.name}" for f in p.remove[:LISTED]]
    if len(p.remove) > LISTED:
        out.append(f"#     … and {len(p.remove) - LISTED} more")
    kept = [
        (p.running, "still RUNNING on this host — kept, the marker is the live evidence"),
        (
            p.untellable,
            "from another host, so liveness cannot be checked here — kept "
            "(--other-hosts removes them anyway)",
        ),
        (p.too_new, "younger than --older-than"),
        (p.undateable, "no readable started_utc, so their age is unknown — kept"),
        (p.foreign, "not runprov markers — kept, untouched"),
    ]
    out += [f"#   {n} {why}" for n, why in kept if n]
    if not p.remove and not any(n for n, _ in kept):
        out.append("#   nothing to do.")
    return "\n".join(out)
