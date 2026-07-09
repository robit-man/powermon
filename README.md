# PowerMon

Terminal UI power monitor for Linux — tracks CPU, DRAM, GPU, and UPS power consumption with live metrics, sparkline graphs, and cost projections.

## Quick Start

```bash
./power.py           # interactive TUI (auto-creates venv, installs deps)
./power.py --daemon  # background data-collection service
```

First run auto-bootstraps a Python venv and installs all dependencies (nvidia-ml-py, textual, psutil, httpx, pillow, pystray).

## Usage

| Command | Description |
|---|---|
| `./power.py` | Interactive terminal UI |
| `./power.py --daemon` | Background data-collection daemon |
| `./power.py --indicator` | System-tray monthly-cost icon |
| `./power.py --fetch-rate` | Discover local electricity rate |
| `./power.py --status` | Show daemon status + data stats |
| `./power.py --install-service` | Install systemd service |
| `./power.py --uninstall-service` | Remove systemd service |


## Features

- **Sensors**: RAPL (CPU/package), NVML (NVIDIA GPU), DRAM estimation, APC UPS
- **Rate discovery**: Built-in US state/city electricity rate table (~250 entries) + Omnius agent for locations not in the table
- **UI**: Textual-based TUI with live sparkline, cost panel, UPS status, rate source
- **Daemon**: systemd service logging JSONL data to `/var/log/power-monitor/`
- **Tray**: System-tray icon via pystray showing projected monthly cost
- **Self-contained**: Auto-creates venv, auto-installs Omnius npm package and spawns daemon if needed

## Configuration

```bash
export POWER_RATE=0.1239   # Override electricity rate ($/kWh)
export OMNIUS_ENDPOINT=http://localhost:11435  # Omnius API endpoint
```

## Dependencies

- Python 3.12+ (venv auto-bootstrapped)
- Node.js + npm (for Omnius agent, auto-installed if missing)
- `apcupsd` (optional, for APC UPS monitoring)
- `nvidia-ml-py` / `pynvml` (auto-detected for GPU power)

## Architecture

```
power.py
├── Omnius Bootstrap    — Install npm package, spawn daemon, mint API key
├── Built-in Rate Table — US state/city electricity rates (EIA data)
├── Sensor Layer        — RAPL, NVML, DRAM estimation, APC UPS
├── Data Store          — Ring buffer (5 min) + JSONL persistence
├── Daemon Mode         — Continuous logging to /var/log/power-monitor/
├── TUI (Textual)       — Live dashboard with sparkline + costs
└── Tray Indicator      — pystray icon with monthly cost projection
```
