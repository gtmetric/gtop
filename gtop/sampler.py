"""Data layer for gtop: CPU / memory / GPU / per-process sampling, no UI.

Run `python -m gtop.sampler --once` for a plain-text dump (useful for sanity checks).
"""
from __future__ import annotations

import argparse
import os
import re
import time
from collections import deque
from dataclasses import dataclass, field

import psutil

try:
    import pynvml
except ImportError:  # pragma: no cover
    pynvml = None

try:
    import win32pdh
except ImportError:  # non-Windows
    win32pdh = None


@dataclass
class GpuInfo:
    index: int
    name: str
    util: float | None = None
    mem_util: float | None = None
    mem_used: int | None = None
    mem_total: int | None = None
    temp: int | None = None
    power_w: float | None = None
    power_limit_w: float | None = None
    fan: int | None = None
    sm_clock: int | None = None
    mem_clock: int | None = None
    util_hist: list[float] = field(default_factory=list)
    mem_hist: list[float] = field(default_factory=list)


@dataclass
class ProcInfo:
    pid: int
    name: str
    user: str
    cpu: float
    mem_pct: float
    rss: int
    threads: int
    gpu_util: float | None
    gpu_mem: int | None
    cmd: str


@dataclass
class Snapshot:
    time: float
    cpu_per_core: list[float]
    cpu_total: float
    cpu_split: list[tuple[float, float, float, float]]  # per core: user, system, interrupt, dpc
    load_avg: tuple[float, float, float]
    threads: int
    mem: object
    swap: object
    uptime: float
    gpus: list[GpuInfo]
    procs: list[ProcInfo]
    gpu_proc_source: str
    sample_ms: float = 0.0
    cuda_version: str = ""
    driver_version: str = ""


def _try(fn, default=None):
    try:
        return fn()
    except Exception:
        return default


class _PdhGpuProcs:
    """Per-process GPU utilisation and dedicated memory via Windows perf counters
    (same source as Task Manager). NVML can't give per-process VRAM under WDDM."""

    _ENG = re.compile(r"pid_(\d+)_.*_engtype_(.+)$")
    _PID = re.compile(r"pid_(\d+)_")

    def __init__(self):
        self.q = win32pdh.OpenQuery()
        self.c_mem = win32pdh.AddEnglishCounter(self.q, r"\GPU Process Memory(*)\Dedicated Usage")
        self.c_eng = win32pdh.AddEnglishCounter(self.q, r"\GPU Engine(*)\Utilization Percentage")
        win32pdh.CollectQueryData(self.q)  # rate counters need a baseline

    def sample(self) -> tuple[dict[int, float], dict[int, int]]:
        win32pdh.CollectQueryData(self.q)
        util: dict[int, float] = {}
        mem: dict[int, int] = {}
        per_type: dict[tuple[int, str], float] = {}
        arr = _try(lambda: win32pdh.GetFormattedCounterArray(self.c_eng, win32pdh.PDH_FMT_DOUBLE), {})
        for inst, val in arr.items():
            m = self._ENG.search(inst)
            if m:
                k = (int(m.group(1)), m.group(2))
                per_type[k] = per_type.get(k, 0.0) + val
        # Task Manager convention: a process's GPU% is its busiest engine type.
        for (pid, _), v in per_type.items():
            util[pid] = min(100.0, max(util.get(pid, 0.0), v))
        arr = _try(lambda: win32pdh.GetFormattedCounterArray(self.c_mem, win32pdh.PDH_FMT_DOUBLE), {})
        for inst, val in arr.items():
            m = self._PID.search(inst)
            if m:
                pid = int(m.group(1))
                mem[pid] = mem.get(pid, 0) + int(val)
        return util, mem


