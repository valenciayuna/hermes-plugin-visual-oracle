"""visual-oracle — a UI claim has to survive a screenshot, and the evidence has to be inspectable.

The failure it exists for: an agent edits layout, the page still renders, nothing throws, and the
agent reports "looks right" without ever looking. Missing buttons, a panel covering a menu, a cut
sprite and a lost z-index are all invisible to tests and to the model's opinion of its own work.

    visual_base    capture the baseline (url or an image you already have)
    visual_check   render now, diff, and answer PASS / FAIL / REVIEW with regions and artefacts
    visual_list    every baseline in this session with its last verdict
    pre_verify     keeps the turn open while a check is failing

Three states, because a visual difference is evidence of a change, not proof of a defect:

    PASS    within policy: under the threshold, or exactly the change the contract declared
    FAIL    outside policy: protected region touched, unexpected region over threshold, or over max
    REVIEW  the policy cannot be evaluated reliably, or the change needs a human say-so

The baseline is never updated automatically. A check that cannot run is an error, not a pass.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import time
from pathlib import Path

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

import pngdiff  # noqa: E402
from capture import capture, resolve_masks  # noqa: E402
from cdp import Browser, CdpError  # noqa: E402

DEFAULT_THRESHOLD = 0.2
_store_dir_override: Path | None = None


# --------------------------------------------------------------------------- storage


def data_dir() -> Path:
    if _store_dir_override is not None:
        path = Path(_store_dir_override)
    else:
        home = os.environ.get("HERMES_HOME") or str(Path.home() / ".hermes")
        path = Path(home) / "plugin-data" / "visual-oracle"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _session_dir() -> Path:
    session = os.environ.get("HERMES_SESSION_ID") or "local"
    safe = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in session)[:60]
    path = data_dir() / safe
    path.mkdir(parents=True, exist_ok=True)
    return path


def _case_dir(name: str) -> Path:
    safe = "".join(ch if ch.isalnum() or ch in "-_." else "_" for ch in str(name))[:60] or "case"
    path = _session_dir() / safe
    path.mkdir(parents=True, exist_ok=True)
    return path


def set_store_dir(path) -> None:
    global _store_dir_override
    _store_dir_override = Path(path) if path else None


def _read_meta(case: Path) -> dict:
    path = case / "meta.json"
    if not path.is_file():
        return {}
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return {}


def _write_meta(case: Path, meta: dict) -> None:
    (case / "meta.json").write_text(json.dumps(meta, indent=1))


# --------------------------------------------------------------------------- contract helpers

def _contract_from(value) -> dict:
    """Accept a dict, or a JSON/YAML-free path to a .json file."""
    if not value:
        return {}
    if isinstance(value, dict):
        return value
    path = Path(os.path.expanduser(str(value)))
    if not path.is_file():
        raise ValueError(f"contract file not found: {path}")
    try:
        return json.loads(path.read_text())
    except ValueError as exc:
        raise ValueError(f"contract is not valid JSON: {exc}") from None


def _overlaps(a: list[int], b: list[int]) -> bool:
    ax, ay, aw, ah = a
    bx, by, bw, bh = b
    return not (ax + aw <= bx or bx + bw <= ax or ay + ah <= by or by + bh <= ay)


def _same_footprint(a: list[int], b: list[int], slack: float = 0.15) -> bool:
    """True when two boxes are the same size within slack — the hole a moved element leaves."""
    aw, ah = a[2], a[3]
    bw, bh = b[2], b[3]
    if not bw or not bh:
        return False
    return abs(aw - bw) <= bw * slack and abs(ah - bh) <= bh * slack


def _inside(inner: list[int], outer: list[int]) -> bool:
    ix, iy, iw, ih = inner
    ox, oy, ow, oh = outer
    return ix >= ox and iy >= oy and ix + iw <= ox + ow and iy + ih <= oy + oh


# --------------------------------------------------------------------------- tools


def visual_base(name: str, url: str = "", image: str = "", width: int = 1280,
                height: int = 720, wait_ms: int = 250, wait_selector: str = "",
                stable: bool = True, mask: list | None = None,
                require_images: bool = False) -> dict:
    """Capture (or accept) a screenshot and store it as the baseline for `name`."""
    case = _case_dir(name)
    dest = case / "baseline.png"
    try:
        if image:
            src = Path(os.path.expanduser(image))
            if not src.is_file():
                raise CdpError(f"image not found: {src}")
            if dest.exists():
                dest.unlink()
            shutil.copyfile(src, dest)
            info = {"source": "image", "from": str(src), "bytes": dest.stat().st_size}
        else:
            if not url:
                raise CdpError("need either url=... or image=...")
            info = capture(url, dest, width=width, height=height, wait_ms=wait_ms,
                           wait_selector=wait_selector, stable=stable, masks=mask,
                           require_images=require_images)
            info["source"] = "chrome"
    except (CdpError, OSError, ValueError) as exc:
        return {"ok": False, "error": str(exc)}

    _write_meta(case, {"name": name, "created": time.time(), "capture": info,
                       "mask": mask or [], "width": width, "height": height})
    return {"ok": True, "name": name, "baseline": str(dest), "bytes": dest.stat().st_size,
            "capture": info}


def visual_check(name: str, url: str = "", image: str = "", threshold: float = DEFAULT_THRESHOLD,
                 mask: list | None = None, contract=None, width: int = 0, height: int = 0,
                 wait_ms: int = 250, wait_selector: str = "", stable: bool = True,
                 on_exceed: str = "fail") -> dict:
    """Render now, diff against the baseline, and decide PASS / FAIL / REVIEW."""
    case = _case_dir(name)
    base = case / "baseline.png"
    if not base.is_file():
        return {"ok": False, "error": f"no baseline named '{name}'; call visual_base first"}

    meta = _read_meta(case)
    stored = (meta.get("capture") or {}).get("viewport") or [1280, 720]
    width = width or int(stored[0])
    height = height or int(stored[1])
    current = case / "current.png"
    artifacts_dir = case / "last"

    try:
        policy = _contract_from(contract)
        # resolve every selector the contract names, so a missing one is an error not a pass
        probes: list[str] = []
        for item in policy.get("expected_changes", []) or []:
            target = item.get("target") if isinstance(item, dict) else item
            if isinstance(target, str):
                probes.append(target)
        for item in policy.get("protected_regions", []) or []:
            if isinstance(item, str):
                probes.append(item)

        if image:
            src = Path(os.path.expanduser(image))
            if not src.is_file():
                raise CdpError(f"image not found: {src}")
            current.write_bytes(src.read_bytes())
            capture_info = {"source": "image", "from": str(src)}
            probe_rects: dict[str, list] = {}
        else:
            if not url:
                raise CdpError("need either url=... or image=...")
            capture_info = capture(url, current, width=width, height=height, wait_ms=wait_ms,
                                   wait_selector=wait_selector, stable=stable, masks=mask)
            capture_info["source"] = "chrome"
            probe_rects = {}
            if probes:
                with Browser(width=width, height=height) as browser:
                    browser.navigate(url, wait_ms=wait_ms)
                    browser.wait_fonts()
                    for selector in probes:
                        probe_rects[selector] = browser.rects(selector)

        masks = list(capture_info.get("mask_rects") or [])

        result = pngdiff.compare(str(base), str(current), masks=masks,
                                 artefacts={"dir": str(artifacts_dir)})
    except (CdpError, OSError, ValueError, pngdiff.PngError) as exc:
        return {"ok": False, "error": str(exc)}

    changed_pct = float(result.get("changed_pct", 100.0))
    regions = result.get("regions", [])
    verdict, reasons = _decide(changed_pct, threshold, regions, policy, probe_rects, on_exceed)

    report = {
        "ok": True,
        "name": name,
        "status": verdict,
        "changed_pct": changed_pct,
        "threshold_pct": threshold,
        "changed_pixels": result.get("changed_pixels", 0),
        "total_pixels": result.get("total_pixels", 0),
        "regions": regions,
        "bbox": regions[0]["bbox"] if regions else result.get("bbox"),
        "reasons": reasons,
        "contract": policy or None,
        "source": capture_info,
        "artifacts": result.get("artifacts", {}),
        "baseline": str(base),
        "current": str(current),
        "checked_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }

    artifacts_dir.mkdir(parents=True, exist_ok=True)
    (artifacts_dir / "report.json").write_text(json.dumps(report, indent=1))
    meta["last"] = {k: report[k] for k in
                    ("name", "status", "changed_pct", "threshold_pct", "regions", "reasons",
                     "artifacts", "baseline", "current", "checked_at")}
    _write_meta(case, meta)
    return report


def _decide(changed_pct: float, threshold: float, regions: list[dict], policy: dict,
            probe_rects: dict, on_exceed: str) -> tuple[str, list[str]]:
    """Deterministic verdict. No model, no taste: counts, boxes and the declared contract."""
    reasons: list[str] = []
    expected_declared = bool(policy.get("expected_changes")) if policy else False
    if changed_pct <= threshold and not expected_declared:
        return "PASS", [f"{changed_pct}% changed, within the {threshold}% threshold"]

    if not policy:
        verdict = "REVIEW" if str(on_exceed).lower() == "review" else "FAIL"
        box = regions[0]["bbox"] if regions else None
        return verdict, [f"{changed_pct}% changed, over the {threshold}% threshold"
                         + (f", largest region at {box}" if box else "")]

    expected_boxes: list[list[int]] = []
    move_boxes: list[list[int]] = []
    for item in policy.get("expected_changes", []) or []:
        target = item.get("target") if isinstance(item, dict) else item
        change = str(item.get("change", "")).lower() if isinstance(item, dict) else ""
        if isinstance(target, str):
            rects = probe_rects.get(target) or []
            if not rects:
                return "REVIEW", [f"contract target not found on the page: {target}"]
            expected_boxes.extend(rects)
            if change == "move":
                move_boxes.extend(rects)
        elif isinstance(target, list) and len(target) == 4:
            box = [int(v) for v in target]
            expected_boxes.append(box)
            if change == "move":
                move_boxes.append(box)

    protected_boxes: list[list[int]] = []
    for item in policy.get("protected_regions", []) or []:
        if isinstance(item, str):
            rects = probe_rects.get(item) or []
            if not rects:
                return "REVIEW", [f"protected region not found on the page: {item}"]
            protected_boxes.extend(rects)
        elif isinstance(item, list) and len(item) == 4:
            protected_boxes.append([int(v) for v in item])

    unexpected: list[list[int]] = []
    expected_hits = 0
    for region in regions:
        box = region["bbox"]
        if any(_overlaps(box, p) for p in protected_boxes):
            return "FAIL", [f"protected region changed at {box}"]
        if any(_inside(box, e) or _overlaps(box, e) for e in expected_boxes):
            expected_hits += 1
        elif any(_same_footprint(box, m) for m in move_boxes):
            # a moved element leaves a hole the size of itself; that hole is part of the move
            expected_hits += 1
        else:
            unexpected.append(box)

    max_pct = float(policy.get("max_changed_pct", threshold))
    if changed_pct > max_pct:
        return "FAIL", [f"{changed_pct}% changed, over the contract limit of {max_pct}%"]
    if unexpected:
        return "FAIL", [f"{len(unexpected)} region(s) outside the contract: "
                        + ", ".join(str(b) for b in unexpected[:3])]
    if expected_hits:
        reasons.append(f"{expected_hits} expected change(s) confirmed inside the declared targets")
    if policy.get("expected_changes") and not expected_hits:
        return "REVIEW", ["contract expected a change that was not observed"]
    return "PASS", reasons or [f"{changed_pct}% changed, inside the declared contract"]


def visual_list() -> dict:
    """Every baseline in this session, with its last verdict."""
    rows = []
    for case in sorted(p for p in _session_dir().iterdir() if p.is_dir()):
        meta = _read_meta(case)
        last = meta.get("last") or {}
        rows.append({
            "name": meta.get("name", case.name),
            "baseline": (case / "baseline.png").is_file(),
            "last_status": last.get("status", "never checked"),
            "changed_pct": last.get("changed_pct"),
            "regions": len(last.get("regions") or []),
        })
    return {"ok": True, "cases": rows, "count": len(rows)}


# --------------------------------------------------------------------------- hook


def on_pre_verify(**_):
    """A FAIL or REVIEW keeps the turn open; the agent gets the numbers and the image paths."""
    for case in sorted(p for p in _session_dir().iterdir() if p.is_dir()):
        last = (_read_meta(case) or {}).get("last") or {}
        if last.get("status") in ("FAIL", "REVIEW"):
            regions = last.get("regions") or []
            where = ", ".join(str(r["bbox"]) for r in regions[:3])
            artifacts = last.get("artifacts") or {}
            return {
                "action": "continue",
                "message": (
                    f"[visual-oracle] {last['status']} on '{last['name']}': "
                    f"{last['changed_pct']}% changed (allowed {last['threshold_pct']}%)\n"
                    + (f"regions: {where}\n" if where else "")
                    + (f"diff: {artifacts.get('diff')}\n" if artifacts.get("diff") else "")
                    + f"baseline: {last['baseline']}\ncurrent : {last['current']}\n"
                    "Look at the diff. Fix the layout, or state plainly what changed and why."
                ),
            }
    return None


def capabilities() -> dict:
    return {"tools": ["visual_base", "visual_check", "visual_list"], "hooks": ["pre_verify"]}


# --------------------------------------------------------------------------- registration

_BASE_SCHEMA = {
    "name": "visual_base",
    "description": (
        "Capture a baseline screenshot of a page (or accept an existing PNG) under a name. Waits "
        "for fonts, an optional selector, and two identical frames before accepting the image."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "Case name, e.g. 'checkout-mobile'."},
            "url": {"type": "string", "description": "Page to render."},
            "image": {"type": "string", "description": "Existing PNG instead of rendering."},
            "width": {"type": "integer", "description": "Viewport width (default 1280)."},
            "height": {"type": "integer", "description": "Viewport height (default 720)."},
            "wait_ms": {"type": "integer", "description": "Extra settle time after load."},
            "wait_selector": {"type": "string", "description": "Block until this selector exists."},
            "stable": {"type": "boolean", "description": "Require two identical frames (default true)."},
            "mask": {"type": "array", "description": "Regions [x,y,w,h] or CSS selectors to ignore.",
                     "items": {}},
            "require_images": {"type": "boolean", "description": "Fail if any <img> is broken."},
        },
        "required": ["name"],
    },
}

_CHECK_SCHEMA = {
    "name": "visual_check",
    "description": (
        "Render the page now and pixel-diff it against the baseline. Returns PASS, FAIL or REVIEW, "
        "independent change regions with density, and diff/overlay/region artefacts. An optional "
        "contract declares expected changes and protected regions so a change can match intent."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "Case name created by visual_base."},
            "url": {"type": "string", "description": "Page to render now."},
            "image": {"type": "string", "description": "New screenshot PNG to compare instead."},
            "threshold": {"type": "number", "description": "Allowed percent changed (default 0.2)."},
            "mask": {"type": "array", "description": "Regions [x,y,w,h] or selectors to ignore.",
                     "items": {}},
            "contract": {"type": "object", "description":
                         '{"expected_changes":[{"target":".btn","change":"move"}],'
                         '"protected_regions":["#nav"],"max_changed_pct":1.0}'},
            "on_exceed": {"type": "string", "description": "No contract: 'fail' (default) or 'review'."},
            "width": {"type": "integer", "description": "Override viewport width."},
            "height": {"type": "integer", "description": "Override viewport height."},
            "wait_selector": {"type": "string", "description": "Block until this selector exists."},
        },
        "required": ["name"],
    },
}

_LIST_SCHEMA = {
    "name": "visual_list",
    "description": "List every baseline in this session with its last verdict and region count.",
    "parameters": {"type": "object", "properties": {}},
}


def _base_handler(args: dict, **_) -> str:
    out = visual_base(str(args.get("name") or ""), str(args.get("url") or ""),
                      str(args.get("image") or ""), int(args.get("width") or 1280),
                      int(args.get("height") or 720), int(args.get("wait_ms") or 250),
                      str(args.get("wait_selector") or ""), bool(args.get("stable", True)),
                      args.get("mask"), bool(args.get("require_images", False)))
    if not out.get("ok"):
        return f"visual_base failed: {out.get('error')}"
    cap = out.get("capture") or {}
    return (f"baseline '{out['name']}' stored ({out['bytes']} bytes)\n"
            f"viewport {cap.get('viewport')} · chrome {cap.get('chrome', '?')} · "
            f"fonts {cap.get('fonts', '?')} · stable after {cap.get('stable_tries', 1)} capture(s)")


def _check_handler(args: dict, **_) -> str:
    out = visual_check(str(args.get("name") or ""), str(args.get("url") or ""),
                       str(args.get("image") or ""),
                       float(args.get("threshold") or DEFAULT_THRESHOLD),
                       args.get("mask"), args.get("contract"), int(args.get("width") or 0),
                       int(args.get("height") or 0), int(args.get("wait_ms") or 250),
                       str(args.get("wait_selector") or ""), bool(args.get("stable", True)),
                       str(args.get("on_exceed") or "fail"))
    if not out.get("ok"):
        return f"visual_check failed: {out.get('error')}"
    lines = [f"{out['status']} {out['name']}: {out['changed_pct']}% changed "
             f"(allowed {out['threshold_pct']}%)"]
    for region in out.get("regions", [])[:5]:
        lines.append(f"  region {region['bbox']} {region['changed_pixels']}px "
                     f"density {region['density']}")
    for reason in out.get("reasons", []):
        lines.append(f"  · {reason}")
    artifacts = out.get("artifacts") or {}
    if artifacts.get("diff"):
        lines.append(f"  diff: {artifacts['diff']}")
    if artifacts.get("overlay"):
        lines.append(f"  overlay: {artifacts['overlay']}")
    lines.append(f"  report: {Path(out['baseline']).parent / 'last' / 'report.json'}")
    return "\n".join(lines)


def _list_handler(args: dict, **_) -> str:
    out = visual_list()
    if not out.get("cases"):
        return "no baselines in this session"
    return "\n".join(f"{row['name']:<20} {row['last_status']:<16} "
                     f"{row['changed_pct'] if row['changed_pct'] is not None else '-'}% "
                     f"{row['regions']} region(s)" for row in out["cases"])


def register(ctx) -> None:
    global _store_dir_override
    try:
        candidate = getattr(ctx, "plugin_data_dir", None)
        if callable(candidate):
            _store_dir_override = Path(candidate())
        elif isinstance(candidate, (str, Path)):
            _store_dir_override = Path(candidate)
    except Exception:
        _store_dir_override = None

    ctx.register_hook("pre_verify", on_pre_verify)

    for schema, handler, description in (
        (_BASE_SCHEMA, _base_handler, "Store a baseline screenshot."),
        (_CHECK_SCHEMA, _check_handler, "Pixel-diff the page against its baseline."),
        (_LIST_SCHEMA, _list_handler, "List baselines and verdicts."),
    ):
        ctx.register_tool(name=schema["name"], toolset="visual-oracle", schema=schema,
                          handler=handler, description=description)
