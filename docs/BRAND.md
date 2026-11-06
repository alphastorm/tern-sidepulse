# tern-sidepulse — brand spec

Identity for `alphastorm/tern-sidepulse`: the [Tern](https://stencil.so/tern) window plugin that puts the
[OMP](https://github.com/can1357/oh-my-pi) sessions visible in your Tern windows on
[SidePulse](https://github.com/inteliwear/sidepulse), as the LEDs and menu-bar status you already watch.

**Brand sibling of [OMP Session Gateway](https://github.com/carrythroughsystems/omp-session-gateway),
[OMP NInfer](https://github.com/alphastorm/omp-ninfer), [OMP Oracle](https://github.com/alphastorm/omp-oracle)
and [OMP Strata](https://github.com/alphastorm/omp-strata).** tern-sidepulse shares the family ground, neutrals,
type, kinship rule and voice register defined by the gateway; this file is the complete authority for the
tern-sidepulse identity. Live emerald `#31C48D` belongs to the gateway, Seam teal `#37C4CB` to Strata, Local
violet `#8E7BE8` to NInfer and Answer rose `#DE82B7` to Oracle; the retired signal blue `#3FA9DC` and exec
amber `#E0A33E` stay retired.

## Naming and message

One name, used everywhere the user types or reads it:

- Repository, plugin id, install string, data directory, state files, log lines, `plugin.toml` `name`, palette
  commands, site wordmark, social card: **`tern-sidepulse`**, lowercase, hyphen intact. Set as a wordmark in
  Space Grotesk 600 (the way the NInfer lockup carries its name); in running text and code it is plain
  `tern-sidepulse`. There is no "OMP ⟨Name⟩" display name: OMP is not the subject here. Tern is the host,
  SidePulse is the destination, OMP sessions are the payload, and a name that leads with either third party
  reads as that party's own integration.
- Entity grammar, in the order the reader meets it: *tern-sidepulse is a Tern plugin. It puts your OMP
  sessions on SidePulse.* Spell out "Oh My Pi" before using "OMP" on any surface that can be read standalone.
- Category: **Tern window plugin**.
- Headline: **Your OMP sessions in Tern. On SidePulse.** Two sentences; the second is set in Pane sky on
  branded artwork and the site hero.
- Sub line (the `plugin.toml` description and the GitHub About text): *Show the OMP sessions in your Tern
  windows on SidePulse: working, waiting, done, failed.*
- Palette commands are `tern-sidepulse: doctor`, `tern-sidepulse: resync`, `tern-sidepulse: disable` and
  `tern-sidepulse: enable`. "SidePulse bridge" is retired as a name; `bridge.py` stays because it is a file,
  not a brand.
- Primary action: **Get started**, linked to the README's Install section. The compatibility probe
  (`docs/compatibility.md`) is the proof surface, never a second call to action.
- Status grammar: name the exact Tern, OMP and SidePulse revisions a claim was exercised against; never round a
  partial probe up, and never describe a socket write as LED output.

## Voice

Sober and exact, in the gateway's register: sentence case except mono eyebrows, no emoji, no exclamation
marks. Every claim on an owned surface already appears in the README, the site or `docs/`. Artwork carries no
version, date or measured number, so it never goes stale between releases. The standing disclaimer appears on
every owned surface: **Community project; not affiliated with or endorsed by the Oh My Pi, Tern, or SidePulse
maintainers.** Never set the OMP, Tern or SidePulse marks on an owned surface; the SidePulse hardware and
menu-bar app appear only as neutral illustration, never as product photography.

## The mark: "The Pane"

A Tern window reduced to its title bar and the divider between two panes; the dot is the one pane whose OMP
session is lit, the status this plugin carries to SidePulse. Nothing else is drawn because nothing else is
added. Geometry (96×96 viewBox, all radii 2):

- title bar `x12 y16 w72 h10`
- divider `x43 y26 w10 h54`
- dot `cx68 cy54 r8` in Pane sky

Rules, the family's numbers:

- Never use upstream OMP's π-with-plug mark, Tern's mark or SidePulse's mark, or derivatives of them.
- Clearspace: one dot diameter (16 units) on all sides. Minimum size 16px.
- The dot is always Pane sky; the bars are Ink on dark surfaces (`logo.svg`) or Ink-dark on light surfaces
  (`logo-light.svg`). Never recolor, never give the dot a sibling's accent, never add a second dot (two lit
  panes read as a colon), and never add a third bar (three bars read as a menu icon).

## Color

The family ground and neutral ramp: `ground #060809`, `ink-dark #0B0E11`, `surface #0E1319`,
`border-subtle #161C22`, `border #1C232B`, `ink #E8ECEF`, `body #B6BEC7`, `muted #8A939D`.
tern-sidepulse owns:

| Token | Hex | oklch | Use |
|---|---|---|---|
| pane | `#70B3F7` | oklch(0.75 0.12 250) | the dot, links, eyebrows, the primary action, focus rings |
| pane-hover | `#97C9FD` | oklch(0.82 0.09 250) | link and action hover |
| kinship | `#F97316` | — (upstream orange) | citation micro-dot only; see the rule below |

Pane sky sits in the open gap between the retired signal blue (233°) and NInfer's violet (289°), clear of
the gateway's emerald and Strata's teal, and clear of every SidePulse default LED program (cyan roll, amber
pulse, solid green), so it never reads as a status. It measures 9.1:1 against `ground`, and `ink-dark` text on
it measures 8.7:1. Danger red `#C85045` stays the family error color. On a branded surface, SidePulse's
status colors appear only inside the demo illustration, rendering SidePulse's documented default programs;
never use them for pass/fail states in prose, badges or UI.

**Kinship rule (the gateway's):** upstream orange appears at most once per surface, only as a micro-dot
(≤5px UI, ≤8px artwork) beside an Oh My Pi mention — never in the mark, never on interactive elements,
never as a fill.

## Type

- **Public site:** first-party latin subsets of Space Grotesk and JetBrains Mono served from `site/fonts/`
  (OFL-1.1, licenses in `licenses/fonts/`), falling back to system stacks. No remote fonts, scripts,
  stylesheets or CDNs.
- **Exported artwork:** `assets/banner.html`, `assets/og.html` and `assets/demo.html` use Space Grotesk 500/600
  and JetBrains Mono 400/500, imported from Google Fonts by `assets/brand.css` at render time. Never copy that
  import into the site.
- Wordmark: `tern-sidepulse`, weight 600, letter-spacing −0.015em, lowercase. Mono eyebrows: 11–12px,
  uppercase, letter-spacing 0.16–0.18em.

## Maintenance procedures

<details>
<summary>Asset inventory, regeneration, README header and GitHub social preview</summary>

### Asset inventory

Paths are relative to the repository root. `assets/`, `docs/media/` and `site/` are presentation only;
nothing under them is read by the plugin, the helper, or the tests that exercise them.

| File | Purpose |
|---|---|
| `assets/logo.svg` | mark for dark backgrounds; site header mark and README dark mode |
| `assets/logo-light.svg` | mark for light backgrounds; README light mode |
| `assets/favicon.svg` | site favicon (96, rx22 tile) |
| `assets/brand.css` | shared tokens and primitives for the artwork sources |
| `assets/banner.html` | banner source (1280×320) |
| `assets/banner.png` | rendered banner @2x (2560×640) |
| `assets/og.html` | social preview source (1280×640) |
| `assets/og.png` | GitHub social preview and site `og:image` (1280×640) |
| `assets/demo.html` | synthetic demo source: a stylized Tern window and SidePulse's status, seekable by `?t=<seconds>`; `?probe=fonts` reports whether the webfonts loaded |
| `docs/media/tern-sidepulse-demo.gif`, `.mp4`, `-poster.png` | rendered demo loop for the README (GIF) and the site (MP4 with poster), with provenance in `docs/media/README.md` |
| `docs/media/manifest.json` | digest of the demo sources plus the SHA-256, size and dimensions of each rendered output; CI compares it against the files so a stale render fails without Chrome or ffmpeg |
| `site/` | public site (`alphastorm.github.io/tern-sidepulse`): the site's "What you see" strips are live CSS renderings of SidePulse's default programs, not images. `.github/workflows/pages.yml` stages `logo.svg`, `favicon.svg`, `og.png`, `demo.mp4` and `demo-poster.png` from `assets/` and `docs/media/` at deploy time, so `site/` never carries copies |

`tests/test_site.py` (host-free, part of the normal `unittest` discovery and CI) fails when a site page
references an asset the deployment would not serve, when a staged copy is not ignored by Git or would not
redeploy, when `og:image:width`/`og:image:height` disagree with `og.png`, when the sitemap, `robots.txt`,
`llms.txt` and canonical links disagree about the site's pages, when the site or README load anything remote
beyond the sanctioned shields.io badges, when the rendered media no longer match `docs/media/manifest.json`,
when a retired name survives anywhere, when the README header, `plugin.toml`, the palette commands in
`window.luau` and the site disagree about the name, the versions or the exercised Tern, OMP and SidePulse
revisions, when an owned surface drops the standing disclaimer, or when a documented test count goes stale.

### Regeneration

After editing an artwork source, run `python3 scripts/render_assets.py`. It renders `banner`, `og` and the
demo frames with headless Chrome (from `--chrome <path>`, `CHROME`, `PATH` or the default install), assembles
the GIF, MP4 and poster with `ffmpeg`, and verifies each PNG's dimensions; name targets to render only some
(`python3 scripts/render_assets.py og`). The render needs network access for the webfonts. `--check` renders
without writing and fails when a committed PNG no longer matches its source. Byte equality holds only for
the Chrome build and fonts that produced the committed PNG, so `--check` is a same-machine check and stays
out of CI.

### README header

The theme-aware mark heads the centered header block, as in the gateway README:

```html
<picture>
  <source media="(prefers-color-scheme: dark)" srcset="assets/logo.svg">
  <source media="(prefers-color-scheme: light)" srcset="assets/logo-light.svg">
  <img src="assets/logo-light.svg" alt="" width="72" height="72">
</picture>
```

Badges are shields.io flat style, one row, `labelColor 0B0E11`, values in `border` `#1C232B` or Pane sky
`#70B3F7` — never green or red status colors. Sanctioned badges: CI, release, Tern baseline, license.

### GitHub social preview

GitHub has no API for the repository social preview. After changing `assets/og.png`, upload it at
GitHub → Settings → General → Social preview.

</details>

## Relationship to upstream

Independent community project; not affiliated with or endorsed by the Oh My Pi, Tern, or SidePulse
maintainers. "Tern", "SidePulse" and "OMP" appear in the name, headline and docs as plain nominative reference
to the three products this plugin connects unmodified; do not restyle any of their logos.
