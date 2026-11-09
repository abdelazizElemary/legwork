"""What the desks' shared header and footer need: the kit version, and the other desks on this machine.
Read-only, from files the installer and each desk already write."""

import json
import os
from pathlib import Path

DESKS = (("pr-review-agent", "Reviews"), ("ticket-agent", "Tickets"))


def kit_version(lib):
    """The installed kit's version, written next to the shared library by the installer."""
    try:
        return json.loads((Path(lib) / "kit_version.json").read_text()).get("version")
    except (OSError, ValueError):
        return None


def pid_alive(pid):
    try:
        os.kill(int(pid), 0)
        return True
    except (OSError, TypeError, ValueError):
        return False


def desks(claude_dir, me):
    """Each desk on this machine: its name, and its link when it is running. The link carries its own token, which
    only ever goes to this person's own browser over 127.0.0.1."""
    out = []
    for folder, label in DESKS:
        if not (Path(claude_dir) / folder / "agent.py").exists():
            continue
        entry = {"id": folder, "label": label, "me": folder == me, "url": None}
        try:
            info = json.loads((Path(claude_dir) / folder / "dashboard.json").read_text())
            if pid_alive(info.get("pid")):
                entry["url"] = f"http://127.0.0.1:{info['port']}/?t={info['token']}"
        except (OSError, ValueError, KeyError):
            pass
        out.append(entry)
    return out


def frame(claude_dir, lib, me):
    return {"version": kit_version(lib), "desks": desks(claude_dir, me)}
