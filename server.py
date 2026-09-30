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
import fcntl
import json
import os
import shutil
import subprocess
import sys
import threading
import time
import uuid
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(BASE_DIR, "static")
DATA_DIR = os.path.join(BASE_DIR, "data")
STATE_FILE = os.path.join(DATA_DIR, "state.json")

# Grid thumbnails are a cache: every one of them can be rebuilt from the original
# in about 70ms, and each is a downscaled copy of somebody's own photo. That means
# it belongs where the OS already understands caches -- macOS purges ~/Library/
# Caches under disk pressure without being asked -- and not in the app directory,
# where an earlier version of this put it.
LEGACY_THUMB_DIR = os.path.join(DATA_DIR, "thumbs")
# The cache is now a shared, per-user location, so the test suite has to be able to
# point somewhere else. Without this, running the tests would wipe a real cache.
THUMB_DIR = os.environ.get("TRIAGE_CACHE_DIR") or os.path.join(
    os.path.expanduser("~/Library/Caches" if sys.platform == "darwin" else "~/.cache"),
    "screenshot-triage", "thumbs")

# Quarantine used to live inside this app folder. That is the wrong place for
# files we promise never to delete: the app folder is a git checkout, and
# `git clean -xfd` walks straight past .gitignore. It now defaults to a folder
# in your home directory that no repo command can reach, and it is configurable.
LEGACY_QUARANTINE = os.path.join(DATA_DIR, "quarantine")
DEFAULT_QUARANTINE = os.path.join(os.path.expanduser("~"), ".screenshot-triage", "quarantine")

os.makedirs(DATA_DIR, exist_ok=True)

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

# What W actually does. It used to be hardcoded: mark it kept and paint it red in
# Finder. A Finder label is invisible outside Finder, so the other half of the
# choice is putting a mark in the filename itself, which travels anywhere.
#
# Measured on this machine, not remembered: `label index` N sets kMDItemFSLabel
# 8-N, so index 1..7 walks orange, red, yellow, blue, purple, green, gray.
STAR_COLORS = [
    {"index": 1, "name": "Orange", "css": "#F0A030"},
    {"index": 2, "name": "Red",    "css": "#E0554E"},
    {"index": 3, "name": "Yellow", "css": "#EFC94C"},
    {"index": 4, "name": "Blue",   "css": "#4C8DEF"},
    {"index": 5, "name": "Purple", "css": "#A46FD8"},
    {"index": 6, "name": "Green",  "css": "#5FBF6A"},
    {"index": 7, "name": "Gray",   "css": "#9AA0A8"},
]
DEFAULT_STAR = {"mode": "label", "color": 2, "affix": "prefix", "text": "★ "}

# Thumbnails are the whole reason the grid is quick, but they are copies of your
# images on disk, so turning them off has to be a real option and not a rebuild.
DEFAULT_THUMBNAILS = True

# How long a thumbnail is allowed to sit on disk. 0 means keep nothing between
# sessions; -1 means keep one for as long as its file is still waiting to be
# reviewed, and no longer. Anything else is a number of days.
THUMB_TTL_CHOICES = [
    {"days": 0,  "label": "Every launch",   "hint": "nothing is kept between sessions"},
    {"days": 1,  "label": "After a day",    "hint": ""},
    {"days": 7,  "label": "After a week",   "hint": ""},
    {"days": -1, "label": "Keep them",      "hint": "until the file leaves the queue"},
]
DEFAULT_THUMB_TTL = 7
SWEEP_EVERY = 1800   # seconds between the background sweeps

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
    # An existing install already has files sitting in the old in-app folder.
    # Moving the default out from under them would orphan those files, so keep
    # pointing at the old folder while it still holds anything.
    if not config.get("quarantine"):
        config["quarantine"] = legacy_or_default_quarantine()
    star = config.setdefault("star", dict(DEFAULT_STAR))
    for k, v in DEFAULT_STAR.items():
        star.setdefault(k, v)
    config.setdefault("thumbnails", DEFAULT_THUMBNAILS)
    config.setdefault("thumb_ttl_days", DEFAULT_THUMB_TTL)
    state.setdefault("items", {})
    return state


