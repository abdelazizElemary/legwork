"""A running agent follows a model or effort change: the runner sends set_model and apply_flag_settings on stdin,
records the session id, and closes stdin when the result arrives so the run ends."""

import json
import os
import sys
import time

from conftest import KIT, load_module, write_exe

FAKE = r'''#!/usr/bin/env python3
import json, os, select, sys, time
log = open(os.environ["FAKE_CLAUDE_LOG"], "a")
def out(o):
    sys.stdout.write(json.dumps(o) + "\n"); sys.stdout.flush()
buf = b""
def lines():
    global buf
    while b"\n" in buf:
        line, buf = buf.split(b"\n", 1)
        yield line.decode()
def drain(seconds):
    # raw reads: a buffered readline would swallow two lines that arrive together
    global buf
    end = time.time() + seconds
    while time.time() < end:
        r, _, _ = select.select([0], [], [], 0.05)
        if r:
            chunk = os.read(0, 65536)
            if not chunk:
                return False
            buf += chunk
            for line in lines():
                log.write(line + "\n"); log.flush()
                m = json.loads(line)
                if m.get("type") == "control_request":
                    out({"type": "control_response", "response": {"subtype": "success", "request_id": m["request_id"]}})
    return True
out({"type": "system", "subtype": "init", "session_id": "sess-1"})
while b"\n" not in buf:
    buf += os.read(0, 65536)
for line in lines():
    log.write(line + "\n"); log.flush()
for i in range(4):
    out({"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "Bash", "input": {"description": f"step {i}"}}]}})
    drain(0.6)
out({"type": "result", "subtype": "success", "result": "done", "total_cost_usd": 0.01, "session_id": "sess-1"})
while os.read(0, 65536):
    pass
log.write("STDIN CLOSED\n")
'''


def test_running_agent_switches_model_and_effort(tmp_path, monkeypatch):
    progress = load_module(KIT / "agents/agent-lib/progress.py", "progress_under_test")
    progress.SWITCH_POLL_S = 0.2
    claude = write_exe(tmp_path / "claude", FAKE)
    log = tmp_path / "claude.log"
    monkeypatch.setenv("FAKE_CLAUDE_LOG", str(log))
    started = time.time()
    desired = lambda: ("haiku", "low") if time.time() - started < 1.0 else ("sonnet", "medium")
    prog = tmp_path / "prog.json"
    res, _ = progress.run_claude([str(claude), "-p", "do the thing", "--model", "haiku"], str(tmp_path),
                                 dict(os.environ), 30, prog, "test", desired=desired)
    sent = [json.loads(l) for l in log.read_text().splitlines() if l.startswith("{")]
    assert sent[0] == {"type": "user", "message": {"role": "user", "content": "do the thing"}}
    reqs = [m["request"] for m in sent if m.get("type") == "control_request"]
    assert {"subtype": "set_model", "model": "sonnet"} in reqs
    assert {"subtype": "apply_flag_settings", "settings": {"effortLevel": "medium"}} in reqs
    assert log.read_text().rstrip().endswith("STDIN CLOSED"), "stdin is closed after the result"
    assert res["result"] == "done"
    p = json.loads(prog.read_text())
    assert p["session_id"] == "sess-1" and p["model"] == "sonnet" and p["state"] == "done"
    assert any("Switched to" in s["text"] for s in p["steps"])


def test_without_a_desired_choice_the_prompt_stays_on_the_command_line(tmp_path, monkeypatch):
    progress = load_module(KIT / "agents/agent-lib/progress.py", "progress_plain")
    args = tmp_path / "args.json"
    claude = write_exe(tmp_path / "claude", f'''#!{sys.executable}
import json, sys
open({str(args)!r}, "w").write(json.dumps(sys.argv[1:]))
print(json.dumps({{"type": "result", "subtype": "success", "result": "ok"}}))
''')
    res, _ = progress.run_claude([str(claude), "-p", "hello"], str(tmp_path), dict(os.environ), 30, tmp_path / "p.json", "t")
    argv = json.loads(args.read_text())
    assert res["result"] == "ok" and argv[:2] == ["-p", "hello"] and "--input-format" not in argv
