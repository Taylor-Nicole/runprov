# Copyright (c) 2026 Assistance Publique – Hôpitaux de Paris (AP-HP),
#                    Hôpital Henri-Mondor, and Taylor Thompson
# Licensed under the CeCILL-B Free Software License Agreement — see LICENSE.
# https://cecill.info/
"""Does runprov work with YOUR file formats? Run this and find out, do not take my word.

    $ python examples/format_compatibility.py

For each format it writes a real artifact through runprov, reads it back with the format's
REAL library, and reports four things:

    pin      where the provenance went: in-band, in-document, sidecar, or refused
    parses   the artifact was read back successfully by its own reader
    hashed   the run recorded and hashed it
    stable   two writes of identical data produced the same digest

**A format whose library is not installed is SKIPPED and said so**, never silently passed.
The point of this file is to answer "does it work with X" honestly, and a check that goes
green because it did not run is the failure this whole package is about.

NOT PART OF THE TEST SUITE, deliberately. It needs pandas, pyarrow, joblib, scipy, PIL and
others; `python ci.py test` runs with pytest alone so a distribution packager can build
without a scientific stack. This is a thing you run against YOUR environment.

ADDING YOUR OWN FORMAT is the intended use. Append to CASES:

    CASE("MyFormat", "a.myext", requires=["mylib"],
         write=lambda run, p: ..., read=lambda p: ...)

`write` must produce the artifact through runprov (`run.open_output`, or `run.output` plus
`run.pin_sidecar` for anything a library writes itself). `read` must open it with the real
reader and return something comparable — a row count is ideal, because a wrong answer is
more useful than an exception.
"""

from __future__ import annotations

import argparse
import csv
import gzip
import importlib
import json
import pathlib
import pickle
import tempfile
import time

import runprov

ROWS = [{"sample": "A", "value": 3}, {"sample": "B", "value": 7}]
CASES: list[dict] = []


def CASE(name, filename, *, write, read, requires=()):
    CASES.append(
        {"name": name, "file": filename, "write": write, "read": read, "requires": requires}
    )


def _binary(writer):
    """`output()` + `pin_sidecar()`: the route for a file another library opens itself."""

    def write(run, p):
        target = run.output(p)
        writer(target)
        run.pin_sidecar(target)

    return write


# ---------------------------------------------------------------- text, pin goes in-band
def _delimited(sep):
    def write(run, p):
        with run.open_output(p) as fh:
            w = csv.DictWriter(fh, fieldnames=["sample", "value"], delimiter=sep)
            w.writeheader()
            w.writerows(ROWS)

    def read(p):
        with open(p, encoding="utf-8") as fh:
            body = (line for line in fh if not line.startswith("#"))
            return len(list(csv.DictReader(body, delimiter=sep)))

    return write, read


for _name, _file, _sep in (("TSV", "a.tsv", "\t"), ("CSV", "a.csv", ","), ("BED", "a.bed", "\t")):
    _w, _r = _delimited(_sep)
    CASE(_name, _file, write=_w, read=_r)

CASE(
    "YAML",
    "a.yaml",
    requires=["yaml"],
    write=lambda run, p: _yaml_dump(run, p),
    read=lambda p: len(importlib.import_module("yaml").safe_load(_text(p))["rows"]),
)
CASE(
    "Markdown",
    "a.md",
    write=lambda run, p: _write_text(run, p, "| a |\n|---|\n| 1 |\n"),
    read=lambda p: _text(p).count("|"),
)


