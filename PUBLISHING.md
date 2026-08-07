# Publishing `runprov`

Status as of 2026-08-07: the name **`runprov` is free on PyPI** (checked; `/pypi/runprov/json`
returns 404 while a control package returns 200). Everything below is prepared and nothing
has been published — that decision, and the account it happens under, are yours.

## The one blocking decision

**There is no LICENSE file in this repository.** Without one the code is "all rights
reserved" by default: nobody may legally use, copy or redistribute it, PyPI will accept the
upload but the package is unusable, and JOSS and every software registry will reject it.

It has to be decided before anything else because it determines the copyright holder — and
under **CPI art. L113-9** the economic rights in software written by an employee or public
agent vest in the employer automatically, with no assignment and no signature. So the holder
is probably your institution, not you. **[LICENSING.md](LICENSING.md)** works that through,
with the exact questions to put to the DRCI.

Recommendation, so it is not buried: **MIT, holder = the CHU, plus a DCO from the first
commit.** MIT can be relicensed to Apache-2.0 later for future versions — but only while the
rights holder is a single party, which is precisely what the DCO preserves.

Once you know the holder:

| licence | choose it when |
|---|---|
| **MIT** | you want the widest possible use with the least friction. The default for small research utilities, and the easiest for other labs' legal teams to approve. **Recommended.** |
| **BSD-3-Clause** | same practical effect as MIT, plus an explicit no-endorsement clause. Some institutions prefer it. |
| **Apache-2.0** | adds an express patent grant and a contribution clause. Preferred by industry users; slightly heavier. |

Avoid a copyleft licence (GPL) here unless the institution requires it: a provenance module
is imported by everything, and a copyleft dependency at that position propagates its terms
into every pipeline that uses it, which is the surest way to have people not use it.

## Extraction

`runprov` currently ships inside the `hcv-genotyping` wheel. To publish it on its own it
needs its own repository:

```bash
make tools-extract-runprov DEST=../runprov \
     AUTHOR="Your Name" EMAIL="you@example.org" \
     HOMEPAGE="https://github.com/<you>/runprov"
```

That is a committed script, not a copy-paste: it records its own provenance (with `runprov`),
asserts the distribution version matches `__version__`, adjusts the tests' root depth, and
**exits non-zero while a LICENSE is missing** rather than reporting success on something that
cannot legally be published.

Then, in the new directory:

```bash
git init && git add -A && git commit -m "runprov 0.1.0"
```

## Build and check, before anything is public

```bash
python -m pip install --upgrade build twine
python -m build                    # -> dist/runprov-0.1.0{.tar.gz,-py3-none-any.whl}
python -m twine check --strict dist/*
```

`twine check` catches a README that PyPI will refuse to render. Fix it now: **a version can
never be re-uploaded**, so a broken description on 0.1.0 is permanent and the only remedy is
publishing 0.1.1.

Then verify the built artifact actually works, from the wheel rather than the source tree —
this is what catches a missing file in the package data:

```bash
python -m venv /tmp/v && /tmp/v/bin/pip install dist/runprov-0.1.0-py3-none-any.whl
/tmp/v/bin/python -c "import runprov; print(runprov.__version__, runprov.Run)"
```

## TestPyPI first

```bash
python -m twine upload --repository testpypi dist/*
python -m pip install --index-url https://test.pypi.org/simple/ runprov
```

TestPyPI is a separate account and a separate name reservation. It exists precisely so the
irreversible step is rehearsed once.

## PyPI

Two ways, and the second is better:

**Token upload** — create a scoped API token at pypi.org, then `python -m twine upload dist/*`.
Simple, but the token is a long-lived secret you now have to keep somewhere.

**Trusted publishing (recommended)** — GitHub Actions authenticates to PyPI over OIDC, so no
token exists to leak. The extraction script writes `.github/workflows/publish.yml` that
builds, runs the tests on 3.10–3.13, and publishes on a `v*` tag. Configure the publisher
once at <https://pypi.org/manage/account/publishing/> with owner, repo, workflow filename
`publish.yml`, environment `pypi`. After that, releasing is:

```bash
git tag v0.1.0 && git push --tags
```

## Version discipline

`runprov/__init__.py` holds `__version__`; the extraction script reads it and fails if the
distribution disagrees. Keep them one string.

Start at **0.1.0**, not 1.0.0. `0.x` says the API may still move, which is honest — the
`with`-block failure recording arrived after the first version of this package existed, and
the environment snapshot after that. Go to 1.0.0 when a second project has used it for a
while and you have stopped changing the signatures.

## Should it be published at all?

Two honest arguments against, worth having answers to before you do it:

* **You take on maintenance.** An installed package that stops working on Python 3.15 is now
  a small obligation. The counter-argument: it has zero dependencies, so almost nothing can
  break it from outside.
* **It is not novel.** `provenance`, `recipy`, `sumatra` and `dvc` all exist. The honest
  positioning is not "new idea" — it is "the smallest possible one, with the failure modes
  documented from a real project that hit every one of them". See the README.

And the argument for, which is the one that matters for your thesis: **a reviewer can
install it and check your claims.** A provenance mechanism described in a Methods section is
an assertion. A provenance mechanism with a version number, a test suite and a public
release is a thing someone can run against your artifacts.
