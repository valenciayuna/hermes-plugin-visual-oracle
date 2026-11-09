#!/usr/bin/env python3
"""Code audit for pngdiff and the contract.

The point of this file is distrust: an independent, deliberately naive reference implementation
compares against the shipped one on generated images, so a wrong pixel count or a lazy bounding box
shows up as a mismatch instead of a green tick.
"""

from __future__ import annotations

import json
import random
import struct
import sys
import tempfile
import zlib
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
import pngdiff  # noqa: E402

TMP = Path(tempfile.mkdtemp(prefix="pngdiff-audit-"))
CHECKS: list[tuple[str, bool, str]] = []


def ok(name: str, condition: bool, detail: str = "") -> None:
    CHECKS.append((name, bool(condition), detail))


def write_png(path: Path, width: int, height: int, pixels: bytes, channels: int = 3,
              filter_type: int = 0) -> Path:
    stride = width * channels
    raw = bytearray()
    prev = bytes(stride)
    for y in range(height):
        row = pixels[y * stride:(y + 1) * stride]
        raw.append(filter_type)
        for i, value in enumerate(row):
            left = row[i - channels] if i >= channels else 0
            up = prev[i]
            up_left = prev[i - channels] if i >= channels else 0
            if filter_type == 0:
                raw.append(value)
            elif filter_type == 1:
                raw.append((value - left) & 0xFF)
            elif filter_type == 2:
                raw.append((value - up) & 0xFF)
            elif filter_type == 3:
                raw.append((value - ((left + up) >> 1)) & 0xFF)
            else:
                p = left + up - up_left
                pa, pb, pc = abs(p - left), abs(p - up), abs(p - up_left)
                pred = left if (pa <= pb and pa <= pc) else (up if pb <= pc else up_left)
                raw.append((value - pred) & 0xFF)
        prev = row

    def chunk(kind: bytes, body: bytes) -> bytes:
        return (struct.pack(">I", len(body)) + kind + body
                + struct.pack(">I", zlib.crc32(kind + body) & 0xFFFFFFFF))

    colour = 2 if channels == 3 else 6
    data = pngdiff.PNG_SIG
    data += chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, colour, 0, 0, 0))
    data += chunk(b"IDAT", zlib.compress(bytes(raw), 6))
    data += chunk(b"IEND", b"")
    path.write_bytes(data)
    return path


def reference_diff(a: bytes, b: bytes, width: int, height: int, tolerance: int = 8,
                   masks: list[list[int]] | None = None) -> tuple[int, list[int] | None]:
    """Deliberately naive: no blocks, no grouping. Just count and the outer box."""
    masks = masks or []
    changed = 0
    min_x = min_y = 10 ** 9
    max_x = max_y = -1
    for y in range(height):
        for x in range(width):
            if any(mx <= x < mx + mw and my <= y < my + mh for mx, my, mw, mh in masks):
                continue
            i = (y * width + x) * 3
            if (abs(a[i] - b[i]) > tolerance or abs(a[i + 1] - b[i + 1]) > tolerance
                    or abs(a[i + 2] - b[i + 2]) > tolerance):
                changed += 1
                min_x = min(min_x, x)
                min_y = min(min_y, y)
                max_x = max(max_x, x)
                max_y = max(max_y, y)
    if max_x < 0:
        return 0, None
    return changed, [min_x, min_y, max_x - min_x + 1, max_y - min_y + 1]


def union_bbox(regions: list[dict]) -> list[int] | None:
    if not regions:
        return None
    x0 = min(r["bbox"][0] for r in regions)
    y0 = min(r["bbox"][1] for r in regions)
    x1 = max(r["bbox"][0] + r["bbox"][2] for r in regions)
    y1 = max(r["bbox"][1] + r["bbox"][3] for r in regions)
    return [x0, y0, x1 - x0, y1 - y0]