class _WinProcScan:
    """One NtQuerySystemInformation(SystemProcessInformation) call returns every
    process's name, thread count, CPU times and working set. psutil does a
    separate system-wide query per process for some fields, which made a full
    scan take ~4 s with ~450 processes."""

    def __init__(self):
        import ctypes
        from ctypes import wintypes as wt

        class UNICODE_STRING(ctypes.Structure):
            _fields_ = [("Length", wt.USHORT), ("MaximumLength", wt.USHORT), ("Buffer", ctypes.c_void_p)]

        class SPI(ctypes.Structure):  # SYSTEM_PROCESS_INFORMATION (prefix we need)
            _fields_ = [
                ("NextEntryOffset", wt.ULONG), ("NumberOfThreads", wt.ULONG),
                ("WorkingSetPrivateSize", ctypes.c_longlong), ("HardFaultCount", wt.ULONG),
                ("NumberOfThreadsHighWatermark", wt.ULONG), ("CycleTime", ctypes.c_ulonglong),
                ("CreateTime", ctypes.c_longlong), ("UserTime", ctypes.c_longlong),
                ("KernelTime", ctypes.c_longlong), ("ImageName", UNICODE_STRING),
                ("BasePriority", ctypes.c_long), ("UniqueProcessId", ctypes.c_void_p),
                ("InheritedFromUniqueProcessId", ctypes.c_void_p), ("HandleCount", wt.ULONG),
                ("SessionId", wt.ULONG), ("UniqueProcessKey", ctypes.c_void_p),
                ("PeakVirtualSize", ctypes.c_size_t), ("VirtualSize", ctypes.c_size_t),
                ("PageFaultCount", wt.ULONG), ("PeakWorkingSetSize", ctypes.c_size_t),
                ("WorkingSetSize", ctypes.c_size_t),
            ]

        self.ct = ctypes
        self.SPI = SPI
        self.ntq = ctypes.windll.ntdll.NtQuerySystemInformation
        self.ntq.restype = ctypes.c_long
        self.bufsize = 1 << 20
        self.prev: dict[tuple[int, int], int] = {}
        self.prev_t: float | None = None

    def scan(self) -> list[dict]:
        ct = self.ct
        while True:
            buf = ct.create_string_buffer(self.bufsize)
            ret = ct.c_ulong(0)
            status = self.ntq(5, buf, self.bufsize, ct.byref(ret)) & 0xFFFFFFFF
            if status == 0xC0000004:  # STATUS_INFO_LENGTH_MISMATCH
                self.bufsize = max(self.bufsize * 2, ret.value + 65536)
                continue
            if status != 0:
                raise OSError(f"NtQuerySystemInformation failed: 0x{status:08X}")
            break
        now = time.perf_counter()
        dt = (now - self.prev_t) if self.prev_t else None
        base = ct.addressof(buf)
        off = 0
        out, cur = [], {}
        while True:
            e = self.SPI.from_address(base + off)
            pid = e.UniqueProcessId or 0
            name = ct.wstring_at(e.ImageName.Buffer, e.ImageName.Length // 2) if e.ImageName.Buffer else "System Idle Process"
            cpu_t = e.UserTime + e.KernelTime  # 100 ns units
            key = (pid, e.CreateTime)
            cur[key] = cpu_t
            cpu = 0.0
            if dt and key in self.prev:
                cpu = max(0.0, (cpu_t - self.prev[key]) / 1e7 / dt * 100.0)
            out.append({"pid": pid, "name": name, "threads": e.NumberOfThreads,
                        "rss": e.WorkingSetSize, "cpu": cpu, "ctime": e.CreateTime})
            if e.NextEntryOffset == 0:
                break
            off += e.NextEntryOffset
        self.prev, self.prev_t = cur, now
        return out


class _WinCpuTimes:
    """Per-core user/kernel/DPC/interrupt split straight from
    NtQuerySystemInformation(SystemProcessorPerformanceInformation).
    psutil.cpu_times_percent(percpu=True) is wrong on Windows: its fields
    summed to only 40-60% per core, which broke the segmented CPU bars."""

    def __init__(self, ncores: int):
        import ctypes

        class SPPI(ctypes.Structure):
            _fields_ = [("IdleTime", ctypes.c_longlong), ("KernelTime", ctypes.c_longlong),
                        ("UserTime", ctypes.c_longlong), ("DpcTime", ctypes.c_longlong),
                        ("InterruptTime", ctypes.c_longlong), ("InterruptCount", ctypes.c_ulong)]

        self.ct = ctypes
        self.arr_t = SPPI * ncores
        self.ntq = ctypes.windll.ntdll.NtQuerySystemInformation
        self.prev = None
        self.last: list[tuple[float, ...]] = [(0.0,) * 5] * ncores
        self.sample()

    def sample(self) -> list[tuple[float, ...]]:
        """Returns per core (busy%, user%, system%, interrupt%, dpc%)."""
        buf = self.arr_t()
        ret = self.ct.c_ulong(0)
        if self.ntq(8, self.ct.byref(buf), self.ct.sizeof(buf), self.ct.byref(ret)) != 0:
            return self.last
        cur = [(c.IdleTime, c.KernelTime, c.UserTime, c.DpcTime, c.InterruptTime) for c in buf]
        if self.prev is not None:
            out = []
            for i, (now, old) in enumerate(zip(cur, self.prev)):
                di, dk, du, ddpc, dint = (a - b for a, b in zip(now, old))
                total = dk + du  # kernel time includes idle, DPC and interrupt time
                if total <= 0:
                    out.append(self.last[i])  # no clock tick on this core yet: keep last value
                    continue

                def pct(x: float) -> float:
                    return max(0.0, min(100.0, 100.0 * x / total))

                system = dk - di - ddpc - dint
                out.append((pct(total - di), pct(du), pct(system), pct(dint), pct(ddpc)))
            self.last = out
        self.prev = cur
        return self.last


class Sampler:
    HIST = 300

    def __init__(self):
        self.ncores = psutil.cpu_count(logical=True) or 1
        self.total_mem = psutil.virtual_memory().total
        self._user_cache: dict[tuple[int, float], str] = {}
        self._cmd_cache: dict[tuple[int, float], str] = {}
        self._util_hist: dict[int, deque] = {}
        self._mem_hist: dict[int, deque] = {}
        self.my_pid = os.getpid()

        self.nvml = False
        self.handles = []
        if pynvml is not None:
            try:
                pynvml.nvmlInit()
                self.handles = [pynvml.nvmlDeviceGetHandleByIndex(i)
                                for i in range(pynvml.nvmlDeviceGetCount())]
                self.nvml = True
            except Exception:
                pass
        self.gpu_names = [_try(lambda h=h: pynvml.nvmlDeviceGetName(h), "GPU") for h in self.handles]
        self.mem_limits = [_try(lambda h=h: pynvml.nvmlDeviceGetMemoryInfo(h).total) for h in self.handles]
        self.cuda_version = ""
        v = _try(pynvml.nvmlSystemGetCudaDriverVersion) if self.nvml else None
        if v:
            self.cuda_version = f"{v // 1000}.{(v % 1000) // 10}"
        self.driver_version = (_try(pynvml.nvmlSystemGetDriverVersion, "") or "") if self.nvml else ""
        self.power_limits = [_try(lambda h=h: pynvml.nvmlDeviceGetEnforcedPowerLimit(h) / 1000) for h in self.handles]

        self.pdh = None
        if win32pdh is not None:
            self.pdh = _try(_PdhGpuProcs)

        self.win = _try(_WinProcScan) if os.name == "nt" else None
        self.cpu_times = _try(lambda: _WinCpuTimes(self.ncores)) if os.name == "nt" else None

        # Prime the rate-based counters so the first real sample isn't all zeros.
        psutil.cpu_times_percent(percpu=True)
        _try(psutil.getloadavg)  # on Windows this starts psutil's load-average emulation
        if self.win is not None:
            self.win.scan()
        else:
            for p in psutil.process_iter(["cpu_percent"]):
                pass

    # ---- helpers -------------------------------------------------------
    def _user(self, p: psutil.Process | int, key) -> str:
        u = self._user_cache.get(key)
        if u is None:
            if isinstance(p, int):
                p = _try(lambda: psutil.Process(p))
            u = (_try(p.username, "") or "") if p is not None else ""
            u = u.split("\\")[-1]
            self._user_cache[key] = u
        return u

    def _cmd(self, p: psutil.Process | int, key, name: str) -> str:
        c = self._cmd_cache.get(key)
        if c is None:
            if isinstance(p, int):
                p = _try(lambda: psutil.Process(p))
            parts = _try(p.cmdline, None) if p is not None else None
            if parts:
                exe = os.path.basename(parts[0]) or name
                c = " ".join([exe] + parts[1:]).strip()
            else:
                c = name
            self._cmd_cache[key] = c
        return c

    def _gpus(self) -> list[GpuInfo]:
        out = []
        for i, h in enumerate(self.handles):
            g = GpuInfo(index=i, name=self.gpu_names[i])
            rates = _try(lambda: pynvml.nvmlDeviceGetUtilizationRates(h))
            if rates is not None:
                g.util, g.mem_util = float(rates.gpu), float(rates.memory)
            mi = _try(lambda: pynvml.nvmlDeviceGetMemoryInfo(h))
            if mi is not None:
                g.mem_used, g.mem_total = mi.used, mi.total
            g.temp = _try(lambda: pynvml.nvmlDeviceGetTemperature(h, pynvml.NVML_TEMPERATURE_GPU))
            pw = _try(lambda: pynvml.nvmlDeviceGetPowerUsage(h))
            g.power_w = pw / 1000 if pw is not None else None
            g.power_limit_w = self.power_limits[i]
            g.fan = _try(lambda: pynvml.nvmlDeviceGetFanSpeed(h))
            g.sm_clock = _try(lambda: pynvml.nvmlDeviceGetClockInfo(h, pynvml.NVML_CLOCK_SM))
            g.mem_clock = _try(lambda: pynvml.nvmlDeviceGetClockInfo(h, pynvml.NVML_CLOCK_MEM))
            uh = self._util_hist.setdefault(i, deque(maxlen=self.HIST))
            mh = self._mem_hist.setdefault(i, deque(maxlen=self.HIST))
            uh.append(g.util or 0.0)
            mh.append(100.0 * g.mem_used / g.mem_total if g.mem_used and g.mem_total else 0.0)
            g.util_hist, g.mem_hist = list(uh), list(mh)
            out.append(g)
        return out

    def _gpu_procs_nvml(self) -> tuple[dict[int, float], dict[int, int]]:
        util: dict[int, float] = {}
        mem: dict[int, int] = {}
        for h in self.handles:
            for fn in (pynvml.nvmlDeviceGetComputeRunningProcesses,
                       pynvml.nvmlDeviceGetGraphicsRunningProcesses):
                for p in _try(lambda: fn(h), []) or []:
                    if p.usedGpuMemory is not None:
                        mem[p.pid] = mem.get(p.pid, 0) + p.usedGpuMemory
            since = int((time.time() - 2) * 1e6)
            for s in _try(lambda: pynvml.nvmlDeviceGetProcessUtilization(h, since), []) or []:
                util[s.pid] = max(util.get(s.pid, 0.0), float(s.smUtil))
        return util, mem

    # ---- main ----------------------------------------------------------
    def sample(self) -> Snapshot:
        t0 = time.perf_counter()
        if self.cpu_times is not None:
            rows = self.cpu_times.sample()
            per_core = [r[0] for r in rows]
            split = [tuple(r[1:]) for r in rows]
        else:
            ctp = psutil.cpu_times_percent(percpu=True)
            split = [(c.user, c.system, getattr(c, "irq", 0.0), getattr(c, "softirq", 0.0)) for c in ctp]
            per_core = [max(0.0, 100.0 - c.idle) for c in ctp]
        gpus = self._gpus()

        if self.pdh is not None:
            gutil, gmem = _try(self.pdh.sample, ({}, {}))
            source = "perf counters"
        elif self.nvml:
            gutil, gmem = self._gpu_procs_nvml()
            source = "NVML"
        else:
            gutil, gmem, source = {}, {}, "none"

        procs: list[ProcInfo] = []
        live = set()
        if self.win is not None:
            rows = [(r["pid"], r["name"], r["cpu"], r["rss"], r["threads"], r["ctime"], r["pid"])
                    for r in self.win.scan()]
        else:
            attrs = ["pid", "name", "cpu_percent", "memory_info", "num_threads", "create_time"]
            rows = []
            for p in psutil.process_iter(attrs, ad_value=None):
                i = p.info
                mi = i["memory_info"]
                rows.append((i["pid"], i["name"], i["cpu_percent"] or 0.0, mi.rss if mi else 0,
                             i["num_threads"] or 0, i["create_time"] or 0.0, p))
        for pid, name, cpu, rss, threads, ctime, handle in rows:
            if pid == 0:  # System Idle Process: would top the CPU sort forever
                continue
            key = (pid, ctime)
            live.add(key)
            name = name or "?"
            procs.append(ProcInfo(
                pid=pid,
                name=name,
                user=self._user(handle, key),
                cpu=cpu,
                mem_pct=100.0 * rss / self.total_mem,
                rss=rss,
                threads=threads,
                gpu_util=gutil.get(pid),
                gpu_mem=gmem.get(pid),
                cmd=self._cmd(handle, key, name),
            ))
        # Drop cache entries for processes that have exited.
        for cache in (self._user_cache, self._cmd_cache):
            for k in [k for k in cache if k not in live]:
                del cache[k]

        return Snapshot(
            time=time.time(),
            cpu_per_core=per_core,
            cpu_total=sum(per_core) / max(1, len(per_core)),
            cpu_split=split,
            load_avg=_try(psutil.getloadavg, (0.0, 0.0, 0.0)),
            threads=sum(p.threads for p in procs),
            cuda_version=self.cuda_version,
            driver_version=self.driver_version,
            mem=psutil.virtual_memory(),
            swap=psutil.swap_memory(),
            uptime=time.time() - psutil.boot_time(),
            gpus=gpus,
            procs=procs,
            gpu_proc_source=source,
            sample_ms=(time.perf_counter() - t0) * 1000,
        )


def fmt_bytes(n: int | float | None) -> str:
    if n is None:
        return "-"
    for unit in ("B", "K", "M", "G", "T"):
        if abs(n) < 1024 or unit == "T":
            return f"{n:.0f}{unit}" if unit in ("B", "K") else f"{n:.1f}{unit}"
        n /= 1024
    return f"{n:.1f}T"


def _dump():
    s = Sampler()
    time.sleep(1.0)
    snap = s.sample()
    print(f"sample took {snap.sample_ms:.0f} ms, {len(snap.procs)} procs, GPU proc source: {snap.gpu_proc_source}")
    print(f"CPU total {snap.cpu_total:.1f}%  cores: " + " ".join(f"{c:.0f}" for c in snap.cpu_per_core))
    print(f"MEM {fmt_bytes(snap.mem.used)}/{fmt_bytes(snap.mem.total)}  SWAP {fmt_bytes(snap.swap.used)}/{fmt_bytes(snap.swap.total)}")
    for g in snap.gpus:
        print(f"GPU{g.index} {g.name}: util {g.util}% vram {fmt_bytes(g.mem_used)}/{fmt_bytes(g.mem_total)} "
              f"temp {g.temp}C power {g.power_w:.0f}/{g.power_limit_w:.0f}W fan {g.fan}% sm {g.sm_clock}MHz")
    print("\nTop by CPU:")
    for p in sorted(snap.procs, key=lambda p: p.cpu, reverse=True)[:8]:
        print(f"  {p.pid:>7} {p.user:<10} cpu {p.cpu:5.1f} mem {fmt_bytes(p.rss):>7} gpu {p.gpu_util or 0:5.1f}% vram {fmt_bytes(p.gpu_mem):>7}  {p.cmd[:60]}")
    print("\nTop by VRAM:")
    for p in sorted(snap.procs, key=lambda p: p.gpu_mem or 0, reverse=True)[:8]:
        print(f"  {p.pid:>7} {p.user:<10} cpu {p.cpu:5.1f} mem {fmt_bytes(p.rss):>7} gpu {p.gpu_util or 0:5.1f}% vram {fmt_bytes(p.gpu_mem):>7}  {p.cmd[:60]}")
    t0 = time.perf_counter()
    s.sample()
    print(f"\nsteady-state sample: {(time.perf_counter() - t0) * 1000:.0f} ms")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true", help="print one sample and exit")
    if ap.parse_args().once:
        _dump()
    else:
        ap.print_help()
