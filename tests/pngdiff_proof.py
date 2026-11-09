#!/usr/bin/env python3
"""Deterministic tests for pngdiff. No browser, no network, no fixtures on disk beforehand.

Every PNG here is generated in this file with a chosen filter type, so the decoder is tested
against all five PNG filter modes rather than against whatever Chrome happened to emit.
"""

from __future__ import annotations

import json
import struct
import sys
import tempfile
import zlib
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
import pngdiff  # noqa: E402

CHECKS: list[tuple[str, bool, str]] = []
TMP = Path(tempfile.mkdtemp(prefix="pngdiff-proof-"))


def ok(name: str, condition: bool, detail: str = "") -> None:
    CHECKS.append((name, bool(condition), detail))


# --------------------------------------------------------------------------- fixtures


def _raw_rows(pixels: bytes, width: int, channels: int) -> list[bytes]:
    stride = width * channels
    return [pixels[y * stride:(y + 1) * stride] for y in range(len(pixels) // stride)]


def _filtered(rows: list[bytes], channels: int, filter_type: int) -> bytes:
    out = bytearray()
    prev = bytes(len(rows[0]))
    for row in rows:
        out.append(filter_type)
        for i, value in enumerate(row):
            left = row[i - channels] if i >= channels else 0
            up = prev[i]
            up_left = prev[i - channels] if i >= channels else 0
            if filter_type == 0:
                out.append(value)
            elif filter_type == 1:
                out.append((value - left) & 0xFF)
            elif filter_type == 2:
                out.append((value - up) & 0xFF)
            elif filter_type == 3:
                out.append((value - ((left + up) >> 1)) & 0xFF)
            else:
                p = left + up - up_left
                pa, pb, pc = abs(p - left), abs(p - up), abs(p - up_left)
                pred = left if (pa <= pb and pa <= pc) else (up if pb <= pc else up_left)
                out.append((value - pred) & 0xFF)
        prev = row
    return bytes(out)


def make_png(path: Path, width: int, height: int, pixels: bytes, channels: int = 3,
             filter_type: int = 0, colour_type: int | None = None) -> Path:
    rows = _raw_rows(pixels, width, channels)
    body = _filtered(rows, channels, filter_type)
    colour = colour_type if colour_type is not None else (2 if channels == 3 else 6)

    def chunk(kind: bytes, data: bytes) -> bytes:
        return (struct.pack(">I", len(data)) + kind + data
                + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF))

    png = pngdiff.PNG_SIG
    png += chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, colour, 0, 0, 0))
    png += chunk(b"IDAT", zlib.compress(body, 6))
    png += chunk(b"IEND", b"")
    path.write_bytes(png)
    return path


def solid(width: int, height: int, rgb: tuple[int, int, int], channels: int = 3) -> bytearray:
    return bytearray(bytes(rgb) * (width * height)) if channels == 3 else \
        bytearray(bytes(rgb + (255,)) * (width * height))


def put(px: bytearray, width: int, x: int, y: int, rgb: tuple[int, int, int], channels: int = 3) -> None:
    offset = (y * width + x) * channels
    px[offset:offset + 3] = bytes(rgb)


# --------------------------------------------------------------------------- checks


