# llm-monitor

A real-time terminal dashboard for monitoring system resources while running LLMs locally. Built for developers and AI engineers who run models locally and want to see exactly what their hardware is doing.

Works on **macOS** (Apple Silicon + Intel), **Linux**, and **Windows**.

![Platform](https://img.shields.io/badge/platform-macOS%20%7C%20Linux%20%7C%20Windows-blue)
![Python](https://img.shields.io/badge/python-3.8%2B-blue)

## Features

**System metrics**
- CPU utilization per core, frequency, load average, P/E core split (Apple Silicon)
- GPU utilization, frequency, power draw — macOS via `powermetrics` (sudo), Linux/Windows via `nvidia-smi`
- Apple Neural Engine (ANE) power usage (Apple Silicon, sudo)
- Package/CPU/GPU power — macOS via `powermetrics`, Linux via Intel RAPL / AMD energy
- CPU and GPU temperatures — macOS (sudo), Linux (`psutil` sensors), Windows (`wmic`)
- Unified memory: used, available, wired, compressed (macOS), cached/buffers (Linux), cached (Windows)
- Swap usage with high-swap warning
- Disk I/O (read/write MB/s), process count, uptime

**AI tool tracking**
- **Ollama** — live process stats + active model name resolved from the Ollama API (`/api/ps`). Ollama queues concurrent requests and runs one model at a time per runner process; the monitor shows whichever model is currently loaded
- **Coding Agents** (Claude Code + Aider) — CPU, RSS, status, working directory, and detected model name
  - Claude Code: model resolved by reading `~/.claude/projects/{cwd}/{sessionId}.jsonl`
  - Aider: model resolved from `~/.aider.conf.yml` or `--model` CLI flag
- **Other AI CLIs** (Codex, Gemini, Grok) — compact idle strip that expands automatically when any process is detected

**Layout**
- Adaptive height: row sizes scale with your terminal height
- macOS: auto re-launches with `sudo` for full GPU/ANE/thermal/power metrics
- `powermetrics` text output parsed as fallback when JSON format is unavailable (macOS 15+)

## Install

```bash
git clone https://github.com/YOUR_USERNAME/llm-monitor.git
cd llm-monitor
python3 -m venv .venv
source .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -r requirements.txt
chmod +x monitor
```

## Usage

```bash
./monitor                   # launches with sudo automatically on macOS
./monitor --interval 2      # refresh every 2 seconds (default: 3)
./monitor --no-ollama       # skip Ollama process scanning
./monitor --no-claude       # skip Claude Code / Aider scanning
```

On **Windows**, run from an Administrator terminal for full metrics.

## Requirements

- Python 3.8+
- `psutil >= 5.9` and `rich >= 13.0`
- macOS: `powermetrics` built-in (sudo required for GPU/power/thermal)
- Linux: temperature sensors via `/sys`, RAPL at `/sys/class/powercap/` (optional)
- Windows: Administrator for temperature metrics (optional); `nvidia-smi` for GPU (optional)

## Platform notes

| Feature | macOS (AS) | macOS (Intel) | Linux | Windows |
|---------|-----------|--------------|-------|---------|
| CPU util / freq | ✓ | ✓ | ✓ | ✓ |
| Load average | ✓ | ✓ | ✓ | ✓ simulated |
| P/E core split | ✓ | — | — | — |
| GPU utilization | sudo | sudo | ✓ nvidia-smi | ✓ nvidia-smi |
| NVIDIA VRAM + power | — | — | ✓ nvidia-smi | ✓ nvidia-smi |
| ANE power | sudo (AS) | — | — | — |
| Package power | sudo | sudo | ✓ RAPL | — |
| CPU temp | sudo | sudo | ✓ psutil | ✓ wmic |
| GPU temp | sudo | sudo | ✓ psutil / nvidia-smi | ✓ nvidia-smi |
| Wired / compressed RAM | ✓ | ✓ | — | — |
| Cached / buffered RAM | — | — | ✓ /proc/meminfo | ✓ psutil |

> **Package power on Windows** requires vendor-specific kernel drivers (Intel XTU, AMD uProf) — not supported.

> **Ollama concurrency**: Ollama serialises requests and keeps one model loaded per runner process. If you switch models between requests, the monitor will reflect whichever is currently active.
