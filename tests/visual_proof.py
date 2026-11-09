#!/usr/bin/env python3
"""End-to-end tests: real Chrome, real pages, real pixels. No mocks.

Covers the contract feature (expected change, protected region, missing target), selector masks,
three-state verdicts, capture waits, and the failure modes that must stay loud.
"""

from __future__ import annotations

import importlib.util
import os
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

os.environ["HERMES_HOME"] = tempfile.mkdtemp(prefix="vo-e2e-")
os.environ["HERMES_SESSION_ID"] = "e2e"

spec = importlib.util.spec_from_file_location("visual_oracle", HERE.parent / "__init__.py")
vo = importlib.util.module_from_spec(spec)
sys.modules["visual_oracle"] = vo
spec.loader.exec_module(vo)

CHECKS: list[tuple[str, bool, str]] = []


def ok(name: str, condition: bool, detail: str = "") -> None:
    CHECKS.append((name, bool(condition), detail))


SHELL = ("body{margin:0;background:#fff;font:20px sans-serif}"
         "#nav{position:absolute;left:0;top:0;width:600px;height:60px;background:#eef2f0}"
         "#cta{position:absolute;left:20px;top:%d;width:120px;height:40px;background:#2f7d4f}"
         "#note{position:absolute;left:20px;top:200px}")


def page(top: int, nav_colour: str = "#eef2f0", note: str = "checkout") -> str:
    return ("data:text/html," + "<style>" + (SHELL % top).replace("#eef2f0", nav_colour, 1)
            + "</style><div id='nav'></div><button id='cta'>BUY</button>"
            + f"<div id='note'>{note}</div>").replace("#", "%23")


BASE = page(120)
MOVED = page(260)
NAV_BROKEN = page(120, nav_colour="#8b1a1a")

CONTRACT = {"expected_changes": [{"target": "#cta", "change": "move"}],
            "protected_regions": ["#nav"],
            "max_changed_pct": 3.0}


def main() -> int:
    base = vo.visual_base("checkout", url=BASE, width=800, height=600, wait_ms=200,
                          wait_selector="#cta")
    ok("baseline captured with fonts + selector wait", base.get("ok") is True,
       (base.get("capture") or {}).get("chrome", base.get("error", "")))

    same = vo.visual_check("checkout", url=BASE)
    ok("unchanged page passes", same.get("status") == "PASS",
       f"{same.get('changed_pct')}%")

    moved = vo.visual_check("checkout", url=MOVED)
    ok("moved button fails without a contract", moved.get("status") == "FAIL",
       f"{moved.get('changed_pct')}% regions={len(moved.get('regions') or [])}")

    declared = vo.visual_check("checkout", url=MOVED, contract=CONTRACT)
    ok("declared change passes the contract", declared.get("status") == "PASS",
       "; ".join(declared.get("reasons") or []))

    unmoved = vo.visual_check("checkout", url=BASE, contract=CONTRACT)
    ok("contract expecting a change that did not happen asks for review",
       unmoved.get("status") == "REVIEW", "; ".join(unmoved.get("reasons") or []))

    nav = vo.visual_check("checkout", url=NAV_BROKEN, contract=CONTRACT)
    ok("protected region change fails", nav.get("status") == "FAIL",
       "; ".join(nav.get("reasons") or [])[:70])

    missing = vo.visual_check("checkout", url=MOVED,
                              contract={"expected_changes": [{"target": "#nope"}]})
    ok("missing contract target yields review, not a pass", missing.get("status") == "REVIEW",
       "; ".join(missing.get("reasons") or [])[:60])

    masked = vo.visual_check("checkout", url=NAV_BROKEN, mask=["#nav"])
    ok("selector mask hides the changing element", masked.get("status") == "PASS",
       f"{masked.get('changed_pct')}%")

    huge = vo.visual_check("checkout", url=MOVED, mask=[[0, 0, 4000, 4000]])
    ok("mask over half the viewport is refused", huge.get("ok") is False,
       huge.get("error", "")[:60])

    ghost = vo.visual_check("checkout", url=MOVED, mask=["#does-not-exist"])
    ok("mask selector that matches nothing is an error", ghost.get("ok") is False,
       ghost.get("error", "")[:70])

    broken_image = vo.visual_base("broken-img", url=page(120, note="<img src='nope.png'>"),
                                  require_images=True)
    ok("required images that fail to load are an error", broken_image.get("ok") is False,
       broken_image.get("error", "")[:60])

    animated = ("data:text/html," + ("<style>@keyframes s{to{transform:translateX(200px)}}"
                "#m{width:40px;height:40px;background:%23c00;animation:s .35s infinite alternate}"
                "</style><div id='m'></div>").replace("#", "%23"))
    unstable = vo.visual_base("animated", url=animated, wait_ms=100)
    ok("never-stable rendering is an error, not a guess", unstable.get("ok") is False,
       unstable.get("error", "")[:70])

    ok("artefacts written for a failing check",
       bool((moved.get("artifacts") or {}).get("diff")) and bool((moved.get("artifacts") or {}).get("overlay")),
       str(sorted((moved.get("artifacts") or {}).keys())))

    report_path = Path(moved["baseline"]).parent / "last" / "report.json"
    ok("machine-readable report on disk", report_path.is_file(),
       f"{report_path.stat().st_size} bytes" if report_path.is_file() else "missing")

    vo.visual_check("checkout", url=MOVED)   # leave a FAIL as the last verdict
    hook = vo.on_pre_verify()
    ok("hook keeps the turn open on FAIL/REVIEW", isinstance(hook, dict)
       and hook.get("action") == "continue", (hook or {}).get("message", "None")[:60])

    listing = vo.visual_list()
    ok("listing shows cases", listing.get("count", 0) >= 2,
       str([(c["name"], c["last_status"]) for c in listing.get("cases", [])]))

    passed = sum(1 for _, good, _ in CHECKS if good)
    print()
    for name, good, detail in CHECKS:
        print(f"  [{'PASS' if good else 'FAIL'}] {name}" + (f"  ({detail})" if detail else ""))
    print(f"\n{passed}/{len(CHECKS)} checks passed")
    return 0 if passed == len(CHECKS) else 1


if __name__ == "__main__":
    sys.exit(main())
