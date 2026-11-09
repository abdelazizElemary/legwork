"""The installer: a teammate gets a neutral kit, a profile makes it theirs, and nothing of theirs is overwritten."""

import json
import re
import subprocess
import sys

from conftest import KIT, config, install


def block(home):
    text = (home / ".claude/CLAUDE.md").read_text()
    return text[text.index("<!-- agent-kit:begin -->"):text.index("<!-- agent-kit:end -->")]


def test_neutral_install_has_no_placeholders_and_no_one_elses_voice(home):
    claude = home / ".claude"
    for f in claude.rglob("*"):
        if f.is_file() and f.suffix in (".md", ".json", ".py", ".sh", ".html") and f.name != "_template.md":
            left = re.findall(r"\{\{[A-Z_]+\}\}", f.read_text(errors="ignore"))
            assert not left, f"{f.relative_to(home)} has unfilled {left}"
    voice = (claude / "skills/pr-review/references/voice.md").read_text()
    assert "Test's voice" in voice and "None yet" in voice, "a person without a profile gets the neutral template"
    assert "- Writing style: follow" in block(home), "no profile means no personal habits in the rules"


def test_profile_voice_rules_and_severity_install(tmp_path, fakes):
    home = install(tmp_path, config(tmp_path, fakes), profile={
        "voice.md": "# Writing in {{AUTHOR_FIRST}}'s voice\n\nterse, lowercase.\n",
        "rules.md": "- Never push on fridays.\n",
        "severity.md": "## Severity\n\nOnly crashes block.\n"})
    refs = home / ".claude/skills/pr-review/references"
    assert (refs / "voice.md").read_text() == "# Writing in Test's voice\n\nterse, lowercase.\n"
    assert "- Never push on fridays." in block(home)
    assert "Only crashes block." in (refs / "policy.md").read_text()


def test_review_policy_and_style_reach_policy_json(tmp_path, fakes):
    cfg = config(tmp_path, fakes, style={"no_emoji": True, "max_words_inline": 50})
    cfg["review"]["policy"] = {"approve": "note", "minor": "post"}
    home = install(tmp_path, cfg)
    p = json.loads((home / ".claude/skills/pr-review/references/policy.json").read_text())
    assert p["policy"] == {"approve": "note", "minor": "post"}
    assert p["style"]["no_emoji"] is True and p["style"]["max_words_inline"] == 50 and p["style"]["no_bold"] is False
    assert "Minor points are posted too" in block(home)


def test_bad_policy_is_refused(tmp_path, fakes):
    cfg = config(tmp_path, fakes)
    cfg["review"]["policy"] = {"approve": "loud"}
    path = tmp_path / "cfg.json"
    path.write_text(json.dumps(cfg))
    out = subprocess.run([sys.executable, str(KIT / "install.py"), "--config", str(path), "--home", str(tmp_path / "h")],
                         capture_output=True, text=True)
    assert out.returncode != 0 and "review.policy" in out.stdout + out.stderr


def test_dry_run_writes_nothing(tmp_path, fakes):
    path = tmp_path / "cfg.json"
    path.write_text(json.dumps(config(tmp_path, fakes)))
    out = subprocess.run([sys.executable, str(KIT / "install.py"), "--config", str(path), "--home", str(tmp_path / "h")],
                         capture_output=True, text=True)
    assert out.returncode == 0 and "dry run" in out.stdout
    assert not (tmp_path / "h").exists()


def test_reinstall_keeps_the_persons_config_state_and_reference(tmp_path, fakes):
    cfg = config(tmp_path, fakes)
    home = install(tmp_path, cfg)
    agent_cfg = home / ".claude/ticket-agent/config.json"
    c = json.loads(agent_cfg.read_text())
    c["label"] = "Edited by hand"
    agent_cfg.write_text(json.dumps(c))
    state = home / ".claude/ticket-agent/state"
    state.mkdir(parents=True, exist_ok=True)
    (state / "DEMO-1.json").write_text('{"key": "DEMO-1"}')
    ref = home / ".claude/skills/pr-review/references/demo.md"
    ref.write_text("mapped by hand\n")
    install(tmp_path, cfg, extra=("--force",))
    assert json.loads(agent_cfg.read_text())["label"] == "Edited by hand"
    assert (state / "DEMO-1.json").exists()
    assert ref.read_text() == "mapped by hand\n"


def test_rules_block_keeps_the_rest_of_claude_md(tmp_path, fakes):
    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True)
    (home / ".claude/CLAUDE.md").write_text("- my own rule above the kit\n")
    install(tmp_path, config(tmp_path, fakes))
    text = (home / ".claude/CLAUDE.md").read_text()
    assert text.startswith("- my own rule above the kit\n") and text.count("<!-- agent-kit:begin -->") == 1


def test_rework_branch_setting_reaches_the_agent_and_a_bad_value_is_refused(tmp_path, fakes):
    def agent_config(home):
        return json.loads((home / ".claude/ticket-agent/config.json").read_text())

    for sub in "abc":
        (tmp_path / sub).mkdir()
    assert agent_config(install(tmp_path / "a", config(tmp_path / "a", fakes)))["rework_branch"] == "same", "the default keeps today's behaviour"
    cfg = config(tmp_path / "b", fakes)
    cfg["ticket"]["rework_branch"] = "new"
    assert agent_config(install(tmp_path / "b", cfg))["rework_branch"] == "new"
    bad = config(tmp_path / "c", fakes)
    bad["ticket"]["rework_branch"] = "sometimes"
    path = tmp_path / "c" / "bad.json"
    path.write_text(json.dumps(bad))
    out = subprocess.run([sys.executable, str(KIT / "install.py"), "--config", str(path), "--home", str(tmp_path / "c" / "home")],
                         capture_output=True, text=True)
    assert out.returncode != 0 and "rework_branch" in (out.stderr + out.stdout)
