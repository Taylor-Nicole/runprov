# Copyright (c) 2026 Assistance Publique – Hôpitaux de Paris (AP-HP),
#                    Hôpital Henri-Mondor, and Taylor Thompson
# SPDX-License-Identifier: BSD-3-Clause
# Licensed under the BSD 3-Clause License — see LICENSE.
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
    write=_binary(lambda p: _torch_write(p)),
    read=lambda p: _torch_read(p),
)
CASE(
    "Zarr",
    "a.zarr",
    requires=["zarr", "numpy"],
    write=_binary(lambda p: _zarr_write(p)),
    read=lambda p: _zarr_read(p),
)
CASE(
    "safetensors",
    "m.safetensors",
    requires=["safetensors", "numpy"],
    write=_binary(lambda p: _safetensors_write(p)),
    read=lambda p: _safetensors_read(p),
)
CASE(
    "ONNX",
    "m.onnx",
    requires=["onnx"],
    write=_binary(lambda p: _onnx_write(p)),
    read=lambda p: len(importlib.import_module("onnx").load(p).graph.node),
)
CASE(
    "BAM",
    "a.bam",
    requires=["pysam"],
    write=_binary(lambda p: _bam_write(p)),
    read=lambda p: _bam_read(p),
)
CASE(
    "CRAM",
    "a.cram",
    requires=["pysam"],
    write=_binary(lambda p: _cram_write(p)),
    read=lambda p: _cram_read(p),
)
CASE(
    "VCF, bgzipped",
    "c.vcf.gz",
    requires=["pysam"],
    write=_binary(lambda p: _bgzf_vcf_write(p)),
    read=lambda p: _bgzf_vcf_read(p),
)
CASE(
    "AnnData .h5ad",
    "a.h5ad",
    requires=["anndata", "numpy"],
    write=_binary(lambda p: _h5ad_write(p)),
    read=lambda p: _h5ad_read(p),
)
CASE(
    "R .rds",
    "a.rds",
    requires=["pyreadr"],
    write=_binary(lambda p: _rds_write(p)),
    read=lambda p: _rds_read(p),
)
# ------------------------------------------------- DIRECTORY-shaped artifacts
# A different code path: `describe()` hashes a directory as a TREE, not a file. A model or a
# dataset that is a folder is normal in 2026 -- TensorFlow SavedModel, a partitioned Parquet
# dataset, a CellRanger `outs/`, a Delta table -- and none of them is a single file to pin.
CASE(
    "Parquet dataset (dir)",
    "ds.parquet",
    requires=["pandas", "pyarrow"],
    write=_binary(lambda p: _parquet_dataset(p)),
    read=lambda p: len(importlib.import_module("pandas").read_parquet(p)),
)
CASE(
    "Model dir (SavedModel shape)",
    "model_dir",
    write=_binary(lambda p: _model_dir(p)),
    read=lambda p: sum(1 for _ in pathlib.Path(p).rglob("*") if _.is_file()),
)

# ------------------------------------------------- an artifact plus its INDEX
# Not a format question but a provenance one: register only the BAM and the index is not
# pinned, so a stale `.bai` is invisible to every check. Both are registered here.
CASE(
    "BAM + .bai index",
    "idx.bam",
    requires=["pysam"],
    write=lambda run, p: _bam_with_index(run, p),
    read=lambda p: _bam_read(p),
)

