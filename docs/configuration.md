# Configuration reference

Every knob, its default, and what is validated. Anything invalid raises instead of being ignored:
a check that cannot run honestly must not report a pass.

---

## `visual_base`

| parameter | type | default | meaning / validation |
|---|---|---|---|
| `name` | string | required | case name; sanitised to `[A-Za-z0-9-_.]`, 60 chars |
| `url` | string | `""` | page to render. Required unless `image` is given |
| `image` | string | `""` | existing PNG path; the file must exist or the call errors |
| `width` | int | `1280` | viewport width, also stored with the baseline |
| `height` | int | `720` | viewport height |
| `wait_ms` | int | `250` | extra settle time after `document.readyState` settles |
| `wait_selector` | string | `""` | block until this selector exists; timeout 10s → error |
| `stable` | bool | `true` | require two consecutive identical frames |
| `mask` | list | `[]` | `[x,y,w,h]` or CSS selectors; see Masks |
| `require_images` | bool | `false` | if true, any broken `<img>` is an error |
| `steps` | list | `[]` | actions before the screenshot: `{"click": sel}`, `{"wait_selector": sel}`, `{"scroll": px}`, `{"settle_ms": ms}` |

Baseline files: `$HERMES_HOME/plugin-data/visual-oracle/<session>/<case>/baseline.png`
plus `meta.json` holding the capture metadata (Chrome version, viewport, fonts, tries, masks).

---

## `visual_check`

| parameter | type | default | meaning / validation |
|---|---|---|---|
| `name` | string | required | must already have a baseline, else error |
| `url` | string | `""` | page to render now |
| `image` | string | `""` | compare a PNG instead of rendering |
| `threshold` | float | `0.2` | percent of pixels allowed to change |
| `mask` | list | `[]` | coordinates or selectors; unlimited count, ≤50% coverage |
| `contract` | object \| string | `{}` | contract dict, or a path to a `.json` file |
| `on_exceed` | `"fail"` \| `"review"` | `"fail"` | verdict when over threshold and no contract |
| `width` / `height` | int | baseline's | override the viewport (usually leave empty) |
| `wait_ms`, `wait_selector`, `stable` | as above | | applying to the current render |
| `steps` | list | `[]` | same deterministic script, run before the render is captured |

Output:

```json
{
  "status": "PASS | FAIL | REVIEW",
  "changed_pct": 2.607, "threshold_pct": 0.2,
  "changed_pixels": 14077, "total_pixels": 540000,
  "regions": [{"bbox": [20,120,160,44], "changed_pixels": 7040, "area": 7040, "density": 1.0}],
  "reasons": ["…"],
  "artifacts": {"diff": "…", "overlay": "…", "regions": ["…"]},
  "baseline": "…/baseline.png", "current": "…/current.png",
  "checked_at": "2026-11-09T14:02:11Z"
}
```

---

## Visual change contract

```json
{
  "expected_changes": [
    {"target": "#cta", "change": "move", "description": "move the pay button below the price"},
    {"target": [20, 120, 160, 44], "change": "recolor"}
  ],
  "protected_regions": ["#nav", "#price", [0, 0, 900, 60]],
  "max_changed_pct": 3.0
}
```

Evaluation order, all deterministic:

1. `changed_pct <= threshold` **and** no `expected_changes` → `PASS`
2. any region overlapping a protected region → `FAIL`
3. region overlapping an expected target → counted as expected
4. `change: "move"` additionally accepts the vacated hole (same footprint within 15%)
5. `changed_pct > max_changed_pct` → `FAIL`
6. regions left over that match nothing declared → `FAIL`
7. `expected_changes` declared but none observed → `REVIEW`
8. a selector in the contract that does not exist on the page → `REVIEW`

`change` is documentation for humans. Only `"move"` changes the geometry rule in step 4.

---

## Masks

Accepted entries, mixed freely:

- `[x, y, w, h]` with positive width and height required
- `"#selector"` / `".class"`: must match at least one visible element (zero-size matches are
  dropped by `getBoundingClientRect`, so a hidden element is an error, not a mask)

Behaviour:

- selector masks are applied **before** capture with `visibility: hidden` (layout preserved)
- all masks are applied to the diff
- total masked area may not exceed **50%** of the viewport
- the resolved rectangles appear in `capture.masks` so a human can audit exactly what was ignored

---

## Capture metadata

Written with every baseline and echoed by `visual_base`:

```json
{"url": "…", "viewport": [1280,720], "chrome": "Chrome/152.0.7977.64",
 "protocol": "1.3", "captured_at": "2026-11-09T14:01:58Z", "elapsed_s": 1.9,
 "wait_ms": 250, "wait_selector": "#cta", "fonts": "loaded", "stable": true,
 "stable_tries": 2, "bytes": 481233, "masks": {"selector": [], "coordinate": []},
 "mask_rects": [], "broken_images": []}
```

---

## Failure policy

| situation | behaviour |
|---|---|
| Chrome missing, with `url=` | error, no image, no verdict |
| selector never appears | error after 10s |
| rendering never stabilises | error naming the animation hint |
| mask matches nothing | error |
| mask covers more than half the viewport | error |
| required image broken | error |
| unsupported / corrupted PNG | `PngError` naming the reason |
| baseline missing on check | error telling you to call `visual_base` |
| contract selector missing | `REVIEW`, never `PASS` |
| `steps` click on a missing element | error naming the selector |
| `steps` wait that times out | error naming the selector |

Nothing in this list degrades into a pass.
