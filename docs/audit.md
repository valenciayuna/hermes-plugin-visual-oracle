# Audit

Two questions, answered separately: is the measurement correct, and is the measurement actually
used by Hermes.

Everything below was run against this repository at the commit that contains the audit tests. The
numbers are copied from the runs, not summarised.

```
tests/pngdiff_proof.py        18/18   decoder and diff, generated fixtures
tests/audit_code.py           11/11   shipped diff vs an independent naive reference
tests/visual_proof.py         16/16   end to end through Chrome
tests/audit_contract.py       12/12   contract and mask matrix
tests/audit_resilience.py      5/5    the hook under broken stores
tests/audit_integration.py     9/9    Hermes dispatcher, call site, nudge budget
hermes plugins validate .     passed
```

## Code audit

The point was distrust: for the diff, an independent and deliberately naive reference
implementation counts changed pixels and the outer bounding box, then the shipped one is compared
against it.

| what was checked | how | result |
|---|---|---|
| pixel counting | 12 seeded random image pairs, shipped vs naive reference | counts and union boxes agreed 12/12 |
| bounding box | a known 24x16 rectangle | `[10, 20, 24, 16]`, 384 px |
| region splitting | three disconnected changes | three regions |
| masks | a mask covering the rectangle, then one column short | hidden 0 px, short 16 px |
| tolerance | delta of 8 then 9 on one channel | 8 ignored, 9 counted |
| full page change | black vs white | one region, full frame, 100.0% |
| determinism | same pair twice | byte-identical JSON |
| corrupt input | bad signature, truncated, empty, garbage chunks | all raise `PngError`, none return a verdict |
| unlucky geometry | changes on image corners | boxes stay inside the frame |

### Bugs the audit found and fixed

1. **A protected region could pass.** The threshold was evaluated before the contract, so a 12x12
   element changing inside `#nav` (0.045% of the page) returned `PASS`. Protected now means
   protected: the contract is resolved first and any overlapping region is `FAIL`, whatever the
   percentage. Covered by "protected region change under the threshold is still FAIL" in
   `tests/audit_contract.py`.
2. **Masks were silently ignored when comparing a supplied image.** `visual_check(image=...)` never
   applied the mask, so the same call returned `FAIL` for a region the caller had masked. Coordinate
   masks are now applied; selector masks raise an explicit error, because an image has no DOM.
3. **The hook could go silent on messy metadata.** A `meta.json` missing keys made the message
   builder raise, and the wrapper swallowed it into `None`, which reads as "no failures". The builder
   is now tolerant and still reports the case with whatever fields exist.
4. **The hook ignored Hermes's session.** It read only `HERMES_SESSION_ID`, which is not set in the
   gateway process, so every chat shared one store. It now takes the `session_id` Hermes passes and
   falls back to the shared directory when that one holds nothing.

## Integration audit

Run through Hermes's own code paths, in a subprocess with a temporary `HERMES_HOME`.

| claim | evidence |
|---|---|
| Hermes holds the hook | `manager._hooks["pre_verify"]` contains the callback, `has_hook("pre_verify")` is true |
| the dispatcher picks it up | `get_pre_verify_continue_message(...)` returns our message |
| the message reaches the agent | the text carries the percentage, the regions, the diff path, baseline and current |
| the turn is kept open | `agent.turn_stop_gates._pre_verify_nudge(agent, "done", 0)` returns the message |
| the loop is bounded | same call at `attempt=3` returns `None` |
| the bound is configurable | `max_verify_nudges({})` is 3, `{"agent": {"max_verify_nudges": 5}}` is 5 |
| no edits, no nudge | with `_turn_file_mutation_paths` empty the call returns `None` |

## What is not covered

Stated plainly, because an audit that claims completeness is the same problem it is auditing.

- **WebSocket fragmentation.** `ws.py` does not reassemble continuation frames. Chrome has not
  fragmented a message in any run here, including 600 KB screenshots, but a different build could.
- **Peak memory on a full-page diff.** Changed pixels are held as a set of `(x, y)` tuples. A
  full-page change on a 1280x720 page is about 900k tuples. It completes, but it is the heaviest path
  and has not been profiled.
- **Chrome versions.** Everything was measured on Chrome/152.0.7977.64. Older headless builds may
  differ in `DevToolsActivePort` timing or `document.fonts` behaviour.
- **A live agent turn.** The integration audit calls the real functions Hermes calls, but it does not
  run a model through a full turn. That would need a provider call and would be a slow, flaky test.
- **Windows and macOS.** Only Linux was exercised.

## Reproducing it

```
python3 tests/pngdiff_proof.py
python3 tests/audit_code.py
python3 tests/visual_proof.py
python3 tests/audit_contract.py
python3 tests/audit_resilience.py
python3 tests/audit_integration.py
```

The first two need no browser. The last four need Chrome. Each prints its own pass count and exits
non-zero on failure, so a CI job can run them as they are.
