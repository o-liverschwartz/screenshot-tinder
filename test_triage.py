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
    app, src, dest, locked, trash_stub, quar, cache = (
        os.path.join(tmp, n) for n in
        ("app", "src", "dest", "locked", "trash-stub", "quar", "cache"))
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
    home = os.path.join(tmp, "home"); os.makedirs(home)
    # A stand-in osascript answers the folder dialog, so no dialog opens during the
    # checks. Every other osascript call passes through to the real one.
    fake_bin = os.path.join(tmp, "bin"); os.makedirs(fake_bin)
    with open(os.path.join(fake_bin, "osascript"), "w") as f:
        f.write('#!/bin/sh\ncase "$*" in *"choose folder"*) echo "$PICK_ANSWER/"; exit 0;; esac\n'
                'exec /usr/bin/osascript "$@"\n')
    os.chmod(os.path.join(fake_bin, "osascript"), 0o755)
    env = {**os.environ, "HOME": home, "TRIAGE_TRASH_DIR": trash_stub, "TRIAGE_CACHE_DIR": cache,
           "PATH": fake_bin + os.pathsep + os.environ.get("PATH", ""), "PICK_ANSWER": dest}
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
                                     "armed": dest,
                                     "quarantine": quar})
        ids = [i["id"] for i in r["queue"]]
        assert len(ids) == 5, r
        ok("scan finds the files")

        # A folder the OS refuses must read as denied, never as an empty folder.
        assert r["problems"] and "permission denied" in r["problems"][0]["problem"], r["problems"]
        ok("a denied folder is reported as denied, not as empty")
        assert r["config"]["armed"] == dest
        ok("the aimed destination survives a scan")

        # Quarantine is configurable and lands nowhere near the app folder.
        assert r["quarantine"] == quar, r["quarantine"]
        assert not r["quarantine"].startswith(app), "quarantine must not live inside the app folder"
        ok("quarantine is configurable and reported back")

        r2 = call(base, "/api/destinations", {"destinations": [{"path": dest, "label": "dest"}],
                                              "armed": dest, "quarantine": "/nope/not/writable"})
        assert "error" in r2, r2
        assert call(base, "/api/state")["quarantine"] == quar, "a rejected path must not be saved"
        ok("an unusable quarantine folder is refused, and the old one stays")

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
        assert os.path.isfile(q) and os.path.dirname(q) == quar, r
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

        # A quarantine folder sitting inside a scanned folder must never feed
        # its own contents back into the queue.
        inner = os.path.join(src, "quarantine-inside")
        r = call(base, "/api/scan", {"folders": [{"path": src, "recursive": True}],
                                     "types": ["images"],
                                     "destinations": [{"path": dest, "label": "dest"}],
                                     "armed": dest, "quarantine": inner})
        before = r["queue_total"]
        first = r["queue"][0]["id"]
        call(base, "/api/action", {"id": first, "action": "remove"})
        r = call(base, "/api/scan", {"folders": [{"path": src, "recursive": True}],
                                     "types": ["images"],
                                     "destinations": [{"path": dest, "label": "dest"}],
                                     "armed": dest, "quarantine": inner})
        assert r["queue_total"] == before - 1, (before, r["queue_total"])
        ok("a quarantine folder inside a scanned folder is never re-scanned")
        call(base, "/api/undo", {})
        call(base, "/api/scan", {"folders": [{"path": src, "recursive": False}],
                                 "types": ["images"],
                                 "destinations": [{"path": dest, "label": "dest"}],
                                 "armed": dest, "quarantine": quar})

        # --- the thumbnail cache clears itself ---
        def thumbs_on_disk():
            return sorted(os.listdir(cache)) if os.path.isdir(cache) else []

        first = call(base, "/api/state")["queue"][0]
        with urllib.request.urlopen(base + "/api/thumb?id=" + first["id"], timeout=20) as r:
            assert r.status == 200 and r.read(), "the thumbnail route must serve something"
        assert len(thumbs_on_disk()) == 1, thumbs_on_disk()
        assert call(base, "/api/thumbs")["files"] == 1
        ok("asking for a thumbnail caches exactly one file")

        # A thumbnail whose file is no longer waiting to be reviewed is dead weight.
        call(base, "/api/action", {"id": first["id"], "action": "keep"})
        call(base, "/api/scan", {"folders": [{"path": src, "recursive": False}],
                                 "types": ["images"], "destinations": [], "armed": None,
                                 "quarantine": quar})
        assert thumbs_on_disk() == [], thumbs_on_disk()
        ok("a scan drops thumbnails for files that left the queue")

        # Off means off: no new copies, and the existing ones go.
        nxt = call(base, "/api/state")["queue"][0]
        urllib.request.urlopen(base + "/api/thumb?id=" + nxt["id"], timeout=20).read()
        assert len(thumbs_on_disk()) == 1
        call(base, "/api/destinations", {"destinations": [], "armed": None, "thumbnails": False,
                                         "thumb_ttl_days": 7})
        assert thumbs_on_disk() == [], "turning thumbnails off must clear what is already cached"
        urllib.request.urlopen(base + "/api/thumb?id=" + nxt["id"], timeout=20).read()
        assert thumbs_on_disk() == [], "with thumbnails off nothing new may be written"
        ok("turning thumbnails off clears the cache and stops writing copies")

        call(base, "/api/destinations", {"destinations": [], "armed": None, "thumbnails": True,
                                         "thumb_ttl_days": 1})
        urllib.request.urlopen(base + "/api/thumb?id=" + nxt["id"], timeout=20).read()
        assert len(thumbs_on_disk()) == 1
        # Age is read off the file's own mtime, so backdating it is a real expiry.
        old = os.path.join(cache, thumbs_on_disk()[0])
        os.utime(old, (time.time() - 3 * 86400, time.time() - 3 * 86400))
        call(base, "/api/scan", {"folders": [{"path": src, "recursive": False}],
                                 "types": ["images"], "destinations": [], "armed": None,
                                 "quarantine": quar})
        assert thumbs_on_disk() == [], "a thumbnail past its age must be swept"
        ok("thumbnails past the configured age are cleared automatically")

        r = call(base, "/api/destinations", {"destinations": [], "armed": None,
                                             "thumbnails": True, "thumb_ttl_days": 999})
        assert "error" in r, r
        ok("an unknown clearing schedule is refused")

        urllib.request.urlopen(base + "/api/thumb?id=" + nxt["id"], timeout=20).read()
        r = call(base, "/api/thumbs/clear", {})
        assert r["removed"] == 1 and thumbs_on_disk() == [], r
        ok("clear now empties the cache")

        # --- what starring does is configurable ---
        r = call(base, "/api/destinations", {"destinations": [], "armed": None,
                                             "star": {"mode": "rename", "affix": "suffix",
                                                      "text": "-KEEP", "color": 2}})
        assert r["config"]["star"]["mode"] == "rename", r["config"]["star"]
        ok("the star setting is saved")

        pending = call(base, "/api/state")["queue"][0]
        before = pending["name"]
        r = call(base, "/api/action", {"id": pending["id"], "action": "star"})
        stem, ext = os.path.splitext(before)
        assert r["item"]["name"] == stem + "-KEEP" + ext, r["item"]["name"]
        assert r["item"]["starred"] and r["item"]["status"] == "kept", r["item"]
        assert os.path.isfile(r["item"]["path"]), "the renamed file must exist on disk"
        assert not os.path.exists(os.path.join(src, before)), "the old name should be gone"
        ok("starring in rename mode marks the name, before the extension")

        r = call(base, "/api/undo", {})
        assert r["undone"] == 1, r
        assert os.path.isfile(os.path.join(src, before)), "undo must put the old name back"
        ok("undo walks a starred rename back")

        for bad, why in [({"mode": "rename", "text": "  ", "affix": "prefix", "color": 2}, "empty mark"),
                         ({"mode": "label", "text": "x", "affix": "prefix", "color": 99}, "bad colour"),
                         ({"mode": "wat", "text": "x", "affix": "prefix", "color": 2}, "bad mode")]:
            r = call(base, "/api/destinations", {"destinations": [], "armed": None, "star": bad})
            assert "error" in r, (why, r)
        assert call(base, "/api/state")["config"]["star"]["text"] == "-KEEP", "a refused setting must not be saved"
        ok("a nonsense star setting is refused and the old one stays")

        # A mark that would build a path instead of a name is stripped, not obeyed.
        r = call(base, "/api/destinations", {"destinations": [], "armed": None,
                                             "star": {"mode": "rename", "affix": "prefix",
                                                      "text": "../../x/", "color": 2}})
        assert "/" not in r["config"]["star"]["text"], r["config"]["star"]
        ok("a star mark cannot contain a path separator")

        call(base, "/api/destinations", {"destinations": [{"path": dest, "label": "dest"}],
                                         "armed": dest,
                                         "star": {"mode": "label", "affix": "prefix",
                                                  "text": "\u2605 ", "color": 2}})

        # --- changing the folder list actually changes the queue ---
        other = os.path.join(tmp, "other")
        os.makedirs(other, exist_ok=True)
        with open(os.path.join(other, "z.png"), "wb") as f:
            f.write(PNG)
        r = call(base, "/api/scan", {"folders": [{"path": other, "recursive": False}],
                                     "types": ["images"],
                                     "destinations": [{"path": dest, "label": "dest"}],
                                     "armed": dest, "quarantine": quar})
        names_now = sorted(i["name"] for i in r["queue"])
        assert names_now == ["z.png"], names_now
        ok("dropping a folder takes its files out of the queue")

        # A folder the OS refuses is not evidence its files are gone.
        r = call(base, "/api/scan", {"folders": [{"path": src, "recursive": False}],
                                     "types": ["images"],
                                     "destinations": [{"path": dest, "label": "dest"}],
                                     "armed": dest, "quarantine": quar})
        queued = r["queue_total"]
        assert queued, r
        os.chmod(src, 0o000)
        r = call(base, "/api/scan", {"folders": [{"path": src, "recursive": False}],
                                     "types": ["images"],
                                     "destinations": [{"path": dest, "label": "dest"}],
                                     "armed": dest, "quarantine": quar})
        os.chmod(src, 0o755)
        assert r["problems"], r
        assert r["queue_total"] == queued, "a denied folder must not empty the queue"
        ok("a folder the OS denies keeps its files queued instead of dropping them")

        # --- a quarantined file that vanished stops being counted, without a scan ---
        r = call(base, "/api/action", {"id": r["queue"][0]["id"], "action": "remove"})
        gone = r["item"]["path"]
        os.remove(gone)
        r = call(base, "/api/scan", {"folders": [{"path": src, "recursive": False}],
                                     "types": ["images"],
                                     "destinations": [{"path": dest, "label": "dest"}],
                                     "armed": dest, "quarantine": quar})
        assert r["counts"].get("removed", 0) == 0, r["counts"]
        ok("a quarantined file deleted behind the app stops being counted")

        # --- quarantine -> Trash (osascript stubbed via TRIAGE_TRASH_DIR) ---
        r = call(base, "/api/state")
        ids = [i["id"] for i in r["queue"]]
        r = call(base, "/api/action", {"id": ids[1], "action": "remove"})
        qpath = r["item"]["path"]
        assert os.path.isfile(qpath), r
        depth = call(base, "/api/state")["history_depth"]

        r = call(base, "/api/quarantine/clear", {})
        assert r["moved"] == 1 and r["failed"] == 0, r
        assert r["counts"]["removed"] == 0, r
        assert not os.path.exists(qpath), "the quarantined file should have left the app's folder"
        stubbed = os.listdir(trash_stub)
        assert len(stubbed) == 1 and stubbed[0] == os.path.basename(qpath), (stubbed, qpath)
        ok("clearing quarantine moves the file to the stub trash dir and zeroes the count")

        assert call(base, "/api/state")["history_depth"] == depth, "clearing quarantine must not touch the undo stack"
        r = call(base, "/api/undo", {})
        assert not r.get("undone") and "Trash" in r.get("error", ""), r
        assert call(base, "/api/state")["history_depth"] == depth, "a refused undo must not pop the stack"
        ok("undoing a trashed remove refuses with a clear message instead of silently doing nothing")

        keeper = call(base, "/api/state")["queue"][0]
        call(base, "/api/action", {"id": keeper["id"], "action": "keep"})
        with urllib.request.urlopen(base + "/api/export", timeout=10) as resp:
            csv = resp.read().decode()
        assert csv.startswith('"status"') and keeper["name"] in csv, csv
        ok("the kept list exports as csv")


        # --- bugs found in review: each one reproduced before it was fixed ---
        t2 = os.path.join(tmp, "t2"); os.makedirs(t2)
        def scan2(rec=False):
            return call(base, "/api/scan", {"folders": [{"path": t2, "recursive": rec}], "types": ["images"],
                                            "destinations": [], "armed": None, "quarantine": quar})
        def put(p, data=PNG):
            with open(p, "wb") as f:
                f.write(data)

        # a page on another site can reach 127.0.0.1 too
        for hdrs in ({"Host": "evil.example"}, {"Origin": "http://evil.example"}):
            req = urllib.request.Request(base + "/api/state", headers=hdrs)
            try:
                urllib.request.urlopen(req, timeout=5); code = 200
            except urllib.error.HTTPError as e:
                code = e.code
            assert code == 403, (hdrs, code)
        ok("a request that does not name this server is refused")

        if sys.platform == "darwin":
            assert call(base, "/api/pick-folder", {"start": src}) == {"path": dest}
            ok("browse hands back the folder picked in the macOS dialog")

        # --demo runs next to a real copy, on its own generated files and state
        dport = free_port()
        demo = subprocess.Popen([sys.executable, "server.py", "--demo", "--port", str(dport), "--no-browser"],
                                cwd=app, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            for _ in range(50):
                try:
                    d = call(f"http://127.0.0.1:{dport}", "/api/state"); break
                except Exception:
                    time.sleep(0.1)
            else:
                raise SystemExit("demo never came up")
            assert d["queue_total"] == 12, d["queue_total"]
            assert not d["config"]["folders"][0]["path"].startswith(home), d["config"]["folders"]
            assert call(base, "/api/state")["queue_total"] != 12, "the demo leaked into the real state"
        finally:
            demo.terminate(); demo.wait(timeout=5)
        ok("--demo starts on twelve generated screenshots, away from real state")

        # undo must never replace a file that took the old name
        put(f"{t2}/shot.png", b"ORIGINAL")
        iid = scan2()["queue"][0]["id"]
        call(base, "/api/action", {"id": iid, "action": "move", "dest": dest})
        put(f"{t2}/shot.png", b"NEWER")
        call(base, "/api/undo", {})
        assert open(f"{t2}/shot.png", "rb").read() == b"NEWER", "undo overwrote a newer file"
        assert sorted(os.listdir(t2)) == ["shot 2.png", "shot.png"], os.listdir(t2)
        ok("undo never overwrites a file that took the old name")
        for n in os.listdir(t2): os.remove(f"{t2}/{n}")

        # a case-only rename is a rename, not a collision
        put(f"{t2}/case.png")
        iid = scan2()["queue"][0]["id"]
        r = call(base, "/api/action", {"id": iid, "action": "rename", "new_name": "CASE.png"})
        assert "error" not in r and "CASE.png" in os.listdir(t2), (r, os.listdir(t2))
        ok("renaming only the case of a name works")
        for n in os.listdir(t2): os.remove(f"{t2}/{n}")

        # an unreadable subfolder is not evidence its files are gone
        os.makedirs(f"{t2}/sub"); put(f"{t2}/top.png"); put(f"{t2}/sub/deep.png")
        assert scan2(rec=True)["queue_total"] == 2
        os.chmod(f"{t2}/sub", 0o000)
        r = scan2(rec=True)
        os.chmod(f"{t2}/sub", 0o755)
        assert r["problems"] and r["queue_total"] == 2, (r["problems"], r["queue_total"])
        ok("a denied subfolder keeps its files queued and is reported")
        shutil.rmtree(t2); os.makedirs(t2)

        # one malformed row must not break scanning
        r = call(base, "/api/destinations", {"destinations": [{"label": "no path"}, {"path": dest}], "armed": None})
        assert [d["path"] for d in r["config"]["destinations"]] == [dest], r["config"]["destinations"]
        assert "queue" in call(base, "/api/scan", {"folders": [{}, {"path": t2}], "types": ["images"]})
        ok("rows without a path are dropped instead of breaking every scan")

        # the export must not hand a spreadsheet a formula
        put(f"{t2}/=1+1.png")
        iid = scan2()["queue"][0]["id"]
        call(base, "/api/action", {"id": iid, "action": "keep"})
        with urllib.request.urlopen(base + "/api/export", timeout=10) as resp:
            assert '"\'=1+1.png"' in resp.read().decode()
        ok("a file named like a formula exports as text")

        print("\nall checks passed")
    finally:
        proc.terminate(); proc.wait(timeout=5)
        os.chmod(locked, 0o755)
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    main()
