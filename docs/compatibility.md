# Compatibility and contract probe

Versions exercised on the development laptop (November 5, 2026):

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

`plugin.toml` has no minimum-API field. At load, `window.luau` probes `cx.agents:list`, `cx.hosts:list`, `cx.session:surface`, and `tern.process.run` under `pcall`; when any member is missing, the window stays inert and reports the missing member through the `tern-sidepulse: doctor` toast and `doctor.json`. `tern.timer` runs in the entry chunk itself, so a Tern build without `tern.timer` fails to load the plugin outright.

## Generated declarations vs. the inspected SDK

`tern plugin types` from Tern 0.9.12 (pinned as `types/tern.d.luau`) matches `plugins/tern.d.luau` in the `stencil-hq/tern-sdk` `a3ba62fd` baseline for every member this plugin uses:

- `AgentsCx:list(): {AgentBlock}` with `AgentBlock = {pane, command?, model?, cwd, state, last_message?, delivery_error?}` and `state` in `idle | working | waiting_input | exited`.
- `HostsCx:list(): {ManagedHost}` with `state` in `connecting | connected | stalled | verify | reconnecting | failed`, plus `sessions` and `panes` membership.
- `SessionCx:surface(pane, {nodes?, depth?, max_chars?})` returning `{surface = {id, title?, role?, mode}, …}`.
- Events `agent_state({pane, old?, new})`, `agent_finished({pane, result})` with `result` in `done | failed | exited | closed`, `pane_closed`, `session_ended`, `window_start`, `budget`.
- `tern.process.run(argv, {stdin, env, timeout_ms}, cb)`, `tern.timer`, `tern.kv`, `tern.fs`, `tern.plugin.data`, `cx:window_key()`.

Observed on 0.9.12 but undocumented in `types/tern.d.luau`: every `cx.agents:list()` row also carries `host` (the host id from `cx.hosts:list()`), `program` (`"omp"`), `kind` (`"agent"`), `alive`, `session`, `tab`, `busy`, `at_prompt`, `alert`, `title`, `running`, `last`, and, while working, `progress = {percent, state}`. The plugin reads `host` and `alive` when present and falls back to the documented fields (`host.panes` and `row.state`) when absent. `kind` and `program` are counted in diagnostics only and never decide classification, so a Tern build that reports different values cannot hide OMP sessions.

## Contract probe results

A disposable probe plugin recorded row projections (never raw rows, titles, `cwd`, or transcript text) while test OMP sessions were driven from the CLI:

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

- Classification checks only `surface.role == "omp.session"`, re-read on every 5 s reconciliation pass (0.01–0.1 ms per pane; the cached result only bridges a surface query that fails outright). A missing surface (OMP still starting) is counted on the next pass that sees the role, and a pane whose role changes in place drops out on the next pass. `program` and `kind` are diagnostics only because wrapped launches such as `tern new tab -- env … omp` report `program = "env"`, while titles, `cwd`, and missing `command` are never used as evidence.
- A 5 s reconciliation timer re-enumerates panes because daemon-side closes emit no window event. Each window holds a 15 s *lease* (an expiry timer at the coordinator, renewed by every complete snapshot), which tolerates one failed helper call; two consecutive failures expire the lease and withdraw that window's contribution for the ~150 ms needed to register again on the next call.
- Each window registers as an *observer* (the per-window `window.luau` instance reporting snapshots) identified by `(parent pid of the helper, TERN_WINDOW_KEY)`, and pane ids are opaque within that observer. A helper whose parent pid changes on every call (such as a forking wrapper configured as `python`) is fenced on every snapshot; after three consecutive fence rejections the window backs off exponentially (`500 * 2^n` ms, capped at 30 s) instead of re-registering continuously, and `doctor` reports the cause.
- An interpreter that exits with status 3 (`python_too_old`, with a JSON body; on this laptop `/usr/bin/python3`, 3.9.6) is rejected for the life of the window VM and the next candidate is tried; `/usr/bin/python3` is tried last and only when `/Library/Developer/CommandLineTools/usr/bin/python3` exists.
- The coordinator answers each client on a blocking write with a 0.5 s deadline across at most 32 concurrent connections; a same-user client that connects and never reads can delay the loop by at most 0.5 s per connection. Lease and notice deadlines are checked against the clock after each request and on every 1 s reaper tick, so a stalled loop withdraws state rather than prolonging it.

