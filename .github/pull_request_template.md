## Summary

<!-- What problem does this change solve? -->

## Verification

- [ ] `python3 -B -m unittest discover -s tests -t . -v` passes
- [ ] `luau-lsp analyze --definitions=types/tern.d.luau window.luau` passes
- [ ] Documentation facts name the exact Tern, Oh My Pi (OMP), SidePulse, and macOS versions exercised, and never describe a socket write as LED output
- [ ] `python3 scripts/render_assets.py --check` passes when `assets/`, `docs/media/`, or `docs/BRAND.md` changed
- [ ] Privacy invariants in `tests/test_privacy.py` are untouched (only the six agent fields, the six host fields, `surface.role`, and OMP tab names or pane titles are read; labels and host keys never leave coordinator memory; and this PR description and diff contain no real user transcripts, prompts, pane titles, or private paths)
- [ ] `CHANGELOG.md` is updated for user-visible changes
