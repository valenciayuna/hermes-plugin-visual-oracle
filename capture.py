"""Capture through Chrome's DevTools session, with the waiting made explicit.

Three things cause false positives in screenshot testing, and all three are handled here rather
than papered over:

1. webfonts swapping in after the first paint  -> wait for `document.fonts.ready`
2. elements that animate into place            -> optional wait for a selector, then stability check
3. images that never load                      -> report them (and fail loudly if asked to)

Selector masks are applied *before* the capture where possible: the element keeps its space but is
made invisible, so a clock or a carousel cannot add noise to the diff.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from cdp import Browser, CdpError

COVERAGE_LIMIT = 0.5   # refusing to mask more than half the viewport: that is not a mask, that is a blindfold


def _mask_css(selectors: list[str]) -> str:
    rules = ",\n".join(selectors)
    return f"{rules} {{ visibility: hidden !important; }}"


def resolve_masks(browser: Browser, masks: list | None, width: int, height: int) -> tuple[list[list[int]], dict]:
    """Turn a mix of [x, y, w, h] and CSS selectors into rectangles, and report what was ignored."""
    rects: list[list[int]] = []
    report: dict = {"coordinate": [], "selector": [], "hidden": [], "coverage_pct": 0.0}

    for item in masks or []:
        if isinstance(item, str):
            matched = browser.rects(item)
            if not matched:
                raise CdpError(f"mask selector matched no visible element: {item!r} "
                               "(a mask that silently matches nothing is how a check lies)")
            rects.extend(matched)
            report["selector"].append({"selector": item, "rects": matched})
            report["hidden"].append(item)
        elif isinstance(item, (list, tuple)) and len(item) == 4:
            rect = [int(v) for v in item]
            if rect[2] <= 0 or rect[3] <= 0:
                raise CdpError(f"mask rectangle must have positive size: {rect}")
            rects.append(rect)
            report["coordinate"].append(rect)
        else:
            raise CdpError(f"mask entry must be [x, y, w, h] or a CSS selector, got {item!r}")

    covered = sum(w * h for _x, _y, w, h in rects)
    total = width * height
    report["coverage_pct"] = round(covered / total * 100.0, 2) if total else 0.0
    if total and covered / total > COVERAGE_LIMIT:
        raise CdpError(
            f"masks cover {report['coverage_pct']}% of the viewport, over the "
            f"{int(COVERAGE_LIMIT * 100)}% limit; a check that ignores half the screen cannot fail")
    return rects, report


def split_masks(masks: list | None) -> tuple[list[list[int]], list[str]]:
    """Separate coordinate masks from selector masks, validating both."""
    coords: list[list[int]] = []
    selectors: list[str] = []
    for item in masks or []:
        if isinstance(item, str):
            selectors.append(item)
        elif isinstance(item, (list, tuple)) and len(item) == 4:
            rect = [int(v) for v in item]
            if rect[2] <= 0 or rect[3] <= 0:
                raise CdpError(f"mask rectangle must have positive size: {rect}")
            coords.append(rect)
        else:
            raise CdpError(f"mask entry must be [x, y, w, h] or a CSS selector, got {item!r}")
    return coords, selectors


def apply_steps(browser: Browser, steps: list | None, settle_ms: int = 120) -> list[str]:
    """Run a short, deterministic script before the screenshot: click, wait, scroll, settle.

    This is how a state that only exists after a tap gets a baseline. No framework, no retries
    with sleeps: each step either succeeds or raises with the selector that failed.
    """
    applied: list[str] = []
    for index, step in enumerate(steps or []):
        if not isinstance(step, dict):
            raise CdpError(f"step {index} must be an object with click/wait_selector/scroll/settle_ms")
        if "click" in step:
            selector = str(step["click"])
            clicked = browser.evaluate(
                "(function(sel){const el=document.querySelector(sel);"
                "if(!el) return 'missing'; el.click(); return 'ok';})(" + json.dumps(selector) + ")")
            if clicked != "ok":
                raise CdpError(f"step {index}: nothing to click for {selector!r}")
            applied.append(f"click {selector}")
        if "wait_selector" in step:
            selector = str(step["wait_selector"])
            browser.wait_selector(selector)
            applied.append(f"wait {selector}")
        if "scroll" in step:
            y = int(step["scroll"])
            browser.evaluate(f"window.scrollTo(0, {y}); true")
            applied.append(f"scroll {y}")
        if "settle_ms" in step:
            time.sleep(max(0, int(step["settle_ms"])) / 1000.0)
            applied.append(f"settle {int(step['settle_ms'])}ms")
    if settle_ms:
        time.sleep(settle_ms / 1000.0)
    return applied


def capture(url: str, out: Path, width: int = 1280, height: int = 720, wait_ms: int = 250,
            wait_selector: str = "", stable: bool = True, masks: list | None = None,
            require_images: bool = False, attempts: int = 4, steps: list | None = None) -> dict:
    """Render `url` and write a PNG. Returns capture metadata; raises CdpError on any doubt."""
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    started = time.time()

    with Browser(width=width, height=height) as browser:
        browser.navigate(url, wait_ms=wait_ms)
        fonts = browser.wait_fonts()

        if wait_selector:
            browser.wait_selector(wait_selector)

        applied = apply_steps(browser, steps)
        if wait_selector and steps:
            browser.wait_selector(wait_selector)

        rects, mask_report = resolve_masks(browser, masks, width, height)
        if mask_report["hidden"]:
            browser.evaluate(
                "(function(css){const s=document.createElement('style');s.textContent=css;"
                "document.head.appendChild(s);return true;})(" + json.dumps(_mask_css(mask_report["hidden"])) + ")")
            time.sleep(0.1)   # let the style land before the frame is taken

        broken = browser.broken_images()
        if broken and require_images:
            raise CdpError(f"{len(broken)} image(s) failed to load: {', '.join(broken[:5])}")

        if stable:
            png, tries = browser.screenshot_stable(attempts=attempts)
        else:
            png, tries = browser.screenshot(), 1
        out.write_bytes(png)

        info = {
            "url": url,
            "viewport": [width, height],
            "chrome": browser.info.get("chrome", ""),
            "protocol": browser.info.get("protocol", ""),
            "captured_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(started)),
            "elapsed_s": round(time.time() - started, 2),
            "wait_ms": wait_ms,
            "wait_selector": wait_selector,
            "fonts": fonts,
            "stable": stable,
            "stable_tries": tries,
            "bytes": len(png),
            "masks": mask_report,
            "mask_rects": rects,
            "broken_images": broken,
            "steps": applied,
        }
    return info
