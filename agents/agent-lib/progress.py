"""Live progress for the agents' headless Claude runs, shared by both agents.

The run streams its events (stream-json). Each tool step lands in a small progress file the dashboards
read, so they can show what the run is doing now, when it last did something, and whether it's still alive.
"""

import json
import os
import subprocess
import threading
import time
from pathlib import Path

KEEP = 60


def _short(path):
    parts = Path(str(path or "")).parts
    return "/".join(parts[-3:]) if parts else ""


def step_text(name, inp):
    inp = inp or {}
    if name == "Bash":
        return inp.get("description") or "Running " + (inp.get("command") or "").strip().split("\n")[0][:80]
    if name == "Read":
        return f"Reading {_short(inp.get('file_path'))}"
    if name in ("Edit", "MultiEdit", "NotebookEdit"):
        return f"Editing {_short(inp.get('file_path') or inp.get('notebook_path'))}"
    if name == "Write":
        return f"Writing {_short(inp.get('file_path'))}"
    if name == "Grep":
        return f"Searching the code for {str(inp.get('pattern', ''))[:50]}"
    if name == "Glob":
        return f"Finding files {str(inp.get('pattern', ''))[:50]}"
    if name in ("Task", "Agent"):
        return f"Started a helper: {inp.get('description') or inp.get('subagent_type') or 'a sub-task'}"
    if name == "Skill":
        return f"Loading the {inp.get('skill', '')} skill"
    if name in ("TodoWrite", "TaskCreate", "TaskUpdate"):
        return "Updating its plan"
    if name in ("WebFetch", "WebSearch"):
        return "Looking something up"
    return name.split("__")[-1] if name.startswith("mcp__") else name


class Progress:
    def __init__(self, path, job):
        self.path, self.job = Path(path), job
        self.data = {"state": "running", "job": job, "started": time.time(), "updated": time.time(), "steps": [], "pid": None}
        self._last_write = 0
        self._lock = threading.RLock()

    def write(self, force=False):
        # the run's reader and the model-switch watcher both write, one at a time
        with self._lock:
            if not force and time.time() - self._last_write < 1:
                return
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_name(f".{self.path.name}.tmp")
            tmp.write_text(json.dumps(self.data))
            tmp.replace(self.path)
            self._last_write = time.time()

    def step(self, text, helper=False):
        with self._lock:
            self.data["steps"] = (self.data["steps"] + [{"at": time.time(), "text": text, "helper": helper}])[-KEEP:]
            self.data["updated"] = time.time()
            self.write(force=True)

    def touch(self):
        self.data["updated"] = time.time()
        self.write()

    def finish(self, state):
        self.data.update({"state": state, "updated": time.time(), "finished": time.time()})
        self.write(force=True)


SWITCH_POLL_S = 3


