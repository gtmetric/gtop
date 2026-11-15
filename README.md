# gtop: htop + nvtop for Windows, Linux and macOS

**A terminal system monitor that shows CPU, memory, GPU usage and per-process VRAM in one window, with htop's look and nvtop's GPU charts.**

gtop combines htop's CPU meters and process list with nvtop's GPU panel: usage and VRAM history charts, temperature, power and CUDA version. The process table shows each process's GPU% and VRAM, so you can see which process is using your GPU. It runs on Windows, where neither htop nor nvtop is available, as well as on Linux and macOS.

![gtop, a terminal system monitor, showing CPU meters, an NVIDIA GPU panel with usage and VRAM charts, and a process list with per-process GPU and VRAM columns](https://raw.githubusercontent.com/gtmetric/gtop/main/docs/screenshot.png)

## Why

htop doesn't run on Windows, and nvtop targets Linux. Task Manager shows per-process GPU use, but not in a terminal, and you can't sort it alongside CPU in an htop-style view. If you train models, gtop shows everything in one place on any OS: which process is using the GPU, how much VRAM it holds, and whether the CPU is the bottleneck.

## Features

- **htop-style header:** a meter for each core split into user, kernel and interrupt time, plus memory and swap meters, task and thread counts, load average and uptime
- **nvtop-style GPU panel:**
  - utilization, VRAM, temperature, power, fan and clock speed
  - CUDA and driver versions
  - history charts for usage and VRAM, with current, average and peak values
- **Per-process GPU% and VRAM columns**, sortable like any other
- **Sorting** by CPU, memory, GPU% or VRAM with one key, or by clicking a column header
- **Filtering** by name, command line, user or PID, and **killing** a process after confirmation
- **Light on resources:** on Windows, one `NtQuerySystemInformation` call per refresh (~10–30 ms with 400+ processes). Sampling runs in a background thread, so the UI stays responsive.

## Platform support

| | Windows | Linux | macOS |
|---|---|---|---|
| CPU meters, memory, process list | ✓ | ✓ | ✓ |
| GPU panel | NVIDIA | NVIDIA | Apple GPU (utilization and memory only) |
| Per-process GPU% / VRAM | any GPU vendor, via Windows' GPU counters | NVIDIA, via NVML¹ | not available |

¹ Not available inside WSL: its GPU passthrough hides processes from NVML.

gtop is tested on Windows 11 and Ubuntu (WSL2) with an NVIDIA RTX GPU. macOS support is new and runs in CI only, so reports from Mac users are welcome.

## Install

You need Python 3.10 or newer. Install with [pipx](https://pipx.pypa.io/):

```sh
pipx install gtop-nv
```

Or with [uv](https://docs.astral.sh/uv/):

```sh
uv tool install gtop-nv
```

Then run `gtop`. If the command isn't found, open a new terminal so it picks up the updated PATH.

### Windows: Smart App Control

gtop runs on your installed Python rather than as a bundled `.exe`, because Windows Smart App Control blocks unsigned executables. pipx and uv still create a small unsigned `gtop.exe` launcher, though, and on some machines Smart App Control blocks it ("An Application Control policy has blocked this file"). If that happens, run gtop through the tool's Python, which is signed:

```powershell
# pipx
& "$env:LOCALAPPDATA\pipx\pipx\venvs\gtop-nv\Scripts\python.exe" -m gtop_nv
# uv
& "$(uv tool dir)\gtop-nv\Scripts\python.exe" -m gtop_nv
```

To keep typing just `gtop`, add a function to your PowerShell profile (`notepad $PROFILE`):

```powershell
function gtop { & "$env:LOCALAPPDATA\pipx\pipx\venvs\gtop-nv\Scripts\python.exe" -m gtop_nv @args }
```

## Usage

```
gtop [-d DELAY] [-s {cpu,mem,gpu,vram,pid,name,user,thr}]
```

| Key | Action |
|---|---|
| `c` `m` `g` `v` | Sort by CPU / memory / GPU% / VRAM (press again to reverse) |
| `p` `n` | Sort by PID / command |
| `F6` | Cycle through sort columns |
| click a header | Sort by that column |
| `F4` or `/` | Filter (Enter keeps it, Esc clears it) |
| `↑` `↓` `PgUp` `PgDn` | Move the selection (it then stays on that process) |
| `Home` | Back to the top |
| `F9` or `k` | Kill the selected process, after confirmation |
| `Space` | Pause updates |
| `+` / `-` | Refresh faster / slower |
| `F10`, `q` or `Ctrl+C` | Quit |

`python -m gtop_nv.sampler --once` prints a single plain-text sample, which is handy for scripting or debugging.

## Notes

- **CPU% per process** follows htop's convention: 100% means one full core, so a busy multi-threaded process can exceed 100%.
- **CPU bar colors:** green is user time (including nice), red is kernel time (including steal), yellow is interrupts, and magenta is soft IRQs on Linux or DPCs on Windows.
- **On Windows, the "running" count** is approximate, because Windows has no "R" process state. gtop counts processes that used at least 1% CPU in the last interval.
- **On Windows, load average** is psutil's emulation, based on the processor queue length. It often stays near zero even under heavy CPU load.
- **Colors** come from your terminal's own 16-color palette, the same way htop does it.

## FAQ

**Is there htop for Windows?**
htop runs on Linux, macOS and the BSDs, but not on Windows. gtop reproduces htop's interface on Windows (same meters, colors, sortable process list and F-key bar) and adds GPU monitoring.

**Can I run nvtop on Windows?**
nvtop targets Linux. gtop gives you an nvtop-style GPU panel on Windows: utilization and VRAM history charts, temperature, power, fan, clocks, and CUDA and driver versions.

**How do I see which process is using my GPU or VRAM on Windows?**
Run `gtop` and press `v` to sort by VRAM, or `g` to sort by GPU%. Per-process numbers come from the same Windows GPU performance counters Task Manager uses. `nvidia-smi` shows "N/A" for per-process memory under the default WDDM driver mode.

**Does it work with AMD or Intel GPUs?**
On Windows, partly: the per-process GPU% and VRAM columns come from Windows' own counters, which work with any GPU vendor. The GPU panel (charts, temperature, power, CUDA version) needs NVIDIA's NVML, or an Apple GPU on macOS.

**Why use it on Linux instead of htop + nvtop?**
You get both in one window, and the process list has GPU% and VRAM columns you can sort by. If you already run htop and nvtop side by side, gtop does the same in a single pane.

**Does it show the GPU on a Mac?**
Yes, on Apple Silicon: GPU utilization and GPU memory in use, read from the I/O Registry without sudo. Temperature, power and per-process GPU usage aren't shown, because macOS only exposes them to `sudo powermetrics`.

**Is it useful for deep learning / PyTorch training?**
That's what it was built for. You can watch GPU utilization and VRAM over time, spot a data-loading bottleneck (busy CPU, idle GPU), and see each training process's VRAM, all in one terminal next to your training run.

## License

[MIT](https://github.com/gtmetric/gtop/blob/main/LICENSE)
