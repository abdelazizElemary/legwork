"""Both desks are local-only and need their token: the HTTP rules that keep other pages and sites out."""

import http.client
import json
import os
from pathlib import Path
import subprocess
import sys
import time

import pytest

DESKS = [("ticket-agent", "/api/state"), ("pr-review-agent", "/api/state")]


@pytest.fixture(params=DESKS, ids=[d[0] for d in DESKS])
def desk(request, home, fakes):
    agent, api = request.param
    base = home / ".claude" / agent
    info_file = base / "dashboard.json"
    proc = subprocess.Popen([sys.executable, str(base / "dashboard.py")], cwd=str(base), stdout=subprocess.DEVNULL,
                            stderr=subprocess.PIPE, env={**os.environ, "HOME": str(home), "FAKE_GH_LOG": str(fakes["log"])})
    for _ in range(300):
        if info_file.exists():
            info = json.loads(info_file.read_text())
            if info.get("pid") == proc.pid:
                break
        time.sleep(0.1)
    else:
        proc.kill()
        pytest.fail(f"{agent} desk didn't start: {proc.stderr.read().decode()[-500:]}")
    yield {"port": info["port"], "token": info["token"], "api": api, "file": info_file}
    proc.terminate()
    proc.wait(timeout=10)


def call(d, path, method="GET", headers=None, body=None):
    c = http.client.HTTPConnection("127.0.0.1", d["port"], timeout=10)
    h = {"Host": f"127.0.0.1:{d['port']}", **(headers or {})}
    c.request(method, path, body=body, headers=h)
    r = c.getresponse()
    return r.status, dict(r.getheaders()), r.read()


def test_page_needs_the_token(desk):
    assert call(desk, "/")[0] == 403
    assert call(desk, "/?t=wrong")[0] == 403
    status, headers, _ = call(desk, f"/?t={desk['token']}")
    assert status == 200
    csp = headers.get("Content-Security-Policy", "")
    assert "frame-ancestors 'none'" in csp and "default-src 'self'" in csp
    assert headers.get("X-Frame-Options") == "DENY" and headers.get("Referrer-Policy") == "no-referrer"


def test_api_needs_the_token_header(desk):
    assert call(desk, desk["api"])[0] == 403
    assert call(desk, f"{desk['api']}?t={desk['token']}")[0] == 403, "the API takes the token only as a header"
    status, _, body = call(desk, desk["api"], headers={"X-Token": desk["token"]})
    assert status == 200 and isinstance(json.loads(body), dict)


def test_another_host_is_refused(desk):
    status, _, _ = call(desk, f"/?t={desk['token']}", headers={"Host": "evil.example:80"})
    assert status == 403, "a DNS-rebinding page can't reach the desk"


def test_posts_must_be_json_with_the_token(desk):
    assert call(desk, "/api/agent", "POST", {"Content-Type": "application/json"}, b'{"action":"pause"}')[0] == 403
    status, _, _ = call(desk, "/api/agent", "POST", {"X-Token": desk["token"], "Content-Type": "text/plain"}, b"x")
    assert status == 403, "a cross-site form post can't send JSON with the token"


def test_token_file_is_private_and_desk_is_local(desk):
    import socket
    assert oct(desk["file"].stat().st_mode & 0o777) == "0o600"
    try:
        ips = [a[4][0] for a in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET) if not a[4][0].startswith("127.")]
    except OSError:
        ips = []
    if not ips:
        pytest.skip("this machine has no address besides loopback")
    with pytest.raises(OSError):
        socket.create_connection((ips[0], desk["port"]), timeout=2)


def test_ticket_desk_refresh_endpoint_needs_the_token_and_reads_prs(home, fakes):
    """The Refresh PR status button posts here; it answers with how many tickets it read, and never writes to GitHub."""
    base = home / ".claude" / "ticket-agent"
    proc = subprocess.Popen([sys.executable, str(base / "dashboard.py")], cwd=str(base), stdout=subprocess.DEVNULL,
                            stderr=subprocess.PIPE, env={**os.environ, "HOME": str(home), "FAKE_GH_LOG": str(fakes["log"])})
    try:
        for _ in range(300):
            f = base / "dashboard.json"
            if f.exists() and json.loads(f.read_text()).get("pid") == proc.pid:
                info = json.loads(f.read_text())
                break
            time.sleep(0.1)
        else:
            pytest.fail("ticket desk didn't start")
        d = {"port": info["port"]}
        body = b"{}"
        assert call(d, "/api/prs/refresh", "POST", {"Content-Type": "application/json"}, body)[0] == 403
        status, _, out = call(d, "/api/prs/refresh", "POST", {"X-Token": info["token"], "Content-Type": "application/json"}, body)
        assert status == 200 and json.loads(out) == {"ok": True, "message": "PR status refreshed for 0 tickets."}
        status, _, out = call(d, "/api/prs/refresh", "POST", {"X-Token": info["token"], "Content-Type": "application/json"},
                              b'{"key": "not a key"}')
        assert status == 400 and json.loads(out)["ok"] is False
        writes = [json.loads(l) for l in fakes["log"].read_text().splitlines()] if fakes["log"].exists() else []
        assert not [w for w in writes if w[:2] in (["pr", "ready"], ["pr", "merge"], ["pr", "edit"], ["pr", "comment"])]
    finally:
        proc.terminate()
        proc.wait(timeout=10)


