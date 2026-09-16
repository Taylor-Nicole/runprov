# Copyright (c) 2026 Assistance Publique – Hôpitaux de Paris (AP-HP),
#                    Hôpital Henri-Mondor, and Taylor Nicole Thompson
# SPDX-License-Identifier: BSD-3-Clause
# Licensed under the BSD 3-Clause License — see LICENSE.
"""What a run consumed, measured rather than declared. ADR-0013, T-29.

THE PROBLEM, in the words it was raised in: to move a pipeline onto a cluster you must declare
`--mem` and `--time` before you have ever run it there. `#SBATCH --mem=64G` is a statement
about what the author BELIEVES the job needs — the same shape as a hand-maintained log, one
domain across — so recording what a run actually used is this package's own argument applied
to a different question.

IT IS NOT A PROFILER. It says how much, never where it went.

THE NUMBER A SCHEDULER ENFORCES IS NOT THE NUMBER `getrusage` REPORTS, and the difference runs
in the dangerous direction. Slurm's `--mem` and Kubernetes' memory limit are enforced against
the CGROUP, which counts every process concurrently plus page cache; `RUSAGE_CHILDREN` is the
high-water mark of the LARGEST SINGLE CHILD. Measured: three children holding ~150 MiB
concurrently report 162 MiB, not 450. A figure from here is a FLOOR, and the block says so.

Every requirement below is numbered in ADR-0013 and checked by
`test_every_resource_requirement_has_a_test`, which derives the list from the ADR.
"""

from __future__ import annotations

__all__: list[str] = []

import os
import pathlib
import time
import typing

try:  # `resource` is POSIX-only; Windows has no equivalent, which is a capability fact
    import resource as _resource
except ImportError:  # pragma: no cover - exercised on Windows, and by injection in tests
    _resource = None  # type: ignore[assignment]

#: Environment variables that prove a scheduler gave this run a cgroup of its own. R-6.
SCHEDULER_VARS = ("SLURM_JOB_ID", "SLURM_STEP_ID", "KUBERNETES_SERVICE_HOST")

#: Path fragments that say the same thing when the variables are absent — a container runtime
#: puts the run in its own group whether or not it exports anything.
CONTAINER_MARKS = ("kubepods", "docker", "containerd", "slurm", "lxc")

#: Snakemake's benchmark columns, read from its SOURCE (`src/snakemake/benchmark.py`) because
#: its documentation does not list them. Emitted in this order so existing tooling reads the
#: file without being told anything. R-13.
SNAKEMAKE_COLUMNS = (
    "s",
    "h:m:s",
    "max_rss",
    "max_vms",
    "max_uss",
    "max_pss",
    "io_in",
    "io_out",
    "mean_load",
    "cpu_time",
)

MIB = 1024 * 1024

#: A sentinel for "not given", so `rusage=None` can mean "this platform HAS no `resource`
#: module" — which is the Windows case and therefore the one most worth being able to inject.
#: With `None` as the default, absence was unrepresentable and U-4 could not be tested at all.
_UNSET: typing.Any = object()


def _maxrss_to_bytes(value: int, platform: str) -> int:
    """`ru_maxrss` in bytes. R-4.

    THE UNITS DIFFER BY PLATFORM and the difference is 1024x: Linux reports kibibytes, macOS
    reports bytes. Getting it wrong is an error that looks entirely plausible on whichever
    platform you tested on, which is why this is a function with a test rather than a
    multiplication inline.
    """
    return value if platform == "darwin" else value * 1024


def owns_cgroup(
    environ: typing.Mapping[str, str] | None = None, cgroup_line: str | None = None
) -> bool:
    """Does this run have a cgroup of its OWN? R-6.

    Outside a scheduler the answer is no, and reading the ambient group would be a disaster
    dressed as precision: measured on a workstation, it is the whole desktop session and
    reports 8 138 MiB of `memory.current` — the browser, the editor and everything else,
    charged to a script.
    """
    env = os.environ if environ is None else environ
    if any(env.get(name) for name in SCHEDULER_VARS):
        return True
    return any(mark in (cgroup_line or "") for mark in CONTAINER_MARKS)


def cgroup_path(
    cgroup_line: str, mount: pathlib.Path = pathlib.Path("/sys/fs/cgroup")
) -> pathlib.Path | None:
    """The cgroup v2 directory for this process, from a `/proc/self/cgroup` line.

    v2 writes exactly one line, `0::<path>`. A v1 line names controllers and is not usable
    the same way, so it is declined rather than half-handled.
    """
    for line in cgroup_line.splitlines():
        if line.startswith("0::"):
            return mount / line[3:].lstrip("/")
    return None


