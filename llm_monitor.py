#!/usr/bin/env python3
"""
llm_monitor.py — Cross-Platform LLM System Monitor
Displays real-time CPU, GPU, Neural Engine, RAM, swap, thermals,
power draw, Ollama, and Claude Code process stats.

Requirements:
    pip install psutil rich
    macOS: uses powermetrics (requires sudo) and sysctl
    Linux/Windows: basic metrics via psutil

Usage:
    python ~/aiops/llm_monitor.py
    python ~/aiops/llm_monitor.py --interval 2
    sudo python ~/aiops/llm_monitor.py   # macOS: full GPU/thermal metrics
"""

import argparse
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from typing import Optional

try:
    import psutil
except ImportError:
    print("[ERROR] psutil not installed. Run: pip install psutil")
    sys.exit(1)

try:
    from rich.console import Console
    from rich.layout import Layout
    from rich.live import Live
    from rich.panel import Panel
    from rich.table import Table
    from rich.text import Text
    from rich import box
except ImportError:
    print("[ERROR] rich not installed. Run: pip install rich")
    sys.exit(1)

console = Console()

POWERMETRICS_TIMEOUT = 2.5
OLLAMA_PROCESS_NAMES = {"ollama", "ollama_llama_server"}
CLAUDE_CODE_PROCESS_NAMES = {"claude"}

PLATFORM = platform.system()   # "Darwin", "Linux", "Windows"
IS_MACOS   = PLATFORM == "Darwin"
IS_LINUX   = PLATFORM == "Linux"
IS_WINDOWS = PLATFORM == "Windows"

def _is_root() -> bool:
    if IS_WINDOWS:
        try:
            import ctypes
            return ctypes.windll.shell32.IsUserAnAdmin() != 0
        except Exception:
            return False
    try:
        return os.geteuid() == 0
    except AttributeError:
        return False

IS_ROOT = _is_root()

def _page_size() -> int:
    try:
        import resource
        return resource.getpagesize()
    except Exception:
        pass
    try:
        out = subprocess.check_output(["sysctl", "-n", "hw.pagesize"], text=True).strip()
        return int(out)
    except Exception:
        return 4096


# ── Dataclasses ──────────────────────────────────────────────────────────────

@dataclass
class ThermalPowerMetrics:
    cpu_die_temp: Optional[float] = None
    gpu_die_temp: Optional[float] = None
    cpu_power_w: Optional[float] = None
    gpu_power_w: Optional[float] = None
    ane_power_w: Optional[float] = None
    package_power_w: Optional[float] = None
    cpu_freq_mhz: Optional[float] = None
    gpu_freq_mhz: Optional[float] = None
    cpu_active_pct: Optional[float] = None
    gpu_active_pct: Optional[float] = None
    ane_active_pct: Optional[float] = None

@dataclass
class MemoryStats:
    total_gb: float = 0.0
    used_gb: float = 0.0
    available_gb: float = 0.0
    percent: float = 0.0
    swap_total_gb: float = 0.0
    swap_used_gb: float = 0.0
    swap_percent: float = 0.0
    wired_gb: float = 0.0
    compressed_gb: float = 0.0

@dataclass
class CPUStats:
    percent_per_core: list = field(default_factory=list)
    overall_percent: float = 0.0
    freq_current_mhz: Optional[float] = None
    freq_max_mhz: Optional[float] = None
    load_avg_1: float = 0.0
    load_avg_5: float = 0.0
    load_avg_15: float = 0.0
    p_core_count: Optional[int] = None
    e_core_count: Optional[int] = None

@dataclass
class OllamaProcess:
    pid: int
    name: str
    cpu_percent: float
    mem_rss_gb: float
    threads: int
    status: str
    model_hint: str = ""

@dataclass
class ClaudeCodeProcess:
    pid: int
    name: str
    cpu_percent: float
    mem_rss_gb: float
    threads: int
    status: str
    cwd: str = ""


# ── Collectors ───────────────────────────────────────────────────────────────