# ------------------------------------------------- more ML / AI
CASE(
    "DuckDB",
    "a.duckdb",
    requires=["duckdb"],
    write=_binary(lambda p: _duckdb_write(p)),
    read=lambda p: _duckdb_read(p),
)
CASE(
    "GGUF",
    "m.gguf",
    requires=["gguf", "numpy"],
    write=_binary(lambda p: _gguf_write(p)),
    read=lambda p: _gguf_read(p),
)
CASE(
    "ORC",
    "t.orc",
    requires=["pyarrow"],
    write=_binary(lambda p: _orc_write(p)),
    read=lambda p: _orc_read(p),
)
CASE(
    "Avro",
    "t.avro",
    requires=["fastavro"],
    write=_binary(lambda p: _avro_write(p)),
    read=lambda p: _avro_read(p),
)
CASE(
    "MessagePack",
    "t.msgpack",
    requires=["msgpack"],
    write=_binary(lambda p: _msgpack_write(p)),
    read=lambda p: len(importlib.import_module("msgpack").unpackb(pathlib.Path(p).read_bytes())),
)
CASE(
    "XGBoost .ubj",
    "m.ubj",
    requires=["xgboost", "numpy"],
    write=_binary(lambda p: _xgb_write(p)),
    read=lambda p: _xgb_read(p),
)
CASE(
    "LightGBM .txt",
    "m.lgb.txt",
    requires=["lightgbm", "numpy"],
    write=_binary(lambda p: _lgb_write(p)),
    read=lambda p: _lgb_read(p),
)
CASE(
    "Stata .dta",
    "t.dta",
    requires=["pandas"],
    write=_binary(lambda p: _df().to_stata(p, write_index=False)),
    read=lambda p: len(importlib.import_module("pandas").read_stata(p)),
)
CASE(
    "SPSS .sav",
    "t.sav",
    requires=["pyreadstat", "pandas"],
    write=_binary(lambda p: importlib.import_module("pyreadstat").write_sav(_df(), str(p))),
    read=lambda p: len(importlib.import_module("pyreadstat").read_sav(str(p))[0]),
)

