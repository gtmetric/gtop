"""Cross-platform tests: run on Windows, Linux and macOS in CI (no GPU required)."""
import asyncio
import subprocess
import time

import pytest

from gtop_nv import sampler as S
from gtop_nv.app import GTop, history_chart, meter

# Trimmed `ioreg -r -d 1 -w 0 -c IOAccelerator` output from an Apple Silicon Mac.
IOREG_APPLE = """+-o AGXAcceleratorG13X  <class AGXAcceleratorG13X, id 0x1000003b2, registered, matched, active, busy 0 (0 ms), retain 63>
    {
      "IOClass" = "AGXAcceleratorG13X"
      "model" = "Apple M1 Pro"
      "gpu-core-count" = 16
      "PerformanceStatistics" = {"In use system memory (driver)"=0,"Alloc system memory"=1234,"Tiler Utilization %"=10,"Renderer Utilization %"=9,"Device Utilization %"=37,"In use system memory"=2147483648}
    }
"""


def test_mac_gpu_parser(monkeypatch):
    class R:
        stdout = IOREG_APPLE

    monkeypatch.setattr(subprocess, "run", lambda *a, **k: R())
    gpus = S._MacGpu().read()
    assert len(gpus) == 1
    g = gpus[0]
    assert g.name == "Apple M1 Pro (16-core)"
    assert g.util == 37.0
    assert g.mem_used == 2 * 2**30  # "(driver)" key must not be picked up instead
    assert g.mem_total and g.mem_total > 0
    assert g.temp is None and g.power_w is None  # so the panel drops its info line


def test_gpu_panel_gives_info_row_to_charts(monkeypatch):
    """Without temp/power/fan/clock (Apple GPUs), the info line is dropped and the
    panel stays the same height because each chart gets one more row."""
    from textual.geometry import Size
    from gtop_nv.app import GpuPanel

    def lines(gpu):
        snap = S.Snapshot(time=0, cpu_per_core=[0.0] * 8, cpu_total=0.0, cpu_split=[(0.0,) * 4] * 8,
                          load_avg=(0.0, 0.0, 0.0), threads=0, mem=None, swap=None, uptime=0.0,
                          gpus=[gpu], procs=[], gpu_proc_source="none")
        p = GpuPanel(1, 8)
        p.snap = snap
        monkeypatch.setattr(GpuPanel, "content_size", property(lambda self: Size(70, 12)))
        return p.render().plain.splitlines()

    mac = S.GpuInfo(index=0, name="Apple M1 Pro", util=10.0, mem_used=2**30, mem_total=16 * 2**30)
    nv = S.GpuInfo(index=0, name="RTX", util=10.0, mem_used=2**30, mem_total=16 * 2**30, temp=50)
    mac_lines, nv_lines = lines(mac), lines(nv)
    assert len(mac_lines) == len(nv_lines)
    assert not any("Temp:" in l for l in mac_lines)
    assert sum("│" in l or "┤" in l for l in mac_lines) == sum("│" in l or "┤" in l for l in nv_lines) + 1


def test_sampler_snapshot():
    s = S.Sampler()
    time.sleep(0.5)
    snap = s.sample()
    assert snap.procs, "process list should not be empty"
    assert len(snap.cpu_per_core) == len(snap.cpu_split) > 0
    for busy, parts in zip(snap.cpu_per_core, snap.cpu_split):
        assert 0.0 <= busy <= 100.0
        assert abs(sum(parts) - busy) < 1.5, "bar segments must add up to the percentage"
    assert snap.mem.total > 0


def test_meter_overlays_text():
    t = meter("3", [(0.5, "green")], 20, "50.0%")
    assert t.plain.startswith("3[") and t.plain.endswith("50.0%]")
    assert len(t.plain) == 20


def test_history_chart_shape():
    lines = history_chart([0, 50, 100], 6, 30, "100%", "50%", "green")
    assert len(lines) == 6
    assert all(len(l.plain) == 30 for l in lines)
    assert lines[-1].plain.strip().startswith("0└")


async def wait_for_rows(app, pilot, timeout=15.0):
    """The first sample takes a while on slow CI runners: wait for it instead of a fixed pause."""
    deadline = time.monotonic() + timeout
    while app.table.row_count == 0 and time.monotonic() < deadline:
        await pilot.pause(0.2)


def test_app_sorts_filters_and_quits():
    async def run():
        app = GTop(interval=0.5)
        async with app.run_test(size=(140, 45)) as pilot:
            await wait_for_rows(app, pilot)
            assert app.table.row_count > 0
            await pilot.press("m")
            assert app.sort_key == "mem" and app.sort_desc
            await pilot.press("m")
            assert not app.sort_desc
            await pilot.press("slash")
            for ch in "zzz_no_such_process":
                await pilot.press(ch)
            await pilot.pause(0.2)
            assert app.table.row_count == 0
            await pilot.press("escape")
            await pilot.pause(0.2)
            assert app.table.row_count > 0
            await pilot.press("ctrl+c")
            await pilot.pause(0.3)
        return app

    app = asyncio.run(run())
    assert not app.is_running


def test_gpu_panel_hidden_without_gpu(monkeypatch):
    from gtop_nv import app as A

    monkeypatch.setattr(A, "gpu_count", lambda: 0)
    monkeypatch.setattr(S.Sampler, "_gpus", lambda self: [])

    async def run():
        app = A.GTop(interval=0.5)
        async with app.run_test(size=(140, 45)) as pilot:
            await wait_for_rows(app, pilot)
            assert not app.query(A.GpuPanel)
            cpu = app.query_one(A.CpuPanel)
            assert cpu.size.width > 120  # CPU meters take the full width
            assert app.table.row_count > 0

    asyncio.run(run())
