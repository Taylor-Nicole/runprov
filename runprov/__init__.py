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

    configure(root=REPO, run_log=REPO / "reports" / "runs.jsonl")

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

from .environment import installed_packages, write_snapshot
from .hashing import VOLATILE, VOLATILE_JSON, content_digest, describe, sha256
from .project import (
    DEFAULT_CODE_PATHS,
    DEFAULT_TRACKED,
    Project,
    active,
    configure,
    default_generation,
    default_run_id,
    detect_root,
    git,
    is_configured,
)
from .run import HISTORY_SCHEMA, SCHEMA, Run
from .sinks import JsonlSink, MemorySink, RecordSink
from .terminal import Capture

__all__ = [
    "DEFAULT_CODE_PATHS",
    "DEFAULT_TRACKED",
    "HISTORY_SCHEMA",
    "SCHEMA",
    "VOLATILE",
    "VOLATILE_JSON",
    "Capture",
    "JsonlSink",
    "MemorySink",
    "Project",
    "RecordSink",
    "Run",
    "active",
    "configure",
    "content_digest",
    "default_generation",
    "default_run_id",
    "describe",
    "detect_root",
    "git",
    "installed_packages",
    "is_configured",
    "sha256",
    "to_yaml",
    "write_snapshot",
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
    `yaml.safe_load_all` raises partway through the result, and eleven
    `fix_transformation_log_*.py` repair scripts exist because of it. The append-only
    history is JSONL for exactly that reason, and this renders a VIEW of it.
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