def collect_memory() -> MemoryStats:
    vm = psutil.virtual_memory()
    sw = psutil.swap_memory()
    stats = MemoryStats(
        total_gb=vm.total / 1e9,
        used_gb=vm.used / 1e9,
        available_gb=vm.available / 1e9,
        percent=vm.percent,
        swap_total_gb=sw.total / 1e9,
        swap_used_gb=sw.used / 1e9,
        swap_percent=sw.percent,
    )
    # macOS wired / compressed — vm_stat
    if IS_MACOS:
        try:
            out = subprocess.check_output(["vm_stat"], text=True)
            ps = _page_size()
            wired = re.search(r"Pages wired down:\s+(\d+)", out)
            compressed = re.search(r"Pages stored in compressor:\s+(\d+)", out)
            if wired:
                stats.wired_gb = int(wired.group(1)) * ps / 1e9
            if compressed:
                stats.compressed_gb = int(compressed.group(1)) * ps / 1e9
        except Exception:
            pass
    return stats


def collect_cpu() -> CPUStats:
    stats = CPUStats()
    cores = psutil.cpu_percent(percpu=True, interval=0.2)
    stats.percent_per_core = cores or []
    stats.overall_percent = sum(cores) / len(cores) if cores else 0.0
    try:
        freq = psutil.cpu_freq()
        if freq:
            stats.freq_current_mhz = freq.current
            stats.freq_max_mhz = freq.max
    except Exception:
        pass
    try:
        load = os.getloadavg()
        stats.load_avg_1, stats.load_avg_5, stats.load_avg_15 = load
    except (AttributeError, OSError):
        # Windows doesn't have getloadavg
        pass
    # Apple Silicon P/E core split
    if IS_MACOS:
        try:
            p = subprocess.check_output(["sysctl", "-n", "hw.perflevel0.logicalcpu"], text=True).strip()
            e = subprocess.check_output(["sysctl", "-n", "hw.perflevel1.logicalcpu"], text=True).strip()
            stats.p_core_count = int(p)
            stats.e_core_count = int(e)
        except Exception:
            pass
    return stats


def collect_powermetrics() -> ThermalPowerMetrics:
    """macOS only — requires sudo. Handles both Apple Silicon and Intel JSON layouts."""
    m = ThermalPowerMetrics()
    if not IS_MACOS or not IS_ROOT:
        return m
    if not shutil.which("powermetrics"):
        return m
    try:
        raw = subprocess.check_output(
            ["powermetrics", "--samplers", "cpu_power,gpu_power,thermal",
             "--format", "json", "-n", "1", "-i", str(int(POWERMETRICS_TIMEOUT * 1000))],
            stderr=subprocess.DEVNULL,
            timeout=POWERMETRICS_TIMEOUT + 2,
        )
        text = raw.decode("utf-8", errors="replace")
        idx = text.find("{")
        if idx == -1:
            return m
        text = text[idx:]
        depth, end = 0, 0
        for i, ch in enumerate(text):
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    end = i + 1
                    break
        data = json.loads(text[:end])
        processor = data.get("processor") or {}

        # Power — keys present on both Apple Silicon and Intel
        try:
            m.cpu_power_w = (processor.get("cpu_energy") or 0) / 1e3
        except Exception:
            pass
        try:
            m.gpu_power_w = (processor.get("gpu_energy") or 0) / 1e3
        except Exception:
            pass
        try:
            m.ane_power_w = (processor.get("ane_energy") or 0) / 1e3 or None
        except Exception:
            pass
        try:
            m.package_power_w = (processor.get("package_energy") or 0) / 1e3 or None
        except Exception:
            pass

        # Freq / active — Apple Silicon uses clusters[], Intel uses packages[].cores[]
        freqs, actives = [], []
        try:
            for cluster in processor.get("clusters") or []:
                if "freq_hz" in cluster:
                    freqs.append(cluster["freq_hz"] / 1e6)
                if "active_ratio" in cluster:
                    actives.append(cluster["active_ratio"] * 100)
        except Exception:
            pass
        if not freqs:  # Intel fallback
            try:
                for pkg in processor.get("packages") or []:
                    for core in pkg.get("cores") or []:
                        if "freq_hz" in core:
                            freqs.append(core["freq_hz"] / 1e6)
                        if "active_ratio" in core:
                            actives.append(core["active_ratio"] * 100)
            except Exception:
                pass
        if freqs:
            m.cpu_freq_mhz = sum(freqs) / len(freqs)
        if actives:
            m.cpu_active_pct = sum(actives) / len(actives)

        # GPU (Apple Silicon)
        try:
            gpu = data.get("gpu") or {}
            m.gpu_freq_mhz = (gpu.get("freq_hz") or 0) / 1e6 or None
            m.gpu_active_pct = (gpu.get("active_ratio") or 0) * 100 or None
        except Exception:
            pass

        # Temperatures — dynamic key scan works across chip generations
        try:
            temps = data.get("thermal") or {}
            for key, val in temps.items():
                if not isinstance(val, (int, float)):
                    continue
                k = key.lower()
                if "cpu" in k and "die" in k and m.cpu_die_temp is None:
                    m.cpu_die_temp = float(val)
                elif "gpu" in k and "die" in k and m.gpu_die_temp is None:
                    m.gpu_die_temp = float(val)
        except Exception:
            pass

    except Exception:
        pass
    return m


