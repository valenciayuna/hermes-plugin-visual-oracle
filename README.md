# visual-oracle

**No screenshot, no "looks fine".**

A Hermes Agent plugin that turns "the UI is fine" from an opinion into a measurement. Capture a
baseline, diff later renders against it pixel by pixel, and keep the turn open while the evidence
says the page changed in a way nobody declared.

```
visual_base    capture the baseline (fonts waited, frames stable)
visual_check   render now, diff, and answer PASS / FAIL / REVIEW with regions and artefacts
visual_list    every baseline in this session with its last verdict
pre_verify     keeps the turn open while a check is failing
```

---

## The failure it exists for

An agent edits layout. The page still renders, nothing throws, no test fails, so it reports "looks
right" without ever looking. A panel covering a menu, a missing button, a cut sprite, a lost
z-index: none of these are exceptions, none are covered by unit tests, and the model's own opinion
of its work is the one thing that cannot catch them.

The usual fix is a human staring at a screenshot. This plugin moves that stare into the loop.

---

## How it works

### 1. Capture (`capture.py` + `cdp.py`)

Chrome is started with `--remote-debugging-port=0`; the port and WebSocket path are read from
`DevToolsActivePort` inside a throwaway profile. A ~120-line WebSocket client in `ws.py` (standard
library only) speaks the DevTools protocol. No Selenium, no Playwright, no pip install.

That buys the waiting that stops false positives:

| problem | what the capture does |
|---|---|
| webfonts swap in after first paint | blocks on `document.fonts.ready` |
| element animates into place | optional `wait_selector`, then a stability check |
| images that never load | reported, and `require_images=true` makes it an error |
| page not settled | two consecutive identical frames required (`stable=false` to skip) |

Every capture records: URL, viewport, Chrome version, protocol version, timestamp, elapsed time,
font status, stability tries, byte size, mask report, broken images.

### 2. Diff (`pngdiff.py`)

Standard library only: `zlib` + `struct`, supporting 8-bit RGB/RGBA non-interlaced PNG — which is
what Chrome emits.

- decode → unfilter (None / Sub / Up / Average / Paeth) → compare per channel, tolerance 8
- changed pixels are grouped on an 8x8 block grid into **independent regions**
- each region reports `bbox`, `changed_pixels`, `area`, `density`
- artefacts written next to the case: `diff.png` (grey page, red changes), `overlay.png`
  (baseline blended over current), `region-N.png` crops, `report.json`

Sample report (`docs/examples/report.json`, produced by a real run):

```json
{ "status": "PASS", "changed_pct": 2.607, "changed_pixels": 14077, "total_pixels": 540000,
  "regions": [ { "bbox": [20, 120, 160, 44], "changed_pixels": 7040, "density": 1.0 },
               { "bbox": [20, 300, 160, 44], "changed_pixels": 7037, "density": 1.0 } ],
  "reasons": ["2 expected change(s) confirmed inside the declared targets"],
  "artifacts": { "diff": "…/diff.png", "overlay": "…/overlay.png" } }
```

### 3. Verdicts

```
PASS    within policy: under the threshold, or exactly the change the contract declared
FAIL    outside policy: protected region touched, unexpected region, or over max_changed_pct
REVIEW  the policy cannot be evaluated reliably, or the change needs a human decision
```

A visual difference is evidence of a change, not proof of a defect. `REVIEW` exists for exactly
that gap, and it is never silently downgraded to a pass.

### 4. The gate

`pre_verify` fires once per turn, before the agent finishes. If the last check is `FAIL` or
`REVIEW`, it returns `{"action": "continue", "message": …}` with the percentages, the regions, the
diff path and both images. Hermes bounds this with `agent.max_verify_nudges`, so it cannot loop.

---

## Visual Change Contract

Declare what you intend to change *before* touching the page, and the verdict can distinguish
"changed as declared" from "changed something else":

```python
visual_check("checkout", url="http://localhost:3000/checkout", contract={
    "expected_changes": [
        {"target": "#cta", "change": "move", "description": "move the pay button below the price"}
    ],
    "protected_regions": ["#nav", "#price"],
    "max_changed_pct": 3.0,
})
```

