# Changelog

## 0.1.0 — 2026-11-05

First release.

- Window plugin (`window.luau`) for [Tern](https://stencil.so/tern): observes the [OMP](https://github.com/can1357/oh-my-pi) sessions visible in open Tern windows across local and attached remote hosts, classifies panes by their `omp.session` surface role, and reports aggregate status (`working`, `waiting_for_input`, `completed`, `blocked_error`, `idle_ready`) on [SidePulse](https://github.com/inteliwear/sidepulse) as one synthetic session (`tern-sidepulse:<install_id>`).
- Standard-library Python 3.10+ helper and on-demand per-user coordinator (`bridge.py`, `tern_sidepulse`): singleton lock, observer registration and 15 s leases, fixed-deadline outcome notices (15 s completion, 30 s failure), sleep-aware clock checks, bounded retry to both SidePulse receivers (`events.sock` for the menu-bar app and `relay-events.sock` for the headless service), and automatic `idle_ready` clear and exit after the last window closes.
- Tern command palette actions (`tern-sidepulse: doctor`, `tern-sidepulse: resync`, `tern-sidepulse: disable`, `tern-sidepulse: enable`) and matching `bridge.py` CLI commands (`status`, `doctor`, `resync`, `disable`, `enable`).
- Verified against Tern 0.9.12 (`e498b49`), OMP 18.16.0, and SidePulse `0.2.4.dev6+gffb1d3c2f` (revision `ffb1d3c2`) with 96 unit and integration tests; see [docs/compatibility.md](docs/compatibility.md).
- Hardening: isolated `python3 -I -S` helper launches with bytecode cached under the plugin data directory (`pycache/`), strict UTF-8 JSON validation and 256 KiB request / 32-connection caps on the coordinator socket, mode `0600` runtime and state files in a `0700` directory, and automatic rejection of pre-3.10 interpreters with exponential retry backoff up to 30 s (see commits `bf38d95` and `8446d6a`).
