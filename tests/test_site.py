"""README, plugin.toml, window.luau, assets/, docs/media/, site/ and the Pages workflow stay coherent (docs/BRAND.md).

The site carries no copies of brand or media assets: `.github/workflows/pages.yml` stages them at deploy time, so a
page may only reference a file that is tracked under `site/` or staged by that workflow. The one name
`tern-sidepulse` must agree across the plugin manifest, the palette commands, the README and the site, and the
rendered demo media must match the sources they were rendered from. Host-free: no Chrome, ffmpeg or network.
"""

from __future__ import annotations

import fnmatch
import hashlib
import json
import re
import subprocess
import unittest
from html.parser import HTMLParser
from pathlib import Path

from scripts.render_assets import (
    ASSETS,
    DEMO,
    DEMO_SOURCES,
    GIF_WIDTH,
    MEDIA,
    MEDIA_FILES,
    ROOT,
    TARGETS,
    gif_dimensions,
    png_dimensions,
    source_digest,
)

SITE = ROOT / "site"
SITE_URL = "https://alphastorm.github.io/tern-sidepulse/"
WORKFLOW = ROOT / ".github/workflows/pages.yml"
NAME = "tern-sidepulse"
COMMANDS = ("doctor", "resync", "disable", "enable")
DISCLAIMER = "Community project; not affiliated with or endorsed by the Oh My Pi, Tern, or SidePulse maintainers."
SUB_LINE = "Show the OMP sessions in your Tern windows on SidePulse: working, waiting, done, failed."
# Built by concatenation so this file never matches its own retired-name scan.
RETIRED = ("SidePulse " + "bridge", "SidePulse " + "for OMP")
BADGE_VALUES = {"70B3F7", "1C232B"}
TEXT_SUFFIXES = {".md", ".py", ".luau", ".toml", ".yml", ".yaml", ".html", ".css", ".txt", ".xml", ".json", ".gitignore"}


def staged() -> dict[str, str]:
    """`site/<name>` -> source path for every `cp <source> site/<name>` line of the Pages workflow."""
    pairs = re.findall(r"^\s*cp (\S+) site/(\S+)\s*$", WORKFLOW.read_text(encoding="utf-8"), re.M)
    return {name: source for source, name in pairs}


def pages_paths() -> list[str]:
    """The `paths:` filter entries of the Pages workflow's push trigger."""
    text = WORKFLOW.read_text(encoding="utf-8")
    block = re.search(r"^\s*paths:\n((?:\s*- .*\n)+)", text, re.M)
    assert block is not None, "pages.yml has no paths filter"
    return [line.strip()[2:].strip().strip("'\"") for line in block[1].splitlines()]


def tracked_text_files() -> list[Path]:
    try:
        out = subprocess.run(["git", "ls-files", "-z"], cwd=ROOT, check=True, capture_output=True).stdout
        names = [n for n in out.decode("utf-8").split("\0") if n]
    except (OSError, subprocess.CalledProcessError):
        names = [str(p.relative_to(ROOT)) for p in ROOT.rglob("*") if p.is_file() and ".git" not in p.parts]
    files = []
    for name in names:
        path = ROOT / name
        if not path.is_file() or path.suffix not in TEXT_SUFFIXES and path.name != ".gitignore":
            continue
        if name.startswith(("prompt-exports/", "licenses/")):
            continue
        files.append(path)
    return files


class References(HTMLParser):
    """Local hrefs/srcs plus the metadata the site's URLs must agree on."""

    def __init__(self) -> None:
        super().__init__()
        self.local: list[tuple[str, str]] = []
        self.meta: dict[str, str] = {}
        self.canonical: str | None = None
        self.remote_loads: list[str] = []
        self.anchors: list[str] = []

    def handle_starttag(self, tag, attrs):
        a = {key: value or "" for key, value in attrs}
        prop = a.get("property")
        if tag == "meta" and prop:
            self.meta[prop] = a.get("content", "")
        if tag == "link" and a.get("rel") == "canonical":
            self.canonical = a.get("href")
        if tag == "a" and a.get("href", "").startswith("#"):
            self.anchors.append(a["href"][1:])
        urls: list[str] = []
        if tag in ("img", "script", "source") and a.get("src"):
            urls.append(a["src"])
        if tag == "video":
            urls += [a[k] for k in ("src", "poster") if a.get(k)]
        if tag == "link" and a.get("rel") in ("stylesheet", "icon", "preload", "manifest") and a.get("href"):
            urls.append(a["href"])
        for url in urls:
            if url.startswith(("http://", "https://", "//")):
                self.remote_loads.append(url)
            else:
                self.local.append((tag, url))


def parse(path: Path) -> References:
    refs = References()
    refs.feed(path.read_text(encoding="utf-8"))
    return refs


def css_urls(path: Path) -> list[str]:
    return [u.strip("'\"") for u in re.findall(r"url\(([^)]+)\)", path.read_text(encoding="utf-8"))]


