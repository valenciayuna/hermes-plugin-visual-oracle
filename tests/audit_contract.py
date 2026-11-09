#!/usr/bin/env python3
"""Audit for the visual change contract and the mask paths.

Every case states the expected verdict. The interesting ones are the cases where a naive
implementation would answer PASS: a protected region changing by a few pixels, a contract whose
selector is gone, an image comparison with a mask.
"""

from __future__ import annotations

import importlib.util
import os
import shutil
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
os.environ["HERMES_HOME"] = tempfile.mkdtemp(prefix="vo-contract-")
os.environ["HERMES_SESSION_ID"] = "contract"

spec = importlib.util.spec_from_file_location("visual_oracle", HERE.parent / "__init__.py")
vo = importlib.util.module_from_spec(spec)
sys.modules["visual_oracle"] = vo
spec.loader.exec_module(vo)

CHECKS: list[tuple[str, bool, str]] = []
W, H = 700, 460


def ok(name: str, condition: bool, detail: str = "") -> None:
    CHECKS.append((name, bool(condition), detail))


def page(cta_top: int = 240, nav: str = "#eef2f0", dot: str = "#eef2f0") -> str:
    css = ("body{margin:0;background:#fff;font:16px sans-serif}"
           f"#nav{{position:absolute;inset:0 0 auto 0;height:70px;background:{nav}}}"
           f"#dot{{position:absolute;left:12px;top:20px;width:12px;height:12px;background:{dot}}}"
           f"#cta{{position:absolute;left:30px;top:{cta_top}px;width:150px;height:44px;background:#2f7d4f}}"
           "#price{position:absolute;left:30px;top:150px}")
    body = ("<div id='nav'><div id='dot'></div></div><div id='price'>Rp 120.000</div>"
            "<button id='cta'>PAY</button>")
    return ("data:text/html,<style>" + css + "</style>" + body).replace("#", "%23")


BASE = page()
MOVED = page(cta_top=320)
NAV_CHANGED = page(nav="#8b1a1a")
DOT_CHANGED = page(dot="#c00000")

MOVED_CONTRACT = {"expected_changes": [{"target": "#cta", "change": "move"}],
                  "protected_regions": ["#nav", "#price"],
                  "max_changed_pct": 6.0}


def main() -> int:
    base = vo.visual_base("checkout", url=BASE, width=W, height=H, wait_ms=120,
                          wait_selector="#cta")
    if not base.get("ok"):
        print("baseline failed:", base.get("error"))
        return 1

    same = vo.visual_check("checkout", url=BASE)
    ok("identical render is PASS", same["status"] == "PASS", f"{same['changed_pct']}%")

    plain = vo.visual_check("checkout", url=MOVED)
    ok("undeclared change over threshold is FAIL", plain["status"] == "FAIL",
       f"{plain['changed_pct']}%")

    soft = vo.visual_check("checkout", url=MOVED, on_exceed="review")
    ok("on_exceed=review turns the same change into REVIEW", soft["status"] == "REVIEW",
       soft["status"])

    declared = vo.visual_check("checkout", url=MOVED, contract=MOVED_CONTRACT)
    ok("declared move inside its target is PASS", declared["status"] == "PASS",
       "; ".join(declared["reasons"])[:70])

    nav = vo.visual_check("checkout", url=NAV_CHANGED, contract=MOVED_CONTRACT)
    ok("protected region change is FAIL", nav["status"] == "FAIL",
       "; ".join(nav["reasons"])[:70])

    # the case the audit found: 144 px changing inside a protected header is only 0.045% of the
    # page, so a threshold-first implementation would answer PASS
    dot = vo.visual_check("checkout", url=DOT_CHANGED, contract={"protected_regions": ["#nav"]})
    ok("protected region change under the threshold is still FAIL",
       dot["status"] == "FAIL" and dot["changed_pct"] < 0.2,
       f"{dot['changed_pct']}% -> {dot['status']}")

    missing = vo.visual_check("checkout", url=MOVED,
                              contract={"expected_changes": [{"target": "#gone"}]})
    ok("missing contract selector is REVIEW", missing["status"] == "REVIEW",
       "; ".join(missing["reasons"])[:60])

    not_observed = vo.visual_check("checkout", url=BASE,
                                   contract={"expected_changes": [{"target": "#cta"}]})
    ok("declared change that did not happen is REVIEW", not_observed["status"] == "REVIEW",
       "; ".join(not_observed["reasons"])[:60])

    unexpected = vo.visual_check("checkout", url=MOVED, contract={
        "expected_changes": [{"target": "#price"}], "max_changed_pct": 6.0})
    ok("region outside the declared targets is FAIL", unexpected["status"] == "FAIL",
       "; ".join(unexpected["reasons"])[:70])

    # masks on a supplied image: re-render the dot change so current.png really is that frame
    vo.visual_check("checkout", url=DOT_CHANGED)
    case = Path(base["baseline"]).parent
    same_size = "/tmp/audit-contract-current.png"
    shutil.copyfile(case / "current.png", same_size)
    masked = vo.visual_check("checkout", image=same_size, mask=[[0, 0, W, 100]])
    ok("coordinate mask works with image=", masked.get("ok") is True and masked["status"] == "PASS",
       f"{masked.get('status')} {masked.get('changed_pct')}%")

    selector_mask = vo.visual_check("checkout", image=same_size, mask=["#nav"])
    ok("selector mask with image= is an explicit error",
       selector_mask.get("ok") is False and "live page" in str(selector_mask.get("error", "")),
       str(selector_mask.get("error", ""))[:60])

    # REVIEW must never be silently downgraded
    reviewable = [soft["status"], missing["status"], not_observed["status"]]
    ok("no REVIEW case returned PASS", all(s == "REVIEW" for s in reviewable), str(reviewable))

    passed = sum(1 for _, good, _ in CHECKS if good)
    print()
    for name, good, detail in CHECKS:
        print(f"  [{'PASS' if good else 'FAIL'}] {name}" + (f"  ({detail})" if detail else ""))
    print(f"\n{passed}/{len(CHECKS)} contract audit checks passed")
    return 0 if passed == len(CHECKS) else 1


if __name__ == "__main__":
    sys.exit(main())
