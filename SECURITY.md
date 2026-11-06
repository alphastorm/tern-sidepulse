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

- **Field-read privacy guarantees**: `window.luau` reads only `pane`, `state`, `kind`, `program`, `host`, and `alive`
  from each `cx.agents:list()` row, `id`, `state`, `panes`, and `kind` from `cx.hosts:list()`, and `surface.role` from
  each pane. It never reads or transmits pane titles, working directories, model names, prompts, assistant text, tool
  arguments, or transcript paths (`tests/test_privacy.py` pins that extra client fields never leave the coordinator and
  that the SidePulse socket payload's `"prompt"` key is always the constant string `"OMP in Tern"`).
- **Coordinator socket protocol**: `~/.local/state/tern-sidepulse/coordinator.sock` (and its fallback runtime paths),
  strict UTF-8 JSON validation (`NaN`/`Infinity` and deep nesting rejected), the 256 KiB request cap, read/write
  deadlines, connection and state caps, and observer registration token fencing.
- **Helper execution**: `tern.process.run` argument vectors (never through a shell) using
  `python3 -I -S -X pycache_prefix=<data>/pycache` with a 4 s timeout.
- **Local file and directory modes**: `~/.local/state/tern-sidepulse` created and verified with mode `0700` (ownership
  checked, symlinks refused), and `coordinator.sock`, `coordinator.lock`, `coordinator.log`, `doctor.json`,
  `identity.json`, and `disabled` written with mode `0600` under `umask 077` (`coordinator.sock` published by atomic
  rename after `listen(16)`).
- **`config.json` handling**: `$HOME/Library/Application Support/Tern/plugin-data/tern-sidepulse/config.json` accepted
  only when it is a regular file of at most 64 KiB (symlinks and special files refused), and validated before launching
  a configured Python interpreter or overriding socket paths.

### Out of scope

Vulnerabilities in [Tern](https://stencil.so/tern), [Oh My Pi](https://github.com/can1357/oh-my-pi) (OMP), or
[SidePulse](https://github.com/inteliwear/sidepulse) themselves are out of scope for this repository and should be
reported to their respective upstream maintainers.

## Supported versions

Security fixes are applied to the latest release and the `main` branch.

Community project; not affiliated with or endorsed by the Oh My Pi, Tern, or SidePulse maintainers.