## SidePulse contract (verified against the installed collector)

Driven through the installed package's `parse_log_line` and `LiveAgentMonitor` with provider `omp` (`tests/fixtures/sidepulse_probe.py` checks the mode after each step of the status and trap rows in this table):

| Sent (`hook_event_name`, `sidepulse_status`) | Resulting status | Note |
| --- | --- | --- |
| `UserPromptSubmit`, `working` | `working` | Also after `completed`, `blocked_error` and `waiting_for_input` |
| `Stop`, `completed` | `completed` | Visible for 20 min by SidePulse's own default; the coordinator sends idle after its 15 s notice |
| `PermissionRequest`, `waiting_for_input` | `waiting_for_input` | No `tool_name`/`tool_input`, so no pending-permission signature blocks later transitions |
| `StopFailure`, `blocked_error` | `blocked_error` | |
| `Interrupt`, any | `idle_ready` | `Interrupt` ignores `sidepulse_status`; idle statuses are stale immediately (`idle_visible_seconds = 0`), which is the neutral cleanup |
| `Notification` after `completed` | ignored | Trap confirmed: never used |
| `SessionEnd` without explicit status | `completed` | Trap confirmed: never used; with `sidepulse_status = idle_ready` it maps to idle, but `Interrupt` is the neutral event |
| Missing `hook_event_name`, or an unknown name | dropped | A top-level status alone is not the protocol |
| `agent_id` present | status key becomes `omp:agent:<id>` | Omitted so it can never supersede the session identity |
| Older `logged_at` than the current status | still replaces | The collector keeps no ordering; the coordinator serializes sends |

Two receivers run on this laptop: the menu-bar app (`io.sidepulse.agentstatus`) on `~/.local/state/sidepulse/agent-monitor/events.sock` and the headless service (`io.sidepulse.service`) on `relay-events.sock` in the same directory. Upstream SidePulse falls back to `/tmp/sidepulse-relay-<uid>-<hash>.sock` when the `relay-events.sock` path exceeds 96 bytes, and the coordinator mirrors that fallback. Each receiver keeps its own `LiveAgentMonitor`, so the coordinator sends every event to both families. For read-only inspection, the menu-bar app writes `~/.local/state/sidepulse/agent-monitor/status-history.jsonl` (`agent_status` and `display_status`) on refresh, and whichever receiver ingested last writes `~/.local/state/sidepulse/agent-monitor/latest.json`. A SidePulse Pro USB LED device (`/Volumes/SidePulse`, display mode `agent`) is attached for physical LED checks, which are reported separately from socket delivery.

## Integration checks on the laptop (real Tern, real SidePulse, linked plugin)

Each check reads `coordinator.log`, `bridge.py status`, and SidePulse's `status-history.jsonl` and `latest.json` without patching SidePulse state. A SidePulse Pro was attached with its display set to `agent`, so the LED followed the menu-bar app's aggregate, while the timings below come from socket and state-file records:

