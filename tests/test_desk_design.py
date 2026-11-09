"""Both desks share one Apple-style design: the system font, Apple's system colours with every text colour passing
4.5:1 on every surface in light and dark, no theme switch (they follow the system), and one Apple layer, word for word."""

import json
import re
import shutil
import subprocess

import pytest

from conftest import KIT, load_module

DESKS = ["agents/ticket-agent/dashboard.html", "agents/pr-review-agent/dashboard.html"]
TEXT = ["ink", "ink-2", "ink-3", "ps", "done", "turn", "bad"]
SURFACES = ["paper", "surface", "sunk", "sel", "turn-soft", "bad-soft", "good-soft"]


def page(rel):
    return (KIT / rel).read_text()


def tokens(css, dark):
    """The hex tokens of the light :root block, or of the dark one layered on top."""
    blocks = re.findall(r"@media \(prefers-color-scheme: dark\) \{\s*:root \{(.*?)\}", css, re.S) if dark else []
    light = re.search(r"<style>\n.*?:root \{(.*?)\n\}", css, re.S).group(1)
    out = dict(re.findall(r"--([a-z0-9-]+): (#[0-9a-f]{6});", light))
    for b in blocks[:1]:
        out.update(re.findall(r"--([a-z0-9-]+): (#[0-9a-f]{6});", b))
    return out


def ratio(a, b):
    def lum(h):
        r, g, bl = (int(h[i:i + 2], 16) / 255 for i in (1, 3, 5))
        f = lambda v: v / 12.92 if v <= 0.03928 else ((v + 0.055) / 1.055) ** 2.4
        return 0.2126 * f(r) + 0.7152 * f(g) + 0.0722 * f(bl)
    la, lb = lum(a), lum(b)
    return (max(la, lb) + 0.05) / (min(la, lb) + 0.05)


@pytest.mark.parametrize("rel", DESKS)
@pytest.mark.parametrize("dark", [False, True], ids=["light", "dark"])
def test_every_text_colour_passes_on_every_surface(rel, dark):
    t = tokens(page(rel), dark)
    low = [(fg, bg, round(ratio(t[fg], t[bg]), 2)) for fg in TEXT for bg in SURFACES if ratio(t[fg], t[bg]) < 4.5]
    assert not low, f"under 4.5:1: {low}"
    for fill in ("fill", "bad-fill"):
        assert ratio("#ffffff", t[fill]) >= 4.5, f"white on {fill}"


@pytest.mark.parametrize("rel", DESKS)
def test_system_font_no_web_fonts_and_no_theme_switch(rel):
    css = page(rel)
    assert "fonts.googleapis" not in css and "fonts.gstatic" not in css
    assert re.search(r"--sans: -apple-system, BlinkMacSystemFont, \"SF Pro Text\"", css)
    assert "data-theme" not in css and 'class="theme"' not in css, "the desks follow the system appearance"
    assert "@media (prefers-contrast: more)" in css, "Increase Contrast gets darker text and lines"


def test_the_apple_layer_is_the_same_in_both_desks():
    layers = [page(rel)[page(rel).index("/* ---------- Apple layer"):page(rel).rindex("</style>")] for rel in DESKS]
    assert layers[0] and layers[0] == layers[1]


@pytest.mark.parametrize("desk", ["ticket-agent", "pr-review-agent"])
def test_the_desks_no_longer_allow_web_fonts(desk):
    py = (KIT / f"agents/{desk}/dashboard.py").read_text()
    assert "fonts.googleapis" not in py and "fonts.gstatic" not in py and "font-src 'self'" in py


@pytest.mark.parametrize("rel", DESKS)
def test_each_count_in_the_sentence_is_a_whole_phrase(rel):
    js = page(rel)
    assert "rest.push([" in js and "rest.push(`" not in js, "a count is never a lone number to click"


@pytest.mark.skipif(not shutil.which("node"), reason="node not installed")
def test_the_plan_button_says_what_it_starts_and_what_it_costs(tmp_path):
    js = page("agents/ticket-agent/dashboard.html")
    fn = js[js.index("function planButton(st, acts) {"):js.index("/* a few ways to start")]
    cases = {
        "rework": [{"do": "note"}, {"do": "rework", "text": "x"}],
        "link_rework": [{"do": "link_pr", "repo": "demo", "number": 42}, {"do": "rework"}],
        "park": [{"do": "park"}],
        "check": [{"do": "check"}],
        "pr_step": [{"do": "fix", "how": "pr_step"}],
    }
    st = {"prs": [{"number": 7, "live": {"state": "open"}}]}
    f = tmp_path / "plan.js"
    f.write_text(f"const data = {{ budgets: {{ check: 4, implement: 40 }} }};\n{fn}\n"
                 f"const st = {json.dumps(st)};\nconsole.log(JSON.stringify(Object.fromEntries("
                 f"Object.entries({json.dumps(cases)}).map(([k, a]) => [k, planButton(st, a)]))));")
    out = json.loads(subprocess.run(["node", str(f)], capture_output=True, text=True, check=True).stdout)
    assert out["rework"] == ["Start rework on #7", "Up to $40 and usually 20 to 90 minutes. Nothing is pushed until its checks pass."]
    assert out["link_rework"][0] == "Link #42 and rework"
    assert out["park"] == ["Park it", ""] and out["check"][1].startswith("Up to $4,")
    assert out["pr_step"][0] == "Run the PR step again"


def test_the_ticket_desk_tells_the_page_what_runs_can_cost(home, fakes, monkeypatch):
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("FAKE_GH_LOG", str(fakes["log"]))
    D = load_module(home / ".claude/ticket-agent/dashboard.py", "ticket_desk_design")
    b = D.build_state()["budgets"]
    assert b == {"check": D.A.CFG["check_budget_usd"], "implement": D.A.CFG["implement_budget_usd"]}
