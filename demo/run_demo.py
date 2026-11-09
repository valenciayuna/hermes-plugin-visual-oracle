#!/usr/bin/env python3
"""Reproduce the demo: baseline, the broken state, the fix. Prints the real numbers.

Usage:  python3 demo/run_demo.py
"""
from __future__ import annotations

import importlib.util
import os
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("HERMES_HOME", tempfile.mkdtemp(prefix="vo-demo-home-"))
os.environ.setdefault("HERMES_SESSION_ID", "demo")

spec = importlib.util.spec_from_file_location("visual_oracle", ROOT / "__init__.py")
vo = importlib.util.module_from_spec(spec)
sys.modules["visual_oracle"] = vo
spec.loader.exec_module(vo)


def main() -> int:
    pages = {name: f"file://{HERE / name}.html" for name in ("baseline", "broken", "fixed")}

    base = vo.visual_base("checkout", url=pages["baseline"], width=1000, height=620,
                          wait_ms=150, wait_selector="#pay")
    if not base.get("ok"):
        print("baseline failed:", base.get("error"))
        return 1
    cap = base.get("capture") or {}
    print(f"baseline  ok · {cap.get('viewport')} · {cap.get('chrome')} · "
          f"fonts {cap.get('fonts')} · stable after {cap.get('stable_tries')} frame(s)")

    for label in ("broken", "fixed"):
        out = vo.visual_check("checkout", url=pages[label])
        if not out.get("ok"):
            print(f"{label:<9} error: {out.get('error')}")
            return 1
        print(f"{label:<9} {out['status']:<5} {out['changed_pct']}% changed "
              f"(allowed {out['threshold_pct']}%)")
        for region in out.get("regions", [])[:3]:
            print(f"           region {region['bbox']} {region['changed_pixels']}px "
                  f"density {region['density']}")
        for path in (out.get("artifacts") or {}).values():
            if isinstance(path, list):
                for item in path:
                    print(f"           {item}")
            elif path:
                print(f"           {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
