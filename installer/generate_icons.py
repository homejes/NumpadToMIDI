"""Generate high-resolution app icons (assets/icon.png, assets/icon.ico, assets/icon.icns)."""
from pathlib import Path
import struct
import sys

from PyQt6.QtCore import QBuffer, QByteArray, QIODevice, QRectF, Qt
from PyQt6.QtGui import QColor, QGuiApplication, QIcon, QImage, QPainter, QPen


def render_grid_png(size: int) -> bytes:
    """Render the 3x3 colorful pad-grid icon at `size` x `size` as PNG bytes."""
    img = QImage(size, size, QImage.Format.Format_ARGB32_Premultiplied)
    img.fill(Qt.GlobalColor.transparent)

    s = float(size) / 64.0
    p = QPainter(img)
    p.setRenderHint(QPainter.RenderHint.Antialiasing, True)

    # Outer dark rounded container with vibrant purple border
    p.setBrush(QColor("#161a24"))
    pen = QPen(QColor("#7c5cff"))
    pen.setWidthF(max(1.0, 3.0 * s))
    p.setPen(pen)
    outer = QRectF(4.0 * s, 4.0 * s, 56.0 * s, 56.0 * s)
    p.drawRoundedRect(outer, 14.0 * s, 14.0 * s)

    # 3x3 colorful pad grid
    colors = ("#7c5cff", "#3ddc84", "#ffb454", "#ff6b81")
    for idx in range(9):
        r, c = divmod(idx, 3)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(colors[(r + c) % len(colors)]))
        cell = QRectF(
            (13.0 + c * 14.0) * s,
            (13.0 + r * 14.0) * s,
            10.0 * s,
            10.0 * s,
        )
        p.drawRoundedRect(cell, 3.0 * s, 3.0 * s)
    p.end()

    ba = QByteArray()
    buf = QBuffer(ba)
    buf.open(QIODevice.OpenModeFlag.WriteOnly)
    img.save(buf, "PNG")
    buf.close()
    return bytes(ba)


def build_ico(sizes: tuple[int, ...] = (16, 32, 48, 64, 128, 256)) -> bytes:
    frames = [(sz, render_grid_png(sz)) for sz in sizes]
    header = struct.pack("<HHH", 0, 1, len(frames))
    offset = 6 + 16 * len(frames)
    entries = []
    blobs = []
    for sz, png in frames:
        w = 0 if sz >= 256 else sz
        h = 0 if sz >= 256 else sz
        entries.append(struct.pack("<BBBBHHII", w, h, 0, 0, 1, 32, len(png), offset))
        blobs.append(png)
        offset += len(png)
    return header + b"".join(entries) + b"".join(blobs)


def build_icns() -> bytes:
    chunk_types = [
        (b"icp4", 16),
        (b"icp5", 32),
        (b"icp6", 64),
        (b"ic07", 128),
        (b"ic08", 256),
        (b"ic09", 512),
        (b"ic10", 1024),
    ]
    chunks = []
    for ostype, sz in chunk_types:
        png = render_grid_png(sz)
        chunks.append(ostype + struct.pack(">I", 8 + len(png)) + png)
    body = b"".join(chunks)
    return b"icns" + struct.pack(">I", 8 + len(body)) + body


def main() -> None:
    app = QGuiApplication.instance() or QGuiApplication(sys.argv)
    assets = Path(__file__).resolve().parents[1] / "assets"
    assets.mkdir(parents=True, exist_ok=True)

    png_512 = render_grid_png(512)
    (assets / "icon.png").write_bytes(png_512)
    (assets / "icon.ico").write_bytes(build_ico((16, 32, 48, 64, 128, 256)))
    (assets / "icon.icns").write_bytes(build_icns())

    for name in ("icon.png", "icon.ico", "icon.icns"):
        p = assets / name
        print(f"Generated {p} ({p.stat().st_size} bytes)")

    ico_icon = QIcon(str(assets / "icon.ico"))
    png_icon = QIcon(str(assets / "icon.png"))
    assert not ico_icon.isNull(), "icon.ico failed to load in QIcon"
    assert not png_icon.isNull(), "icon.png failed to load in QIcon"
    print("Available ICO sizes in QIcon:", [(s.width(), s.height()) for s in ico_icon.availableSizes()])
    _ = app


if __name__ == "__main__":
    main()