def legacy_or_default_quarantine():
    try:
        # Dotfiles do not count. A folder holding nothing but a .DS_Store is empty
        # as far as anyone is concerned, and treating it as occupied would pin an
        # existing install to the unsafe old location forever.
        if os.path.isdir(LEGACY_QUARANTINE) and any(
                not n.startswith(".") for n in os.listdir(LEGACY_QUARANTINE)):
            return LEGACY_QUARANTINE
    except OSError:
        pass
    return DEFAULT_QUARANTINE


def quarantine_dir():
    """Always resolved fresh from config, and always exists by the time it is used."""
    path = os.path.abspath(os.path.expanduser(
        STATE["config"].get("quarantine") or DEFAULT_QUARANTINE))
    os.makedirs(path, exist_ok=True)
    return path


def set_quarantine(raw):
    """Validated at the boundary: we are about to move real files in here."""
    path = os.path.abspath(os.path.expanduser((raw or "").strip()))
    if not raw or not raw.strip() or path == "/":
        return None, "pick a real folder"
    try:
        os.makedirs(path, exist_ok=True)
    except OSError as e:
        return None, f"could not create that folder: {e.strerror or e}"
    if not os.access(path, os.W_OK):
        return None, "that folder is not writable"
    return path, None


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


def reconcile_removed():
    """A quarantined file emptied out by hand, or left behind by a change of
    quarantine folder, is gone from quarantine whatever the stored count says.
    "trashed" is already the word for left-quarantine-and-not-coming-back, so undo
    goes on refusing it cleanly instead of the header claiming files that are not
    there and the empty button failing on every one of them.

    Called at startup and again on every scan. Quarantine only changes through this
    app while it is running, so those are the two moments the count can be stale.
    Caller holds STATE_LOCK, or is startup where nothing else is running yet."""
    fixed = 0
    for item in STATE["items"].values():
        if item["status"] == "removed" and not os.path.exists(item["path"]):
            item["status"] = "trashed"
            fixed += 1
    return fixed


_stale = reconcile_removed()
if _stale:
    save_state(STATE)

# An earlier version cached thumbnails inside the app folder. Nothing in there is
# anything but a rebuildable copy, so it is retired rather than left to rot.
if os.path.isdir(LEGACY_THUMB_DIR):
    try:
        shutil.rmtree(LEGACY_THUMB_DIR)
        print("[cache] retired the old in-app thumbnail folder", file=sys.stderr)
    except OSError:
        pass


def excluded_prefixes():
    """Never scan our own app folder, and never re-scan a destination. Otherwise a
    file you just filed into ~/Desktop/Receipts reappears on the next scan."""
    prefixes = [BASE_DIR, os.path.abspath(os.path.expanduser(
        STATE["config"].get("quarantine") or DEFAULT_QUARANTINE))]
    for dest in STATE["config"].get("destinations", []):
        prefixes.append(os.path.abspath(os.path.expanduser(dest["path"])))
    return prefixes


