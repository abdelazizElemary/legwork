"""Shared fixtures: every test installs the kit into a throwaway home with fake claude and gh, so nothing
reaches Claude, GitHub or the person's real ~/.claude. Run with: python3 -m pytest tests"""

import importlib.util
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

KIT = Path(__file__).resolve().parent.parent

# a gh stand-in: logs every call and answers the few reads the agents make
FAKE_GH = r'''#!/usr/bin/env python3
import json, os, sys
a = sys.argv[1:]
with open(os.environ.get("FAKE_GH_LOG", "/dev/null"), "a") as f:
    f.write(json.dumps(a) + "\n")
canned = os.environ.get("FAKE_GH_DIR")
key = "_".join(x for x in a if not x.startswith("-"))[:120].replace("/", "_")
if canned and os.path.isdir(canned):
    # the longest canned answer whose name starts the call, e.g. pr_view_7 for pr view 7 --repo ... --json ...
    hits = sorted((n for n in os.listdir(canned) if key.startswith(n)), key=len)
    if hits:
        sys.stdout.write(open(os.path.join(canned, hits[-1])).read()); sys.exit(0)
if a[:2] == ["api", "user"]:
    print(json.dumps({"login": "tester", "name": "Test Person"}))
elif a[:2] == ["pr", "create"]:
    print("https://github.com/acme/demo/pull/7")
elif a[:2] == ["search", "prs"] or a[:2] == ["search", "issues"]:
    print("[]")
elif a[:1] == ["api"]:
    print("{}")
'''

FAKE_CLAUDE = "#!/bin/sh\necho '{\"type\":\"result\",\"subtype\":\"success\",\"result\":\"ok\",\"total_cost_usd\":0}'\n"


def write_exe(path, text):
    path.write_text(text)
    path.chmod(0o755)
    return path


@pytest.fixture
def fakes(tmp_path):
    d = tmp_path / "fakebin"
    d.mkdir()
    return {"gh": write_exe(d / "gh", FAKE_GH), "claude": write_exe(d / "claude", FAKE_CLAUDE),
            "log": tmp_path / "gh.log", "dir": d}


def config(tmp_path, fakes, **over):
    """A teammate's config, built from the shipped example, pointing at the throwaway workspace."""
    c = json.loads((KIT / "kit.config.example.json").read_text())
    ws = tmp_path / "ws"
    ws.mkdir(exist_ok=True)
    c.update({"author": {"name": "Test Person", "email": "test@example.com"},
              "projects": [{"id": "demo", "org": "acme", "label": "Demo", "workspace": str(ws), "memory_index": ""}],
              "binaries": {"python": sys.executable, "claude": str(fakes["claude"]), "gh": str(fakes["gh"]),
                           "git": shutil.which("git")},
              "run_path": f"{fakes['dir']}:/usr/bin:/bin", "scheduler": "none", "notify": False})
    c["review"].update({"orgs": ["acme"], "dashboard_port": 0})
    c["ticket"].update({"project": "demo", "label": "Demo", "dashboard_port": 0, "auto_check": "none", "diagnose": False})
    c["ticket"]["tracker"].update({"scope": ["org:acme"], "project": None, "key_prefixes": {"acme/demo": "DEMO"}})
    c["ticket"]["conventions"] = {"pr_title": "{type}: {summary} (#{num})", "commit_subject": "{type}: {summary} (#{num})",
                                  "branch": "dev/{key}-{slug}"}
    # never pick up the profile of whoever runs the tests: an empty one unless a test brings its own
    c["profile_dir"] = os.path.relpath(tmp_path / "no-profile", KIT)
    for k, v in over.items():
        c[k] = v
    return c


def install(tmp_path, cfg, profile=None, extra=()):
    """Install into tmp_path/home. profile: {filename: text} for a profile folder. Returns the home."""
    home = tmp_path / "home"
    if profile is not None:
        pdir = tmp_path / "profile"
        pdir.mkdir(exist_ok=True)
        for name, text in profile.items():
            (pdir / name).write_text(text)
        cfg["profile_dir"] = os.path.relpath(pdir, KIT)
    cfg_path = tmp_path / "cfg.json"
    cfg_path.write_text(json.dumps(cfg))
    out = subprocess.run([sys.executable, str(KIT / "install.py"), "--config", str(cfg_path), "--apply", "--home", str(home), *extra],
                         capture_output=True, text=True)
    assert out.returncode == 0, out.stdout + out.stderr
    return home


@pytest.fixture
def home(tmp_path, fakes):
    return install(tmp_path, config(tmp_path, fakes))


def load_module(path, name):
    """Import an installed agent file as a module, with no command line arguments."""
    argv = sys.argv
    sys.argv = [str(path)]
    try:
        spec = importlib.util.spec_from_file_location(name, path)
        mod = importlib.util.module_from_spec(spec)
        sys.path.insert(0, str(Path(path).parent))
        spec.loader.exec_module(mod)
    finally:
        sys.argv = argv
    return mod


@pytest.fixture
def ticket_agent(home, fakes, monkeypatch):
    monkeypatch.setenv("FAKE_GH_LOG", str(fakes["log"]))
    A = load_module(home / ".claude/ticket-agent/agent.py", "ticket_agent_under_test")
    A.spawned = []
    A.spawn = lambda *a: A.spawned.append(a)
    A.notify = lambda *a: None
    A.CFG["diagnose"] = False
    yield A
    forget(home)


def forget(home):
    """Drop an install's modules from this test process, so the next test imports its own (with its own policy)."""
    for p in list(sys.path):
        if str(home) in p:
            sys.path.remove(p)
    for name, mod in list(sys.modules.items()):
        if str(home) in str(getattr(mod, "__file__", "") or ""):
            del sys.modules[name]


def git(*args, cwd=None):
    out = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    return out.stdout.strip()


@pytest.fixture
def origin(tmp_path):
    """A local bare repo standing in for GitHub, with main and a PR branch, and a clone in the workspace."""
    # the path ends like a GitHub remote, so the agent's "origin is the org's repo" check passes without rewriting
    bare = tmp_path / "remote" / "github.com" / "acme" / "demo.git"
    bare.parent.mkdir(parents=True)
    git("init", "-q", "--bare", "-b", "main", str(bare))
    seed = tmp_path / "seed"
    git("clone", "-q", str(bare), str(seed))
    for k, v in (("user.name", "Test Person"), ("user.email", "test@example.com")):
        git("config", k, v, cwd=seed)
    (seed / "a.txt").write_text("a\n")
    git("add", ".", cwd=seed)
    git("commit", "-qm", "init", cwd=seed)
    git("push", "-q", "origin", "main", cwd=seed)
    clone = tmp_path / "ws" / "demo"
    clone.parent.mkdir(exist_ok=True)
    git("clone", "-q", str(bare), str(clone))
    for k, v in (("user.name", "Test Person"), ("user.email", "test@example.com")):
        git("config", k, v, cwd=clone)
    return {"bare": bare, "seed": seed, "clone": clone}
