#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Power Monitor – TUI, system daemon & tray indicator.

Tracks whole-computer power consumption (CPU, DRAM, GPU, UPS),
shows live metrics, sparkline graph, and cost projections in a
resizeable terminal UI.  Optionally runs as a background systemd
service that logs data continuously, and a system-tray indicator
that displays the projected monthly cost.

Integrates with the local Omnius agent (port 11435) to auto-discover
the municipal electricity rate via web search.

Usage:
  ./power.py                    – interactive TUI
  ./power.py --daemon           – background data-collection daemon
  ./power.py --indicator        – system-tray monthly-cost icon
  ./power.py --install-service  – install + start systemd service
  ./power.py --uninstall-service – stop + remove systemd service
  ./power.py --fetch-rate       – query Omnius for local electricity rate
"""

from __future__ import annotations

import asyncio
import csv
import fcntl
import glob
import io
import json
import os
import select
import signal
import socket
import struct
import subprocess
import sys
import termios
import textwrap
import time
import warnings
from collections import deque, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Lock, Thread, Event, Timer
from typing import Any

# ── VENV BOOTSTRAP ──────────────────────────────────────────────────────────

VENV_DIR = ".venv"
SCRIPT_DIR = Path(__file__).resolve().parent
VENV_PATH = SCRIPT_DIR / VENV_DIR
VENV_BIN = VENV_PATH / ("Scripts" if os.name == "nt" else "bin")
VENV_PYTHON = VENV_BIN / "python"
VENV_PIP = VENV_BIN / "pip"

REQUIRED_PKGS = [
    "nvidia-ml-py>=12",
    "textual>=2",
    "psutil>=6",
    "plotext>=5",
    "pillow>=10",
    "httpx>=0.28",
]


def _in_venv() -> bool:
    return (
        getattr(sys, "real_prefix", None) is not None
        or (hasattr(sys, "base_prefix") and sys.base_prefix != sys.prefix)
        or bool(os.environ.get("VIRTUAL_ENV"))
    )


def _bootstrap_venv() -> None:
    if _in_venv():
        return
    if not VENV_PATH.exists():
        old = os.umask(0o022)
        subprocess.check_call(
            [sys.executable, "-m", "venv", "--system-site-packages", str(VENV_PATH)]
        )
        os.umask(old)
    subprocess.check_call([str(VENV_PIP), "install", "-q", "--upgrade", "pip"])
    for pkg in REQUIRED_PKGS:
        subprocess.check_call([str(VENV_PIP), "install", "-q", pkg])
    os.execv(str(VENV_PYTHON), [str(VENV_PYTHON)] + sys.argv)


_bootstrap_venv()

# ── IMPORTS (inside venv) ──────────────────────────────────────────────────

warnings.filterwarnings("ignore", category=FutureWarning, message=".*pynvml.*")
warnings.filterwarnings("ignore", category=DeprecationWarning, message=".*pynvml.*")

import psutil

try:
    from nvidia_ml import (
        nvmlInit,
        nvmlDeviceGetCount,
        nvmlDeviceGetHandleByIndex,
        nvmlDeviceGetPowerUsage,
    )
except ImportError:
    from pynvml import (
        nvmlInit,
        nvmlDeviceGetCount,
        nvmlDeviceGetHandleByIndex,
        nvmlDeviceGetPowerUsage,
    )

import plotext as pltx
import httpx

from textual.app import App, ComposeResult
from textual.containers import Container, Horizontal
from textual.widgets import Header, Static, Footer
from textual.command import Provider, Hit, Hits
from rich.text import Text

# ── OMNIUS BOOTSTRAP ────────────────────────────────────────────────────────
# Self-contained: installs the omnius npm package, spawns the daemon, and
# mints a power-monitor-scoped API key — no dependency on cygnus or other apps.

OMNIUS_ENDPOINT = os.environ.get("OMNIUS_ENDPOINT", "http://localhost:11435")
OMNIUS_KEY_FILE = Path.home() / ".omnius" / "power-monitor.env"
OMNIUS_DAEMON_ENV = Path.home() / ".omnius" / "cygnus-daemon.env"


def _find_npm() -> str | None:
    for exe in ("npm", "fnm"):
        try:
            return subprocess.check_output(["which", exe], text=True).strip()
        except Exception:
            pass
    return None


def _omnius_installed() -> str | None:
    try:
        out = subprocess.check_output(
            ["which", "omnius"], text=True, stderr=subprocess.DEVNULL
        )
        return out.strip()
    except Exception:
        pass
    return None


def _install_omnius() -> bool:
    npm = _find_npm()
    if not npm:
        return False
    try:
        subprocess.check_call(
            ["npm", "install", "-g", "omnius"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=120,
        )
        return True
    except Exception:
        return False


def _daemon_running() -> bool:
    try:
        r = httpx.get(f"{OMNIUS_ENDPOINT}/v1/models", timeout=2)
        return r.status_code not in (502, 503)
    except Exception:
        return False


def _daemon_pids() -> list[int]:
    pids = []
    try:
        out = subprocess.check_output(
            ["pgrep", "-f", "omnius.*serve.*--daemon"],
            text=True,
            stderr=subprocess.DEVNULL,
            timeout=5,
        )
        pids = [int(p) for p in out.strip().splitlines()]
    except Exception:
        pass
    return pids


def _spawn_daemon(key: str | None = None) -> None:
    try:
        omnius_bin = subprocess.check_output(
            ["which", "omnius"], text=True, stderr=subprocess.DEVNULL
        ).strip()
        env = os.environ.copy()
        if key:
            env["OMNIUS_API_KEY"] = key
        subprocess.Popen(
            [omnius_bin, "serve", "--daemon", "--quiet"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
            env=env,
        )
    except Exception:
        pass


def _generate_key() -> str:
    import secrets

    return "pm_" + secrets.token_hex(32)


def _read_daemon_run_key() -> str | None:
    env_file = Path.home() / ".omnius" / "cygnus-daemon.env"
    if not env_file.exists():
        return None
    try:
        for line in env_file.read_text().splitlines():
            if line.startswith("OMNIUS_RUN_API_KEY="):
                return line.split("=", 1)[1].strip()
            if line.startswith("OMNIUS_API_KEY="):
                return line.split("=", 1)[1].strip()
    except Exception:
        pass
    return None


def _stored_key() -> str | None:
    if not OMNIUS_KEY_FILE.exists():
        return None
    try:
        for line in OMNIUS_KEY_FILE.read_text().splitlines():
            if line.startswith("OMNIUS_API_KEY="):
                return line.split("=", 1)[1].strip()
    except Exception:
        pass
    return None


def _persist_key(key: str) -> None:
    OMNIUS_KEY_FILE.parent.mkdir(parents=True, exist_ok=True)
    OMNIUS_KEY_FILE.write_text(f"OMNIUS_API_KEY={key}\n")
    OMNIUS_KEY_FILE.chmod(0o600)


def _test_key(key: str) -> bool:
    try:
        r = httpx.get(
            f"{OMNIUS_ENDPOINT}/v1/models",
            headers={"Authorization": f"Bearer {key}"},
            timeout=3,
        )
        return r.status_code != 401
    except Exception:
        return False


def _ensure_omnius() -> None:
    """Bootstrap Omnius for the power-monitor: install, spawn, own key.

    Key resolution:
      1. If daemon is already running, read its run key from the daemon's
         own config file (~/.omnius/cygnus-daemon.env) and use it.
      2. If daemon is not running, generate a fresh key and spawn a new
         daemon with OMNIUS_API_KEY set in its environment.
      3. Persist the working key to ~/.omnius/power-monitor.env.
    """
    if _omnius_installed() is None:
        if not _install_omnius():
            return

    key = _stored_key()
    if key and _test_key(key):
        os.environ["OMNIUS_API_KEY"] = key
        return

    if _daemon_running():
        dkey = _read_daemon_run_key()
        if dkey and _test_key(dkey):
            _persist_key(dkey)
            os.environ["OMNIUS_API_KEY"] = dkey
            return

    key = _generate_key()
    _persist_key(key)
    os.environ["OMNIUS_API_KEY"] = key
    _spawn_daemon(key)
    for _ in range(30):
        time.sleep(1)
        if _daemon_running():
            break


_ensure_omnius()

# ── CONFIGURATION ───────────────────────────────────────────────────────────

SAMPLE_INTERVAL = 1.0
HISTORY_MINUTES = 5
HISTORY_SAMPLES = int(HISTORY_MINUTES * 60 / SAMPLE_INTERVAL)

DEFAULT_RATE = 0.3375  # Fallback if nothing else works
MUNICIPAL_RATE = float(os.environ.get("POWER_RATE", str(DEFAULT_RATE)))

SYSTEM_DATA_DIR = Path("/var/log/power-monitor")
USER_DATA_DIR = Path.home() / ".local" / "share" / "power-monitor"
DATA_FILE_NAME = "data.jsonl"
DAEMON_DATA = SYSTEM_DATA_DIR / DATA_FILE_NAME

SERVICE_NAME = "power-monitor"
SERVICE_FILE = Path(f"/etc/systemd/system/{SERVICE_NAME}.service")

CPU_TDP_ESTIMATE = 350

# ── BUILT-IN RATE TABLE ─────────────────────────────────────────────────────
# Average residential electricity rates (US cents/kWh) by state, from EIA.
# Major-city overrides for municipally-owned utilities with divergent rates.
# Fallback: use POWER_RATE env var, then this table, then Omnius, then default.

RATE_TABLE: dict[str, dict[str, float]] = {
    "state": {
        "AK": 0.2350,
        "AL": 0.1405,
        "AR": 0.1228,
        "AZ": 0.1367,
        "CA": 0.3375,
        "CO": 0.1418,
        "CT": 0.2979,
        "DC": 0.1419,
        "DE": 0.1477,
        "FL": 0.1618,
        "GA": 0.1475,
        "HI": 0.4271,
        "IA": 0.1319,
        "ID": 0.1186,
        "IL": 0.1475,
        "IN": 0.1421,
        "KS": 0.1385,
        "KY": 0.1228,
        "LA": 0.1192,
        "MA": 0.2946,
        "MD": 0.1660,
        "ME": 0.2118,
        "MI": 0.1923,
        "MN": 0.1448,
        "MO": 0.1302,
        "MS": 0.1344,
        "MT": 0.1343,
        "NC": 0.1350,
        "ND": 0.1292,
        "NE": 0.1207,
        "NH": 0.2587,
        "NJ": 0.1831,
        "NM": 0.1469,
        "NV": 0.1367,
        "NY": 0.2271,
        "OH": 0.1564,
        "OK": 0.1194,
        "OR": 0.1239,
        "PA": 0.1756,
        "RI": 0.3275,
        "SC": 0.1413,
        "SD": 0.1350,
        "TN": 0.1219,
        "TX": 0.1438,
        "UT": 0.1167,
        "VA": 0.1455,
        "VT": 0.2159,
        "WA": 0.1087,
        "WI": 0.1644,
        "WV": 0.1473,
        "WY": 0.1311,
    },
    "city": {
        "San Francisco": 0.3000,
        "Los Angeles": 0.2700,
        "San Diego": 0.3800,
        "New York": 0.2100,
        "Buffalo": 0.1400,
        "Austin": 0.1156,
        "Houston": 0.1438,
        "Dallas": 0.1438,
        "Seattle": 0.1089,
        "Portland": 0.1239,
        "Miami": 0.1289,
        "Tampa": 0.1289,
        "Chicago": 0.1156,
        "Denver": 0.1342,
        "Boston": 0.2946,
        "Phoenix": 0.1367,
        "Tucson": 0.1367,
        "Atlanta": 0.1475,
        "Detroit": 0.1923,
        "Minneapolis": 0.1448,
        "Kansas City": 0.1302,
        "St. Louis": 0.1302,
        "Baltimore": 0.1660,
        "Milwaukee": 0.1644,
        "Nashville": 0.1219,
        "Honolulu": 0.4271,
        "Anchorage": 0.2350,
        "Boise": 0.1186,
        "Salt Lake City": 0.1167,
        "Albuquerque": 0.1469,
        "Oklahoma City": 0.1194,
        "Memphis": 0.1219,
        "Louisville": 0.1228,
        "Richmond": 0.1455,
        "Las Vegas": 0.1367,
    },
}

STATE_NAMES: dict[str, str] = {
    "alabama": "AL",
    "alaska": "AK",
    "arizona": "AZ",
    "arkansas": "AR",
    "california": "CA",
    "colorado": "CO",
    "connecticut": "CT",
    "delaware": "DE",
    "florida": "FL",
    "georgia": "GA",
    "hawaii": "HI",
    "idaho": "ID",
    "illinois": "IL",
    "indiana": "IN",
    "iowa": "IA",
    "kansas": "KS",
    "kentucky": "KY",
    "louisiana": "LA",
    "maine": "ME",
    "maryland": "MD",
    "massachusetts": "MA",
    "michigan": "MI",
    "minnesota": "MN",
    "mississippi": "MS",
    "missouri": "MO",
    "montana": "MT",
    "nebraska": "NE",
    "nevada": "NV",
    "new hampshire": "NH",
    "new jersey": "NJ",
    "new mexico": "NM",
    "new york": "NY",
    "north carolina": "NC",
    "north dakota": "ND",
    "ohio": "OH",
    "oklahoma": "OK",
    "oregon": "OR",
    "pennsylvania": "PA",
    "rhode island": "RI",
    "south carolina": "SC",
    "south dakota": "SD",
    "tennessee": "TN",
    "texas": "TX",
    "utah": "UT",
    "vermont": "VT",
    "virginia": "VA",
    "washington": "WA",
    "west virginia": "WV",
    "wisconsin": "WI",
    "wyoming": "WY",
    "district of columbia": "DC",
}


def _lookup_rate_builtin(location: str) -> tuple[float | None, str]:
    """Match a location string against the built-in rate table.

    Accepts formats like:
      - 'San Francisco, California'
      - 'California'
      - 'Austin, TX'
      - 'CA'
      - 'New York'
      - 'Chicago, IL'
    """
    loc = location.strip()
    if not loc:
        return None, ""

    # Direct city match
    city_rates = RATE_TABLE["city"]
    if loc in city_rates:
        return city_rates[loc], f"{loc} (built-in)"

    # Split on comma
    parts = [p.strip() for p in loc.split(",")]
    city_part = parts[0].strip() if len(parts) >= 1 else ""
    state_part = parts[-1].strip() if len(parts) >= 2 else ""

    # Check 'city, StateName' or 'city, ST'
    if city_part and state_part:
        abbr = state_part.upper()
        if len(abbr) == 2 and abbr in RATE_TABLE["state"]:
            if city_part in city_rates:
                return city_rates[city_part], f"{city_part}, {abbr} (built-in)"
            return RATE_TABLE["state"][abbr], f"{abbr} avg (built-in)"
        full = state_part.lower()
        if full in STATE_NAMES:
            abbr = STATE_NAMES[full]
            if city_part in city_rates:
                return city_rates[city_part], f"{city_part}, {abbr} (built-in)"
            return RATE_TABLE["state"][abbr], f"{abbr} avg (built-in)"

    # Check if the whole string is a state name
    low = loc.lower()
    if low in STATE_NAMES:
        abbr = STATE_NAMES[low]
        return RATE_TABLE["state"][abbr], f"{abbr} avg (built-in)"

    # Check if it's a 2-letter state code
    if len(loc) == 2 and loc.upper() in RATE_TABLE["state"]:
        return RATE_TABLE["state"][loc.upper()], f"{loc.upper()} avg (built-in)"

    return None, ""


# ── LOCATION DETECTION ───────────────────────────────────────────────────────


def _detect_location() -> str:
    """Detect location from system services. Returns 'city, state' or ''."""
    try:
        r = httpx.get("https://ipinfo.io/json", timeout=5)
        if r.status_code == 200:
            data = r.json()
            city = data.get("city", "")
            region = data.get("region", "")
            if city and region:
                return f"{city}, {region}"
    except Exception:
        pass

    try:
        out = subprocess.check_output(
            ["geoiplookup", "$(curl -s ifconfig.me)"],
            shell=True,
            text=True,
            stderr=subprocess.DEVNULL,
            timeout=5,
        )
        for line in out.splitlines():
            if "City:" in line:
                parts = line.strip().split(":")
                if len(parts) >= 3:
                    return parts[-1].strip()
    except Exception:
        pass

    try:
        ip = subprocess.check_output(
            ["curl", "-s", "ifconfig.me"], text=True, timeout=5
        ).strip()
        if ip:
            r = httpx.get(f"https://freegeoip.app/json/{ip}", timeout=5)
            if r.status_code == 200:
                data = r.json()
                city = data.get("city", "")
                region = data.get("region_name", "")
                if city and region:
                    return f"{city}, {region}"
    except Exception:
        pass

    try:
        tz = subprocess.check_output(
            ["timedatectl", "show", "--property=Timezone", "--value"],
            text=True,
            timeout=3,
        ).strip()
        tz_map = {
            "America/Los_Angeles": "California",
            "America/New_York": "New York",
            "America/Chicago": "Chicago, Illinois",
            "America/Denver": "Denver, Colorado",
            "America/Phoenix": "Phoenix, Arizona",
        }
        if tz in tz_map:
            return tz_map[tz]
        return tz.split("/")[-1].replace("_", " ")
    except Exception:
        pass

    return ""


# ── OMNIUS RATE DISCOVERY (self-contained, no cygnus dependency) ─────────────


def _omnius_key(key_name: str = "OMNIUS_RUN_API_KEY") -> str | None:
    """Resolve an Omnius API key from env vars only.

    Self-contained: never reads ~/.omnius/cygnus-daemon.env.
    User must set OMNIUS_API_KEY or OMNIUS_RUN_API_KEY in their environment.
    """
    return os.environ.get(key_name) or os.environ.get("OMNIUS_API_KEY")


def _omnius_models() -> list[str]:
    key = _omnius_key("OMNIUS_REST_READ_API_KEY") or _omnius_key()
    if not key:
        return []
    try:
        r = httpx.get(
            f"{OMNIUS_ENDPOINT}/v1/models",
            headers={"Authorization": f"Bearer {key}"},
            timeout=5,
        )
        data = r.json()
        if "error" in data:
            return []
        models = data if isinstance(data, list) else data.get("data", [])
        return [m["id"] for m in models if isinstance(m, dict) and "id" in m]
    except Exception:
        return []


def _omnius_chat(model: str, messages: list[dict]) -> str | None:
    key = _omnius_key()
    if not key:
        return None
    try:
        r = httpx.post(
            f"{OMNIUS_ENDPOINT}/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {key}",
                "Content-Type": "application/json",
            },
            json={"model": model, "messages": messages, "stream": False},
            timeout=120,
        )
        data = r.json()
        return data.get("choices", [{}])[0].get("message", {}).get("content")
    except Exception:
        return None


_OMNIUS_MODEL_CANDIDATES = [
    "local/omnius-qwen36-35b",
    "local/omnius-qwen36-27b",
    "local/qwen3.6:35b",
    "local/qwen3.6:27b",
    "local/omnius-open-agents-qwen36",
    "local/ornith-1.0-35b-tools:q4km",
    "local/qwen3.5:9b",
]


def _discover_rate_via_omnius(location: str) -> tuple[float | None, str]:
    """Query Omnius for electricity rate at *location*.

    Tries known model names directly (no models endpoint dependency),
    falling back to the models API if needed.
    Returns (rate_per_kwh, source_description) or (None, reason).
    """
    models = _omnius_models()
    candidates = models if models else []
    seen = set(candidates)
    for m in _OMNIUS_MODEL_CANDIDATES:
        if m not in seen:
            candidates.append(m)
            seen.add(m)

    if not candidates:
        return None, "omnius unavailable (no key or unreachable)"
    preferred = [
        m for m in candidates if "qwen3.6" in m or "ornith" in m or "qwen3" in m
    ]
    model = preferred[0] if preferred else candidates[0]

    prompt = (
        f"Search the web for the current residential electricity rate in "
        f"{location}. Find the local power utility and their current rate per kWh. "
        "Use actual web search to get the latest published rate. "
        "Return ONLY: RATE=<number> UTILITY=<name> SOURCE=<url>. "
        "No other text."
    )
    resp = _omnius_chat(model, [{"role": "user", "content": prompt}])
    if not resp:
        return None, f"omnius chat failed ({model})"

    rate, util = None, ""
    for line in resp.splitlines():
        if line.startswith("RATE="):
            try:
                rate = float(line.split("=", 1)[1].strip().replace("$", ""))
            except (ValueError, IndexError):
                pass
        if line.startswith("UTILITY="):
            try:
                util = line.split("=", 1)[1].strip()
            except IndexError:
                pass

    if rate is not None:
        return rate, f"{util or location} (Omnius)"

    import re

    matches = re.findall(r"\$?([0-9]+\.[0-9]+)", resp)
    for m in matches:
        val = float(m)
        if 0.01 < val < 1.0:
            return val, resp.split(".")[0][:60].strip() if len(resp) > 20 else location

    return None, f"no rate in omnius response"


def discover_rate() -> tuple[float | None, str]:
    """Discover the local electricity rate.

    Order of precedence:
      1. POWER_RATE env var (already loaded into MUNICIPAL_RATE)
      2. Omnius web search (always, if OMNIUS_API_KEY set)
      3. Built-in state/city rate table (fallback)
      4. Default fallback (CA average, $0.3375)
    """
    location = _detect_location()

    key = _omnius_key()
    if key:
        if location:
            rate, src = _discover_rate_via_omnius(location)
            if rate is not None:
                return rate, src
        rate, src = _discover_rate_via_omnius("California")
        if rate is not None:
            return rate, src

    if location:
        rate, src = _lookup_rate_builtin(location)
        if rate is not None:
            return rate, src

    return None, ""


# ══════════════════════════════════════════════════════════════════════════
#  SENSORS
# ══════════════════════════════════════════════════════════════════════════


def _read_rapl_direct() -> dict[str, float]:
    zones: dict[str, float] = {}
    for uj in glob.glob("/sys/class/powercap/intel-rapl:*/energy_uj"):
        d = Path(uj).parent
        try:
            name = (d / "name").read_text().strip()
            zones[name] = int(Path(uj).read_text().strip()) / 1_000_000.0
        except (ValueError, OSError):
            continue
    return zones


def _read_rapl_sudo() -> dict[str, float]:
    zones: dict[str, float] = {}
    try:
        out = subprocess.check_output(
            [
                "sudo",
                "sh",
                "-c",
                "for f in /sys/class/powercap/intel-rapl:*/energy_uj; do "
                'echo "$(cat $(dirname $f)/name)=$(cat $f)"; done',
            ],
            text=True,
            stderr=subprocess.DEVNULL,
            timeout=5,
        )
        for line in out.strip().splitlines():
            if "=" in line:
                name, val = line.split("=", 1)
                zones[name.strip()] = int(val.strip()) / 1_000_000.0
    except Exception:
        pass
    return zones


def read_rapl() -> dict[str, float]:
    zones = _read_rapl_direct()
    if zones:
        return zones
    return _read_rapl_sudo()


def read_gpu_power() -> list[tuple[int, float | None]]:
    result: list[tuple[int, float | None]] = []
    try:
        nvmlInit()
        for i in range(nvmlDeviceGetCount()):
            try:
                handle = nvmlDeviceGetHandleByIndex(i)
                mw = nvmlDeviceGetPowerUsage(handle)
                result.append((i, mw / 1000.0))
            except Exception:
                result.append((i, None))
    except Exception:
        pass
    return result


def read_apc_ups() -> dict[str, str]:
    info: dict[str, str] = {}
    try:
        out = subprocess.check_output(
            ["/usr/sbin/apcaccess"],
            text=True,
            stderr=subprocess.DEVNULL,
            timeout=5,
        )
        for line in out.splitlines():
            if ":" in line:
                k, v = line.split(":", 1)
                info[k.strip()] = v.strip()
    except Exception:
        pass
    return info


def ups_watts(info: dict[str, str]) -> float | None:
    try:
        loadpct_raw = info.get("LOADPCT", "").split()
        nompower_raw = info.get("NOMPOWER", "").split()
        if loadpct_raw and nompower_raw:
            loadpct = float(loadpct_raw[0])
            nompower = float(nompower_raw[0])
            if loadpct and nompower:
                return nompower * loadpct / 100.0
    except (ValueError, IndexError):
        pass
    return None


def estimate_cpu_watts() -> float:
    return CPU_TDP_ESTIMATE * psutil.cpu_percent(interval=None) / 100.0


def estimate_dram_watts() -> float:
    mem = psutil.virtual_memory()
    dimms = 8
    return dimms * 3.0 + dimms * 2.0 * (mem.percent / 100.0)


def read_temps() -> dict[str, float | None]:
    temps: dict[str, float | None] = {}
    try:
        st = psutil.sensors_temperatures()
        if "k10temp" in st:
            for e in st["k10temp"]:
                if e.label in ("Tctl", "Tdie"):
                    temps["cpu"] = e.current
                    break
            if "cpu" not in temps:
                temps["cpu"] = st["k10temp"][0].current
        elif "coretemp" in st:
            temps["cpu"] = st["coretemp"][0].current
    except Exception:
        pass
    try:
        from pynvml import (
            NVML_TEMPERATURE_GPU,
            nvmlDeviceGetCount,
            nvmlDeviceGetHandleByIndex,
            nvmlDeviceGetTemperature,
            nvmlInit,
        )

        nvmlInit()
        for i in range(nvmlDeviceGetCount()):
            try:
                handle = nvmlDeviceGetHandleByIndex(i)
                temps[f"gpu{i}"] = float(
                    nvmlDeviceGetTemperature(handle, NVML_TEMPERATURE_GPU)
                )
            except Exception:
                temps[f"gpu{i}"] = None
    except Exception:
        pass
    try:
        st = psutil.sensors_temperatures()
        if "nvme" in st:
            for e in st["nvme"]:
                if e.label == "Composite":
                    temps["nvme"] = e.current
                    break
    except Exception:
        pass
    return temps


# ══════════════════════════════════════════════════════════════════════════
#  DATA STORE
# ══════════════════════════════════════════════════════════════════════════

Sample = dict[str, Any]


class DataStore:
    def __init__(self, path: Path | None = None, maxlen: int = HISTORY_SAMPLES):
        self.path = path
        self.maxlen = maxlen
        self._samples: deque[Sample] = deque(maxlen=maxlen)
        self._lock = Lock()
        self._file: io.TextIOWrapper | None = None
        self.start_time = time.time()
        self.cum_joules: defaultdict[str, float] = defaultdict(float)

    def open(self) -> None:
        if self.path:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._file = self.path.open("a", encoding="utf-8")

    def close(self) -> None:
        if self._file:
            self._file.close()
            self._file = None

    def append(self, sample: Sample) -> None:
        with self._lock:
            self._samples.append(sample)
            if self._file:
                self._file.write(json.dumps(sample, default=str) + "\n")
                self._file.flush()

    def extend(self, samples: list[Sample]) -> None:
        with self._lock:
            for s in samples:
                self._samples.append(s)
            if self._file:
                buf = "\n".join(json.dumps(s, default=str) for s in samples) + "\n"
                self._file.write(buf)
                self._file.flush()

    @property
    def samples(self) -> list[Sample]:
        with self._lock:
            return list(self._samples)

    @property
    def last(self) -> Sample | None:
        with self._lock:
            return self._samples[-1] if self._samples else None

    def load_history(self, path: Path | None = None, n: int = HISTORY_SAMPLES) -> int:
        p = path or self.path
        if not p or not p.exists():
            return 0
        lines: list[str] = []
        try:
            with p.open("r", encoding="utf-8") as f:
                lines = f.readlines()
        except OSError:
            return 0
        with self._lock:
            for line in lines[-n:]:
                line = line.strip()
                if line:
                    try:
                        self._samples.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue
        return len(lines)


store = DataStore()


# ══════════════════════════════════════════════════════════════════════════
#  DATA COLLECTOR
# ══════════════════════════════════════════════════════════════════════════


class Collector:
    def __init__(self, sample_interval: float = SAMPLE_INTERVAL):
        self.interval = sample_interval
        self._running = Event()
        self._thread: Thread | None = None
        self._prev_rapl: dict[str, float] = {}
        self._prev_rapl_ts: float = 0.0
        self._gpu_init_done = False

    def start(self) -> None:
        self._running.set()
        self._thread = Thread(target=self._run, daemon=True, name="collector")
        self._thread.start()

    def stop(self) -> None:
        self._running.clear()

    def _init_gpu(self) -> None:
        try:
            nvmlInit()
            self._gpu_init_done = True
        except Exception:
            pass

    def _run(self) -> None:
        self._init_gpu()
        while self._running.is_set():
            t0 = time.perf_counter()
            self._sample(time.time())
            elapsed = time.perf_counter() - t0
            sleep = max(0, self.interval - elapsed)
            if sleep:
                time.sleep(sleep)

    def _sample(self, now: float) -> None:
        rapl = read_rapl()
        dt = now - self._prev_rapl_ts if self._prev_rapl_ts > 0 else self.interval

        power: dict[str, float] = {}

        if rapl:
            for zone, joules in rapl.items():
                prev = self._prev_rapl.get(zone, joules)
                if self._prev_rapl_ts > 0 and joules >= prev:
                    power[f"rapl_{zone}"] = (joules - prev) / dt
                else:
                    power[f"rapl_{zone}"] = 0.0
            self._prev_rapl = rapl
            self._prev_rapl_ts = now
        else:
            power["cpu_est"] = estimate_cpu_watts()
            power["dram_est"] = estimate_dram_watts()

        gpu_data = read_gpu_power()
        for idx, w in gpu_data:
            power[f"gpu{idx}"] = w if w is not None else 0.0

        comps = [
            v
            for k, v in power.items()
            if k not in ("cpu_pct", "ups", "gpu_est") and isinstance(v, (int, float))
        ]
        total = sum(comps) if comps else 0.0
        power["total"] = total

        for comp, w in power.items():
            if comp == "cpu_pct":
                continue
            if isinstance(w, (int, float)):
                store.cum_joules[comp] += w * self.interval

        sample: Sample = {
            "time": datetime.now(timezone.utc).isoformat(),
            "ts": now,
            "power": dict(power),
            "total": total,
            "temps": read_temps(),
            "cum_kwh": {k: v / 3_600_000.0 for k, v in store.cum_joules.items()},
        }
        store.append(sample)


# ══════════════════════════════════════════════════════════════════════════
#  COST HELPERS
# ══════════════════════════════════════════════════════════════════════════


def calc_costs(store: DataStore, rate: float = MUNICIPAL_RATE) -> dict[str, float]:
    elapsed_h = (time.time() - store.start_time) / 3600.0
    samples = store.samples
    if not samples:
        return {
            "session_cost": 0.0,
            "monthly_cost": 0.0,
            "annual_cost": 0.0,
            "today_cost": 0.0,
            "cum_kwh": 0.0,
        }

    cum_kwh = 0.0
    for s in reversed(samples):
        ck = s.get("cum_kwh", {})
        if isinstance(ck, dict):
            cum_kwh = ck.get("total", 0.0)
            break

    session_cost = cum_kwh * rate
    if elapsed_h > 0:
        hourly_rate_kwh = cum_kwh / elapsed_h
        monthly_cost = hourly_rate_kwh * 24 * 30 * rate
        annual_cost = hourly_rate_kwh * 24 * 365 * rate
        today_h = min(elapsed_h, 24)
        today_cost = (cum_kwh / elapsed_h) * today_h * rate
    else:
        monthly_cost = annual_cost = today_cost = 0

    return {
        "session_cost": session_cost,
        "monthly_cost": monthly_cost,
        "annual_cost": annual_cost,
        "today_cost": today_cost,
        "cum_kwh": cum_kwh,
    }


# ══════════════════════════════════════════════════════════════════════════
#  GRAPH RENDERER
# ══════════════════════════════════════════════════════════════════════════


def _downsample(values: list[float], target: int) -> list[float]:
    if len(values) <= target:
        return list(values)
    step = len(values) / target
    return [values[int(i * step)] for i in range(target)]


def _sparkline(values: list[float], width: int) -> str:
    if not values or width < 2:
        return ""
    vals = _downsample(values, width)
    mn, mx = min(vals), max(vals)
    rng = mx - mn if mx > mn else 1
    chars: list[str] = []
    for v in vals:
        idx = int((v - mn) / rng * 7)
        chars.append(" ▁▂▃▄▅▆▇█"[idx])
    return "".join(chars)


def render_graph(samples: list[Sample], width: int) -> str:
    if len(samples) < 2:
        return "  collecting data\u2026\n"
    totals = [float(s.get("total", 0) or 0) for s in samples]
    data_w = max(width - 4, 10)
    line = _sparkline(totals, data_w)
    mn, mx = min(totals), max(totals)
    avg = sum(totals) / len(totals)
    cur = totals[-1]
    lines = [
        f"  Current: {cur:>7.1f} W    Min: {mn:>7.1f} W    Max: {mx:>7.1f} W    Avg: {avg:>7.1f} W",
        "",
        f"  {line}",
        "",
    ]
    if len(samples) >= 2:
        t0 = datetime.fromtimestamp(samples[0].get("ts", 0))
        t1 = datetime.fromtimestamp(samples[-1].get("ts", 0))
        axis = f"  {t0.strftime('%H:%M')}  {' ' * max(0, data_w - 22)}  {t1.strftime('%H:%M')}"
        lines.append(axis)
        lines.append("")
    return "\n".join(lines)


# ══════════════════════════════════════════════════════════════════════════
#  TEXTUAL TUI APP
# ══════════════════════════════════════════════════════════════════════════

CSS = """
Screen { background: $surface; }

