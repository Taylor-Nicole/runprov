# Copyright (c) 2026 Assistance Publique – Hôpitaux de Paris (AP-HP),
#                    Hôpital Henri-Mondor, and Taylor Thompson
# Licensed under the CeCILL-B Free Software License Agreement — see LICENSE.
# https://cecill.info/
r"""runprov — record what a script read, wrote and ran as, in a form that can be checked.

RAW, and that is load-bearing rather than stylistic: the example passes `sep="\t"`, and in a
normal docstring that is an actual tab by the time `help(runprov)` prints it — the reader is
shown `sep="        "` and cannot tell what to type. The example is the thing being got right
here, so it has to survive being rendered.

    from runprov import Run, configure

    configure(root=REPO)  # history -> REPO/provenance/runs.jsonl, where the CLI looks

    PROV = OUT.with_name("build_labels_provenance.json")

    # provenance=PROV is what makes a crash record. Without it, __exit__ writes nothing.
    with Run("build_labels", vars(args), provenance=PROV) as run:
        df = pd.read_csv(run.input(INPUT), sep="\t")   # registering IS how you open it
        with open(run.output(OUT), "w", encoding="utf-8") as fh:
            fh.write(run.header())                     # the pin, inside the artifact
            df.to_csv(fh, sep="\t", index=False)
        run.note("n_rows", len(df))

`provenance=` on the CONSTRUCTOR is load-bearing and this docstring used to get it wrong —
it showed `run = Run(...)` with a closing `run.write(PROV)`, which is the shape README's
"Two shapes that record nothing" table lists first: a crash halfway records no sidecar, no
history line, and prints no warning. `with` alone does not fix it either, because `__exit__`
only writes when `provenance=` reached the constructor. That the package's own front page
taught the failure it exists to prevent is why `Run`'s class docstring states the rule
unhedged, and why a test now asserts this one does too.

Three properties, each of which exists because its absence caused a specific defect:

1. **Registration is the ergonomic path.** `input()` returns the path, so the natural way
   to open a file is the recorded way.
2. **The pin lives in the artifact**, not only in a sidecar, so an artifact can say what
   it was made from after the sidecar has been overwritten.
3. **The pin is deterministic.** No timestamps, no run id — those make every artifact
   differ on every run, which produces a permanently red reproducibility check, which
   trains everyone to ignore it.

Promoted out of `scripts/audit/_provenance.py` (ADR-028). That module is now a shim over
this package, so the audit and any new pipeline share one implementation rather than two
copies that agree until they do not.
"""

import typing

from .hashing import content_digest, describe, sha256
from .project import (
    DEFAULT_CODE_PATHS,
    DEFAULT_TRACKED,
    Project,
    active,
    configure,
    detect_root,
    is_configured,
)
from .run import HISTORY_SCHEMA, SCHEMA, Run, Terminated
from .sinks import JsonlSink, RecordSink

