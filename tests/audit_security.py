#!/usr/bin/env python3
"""Tests for the three review findings on Visual Oracle.

1. only public http(s) renders; file://, private ranges and metadata addresses are refused, and a
   redirect cannot slip past
2. --no-sandbox is only passed when the sandbox cannot work
3. image input must be a real PNG, absurd dimensions are refused, and a contract path cannot point
   outside the case folder
"""
from __future__ import annotations

import http.server
import importlib
import importlib.util
import os
import socket
import sys
import threading
from pathlib import Path

PLUGIN = Path("/root/hackathon/hermes-plugin-visual-oracle")
sys.path.insert(0, str(PLUGIN))

mods = {}
for name in ("ws", "urlpolicy", "cdp", "pngdiff", "capture"):
    mods[name] = importlib.import_module(name)

spec = importlib.util.spec_from_file_location("vo", PLUGIN / "__init__.py")
vo = importlib.util.module_from_spec(spec)
sys.modules["vo"] = vo
spec.loader.exec_module(vo)

checks = []


def check(name: str, ok: bool, detail: str = "") -> None:
    checks.append((name, bool(ok)))
    print(f"{'ok  ' if ok else 'FAIL'} {name}{'' if ok else ' :: ' + str(detail)[:160]}")


# ------------------------------------------------------------------ policy
UP = mods["urlpolicy"]
refused = [
    "file:///etc/passwd",
    "file:///root/.ssh/id_rsa",
    "data:text/html,<h1>x</h1>",
    "javascript:alert(1)",
    "http://169.254.169.254/latest/meta-data/",
    "http://10.1.2.3/",
    "http://192.168.0.10/",
    "http://[fd00::1]/",
    "http://metadata.google.internal/",
]
allowed = ["https://example.com/", "http://localhost:8000/", "http://127.0.0.1:5000/"]

for url in refused:
    try:
        UP.check_url(url)
        check(f"refused: {url}", False, "was allowed")
    except UP.UrlPolicyError:
        check(f"refused: {url}", True)

for url in allowed:
    try:
        UP.check_url(url)
        check(f"allowed: {url}", True)
    except UP.UrlPolicyError as exc:
        check(f"allowed: {url}", False, exc)

# ------------------------------------------------------- the tool refuses it
from capture import CdpError  # noqa: E402

res = vo.visual_base("audit-file-url", url="file:///etc/passwd")
check("visual_base refuses file://", res.get("ok") is False and "http" in res.get("error", ""), res)

# ------------------------------------------------------------ redirect hop
class Redirecting(http.server.BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        if self.path.startswith("/away"):
            self.send_response(302)
            self.send_header("Location", "http://169.254.169.254/latest/meta-data/")
            self.end_headers()
        else:
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(b"<h1>hello</h1>")

    def log_message(self, *args):
        pass


sock = socket.socket()
sock.bind(("127.0.0.1", 0))
port = sock.getsockname()[1]
sock.close()
server = http.server.ThreadingHTTPServer(("127.0.0.1", port), Redirecting)
threading.Thread(target=server.serve_forever, daemon=True).start()

try:
    away = vo.visual_base("audit-redirect", url=f"http://127.0.0.1:{port}/away")
    check("a redirect to a metadata address is refused",
          away.get("ok") is False and "not public" in away.get("error", ""), away)

    local = vo.visual_base("audit-local-ok", url=f"http://127.0.0.1:{port}/")
    check("a local page is still allowed", local.get("ok") is True, local)
finally:
    server.shutdown()

# ------------------------------------------------------------------ sandbox
CDP = mods["cdp"]
real_geteuid = os.geteuid
os.geteuid = lambda: 1000
try:
    namespaces_ok = True
    try:
        namespaces_ok = int(open("/proc/sys/user/max_user_namespaces").read().strip() or "0") > 0
    except OSError:
        namespaces_ok = True
    check("sandbox is used when it can work", CDP.sandbox_usable() is True or not namespaces_ok)
finally:
    os.geteuid = real_geteuid

check("sandbox is skipped as root", CDP.sandbox_usable() is False)

# --------------------------------------------------------------- image input
bogus = Path("/tmp/vo-not-a-png.png")
bogus.write_text("i am not a png\n")
png_res = vo.visual_base("audit-png", image=str(bogus))
check("a non PNG image is refused", png_res.get("ok") is False and "PNG" in png_res.get("error", ""),
      png_res)

PNG = mods["pngdiff"]
huge = Path("/tmp/vo-huge.png")


def _chunk(kind: bytes, payload: bytes) -> bytes:
    import struct
    import zlib
    return (struct.pack(">I", len(payload)) + kind + payload
            + struct.pack(">I", zlib.crc32(kind + payload) & 0xFFFFFFFF))


import struct as _struct  # noqa: E402
import zlib as _zlib  # noqa: E402

_ihdr = _struct.pack(">IIBBBBB", 20000, 20000, 8, 6, 0, 0, 0)
huge.write_bytes(
    b"\x89PNG\r\n\x1a\n"
    + _chunk(b"IHDR", _ihdr)
    + _chunk(b"IDAT", _zlib.compress(b"\x00" * 64))
    + _chunk(b"IEND", b"")
)
try:
    PNG.decode(huge)
    check("a 20000 px wide PNG is refused before decompressing", False, "decoded")
except Exception as exc:
    check("a 20000 px wide PNG is refused before decompressing",
          "limit" in str(exc).lower() or "8192" in str(exc), exc)

# ------------------------------------------------------------- contract path
case = vo._case_dir("audit-contract")
case.mkdir(parents=True, exist_ok=True)
outside = Path("/tmp/vo-contract.json")
outside.write_text('{"max_changed_pct": 1.0}')
try:
    vo._contract_from(str(outside), case)
    check("a contract path outside the case folder is refused", False, "accepted")
except ValueError as exc:
    check("a contract path outside the case folder is refused", "data folder" in str(exc), exc)

inside = case / "contract.json"
inside.write_text('{"max_changed_pct": 1.0}')
try:
    got = vo._contract_from(str(inside), case)
    check("a contract inside the case folder still loads", got.get("max_changed_pct") == 1.0, got)
except ValueError as exc:
    check("a contract inside the case folder still loads", False, exc)

passed = sum(1 for _, ok in checks if ok)
print(f"\n{passed}/{len(checks)} checks passed")
sys.exit(0 if passed == len(checks) else 1)
