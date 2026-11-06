# Compatibility and contract probe

Versions actually exercised on the development laptop (November 5, 2026):

| Component | Version | Notes |
| --- | --- | --- |
| macOS | 27.2 (26B123) | Apple Silicon |
| Tern | 0.9.12 (e498b49), `/Applications/Tern.app` | Dock-launched; plugin processes inherit `PATH=/usr/bin:/bin:/usr/sbin:/sbin` |
| Tern SDK declarations | `tern plugin types` output from 0.9.12, pinned as `types/tern.d.luau` | Research baseline was stencil-hq/tern-sdk `a3ba62fd` (November 4, 2026) |
| OMP | 18.16.0 | Shell-typed `omp` and `tern new tab -- omp` blocks |
| SidePulse | 0.2.4.dev6+gffb1d3c2f (venv at `~/.local/share/sidepulse`) | Exactly the inspected revision `ffb1d3c2` |
| Python | 3.15.2 (`/opt/homebrew/bin/python3`), 3.13.18 also present | `/usr/bin/python3` is 3.9.6 and is never chosen |
| Luau tooling | luau 0.745 (`luau-analyze`), luau-lsp 1.70.1 (`luau-lsp analyze --definitions=types/tern.d.luau`) | Development/CI checks only |
| Remote host | A Mac Studio attached over Tern remoting, Tern 0.9.12 there too | No SidePulse, helper, or service on the remote host |

The manifest has no minimum-API field; the plugin probes the members it needs at load (`cx.agents:list`, `cx.hosts:list`, `cx.session:surface`, `tern.process.run`, each under `pcall`) and, when one is missing, stays inert and reports the reason through the doctor toast and `doctor.json` instead of assuming a version. `tern.timer` is used by the entry chunk itself, so a build without it fails to load the plugin outright.

## Generated declarations vs. the inspected SDK

`tern plugin types` from 0.9.12 agrees with the inspected `plugins/tern.d.luau` for every member this plugin uses:

- `AgentsCx:list(): {AgentBlock}` with `AgentBlock = {pane, command?, model?, cwd, state, last_message?, delivery_error?}` and `state` in `idle | working | waiting_input | exited`.
- `HostsCx:list(): {ManagedHost}` with `state` in `connecting | connected | stalled | verify | reconnecting | failed`, plus `sessions` and `panes` membership.
- `SessionCx:surface(pane, {nodes?, depth?, max_chars?})` returning `{surface = {id, title?, role?, mode}, …}`.
- Events `agent_state({pane, old?, new})`, `agent_finished({pane, result})` with `result` in `done | failed | exited | closed`, `pane_closed`, `session_ended`, `window_start`, `budget`.
- `tern.process.run(argv, {stdin, env, timeout_ms}, cb)`, `tern.timer`, `tern.kv`, `tern.fs`, `tern.plugin.data`, `cx:window_key()`.

Observed but undocumented: every `cx.agents:list()` row also carries `host` (the host id from `cx.hosts:list()`), `program` (`"omp"`), `kind` (`"agent"`), `alive`, `session`, `tab`, `busy`, `at_prompt`, `alert`, `title`, `running`, `last` and, while working, `progress = {percent, state}`. The plugin reads `host` and `alive` when present and falls back to the documented fields when absent; `kind` and `program` are counted in diagnostics only and never decide classification, so a Tern that reports other values (say the native pane kind) cannot make sessions disappear.

## Contract probe results

A throwaway window plugin recorded projections of the rows (never the rows themselves, titles, cwd or transcript text) into its data directory while disposable OMP sessions were driven from the CLI.

