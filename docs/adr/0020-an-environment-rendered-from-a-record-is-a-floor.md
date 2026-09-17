# 20. An environment rendered from a record is a floor

**Status:** Proposed — T-36. Specification for a feature that is not built.

## Context

The record carries the interpreter, the platform, the package list, the manager, and — where
one existed — an archived lock file or the git blob id of one. `runprov resources` already
renders a recorded measurement into the syntax of wherever the work is going next. This is the
same move for the environment.

## Decision

**R-1.** `runprov env <run>` renders the recorded environment as `requirements.txt`, a conda
YAML, or a `uv` specification, selected by `--format`.

**R-2.** **Where a lock file was archived, the lock is the answer** and the package list is not.
A list of installed packages and their versions is not a lock: it does not pin transitive
resolution, and an environment built from it satisfies the record without being the environment
that ran.

**R-3.** **Where only a list exists, the output says it is a floor**, in the emitted file, as a
comment — the same discipline `resources` uses for a measurement that understates. A rendered
environment presented as exact is a wrong record in a new file.

**R-4.** The interpreter version is emitted and is not negotiable. A package set resolved
against a different Python is a different environment, and this is the single most common way a
"reproduced" run differs.

**R-5.** Packages the run could not identify — the `bad` list the environment block already
records — are named in the output. Silence there would present a partial list as a whole one.

## Alternatives considered

**Build the environment, not just describe it.** Rejected: it needs a package manager, a
network, and a policy about what to do when resolution fails. `runprov` records; the manager
installs.

**Emit a container recipe.** Rejected for now: the record describes a Python environment, not a
system one, and a Dockerfile implies the latter. Revisit if the lock and the system packages are
ever both recorded.