def clean_rows(rows):
    """Folder and destination lists come from the browser and are indexed by
    ["path"] on every scan. One row without a path would break every scan after."""
    if not isinstance(rows, list):
        return []
    return [{"path": r["path"], **{k: r[k] for k in ("label", "recursive") if k in r}}
            for r in rows
            if isinstance(r, dict) and isinstance(r.get("path"), str) and r["path"].strip()]


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
        problem, blocked = None, []
        for root, dirs, files in os.walk(expanded, onerror=blocked.append):
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
        if blocked and not problem:
            # Not evidence the files under it are gone. run_scan keeps this folder's
            # queued files when it sees a problem, the same as a denied top folder.
            problem = f"could not read {blocked[0].filename}: {blocked[0].strerror}"
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

        all_found, problems = [], []
        for folder in config["folders"]:
            paths, problem = scan_folder(folder["path"], folder.get("recursive", False), config["types"], prefixes)
            all_found.extend(paths)
            if problem:
                problems.append({"path": folder["path"], "problem": problem})

        # The queue is defined as what the configured folders currently hold. Take
        # a folder off the list and its files have to leave the queue with it, or
        # changing folders visibly does nothing. This also covers
        # a pending file that was moved or deleted behind the app's back: it simply
        # is not in what the scan found.
        #
        # A folder the OS refused is the one exception. An unreadable folder is not
        # evidence its files are gone, so anything under it stays put -- the same
        # reason scan_folder reports a denial rather than an empty result.
        found_set = set(all_found)
        denied = [os.path.abspath(os.path.expanduser(p["path"])) for p in problems]
        for iid, item in list(STATE["items"].items()):
            if item["status"] != "pending" or item["path"] in found_set:
                continue
            if any(item["path"] == d or item["path"].startswith(d + os.sep) for d in denied):
                continue
            del STATE["items"][iid]
            existing_paths.pop(item["path"], None)
        reconcile_removed()

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

        sweep_thumbs()
        expire_thumbs()
        save_state(STATE)
        return added, problems


def set_finder_label(path, label_index):
    """Colour the file in Finder. macOS only, best effort, never fatal."""
    if sys.platform != "darwin":
        return False
    try:
        escaped = path.replace("\\", "\\\\").replace('"', '\\"')
        script = f'tell application "Finder" to set label index of (POSIX file "{escaped}" as alias) to {label_index}'
        done = subprocess.run(["osascript", "-e", script], capture_output=True, timeout=10)
        if done.returncode:
            print(f"[warn] Finder label failed for {path}: {done.stderr.decode().strip()}", file=sys.stderr)
        return done.returncode == 0
    except Exception as e:
        print(f"[warn] Finder label failed for {path}: {e}", file=sys.stderr)
        return False


def trash_files(paths):
    """Move files to the macOS Trash, recoverable there, via one Finder/osascript call
    for all of them. TRIAGE_TRASH_DIR overrides this with a plain move into that folder,
    so tests never have to actually touch the real Trash. Returns (moved, failed)."""
    override = os.environ.get("TRIAGE_TRASH_DIR")
    if override:
        os.makedirs(override, exist_ok=True)
        moved, failed = [], []
        for p in paths:
            try:
                shutil.move(p, free_path(override, os.path.basename(p)))
                moved.append(p)
            except OSError as e:
                failed.append({"path": p, "error": str(e)})
        return moved, failed

    existing = [p for p in paths if os.path.exists(p)]
    failed = [{"path": p, "error": "not found"} for p in paths if p not in existing]
    if not existing:
        return [], failed
    items = ", ".join(
        'POSIX file "%s"' % p.replace("\\", "\\\\").replace('"', '\\"') for p in existing
    )
    script = f'tell application "Finder" to delete {{{items}}}'
    try:
        subprocess.run(["osascript", "-e", script], capture_output=True, timeout=30, check=True)
        return existing, failed
    except Exception as e:
        print(f"[warn] Trash failed: {e}", file=sys.stderr)
        return [], failed + [{"path": p, "error": str(e)} for p in existing]


def do_quarantine_reveal():
    """A hidden folder you cannot get to is a promise you cannot check. One button
    opens it in Finder. Only ever the quarantine folder -- this does not take a path
    from the browser, so it cannot be pointed at anything else."""
    if sys.platform != "darwin":
        return {"error": "Only supported on macOS"}, 400
    path = quarantine_dir()
    try:
        subprocess.run(["open", path], capture_output=True, timeout=10, check=True)
    except Exception as e:
        return {"error": f"could not open it: {e}"}, 400
    return {"opened": path}, 200


