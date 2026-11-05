# Changelog

## 0.1.0 — 2026-11-05

First release.

- Window-only Tern plugin: observes the OMP sessions visible in each window (local and attached remote hosts), classifies panes by their `omp.session` surface role, and reports working / waiting / completed / failed as one synthetic SidePulse session.
- Standard-library Python helper with an on-demand local coordinator: singleton lock, generation fencing across plugin reloads, 15 s observer leases, fixed-deadline outcome notices (15 s done, 30 s failed), sleep-aware clocks, bounded retry to both SidePulse receiver families (menu-bar app and headless service), neutral-then-exit after the last window.
- Palette commands: doctor, resync, enable, disable; the same operations from `bridge.py`.
- Verified against Tern 0.9.12, OMP 18.16.0 and SidePulse `ffb1d3c2` (`docs/compatibility.md`).