def run_claude(cmd, cwd, env, timeout_s, progress_path, job, desired=None, describe=None):
    """Run a headless claude with streamed events. Returns (result dict, stderr tail). Raises TimeoutExpired.

    With desired, a function returning the (model, effort) the run should use now, the prompt goes in on stdin
    (stream-json) and the run switches model and effort while it works whenever that choice changes.
    """
    if "--output-format" in cmd:
        i = cmd.index("--output-format")
        cmd = cmd[:i] + cmd[i + 2:]
    cmd = cmd + ["--output-format", "stream-json", "--verbose"]
    prompt = None
    if desired:
        i = cmd.index("-p")
        prompt, cmd = cmd[i + 1], cmd[:i + 1] + cmd[i + 2:] + ["--input-format", "stream-json"]
    prog = Progress(progress_path, job)
    proc = subprocess.Popen(cmd, cwd=cwd, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                            stdin=subprocess.PIPE if desired else subprocess.DEVNULL)
    prog.data["pid"] = proc.pid
    prog.step("Starting")
    err = []
    threading.Thread(target=lambda: err.extend(proc.stderr), daemon=True).start()
    timed_out, done = threading.Event(), threading.Event()
    send_lock = threading.Lock()

    def send(obj):
        with send_lock:
            try:
                proc.stdin.write(json.dumps(obj) + "\n")
                proc.stdin.flush()
                return True
            except (BrokenPipeError, ValueError, OSError):
                return False

    def close_stdin():
        with send_lock:
            try:
                proc.stdin.close()
            except (BrokenPipeError, ValueError, OSError):
                pass

    if desired:
        current = desired()
        prog.data.update({"model": current[0], "effort": current[1]})
        send({"type": "user", "message": {"role": "user", "content": prompt}})

        def watch():
            nonlocal current
            n = 0
            while not done.wait(SWITCH_POLL_S):
                try:
                    want = desired()
                except Exception:
                    continue
                if want == current:
                    continue
                if want[0] and want[0] != current[0]:
                    n += 1
                    send({"type": "control_request", "request_id": f"model-{n}",
                          "request": {"subtype": "set_model", "model": want[0]}})
                if want[1] and want[1] != current[1]:
                    n += 1
                    send({"type": "control_request", "request_id": f"effort-{n}",
                          "request": {"subtype": "apply_flag_settings", "settings": {"effortLevel": want[1]}}})
                current = want
                prog.data.update({"model": want[0], "effort": want[1], "switched_at": time.time()})
                prog.step(f"Switched to {describe(*want) if describe else ' at '.join(x for x in want if x)}, from its next step on")
        threading.Thread(target=watch, daemon=True).start()

    def kill():
        timed_out.set()
        proc.kill()
    timer = threading.Timer(timeout_s, kill)
    timer.start()
    result = {}
    try:
        for line in proc.stdout:
            try:
                ev = json.loads(line)
            except ValueError:
                continue
            kind = ev.get("type")
            helper = bool(ev.get("parent_tool_use_id"))
            if ev.get("session_id") and not prog.data.get("session_id"):
                # kept so a failed or switched run can be resumed with all its context
                prog.data["session_id"] = ev["session_id"]
                prog.write(force=True)
            if kind == "assistant":
                for block in (ev.get("message") or {}).get("content") or []:
                    if block.get("type") == "tool_use":
                        prog.step(step_text(block.get("name", ""), block.get("input")), helper)
                    else:
                        prog.touch()
            elif kind == "result":
                result = ev
                done.set()
                if desired:
                    # in stream-json input mode the process waits for more input until stdin closes
                    close_stdin()
            elif kind == "control_response":
                r = ev.get("response") or {}
                if r.get("subtype") == "error":
                    prog.step(f"Couldn't switch: {r.get('error', 'unknown error')}")
            else:
                prog.touch()
        proc.wait()
    finally:
        done.set()
        timer.cancel()
        if desired:
            close_stdin()
    if timed_out.is_set():
        prog.finish("timed_out")
        raise subprocess.TimeoutExpired(cmd, timeout_s)
    prog.finish("failed" if result.get("is_error") or not result else "done")
    return result, "".join(err)[-4000:]


def view(path, max_steps=8):
    """What the dashboards show: the current step, the last few, how long since the last one, and if it's alive."""
    try:
        d = json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return None
    alive = False
    if d.get("pid"):
        try:
            os.kill(d["pid"], 0)
            alive = True
        except OSError:
            alive = False
    now = time.time()
    steps = d.get("steps") or []
    state = d.get("state")
    if state == "running" and not alive:
        state = "stopped"
    return {"state": state, "job": d.get("job"), "started": d.get("started"), "updated": d.get("updated"),
            "elapsed_s": int(now - (d.get("started") or now)), "idle_s": int(now - (d.get("updated") or now)),
            "current": steps[-1]["text"] if steps else None, "steps": steps[-max_steps:], "count": len(steps),
            "session_id": d.get("session_id"), "pid": d.get("pid"), "model": d.get("model"), "effort": d.get("effort")}