def do_quarantine_clear():
    """Empty the quarantine folder into the macOS Trash. Every quarantine record is
    dropped from state, and any pending undo that would restore one of those files
    is left in place but made to refuse cleanly (see do_undo) instead of moving a
    file that is no longer there."""
    if sys.platform != "darwin" and not os.environ.get("TRIAGE_TRASH_DIR"):
        return {"error": "Only supported on macOS"}, 400
    with STATE_LOCK:
        removed = [(iid, item) for iid, item in STATE["items"].items() if item["status"] == "removed"]
        if not removed:
            return {"moved": 0, "failed": 0}, 200
        moved, failed = trash_files([item["path"] for _, item in removed])
        moved_set = set(moved)
        moved_count = 0
        for iid, item in removed:
            if item["path"] in moved_set:
                item["status"] = "trashed"
                moved_count += 1
        save_state(STATE)
        return {"moved": moved_count, "failed": len(failed)}, 200


def thumb_name(item_id, st):
    """The cache key is the source file's own mtime and size, so editing or
    replacing a file invalidates its thumbnail without anything having to notice."""
    return f"{item_id}-{int(st.st_mtime)}-{st.st_size}.jpg"


def thumb_stats():
    total, count = 0, 0
    try:
        with os.scandir(THUMB_DIR) as it:
            for entry in it:
                try:
                    total += entry.stat().st_size
                    count += 1
                except OSError:
                    pass
    except OSError:
        pass
    return {"files": count, "bytes": total, "dir": THUMB_DIR,
            "enabled": bool(STATE["config"].get("thumbnails", DEFAULT_THUMBNAILS)),
            "ttl_days": STATE["config"].get("thumb_ttl_days", DEFAULT_THUMB_TTL),
            "choices": THUMB_TTL_CHOICES}


def clear_thumbs():
    """Everything, unconditionally. Nothing here is anything but a rebuildable copy."""
    freed, removed = 0, 0
    try:
        with os.scandir(THUMB_DIR) as it:
            entries = list(it)
    except OSError:
        return {"removed": 0, "freed": 0}
    for entry in entries:
        try:
            size = entry.stat().st_size
            os.remove(entry.path)
            freed += size
            removed += 1
        except OSError:
            pass
    return {"removed": removed, "freed": freed}


def expire_thumbs():
    """The scheduled half. Age is read off the thumbnail's own mtime, so it survives
    restarts without a ledger to keep in sync."""
    ttl = STATE["config"].get("thumb_ttl_days", DEFAULT_THUMB_TTL)
    if ttl < 0:
        return 0
    if ttl == 0:
        return clear_thumbs()["removed"]
    cutoff = time.time() - ttl * 86400
    freed, removed = 0, 0
    try:
        with os.scandir(THUMB_DIR) as it:
            entries = list(it)
    except OSError:
        return 0
    for entry in entries:
        try:
            st = entry.stat()
            if st.st_mtime >= cutoff:
                continue
            os.remove(entry.path)
            freed += st.st_size
            removed += 1
        except OSError:
            pass
    if removed:
        print(f"[cache] expired {removed} thumbnail(s), {freed // 1024} KB", file=sys.stderr)
    return removed


def sweep_now():
    """Both halves, under the lock. Startup, every scan, and the background timer."""
    with STATE_LOCK:
        return sweep_thumbs() + expire_thumbs()


def sweep_loop():
    while True:
        time.sleep(SWEEP_EVERY)
        try:
            sweep_now()
        except Exception as e:
            print(f"[cache] sweep failed: {e}", file=sys.stderr)


def sweep_thumbs():
    """Drop every thumbnail that no longer backs a file waiting to be reviewed.
    The grid only ever draws pending items, so a thumbnail for anything else is a
    copy of a photo being kept for no reason. Rebuilding one costs about 70ms.

    Called at startup and after every scan, which are the two moments the set of
    pending items changes wholesale. Caller holds STATE_LOCK, or is startup."""
    keep = set()
    for iid, item in STATE["items"].items():
        if item["status"] != "pending" or item.get("category") != "image":
            continue
        try:
            keep.add(thumb_name(iid, os.stat(item["path"])))
        except OSError:
            continue
    freed, removed = 0, 0
    try:
        with os.scandir(THUMB_DIR) as it:
            entries = list(it)
    except OSError:
        return 0
    for entry in entries:
        if entry.name in keep:
            continue
        try:
            size = entry.stat().st_size
            os.remove(entry.path)
            freed += size
            removed += 1
        except OSError:
            pass
    if removed:
        print(f"[cache] dropped {removed} stale thumbnail(s), {freed // 1024} KB", file=sys.stderr)
    return removed


