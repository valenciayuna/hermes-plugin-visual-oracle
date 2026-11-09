"""Minimal WebSocket client, sized for one job: talking to Chrome's DevTools endpoint.

The standard library has no WebSocket, and a driver (Selenium/Playwright) is exactly what this
plugin promised not to need. So: handshake with a random key, masked text frames out, unmasked
frames in, fragmentation handled, ping answered. About eighty lines, all of it auditable.
"""

from __future__ import annotations

import base64
import json
import os
import socket
from urllib.parse import urlparse

OP_TEXT, OP_BINARY, OP_CLOSE, OP_PING, OP_PONG = 0x1, 0x2, 0x8, 0x9, 0xA


class WsError(RuntimeError):
    pass


class WebSocket:
    def __init__(self, url: str, timeout: float = 30.0):
        parsed = urlparse(url)
        if parsed.scheme != "ws":
            raise WsError(f"only ws:// supported, got {parsed.scheme}")
        host = parsed.hostname or "127.0.0.1"
        port = parsed.port or 80
        path = parsed.path or "/"
        if parsed.query:
            path += "?" + parsed.query

        self.sock = socket.create_connection((host, port), timeout=timeout)
        self.sock.settimeout(timeout)
        key = base64.b64encode(os.urandom(16)).decode()
        handshake = (
            f"GET {path} HTTP/1.1\r\n"
            f"Host: {host}:{port}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n\r\n"
        )
        self.sock.sendall(handshake.encode())
        header = self._read_until(b"\r\n\r\n")
        first_line = header.split(b"\r\n")[0]
        if b"101" not in first_line:
            raise WsError(f"handshake rejected: {first_line[:80]!r}")

    def _read_until(self, marker: bytes) -> bytes:
        data = b""
        while marker not in data:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise WsError("connection closed during handshake")
            data += chunk
        return data

    # ------------------------------------------------------------------ frames

    def send(self, payload: dict) -> None:
        body = json.dumps(payload).encode()
        length = len(body)
        header = bytearray([0x81])  # FIN + text
        if length < 126:
            header.append(0x80 | length)
        elif length < 65536:
            header.append(0x80 | 126)
            header += length.to_bytes(2, "big")
        else:
            header.append(0x80 | 127)
            header += length.to_bytes(8, "big")
        mask = os.urandom(4)
        header += mask
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(body))
        self.sock.sendall(bytes(header) + masked)

    def _recv_exact(self, n: int) -> bytes:
        data = b""
        while len(data) < n:
            chunk = self.sock.recv(n - len(data))
            if not chunk:
                raise WsError("connection closed")
            data += chunk
        return data

    def recv(self) -> dict:
        while True:
            first, second = self._recv_exact(2)
            opcode = first & 0x0F
            length = second & 0x7F
            if length == 126:
                length = int.from_bytes(self._recv_exact(2), "big")
            elif length == 127:
                length = int.from_bytes(self._recv_exact(8), "big")
            masked = second & 0x80
            mask = self._recv_exact(4) if masked else b""
            body = self._recv_exact(length) if length else b""
            if masked:
                body = bytes(b ^ mask[i % 4] for i, b in enumerate(body))

            if opcode == OP_PING:
                self._pong(body)
                continue
            if opcode == OP_PONG:
                continue
            if opcode == OP_CLOSE:
                raise WsError("chrome closed the websocket")
            if opcode in (OP_TEXT, OP_BINARY):
                try:
                    return json.loads(body.decode("utf-8", "replace"))
                except ValueError:
                    return {}

    def _pong(self, body: bytes) -> None:
        header = bytearray([0x8A])
        length = len(body)
        if length < 126:
            header.append(0x80 | length)
        else:
            header.append(0x80 | 126)
            header += length.to_bytes(2, "big")
        mask = os.urandom(4)
        header += mask
        self.sock.sendall(bytes(header) + bytes(b ^ mask[i % 4] for i, b in enumerate(body)))

    def close(self) -> None:
        try:
            self.sock.close()
        except OSError:
            pass
