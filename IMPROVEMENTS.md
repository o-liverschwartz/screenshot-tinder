# Refinement pass, 2026-08-27

A refinement inside the warm ivory direction, not a redesign. No layout was restructured, no
feature added or removed, no route or function renamed. One file changed: `static/index.html`.
`python3 test_triage.py` passes all 14 checks before and after.

Screenshots at deviceScaleFactor 2, same fourteen states before and after:

- Before: `docs/refinement-2026-08-27/before/`
- After: `docs/refinement-2026-08-27/after/`

`01-setup-empty` · `02-setup-filled` · `03-browse` · `04-review-unaimed` · `05-review-aimed` ·
`06-picker` · `07-shortcuts` · `08-drag-keep` · `09-rename` · `10-quarantine` ·
`11-quarantine-confirm` · `12-grid` · `13-toast` · `14-done`

---

## The five most visible

1. **The four action buttons stopped feeling cheap.** The circles carried a flat `0 2px 0` bottom
   lip, which reads as a keycap on a rectangle and as a smudge on a circle. Replaced with real
   elevation, border thinned 1.5px to 1px, and each of the three secondary buttons now hovers the
   same way in its own colour. Throw out previously got no hover ring at all while Rename and Star
   both did. *(Corpus: "something is cheap about these buttons", v5#74.)*
2. **The key hints under those buttons stopped being four more boxes.** `A or ←` was a 9px mono
   label inside its own bordered, shadowed mini-keycap: four tiny boxes under four circles under a
   photo. It is plain mono at 11px now, and the label above it went 11.5px to 13px. *(TASTE 7,
   "bigger, fewer text elements. No micro-typography.")*
3. **The tally stopped printing zeroes.** `8 to go · 0 kept · 0 filed · 0 out` is now `8 to go`,
   and each counter appears the first time it has a number. *(TASTE 8, "if a number doesn't change
   a decision, don't print it.")*
4. **Select many lost its wrapper panel.** The grid's heading and one-line subtitle sat inside a
   bordered card that contained nothing else, which is chrome around chrome. It is a plain section
   head aligned to the tiles now.
5. **Grid tile names say something.** Every screenshot is called `Screenshot 2026-0…`, so an 8.5px
   label truncating from the right told you nothing, twice. Truncation now runs from the left and
   the type is at 11px: `…t 1.33.10 PM.png`.

---

## Every change

### System: one scale instead of thirteen

- **Radius scale** (`--r-key` 8 / `--r-sm` 12 / `--r-md` 16 / `--r-lg` 24 / `--r-pill`), `:root`.
  There were 13 distinct raw radii in the file (5, 6, 7, 9, 10, 11, 12, 13, 14, 16, 22, 50%, 100px)
  and now there are five tokens plus `50%` for the circles. This is the single biggest reason a
  screen reads as assembled rather than drawn.
- **Type scale** (`--t-key` 11 → `--t-3xl` 25, eight steps), `:root`. There were 20 distinct font
  sizes; there is now one literal left in the file, the 22px swipe stamp, which is a deliberate
  one-off sized against the photo. Net direction is up, never down. *(TASTE 7; corpus "fonts are
  small".)*
- **Two near-identical spring curves collapsed to one.** `cubic-bezier(.34,1.6,.64,1)` and
  `cubic-bezier(.34,1.76,.64,1)` were both in use for the same job; the six sites on the second one
  now share a `--pop` token. Transition durations went from ten values to five, each with a reason:
  `.15s` state, `.22s` pop, `.26s` toast, `.3s` flyer, `.34s` land. The two curves that genuinely do
  something different, the card motion and the flyer, are untouched. No motion added anywhere it was
  absent.
- **One disabled language.** `.mini` used `opacity:.38`, `.vault-trash-btn` used `.4`, and `.go`
  swapped its background. All are `opacity:.4; cursor:default` now, with hover suppressed, and
  `.go:disabled` gets a border so it is a button outline rather than a beige slab.
- **Spacing normalised to 4px steps** across the setup panel, the sheet, the dock and the browse
  box (the file was full of 3, 7, 9, 11, 13, 15 and one 62).

### Bugs and flaws found while working

- **The browse panel's own buttons were unreachable.** `Use this folder` and `Close` sat inside the
  240px `overflow-y:auto` box and fell off the bottom of it on any real home folder. The list
  scrolls now, the two buttons stay pinned. `.browse` / `#browse-list`.
- **Disabled action buttons rendered as fully live.** On the done screen all four circles are
  `disabled` in the DOM but looked identical to enabled ones. Added `.act:disabled`. Visible in
  `14-done` before vs after.
- **Hover and selected were the same fill in the destination picker.** `.dest:hover` and `.dest.on`
  both painted `--spark-soft`, so pointing at a row looked like you had already aimed at it. Hover
  is the outline now, aimed is the fill.
- **Paths broke mid-token everywhere.** `word-break: break-all` split `data` into `da` / `ta` in
  the quarantine popover and cut folder names in half in the setup rows and the picker. Swapped for
  `overflow-wrap: anywhere`, which breaks only where a line actually runs out. Four places.
- **The Keep caption sat 3px low.** Keep is 6px taller than its siblings and its caption is anchored
  to its own bottom edge, so the four labels were on two baselines. `.act.keep .cap` offsets by the
  difference.
- **Shortcut labels started at four different x positions**, because rows carry one, two or three
  keycaps. Added a fixed-width `.kbd-keys` column; every label now begins at the same place. Best
  seen in `07-shortcuts`.
- **`border-radius` inside the `:focus-visible` rule.** A state selector was setting a geometric
  property, which reached any focused element that did not declare its own radius. Removed;
  `outline-offset` raised 2px to 3px so the ring clears the element it is on.

### Masthead

- `?` and `Folders and setup` were 32px and 34px tall next to each other. `.mini` now has an
  explicit 34px height and the help button matches exactly.
- The quarantine readout went from 10.5px to 12px mono, and the count carries a dotted underline
  that solidifies on hover, so it looks like the control it is. It was grey mono text with zero
  affordance carrying the app's whole promise. *(Corpus: tiny grey microcopy doing important work.)*
- The quarantine popover widened 240px to 276px, padding 10/13 to 14/16, body type 11px to 13px.
- `Move all N to Trash` and the two confirm buttons dropped `--mono` for the UI face and went to
  13px. A mono label inside a button was the only place in the app where that happened.

### Dock

- Filename 18px to 20px; the facts line 11px to 12px.
- **`age()` stopped saying the date twice.** `21 days ago, Aug 6, 2026` is now `21 days ago`, with
  the absolute date taking over past 30 days where the relative one stops being useful. This is a
  display function only, no behaviour touched. *(TASTE 8, "unnecessary if theres text above".)*
- The aim bar's `space` keycap now matches the shortcuts overlay and the picker numbers exactly:
  one keycap look for the three places a key is drawn as an object.
- Aimed folder name 13.5px to 15px, its path 10px to 12px.
- Aim bar copy trimmed: "Space files the card into it, every time, until you change it" →
  "Space files into it until you change it". *(TASTE 5, "every copy rewrite trims".)*
- The crosshair icon 30px to 32px, so it reads at the same weight as the keycap beside it.

### Setup

- Panel padding 20px to 22/24, headings 17px, body copy 12.5px to 14px.
- The `✕` on each row was a bare 15px glyph on `padding: 2px 5px`, sized nothing like the buttons
  around it. It is a 28px square target now, with a radius from the scale and a border that appears
  in the warn colour on hover.
- Chips, ghost buttons and inputs all moved onto the shared radius and type scale; ghost and chip
  hovers now also lift the background, which they did not before while `.mini` did.
- The destinations panel subtitle lost its trailing clause, which the empty state below it already
  says: "…and you can add more mid review" cut.
- `↑ ..` in the folder browser is `↑ up one folder`.

### Grid and bulk bar

- **Bulk buttons had no border at all.** Flat beige shapes on a beige bar. They are the same
  bordered pill family as `.mini` now, with `File` as the one filled primary.
- Tiles gained a resting shadow and a 2px hover lift (it was 1px, which is invisible).
- Tile label 8.5px to 11px, and truncated from the left as above.

### Sheets

- Picker number badges were blue chips; they are keycaps now, matching the other two keycap sites.
- Sheet radius 22 to 24, padding 22 to 24, heading 19px to 20px, body 12.5px to 14px.
- `aimed` tag 9.5px to 11px and vertically aligned.
- Three full-width buttons carried three copies of the same inline style. One `.ghost.wide` class.

### Copy, deadpan register

- Quarantine confirm: **"2 files."** followed by a dangling "These go to the macOS Trash…" became
  "**2 files** go to the macOS Trash, where you can still get them back. This app never deletes
  anything itself." Same facts, one sentence shape.
- Done screen dropped its hard `<br>` mid-sentence for a `44ch` measure, so the line breaks where
  the box ends.
- `Aim + file into that folder` → `Aim at that folder and file into it`.
- `Select many (grid)` → `Select many`, matching the button that opens it.
- One error toast said "that did not work" lowercase where its sibling said "That did not work".

---

## Left alone

- **The warm ivory palette, the blue-grey stage, the film grain, Fraunces / Outfit / DM Mono.**
  That is the direction and it is yours; every change here works inside it.
- **The swipe-stamp system** (`keep` / `pass` / `star` / `filed`) at 22px display serif. It is the
  signature and it is already the best thing on the screen. Only its corner radius moved onto the
  scale.
- **The wordmark's rotate-and-scale on hover.** A wiggling logo would normally be a slop tell, but
  this is a personal app and TASTE 12 gives personal pages more license than the business-case
  pages.
- **The card-stack peek, the pre-decode, the `stageKey` guard, the optimistic advance, the
  transitionend-plus-safety-net settle.** These are load-bearing and comment-documented; a visual
  pass has no business inside them.
- **The `include subfolders` checkbox sitting below the add row while each added row has its own.**
  It is genuinely confusing, but the two checkboxes mean different things and changing either would
  change behaviour. Flagged, not touched.
- **The path in the facts line truncating from the right**, hiding the leaf folder. The left-side
  truncation used on grid tiles would suit it, but the facts line is a single mixed string
  (`size · age · path`) and the trick only works on a whole element. It would need the path split
  into its own span, which is markup for a marginal gain. Flagged, not touched.
- **`server.py`, `test_triage.py`, `run.sh`, `README.md`.** Untouched.