def collect_linux_temps() -> tuple:
    """Returns (cpu_temp, gpu_temp) floats or None via psutil sensors (Linux)."""
    cpu_temp = gpu_temp = None
    if not IS_LINUX:
        return cpu_temp, gpu_temp
    try:
        sensors = psutil.sensors_temperatures()
        for name, entries in sensors.items():
            n = name.lower()
            for e in entries:
                if e.current and e.current > 0:
                    if cpu_temp is None and any(k in n for k in ("coretemp", "k10temp", "cpu", "acpi")):
                        cpu_temp = e.current
                    elif gpu_temp is None and any(k in n for k in ("amdgpu", "nouveau", "nvidia")):
                        gpu_temp = e.current
    except Exception:
        pass
    return cpu_temp, gpu_temp


def collect_ollama_processes() -> list:
    procs = []
    for proc in psutil.process_iter(
        ["pid", "name", "cpu_percent", "memory_info", "num_threads", "status", "cmdline"]
    ):
        try:
            name = proc.info["name"] or ""
            if name.lower() in OLLAMA_PROCESS_NAMES or "ollama" in name.lower():
                cmdline = proc.info.get("cmdline") or []
                model_hint = ""
                for i, part in enumerate(cmdline):
                    if part in ("--model", "-m") and i + 1 < len(cmdline):
                        model_hint = cmdline[i + 1]
                    elif part.endswith(".gguf"):
                        model_hint = os.path.basename(part)
                mem_info = proc.info["memory_info"]
                mem_rss = mem_info.rss / 1e9 if mem_info else 0.0
                procs.append(OllamaProcess(
                    pid=proc.info["pid"],
                    name=name,
                    cpu_percent=proc.info["cpu_percent"] or 0.0,
                    mem_rss_gb=mem_rss,
                    threads=proc.info["num_threads"] or 0,
                    status=proc.info["status"] or "?",
                    model_hint=model_hint,
                ))
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            pass
    return procs


def collect_claude_code_processes() -> list:
    procs = []
    for proc in psutil.process_iter(
        ["pid", "name", "cpu_percent", "memory_info", "num_threads", "status", "cmdline"]
    ):
        try:
            name = proc.info["name"] or ""
            cmdline = proc.info.get("cmdline") or []
            cmdline_str = " ".join(cmdline)
            is_claude = (
                name.lower() in CLAUDE_CODE_PROCESS_NAMES
                or (name.lower() in ("node", "node.js") and (
                    "claude-code" in cmdline_str
                    or "@anthropic-ai/claude-code" in cmdline_str
                    or "/claude" in cmdline_str
                ))
            )
            if not is_claude:
                continue
            mem_info = proc.info["memory_info"]
            mem_rss = mem_info.rss / 1e9 if mem_info else 0.0
            try:
                cwd = proc.cwd()
            except Exception:
                cwd = ""
            procs.append(ClaudeCodeProcess(
                pid=proc.info["pid"],
                name=name,
                cpu_percent=proc.info["cpu_percent"] or 0.0,
                mem_rss_gb=mem_rss,
                threads=proc.info["num_threads"] or 0,
                status=proc.info["status"] or "?",
                cwd=os.path.basename(cwd) if cwd else "",
            ))
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            pass
    return procs


def get_disk_io() -> tuple:
    try:
        c1 = psutil.disk_io_counters()
        time.sleep(0.3)
        c2 = psutil.disk_io_counters()
        if c1 is None or c2 is None:
            return 0.0, 0.0
        read_mbs = (c2.read_bytes - c1.read_bytes) / 0.3 / 1e6
        write_mbs = (c2.write_bytes - c1.write_bytes) / 0.3 / 1e6
        return max(read_mbs, 0.0), max(write_mbs, 0.0)
    except Exception:
        return 0.0, 0.0


