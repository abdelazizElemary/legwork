"""Model and effort choices for the agents' headless Claude runs, shared by both agents.

A run uses, in order: the job's own override, the agent's default for that job type, and otherwise the
user's normal Claude default (settings.json), in which case no flag is passed at all.
"""

import json
import re
import subprocess
from pathlib import Path

EFFORTS = ["low", "medium", "high", "xhigh", "max"]
CANDIDATES = ["opus", "opus[1m]", "sonnet", "sonnet[1m]", "fable", "haiku"]
USER_SETTINGS = Path.home() / ".claude" / "settings.json"


def label(model):
    """'claude-opus-5-5[1m]' or 'opus[1m]' -> 'Opus 5.5, 1M context'."""
    if not model:
        return ""
    m = re.match(r"(?:claude-)?([a-z]+)(?:-(\d+)-(\d+))?(\[1m\])?$", model)
    if not m:
        return model
    name = m.group(1).title() + (f" {m.group(2)}.{m.group(3)}" if m.group(2) else "")
    return name + (", 1M context" if m.group(4) else "")


def user_default():
    try:
        s = json.loads(USER_SETTINGS.read_text())
    except (OSError, ValueError):
        s = {}
    return {"model": s.get("model"), "effort": s.get("effortLevel")}


def load(path, jobs):
    d = json.loads(path.read_text()) if path.exists() else {}
    d.setdefault("defaults", {})
    for j in jobs:
        d["defaults"].setdefault(j, {"model": None, "effort": None})
    d.setdefault("models", [])
    return d


def save(path, data):
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=1))
    tmp.replace(path)


def clean(model, effort, data):
    """Only choices that are known to work get through."""
    ids = {m["id"] for m in data.get("models", [])}
    model = model if model in ids else None
    effort = effort if effort in EFFORTS else None
    return model, effort


def resolve(data, job, override=None):
    o = override or {}
    d = data["defaults"].get(job, {})
    model = o.get("model") or d.get("model")
    effort = o.get("effort") or d.get("effort")
    return clean(model, effort, data)


def concrete(model, effort):
    """The model and effort a run really uses: the choice, else the user's own Claude default."""
    u = user_default()
    return model or u.get("model"), effort or u.get("effort")


def flags(model, effort):
    return (["--model", model] if model else []) + (["--effort", effort] if effort else [])


def name_of(model, data=None):
    for m in (data or {}).get("models", []):
        if m["id"] == model:
            return m["label"]
    return label(model)


def describe(model, effort, data=None):
    """Plain words for what a run will use."""
    u = user_default()
    m = name_of(model or u.get("model") or "", data)
    e = effort or u.get("effort")
    return f"{m}{' at ' + e + ' effort' if e else ''}" + ("" if model or effort else " (your default)")


def probe(claude, env, cwd):
    """Ask which models this Claude setup allows, with a tiny prompt each. Costs a few cents per allowed model."""
    found = []
    for m in CANDIDATES:
        out = subprocess.run([str(claude), "-p", "Reply with exactly: ok", "--model", m, "--effort", "low",
                              "--output-format", "json", "--max-budget-usd", "0.5"],
                             capture_output=True, text=True, env=env, cwd=cwd, timeout=180)
        try:
            d = json.loads(out.stdout)
        except ValueError:
            continue
        if not d.get("is_error") and (d.get("result") or "").strip().lower().startswith("ok"):
            used = next(iter((d.get("modelUsage") or {}).keys()), m)
            found.append({"id": m, "label": label(used)})
    return found
