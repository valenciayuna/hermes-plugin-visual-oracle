"""Minimal PNG decode + pixel diff, standard library only.

Why hand-rolled: a catalog plugin that pulls Pillow adds an install path, a version floor and a
review conversation. Chrome screenshots are PNG colour type 2 or 6 at 8-bit, non-interlaced, so
decoding them is about eighty lines. Anything else is refused loudly rather than guessed at.
"""

from __future__ import annotations

import struct
import zlib

PNG_SIG = b"\x89PNG\r\n\x1a\n"
CHANNELS = {2: 3, 6: 4}  # truecolour RGB, truecolour + alpha


class PngError(ValueError):
    """The file is not a PNG this module can read."""


def _paeth(a: int, b: int, c: int) -> int:
    p = a + b - c
    pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
    if pa <= pb and pa <= pc:
        return a
    return b if pb <= pc else c


def decode(path: str) -> tuple[int, int, int, bytes]:
    """Return (width, height, channels, pixels) for an 8-bit RGB/RGBA non-interlaced PNG."""
    with open(path, "rb") as handle:
        raw = handle.read()
    if not raw.startswith(PNG_SIG):
        raise PngError("not a PNG")

    pos = len(PNG_SIG)
    width = height = depth = colour = interlace = None
    idat = bytearray()
    while pos + 8 <= len(raw):
        length, kind = struct.unpack(">I4s", raw[pos:pos + 8])
        body = raw[pos + 8:pos + 8 + length]
        pos += 12 + length
        if kind == b"IHDR":
            width, height, depth, colour, _comp, _filt, interlace = struct.unpack(">IIBBBBB", body)
            if depth != 8 or colour not in CHANNELS or interlace != 0:
                raise PngError(
                    f"unsupported PNG (depth={depth}, colour={colour}, interlace={interlace}); "
                    "expected 8-bit RGB/RGBA, non-interlaced"
                )
        elif kind == b"IDAT":
            idat += body
        elif kind == b"IEND":
            break

    if width is None:
        raise PngError("no IHDR")

    channels = CHANNELS[colour]
    stride = width * channels
    data = zlib.decompress(bytes(idat))
    out = bytearray(stride * height)
    prev = bytearray(stride)
    src = 0
    for y in range(height):
        filter_type = data[src]
        src += 1
        line = bytearray(data[src:src + stride])
        src += stride
        if filter_type == 1:      # Sub
            for i in range(channels, stride):
                line[i] = (line[i] + line[i - channels]) & 0xFF
        elif filter_type == 2:    # Up
            for i in range(stride):
                line[i] = (line[i] + prev[i]) & 0xFF
        elif filter_type == 3:    # Average
            for i in range(stride):
                left = line[i - channels] if i >= channels else 0
                line[i] = (line[i] + ((left + prev[i]) >> 1)) & 0xFF
        elif filter_type == 4:    # Paeth
            for i in range(stride):
                left = line[i - channels] if i >= channels else 0
                up_left = prev[i - channels] if i >= channels else 0
                line[i] = (line[i] + _paeth(left, prev[i], up_left)) & 0xFF
        elif filter_type != 0:
            raise PngError(f"bad filter type {filter_type} on row {y}")
        out[y * stride:(y + 1) * stride] = line
        prev = line

    return width, height, channels, bytes(out)


def _in_mask(x: int, y: int, masks: list[list[int]]) -> bool:
    for mx, my, mw, mh in masks:
        if mx <= x < mx + mw and my <= y < my + mh:
            return True
    return False


def compare(a_path: str, b_path: str, tolerance: int = 8,
            masks: list[list[int]] | None = None) -> dict:
    """Pixel-compare two PNGs.

    tolerance: per-channel difference that still counts as the same colour (0-255).
    masks: [[x, y, w, h], ...] regions to ignore (animations, clocks, cursors).
    """
    aw, ah, ac, apx = decode(a_path)
    bw, bh, bc, bpx = decode(b_path)
    masks = masks or []

    if (aw, ah) != (bw, bh):
        return {
            "status": "fail",
            "reason": f"size changed: {aw}x{ah} -> {bw}x{bh}",
            "changed_pixels": 0,
            "total_pixels": aw * ah,
            "changed_pct": 100.0,
            "bbox": None,
        }

    changed = 0
    min_x, min_y, max_x, max_y = aw, ah, -1, -1
    for y in range(ah):
        row_a = y * aw * ac
        row_b = y * aw * bc
        for x in range(aw):
            if masks and _in_mask(x, y, masks):
                continue
            ia = row_a + x * ac
            ib = row_b + x * bc
            if (abs(apx[ia] - bpx[ib]) > tolerance
                    or abs(apx[ia + 1] - bpx[ib + 1]) > tolerance
                    or abs(apx[ia + 2] - bpx[ib + 2]) > tolerance):
                changed += 1
                if x < min_x:
                    min_x = x
                if y < min_y:
                    min_y = y
                if x > max_x:
                    max_x = x
                if y > max_y:
                    max_y = y

    total = aw * ah
    pct = (changed / total * 100.0) if total else 0.0
    bbox = None
    if max_x >= 0:
        bbox = [min_x, min_y, max_x - min_x + 1, max_y - min_y + 1]
    return {
        "status": "changed" if changed else "same",
        "changed_pixels": changed,
        "total_pixels": total,
        "changed_pct": round(pct, 3),
        "bbox": bbox,
    }