def test_ticket_desk_upload_saves_images_only_and_rework_takes_them(home, fakes):
    """Screenshots go through /api/upload (token, JSON, images only), then ride along on /api/rework."""
    import base64
    base = home / ".claude" / "ticket-agent"
    (base / "state").mkdir(parents=True, exist_ok=True)
    (base / "state" / "DEMO-7.json").write_text(json.dumps({"key": "DEMO-7", "phase": "ready", "context": []}))
    proc = subprocess.Popen([sys.executable, str(base / "dashboard.py")], cwd=str(base), stdout=subprocess.DEVNULL,
                            stderr=subprocess.PIPE, env={**os.environ, "HOME": str(home), "FAKE_GH_LOG": str(fakes["log"])})
    try:
        for _ in range(300):
            f = base / "dashboard.json"
            if f.exists() and json.loads(f.read_text()).get("pid") == proc.pid:
                info = json.loads(f.read_text())
                break
            time.sleep(0.1)
        else:
            pytest.fail("ticket desk didn't start")
        d, h = {"port": info["port"]}, {"X-Token": info["token"], "Content-Type": "application/json"}
        png = "data:image/png;base64," + base64.b64encode(b"\x89PNG\r\n\x1a\n" + b"0" * 16).decode()
        assert call(d, "/api/upload", "POST", {"Content-Type": "application/json"}, json.dumps({"key": "DEMO-7", "data": png}).encode())[0] == 403
        status, _, out = call(d, "/api/upload", "POST", h, json.dumps({"key": "DEMO-7", "name": "qa.png", "data": png}).encode())
        saved = json.loads(out)
        assert status == 200 and saved["message"].endswith(".png")
        assert (base / "tickets" / "DEMO-7" / "attachments" / saved["message"]).exists()
        script = "data:text/html;base64," + base64.b64encode(b"<script>alert(1)</script>").decode()
        status, _, out = call(d, "/api/upload", "POST", h, json.dumps({"key": "DEMO-7", "name": "x.png", "data": script}).encode())
        assert status == 400 and json.loads(out)["ok"] is False
        status, _, out = call(d, "/api/upload", "POST", h, json.dumps({"key": "DEMO-404", "name": "x.png", "data": png}).encode())
        assert status == 400, "an unknown ticket gets no folder"
        assert not (base / "tickets" / "DEMO-404").exists()
        # a body that claims to be 17 MB is refused from the header alone, nothing is read
        import socket
        with socket.create_connection(("127.0.0.1", d["port"]), timeout=10) as sock:
            sock.sendall((f"POST /api/upload HTTP/1.1\r\nHost: 127.0.0.1:{d['port']}\r\nX-Token: {info['token']}\r\n"
                          "Content-Type: application/json\r\nContent-Length: 17000000\r\n\r\n").encode())
            # the reason phrase differs between Python versions, the code doesn't
            assert sock.recv(64).split(b"\r\n")[0].split()[1] == b"413", "refused before any body is read"
        assert call(d, "/api/state", headers={"X-Token": info["token"]})[0] == 200, "the desk is still up"
    finally:
        proc.terminate()
        proc.wait(timeout=10)


def test_the_desk_frame_lists_the_desks_and_the_version(home):
    """The header switches between the desks and the footer shows the kit version; both come from files on disk."""
    import importlib.util
    lib = home / ".claude" / "agent-lib"
    spec = importlib.util.spec_from_file_location("frame", lib / "frame.py")
    F = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(F)
    kit = json.loads((Path(__file__).resolve().parent.parent / "kit.json").read_text())
    assert F.kit_version(lib) == kit["version"], "install writes the version next to the shared library"
    got = F.frame(home / ".claude", lib, "ticket-agent")
    assert [d["id"] for d in got["desks"]] == ["pr-review-agent", "ticket-agent"]
    assert [d["me"] for d in got["desks"]] == [False, True]
    assert all(d["url"] is None for d in got["desks"]), "no desk is running, so there is no link"
    # a running desk gets its link; a stale file from a stopped one doesn't
    (home / ".claude/pr-review-agent/dashboard.json").write_text(json.dumps({"pid": os.getpid(), "port": 8787, "token": "abc"}))
    (home / ".claude/ticket-agent/dashboard.json").write_text(json.dumps({"pid": 999999, "port": 8788, "token": "x"}))
    got = F.frame(home / ".claude", lib, "ticket-agent")
    assert got["desks"][0]["url"] == "http://127.0.0.1:8787/?t=abc" and got["desks"][1]["url"] is None


def test_both_desks_answer_the_frame_endpoint(desk):
    status, _, body = call(desk, "/api/frame", headers={"X-Token": desk["token"]})
    assert status == 200 and "desks" in json.loads(body)
    assert call(desk, "/api/frame")[0] == 403, "the frame carries desk links, so it needs the token"
