"""Serve generated HTML over http://127.0.0.1 so tests render through the same policy as real use.

Rendering file:// or data: fixtures would test a path the plugin now refuses on purpose.
"""
from __future__ import annotations

import http.server
import itertools
import socket
import threading

_PAGES: dict[str, str] = {}


class _Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        html = _PAGES.get(self.path)
        if html is None:
            self.send_response(404)
            self.send_header("Content-Length", "4")
            self.end_headers()
            self.wfile.write(b"nope")
            return
        body = html.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):  # keep test output clean
        pass


class LocalPages:
    def __init__(self) -> None:
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        sock.close()
        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", port), _Handler)
        self.server.daemon_threads = True
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{port}"
        self._ids = itertools.count()

    def add(self, html: str) -> str:
        name = f"p{next(self._ids)}"
        _PAGES[f"/{name}"] = html
        return f"{self.base}/{name}"


PAGES = LocalPages()
