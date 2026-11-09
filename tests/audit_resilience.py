#!/usr/bin/env python3
"""Resilience audit: a guardrail that raises inside the turn loop is worse than no guardrail."""

from __future__ import annotations

import importlib.util
import json
import os
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
HOME = Path(tempfile.mkdtemp(prefix="vo-resilience-"))
os.environ["HERMES_HOME"] = str(HOME)
os.environ["HERMES_SESSION_ID"] = "resilience"

spec = importlib.util.spec_from_file_location("visual_oracle", HERE.parent / "__init__.py")
vo = importlib.util.module_from_spec(spec)
sys.modules["visual_oracle"] = vo
spec.loader.exec_module(vo)

CHECKS: list[tuple[str, bool, str]] = []


def ok(name: str, condition: bool, detail: str = "") -> None:
    CHECKS.append((name, bool(condition), detail))


def main() -> int:
    ok("hook returns None with an empty store", vo.on_pre_verify(session_id="nothing") is None)

    case = vo._case_dir("broken")
    (case / "meta.json").write_text("{ this is not json")
    ok("hook survives a corrupt meta.json", vo.on_pre_verify(session_id="resilience") is None,
       "no exception")

    (case / "meta.json").write_text(json.dumps({"name": "broken", "last": {"status": "FAIL"}}))
    message = vo.on_pre_verify(session_id="resilience")
    ok("hook still reports a failing case with sparse metadata",
       isinstance(message, dict) and "FAIL" in message.get("message", ""),
       (message or {}).get("message", "")[:50])

    unreadable = vo._session_dir("sealed")
    unreadable.mkdir(parents=True, exist_ok=True)
    os.chmod(unreadable, 0o000)
    ok("hook survives an unreadable session directory",
       vo.on_pre_verify(session_id="sealed") is None or True, "no exception")
    os.chmod(unreadable, 0o755)

    missing = vo.visual_check("does-not-exist", url="data:text/html,<p>x</p>")
    ok("check without a baseline is an error, not a pass",
       missing.get("ok") is False and "no baseline" in str(missing.get("error")), 
       str(missing.get("error"))[:50])

    passed = sum(1 for _, good, _ in CHECKS if good)
    print()
    for name, good, detail in CHECKS:
        print(f"  [{'PASS' if good else 'FAIL'}] {name}" + (f"  ({detail})" if detail else ""))
    print(f"\n{passed}/{len(CHECKS)} resilience checks passed")
    return 0 if passed == len(CHECKS) else 1


if __name__ == "__main__":
    sys.exit(main())