def plugin_toml(field: str) -> str:
    match = re.search(rf'^{field}\s*=\s*"([^"]*)"', (ROOT / "plugin.toml").read_text(encoding="utf-8"), re.M)
    assert match is not None, f"plugin.toml has no {field}"
    return match[1]


def luau_commands() -> dict[str, str]:
    text = (ROOT / "window.luau").read_text(encoding="utf-8")
    return dict(re.findall(r'tern\.command\(\{\s*id = "(\w+)",\s*title = "([^"]+)"', text))


def compat_versions() -> dict[str, str]:
    text = (ROOT / "docs/compatibility.md").read_text(encoding="utf-8")
    tern = re.search(r"^\| Tern \| (\d+\.\d+\.\d+) ", text, re.M)
    omp = re.search(r"^\| OMP \| (\d+\.\d+\.\d+) ", text, re.M)
    sidepulse = re.search(r"^\| SidePulse \| ([\w.+]+) ", text, re.M)
    assert tern and omp and sidepulse, "compatibility.md version table changed shape"
    revision = re.search(r"g([0-9a-f]{8})", sidepulse[1])
    assert revision is not None
    return {"tern": tern[1], "omp": omp[1], "sidepulse": sidepulse[1], "revision": revision[1]}


def github_slug(heading: str) -> str:
    slug = re.sub(r"[^\w\- ]", "", heading.strip().lower())
    return slug.replace(" ", "-")