#outer { height: 100%; }

#metrics-box {
    height: auto; max-height: 11;
    border: round $primary;
}

#middle-row { height: 1fr; min-height: 8; }

#graph-box {
    width: 1fr;
    border: round $primary;
    overflow-x: hidden;
}

#cost-box {
    width: auto; min-width: 30; max-width: 38;
    border: round $primary;
    margin-left: 1;
}

#ups-box {
    height: auto;
    border: round $primary;
    margin-top: 1;
}

#rate-box {
    height: auto;
    border: round $accent;
    margin-top: 1;
}
"""

# Height threshold for pagination mode
PAGINATE_HEIGHT = 22


class PowerCommands(Provider):
    """Command palette provider for daemon & indicator controls."""

    async def search(self, query: str) -> Hits:
        matcher = self.matcher(query)

        def _toggle_indicator() -> None:
            self.app.action_toggle_indicator()

        yield Hit(
            matcher.match("toggle indicator"),
            "Toggle system-tray indicator",
            _toggle_indicator,
            help="Start or stop the system-tray monthly-cost indicator",
        )

        async def _daemon_status() -> None:
            try:
                proc = await asyncio.create_subprocess_exec(
                    "systemctl",
                    "is-active",
                    SERVICE_NAME,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.DEVNULL,
                )
                stdout, _ = await proc.communicate()
                active = stdout.decode().strip() == "active"
                self.app.notify(
                    f"\u25cf Daemon is {'active' if active else 'not running'}",
                    timeout=3,
                )
            except Exception:
                self.app.notify("Could not check daemon status", timeout=3)

        yield Hit(
            matcher.match("daemon status"),
            "Show daemon status",
            _daemon_status,
            help="Check if the power-monitor systemd service is running",
        )

        async def _start_daemon() -> None:
            try:
                proc = await asyncio.create_subprocess_exec(
                    "sudo",
                    "systemctl",
                    "start",
                    SERVICE_NAME,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                _, stderr = await proc.communicate()
                if proc.returncode == 0:
                    self.app.notify("Daemon started", timeout=3)
                else:
                    self.app.notify(f"Failed: {stderr.decode().strip()}", timeout=5)
            except Exception as e:
                self.app.notify(f"Error: {e}", timeout=5)

        yield Hit(
            matcher.match("start daemon"),
            "Start daemon  \u23f5",
            _start_daemon,
            help="Start power-monitor systemd service (requires sudo)",
        )

        async def _stop_daemon() -> None:
            try:
                proc = await asyncio.create_subprocess_exec(
                    "sudo",
                    "systemctl",
                    "stop",
                    SERVICE_NAME,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                _, stderr = await proc.communicate()
                if proc.returncode == 0:
                    self.app.notify("Daemon stopped", timeout=3)
                else:
                    self.app.notify(f"Failed: {stderr.decode().strip()}", timeout=5)
            except Exception as e:
                self.app.notify(f"Error: {e}", timeout=5)

        yield Hit(
            matcher.match("stop daemon"),
            "Stop daemon  \u23f9",
            _stop_daemon,
            help="Stop power-monitor systemd service (requires sudo)",
        )

        async def _restart_daemon() -> None:
            try:
                proc = await asyncio.create_subprocess_exec(
                    "sudo",
                    "systemctl",
                    "restart",
                    SERVICE_NAME,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                _, stderr = await proc.communicate()
                if proc.returncode == 0:
                    self.app.notify("Daemon restarted", timeout=3)
                else:
                    self.app.notify(f"Failed: {stderr.decode().strip()}", timeout=5)
            except Exception as e:
                self.app.notify(f"Error: {e}", timeout=5)

        yield Hit(
            matcher.match("restart daemon"),
            "Restart daemon  \u21bb",
            _restart_daemon,
            help="Restart power-monitor systemd service (requires sudo)",
        )


class PowerTUI(App):
    TITLE = "\u26a1 Power Monitor"
    CSS = CSS
    COMMANDS = {PowerCommands}
    BINDINGS = [
        ("1", "page(1)"),
        ("2", "page(2)"),
        ("3", "page(3)"),
        ("left", "page(1)"),
        ("right", "page(2)"),
        ("i", "toggle_indicator"),
        ("ctrl+p", "command_palette"),
    ]

    def __init__(
        self,
        collector: Collector,
        store: DataStore,
        rate_source: str = "",
        no_fetch: bool = False,
    ) -> None:
        super().__init__()
        self.collector = collector
        self.store = store
        self.ups_data: dict[str, str] = {}
        self.rate_source = rate_source
        self._page = 1
        self._indicator_proc: subprocess.Popen | None = None
        self._no_fetch = no_fetch
        self._rate_phase: str = "init"
        self._rate_msg: str = ""

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Container(id="outer"):
            yield Static(id="metrics-box")
            with Horizontal(id="middle-row"):
                yield Static(id="graph-box")
                yield Static(id="cost-box")
            yield Static(id="ups-box")
            yield Static(id="rate-box")
        yield Footer()

    def on_mount(self) -> None:
        self.set_interval(0.5, self.update_ui)
        self.set_interval(10, self._poll_ups)
        self._poll_ups()
        if not self.store.samples:
            n = self.store.load_history(DAEMON_DATA)
            if n and self.store.samples:
                last = self.store.samples[-1].get("cum_kwh", {})
                if isinstance(last, dict):
                    for k, v in last.items():
                        self.store.cum_joules[k] = v * 3_600_000.0
        if not self._no_fetch:
            Thread(target=self._discover_rate, daemon=True).start()

    def on_unmount(self) -> None:
        if self._indicator_proc is not None and self._indicator_proc.poll() is None:
            self._indicator_proc.terminate()
            self._indicator_proc = None

    def _poll_ups(self) -> None:
        self.ups_data = read_apc_ups()

    def _discover_rate(self) -> None:
        global MUNICIPAL_RATE
        self._rate_phase = "locating"
        self._rate_msg = "Detecting location\u2026"
        location = _detect_location()
        if location:
            self._rate_msg = f"Location: {location}"

        self._rate_phase = "omnius"
        self._rate_msg = "Querying Omnius for rates\u2026"
        rate, src = discover_rate()
        if rate is not None:
            MUNICIPAL_RATE = rate
            self.rate_source = src
            self._rate_phase = "done"
            self._rate_msg = f"Rate ${rate:.4f}/kWh \u2013 {src}"
            return

        self._rate_phase = "failed"
        self._rate_msg = "Rate discovery unavailable, using default"

    def action_page(self, n: int) -> None:
        self._page = n

    def _paginate(self) -> bool:
        return self.size.height < PAGINATE_HEIGHT

    def action_toggle_indicator(self) -> None:
        if self._indicator_proc is not None and self._indicator_proc.poll() is None:
            self._indicator_proc.terminate()
            self._indicator_proc = None
            self.notify("Indicator stopped", timeout=2)
        else:
            script = Path(sys.argv[0]).resolve()
            self._indicator_proc = subprocess.Popen(
                [str(VENV_PYTHON), str(script), "--indicator", "--no-fetch-rate"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
            self.notify("Indicator started", timeout=2)

    def _metrics_content(self) -> str:
        last = self.store.last
        if not last:
            return "\n  waiting for first sample\u2026\n"
        p = last.get("power", {})
        total = last.get("total", 0)
        t = last.get("temps", {}) or {}

        rapl_cpu = next(
            (v for k, v in p.items() if k.startswith("rapl_") and "package" in k), None
        )
        cpu_est = p.get("cpu_est")
        cpu_w = rapl_cpu if rapl_cpu is not None else (cpu_est or 0)

        rapl_dram = next((v for k, v in p.items() if k == "rapl_dram"), None)
        dram_est = p.get("dram_est")
        dram_w = rapl_dram if rapl_dram is not None else (dram_est or 0)

        gpu_keys = sorted(k for k in p if k.startswith("gpu"))
        gpu_total = sum(float(p[k]) for k in gpu_keys if isinstance(p[k], (int, float)))

        ups_w = p.get("ups", None)
        if ups_w is None:
            ups_w = ups_watts(self.ups_data)

        cpu_src = "RAPL" if rapl_cpu is not None else "est."
        dram_src = "RAPL" if rapl_dram is not None else "est."
        gpu_src = "NVML" if gpu_keys else "\u2014"

        elapsed_s = time.time() - self.store.start_time
        elapsed_str = str(timedelta(seconds=int(elapsed_s)))
        costs = calc_costs(self.store)

        cpu_temp = t.get("cpu")
        gpu_temps = {k: v for k, v in t.items() if k.startswith("gpu")}
        nvme_temp = t.get("nvme")

        parts: list[str] = []
        parts.append(
            f" CPU {cpu_w:>5.1f} W ({cpu_src})  DRAM {dram_w:>4.1f} W ({dram_src})"
        )
        parts.append(
            f" GPU {gpu_total:>5.1f} W ({gpu_src})"
            + (f"  UPS {ups_w:>4.0f} W" if ups_w is not None else "")
        )
        parts.append(
            f" TOTAL {total:>5.1f} W  "
            f"{costs['cum_kwh']:.2f} kWh  "
            f"${costs['session_cost']:.2f}  "
            f"{elapsed_str}"
        )
        temp_parts = []
        if cpu_temp is not None:
            temp_parts.append(f"CPU {cpu_temp:.0f}\u00b0C")
        for idx_str in sorted(gpu_temps):
            v = gpu_temps[idx_str]
            if v is not None:
                temp_parts.append(f"GPU{idx_str[3:]} {v:.0f}\u00b0C")
        if nvme_temp is not None:
            temp_parts.append(f"NVMe {nvme_temp:.0f}\u00b0C")
        if temp_parts:
            parts.append("  " + "  ".join(temp_parts))
        parts.append(f" rate ${MUNICIPAL_RATE:.2f}/kWh")
        return "\n".join("  " + p for p in parts)

    def _graph_content(self) -> str:
        samples = self.store.samples
        if len(samples) < 2:
            return "\n  collecting data\u2026\n"
        try:
            box = self.query_one("#graph-box")
            w = box.content_region.width - 6 if box.content_region.width > 10 else 20
        except Exception:
            w = 40
        totals = [float(s.get("total", 0) or 0) for s in samples]
        data_w = max(w, 10)
        line = _sparkline(totals, data_w)
        mn, mx = min(totals), max(totals)
        avg = sum(totals) / len(totals)
        cur = totals[-1]
        txt = (
            f"  Current: {cur:>6.1f} W  Min: {mn:>6.1f} W  "
            f"Max: {mx:>6.1f} W  Avg: {avg:>6.1f} W\n"
            f"  {line}\n"
        )
        if len(samples) >= 2:
            t0 = datetime.fromtimestamp(samples[0].get("ts", 0))
            t1 = datetime.fromtimestamp(samples[-1].get("ts", 0))
            txt += f"  {t0.strftime('%H:%M')}{' ' * max(0, data_w - 16)}{t1.strftime('%H:%M')}\n"

        # Accumulated cost sparkline
        costs = [
            float(s.get("cum_kwh", {}).get("total", 0) or 0) * MUNICIPAL_RATE
            for s in samples
        ]
        if any(c > 0 for c in costs):
            cost_line = _sparkline(costs, data_w)
            cur_cost = costs[-1]
            proj = calc_costs(self.store)
            txt += (
                f"\n  Cost: ${cur_cost:.4f}  Month: ${proj['monthly_cost']:.2f}  "
                f"Year: ${proj['annual_cost']:.2f}\n"
                f"  {cost_line}\n"
            )
        return txt

    def _cost_content(self) -> str:
        costs = calc_costs(self.store)
        return "\n".join(
            [
                "",
                "  Rate ${:.3f}/kWh".format(MUNICIPAL_RATE),
                "",
                "  Session  ${:.3f}".format(costs["session_cost"]),
                "  Today    ${:.3f}".format(costs["today_cost"]),
                "  Monthly  ${:.2f}".format(costs["monthly_cost"]),
                "  Annual   ${:.2f}".format(costs["annual_cost"]),
                "",
            ]
        )

    def _ups_content(self) -> str:
        d = self.ups_data
        status = d.get("STATUS", "\u2014")
        bcharge = d.get("BCHARGE", "\u2014")
        timeleft = d.get("TIMELEFT", "\u2014")
        model = d.get("MODEL", "").strip()
        load_str = d.get("LOADPCT", "\u2014")
        nom_str = d.get("NOMPOWER", "\u2014")
        if load_str == "\u2014" or nom_str == "\u2014":
            ups_power = model
        else:
            ups_power = f"Load {load_str}  Rating {nom_str}"
        return (
            f"  UPS: {status:<12}  Batt: {bcharge:<8}  "
            f"Time: {timeleft:<8}  {ups_power}\n"
        )

    def _rate_content(self) -> str:
        ind = (
            "\u25c9 Indicator on  [i] toggle"
            if self._indicator_proc is not None and self._indicator_proc.poll() is None
            else "\u25cb Indicator off  [i] toggle"
        )
        if self._rate_phase == "done":
            src = self.rate_source[:45] if self.rate_source else "default"
            return f"  Rate source: {src:<45}\n  ${MUNICIPAL_RATE:.4f}/kWh  {ind}\n"
        spinner = "\u25d0\u25d1\u25d2\u25d3"[int(time.time() * 2) % 4]
        msg = self._rate_msg or "Initializing\u2026"
        return f"  {spinner} {msg:<55}\n  {'':>20} {ind}\n"
        return f"  Rate source: {src:<45}\n  ${MUNICIPAL_RATE:.4f}/kWh  {ind}\n"

    def update_ui(self) -> None:
        try:
            page = self._page if self._paginate() else 0
            if page == 0 or page == 1:
                self.query_one("#metrics-box").update(self._metrics_content())
                self.query_one("#graph-box").update(self._graph_content())
            if page == 0 or page == 2:
                self.query_one("#cost-box").update(self._cost_content())
            if page == 0 or page == 3:
                self.query_one("#ups-box").update(self._ups_content())
                self.query_one("#rate-box").update(self._rate_content())
        except Exception:
            pass


# ══════════════════════════════════════════════════════════════════════════
#  DAEMON MODE
# ══════════════════════════════════════════════════════════════════════════


def run_daemon() -> None:
    data_path = SYSTEM_DATA_DIR / DATA_FILE_NAME
    data_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        data_path.parent.chmod(0o755)
    except OSError:
        pass

    store.__init__(maxlen=0)
    store.path = data_path
    store.start_time = time.time()
    store.open()
    store.load_history(data_path, n=0)

    collector = Collector()
    collector.start()

    def _handle_sig(signum, frame):
        collector.stop()
        store.close()
        sys.exit(0)

    signal.signal(signal.SIGTERM, _handle_sig)
    signal.signal(signal.SIGINT, _handle_sig)

    print(f"Power daemon started \u2013 logging to {data_path}")
    sys.stdout.flush()
    signal.pause()


# ══════════════════════════════════════════════════════════════════════════
#  INDICATOR MODE
# ══════════════════════════════════════════════════════════════════════════


def run_indicator() -> None:
    """GNOME top bar indicator using AyatanaAppIndicator3, with pystray fallback."""

    _try_ayatana = True
    try:
        import gi

        gi.require_version("AyatanaAppIndicator3", "0.1")
        from gi.repository import AyatanaAppIndicator3 as AppIndicator3
        from gi.repository import GLib, Gtk
    except (ImportError, ValueError):
        _try_ayatana = False

    if not _try_ayatana:
        try:
            import pystray
            from PIL import Image, ImageDraw, ImageFont
        except ImportError as exc:
            print(f"Indicator requires AyatanaAppIndicator3 or pystray: {exc}")
            sys.exit(1)

        store.load_history(DAEMON_DATA)
        collector = Collector()
        collector.start()

        def _create_icon(cost: float) -> Image.Image:
            s = 64
            img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
            draw = ImageDraw.Draw(img)
            try:
                font = ImageFont.truetype(
                    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 14
                )
            except (IOError, OSError):
                font = ImageFont.load_default()
            txt = f"\u26a1${cost:.0f}"
            bbox = draw.textbbox((0, 0), txt, font=font)
            tw = bbox[2] - bbox[0]
            th = bbox[3] - bbox[1]
            draw.text(
                ((s - tw) / 2, (s - th) / 2),
                txt,
                fill=(255, 200, 0, 255),
                font=font,
            )
            return img

        def _update_icon_data(icon: pystray.Icon) -> None:
            costs = calc_costs(store)
            icon.icon = _create_icon(costs["monthly_cost"])
            icon.title = f"\u26a1 ${costs['monthly_cost']:.0f}/mo"

        def _on_quit(item):
            collector.stop()
            icon.stop()

        def _pystray_tick(icon: pystray.Icon) -> None:
            try:
                _update_icon_data(icon)
            except Exception:
                pass
            Timer(5, _pystray_tick, [icon]).start()

        icon = pystray.Icon(
            "power-monitor",
            _create_icon(0),
            menu=pystray.Menu(pystray.MenuItem("Quit", _on_quit)),
        )
        _update_icon_data(icon)
        Timer(5, _pystray_tick, [icon]).start()
        icon.run()
        return

    # ── AyatanaAppIndicator3 ────────────────────────────────────────────
    store.load_history(DAEMON_DATA)
    collector = Collector()
    collector.start()

    indicator = AppIndicator3.Indicator.new(
        "power-monitor",
        "utilities-system-monitor",
        AppIndicator3.IndicatorCategory.HARDWARE,
    )
    indicator.set_status(AppIndicator3.IndicatorStatus.ACTIVE)

    menu = Gtk.Menu()

    mi_title = Gtk.MenuItem(label="Power Monitor")
    mi_title.set_sensitive(False)
    menu.append(mi_title)

    menu.append(Gtk.SeparatorMenuItem())

    mi_cpu = Gtk.MenuItem(label="CPU: --- W  ---\u00b0C")
    mi_cpu.set_sensitive(False)
    menu.append(mi_cpu)

    mi_gpu = Gtk.MenuItem(label="GPU: --- W  ---\u00b0C")
    mi_gpu.set_sensitive(False)
    menu.append(mi_gpu)

    mi_dram = Gtk.MenuItem(label="DRAM: --- W")
    mi_dram.set_sensitive(False)
    menu.append(mi_dram)

    mi_ups = Gtk.MenuItem(label="UPS: --- W")
    mi_ups.set_sensitive(False)
    menu.append(mi_ups)

    mi_nvme = Gtk.MenuItem(label="NVMe: ---\u00b0C")
    mi_nvme.set_sensitive(False)
    menu.append(mi_nvme)

    menu.append(Gtk.SeparatorMenuItem())

    mi_session = Gtk.MenuItem(label="Session: $---")
    mi_session.set_sensitive(False)
    menu.append(mi_session)

    mi_today = Gtk.MenuItem(label="Today: $---")
    mi_today.set_sensitive(False)
    menu.append(mi_today)

    mi_monthly = Gtk.MenuItem(label="Monthly: $---")
    mi_monthly.set_sensitive(False)
    menu.append(mi_monthly)

    mi_annual = Gtk.MenuItem(label="Annual: $---")
    mi_annual.set_sensitive(False)
    menu.append(mi_annual)

    menu.append(Gtk.SeparatorMenuItem())

    mi_rate = Gtk.MenuItem(label="Rate: $---/kWh")
    mi_rate.set_sensitive(False)
    menu.append(mi_rate)

    menu.append(Gtk.SeparatorMenuItem())

    mi_quit = Gtk.MenuItem(label="Quit")
    mi_quit.connect("activate", lambda *a: _on_quit())
    menu.append(mi_quit)

    menu.show_all()
    indicator.set_menu(menu)

    refs = {
        "cpu": mi_cpu,
        "gpu": mi_gpu,
        "dram": mi_dram,
        "ups": mi_ups,
        "nvme": mi_nvme,
        "session": mi_session,
        "today": mi_today,
        "monthly": mi_monthly,
        "annual": mi_annual,
        "rate": mi_rate,
    }

    def _on_quit() -> None:
        collector.stop()
        Gtk.main_quit()

    def _update() -> bool:
        costs = calc_costs(store)
        indicator.set_label(f"\u26a1 ${costs['monthly_cost']:.0f}/mo", "")

        p = store.last.get("power", {}) if store.last else {}
        t = store.last.get("temps", {}) if store.last else {}
        rapl_cpu = next(
            (v for k, v in p.items() if k.startswith("rapl_") and "package" in k), None
        )
        cpu_w = rapl_cpu if rapl_cpu is not None else p.get("cpu_est", 0)
        rapl_dram = next((v for k, v in p.items() if k == "rapl_dram"), None)
        dram_w = rapl_dram if rapl_dram is not None else p.get("dram_est", 0)
        gpu_keys = sorted(k for k in p if k.startswith("gpu"))
        gpu_total = sum(float(p[k]) for k in gpu_keys if isinstance(p[k], (int, float)))
        ups_w = ups_watts(read_apc_ups()) or 0

        cpu_temp = t.get("cpu")
        cpu_label = f"CPU: {cpu_w:.0f} W"
        if cpu_temp is not None:
            cpu_label += f"  {cpu_temp:.0f}\u00b0C"

        gpu_temp_str = ""
        first_gpu_temp = next(
            (t[k] for k in sorted(t) if k.startswith("gpu") and t[k] is not None), None
        )
        if first_gpu_temp is not None:
            gpu_temp_str = f"  {first_gpu_temp:.0f}\u00b0C"

        nvme_temp = t.get("nvme")
        nvme_label = "NVMe: ---\u00b0C"
        if nvme_temp is not None:
            nvme_label = f"NVMe: {nvme_temp:.0f}\u00b0C"

        refs["cpu"].set_label(cpu_label)
        refs["gpu"].set_label(f"GPU: {gpu_total:.0f} W{gpu_temp_str}")
        refs["dram"].set_label(f"DRAM: {dram_w:.0f} W")
        refs["ups"].set_label(f"UPS: {ups_w:.0f} W")
        refs["nvme"].set_label(nvme_label)
        refs["session"].set_label(f"Session: ${costs['session_cost']:.2f}")
        refs["today"].set_label(f"Today: ${costs['today_cost']:.2f}")
        refs["monthly"].set_label(f"Monthly: ${costs['monthly_cost']:.1f}")
        refs["annual"].set_label(f"Annual: ${costs['annual_cost']:.0f}")
        refs["rate"].set_label(f"Rate: ${MUNICIPAL_RATE:.3f}/kWh")

        return True

    _update()
    GLib.timeout_add_seconds(5, _update)
    Gtk.main()


# ══════════════════════════════════════════════════════════════════════════
#  SERVICE INSTALL / UNINSTALL
# ══════════════════════════════════════════════════════════════════════════

SERVICE_UNIT = textwrap.dedent(f"""\
    [Unit]
    Description=Power Monitor Daemon
    Documentation=https://github.com/\u2026
    Wants=network.target
    After=network.target

    [Service]
    Type=simple
    User=root
    ExecStart={VENV_PYTHON} {SCRIPT_DIR / "power.py"} --daemon
    Restart=always
    RestartSec=5
    StandardOutput=append:{SYSTEM_DATA_DIR / "stdout.log"}
    StandardError=append:{SYSTEM_DATA_DIR / "stderr.log"}

    [Install]
    WantedBy=multi-user.target