# EVERY NAME HERE IS A PROMISE. It was 27, and the quickstart uses two of them; a previous
# review already called 24 too many to freeze. Five came out on 2026-08-19 (ledger L-24):
#
#   VOLATILE, VOLATILE_JSON   compiled regexes -- the MECHANISM of volatile-stamp stripping.
#                             Exporting them means never being able to change how it works.
#   PIN_UNSAFE                documentation rendered into a refusal message. Nothing branches
#                             on it -- proved by emptying it, behaviour unchanged.
#   default_run_id,           defaults `Project` already supplies; a caller passes a callable
#   default_generation        rather than reaching for these.
#
# They still EXIST at `runprov.run.PIN_UNSAFE`, `runprov.hashing.VOLATILE` and
# `runprov.project.default_run_id` -- they simply stop being promised. SCHEMA and
# HISTORY_SCHEMA stay (a consumer parsing records needs the version) and so do the two
# DEFAULT_* tuples (people extend them).
#
# FIVE MORE ON 2026-08-20 (ledger L-84 follow-up), for a different reason: not "this is
# mechanism" but "nobody was ever told to call this". Every one had ZERO references in
# README, GETTING-STARTED, WHY or any ADR -- checked, not assumed -- while the features they
# belong to are documented entirely as configuration and record fields:
#
#   installed_packages,       the environment snapshot is reached through
#   write_snapshot            `Project(env_snapshot_dir=...)` and the record it writes
#   Capture                   terminal capture is reached through `terminal_log`
#   MemorySink                `sink=` takes one; the class is for tests and for people
#                             writing their own, neither of which needs a promise
#   git                       6 internal call sites and no documented caller. A bare `git`
#                             in an importing namespace also collides with GitPython's
#                             top-level module, and the contract -- swallow everything,
#                             return None, 20s timeout -- is provenance capture rather than
#                             a general-purpose runner. ADR-0003 left this open; this closes
#                             it by withdrawal rather than by rename.
#
# 17 names. All ten withdrawn names still exist on their own modules.
__all__ = [
    "DEFAULT_CODE_PATHS",
    "DEFAULT_TRACKED",
    "HISTORY_SCHEMA",
    "SCHEMA",
    "JsonlSink",
    "Project",
    "RecordSink",
    "Run",
    "Terminated",
    "active",
    "configure",
    "content_digest",
    "describe",
    "detect_root",
    "is_configured",
    "sha256",
    "to_yaml",
]
__version__ = "0.1.0"


def to_yaml(records: typing.Any) -> str:  # noqa: ANN401 - one record or an iterable of them
    """Render a record — or a whole history — as the transformation-log YAML shape.

    The predecessor wrote a per-run `*_manifest_*.yml` with `yaml.safe_dump`, and every
    script doing so carried its own `json_safe()` to make pandas and numpy scalars
    dumpable. Both of those are covered here: the renderer needs **no pyyaml**, and the
    record has already been through `_jsonable`, which resolves a `numpy.int64` to an
    `int` rather than to the string `"6"`.

    `python -m runprov log --format yaml` is this same function over a whole history. This
    is it for one run, for a caller who wants the manifest as a file beside the artifact:

        with Run("step", provenance=PROV) as run:
            ...
        MANIFEST.write_text(runprov.to_yaml(run.record), encoding="utf-8")

    AFTER the block, deliberately. `__exit__` is where outputs are hashed and the status
    becomes known, so a manifest rendered inside the block describes a run that has not
    finished — the same trap as `run.write()` instead of `provenance=`.

    What it does NOT do is append `---` documents to a shared file. That is not an
    oversight: the predecessor's writer appended `---` into a file that began as a list,
    `yaml.safe_load_all` raises partway through the result, and nine
    `fix_transformation_log_*.py` repair scripts exist because of it. The append-only
    history is JSONL for exactly that reason, and this renders a VIEW of it.

    THE ONLY `to_yaml` IN THE PACKAGE, since 2026-08-19. `runprov.show` carried a second
    function of the same name and a different shape — it renders any nested structure as
    indented YAML, this one renders RUN RECORDS in the transformation-log shape. Both were
    importable and neither raised on the other's input, so the wrong import produced a
    plausible file of the WRONG SHAPE rather than an error. It is now `show.render_yaml`
    (ledger L-47).

    This one also normalises with `_jsonable` first, which `render_yaml` does not — that is
    what keeps a manifest written here byte-comparable with the sidecar written by
    `run.write()`, and it is why the two disagreed: on a record holding a numpy-like scalar,
    this renders `118` where the low-level one renders `"<scalar 118>"`.
    """
    from .__main__ import _yaml  # local: keeps the CLI module off the package import path
    from .run import _jsonable

    # THE SAME normalisation the sidecar and the history line go through. `_jsonable` runs
    # at serialisation time, so a LIVE `run.record` still holds whatever the caller handed
    # to `note()` -- and rendering that directly produced a manifest reading
    # `n_exact_matches: "<scalar 118>"` beside a sidecar reading `118`, for one run. Two
    # renderings of the same record must not disagree, which is the whole claim here.
    rows = [records] if isinstance(records, dict) else list(records)
    return _yaml([_jsonable(r) for r in rows])
