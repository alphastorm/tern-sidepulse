"""Command line used by the window observer (register, snapshot) and by people (status, doctor, enable, disable, resync).

Exactly one JSON object is printed to stdout per invocation. Exit codes: 0 when a JSON response
was produced (including protocol rejections), 2 for a usage error, 1 for an internal error.
"""
from __future__ import annotations

import argparse
import errno
import json
import os
import sys
from pathlib import Path

from . import PROTOCOL_SCHEMA, VERSION
from .client import CoordinatorUnavailable, Settings, request
from .coordinator import Limits
from .paths import runtime_dir, sidepulse_service_sockets, sidepulse_ui_sockets
from .storage import is_disabled, load_config, set_disabled

MAX_STDIN_BYTES = 256 * 1024
SUBCOMMANDS = ("register", "snapshot", "status", "enable", "disable", "resync", "doctor", "coordinator")


class UsageError(Exception):
    pass


def _emit(body: dict) -> None:
    sys.stdout.write(json.dumps(body, separators=(",", ":"), sort_keys=True) + "\n")
    sys.stdout.flush()


def _paths_from(values: list[str] | None, env_name: str, config: dict, key: str, default: tuple[Path, ...]) -> tuple[Path, ...]:
    if values:
        return tuple(Path(v).expanduser() for v in values)
    env = os.environ.get(env_name)
    if env:
        return tuple(Path(v).expanduser() for v in env.split(os.pathsep) if v)
    configured = config.get(key)
    if isinstance(configured, list) and all(isinstance(v, str) for v in configured) and configured:
        return tuple(Path(v).expanduser() for v in configured)
    return default


def resolve_settings(args: argparse.Namespace, bridge_path: Path) -> Settings:
    """Precedence: flag > environment > <data>/config.json > default."""
    data_dir = args.data_dir or os.environ.get("TERN_PLUGIN_DATA")
    data_path = Path(data_dir).expanduser() if data_dir else None
    config = load_config(data_path) if data_path else {}
    runtime_value = args.runtime_dir or os.environ.get("TERN_SIDEPULSE_RUNTIME_DIR")
    runtime = runtime_dir(Path(runtime_value).expanduser()) if runtime_value else runtime_dir()
    limits_json = args.limits or os.environ.get("TERN_SIDEPULSE_LIMITS")
    try:
        limits = Limits.from_mapping(json.loads(limits_json)) if limits_json else Limits()
    except ValueError as exc:
        raise UsageError(f"bad limits: {exc}") from exc
    return Settings(
        data_dir=data_path,
        runtime_dir=runtime,
        ui_sockets=_paths_from(args.ui_socket, "TERN_SIDEPULSE_UI_SOCKETS", config, "ui_sockets", sidepulse_ui_sockets()),
        service_sockets=_paths_from(args.service_socket, "TERN_SIDEPULSE_SERVICE_SOCKETS", config, "service_sockets", sidepulse_service_sockets()),
        limits=limits,
        bridge_path=bridge_path,
        limits_json=limits_json,
    )


def read_stdin_json() -> dict:
    raw = sys.stdin.buffer.read(MAX_STDIN_BYTES + 1)
    if len(raw) > MAX_STDIN_BYTES:
        raise UsageError("stdin exceeds the size limit")
    if not raw.strip():
        return {}
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise UsageError("stdin is not valid JSON") from exc
    if not isinstance(data, dict):
        raise UsageError("stdin must be a JSON object")
    return data


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="bridge.py", description="tern-sidepulse helper")
    parser.add_argument("command", choices=SUBCOMMANDS)
    parser.add_argument("--data-dir", help="Tern plugin data directory (default: $TERN_PLUGIN_DATA)")
    parser.add_argument("--runtime-dir", help="bridge runtime directory (default: ~/.local/state/tern-sidepulse)")
    parser.add_argument("--ui-socket", action="append", help="SidePulse app socket candidate (repeatable)")
    parser.add_argument("--service-socket", action="append", help="SidePulse service socket candidate (repeatable)")
    parser.add_argument("--limits", help="JSON object overriding timing limits (tests)")
    return parser


