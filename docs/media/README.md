# Canonical synthetic demo

These files are a deterministic offline composite rendered from [`assets/demo.html`](../../assets/demo.html), not a capture of Tern, Oh My Pi (OMP), or SidePulse. Every pane, transcript skeleton, status card, and device is synthetic. The plugin has no UI of its own. The animation is an illustration of the documented status flow, not release-qualification evidence or evidence of physical LED output: a socket write is not an LED check.

Durations are compressed for the 16 s loop. The real notices are **15 s for completion** and **30 s for failure**, unless the session resumes first. The illustrated connector delay is editorial pacing, not a measured delivery latency. See [`../compatibility.md`](../compatibility.md) for the exercised versions and actual checks.

## Default LED programs

The illustration reproduces SidePulse's built-in defaults at revision `ffb1d3c2` as CSS keyframes, not as a hardware capture. The source programs live in [`src/sidepulse/resources/animations/`](https://github.com/inteliwear/sidepulse/tree/ffb1d3c2/src/sidepulse/resources/animations):

| Source | CSS illustration |
| --- | --- |
| `cyan-roll-8.LED` | `#00E5FF` pulses lasting 760 ms, with adjacent LEDs 95 ms apart. |
| `amber-pulse.LED` | A `#FF3A00` pulse over 1.6 s. |
| `solid-green.LED` | Solid `#00FF66`. |
| `idle-pulse-8.LED` | Off for 2 s, then LEDs 2–5 (zero-based) pulse `#006060 #00E5FF #00E5FF #006060` over 2 s. |

The eight-LED strip, its glow, and the status card are neutral illustrations. Their colors describe those default programs; they do not prove that a connected device received or displayed a status, and they need not match a user's configured animations.

## Regenerate and verify

From the repository root with Python 3.10+, Chrome/Chromium, FFmpeg with `libx264`, and ffprobe installed:

```sh
python3 scripts/render_assets.py
python3 scripts/render_assets.py --check banner og
```

The renderer uses only the Python standard library. It loads the local HTML sources with headless Chrome; network access is needed only for the artwork's Google Fonts webfonts. A font preflight fails rather than knowingly publishing fallback typography. No Tern, OMP, or SidePulse process participates in the render, and nothing runs merely by importing `scripts.render_assets`.

Name `banner`, `og`, or `demo` to regenerate a subset. Use `--chrome /path/to/chrome` or `CHROME` to select Chrome and `--ffmpeg /path/to/ffmpeg` or `FFMPEG` to select FFmpeg; ffprobe is resolved beside FFmpeg or from the normal tool locations. `--jobs` controls frame workers (default: the lesser of four and the CPU count). Each worker reuses its own temporary Chrome profile.

```sh
python3 scripts/render_assets.py demo --keep-frames /tmp/tsp-brand/frames
python3 scripts/render_assets.py --check demo
```

`--check` regenerates into a temporary directory and compares bytes without publishing; it lists stale files and exits 1 on a mismatch. Byte equality depends on the same Chrome build, fonts, and encoder tools, so this is a **same-machine check, not a CI check**. All requested targets are staged and validated before replacement, with `manifest.json` published last. The manifest fingerprints `assets/brand.css`, `assets/demo.html`, and `assets/logo.svg` using sorted relative paths and source bytes, separated by NULs, and records each demo output's SHA-256, size, and dimensions.

A checkout linked with `tern plugin link` reloads the plugin on every file write, including generated artwork and media. Render from an **unlinked clone** when those reloads matter. Do not run or relink Tern to regenerate this package.

## Recorded render

Source revision at render time: `3fc0d8d` (branding working tree). The exact demo input bytes are identified by [`manifest.json`](manifest.json), not by an assumption that the working tree matched that commit.

Tool versions, recorded verbatim in the manifest:

- Chrome: `Google Chrome 156.0.8078.80`
- FFmpeg: `ffmpeg version 9.0-https://www.martin-riedl.de Copyright (c) 2000-2026 the FFmpeg developers`
- ffprobe: `ffprobe version 9.0.2 Copyright (c) 2007-2026 the FFmpeg developers`
- GIF optimizer: `LCDF Gifsicle 1.98`

The canvas is **960×540 CSS pixels at DPR 2**, producing **1920×1080 PNG frames** in sRGB. The renderer samples **320 frames at 20 fps over 16.0 s**, with `t = i / 20` from 0 through 15.95 s, using Chrome's 5000 ms virtual-time budget. The poster is frame 112, exactly **t = 5.6 s**, in `waiting_for_input` at the amber pulse's peak.

GIF settings: **960×540, 10 fps, 160 frames, 16 s, infinite loop**; Lanczos downscale; one global palette with at most 256 colors, `stats_mode=full`; `paletteuse=dither=bayer:bayer_scale=3:diff_mode=rectangle`; metadata removed and bitexact flags enabled. `gifsicle -O3 -Okeep-empty` ran after encoding; keeping empty frames preserves the prescribed sample count without changing the timing. The initial palette settings met the 1.5 MiB target, so no `stats_mode=diff` or `bayer_scale=5` fallback was needed.

MP4 settings: **1920×1080, 20 fps, 320 frames, 16 s**, `libx264`, `yuv420p`, CRF 20, preset `slow`, tune `animation`, no audio, `+faststart`, `-map_metadata -1`, `-fflags +bitexact`, and `-flags:v +bitexact`.

Exact byte sizes and SHA-256 digests of the three files are recorded in [`manifest.json`](manifest.json) at every render. The GIF stays below the 1.5 MiB target and both animations below the 3 MiB hard cap; the social preview (`assets/og.png`, 1280×640) stays below GitHub's 1,000,000-byte limit, and `tests/test_site.py` fails when any of these drift.

## Pixel provenance

| Pixels / files | Provenance |
| --- | --- |
| GIF, MP4, poster | Entirely offline presentation composites of the local HTML/CSS/JavaScript illustration; no runtime application screenshots or live sessions. |
| Tern window and pane contents | Neutral title bar, tab, panes, skeleton transcript lines, and synthetic ask affordances. Not Tern or OMP UI captures; no third-party marks. |
| LED strip and SidePulse status card | Neutral device and card illustrations without third-party marks. The plugin itself has no UI. |
| Captions, `tern-sidepulse` wordmark, Pane mark, connector, and moving pulse | Editorial presentation chrome, not plugin UI or measured timing evidence. |
| Status names and LED colors | SidePulse's documented statuses and the default programs identified above; CSS-rendered behavior, not observed physical LEDs. |

## Canonical directory

This directory contains exactly the three binaries, this provenance file, and `manifest.json`:

- `tern-sidepulse-demo.gif`
- `tern-sidepulse-demo.mp4`
- `tern-sidepulse-demo-poster.png`
- `README.md`
- `manifest.json`

Frame PNGs, ffprobe evidence, contact sheets, and temporary encoding candidates stay outside this directory. Regenerate the demo and update the recorded render together after changing its sources or render tools; do not hand-edit a binary or relabel old assets as a new capture. The banner and social preview are maintained separately in `assets/` and are not part of the demo manifest.

Community project; not affiliated with or endorsed by the Oh My Pi, Tern, or SidePulse maintainers.