def main() -> int:
    rng = random.Random(20261109)
    width, height = 96, 72

    # 1. random images, shipped diff against the naive reference
    mismatches = 0
    for trial in range(12):
        base = bytearray(rng.randrange(256) for _ in range(width * height * 3))
        other = bytearray(base)
        for _ in range(rng.randrange(1, 40)):
            x, y = rng.randrange(width), rng.randrange(height)
            i = (y * width + x) * 3
            other[i:i + 3] = bytes((rng.randrange(256), rng.randrange(256), rng.randrange(256)))
        a = write_png(TMP / f"r{trial}-a.png", width, height, bytes(base))
        b = write_png(TMP / f"r{trial}-b.png", width, height, bytes(other))
        got = pngdiff.compare(a, b)
        pixels, box = reference_diff(bytes(base), bytes(other), width, height)
        if got["changed_pixels"] != pixels or union_bbox(got["regions"]) != box:
            mismatches += 1
    ok("random images match the naive reference (pixels and union bbox)", mismatches == 0,
       f"{12 - mismatches}/12 trials agreed")

    # 2. bbox is exact for a known rectangle
    base = bytearray(b"\xf0" * (width * height * 3))
    other = bytearray(base)
    for y in range(20, 36):
        for x in range(10, 34):
            i = (y * width + x) * 3
            other[i:i + 3] = b"\x00\x00\x00"
    a = write_png(TMP / "rect-a.png", width, height, bytes(base))
    b = write_png(TMP / "rect-b.png", width, height, bytes(other))
    got = pngdiff.compare(a, b)
    ok("known rectangle reports the exact bbox and area",
       got["regions"][0]["bbox"] == [10, 20, 24, 16] and got["changed_pixels"] == 24 * 16,
       f"{got['regions'][0]['bbox']} {got['changed_pixels']}px")

    # 3. mask edge: inside is ignored, one pixel outside is not
    inside = pngdiff.compare(a, b, masks=[[0, 0, 96, 72]])
    ok("a mask covering the rectangle hides it", inside["changed_pixels"] == 0, inside["status"])
    narrow = pngdiff.compare(a, b, masks=[[10, 20, 23, 16]])   # one column short
    ok("a mask one column short still reports the change", narrow["changed_pixels"] == 16,
       f"{narrow['changed_pixels']}px")

    # 4. tolerance boundary: 8 is the same colour, 9 is not
    soft = bytearray(base)
    i = (10 * width + 10) * 3
    soft[i:i + 3] = bytes((0xF0 - 8, 0xF0 - 8, 0xF0 - 8))
    t1 = pngdiff.compare(a, write_png(TMP / "tol8.png", width, height, bytes(soft)))
    hard = bytearray(base)
    hard[i:i + 3] = bytes((0xF0 - 9, 0xF0 - 9, 0xF0 - 9))
    t2 = pngdiff.compare(a, write_png(TMP / "tol9.png", width, height, bytes(hard)))
    ok("tolerance is exactly 8 per channel", t1["changed_pixels"] == 0 and t2["changed_pixels"] == 1,
       f"8->{t1['changed_pixels']}px 9->{t2['changed_pixels']}px")

    # 5. determinism of the whole report, including region order
    first = json.dumps(pngdiff.compare(a, b), sort_keys=True)
    second = json.dumps(pngdiff.compare(a, b), sort_keys=True)
    ok("report is byte-identical across runs", first == second)

    # 6. an all-black vs all-white page: one region, full frame
    black = write_png(TMP / "black.png", width, height, b"\x00" * (width * height * 3))
    white = write_png(TMP / "white.png", width, height, b"\xff" * (width * height * 3))
    full = pngdiff.compare(black, white)
    ok("full-page change is one region covering the frame",
       len(full["regions"]) == 1 and full["regions"][0]["bbox"] == [0, 0, width, height]
       and full["changed_pct"] == 100.0, f"{full['regions'][0]['bbox']} {full['changed_pct']}%")

    # 7. corrupted inputs never return a verdict
    cases = {}
    bad = TMP / "sig.png"; bad.write_bytes(b"GIF89a" + b"0" * 32); cases["signature"] = bad
    trunc = TMP / "trunc.png"; trunc.write_bytes(a.read_bytes()[:48]); cases["truncated"] = trunc
    empty = TMP / "empty.png"; empty.write_bytes(b""); cases["empty"] = empty
    garbage = TMP / "garbage.png"; garbage.write_bytes(pngdiff.PNG_SIG + b"\x00" * 64)
    cases["garbage chunks"] = garbage
    for name, path in cases.items():
        try:
            pngdiff.compare(a, path)
            ok(f"corrupt input ({name}) raises", False)
        except pngdiff.PngError as exc:
            ok(f"corrupt input ({name}) raises", True, str(exc)[:42])

    passed = sum(1 for _, good, _ in CHECKS if good)
    print()
    for name, good, detail in CHECKS:
        print(f"  [{'PASS' if good else 'FAIL'}] {name}" + (f"  ({detail})" if detail else ""))
    print(f"\n{passed}/{len(CHECKS)} audit checks passed")
    return 0 if passed == len(CHECKS) else 1


if __name__ == "__main__":
    sys.exit(main())
