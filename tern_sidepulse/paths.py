"""Where the bridge keeps its runtime files and where SidePulse listens.

Two directories matter:

* the Tern plugin data directory (``TERN_PLUGIN_DATA`` / ``--data-dir``): the stable
  bridge identity, configuration and the disabled latch, which Tern keeps across plugin
  reinstalls;
* the bridge runtime directory: the coordinator socket, its singleton lock, the log and the
  doctor report. It is user-private, derived from ``HOME`` only (a Dock-launched Tern and a
  terminal must agree on it), and short enough for a macOS ``sun_path``.
"""
from __future__ import annotations

import hashlib
import os
import stat
from pathlib import Path

PLUGIN_ID = "tern-sidepulse"
SOCKET_NAME = "coordinator.sock"
LOCK_NAME = "coordinator.lock"
LOG_NAME = "coordinator.log"
DOCTOR_NAME = "doctor.json"
IDENTITY_NAME = "identity.json"
CONFIG_NAME = "config.json"
DISABLED_NAME = "disabled"
MAX_SOCKET_PATH_BYTES = 100  # macOS sun_path holds 104 bytes including the terminator.
TEMP_SOCKET_SUFFIX_MAX = 11  # "." plus up to ten pid digits while binding.


def runtime_dir(base: Path | None = None) -> Path:
    """The bridge runtime directory, falling back to /tmp when the bound socket path would be too long."""
    candidate = (base or (Path.home() / ".local" / "state" / PLUGIN_ID)).expanduser()
    longest = len(str(candidate / SOCKET_NAME).encode("utf-8")) + TEMP_SOCKET_SUFFIX_MAX
    if longest > MAX_SOCKET_PATH_BYTES:
        digest = hashlib.sha256(str(candidate).encode("utf-8")).hexdigest()[:12]
        candidate = Path("/tmp") / f"{PLUGIN_ID}-{os.getuid()}-{digest}"
    return candidate


def ensure_private_dir(path: Path) -> Path:
    """Create ``path`` as a user-private directory; refuse symlinks and foreign ownership."""
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = os.lstat(path)
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise OSError(f"{path} is not a directory")
    if info.st_uid != os.getuid():
        raise OSError(f"{path} is owned by uid {info.st_uid}, not {os.getuid()}")
    if stat.S_IMODE(info.st_mode) & 0o077:
        os.chmod(path, 0o700)
    return path


def sidepulse_state_dirs(home: Path | None = None) -> tuple[Path, ...]:
    """SidePulse's candidate state directories, most likely first (mirrors upstream ``candidate_state_dirs``)."""
    dirs: list[Path] = []
    xdg = os.environ.get("XDG_STATE_HOME")
    if home is None and xdg:
        dirs.append(Path(xdg).expanduser() / "sidepulse" / "agent-monitor")
    fallback = (home or Path.home()) / ".local" / "state" / "sidepulse" / "agent-monitor"
    if fallback not in dirs:
        dirs.append(fallback)
    return tuple(dirs)


def sidepulse_ui_sockets(home: Path | None = None) -> tuple[Path, ...]:
    """The menu-bar app's ``events.sock`` candidates."""
    return tuple(directory / "events.sock" for directory in sidepulse_state_dirs(home))


def sidepulse_relay_socket_for_dir(state_dir: Path) -> Path:
    """Upstream's ``relay_socket_path_for_state_dir``: a /tmp fallback when the path is over 96 bytes."""
    path = state_dir / "relay-events.sock"
    if len(str(path).encode("utf-8")) <= 96:
        return path
    digest = hashlib.sha256(str(path).encode("utf-8")).hexdigest()[:16]
    return Path("/tmp") / f"sidepulse-relay-{os.getuid()}-{digest}.sock"


def sidepulse_service_sockets(home: Path | None = None) -> tuple[Path, ...]:
    """The headless service's ``relay-events.sock`` candidates (a local socket despite the name)."""
    return tuple(sidepulse_relay_socket_for_dir(directory) for directory in sidepulse_state_dirs(home))