def require_data_dir(settings: Settings) -> Path:
    if settings.data_dir is None:
        raise UsageError("--data-dir (or TERN_PLUGIN_DATA) is required for this command")
    return settings.data_dir


def observer_payload(window: dict, op: str) -> dict:
    raw_observer = window.get("observer")
    observer: dict = raw_observer if isinstance(raw_observer, dict) else {}
    window_key = observer.get("window_key", os.environ.get("TERN_WINDOW_KEY", ""))
    payload = dict(window)
    payload["schema"] = PROTOCOL_SCHEMA
    payload["op"] = op
    payload["observer"] = {"parent_pid": os.getppid(), "window_key": window_key if isinstance(window_key, str) else ""}
    if op == "register":
        payload["client_version"] = VERSION
    return payload


def main(argv: list[str], bridge_path: Path) -> int:
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        _emit({"ok": False, "error": "usage", "detail": "bad arguments"})
        return 2 if exc.code else 0
    try:
        settings = resolve_settings(args, bridge_path)
        command = args.command
        if command == "coordinator":
            from .server import run_coordinator

            data_dir = require_data_dir(settings)
            return run_coordinator(
                runtime=settings.runtime_dir,
                data_dir=data_dir,
                limits=settings.limits,
                ui_sockets=settings.ui_sockets,
                service_sockets=settings.service_sockets,
            )
        if command in ("register", "snapshot"):
            data_dir = require_data_dir(settings)
            window = read_stdin_json()
            if is_disabled(data_dir):
                _emit({"ok": False, "error": "disabled", "detail": "the bridge is disabled; run enable first"})
                return 0
            _emit(request(settings, observer_payload(window, command), spawn=True))
            return 0
        if command == "status":
            _emit(request(settings, {"schema": PROTOCOL_SCHEMA, "op": "status"}, spawn=False, total_timeout=1.0))
            return 0
        if command == "resync":
            _emit(request(settings, {"schema": PROTOCOL_SCHEMA, "op": "resync"}, spawn=True))
            return 0
        if command == "disable":
            data_dir = require_data_dir(settings)
            latch = True
            try:
                set_disabled(data_dir, True)
            except OSError:
                latch = False
            response = request(settings, {"schema": PROTOCOL_SCHEMA, "op": "disable"}, spawn=True)
            response["latch_persisted"] = latch
            _emit(response)
            return 0
        if command == "enable":
            data_dir = require_data_dir(settings)
            set_disabled(data_dir, False)
            try:
                response = request(settings, {"schema": PROTOCOL_SCHEMA, "op": "enable"}, spawn=False, total_timeout=1.0)
                response["coordinator"] = "running"
            except CoordinatorUnavailable:
                response = {"ok": True, "disabled": False, "coordinator": "not_running"}
            _emit(response)
            return 0
        if command == "doctor":
            from .doctor import run_doctor

            window = read_stdin_json() if not sys.stdin.isatty() else {}
            window_key = window.get("window_key") if isinstance(window.get("window_key"), str) else os.environ.get("TERN_WINDOW_KEY")
            _emit(run_doctor(settings, window.get("diag"), window_key))
            return 0
        raise UsageError("unknown command")
    except UsageError as exc:
        _emit({"ok": False, "error": "usage", "detail": str(exc)})
        return 2
    except CoordinatorUnavailable as exc:
        _emit({"ok": False, "error": "coordinator_unavailable", "detail": exc.reason})
        return 0
    except Exception as exc:  # noqa: BLE001 - the window needs one JSON line, never a traceback
        _emit({"ok": False, "error": "internal", "detail": type(exc).__name__, "errno": errno.errorcode.get(getattr(exc, "errno", 0) or 0)})
        return 1