# ── Rendering helpers ─────────────────────────────────────────────────────────

def _bar(percent: float, width: int = 20) -> Text:
    percent = max(0.0, min(100.0, percent))
    filled = int(percent / 100 * width)
    bar = "█" * filled + "░" * (width - filled)
    color = "bold red" if percent >= 85 else "bold yellow" if percent >= 60 else "bold green"
    t = Text()
    t.append(f"[{bar}] ", style=color)
    t.append(f"{percent:5.1f}%", style=color)
    return t

def _temp_color(temp: Optional[float]) -> str:
    if temp is None:
        return "dim"
    return "bold red" if temp >= 90 else "bold yellow" if temp >= 75 else "bold green"

def _na(reason: str = "") -> str:
    suffix = f" ({reason})" if reason else ""
    return f"[dim]N/A{suffix}[/]"


# ── Panel builders ────────────────────────────────────────────────────────────

def build_cpu_panel(cpu: CPUStats, pm: ThermalPowerMetrics, linux_cpu_temp: Optional[float]) -> Panel:
    t = Table(show_header=False, box=None, padding=(0, 1))
    t.add_column("Label", style="bold cyan", width=22)
    t.add_column("Value")
    t.add_row("Overall CPU", _bar(cpu.overall_percent))
    if cpu.load_avg_1 or cpu.load_avg_5 or cpu.load_avg_15:
        t.add_row("Load Avg (1/5/15)",
                  f"[cyan]{cpu.load_avg_1:.2f}[/]  [yellow]{cpu.load_avg_5:.2f}[/]  [dim]{cpu.load_avg_15:.2f}[/]")
    if cpu.p_core_count:
        t.add_row("P-Cores / E-Cores", f"[green]{cpu.p_core_count}[/] / [yellow]{cpu.e_core_count}[/]")
    freq = pm.cpu_freq_mhz or cpu.freq_current_mhz
    if freq:
        t.add_row("CPU Freq", f"[cyan]{freq:.0f} MHz[/]")
    if pm.cpu_power_w:
        t.add_row("CPU Power", f"[yellow]{pm.cpu_power_w:.2f} W[/]")
    if pm.cpu_active_pct is not None:
        t.add_row("CPU Active (PM)", _bar(pm.cpu_active_pct))
    cpu_temp = pm.cpu_die_temp or linux_cpu_temp
    if cpu_temp is not None:
        color = _temp_color(cpu_temp)
        t.add_row("CPU Die Temp", f"[{color}]{cpu_temp:.1f} °C[/]")
    cores = cpu.percent_per_core[:16]
    if cores:
        core_line = Text()
        for i, pct in enumerate(cores):
            color = "red" if pct >= 85 else "yellow" if pct >= 60 else "green"
            core_line.append(f"C{i:<2}", style="dim")
            core_line.append(f"{pct:4.0f}%  ", style=color)
        t.add_row("Per-Core %", core_line)
    return Panel(t, title="[bold white]CPU", border_style="cyan")


def build_gpu_ane_panel(pm: ThermalPowerMetrics, linux_gpu_temp: Optional[float]) -> Panel:
    t = Table(show_header=False, box=None, padding=(0, 1))
    t.add_column("Label", style="bold magenta", width=22)
    t.add_column("Value")

    if pm.gpu_active_pct is not None:
        t.add_row("GPU Utilization", _bar(pm.gpu_active_pct))
    elif IS_MACOS and not IS_ROOT:
        t.add_row("GPU Utilization", _na("needs sudo"))
    elif not IS_MACOS:
        t.add_row("GPU Utilization", _na("macOS only"))

    if pm.gpu_freq_mhz:
        t.add_row("GPU Freq", f"[magenta]{pm.gpu_freq_mhz:.0f} MHz[/]")
    if pm.gpu_power_w:
        t.add_row("GPU Power", f"[yellow]{pm.gpu_power_w:.2f} W[/]")

    gpu_temp = pm.gpu_die_temp or linux_gpu_temp
    if gpu_temp is not None:
        color = _temp_color(gpu_temp)
        t.add_row("GPU Die Temp", f"[{color}]{gpu_temp:.1f} °C[/]")

    t.add_row("", "")

    if IS_MACOS:
        if pm.ane_power_w is not None:
            t.add_row("ANE Power", f"[blue]{pm.ane_power_w:.2f} W[/]")
        else:
            t.add_row("ANE Power", _na("needs sudo"))
    else:
        t.add_row("ANE Power", _na("macOS only"))

    if pm.package_power_w:
        t.add_row("Package Total", f"[bold yellow]{pm.package_power_w:.2f} W[/]")

    if IS_MACOS and not IS_ROOT:
        t.add_row("[dim]", "[dim]sudo for full metrics[/]")

    return Panel(t, title="[bold white]GPU / ANE / Power", border_style="magenta")


