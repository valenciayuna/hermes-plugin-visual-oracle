"""Driving Chrome over its DevTools endpoint, with no driver library in between.

Chrome can open a debugging port and hand out a WebSocket URL in `<profile>/DevToolsActivePort`.
That gives this plugin the things a one-shot `--screenshot` cannot do:

- wait until `document.fonts.ready` has resolved
- wait for a selector to exist, or fail loudly with a timeout
- read an element's box, so a mask can name a selector instead of pixels
- take screenshots in a row and only accept them once the pixels stop changing
- report the actual Chrome version and viewport that produced an image

No Selenium, no Playwright, no pip install: one socket, JSON messages, standard library only.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

from ws import WebSocket, WsError

CHROME_CANDIDATES = (
    "google-chrome", "google-chrome-stable", "chromium", "chromium-browser",
    "/opt/google/chrome/chrome",
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
)


class CdpError(RuntimeError):
    """Chrome said no, or the session died."""


def find_chrome() -> str | None:
    for name in CHROME_CANDIDATES:
        found = shutil.which(name) or (name if Path(name).exists() else None)
        if found and not Path(found).is_symlink() or (found and Path(found).exists()):
            return found
    return None


class Browser:
    """One Chrome process, one page target, flattened DevTools sessions."""

    def __init__(self, width: int = 1280, height: int = 720, timeout: float = 30.0):
        chrome = find_chrome()
        if not chrome:
            raise CdpError("no chrome/chromium found; pass image=... instead of url=...")
        self.timeout = timeout
        self._next_id = 0
        self._session: str | None = None
        self.info: dict = {}

        self.profile = Path(tempfile.mkdtemp(prefix="vo-chrome-"))
        self.proc = subprocess.Popen(
            [
                chrome, "--headless=new", "--no-sandbox", "--disable-gpu",
                "--disable-dev-shm-usage", "--hide-scrollbars", "--force-device-scale-factor=1",
                "--no-first-run", "--no-default-browser-check",
                f"--user-data-dir={self.profile}",
                f"--window-size={width},{height}",
                "--remote-debugging-port=0",
                "about:blank",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        ws_url = self._wait_for_devtools(timeout)
        self.ws = WebSocket(ws_url, timeout=timeout)

        version = self.call("Browser.getVersion", session=False)
        self.info["chrome"] = version.get("product", "")
        self.info["protocol"] = version.get("protocolVersion", "")

        target = self.call("Target.createTarget", {"url": "about:blank"}, session=False)
        attached = self.call("Target.attachToTarget",
                             {"targetId": target["targetId"], "flatten": True}, session=False)
        self._session = attached["sessionId"]

        self.send("Page.enable")
        self.send("Runtime.enable")
        self.send("Emulation.setDeviceMetricsOverride",
                  {"width": width, "height": height, "deviceScaleFactor": 1, "mobile": False})
        self.info["viewport"] = [width, height]

    # ------------------------------------------------------------------ startup

    def _wait_for_devtools(self, timeout: float) -> str:
        port_file = self.profile / "DevToolsActivePort"
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.proc.poll() is not None:
                raise CdpError(f"chrome exited with code {self.proc.returncode} before opening a "
                               "debugging port")
            if port_file.is_file():
                lines = port_file.read_text().splitlines()
                if len(lines) >= 2 and lines[0].strip().isdigit():
                    return f"ws://127.0.0.1:{lines[0].strip()}{lines[1].strip()}"
            time.sleep(0.05)
        raise CdpError(f"chrome did not open a debugging port within {timeout}s")

    # ------------------------------------------------------------------ transport

    def call(self, method: str, params: dict | None = None, session: bool = True) -> dict:
        self._next_id += 1
        message = {"id": self._next_id, "method": method, "params": params or {}}
        if session and self._session:
            message["sessionId"] = self._session
        self.ws.send(message)

        deadline = time.time() + self.timeout
        while time.time() < deadline:
            try:
                reply = self.ws.recv()
            except WsError as exc:
                raise CdpError(str(exc)) from None
            if reply.get("id") != self._next_id:
                continue  # an event, or a reply to an earlier call
            if "error" in reply:
                err = reply["error"]
                raise CdpError(f"{method}: {err.get('message')} ({err.get('code')})")
            return reply.get("result", {})
        raise CdpError(f"{method}: timed out after {self.timeout}s")

    def send(self, method: str, params: dict | None = None) -> None:
        self._next_id += 1
        message = {"id": self._next_id, "method": method, "params": params or {}}
        if self._session:
            message["sessionId"] = self._session
        self.ws.send(message)

    # ------------------------------------------------------------------ page

    def evaluate(self, expression: str, await_promise: bool = False):
        out = self.call("Runtime.evaluate", {
            "expression": expression,
            "returnByValue": True,
            "awaitPromise": await_promise,
        })
        if out.get("exceptionDetails"):
            detail = (out["exceptionDetails"].get("exception") or {}).get("description", "")
            raise CdpError(f"JS error: {detail or out['exceptionDetails'].get('text')}")
        return (out.get("result") or {}).get("value")

    def navigate(self, url: str, wait_ms: int = 0) -> None:
        self.call("Page.navigate", {"url": url})
        deadline = time.time() + self.timeout
        while time.time() < deadline:
            if self.evaluate("document.readyState") in ("interactive", "complete"):
                break
            time.sleep(0.05)
        else:
            raise CdpError(f"page never finished loading: {url}")
        if wait_ms:
            time.sleep(wait_ms / 1000.0)

    def wait_fonts(self) -> str:
        """Block on document.fonts.ready. Returns the status Chrome reports."""
        return str(self.evaluate(
            "document.fonts ? document.fonts.ready.then(() => document.fonts.status) : 'none'",
            await_promise=True) or "unknown")

    def wait_selector(self, selector: str, timeout_s: float = 10.0) -> None:
        expr = f"!!document.querySelector({json.dumps(selector)})"
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            if self.evaluate(expr):
                return
            time.sleep(0.08)
        raise CdpError(f"selector never appeared within {timeout_s}s: {selector}")

    def broken_images(self) -> list[str]:
        return self.evaluate(
            "Array.from(document.images).filter(i => i.complete && i.naturalWidth === 0)"
            ".map(i => i.currentSrc || i.src).slice(0, 10)") or []

    def rects(self, selector: str) -> list[list[int]]:
        return self.evaluate(
            "Array.from(document.querySelectorAll(" + json.dumps(selector) + "))"
            ".map(e => { const r = e.getBoundingClientRect();"
            " return [Math.round(r.x), Math.round(r.y), Math.round(r.width), Math.round(r.height)]; })"
            ".filter(r => r[2] > 0 && r[3] > 0)") or []

    def screenshot(self) -> bytes:
        import base64
        out = self.call("Page.captureScreenshot", {"format": "png", "captureBeyondViewport": False})
        return base64.b64decode(out["data"])

    def screenshot_stable(self, attempts: int = 4, gap_ms: int = 150) -> tuple[bytes, int]:
        """Screenshots in a row until two are byte-identical; returns (png, tries)."""
        previous = self.screenshot()
        for attempt in range(2, attempts + 1):
            time.sleep(gap_ms / 1000.0)
            current = self.screenshot()
            if current == previous:
                return current, attempt
            previous = current
        raise CdpError(
            f"rendering never stabilised after {attempts} captures "
            "(continuous animation? mask the moving regions or set stable=False)")

    def close(self) -> None:
        self.ws.close()
        if self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        shutil.rmtree(self.profile, ignore_errors=True)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False
