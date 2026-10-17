"""Local-first Python HTTP bridge for the Helpyard Windows dashboard."""

from __future__ import annotations

import argparse
import base64
import ctypes
from contextlib import contextmanager
import hashlib
import hmac
import ipaddress
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
import uuid
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Iterator
from ctypes import wintypes

ROOT = Path(__file__).resolve().parent
DASHBOARD_PATH = ROOT / "pc-control-dashboard.html"
MAX_REQUEST_BYTES = 1_048_576
BOOTSTRAP_TTL_SECONDS = 60
DEFAULT_OLLAMA_URL = "http://127.0.0.1:11434"
PORT_CANDIDATES = (8787, 8788, 8789, 8790, 8791, 8792, 8800, 9000, 9001)
SKILLS_SCHEMA_VERSION = 2
MAX_SKILLS = 500
MAX_SKILLS_FILE_BYTES = 64 * 1024 * 1024
SKILLS_LOCK = threading.RLock()
SKILLS_FILE_LOCK_STATE = threading.local()
BUILTIN_SKILL_DEFINITIONS = [
    ("00000000-0000-4000-8000-000000000001", "Show IP addresses", "Network", "Local network addresses", "Display IPv4 addresses assigned to local network interfaces."),
    ("00000000-0000-4000-8000-000000000002", "Top memory apps", "Performance", "15 biggest memory users", "List the 15 processes using the most working-set memory."),
    ("00000000-0000-4000-8000-000000000003", "Disk usage", "Storage", "Used and free space per drive", "Show capacity and free space for fixed disks."),
    ("00000000-0000-4000-8000-000000000004", "Installed apps", "Software", "List installed programs", "List installed program names and versions from Windows uninstall registry entries."),
    ("00000000-0000-4000-8000-000000000005", "Open ports", "Network", "Listening TCP ports", "Show TCP ports in the listening state and their owning process IDs."),
    ("00000000-0000-4000-8000-000000000006", "Startup programs", "System", "What starts with Windows", "List registered Windows startup commands without running them."),
    ("00000000-0000-4000-8000-000000000007", "Defender status", "Security", "Antivirus protection state", "Show Windows Defender service and real-time protection status."),
    ("00000000-0000-4000-8000-000000000008", "Wi-Fi status", "Network", "Current wireless connection", "Show local Wi-Fi interface and connection profile status."),
    ("00000000-0000-4000-8000-000000000009", "GPU info", "Hardware", "Graphics card and driver", "Show detected graphics adapters and driver versions."),
    ("00000000-0000-4000-8000-000000000010", "Battery", "Hardware", "Charge level", "Show battery charge and status when a battery is present."),
    ("00000000-0000-4000-8000-000000000011", "Recent updates", "System", "Last 10 Windows updates", "Show the ten most recently installed Windows hotfixes."),
    ("00000000-0000-4000-8000-000000000012", "Dev tool versions", "Development", "Node, Python, Git, Ollama", "Show versions for Python and selected developer tools found in standard installation locations."),
    ("00000000-0000-4000-8000-000000000013", "Ollama models", "AI", "Installed local AI models", "List models available from the configured local Ollama service."),
]
RUNNABLE_SKILL_IDS = {definition[0] for definition in BUILTIN_SKILL_DEFINITIONS}


