"""The landing page in site/:
it builds, every local reference resolves, and nothing loads from another origin."""
from __future__ import annotations

import html.parser
import importlib.util
import json
import os
import re
import shutil
import tempfile
import unittest

from support import ROOT

SITE = os.path.join(ROOT, "site")
CANONICAL = "https://ploono.github.io/passnote/"
EXTERNAL = re.compile(r"^(?:[a-z][a-z0-9+.-]*:|//)", re.I)
# Tags whose src/href/srcset the browser fetches while loading the page.
LOADS = {"img", "source", "script", "link", "iframe", "video", "audio", "embed", "object", "track"}
VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "source", "track", "wbr"}


def load_build():
    spec = importlib.util.spec_from_file_location("site_build", os.path.join(SITE, "build.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def read(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def norm(text):
    return " ".join(text.split())


class Page(html.parser.HTMLParser):
    """Every start tag as (tag, attrs), and the text inside each element that has an id."""

    def __init__(self, source):
        super().__init__(convert_charrefs=True)
        self.tags, self.texts, self._open, self.parents = [], {}, [], []
        self.feed(source)
        self.close()

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        self.tags.append((tag, attrs))
        self.parents.append([(t, c) for t, _, c in self._open])
        if tag not in VOID:
            self._open.append((tag, attrs.get("id"), attrs.get("class", "")))
            if attrs.get("id"):
                self.texts[attrs["id"]] = ""

    def handle_startendtag(self, tag, attrs):
        self.tags.append((tag, dict(attrs)))
        self.parents.append([(t, c) for t, _, c in self._open])

    def handle_endtag(self, tag):
        for i in range(len(self._open) - 1, -1, -1):
            if self._open[i][0] == tag:
                del self._open[i:]
                return

    def handle_data(self, data):
        for _, ident, _ in self._open:
            if ident:
                self.texts[ident] += data

    def find(self, tag=None, **attrs):
        want = {key.replace("_", "-"): value for key, value in attrs.items()}
        return [a for t, a in self.tags if (tag is None or t == tag) and all(a.get(k) == v for k, v in want.items())]


def words(text):
    """Words a reader counts: whitespace-separated tokens with at least one letter or digit."""
    return [token for token in text.split() if re.search(r"\w", token)]


class VisibleWords(html.parser.HTMLParser):
    """The words a visitor reads without opening anything: text outside <head>, <script>, <style>
    and <details>, and outside any element whose id is in `skip`; only inside `within` if given."""

    def __init__(self, source, within=None, skip=()):
        super().__init__(convert_charrefs=True)
        self.words, self._open, self.within, self.skip = [], [], within, set(skip)
        self.feed(source)
        self.close()

    def handle_starttag(self, tag, attrs):
        if tag not in VOID:
            self._open.append((tag, dict(attrs).get("id")))

    def handle_endtag(self, tag):
        for i in range(len(self._open) - 1, -1, -1):
            if self._open[i][0] == tag:
                del self._open[i:]
                return

    def handle_data(self, data):
        tags = {t for t, _ in self._open}
        ids = {i for _, i in self._open if i}
        if tags & {"head", "script", "style", "details"} or ids & self.skip:
            return
        if self.within and self.within not in ids:
            return
        self.words += words(data)


def page():
    return Page(read(os.path.join(SITE, "index.html")))


def css_urls(css):
    return [u.strip("'\"") for u in re.findall(r"url\(\s*([^)]+?)\s*\)", css)]


class BuildTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.build = load_build()

    def test_build_copies_the_site_and_the_assets_it_uses(self):
        out = self.build.build(os.path.join(self.tmp, "_site"))
        self.assertTrue(os.path.isfile(os.path.join(out, "index.html")))
        self.assertFalse(os.path.exists(os.path.join(out, "build.py")))
        for rel in self.build.ASSETS:
            self.assertTrue(os.path.isfile(os.path.join(out, rel)), rel)

    def test_build_replaces_its_own_previous_output(self):
        out = os.path.join(self.tmp, "_site")
        self.build.build(out)
        with open(os.path.join(out, "stale.txt"), "w") as fh:
            fh.write("old")
        self.build.build(out)
        self.assertFalse(os.path.exists(os.path.join(out, "stale.txt")))

    def test_build_refuses_a_folder_it_did_not_make(self):
        keep = os.path.join(self.tmp, "keep.txt")
        with open(keep, "w") as fh:
            fh.write("mine")
        with self.assertRaises(self.build.BuildError):
            self.build.build(self.tmp)
        self.assertTrue(os.path.exists(keep))
        self.assertEqual(self.build.main([self.tmp]), 2)
        self.assertTrue(os.path.exists(keep))


class PageTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp()
        cls.out = load_build().build(os.path.join(cls.tmp, "_site"))
        cls.page = page()
        cls.css = read(os.path.join(SITE, "styles.css"))

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, True)

    def test_every_local_reference_resolves_in_the_build(self):
        missing = []
        for tag, attrs in self.page.tags:
            for key in ("src", "href", "srcset"):
                value = attrs.get(key)
                if not value or value.startswith("#") or EXTERNAL.match(value):
                    continue
                path = value.split("#")[0].split("?")[0].split()[0]
                if not os.path.exists(os.path.join(self.out, path)):
                    missing.append(f"<{tag} {key}={value}>")
        missing += [u for u in css_urls(self.css) if not os.path.exists(os.path.join(self.out, u))]
        self.assertEqual(missing, [])

    def test_nothing_loads_from_another_origin(self):
        outside = []
        for tag, attrs in self.page.tags:
            if tag not in LOADS or (tag == "link" and attrs.get("rel") == "canonical"):
                continue
            for key in ("src", "href", "srcset", "data"):
                if attrs.get(key) and EXTERNAL.match(attrs[key]) and not attrs[key].startswith("data:"):
                    outside.append(f"<{tag} {key}={attrs[key]}>")
        outside += [u for u in css_urls(self.css) if EXTERNAL.match(u)]
        self.assertNotIn("@import", self.css)
        self.assertEqual(outside, [])

    def test_page_structure(self):
        self.assertEqual(self.page.find("html")[0].get("lang"), "en")
        self.assertEqual(len(self.page.find("h1")), 1)
        for landmark in ("header", "main", "footer"):
            self.assertTrue(self.page.find(landmark), landmark)
        self.assertEqual(self.page.find("link", rel="canonical")[0]["href"], CANONICAL)
        self.assertTrue(self.page.find("meta", name="description")[0]["content"])
        self.assertEqual(self.page.find("meta", property="og:url")[0]["content"], CANONICAL)
        self.assertTrue(self.page.find("a", href="#main"), "skip link")

    def test_images_have_size_and_alt(self):
        for attrs in self.page.find("img"):
            for key in ("alt", "width", "height"):
                self.assertIn(key, attrs, attrs.get("src"))

    def test_scripts_are_deferred_or_data(self):
        for attrs in self.page.find("script"):
            self.assertTrue("defer" in attrs or attrs.get("type") == "application/ld+json", attrs)

    def test_fonts_are_self_hosted_with_their_licences(self):
        fonts = os.path.join(SITE, "fonts")
        for name in ("fraunces", "instrument-sans", "geist-mono"):
            self.assertIn(f"fonts/{name}-latin-wght-normal.woff2", self.css)
        for licence in ("Fraunces-OFL.txt", "InstrumentSans-OFL.txt", "GeistMono-OFL.txt"):
            self.assertIn("SIL OPEN FONT LICENSE", read(os.path.join(fonts, licence)).upper())

    def test_dark_mode_and_reduced_motion_are_handled(self):
        self.assertIn("prefers-color-scheme: dark", self.css)
        self.assertIn("prefers-reduced-motion: reduce", self.css)


class DemoTest(unittest.TestCase):
    """v2 demo: two session cards and a flying folded note, three short beats (brief v2)."""

    @classmethod
    def setUpClass(cls):
        cls.source = read(os.path.join(SITE, "index.html"))
        cls.page = Page(cls.source)
        cls.css = read(os.path.join(SITE, "styles.css"))
        cls.demo = re.search(r'<section[^>]*id="demo".*?</section>', cls.source, re.S).group(0)

    def test_three_beats_with_captions_of_at_most_six_words(self):
        beats = {attrs["data-beat"] for _, attrs in self.page.tags if "data-beat" in attrs}
        self.assertEqual(beats, {"1", "2", "3"})
        captions = re.findall(r"<li[^>]*>(.*?)</li>", re.search(r'<ol class="captions"[^>]*>(.*?)</ol>', self.demo, re.S).group(1), re.S)
        self.assertEqual(len(captions), 3)
        for caption in captions:
            self.assertLessEqual(len(words(re.sub(r"<[^>]+>", "", caption))), 6, caption)  # inline tags add no space

    def test_captions_play_inside_the_stage(self):
        for (tag, attrs), parents in zip(self.page.tags, self.page.parents):
            if tag == "ol" and attrs.get("class") == "captions":
                self.assertIn("stage", " ".join(c for _, c in parents).split())
                break
        else:
            self.fail("no captions list")

    def test_demo_is_at_most_25_words(self):
        self.assertLessEqual(len(VisibleWords(self.source, within="demo").words), 25)

    def test_demo_shows_the_story_without_transcript_lines(self):
        text = norm(self.page.texts["demo"])
        for phrase in ("tester", "app", "Sign up broken on phones", "Fixed"):
            self.assertIn(phrase, text)
        for transcript in ("ask:", "re=", "→you", "data-at"):
            self.assertNotIn(transcript, self.demo)

    def test_the_note_is_the_brand_symbol(self):
        symbol = read(os.path.join(ROOT, "assets", "symbol.svg"))
        ink = re.search(r'<path fill="#1b1714" d="([^"]+)"', symbol).group(1)
        self.assertIn(ink, self.demo)

    def test_hiding_only_happens_once_javascript_runs(self):
        # Keyframe steps only run under a .demo.is-js animation rule, so they are not hiding rules.
        css = re.sub(r"@keyframes[^{]*\{(?:[^{}]*\{[^{}]*\})*[^{}]*\}", "", self.css)
        hidden_rules = re.findall(r"([^{}]*)\{[^}]*opacity:0[;}]", css)
        self.assertTrue(hidden_rules)
        for selector in hidden_rules:
            self.assertIn(".demo.is-js", selector)

    def test_replay_button_is_named_and_starts_hidden(self):
        buttons = self.page.find("button", data_action="replay")
        self.assertEqual(len(buttons), 1)
        self.assertTrue(buttons[0].get("aria-label"))
        self.assertIn("hidden", buttons[0])

    def test_reduced_motion_gets_the_still(self):
        block = self.css.split("@media (prefers-reduced-motion: reduce)")[-1]
        self.assertIn("animation:none", block)
        js = read(os.path.join(SITE, "main.js"))
        body = js[js.index("function setupDemo("):]
        check = body.index('matchMedia("(prefers-reduced-motion: reduce)").matches) return;')
        for starter in ('classList.add("is-js")', "setPlaying(true)", "setTimeout("):
            self.assertLess(check, body.index(starter), starter)

    def test_looping_motion_can_be_paused_and_stops_on_its_own(self):
        # WCAG 2.2.2: motion that runs longer than 5 s next to other content needs a pause control.
        js = read(os.path.join(SITE, "main.js"))
        self.assertIn('"Pause the demo"', js)
        self.assertIn('"Replay the demo"', js)
        self.assertRegex(js, r"MAX_LOOPS = [1-5];")
        stop_branch = re.search(r"if \(loops >= MAX_LOOPS\) \{([^}]*)\}", js)
        self.assertIsNotNone(stop_branch)
        self.assertIn("stop();", stop_branch.group(1))
        self.assertIn("return;", stop_branch.group(1))
        self.assertRegex(js, r"function stop\(\) \{ clearTimeout\(timer\);")

    def test_rest_frame_shows_the_note(self):
        # Before it plays (and between loops) the stage is the still composition, not two empty cards.
        self.assertIn('.demo.is-js .stage[data-step="0"] .note-out{opacity:1}', self.css)

    def test_caret_blinks_only_while_playing(self):
        rules = re.findall(r"([^{}]*)\{[^}]*animation:caret", self.css)
        self.assertTrue(rules)
        for selector in rules:
            self.assertIn(".is-playing", selector)
        self.assertIn('stage.classList.toggle("is-playing", on)', read(os.path.join(SITE, "main.js")))

    def test_captions_do_not_fade_on_first_paint(self):
        transitions = re.findall(r"([^{}]*\.captions li[^{}]*)\{[^}]*transition:", self.css)
        self.assertTrue(transitions)
        for selector in transitions:
            self.assertIn(".is-ready", selector)
        # Two frames: the first lets the browser style the is-js state, so nothing transitions from it.
        self.assertIn('requestAnimationFrame(function () { requestAnimationFrame(function () { demo.classList.add("is-ready"); }); });',
                      read(os.path.join(SITE, "main.js")))

    def test_script_is_local_and_deferred(self):
        scripts = self.page.find("script", src="main.js")
        self.assertEqual(len(scripts), 1)
        self.assertIn("defer", scripts[0])


class ContentTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = read(os.path.join(SITE, "index.html"))
        cls.page = Page(cls.source)
        cls.readme = read(os.path.join(ROOT, "README.md"))

    def test_sections_come_in_the_brief_order(self):
        order = ["hero", "demo", "how", "qlane", "faq"]
        positions = [self.source.find(f'id="{ident}"') for ident in order]
        self.assertNotIn(-1, positions)
        self.assertEqual(positions, sorted(positions))

    def test_install_commands_match_the_readme_and_sit_in_the_hero(self):
        block = re.search(r"In Claude Code:\n```\n(.*?)\n```", self.readme, re.S).group(1).splitlines()
        self.assertEqual([norm(self.page.texts[i]) for i in ("cmd-marketplace", "cmd-install")], block)
        hero = norm(self.page.texts["hero"])
        for command in block:
            self.assertIn(command, hero)

    def test_copy_buttons_point_at_the_commands_and_start_hidden(self):
        buttons = self.page.find("button", **{"class": "copy"})
        self.assertEqual(sorted(b["data-copy"] for b in buttons), ["cmd-install", "cmd-marketplace"])
        for attrs in buttons:
            self.assertIn("hidden", attrs)
            self.assertTrue(attrs.get("aria-label"))

    def test_copy_feedback_is_announced_and_commands_are_not_translated(self):
        live = self.page.find(id="copy-status")
        self.assertEqual(len(live), 1)
        self.assertEqual(live[0].get("aria-live"), "polite")
        self.assertIn('getElementById("copy-status")', read(os.path.join(SITE, "main.js")))
        for ident in ("cmd-marketplace", "cmd-install"):
            self.assertEqual(self.page.find("code", id=ident)[0].get("translate"), "no")

    def test_qlane_band_says_what_it_is(self):
        text = norm(self.page.texts["qlane"])
        for phrase in ("separate, paid cloud service", "passnote stays free"):
            self.assertIn(phrase, text)
        self.assertNotRegex(text, r"\$\d")  # no prices
        self.assertTrue(self.page.find("a", href="https://qlane.ai"))

    def test_diagrams_reuse_the_readme_alt_texts(self):
        readme_alts = dict((path, alt) for alt, path in re.findall(r"!\[([^\]]+)\]\((assets/readme/[a-z]+\.svg)\)", self.readme))
        for name in ("architecture", "delivery", "wake"):
            path = f"assets/readme/{name}.svg"
            imgs = self.page.find("img", src=path)
            self.assertEqual(len(imgs), 1, path)
            self.assertEqual(imgs[0]["alt"], readme_alts[path])

    def test_diagrams_sit_in_scrollable_frames_with_full_size_links(self):
        for (tag, attrs), parents in zip(self.page.tags, self.page.parents):
            if tag == "img" and attrs.get("src", "").startswith("assets/readme/"):
                self.assertIn("details", [t for t, _ in parents], attrs["src"])
                self.assertIn("frame", " ".join(c for _, c in parents).split(), attrs["src"])
                self.assertTrue(self.page.find("a", href=attrs["src"]), attrs["src"])

    def test_details_are_behind_disclosures(self):
        summaries = re.findall(r"<summary[^>]*>(.*?)</summary>", self.source, re.S)
        plain = [norm(re.sub(r"<[^>]+>", " ", s)) for s in summaries]
        self.assertIn("Show me how it works", plain)
        self.assertIn("See the numbers", plain)

    def test_tiles_stay_put_when_details_open(self):
        # Detail opens in full-width rows under the tiles, so opening one never reflows the tiles.
        for (tag, _), parents in zip(self.page.tags, self.page.parents):
            if tag == "details":
                self.assertNotIn("tile", " ".join(c for _, c in parents).split())

    def test_chart_labels_are_text_so_they_stay_readable_on_phones(self):
        chart = re.search(r'<figure class="chart".*?</figure>', self.source, re.S).group(0)
        self.assertNotIn("<text", chart)
        text = norm(re.sub(r"<[^>]+>", " ", chart))
        for phrase in ("Waking an idle session", "15,000", "25,000", "A note", "tens"):
            self.assertIn(phrase, text)

    def test_faq_markers_stay_out_of_the_questions_accessible_names(self):
        # Generated text in ::before/::after joins a summary's accessible name ("Is it free? +").
        css = read(os.path.join(SITE, "styles.css"))
        for marker in ('content:"+"', 'content:"−"'):
            self.assertNotIn(marker, css)

    def test_text_colours_meet_aa_contrast_in_both_themes(self):
        css = read(os.path.join(SITE, "styles.css"))
        light = dict(re.findall(r"--([\w-]+):(#[0-9a-f]{6})", css.split("@media (prefers-color-scheme: dark)")[0]))
        dark = dict(light, **dict(re.findall(r"--([\w-]+):(#[0-9a-f]{6})", css.split("@media (prefers-color-scheme: dark)")[1].split("}\n}")[0])))

        def luminance(hex_colour):
            channels = [int(hex_colour[i:i + 2], 16) / 255 for i in (1, 3, 5)]
            linear = [c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4 for c in channels]
            return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]

        def ratio(a, b):
            hi, lo = sorted((luminance(a), luminance(b)), reverse=True)
            return (hi + 0.05) / (lo + 0.05)

        pairs = [("ink", "paper"), ("graphite", "paper"), ("graphite", "surface"), ("ruled", "paper"),
                 ("on-ink", "band"), ("band-accent", "band")]
        for theme, tokens in (("light", light), ("dark", dark)):
            for fg, bg in pairs:
                self.assertGreaterEqual(ratio(tokens[fg], tokens[bg]), 4.5, (theme, fg, bg))

    def test_safety_copy_claims_only_what_the_code_does(self):
        # Holds go both ways and can be lifted, the header is advisory, delivery goes through Claude
        # Code, wakes fire for more than "needs an answer", and the secret guard is a heuristic.
        text = norm(self.page.texts["how"])
        for claim in (
            "Notes between a session that asks before acting and one that doesn’t are held both ways and shown only to you.",
            "Every note arrives marked as coming from another session, not from you.",
            "Delivered notes reach Claude through your usual Claude Code connection.",
            "passnote refuses to post text that looks like an API key, token or private key.",
            "Idle sessions are woken only when a note needs them now.",
            "a guard against posting keys",
        ):
            self.assertIn(claim, text)
        for overclaim in ("won’t take orders", "never as something you typed", "no secrets",
                          "password or an API key", "needs an answer now"):
            self.assertNotIn(overclaim, text)
        llms = read(os.path.join(SITE, "llms.txt"))
        self.assertIn("Idle sessions are woken only when a note needs them now.", llms)
        self.assertNotIn("needs an answer now", llms)
        self.assertIn("in the same project", norm(self.page.texts["install"]))

    def test_faq_has_four_answers_of_20_to_45_words(self):
        questions = [ident for _, attrs in self.page.tags for ident in [attrs.get("id", "")] if re.fullmatch(r"q-\d", ident)]
        self.assertEqual(len(questions), 4)
        for i in range(1, 5):
            self.assertTrue(norm(self.page.texts[f"q-{i}"]).endswith("?"))
            count = len(norm(self.page.texts[f"a-{i}"]).split())
            self.assertTrue(20 <= count <= 45, (i, count))

    def test_visible_copy_stays_within_budget(self):
        self.assertLessEqual(len(VisibleWords(self.source, skip={"faq"}).words), 220)
        self.assertLessEqual(len(VisibleWords(self.source, within="hero", skip={"install"}).words), 35)

    def test_grids_cannot_be_widened_by_wide_content(self):
        # The install commands never wrap and the diagrams keep a 760px minimum inside their
        # scrolling frames; an auto grid column would grow to fit them and push the block past a
        # phone's width (the hero's overflow:hidden even hides that from scrollWidth).
        css = read(os.path.join(SITE, "styles.css"))
        for selector in (".hero-grid", ".tiles", ".numbers"):
            rule = re.search(re.escape(selector) + r"\{([^}]*)\}", css).group(1)
            self.assertIn("grid-template-columns:minmax(0,1fr)", rule, selector)

    def test_requirements_match_the_readme_and_the_doctor(self):
        from passnote import doctor
        claude = re.search(r"^- Claude Code (\d+\.\d+\.\d+) or newer", self.readme, re.M).group(1)
        python, systems = re.search(r"^- python3 (\d+\.\d+) or newer, on (\w+ or \w+);", self.readme, re.M).groups()
        self.assertEqual(claude, doctor.MIN_CLAUDE_TEXT)
        text = norm(self.page.texts["hero"])
        for requirement in (f"Claude Code {claude} or newer", f"Python {python} or newer", systems):
            self.assertIn(requirement, text)


class DiscoveryTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.page = page()
        cls.data = json.loads(cls.page.texts["structured-data"])
        cls.graph = {node["@type"]: node for node in cls.data["@graph"]}

    def test_structured_data_describes_the_app(self):
        app = self.graph["SoftwareApplication"]
        plugin = json.loads(read(os.path.join(ROOT, "plugin", ".claude-plugin", "plugin.json")))
        self.assertEqual(app["url"], CANONICAL)
        self.assertEqual(app["softwareVersion"], plugin["version"])
        self.assertEqual(app["offers"]["price"], "0")
        self.assertEqual(app["author"]["name"], plugin["author"]["name"])
        self.assertEqual(self.graph["WebSite"]["url"], CANONICAL)
        self.assertIn(f"v{plugin['version']}", norm(self.page.texts["site-footer"]))

    def test_faq_structured_data_matches_the_visible_faq(self):
        entities = self.graph["FAQPage"]["mainEntity"]
        self.assertEqual(len(entities), len([k for k in self.page.texts if re.fullmatch(r"q-\d", k)]))
        for i, entity in enumerate(entities, 1):
            self.assertEqual(entity["name"], norm(self.page.texts[f"q-{i}"]))
            self.assertEqual(entity["acceptedAnswer"]["text"], norm(self.page.texts[f"a-{i}"]))

    def test_discovery_files(self):
        robots = read(os.path.join(SITE, "robots.txt"))
        self.assertIn(f"Sitemap: {CANONICAL}sitemap.xml", robots)
        self.assertIn(f"<loc>{CANONICAL}</loc>", read(os.path.join(SITE, "sitemap.xml")))
        llms = read(os.path.join(SITE, "llms.txt"))
        self.assertTrue(llms.startswith("# passnote\n"))
        for command in ("/plugin marketplace add ploono/passnote", "/plugin install passnote@passnote"):
            self.assertIn(command, llms)


class WorkflowTest(unittest.TestCase):
    def test_pages_workflow_builds_and_deploys_with_pinned_actions(self):
        flow = read(os.path.join(ROOT, ".github", "workflows", "pages.yml"))
        self.assertIn("python3 site/build.py _site", flow)
        self.assertIn("path: _site", flow)
        for line in re.findall(r"uses:\s*(\S+)", flow):
            self.assertRegex(line, r"^[\w.-]+/[\w.-]+@[0-9a-f]{40}$")
        block = re.search(r"^permissions:\n((?:  .*\n)+)", flow, re.M).group(1)
        self.assertEqual(block, "  contents: read\n  pages: write\n  id-token: write\n")
        self.assertEqual(len(re.findall(r"^\s*(?:- )?uses:", flow, re.M)), 4)
        self.assertIn("persist-credentials: false", flow)
        for path in ("site/**", "assets/**", ".github/workflows/pages.yml"):
            self.assertIn(path, flow)


if __name__ == "__main__":
    unittest.main()
