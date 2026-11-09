# visual-oracle

**No screenshot, no "looks fine".**

A Hermes Agent plugin that turns the sentence *"the UI is fine"* from an opinion into a
measurement. It captures a page once as a baseline, then compares later renders pixel by pixel and
answers with numbers, boxes and images, and it will not let an agent close a turn while the page
is showing something the agent has not looked at.

```python
visual_base(name="checkout", url="http://localhost:3000/checkout")
visual_check(name="checkout", url="http://localhost:3000/checkout")
# FAIL  checkout: 22.774% changed (allowed 0.2%)
#   region [653, 241, 347, 236] 80192px density 0.979
#   region [0, 227, 341, 186]   61004px density 0.962
#   diff: …/checkout/last/diff.png
```

## Why this exists

An agent edits CSS, the page still renders, nothing throws, and every test still passes. So the
agent reports success. Meanwhile the payment button is hidden behind a panel, the nav overlaps the
title, or a sprite has been cut in half.

These are not exceptions. They are not caught by unit tests. And the one thing that cannot catch
them is the model's own opinion of its own work, which is exactly what "looks fine" is.

The usual fix is a human staring at a screenshot afterwards. visual-oracle moves that stare into the
loop, as a check the agent has to pass before it can say it is done.

**What it is not:** it does not judge design, it does not know what "beautiful" means, and it never
claims a page is correct. It reports what changed, where, and whether that change was declared.

## What a check gives you

Every `visual_check` returns a verdict and the evidence behind it:

```json
{
  "status": "FAIL",
  "changed_pct": 22.774,
  "changed_pixels": 141196,
  "total_pixels": 620000,
  "regions": [
    {"bbox": [653, 241, 347, 236], "changed_pixels": 80192, "area": 81892, "density": 0.979},
    {"bbox": [0, 227, 341, 186],   "changed_pixels": 61004, "area": 63426, "density": 0.962}
  ],
  "reasons": ["22.774% changed, over the 0.2% threshold, largest region at [653, 241, 347, 236]"],
  "artifacts": {
    "diff":    "…/last/diff.png",
    "overlay": "…/last/overlay.png",
    "regions": ["…/last/region-0.png", "…/last/region-1.png"]
  }
}
```

- `regions` are **independent** boxes, not one box swallowing the whole screen
- `density` tells you whether a box is one solid change or a few scattered pixels
- `diff.png` is the page in grey with changed pixels in red
- `overlay.png` is the baseline blended over the current render
- `report.json` on disk holds the same payload for anything that wants to read it later

## How it works

### 1. Capture, a real browser session, no driver

Chrome is launched with `--remote-debugging-port=0`; the port and WebSocket path are read from
`DevToolsActivePort` in a throwaway profile, and `ws.py` (a ~120-line WebSocket client, standard
library only) speaks the DevTools protocol to it.

That is what makes the waiting possible, and the waiting is what stops false positives:

| cause of a false positive | what the capture does about it |
|---|---|
| webfonts swap in after first paint | blocks on `document.fonts.ready` |
| element animates into place | optional `wait_selector`, then a stability check |
| page still settling | requires two consecutive identical frames |
| images that never load | reported; `require_images=true` makes it an error |
| continuous animation | refuses to guess, raises "rendering never stabilised" |

Each capture records what produced it: URL, viewport, Chrome version, protocol version, timestamp,
font status, number of frames needed, mask report, broken images, byte size.

### 2. Diff, decoded and compared in-process

`pngdiff.py` reads the PNG chunk stream, inflates the pixels and undoes the per-scanline filters
(None / Sub / Up / Average / Paeth), then compares channels with a tolerance. No Pillow, no
ImageMagick, no service.

Changed pixels are grouped on an 8×8 block grid and merged into connected regions, so a page with
three separate changes reports three boxes.

### 3. Verdict, three states, never a shrug

```
PASS    within policy: under the threshold, or exactly the change the contract declared
FAIL    outside policy: protected region touched, unexpected region, or over max_changed_pct
REVIEW  the policy cannot be evaluated reliably, or the change needs a human decision
```

`REVIEW` matters: a visual difference is evidence of a change, not proof of a defect. When the
plugin cannot decide honestly, it says so instead of picking the convenient answer.

### 4. Gate, the turn does not close on a failing check

`pre_verify` fires once per turn, before the agent finishes. On `FAIL` or `REVIEW` it returns:

```json
{"action": "continue",
 "message": "[visual-oracle] FAIL on 'checkout': 22.774% changed (allowed 0.2%)\nregions: [653,241,347,236], [0,227,341,186]\ndiff: …\nlook at the diff. Fix the layout, or state plainly what changed and why."}
```

