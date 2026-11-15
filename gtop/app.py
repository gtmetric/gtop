"""gtop: htop + nvtop in one terminal UI (Windows-friendly), in htop's default colours.

Keys: c/m/g/v/p/n sort by CPU/MEM/GPU%/VRAM/PID/Command (press again to flip),
F6 cycle sort, F4 or / filter, Esc clear filter, F9 or k kill, space pause,
+/- refresh interval, F10 or q quit. Click a column header to sort by it.
"""
from __future__ import annotations

import argparse
import math
import os
import time

import psutil
from rich.text import Text
from textual import work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widget import Widget
from textual.widgets import Button, DataTable, Footer, Input, Label, Static

from .sampler import Sampler, Snapshot, fmt_bytes

# htop's default colour scheme (CRT.c, COLORSCHEME_DEFAULT), drawn with the
# terminal's own ANSI palette, exactly as htop does.
C_TEXT = "cyan"                  # METER_TEXT: meter labels
C_VALUE = "bold cyan"            # METER_VALUE / UPTIME
C_BORDER = "bold"                # BAR_BORDER: the [ ] brackets
C_SHADOW = "bold bright_black"   # BAR_SHADOW / PROCESS_SHADOW: empty bar, zero values
C_USER = "green"                 # CPU_NORMAL
C_SYSTEM = "red"                 # CPU_SYSTEM
C_IRQ = "yellow"                 # CPU_IRQ
C_SOFTIRQ = "magenta"            # CPU_SOFTIRQ (Windows DPC time)
C_MEM = "green"                  # MEMORY_USED
C_SWAP = "red"                   # SWAP
C_MB = "cyan"                    # PROCESS_MEGABYTES
C_GB = "green"                   # PROCESS_GIGABYTES
C_WARN = "bold yellow"           # METER_VALUE_WARN
C_ERROR = "bold red"             # METER_VALUE_ERROR
C_GRAPH = "bold cyan"            # GRAPH_1

SPARK = " ▁▂▃▄▅▆▇█"


def meter(label: str, segments: list[tuple[float, str]], width: int, text: str,
          label_w: int | None = None) -> Text:
    """htop bar meter: LABEL[||||||||        42.0%].

    segments: (fraction 0..1, style) stacked left to right. The value text is
    right-aligned inside the bar; where it overlaps filled cells it takes the
    segment colour, elsewhere the shadow colour (as htop's BarMeterMode_draw)."""
    label = label.rjust(label_w) if label_w else label
    inner = max(1, width - len(label) - 2)
    styles: list[str | None] = []
    for frac, style in segments:
        n = int(round(max(0.0, frac) * inner))
        styles.extend([style] * n)
    styles = styles[:inner] + [None] * max(0, inner - len(styles))
    text = text[-inner:]
    start = inner - len(text)
    t = Text(label, style=C_TEXT)
    t.append("[", style=C_BORDER)
    for i, st in enumerate(styles):
        ch = text[i - start] if i >= start else ("|" if st else " ")
        t.append(ch, style=st or C_SHADOW)
    t.append("]", style=C_BORDER)
    return t


def sparkline(values: list[float], width: int, style: str = C_GRAPH) -> Text:
    vals = values[-width:]
    s = "".join(SPARK[min(8, int(math.ceil(v / 100 * 8)))] if v > 0 else " " for v in vals)
    return Text(s.rjust(width), style=style)


def fmt_uptime(sec: float) -> str:
    """htop UptimeMeter format: '2 days, 11:40:04'."""
    d, rem = divmod(int(sec), 86400)
    h, rem = divmod(rem, 3600)
    m, s = divmod(rem, 60)
    days = f"{d} day{'s' if d != 1 else ''}, " if d else ""
    return f"{days}{h:02d}:{m:02d}:{s:02d}"


def htop_bytes(n: int | None) -> Text:
    """Sizes coloured like htop's process columns: K plain, M cyan, G green."""
    if not n:
        return Text("0", style=C_SHADOW, justify="right")
    s = fmt_bytes(n)
    style = C_GB if s.endswith(("G", "T")) else C_MB if s.endswith("M") else ""
    return Text(s, style=style, justify="right")


def htop_pct(v: float | None) -> Text:
    """Percentages like htop 3: values that round to 0.0 are drawn in the shadow colour."""
    v = v or 0.0
    s = f"{v:.1f}" if v < 999.95 else f"{v:.0f}"
    return Text(s, style=C_SHADOW if v < 0.05 else "", justify="right")


