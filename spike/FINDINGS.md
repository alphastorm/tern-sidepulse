# SidePulse #72 adapter spike: findings

Spike branch only; never merged. Subject: inteliwear/sidepulse#72 at `337c703` (open, 2026-11-05) and
tern-sidepulse `feat/per-pane-rows` (B core). Nothing here was posted upstream.

## What was built

- `tern_sidepulse/program_transport.py`: `ProgramStatusTransport`, one `program_status` report per row on its own
  connection, acknowledged by the daemon; `clear` for rows that went away; a write-ahead scope ledger
  (`<runtime>/program-scopes.json`) so the next coordinator clears a crashed one's rows; resubmission when the socket
  is replaced; at most 32 rows; `status.title` cut to 192 bytes; `title_source: "session"`.
- It plugs into the B-core transport boundary unchanged (`publish`, `tick`, `next_due`, `undelivered_neutral`,
  `neutral_delivered`, `delivery_summary`, `to_dict`, `session_id`). The only other edit is the spike-only switch
  in `server.run_coordinator` (`"transport": "program_status"` in `config.json`, or `TERN_SIDEPULSE_TRANSPORT`).
- `spike/pr72_server.py` runs #72's `ProgramState` + `ProgramServer` alone on a private socket (no devices, relays,
  phone links, or the user's publisher id). `spike/experiments.py` probes #72's rules in process
  (`experiments-output.json`). `spike/e2e_adapter.py` drives the real helper and coordinator against that server,
  with SIGKILL and SIGTERM for the crash and restart cases (`e2e-output.json`).

## Results

| Check | Result |
| --- | --- |
| Three sessions (two local, one remote) | three rows; OMP's status glyph removed from every label |
| Label change | the row's name follows (requires `title_source: "session"`, see E4) |
| SIGKILL the coordinator | all three rows stay in SidePulse; the ledger lists three scopes |
| Next window snapshot | a new coordinator clears the two closed sessions' rows; one row remains |
| SidePulse service restart | rows drop to 0; the adapter sees the new socket inode and restores the row in 0.73 s |
| `disable` | rows go to 0 |
| Lease expiry (no windows) | the row is cleared 3.0 s after the 3 s lease; the coordinator exits drained |
| Live Tern tracer (a side-by-side copy as `tern-sidepulse-dev`, 2026-11-05) | the window's three real OMP sessions became three rows: `Context and Task Briefs  V0211`, `RepoPrompt  ROWS`, `Review docs  DOCS`; after unlinking, leases expired, the rows were cleared and the coordinator exited |

### #72 rules (in-process probes)

- **E2, no expiry except `done`:** rows backdated two days still show `working`, `waiting_for_input`,
  `blocked_error` and `idle_ready`; the aggregate stays `blocked_error` with three active. Only `done` ages out
  (20 minutes).
- **Idle timeout no longer applies:** at `ffb1d3c` the status bar passed the user's idle timeout
  (`settings.idle_timeout_seconds`, default 60 minutes) to the collector as `stale_after_seconds`
  (`status_bar.py:1234`). In #72 the settings window still shows and saves it (`status_bar.py:1591`, `1752`, `1777`)
  but nothing reads it: `ProgramMonitor.snapshot` is the only status path, and it filters only old `done` rows. This
  affects every reporter, including Claude and Codex hook rows, not just this plugin.
- **E3, restart:** rows 2 → 0; the socket inode changes; the next ack restarts at `seq 1`, `revision 1`.
- **E4, titles:** with `title_source: "prompt"` (or absent) the daemon keeps the first title for good; only
  `"session"` replaces it.
- **E5, menu text:** `"DOCS: Review docs"` renders as `Review docs  DOCS`: the menu treats the text before `": "` as a
  project name. Without a `session_id`/`agent_id` the name gets an empty `()` suffix; without a label the row shows
  the scope. With a `cwd` it renders `DOCS: Review docs  <git project>`.
- **E6, capacity:** 64 rows are shared by every provider; a 65th scope evicts the oldest silently.
- **E7, size:** a `status.title` over 192 bytes makes the daemon reject the whole report.
- **E9, forwarding:** each scope's latest update goes to configured relays and phone links (`bridge_update`,
  `phone_update`) with `status.title`, i.e. the label. `cwd` and `context.title` are not forwarded.

## The two open questions

1. **Service restart drops every row.** Confirmed, and solvable on our side: the socket inode changes on restart,
   and the adapter resubmits within one reaper tick (0.73 s measured). No upstream change needed.
2. **A dead reporter's rows stay up.** Confirmed. On our side the ledger clears them as soon as any Tern window
   respawns the coordinator (measured). Left: if the coordinator dies and never starts again (Tern quits at the same
   time, or the plugin is removed after a crash), its rows stay until SidePulse restarts, and an orphaned
   `working`/`blocked_error` row keeps the LED on. The same is true of a Claude or Codex session killed without
   `SessionEnd`, because #72 dropped the idle-timeout staleness.

## Draft upstream note (not posted; needs founder approval)

> In #72, `idle_timeout_seconds` is still shown and saved in Settings but no longer reaches any status path: at
> `ffb1d3c` it became the collector's `stale_after_seconds`; `ProgramMonitor.snapshot` only ages out `done` rows. A
> reporter that dies (a killed agent without `SessionEnd`, or a bridge process) now leaves `working`/`blocked`/`error`
> rows, and the LED, up until the daemon restarts. Would you apply the idle timeout to program rows (by
> `updated_at`), or accept an optional per-report lease that reporters renew? Repro: submit a `working` report, stop
> reporting, backdate `updated_at` past the timeout; the row still counts as active.

Optional, lower value: a way to show a reporter's title verbatim (no `"A: B"` project split), and documenting
`title_source`.

## Adapter changes the spike surfaced (for the real 0.2 adapter)

- Defer the predecessor's ledger clears until the first complete snapshot (or a short grace), so a session that
  is still open is overwritten instead of being cleared and re-added.
- Labels leave the Mac when the user has a SidePulse relay or phone link: a founder decision before 0.2.0
  (send them, make them opt-out, or send a generic name instead).
- Decide whether to send `cwd` (better menu text, stays local per E9) — not authorized today.
