"""The desks come back on their own after a restart or a crash, never after you stopped them, and a run a desk starts
gets the scheduler's low priority. The ticket pane scrolls as one, whatever the size of its head."""

import json
import os
import re
import socket
import sys

from conftest import KIT, load_module


def osglue(home):
    sys.path.insert(0, str(home / ".claude/agent-lib"))
    import osglue
    return osglue


def test_a_desk_from_before_a_restart_is_not_taken_for_a_running_one(home):
    OS = osglue(home)
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    s.listen()
    port = s.getsockname()[1]
    assert OS.desk_up({"pid": os.getpid(), "port": port})
    s.close()
    # after a restart the saved pid can belong to another process, and nothing listens on the port
    assert not OS.desk_up({"pid": os.getpid(), "port": port})
    assert not OS.desk_up({"pid": 999999, "port": port}) and not OS.desk_up({})


def test_the_ticket_desk_comes_back_after_a_restart(home, monkeypatch, fakes):
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("FAKE_GH_LOG", str(fakes["log"]))
    A = load_module(home / ".claude/ticket-agent/agent.py", "ticket_agent_desk_life")
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    stale = s.getsockname()[1]
    s.close()
    A.DASH.write_text(json.dumps({"pid": os.getpid(), "port": stale, "token": "old"}))
    info = A.start_desk()
    try:
        assert info and info["pid"] != os.getpid() and A.OS.desk_up(info)
    finally:
        if info:
            os.kill(info["pid"], 15)


def cycle_starts_desk(A, monkeypatch):
    started = []
    monkeypatch.setattr(A, "start_desk", lambda: started.append(1))
    A.PAUSE.parent.mkdir(parents=True, exist_ok=True)
    A.PAUSE.touch()
    for name in ("heal_stopped_runs", "sync_tracker", "sync_prs"):
        if hasattr(A, name):
            monkeypatch.setattr(A, name, lambda *a: None)
    A.cmd_cycle(None)
    return bool(started)


def test_the_cycle_brings_a_desk_back_unless_you_stopped_it(home, monkeypatch, fakes):
    monkeypatch.setenv("FAKE_GH_LOG", str(fakes["log"]))
    for agent in ("ticket-agent", "pr-review-agent"):
        A = load_module(home / f".claude/{agent}/agent.py", f"{agent}_cycle_desk")
        assert cycle_starts_desk(A, monkeypatch), agent
        A.cmd_dashboard("stop")
        assert A.DESK_STOPPED.exists() and not cycle_starts_desk(A, monkeypatch), agent


def test_runs_a_desk_starts_get_a_low_priority(home, monkeypatch, fakes):
    monkeypatch.setenv("FAKE_GH_LOG", str(fakes["log"]))
    A = load_module(home / ".claude/ticket-agent/agent.py", "ticket_agent_low")
    seen = []
    monkeypatch.setattr(A.subprocess, "Popen", lambda cmd, **kw: seen.append(cmd))
    A.spawn("_check", "DEMO-9")
    assert seen and seen[0][:3] == ["nice", "-n", "10"]
    review_desk = (home / ".claude/pr-review-agent/dashboard.py").read_text()
    assert "[*A.OS.LOW, sys.executable" in review_desk


def test_the_page_is_the_only_scroller_on_the_ticket_desk():
    page = (KIT / "agents/ticket-agent/dashboard.html").read_text()
    pane = re.search(r"^\.pane \{[^}]*\}", page, re.M).group(0)
    assert "overflow" not in pane and "max-height" not in pane and "sticky" not in pane, "the pane flows with the page"
    scrollers = [r for r in re.findall(r"^([^{\n]+)\{[^}]*overflow[-xy]*: *(?:auto|scroll|hidden auto)", page, re.M)]
    assert [r.strip() for r in scrollers] == [".jira .table-wrap"], f"nothing else scrolls inside the page: {scrollers}"
    tell = [r for r in re.findall(r"^\.tell-[^{\n]*\{[^}]*\}", page, re.M) if not r.startswith(".tell-dock")]
    assert tell and not [r for r in tell if "max-height" in r or "line-clamp" in r], "the conversation is never cut short"
    assert 'id="pane" aria-live' not in page, "the pane is redrawn on every poll, so it is no live region"
