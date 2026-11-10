# Compatibility and contract probe

Versions exercised on the development laptop:

| Component | Version | Notes |
| --- | --- | --- |
| macOS | 27.2.1 (26B131) | Apple Silicon; the 0.1.0 checks ran on 27.2 (26B123) |
| Tern | 0.9.16 (41a10a5), `/Applications/Tern.app` | 0.2.0 checks (November 10, 2026); the 0.1.0 checks ran on 0.9.12 (e498b49). Dock-launched; plugin processes inherit `PATH=/usr/bin:/bin:/usr/sbin:/sbin` |
| Tern SDK declarations | `tern plugin types` output from 0.9.12, pinned as `types/tern.d.luau` | Research baseline was stencil-hq/tern-sdk `a3ba62fd` (November 4, 2026) |
| OMP | 18.18.7 | Shell-typed `omp` and `tern new tab -- omp` blocks; the 0.1.0 checks used 18.16.0 |
| SidePulse | 0.2.4.dev36+gad4a5217f (venv at `~/.local/share/sidepulse`) | The [inteliwear/sidepulse#72](https://github.com/inteliwear/sidepulse/pull/72) merge `ad4a5217` (2026-11-10; SidePulse desktop has no release tags, `setup.sh` and `sidepulse update` install `main`) |
| Python | 3.15.2 (`/opt/homebrew/bin/python3`), 3.13.18 also present | `/usr/bin/python3` is 3.9.6 and is never chosen |
| Luau tooling | luau 0.745 (`luau`, `luau-analyze`), luau-lsp 1.70.1 (`luau-lsp analyze --definitions=types/tern.d.luau`) | Development/CI checks (`tests/test_luau_harness.py` and static analysis) |
| Remote host | A Mac Studio attached over Tern remoting, Tern 0.9.12 there too (0.1.0 checks) | No SidePulse, helper, or service on the remote host |

`plugin.toml` has no minimum-API field. At load, `window.luau` probes `cx.agents:list`, `cx.hosts:list`, `cx.session:surface`, and `tern.process.run` under `pcall`; when any member is missing, the window stays inert and reports the missing member through the `tern-sidepulse: doctor` toast and `doctor.json`. `tern.timer` runs in the entry chunk itself, so a Tern build without `tern.timer` fails to load the plugin outright.

## Generated declarations vs. the inspected SDK

`tern plugin types` from Tern 0.9.12 (pinned as `types/tern.d.luau`) matches `plugins/tern.d.luau` in the `stencil-hq/tern-sdk` `a3ba62fd` baseline for every member this plugin uses:

- `AgentsCx:list(): {AgentBlock}` with `AgentBlock = {pane, command?, model?, cwd, state, last_message?, delivery_error?}` and `state` in `idle | working | waiting_input | exited`.
- `HostsCx:list(): {ManagedHost}` with `id`, `name`, `kind` in `local | remote`, `address?`, and `state` in `connecting | connected | stalled | verify | reconnecting | failed`, plus `sessions` and `panes` membership.
- `SessionCx:surface(pane, {nodes?, depth?, max_chars?})` returning `{surface = {id, title?, role?, mode}, …}`.
- `SessionCx:panes(): {PaneInfo}` with `PaneInfo = {pane, title, tab?, …}` (every pane, parked ones included; a parked pane has no `tab`) and `SessionCx:tabs(): {TabInfo}` with `TabInfo = {id, name?, title, …}` (`name` is the user-given tab name; `title` is the focused pane's title and is never read).
- Events `agent_state({pane, old?, new})`, `agent_finished({pane, result})` with `result` in `done | failed | exited | closed`, `pane_closed`, `session_ended`, `window_start`, `budget` (no `title` event subscription; labels refresh on the 5 s reconciliation pass).
- `tern.process.run(argv, {stdin, env, timeout_ms}, cb)`, `tern.timer`, `tern.kv`, `tern.fs`, `tern.json`, `tern.plugin.data`, `cx:window_key()`.

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
- Each window registers as an *observer* (the per-window `window.luau` instance reporting snapshots) identified by `(parent pid of the helper, TERN_WINDOW_KEY)`. Inside the coordinator, each OMP session row is keyed by `(host, pane)` — where `host` is `local` or the remote host's `address` (else `name`, else `id:<n>`) — and deduplicated across windows. A helper whose parent pid changes on every call (such as a forking wrapper configured as `python`) is fenced on every snapshot; after three consecutive fence rejections the window backs off exponentially (`500 * 2^n` ms, capped at 30 s) instead of re-registering continuously, and `doctor` reports the cause.
- An interpreter that exits with status 3 (`python_too_old`, with a JSON body; on this laptop `/usr/bin/python3`, 3.9.6) is rejected for the life of the window VM and the next candidate is tried; `/usr/bin/python3` is tried last and only when `/Library/Developer/CommandLineTools/usr/bin/python3` exists.
- The coordinator answers each client on a blocking write with a 0.5 s deadline across at most 32 concurrent connections; a same-user client that connects and never reads can delay the loop by at most 0.5 s per connection. Lease and notice deadlines are checked against the clock after each request and on every 1 s reaper tick, so a stalled loop withdraws state rather than prolonging it.

## Labels and host keys

Additional contract probes on the development laptop (November 5, 2026) verified how Tern 0.9.12 exposes pane titles, tab names, host addresses, and JSON encoding:

| Probe | Result |
| --- | --- |
| `cx.session:panes()` and `cx.session:tabs()` scope and cost | Both cover every session in the window, including panes and tabs on attached remote hosts, and cost about 0.03 ms each |
| Tab join (`PaneInfo.tab` vs. `TabInfo.id`) | `PaneInfo.tab` equals `TabInfo.id`; a parked pane has `tab = nil` |
| Tab names vs. tab titles | Unnamed tabs have no `name` (`TabInfo.name == nil`); `TabInfo.title` is the focused pane's title (in a split tab, whichever pane has focus, such as `zsh`), so `TabInfo.title` is never read |
| OMP pane titles (`PaneInfo.title`) | Start with a Unicode private-use status glyph (such as `U+F1517` or `U+F412`), a space, and the session title |
| `tern.json.encode` | Emits raw UTF-8, escapes C0 control characters, and replaces invalid UTF-8 byte sequences with `U+FFFD` |

How the window and coordinator handle labels and host keys (and the integer diagnostic counters `window.luau` reports in `doctor.json` under `window_diag`):

- **Per-pass lookup and label precedence**: When a window pass has at least one OMP observation, `window.luau` calls `cx.session:panes()` and `cx.session:tabs()` at most once per 5 s pass under `pcall` (an empty pass skips both calls; a failed call increments the `labels_unavailable` diagnostic counter and preserves status reporting). Each OMP pane takes its tab's user-given `TabInfo.name` when that string has a non-space character (incrementing `label_tab`), else the OMP pane's own `PaneInfo.title` when non-blank (incrementing `label_title`), else no label (incrementing `label_none`).
- **Host key**: `local` for the local host, or a remote host's `address` when non-empty, free of control characters, and at most 128 bytes, else its `name`, else `id:<n>` (incrementing `host_fallback`).
- **Sanitize rule (`tern_sidepulse/reducer.py`)**: The coordinator collapses whitespace runs to one space, removes C0 controls (`U+0000`–`U+001F`), `DEL` and C1 controls (`U+007F`–`U+009F`), lone surrogates (`U+D800`–`U+DFFF`), and Unicode private-use characters (`U+E000`–`U+F8FF`, `U+F0000`–`U+FFFFD`, `U+100000`–`U+10FFFD`, including OMP's status glyph), trims leading and trailing spaces, and caps the result at 96 characters (an empty string becomes `None`). A malformed wire label is dropped without rejecting the snapshot.
- **Size chain**: `window.luau` cuts each label to at most 256 bytes at a UTF-8 character boundary (`MAX_LABEL_BYTES = 256`) and caps host keys at 128 bytes (`MAX_HOST_KEY_BYTES = 128`) across at most 512 observations per window. If the encoded snapshot exceeds 192 KiB (`MAX_SNAPSHOT_BYTES = 192 * 1024`) or `tern.json.encode` fails while labels are present, the window strips all labels, increments `labels_dropped`, and re-encodes once so status delivery still succeeds (a persistent encode failure increments `encode_failed` and backs off). The coordinator caps requests at 1 MiB (`max_request_bytes = 1024 * 1024`), host keys at 128 characters, and sanitized labels at 96 characters.
- **Privacy and SidePulse boundary**: Labels and host keys travel over the helper's stdin pipe to the coordinator's memory. By default, sanitized labels are sent to SidePulse in each row's `program_status` report (`context.title`); setting `"labels": false` in `<plugin data dir>/config.json` sends `"title": ""` and `"display_name": "OMP in Tern"` in `context` instead. Host keys never appear outside coordinator memory (beyond that stdin pipe), and pane ids and host keys never leave the Mac: SidePulse scopes (`tern-sidepulse-<24 hex>`), `context.session_id` (`<24 hex>`), and the on-disk scope ledger (`<runtime dir>/program-scopes.json`) store only `sha256(install_id NUL host NUL pane)[:24]` hashes salted with `install_id` from `identity.json` (local `status` and `doctor.json` list pane ids of active outcome notices). Labels and host keys never appear in `coordinator.log`, `doctor.json`, `bridge.py status` output, coordinator acknowledgements, or `program-scopes.json` (`status` and `doctor` report `rows` by mode, `rows_total`, `rows_labelled`, and allow-listed integer window diagnostic counters including `label_tab`, `label_title`, `label_none`, `labels_unavailable`, `labels_dropped`, `host_fallback`, and `encode_failed`).

## SidePulse program-status contract (verified at `ad4a5217`, inteliwear/sidepulse#72)

`tern-sidepulse` 0.2.0 talks only to SidePulse's program-status protocol ([inteliwear/sidepulse#72](https://github.com/inteliwear/sidepulse/pull/72), merged 2026-11-10 as `ad4a5217`); the 0.1.0 hook-event transport (`events.sock` and `relay-events.sock`) is removed.

- **Receiver and socket paths**: The SidePulse background service (`io.sidepulse.service`) listens on `program-status.sock` in SidePulse's state directory (`$XDG_STATE_HOME/sidepulse/agent-monitor`, else `~/.local/state/sidepulse/agent-monitor`), or `/tmp/sidepulse-status-<uid>/<sha256(path)[:16]>.sock` when that path exceeds 96 bytes (`sidepulse_program_socket_for_dir` mirrors upstream `status_socket_path`). Candidates can be overridden with `program_sockets` in `config.json`, `TERN_SIDEPULSE_PROGRAM_SOCKETS`, or `bridge.py coordinator --program-socket PATH`. After `#72`, `events.sock` still exists in the state directory but only answers `{"command": "open-settings"}` (other JSON is ignored, so 0.1.0 writes succeed and do nothing), and `relay-events.sock` is gone. When `relay-events.sock` exists without `program-status.sock`, `tern-sidepulse: doctor` fails the `receiver` check (`this SidePulse predates program status`, remedy `sidepulse update`); `tern-sidepulse` 0.1.0 remains the version for pre-`#72` SidePulse builds.
- **Wire framing and acknowledgements**: One UTF-8 JSON object per `AF_UNIX` `SOCK_STREAM` connection within a 0.2 s send timeout (`SEND_TIMEOUT_SECONDS = 0.2`), framed by half-closing the write side (`shutdown(SHUT_WR)`). The daemon replies with one JSON object (up to 64 KiB): `{"ok": true, "seq": n, "revision": n}` when accepted, or `{"ok": false, "error": "…"}` (`"Daemon is busy…"` triggers retry backoff; any other rejection marks the entry rejected without retrying until the row changes or SidePulse restarts, increments `status.transport.rejected`, raises the `receiver_rejections` doctor warning, and logs fixed text only, never SidePulse's error string).
- **Report shapes**:
  - Labelled row (`labels` enabled and row has a non-empty sanitized label; `display_name` is omitted):
    `{"v":1,"type":"program_status","scope":"tern-sidepulse-<24 hex>","request_id":"<uuid4>","status":{"state":"working"},"context":{"provider":"omp","origin":"Tern","session_id":"<24 hex>","title_source":"session","title":"<label>"}}`
  - Unlabelled row, or `"labels": false` in `<plugin data dir>/config.json` (read when the coordinator starts; `tern-sidepulse: disable` then `enable` restarts the coordinator on the next snapshot):
    in `context`, `"title": ""` plus `"display_name": "OMP in Tern"` (`"title_source": "session"` with an empty `"title"` stops SidePulse from carrying an older label forward when a pane loses its label or labels are turned off).
  - Clear (row withdrawn):
    `{"v":1,"type":"program_status","scope":"tern-sidepulse-<24 hex>","request_id":"<uuid4>","status":{"state":"clear"}}`
- **Mode map**:

| Coordinator row mode (`tern_sidepulse/reducer.py`) | `program_status` `status.state` | SidePulse normalized mode | Note |
| --- | --- | --- | --- |
| `blocked_error` | `error` | `blocked_error` | Held for the 30 s failure notice unless the pane resumes first |
| `waiting_for_input` | `blocked` | `waiting_for_input` | Outranks `working` |
| `working` | `working` | `working` | Active turn |
| `completed` | `done` | `completed` | Held for the 15 s completion notice unless the pane resumes first; SidePulse ages out `done` rows after 20 min |
| `idle_ready` | `idle` | `idle_ready` | Open OMP pane at the prompt with no active notice |
| *(withdrawn)* | `clear` | *(removed)* | Pane closed, host disconnected, window lease expired, plugin disabled, or coordinator shutting down |

- **Scope derivation**: `scope = "tern-sidepulse-" + sha256(install_id NUL host NUL pane)[:24]` (salted with `install_id` from `<plugin data dir>/identity.json`), with `context.session_id` set to the 24-hex digest. Because the hash is deterministic per install, host, and pane, a restarted coordinator overwrites its own rows in place while keeping host keys and pane ids out of SidePulse state and logs.
- **Row limits (64 in SidePulse, 32 in `tern-sidepulse`)**: SidePulse shares a 64-row table across all providers and silently evicts the least recently updated row when full. `SidePulseTransport` therefore caps delivery at 32 rows (`MAX_ROWS = 32`), ordered by urgency (`blocked_error`, `waiting_for_input`, `working`, `completed`, `idle_ready`, then `(host, pane)`), and reports any omitted rows in `status.transport.rows_capped`.
- **Change-only sends, retries, restart, and 300 s refresh**: One report is queued per row when its report changes; unchanged rows are not resent (tick send budget `TICK_BUDGET_SECONDS = 0.3`, clears sent before updates). Retries reuse the same `request_id` (so SidePulse's receipt cache applies a retried report at most once) with backoff of 2, 4, 8, 16, 30 s; if a candidate socket appears or changes inode/ctime during backoff, the transport retries immediately so SidePulse starting after Tern is picked up at once. Deliberate resends (a changed row, `tern-sidepulse: resync`, the 300 s refresh, or a SidePulse restart) allocate a new `request_id`. Active rows (`working`, `blocked`, `error`) are resent every 300 s (`ACTIVE_REFRESH_SECONDS = 300.0`) to restore any row SidePulse evicted; `idle` and `done` rows are never refreshed. When SidePulse restarts (a new socket inode at the target `program-status.sock`, or its socket gone while another candidate now listens), every current row is resubmitted except delivered `done` rows, which must not read as a fresh completion and go out again when their state changes.
- **In-memory rows, idle-timeout gap, write-ahead ledger, and inherited-scope grace**: SidePulse keeps rows in memory only; a service restart drops them, and only `done` rows age out (after 20 min). SidePulse's "idle timeout" setting no longer applies to any row (reported upstream at [inteliwear/sidepulse#72 (comment)](https://github.com/inteliwear/sidepulse/pull/72#issuecomment-6676920089); `#72` merged without a change). To prevent orphaned rows, the transport writes `<runtime dir>/program-scopes.json` (mode `0600`, salted scope hashes only) before a scope's first report and prunes each scope when its `clear` is acknowledged; a row whose scope cannot be written to the ledger is held back. A coordinator starting with scopes left by a crashed predecessor keeps them for one 15 s lease (`inherit_grace = 15.0`): rows that a window still reports are overwritten in place without a clear/re-add flicker, and unclaimed scopes are cleared when the 15 s lease ends, or at once if they would push the install past its 32-row cap. A graceful exit (`SIGTERM`, idle drain after the 5 s `exit_grace`, `disable`, or version hand-off) clears every row first, ticking until every clear is acknowledged, a send fails, or 2 s pass (`DRAIN_BUDGET_SECONDS`); `disable` always stops the coordinator, so the next one reads `config.json` afresh. An idle coordinator exits only after every scope it may have shown is cleared (or after the 60 s `final_grace`), and scopes it could not clear stay in the ledger for the next coordinator.
- **Menu text and relay/phone forwarding**: In SidePulse's menu-bar **Agents** list, a label `DOCS: Review docs` renders as `Review docs  DOCS` (SidePulse displays the prefix before `": "` as the project), and a plain label renders as itself. If the user has configured a SidePulse relay or phone link, SidePulse forwards each row's latest update with `status.title` = the label (cut to 192 bytes) and context `provider` + `session_id`, the same way it forwards Claude and Codex prompt titles; when no relay or phone link is configured, nothing leaves the Mac.
- **Program-status spike against `ad4a5217`**: Driven against SidePulse `ad4a5217` (`program-status.sock`): three OMP sessions produced three distinct rows, tab/pane rename followed on the next pass, a SidePulse service restart recovered its rows in 0.72 s, lease-expiry cleared rows 3.09 s after a 3 s lease, and a `SIGKILL`ed coordinator left its rows in SidePulse until the next coordinator started and cleared them from `program-scopes.json`.
- **Display names**: SidePulse builds a labelled row's display name as `<label> (<first 8 of session_id>)` and cuts it to 96 characters, so a label at the 96-character cap loses its tail in the menu; the report itself is accepted (`tests/test_sidepulse_contract.py`).

## Laptop checks for 0.2.0 (November 10, 2026)

Run with the versions in the table above, after `sidepulse update` installed SidePulse `0.2.4.dev36+gad4a5217f` on the laptop:

| Check | Observed |
| --- | --- |
| A side-by-side copy (`tern-sidepulse-dev` 0.2.0, its own plugin id, data and runtime directories) linked into the running Tern with four live OMP sessions | Four SidePulse rows, one per session, origin `Tern`, named from tab or pane titles (for example `Prep Loop  PVC` for a waiting session); 4 reports accepted, 0 rejected; `doctor` reported the `receiver` check `ok` with 4 rows |
| Unlinking that copy | Its window lease expired and every row was gone from SidePulse 11 s after unlinking; the coordinator exited drained 5 s later with an empty `program-scopes.json` |
| The upgrade path: a running 0.1.0 coordinator, then a 0.2.0 window registering in the same runtime directory (against SidePulse's own program-status server on a private socket) | The 0.1.0 coordinator answered `version_mismatch` and exited; the next register started a 0.2.0 coordinator 1.6 s later, and its row appeared 30 ms after the first snapshot |
| `kill -9` of that 0.2.0 coordinator with two rows shown | Both rows stayed in SidePulse; the next coordinator logged `2 scope(s) left by an earlier coordinator`, updated the still-open session in place (same row, now waiting), and cleared the session that had closed meanwhile once its 15 s grace ended |
| `SIGTERM` | Every row cleared and the ledger emptied before the coordinator exited |
| `tern-sidepulse` 0.1.0 against this SidePulse | Its hook events reach the rebuilt `events.sock`, which ignores them, and `relay-events.sock` is gone, so it shows nothing |

## Laptop lifecycle checks (exercised with 0.1.0 against SidePulse `ffb1d3c2`, November 5, 2026)

The checks below were recorded on the development laptop with `tern-sidepulse` 0.1.0 against SidePulse `0.2.4.dev6+gffb1d3c2f` (`ffb1d3c2`, using the 0.1.0 hook-event transport over `events.sock` and `relay-events.sock`). The window observer (`window.luau`), 15 s observer leases, and sleep-aware clock discontinuity code they exercised are unchanged in 0.2.0:

| Check | Observed (0.1.0 against `ffb1d3c2`) |
| --- | --- |
| Install/reload with three existing local OMP sessions | Discovered on the first snapshot (~60 ms after load); no session restarted |
| Coordinator survival | pid stayed the same across the double window reload after `tern plugin link`, across every edit-triggered reload during development, and runs under launchd in its own session/process group |
| Plugin unlinked while a session was working | Last snapshot 22:47:18 → lease expiry at 22:47:33 (15 s) → coordinator exit 5 s later; only the coordinator's record was withdrawn |
| Failed turn (401) in a disposable session while another session worked | `blocked_error` 3.4 s after the session was launched (the request itself takes ~2.4 s), held 30.0 s, then `working` again |
| OMP `ask` in a disposable session | `waiting_for_input` outranked `working`; a daemon-side `tern close` (no window event) was reconciled within 2 s |
| Remote OMP on the attached Mac Studio | Observed as `host = 1`; its `ask` produced `waiting_for_input`, its close cleared that pane's contribution |
| Second window opened | Registered as observer `<pid>:2` beside `<pid>:` with an empty view; when its process died its lease expired 15 s after its last snapshot and window 1's observations were unaffected |
| `time.monotonic()` on this macOS | `mach_absolute_time()`, which pauses during sleep; the coordinator compares wall and monotonic deltas on every request and tick, so either clock behaviour is handled |
| Linked checkout with git's fsmonitor daemon | Every git command touched `.git/fsmonitor--daemon/cookies` and reloaded the window (a new generation each time); `git config --local core.fsmonitor false` in the checkout stops it; installed copies carry no `.git` |
| Sleep/wake (`pmset sleepnow` with three working sessions; asleep 78 s, 19:09:01–19:10:19 PST on battery) | First reaper tick after wake logged `clock discontinuity: wall +69.8s, monotonic +1.0s; leases uncertain` (so `mach_absolute_time()` paused for the whole sleep), marked the observer uncertain and withdrew status at once; the window's first post-wake snapshot 3.0 s later restored `working`; same coordinator pid and generation, `expired_observers` stayed 0, `discontinuities` 1. An earlier attempt that woke 11 s after the sleep command froze the machine for only ~3 s (under the 5 s threshold) and correctly logged nothing |
| Full Tern quit and relaunch (`osascript -e 'tell application "Tern" to quit'` with three working sessions, windows closed about 19 s, relaunched from the Dock) | The session daemon (`tern remote serve --user`) kept every pane's program alive, including the OMP sessions. The coordinator (same pid, started 00:55 UTC) logged `observer 1542:: generation 100 lease expired` 15.0 s after the last snapshot and withdrew status in the same millisecond; the relaunched app (pid 19961) registered two fresh observers (`19961:` with three working panes, `19961:2` with none) and its first snapshot restored `working` 3.7 s later; `expired_observers` 1 |
| Remote disconnect/reconnect from the host dialog (sf-studio over Tern remoting, one working OMP session there beside two local ones) | The dialog's disconnect *forgets* the host (`cx.hosts:disconnect`; `tern remote hosts` listed nothing), so the pane simply left `cx.agents:list()`: within one 5 s pass the observer's snapshot went from `working: 3` (`diag omp: 3`) to `working: 2` (`diag omp: 2`), and the two local panes stayed `working`; no `host_unavailable` count, because that only covers a host still listed in a non-connected state. Reconnecting from the dialog brought the remote pane back on the next pass (`working: 3`, `diag omp: 3`) under the same observer generation |

Automated suite ([`../README.md#development`](../README.md#development)): unit, transport, and integration tests (`python3 -B -m unittest discover -s tests -t .`), verified on Python 3.13.18 and 3.15.2 locally and on Python 3.10 and 3.13 in CI. The suite covers `SidePulseTransport` (`tests/test_transport.py`), an end-to-end subprocess coordinator with fake `program-status.sock` receivers, per-pane row reduction and cross-window deduplication (`tests/test_rows.py`), sentinel checks proving host keys and unknown fields never leave the coordinator and labels go only to SidePulse when enabled (`tests/test_privacy.py`), hostile-client inputs (deep nesting, `NaN`/`Infinity`, non-printable observer keys, and symlinked, FIFO, or oversized `config.json`), the coherence of the README, `plugin.toml`, the palette commands, the rendered media, and the site (`tests/test_site.py`), and three conditional test modules:

- `tests/test_luau_harness.py` (`LuauHarnessTest`): runs the real `window.luau` headlessly under the `luau` CLI (`tests/luau/harness.luau`, `window.luau`, and each drive script; skipped when `luau` is absent unless `TERN_SIDEPULSE_REQUIRE_LUAU=1` is set, and run in CI's `luau` job under Luau 0.745). It exercises backoff, interpreter replacement, disabled-latch notice dropping, synchronous command failure toasts, and in-place role changes (`drive_backoff.luau`, scenarios A–G), user-given tab names vs. OMP pane titles, UTF-8 byte truncation, optional `panes()`/`tabs()` failures, per-pass call counts, remote host keys, and 5 s rename refresh (`drive_labels.luau`, scenarios L1–L12), and the 192 KiB encoded snapshot budget with label-drop fallback and encoder-failure recovery (`drive_budget.luau`, scenarios B1–B3).
- `tests/test_sidepulse_contract.py` (`SidePulseContractTest`): drives `SidePulseTransport` against SidePulse's own program-status server, from `SIDEPULSE_SRC=<sidepulse checkout>/src` (`SIDEPULSE_SRC=<sidepulse checkout>/src python3 -m unittest tests.test_sidepulse_contract`) or from the installed SidePulse in `~/.local/share/sidepulse/venv` when it includes `#72`; skipped otherwise unless `TERN_SIDEPULSE_REQUIRE_CONTRACT=1` is set. CI runs it against SidePulse `ad4a5217`.
- `tests/test_e2e.py` (`EndToEndTest.test_default_limits_clear_within_20s`): runs only when `TERN_SIDEPULSE_SLOW_TESTS=1` is set (skipped in the default fast run) and checks that an unrenewed 15 s lease clears its rows within the 20 s target.

Not exercised here, with the exact check still to run:

- A remote host that stays listed but drops to a non-connected state (`stalled`, `reconnecting`, `failed`): the `host_unavailable` path, as opposed to the dialog's forget-and-reconnect above.
- The two-window close-one case with both windows showing OMP sessions (window 2 above showed none).

User-facing runtime limitations are listed in [`../README.md#known-limitations`](../README.md#known-limitations).
