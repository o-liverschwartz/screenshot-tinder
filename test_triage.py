#!/usr/bin/env python3
"""
Self-check for screenshot-triage. Stdlib only, touches nothing outside a temp dir.

    python3 test_triage.py

Starts its own server on a spare port against throwaway files, exercises every
action, and asserts the two things that must never break: nothing is ever
deleted, and every action can be undone.
"""
import base64, json, os, shutil, socket, subprocess, sys, tempfile, time, urllib.error, urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==")


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def call(base, path, payload=None):
    req = urllib.request.Request(base + path, method="POST" if payload is not None else "GET")
    data = None
    if payload is not None:
        req.add_header("Content-Type", "application/json")
        data = json.dumps(payload).encode()
    try:
        with urllib.request.urlopen(req, data, timeout=10) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        return json.loads(e.read())


def main():
    tmp = tempfile.mkdtemp(prefix="triage-test-")
    app, src, dest, locked, trash_stub = (
        os.path.join(tmp, n) for n in ("app", "src", "dest", "locked", "trash-stub"))
    os.makedirs(os.path.join(app, "static"), exist_ok=True)
    os.makedirs(src); os.makedirs(dest); os.makedirs(locked)
    shutil.copy(os.path.join(HERE, "server.py"), app)
    shutil.copy(os.path.join(HERE, "static", "index.html"), os.path.join(app, "static"))
    names = ["a.png", "b.png", "c.png", "d.png", "e.png"]
    for n in names:
        with open(os.path.join(src, n), "wb") as f:
            f.write(PNG)
    os.chmod(locked, 0o000)

    port = free_port()
    base = f"http://127.0.0.1:{port}"
    env = {**os.environ, "TRIAGE_TRASH_DIR": trash_stub}
    proc = subprocess.Popen([sys.executable, "server.py", "--port", str(port), "--no-browser"],
                            cwd=app, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        for _ in range(50):
            try:
                call(base, "/api/state"); break
            except Exception:
                time.sleep(0.1)
        else:
            raise SystemExit("server never came up")

        ok = lambda msg: print(f"  ok  {msg}")

        r = call(base, "/api/scan", {"folders": [{"path": src, "recursive": False},
                                                 {"path": locked, "recursive": False}],
                                     "types": ["images"],
                                     "destinations": [{"path": dest, "label": "dest"}],
                                     "armed": dest})
        ids = [i["id"] for i in r["queue"]]
        assert len(ids) == 5, r
        ok("scan finds the files")

        # A folder the OS refuses must read as denied, never as an empty folder.
        assert r["problems"] and "permission denied" in r["problems"][0]["problem"], r["problems"]
        ok("a denied folder is reported as denied, not as empty")
        assert r["config"]["armed"] == dest
        ok("the aimed destination survives a scan")

        r = call(base, "/api/action", {"id": ids[0], "action": "move", "dest": dest})
        assert r["item"]["status"] == "filed" and os.path.isfile(f"{dest}/a.png"), r
        ok("move files into the destination")

        call(base, "/api/action", {"id": ids[1], "action": "rename", "new_name": "a.png"})
        r = call(base, "/api/action", {"id": ids[1], "action": "move", "dest": dest})
        assert r["item"]["name"] == "a 2.png", r
        assert os.path.isfile(f"{dest}/a.png"), "the original was overwritten"
        ok("a name collision never overwrites what is already there")

        r = call(base, "/api/action", {"id": ids[2], "action": "remove"})
        q = r["item"]["path"]
        assert os.path.isfile(q) and "/quarantine/" in q, r
        assert not os.path.exists(f"{src}/c.png")
        ok("remove quarantines the file instead of deleting it")

        depth = call(base, "/api/state")["history_depth"]
        r = call(base, "/api/bulk", {"ids": [ids[3], ids[4]], "action": "keep"})
        assert r["applied"] == 2, r
        assert call(base, "/api/state")["history_depth"] == depth + 1
        ok("many files at once collapse into one undo step")

        r = call(base, "/api/undo", {})
        assert r["undone"] == 2, r
        ok("one undo puts the whole batch back")

        r = call(base, "/api/undo", {})
        assert r["undone"] == 1 and os.path.isfile(f"{src}/c.png"), r
        assert not os.path.exists(q), "the quarantined copy should be gone after undo"
        ok("undo brings a quarantined file home")

        call(base, "/api/undo", {})   # the move of b
        call(base, "/api/undo", {})   # the rename of b
        call(base, "/api/undo", {})   # the move of a
        assert os.path.isfile(f"{src}/a.png"), "a.png never came back"
        ok("undo walks all the way back out of a destination")

        # Every file that went in came back out. Nothing was destroyed.
        assert sorted(os.listdir(src)) == sorted(names), sorted(os.listdir(src))
        ok("after undoing everything, not one file was lost")

        # --- quarantine -> Trash (osascript stubbed via TRIAGE_TRASH_DIR) ---
        r = call(base, "/api/action", {"id": ids[1], "action": "remove"})
        qpath = r["item"]["path"]
        assert os.path.isfile(qpath), r
        depth = call(base, "/api/state")["history_depth"]

        r = call(base, "/api/quarantine/clear", {})
        assert r["moved"] == 1 and r["failed"] == 0, r
        assert r["counts"]["removed"] == 0, r
        assert not os.path.exists(qpath), "the quarantined file should have left the app's folder"
        stubbed = os.listdir(trash_stub)
        assert len(stubbed) == 1 and stubbed[0].endswith("b.png"), stubbed
        ok("clearing quarantine moves the file to the stub trash dir and zeroes the count")

        assert call(base, "/api/state")["history_depth"] == depth, "clearing quarantine must not touch the undo stack"
        r = call(base, "/api/undo", {})
        assert not r.get("undone") and "Trash" in r.get("error", ""), r
        assert call(base, "/api/state")["history_depth"] == depth, "a refused undo must not pop the stack"
        ok("undoing a trashed remove refuses with a clear message instead of silently doing nothing")

        call(base, "/api/action", {"id": ids[0], "action": "keep"})
        with urllib.request.urlopen(base + "/api/export", timeout=10) as resp:
            csv = resp.read().decode()
        assert csv.startswith('"status"') and "a.png" in csv
        ok("the kept list exports as csv")

        print("\nall checks passed")
    finally:
        proc.terminate(); proc.wait(timeout=5)
        os.chmod(locked, 0o755)
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    main()
