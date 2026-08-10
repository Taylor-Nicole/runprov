# Copyright (c) 2026 Hôpital Henri-Mondor and Taylor Thompson
# Licensed under the CeCILL-B Free Software License Agreement — see LICENSE.
# https://cecill.info/
"""runprov — record what a script read, wrote and ran as, in a form that can be checked.

    from runprov import Run, Project, configure

    configure(root=REPO, run_log=REPO / "reports" / "runs.jsonl")

    run = Run("build_labels", vars(args))
    df = pd.read_csv(run.input(INPUT))          # registering IS how you open it
    with open(run.output(OUT), "w") as fh:
        fh.write(run.header())                  # the pin, inside the artifact
        df.to_csv(fh, sep="\t", index=False)
    run.note("n_rows", len(df))
    run.write(OUT.with_name("build_labels_provenance.json"))

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
)
from .run import Run

__all__ = [
    "DEFAULT_CODE_PATHS",
    "DEFAULT_TRACKED",
    "VOLATILE",
    "VOLATILE_JSON",
    "Project",
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
    "sha256",
    "write_snapshot",
]
__version__ = "0.1.0"