# ------------------------------------------------- more bioinformatics
CASE(
    "Matrix Market .mtx",
    "a.mtx",
    requires=["scipy", "numpy"],
    write=_binary(lambda p: _mtx_write(p)),
    read=lambda p: _mtx_read(p),
)
CASE(
    "GFF3",
    "a.gff3",
    write=lambda run, p: _write_text(
        run, p, "##gff-version 3\nchr1\t.\tgene\t1\t9\t.\t+\t.\tID=g1\n"
    ),
    read=lambda p: sum(1 for x in _text(p).splitlines() if not x.startswith("#")),
)
CASE(
    "GenBank",
    "a.gb",
    requires=["Bio"],
    write=lambda run, p: _genbank_write(run, p),
    read=lambda p: _genbank_read(p),
)
CASE(
    "PDB",
    "a.pdb",
    requires=["Bio"],
    write=lambda run, p: _write_text(run, p, _pdb_body()),
    read=lambda p: _pdb_read(p),
)
CASE(
    "Stockholm MSA",
    "a.sto",
    requires=["Bio"],
    write=lambda run, p: _write_text(run, p, _stockholm_body()),
    read=lambda p: _msa_read(p, "stockholm"),
)
CASE(
    "PHYLIP MSA",
    "a.phy",
    requires=["Bio"],
    write=lambda run, p: _write_text(run, p, " 2 4\nseq1      ACGT\nseq2      TTTT\n"),
    read=lambda p: _msa_read(p, "phylip"),
)
CASE(
    "Nexus",
    "a.nex",
    requires=["Bio"],
    write=lambda run, p: _write_text(run, p, _nexus_body()),
    read=lambda p: _msa_read(p, "nexus"),
)
CASE(
    "BCF",
    "c.bcf",
    requires=["pysam"],
    write=_binary(lambda p: _bcf_write(p)),
    read=lambda p: _bcf_read(p),
)
CASE(
    "bigWig",
    "a.bw",
    requires=["pyBigWig"],
    write=_binary(lambda p: _bigwig_write(p)),
    read=lambda p: _bigwig_read(p),
)
CASE(
    "PLINK .bim/.fam/.bed",
    "plink.bim",
    write=lambda run, p: _plink_write(run, p),
    read=lambda p: len(_text(p).splitlines()),
)
CASE(
    "mzML",
    "a.mzML",
    requires=["pyteomics"],
    write=lambda run, p: _write_text(run, p, _mzml_body()),
    read=lambda p: len(list(_xml_root(p))),
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
    # `.shape[0]`, not `len()`: a zarr v3 Array is not Sized, and a reader that raises
    # TypeError would be reported as a runprov failure when it is this file's bug.
    zarr = importlib.import_module("zarr")
    return int(zarr.open(str(p), mode="r").shape[0])


def _nc_write(p):
    xr = importlib.import_module("xarray")
    xr.Dataset({"x": ("i", _arange())}).to_netcdf(p)


def _nc_read(p):
    xr = importlib.import_module("xarray")
    with xr.open_dataset(p) as ds:
        return ds.sizes["i"]


def _torch_write(p):
    # A TENSOR, not a list of ints: a `.pt` in the wild is a checkpoint, and a container
    # tested with a payload it never carries answers an easier question than the real one.
    #
    # A `.pt` IS A ZIP, and torch names the entries after the FILE STEM -- `m.pt` holds
    # `m/data.pkl`. So the same weights saved as `model_v1.pt` and `model_v2.pt` have
    # different digests, and RENAMING a checkpoint changes its hash. That is a property of
    # torch rather than of runprov, and it is worth knowing before a rename reads as a
    # retrain. Measured: identical weights to the same filename in two directories give the
    # same digest; to two filenames, they do not.
    torch = importlib.import_module("torch")
    torch.save({"w": torch.zeros(4), "epoch": 3}, str(p))


def _torch_read(p):
    torch = importlib.import_module("torch")
    return len(torch.load(str(p), weights_only=True)["w"])


def _safetensors_write(p):
    # The NUMPY backend, not the torch one: safetensors is a container format and testing it
    # should not require a 2 GB dependency to say whether the container round-trips.
    importlib.import_module("safetensors.numpy").save_file({"w": _arange()}, str(p))


def _safetensors_read(p):
    return len(importlib.import_module("safetensors.numpy").load_file(str(p))["w"])


def _onnx_write(p):
    onnx = importlib.import_module("onnx")
    h = importlib.import_module("onnx.helper")
    tp = importlib.import_module("onnx").TensorProto
    node = h.make_node("Identity", ["x"], ["y"])
    graph = h.make_graph(
        [node],
        "g",
        [h.make_tensor_value_info("x", tp.FLOAT, [1])],
        [h.make_tensor_value_info("y", tp.FLOAT, [1])],
    )
    onnx.save(h.make_model(graph), str(p))


def _sam_body():
    return (
        "@HD\tVN:1.6\tSO:coordinate\n@SQ\tSN:chr1\tLN:100\n"
        "r1\t0\tchr1\t1\t60\t4M\t*\t0\t0\tACGT\tIIII\n"
        "r2\t0\tchr1\t5\t60\t4M\t*\t0\t0\tTTTT\tIIII\n"
    )


def _bam_write(p):
    pysam = importlib.import_module("pysam")
    sam = pathlib.Path(p).with_suffix(".sam")
    sam.write_text(_sam_body(), encoding="utf-8")
    with (
        pysam.AlignmentFile(str(sam), "r") as src,
        pysam.AlignmentFile(str(p), "wb", header=src.header) as out,
    ):
        for rec in src:
            out.write(rec)


def _bam_read(p):
    pysam = importlib.import_module("pysam")
    with pysam.AlignmentFile(str(p), "rb") as fh:
        return sum(1 for _ in fh)


def _cram_write(p):
    pysam = importlib.import_module("pysam")
    ref = pathlib.Path(p).with_name("ref.fa")
    ref.write_text(">chr1\n" + "A" * 100 + "\n", encoding="utf-8")
    pysam.faidx(str(ref))
    sam = pathlib.Path(p).with_suffix(".sam")
    sam.write_text(_sam_body(), encoding="utf-8")
    with (
        pysam.AlignmentFile(str(sam), "r") as src,
        pysam.AlignmentFile(str(p), "wc", header=src.header, reference_filename=str(ref)) as out,
    ):
        for rec in src:
            out.write(rec)


def _cram_read(p):
    pysam = importlib.import_module("pysam")
    ref = pathlib.Path(p).with_name("ref.fa")
    with pysam.AlignmentFile(str(p), "rc", reference_filename=str(ref)) as fh:
        return sum(1 for _ in fh)


def _bgzf_vcf_write(p):
    pysam = importlib.import_module("pysam")
    plain = pathlib.Path(p).with_suffix("")
    plain.write_text(
        "##fileformat=VCFv4.2\n##contig=<ID=chr1,length=100>\n"
        "#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO\n"
        "chr1\t1\t.\tA\tG\t.\t.\t.\nchr1\t5\t.\tT\tC\t.\t.\t.\n",
        encoding="utf-8",
    )
    pysam.tabix_compress(str(plain), str(p), force=True)


def _bgzf_vcf_read(p):
    pysam = importlib.import_module("pysam")
    with pysam.VariantFile(str(p)) as fh:
        return sum(1 for _ in fh)


def _h5ad_write(p):
    ad = importlib.import_module("anndata")
    np = importlib.import_module("numpy")
    ad.AnnData(np.zeros((2, 3), dtype="float32")).write_h5ad(pathlib.Path(p))


def _h5ad_read(p):
    ad = importlib.import_module("anndata")
    return ad.read_h5ad(pathlib.Path(p)).n_obs


def _rds_write(p):
    pyreadr = importlib.import_module("pyreadr")
    pyreadr.write_rds(str(p), _df())


def _rds_read(p):
    pyreadr = importlib.import_module("pyreadr")
    return len(pyreadr.read_r(str(p))[None])


# ---------------------------------------------------------------- directory + index
def _parquet_dataset(p):
    pq = importlib.import_module("pyarrow.parquet")
    pa = importlib.import_module("pyarrow")
    pq.write_to_dataset(pa.Table.from_pylist(ROWS), root_path=str(p), partition_cols=["sample"])


def _model_dir(p):
    """The SHAPE of a TensorFlow SavedModel or an MLflow model: a directory of files.

    TensorFlow itself is a ~600 MB dependency to prove that a folder hashes as a tree, and
    the tree is the thing under test. `describe()` does not know or care what produced it.
    """
    root = pathlib.Path(p)
    (root / "variables").mkdir(parents=True, exist_ok=True)
    (root / "saved_model.pb").write_bytes(b"\x08\x01\x12\x04test")
    (root / "variables" / "variables.index").write_bytes(b"IDX\x00")
    (root / "fingerprint.pb").write_bytes(b"\x08\x02")


def _bam_with_index(run, p):
    """Both the BAM and its `.bai`, because registering only one pins only one.

    A stale index is a real failure mode and it is invisible to any check that never
    recorded the index in the first place.
    """
    pysam = importlib.import_module("pysam")
    bam = run.output(p)
    _bam_write(bam)
    pysam.index(str(bam))
    run.output(pathlib.Path(str(bam) + ".bai"))
    run.pin_sidecar(bam)


# ---------------------------------------------------------------- ML / AI
def _duckdb_write(p):
    duckdb = importlib.import_module("duckdb")
    con = duckdb.connect(str(p))
    con.execute("create table t (a integer)")
    con.execute("insert into t values (1), (2)")
    con.close()


def _duckdb_read(p):
    duckdb = importlib.import_module("duckdb")
    con = duckdb.connect(str(p), read_only=True)
    try:
        return con.execute("select count(*) from t").fetchone()[0]
    finally:
        con.close()


def _gguf_write(p):
    gguf = importlib.import_module("gguf")
    np = importlib.import_module("numpy")
    w = gguf.GGUFWriter(str(p), "demo")
    w.add_uint32("demo.count", 2)
    w.add_tensor("w", np.zeros((2, 2), dtype="float32"))
    w.write_header_to_file()
    w.write_kv_data_to_file()
    w.write_tensors_to_file()
    w.close()


def _gguf_read(p):
    gguf = importlib.import_module("gguf")
    return len(gguf.GGUFReader(str(p)).tensors)


def _orc_write(p):
    orc = importlib.import_module("pyarrow.orc")
    pa = importlib.import_module("pyarrow")
    orc.write_table(pa.Table.from_pylist(ROWS), str(p))


def _orc_read(p):
    orc = importlib.import_module("pyarrow.orc")
    return orc.read_table(str(p)).num_rows


def _avro_write(p):
    fastavro = importlib.import_module("fastavro")
    schema = {
        "type": "record",
        "name": "r",
        "fields": [{"name": "sample", "type": "string"}, {"name": "value", "type": "int"}],
    }
    with open(p, "wb") as fh:
        fastavro.writer(fh, fastavro.parse_schema(schema), ROWS)


def _avro_read(p):
    fastavro = importlib.import_module("fastavro")
    with open(p, "rb") as fh:
        return len(list(fastavro.reader(fh)))


def _msgpack_write(p):
    msgpack = importlib.import_module("msgpack")
    pathlib.Path(p).write_bytes(msgpack.packb(ROWS))


def _xgb_write(p):
    xgb = importlib.import_module("xgboost")
    np = importlib.import_module("numpy")
    m = xgb.XGBRegressor(n_estimators=2, max_depth=2, random_state=0)
    m.fit(np.arange(8).reshape(4, 2), np.array([0.0, 1.0, 2.0, 3.0]))
    m.save_model(str(p))


def _xgb_read(p):
    xgb = importlib.import_module("xgboost")
    m = xgb.XGBRegressor()
    m.load_model(str(p))
    return m.n_estimators or 2


def _lgb_write(p):
    lgb = importlib.import_module("lightgbm")
    np = importlib.import_module("numpy")
    ds = lgb.Dataset(np.arange(40).reshape(20, 2), label=np.arange(20) % 2)
    booster = lgb.train({"objective": "binary", "verbose": -1, "num_leaves": 2}, ds, 2)
    booster.save_model(str(p))


def _lgb_read(p):
    lgb = importlib.import_module("lightgbm")
    return lgb.Booster(model_file=str(p)).num_trees()


# ---------------------------------------------------------------- bioinformatics
def _mtx_write(p):
    sio = importlib.import_module("scipy.io")
    sp = importlib.import_module("scipy.sparse")
    np = importlib.import_module("numpy")
    sio.mmwrite(str(p), sp.csr_matrix(np.eye(3)))


def _mtx_read(p):
    sio = importlib.import_module("scipy.io")
    return sio.mmread(str(p)).shape[0]


def _genbank_write(run, p):
    seqio = importlib.import_module("Bio.SeqIO")
    seq = importlib.import_module("Bio.Seq")
    rec = importlib.import_module("Bio.SeqRecord")
    r = rec.SeqRecord(seq.Seq("ACGTACGT"), id="X1", name="X1", description="demo")
    r.annotations["molecule_type"] = "DNA"
    with run.open_output(p) as fh:
        seqio.write([r], fh, "genbank")


def _genbank_read(p):
    seqio = importlib.import_module("Bio.SeqIO")
    return len(list(seqio.parse(str(p), "genbank")))


def _pdb_body():
    return (
        "ATOM      1  N   MET A   1      11.104  13.207  10.000  1.00 20.00           N\n"
        "ATOM      2  CA  MET A   1      12.104  14.207  11.000  1.00 20.00           C\n"
        "END\n"
    )


def _pdb_read(p):
    pdb = importlib.import_module("Bio.PDB")
    parser = pdb.PDBParser(QUIET=True)
    return len(list(parser.get_structure("s", str(p)).get_atoms()))


def _stockholm_body():
    return "# STOCKHOLM 1.0\nseq1 ACGT\nseq2 TTTT\n//\n"


def _nexus_body():
    return (
        "#NEXUS\nbegin data;\ndimensions ntax=2 nchar=4;\nformat datatype=dna;\n"
        "matrix\nseq1 ACGT\nseq2 TTTT\n;\nend;\n"
    )


def _msa_read(p, fmt):
    alignio = importlib.import_module("Bio.AlignIO")
    return len(alignio.read(str(p), fmt))


def _bcf_write(p):
    pysam = importlib.import_module("pysam")
    plain = pathlib.Path(p).with_suffix(".vcf")
    plain.write_text(
        "##fileformat=VCFv4.2\n##contig=<ID=chr1,length=100>\n"
        "#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO\n"
        "chr1\t1\t.\tA\tG\t.\t.\t.\nchr1\t5\t.\tT\tC\t.\t.\t.\n",
        encoding="utf-8",
    )
    with (
        pysam.VariantFile(str(plain)) as src,
        pysam.VariantFile(str(p), "wb", header=src.header) as out,
    ):
        for rec in src:
            out.write(rec)


def _bcf_read(p):
    pysam = importlib.import_module("pysam")
    with pysam.VariantFile(str(p)) as fh:
        return sum(1 for _ in fh)


def _bigwig_write(p):
    bw = importlib.import_module("pyBigWig").open(str(p), "w")
    bw.addHeader([("chr1", 100)])
    bw.addEntries(["chr1", "chr1"], [0, 10], ends=[5, 15], values=[1.0, 2.0])
    bw.close()


def _bigwig_read(p):
    bw = importlib.import_module("pyBigWig").open(str(p))
    try:
        return len(bw.chroms())
    finally:
        bw.close()


def _plink_write(run, p):
    """The PLINK trio: `.bim` and `.fam` are text, `.bed` is binary with a magic prefix.

    Three files ARE the artifact, so all three are registered. Pinning one of them would
    describe a third of a dataset.
    """
    stem = pathlib.Path(p).with_suffix("")
    with run.open_output(p) as fh:  # .bim
        fh.write("1\trs1\t0\t1\tA\tG\n1\trs2\t0\t5\tT\tC\n")
    fam = run.output(stem.with_suffix(".fam"))
    fam.write_text("F1 I1 0 0 1 -9\n", encoding="utf-8")
    bed = run.output(stem.with_suffix(".bed"))
    bed.write_bytes(b"\x6c\x1b\x01\x00\x00")
    run.pin_sidecar(bed)


def _mzml_body():
    return (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        '<mzML xmlns="http://psi.hupo.org/ms/mzml" version="1.1.0">'
        '<run id="r1"><spectrumList count="1"><spectrum index="0" id="scan=1"/>'
        "</spectrumList></run></mzML>\n"
    )


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
        "\nstable = the digest the PIN uses (content_sha256) is the same across two writes.\n"
        "  'raw-only'  the bytes moved, the content digest did not — that is gzip's mtime\n"
        "              header, and stripping it is what content_digest exists for.\n"
        "  'NO'        the pin moves on every run, so any check over that artifact is\n"
        "              permanently red. FOUR here, and the causes are not the same:\n"
        "                SciPy .mat  writes `Created on: <date>` into its header\n"
        "                SPSS  .sav  one byte at offset 108 — the creation TIME\n"
        "                CRAM        differs across two writes of identical records with\n"
        "                            an identical reference path\n"
        "                Avro        a RANDOM 16-byte sync marker per file, not a clock —\n"
        "                            so freezing time would not help it\n"
        "              Prefer .npz over .mat and Parquet over Avro; for CRAM pin the BAM.\n"
        "              Otherwise record the sha256 and accept that this artifact moves."
    )
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