class BridgeError(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status


ACTION_REGISTRY: dict[str, dict[str, Any]] = {
    "lock": {
        "name": "lock",
        "description": "Lock the current Windows session",
        "permission": "standard",
        "risk": "low",
        "requiresApproval": False,
        "timeoutSec": 10,
        "outputLimit": 2048,
        "audit": "security screen lock",
    },
    "sleep": {
        "name": "sleep",
        "description": "Suspend the computer",
        "permission": "standard",
        "risk": "medium",
        "requiresApproval": True,
        "timeoutSec": 15,
        "outputLimit": 1024,
        "audit": "power management",
    },
    "restart": {
        "name": "restart",
        "description": "Restart the computer after a short delay",
        "permission": "admin",
        "risk": "high",
        "requiresApproval": True,
        "timeoutSec": 20,
        "outputLimit": 1024,
        "audit": "system reboot",
    },
    "shutdown": {
        "name": "shutdown",
        "description": "Shut down the computer after a short delay",
        "permission": "admin",
        "risk": "high",
        "requiresApproval": True,
        "timeoutSec": 20,
        "outputLimit": 1024,
        "audit": "system shutdown",
    },
    "cancel": {
        "name": "cancel",
        "description": "Cancel the pending shutdown or restart",
        "permission": "standard",
        "risk": "low",
        "requiresApproval": False,
        "timeoutSec": 10,
        "outputLimit": 1024,
        "audit": "shutdown cancellation",
    },
    "flushdns": {
        "name": "flushdns",
        "description": "Flush the Windows DNS cache",
        "permission": "standard",
        "risk": "low",
        "requiresApproval": False,
        "timeoutSec": 15,
        "outputLimit": 1024,
        "audit": "network refresh",
    },
    "taskmgr": {
        "name": "taskmgr",
        "description": "Open Task Manager",
        "permission": "standard",
        "risk": "low",
        "requiresApproval": False,
        "timeoutSec": 10,
        "outputLimit": 512,
        "audit": "system utility open",
    },
    "explorer": {
        "name": "explorer",
        "description": "Open File Explorer",
        "permission": "standard",
        "risk": "low",
        "requiresApproval": False,
        "timeoutSec": 10,
        "outputLimit": 512,
        "audit": "file explorer launch",
    },
    "settings": {
        "name": "settings",
        "description": "Open Windows Settings",
        "permission": "standard",
        "risk": "low",
        "requiresApproval": False,
        "timeoutSec": 10,
        "outputLimit": 512,
        "audit": "system settings",
    },
    "devmgr": {
        "name": "devmgr",
        "description": "Open Device Manager",
        "permission": "standard",
        "risk": "low",
        "requiresApproval": False,
        "timeoutSec": 10,
        "outputLimit": 512,
        "audit": "device manager launch",
    },
    "emptybin": {
        "name": "emptybin",
        "description": "Empty the Recycle Bin",
        "permission": "standard",
        "risk": "medium",
        "requiresApproval": True,
        "timeoutSec": 15,
        "outputLimit": 1024,
        "audit": "file deletion",
    },
    "cleantemp": {
        "name": "cleantemp",
        "description": "Clean the user temporary folder",
        "permission": "standard",
        "risk": "medium",
        "requiresApproval": True,
        "timeoutSec": 20,
        "outputLimit": 1024,
        "audit": "temporary file cleanup",
    },
    "perf_high": {
        "name": "perf_high",
        "description": "Enable the high performance power scheme",
        "permission": "admin",
        "risk": "medium",
        "requiresApproval": True,
        "timeoutSec": 15,
        "outputLimit": 1024,
        "audit": "power policy change",
    },
    "perf_balanced": {
        "name": "perf_balanced",
        "description": "Enable the balanced power scheme",
        "permission": "standard",
        "risk": "low",
        "requiresApproval": False,
        "timeoutSec": 15,
        "outputLimit": 1024,
        "audit": "power policy change",
    },
    "perf_saver": {
        "name": "perf_saver",
        "description": "Enable the power saver scheme",
        "permission": "standard",
        "risk": "low",
        "requiresApproval": False,
        "timeoutSec": 15,
        "outputLimit": 1024,
        "audit": "power policy change",
    },
    "winupdate": {
        "name": "winupdate",
        "description": "Open Windows Update settings",
        "permission": "standard",
        "risk": "low",
        "requiresApproval": False,
        "timeoutSec": 10,
        "outputLimit": 512,
        "audit": "update settings",
    },
}

ACTION_SCRIPTS = {
    "lock": "Start-Process rundll32.exe -ArgumentList 'user32.dll,LockWorkStation'; $result = 'Lock requested'",
    "sleep": "Start-Process rundll32.exe -ArgumentList 'powrprof.dll,SetSuspendState 0,1,0'; $result = 'Sleep requested'",
    "restart": "shutdown.exe /r /t 30; if ($LASTEXITCODE -ne 0) { throw 'Restart could not be scheduled.' }; $result = 'Restart scheduled'",
    "shutdown": "shutdown.exe /s /t 30; if ($LASTEXITCODE -ne 0) { throw 'Shutdown could not be scheduled.' }; $result = 'Shutdown scheduled'",
    "cancel": "shutdown.exe /a; if ($LASTEXITCODE -ne 0) { throw 'No pending shutdown could be cancelled.' }; $result = 'Pending shutdown cancelled'",
    "flushdns": "ipconfig.exe /flushdns; if ($LASTEXITCODE -ne 0) { throw 'DNS cache could not be flushed.' }; $result = 'DNS cache flushed'",
    "taskmgr": "Start-Process taskmgr.exe; $result = 'Task Manager opened'",
    "explorer": "Start-Process explorer.exe; $result = 'File Explorer opened'",
    "settings": "Start-Process 'ms-settings:'; $result = 'Windows Settings opened'",
    "devmgr": "Start-Process devmgmt.msc; $result = 'Device Manager opened'",
    "emptybin": "Clear-RecycleBin -Force -ErrorAction Stop; $result = 'Recycle Bin emptied'",
    "cleantemp": "Get-ChildItem -LiteralPath $env:TEMP -Force -ErrorAction Stop | Remove-Item -Recurse -Force -ErrorAction Stop; $result = 'User temp folder cleaned'",
    "perf_high": "powercfg.exe /setactive SCHEME_MIN; if ($LASTEXITCODE -ne 0) { throw 'Power plan change failed.' }; $result = 'High performance plan active'",
    "perf_balanced": "powercfg.exe /setactive SCHEME_BALANCED; if ($LASTEXITCODE -ne 0) { throw 'Power plan change failed.' }; $result = 'Balanced plan active'",
    "perf_saver": "powercfg.exe /setactive SCHEME_MAX; if ($LASTEXITCODE -ne 0) { throw 'Power plan change failed.' }; $result = 'Power saver plan active'",
    "winupdate": "Start-Process 'ms-settings:windowsupdate'; $result = 'Windows Update opened'",
}

READ_ONLY_SKILL_SCRIPTS: dict[str, str] = {
    BUILTIN_SKILL_DEFINITIONS[0][0]: (
        "@(Get-NetIPAddress -AddressFamily IPv4 -ErrorAction Stop | "
        "Where-Object { $_.IPAddress -ne '127.0.0.1' -and $_.IPAddress -notlike '169.254.*' } | "
        "Select-Object InterfaceAlias, IPAddress, PrefixLength, AddressState)"
    ),
    BUILTIN_SKILL_DEFINITIONS[1][0]: (
        "@(Get-Process -ErrorAction Stop | Sort-Object WorkingSet64 -Descending | Select-Object -First 15 | "
        "ForEach-Object { @{ name = $_.ProcessName; id = $_.Id; memoryMB = [math]::Round($_.WorkingSet64 / 1MB, 1) } })"
    ),
    BUILTIN_SKILL_DEFINITIONS[2][0]: (
        "@(Get-CimInstance Win32_LogicalDisk -Filter 'DriveType=3' | "
        "ForEach-Object { @{ drive = $_.DeviceID; totalGB = [math]::Round($_.Size / 1GB, 1); "
        "freeGB = [math]::Round($_.FreeSpace / 1GB, 1); usedGB = [math]::Round(($_.Size - $_.FreeSpace) / 1GB, 1) } })"
    ),
    BUILTIN_SKILL_DEFINITIONS[3][0]: (
        "$apps = @(foreach ($key in @('HKLM:\\Software\\Microsoft\\Windows\\CurrentVersion\\Uninstall\\*', "
        "'HKLM:\\Software\\WOW6432Node\\Microsoft\\Windows\\CurrentVersion\\Uninstall\\*', "
        "'HKCU:\\Software\\Microsoft\\Windows\\CurrentVersion\\Uninstall\\*')) { "
        "Get-ItemProperty -Path $key -ErrorAction SilentlyContinue | "
        "Where-Object { $_.DisplayName } | Select-Object @{N='name';E={$_.DisplayName}}, "
        "@{N='version';E={$_.DisplayVersion}}, @{N='publisher';E={$_.Publisher}} }); "
        "[ordered]@{ items = @($apps | Sort-Object name -Unique | Select-Object -First 200); truncated = ($apps.Count -gt 200) }"
    ),
    BUILTIN_SKILL_DEFINITIONS[4][0]: (
        "$processNames = @{}; Get-Process -ErrorAction SilentlyContinue | "
        "ForEach-Object { $processNames[[string]$_.Id] = $_.ProcessName }; "
        "$allPorts = @(Get-NetTCPConnection -State Listen -ErrorAction Stop); "
        "$ports = @($allPorts | Sort-Object LocalPort | Select-Object -First 250 | "
        "ForEach-Object { @{ address = $_.LocalAddress; port = $_.LocalPort; "
        "processId = $_.OwningProcess; process = $processNames[[string]$_.OwningProcess] } }); "
        "[ordered]@{ items = $ports; truncated = ($allPorts.Count -gt 250) }"
    ),
    BUILTIN_SKILL_DEFINITIONS[5][0]: (
        "$allItems = @(Get-CimInstance Win32_StartupCommand -ErrorAction Stop); "
        "$items = @($allItems | Select-Object -First 200 Name, Location, User); "
        "[ordered]@{ items = $items; truncated = ($allItems.Count -gt 200) }"
    ),
    BUILTIN_SKILL_DEFINITIONS[6][0]: (
        "$status = Get-MpComputerStatus -ErrorAction Stop; "
        "[ordered]@{ antivirusEnabled = $status.AntivirusEnabled; realTimeProtectionEnabled = $status.RealTimeProtectionEnabled; "
        "antispywareEnabled = $status.AntispywareEnabled; quickScanAgeDays = $status.QuickScanAge; fullScanAgeDays = $status.FullScanAge }"
    ),
    BUILTIN_SKILL_DEFINITIONS[7][0]: (
        "@(Get-NetConnectionProfile -ErrorAction Stop | "
        "Where-Object { $_.InterfaceAlias -match '(?i)wi-?fi|wireless' } | "
        "Select-Object Name, InterfaceAlias, NetworkCategory, IPv4Connectivity, IPv6Connectivity)"
    ),
    BUILTIN_SKILL_DEFINITIONS[8][0]: (
        "@(Get-CimInstance Win32_VideoController -ErrorAction Stop | "
        "Select-Object Name, DriverVersion, @{N='adapterMemoryMB';E={[math]::Round($_.AdapterRAM / 1MB, 0)}})"
    ),
    BUILTIN_SKILL_DEFINITIONS[9][0]: (
        "$batteries = @(Get-CimInstance Win32_Battery -ErrorAction Stop | "
        "Select-Object Name, EstimatedChargeRemaining, BatteryStatus); "
        "[ordered]@{ available = ($batteries.Count -gt 0); items = $batteries }"
    ),
    BUILTIN_SKILL_DEFINITIONS[10][0]: (
        "@(Get-HotFix -ErrorAction Stop | Sort-Object InstalledOn -Descending | "
        "Select-Object -First 10 HotFixID, Description, InstalledOn, InstalledBy)"
    ),
}


def is_admin() -> bool:
    if os.name != "nt":
        return False
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except (AttributeError, OSError):
        return False


def powershell_executable() -> str:
    windows = os.environ.get("WINDIR", r"C:\Windows")
    executable = Path(windows) / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"
    if not executable.is_file():
        raise BridgeError(503, "Windows PowerShell 5.1 was not found.")
    return str(executable)


def run_powershell(script: str, timeout: int = 30) -> str:
    """Run a fixed bridge operation without invoking a shell or exposing tokens."""
    preamble = (
        "$ErrorActionPreference = 'Stop'; "
        "[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false); "
    )
    encoded = base64.b64encode((preamble + script).encode("utf-16le")).decode("ascii")
    try:
        result = subprocess.run(
            [
                powershell_executable(),
                "-NoLogo",
                "-NoProfile",
                "-NonInteractive",
                "-ExecutionPolicy",
                "Bypass",
                "-EncodedCommand",
                encoded,
            ],
            capture_output=True,
            timeout=timeout,
            check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except subprocess.TimeoutExpired as exc:
        raise BridgeError(504, "The Windows operation timed out.") from exc
    except OSError as exc:
        raise BridgeError(503, f"Could not start Windows PowerShell: {exc}") from exc

    stdout = result.stdout.decode("utf-8-sig", errors="replace").strip()
    stderr = result.stderr.decode("utf-8-sig", errors="replace").strip()
    if result.returncode:
        message = stderr or stdout or f"Windows operation failed with exit code {result.returncode}."
        raise BridgeError(502, message[-2000:])
    return stdout


def run_powershell_json(script: str, timeout: int = 30) -> Any:
    output = run_powershell(
        f"$result = & {{ {script} }}; "
        "ConvertTo-Json -InputObject $result -Depth 12 -Compress",
        timeout,
    )
    try:
        return json.loads(output)
    except json.JSONDecodeError as exc:
        raise BridgeError(502, "Windows operation returned invalid JSON.") from exc


def quote_powershell(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def read_json_body(handler: BaseHTTPRequestHandler) -> dict[str, Any]:
    raw_length = handler.headers.get("Content-Length", "0")
    try:
        length = int(raw_length)
    except ValueError as exc:
        raise BridgeError(400, "Invalid Content-Length header.") from exc
    if length < 0:
        raise BridgeError(400, "Invalid Content-Length header.")
    if length > MAX_REQUEST_BYTES:
        if length == MAX_REQUEST_BYTES + 1:
            remaining = length
            while remaining:
                chunk = handler.rfile.read(min(65536, remaining))
                if not chunk:
                    handler.close_connection = True
                    break
                remaining -= len(chunk)
        else:
            handler.close_connection = True
        raise BridgeError(413, "Request body exceeds the 1 MiB limit.")
    body_bytes = handler.rfile.read(length)
    if len(body_bytes) != length:
        raise BridgeError(400, "Request body ended before Content-Length bytes were received.")
    try:
        body = json.loads(body_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BridgeError(400, "Invalid JSON request body.") from exc
    if not isinstance(body, dict):
        raise BridgeError(400, "The JSON request body must be an object.")
    return body


def assert_allowed_properties(body: dict[str, Any], allowed_fields: set[str]) -> None:
    unexpected = set(body) - allowed_fields
    if unexpected:
        field = sorted(unexpected)[0]
        raise BridgeError(400, f"Unexpected request field '{field}'.")


def assert_approved(body: dict[str, Any], message: str) -> None:
    if body.get("approved") is not True:
        raise BridgeError(403, message)


def normalize_loopback_url(value: str) -> str:
    parsed = urllib.parse.urlsplit(value)
    if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost"}:
        raise ValueError("Ollama URL must use http://127.0.0.1 or http://localhost.")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("Ollama URL must not contain credentials, a query, or a fragment.")
    if parsed.path not in {"", "/"}:
        raise ValueError("Ollama URL must be an origin without a path.")
    try:
        port = parsed.port or 11434
    except ValueError as exc:
        raise ValueError("Ollama URL has an invalid port.") from exc
    return f"http://{parsed.hostname}:{port}"


def get_projects() -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = [
        {
            "name": ROOT.name,
            "path": str(ROOT),
            "kind": "folder",
            "accessible": True,
        }
    ]
    try:
        children = sorted(ROOT.iterdir(), key=lambda entry: entry.name.casefold())
    except OSError as exc:
        raise BridgeError(500, f"Could not read workspace folder: {exc}") from exc
    for directory in children:
        if not directory.is_dir() or not (directory / ".git").exists():
            continue
        branch_result = subprocess.run(
            ["git", "-C", str(directory), "rev-parse", "--abbrev-ref", "HEAD"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        status_result = subprocess.run(
            ["git", "-C", str(directory), "status", "--short", "--branch"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        items.append(
            {
                "name": directory.name,
                "path": str(directory),
                "kind": "git",
                "accessible": True,
                "branch": branch_result.stdout.strip() if branch_result.returncode == 0 else "",
                "status": status_result.stdout.strip() if status_result.returncode == 0 else "",
            }
        )
    items.sort(key=lambda project: project["name"].casefold())
    return items


def get_claude_config() -> dict[str, Any]:
    appdata = os.environ.get("APPDATA")
    if not appdata:
        raise BridgeError(500, "The current user's APPDATA folder is unavailable.")
    path = Path(appdata) / "Claude" / "claude_desktop_config.json"
    try:
        content = path.read_text(encoding="utf-8-sig") if path.is_file() else ""
    except OSError as exc:
        raise BridgeError(500, f"Could not read Claude configuration: {exc}") from exc
    return {"path": str(path), "exists": path.is_file(), "content": content}


def set_claude_config(content: Any) -> dict[str, Any]:
    if not isinstance(content, str):
        raise BridgeError(400, "Claude configuration content must be a JSON string.")
    try:
        json.loads(content)
    except json.JSONDecodeError as exc:
        raise BridgeError(400, f"Invalid Claude configuration JSON: {exc.msg}") from exc
    appdata = os.environ.get("APPDATA")
    if not appdata:
        raise BridgeError(500, "The current user's APPDATA folder is unavailable.")
    directory = Path(appdata) / "Claude"
    path = directory / "claude_desktop_config.json"
    try:
        directory.mkdir(parents=True, exist_ok=True)
        if path.is_file():
            timestamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
            path.replace(path.with_name(f"{path.name}.bak-{timestamp}"))
        path.write_text(content, encoding="utf-8")
    except OSError as exc:
        raise BridgeError(500, f"Could not update Claude configuration: {exc}") from exc
    return {"ok": True, "path": str(path)}


def skills_file_path() -> Path:
    appdata = os.environ.get("APPDATA")
    base = Path(appdata) if appdata else Path.home() / ".helpyard"
    return base / "Helpyard" / "skills.json" if appdata else base / "skills.json"


def default_skills() -> dict[str, Any]:
    now = datetime.now(timezone.utc).isoformat()
    return {
        "schemaVersion": SKILLS_SCHEMA_VERSION,
        "skills": [
            {
                "id": skill_id,
                "name": name,
                "category": category,
                "description": description,
                "instructions": f"{description}. This built-in check is read-only; review its results locally.",
                "status": "enabled",
                "builtIn": True,
                "pinned": False,
                "runnerId": skill_id,
                "createdAt": now,
                "updatedAt": now,
                "version": 1,
            }
            for skill_id, name, category, description, _ in BUILTIN_SKILL_DEFINITIONS
        ],
    }


def _valid_timestamp(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    try:
        datetime.fromisoformat(value)
    except ValueError:
        return False
    return True


def _validate_skill_record(record: Any) -> bool:
    if not isinstance(record, dict) or set(record) != {
        "id", "name", "category", "description", "instructions", "status",
        "builtIn", "pinned", "runnerId", "createdAt", "updatedAt", "version",
    }:
        return False
    try:
        valid_id = str(uuid.UUID(record["id"])) == record["id"]
    except (ValueError, TypeError, AttributeError):
        valid_id = False
    return (
        valid_id
        and isinstance(record["name"], str) and 1 <= len(record["name"]) <= 120
        and isinstance(record["category"], str) and 1 <= len(record["category"]) <= 80
        and isinstance(record["description"], str) and len(record["description"]) <= 2000
        and isinstance(record["instructions"], str) and 1 <= len(record["instructions"]) <= 20000
        and isinstance(record["status"], str) and record["status"] in {"draft", "enabled", "disabled"}
        and isinstance(record["builtIn"], bool)
        and isinstance(record["pinned"], bool)
        and (
            (record["builtIn"] and record["runnerId"] == record["id"] and record["runnerId"] in RUNNABLE_SKILL_IDS)
            or (not record["builtIn"] and record["runnerId"] is None)
        )
        and _valid_timestamp(record["createdAt"])
        and _valid_timestamp(record["updatedAt"])
        and isinstance(record["version"], int) and not isinstance(record["version"], bool)
        and record["version"] >= 1
    )


def _validate_v1_skill_record(record: Any) -> bool:
    if not isinstance(record, dict) or set(record) != {
        "id", "name", "category", "description", "instructions", "status",
        "createdAt", "updatedAt", "version",
    }:
        return False
    try:
        valid_id = str(uuid.UUID(record["id"])) == record["id"]
    except (ValueError, TypeError, AttributeError):
        valid_id = False
    return (
        valid_id
        and isinstance(record["name"], str) and 1 <= len(record["name"]) <= 120
        and isinstance(record["category"], str) and 1 <= len(record["category"]) <= 80
        and isinstance(record["description"], str) and len(record["description"]) <= 2000
        and isinstance(record["instructions"], str) and 1 <= len(record["instructions"]) <= 20000
        and isinstance(record["status"], str) and record["status"] in {"draft", "enabled", "disabled"}
        and _valid_timestamp(record["createdAt"])
        and _valid_timestamp(record["updatedAt"])
        and isinstance(record["version"], int) and not isinstance(record["version"], bool)
        and record["version"] >= 1
    )


def _migrate_v1_skills(payload: Any) -> dict[str, Any]:
    if (
        not isinstance(payload, dict)
        or set(payload) != {"schemaVersion", "skills"}
        or type(payload.get("schemaVersion")) is not int
        or payload["schemaVersion"] != 1
        or not isinstance(payload.get("skills"), list)
        or len(payload["skills"]) > MAX_SKILLS
        or not all(_validate_v1_skill_record(item) for item in payload["skills"])
        or len({item["id"] for item in payload["skills"]}) != len(payload["skills"])
    ):
        raise BridgeError(500, "Saved Skills data has an unsupported or invalid schema; the original file was preserved.")
    skills = [
        {**skill, "builtIn": False, "pinned": False, "runnerId": None}
        for skill in payload["skills"]
    ]
    existing_ids = {skill["id"] for skill in skills}
    for builtin in default_skills()["skills"]:
        if len(skills) >= MAX_SKILLS:
            break
        if builtin["id"] not in existing_ids:
            skills.append(builtin)
    return {"schemaVersion": SKILLS_SCHEMA_VERSION, "skills": skills}


def read_skills() -> dict[str, Any]:
    path = skills_file_path()
    with SKILLS_LOCK:
        try:
            if path.is_symlink():
                raise BridgeError(500, "Saved Skills path must not be a symbolic link; the original path was preserved.")
            if not path.exists():
                with _skills_write_lock():
                    if path.exists():
                        return read_skills()
                    payload = default_skills()
                    _write_skills(payload)
                    return payload
            if path.stat().st_size > MAX_SKILLS_FILE_BYTES:
                raise BridgeError(500, "Saved Skills data exceeds the size limit; the original file was preserved.")
            payload = json.loads(path.read_text(encoding="utf-8"))
        except BridgeError:
            raise
        except (OSError, UnicodeError) as exc:
            raise BridgeError(500, "Could not read saved Skills; the original data was preserved.") from exc
        except json.JSONDecodeError as exc:
            raise BridgeError(500, "Saved Skills data is invalid JSON; the original file was preserved.") from exc
        if isinstance(payload, dict) and type(payload.get("schemaVersion")) is int and payload["schemaVersion"] == 1:
            with _skills_write_lock():
                try:
                    if path.stat().st_size > MAX_SKILLS_FILE_BYTES:
                        raise BridgeError(500, "Saved Skills data exceeds the size limit; the original file was preserved.")
                    latest_payload = json.loads(path.read_text(encoding="utf-8"))
                except BridgeError:
                    raise
                except (OSError, UnicodeError) as exc:
                    raise BridgeError(500, "Could not migrate saved Skills; the original file was preserved.") from exc
                except json.JSONDecodeError as exc:
                    raise BridgeError(500, "Saved Skills data is invalid JSON; the original file was preserved.") from exc
                if isinstance(latest_payload, dict) and latest_payload.get("schemaVersion") != 1:
                    return read_skills()
                migrated = _migrate_v1_skills(latest_payload)
                _write_skills(migrated)
                return migrated
        if (
            not isinstance(payload, dict)
            or set(payload) != {"schemaVersion", "skills"}
            or type(payload.get("schemaVersion")) is not int
            or payload.get("schemaVersion") != SKILLS_SCHEMA_VERSION
            or not isinstance(payload.get("skills"), list)
            or len(payload["skills"]) > MAX_SKILLS
            or not all(_validate_skill_record(item) for item in payload["skills"])
            or len({item["id"] for item in payload["skills"]}) != len(payload["skills"])
        ):
            raise BridgeError(500, "Saved Skills data has an unsupported or invalid schema; the original file was preserved.")
        return payload


def _write_skills(payload: dict[str, Any]) -> None:
    path = skills_file_path()
    temporary_path: str | None = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.is_symlink():
            raise BridgeError(500, "Saved Skills path must not be a symbolic link; the original path was preserved.")
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", newline="\n", dir=path.parent,
            prefix=f".{path.name}.", suffix=".tmp", delete=False,
        ) as temporary:
            temporary_path = temporary.name
            json.dump(payload, temporary, ensure_ascii=False, indent=2)
            temporary.write("\n")
            temporary.flush()
            os.fsync(temporary.fileno())
        if Path(temporary_path).stat().st_size > MAX_SKILLS_FILE_BYTES:
            raise BridgeError(413, "Saved Skills data exceeds the storage size limit.")
        if path.is_file():
            backup = path.with_name(
                f"{path.name}.bak-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')}"
            )
            shutil.copy2(path, backup)
        os.replace(temporary_path, path)
        temporary_path = None
    except OSError as exc:
        raise BridgeError(500, "Could not safely save Skills data; existing data was preserved.") from exc
    finally:
        if temporary_path:
            try:
                os.unlink(temporary_path)
            except OSError:
                pass


@contextmanager
def _skills_write_lock() -> Iterator[None]:
    if getattr(SKILLS_FILE_LOCK_STATE, "held", False):
        yield
        return
    path = skills_file_path()
    lock_path = path.with_name(path.name + ".lock")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        if lock_path.is_symlink():
            raise BridgeError(500, "Skills lock path must not be a symbolic link.")
        with lock_path.open("a+b") as lock_file:
            if os.name == "nt":
                import msvcrt

                lock_file.seek(0)
                if not lock_file.read(1):
                    lock_file.seek(0)
                    lock_file.write(b"\0")
                    lock_file.flush()
                lock_file.seek(0)
                msvcrt.locking(lock_file.fileno(), msvcrt.LK_LOCK, 1)
                SKILLS_FILE_LOCK_STATE.held = True
                try:
                    yield
                finally:
                    SKILLS_FILE_LOCK_STATE.held = False
                    lock_file.seek(0)
                    msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
                SKILLS_FILE_LOCK_STATE.held = True
                try:
                    yield
                finally:
                    SKILLS_FILE_LOCK_STATE.held = False
                    fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
    except OSError as exc:
        raise BridgeError(500, "Could not coordinate a safe Skills update.") from exc


def _developer_tool_versions() -> dict[str, Any]:
    program_files = Path(os.environ.get("ProgramFiles", r"C:\Program Files"))
    local_app_data = os.environ.get("LOCALAPPDATA")
    candidates: dict[str, list[Path]] = {
        "Node": [program_files / "nodejs" / "node.exe"],
        "Git": [program_files / "Git" / "cmd" / "git.exe", program_files / "Git" / "bin" / "git.exe"],
        "Ollama": [program_files / "Ollama" / "ollama.exe"],
    }
    if local_app_data:
        local = Path(local_app_data)
        candidates["Node"].append(local / "Programs" / "nodejs" / "node.exe")
        candidates["Git"].append(local / "Programs" / "Git" / "cmd" / "git.exe")
        candidates["Ollama"].append(local / "Programs" / "Ollama" / "ollama.exe")

    versions: dict[str, Any] = {"Python": {"available": True, "version": sys.version.split()[0]}}
    for name, paths in candidates.items():
        executable = next((path for path in paths if path.is_file()), None)
        if executable is None:
            versions[name] = {"available": False, "version": "Not found in standard install locations"}
            continue
        try:
            result = subprocess.run(
                [str(executable), "--version"],
                capture_output=True,
                text=True,
                timeout=8,
                check=False,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except subprocess.TimeoutExpired as exc:
            raise BridgeError(504, f"{name} version check timed out.") from exc
        except OSError as exc:
            raise BridgeError(503, f"{name} version check could not start.") from exc
        output = (result.stdout or result.stderr).strip()
        if result.returncode != 0 or not output:
            raise BridgeError(502, f"{name} did not return a version.")
        versions[name] = {"available": True, "version": output[:200]}
    return versions


def set_skill_pinned(skill_id: Any, pinned: Any) -> dict[str, Any]:
    if not isinstance(pinned, bool):
        raise BridgeError(400, "Pinned state must be a JSON boolean.")
    try:
        valid_id = isinstance(skill_id, str) and str(uuid.UUID(skill_id)) == skill_id
    except ValueError:
        valid_id = False
    if not valid_id:
        raise BridgeError(400, "A valid Skill ID is required.")
    with SKILLS_LOCK, _skills_write_lock():
        payload = read_skills()
        skill = next((item for item in payload["skills"] if item["id"] == skill_id), None)
        if skill is None:
            raise BridgeError(404, "Skill not found.")
        skill["pinned"] = pinned
        skill["updatedAt"] = datetime.now(timezone.utc).isoformat()
        _write_skills(payload)
        return skill


def run_skill_check(skill_id: Any, ollama_url: str = DEFAULT_OLLAMA_URL) -> dict[str, Any]:
    if not isinstance(skill_id, str) or skill_id not in RUNNABLE_SKILL_IDS:
        raise BridgeError(403, "This skill has no approved read-only operation.")
    payload = read_skills()
    skill = next((item for item in payload["skills"] if item["id"] == skill_id), None)
    if skill is None:
        raise BridgeError(404, "Skill not found.")
    if not skill["builtIn"] or skill["runnerId"] != skill_id:
        raise BridgeError(403, "This skill has no approved read-only operation.")
    if skill["status"] != "enabled":
        raise BridgeError(403, "Enable this built-in skill before running its read-only check.")

    if skill_id == BUILTIN_SKILL_DEFINITIONS[11][0]:
        result = _developer_tool_versions()
    elif skill_id == BUILTIN_SKILL_DEFINITIONS[12][0]:
        result = call_ollama(ollama_url, "/api/tags", None, 10)
    else:
        script = READ_ONLY_SKILL_SCRIPTS.get(skill_id)
        if script is None:
            raise BridgeError(403, "This skill has no approved read-only operation.")
        result = run_powershell_json(script, timeout=20)
    return {
        "skillId": skill_id,
        "checkedAt": datetime.now(timezone.utc).isoformat(),
        "result": result,
    }


def _skill_fields(value: Any, updating: bool) -> dict[str, str]:
    if not isinstance(value, dict):
        raise BridgeError(400, "Skill data must be an object.")
    allowed = {"name", "category", "description", "instructions", "status"}
    if updating:
        allowed.add("id")
    assert_allowed_properties(value, allowed)
    for field in ("name", "category", "instructions", "status"):
        if not isinstance(value.get(field), str) or not value[field].strip():
            raise BridgeError(400, f"Skill {field} is required.")
    description = value.get("description", "")
    if not isinstance(description, str):
        raise BridgeError(400, "Skill description must be text.")
    fields = {
        "name": value["name"].strip(),
        "category": value["category"].strip(),
        "description": description.strip(),
        "instructions": value["instructions"].strip(),
        "status": value["status"].strip(),
    }
    limits = {"name": 120, "category": 80, "description": 2000, "instructions": 20000}
    for field, maximum in limits.items():
        if len(fields[field]) > maximum:
            raise BridgeError(400, f"Skill {field} exceeds the {maximum}-character limit.")
    if fields["status"] not in {"draft", "enabled", "disabled"}:
        raise BridgeError(400, "Skill status must be draft, enabled, or disabled.")
    if updating:
        skill_id = value.get("id")
        try:
            valid_id = isinstance(skill_id, str) and str(uuid.UUID(skill_id)) == skill_id
        except ValueError:
            valid_id = False
        if not valid_id:
            raise BridgeError(400, "A valid Skill ID is required.")
        fields["id"] = skill_id
    return fields


def save_skill(value: Any) -> dict[str, Any]:
    fields = _skill_fields(value, updating=isinstance(value, dict) and "id" in value)
    with SKILLS_LOCK, _skills_write_lock():
        payload = read_skills()
        now = datetime.now(timezone.utc).isoformat()
        if "id" not in fields:
            if len(payload["skills"]) >= MAX_SKILLS:
                raise BridgeError(409, "The Skills limit has been reached.")
            record = {
                **fields,
                "id": str(uuid.uuid4()),
                "builtIn": False,
                "pinned": False,
                "runnerId": None,
                "createdAt": now,
                "updatedAt": now,
                "version": 1,
            }
            payload["skills"].append(record)
        else:
            record = next((item for item in payload["skills"] if item["id"] == fields["id"]), None)
            if record is None:
                raise BridgeError(404, "Skill not found.")
            record.update({key: val for key, val in fields.items() if key != "id"})
            record["updatedAt"] = now
            record["version"] += 1
        _write_skills(payload)
        return record


def delete_skill(skill_id: Any) -> dict[str, bool]:
    try:
        valid_id = isinstance(skill_id, str) and str(uuid.UUID(skill_id)) == skill_id
    except ValueError:
        valid_id = False
    if not valid_id:
        raise BridgeError(400, "A valid Skill ID is required.")
    with SKILLS_LOCK, _skills_write_lock():
        payload = read_skills()
        remaining = [skill for skill in payload["skills"] if skill["id"] != skill_id]
        if len(remaining) == len(payload["skills"]):
            raise BridgeError(404, "Skill not found.")
        payload["skills"] = remaining
        _write_skills(payload)
    return {"ok": True}


def call_ollama(base_url: str, path: str, body: dict[str, Any] | None, timeout: int) -> Any:
    payload = None if body is None else json.dumps(body, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        base_url + path,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="GET" if body is None else "POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read(MAX_REQUEST_BYTES + 1)
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise BridgeError(502, f"Ollama request failed: {exc}") from exc
    if len(raw) > MAX_REQUEST_BYTES:
        raise BridgeError(502, "Ollama response exceeds the 1 MiB limit.")
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BridgeError(502, "Ollama returned invalid JSON.") from exc


def validate_local_address(value: str) -> bool:
    if value.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(value).is_loopback
    except ValueError:
        return False


class BridgeHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 64
    max_workers = 16

    def __init__(
        self,
        address: tuple[str, int],
        handler_class: type[BaseHTTPRequestHandler],
        token: str,
        bootstrap_code: str,
        ollama_url: str,
    ) -> None:
        self.token = token
        self.bootstrap_code = bootstrap_code
        self.bootstrap_expires = time.monotonic() + BOOTSTRAP_TTL_SECONDS
        self.bootstrap_consumed = False
        self.bootstrap_lock = threading.Lock()
        self.ollama_url = normalize_loopback_url(ollama_url)
        self._capacity = threading.BoundedSemaphore(self.max_workers)
        super().__init__(address, handler_class)

    def process_request(self, request: Any, client_address: Any) -> None:
        if not self._capacity.acquire(blocking=False):
            body = b'{"error":"Bridge is busy; retry the request shortly."}'
            response = (
                b"HTTP/1.1 503 Service Unavailable\r\n"
                b"Content-Type: application/json; charset=utf-8\r\n"
                b"Cache-Control: no-store\r\n"
                b"Connection: close\r\n"
                + f"Content-Length: {len(body)}\r\n\r\n".encode("ascii")
                + body
            )
            try:
                request.sendall(response)
            except OSError:
                pass
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except Exception:
            self._capacity.release()
            raise

    def process_request_thread(self, request: Any, client_address: Any) -> None:
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._capacity.release()


class BridgeRequestHandler(BaseHTTPRequestHandler):
    server: BridgeHTTPServer
    protocol_version = "HTTP/1.1"
    server_version = "HelpyardBridge/1.0"
    sys_version = ""

    def log_message(self, format_string: str, *args: Any) -> None:
        message = format_string % args
        print(f"[{self.log_date_time_string()}] {self.client_address[0]} {message}", flush=True)

    def _send_bytes(
        self,
        status: int,
        body: bytes,
        content_type: str,
        extra_headers: dict[str, str] | None = None,
    ) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        if self.close_connection:
            self.send_header("Connection", "close")
        origin = self.headers.get("Origin")
        if origin and origin.lower() != "null":
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Vary", "Origin")
        if extra_headers:
            for name, value in extra_headers.items():
                self.send_header(name, value)
        self.end_headers()
        if self.command != "HEAD" and body:
            self.wfile.write(body)

    def _send_json(self, status: int, value: Any) -> None:
        body = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self._send_bytes(status, body, "application/json; charset=utf-8")

    def _ensure_local(self) -> None:
        remote = self.client_address[0]
        if not validate_local_address(remote):
            raise BridgeError(403, "Only localhost requests are allowed.")

        host = self.headers.get("Host", "")
        if not host or not re.fullmatch(
            r"(localhost|127\.0\.0\.1|\[::1\]|\[::ffff:127\.0\.0\.1\]|::1|::ffff:127\.0\.0\.1)(:\d+)?",
            host.strip(),
            re.IGNORECASE,
        ):
            raise BridgeError(403, "Invalid Host header.")

        origin = self.headers.get("Origin", "")
        if origin and origin.lower() != "null":
            try:
                parsed = urllib.parse.urlsplit(origin)
                valid_origin = (
                    parsed.scheme in {"http", "https"}
                    and parsed.hostname is not None
                    and validate_local_address(parsed.hostname)
                    and parsed.username is None
                    and parsed.password is None
                    and parsed.path in {"", "/"}
                    and not parsed.query
                    and not parsed.fragment
                    and (parsed.port is None or 1 <= parsed.port <= 65535)
                )
            except ValueError:
                valid_origin = False
            if not valid_origin:
                raise BridgeError(403, "Invalid Origin header.")

    def _authorize(self) -> None:
        authorization = self.headers.get("Authorization", "").strip()
        if authorization.lower().startswith("bearer "):
            authorization = authorization[7:].strip()
        if not authorization or not hmac.compare_digest(authorization, self.server.token):
            raise BridgeError(401, "Unauthorized: wrong or missing token.")

    def _read_body(self) -> dict[str, Any]:
        return read_json_body(self)

    def _read_route(self) -> Any:
        path = urllib.parse.urlsplit(self.path).path
        if path == "/api/ping":
            return {"ok": True, "admin": is_admin(), "host": os.environ.get("COMPUTERNAME", ""), "user": os.environ.get("USERNAME", "")}
        if path == "/api/health":
            data = run_powershell_json(
                "$os = Get-CimInstance Win32_OperatingSystem; "
                "[ordered]@{ ok = $true; admin = $false; host = $env:COMPUTERNAME; "
                "user = $env:USERNAME; os = $os.Caption; build = $os.BuildNumber; "
                "uptimeHours = [math]::Round(((Get-Date) - $os.LastBootUpTime).TotalHours, 1); "
                "time = (Get-Date).ToString('o') }"
            )
            data["admin"] = is_admin()
            return data
        if path == "/api/actions":
            return list(ACTION_REGISTRY.values())
        if path == "/api/projects":
            return get_projects()
        if path == "/api/skills":
            return read_skills()
        if path == "/api/stats":
            return run_powershell_json(
                "$os = Get-CimInstance Win32_OperatingSystem; "
                "$processors = @(Get-CimInstance Win32_Processor); "
                "$cpu = ($processors | Measure-Object LoadPercentage -Average).Average; "
                "$cpuName = ($processors | Select-Object -First 1).Name; "
                "$cpuCores = ($processors | Measure-Object NumberOfCores -Sum).Sum; "
                "$cpuThreads = ($processors | Measure-Object NumberOfLogicalProcessors -Sum).Sum; "
                "$cpuMaxMHz = ($processors | Measure-Object MaxClockSpeed -Maximum).Maximum; "
                "$disks = @(Get-CimInstance Win32_LogicalDisk -Filter 'DriveType=3' | ForEach-Object { "
                "@{ name = $_.DeviceID; totalGB = [math]::Round($_.Size / 1GB, 1); "
                "freeGB = [math]::Round($_.FreeSpace / 1GB, 1) } }); "
                "[ordered]@{ host = $env:COMPUTERNAME; user = $env:USERNAME; admin = $false; "
                "os = $os.Caption; build = $os.BuildNumber; cpuName = $cpuName; cpu = [int]$cpu; "
                "cpuCores = [int]$cpuCores; cpuThreads = [int]$cpuThreads; cpuMaxMHz = [int]$cpuMaxMHz; "
                "ramTotalGB = [math]::Round($os.TotalVisibleMemorySize / 1MB, 1); "
                "ramFreeGB = [math]::Round($os.FreePhysicalMemory / 1MB, 1); "
                "uptimeHours = [math]::Round(((Get-Date) - $os.LastBootUpTime).TotalHours, 1); "
                "disks = $disks; processCount = (Get-Process).Count }"
            )
        if path == "/api/processes":
            return run_powershell_json(
                "@(Get-Process | Sort-Object WorkingSet64 -Descending | Select-Object -First 60 | "
                "ForEach-Object { $cpu = 0; if ($null -ne $_.CPU) { $cpu = [math]::Round([double]$_.CPU, 1) }; "
                "@{ id = $_.Id; name = $_.ProcessName; cpu = $cpu; memMB = [math]::Round($_.WorkingSet64 / 1MB, 0) } })"
            )
        if path == "/api/services":
            return run_powershell_json(
                "@(Get-Service | ForEach-Object { "
                "@{ name = $_.Name; display = $_.DisplayName; status = [string]$_.Status; start = [string]$_.StartType } })"
            )
        if path == "/api/mcp/status":
            return []
        if path == "/api/ollama/tags":
            return call_ollama(self.server.ollama_url, "/api/tags", None, 10)
        if path == "/api/claude-config":
            return get_claude_config()
        raise BridgeError(404, f"Not found: GET {path}")

    def _post_route(self) -> Any:
        path = urllib.parse.urlsplit(self.path).path
        body = self._read_body()

        if path == "/api/bootstrap":
            assert_allowed_properties(body, {"code"})
            origin = self.headers.get("Origin", "")
            expected_origin = f"http://127.0.0.1:{self.server.server_port}"
            code = body.get("code")
            if not isinstance(code, str) or not origin or origin.rstrip("/") != expected_origin:
                raise BridgeError(403, "A same-origin browser session is required for connection setup.")
            with self.server.bootstrap_lock:
                if time.monotonic() >= self.server.bootstrap_expires:
                    raise BridgeError(410, "The browser connection code has expired. Restart the launcher.")
                if self.server.bootstrap_consumed or not hmac.compare_digest(code, self.server.bootstrap_code):
                    raise BridgeError(401, "The browser connection code is invalid or already used.")
                self.server.bootstrap_consumed = True
                self.server.bootstrap_code = ""
            return {"token": self.server.token}

        self._authorize()

        if path == "/api/skills":
            assert_allowed_properties(body, {"action", "skill", "id", "pinned"})
            action = body.get("action")
            if action == "create":
                skill = body.get("skill")
                if set(body) != {"action", "skill"}:
                    raise BridgeError(400, "Skill creation requires only an action and skill object.")
                if not isinstance(skill, dict):
                    raise BridgeError(400, "Skill data must be an object.")
                if "id" in skill:
                    raise BridgeError(400, "New Skills cannot specify an ID.")
                return {"skill": save_skill(skill)}
            if action == "update":
                skill = body.get("skill")
                if set(body) != {"action", "skill"} or not isinstance(skill, dict) or "id" not in skill:
                    raise BridgeError(400, "Skill updates require an action and an identified skill object.")
                return {"skill": save_skill(skill)}
            if action == "delete":
                if set(body) != {"action", "id"}:
                    raise BridgeError(400, "Skill deletion requires only an action and skill ID.")
                return delete_skill(body.get("id"))
            if action == "pin":
                if set(body) != {"action", "id", "pinned"}:
                    raise BridgeError(400, "Skill pinning requires an action, skill ID, and pinned state.")
                return {"skill": set_skill_pinned(body.get("id"), body.get("pinned"))}
            if action == "run":
                if set(body) != {"action", "id"}:
                    raise BridgeError(400, "Running a skill requires only an action and skill ID.")
                return run_skill_check(body.get("id"), self.server.ollama_url)
            raise BridgeError(400, "A supported Skill action is required.")

        if path == "/api/process/stop":
            assert_allowed_properties(body, {"id", "approved"})
            process_id = body.get("id")
            if isinstance(process_id, bool) or not isinstance(process_id, int) or process_id <= 0:
                raise BridgeError(400, "A valid process ID is required.")
            assert_approved(body, "Stopping a process requires explicit approval.")
            if process_id == os.getpid():
                raise BridgeError(400, "The bridge process cannot stop itself.")
            run_powershell(
                f"Stop-Process -Id {process_id} -Force -ErrorAction Stop; $result = 'Process stopped'"
            )
            return {"ok": True}

        if path == "/api/service":
            assert_allowed_properties(body, {"name", "action", "approved"})
            name = body.get("name")
            action = body.get("action")
            if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", name):
                raise BridgeError(400, "A valid exact service name is required.")
            if action not in {"start", "stop", "restart"}:
                raise BridgeError(400, "Unknown service action.")
            assert_approved(body, "Changing a service requires explicit approval.")
            operation = {"start": "Start-Service", "stop": "Stop-Service", "restart": "Restart-Service"}[action]
            run_powershell(
                f"$service = Get-Service -Name {quote_powershell(name)} -ErrorAction Stop; "
                f"{operation} -InputObject $service -Force -ErrorAction Stop; $result = 'Service updated'"
            )
            return {"ok": True}

        if path == "/api/action":
            assert_allowed_properties(body, {"name", "approved"})
            name = body.get("name")
            if not isinstance(name, str) or not name:
                raise BridgeError(400, "Action name is required.")
            if name not in ACTION_REGISTRY:
                raise BridgeError(400, f"Unknown action '{name}'.")
            rule = ACTION_REGISTRY[name]
            if rule["permission"] == "admin" and not is_admin():
                raise BridgeError(403, f"Action '{name}' requires administrator rights.")
            if rule["requiresApproval"]:
                assert_approved(body, f"Action '{name}' requires explicit approval before execution.")
            output = run_powershell(ACTION_SCRIPTS[name], timeout=int(rule["timeoutSec"])).strip()
            return {
                "ok": True,
                "output": output or "Action completed.",
                "action": name,
                "permission": rule["permission"],
                "risk": rule["risk"],
            }

        if path == "/api/run":
            raise BridgeError(403, "Arbitrary PowerShell execution is disabled. Use reviewed named operations instead.")

        if path == "/api/ollama/chat":
            assert_allowed_properties(body, {"model", "messages", "stream", "options"})
            model = body.get("model")
            messages = body.get("messages")
            if not isinstance(model, str) or not model.strip():
                raise BridgeError(400, "Ollama model is required.")
            if not isinstance(messages, list) or not messages:
                raise BridgeError(400, "Ollama messages are required.")
            for message in messages:
                if (
                    not isinstance(message, dict)
                    or message.get("role") not in {"system", "user", "assistant"}
                    or not isinstance(message.get("content"), str)
                ):
                    raise BridgeError(400, "Ollama messages must be objects with a supported role and text content.")
            request_body = dict(body)
            request_body["stream"] = False
            return call_ollama(self.server.ollama_url, "/api/chat", request_body, 600)

        if path == "/api/mcp/start":
            raise BridgeError(403, "MCP startup is disabled until a reviewed server registry is configured.")

        if path == "/api/mcp/stop":
            assert_allowed_properties(body, {"id"})
            return {"ok": True}

        if path == "/api/claude-config":
            assert_allowed_properties(body, {"content", "approved"})
            assert_approved(body, "Updating Claude configuration requires explicit approval.")
            return set_claude_config(body.get("content"))

        raise BridgeError(404, f"Not found: POST {path}")

    def _handle(self, callback: Any) -> None:
        try:
            self._ensure_local()
            value = callback()
            if isinstance(value, tuple) and len(value) == 2 and isinstance(value[0], int):
                self._send_json(value[0], value[1])
            else:
                self._send_json(200, value)
        except BridgeError as exc:
            self._send_json(exc.status, {"error": str(exc)})
        except (BrokenPipeError, ConnectionResetError):
            return
        except Exception as exc:
            self.log_error("Request failed (%s): %s", type(exc).__name__, exc)
            self._send_json(500, {"error": "The bridge could not complete the request. Check its console for details."})

    def do_OPTIONS(self) -> None:
        try:
            self._ensure_local()
            self._send_bytes(
                int(HTTPStatus.NO_CONTENT),
                b"",
                "application/json; charset=utf-8",
                {
                    "Access-Control-Allow-Headers": "Authorization, Content-Type",
                    "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
                },
            )
        except BridgeError as exc:
            self._send_json(exc.status, {"error": str(exc)})

    def do_GET(self) -> None:
        path = urllib.parse.urlsplit(self.path).path
        if path in {"/", "/pc-control-dashboard.html"}:
            try:
                self._ensure_local()
                dashboard = DASHBOARD_PATH.read_text(encoding="utf-8-sig")
                self._send_bytes(200, dashboard.encode("utf-8"), "text/html; charset=utf-8")
            except OSError as exc:
                self._send_json(500, {"error": f"Could not read the dashboard: {exc}"})
            except BridgeError as exc:
                self._send_json(exc.status, {"error": str(exc)})
            return
        if path == "/api/bridge-probe":
            try:
                self._ensure_local()
                self._send_bytes(int(HTTPStatus.NO_CONTENT), b"", "application/json; charset=utf-8")
            except BridgeError as exc:
                self._send_json(exc.status, {"error": str(exc)})
            return

        def callback() -> Any:
            self._authorize()
            return self._read_route()

        self._handle(callback)

    def do_POST(self) -> None:
        self._handle(self._post_route)


def create_server(
    port: int,
    token: str | None = None,
    bootstrap_code: str | None = None,
    ollama_url: str = DEFAULT_OLLAMA_URL,
) -> BridgeHTTPServer:
    if not DASHBOARD_PATH.is_file():
        raise FileNotFoundError(f"Dashboard file not found: {DASHBOARD_PATH}")
    failures: list[str] = []
    candidates = (port,) + tuple(candidate for candidate in PORT_CANDIDATES if candidate != port)
    for candidate in candidates:
        try:
            server = BridgeHTTPServer(
                ("127.0.0.1", candidate),
                BridgeRequestHandler,
                token or secrets.token_urlsafe(32),
                bootstrap_code or secrets.token_urlsafe(32),
                ollama_url,
            )
            if candidate != port:
                print(f"Port {port} was busy; using 127.0.0.1:{candidate}.", flush=True)
            return server
        except OSError as exc:
            failures.append(f"{candidate}: {exc}")
    raise OSError("No free localhost port was available. " + "; ".join(failures))


def acquire_single_instance() -> Any:
    if os.name != "nt":
        return None
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateMutexW.argtypes = [wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR]
    kernel32.CreateMutexW.restype = wintypes.HANDLE
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    mutex_name = "Local\\Helpyard-" + hashlib.sha256(str(ROOT).casefold().encode("utf-8")).hexdigest()[:24]
    ctypes.set_last_error(0)
    handle = kernel32.CreateMutexW(None, False, mutex_name)
    if not handle:
        raise OSError(ctypes.get_last_error(), "Could not create the Helpyard instance lock.")
    if ctypes.get_last_error() == 183:
        kernel32.CloseHandle(handle)
        return False
    return handle


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the loopback-only Helpyard dashboard and API.")
    parser.add_argument("--port", type=int, default=8787)
    parser.add_argument("--ollama-url", default=DEFAULT_OLLAMA_URL)
    parser.add_argument("--open-browser", action="store_true")
    args = parser.parse_args(argv)
    if not 1 <= args.port <= 65535:
        parser.error("--port must be between 1 and 65535.")

    instance_handle = acquire_single_instance()
    if instance_handle is False:
        print("Helpyard is already running for this workspace. Use its open dashboard window or stop it first.")
        return 0

    server: BridgeHTTPServer | None = None
    serve_started = False
    try:
        server = create_server(args.port, ollama_url=args.ollama_url)
        dashboard_url = f"http://127.0.0.1:{server.server_port}/pc-control-dashboard.html"
        launch_url = dashboard_url + "#connect=" + server.bootstrap_code
        print("Helpyard Python bridge is running.", flush=True)
        print(f"Bridge URL : http://127.0.0.1:{server.server_port}", flush=True)
        print(f"Dashboard  : {dashboard_url}", flush=True)
        print("Frontend   : HTML / CSS / JavaScript", flush=True)
        print("Runtime    : Python standard library; no pip packages required", flush=True)
        print("Binding    : 127.0.0.1 only", flush=True)
        print("Press Ctrl+C or close this minimized window to stop.", flush=True)
        if args.open_browser and not webbrowser.open(launch_url):
            print("Could not open a browser automatically.", flush=True)
            print(f"Connection URL (valid for 60 seconds): {launch_url}", flush=True)
        serve_started = True
        server.serve_forever(poll_interval=0.2)
    except KeyboardInterrupt:
        print("\nStopping Helpyard bridge.", flush=True)
    except (OSError, ValueError, BridgeError) as exc:
        print(f"Helpyard failed to start: {exc}", file=sys.stderr, flush=True)
        return 1
    finally:
        if server is not None:
            if serve_started:
                server.shutdown()
            server.server_close()
        if instance_handle not in (None, False):
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel32.ReleaseMutex.argtypes = [wintypes.HANDLE]
            kernel32.ReleaseMutex.restype = wintypes.BOOL
            kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
            kernel32.CloseHandle.restype = wintypes.BOOL
            kernel32.ReleaseMutex(instance_handle)
            kernel32.CloseHandle(instance_handle)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