| Probe | Result |
| --- | --- |
| Three shell-typed OMP sessions in one window | All listed by `cx.agents:list()` with `program = "omp"`, `kind = "agent"`, `command = nil`; a dedicated `tern new tab -- omp` block lists with `command` set |
| Remote OMP (`tern new tab -- omp` on the remote host's daemon) | Listed with `host = 1`, the remote host's id in `cx.hosts:list()`; its pane id is the packed remote id also present in that host's `panes` |
| Non-OMP control (plain `zsh` pane on the remote host) | Not in `cx.agents:list()`; `cx.session:surface()` returns `nil` for it |
| Surface header query `{nodes = 1, depth = 1}` | 126–128 bytes encoded, 0.01–0.1 ms, `surface.role = "omp.session"` for every OMP pane, local and remote |
| `{nodes = 0}` | Works on 0.9.12 but is undocumented; not used |
| `{max_chars = 1}` alone | Returns the whole tree (200–260 KB); never use `max_chars` as the only cap |
| `{depth = 0}` | Returns the three region roots (~190 bytes); undocumented, not used |
| New OMP session | `agent_state {new = "idle"}` without `old` (startup idle; not a success) |
| Turn lifecycle | `agent_state idle→working`, then `agent_finished {result = "done"}`, then `agent_state working→idle` about 0.2–1.1 s later; at `agent_finished` time a fresh `cx.agents:list()` already reports `idle` |
| Failed turn (401 from an invalid provider key) | `agent_finished {result = "failed"}`; no `working` sample was emitted for that short turn (sampled events miss brief transitions) |
| OMP `ask` tool | `agent_state working→waiting_input`; the row also shows `alert = true`, `busy = true` |
| `/exit` in OMP | `agent_finished {result = "exited"}` then `pane_closed` |
| `tern close BLOCK` (daemon-side close) | No window event at all: neither `pane_closed` nor `agent_finished closed`; only re-enumeration notices the pane is gone |
| Editing a linked plugin file | Window reload within ~1 s; timers die; `window_start` does not run again |
| `tern.process.run` from the window half | Parent process is the Tern application process (`/Applications/Tern.app/Contents/MacOS/tern`, parent launchd); env carries `TERN_PLUGIN_DATA` and `TERN_WINDOW_KEY` (`""` for the first window); `TERN_PANE` is empty |

Consequences built into the plugin:

- Classification is the current surface's `role == "omp.session"`, nothing else; a missing surface (OMP still starting) is re-checked on the next reconciliation. `program` and `kind` are diagnostics only: `tern new tab -- env … omp` reports `program = "env"`, so a program-name rule would wrongly exclude wrapped launches. Titles, cwd and missing `command` are never evidence.
- A 5 s reconciliation timer re-enumerates everything because daemon-side closes are silent in the window. Against the 15 s lease that tolerates one failed helper call; two consecutive failures expire the window and it registers again on the next call (about 150 ms of withdrawn contribution).
- Observer identity is `(parent pid of the helper, TERN_WINDOW_KEY)`; pane ids are opaque within that observer. A helper whose parent pid changes on every call (a forking shim configured as `python`) is fenced on every snapshot; after three fences in a row the window backs off exponentially to 30 s instead of re-registering continuously, and doctor names the cause.
- An interpreter that answers `python_too_old` (exit status 3 with a JSON body) is rejected for the life of the VM and the next candidate is tried; `/usr/bin/python3` is only ever a candidate when the Command Line Tools provide it.

## SidePulse contract (verified against the installed collector)

Driven through the installed package's `parse_log_line` + `LiveAgentMonitor` with provider `omp`:

| Sent (`hook_event_name`, `sidepulse_status`) | Resulting status | Note |
| --- | --- | --- |
| `UserPromptSubmit`, `working` | `working` | Also after `completed`, `blocked_error` and `waiting_for_input` |
| `Stop`, `completed` | `completed` | Visible for 20 min by SidePulse's own default; the bridge sends idle after its 15 s notice |
| `PermissionRequest`, `waiting_for_input` | `waiting_for_input` | No `tool_name`/`tool_input`, so no pending-permission signature blocks later transitions |
| `StopFailure`, `blocked_error` | `blocked_error` | |
| `Interrupt`, any | `idle_ready` | `Interrupt` ignores `sidepulse_status`; idle statuses are stale immediately (`idle_visible_seconds = 0`), which is the neutral cleanup |
| `Notification` after `completed` | ignored | Trap confirmed: never used |
| `SessionEnd` without explicit status | `completed` | Trap confirmed: never used; with `sidepulse_status = idle_ready` it maps to idle, but `Interrupt` is the neutral event |
| Missing `hook_event_name`, or an unknown name | dropped | A top-level status alone is not the protocol |
| `agent_id` present | status key becomes `omp:agent:<id>` | Omitted so it can never supersede the session identity |
| Older `logged_at` than the current status | still replaces | The collector keeps no ordering; the coordinator serializes sends |

Receivers on this laptop: the menu-bar app (`io.sidepulse.agentstatus`) on `~/.local/state/sidepulse/agent-monitor/events.sock` and the headless service (`io.sidepulse.service`) on `relay-events.sock` in the same directory (upstream falls back to `/tmp/sidepulse-relay-<uid>-<hash>.sock` when that path exceeds 96 bytes; the bridge mirrors it). Each keeps its own `LiveAgentMonitor`, so both receive every event. Read-only inspection of the result: `~/.local/state/sidepulse/agent-monitor/status-history.jsonl` (`agent_status`, `display_status`, written by the menu-bar app on refresh) and `latest.json` (written by whichever receiver ingested last). A SidePulse Pro (`/Volumes/SidePulse`, display `agent`) is attached, so LED checks are possible; they are reported separately from socket delivery.

## Integration checks on the laptop (real Tern, real SidePulse, linked plugin)

Each check reads the coordinator log, `bridge.py status`, and SidePulse's own `status-history.jsonl` / `latest.json`; none of them patches SidePulse state. A SidePulse Pro was attached with its display set to `agent`, so the LED followed the menu-bar app's aggregate, but the figures below are socket/record observations, not LED measurements.

| Check | Observed |
| --- | --- |
| Install/reload with three existing local OMP sessions | Discovered on the first snapshot (~60 ms after load); SidePulse's aggregate went `completed` (two other agents) → `working` the same second; no session restarted |
| Coordinator survival | pid stayed the same across the double window reload after `tern plugin link`, across every edit-triggered reload during development, and runs under launchd in its own session/process group |
| Plugin unlinked while a session was working | Last snapshot 22:47:18 → lease expiry and `Interrupt`/`idle_ready` at 22:47:33 (15 s) → coordinator exit 5 s later; SidePulse fell back to the other agents' `completed`, so only the bridge's record was withdrawn |
| Failed turn (401) in a disposable session while another session worked | `blocked_error` 3.4 s after the session was launched (the request itself takes ~2.4 s), held exactly 30.0 s, then `working` again |
| OMP `ask` in a disposable session | `waiting_for_input` outranked `working`; a daemon-side `tern close` (no window event) was reconciled back to `working` within 2 s |
| Remote OMP on the attached Mac Studio | Observed as `host = 1`; its `ask` produced `waiting_for_input`, its close returned the aggregate to `working` |
| SidePulse headless service restarted (`launchctl kickstart -k`) | New socket inode detected on the next reaper tick; `working` resent to the service family only, 1 s after the restart; the menu-bar app family was untouched |
| Second window opened | Registered as observer `<pid>:2` beside `<pid>:` with an empty view; when its process died its lease expired 15 s after its last snapshot and window 1's observations were unaffected |
| `time.monotonic()` on this macOS | `mach_absolute_time()`, which pauses during sleep; the coordinator compares wall and monotonic deltas on every request and tick, so either clock behaviour is handled |
| Linked checkout with git's fsmonitor daemon | Every git command touched `.git/fsmonitor--daemon/cookies` and reloaded the window (a new generation each time); `git config --local core.fsmonitor false` in the checkout stops it; installed copies carry no `.git` |

Automated: 93 unit/integration tests (`python3 -B -m unittest discover -s tests -t .`) on Python 3.13 and 3.15 locally, 3.10 and 3.13 in CI, including an end-to-end subprocess coordinator with fake receivers, hostile-client cases (deep nesting, non-finite numbers, unprintable observer keys, symlinked or FIFO `config.json`), the optional contract test against the installed SidePulse normalizer, and the slow default-limits test (neutral 15 s after the last snapshot, inside the 20 s target). The window half's retry policy (backoff to 30 s while the coordinator is unavailable or every snapshot is fenced, replacement of a too-old interpreter, recovery logged at warn) was exercised with a throwaway `luau` harness driving `window.luau` against a fake `tern`.

Not exercised here, with the exact check still to run:

- Sleep/wake: `pmset sleepnow` with a working session, then confirm the coordinator log shows `clock discontinuity` and the next snapshot restores `working`.
- Full Tern quit and restart: quit Tern with a working session, confirm neutral within 20 s, relaunch, confirm `working` returns after the first snapshot.
- Remote disconnect/reconnect: disconnect the remote host from the host dialog with a working remote session; the next reconciliation drops its contribution (`host_unavailable` in doctor); reconnect and confirm it returns.
- The two-window close-one case with both windows showing OMP sessions (window 2 above showed none).

## Known limitations

- Tern samples agent state: brief transitions (a turn that fails within a second) can lack a `working` sample; the bridge still shows the outcome notice.
- Pane ids are window-local; the bridge emits one synthetic SidePulse session and reduces duplicates idempotently rather than counting distinct OMP agents.
- Cleanup after the last window closes depends on the coordinator staying alive and SidePulse being reachable within the final 60 s grace; a hard-killed coordinator or a longer outage leaves the last mode until the next coordinator start neutralizes the same identity.
- Remote coverage follows the window's host connection state; nothing is polled on the remote host after all windows close.
- A pane whose live surface is not `omp.session` (OMP still starting, or a non-OMP agent) is not counted until a reconciliation sees that role.
