"""PNG decode, pixel diff, regions, and the artefacts that make a diff inspectable.

Standard library only: zlib + struct. Supports 8-bit RGB (colour type 2) and RGBA (6), non-interlaced,
which is exactly what Chrome emits. Everything else is refused with a named error instead of being
guessed at.

The diff is deterministic: same inputs, same regions, same bounding boxes, same percentages. Regions
come from a block grid (8x8) so a page with three separate changes reports three boxes, not one box
that swallows the whole screen. Each region keeps its own pixel count, area and density, and can be
cropped to its own PNG.
"""

from __future__ import annotations

import struct
import zlib
from pathlib import Path

PNG_SIG = b"\x89PNG\r\n\x1a\n"
CHANNELS = {2: 3, 6: 4}
SUPPORTED = "8-bit RGB/RGBA, non-interlaced"
BLOCK = 8            # grid cell used to group changed pixels into regions
TOLERANCE = 8        # per-channel delta still treated as the same colour


class PngError(ValueError):
    """The file is not a PNG this module can read."""


def _paeth(a: int, b: int, c: int) -> int:
    p = a + b - c
    pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
    if pa <= pb and pa <= pc:
        return a
    return b if pb <= pc else c


def decode(path: str | Path) -> tuple[int, int, int, bytes]:
    """Return (width, height, channels, pixels) for a supported PNG."""
    raw = Path(path).read_bytes()
    if not raw.startswith(PNG_SIG):
        raise PngError("not a PNG (bad signature)")

    pos = len(PNG_SIG)
    width = height = depth = colour = interlace = None
    idat = bytearray()
    seen_end = False
    while pos + 8 <= len(raw):
        length, kind = struct.unpack(">I4s", raw[pos:pos + 8])
        if pos + 12 + length > len(raw):
            raise PngError(f"truncated chunk {kind!r}")
        body = raw[pos + 8:pos + 8 + length]
        pos += 12 + length
        if kind == b"IHDR":
            if length != 13:
                raise PngError("malformed IHDR")
            width, height, depth, colour, _c, _f, interlace = struct.unpack(">IIBBBBB", body)
            if depth != 8 or colour not in CHANNELS or interlace != 0:
                raise PngError(
                    f"unsupported PNG (depth={depth}, colour={colour}, interlace={interlace}); "
                    f"this plugin reads {SUPPORTED}")
        elif kind == b"IDAT":
            idat += body
        elif kind == b"IEND":
            seen_end = True
            break

    if width is None:
        raise PngError("no IHDR chunk")
    if not idat:
        raise PngError("no IDAT chunk")
    if not seen_end:
        raise PngError("no IEND chunk (incomplete file)")

    channels = CHANNELS[colour]
    stride = width * channels
    try:
        data = zlib.decompress(bytes(idat))
    except zlib.error as exc:
        raise PngError(f"bad IDAT stream: {exc}") from None
    if len(data) < stride * height:
        raise PngError("pixel data is shorter than the header claims")

    out = bytearray(stride * height)
    prev = bytearray(stride)
    src = 0
    for y in range(height):
        filter_type = data[src]
        src += 1
        line = bytearray(data[src:src + stride])
        src += stride
        if filter_type == 1:
            for i in range(channels, stride):
                line[i] = (line[i] + line[i - channels]) & 0xFF
        elif filter_type == 2:
            for i in range(stride):
                line[i] = (line[i] + prev[i]) & 0xFF
        elif filter_type == 3:
            for i in range(stride):
                left = line[i - channels] if i >= channels else 0
                line[i] = (line[i] + ((left + prev[i]) >> 1)) & 0xFF
        elif filter_type == 4:
            for i in range(stride):
                left = line[i - channels] if i >= channels else 0
                up_left = prev[i - channels] if i >= channels else 0
                line[i] = (line[i] + _paeth(left, prev[i], up_left)) & 0xFF
        elif filter_type != 0:
            raise PngError(f"bad filter type {filter_type} on row {y}")
        out[y * stride:(y + 1) * stride] = line
        prev = line
    return width, height, channels, bytes(out)


def encode(path: str | Path, width: int, height: int, rgb: bytearray) -> None:
    """Write a plain RGB PNG (filter 0), used for diff, overlay and crops."""
    stride = width * 3
    raw = bytearray()
    for y in range(height):
        raw.append(0)
        raw += rgb[y * stride:(y + 1) * stride]

    def chunk(kind: bytes, body: bytes) -> bytes:
        return (struct.pack(">I", len(body)) + kind + body
                + struct.pack(">I", zlib.crc32(kind + body) & 0xFFFFFFFF))

    png = PNG_SIG
    png += chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
    png += chunk(b"IDAT", zlib.compress(bytes(raw), 6))
    png += chunk(b"IEND", b"")
    Path(path).write_bytes(png)


def _masked(x: int, y: int, masks: list[list[int]]) -> bool:
    for mx, my, mw, mh in masks:
        if mx <= x < mx + mw and my <= y < my + mh:
            return True
    return False


def _pixel_differs(a: bytes, b: bytes, ia: int, ib: int, tolerance: int) -> bool:
    return (abs(a[ia] - b[ib]) > tolerance
            or abs(a[ia + 1] - b[ib + 1]) > tolerance
            or abs(a[ia + 2] - b[ib + 2]) > tolerance)


