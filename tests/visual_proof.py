#!/usr/bin/env python3
"""Proof script for visual-oracle: real pages, real PNGs, no mocks.

Each check states what it proves. A failure prints the actual numbers so the claim can be
disproved instead of argued about.
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

HOME = Path(tempfile.mkdtemp(prefix="vo-proof-"))
os.environ["HERMES_HOME"] = str(HOME)
os.environ["HERMES_SESSION_ID"] = "proof"

import importlib.util  # noqa: E402

PLUGIN = HERE.parent / "__init__.py"
spec = importlib.util.spec_from_file_location("visual_oracle", PLUGIN)
vo = importlib.util.module_from_spec(spec)
sys.modules["visual_oracle"] = vo
spec.loader.exec_module(vo)

CHECKS: list[tuple[str, bool, str]] = []


def ok(name: str, condition: bool, detail: str = "") -> None:
    CHECKS.append((name, bool(condition), detail))


PAGE = "data:text/html," + (
    "<body style='margin:0;background:%23ffffff;font:24px sans-serif'>"
    "<div style='padding:20px'>Rumah &amp; Resto</div>"
    "<button style='position:absolute;left:20px;top:120px'>SIGN IN</button>"
    "</body>"
)

PAGE_MOVED = "data:text/html," + (
    "<body style='margin:0;background:%23ffffff;font:24px sans-serif'>"
    "<div style='padding:20px'>Rumah &amp; Resto</div>"
    "<button style='position:absolute;left:20px;top:400px'>SIGN IN</button>"
    "</body>"
)

PAGE_TINY = "data:text/html," + (
    "<body style='margin:0;background:%23ffffff;font:24px sans-serif'>"
    "<div style='padding:20px'>Rumah &amp; Resto</div>"
    "<button style='position:absolute;left:20px;top:122px'>SIGN IN</button>"
    "</body>"
)


def main() -> int:
    base = vo.visual_base("login", url=PAGE, width=800, height=600)
    ok("baseline captured", base.get("ok") and Path(base.get("baseline", "")).is_file(),
       f"bytes={base.get('bytes')}")

    same = vo.visual_check("login", url=PAGE, threshold=0.2)
    ok("identical render passes", same.get("status") == "PASS",
       f"changed={same.get('changed_pct')}% bbox={same.get('bbox')}")

    moved = vo.visual_check("login", url=PAGE_MOVED, threshold=0.2)
    ok("moved button fails the check", moved.get("status") == "FAIL",
       f"changed={moved.get('changed_pct')}% bbox={moved.get('bbox')}")
    ok("bbox points at the button row", bool(moved.get("bbox")) and moved["bbox"][1] > 80,
       f"bbox={moved.get('bbox')}")

    tiny = vo.visual_check("login", url=PAGE_TINY, threshold=0.2)
    ok("2px nudge is below a 0.2% threshold", tiny.get("status") == "PASS",
       f"changed={tiny.get('changed_pct')}%")

    masked = vo.visual_check("login", url=PAGE_MOVED, threshold=0.2,
                             mask=[[0, 80, 800, 420]])
    ok("masked region is ignored", masked.get("status") == "PASS",
       f"changed={masked.get('changed_pct')}%")

    # leave a real failure on disk so the hook has something to catch
    vo.visual_check("login", url=PAGE_MOVED, threshold=0.2)
    hook = vo.on_pre_verify()
    ok("hook blocks a turn with a failing check",
       isinstance(hook, dict) and hook.get("action") == "continue",
       (hook or {}).get("message", "returned None")[:70].replace("\n", " "))

    listing = vo.visual_list()
    ok("listing shows the case", listing.get("count", 0) >= 1,
       str([(c["name"], c["last_status"]) for c in listing.get("cases", [])]))

    bad = vo.visual_check("does-not-exist", url=PAGE)
    ok("unknown case is an error, not a pass", bad.get("ok") is False, bad.get("error", "")[:60])

    broken = vo.visual_base("broken", image="/nonexistent/shot.png")
    ok("missing image is an error", broken.get("ok") is False, broken.get("error", "")[:60])

    passed = sum(1 for _, good, _ in CHECKS if good)
    print()
    for name, good, detail in CHECKS:
        print(f"  [{'PASS' if good else 'FAIL'}] {name}" + (f"  ({detail})" if detail else ""))
    print(f"\n{passed}/{len(CHECKS)} checks passed")
    return 0 if passed == len(CHECKS) else 1


if __name__ == "__main__":
    sys.exit(main())
