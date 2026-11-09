"""What must never be in the shared repo: personal data, secrets, someone's own config or profile. And every change
to agent code is released properly (version bump and changelog), so people pulling it know what changed."""

import json
import os
import re
import subprocess

import pytest

from conftest import KIT
from test_static import shipped

FILES = [p for p in shipped() if not str(p).startswith("tests/")]
TEXT = {}
for p in FILES:
    try:
        TEXT[str(p)] = (KIT / p).read_text()
    except (UnicodeDecodeError, OSError):
        pass

SECRETS = {
    "GitHub token": r"\bgh[pousr]_[A-Za-z0-9]{30,}", "Anthropic key": r"sk-ant-[A-Za-z0-9_-]{10,}",
    "AWS key": r"\bAKIA[0-9A-Z]{16}\b", "private key": r"-----BEGIN [A-Z ]*PRIVATE KEY-----",
    "Slack token": r"\bxox[baprs]-[A-Za-z0-9-]{10,}", "JWT": r"\beyJ[A-Za-z0-9_-]{15,}\.[A-Za-z0-9_-]{15,}\.",
}
# example addresses, GitHub's own, and Anthropic's noreply in the default attribution trailer
ALLOWED_EMAIL = re.compile(r"@(example\.(com|org)|users\.noreply\.github\.com)$|^(git@github\.com|noreply@anthropic\.com)$", re.I)


@pytest.mark.parametrize("name,pattern", SECRETS.items(), ids=list(SECRETS))
def test_no_secrets(name, pattern):
    hits = [f for f, t in TEXT.items() if re.search(pattern, t)]
    assert not hits, f"{name} in {hits}"


def test_no_personal_files_are_tracked():
    bad = [str(p) for p in shipped() if str(p) == "kit.config.json" or str(p).startswith(("profile/", "dist/", ".venv/"))
           or str(p).endswith(".bak")]
    assert not bad, f"these belong on one machine, not in the repo: {bad}"


def test_project_maps_never_ship():
    """A mapped project reference describes someone's own repos: it stays in their git-ignored references/."""
    bad = [str(p) for p in shipped() if p.parts[0] == "references" and str(p) != "references/README.md"]
    assert not bad, f"project maps stay on your machine (references/ is git-ignored): {bad}"


def test_no_home_paths():
    hits = {f: m.group(0) for f, t in TEXT.items() if (m := re.search(r"/(Users|home)/(?!runner/)[A-Za-z0-9._-]+/", t))}
    assert not hits, f"use ~ or a placeholder instead of someone's home folder: {hits}"


def test_no_real_email_addresses():
    found = {}
    for f, t in TEXT.items():
        for m in re.finditer(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(\.[A-Za-z0-9-]+)+", t):
            if not ALLOWED_EMAIL.search(m.group(0)) and not m.group(0).endswith((".py", ".md", ".json")):
                found.setdefault(f, []).append(m.group(0))
    assert not found, f"use example.com addresses in examples: {found}"


def test_no_private_terms():
    """KIT_PRIVATE_TERMS (a CI secret or your shell) lists words that must never ship: your name, alias, work email.
    Keeping them in a secret means the list itself never lands in the repo."""
    terms = [t.strip() for t in os.environ.get("KIT_PRIVATE_TERMS", "").split(",") if t.strip()]
    if not terms:
        pytest.skip("KIT_PRIVATE_TERMS not set")
    # CODEOWNERS has to name the maintainer's GitHub handle, it's the one file allowed to
    hits = {f: [t for t in terms if re.search(re.escape(t), text, re.I)] for f, text in TEXT.items() if f != ".github/CODEOWNERS"}
    hits = {f: h for f, h in hits.items() if h}
    assert not hits, f"private words in {sorted(hits)} (which words is not printed, check them yourself)"


def version(v):
    return tuple(int(x) for x in re.findall(r"\d+", v)[:3])


RELEASED = ("agents/", "skills/", "rules/", "service/", "install.py", "package.py")


def test_agent_changes_are_released():
    """On a PR (CI sets BASE_SHA): a change to what gets installed needs a new version and a changelog entry."""
    base = os.environ.get("BASE_SHA")
    if not base:
        pytest.skip("not a PR run")
    changed = subprocess.run(["git", "-C", str(KIT), "diff", "--name-only", f"{base}...HEAD"], capture_output=True,
                             text=True, check=True).stdout.split()
    if not any(f.startswith(RELEASED) for f in changed):
        pytest.skip("no installed files changed")
    old = json.loads(subprocess.run(["git", "-C", str(KIT), "show", f"{base}:kit.json"], capture_output=True, text=True,
                                    check=True).stdout)["version"]
    new = json.loads((KIT / "kit.json").read_text())["version"]
    assert version(new) > version(old), f"bump kit.json version (base has {old}) and add a CHANGELOG entry"