def _read_int(path: pathlib.Path) -> int | None:
    try:
        return int(path.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None


class Measurement(typing.NamedTuple):
    """One run's consumption, in canonical units. R-2: bytes and seconds, never `8G`."""

    wall_seconds: float
    cpu_seconds: float | None
    max_rss_bytes: int | None
    max_vms_bytes: int | None
    io_read_bytes: int | None
    io_write_bytes: int | None
    source: str  # "cgroup" | "getrusage" | "none"   R-5
    unavailable: tuple[str, ...]  # R-8
    io_self_only: bool  # R-9b

    def as_record(self) -> dict[str, typing.Any]:
        """The `resources` block. A figure that could not be measured is ABSENT, not 0. R-7."""
        out: dict[str, typing.Any] = {
            "wall_seconds": round(self.wall_seconds, 6),
            "source": self.source,
        }
        for key, value in (
            ("cpu_seconds", None if self.cpu_seconds is None else round(self.cpu_seconds, 6)),
            ("max_rss_bytes", self.max_rss_bytes),
            ("max_vms_bytes", self.max_vms_bytes),
            ("io_read_bytes", self.io_read_bytes),
            ("io_write_bytes", self.io_write_bytes),
        ):
            if value is not None:
                out[key] = value
        if self.unavailable:
            out["unavailable"] = list(self.unavailable)
        if self.io_self_only and (self.io_read_bytes is not None):
            # R-9b. `/proc/self/io` has no children's equivalent, so a pipeline whose reads
            # happen in samtools reports near zero. Said, rather than left to be assumed.
            out["io_self_only"] = True
        if self.source == "getrusage" and self.max_rss_bytes is not None:
            # R-9. RUSAGE_CHILDREN is a MAXIMUM, not a sum. The caveat travels WITH the
            # number, because the number alone gets a job OOM-killed.
            out["max_rss_is_floor"] = FLOOR_NOTE
        return out


#: The prose caveat is DERIVED from `source`, never stored twice. It is ~120 characters and
#: the history is appended forever; ten thousand runs would carry ten thousand copies of one
#: sentence. The history therefore projects the block without it, the same shape as `steps`
#: carrying a count rather than a list, and `from_record` puts it back.
FLOOR_NOTE = (
    "the maximum of any one child, not the concurrent total; "
    "a scheduler enforces the cgroup total, which is larger"
)


def from_record(block: typing.Mapping[str, typing.Any]) -> Measurement:
    """Rebuild a `Measurement` from a recorded block, so the renderers can read a history.

    A field the record does not carry comes back as None rather than 0 — R-7 holds in both
    directions, and a round trip that invented zeros would launder an absent measurement into
    a present one.
    """
    return Measurement(
        wall_seconds=float(block.get("wall_seconds") or 0.0),
        cpu_seconds=block.get("cpu_seconds"),
        max_rss_bytes=block.get("max_rss_bytes"),
        max_vms_bytes=block.get("max_vms_bytes"),
        io_read_bytes=block.get("io_read_bytes"),
        io_write_bytes=block.get("io_write_bytes"),
        source=str(block.get("source", "none")),
        unavailable=tuple(block.get("unavailable") or ()),
        io_self_only=bool(block.get("io_self_only")),
    )


class Meter:
    """Measures one run. Started at `__enter__`, read at the seal.

    Everything it reads is injected so that every branch runs on every platform, which is the
    `Observer` rule — and here it matters twice, because the interesting cases are a cgroup
    this machine does not have and a platform this machine is not.
    """

    def __init__(
        self,
        *,
        clock: typing.Callable[[], float] = time.monotonic,
        rusage: typing.Any = _UNSET,  # noqa: ANN401 - the module, a stub, or None for absent
        environ: typing.Mapping[str, str] | None = None,
        proc: pathlib.Path = pathlib.Path("/proc/self"),
        cgroup_mount: pathlib.Path = pathlib.Path("/sys/fs/cgroup"),
        platform: str | None = None,
    ) -> None:
        self._clock = clock
        self._resource = _resource if rusage is _UNSET else rusage
        self._environ = environ
        self._proc = proc
        self._cgroup_mount = cgroup_mount
        self._platform = platform if platform is not None else __import__("sys").platform
        self._started = clock()  # R-10: monotonic, so NTP cannot move it backwards mid-run

    # ------------------------------------------------------------------ the pieces
    def _cpu_and_rss(self) -> tuple[float | None, int | None]:
        """CPU seconds and peak RSS from `getrusage`, SELF and CHILDREN together. R-3."""
        if self._resource is None:
            return None, None
        try:
            me = self._resource.getrusage(self._resource.RUSAGE_SELF)
            kids = self._resource.getrusage(self._resource.RUSAGE_CHILDREN)
        except Exception:  # R-12: measurement never fails the run
            return None, None
        cpu = me.ru_utime + me.ru_stime + kids.ru_utime + kids.ru_stime
        # THE MAXIMUM OF THE TWO, not SELF alone: a run whose subprocess held 200 MiB reports
        # 15 708 KiB for SELF and 217 364 for CHILDREN, and the second is the true answer.
        peak = max(me.ru_maxrss, kids.ru_maxrss)
        return cpu, _maxrss_to_bytes(peak, self._platform)

    def _proc_value(self, name: str, key: str) -> int | None:
        try:
            for line in (self._proc / name).read_text(encoding="utf-8").splitlines():
                if line.startswith(key):
                    return int(line.split()[1])
        except (OSError, ValueError, IndexError):
            return None
        return None

    def _cgroup_peak(self) -> tuple[int | None, float | None, tuple[str, ...]]:
        """The cgroup's own peak, which IS what Slurm and Kubernetes enforce. R-5, R-6."""
        try:
            line = (self._proc / "cgroup").read_text(encoding="utf-8")
        except OSError:
            return None, None, ("cgroup: unreadable",)
        if not owns_cgroup(self._environ, line):
            return None, None, ("cgroup: not this run's own (no scheduler or container)",)
        directory = cgroup_path(line, self._cgroup_mount)
        if directory is None:
            return None, None, ("cgroup: v1, which has no comparable peak",)
        peak = _read_int(directory / "memory.peak")
        missing: tuple[str, ...] = ()
        if peak is None:
            # MEASURED: this project's own 5.15 kernel has memory.current and cpu.stat but no
            # memory.peak. `memory.current` is NOT a peak and is not substituted for one.
            missing = ("cgroup: memory.peak absent on this kernel",)
        cpu = None
        try:
            for stat in (directory / "cpu.stat").read_text(encoding="utf-8").splitlines():
                if stat.startswith("usage_usec"):
                    cpu = int(stat.split()[1]) / 1_000_000
        except (OSError, ValueError, IndexError):
            pass
        return peak, cpu, missing

    # ------------------------------------------------------------------ the answer
    def read(self) -> Measurement:
        """Measure now. Never raises. R-12."""
        wall = self._clock() - self._started
        unavailable: list[str] = []

        cg_rss, cg_cpu, cg_missing = self._cgroup_peak()
        unavailable.extend(cg_missing)
        ru_cpu, ru_rss = self._cpu_and_rss()
        if self._resource is None:
            unavailable.append("resource module: not on this platform")

        if cg_rss is not None:
            source, rss, cpu = "cgroup", cg_rss, (cg_cpu if cg_cpu is not None else ru_cpu)
        elif ru_rss is not None:
            source, rss, cpu = "getrusage", ru_rss, ru_cpu
        else:
            source, rss, cpu = "none", None, ru_cpu

        vms = self._proc_value("status", "VmPeak")
        if vms is not None:
            vms *= 1024  # /proc reports kB; the record is bytes. R-2.
        return Measurement(
            wall_seconds=wall,
            cpu_seconds=cpu,
            max_rss_bytes=rss,
            max_vms_bytes=vms,
            io_read_bytes=self._proc_value("io", "read_bytes"),
            io_write_bytes=self._proc_value("io", "write_bytes"),
            source=source,
            unavailable=tuple(unavailable),
            io_self_only=True,
        )


def _hms(seconds: float) -> str:
    """`h:m:s`, Snakemake's second column."""
    whole = int(seconds)
    return f"{whole // 3600}:{(whole % 3600) // 60:02d}:{whole % 60:02d}"


def mean_cores(m: Measurement) -> float | None:
    """CPU seconds over wall seconds: how many cores this run kept busy on average.

    Neither scheduler takes CPU as "seconds of CPU". Slurm wants a COUNT of cores and
    Kubernetes a RATE in millicores, and both are this ratio in different clothes.
    """
    if m.cpu_seconds is None or m.wall_seconds <= 0:
        return None
    return m.cpu_seconds / m.wall_seconds


def snakemake_row(m: Measurement) -> tuple[list[str], list[str]]:
    """Snakemake's benchmark columns, header and one row. R-13.

    UNITS ARE SNAKEMAKE'S, NOT OURS. Its documentation states memory is in MiB, so the bytes
    of R-2 are converted here. Emitting bytes into a column every existing consumer reads as
    MiB is a 1 048 576x error nobody notices until a plot looks wrong.

    A column this package cannot fill is EMPTY, never 0 (R-7): `max_uss` and `max_pss` need
    psutil or `/proc/*/smaps`, and `mean_load` needs sampling, which needs a polling thread.
    """

    def mib(value: int | None) -> str:
        return "" if value is None else f"{value / MIB:.2f}"

    # `max_vms` IS THIS PROCESS'S, `max_rss` MAY BE A CHILD'S. Measured while building this:
    # a run whose child held 120 MiB printed max_rss 132.39 and max_vms 53.69 — a virtual size
    # smaller than the resident one, which is impossible for one process and obvious nonsense
    # for two. A row whose columns describe different subjects is worse than a missing column,
    # so it is dropped rather than explained in a footnote nobody reads beside a TSV.
    comparable_vms = (
        m.max_vms_bytes
        if (
            m.max_vms_bytes is not None
            and m.max_rss_bytes is not None
            and m.max_vms_bytes >= m.max_rss_bytes
        )
        else None
    )
    row = {
        "s": f"{m.wall_seconds:.4f}",
        "h:m:s": _hms(m.wall_seconds),
        "max_rss": mib(m.max_rss_bytes),
        "max_vms": mib(comparable_vms),
        "max_uss": "",
        "max_pss": "",
        "io_in": mib(m.io_read_bytes),
        "io_out": mib(m.io_write_bytes),
        "mean_load": "",
        "cpu_time": "" if m.cpu_seconds is None else f"{m.cpu_seconds:.4f}",
    }
    return list(SNAKEMAKE_COLUMNS), [row[c] for c in SNAKEMAKE_COLUMNS]


def _margin_note(m: Measurement, margin: float) -> list[str]:
    """R-15. Never a bare value to paste."""
    note = [
        f"# MEASURED FLOOR x{margin:g}. These are what one run on one machine used, not a",
        "# prediction: a larger input needs more. Raise them, never lower them.",
    ]
    if m.source == "getrusage":
        note.append(
            "# And this floor UNDER-REPORTS: RUSAGE_CHILDREN is the largest single child, "
            "while the scheduler enforces the concurrent cgroup total."
        )
    elif m.source == "cgroup":
        note.append("# From the cgroup, which is the quantity the scheduler enforces.")
    return note


def render_slurm(m: Measurement, margin: float = 1.5) -> list[str]:
    """Slurm's syntax: `--mem` in MiB, `--cpus-per-task` a COUNT, `--time` a walltime. R-14."""
    import math

    out = _margin_note(m, margin)
    if m.max_rss_bytes is not None:
        out.append(f"#SBATCH --mem={max(1, math.ceil(m.max_rss_bytes * margin / MIB))}M")
    else:
        out.append("# --mem: NOT MEASURED here; see `unavailable` in the record")
    cores = mean_cores(m)
    if cores is not None:
        out.append(f"#SBATCH --cpus-per-task={max(1, math.ceil(cores * margin))}")
    minutes = max(1, math.ceil(m.wall_seconds * margin / 60))
    out.append(f"#SBATCH --time={minutes // 60:02d}:{minutes % 60:02d}:00")
    return out


def render_k8s(m: Measurement, margin: float = 1.5) -> list[str]:
    """Kubernetes' syntax, which disagrees with Slurm's in two ways that corrupt a number.

    Memory is BINARY here — `Mi`, not `M`, because plain `M` is decimal and the 4.8 %
    difference reads like a typo rather than an error. And CPU is a RATE in millicores, so
    `500m` is half a core over time, not a count of cores. R-14.
    """
    import math

    out = _margin_note(m, margin)
    # BUILT AS ONE MAPPING, then emitted twice. The first version appended cpu after the
    # `limits:` block and produced YAML with memory under both keys and cpu under only one —
    # valid YAML, wrong manifest, and the kind of thing that is found by reading the output
    # rather than the code.
    fields: list[str] = []
    if m.max_rss_bytes is not None:
        fields.append(f"memory: {max(1, math.ceil(m.max_rss_bytes * margin / MIB))}Mi")
    cores = mean_cores(m)
    if cores is not None:
        fields.append(f'cpu: "{max(1, math.ceil(cores * margin * 1000))}m"   # a RATE, not a count')
    if not fields:
        return [*out, "# nothing measured here; see `unavailable` in the record"]
    out.append("resources:")
    for key in ("requests", "limits"):
        out.append(f"  {key}:")
        out += [f"    {f}" for f in fields]
    return out
