# Your project references

When you map a project (see `skills/pr-review/references/MAPPING.md`), `python3 package.py` copies the map
from `~/.claude/skills/pr-review/references/<id>.md` into this folder as `<id>.md`, with your paths turned
back into placeholders. The next `install.py` run seeds from it, so a reinstall or another machine starts from
your map instead of the blank template.

Git ignores everything here except this file. Your maps describe your own repos, so they stay on your
machine. A zip from `package.py` does carry them: share that zip only with people who may see those repos.
