#!/usr/bin/env python3
"""Bring the kit folder up to date with this machine, then pack it to share.

python3 package.py            sync the mapped project references, refresh kit.json, write dist/legwork-<version>.zip
python3 package.py --check    only report what's out of date, write nothing
  --config FILE               the config that names the projects (default kit.config.json)

The kit folder is the source of every agent file: edit it, then `install.py --apply --force`. This script
catches what lives only on this machine (project references mapped into ~/.claude) and warns when an
installed file was edited in place instead of in the kit. Your kit.config.json never goes in the zip.
"""

import argparse
import hashlib
import json
import subprocess
import sys
import zipfile
from pathlib import Path

KIT = Path(__file__).resolve().parent
HOME = Path.home()
SKIP_NAMES = {"kit.config.json", ".DS_Store"}
SKIP_DIRS = {"dist", "__pycache__", ".playwright-mcp", ".git", "profile", ".venv", ".pytest_cache"}
SKIP_SUFFIXES = (".pyc", ".bak", ".tmp")


def kit_files():
    for f in sorted(KIT.rglob("*")):
        rel = f.relative_to(KIT)
        if f.is_file() and not (set(rel.parts) & SKIP_DIRS) and f.name not in SKIP_NAMES and not f.name.endswith(SKIP_SUFFIXES):
            yield rel


def portable(text, cfg, p):
    """The installed reference with this machine's details turned back into placeholders the installer fills."""
    home = str(HOME)
    ws = p["workspace"]
    for form in sorted({ws, ws.replace("~", home, 1), ws.replace(home, "~", 1)}, key=len, reverse=True):
        text = text.replace(form, "{{WORKSPACE}}")
    mi = p.get("memory_index") or ""
    for form in {mi, mi.replace("~", home, 1)} - {""}:
        text = text.replace(f"`{form}`", "{{MEMORY_INDEX}}")
    first = (cfg.get("author") or {}).get("first_name") or ((cfg.get("author") or {}).get("name") or "").split(" ")[0]
    return text.replace(first, "{{AUTHOR_FIRST}}") if first else text


def sync_references(cfg, check):
    """A project mapped on this machine goes into references/ (git-ignored), so the next install starts from the real map."""
    changed = []
    for p in cfg.get("projects", []):
        installed = HOME / ".claude/skills/pr-review/references" / f"{p['id']}.md"
        if not installed.exists() or "Fill every section from the code" in installed.read_text():
            continue
        text = portable(installed.read_text(), cfg, p)
        target = KIT / "references" / f"{p['id']}.md"
        if not target.exists() or target.read_text() != text:
            changed.append(str(target.relative_to(KIT)))
            if not check:
                target.parent.mkdir(exist_ok=True)
                target.write_text(text)
    return changed


def install_drift(config):
    """Installed files that differ from what the kit would install: an edit made in ~/.claude, not in the kit."""
    out = subprocess.run([sys.executable, str(KIT / "install.py"), "--config", str(config)], capture_output=True, text=True)
    return [l.strip() for l in out.stdout.splitlines() if l.strip().startswith(("differs", "replace", "update"))]


def refresh_manifest(check):
    k = json.loads((KIT / "kit.json").read_text())
    listed = {f["path"]: f for f in k["files"]}
    missing = []
    for rel in kit_files():
        path = str(rel)
        if path not in listed:
            if not path.startswith(("tests/", ".github/", "references/")):
                missing.append(path)
            continue
        f = listed[path]
        if "sha256" in f and not check:
            b = (KIT / rel).read_bytes()
            f["bytes"], f["sha256"] = len(b), hashlib.sha256(b).hexdigest()[:16]
    gone = [p for p in listed if not (KIT / p).exists()]
    if not check:
        (KIT / "kit.json").write_text(json.dumps(k, indent=1, ensure_ascii=False) + "\n")
    return k["version"], missing, gone


def pack(version):
    dist = KIT / "dist"
    dist.mkdir(exist_ok=True)
    out = dist / f"legwork-{version}.zip"
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for rel in kit_files():
            z.write(KIT / rel, str(Path("legwork") / rel))
    latest = dist / "legwork-latest.zip"
    latest.write_bytes(out.read_bytes())
    local = sorted(r.name for r in kit_files() if r.parts[0] == "references" and r.name != "README.md")
    return out, latest, local


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default=str(KIT / "kit.config.json"))
    ap.add_argument("--check", action="store_true")
    a = ap.parse_args()
    cfg = json.loads(Path(a.config).read_text()) if Path(a.config).exists() else {}
    refs = sync_references(cfg, a.check)
    drift = install_drift(a.config) if cfg else []
    version, missing, gone = refresh_manifest(a.check)
    for r in refs:
        print(f"{'out of date' if a.check else 'synced'}: {r} (from ~/.claude)")
    for d in drift:
        print(f"warning: installed copy differs from the kit: {d}. Edit the kit, then install.py --apply --force")
    for m in missing:
        print(f"warning: {m} isn't listed in kit.json files, add it with its purpose")
    for g in gone:
        print(f"warning: kit.json lists {g}, which doesn't exist")
    if a.check:
        print("up to date" if not (refs or drift or missing or gone) else "run python3 package.py to sync")
        return
    out, latest, local = pack(version)
    print(f"kit {version}: {out}\nshare this one: {latest}")
    if local:
        print(f"it carries your project maps ({', '.join(local)}): share it only with people who may see those repos")


if __name__ == "__main__":
    main()