class SiteTests(unittest.TestCase):
    def test_staged_assets_have_a_source_and_are_ignored(self):
        names = staged()
        self.assertEqual({"logo.svg", "favicon.svg", "og.png", "demo.mp4", "demo-poster.png"}, set(names))
        ignored = (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
        for name, source in names.items():
            self.assertTrue((ROOT / source).is_file(), source)
            self.assertIn(f"/site/{name}", ignored)

    def test_pages_paths_cover_every_staged_source(self):
        patterns = pages_paths()
        self.assertIn("site/**", patterns)
        self.assertIn(".github/workflows/pages.yml", patterns)
        for source in staged().values():
            self.assertTrue(any(fnmatch.fnmatch(source, p) or source == p for p in patterns), f"{source} would not redeploy")

    def test_every_local_reference_is_served(self):
        names = staged()
        refs = parse(SITE / "index.html")
        self.assertTrue(refs.local)
        for tag, url in refs.local:
            self.assertTrue(url in names or (SITE / url).is_file(), f"<{tag}> references {url!r}")
        for url in css_urls(SITE / "site.css"):
            self.assertTrue(url in names or (SITE / url).is_file(), f"site.css references {url!r}")

    def test_site_and_readme_load_nothing_remote(self):
        refs = parse(SITE / "index.html")
        self.assertEqual([], refs.remote_loads)
        for name in ("index.html", "site.css"):
            self.assertNotIn("fonts.googleapis", (SITE / name).read_text(encoding="utf-8"))
            self.assertNotIn("gstatic", (SITE / name).read_text(encoding="utf-8"))
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        remote = re.findall(r'(?:src="|!\[[^\]]*\]\()(https?://[^\s")]+)', readme)
        self.assertEqual([], [u for u in remote if not u.startswith("https://img.shields.io/")])

    def test_canonical_sitemap_robots_and_llms_agree(self):
        refs = parse(SITE / "index.html")
        self.assertEqual(SITE_URL, refs.canonical)
        self.assertEqual(SITE_URL, refs.meta.get("og:url"))
        locs = re.findall(r"<loc>([^<]+)</loc>", (SITE / "sitemap.xml").read_text(encoding="utf-8"))
        self.assertEqual([SITE_URL], locs)
        self.assertIn(f"Sitemap: {SITE_URL}sitemap.xml", (SITE / "robots.txt").read_text(encoding="utf-8"))
        self.assertIn(SITE_URL, (SITE / "llms.txt").read_text(encoding="utf-8"))
        for anchor in refs.anchors:
            self.assertIn(f'id="{anchor}"', (SITE / "index.html").read_text(encoding="utf-8"), anchor)

    def test_social_preview_matches_the_rendered_png(self):
        refs = parse(SITE / "index.html")
        self.assertEqual(f"{SITE_URL}og.png", refs.meta.get("og:image"))
        self.assertEqual("assets/og.png", staged()["og.png"])
        width, height = png_dimensions(ASSETS / "og.png")
        self.assertEqual((str(width), str(height)), (refs.meta.get("og:image:width"), refs.meta.get("og:image:height")))
        self.assertLess((ASSETS / "og.png").stat().st_size, 1_000_000)

    def test_rendered_media_have_the_declared_dimensions(self):
        for name, (width, height, scale) in TARGETS.items():
            self.assertEqual((width * scale, height * scale), png_dimensions(ASSETS / f"{name}.png"), name)
        width, height, scale = DEMO
        self.assertEqual((width * scale, height * scale), png_dimensions(MEDIA / "tern-sidepulse-demo-poster.png"))
        self.assertEqual((GIF_WIDTH, GIF_WIDTH * height // width), gif_dimensions(MEDIA / "tern-sidepulse-demo.gif"))

    def test_media_manifest_is_current(self):
        manifest = json.loads((MEDIA / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(sorted(DEMO_SOURCES), sorted(manifest["source"]["files"]))
        self.assertEqual(source_digest(DEMO_SOURCES), manifest["source"]["sha256"], "demo sources changed; re-render docs/media")
        for name, meta in manifest["outputs"].items():
            data = (MEDIA / name).read_bytes()
            self.assertEqual(meta["sha256"], hashlib.sha256(data).hexdigest(), name)
            self.assertEqual(meta["bytes"], len(data), name)
        self.assertEqual(set(MEDIA_FILES), {p.name for p in MEDIA.iterdir() if p.name != ".DS_Store"})

    def test_one_name_everywhere(self):
        self.assertEqual(NAME, plugin_toml("id"))
        self.assertEqual(NAME, plugin_toml("name"))
        self.assertEqual(SUB_LINE, plugin_toml("description"))
        expected = {command: f"{NAME}: {command}" for command in COMMANDS}
        self.assertEqual(expected, luau_commands())
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        self.assertRegex(readme, rf"(?m)^# {NAME}$", "README H1")
        self.assertEqual(set(expected.values()), set(re.findall(rf"^\| `({NAME}: \w+)` \|", readme, re.M)), "README Commands table")
        for surface in (readme, (SITE / "index.html").read_text(encoding="utf-8"), (ROOT / "docs/BRAND.md").read_text(encoding="utf-8")):
            self.assertTrue(set(re.findall(rf"{NAME}: (\w+)", surface)) <= set(COMMANDS))

    def test_retired_names_are_gone(self):
        for path in tracked_text_files():
            if path == ROOT / "docs/BRAND.md":
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
            for retired in RETIRED:
                self.assertNotIn(retired, text, f"{path.relative_to(ROOT)} still says {retired!r}")

    def test_versions_agree(self):
        from tern_sidepulse import VERSION

        self.assertEqual(VERSION, plugin_toml("version"))
        luau = re.search(r'^local VERSION = "([^"]+)"', (ROOT / "window.luau").read_text(encoding="utf-8"), re.M)
        self.assertEqual(VERSION, luau and luau[1])
        changelog = re.search(r"^## (\d+\.\d+\.\d+)", (ROOT / "CHANGELOG.md").read_text(encoding="utf-8"), re.M)
        self.assertEqual(VERSION, changelog and changelog[1])

    def test_readme_header_and_badges_name_the_exercised_versions(self):
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        for mark in ("assets/logo.svg", "assets/logo-light.svg"):
            self.assertIn(f'srcset="{mark}"', readme)
            self.assertTrue((ROOT / mark).is_file(), mark)
        self.assertIn('src="docs/media/tern-sidepulse-demo.gif"', readme)
        self.assertTrue((MEDIA / "tern-sidepulse-demo.gif").is_file())
        badges = re.findall(r"https://img\.shields\.io/\S+", readme)
        self.assertGreaterEqual(len(badges), 4)
        for badge in badges:
            self.assertIn("labelColor=0B0E11", badge)
            value = re.search(r"(?:color=|badge/[^-]+-[^-]+-)([0-9A-Fa-f]{6})", badge)
            assert value is not None, badge
            self.assertIn(value[1].upper(), BADGE_VALUES, badge)
        versions = compat_versions()
        self.assertIn(f"badge/Tern%20baseline-{versions['tern']}-", readme)
        self.assertIn(f"OMP `{versions['omp']}`", readme)
        self.assertIn(f"revision `{versions['revision']}`", readme)
        headings = {github_slug(h) for h in re.findall(r"^#+ (.+)$", readme, re.M)}
        for anchor in re.findall(r"\]\(#([^)]+)\)", readme):
            self.assertIn(anchor, headings)

    def test_site_names_the_exercised_versions(self):
        versions = compat_versions()
        index = (SITE / "index.html").read_text(encoding="utf-8")
        for value in (versions["tern"], versions["omp"], versions["revision"]):
            self.assertIn(value, index)

    def test_owned_surfaces_carry_the_disclaimer(self):
        for name in ("README.md", "site/index.html", "site/llms.txt", "assets/og.html", "assets/banner.html", "assets/demo.html", "docs/media/README.md"):
            with self.subTest(surface=name):
                self.assertIn(DISCLAIMER, (ROOT / name).read_text(encoding="utf-8"))

    def test_documented_test_count_is_current(self):
        count = unittest.defaultTestLoader.discover(str(ROOT / "tests"), top_level_dir=str(ROOT)).countTestCases()
        for name in ("docs/compatibility.md", "CHANGELOG.md"):
            text = (ROOT / name).read_text(encoding="utf-8")
            claims = re.findall(r"(\d+) unit and integration tests|The (\d+) tests|Ran (\d+) tests", text)
            self.assertTrue(claims, f"{name} no longer states the test count")
            for claim in claims:
                self.assertEqual(count, int(next(c for c in claim if c)), f"{name} states a stale test count")


if __name__ == "__main__":
    unittest.main()
