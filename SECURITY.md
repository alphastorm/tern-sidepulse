# Security policy

## Reporting a vulnerability

Do not open a public issue or pull request for a suspected security or privacy vulnerability. Report it privately
through GitHub private vulnerability reporting:
[Report a vulnerability](https://github.com/alphastorm/tern-sidepulse/security/advisories/new).

Include the affected version or commit, the macOS, Tern, Oh My Pi (OMP), and SidePulse versions, and minimal
reproduction steps. Review `~/.local/state/tern-sidepulse/doctor.json` and `coordinator.log` before attaching any
diagnostics, redact usernames and private filesystem paths, and never include OMP transcripts, prompts, or pane titles.

If GitHub private vulnerability reporting is unavailable, contact `@alphastorm` through a private channel listed on the
maintainer's GitHub profile.

## Scope

The threat model and hardening controls are documented in
[`README.md` — Privacy and hardening](README.md#privacy-and-hardening). In-scope areas for `tern-sidepulse` are:

- **Field-read and label privacy guarantees**: the per-window observer (`window.luau`) reads only `pane`, `state`,
  `kind`, `program`, `host`, and `alive` from each `cx.agents:list()` row in Tern's window context (`cx`); `id`,
  `state`, `panes`, `kind`, `address`, and `name` from `cx.hosts:list()`; `surface.role` from
  `cx.session:surface(pane, {nodes = 1, depth = 1})`; and, at most once per 5 s pass when the window has at least one
  OMP pane, `pane`, `tab`, and `title` from `cx.session:panes()` plus `id` and `name` from `cx.session:tabs()`. Labels
  come only from user-given tab names (`TabInfo.name`) and OMP panes' own `PaneInfo.title` (never `TabInfo.title`), and
  the plugin never reads working directories (`cwd`), model names, commands, prompts, assistant text, tool arguments, or
  transcript paths. By default, sanitized labels are sent to SidePulse in each row's `program_status` report
  (`context.title`), and only if the user configured a SidePulse relay or phone link does SidePulse forward them (the
  same way it forwards Claude and Codex titles); setting `"labels": false` in `config.json` stops sending labels to
  SidePulse (`"title": ""` and `"display_name": "OMP in Tern"`). Host keys never appear outside coordinator memory, and
  pane ids and host keys never leave the Mac: SidePulse scopes (`tern-sidepulse-<24 hex>`), `context.session_id`, and
  the write-ahead scope ledger (`program-scopes.json`) hold only salted `sha256(install_id NUL host NUL pane)[:24]`
  hashes (local `status` and `doctor.json` list pane ids of active outcome notices). Labels and host keys never appear
  in `coordinator.log`, `doctor.json`, `bridge.py status` output, coordinator acknowledgements, or
  `program-scopes.json`, and a rejected report logs fixed text only, never SidePulse's error string
  (`tests/test_privacy.py`, `tests/test_rows.py`, and `tests/test_transport.py` pin this).
- **Coordinator socket protocol**: `~/.local/state/tern-sidepulse/coordinator.sock` (and its fallback runtime paths),
  strict UTF-8 JSON validation (`NaN`/`Infinity` and deep nesting rejected), the 1 MiB request cap (with the window's
  192 KiB snapshot budget that drops labels to fit), read/write deadlines (2 s / 0.5 s), connection (32), state (64
  windows, 512 panes per window, 256 registration tokens), host-key (128 bytes in the window, 128 characters in the
  coordinator), and sanitized-label (96 characters) caps, and observer registration token fencing (rejecting superseded
  or unrecognized observer generations).
- **Helper execution**: `tern.process.run` argument vectors (never through a shell) using
  `python3 -I -S -X pycache_prefix=<data>/pycache` with a 4 s timeout.
- **Local file and directory modes**: `~/.local/state/tern-sidepulse` created and verified with mode `0700` (ownership
  checked, symlinks refused), and `coordinator.sock`, `coordinator.lock`, `coordinator.log`, `doctor.json`,
  `program-scopes.json`, `identity.json`, and `disabled` written with mode `0600` under `umask 077` (`coordinator.sock`
  published by atomic rename after `listen(16)`).
- **`config.json` handling**: `$HOME/Library/Application Support/Tern/plugin-data/tern-sidepulse/config.json` accepted
  only when it is a regular file of at most 64 KiB (symlinks and special files refused), and validated before launching
  a configured Python interpreter, overriding `program_sockets`, or applying the `labels` setting.

### Out of scope

Vulnerabilities in [Tern](https://stencil.so/tern), [Oh My Pi](https://github.com/can1357/oh-my-pi) (OMP), or
[SidePulse](https://github.com/inteliwear/sidepulse) themselves are out of scope for this repository and should be
reported to their respective upstream maintainers.

## Supported versions

Security fixes are applied to the latest release and the `main` branch.

Community project; not affiliated with or endorsed by the Oh My Pi, Tern, or SidePulse maintainers.
