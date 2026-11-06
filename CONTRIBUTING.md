# Contributing

`tern-sidepulse` is developed in public. Issues and pull requests are welcome; the bar is that compatibility and
behavior claims are backed by evidence, privacy boundaries stay intact, and the plugin never forks or patches
[Tern](https://stencil.so/tern), [Oh My Pi](https://github.com/can1357/oh-my-pi) (OMP), or
[SidePulse](https://github.com/inteliwear/sidepulse).

## Prerequisites

- **Python 3.10 or later**: the runtime (`bridge.py`, `tern_sidepulse/`) and test suite (`tests/`) use the Python
  standard library only (compatible with Python 3.10+, so do not import `tomllib`). Nothing is installed with `pip`,
  and the repository uses no Node or Bun dependencies.
- **`luau-lsp` 1.70.1 and `luau` 0.745**: `luau-lsp` analyzes `window.luau` against `types/tern.d.luau` (the
  `tern plugin types` output from Tern 0.9.12; release archives at
  [JohnnyMorganz/luau-lsp v1.70.1](https://github.com/JohnnyMorganz/luau-lsp/releases/tag/1.70.1)), and `luau` runs
  `window.luau` headlessly in `tests/test_luau_harness.py` (release archives at
  [luau-lang/luau 0.745](https://github.com/luau-lang/luau/releases/tag/0.745), as in
  [`.github/workflows/ci.yml`](.github/workflows/ci.yml); set `LUAU=/path/to/luau` when not on `PATH`, or
  `TERN_SIDEPULSE_REQUIRE_LUAU=1` to fail instead of skipping when `luau` is absent).
- **Tern 0.9.12 on macOS**: required only for manual end-to-end checks against a live Tern window and SidePulse's agent
  monitor (`events.sock` and `relay-events.sock`); see [`docs/compatibility.md`](docs/compatibility.md) for the probe
  and integration checks.
- **Headless Chrome and `ffmpeg` (with `ffprobe` and `libx264`; `/opt/homebrew/bin/gifsicle` optional)**: required only
  when regenerating brand artwork (`assets/*.png`) or demo media (`docs/media/*`) with `scripts/render_assets.py`.

## Checks to run

Run both checks before opening a pull request:

```sh
python3 -B -m unittest discover -s tests -t . -v   # -B keeps bytecode out of a linked checkout
luau-lsp analyze --definitions=types/tern.d.luau window.luau
```

Set `SIDEPULSE_PYTHON` to a SidePulse interpreter (defaults to `~/.local/share/sidepulse/venv/bin/python` when present)
to run `tests/test_sidepulse_contract.py` against the installed SidePulse normalizer, and set
`TERN_SIDEPULSE_SLOW_TESTS=1` to include the 20 s default-limits timing test in `tests/test_e2e.py`.

## Working with a linked checkout

- Linking a checkout with `tern plugin link /absolute/path/to/tern-sidepulse` makes Tern watch the directory and reload
  the plugin on every file write inside it, including `.git/`.
- Always pass `-B` when running `python3` in a linked checkout so Python does not write `__pycache__` directories that
  trigger window reloads.
- If Git's `core.fsmonitor` daemon is enabled (`git config --get core.fsmonitor`), every Git command updates
  `.git/fsmonitor--daemon/cookies` and reloads the plugin. Disable it in the linked checkout:

  ```sh
  git config --local core.fsmonitor false
  ```

- Render artwork and demo media (`python3 scripts/render_assets.py`) from an unlinked clone (a checkout that is not
  registered with `tern plugin link`) so intermediate file writes do not repeatedly reload Tern.

## Invariants and documentation standards

1. **Standard library only**: do not add third-party Python packages, external runtime tools, or network calls.
2. **Privacy invariants (`tests/test_privacy.py`, `tests/test_rows.py`)**: the window observer (`window.luau`) reads
   only `pane`, `state`, `kind`, `program`, `host`, and `alive` from `cx.agents:list()`; `id`, `state`, `panes`, `kind`,
   `address`, and `name` from `cx.hosts:list()`; `surface.role` from `cx.session:surface(pane, {nodes = 1, depth = 1})`;
   and, at most once per 5 s pass when the window has at least one OMP pane, `pane`, `tab`, and `title` from
   `cx.session:panes()` and `id` and `name` from `cx.session:tabs()`. Labels come only from user-given tab names
   (`TabInfo.name`) and OMP panes' own `PaneInfo.title` (never `TabInfo.title`), and the plugin never reads working
   directories, model names, commands, prompts, assistant text, tool arguments, or transcript paths. Labels and host
   keys stay in the coordinator's memory only: they are never logged in `coordinator.log`, persisted to disk, returned
   by `bridge.py status` or `doctor.json`, echoed in coordinator acknowledgements or rejections, or sent to SidePulse by
   the hook-event transport. `tests/test_privacy.py` pins that sentinels in labels, host keys, and extra client fields
   never leave the coordinator, `doctor.json` filters window diagnostics to allow-listed integer counters, and the
   SidePulse socket payload's `line` object carries only its seven fixed status keys plus `"prompt"` on `working`
   events (where `"prompt"` is always the constant `"OMP in Tern"`, never user prompt text or a pane label).
3. **Exact versions in documentation**: compatibility and behavior claims in `README.md` and
   [`docs/compatibility.md`](docs/compatibility.md) name the exact Tern, OMP, SidePulse, and macOS versions they were
   exercised against, and never describe a local socket write as physical LED output.

## Brand, media, and public site

[`docs/BRAND.md`](docs/BRAND.md) is the authority for the Pane mark, color tokens, typography, and voice. After editing
an artwork source (`assets/*.html`, `assets/brand.css`, or `assets/*.svg`), regenerate the outputs from an unlinked
clone and verify them on the same machine:

```sh
python3 scripts/render_assets.py
python3 scripts/render_assets.py --check
```

`tests/test_site.py` checks that `README.md`, `plugin.toml`, `window.luau`, `site/`, and `.github/workflows/pages.yml`
stay coherent.

`site/` does not store copies of the mark, social preview, or demo video; `.github/workflows/pages.yml` stages them
at deploy time and `.gitignore` ignores the staged paths. To preview the site locally, run the staging `cp` lines from
`.github/workflows/pages.yml` and start a local HTTP server:

```sh
cp assets/logo.svg site/logo.svg
cp assets/favicon.svg site/favicon.svg
cp assets/og.png site/og.png
cp docs/media/tern-sidepulse-demo.mp4 site/demo.mp4
cp docs/media/tern-sidepulse-demo-poster.png site/demo-poster.png
python3 -m http.server -d site
```

## Commits and pull requests

- Commit titles follow [Conventional Commits](https://www.conventionalcommits.org/en/v1.0.0/):
  `type(scope): description`, lowercase imperative (for example, `fix(coordinator): fence stale observer epochs`).
- Update [`CHANGELOG.md`](CHANGELOG.md) when a change is user-visible. When bumping a release version, keep
  `tern_sidepulse/__init__.py` (`VERSION`), `plugin.toml` (`version`), `window.luau` (`VERSION`), and the top
  `## <version>` heading in `CHANGELOG.md` identical (`tests/test_site.py` checks all four).
- Pull requests use [`.github/pull_request_template.md`](.github/pull_request_template.md) and land by squash merge.

## Security and conduct

Report vulnerabilities privately per [`SECURITY.md`](SECURITY.md). Participation is governed by
[`CODE_OF_CONDUCT.md`](CODE_OF_CONDUCT.md).

Community project; not affiliated with or endorsed by the Oh My Pi, Tern, or SidePulse maintainers.
