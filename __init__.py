"""visual-oracle — every claim that the UI is fine has to survive a screenshot.

The failure it exists for: an agent edits CSS or layout, the page still renders, nothing throws, and
the agent reports "looks right" without ever looking. Missing buttons, panels covering menus, cut
sprites and z-index slips are all invisible to tests and to the model's own opinion.

    visual_base(name, url=...)      capture the baseline once
    visual_check(name, url=...)     capture again, pixel-diff against the baseline
    pre_verify hook                 refuses to finish while the last check is failing

The diff is a real measurement: two PNGs decoded and compared pixel by pixel in this process, with a
bounding box for what moved. It says nothing about taste and never claims a UI is good, only whether
it changed and where.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import time
import uuid
from pathlib import Path

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

import pngdiff  # noqa: E402
from capture import capture  # noqa: E402

DEFAULT_THRESHOLD = 0.2  # percent of pixels allowed to change before it is a failure
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


def _meta_path(case: Path) -> Path:
    return case / "meta.json"


def _read_meta(case: Path) -> dict:
    path = _meta_path(case)
    if not path.is_file():
        return {}
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return {}


def _write_meta(case: Path, meta: dict) -> None:
    _meta_path(case).write_text(json.dumps(meta, indent=1))


def _grab(url: str | None, image: str | None, dest: Path, width: int, height: int,
          wait_ms: int) -> dict:
    """Either capture the url, or copy a screenshot the caller already took."""
    if image:
        src = Path(os.path.expanduser(image))
        if not src.is_file():
            raise RuntimeError(f"image not found: {src}")
        shutil.copyfile(src, dest)
        return {"source": "image", "from": str(src)}
    if not url:
        raise RuntimeError("need either url=... or image=...")
    capture(url, dest, width=width, height=height, wait_ms=wait_ms)
    return {"source": "chrome", "url": url, "viewport": [width, height]}


# --------------------------------------------------------------------------- tools


def visual_base(name: str, url: str = "", image: str = "", width: int = 1280,
                height: int = 720, wait_ms: int = 1500, mask: list | None = None) -> dict:
    """Capture (or accept) a screenshot and store it as the baseline for `name`.

    mask: [[x, y, w, h], ...] regions to ignore in later comparisons (clocks, cursors, animation).
    """
    case = _case_dir(name)
    dest = case / "baseline.png"
    try:
        how = _grab(url or None, image or None, dest, width, height, wait_ms)
    except (RuntimeError, OSError, ValueError) as exc:
        return {"ok": False, "error": str(exc)}
    _write_meta(case, {
        "name": name,
        "created": time.time(),
        "how": how,
        "mask": mask or [],
        "baseline_bytes": dest.stat().st_size,
    })
    return {"ok": True, "name": name, "baseline": str(dest),
            "bytes": dest.stat().st_size, "how": how}


def visual_check(name: str, url: str = "", image: str = "", threshold: float = DEFAULT_THRESHOLD,
                 mask: list | None = None, width: int = 0, height: int = 0) -> dict:
    """Capture now and diff against the baseline. PASS only when the pixels agree within threshold.

    The viewport defaults to the one the baseline was captured with: comparing a 800x600 baseline
    against a 1280x720 render would report "size changed" every time, which is noise, not a finding.
    """
    case = _case_dir(name)
    base = case / "baseline.png"
    if not base.is_file():
        return {"ok": False, "error": f"no baseline named '{name}'; call visual_base first"}

    meta = _read_meta(case)
    stored = (meta.get("how") or {}).get("viewport") or [1280, 720]
    width = width or int(stored[0])
    height = height or int(stored[1])
    current = case / "current.png"
    try:
        how = _grab(url or None, image or None, current, width, height, 1500)
        result = pngdiff.compare(str(base), str(current), masks=mask or meta.get("mask") or [])
    except (RuntimeError, OSError, ValueError) as exc:
        return {"ok": False, "error": str(exc)}

    changed = result.get("changed_pct", 100.0)
    verdict = "PASS" if result.get("status") == "same" or changed <= threshold else "FAIL"
    payload = {
        "ok": True,
        "name": name,
        "status": verdict,
        "changed_pct": changed,
        "threshold_pct": threshold,
        "changed_pixels": result.get("changed_pixels", 0),
        "total_pixels": result.get("total_pixels", 0),
        "bbox": result.get("bbox"),
        "reason": result.get("reason", ""),
        "baseline": str(base),
        "current": str(current),
        "how": how,
        "checked": time.time(),
    }
    meta["last"] = payload
    _write_meta(case, meta)
    return payload


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
            "bbox": last.get("bbox"),
        })
    return {"ok": True, "cases": rows, "count": len(rows)}


# --------------------------------------------------------------------------- hook


def on_pre_verify(**_):
    """If the last visual check failed, the turn does not get to call itself done."""
    worst = None
    for case in sorted(p for p in _session_dir().iterdir() if p.is_dir()):
        last = (_read_meta(case) or {}).get("last") or {}
        if last.get("status") == "FAIL":
            worst = last
            break
    if not worst:
        return None

    where = f" bbox {worst['bbox']}" if worst.get("bbox") else ""
    return {
        "action": "continue",
        "message": (
            f"[visual-oracle] screenshot '{worst['name']}' changed by {worst['changed_pct']}% "
            f"(allowed {worst['threshold_pct']}%){where}.\n"
            f"baseline: {worst['baseline']}\ncurrent : {worst['current']}\n"
            "Look at both images. Either fix the layout or say plainly what changed and why."
        ),
    }


def capabilities() -> dict:
    return {"tools": ["visual_base", "visual_check", "visual_list"], "hooks": ["pre_verify"]}


# --------------------------------------------------------------------------- registration

_BASE_SCHEMA = {
    "name": "visual_base",
    "description": (
        "Capture a baseline screenshot of a page (or accept an image file) under a name. Later "
        "renders are pixel-compared against it, so a claim that the UI is unchanged has evidence."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "Case name, e.g. 'login-mobile'."},
            "url": {"type": "string", "description": "Page to render headlessly."},
            "image": {"type": "string", "description": "Existing PNG to use instead of rendering."},
            "width": {"type": "integer", "description": "Viewport width (default 1280)."},
            "height": {"type": "integer", "description": "Viewport height (default 720)."},
            "wait_ms": {"type": "integer", "description": "Virtual time budget before capture."},
            "mask": {"type": "array", "items": {"type": "array", "items": {"type": "integer"}},
                     "description": "Regions [x,y,w,h] to ignore in comparisons."},
        },
        "required": ["name"],
    },
}

_CHECK_SCHEMA = {
    "name": "visual_check",
    "description": (
        "Render the page now and pixel-diff it against the stored baseline. Reports the percentage "
        "changed, the bounding box of what moved, and PASS or FAIL against the threshold. A failing "
        "check keeps the turn open until the layout is fixed or the change is explained."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "Case name created by visual_base."},
            "url": {"type": "string", "description": "Page to render now."},
            "image": {"type": "string", "description": "New screenshot PNG to compare instead."},
            "threshold": {"type": "number", "description": "Allowed percent changed (default 0.2)."},
            "mask": {"type": "array", "items": {"type": "array", "items": {"type": "integer"}},
                     "description": "Regions [x,y,w,h] to ignore for this check."},
            "width": {"type": "integer", "description": "Override viewport width."},
            "height": {"type": "integer", "description": "Override viewport height."},
        },
        "required": ["name"],
    },
}

_LIST_SCHEMA = {
    "name": "visual_list",
    "description": "List every baseline in this session with its last verdict, change percent and bbox.",
    "parameters": {"type": "object", "properties": {}},
}


def _base_handler(args: dict, **_) -> str:
    out = visual_base(str(args.get("name") or ""), str(args.get("url") or ""),
                      str(args.get("image") or ""), int(args.get("width") or 1280),
                      int(args.get("height") or 720), int(args.get("wait_ms") or 1500),
                      args.get("mask"))
    if not out.get("ok"):
        return f"visual_base failed: {out.get('error')}"
    return f"baseline '{out['name']}' stored ({out['bytes']} bytes) at {out['baseline']}"


def _check_handler(args: dict, **_) -> str:
    out = visual_check(str(args.get("name") or ""), str(args.get("url") or ""),
                       str(args.get("image") or ""),
                       float(args.get("threshold") or DEFAULT_THRESHOLD),
                       args.get("mask"), int(args.get("width") or 0), int(args.get("height") or 0))
    if not out.get("ok"):
        return f"visual_check failed: {out.get('error')}"
    where = f" bbox={out['bbox']}" if out.get("bbox") else ""
    return (f"{out['status']} {out['name']}: {out['changed_pct']}% changed "
            f"(allowed {out['threshold_pct']}%){where}\n"
            f"baseline: {out['baseline']}\ncurrent : {out['current']}")


def _list_handler(args: dict, **_) -> str:
    out = visual_list()
    if not out.get("cases"):
        return "no baselines in this session"
    return "\n".join(
        f"{row['name']:<20} {row['last_status']:<14} "
        f"{row['changed_pct'] if row['changed_pct'] is not None else '-'}%"
        for row in out["cases"]
    )


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

    for schema, handler, description, emoji in (
        (_BASE_SCHEMA, _base_handler, "Store a baseline screenshot.", "🖼️"),
        (_CHECK_SCHEMA, _check_handler, "Pixel-diff the page against its baseline.", "🔍"),
        (_LIST_SCHEMA, _list_handler, "List baselines and verdicts.", "📋"),
    ):
        ctx.register_tool(name=schema["name"], toolset="visual-oracle", schema=schema,
                          handler=handler, description=description, emoji=emoji)
