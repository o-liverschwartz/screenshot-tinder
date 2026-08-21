#!/usr/bin/env python3
"""
Screenshot Triage — a local, keyboard-driven reviewer for the files that pile up.

One file fills the screen. You keep it, file it, star it, rename it, or throw it out.
Nothing is ever hard deleted: "remove" moves the file into a local quarantine folder
that you empty yourself, and every action can be undone.

Stdlib only, no dependencies, never talks to the network.

    python3 server.py            # opens http://127.0.0.1:8765/
    python3 server.py --port 9000 --no-browser
"""
import argparse
import json
import os
import shutil
import subprocess
import sys
import threading
import uuid
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs, quote

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(BASE_DIR, "static")
DATA_DIR = os.path.join(BASE_DIR, "data")
TRASH_DIR = os.path.join(DATA_DIR, "quarantine")
STATE_FILE = os.path.join(DATA_DIR, "state.json")

os.makedirs(TRASH_DIR, exist_ok=True)

IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".bmp", ".tiff", ".tif", ".webp", ".heic", ".heif"}
CSV_EXTS = {".csv"}
IMAGE_MIME = {
    ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
    ".gif": "image/gif", ".bmp": "image/bmp", ".tiff": "image/tiff",
    ".tif": "image/tiff", ".webp": "image/webp", ".heic": "image/heic",
    ".heif": "image/heif",
}

# How many pending items the browser is given at once. The card view needs one,
# the grid needs a screenful. Sending all of them makes every keystroke re-send
# the entire library, which is slow once you point this at a big folder.
QUEUE_PAGE = 200

# No personal paths. Setup starts empty and the app suggests folders that
# actually exist on this machine, which the person confirms before anything runs.
DEFAULT_CONFIG = {
    "folders": [],
    "types": ["images"],
    "destinations": [],
    # The folder Space fires into. You aim once and keep swiping; changing it is
    # a deliberate act, not a decision you re-make on every single file.
    "armed": None,
}

SUGGESTED_FOLDERS = [
    ("Desktop", "~/Desktop"),
    ("Screenshots", "~/Desktop/Screenshots"),
    ("Downloads", "~/Downloads"),
    ("Pictures", "~/Pictures"),
]

STATE_LOCK = threading.Lock()


def migrate_state(state):
    """Old builds stored one history entry per action as {id, physical, prev}.
    Everything is a batch now, so wrap the legacy shape rather than dropping undo."""
    fixed = []
    for entry in state.get("history", []):
        if "ops" in entry:
            fixed.append(entry)
        elif "id" in entry:
            fixed.append({"label": entry.get("physical") or "keep", "ops": [entry]})
    state["history"] = fixed
    config = state.setdefault("config", dict(DEFAULT_CONFIG))
    config.setdefault("folders", [])
    config.setdefault("types", ["images"])
    config.setdefault("destinations", [])
    config.setdefault("armed", None)
    state.setdefault("items", {})
    return state


def load_state():
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r") as f:
                return migrate_state(json.load(f))
        except (json.JSONDecodeError, OSError):
            pass
    return migrate_state({"config": dict(DEFAULT_CONFIG), "items": {}, "history": []})


def save_state(state):
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f, indent=2)
    os.replace(tmp, STATE_FILE)


STATE = load_state()


def excluded_prefixes():
    """Never scan our own app folder, and never re-scan a destination. Otherwise a
    file you just filed into ~/Desktop/Receipts reappears on the next scan."""
    prefixes = [BASE_DIR]
    for dest in STATE["config"].get("destinations", []):
        prefixes.append(os.path.abspath(os.path.expanduser(dest["path"])))
    return prefixes


def is_excluded(path, prefixes):
    abspath = os.path.abspath(path)
    return any(abspath == p or abspath.startswith(p + os.sep) for p in prefixes)


def category_for_ext(ext):
    ext = ext.lower()
    if ext in IMAGE_EXTS:
        return "image"
    if ext in CSV_EXTS:
        return "csv"
    return None


def wanted(cat, wanted_types):
    return (cat == "image" and "images" in wanted_types) or (cat == "csv" and "csvs" in wanted_types)


def make_item(path):
    st = os.stat(path)
    name = os.path.basename(path)
    ext = os.path.splitext(name)[1]
    return {
        "path": path,
        "name": name,
        "ext": ext,
        "category": category_for_ext(ext),
        "size": st.st_size,
        "mtime": st.st_mtime,
        "status": "pending",
        "starred": False,
        "filed_to": None,
    }