# --------------------------------------------------------------------------- panels
class CpuPanel(Widget):
    DEFAULT_CSS = "CpuPanel { height: auto; width: 1fr; padding: 0 1; }"
    TEXT_LINES = 3  # Tasks / Load average / Uptime

    def __init__(self, **kw):
        super().__init__(**kw)
        self.snap: Snapshot | None = None
        self.ncores = psutil.cpu_count(logical=True) or 1

    def _cols(self, width: int) -> int:
        return 4 if width >= 120 else 2 if width >= 44 else 1

    def get_content_height(self, container, viewport, width: int) -> int:
        return math.ceil(self.ncores / self._cols(width)) + 2 + self.TEXT_LINES

    def render(self) -> Text:
        w = max(20, self.content_size.width)
        s = self.snap
        if s is None:
            return Text("sampling…", style=C_SHADOW)
        n = len(s.cpu_per_core)
        cols = self._cols(w)
        rows = math.ceil(n / cols)
        colw = (w - (cols - 1)) // cols
        lw = len(str(n - 1))
        out = Text()
        for r in range(rows):
            for c in range(cols):
                i = c * rows + r  # column-major, like htop
                if i >= n:
                    continue
                user, system, irq, dpc = s.cpu_split[i]
                segs = [(user / 100, C_USER), (system / 100, C_SYSTEM),
                        (irq / 100, C_IRQ), (dpc / 100, C_SOFTIRQ)]
                out.append_text(meter(str(i), segs, colw, f"{s.cpu_per_core[i]:.1f}%", label_w=lw))
                if c < cols - 1:
                    out.append(" ")
            out.append("\n")
        m, sw = s.mem, s.swap
        out.append_text(meter("Mem", [(m.percent / 100, C_MEM)], w, f"{fmt_bytes(m.used)}/{fmt_bytes(m.total)}"))
        out.append("\n")
        out.append_text(meter("Swp", [(sw.percent / 100, C_SWAP)], w, f"{fmt_bytes(sw.used)}/{fmt_bytes(sw.total)}"))
        out.append("\n")
        # TasksMeter. Windows has no "R" state, so "running" counts processes
        # that used >= 1% CPU in the last interval.
        running = sum(1 for p in s.procs if p.cpu >= 1.0)
        out.append("Tasks: ", style=C_TEXT)
        out.append(str(len(s.procs)), style=C_VALUE)
        out.append(", ", style=C_TEXT)
        out.append(str(s.threads), style="bold green")
        out.append(" thr", style="green")
        out.append("; ", style=C_TEXT)
        out.append(str(running), style="bold green")
        out.append(" running", style=C_TEXT)
        out.append("\n")
        l1, l5, l15 = s.load_avg
        out.append("Load average: ", style=C_TEXT)
        out.append(f"{l1:.2f} ", style="bold white")
        out.append(f"{l5:.2f} ", style="bold cyan")
        out.append(f"{l15:.2f}", style="cyan")
        out.append("\n")
        out.append("Uptime: ", style=C_TEXT)
        out.append(fmt_uptime(s.uptime), style=C_VALUE)
        return out


def history_chart(values: list[float], height: int, width: int, top: str, mid: str,
                  style: str) -> list[Text]:
    """Multi-row bar chart of 0-100 values (newest on the right), 8 sub-steps per
    row, with a y-axis labelled top / mid and an x-axis baseline labelled 0.
    Returns `height` lines of `width` cells (height - 1 bar rows + the baseline)."""
    lw = max(len(top), len(mid), 1)
    gw = max(1, width - lw - 1)
    rows = max(1, height - 1)
    vals = values[-gw:]
    pad = gw - len(vals)
    eighths = [int(round(max(0.0, min(100.0, v)) / 100 * rows * 8)) for v in vals]
    lines = []
    for r in range(rows):
        b = rows - 1 - r  # row index counted from the bottom
        if r == 0:
            lab = top
        elif rows >= 3 and b == rows // 2:
            lab = mid
        else:
            lab = ""
        t = Text(lab.rjust(lw), style=C_TEXT)
        t.append("┤" if lab else "│", style=C_SHADOW)
        t.append(" " * pad)
        t.append("".join(SPARK[max(0, min(8, e - b * 8))] for e in eighths), style=style)
        lines.append(t)
    base = Text("0".rjust(lw), style=C_TEXT)
    base.append("└" + "─" * gw, style=C_SHADOW)
    lines.append(base)
    return lines


