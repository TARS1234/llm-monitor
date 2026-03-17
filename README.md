# llm-monitor

A real-time terminal dashboard for monitoring system resources while running LLMs locally. Tracks CPU, GPU, Neural Engine, RAM, swap, thermals, power draw, Ollama processes, and Claude Code sessions.

Works on **macOS** (Apple Silicon + Intel), **Linux**, and **Windows**.

![Platform](https://img.shields.io/badge/platform-macOS%20%7C%20Linux%20%7C%20Windows-blue)
![Python](https://img.shields.io/badge/python-3.8%2B-blue)

## Features

- CPU utilization per core, frequency, load average, thermals
- GPU utilization, frequency, power draw (macOS with sudo)
- Apple Neural Engine (ANE) power usage (Apple Silicon with sudo)
- Unified memory pressure, wired, compressed, swap
- Live Ollama process tracking with model hints
- Live Claude Code session tracking with working directory
- Disk I/O, process count, uptime
- Linux temperature sensors via psutil
- No configuration required

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
./monitor                        # basic
./monitor --interval 2           # refresh every 2 seconds
./monitor --no-ollama            # skip Ollama scanning
./monitor --no-claude            # skip Claude Code scanning
sudo ./monitor                   # macOS: enables GPU/ANE/thermal/power metrics
```

On **Windows**, run from an Administrator terminal for full metrics.

## Requirements

- Python 3.8+
- `psutil` and `rich` (see `requirements.txt`)
- macOS: `powermetrics` is built-in (sudo required for GPU/power/thermal)
- Linux: temperature sensors exposed via `/sys` (optional)
- Windows: Administrator for some metrics (optional)

## Platform notes

| Feature | macOS (AS) | macOS (Intel) | Linux | Windows |
|---------|-----------|--------------|-------|---------|
| CPU util / freq | ✓ | ✓ | ✓ | ✓ |
| Load average | ✓ | ✓ | ✓ | — |
| P/E core split | ✓ | — | — | — |
| GPU utilization | sudo | sudo | — | — |
| ANE power | sudo (AS) | — | — | — |
| Package power | sudo | sudo | — | — |
| CPU/GPU temp | sudo | sudo | ✓ | — |
| Wired/compressed RAM | ✓ | ✓ | — | — |