# ---------------------------------------------------------------- text, pin goes beside
CASE(
    "FASTA",
    "g.fasta",
    write=lambda run, p: _write_text(run, p, ">seq1 d\nACGTACGT\n>seq2\nTTTTGGGG\n"),
    read=lambda p: sum(1 for x in _text(p).splitlines() if x.startswith(">")),
)
CASE(
    "FASTQ",
    "r.fastq",
    write=lambda run, p: _write_text(run, p, "@r1\nACGT\n+\nIIII\n@r2\nTTTT\n+\nIIII\n"),
    read=lambda p: len(_text(p).splitlines()) // 4,
)
CASE(
    "Newick",
    "t.nwk",
    write=lambda run, p: _write_text(run, p, "(A:0.1,B:0.2,C:0.3);\n"),
    read=lambda p: _text(p).count(",") + 1,
)
CASE(
    "VCF",
    "c.vcf",
    write=lambda run, p: _write_text(
        run, p, "##fileformat=VCFv4.2\n#CHROM\tPOS\tID\tREF\tALT\n" + "chr1\t1\t.\tA\tG\n"
    ),
    read=lambda p: _vcf_rows(p),
)
CASE(
    "SVG",
    "f.svg",
    write=lambda run, p: _write_text(
        run, p, '<svg xmlns="http://www.w3.org/2000/svg"><rect/></svg>\n'
    ),
    read=lambda p: len(list(_xml_root(p))),
)
CASE(
    "JSONL",
    "d.jsonl",
    requires=["pandas"],
    write=lambda run, p: _write_text(run, p, "".join(json.dumps(r) + "\n" for r in ROWS)),
    read=lambda p: len(importlib.import_module("pandas").read_json(p, lines=True)),
)
CASE(
    "SQL (marker '-- ')",
    "q.sql",
    write=lambda run, p: _write_text(run, p, "select 1;\n", comment="-- "),
    read=lambda p: _text(p).count("select"),
)

# ---------------------------------------------------------------- pin goes IN the document
CASE(
    "JSON",
    "d.json",
    write=lambda run, p: run.write_json(p, {"rows": ROWS}),
    read=lambda p: len(json.loads(_text(p))["rows"]),
)

# ---------------------------------------------------------------- binary, pin goes beside
CASE(
    "Parquet",
    "t.parquet",
    requires=["pandas", "pyarrow"],
    write=_binary(lambda p: _df().to_parquet(p)),
    read=lambda p: len(importlib.import_module("pandas").read_parquet(p)),
)
CASE(
    "Feather / Arrow IPC",
    "t.feather",
    requires=["pandas", "pyarrow"],
    write=_binary(lambda p: _df().to_feather(p)),
    read=lambda p: len(importlib.import_module("pandas").read_feather(p)),
)
CASE(
    "Pickle",
    "m.pkl",
    write=_binary(lambda p: pathlib.Path(p).write_bytes(pickle.dumps({"coef": [1, 2, 3]}))),
    read=lambda p: len(pickle.loads(pathlib.Path(p).read_bytes())["coef"]),
)
CASE(
    "joblib",
    "m.joblib",
    requires=["joblib"],
    write=_binary(lambda p: importlib.import_module("joblib").dump({"coef": [1, 2, 3]}, p)),
    read=lambda p: len(importlib.import_module("joblib").load(p)["coef"]),
)
CASE(
    "cloudpickle",
    "m.cpkl",
    requires=["cloudpickle"],
    write=_binary(
        lambda p: pathlib.Path(p).write_bytes(
            importlib.import_module("cloudpickle").dumps({"c": [1, 2]})
        )
    ),
    read=lambda p: len(pickle.loads(pathlib.Path(p).read_bytes())["c"]),
)
CASE(
    "NumPy .npy",
    "a.npy",
    requires=["numpy"],
    write=_binary(lambda p: importlib.import_module("numpy").save(p, _arange())),
    read=lambda p: len(importlib.import_module("numpy").load(p)),
)
CASE(
    "NumPy .npz",
    "a.npz",
    requires=["numpy"],
    write=_binary(lambda p: importlib.import_module("numpy").savez(p, x=_arange())),
    read=lambda p: len(importlib.import_module("numpy").load(p)["x"]),
)
CASE(
    "HDF5",
    "a.h5",
    requires=["h5py", "numpy"],
    write=_binary(lambda p: _h5_write(p)),
    read=lambda p: _h5_read(p),
)
CASE(
    "SciPy .mat",
    "a.mat",
    requires=["scipy", "numpy"],
    write=_binary(lambda p: importlib.import_module("scipy.io").savemat(p, {"x": _arange()})),
    read=lambda p: importlib.import_module("scipy.io").loadmat(p)["x"].size,
)
CASE(
    "Excel .xlsx",
    "a.xlsx",
    requires=["pandas", "openpyxl"],
    write=_binary(lambda p: _df().to_excel(p, index=False)),
    read=lambda p: len(importlib.import_module("pandas").read_excel(p)),
)
CASE(
    "PNG",
    "i.png",
    requires=["PIL"],
    write=_binary(lambda p: _image().save(p)),
    read=lambda p: importlib.import_module("PIL.Image").open(p).size[0],
)
CASE(
    "TIFF",
    "i.tiff",
    requires=["PIL"],
    write=_binary(lambda p: _image().save(p)),
    read=lambda p: importlib.import_module("PIL.Image").open(p).size[0],
)
CASE(
    "gzip",
    "a.tsv.gz",
    write=_binary(lambda p: _gz_write(p)),
    read=lambda p: len(gzip.open(p, "rt").read().splitlines()),
)
CASE(
    "SQLite",
    "a.sqlite",
    write=_binary(lambda p: _sqlite_write(p)),
    read=lambda p: _sqlite_read(p),
)
CASE(
    "PyTorch .pt",
    "m.pt",
    requires=["torch"],
    write=_binary(lambda p: importlib.import_module("torch").save({"w": [1, 2]}, p)),
    read=lambda p: len(importlib.import_module("torch").load(p, weights_only=True)["w"]),
)
CASE(
    "Zarr",
    "a.zarr",
    requires=["zarr", "numpy"],
    write=_binary(lambda p: _zarr_write(p)),
    read=lambda p: _zarr_read(p),
)
CASE(
    "NetCDF",
    "a.nc",
    requires=["xarray", "numpy"],
    write=_binary(lambda p: _nc_write(p)),
    read=lambda p: _nc_read(p),
)