def thumb_for(item_id, path):
    """A grid of 200 tiles was pulling 200 full-resolution originals over the wire,
    which is the whole reason the grid felt slow. sips ships with macOS, so a 480px
    JPEG costs no dependency; it is cached on disk keyed by the file's own mtime and
    size, so it is generated once. Returns None anywhere sips is unavailable or
    unhappy, and the caller falls back to the original file."""
    if not STATE["config"].get("thumbnails", DEFAULT_THUMBNAILS):
        return None
    try:
        st = os.stat(path)
    except OSError:
        return None
    out = os.path.join(THUMB_DIR, thumb_name(item_id, st))
    if os.path.exists(out):
        return out
    tmp = f"{out}.{uuid.uuid4().hex}.tmp.jpg"
    try:
        os.makedirs(THUMB_DIR, exist_ok=True)
        # Unique temp then atomic replace: two browser tabs can ask for the same
        # tile at the same moment and must not write over each other mid-file.
        subprocess.run(["sips", "-s", "format", "jpeg", "-s", "formatOptions", "70",
                        "-Z", "480", path, "--out", tmp],
                       capture_output=True, timeout=20, check=True)
        os.replace(tmp, out)
        return out
    except Exception:
        try:
            os.remove(tmp)
        except OSError:
            pass
        return None


def clean_star(raw):
    """Whatever the browser sends becomes part of a real filename, so it is cleaned
    here rather than trusted. Returns (star_config, error)."""
    star = dict(DEFAULT_STAR)
    star.update({k: v for k, v in (raw or {}).items() if k in DEFAULT_STAR})
    if star["mode"] not in ("label", "rename"):
        return None, "pick either a Finder label or a name change"
    if star["affix"] not in ("prefix", "suffix"):
        return None, "the mark goes at the front or the back"
    try:
        star["color"] = int(star["color"])
    except (TypeError, ValueError):
        return None, "that is not a colour"
    if star["color"] not in [c["index"] for c in STAR_COLORS]:
        return None, "that is not a colour"
    # Anything that would build a path instead of a name, or a name macOS refuses.
    star["text"] = "".join(ch for ch in str(star["text"] or "") if ch not in '/:\\\0').strip()
    if star["mode"] == "rename" and not star["text"]:
        return None, "give it something to add to the name"
    return star, None


def starred_name(name, star):
    """Front or back, and the back means before the extension -- a mark after .jpg
    would change what the file is, not what it is called."""
    text = star.get("text") or ""
    if not text:
        return name
    stem, ext = os.path.splitext(name)
    if star.get("affix") == "suffix":
        return name if stem.endswith(text) else f"{stem}{text}{ext}"
    return name if name.startswith(text) else f"{text}{stem}{ext}"


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
    physical = warn = None

    if action == "keep":
        item["status"] = "kept"

    elif action == "remove":
        dest_path = os.path.join(quarantine_dir(), f"{item_id}__{item['name']}")
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
        # samefile: on a case-insensitive volume "a.png" -> "A.png" names itself.
        if os.path.exists(new_path) and not os.path.samefile(new_path, item["path"]):
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
        star = STATE["config"].get("star") or DEFAULT_STAR
        item["starred"] = True
        item["status"] = "kept"
        if star.get("mode") == "rename":
            new_name = starred_name(item["name"], star)
            if new_name != item["name"]:
                try:
                    # free_path rather than refusing on a collision: starring is a
                    # swipe, and a swipe that fails mid-run is worse than a name
                    # that picked up a " 2".
                    target = free_path(os.path.dirname(item["path"]), new_name)
                    os.rename(item["path"], target)
                except OSError as e:
                    return None, str(e)
                item["path"] = target
                item["name"] = os.path.basename(target)
                # Reuse the rename op so undo already knows how to walk this back.
                physical = "rename"
        else:
            if not set_finder_label(item["path"], int(star.get("color", 2))):
                warn = "Kept, but the Finder label failed. Allow this terminal to control Finder in System Settings."
            physical = "star"

    else:
        return None, "unknown action"

    return {"id": item_id, "physical": physical, "prev": prev_snapshot, "warn": warn}, None


