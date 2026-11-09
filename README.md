# visual-oracle

**No screenshot, no "looks fine".**

A Hermes Agent plugin that makes a UI claim measurable. An agent captures a page once as a
baseline, later renders are decoded and compared pixel by pixel against it, and a turn cannot
close while a check is failing.

```
visual_base    capture the baseline
visual_check   render now, diff against the baseline, PASS or FAIL
visual_list    every baseline in this session with its last verdict
pre_verify     hook: keeps the turn open while the last check is failing
```

---

## The failure it exists for

An agent edits CSS or layout. The page still renders. Nothing throws, no test fails, so the agent
reports "looks right" — without ever looking. Missing buttons, a panel covering a menu, a cut
sprite, a chip that lost its z-index: none of these are exceptions, and none are visible to unit
tests or to the model's own opinion of its work.

The usual outcome is that a human has to notice. This plugin moves that noticing into the loop.

---

## How it works

### 1. Capture

`visual_base` and `visual_check` render the page through a local Chrome:

```
google-chrome --headless=new --no-sandbox --disable-gpu --hide-scrollbars \
  --force-device-scale-factor=1 --user-data-dir=<temp> \
  --window-size=1280,720 --virtual-time-budget=1500 \
  --screenshot=<out.png> <url>
```

No driver, no browser API, no network beyond the page itself. The viewport, `wait_ms` and a
temporary profile are all explicit. If Chrome is missing, or an image path you passed does not
exist, the tool returns an **error** — never a pass.

### 2. Decode and diff

The comparison is done in this process, in pure Python:

- read the PNG chunk stream (`IHDR`, `IDAT`, `IEND`), `zlib.decompress` the pixels
- undo per-scanline filters (None / Sub / Up / Average / Paeth)
- walk both images and count pixels whose channel delta exceeds `tolerance` (default 8)

Output for every check:

```json
{ "status": "FAIL", "changed_pct": 0.677, "changed_pixels": 6237, "total_pixels": 921600,
  "bbox": [1198, 190, 68, 114], "threshold_pct": 0.2 }
```

`bbox` is the bounding box of everything that moved, in image coordinates. A different image size
is reported as `size changed`, not silently compared.

### 3. The gate

`pre_verify` fires once per turn, just before the agent finishes. If the last check in the session
is `FAIL`, the hook returns:

```json
{ "action": "continue",
  "message": "[visual-oracle] screenshot 'resto-menu' changed by 0.677% (allowed 0.2%) bbox [1198, 190, 68, 114].\nbaseline: …\ncurrent : …\nLook at both images. Either fix the layout or say plainly what changed and why." }
```

The turn is refused and the agent is handed **both image paths**. It can fix the layout, or state
what changed — but it cannot close the turn on the claim that everything is fine. Hermes bounds
this with `agent.max_verify_nudges`, so a hook cannot trap the loop.

---

## Masks and thresholds

Animated pages change every frame. Mask the regions that are allowed to move:

```python
visual_check("menu", url="https://…", mask=[[0, 600, 1280, 120]])  # x, y, w, h
```

`threshold` is the percentage of pixels allowed to change before a check fails (default `0.2`).
A 2px nudge on a 1280x720 page is about 0.15% and passes; a button moving 280px is 0.68% and fails.

---

## What it does not do

- It does not judge design. It has no opinion about whether a UI is good, only whether it changed
  and where.
- It is not an end-to-end test framework. It compares renders; it does not drive the app.
- It does not read the DOM. A `bbox` tells you where pixels moved, not which element moved.
- It cannot explain intent. "Changed on purpose" and "changed by accident" look identical; that
  verdict stays with a human.

## Limits

- 8-bit RGB/RGBA, non-interlaced PNG only. Anything else is refused with a clear error rather than
  guessed at. Chrome screenshots satisfy this; some other tools do not.
- Requires Chrome or Chromium on the machine, or an `image=` path you captured yourself.
- Full-image comparison on a page with continuous animation produces noise. Mask the moving
  regions, or pause the page first.

## Requirements

- Hermes Agent with plugin hooks (`pre_verify`) — `requires_hermes` is not declared yet; tested on
  0.21.x
- Python 3.10+
- Chrome or Chromium for `url=` captures (optional if you always pass `image=`)

## Proof

```
python3 tests/visual_proof.py      10/10 checks passed
hermes plugins validate .          Validation passed
```

The proof script renders real pages with Chrome and asserts on the real numbers: identical render
passes at 0.0%, a moved button fails at 0.576% with a bbox on the button row, a 2px nudge stays
under a 0.2% threshold, a masked region is ignored, a failing check keeps the turn open, and
unknown cases or missing images are errors rather than passes.

## Real run against a live page

```
baseline 'resto-menu' stored (554057 bytes)
PASS resto-menu: 0.0% changed (allowed 0.2%)          # same page twice, no false alarm
FAIL resto-menu: 0.677% changed (allowed 0.2%) bbox=[1198, 190, 68, 114]
```

The second result came from moving one CSS rule by 46px; the bounding box lands exactly on the
button that moved.

## Demo

`visual-oracle-demo.mp4` — 39 seconds, English narration, recorded from this build.

## Layout

```
__init__.py      tools, storage, pre_verify hook, register()
capture.py       headless Chrome capture
pngdiff.py       PNG decode + pixel diff (standard library only)
tests/           proof script (real Chrome, real PNGs)
tools/           evidence image composer used in the demo
```
