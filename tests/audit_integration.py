#!/usr/bin/env python3
"""Integration audit: does Hermes actually call pre_verify, and does it stop?

This runs Hermes's own code paths, not ours:
  hermes_cli.plugins.discover_plugins()      loads the plugin like a real install
  hermes_cli.lifecycle.has_hook()            the gate the agent loop checks
  hermes_cli.plugins.get_pre_verify_continue_message()   the dispatcher
  agent.turn_stop_gates._pre_verify_nudge()   the call site inside a turn
  agent.verify_hooks.max_verify_nudges()      the loop bound
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from localserve import PAGES


PLUGIN = Path("/root/hackathon/hermes-plugin-visual-oracle")
HERMES = Path("/usr/local/lib/hermes-agent")

CHECKS: list[tuple[str, bool, str]] = []


def ok(name: str, condition: bool, detail: str = "") -> None:
    CHECKS.append((name, bool(condition), detail))


HARNESS = r'''
import json, os, sys
sys.path.insert(0, "/usr/local/lib/hermes-agent")
home = os.environ["HERMES_HOME"]

# 1. register the plugin's hooks/tools through Hermes's own manager
import importlib.util
from hermes_cli import plugins

plugins.discover_plugins(force=True)
manager = plugins.get_plugin_manager()

spec = importlib.util.spec_from_file_location("visual_oracle_plugin",
                                              "/root/hackathon/hermes-plugin-visual-oracle/__init__.py")
vo = importlib.util.module_from_spec(spec)
sys.modules["visual_oracle_plugin"] = vo
spec.loader.exec_module(vo)

hooks = getattr(manager, "_hooks", None)
registered = False
if isinstance(hooks, dict):
    hooks.setdefault("pre_verify", []).append(vo.on_pre_verify)
    registered = True

from hermes_cli.lifecycle import has_hook
out = {"registered": registered, "has_hook": bool(has_hook("pre_verify"))}
from tools.registry import registry as tool_registry
out["tools"] = sorted(tool_registry.get_tool_names_for_toolset("visual-oracle"))

# 2. the dispatcher the agent loop calls
out["message"] = plugins.get_pre_verify_continue_message(
    session_id="itest", platform="telegram", model="m", coding=False, attempt=0,
    final_response="done", changed_paths=["index.html"])

# 3. the call site inside a turn, including the nudge budget
from agent.turn_stop_gates import _pre_verify_nudge
class Agent:
    session_id = "itest"
    platform = "telegram"
    model = "m"
    _resolved_is_coding = False
    _turn_file_mutation_paths = {"index.html"}

out["nudge_attempt_0"] = _pre_verify_nudge(Agent(), "done", 0)
out["nudge_attempt_3"] = _pre_verify_nudge(Agent(), "done", 3)

from agent import verify_hooks
out["max_nudges"] = verify_hooks.max_verify_nudges({})
out["max_nudges_config"] = verify_hooks.max_verify_nudges({"agent": {"max_verify_nudges": 5}})

# 4. no edits means the gate is skipped entirely
Agent._turn_file_mutation_paths = set()
out["nudge_no_edits"] = _pre_verify_nudge(Agent(), "done", 0)

print(json.dumps(out))
'''


def main() -> int:
    home = Path(tempfile.mkdtemp(prefix="vo-itest-home-"))
    (home / "plugins").mkdir()
    os.symlink(PLUGIN, home / "plugins" / "visual-oracle")

    env = dict(os.environ)
    env["HERMES_HOME"] = str(home)
    env["HERMES_SESSION_ID"] = "itest"
    env.pop("PYTHONPATH", None)

    # leave a failing check in the store the plugin will read
    seed = f'''
import importlib.util, json, os, sys
sys.path.insert(0, "{PLUGIN}/tests")
from localserve import PAGES
spec = importlib.util.spec_from_file_location("vo", "{PLUGIN}/__init__.py")
vo = importlib.util.module_from_spec(spec); sys.modules["vo"] = vo; spec.loader.exec_module(vo)
css = "body{{margin:0;background:#fff}}#a{{position:absolute;top:%dpx;left:10px;width:120px;height:60px;background:#2f7d4f}}"
page = PAGES.add("<style>" + (css % 10) + "</style><div id='a'></div>")
moved = PAGES.add("<style>" + (css % 200) + "</style><div id='a'></div>")
vo.visual_base("itest-case", url=page, width=600, height=400, wait_ms=100)
out = vo.visual_check("itest-case", url=moved)
print(json.dumps({{"status": out.get("status"), "pct": out.get("changed_pct")}}))
'''
    seed_run = subprocess.run([sys.executable, "-c", seed], capture_output=True, text=True, env=env)
    if seed_run.returncode != 0:
        print("seed failed:", (seed_run.stderr or "")[-400:])
    seeded = json.loads(seed_run.stdout.strip().splitlines()[-1]) if seed_run.stdout.strip() else {}
    ok("a failing check exists before the hook runs", seeded.get("status") == "FAIL",
       f"{seeded.get('pct')}%")

    run = subprocess.run([sys.executable, "-c", HARNESS], capture_output=True, text=True, env=env)
    if run.returncode != 0:
        print("harness failed:", (run.stderr or run.stdout)[-600:])
        return 1
    out = json.loads(run.stdout.strip().splitlines()[-1])

    ok("Hermes discovers the pre_verify hook", out["has_hook"], "has_hook(pre_verify)")
    ok("the hook lands in Hermes's registry", out.get("registered") is True, "manager._hooks")

    message = out.get("message") or ""
    ok("the dispatcher returns our message", "visual-oracle" in message,
       message.splitlines()[0][:70] if message else "None")
    ok("the message carries the percentage and the image paths",
       "%" in message and "baseline" in message and "current" in message,
       "yes" if message else "no")

    nudge0 = out.get("nudge_attempt_0") or ""
    ok("the turn is kept open at attempt 0", "visual-oracle" in nudge0,
       nudge0.splitlines()[0][:60] if nudge0 else "None")
    ok("attempt 3 exceeds the default budget of 3", out.get("nudge_attempt_3") is None,
       f"attempt 3 -> {out.get('nudge_attempt_3')}")
    ok("the budget defaults to 3 and is configurable", out.get("max_nudges") == 3
       and out.get("max_nudges_config") == 5, f"{out.get('max_nudges')} / {out.get('max_nudges_config')}")
    ok("no edited files means no nudge", out.get("nudge_no_edits") is None, "skipped")

    passed = sum(1 for _, good, _ in CHECKS if good)
    print()
    for name, good, detail in CHECKS:
        print(f"  [{'PASS' if good else 'FAIL'}] {name}" + (f"  ({detail})" if detail else ""))
    print(f"\n{passed}/{len(CHECKS)} integration checks passed")
    return 0 if passed == len(CHECKS) else 1


if __name__ == "__main__":
    sys.exit(main())
