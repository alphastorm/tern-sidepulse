"""Doctor: runtime/contract compatibility, coordinator health and receiver transport, read-only.

A successful socket write is reported as delivery, never as an LED check.
"""
from __future__ import annotations

import json
import os
import stat
import sys

from . import PROTOCOL_SCHEMA, VERSION
from .client import CoordinatorUnavailable, Settings, request
from .coordinator import parse_diag
from .paths import DOCTOR_NAME, MAX_SOCKET_PATH_BYTES, PLUGIN_ID, SOCKET_NAME, TEMP_SOCKET_SUFFIX_MAX, ensure_private_dir, sidepulse_state_dirs
from .storage import is_disabled, load_identity, session_id_for

LEVELS = {"ok": 0, "warn": 1, "error": 2}


def _check(checks: list, id_: str, level: str, detail: str, remedy: str | None = None) -> None:
    item = {"id": id_, "level": level, "detail": detail}
    if remedy:
        item["remedy"] = remedy
    checks.append(item)


def run_doctor(settings: Settings, window_diag: dict | None = None, window_key: str | None = None) -> dict:
    checks: list[dict] = []
    _check(checks, "python", "ok" if sys.version_info >= (3, 10) else "error", f"{sys.executable} {sys.version.split()[0]}")
    _check(checks, "bridge", "ok", f"{PLUGIN_ID} {VERSION}, protocol schema {PROTOCOL_SCHEMA}, {settings.bridge_path}")

    data_dir = settings.data_dir
    if data_dir is None:
        _check(checks, "data_dir", "warn", "no data directory given", "run from the plugin, or pass --data-dir")
    else:
        plugin_dir = settings.bridge_path.resolve().parent
        inside_plugin = str(data_dir.resolve()).startswith(str(plugin_dir) + os.sep)
        if inside_plugin:
            _check(checks, "data_dir", "error", f"{data_dir} is inside the plugin package", "Tern reloads on every write there; use tern.plugin.data")
        elif not data_dir.is_dir():
            _check(checks, "data_dir", "warn", f"{data_dir} does not exist yet (created on first use)")
        elif not os.access(data_dir, os.W_OK):
            _check(checks, "data_dir", "error", f"{data_dir} is not writable")
        else:
            _check(checks, "data_dir", "ok", str(data_dir))
        identity = load_identity(data_dir, create=False)
        if identity is None:
            _check(checks, "identity", "warn", "no identity yet (created when the coordinator first starts)")
        else:
            _check(checks, "identity", "ok", f"session {session_id_for(identity)[:24]}…")
        if is_disabled(data_dir):
            _check(checks, "latch", "warn", "bridge disabled", "run the SidePulse: Enable command")
        else:
            _check(checks, "latch", "ok", "enabled")

    runtime = settings.runtime_dir
    try:
        ensure_private_dir(runtime)
        mode = stat.S_IMODE(os.stat(runtime).st_mode)
        longest = len(str(runtime / SOCKET_NAME).encode()) + TEMP_SOCKET_SUFFIX_MAX
        _check(checks, "runtime_dir", "ok", f"{runtime} (mode {mode:o}, socket path budget {longest}/{MAX_SOCKET_PATH_BYTES})")
    except OSError as exc:
        _check(checks, "runtime_dir", "error", f"{runtime}: {exc.strerror or type(exc).__name__}")

    status = None
    try:
        response = request(settings, {"schema": PROTOCOL_SCHEMA, "op": "status"}, spawn=False, total_timeout=1.0)
        status = response.get("status") if response.get("ok") else None
        if status is None:
            _check(checks, "coordinator", "warn", f"status refused: {response.get('error')}")
    except CoordinatorUnavailable as exc:
        _check(checks, "coordinator", "warn" if exc.reason == "not_running" else "error", f"not running ({exc.reason}); it starts with the next window snapshot")
    if status is not None:
        observers = status.get("observers", [])
        _check(
            checks,
            "coordinator",
            "ok",
            f"pid {status.get('pid')} epoch {status.get('epoch')} up {status.get('uptime_seconds')}s, desired {status.get('desired')}, {len(observers)} observer(s), disabled={status.get('disabled')}",
        )
        if window_key is not None:
            mine = f"{os.getppid()}:{window_key}"
            if any(observer.get("key") == mine for observer in observers):
                _check(checks, "observer", "ok", f"this window is observer {mine}")
            else:
                _check(checks, "observer", "warn", f"this window ({mine}) is not a live observer yet", "wait one reconciliation (5 s) or run resync; a changing parent pid means a shim interpreter, set python in config.json")
        for family in status.get("transport", {}).get("families", []):
            present = family.get("receiver_present")
            delivered = family.get("delivered_mode")
            error = family.get("last_error")
            if not present:
                _check(checks, f"receiver_{family['name']}", "warn", "no socket file", "start SidePulse (menu-bar app or `sidepulse service`)")
            elif error and family.get("pending"):
                _check(checks, f"receiver_{family['name']}", "error", f"delivery failing: {error}", "restart SidePulse; the bridge retries with backoff")
            else:
                _check(checks, f"receiver_{family['name']}", "ok", f"delivered {delivered} at {family.get('delivered_wall')} via {family.get('delivered_target')}")
    for directory in sidepulse_state_dirs():
        _check(checks, "sidepulse_state", "ok" if directory.is_dir() else "warn", str(directory), None if directory.is_dir() else "install SidePulse's agent monitor")
        break

    diag = parse_diag(window_diag)
    if diag:
        _check(checks, "window", "ok", ", ".join(f"{key}={value}" for key, value in sorted(diag.items())))

    worst = max((LEVELS[item["level"]] for item in checks), default=0)
    level = next(name for name, value in LEVELS.items() if value == worst)
    summary = f"{level}: {sum(1 for c in checks if c['level'] != 'ok')} issue(s); socket delivery is not an LED check"
    report = {
        "ok": True,
        "level": level,
        "summary": summary,
        "checks": checks,
        "status": status,
        "window_diag": diag,
    }
    try:
        ensure_private_dir(runtime)
        path = runtime / DOCTOR_NAME
        path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        os.chmod(path, 0o600)
        report["report"] = str(path)
    except OSError:
        report["report"] = None
    return report
