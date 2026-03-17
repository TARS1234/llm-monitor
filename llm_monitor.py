#!/usr/bin/env python3
"""
llm_monitor.py — Cross-Platform LLM System Monitor
Displays real-time CPU, GPU, Neural Engine, RAM, swap, thermals,
power draw, Ollama, and Claude Code process stats.

Requirements:
    pip install psutil>=5.9 rich>=13.0
    macOS : uses powermetrics (sudo) and sysctl
    Linux : uses /proc/meminfo, RAPL powercap, psutil sensors, nvidia-smi
    Windows: uses wmic for temps, nvidia-smi for GPU

Usage:
    python llm_monitor.py
    python llm_monitor.py --interval 2
    sudo python llm_monitor.py        # macOS: full GPU/ANE/thermal/power
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
OLLAMA_PROCESS_NAMES      = {"ollama", "ollama_llama_server"}
CLAUDE_CODE_PROCESS_NAMES = {"claude"}

# Other AI agent CLIs
AI_AGENT_SPECS = {
    # tool_label : (exact_process_names, cmdline_substrings)
    "Codex":  ({"codex"},  ["@openai/codex", "/codex"]),
    "Gemini": ({"gemini"}, ["@google/generative-ai", "gemini-cli", "/gemini"]),
    "Grok":   ({"grok"},   ["@xai/grok", "grok-cli", "/grok"]),
}

PLATFORM   = platform.system()   # "Darwin" | "Linux" | "Windows"
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
        return int(subprocess.check_output(["sysctl", "-n", "hw.pagesize"], text=True).strip())
    except Exception:
        return 4096


# ── Dataclasses ───────────────────────────────────────────────────────────────

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
class NvidiaStats:
    util_pct: Optional[float] = None
    mem_used_gb: Optional[float] = None
    mem_total_gb: Optional[float] = None
    temp_c: Optional[float] = None
    power_w: Optional[float] = None
    gpu_name: str = ""

@dataclass
class MemoryStats:
    total_gb: float = 0.0
    used_gb: float = 0.0
    available_gb: float = 0.0
    percent: float = 0.0
    swap_total_gb: float = 0.0
    swap_used_gb: float = 0.0
    swap_percent: float = 0.0
    # macOS
    wired_gb: float = 0.0
    compressed_gb: float = 0.0
    # Linux
    cached_gb: float = 0.0
    buffers_gb: float = 0.0
    # Windows
    win_cached_gb: float = 0.0

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
    tool: str = "Claude"
    model: str = ""

@dataclass
class AIAgentProcess:
    tool: str
    pid: int
    name: str
    cpu_percent: float
    mem_rss_gb: float
    status: str
    cwd: str = ""


# ── Collectors ────────────────────────────────────────────────────────────────

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
    if IS_MACOS:
        try:
            out = subprocess.check_output(["vm_stat"], text=True)
            ps = _page_size()
            wired      = re.search(r"Pages wired down:\s+(\d+)", out)
            compressed = re.search(r"Pages stored in compressor:\s+(\d+)", out)
            if wired:
                stats.wired_gb      = int(wired.group(1))      * ps / 1e9
            if compressed:
                stats.compressed_gb = int(compressed.group(1)) * ps / 1e9
        except Exception:
            pass
    if IS_LINUX:
        try:
            with open("/proc/meminfo") as f:
                meminfo = f.read()
            cached  = re.search(r"^Cached:\s+(\d+)",  meminfo, re.M)
            buffers = re.search(r"^Buffers:\s+(\d+)", meminfo, re.M)
            if cached:
                stats.cached_gb  = int(cached.group(1))  * 1024 / 1e9
            if buffers:
                stats.buffers_gb = int(buffers.group(1)) * 1024 / 1e9
        except Exception:
            pass
    if IS_WINDOWS:
        try:
            cached = getattr(vm, "cached", None)
            if cached:
                stats.win_cached_gb = cached / 1e9
        except Exception:
            pass
    return stats


def collect_cpu() -> CPUStats:
    stats = CPUStats()
    cores = psutil.cpu_percent(percpu=True, interval=0.2)
    stats.percent_per_core = cores or []
    stats.overall_percent  = sum(cores) / len(cores) if cores else 0.0
    try:
        freq = psutil.cpu_freq()
        if freq:
            stats.freq_current_mhz = freq.current
            stats.freq_max_mhz     = freq.max
    except Exception:
        pass
    # psutil.getloadavg() is cross-platform since 5.9 (simulated on Windows)
    try:
        load = psutil.getloadavg()
        stats.load_avg_1, stats.load_avg_5, stats.load_avg_15 = load
    except (AttributeError, OSError):
        pass
    if IS_MACOS:
        try:
            p = subprocess.check_output(["sysctl", "-n", "hw.perflevel0.logicalcpu"], text=True).strip()
            e = subprocess.check_output(["sysctl", "-n", "hw.perflevel1.logicalcpu"], text=True).strip()
            stats.p_core_count = int(p)
            stats.e_core_count = int(e)
        except Exception:
            pass
    return stats


def _parse_powermetrics_text(text: str, m: ThermalPowerMetrics) -> None:
    """Fallback: parse powermetrics plain-text output (used when JSON is unavailable)."""
    patterns = [
        ("gpu_active_pct",  r"GPU HW active residency:\s+([\d.]+)%",         float),
        ("gpu_freq_mhz",    r"GPU HW active frequency:\s+([\d.]+) MHz",       float),
        ("gpu_power_w",     r"GPU Power:\s+([\d.]+) mW",                      lambda v: float(v) / 1000),
        ("cpu_power_w",     r"CPU Power:\s+([\d.]+) mW",                      lambda v: float(v) / 1000),
        ("ane_power_w",     r"ANE Power:\s+([\d.]+) mW",                      lambda v: float(v) / 1000),
        ("package_power_w", r"Combined Power[^:]*:\s+([\d.]+) mW",            lambda v: float(v) / 1000),
        ("cpu_die_temp",    r"CPU die temperature:\s+([\d.]+)",                float),
        ("gpu_die_temp",    r"GPU die temperature:\s+([\d.]+)",                float),
    ]
    for attr, pattern, conv in patterns:
        match = re.search(pattern, text)
        if match:
            setattr(m, attr, conv(match.group(1)))

    # CPU freq: average of all active cluster frequencies
    freqs = [float(f) for f in re.findall(
        r"(?:E-Cluster|P\d+-Cluster) HW active frequency:\s+([\d.]+) MHz", text)]
    if freqs:
        active = [f for f in freqs if f > 0]
        m.cpu_freq_mhz = sum(active) / len(active) if active else freqs[0]

    # CPU active: average of all cluster active residencies
    actives = [float(a) for a in re.findall(
        r"(?:E-Cluster|P\d+-Cluster) HW active residency:\s+([\d.]+)%", text)]
    if actives:
        m.cpu_active_pct = sum(actives) / len(actives)


def collect_powermetrics() -> ThermalPowerMetrics:
    """macOS only — sudo required. Handles Apple Silicon and Intel JSON layouts,
    and falls back to plain-text parsing when JSON is not available."""
    m = ThermalPowerMetrics()
    if not IS_MACOS or not IS_ROOT or not shutil.which("powermetrics"):
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
            # JSON not available on this macOS version — parse text output instead
            _parse_powermetrics_text(text, m)
            return m
        text = text[idx:]
        depth, end = 0, 0
        for i, ch in enumerate(text):
            if ch == "{":   depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    end = i + 1
                    break
        data      = json.loads(text[:end])
        processor = data.get("processor") or {}

        for attr, key in [("cpu_power_w", "cpu_energy"), ("gpu_power_w", "gpu_energy"),
                          ("ane_power_w", "ane_energy"), ("package_power_w", "package_energy")]:
            try:
                if key in processor:
                    setattr(m, attr, processor[key] / 1e3)
            except Exception:
                pass

        # Freq / active — Apple Silicon: clusters[]; Intel: packages[].cores[]
        freqs, actives = [], []
        try:
            for cluster in processor.get("clusters") or []:
                if "freq_hz"      in cluster: freqs.append(cluster["freq_hz"] / 1e6)
                if "active_ratio" in cluster: actives.append(cluster["active_ratio"] * 100)
        except Exception:
            pass
        if not freqs:
            try:
                for pkg in processor.get("packages") or []:
                    for core in pkg.get("cores") or []:
                        if "freq_hz"      in core: freqs.append(core["freq_hz"] / 1e6)
                        if "active_ratio" in core: actives.append(core["active_ratio"] * 100)
            except Exception:
                pass
        if freqs:   m.cpu_freq_mhz   = sum(freqs)   / len(freqs)
        if actives: m.cpu_active_pct = sum(actives)  / len(actives)

        try:
            gpu = data.get("gpu") or {}
            if "freq_hz" in gpu:
                m.gpu_freq_mhz   = gpu["freq_hz"] / 1e6
            if "active_ratio" in gpu:
                m.gpu_active_pct = gpu["active_ratio"] * 100
        except Exception:
            pass

        try:
            for key, val in (data.get("thermal") or {}).items():
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


def collect_nvidia_stats() -> NvidiaStats:
    """Works on Linux, Windows, and macOS (eGPU). Requires nvidia-smi in PATH."""
    s = NvidiaStats()
    if not shutil.which("nvidia-smi"):
        return s
    try:
        out = subprocess.check_output(
            ["nvidia-smi",
             "--query-gpu=name,utilization.gpu,memory.used,memory.total,temperature.gpu,power.draw",
             "--format=csv,noheader,nounits"],
            text=True, stderr=subprocess.DEVNULL, timeout=4,
        ).strip().splitlines()[0]   # first GPU only
        parts = [p.strip() for p in out.split(",")]
        s.gpu_name    = parts[0]
        s.util_pct    = float(parts[1])
        s.mem_used_gb = float(parts[2]) / 1024
        s.mem_total_gb = float(parts[3]) / 1024
        s.temp_c      = float(parts[4])
        try:
            val = float(parts[5])
            s.power_w = val if val > 0 else None
        except Exception:
            pass
    except Exception:
        pass
    return s


def collect_linux_rapl_power() -> Optional[float]:
    """Package power via Intel RAPL or AMD energy driver (Linux only)."""
    if not IS_LINUX:
        return None
    candidates = [
        "/sys/class/powercap/intel-rapl/intel-rapl:0/energy_uj",
        "/sys/class/powercap/intel-rapl:0/energy_uj",
        "/sys/class/powercap/amd-energy/amd_energy:0/energy_uj",
    ]
    for path in candidates:
        try:
            with open(path) as f: e1 = int(f.read())
            time.sleep(0.25)
            with open(path) as f: e2 = int(f.read())
            return max((e2 - e1) / 0.25 / 1e6, 0.0)
        except Exception:
            continue
    return None


def collect_temps() -> tuple:
    """
    Returns (cpu_temp, gpu_temp): Optional[float] each.
    Sources: psutil sensors (Linux), powermetrics fields are handled separately,
    wmic thermal zones (Windows).
    """
    cpu_temp = gpu_temp = None

    if IS_LINUX:
        try:
            sensors = psutil.sensors_temperatures()
            for name, entries in sensors.items():
                n = name.lower()
                for e in entries:
                    t = e.current
                    if not t or t <= 0:
                        continue
                    if cpu_temp is None and any(k in n for k in ("coretemp", "k10temp", "cpu", "acpitz")):
                        cpu_temp = t
                    elif gpu_temp is None and any(k in n for k in ("amdgpu", "nouveau", "nvidia", "radeon")):
                        gpu_temp = t
        except Exception:
            pass

    if IS_WINDOWS:
        try:
            out = subprocess.check_output(
                ["wmic", "/namespace:\\\\root\\wmi", "PATH",
                 "MSAcpi_ThermalZoneTemperature", "get", "CurrentTemperature"],
                text=True, stderr=subprocess.DEVNULL, timeout=5,
            )
            temps = []
            for line in out.strip().splitlines():
                line = line.strip()
                if line.isdigit():
                    c = (int(line) - 2732) / 10.0
                    if 0 < c < 120:
                        temps.append(c)
            if temps:
                cpu_temp = min(temps)   # lowest zone ≈ coolest / most representative
        except Exception:
            pass

    return cpu_temp, gpu_temp


def _ollama_running_models() -> list:
    """Query Ollama API for currently loaded model names. Returns list of name strings."""
    try:
        import urllib.request
        with urllib.request.urlopen("http://localhost:11434/api/ps", timeout=1) as r:
            data = json.loads(r.read())
        return [m["name"] for m in data.get("models", [])]
    except Exception:
        return []


def collect_ollama_processes() -> list:
    # Fetch loaded model names from the API once for the whole scan
    api_models = _ollama_running_models()
    runner_index = 0

    procs = []
    for proc in psutil.process_iter(
        ["pid", "name", "cpu_percent", "memory_info", "num_threads", "status", "cmdline"]
    ):
        try:
            name = proc.info["name"] or ""
            if name.lower() in OLLAMA_PROCESS_NAMES or "ollama" in name.lower():
                cmdline     = proc.info.get("cmdline") or []
                cmdline_str = " ".join(cmdline)
                model_hint  = ""

                if "runner" in cmdline_str:
                    # Dedicated runner subprocess — one loaded model
                    if runner_index < len(api_models):
                        model_hint = api_models[runner_index]
                    runner_index += 1
                elif "serve" in cmdline_str or name.lower() == "ollama":
                    # serve process hosts all loaded models
                    model_hint = ", ".join(api_models) if api_models else ""
                else:
                    # Fallback: --model flag or .gguf filename
                    for i, part in enumerate(cmdline):
                        if part in ("--model", "-m") and i + 1 < len(cmdline):
                            model_hint = cmdline[i + 1]
                        elif part.endswith(".gguf"):
                            model_hint = os.path.basename(part)

                mem_info = proc.info["memory_info"]
                procs.append(OllamaProcess(
                    pid=proc.info["pid"], name=name,
                    cpu_percent=proc.info["cpu_percent"] or 0.0,
                    mem_rss_gb=mem_info.rss / 1e9 if mem_info else 0.0,
                    threads=proc.info["num_threads"] or 0,
                    status=proc.info["status"] or "?",
                    model_hint=model_hint,
                ))
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            pass
    return procs


_PYTHON_NAMES = {"python", "python3", "python3.9", "python3.10", "python3.11", "python3.12", "python3.13"}

def _claude_model_from_proc(proc) -> str:
    """
    Resolve the Claude Code model by reading the session JSONL:
      ~/.claude/sessions/{pid}.json  →  sessionId + cwd
      ~/.claude/projects/{escaped_cwd}/{sessionId}.jsonl  →  last message.model
    Falls back to env vars and settings.json.
    """
    try:
        pid = proc.pid
        session_file = os.path.expanduser(f"~/.claude/sessions/{pid}.json")
        session = json.loads(open(session_file).read())
        session_id = session.get("sessionId", "")
        cwd        = session.get("cwd", "")
        if session_id and cwd:
            escaped = cwd.replace("/", "-")
            jsonl   = os.path.expanduser(f"~/.claude/projects/{escaped}/{session_id}.jsonl")
            with open(jsonl) as f:
                for raw in reversed(f.readlines()):
                    try:
                        entry = json.loads(raw)
                        model = (entry.get("message") or {}).get("model")
                        if model:
                            return model
                    except Exception:
                        continue
    except Exception:
        pass
    # Fallbacks
    try:
        env = proc.environ()
        for key in ("ANTHROPIC_MODEL", "CLAUDE_MODEL", "CLAUDE_CODE_MODEL"):
            if key in env:
                return env[key]
    except Exception:
        pass
    try:
        cfg = json.loads(open(os.path.expanduser("~/.claude/settings.json")).read())
        if cfg.get("model"):
            return cfg["model"]
    except Exception:
        pass
    return ""

def _aider_model_from_proc(proc) -> str:
    """Try to resolve the Aider model from cmdline args or ~/.aider.conf.yml."""
    try:
        cmdline = proc.cmdline()
        for i, part in enumerate(cmdline):
            if part in ("--model", "-m") and i + 1 < len(cmdline):
                return cmdline[i + 1]
    except Exception:
        pass
    try:
        import re as _re
        raw = open(os.path.expanduser("~/.aider.conf.yml")).read()
        m = _re.search(r"^model:\s*(.+)$", raw, _re.MULTILINE)
        if m:
            return m.group(1).strip()
    except Exception:
        pass
    return ""

def collect_coding_agent_processes() -> list:
    """Collect Claude Code and Aider processes."""
    procs = []
    for proc in psutil.process_iter(
        ["pid", "name", "cpu_percent", "memory_info", "num_threads", "status", "cmdline"]
    ):
        try:
            name        = proc.info["name"] or ""
            cmdline     = proc.info.get("cmdline") or []
            cmdline_str = " ".join(cmdline)
            name_lower  = name.lower()

            # Claude Code: native 'claude' binary or node wrapper
            is_claude = (
                name_lower in CLAUDE_CODE_PROCESS_NAMES
                or (name_lower in ("node", "node.js") and (
                    "claude-code" in cmdline_str
                    or "@anthropic-ai/claude-code" in cmdline_str
                    or "/claude" in cmdline_str
                ))
            )
            # Aider: python process whose last argument is the aider script
            is_aider = (
                name_lower == "aider"
                or (name_lower in _PYTHON_NAMES
                    and cmdline
                    and os.path.basename(cmdline[-1]) == "aider")
            )

            if not (is_claude or is_aider):
                continue

            tool     = "Claude" if is_claude else "Aider"
            mem_info = proc.info["memory_info"]
            try:    cwd = proc.cwd()
            except: cwd = ""
            model = _claude_model_from_proc(proc) if is_claude else _aider_model_from_proc(proc)
            procs.append(ClaudeCodeProcess(
                pid=proc.info["pid"], name=name,
                cpu_percent=proc.info["cpu_percent"] or 0.0,
                mem_rss_gb=mem_info.rss / 1e9 if mem_info else 0.0,
                threads=proc.info["num_threads"] or 0,
                status=proc.info["status"] or "?",
                cwd=os.path.basename(cwd) if cwd else "",
                tool=tool,
                model=model,
            ))
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            pass
    return procs


def collect_ai_agent_processes() -> list:
    """Collect Codex, Gemini, and Grok CLI processes into a single list."""
    procs = []
    for proc in psutil.process_iter(
        ["pid", "name", "cpu_percent", "memory_info", "status", "cmdline"]
    ):
        try:
            name        = proc.info["name"] or ""
            cmdline     = proc.info.get("cmdline") or []
            cmdline_str = " ".join(cmdline)
            name_lower  = name.lower()
            for tool, (exact_names, hints) in AI_AGENT_SPECS.items():
                matched = (
                    name_lower in exact_names
                    or (name_lower in ("node", "node.js", "python", "python3")
                        and any(h in cmdline_str for h in hints))
                )
                if not matched:
                    continue
                mem_info = proc.info["memory_info"]
                try:    cwd = proc.cwd()
                except: cwd = ""
                procs.append(AIAgentProcess(
                    tool=tool,
                    pid=proc.info["pid"],
                    name=name,
                    cpu_percent=proc.info["cpu_percent"] or 0.0,
                    mem_rss_gb=mem_info.rss / 1e9 if mem_info else 0.0,
                    status=proc.info["status"] or "?",
                    cwd=os.path.basename(cwd) if cwd else "",
                ))
                break   # don't double-match same process
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
        return (
            max((c2.read_bytes  - c1.read_bytes)  / 0.3 / 1e6, 0.0),
            max((c2.write_bytes - c1.write_bytes) / 0.3 / 1e6, 0.0),
        )
    except Exception:
        return 0.0, 0.0


# ── Rendering helpers ─────────────────────────────────────────────────────────

def _bar(percent: float, width: int = 20) -> Text:
    percent = max(0.0, min(100.0, percent))
    filled  = int(percent / 100 * width)
    bar     = "█" * filled + "░" * (width - filled)
    color   = "bold red" if percent >= 85 else "bold yellow" if percent >= 60 else "bold green"
    t = Text()
    t.append(f"[{bar}] ", style=color)
    t.append(f"{percent:5.1f}%", style=color)
    return t

def _temp_color(temp: Optional[float]) -> str:
    if temp is None: return "dim"
    return "bold red" if temp >= 90 else "bold yellow" if temp >= 75 else "bold green"

def _na(reason: str = "") -> str:
    return f"[dim]N/A{(' (' + reason + ')') if reason else ''}[/]"


# ── Panel builders ────────────────────────────────────────────────────────────

def build_cpu_panel(cpu: CPUStats, pm: ThermalPowerMetrics,
                    extra_cpu_temp: Optional[float],
                    linux_pkg_power: Optional[float]) -> Panel:
    t = Table(show_header=False, box=None, padding=(0, 1))
    t.add_column("Label", style="bold cyan", width=22)
    t.add_column("Value")

    t.add_row("Overall CPU", _bar(cpu.overall_percent))

    if cpu.load_avg_1 or cpu.load_avg_5 or cpu.load_avg_15:
        t.add_row("Load Avg (1/5/15)",
                  f"[cyan]{cpu.load_avg_1:.2f}[/]  [yellow]{cpu.load_avg_5:.2f}[/]  [dim]{cpu.load_avg_15:.2f}[/]")

    if cpu.p_core_count:
        t.add_row("P-Cores / E-Cores",
                  f"[green]{cpu.p_core_count}[/] / [yellow]{cpu.e_core_count}[/]")

    freq = pm.cpu_freq_mhz or cpu.freq_current_mhz
    if freq:
        t.add_row("CPU Freq", f"[cyan]{freq:.0f} MHz[/]")

    cpu_power = pm.cpu_power_w or linux_pkg_power
    if cpu_power:
        label = "CPU Power" if pm.cpu_power_w else "Package Power"
        t.add_row(label, f"[yellow]{cpu_power:.2f} W[/]")

    if pm.cpu_active_pct is not None:
        t.add_row("CPU Active (PM)", _bar(pm.cpu_active_pct))

    cpu_temp = pm.cpu_die_temp or extra_cpu_temp
    if cpu_temp is not None:
        t.add_row("CPU Temp", f"[{_temp_color(cpu_temp)}]{cpu_temp:.1f} °C[/]")

    cores = cpu.percent_per_core[:16]
    if cores:
        core_line = Text()
        for i, pct in enumerate(cores):
            color = "red" if pct >= 85 else "yellow" if pct >= 60 else "green"
            core_line.append(f"C{i:<2}", style="dim")
            core_line.append(f"{pct:4.0f}%  ", style=color)
        t.add_row("Per-Core %", core_line)

    return Panel(t, title="[bold white]CPU", border_style="cyan")


def build_gpu_ane_panel(pm: ThermalPowerMetrics, nvidia: NvidiaStats,
                         extra_gpu_temp: Optional[float]) -> Panel:
    t = Table(show_header=False, box=None, padding=(0, 1))
    t.add_column("Label", style="bold magenta", width=22)
    t.add_column("Value")

    # GPU utilization
    if pm.gpu_active_pct is not None:
        t.add_row("GPU Utilization", _bar(pm.gpu_active_pct))
    elif nvidia.util_pct is not None:
        label = f"GPU Util ({nvidia.gpu_name[:16]})" if nvidia.gpu_name else "GPU Utilization"
        t.add_row(label, _bar(nvidia.util_pct))
    elif IS_MACOS and not IS_ROOT:
        t.add_row("GPU Utilization", _na("needs sudo"))
    else:
        t.add_row("GPU Utilization", _na("no GPU detected"))

    # GPU freq
    if pm.gpu_freq_mhz:
        t.add_row("GPU Freq", f"[magenta]{pm.gpu_freq_mhz:.0f} MHz[/]")

    # GPU power / NVIDIA power
    gpu_power = pm.gpu_power_w or nvidia.power_w
    if gpu_power:
        t.add_row("GPU Power", f"[yellow]{gpu_power:.2f} W[/]")

    # NVIDIA VRAM
    if nvidia.mem_used_gb is not None:
        t.add_row("VRAM Used / Total",
                  f"[magenta]{nvidia.mem_used_gb:.2f} GB[/] / [dim]{nvidia.mem_total_gb:.1f} GB[/]")

    # GPU temp
    gpu_temp = pm.gpu_die_temp or nvidia.temp_c or extra_gpu_temp
    if gpu_temp is not None:
        t.add_row("GPU Temp", f"[{_temp_color(gpu_temp)}]{gpu_temp:.1f} °C[/]")

    t.add_row("", "")

    # ANE (Apple Silicon only)
    if IS_MACOS:
        if pm.ane_power_w is not None:
            t.add_row("ANE Power", f"[blue]{pm.ane_power_w:.2f} W[/]")
        elif not IS_ROOT:
            t.add_row("ANE Power", _na("needs sudo"))

    # Package total
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

    # macOS
    if mem.wired_gb:
        t.add_row("Wired",      f"[yellow]{mem.wired_gb:.2f} GB[/]")
    if mem.compressed_gb:
        t.add_row("Compressed", f"[cyan]{mem.compressed_gb:.2f} GB[/]")

    # Linux
    if mem.cached_gb:
        t.add_row("Cached",  f"[cyan]{mem.cached_gb:.2f} GB[/]")
    if mem.buffers_gb:
        t.add_row("Buffers", f"[dim]{mem.buffers_gb:.2f} GB[/]")

    # Windows
    if mem.win_cached_gb:
        t.add_row("Cached", f"[cyan]{mem.win_cached_gb:.2f} GB[/]")

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
    t.add_column("PID",    width=6)
    t.add_column("CPU%",   width=5)
    t.add_column("RSS",    width=5)
    t.add_column("Status", width=8)
    t.add_column("Model",  width=28)
    for p in procs:
        cc = "red" if p.cpu_percent > 80 else "yellow" if p.cpu_percent > 40 else "green"
        t.add_row(str(p.pid),
                  f"[{cc}]{p.cpu_percent:.1f}[/]",
                  f"[cyan]{p.mem_rss_gb:.2f}[/]",
                  p.status,
                  f"[dim]{p.model_hint or '—'}[/]")
    return Panel(t, title="[bold white]Ollama Processes", border_style="yellow")


_CODING_AGENT_COLORS = {"Claude": "green", "Aider": "yellow"}

def build_coding_agents_panel(procs: list) -> Panel:
    if not procs:
        return Panel(
            "[dim]No Claude / Aider processes detected.[/]",
            title="[bold white]Coding Agents", border_style="green",
        )
    t = Table(show_header=True, box=box.SIMPLE, header_style="bold green")
    t.add_column("Tool",   width=7)
    t.add_column("CPU%",   width=5)
    t.add_column("RSS",    width=5)
    t.add_column("Status", width=8)
    t.add_column("Model",  width=24)
    t.add_column("Dir",    width=16)
    for p in procs:
        color  = _CODING_AGENT_COLORS.get(p.tool, "white")
        cc     = "red" if p.cpu_percent > 80 else "yellow" if p.cpu_percent > 40 else "green"
        t.add_row(
            f"[bold {color}]{p.tool}[/]",
            f"[{cc}]{p.cpu_percent:.1f}[/]",
            f"[cyan]{p.mem_rss_gb:.2f}[/]",
            p.status,
            f"[cyan]{p.model or '—'}[/]",
            f"[dim]{p.cwd or '—'}[/]",
        )
    n = len(procs)
    return Panel(t, title=f"[bold white]Coding Agents ({n})", border_style="green")


_AGENT_COLORS = {"Codex": "cyan", "Gemini": "blue", "Grok": "magenta"}

def build_other_agents_panel(procs: list) -> Panel:
    if not procs:
        idle = Text("  Codex  ·  Gemini  ·  Grok  — no processes detected", style="dim")
        return Panel(idle, title="[bold white]Other AI Agents", border_style="dim")

    t = Table(show_header=True, box=box.SIMPLE, header_style="bold white")
    t.add_column("Tool",    width=8)
    t.add_column("PID",     width=7)
    t.add_column("CPU%",    width=6)
    t.add_column("RSS GB",  width=7)
    t.add_column("Status",  width=10)
    t.add_column("Working Dir", width=28)
    for p in procs:
        color   = _AGENT_COLORS.get(p.tool, "white")
        cpu_col = "red" if p.cpu_percent > 80 else "yellow" if p.cpu_percent > 40 else "green"
        t.add_row(
            f"[bold {color}]{p.tool}[/]",
            str(p.pid),
            f"[{cpu_col}]{p.cpu_percent:.1f}[/]",
            f"[cyan]{p.mem_rss_gb:.2f}[/]",
            p.status,
            f"[dim]{p.cwd or '—'}[/]",
        )
    tools_running = ", ".join(dict.fromkeys(p.tool for p in procs))
    return Panel(t, title=f"[bold white]Other AI Agents  [{tools_running}]",
                 border_style="white")


def build_system_panel(cpu: CPUStats) -> Panel:
    t = Table(show_header=False, box=None, padding=(0, 1))
    t.add_column("Label", style="bold white", width=22)
    t.add_column("Value")
    read_mbs, write_mbs = get_disk_io()
    t.add_row("Disk Read",  f"[green]{read_mbs:.2f} MB/s[/]")
    t.add_row("Disk Write", f"[yellow]{write_mbs:.2f} MB/s[/]")
    try:
        t.add_row("Processes", f"[dim]{len(psutil.pids())}[/]")
    except Exception:
        pass
    try:
        uptime_s = time.time() - psutil.boot_time()
        h, rem   = divmod(int(uptime_s), 3600)
        m, s     = divmod(rem, 60)
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
                cpu    = collect_cpu()
                mem    = collect_memory()
                pm     = collect_powermetrics()
                nvidia = collect_nvidia_stats()
                linux_pkg_power    = collect_linux_rapl_power()
                extra_cpu_temp, extra_gpu_temp = collect_temps()
                ollama_procs  = collect_ollama_processes() if show_ollama else []
                coding_procs  = collect_coding_agent_processes() if show_claude else []
                agent_procs   = collect_ai_agent_processes()

                # Adaptive sizing: row1 gets ~45% of usable height, min 12, max 20
                term_h    = console.size.height
                row3_size = 3 if not agent_procs else max(5, 3 + len(agent_procs))
                usable    = term_h - 6 - row3_size   # subtract header+footer+row3
                row1_size = max(12, min(20, int(usable * 0.45)))

                layout = Layout()
                layout.split_column(
                    Layout(Panel(header, border_style="dark_blue"), size=3),
                    Layout(name="row1", size=row1_size),
                    Layout(name="row2"),
                    Layout(name="row3", size=row3_size),
                    Layout(Panel(footer, border_style="dim"), size=3),
                )
                layout["row1"].split_row(
                    Layout(build_cpu_panel(cpu, pm, extra_cpu_temp, linux_pkg_power)),
                    Layout(build_gpu_ane_panel(pm, nvidia, extra_gpu_temp)),
                    Layout(build_memory_panel(mem)),
                )
                layout["row2"].split_row(
                    Layout(name="row2_left", ratio=3),
                    Layout(build_system_panel(cpu), ratio=1),
                )
                layout["row2_left"].split_column(
                    Layout(build_ollama_panel(ollama_procs)),
                    Layout(build_coding_agents_panel(coding_procs)),
                )
                layout["row3"].update(build_other_agents_panel(agent_procs))
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
        console.print("[yellow]⚠  Not running as Administrator — temperature metrics may be limited.[/yellow]\n")
        time.sleep(1.0)

    build_dashboard(
        interval=args.interval,
        show_ollama=not args.no_ollama,
        show_claude=not args.no_claude,
    )
    console.print("\n[dim]Monitor stopped.[/dim]")


if __name__ == "__main__":
    main()