""")


def install_service() -> None:
    if os.geteuid() != 0:
        print("Must run as root to install systemd service (try sudo).")
        sys.exit(1)
    SYSTEM_DATA_DIR.mkdir(parents=True, exist_ok=True)
    SYSTEM_DATA_DIR.chmod(0o755)
    with open(SERVICE_FILE, "w") as f:
        f.write(SERVICE_UNIT)
    SERVICE_FILE.chmod(0o644)
    subprocess.check_call(["systemctl", "daemon-reload"])
    subprocess.check_call(["systemctl", "enable", SERVICE_NAME])
    subprocess.check_call(["systemctl", "start", SERVICE_NAME])
    print(f"\u2713 Service {SERVICE_NAME} installed and started.")
    print(f"  Data \u2192 {DAEMON_DATA}")


def uninstall_service() -> None:
    if os.geteuid() != 0:
        print("Must run as root to uninstall systemd service (try sudo).")
        sys.exit(1)
    subprocess.run(["systemctl", "stop", SERVICE_NAME], capture_output=True)
    subprocess.run(["systemctl", "disable", SERVICE_NAME], capture_output=True)
    SERVICE_FILE.unlink(missing_ok=True)
    subprocess.check_call(["systemctl", "daemon-reload"])
    print(f"\u2713 Service {SERVICE_NAME} removed.")


def show_service_status() -> None:
    try:
        out = subprocess.check_output(
            ["systemctl", "is-active", SERVICE_NAME],
            text=True,
            stderr=subprocess.DEVNULL,
        )
        active = out.strip() == "active"
    except Exception:
        active = False
    print(
        f"{'●' if active else '○'} {SERVICE_NAME} is {'active' if active else 'not running'}"
    )
    if DAEMON_DATA.exists():
        size = DAEMON_DATA.stat().st_size
        lines_n = sum(1 for _ in DAEMON_DATA.open())
        mtime = datetime.fromtimestamp(DAEMON_DATA.stat().st_mtime)
        print(f"  Data: {DAEMON_DATA}")
        print(f"  Size: {size:,} bytes, {lines_n:,} samples")
        print(f"  Last: {mtime.strftime('%Y-%m-%d %H:%M:%S')}")
        store.load_history(DAEMON_DATA)
        if store.last:
            total = store.last.get("total", 0)
            costs = calc_costs(store)
            print(f"  Latest: {total:.1f} W")
            print(f"  Session: ${costs['session_cost']:.3f}")
            print(f"  Monthly: ${costs['monthly_cost']:.2f}")
    else:
        print("  Data file not found")


# ══════════════════════════════════════════════════════════════════════════
#  ENTRY POINT
# ══════════════════════════════════════════════════════════════════════════


def main() -> None:
    global MUNICIPAL_RATE
    import argparse

    parser = argparse.ArgumentParser(
        description="Power Monitor \u2013 TUI, daemon & tray indicator"
    )
    parser.add_argument(
        "--daemon", action="store_true", help="Run as background data-collection daemon"
    )
    parser.add_argument(
        "--indicator",
        action="store_true",
        help="Show system-tray monthly-cost indicator",
    )
    parser.add_argument(
        "--install-service", action="store_true", help="Install + start systemd service"
    )
    parser.add_argument(
        "--uninstall-service", action="store_true", help="Stop + remove systemd service"
    )
    parser.add_argument(
        "--status", action="store_true", help="Show service and data status"
    )
    parser.add_argument(
        "--fetch-rate",
        action="store_true",
        help="Query Omnius for local electricity rate",
    )
    parser.add_argument(
        "--no-fetch-rate",
        action="store_true",
        help="Skip Omnius rate discovery on startup",
    )
    args = parser.parse_args()

    if args.install_service:
        install_service()
    elif args.uninstall_service:
        uninstall_service()
    elif args.status:
        show_service_status()
    elif args.daemon:
        run_daemon()
    elif args.indicator:
        run_indicator()
    elif args.fetch_rate:
        rate, source = discover_rate()
        if rate is not None:
            print(f"Rate: ${rate:.4f}/kWh  Source: {source}")
            print(f"Set:  export POWER_RATE={rate}")
        else:
            print(f"Rate discovery failed: {source}")
            print(f"Current rate: ${MUNICIPAL_RATE:.4f}/kWh (default)")
        sys.exit(0)
    else:
        collector = Collector()
        collector.start()
        store.load_history(DAEMON_DATA)
        app = PowerTUI(collector, store, no_fetch=args.no_fetch_rate)
        try:
            app.run()
        except KeyboardInterrupt:
            pass
        finally:
            collector.stop()


if __name__ == "__main__":
    main()