def main() -> int:
    w, h = 40, 30

    # 1. round trip on every filter type
    base = solid(w, h, (250, 250, 250))
    for x in range(0, w, 7):
        put(base, w, x, 5, (10, 20, 30))
    for kind in (0, 1, 2, 3, 4):
        path = make_png(TMP / f"filter{kind}.png", w, h, base, filter_type=kind)
        dw, dh, dc, px = pngdiff.decode(path)
        ok(f"decode filter {kind}", (dw, dh, dc) == (w, h, 3) and bytes(px) == bytes(base),
           f"{dw}x{dh} ch={dc}")

    # 2. RGBA decodes and compares on RGB channels only
    rgba = solid(w, h, (200, 100, 50), channels=4)
    rgba_path = make_png(TMP / "rgba.png", w, h, rgba, channels=4, filter_type=1)
    rgb_path = make_png(TMP / "rgb-same.png", w, h, solid(w, h, (200, 100, 50)))
    res = pngdiff.compare(rgb_path, rgba_path)
    ok("RGB vs RGBA of the same colour matches", res["changed_pixels"] == 0, res["status"])

    # 3. identical images
    a = make_png(TMP / "a.png", w, h, base)
    b = make_png(TMP / "b.png", w, h, base)
    same = pngdiff.compare(a, b)
    ok("identical images report same", same["status"] == "same" and same["changed_pct"] == 0.0,
       f"{same['changed_pct']}%")

    # 4. single pixel
    one = bytearray(base)
    put(one, w, 17, 11, (255, 0, 0))
    single = pngdiff.compare(a, make_png(TMP / "single.png", w, h, one))
    ok("single pixel change is one region of one pixel",
       single["changed_pixels"] == 1 and len(single["regions"]) == 1
       and single["regions"][0]["bbox"] == [17, 11, 1, 1],
       json.dumps(single["regions"]))

    # 5. disconnected regions
    multi = bytearray(base)
    for (x, y) in ((2, 2), (30, 2), (2, 25)):
        put(multi, w, x, y, (0, 0, 0))
        put(multi, w, x, y + 1, (0, 0, 0))
    many = pngdiff.compare(a, make_png(TMP / "multi.png", w, h, multi))
    ok("three disconnected changes become three regions", len(many["regions"]) == 3,
       f"{len(many['regions'])} regions, largest {many['regions'][0]['bbox']}")

    # 6. boundary bbox
    edge = bytearray(base)
    put(edge, w, 0, 0, (0, 0, 0))
    put(edge, w, w - 1, h - 1, (0, 0, 0))
    corners = pngdiff.compare(a, make_png(TMP / "edges.png", w, h, edge))
    boxes = sorted(r["bbox"] for r in corners["regions"])
    ok("corner pixels keep a bbox inside the image",
       boxes == [[0, 0, 1, 1], [w - 1, h - 1, 1, 1]], json.dumps(boxes))

    # 7. masks
    masked = pngdiff.compare(a, make_png(TMP / "single2.png", w, h, one), masks=[[10, 5, 20, 20]])
    ok("mask covers the changed pixel", masked["status"] == "same", f"{masked['changed_pct']}%")

    # 8. dimension mismatch
    small = make_png(TMP / "small.png", 10, 10, solid(10, 10, (250, 250, 250)))
    mismatch = pngdiff.compare(a, small)
    ok("dimension mismatch is reported, not compared",
       mismatch["status"] == "fail" and "size changed" in mismatch["reason"], mismatch["reason"])

    # 9. corrupted inputs
    bad_sig = TMP / "bad-signature.png"
    bad_sig.write_bytes(b"GIF89a" + b"0" * 64)
    try:
        pngdiff.decode(bad_sig)
        ok("bad signature raises", False)
    except pngdiff.PngError as exc:
        ok("bad signature raises", True, str(exc)[:40])

    truncated = TMP / "truncated.png"
    truncated.write_bytes(a.read_bytes()[:40])
    try:
        pngdiff.decode(truncated)
        ok("truncated file raises", False)
    except pngdiff.PngError as exc:
        ok("truncated file raises", True, str(exc)[:40])

    broken_idat = TMP / "broken-idat.png"
    broken_idat.write_bytes(pngdiff.PNG_SIG
                            + struct.pack(">I", 13) + b"IHDR"
                            + struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)
                            + struct.pack(">I", zlib.crc32(b"IHDR" + struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)) & 0xFFFFFFFF)
                            + struct.pack(">I", 8) + b"IDAT" + b"\x00\x01\x02\x03\x04\x05\x06\x07"
                            + struct.pack(">I", 0) + b"IEND" + struct.pack(">I", 0))
    try:
        pngdiff.decode(broken_idat)
        ok("broken IDAT raises", False)
    except pngdiff.PngError as exc:
        ok("broken IDAT raises", True, str(exc)[:40])

    # 10. unsupported format is named, not guessed
    palette = TMP / "palette.png"
    palette.write_bytes(pngdiff.PNG_SIG
                        + struct.pack(">I", 13) + b"IHDR"
                        + struct.pack(">IIBBBBB", 4, 4, 4, 3, 0, 0, 0)
                        + struct.pack(">I", zlib.crc32(b"IHDR" + struct.pack(">IIBBBBB", 4, 4, 4, 3, 0, 0, 0)) & 0xFFFFFFFF)
                        + struct.pack(">I", 0) + b"IDAT"
                        + struct.pack(">I", 0) + b"IEND" + struct.pack(">I", 0))
    try:
        pngdiff.decode(palette)
        ok("unsupported colour type raises", False)
    except pngdiff.PngError as exc:
        ok("unsupported colour type raises", True, str(exc)[:60])

    # 11. deterministic JSON: the same pair twice produces the same report
    first = pngdiff.compare(a, make_png(TMP / "multi2.png", w, h, multi),
                            artefacts={"dir": str(TMP / "art")})
    second = pngdiff.compare(a, make_png(TMP / "multi3.png", w, h, multi),
                             artefacts={"dir": str(TMP / "art")})
    first.pop("artifacts", None)
    second.pop("artifacts", None)
    ok("reports are byte-identical for identical inputs",
       json.dumps(first, sort_keys=True) == json.dumps(second, sort_keys=True),
       f"{first['changed_pct']}% both times")

    # 12. artefacts exist and are readable PNGs
    arts = pngdiff.compare(a, make_png(TMP / "multi4.png", w, h, multi),
                           artefacts={"dir": str(TMP / "art4")})["artifacts"]
    readable = []
    for key in ("diff", "overlay"):
        if arts.get(key):
            dw, dh, _dc, _px = pngdiff.decode(arts[key])
            readable.append((key, dw, dh))
    crops = [pngdiff.decode(p)[:2] for p in arts.get("regions", [])]
    ok("diff, overlay and region crops are valid PNGs",
       len(readable) == 2 and crops and all(dim == (w, h) for _, dim in
                                            [("diff", readable[0][1:]), ("overlay", readable[1][1:])]),
       f"{readable} crops={crops[:3]}")

    passed = sum(1 for _, good, _ in CHECKS if good)
    print()
    for name, good, detail in CHECKS:
        print(f"  [{'PASS' if good else 'FAIL'}] {name}" + (f"  ({detail})" if detail else ""))
    print(f"\n{passed}/{len(CHECKS)} checks passed")
    return 0 if passed == len(CHECKS) else 1


if __name__ == "__main__":
    sys.exit(main())
