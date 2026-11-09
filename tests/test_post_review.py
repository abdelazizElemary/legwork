"""post_review.py decides what may be posted. These are the rules that hold whatever the model drafts."""

import pytest

from conftest import config, install, load_module

ALL_STYLE = {"no_emoji": True, "no_headers": True, "no_bold": True, "no_tables": True, "no_bullets": True,
             "no_em_dash": True, "no_thanks_opener": True, "no_filler": True, "no_severity_labels": True,
             "max_words_inline": 20, "max_words_body": 40}


def checker(tmp_path, fakes, style=None, policy=None):
    cfg = config(tmp_path, fakes, style=style or {})
    if policy:
        cfg["review"]["policy"] = policy
    home = install(tmp_path, cfg)
    return load_module(home / ".claude/skills/pr-review/scripts/post_review.py", f"post_review_{id(cfg)}")


def lint(m, text, inline=True):
    errors, warnings = [], []
    m.lint_text("c", text, errors, warnings, inline)
    return errors, warnings


@pytest.fixture
def strict(tmp_path, fakes):
    return checker(tmp_path, fakes, style=ALL_STYLE)


@pytest.fixture
def neutral(tmp_path, fakes):
    return checker(tmp_path, fakes)


@pytest.mark.parametrize("text,needle", [
    ("thanks for this! `x` breaks", "thanks"), ("**bold** `x`", "bold"), ("# Header\n`x`", "header"),
    ("| a | b |\n`x`", "table"), ("fine 🚀 `x`", "emoji")])
def test_strict_style_blocks(strict, text, needle):
    errors, _ = lint(strict, text)
    assert any(needle in e for e in errors), errors


@pytest.mark.parametrize("text", ["thanks for this! `x` breaks", "**bold** `x`", "fine 🚀 `x`"])
def test_neutral_style_allows_what_the_person_allows(neutral, text):
    errors, _ = lint(neutral, text)
    assert not errors, errors


@pytest.mark.parametrize("text", ["Generated with Claude Code", "Co-Authored-By: x", "as an AI I think"])
def test_attribution_is_always_refused(neutral, text):
    errors, _ = lint(neutral, text)
    assert any("attribution" in e for e in errors)


def test_unclosed_fence_is_always_refused(neutral):
    errors, _ = lint(neutral, "```suggestion\nx = 1\n")
    assert any("unclosed" in e for e in errors)


def test_word_limit_and_em_dash_warn(strict):
    _, warnings = lint(strict, "`x` " + "word " * 30 + "— more")
    assert any("words" in w for w in warnings) and any("em dash" in w for w in warnings)


def test_thanks_check_is_not_fooled_by_a_word_that_starts_with_it(strict):
    errors, _ = lint(strict, "thanksgiving_date is parsed wrong at line 3")
    assert not [e for e in errors if "thanks" in e]


BLOCKING = [{"blocking": True, "body": "x"}]
MINOR = [{"blocking": False, "body": "x"}]


def test_silent_private_policy(neutral):
    e = neutral.event_errors
    assert e("APPROVE", "", []) == []
    assert any("silent" in x for x in e("APPROVE", "looks good", []))
    assert any("no inline comments" in x for x in e("APPROVE", "", MINOR))
    assert e("REQUEST_CHANGES", "", BLOCKING) == []
    assert any("approve instead" in x for x in e("REQUEST_CHANGES", "", MINOR))
    assert any("request changes instead" in x for x in e("APPROVE", "", BLOCKING))
    assert any("own PR" in x for x in e("APPROVE", "", [], own_pr=True))
    assert neutral.comment_policy_errors("c", {"blocking": False}, "REQUEST_CHANGES", "small thing")


def test_note_and_post_policy(tmp_path, fakes):
    m = checker(tmp_path, fakes, policy={"approve": "note", "minor": "post"})
    assert m.event_errors("APPROVE", "looks good, one optional note inline", MINOR) == []
    assert m.comment_policy_errors("c", {"blocking": False}, "APPROVE", "optional: rename this") == []
    assert any("request changes instead" in x for x in m.event_errors("APPROVE", "", BLOCKING))
