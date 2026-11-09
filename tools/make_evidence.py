"""Compose the evidence image: baseline crop | current crop | changed pixels highlighted."""
import struct
import sys
import zlib
from pathlib import Path

sys.path.insert(0, "/root/hackathon/hermes-plugin-visual-oracle")
import pngdiff


def write_png(path: Path, width: int, height: int, rgb: bytearray) -> None:
    raw = bytearray()
    stride = width * 3
    for y in range(height):
        raw.append(0)
        raw += rgb[y * stride:(y + 1) * stride]

    def chunk(kind: bytes, body: bytes) -> bytes:
        return (struct.pack(">I", len(body)) + kind + body
                + struct.pack(">I", zlib.crc32(kind + body) & 0xFFFFFFFF))

    png = pngdiff.PNG_SIG
    png += chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
    png += chunk(b"IDAT", zlib.compress(bytes(raw), 6))
    png += chunk(b"IEND", b"")
    path.write_bytes(png)


def crop(px: bytes, ch: int, w: int, x: int, y: int, cw: int, chh: int, scale: int) -> bytearray:
    out = bytearray(cw * scale * chh * scale * 3)
    for row in range(chh * scale):
        sy = y + row // scale
        for col in range(cw * scale):
            sx = x + col // scale
            src = (sy * w + sx) * ch
            dst = (row * cw * scale + col) * 3
            out[dst:dst + 3] = px[src:src + 3]
    return out


def main() -> None:
    base = Path(sys.argv[1])
    cur = Path(sys.argv[2])
    bbox = [int(v) for v in sys.argv[3].split(",")]
    out = Path(sys.argv[4])

    bw, bh, bc, bpx = pngdiff.decode(str(base))
    cw_, chh_, cc, cpx = pngdiff.decode(str(cur))

    pad, scale, gap = 30, 3, 12
    x0 = max(0, bbox[0] - pad)
    y0 = max(0, bbox[1] - pad)
    w = min(bw - x0, bbox[2] + pad * 2)
    h = min(bh - y0, bbox[3] + pad * 2)

    left = crop(bpx, bc, bw, x0, y0, w, h, scale)
    right = crop(cpx, cc, cw_, x0, y0, w, h, scale)

    # highlight the pixels that differ, in red, on top of the current crop
    diff = bytearray(right)
    for row in range(h * scale):
        sy = y0 + row // scale
        for col in range(w * scale):
            sx = x0 + col // scale
            ia = (sy * bw + sx) * bc
            ib = (sy * cw_ + sx) * cc
            if (abs(bpx[ia] - cpx[ib]) > 8 or abs(bpx[ia + 1] - cpx[ib + 1]) > 8
                    or abs(bpx[ia + 2] - cpx[ib + 2]) > 8):
                dst = (row * w * scale + col) * 3
                diff[dst:dst + 3] = bytes((230, 40, 40))

    panel_w = w * scale
    total_w = panel_w * 3 + gap * 2
    total_h = h * scale
    canvas = bytearray(b"\x18\x18\x18" * (total_w * total_h))

    def blit(panel: bytearray, offset_x: int) -> None:
        for row in range(total_h):
            src = row * panel_w * 3
            dst = (row * total_w + offset_x) * 3
            canvas[dst:dst + panel_w * 3] = panel[src:src + panel_w * 3]

    blit(left, 0)
    blit(right, panel_w + gap)
    blit(diff, (panel_w + gap) * 2)

    write_png(out, total_w, total_h, canvas)
    print(f"{out}  {total_w}x{total_h}  panels: baseline | current | changed-pixels")


if __name__ == "__main__":
    main()