class GpuPanel(Widget):
    """nvtop-style: header, meters, then usage and VRAM history charts side by side,
    sized so the panel is as tall as the CPU panel next to it."""

    DEFAULT_CSS = "GpuPanel { height: auto; width: 1fr; padding: 0 1; }"
    FIXED_LINES = 5  # header, info, GPU meter, Mem meter, chart titles
    MIN_CHART = 3

    def __init__(self, ngpus: int, ncores: int, **kw):
        super().__init__(**kw)
        self.snap: Snapshot | None = None
        self.ngpus = ngpus
        self.ncores = ncores
        self.interval = 1.5

    def _lines_per_gpu(self, width: int) -> int:
        cols = 4 if width >= 120 else 2 if width >= 44 else 1  # same rule as CpuPanel
        cpu_h = math.ceil(self.ncores / cols) + 2 + CpuPanel.TEXT_LINES
        return max(self.FIXED_LINES + self.MIN_CHART, cpu_h // max(1, self.ngpus))

    def get_content_height(self, container, viewport, width: int) -> int:
        if not self.ngpus:
            return 1
        return self.ngpus * self._lines_per_gpu(width)

    @staticmethod
    def _kv(out: Text, key: str, val: str, style: str = C_VALUE) -> None:
        out.append(key, style=C_TEXT)
        out.append(val, style=style)

    def render(self) -> Text:
        w = max(20, self.content_size.width)
        s = self.snap
        if s is None:
            return Text("sampling…", style=C_SHADOW)
        if not s.gpus:
            return Text("No NVIDIA GPU found (NVML unavailable)", style=C_SHADOW)
        out = Text()
        chart_h = self._lines_per_gpu(w) - self.FIXED_LINES
        for g in s.gpus:
            out.append(f"GPU{g.index}: ", style=C_TEXT)
            # Value colours rotate through htop's TasksMeter / LoadAverage palette
            # (bold white, bold cyan, bold green, green) so each field stands apart.
            out.append(g.name.replace("NVIDIA GeForce ", ""), style="bold white")
            if s.cuda_version:
                self._kv(out, "  CUDA: ", s.cuda_version, "bold green")
            if s.driver_version:
                self._kv(out, "  Driver: ", s.driver_version, "green")
            out.append("\n")
            parts = []
            if g.temp is not None:
                parts.append(("Temp: ", [(f"{g.temp}°C", C_ERROR if g.temp >= 90 else C_WARN if g.temp >= 80
                                          else "bold white")]))
            if g.power_w is not None:
                pw = [(f"{g.power_w:.0f}", "bold green")]
                if g.power_limit_w:
                    pw.append((f"/{g.power_limit_w:.0f}", "green"))
                pw.append(("W", "green"))
                parts.append(("Power: ", pw))
            if g.fan is not None:
                parts.append(("Fan: ", [(f"{g.fan}%", C_VALUE)]))
            if g.sm_clock is not None:
                parts.append(("Clock: ", [(f"{g.sm_clock}", "bold white"), ("MHz", "white")]))
            for i, (key, vals) in enumerate(parts):
                if i:
                    out.append("  ")
                out.append(key, style=C_TEXT)
                for txt, st in vals:
                    out.append(txt, style=st)
            out.append("\n")
            util = g.util or 0.0
            out.append_text(meter("GPU", [(util / 100, C_USER)], w, f"{util:.1f}%"))
            out.append("\n")
            mfrac = (g.mem_used / g.mem_total) if g.mem_used and g.mem_total else 0.0
            out.append_text(meter("Mem", [(mfrac, C_MEM)], w,
                                  f"{fmt_bytes(g.mem_used)}/{fmt_bytes(g.mem_total)}"))
            out.append("\n")

            # Two history charts side by side: GPU utilisation and VRAM.
            gap = 2
            cw = (w - gap) // 2
            total_gb = (g.mem_total or 0) / 2**30
            uh = g.util_hist
            mh = g.mem_hist
            left = history_chart(uh, chart_h, cw, "100%", "50%", C_USER)
            right = history_chart(mh, chart_h, cw, f"{total_gb:.0f}G", f"{total_gb / 2:.0f}G", "cyan")
            # Titles with current / average / peak over the visible window.
            gw = cw - 5
            uwin, mwin = uh[-gw:] or [0.0], mh[-gw:] or [0.0]
            lt = Text()
            # now / avg / max styled like htop's load average (1 / 5 / 15 min).
            self._kv(lt, "GPU util ", f"{util:.0f}%", "bold white")
            self._kv(lt, "  avg ", f"{sum(uwin) / len(uwin):.0f}%", "bold cyan")
            self._kv(lt, "  max ", f"{max(uwin):.0f}%", "bold green")
            rt = Text()
            peak = max(mwin) / 100 * (g.mem_total or 0)
            self._kv(rt, "VRAM ", f"{mfrac * 100:.0f}%", "bold white")
            self._kv(rt, "  used ", fmt_bytes(g.mem_used), "bold cyan")
            self._kv(rt, "  peak ", fmt_bytes(peak), "bold green")
            lt.truncate(cw, pad=True)
            rt.truncate(cw)
            out.append_text(lt)
            out.append(" " * gap)
            out.append_text(rt)
            out.append("\n")
            for a, b in zip(left, right):
                a.truncate(cw, pad=True)
                out.append_text(a)
                out.append(" " * gap)
                out.append_text(b)
                out.append("\n")
        out.rstrip()
        return out


# --------------------------------------------------------------------------- kill dialog
class ConfirmKill(ModalScreen[bool]):
    DEFAULT_CSS = """
    ConfirmKill { align: center middle; background: ansi_default 0%; }
    #dlg { width: 72; height: auto; padding: 0 1 1 1; border: none; background: ansi_black; }
    #dlg #title { width: 100%; background: ansi_green; color: ansi_black; padding: 0 1; margin: 0 -1 1 -1; }
    #dlg #msg { width: 100%; margin-bottom: 1; color: ansi_white; }
    #btns { height: auto; align: center middle; }
    #btns Button { margin: 0 2; min-width: 14; height: 1; border: none; }
    #yes { background: ansi_red; color: ansi_black; }
    #no { background: ansi_cyan; color: ansi_black; }
    """
    BINDINGS = [Binding("y", "yes", "Yes"), Binding("n,escape", "no", "No")]

    def __init__(self, msg: str):
        super().__init__()
        self.msg = msg

    def compose(self) -> ComposeResult:
        with Vertical(id="dlg"):
            yield Label("Send signal: SIGTERM (terminate)", id="title")
            yield Label(self.msg, id="msg")
            with Horizontal(id="btns"):
                yield Button("Kill (y)", id="yes")
                yield Button("Cancel (n)", id="no")

    def on_button_pressed(self, ev: Button.Pressed) -> None:
        self.dismiss(ev.button.id == "yes")

    def action_yes(self) -> None:
        self.dismiss(True)

    def action_no(self) -> None:
        self.dismiss(False)


# --------------------------------------------------------------------------- app
COLUMNS = [  # key, label, width, sort-key, default-descending
    ("pid", "PID", 7, lambda p: p.pid, False),
    ("user", "USER", 10, lambda p: p.user.lower(), False),
    ("cpu", "CPU%", 6, lambda p: p.cpu, True),
    ("mem", "MEM%", 6, lambda p: p.rss, True),
    ("res", "RES", 7, lambda p: p.rss, True),
    ("thr", "THR", 4, lambda p: p.threads, True),
    ("gpu", "GPU%", 6, lambda p: p.gpu_util or 0.0, True),
    ("vram", "VRAM", 7, lambda p: p.gpu_mem or 0, True),
    ("name", "Command", None, lambda p: p.cmd.lower(), False),
]
COL = {c[0]: c for c in COLUMNS}
SORT_CYCLE = ["cpu", "mem", "gpu", "vram", "pid", "name"]


class ProcTable(DataTable):
    """DataTable that records whether the user picked a row (then the cursor
    follows that process across refreshes) or is at the top (cursor stays on row 0)."""

    following = False

    def action_cursor_down(self) -> None:
        self.following = True
        super().action_cursor_down()

    def action_cursor_up(self) -> None:
        self.following = True
        super().action_cursor_up()

    def action_page_down(self) -> None:
        self.following = True
        super().action_page_down()

    def action_page_up(self) -> None:
        self.following = True
        super().action_page_up()

    def action_scroll_bottom(self) -> None:
        self.following = True
        super().action_scroll_bottom()

    def action_scroll_top(self) -> None:
        self.following = False
        super().action_scroll_top()

    def _on_click(self, event) -> None:
        # Textual also runs DataTable._on_click itself (handlers dispatch along the MRO),
        # so don't call super() here or every click is handled twice.
        if event.y > 0:  # a row, not the header
            self.following = True


class GTop(App):
    TITLE = "gtop"
    ENABLE_COMMAND_PALETTE = False
    CSS = """
    Screen { layout: vertical; background: ansi_default; color: ansi_default; }
    #top { height: auto; margin-bottom: 1; }
    #status { height: 1; padding: 0 1; display: none; }
    #status.visible { display: block; }

    /* htop draws no scrollbars; scrolling still works with keys / mouse wheel. */
    ProcTable { height: 1fr; background: ansi_default; color: ansi_default;
                scrollbar-size-vertical: 0; scrollbar-size-horizontal: 0; }
    ProcTable > .datatable--header { background: ansi_green; color: ansi_black; text-style: none; }
    ProcTable > .datatable--header-hover { background: ansi_green; color: ansi_black; }
    ProcTable > .datatable--header-cursor { background: ansi_green; color: ansi_black; }
    ProcTable > .datatable--cursor { background: ansi_cyan; color: ansi_black; text-style: none; }
    ProcTable > .datatable--hover { background: ansi_default; text-style: none; }
    ProcTable > .datatable--even-row, ProcTable > .datatable--odd-row { background: ansi_default; }

    #filter { dock: bottom; display: none; height: 1; border: none; padding: 0 1;
              background: ansi_cyan; color: ansi_black; }
    #filter.visible { display: block; }
    #filter > .input--placeholder { color: ansi_bright_black; }
    #filter > .input--cursor { background: ansi_black; color: ansi_cyan; }

    Footer { background: ansi_cyan; }
    FooterKey { background: ansi_cyan; }
    FooterKey .footer-key--key { color: ansi_white; background: ansi_black; text-style: none; padding: 0 0; }
    FooterKey .footer-key--description { color: ansi_black; background: ansi_cyan; padding: 0 1 0 0; }
    FooterKey:hover { background: ansi_cyan; }
    """
    # Footer order mirrors htop's function bar (F-keys), with gtop's sort keys in between.
    BINDINGS = [
        Binding("f4,slash", "filter", "Filter", key_display="F4"),
        Binding("f6", "cycle_sort", "SortBy", key_display="F6"),
        Binding("c", "sort('cpu')", "CPU"),
        Binding("m", "sort('mem')", "MEM"),
        Binding("g", "sort('gpu')", "GPU%"),
        Binding("v", "sort('vram')", "VRAM"),
        Binding("p", "sort('pid')", "PID", show=False),
        Binding("n", "sort('name')", "Command", show=False),
        Binding("i", "invert", "Invert", show=False),
        Binding("escape", "clear_filter", "Clear", show=False),
        Binding("space", "pause", "Pause", key_display="Spc"),
        Binding("plus,equals_sign", "faster", "Faster", show=False),
        Binding("minus", "slower", "Slower", show=False),
        Binding("f9,k", "kill", "Kill", key_display="F9"),
        Binding("f10,q", "quit", "Quit", key_display="F10"),
        # Textual binds ctrl+c to "copy" by default; make it quit like htop.
        Binding("ctrl+c", "quit", "Quit", show=False, priority=True),
    ]

    def __init__(self, interval: float = 1.5):
        super().__init__(ansi_color=True)  # use the terminal's palette, like htop
        self.interval = interval
        self.sort_key = "cpu"
        self.sort_desc = True
        self.filter_text = ""
        self.paused = False
        self.busy = False
        self.snap: Snapshot | None = None
        self.sampler: Sampler | None = None
        self.ngpus = self._count_gpus()
        self.timer = None

    @staticmethod
    def _count_gpus() -> int:
        try:
            import pynvml
            pynvml.nvmlInit()
            return pynvml.nvmlDeviceGetCount()
        except Exception:
            return 0

    def compose(self) -> ComposeResult:
        with Horizontal(id="top"):
            yield CpuPanel(id="cpu")
            yield GpuPanel(self.ngpus, psutil.cpu_count(logical=True) or 1, id="gpu")
        yield Static(id="status")
        yield ProcTable(id="procs", cursor_type="row", zebra_stripes=False,
                        cursor_foreground_priority="css", cursor_background_priority="css")
        yield Input(placeholder="Filter: name / command / user  (Enter keeps, Esc clears)", id="filter")
        yield Footer()

    def on_mount(self) -> None:
        self.table = self.query_one(ProcTable)
        self._build_columns()
        self._update_status()
        self.table.focus()
        self.timer = self.set_interval(self.interval, self.tick)
        self.tick()

    # ---- sampling ------------------------------------------------------
    def tick(self) -> None:
        if self.paused or self.busy:
            return
        self.busy = True
        self.sample_worker()

    @work(thread=True, exclusive=True, group="sample", exit_on_error=False)
    def sample_worker(self) -> None:
        try:
            if self.sampler is None:
                self.sampler = Sampler()
                time.sleep(0.5)  # give rate counters a real interval for the first frame
            snap = self.sampler.sample()
            self.call_from_thread(self.apply, snap)
        except Exception as e:  # keep the UI alive; show the error
            self.call_from_thread(self.notify, f"sample failed: {e!r}", severity="error")
        finally:
            self.busy = False

    def apply(self, snap: Snapshot) -> None:
        self.snap = snap
        cpu, gpu = self.query_one(CpuPanel), self.query_one(GpuPanel)
        cpu.snap = gpu.snap = snap
        gpu.interval = self.interval
        cpu.refresh(layout=True)
        gpu.refresh(layout=True)
        self._refresh_table()
        self._update_status()

    # ---- table ---------------------------------------------------------
    def _build_columns(self) -> None:
        self.table.clear(columns=True)
        for key, label, width, _, _ in COLUMNS:
            if key == self.sort_key:
                # htop 3 marks the sort column in PANEL_SELECTION_FOCUS (black on cyan) with ▽/△.
                lab = Text(f"{label}{'▽' if self.sort_desc else '△'}", style="black on cyan")
            else:
                lab = Text(label)
            if key != "name":
                lab.justify = "right"
            self.table.add_column(lab, key=key, width=None if width is None else width + 1)

    def _rows(self) -> list:
        if self.snap is None:
            return []
        procs = self.snap.procs
        if self.filter_text:
            f = self.filter_text.lower()
            procs = [p for p in procs if f in p.cmd.lower() or f in p.name.lower()
                     or f in p.user.lower() or f == str(p.pid)]
        keyfn = COL[self.sort_key][3]
        return sorted(procs, key=keyfn, reverse=self.sort_desc)

    def _refresh_table(self) -> None:
        t = self.table
        follow_key = None
        if t.row_count and t.following:
            try:
                follow_key = t.coordinate_to_cell_key(t.cursor_coordinate).row_key.value
            except Exception:
                pass
        old_row = t.cursor_row
        scroll_y = t.scroll_y
        t.clear()
        keys = set()
        for p in self._rows():
            t.add_row(
                Text(str(p.pid), justify="right"),
                Text(p.user[:10] or "-"),
                htop_pct(p.cpu),
                htop_pct(p.mem_pct),
                htop_bytes(p.rss),
                Text(str(p.threads), justify="right"),
                htop_pct(p.gpu_util),
                htop_bytes(p.gpu_mem),
                Text(p.cmd, no_wrap=True, overflow="ellipsis"),
                key=str(p.pid),
            )
            keys.add(str(p.pid))
        if not t.row_count:
            return
        if follow_key in keys:
            t.move_cursor(row=t.get_row_index(follow_key), animate=False, scroll=False)
        elif t.following:  # followed process exited: stay at the same position
            t.move_cursor(row=min(old_row, t.row_count - 1), animate=False, scroll=False)
        else:
            t.move_cursor(row=0, animate=False, scroll=False)
        t.scroll_to(y=scroll_y, animate=False)
        # Keep the cursor visible if it moved off-screen.
        t.move_cursor(row=t.cursor_row, animate=False, scroll=True)

    def _update_status(self) -> None:
        """Only shown while a filter is active or updates are paused (htop shows nothing otherwise)."""
        st = self.query_one("#status", Static)
        if not (self.filter_text or self.paused):
            st.remove_class("visible")
            return
        s = Text()
        if self.filter_text:
            s.append("Filter: ", style=C_TEXT)
            s.append(self.filter_text, style=C_VALUE)
            s.append("   ")
        if self.paused:
            s.append("PAUSED", style="black on yellow")
        st.update(s)
        st.add_class("visible")

    # ---- actions -------------------------------------------------------
    def _apply_sort(self, key: str, desc: bool) -> None:
        self.sort_key, self.sort_desc = key, desc
        self.table.following = False
        self._build_columns()
        self._refresh_table()
        self.table.move_cursor(row=0, animate=False)
        self._update_status()

    def action_sort(self, key: str) -> None:
        """Select a sort column; pressing the same key again flips direction."""
        if key == "res":
            key = "mem"
        if key == self.sort_key:
            self._apply_sort(key, not self.sort_desc)
        else:
            self._apply_sort(key, COL[key][4])

    def action_cycle_sort(self) -> None:
        i = SORT_CYCLE.index(self.sort_key) if self.sort_key in SORT_CYCLE else -1
        key = SORT_CYCLE[(i + 1) % len(SORT_CYCLE)]
        self._apply_sort(key, COL[key][4])

    def action_invert(self) -> None:
        self._apply_sort(self.sort_key, not self.sort_desc)

    def on_data_table_header_selected(self, ev: DataTable.HeaderSelected) -> None:
        self.action_sort(ev.column_key.value)

    def action_filter(self) -> None:
        inp = self.query_one("#filter", Input)
        inp.add_class("visible")
        inp.value = self.filter_text
        inp.focus()

    def on_input_changed(self, ev: Input.Changed) -> None:
        self.filter_text = ev.value.strip()
        self._refresh_table()
        self._update_status()

    def on_input_submitted(self, ev: Input.Submitted) -> None:
        ev.input.remove_class("visible")
        self.table.focus()

    def action_clear_filter(self) -> None:
        inp = self.query_one("#filter", Input)
        inp.remove_class("visible")
        if self.filter_text or inp.value:
            inp.value = ""
            self.filter_text = ""
            self._refresh_table()
            self._update_status()
        self.table.focus()

    def action_pause(self) -> None:
        self.paused = not self.paused
        self._update_status()

    def _set_interval(self, v: float) -> None:
        self.interval = max(0.5, min(10.0, round(v, 1)))
        if self.timer is not None:
            self.timer.stop()
        self.timer = self.set_interval(self.interval, self.tick)
        self.notify(f"Refresh every {self.interval:.1f}s", timeout=1.5)

    def action_faster(self) -> None:
        self._set_interval(self.interval - 0.5)

    def action_slower(self) -> None:
        self._set_interval(self.interval + 0.5)

    def action_kill(self) -> None:
        t = self.table
        if not t.row_count:
            return
        try:
            pid = int(t.coordinate_to_cell_key(t.cursor_coordinate).row_key.value)
        except Exception:
            return
        if pid == os.getpid():
            self.notify("That's gtop itself. Use q to quit.", severity="warning")
            return
        proc = next((p for p in (self.snap.procs if self.snap else []) if p.pid == pid), None)
        desc = proc.cmd[:200] if proc else "?"
        extra = ""
        if proc and (proc.gpu_mem or 0) > 0 and "python" in proc.name.lower():
            extra = "\n\nThis looks like a GPU training process."

        def done(ok: bool | None) -> None:
            if not ok:
                return
            try:
                psutil.Process(pid).terminate()
                self.notify(f"Terminated PID {pid}")
            except psutil.NoSuchProcess:
                self.notify(f"PID {pid} already exited", severity="warning")
            except psutil.AccessDenied:
                self.notify(f"Access denied for PID {pid} (try an admin terminal)", severity="error")
            except Exception as e:
                self.notify(f"Kill failed: {e!r}", severity="error")

        self.push_screen(ConfirmKill(f"Kill PID {pid}?\n\n{desc}{extra}"), done)


def main() -> None:
    ap = argparse.ArgumentParser(description="htop + nvtop for Windows")
    ap.add_argument("-d", "--delay", type=float, default=1.5, help="refresh interval in seconds (default 1.5)")
    ap.add_argument("-s", "--sort", choices=SORT_CYCLE + ["user", "thr"], default="cpu")
    a = ap.parse_args()
    app = GTop(interval=max(0.5, a.delay))
    app.sort_key, app.sort_desc = a.sort, COL[a.sort][4]
    app.run()


if __name__ == "__main__":
    main()