The agent gets the numbers and the image paths. Hermes bounds this with `agent.max_verify_nudges`,
so it can never become an unbounded loop.

## Visual change contracts

Declaring intent before touching the page is what separates "changed as planned" from "changed
something else":

```python
visual_check("checkout", url="…", contract={
    "expected_changes": [
        {"target": "#cta", "change": "move", "description": "move pay below the price"}
    ],
    "protected_regions": ["#nav", "#price"],
    "max_changed_pct": 3.0,
})
```

Deterministic rules:

1. under threshold **and** nothing declared → `PASS`
2. any region overlapping a `protected_regions` box → `FAIL`
3. region overlapping an expected target → counted as expected
4. `change: "move"` also accepts the **hole** the element left (same footprint, ±15%)
5. over `max_changed_pct` → `FAIL`
6. leftover regions matching nothing declared → `FAIL`
7. expected change declared but not observed → `REVIEW`
8. contract selector missing from the page → `REVIEW`

**Selectors describe location, not meaning.** The contract cannot know that moving `#cta` below
`#price` is the right move. It knows the pixels inside `#cta` moved and nothing else did. The
judgement beyond that stays with a human, on purpose.

## Masks

```python
visual_check("feed", url="…", mask=[[0, 0, 1280, 80],   # coordinates
                                    "#live-clock"])      # CSS selector
```

Selector masks are resolved **before** the capture: the element keeps its space but is set to
`visibility: hidden`, so a clock or a carousel cannot pollute the diff.

- a selector matching nothing is an **error**, not a silent no-op
- masks may not cover more than 50% of the viewport ("a check that ignores half the screen cannot
  fail")
- every ignored rectangle is listed in the capture metadata, so a human can audit exactly what was
  excluded

## Tools

| tool | what it does |
|---|---|
| `visual_base` | capture (or accept) a baseline: fonts waited, optional selector wait, stable frames |
| `visual_check` | render now, diff, apply contract and masks, return PASS / FAIL / REVIEW with artefacts |
| `visual_list` | every baseline in this session with its last verdict and region count |
| `pre_verify` (hook) | keeps the turn open while the last verdict is FAIL or REVIEW |

## Tests

```
python3 tests/pngdiff_proof.py     18/18 checks passed   no browser, generated fixtures
python3 tests/visual_proof.py      16/16 checks passed   real Chrome, real pages
hermes plugins validate .          Validation passed
```

`pngdiff_proof.py` generates PNGs using every filter type and both colour models, then asserts:
identical images, a single changed pixel, three disconnected regions, corner bounding boxes, mask
correctness, dimension mismatch, bad signatures, truncated files, broken IDAT streams, unsupported
colour types, and byte-identical JSON for identical inputs.

`visual_proof.py` drives Chrome: font wait, selector wait, an unstable page that must fail loudly, a
declared move that passes a contract, a protected region that fails it, a missing contract target
(REVIEW), selector masks, the mask coverage limit, required-image failures, artefacts on disk, and
the hook refusing to close a turn.

## Demo

`demo/` contains the fixture used in the recording: `baseline.html`, `broken.html`, `fixed.html`;
the bug is one panel landing on top of the pay button, plus `demo.mp4` (30s, English narration).

Real numbers from that run: the broken state is **22.774% changed** with two regions, and the fixed
state is **0.0%**.

## Requirements and limits

- Python 3.10+
- Hermes Agent with the `pre_verify` hook (tested on 0.21.x; `requires_hermes` not declared yet)
- Chrome or Chromium for `url=` captures; `image=` works without a browser
- PNG: 8-bit RGB/RGBA, non-interlaced. Anything else raises `PngError` naming the format
- selector masks and contracts need a live page; with `image=` there is no DOM, so selectors
  produce `REVIEW`
- sub-tolerance colour shifts (< 8 per channel) are not reported
- the diff is geometry: a wrong colour inside an unchanged shape may not register

Full parameter reference: `docs/configuration.md`. Example artefacts: `docs/examples/`.

## Layout

```
__init__.py   tools, contract evaluation, storage, pre_verify hook, register()
capture.py    DevTools capture: fonts, selectors, stability, selector masks, metadata
cdp.py        Chrome session over the DevTools protocol
ws.py         minimal WebSocket client (standard library only)
pngdiff.py    PNG decode, pixel diff, regions, diff/overlay/crop artefacts
tests/        deterministic decoder tests + real-Chrome end-to-end tests
docs/         configuration reference, example report and images
demo/         fixture pages and the recorded demo
```
