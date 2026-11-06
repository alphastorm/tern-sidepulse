"""Command line used by the window observer (register, snapshot) and by people (status, doctor, enable, disable, resync).

Exactly one JSON object is printed to stdout per invocation. Exit codes: 0 when a JSON response
was produced (including protocol rejections), 2 for a usage error, 1 for an internal error.
"""
from __future__ import annotations

import errno
import json
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path

from . import PROTOCOL_SCHEMA, VERSION
from .client import CoordinatorUnavailable, Settings, request
from .jsonio import load_json
from .limits import Limits
from .paths import runtime_dir, sidepulse_service_sockets, sidepulse_ui_sockets
from .storage import is_disabled, load_config, set_disabled

MAX_STDIN_BYTES = 256 * 1024
SUBCOMMANDS = ("register", "snapshot", "status", "enable", "disable", "resync", "doctor", "coordinator")
SINGLE_OPTIONS = {"--data-dir": "data_dir", "--runtime-dir": "runtime_dir", "--limits": "limits"}
REPEATED_OPTIONS = {"--ui-socket": "ui_socket", "--service-socket": "service_socket"}


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


def resolve_settings(args: Args, bridge_path: Path) -> Settings:
    """Precedence: flag > environment > <data>/config.json > default."""
    data_dir = args.data_dir or os.environ.get("TERN_PLUGIN_DATA")
    data_path = Path(data_dir).expanduser() if data_dir else None
    config = load_config(data_path) if data_path else {}
    runtime_value = args.runtime_dir or os.environ.get("TERN_SIDEPULSE_RUNTIME_DIR")
    runtime = runtime_dir(Path(runtime_value).expanduser()) if runtime_value else runtime_dir()
    limits_json = args.limits or os.environ.get("TERN_SIDEPULSE_LIMITS")
    try:
        limits = Limits.from_mapping(load_json(limits_json.encode("utf-8"))) if limits_json else Limits()
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
        data = load_json(raw)
    except ValueError as exc:
        raise UsageError("stdin is not valid JSON") from exc
    if not isinstance(data, dict):
        raise UsageError("stdin must be a JSON object")
    return data


@dataclass
class Args:
    """``bridge.py <command> [--data-dir D] [--runtime-dir R] [--ui-socket S]… [--service-socket S]… [--limits JSON]``."""

    command: str
    data_dir: str | None = None
    runtime_dir: str | None = None
    limits: str | None = None
    ui_socket: list[str] = field(default_factory=list)
    service_socket: list[str] = field(default_factory=list)


def parse_args(argv: list[str]) -> Args:
    """A fixed grammar instead of argparse, which costs the helper more than its own work."""
    if not argv or argv[0] not in SUBCOMMANDS:
        raise UsageError("command must be one of " + ", ".join(SUBCOMMANDS))
    args = Args(argv[0])
    rest = argv[1:]
    while rest:
        flag = rest[0]
        if len(rest) < 2 or flag not in SINGLE_OPTIONS and flag not in REPEATED_OPTIONS:
            raise UsageError("bad arguments")
        value, rest = rest[1], rest[2:]
        if flag in SINGLE_OPTIONS:
            setattr(args, SINGLE_OPTIONS[flag], value)
        else:
            getattr(args, REPEATED_OPTIONS[flag]).append(value)
    return args


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
    try:
        args = parse_args(argv)
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
                _emit({"ok": False, "error": "disabled", "detail": "tern-sidepulse is disabled; run enable first"})
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
            inert = window.get("inert")
            _emit(run_doctor(settings, window.get("diag"), window_key, inert if isinstance(inert, str) else None))
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