Semantics, all deterministic:

- `target` is a CSS selector (or an `[x, y, w, h]` rectangle)
- a region overlapping an expected target counts as expected
- for `change: "move"`, the hole the element leaves behind is also expected (same footprint
  within 15%)
- a region overlapping a `protected_regions` entry is an immediate **FAIL**
- `max_changed_pct` caps the total, even when every change is declared
- a selector that does not exist on the page yields **REVIEW**, never a pass
- a contract that expects a change which did not happen yields **REVIEW**

**Selectors describe location, not meaning.** The contract cannot know that moving `#cta` below
`#price` is the *right* move. It only knows that the pixels inside `#cta` moved and nothing else
did. Any judgement beyond that is a human's, by design.

---

## Masks

Two forms, mixable:

```python
visual_check("feed", url="…", mask=[[0, 0, 1280, 80],      # coordinates
                                   "#live-clock"])         # CSS selector
```

Selector masks are resolved **before** the capture: the element keeps its space but is set to
`visibility: hidden`, so a clock or carousel cannot add noise. Rules:

- a selector that matches nothing is an **error**, not a silent no-op
- masks may not cover more than 50% of the viewport
- every ignored rect is listed in the capture metadata

---

## What it does not do

- It does not judge design. It has no opinion about whether a UI is good.
- It is not an end-to-end test framework: it compares renders, it does not drive the app.
- It does not read the DOM to explain a change. `bbox` is where pixels moved, not which node.
- It cannot tell an intended change from an accident. Only the contract narrows that gap, and it
  narrows it by geometry, not by understanding.
- It does not update baselines automatically. Ever.

## Limits

- 8-bit RGB/RGBA, non-interlaced PNG only; anything else raises `PngError` naming the format.
- Chrome or Chromium must be installed for `url=` captures. `image=` works without it.
- Continuous animation prevents a stable capture; mask the moving region or pass `stable=false`.
- Selector masks and contracts require a live page (`url=`); with `image=` there is no DOM, so
  selectors yield `REVIEW`.
- Screenshot diffs are geometry. A wrong colour that keeps the same shape is caught only inside a
  region that changed; sub-tolerance colour shifts (< 8 per channel) are not.

## Requirements

- Python 3.10+
- Hermes Agent with the `pre_verify` hook (tested against 0.21.x; `requires_hermes` is not declared)
- Chrome/Chromium for capture

## Tests

```
python3 tests/pngdiff_proof.py     18/18 checks passed   (no browser, generated fixtures)
python3 tests/visual_proof.py      16/16 checks passed   (real Chrome, real pages)
hermes plugins validate .          Validation passed
```

`pngdiff_proof.py` generates PNGs with every filter type (None/Sub/Up/Average/Paeth), RGB and RGBA,
single-pixel and disconnected changes, corner bounding boxes, masks, dimension mismatch, corrupted
signatures, truncated files, broken IDAT streams and unsupported colour types — and asserts that the
same inputs produce byte-identical JSON reports.

`visual_proof.py` drives Chrome: font wait, selector wait, an unstable page that must fail loudly, a
declared move that passes the contract, a protected region that fails it, a contract target that is
missing (REVIEW), selector masks, mask coverage limits, required-image failures, artefacts on disk,
and the hook refusing to close a turn.

## Docs

- `docs/configuration.md` — every parameter, default, and validation rule
- `docs/examples/` — a real report, its diff, overlay, baseline and current images

## Demo

`visual-oracle-demo.mp4` — 39 seconds, English narration, recorded from this build.

## Layout

```
__init__.py   tools, contract evaluation, storage, pre_verify hook, register()
capture.py    DevTools capture: fonts, selector, stability, selector masks, metadata
cdp.py        Chrome session over the DevTools protocol
ws.py         minimal WebSocket client (standard library)
pngdiff.py    PNG decode, pixel diff, regions, diff/overlay/crop artefacts
tests/        deterministic decoder tests + real-Chrome end-to-end tests
docs/         configuration reference and example artefacts
```