def scan_folder(folder_path, recursive, wanted_types, prefixes):
    """Returns (paths, problem). A problem is never silently an empty folder:
    macOS denying access to Desktop or Downloads must read as denied, not as done."""
    found = []
    expanded = os.path.abspath(os.path.expanduser(folder_path))
    if not os.path.exists(expanded):
        return found, "folder does not exist"
    if not os.path.isdir(expanded):
        return found, "not a folder"

    def listdir(d):
        try:
            return sorted(os.listdir(d)), None
        except PermissionError:
            return [], "permission denied by macOS, grant Full Disk Access to your terminal"
        except OSError as e:
            return [], f"could not read: {e.strerror or e}"

    if recursive:
        problem = None
        for root, dirs, files in os.walk(expanded, onerror=lambda e: None):
            if is_excluded(root, prefixes):
                dirs[:] = []
                continue
            dirs[:] = [d for d in dirs if not is_excluded(os.path.join(root, d), prefixes)]
            for fname in files:
                if fname.startswith("."):
                    continue
                if wanted(category_for_ext(os.path.splitext(fname)[1]), wanted_types):
                    found.append(os.path.join(root, fname))
        if not found:
            _, problem = listdir(expanded)
        return found, problem

    entries, problem = listdir(expanded)
    if problem:
        return found, problem
    for fname in entries:
        if fname.startswith("."):
            continue
        full = os.path.join(expanded, fname)
        if not os.path.isfile(full) or is_excluded(full, prefixes):
            continue
        if wanted(category_for_ext(os.path.splitext(fname)[1]), wanted_types):
            found.append(full)
    return found, None


def run_scan(config):
    with STATE_LOCK:
        STATE["config"] = config
        prefixes = excluded_prefixes()
        existing_paths = {item["path"]: iid for iid, item in STATE["items"].items()}

        # Forget pending items whose file moved or vanished outside the app.
        for iid, item in list(STATE["items"].items()):
            if item["status"] == "pending" and not os.path.exists(item["path"]):
                del STATE["items"][iid]
                existing_paths.pop(item["path"], None)

        all_found, problems = [], []
        for folder in config["folders"]:
            paths, problem = scan_folder(folder["path"], folder.get("recursive", False), config["types"], prefixes)
            all_found.extend(paths)
            if problem:
                problems.append({"path": folder["path"], "problem": problem})

        added = 0
        for path in all_found:
            if path in existing_paths:
                iid = existing_paths[path]
                item = STATE["items"][iid]
                if item["status"] == "pending":
                    try:
                        st = os.stat(path)
                        item["size"] = st.st_size
                        item["mtime"] = st.st_mtime
                    except OSError:
                        pass
                continue
            try:
                STATE["items"][uuid.uuid4().hex] = make_item(path)
            except OSError:
                continue
            existing_paths[path] = True
            added += 1

        save_state(STATE)
        return added, problems


def set_finder_label(path, label_index):
    """Colour the file in Finder. macOS only, best effort, never fatal."""
    if sys.platform != "darwin":
        return False
    try:
        escaped = path.replace("\\", "\\\\").replace('"', '\\"')
        script = f'tell application "Finder" to set label index of (POSIX file "{escaped}" as alias) to {label_index}'
        subprocess.run(["osascript", "-e", script], capture_output=True, timeout=10)
        return True
    except Exception as e:
        print(f"[warn] Finder label failed for {path}: {e}", file=sys.stderr)
        return False


def free_path(directory, name):
    """~/Receipts/shot.png exists already, so become shot 2.png rather than clobber it."""
    candidate = os.path.join(directory, name)
    if not os.path.exists(candidate):
        return candidate
    stem, ext = os.path.splitext(name)
    n = 2
    while True:
        candidate = os.path.join(directory, f"{stem} {n}{ext}")
        if not os.path.exists(candidate):
            return candidate
        n += 1