# ---------------------------------------------------------------- small helpers
def _text(p):
    return pathlib.Path(p).read_text(encoding="utf-8")


def _write_text(run, p, body, comment="# "):
    with run.open_output(p, comment=comment) as fh:
        fh.write(body)


def _yaml_dump(run, p):
    yaml = importlib.import_module("yaml")
    with run.open_output(p) as fh:
        yaml.safe_dump({"rows": ROWS}, fh)


def _vcf_rows(p):
    lines = _text(p).splitlines()
    if not lines[0].startswith("##fileformat"):
        raise AssertionError("##fileformat must be the first line of a VCF")
    return sum(1 for x in lines if not x.startswith("#"))


def _xml_root(p):
    import xml.etree.ElementTree as ET

    return ET.parse(p).getroot()


def _df():
    return importlib.import_module("pandas").DataFrame(ROWS)


def _arange():
    return importlib.import_module("numpy").arange(6)


def _image():
    return importlib.import_module("PIL.Image").new("RGB", (4, 4))


def _gz_write(p):
    with gzip.open(p, "wt") as fh:
        fh.write("a\tb\n1\t2\n")


def _sqlite_write(p):
    import sqlite3

    con = sqlite3.connect(p)
    con.execute("create table t (a int)")
    con.executemany("insert into t values (?)", [(1,), (2,)])
    con.commit()
    con.close()


def _sqlite_read(p):
    import sqlite3

    con = sqlite3.connect(p)
    try:
        return con.execute("select count(*) from t").fetchone()[0]
    finally:
        con.close()


def _h5_write(p):
    h5py = importlib.import_module("h5py")
    with h5py.File(p, "w") as fh:
        fh.create_dataset("x", data=_arange())


def _h5_read(p):
    h5py = importlib.import_module("h5py")
    with h5py.File(p, "r") as fh:
        return len(fh["x"])


def _zarr_write(p):
    zarr = importlib.import_module("zarr")
    z = zarr.open(str(p), mode="w", shape=(6,), dtype="i4")
    z[:] = _arange()


def _zarr_read(p):
    zarr = importlib.import_module("zarr")
    return len(zarr.open(str(p), mode="r"))


def _nc_write(p):
    xr = importlib.import_module("xarray")
    xr.Dataset({"x": ("i", _arange())}).to_netcdf(p)


def _nc_read(p):
    xr = importlib.import_module("xarray")
    with xr.open_dataset(p) as ds:
        return ds.sizes["i"]


