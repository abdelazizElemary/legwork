"""OS glue shared by both agents: notifications, the scheduler job, opening URLs and the login-shell env.

macOS uses launchd and osascript, Linux uses systemd --user (or cron) and notify-send.
"""

import fcntl
import os
import shutil
import socket
import subprocess
import sys


def notify(title, text):
    if sys.platform == "darwin":
        q = lambda s: s.replace("\\", "\\\\").replace('"', '\\"')
        subprocess.run(["osascript", "-e", f'display notification "{q(text)}" with title "{q(title)}"'],
                       capture_output=True)
    elif shutil.which("notify-send"):
        subprocess.run(["notify-send", title, text], capture_output=True)


def open_url(url):
    opener = "open" if sys.platform == "darwin" else "xdg-open"
    if shutil.which(opener):
        subprocess.run([opener, url], capture_output=True)


# runs started from a desk get a low CPU priority, like the scheduler's own (launchd ProcessType Background)
LOW = ["nice", "-n", "10"]


def desk_up(info):
    """The desk is up when its process is alive and its port answers. After a reboot the saved pid can be another process."""
    try:
        os.kill(info["pid"], 0)
        with socket.create_connection(("127.0.0.1", info["port"]), timeout=1):
            return True
    except (OSError, KeyError, TypeError):
        return False


def lock_held(path):
    """True when another process holds the flock on path, e.g. a cycle that is running now."""
    with open(path, "a") as f:
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        fcntl.flock(f, fcntl.LOCK_UN)
        return False


def service_loaded(kind, label):
    if kind == "launchd":
        return subprocess.run(["launchctl", "print", f"gui/{os.getuid()}/{label}"], capture_output=True).returncode == 0
    if kind == "systemd":
        out = subprocess.run(["systemctl", "--user", "is-enabled", f"{label}.timer"], capture_output=True, text=True)
        return out.stdout.strip() == "enabled"
    if kind == "cron":
        out = subprocess.run(["crontab", "-l"], capture_output=True, text=True)
        return label in out.stdout
    return False


def service_kick(kind, label, fallback):
    """Start one cycle now. fallback is the argv that runs a cycle directly."""
    if kind == "launchd":
        r = subprocess.run(["launchctl", "kickstart", f"gui/{os.getuid()}/{label}"], capture_output=True, text=True)
        return r.returncode == 0, r.stderr.strip()
    if kind == "systemd":
        r = subprocess.run(["systemctl", "--user", "start", f"{label}.service"], capture_output=True, text=True)
        return r.returncode == 0, r.stderr.strip()
    subprocess.Popen(fallback, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     start_new_session=True)
    return True, ""


def shell_env(names, home):
    """Read env vars from an interactive login shell, since launchd and systemd skip the shell rc files."""
    if not names:
        return {}
    shell = os.environ.get("SHELL") or ("/bin/zsh" if sys.platform == "darwin" else "/bin/bash")
    script = 'printf "%s\\0" ' + " ".join(f'"${n}"' for n in names)
    try:
        out = subprocess.run([shell, "-i", "-c", script], capture_output=True, text=True, stdin=subprocess.DEVNULL,
                             timeout=60, env={"HOME": str(home), "USER": os.environ.get("USER", ""),
                                              "PATH": "/usr/bin:/bin", "SHELL": shell})
    except (OSError, subprocess.TimeoutExpired):
        return {}
    return {n: v for n, v in zip(names, out.stdout.split("\0")) if v}


def claude_env(names, home, run_path, extra=None):
    """Env for a headless claude: no nested-session vars, the credentials from the login shell when missing."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("CLAUDE") or k in names}
    missing = [n for n in names if not env.get(n)]
    if missing:
        env.update(shell_env(missing, home))
    env["PATH"] = run_path
    env.update(extra or {})
    return env
