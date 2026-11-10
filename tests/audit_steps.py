#!/usr/bin/env python3
"""Proves why state capture matters.

Two pages render identically until you tap the menu button. Without `steps` the broken one passes,
with `steps` the bug is caught. That is the exact gap between "the page matches" and "the state the
user sees matches".
"""

from __future__ import annotations

import importlib.util
import os
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
os.environ["HERMES_HOME"] = tempfile.mkdtemp(prefix="vo-steps-")
os.environ["HERMES_SESSION_ID"] = "steps"

spec = importlib.util.spec_from_file_location("visual_oracle", HERE.parent / "__init__.py")
vo = importlib.util.module_from_spec(spec)
sys.modules["visual_oracle"] = vo
spec.loader.exec_module(vo)

CHECKS: list[tuple[str, bool, str]] = []


def ok(name: str, condition: bool, detail: str = "") -> None:
    CHECKS.append((name, bool(condition), detail))


def page(menu_css: str) -> str:
    css = ("body{margin:0;background:#fff;font:16px system-ui}"
           "#cta{position:absolute;left:40px;top:250px;width:200px;height:52px;background:#2f6b4a}"
           "#menu-btn{position:absolute;right:24px;top:16px;width:44px;height:44px}"
           "#menu{position:absolute;right:24px;top:72px;opacity:0;pointer-events:none}"
           "#menu.open{opacity:1}"
           + menu_css)
    body = ("<button id='menu-btn'>MENU</button>"
            "<div id='menu'><div class='item'>Home</div><div class='item'>Orders</div></div>"
            "<button id='cta'>PAY</button>"
            "<script>document.getElementById('menu-btn').onclick=()=>"
            "document.getElementById('menu').classList.add('open');</script>")
    return ("data:text/html,<style>" + css + "</style>" + body).replace("#", "%23")


# correct: the open menu sits in the top right corner, clear of the pay button
GOOD = page(".item{padding:6px 10px;background:#eef2f0;margin:4px}")
# broken: the open menu grows and covers the pay button
BROKEN = page(".item{padding:6px 10px;background:#eef2f0;margin:4px}"
              "#menu.open{left:0;right:auto;top:72px;width:600px;height:340px;"
              "background:#1b2320;opacity:1}")

STEPS = [{"click": "#menu-btn"}, {"wait_selector": "#menu.open"}, {"settle_ms": 150}]


def main() -> int:
    flat_base = vo.visual_base("menu", url=GOOD, width=800, height=520, wait_ms=120)
    ok("closed-state baseline captured", flat_base.get("ok") is True, flat_base.get("error", ""))

    flat = vo.visual_check("menu", url=BROKEN)
    ok("without steps the broken menu passes (the gap)", flat.get("status") == "PASS",
       f"{flat.get('changed_pct')}% changed")

    open_base = vo.visual_base("menu-open", url=GOOD, width=800, height=520, wait_ms=120,
                               steps=STEPS)
    ok("open-state baseline captured with steps", open_base.get("ok") is True,
       " then ".join((open_base.get("capture") or {}).get("steps") or []))

    opened = vo.visual_check("menu-open", url=BROKEN, steps=STEPS)
    ok("with steps the same page fails", opened.get("status") == "FAIL",
       f"{opened.get('changed_pct')}% changed, regions={len(opened.get('regions') or [])}")

    regions = opened.get("regions") or []
    covers_cta = any(r["bbox"][1] < 320 and r["bbox"][0] < 260 for r in regions)
    ok("the failing region sits over the pay button", covers_cta,
       str([r["bbox"] for r in regions[:3]]))

    same_state = vo.visual_check("menu-open", url=GOOD, steps=STEPS)
    ok("the same state twice passes", same_state.get("status") == "PASS",
       f"{same_state.get('changed_pct')}% changed")

    bad_step = vo.visual_base("bad-step", url=GOOD, steps=[{"click": "#nope"}])
    ok("a click on a missing selector is an error, not a silent skip",
       bad_step.get("ok") is False and "nothing to click" in str(bad_step.get("error")),
       str(bad_step.get("error"))[:60])

    bad_wait = vo.visual_base("bad-wait", url=GOOD, steps=[{"wait_selector": "#never"}])
    ok("a wait on a selector that never appears is an error",
       bad_wait.get("ok") is False, str(bad_wait.get("error"))[:50])

    malformed = vo.visual_base("bad-shape", url=GOOD, steps=["click #cta"])
    ok("a malformed step is rejected", malformed.get("ok") is False,
       str(malformed.get("error"))[:60])

    passed = sum(1 for _, good, _ in CHECKS if good)
    print()
    for name, good, detail in CHECKS:
        print(f"  [{'PASS' if good else 'FAIL'}] {name}" + (f"  ({detail})" if detail else ""))
    print(f"\n{passed}/{len(CHECKS)} state-capture checks passed")
    return 0 if passed == len(CHECKS) else 1


if __name__ == "__main__":
    sys.exit(main())