def do_action(item_id, action, new_name=None, dest=None):
    with STATE_LOCK:
        op, error = apply_one(item_id, action, new_name, dest)
        if error:
            return {"error": error}, (404 if error == "not found" else 400)
        STATE["history"].append({"label": action, "ops": [op]})
        save_state(STATE)
        result = {"item": {"id": item_id, **STATE["items"][item_id]}}
        if op.get("warn"):
            result["warning"] = op["warn"]
        return result, 200


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
        entry = STATE["history"][-1]
        for op in entry["ops"]:
            item = STATE["items"].get(op["id"])
            if op["physical"] == "remove" and item and item.get("status") == "trashed":
                return {"error": "That file was moved to the Trash and can't be restored from here."}, 400
        STATE["history"].pop()
        undone, errors, retry = 0, [], []
        for op in reversed(entry["ops"]):
            item_id, prev = op["id"], op["prev"]
            item = STATE["items"].get(item_id)
            if not item:
                continue
            try:
                if op["physical"] in ("remove", "move", "rename"):
                    back = prev["path"]
                    # Something new can take the old name while the file is away.
                    # os.rename would replace it without a word, so step aside.
                    if os.path.lexists(back) and not (
                            os.path.exists(item["path"]) and os.path.samefile(item["path"], back)):
                        back = free_path(os.path.dirname(back), os.path.basename(back))
                        prev = {**prev, "path": back, "name": os.path.basename(back)}
                    shutil.move(item["path"], back)
                elif op["physical"] == "star" and not prev.get("starred"):
                    set_finder_label(prev["path"], 0)
            except OSError as e:
                errors.append(str(e))
                if os.path.exists(item["path"]):    # still here, so this can be retried
                    retry.append(op)
                continue
            STATE["items"][item_id] = prev
            undone += 1
        if retry:
            STATE["history"].append({"label": entry.get("label"), "ops": retry})
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
            "quarantine": quarantine_dir(),
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
    # A name that starts with = + - @ is a formula to Excel and Numbers.
    cell = lambda c: "'" + str(c) if str(c).startswith(("=", "+", "-", "@", "\t", "\r")) else str(c)
    return "\n".join(",".join('"' + cell(c).replace('"', '""') + '"' for c in row) for row in rows)


COUNT_CAP = 2000


def count_matching(directory, wanted_types):
    """How many reviewable files sit directly in this folder. Browsing blind -- a
    list of names with no idea which one holds the 400 screenshots -- is the reason
    picking a folder felt like guesswork. Capped so a huge folder cannot stall the
    listing, and never recursive."""
    n = 0
    try:
        with os.scandir(directory) as it:
            for entry in it:
                if n >= COUNT_CAP:
                    return COUNT_CAP, True
                if entry.name.startswith("."):
                    continue
                try:
                    if not entry.is_file(follow_symlinks=False):
                        continue
                except OSError:
                    continue
                if wanted(category_for_ext(os.path.splitext(entry.name)[1]), wanted_types):
                    n += 1
    except OSError:
        return None, False
    return n, False


def browse_dirs(path, wanted_types, show_hidden=False):
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
    dirs = []
    for name in entries:
        # The default quarantine lives in a dot folder, so hiding every dot folder
        # made the app's own default unreachable through the app's own picker.
        if name.startswith(".") and not show_hidden:
            continue
        full = os.path.join(path, name)
        if not os.path.isdir(full):
            continue
        count, capped = count_matching(full, wanted_types)
        dirs.append({"name": name, "path": full, "count": count, "capped": capped})
    here, here_capped = count_matching(path, wanted_types)
    return {
        "path": path,
        "parent": os.path.dirname(path) if path != "/" else None,
        "dirs": dirs,
        "count": here,
        "capped": here_capped,
        "problem": problem,
    }


