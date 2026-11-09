"""Everything parses, and kit.json describes exactly the files the kit ships."""

import hashlib
import json
import py_compile
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from conftest import KIT

SKIP = {"dist", "__pycache__", ".git", "profile", ".playwright-mcp", ".pytest_cache", ".venv"}


def shipped():
    """The files the repo ships: git's own list (tracked plus new, minus ignored), or a walk without git."""
    if (KIT / ".git").exists():
        out = subprocess.run(["git", "-C", str(KIT), "ls-files", "--cached", "--others", "--exclude-standard"],
                             capture_output=True, text=True, check=True).stdout.split("\n")
        for line in sorted(set(out)):
            if line and (KIT / line).is_file():
                yield Path(line)
        return
    for f in sorted(KIT.rglob("*")):
        rel = f.relative_to(KIT)
        if f.is_file() and not set(rel.parts) & SKIP and f.name not in {"kit.config.json", ".DS_Store"} \
                and not f.name.endswith((".pyc", ".bak")):
            yield rel


PY = [p for p in shipped() if p.suffix == ".py" or str(p).endswith(("shims/gh", "shims/git"))]
SH = [p for p in shipped() if p.suffix == ".sh"]


@pytest.mark.parametrize("rel", PY, ids=str)
def test_python_compiles(rel, tmp_path):
    py_compile.compile(str(KIT / rel), cfile=str(tmp_path / "x.pyc"), doraise=True)


@pytest.mark.parametrize("rel", SH, ids=str)
def test_shell_parses(rel):
    out = subprocess.run(["bash", "-n", str(KIT / rel)], capture_output=True, text=True)
    assert out.returncode == 0, out.stderr


@pytest.mark.skipif(not shutil.which("node"), reason="node not installed")
@pytest.mark.parametrize("page", ["agents/pr-review-agent/dashboard.html", "agents/ticket-agent/dashboard.html"])
def test_desk_scripts_parse(page, tmp_path):
    js = re.findall(r"<script>(.*?)</script>", (KIT / page).read_text(), re.S)
    assert js, "no inline script found"
    f = tmp_path / "desk.js"
    f.write_text("\n;\n".join(js))
    out = subprocess.run(["node", "--check", str(f)], capture_output=True, text=True)
    assert out.returncode == 0, out.stderr


@pytest.mark.parametrize("rel", [p for p in shipped() if p.suffix == ".json"], ids=str)
def test_json_parses(rel):
    json.loads((KIT / rel).read_text())


def test_manifest_lists_every_file_and_nothing_else():
    k = json.loads((KIT / "kit.json").read_text())
    listed = {f["path"] for f in k["files"]}
    files = {str(p) for p in shipped() if not str(p).startswith("tests/") and not str(p).startswith(".github/")}
    assert files - listed == set(), f"add these to kit.json files: {sorted(files - listed)}"
    assert listed - files == set(), f"kit.json lists missing files: {sorted(listed - files)}"


def test_manifest_hashes_are_current():
    """package.py refreshes them; a stale hash means a change skipped the release steps."""
    k = json.loads((KIT / "kit.json").read_text())
    stale = [f["path"] for f in k["files"] if f.get("sha256") and
             hashlib.sha256((KIT / f["path"]).read_bytes()).hexdigest()[:16] != f["sha256"]]
    assert not stale, f"run python3 package.py to refresh kit.json: {stale}"


def test_changelog_matches_the_version():
    k = json.loads((KIT / "kit.json").read_text())
    top = re.search(r"^## (\S+)", (KIT / "CHANGELOG.md").read_text(), re.M)
    assert top and top.group(1) == k["version"], "CHANGELOG.md's newest entry must be kit.json's version"
    assert k["changes"][-1].startswith(k["version"] + ":"), "kit.json changes must end with this version"