def build_memory_panel(mem: MemoryStats) -> Panel:
    t = Table(show_header=False, box=None, padding=(0, 1))
    t.add_column("Label", style="bold blue", width=22)
    t.add_column("Value")
    t.add_row("RAM Used / Total",
              f"[bold blue]{mem.used_gb:.2f} GB[/] / [dim]{mem.total_gb:.1f} GB[/]")
    t.add_row("RAM Pressure", _bar(mem.percent))
    t.add_row("Available", f"[green]{mem.available_gb:.2f} GB[/]")
    if mem.wired_gb:
        t.add_row("Wired", f"[yellow]{mem.wired_gb:.2f} GB[/]")
    if mem.compressed_gb:
        t.add_row("Compressed", f"[cyan]{mem.compressed_gb:.2f} GB[/]")
    t.add_row("", "")
    swap_color = "red" if mem.swap_percent > 50 else "yellow" if mem.swap_percent > 10 else "green"
    t.add_row("Swap Used / Total",
              f"[{swap_color}]{mem.swap_used_gb:.2f} GB[/] / [dim]{mem.swap_total_gb:.1f} GB[/]")
    t.add_row("Swap Usage", _bar(mem.swap_percent))
    if mem.swap_percent > 20:
        t.add_row("[bold red]⚠ WARNING", "[bold red]High swap — model thrashing disk[/]")
    return Panel(t, title="[bold white]Memory", border_style="blue")


def build_ollama_panel(procs: list) -> Panel:
    if not procs:
        return Panel(
            "[dim]No Ollama processes detected.\nStart a model: ollama run <model>[/]",
            title="[bold white]Ollama", border_style="yellow",
        )
    t = Table(show_header=True, box=box.SIMPLE, header_style="bold yellow")
    t.add_column("PID", width=7)
    t.add_column("Name", width=22)
    t.add_column("CPU%", width=6)
    t.add_column("RSS GB", width=7)
    t.add_column("Threads", width=8)
    t.add_column("Status", width=10)
    t.add_column("Model Hint", width=30)
    for p in procs:
        cpu_color = "red" if p.cpu_percent > 80 else "yellow" if p.cpu_percent > 40 else "green"
        t.add_row(str(p.pid), p.name,
                  f"[{cpu_color}]{p.cpu_percent:.1f}[/]",
                  f"[cyan]{p.mem_rss_gb:.2f}[/]",
                  str(p.threads), p.status,
                  f"[dim]{p.model_hint or '—'}[/]")
    return Panel(t, title="[bold white]Ollama Processes", border_style="yellow")


def build_claude_panel(procs: list) -> Panel:
    if not procs:
        return Panel(
            "[dim]No Claude Code processes detected.[/]",
            title="[bold white]Claude Code", border_style="green",
        )
    t = Table(show_header=True, box=box.SIMPLE, header_style="bold green")
    t.add_column("PID", width=7)
    t.add_column("Name", width=12)
    t.add_column("CPU%", width=6)
    t.add_column("RSS GB", width=7)
    t.add_column("Threads", width=8)
    t.add_column("Status", width=10)
    t.add_column("Working Dir", width=25)
    for p in procs:
        cpu_color = "red" if p.cpu_percent > 80 else "yellow" if p.cpu_percent > 40 else "green"
        t.add_row(str(p.pid), p.name,
                  f"[{cpu_color}]{p.cpu_percent:.1f}[/]",
                  f"[cyan]{p.mem_rss_gb:.2f}[/]",
                  str(p.threads), p.status,
                  f"[dim]{p.cwd or '—'}[/]")
    label = f"[bold white]Claude Code ({len(procs)} session{'s' if len(procs) != 1 else ''})"
    return Panel(t, title=label, border_style="green")