def apply_one(item_id, action, new_name=None, dest=None):
    """Mutates one item. Caller holds STATE_LOCK. Returns (op, error)."""
    item = STATE["items"].get(item_id)
    if not item:
        return None, "not found"
    prev_snapshot = dict(item)
    physical = None

    if action == "keep":
        item["status"] = "kept"

    elif action == "remove":
        dest_path = os.path.join(TRASH_DIR, f"{item_id}__{item['name']}")
        try:
            shutil.move(item["path"], dest_path)
        except OSError as e:
            return None, str(e)
        item["path"] = dest_path
        item["status"] = "removed"
        physical = "remove"

    elif action == "move":
        if not dest:
            return None, "no destination folder chosen"
        target_dir = os.path.abspath(os.path.expanduser(dest))
        try:
            os.makedirs(target_dir, exist_ok=True)
            target = free_path(target_dir, item["name"])
            shutil.move(item["path"], target)
        except OSError as e:
            return None, str(e)
        item["path"] = target
        item["name"] = os.path.basename(target)
        item["status"] = "filed"
        item["filed_to"] = target_dir
        physical = "move"

    elif action == "rename":
        if not new_name:
            return None, "new name required"
        new_name = os.path.basename(new_name).strip()
        if not new_name:
            return None, "new name required"
        new_path = os.path.join(os.path.dirname(item["path"]), new_name)
        if os.path.exists(new_path):
            return None, "a file with that name already exists"
        try:
            os.rename(item["path"], new_path)
        except OSError as e:
            return None, str(e)
        item["path"] = new_path
        item["name"] = new_name
        item["status"] = "kept"
        physical = "rename"

    elif action == "star":
        item["starred"] = True
        item["status"] = "kept"
        set_finder_label(item["path"], 2)
        physical = "star"

    else:
        return None, "unknown action"

    return {"id": item_id, "physical": physical, "prev": prev_snapshot}, None


def do_action(item_id, action, new_name=None, dest=None):
    with STATE_LOCK:
        op, error = apply_one(item_id, action, new_name, dest)
        if error:
            return {"error": error}, (404 if error == "not found" else 400)
        STATE["history"].append({"label": action, "ops": [op]})
        save_state(STATE)
        return {"item": {"id": item_id, **STATE["items"][item_id]}}, 200


def do_bulk(item_ids, action, dest=None):
    """Many files, one undo step. Half-failing is fine and reported; the ops that
    did land stay landed and stay undoable together."""
    with STATE_LOCK:
        ops, failures = [], []
        for item_id in item_ids:
            op, error = apply_one(item_id, action, None, dest)
            if error:
                failures.append({"id": item_id, "error": error})
            else:
                ops.append(op)
        if ops:
            STATE["history"].append({"label": f"{action} {len(ops)}", "ops": ops})
            save_state(STATE)
        return {"applied": len(ops), "failures": failures}, 200


def do_undo():
    with STATE_LOCK:
        if not STATE["history"]:
            return {"error": "nothing to undo"}, 400
        entry = STATE["history"].pop()
        undone, errors = 0, []
        for op in reversed(entry["ops"]):
            item_id, prev = op["id"], op["prev"]
            item = STATE["items"].get(item_id)
            if not item:
                continue
            try:
                if op["physical"] in ("remove", "move"):
                    shutil.move(item["path"], prev["path"])
                elif op["physical"] == "rename":
                    os.rename(item["path"], prev["path"])
                elif op["physical"] == "star" and not prev.get("starred"):
                    set_finder_label(prev["path"], 0)
            except OSError as e:
                errors.append(str(e))
                continue
            STATE["items"][item_id] = prev
            undone += 1
        save_state(STATE)
        return {"undone": undone, "label": entry.get("label"), "errors": errors}, 200


def queue_snapshot(problems=None):
    with STATE_LOCK:
        items = STATE["items"]
        pending = [{"id": iid, **item} for iid, item in items.items() if item["status"] == "pending"]
        pending.sort(key=lambda x: x["mtime"])
        counts = {"pending": 0, "kept": 0, "removed": 0, "filed": 0}
        starred = 0
        for item in items.values():
            counts[item["status"]] = counts.get(item["status"], 0) + 1
            if item.get("starred"):
                starred += 1
        counts["starred"] = starred
        last = STATE["history"][-1]["label"] if STATE["history"] else None
        return {
            "config": STATE["config"],
            "queue": pending[:QUEUE_PAGE],
            "queue_total": len(pending),
            "counts": counts,
            "history_depth": len(STATE["history"]),
            "last_action": last,
            "problems": problems or [],
            "quarantine": TRASH_DIR,
        }


def kept_report():
    """The other output. Everything you decided to keep, as a file you can take away."""
    with STATE_LOCK:
        rows = [("status", "starred", "filed_to", "name", "path")]
        keepers = [i for i in STATE["items"].values() if i["status"] in ("kept", "filed")]
        keepers.sort(key=lambda i: (i["status"], i["name"].lower()))
        for item in keepers:
            rows.append((
                item["status"],
                "yes" if item.get("starred") else "",
                item.get("filed_to") or "",
                item["name"],
                item["path"],
            ))
    return "\n".join(",".join('"' + str(c).replace('"', '""') + '"' for c in row) for row in rows)