def _regions(changed: set[tuple[int, int]], width: int, height: int) -> list[dict]:
    """Group changed pixels into independent regions using an 8x8 block grid."""
    if not changed:
        return []
    blocks: dict[tuple[int, int], list[int]] = {}
    for x, y in changed:
        key = (x // BLOCK, y // BLOCK)
        cell = blocks.get(key)
        if cell is None:
            blocks[key] = [x, y, x, y, 1]
        else:
            cell[0] = min(cell[0], x)
            cell[1] = min(cell[1], y)
            cell[2] = max(cell[2], x)
            cell[3] = max(cell[3], y)
            cell[4] += 1

    # union adjacent blocks (4-connected) so one moved element stays one region
    seen: set[tuple[int, int]] = set()
    regions: list[dict] = []
    for key in sorted(blocks):
        if key in seen:
            continue
        stack = [key]
        seen.add(key)
        group: list[tuple[int, int]] = []
        while stack:
            current = stack.pop()
            group.append(current)
            cx, cy = current
            for neighbour in ((cx + 1, cy), (cx - 1, cy), (cx, cy + 1), (cx, cy - 1)):
                if neighbour in blocks and neighbour not in seen:
                    seen.add(neighbour)
                    stack.append(neighbour)

        min_x = min(blocks[b][0] for b in group)
        min_y = min(blocks[b][1] for b in group)
        max_x = max(blocks[b][2] for b in group)
        max_y = max(blocks[b][3] for b in group)
        pixels = sum(blocks[b][4] for b in group)
        box_w = max_x - min_x + 1
        box_h = max_y - min_y + 1
        area = box_w * box_h
        regions.append({
            "bbox": [min_x, min_y, box_w, box_h],
            "changed_pixels": pixels,
            "area": area,
            "density": round(pixels / area, 3) if area else 0.0,
        })

    regions.sort(key=lambda r: (-r["changed_pixels"], r["bbox"][1], r["bbox"][0]))
    return regions


def compare(a_path: str | Path, b_path: str | Path, tolerance: int = TOLERANCE,
            masks: list[list[int]] | None = None,
            artefacts: dict | None = None) -> dict:
    """Pixel-compare two PNGs.

    Returns changed_pct, regions (independent boxes with density), and, when `artefacts` names
    paths, writes diff / overlay / per-region crops there.
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
            "regions": [],
            "artifacts": {},
        }

    total = aw * ah
    changed: set[tuple[int, int]] = set()
    for y in range(ah):
        row_a = y * aw * ac
        row_b = y * aw * bc
        for x in range(aw):
            if masks and _masked(x, y, masks):
                continue
            if _pixel_differs(apx, bpx, row_a + x * ac, row_b + x * bc, tolerance):
                changed.add((x, y))

    regions = _regions(changed, aw, ah)
    pct = round(len(changed) / total * 100.0, 3) if total else 0.0
    result = {
        "status": "changed" if changed else "same",
        "changed_pixels": len(changed),
        "total_pixels": total,
        "changed_pct": pct,
        "regions": regions,
        "bbox": regions[0]["bbox"] if regions else None,
        "artifacts": {},
    }

    if artefacts and changed:
        result["artifacts"] = _write_artefacts(artefacts, apx, bpx, ac, bc, aw, ah, changed, regions)
    return result


def _write_artefacts(targets: dict, apx: bytes, bpx: bytes, ac: int, bc: int,
                     width: int, height: int, changed: set[tuple[int, int]],
                     regions: list[dict]) -> dict:
    Path(targets["dir"]).mkdir(parents=True, exist_ok=True)
    written: dict = {}

    diff = bytearray(width * height * 3)
    overlay = bytearray(width * height * 3)
    for y in range(height):
        for x in range(width):
            ia = (y * width + x) * ac
            ib = (y * width + x) * bc
            dst = (y * width + x) * 3
            hit = (x, y) in changed
            if hit:
                diff[dst:dst + 3] = bytes((230, 40, 40))
            else:
                grey = (bpx[ib] + bpx[ib + 1] + bpx[ib + 2]) // 6
                diff[dst:dst + 3] = bytes((grey, grey, grey))
            overlay[dst:dst + 3] = bytes((
                (apx[ia] + bpx[ib]) // 2,
                (apx[ia + 1] + bpx[ib + 1]) // 2,
                (apx[ia + 2] + bpx[ib + 2]) // 2,
            ))

    diff_path = Path(targets["dir"]) / "diff.png"
    overlay_path = Path(targets["dir"]) / "overlay.png"
    encode(diff_path, width, height, diff)
    encode(overlay_path, width, height, overlay)
    written["diff"] = str(diff_path)
    written["overlay"] = str(overlay_path)

    crops = []
    for index, region in enumerate(regions[:5]):
        x, y, w, h = region["bbox"]
        pad = 6
        x0, y0 = max(0, x - pad), max(0, y - pad)
        x1, y1 = min(width, x + w + pad), min(height, y + h + pad)
        cw, ch = x1 - x0, y1 - y0
        crop = bytearray(cw * ch * 3)
        for row in range(ch):
            for col in range(cw):
                src = ((y0 + row) * width + (x0 + col)) * bc
                dst = (row * cw + col) * 3
                crop[dst:dst + 3] = bpx[src:src + 3]
        path = Path(targets["dir"]) / f"region-{index}.png"
        encode(path, cw, ch, crop)
        crops.append(str(path))
    if crops:
        written["regions"] = crops
    return written
