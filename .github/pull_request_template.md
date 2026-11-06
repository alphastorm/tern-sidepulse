## Summary

<!-- What problem does this change solve? -->

## Verification

- [ ] `python3 -B -m unittest discover -s tests -t . -v` passes
- [ ] `luau-lsp analyze --definitions=types/tern.d.luau window.luau` passes
- [ ] Documentation facts name the exact Tern, Oh My Pi (OMP), SidePulse, and macOS versions exercised, and never describe a socket write as LED output
- [ ] `python3 scripts/render_assets.py --check` passes when `assets/`, `docs/media/`, or `docs/BRAND.md` changed
- [ ] Privacy invariants in `tests/test_privacy.py` are untouched (only the six agent fields, four host fields, and `surface.role` are read; no transcripts, prompts, titles, or private paths appear in this PR)
- [ ] `CHANGELOG.md` is updated for user-visible changes