def browse_dirs(path):
    path = os.path.abspath(os.path.expanduser(path or "~"))
    if not os.path.isdir(path):
        path = os.path.expanduser("~")
    problem = None
    try:
        entries = sorted(os.listdir(path))
    except PermissionError:
        entries, problem = [], "permission denied by macOS"
    except OSError as e:
        entries, problem = [], f"could not read: {e.strerror or e}"
    dirs = [
        {"name": name, "path": os.path.join(path, name)}
        for name in entries
        if not name.startswith(".") and os.path.isdir(os.path.join(path, name))
    ]
    return {
        "path": path,
        "parent": os.path.dirname(path) if path != "/" else None,
        "dirs": dirs,
        "problem": problem,
    }


def suggestions():
    out = []
    for label, raw in SUGGESTED_FOLDERS:
        full = os.path.expanduser(raw)
        if os.path.isdir(full):
            out.append({"label": label, "path": full})
    return out


class Handler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass

    def _send_json(self, payload, status=200):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self):
        length = int(self.headers.get("Content-Length", 0))
        if length == 0:
            return {}
        try:
            return json.loads(self.rfile.read(length))
        except json.JSONDecodeError:
            return {}

    def do_GET(self):
        parsed = urlparse(self.path)
        route = parsed.path
        if route in ("/", "/index.html"):
            self._serve_static("index.html", "text/html")
        elif route == "/api/state":
            self._send_json(queue_snapshot())
        elif route == "/api/suggestions":
            self._send_json({"suggestions": suggestions(), "home": os.path.expanduser("~")})
        elif route == "/api/browse":
            qs = parse_qs(parsed.query)
            self._send_json(browse_dirs(qs.get("path", [os.path.expanduser("~")])[0]))
        elif route == "/api/image":
            qs = parse_qs(parsed.query)
            self._serve_image(qs.get("id", [None])[0])
        elif route == "/api/export":
            self._serve_export()
        else:
            self.send_response(404)
            self.end_headers()

    def _serve_image(self, item_id):
        with STATE_LOCK:
            item = STATE["items"].get(item_id)
            path = item["path"] if item and item.get("category") == "image" else None
        if not path or not os.path.isfile(path):
            self.send_response(404)
            self.end_headers()
            return
        try:
            with open(path, "rb") as f:
                body = f.read()
        except OSError:
            self.send_response(404)
            self.end_headers()
            return
        ext = os.path.splitext(path)[1].lower()
        self.send_response(200)
        self.send_header("Content-Type", IMAGE_MIME.get(ext, "application/octet-stream"))
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _serve_export(self):
        body = kept_report().encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/csv; charset=utf-8")
        self.send_header("Content-Disposition", 'attachment; filename="kept-files.csv"')
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        route = urlparse(self.path).path
        if route == "/api/scan":
            body = self._read_json()
            config = {
                "folders": body.get("folders", []),
                "types": body.get("types", ["images"]),
                "destinations": body.get("destinations", []),
                "armed": body.get("armed"),
            }
            added, problems = run_scan(config)
            snap = queue_snapshot(problems)
            snap["added"] = added
            self._send_json(snap)
        elif route == "/api/destinations":
            body = self._read_json()
            with STATE_LOCK:
                STATE["config"]["destinations"] = body.get("destinations", [])
                STATE["config"]["armed"] = body.get("armed")
                save_state(STATE)
            self._send_json(queue_snapshot())
        elif route == "/api/action":
            body = self._read_json()
            result, status = do_action(body.get("id"), body.get("action"), body.get("new_name"), body.get("dest"))
            if status == 200:
                snap = queue_snapshot()
                result["counts"] = snap["counts"]
            self._send_json(result, status)
        elif route == "/api/bulk":
            body = self._read_json()
            result, status = do_bulk(body.get("ids", []), body.get("action"), body.get("dest"))
            self._send_json(result, status)
        elif route == "/api/undo":
            result, status = do_undo()
            self._send_json(result, status)
        else:
            self.send_response(404)
            self.end_headers()

    def _serve_static(self, filename, content_type):
        try:
            with open(os.path.join(STATIC_DIR, filename), "rb") as f:
                body = f.read()
        except OSError:
            self.send_response(404)
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Type", content_type + "; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def main():
    parser = argparse.ArgumentParser(description="Local keyboard-driven file triage.")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()

    port, server = args.port, None
    for _ in range(10):
        try:
            server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
            break
        except OSError:
            port += 1
    if server is None:
        print("Could not bind to a port", file=sys.stderr)
        sys.exit(1)

    url = f"http://127.0.0.1:{port}/"
    print(f"Screenshot Triage  {url}")
    print(f"Quarantine         {TRASH_DIR}")
    print("Nothing is deleted. Ctrl+C to stop.")

    if not args.no_browser:
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
