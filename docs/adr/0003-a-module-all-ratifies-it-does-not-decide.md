# 3. A module's `__all__` ratifies the package's promise; it never makes one

Date: 2026-08-20 · Status: accepted · Ledger: L-44 (with L-24 one level up)

## Context

Eleven of twelve modules declared no `__all__`. Every top-level name in them —
`show.staleness`, `verify.verify`, `hashing.pin_digest`, 101 names in total — was importable
but unpromised, and after the first PyPI upload they would be frozen **by use rather than by
decision**. That is the same defect L-24 had just closed one level up, where the package
`__all__` had grown to 27 names with five of them there by accident.

The obvious repair — let each module declare what it thinks is public — makes it worse. Ten
files would each be adjudicating the public surface, and they would disagree: an inventory
across all eleven produced `verify.__all__ = ["verify"]` from one reviewer and
`show.__all__ = []` from another, for two modules that are both absent from `__init__.py`,
both reached only through the CLI, and both halves of the same restructuring.

## Decision

A name belongs in a module's `__all__` **if and only if** `runprov/__init__.py` re-exports
it and lists it in the package `__all__`.

One other thing can make a name public, and it is not a module's to grant either:
`pyproject.toml`'s `[project.scripts]` names `runprov.__main__:main`, so an installed
artefact outside this tree depends on that symbol. Hence `__main__.__all__ == ["main"]`.

Three things that look like evidence and are not:

- **Cross-module use inside `runprov/`.** That is INTERNAL. `__all__` neither describes nor
  protects it — `from .show import _yaml_entry` keeps working whatever the list says.
- **Test references.** The suite reaches into internals on purpose. 298 references to `Run`
  and 68 to `content_digest` say nothing the package `__all__` has not already said.
- **Prose.** A document that describes a printed string (`STALE`), a CLI flag, or a record
  key is not telling a reader to call a name.

A module that contributes no promised name declares `__all__: list[str] = []` with a line
saying how it is actually reached. A module whose own name starts with an underscore —
`_report.py` — declares nothing: the underscore is the statement, and an `__all__` there
would govern only `from runprov._report import *` while *not* governing
`from ._report import diagnostic`, which is how every caller imports it.

There is a fourth state beside PUBLIC / INTERNAL / PRIVATE. **WITHDRAWN** holds the five
names taken off the package surface on 2026-08-19: `VOLATILE`, `VOLATILE_JSON`,
`PIN_UNSAFE`, `default_run_id`, `default_generation`. They stay on their own modules, keep
their un-underscored names — a test pins their reachability as *withdrawn, not deleted* —
and appear in no `__all__` anywhere.

**Rename before declaring.** A name that is going to change must change before it appears in
any list, because afterwards it is recorded in two places instead of one. Five landed first
for this reason: `environment.render`→`_render_snapshot` and `verify.render`→`render_report`
(two different functions sharing a word, neither raising on the other's input),
`verify.ANCHOR`→`hashing.PIN_ANCHOR` (the one sentence identifying a pin, written twice),
and the deletion of `show.SHORT` and `run.SERIALISATION_ERRORS`.

## Consequences

A test asserts both directions, because each catches a different mistake: a module list that
**misses** a promised name means the package promises something its own module treats as
internal; a module list that **adds** one means a module quietly widened the surface.

The declarations are cheap to change while nothing is published and expensive afterwards,
which is why they are being made now rather than discovered later.

**One name is ratified with an open question against it.** `project.git` is in the package
`__all__`, so `project.py` must list it — but a bare `git` collides with GitPython's
top-level module in an importing namespace, and the contract is *swallow every exception,
return None, 20-second timeout*: provenance capture, not a general-purpose runner. It has
zero documentation references. Withdrawing or renaming it is a package-level decision and is
still open. This ADR is the reason the module could not take it.

Five of the 22 promised names have no documented user at all — `installed_packages`,
`write_snapshot`, `Capture`, `git` and `MemorySink`. That is a question for the ledger, not
for the modules, and the modules ratify them meanwhile.
