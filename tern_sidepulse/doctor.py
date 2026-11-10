"""Doctor: runtime/contract compatibility, coordinator health and receiver transport, read-only.

An acknowledged report means SidePulse accepted the row, never that an LED changed.
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
from .storage import is_disabled, load_identity

LEVELS = {"ok": 0, "warn": 1, "error": 2}


def _check(checks: list, id_: str, level: str, detail: str, remedy: str | None = None) -> None:
    item = {"id": id_, "level": level, "detail": detail}
    if remedy:
        item["remedy"] = remedy
    checks.append(item)


def run_doctor(settings: Settings, window_diag: dict | None = None, window_key: str | None = None, inert: str | None = None) -> dict:
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
            _check(checks, "identity", "ok", f"install {str(identity['install_id'])[:8]}…")
        if is_disabled(data_dir):
            _check(checks, "latch", "warn", "disabled (latch file present)", "run the tern-sidepulse: enable command")
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
        modes = ", ".join(f"{mode} {count}" for mode, count in status.get("rows", {}).items()) or "none"
        _check(
            checks,
            "coordinator",
            "ok",
            f"pid {status.get('pid')} epoch {status.get('epoch')} up {status.get('uptime_seconds')}s, "
            f"rows {status.get('rows_total')} ({modes}), {status.get('rows_labelled')} labelled, "
            f"{len(observers)} observer(s), disabled={status.get('disabled')}",
        )
        if window_key is not None:
            mine = f"{os.getppid()}:{window_key}"
            if any(observer.get("key") == mine for observer in observers):
                _check(checks, "observer", "ok", f"this window is observer {mine}")
            else:
                _check(checks, "observer", "warn", f"this window ({mine}) is not a live observer yet", "wait one reconciliation (5 s) or run resync; a changing parent pid means a shim interpreter, set python in config.json")
    transport = status.get("transport") if status is not None else None
    present = next((path for path in settings.program_sockets if path.exists()), None)
    if present is None:
        # 0.1.0's receiver sat next to where program-status.sock belongs; SidePulse removed it with #72.
        legacy = next((p.parent / "relay-events.sock" for p in settings.program_sockets if (p.parent / "relay-events.sock").exists()), None)
        if legacy is not None:
            _check(checks, "receiver", "error", f"{legacy} exists but no program-status.sock: this SidePulse predates program status", "update SidePulse (`sidepulse update`); tern-sidepulse needs a SidePulse that includes inteliwear/sidepulse#72")
        else:
            _check(checks, "receiver", "warn", "no program-status.sock: " + ", ".join(str(path) for path in settings.program_sockets), "start SidePulse (`sidepulse service start`, or open the menu-bar app)")
    elif isinstance(transport, dict) and transport.get("last_error") and transport.get("pending"):
        _check(checks, "receiver", "error", f"delivery failing: {transport['last_error']}", "restart SidePulse; the coordinator retries with backoff")
    elif isinstance(transport, dict):
        labels = "" if transport.get("labels") else ", labels off"
        _check(checks, "receiver", "ok", f"{transport.get('rows')} row(s) via {transport.get('target') or present}, last accepted {transport.get('delivered_wall')}{labels}")
        if transport.get("rejected"):
            _check(checks, "receiver_rejections", "warn", f"SidePulse rejected {transport['rejected']} report(s)", "see coordinator.log; a SidePulse or tern-sidepulse update may be needed")
    else:
        _check(checks, "receiver", "ok", f"{present} (no coordinator running)")
    for directory in sidepulse_state_dirs():
        _check(checks, "sidepulse_state", "ok" if directory.is_dir() else "warn", str(directory), None if directory.is_dir() else "install SidePulse")
        break

    if inert is not None and inert.isprintable() and len(inert) <= 120:
        _check(checks, "window", "error", f"observer inert: {inert}", "this Tern build lacks a window API the plugin needs")
    diag = parse_diag(window_diag)
    if diag:
        _check(checks, "window", "ok", ", ".join(f"{key}={value}" for key, value in sorted(diag.items())))

    worst = max((LEVELS[item["level"]] for item in checks), default=0)
    level = next(name for name, value in LEVELS.items() if value == worst)
    summary = f"{level}: {sum(1 for c in checks if c['level'] != 'ok')} issue(s); an accepted report is not an LED check"
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
