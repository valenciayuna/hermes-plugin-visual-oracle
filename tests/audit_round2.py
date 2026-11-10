#!/usr/bin/env python3
"""Second-pass audit of Visual Oracle: redirects the page itself starts, and policy bypasses."""
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

UP = importlib.import_module("urlpolicy")
spec = importlib.util.spec_from_file_location("vo", PLUGIN / "__init__.py")
vo = importlib.util.module_from_spec(spec)
sys.modules["vo"] = vo
spec.loader.exec_module(vo)

checks = []


def check(name, ok, detail=""):
    checks.append(bool(ok))
    print(f"{'ok  ' if ok else 'FAIL'} {name}{'' if ok else ' :: ' + str(detail)[:150]}")


META = "169.254.169.254"


class Pages(http.server.BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        body = {
            "/js": f"<h1>a</h1><script>setTimeout(()=>{{location.href='http://{META}/latest/meta-data/'}},50)</script>",
            "/meta": f"<meta http-equiv='refresh' content='0; url=http://{META}/latest/meta-data/'>",
            "/img": f"<img src='http://{META}/x.png'><h1>b</h1>",
            "/fetch": f"<script>fetch('http://{META}/y')</script><h1>c</h1>",
            "/plain": "<h1>fine</h1>",
        }.get(self.path.split("?")[0])
        if body is None:
            self.send_response(404)
            self.end_headers()
            return
        data = body.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args):
        pass


sock = socket.socket()
sock.bind(("127.0.0.1", 0))
port = sock.getsockname()[1]
sock.close()
srv = http.server.ThreadingHTTPServer(("127.0.0.1", port), Pages)
srv.daemon_threads = True
threading.Thread(target=srv.serve_forever, daemon=True).start()
base = f"http://127.0.0.1:{port}"

try:
    for path, label in (("/js", "a JS redirect"), ("/meta", "a meta refresh"),
                        ("/img", "an image from the metadata host"),
                        ("/fetch", "a fetch to the metadata host")):
        res = vo.visual_base(f"audit2{path.replace('/', '-')}", url=base + path)
        blocked = res.get("ok") is False and "not public" in str(res.get("error", ""))
        check(f"{label} is refused", blocked, res)

    plain = vo.visual_base("audit2-plain", url=base + "/plain")
    check("a plain page still renders", plain.get("ok") is True, plain)
finally:
    srv.shutdown()

# a hostname that resolves to loopback is refused even though the name looks public
try:
    UP.check_url("http://localtest.me/")
    check("a name resolving to loopback is refused", False, "allowed")
except UP.UrlPolicyError as exc:
    check("a name resolving to loopback is refused", True, exc)

# the allow list is the only way in, and it is exact
os.environ["VO_ALLOW_HOSTS"] = "internal.test"
hosts = vo.allow_hosts()
check("the default allow list is localhost only", "localhost" in hosts and "127.0.0.1" in hosts, hosts)
check("extra hosts come from the setting", "internal.test" in hosts, hosts)
try:
    UP.check_url("http://10.0.0.9/", hosts)
    check("allow list still refuses an unlisted private address", False, "allowed")
except UP.UrlPolicyError:
    check("allow list still refuses an unlisted private address", True)

# an addr that is allowed by name but private is still refused unless listed
try:
    UP.check_url("http://127.0.0.2/", hosts)
    check("127.0.0.2 is not allowed just because 127.0.0.1 is", False, "allowed")
except UP.UrlPolicyError:
    check("127.0.0.2 is not allowed just because 127.0.0.1 is", True)

# contract traversal
case = vo._case_dir("audit2-contract")
case.mkdir(parents=True, exist_ok=True)
escape = case / ".." / ".." / "escape.json"
escape = escape.resolve()
escape.write_text('{"max_changed_pct": 9}')
try:
    vo._contract_from(str(escape), case)
    check("a contract path with .. is refused", False, "accepted")
except ValueError as exc:
    check("a contract path with .. is refused", True, exc)

passed = sum(checks)
print(f"\n{passed}/{len(checks)} checks passed")
sys.exit(0 if passed == len(checks) else 1)