def build_system_panel(cpu: CPUStats) -> Panel:
    t = Table(show_header=False, box=None, padding=(0, 1))
    t.add_column("Label", style="bold white", width=22)
    t.add_column("Value")
    read_mbs, write_mbs = get_disk_io()
    t.add_row("Disk Read", f"[green]{read_mbs:.2f} MB/s[/]")
    t.add_row("Disk Write", f"[yellow]{write_mbs:.2f} MB/s[/]")
    try:
        t.add_row("Processes", f"[dim]{len(psutil.pids())}[/]")
    except Exception:
        pass
    try:
        uptime_s = time.time() - psutil.boot_time()
        h, rem = divmod(int(uptime_s), 3600)
        m, s = divmod(rem, 60)
        t.add_row("Uptime", f"[dim]{h}h {m}m {s}s[/]")
    except Exception:
        pass
    t.add_row("Platform", f"[dim]{PLATFORM} · {'admin' if IS_ROOT else 'user'}[/]")
    return Panel(t, title="[bold white]System", border_style="white")


# ── Dashboard ─────────────────────────────────────────────────────────────────

def build_dashboard(interval: float, show_ollama: bool, show_claude: bool) -> None:
    sudo_note = ""
    if IS_MACOS:
        sudo_note = "  [sudo: full metrics]" if IS_ROOT else "  [no sudo: limited]"
    elif IS_WINDOWS:
        sudo_note = "  [admin]" if IS_ROOT else "  [user]"

    header = Text(
        f"  LLM System Monitor  ·  {PLATFORM}  ·  Refresh: {interval}s{sudo_note}  ",
        style="bold white on dark_blue", justify="center",
    )
    footer = Text("  Ctrl+C to exit  ", style="dim", justify="center")

    with Live(console=console, refresh_per_second=1, screen=True) as live:
        while True:
            try:
                cpu  = collect_cpu()
                mem  = collect_memory()
                pm   = collect_powermetrics()
                linux_cpu_temp, linux_gpu_temp = collect_linux_temps()
                ollama_procs = collect_ollama_processes() if show_ollama else []
                claude_procs = collect_claude_code_processes() if show_claude else []

                layout = Layout()
                layout.split_column(
                    Layout(Panel(header, border_style="dark_blue"), size=3),
                    Layout(name="row1", size=20),
                    Layout(name="row2"),
                    Layout(Panel(footer, border_style="dim"), size=3),
                )
                layout["row1"].split_row(
                    Layout(build_cpu_panel(cpu, pm, linux_cpu_temp)),
                    Layout(build_gpu_ane_panel(pm, linux_gpu_temp)),
                    Layout(build_memory_panel(mem)),
                )
                layout["row2"].split_row(
                    Layout(build_ollama_panel(ollama_procs), ratio=2),
                    Layout(build_claude_panel(claude_procs), ratio=2),
                    Layout(build_system_panel(cpu), ratio=1),
                )
                live.update(layout)
                sleep_time = max(0.5, interval - POWERMETRICS_TIMEOUT) if (IS_MACOS and IS_ROOT) else interval
                time.sleep(sleep_time)
            except KeyboardInterrupt:
                break
            except Exception:
                live.stop()
                console.print_exception()
                raise


def main():
    parser = argparse.ArgumentParser(description="Cross-Platform LLM System Monitor")
    parser.add_argument("--interval", "-i", type=float, default=3.0,
                        help="Refresh interval in seconds (default: 3)")
    parser.add_argument("--no-ollama", action="store_true", help="Skip Ollama process scanning")
    parser.add_argument("--no-claude", action="store_true", help="Skip Claude Code process scanning")
    args = parser.parse_args()

    if IS_MACOS and not IS_ROOT:
        console.print(
            "[yellow]⚠  No sudo — GPU/ANE/power/thermal metrics unavailable.\n"
            "   Full metrics: [bold]sudo ./monitor[/bold][/yellow]\n"
        )
        time.sleep(1.5)
    elif IS_WINDOWS and not IS_ROOT:
        console.print("[yellow]⚠  Not running as Administrator — some metrics may be limited.[/yellow]\n")
        time.sleep(1.0)

    build_dashboard(
        interval=args.interval,
        show_ollama=not args.no_ollama,
        show_claude=not args.no_claude,
    )
    console.print("\n[dim]Monitor stopped.[/dim]")


if __name__ == "__main__":
    main()