def suggestions(wanted_types):
    out = []
    for label, raw in SUGGESTED_FOLDERS:
        full = os.path.expanduser(raw)
        if os.path.isdir(full):
            count, capped = count_matching(full, wanted_types)
            out.append({"label": label, "path": full, "count": count, "capped": capped})
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
        try:
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length)) if length else {}
        except ValueError:      # bad length, or JSONDecodeError, which is one
            return {}
        return body if isinstance(body, dict) else {}

    def _ours(self):
        """Any page in any tab can send requests to 127.0.0.1, and a rebound DNS
        name can read the answers. Only take requests that name this server."""
        host = self.headers.get("Host") or ""
        if host.rsplit(":", 1)[0] not in ("127.0.0.1", "localhost"):
            return False
        origin = self.headers.get("Origin")
        return origin is None or origin == "http://" + host

    def do_GET(self):
        if not self._ours():
            return self._refuse()
        parsed = urlparse(self.path)
        route = parsed.path
        if route in ("/", "/index.html"):
            self._serve_static("index.html", "text/html")
        elif route == "/api/state":
            self._send_json(queue_snapshot())
        elif route == "/api/suggestions":
            qs = parse_qs(parsed.query)
            types = qs.get("types", ["images"])[0].split(",")
            self._send_json({"suggestions": suggestions(types), "home": os.path.expanduser("~"),
                             "quarantine": quarantine_dir(),
                             "default_quarantine": DEFAULT_QUARANTINE,
                             "app_dir": BASE_DIR,
                             "star_colors": STAR_COLORS,
                             "finder_labels": sys.platform == "darwin"})
        elif route == "/api/browse":
            qs = parse_qs(parsed.query)
            types = qs.get("types", ["images"])[0].split(",")
            hidden = qs.get("hidden", ["0"])[0] == "1"
            self._send_json(browse_dirs(qs.get("path", [os.path.expanduser("~")])[0], types, hidden))
        elif route in ("/api/image", "/api/thumb"):
            qs = parse_qs(parsed.query)
            self._serve_image(qs.get("id", [None])[0], thumb=(route == "/api/thumb"))
        elif route == "/api/thumbs":
            self._send_json(thumb_stats())
        elif route == "/api/export":
            self._serve_export()
        else:
            self.send_response(404)
            self.end_headers()

    def _serve_image(self, item_id, thumb=False):
        with STATE_LOCK:
            item = STATE["items"].get(item_id)
            path = item["path"] if item and item.get("category") == "image" else None
        if not path or not os.path.isfile(path):
            self.send_response(404)
            self.end_headers()
            return
        served = (thumb and thumb_for(item_id, path)) or path

        # The old header was no-store, so flipping back to a card you have already
        # seen, or redrawing the grid, re-read and re-decoded the file every single
        # time. The tag is the file's own mtime and size: rename or replace the file
        # and it changes, so a stale picture is not possible.
        try:
            st = os.stat(served)
        except OSError:
            self.send_response(404)
            self.end_headers()
            return
        etag = '"%x-%x"' % (int(st.st_mtime), st.st_size)
        if self.headers.get("If-None-Match") == etag:
            self.send_response(304)
            self.send_header("ETag", etag)
            self.send_header("Cache-Control", "private, max-age=300")
            self.end_headers()
            return
        try:
            with open(served, "rb") as f:
                body = f.read()
        except OSError:
            self.send_response(404)
            self.end_headers()
            return
        ext = os.path.splitext(served)[1].lower()
        self.send_response(200)
        self.send_header("Content-Type", IMAGE_MIME.get(ext, "application/octet-stream"))
        self.send_header("Content-Length", str(len(body)))
        self.send_header("ETag", etag)
        self.send_header("Cache-Control", "private, max-age=300")
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

    def _refuse(self):
        self.send_response(403)
        self.end_headers()

    def do_POST(self):
        if not self._ours():
            return self._refuse()
        route = urlparse(self.path).path
        if route == "/api/scan":
            body = self._read_json()
            config = {
                "folders": clean_rows(body.get("folders")),
                "types": [t for t in body.get("types", ["images"]) if t in ("images", "csvs")],
                "destinations": clean_rows(body.get("destinations")),
                "armed": body.get("armed"),
                "quarantine": STATE["config"].get("quarantine"),
                "star": STATE["config"].get("star"),
                "thumbnails": STATE["config"].get("thumbnails", DEFAULT_THUMBNAILS),
                "thumb_ttl_days": STATE["config"].get("thumb_ttl_days", DEFAULT_THUMB_TTL),
            }
            if body.get("quarantine"):
                path, error = set_quarantine(body["quarantine"])
                if error:
                    self._send_json({"error": error}, 400)
                    return
                config["quarantine"] = path
            added, problems = run_scan(config)
            snap = queue_snapshot(problems)
            snap["added"] = added
            self._send_json(snap)
        elif route == "/api/destinations":
            body = self._read_json()
            thumbs = None
            if "thumbnails" in body or "thumb_ttl_days" in body:
                ttl = body.get("thumb_ttl_days", STATE["config"].get("thumb_ttl_days"))
                if ttl not in [c["days"] for c in THUMB_TTL_CHOICES]:
                    self._send_json({"error": "that is not one of the clearing options"}, 400)
                    return
                thumbs = (bool(body.get("thumbnails", True)), ttl)
            star = None
            if body.get("star"):
                star, error = clean_star(body["star"])
                if error:
                    self._send_json({"error": error}, 400)
                    return
            quarantine = None
            if body.get("quarantine"):
                quarantine, error = set_quarantine(body["quarantine"])
                if error:
                    self._send_json({"error": error}, 400)
                    return
            with STATE_LOCK:
                STATE["config"]["destinations"] = clean_rows(body.get("destinations"))
                STATE["config"]["armed"] = body.get("armed")
                if quarantine:
                    STATE["config"]["quarantine"] = quarantine
                if star:
                    STATE["config"]["star"] = star
                if thumbs:
                    STATE["config"]["thumbnails"], STATE["config"]["thumb_ttl_days"] = thumbs
                save_state(STATE)
            # Switching thumbnails off has to take the existing copies with it, or
            # the setting only stops new ones being written.
            if thumbs and not thumbs[0]:
                clear_thumbs()
            elif thumbs:
                sweep_now()
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
        elif route == "/api/thumbs/clear":
            freed = clear_thumbs()
            freed["stats"] = thumb_stats()
            self._send_json(freed)
        elif route == "/api/quarantine/reveal":
            result, status = do_quarantine_reveal()
            self._send_json(result, status)
        elif route == "/api/quarantine/clear":
            result, status = do_quarantine_clear()
            if status == 200:
                result["counts"] = queue_snapshot()["counts"]
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
    # Two copies would each hold their own STATE and overwrite data/state.json, and
    # the port fallback below would hide that. Ceiling: flock is POSIX, and this
    # app already needs macOS for Finder and the Trash.
    lock = open(os.path.join(DATA_DIR, "server.lock"), "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        sys.exit("Already running. Two copies would overwrite each other's state.")
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

    sweep_now()
    threading.Thread(target=sweep_loop, daemon=True).start()

    stats = thumb_stats()
    ttl = stats["ttl_days"]
    when = next((c["label"] for c in THUMB_TTL_CHOICES if c["days"] == ttl), str(ttl))
    url = f"http://127.0.0.1:{port}/"
    print(f"Screenshot Triage  {url}")
    print(f"Quarantine         {quarantine_dir()}")
    print(f"Thumbnail cache    {stats['dir']}  ({when.lower()})")
    print("Nothing is deleted. Ctrl+C to stop.")

    if not args.no_browser:
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