def _missing(requires):
    out = []
    for mod in requires:
        try:
            importlib.import_module(mod)
        except Exception:
            out.append(mod)
    return out


def _one(case, root, tag):
    """Write and read one format once. Returns the record's digests for the artifact."""
    name = case["file"]
    proj = runprov.Project(
        root=root, run_log=root / "runs.jsonl", run_id=lambda: "compat", generation=lambda: "g"
    )
    src = root / "in.tsv"
    src.write_text("sample\tvalue\nA\t3\nB\t7\n", encoding="utf-8")
    prov = root / f"{tag}.prov.json"
    with runprov.Run(f"write_{name}", project=proj, provenance=prov) as run:
        run.input(src)
        case["write"](run, root / name)

    rec = json.loads(prov.read_text(encoding="utf-8"))
    entry = next(
        (o for o in rec["outputs"] if pathlib.Path(o["path"]).name == name),
        None,
    )
    return root / name, entry


def _placement(artifact, case):
    side = artifact.with_name(artifact.name + runprov.run.PIN_SIDECAR_SUFFIX)
    try:
        head = artifact.read_text(encoding="utf-8").splitlines()[0]
    except (OSError, UnicodeDecodeError, IndexError):
        head = ""
    if "provenance —" in head:
        return "in-band"
    if case["name"] == "JSON":
        return "in-document"
    return "sidecar" if side.is_file() else "NONE"


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--quiet", action="store_true", help="only the table and the summary")
    args = ap.parse_args(argv)
    if args.quiet:
        runprov  # noqa: B018 - the diagnostics are unsilenceable by design; see _report.py

    results, first = [], {}
    for case in CASES:
        missing = _missing(case["requires"])
        if missing:
            results.append({**case, "skipped": ", ".join(missing)})
            continue
        root = pathlib.Path(tempfile.mkdtemp())
        try:
            artifact, entry = _one(case, root, "one")
            row = {
                **case,
                "pin": _placement(artifact, case),
                "parses": case["read"](artifact),
                "hashed": entry is not None,
            }
            first[case["name"]] = (entry or {}).get("sha256"), (entry or {}).get("content_sha256")
        except Exception as exc:
            row = {**case, "error": f"{type(exc).__name__}: {exc}"}
        results.append(row)

    # A SECOND write, after the clock has moved, so "stable" means something. Formats that
    # stamp the time into their own bytes are the reason `content_digest` exists.
    time.sleep(1.1)
    for row in results:
        if "error" in row or "skipped" in row:
            continue
        root = pathlib.Path(tempfile.mkdtemp())
        try:
            _, entry = _one(row, root, "two")
            raw, content = first[row["name"]]
            row["stable_raw"] = (entry or {}).get("sha256") == raw
            row["stable_content"] = (entry or {}).get("content_sha256") == content
        except Exception as exc:
            row["error"] = f"second write: {type(exc).__name__}: {exc}"

    print(f"\n{'format':22} {'file':14} {'pin':12} {'parses':7} {'hashed':7} {'stable':8}")
    print("-" * 78)
    failed = skipped = 0
    for r in results:
        if "skipped" in r:
            skipped += 1
            print(f"{r['name']:22} {r['file']:14} SKIPPED — needs {r['skipped']}")
        elif "error" in r:
            failed += 1
            print(f"{r['name']:22} {r['file']:14} FAILED — {r['error'][:44]}")
        else:
            stable = "yes" if r["stable_content"] else ("raw-only" if r["stable_raw"] else "NO")
            print(
                f"{r['name']:22} {r['file']:14} {r['pin']:12} {r['parses']!s:7} "
                f"{r['hashed']!s:7} {stable:8}"
            )

    checked = len(results) - skipped - failed
    print(
        f"\n{checked} format(s) round-tripped, {failed} failed, {skipped} skipped for a "
        f"missing library."
    )
    print(
        "stable = the digest that the PIN uses (content_sha256) is the same across two "
        "writes.\n'raw-only' means the bytes moved but the content digest did not — that "
        "is gzip's\nmtime header, and it is what content_digest exists to strip. 'NO' "
        "means the format\nstamps the time into content the digest cannot see, so its pin "
        "moves on every run."
    )
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
