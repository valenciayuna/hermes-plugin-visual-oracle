"""Screenshot capture through a local Chrome. No driver, no API, no network beyond the page."""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

CHROME_CANDIDATES = (
    "google-chrome", "google-chrome-stable", "chromium", "chromium-browser",
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
)


def find_chrome() -> str | None:
    for name in CHROME_CANDIDATES:
        found = shutil.which(name) or (name if Path(name).exists() else None)
        if found:
            return found
    return None


def capture(url: str, out: Path, width: int = 1280, height: int = 720,
            wait_ms: int = 1500, timeout: int = 60) -> Path:
    """Render `url` headlessly and write a PNG. Raises RuntimeError with the reason on failure."""
    chrome = find_chrome()
    if not chrome:
        raise RuntimeError("no chrome/chromium found; pass image=... instead of url=...")

    out.parent.mkdir(parents=True, exist_ok=True)
    if out.exists():
        out.unlink()

    with tempfile.TemporaryDirectory() as profile:
        argv = [
            chrome,
            "--headless=new",
            "--no-sandbox",
            "--disable-gpu",
            "--disable-dev-shm-usage",
            "--hide-scrollbars",
            "--force-device-scale-factor=1",
            f"--user-data-dir={profile}",
            f"--window-size={width},{height}",
            f"--virtual-time-budget={max(wait_ms, 200)}",
            f"--screenshot={out}",
            url,
        ]
        env = dict(os.environ)
        env.pop("DISPLAY", None)
        started = time.time()
        try:
            proc = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, env=env)
        except subprocess.TimeoutExpired:
            raise RuntimeError(f"chrome timed out after {timeout}s rendering {url}") from None
        elapsed = round(time.time() - started, 2)

    if not out.exists() or out.stat().st_size == 0:
        tail = (proc.stderr or proc.stdout or "").strip().splitlines()[-3:]
        raise RuntimeError(f"chrome produced no screenshot ({elapsed}s): {' / '.join(tail)}")
    return out
