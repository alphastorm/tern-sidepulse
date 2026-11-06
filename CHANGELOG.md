# Changelog

## 0.1.0 — 2026-11-05

First release.

- Window-only Tern plugin: observes the OMP sessions visible in each window (local and attached remote hosts), classifies panes by their `omp.session` surface role, and reports working / waiting / completed / failed as one synthetic SidePulse session.
- Standard-library Python helper with an on-demand local coordinator: singleton lock, generation fencing across plugin reloads, 15 s observer leases, fixed-deadline outcome notices (15 s done, 30 s failed), sleep-aware clocks, bounded retry to both SidePulse receiver families (menu-bar app and headless service), neutral-then-exit after the last window.
- Palette commands: doctor, resync, enable, disable; the same operations from `bridge.py`.
- Verified against Tern 0.9.12, OMP 18.16.0 and SidePulse `ffb1d3c2` (`docs/compatibility.md`).

Pre-release audit (security, robustness, performance), fixed before the tag:

- Window observer: a too-old interpreter (`python_too_old`, exit 3) is now actually rejected and the next candidate tried; helper and fence failures back off exponentially to 30 s instead of retrying every reconciliation or, for a shim interpreter, re-registering continuously; counters reset only on an accepted snapshot. Classification no longer excludes rows by the undocumented `kind`; the surface role alone decides. Bootstrap retries a nil-cx timer, probes window capabilities under `pcall`, and doctor reports an inert window and its reason. Recovery is logged at warn so Tern's default filter shows it. Every path that leaves a generation (fence, resync, enable, disable, latch) drops pending outcome notices.
- Coordinator: strict JSON on the socket and the helper's stdin (no `NaN`/`Infinity`; deep nesting is `malformed_json`, never a `RecursionError` that unwound the loop on Python ≤ 3.13); `observer.window_key` must be printable (a newline forged log lines, a lone surrogate crashed the reaper on lease expiry); a per-connection guard drops a client whose request raises; the process runs with `umask 077`; the socket listens before it is published; a crash exit is logged as such; the log tolerates undecodable text and a failed rotation.
- Helper hot path: `-I -S` with bytecode cached under the plugin data directory (`pycache/`) instead of `-B`; `argparse`, `subprocess`, `secrets`, `traceback`, `hashlib` and `uuid` are off the per-snapshot path (median `status` round trip 44 ms → 24 ms on Python 3.15). `config.json` must be a regular, non-symlink file of at most 64 KiB on the Python side too. The runtime directory falls back to `$TMPDIR/tern-sidepulse` before the guessable `/tmp` name.
- Removed dead code (`lock_holder_pid`, `save_config`, write-only observer fields) and the no-op `PYTHONDONTWRITEBYTECODE` environment entry.
- Focused review of the batch (three independent reviewers) closed six more: a concurrent command callback could reject the healthy interpreter a sibling callback had just chosen; the disabled latch seen from `pump()` kept the generation and pending notices; commands that failed before spawning a helper lost their toast; `identity.json` and the latch were 0644; a graceful coordinator exit skipped its final neutral when a family had failed less than 1 s earlier; a plain `python3 bridge.py …` from a shell wrote `__pycache__` into the plugin directory. The surface role is now re-read on every reconciliation so a pane that stops being an OMP session drops out without an event.