| Check | Observed |
| --- | --- |
| Install/reload with three existing local OMP sessions | Discovered on the first snapshot (~60 ms after load); SidePulse's aggregate went `completed` (two other agents) → `working` the same second; no session restarted |
| Coordinator survival | pid stayed the same across the double window reload after `tern plugin link`, across every edit-triggered reload during development, and runs under launchd in its own session/process group |
| Plugin unlinked while a session was working | Last snapshot 22:47:18 → lease expiry and `Interrupt`/`idle_ready` at 22:47:33 (15 s) → coordinator exit 5 s later; SidePulse fell back to the other agents' `completed`, so only the coordinator's record was withdrawn |
| Failed turn (401) in a disposable session while another session worked | `blocked_error` 3.4 s after the session was launched (the request itself takes ~2.4 s), held exactly 30.0 s, then `working` again |
| OMP `ask` in a disposable session | `waiting_for_input` outranked `working`; a daemon-side `tern close` (no window event) was reconciled back to `working` within 2 s |
| Remote OMP on the attached Mac Studio | Observed as `host = 1`; its `ask` produced `waiting_for_input`, its close returned the aggregate to `working` |
| SidePulse headless service restarted (`launchctl kickstart -k`) | New socket inode detected on the next reaper tick; `working` resent to the service family only, 1 s after the restart; the menu-bar app family was untouched |
| Second window opened | Registered as observer `<pid>:2` beside `<pid>:` with an empty view; when its process died its lease expired 15 s after its last snapshot and window 1's observations were unaffected |
| `time.monotonic()` on this macOS | `mach_absolute_time()`, which pauses during sleep; the coordinator compares wall and monotonic deltas on every request and tick, so either clock behaviour is handled |
| Linked checkout with git's fsmonitor daemon | Every git command touched `.git/fsmonitor--daemon/cookies` and reloaded the window (a new generation each time); `git config --local core.fsmonitor false` in the checkout stops it; installed copies carry no `.git` |
| Rename to the one name `tern-sidepulse` (plugin name, palette titles, toast titles, helper messages) | The linked checkout reloaded in place; `tern plugin list` reports `tern-sidepulse 0.1.0 tern-sidepulse … window ready`; `bridge.py doctor` kept its check ids and reports the renamed remedy strings; the coordinator (same pid, `VERSION 0.1.0`) needed no restart. The four palette titles were not re-exercised from the palette itself after the rename |
| Sleep/wake (`pmset sleepnow` with three working sessions; asleep 78 s, 19:09:01–19:10:19 PST on battery) | First reaper tick after wake logged `clock discontinuity: wall +69.8s, monotonic +1.0s; leases uncertain` (so `mach_absolute_time()` paused for the whole sleep), marked the observer uncertain and sent `idle_ready` to both receivers at once (SidePulse: `Working` → `Idle`); the window's first post-wake snapshot 3.0 s later restored `working` on both receivers; same coordinator pid and generation, `expired_observers` stayed 0, `discontinuities` 1. An earlier attempt that woke 11 s after the sleep command froze the machine for only ~3 s (under the 5 s threshold) and correctly logged nothing |

Automated suite ([`../README.md#development`](../README.md#development)): 111 unit and integration tests in total (`python3 -B -m unittest discover -s tests -t .`), verified on Python 3.13.18 and 3.15.2 locally and on Python 3.10 and 3.13 in CI. The 111 tests cover an end-to-end subprocess coordinator with fake receivers, hostile-client inputs (deep nesting, `NaN`/`Infinity`, non-printable observer keys, and symlinked, FIFO, or oversized `config.json`), the coherence of the README, `plugin.toml`, the palette commands, the rendered media and the site (`tests/test_site.py`), and include two conditional tests:

- `tests/test_sidepulse_contract.py` (`SidePulseContractTest.test_mapping_table_and_traps`): runs when a SidePulse interpreter exists at `SIDEPULSE_PYTHON` or `~/.local/share/sidepulse/venv/bin/python` and skips otherwise.
- `tests/test_e2e.py` (`EndToEndTest.test_default_limits_neutral_within_20s`): runs only when `TERN_SIDEPULSE_SLOW_TESTS=1` is set (skipped in the default fast run, which reports `Ran 111 tests … OK (skipped=1)` when SidePulse is installed) and checks that an unrenewed 15 s lease reaches `idle_ready` within the 20 s target.

The window half's retry policy (backoff up to 30 s while the coordinator is unavailable or every snapshot is fenced, replacement of a `python_too_old` interpreter, and `warn`-level failure/recovery logging) was exercised with a standalone `luau` harness driving `window.luau` against a fake `tern` environment.

Not exercised here, with the exact check still to run:

- Full Tern quit and restart: quit Tern with a working session, confirm neutral within 20 s, relaunch, confirm `working` returns after the first snapshot.
- Remote disconnect/reconnect: disconnect the remote host from the host dialog with a working remote session; the next reconciliation drops its contribution (`host_unavailable` in doctor); reconnect and confirm it returns.
- The two-window close-one case with both windows showing OMP sessions (window 2 above showed none).

User-facing runtime limitations are listed in [`../README.md#known-limitations`](../README.md#known-limitations).
