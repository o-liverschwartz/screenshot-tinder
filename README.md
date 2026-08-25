# Screenshot Triage

![Screenshot Triage](docs/card.png)

Your Mac drops every screenshot on the Desktop and you never look at them again.
This is a local reviewer for that pile. One file fills the screen, you make one
decision, the next one appears.

It is a dating app for screenshots, not a file manager.

## Run it

Python 3, no dependencies, no install step, never touches the network.

```
python3 server.py
```

That opens `http://127.0.0.1:8765/`. Pick the folders you want to review, pick
the folders you might file things into, and start.

```
python3 server.py --port 9000 --no-browser
```

## The keys

| Key | What happens |
| --- | --- |
| `A` or `←` | Throw out. The file moves into a local quarantine folder. |
| `D` or `→` | Keep. The file stays exactly where it is, marked done. |
| `↑` | Star. Keeps it and colours it in Finder so you can find it later. |
| `↓` | Rename, without leaving the card. |
| `Space` | File it into the folder you aimed at. |
| `1` – `9` | Aim at a different folder and file this one into it. |
| `C` | Change what Space is aimed at. |
| `G` | Select many at once. |
| `Z` | Undo. |

## Aiming

The bar under the card shows where `Space` sends things. Aim it once and every
`Space` after that goes to the same folder, so a run of receipts is five presses
rather than five menus. Press `C` or a number key when the subject changes.
Destinations are created if they do not exist, and you can add one mid session.

## Nothing is deleted

Throwing a file out moves it to `data/quarantine/` inside this folder. Nothing in
this app calls `rm`. When the pile is big enough, one button moves the whole
quarantine to the macOS Trash, where it is still recoverable, and that is the only
time anything leaves this folder.

Every action is undoable, including a batch of them. `Z` walks back through the
history and moves files physically back where they came from. The one thing undo
will not do is pull a file back out of the Trash. It says so instead of quietly
doing nothing.

## Ten files

Run it, pick your Desktop, and swipe ten. Aim `Space` at a folder and file one.
Press `Z` and watch it come back. That is the whole app.

## Permissions

macOS may refuse to let a terminal read `~/Desktop` or `~/Downloads`. When that
happens the app says the folder was denied. It never reports a denied folder as
an empty one, because "you are done" and "I was not allowed to look" are not the
same sentence. Grant Full Disk Access to your terminal in System Settings and
scan again.

## Checking it still works

```
python3 test_triage.py
```

Runs against a temp folder and asserts the things that matter: a collision never
overwrites, a removed file is still on disk, and undoing everything leaves every
file exactly where it started.

## What it holds

```
server.py          the whole backend, stdlib only
static/index.html  the whole frontend, one file
test_triage.py     the self-check
run.sh             the same thing, with a shorter name
docs/card.png      the picture at the top of this file
data/              your state and your quarantine, git ignored
```
