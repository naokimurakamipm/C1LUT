"""Generate the C-One LUT application icon (art/COneLUT.ico + preview PNG).

Draws an isometric RGB cube - the 3D LUT - on a dark rounded square.
Run:  python art/make_icon.py   (requires Pillow)
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw

OUT = Path(__file__).with_name("COneLUT.ico")
PREVIEW = Path(__file__).with_name("COneLUT_preview.png")

BG = (18, 22, 40, 255)      # dark navy
EDGE = (10, 13, 26, 255)    # outline
TOP = (255, 94, 94, 255)    # red face
LEFT = (46, 204, 113, 255)  # green face
RIGHT = (77, 140, 255, 255) # blue face

S = 1024  # supersampled master, downscaled for every icon size


def build_master() -> Image.Image:
    image = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle((36, 36, S - 36, S - 36), radius=230, fill=BG)

    # Isometric cube centred in the canvas.
    half_w, half_h, drop = 292, 190, 310
    top = 167
    t = (S // 2, top)
    r = (S // 2 + half_w, top + half_h)
    m = (S // 2, top + 2 * half_h)
    left = (S // 2 - half_w, top + half_h)
    left_b = (left[0], left[1] + drop)
    m_b = (m[0], m[1] + drop)
    r_b = (r[0], r[1] + drop)

    draw.polygon([t, r, m, left], fill=TOP, outline=EDGE, width=16)
    draw.polygon([left, m, m_b, left_b], fill=LEFT, outline=EDGE, width=16)
    draw.polygon([m, r, r_b, m_b], fill=RIGHT, outline=EDGE, width=16)
    return image


def main() -> None:
    master = build_master()
    base = master.resize((256, 256), Image.LANCZOS)
    base.save(OUT, format="ICO",
              sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
    base.save(PREVIEW)
    print(f"wrote {OUT} and {PREVIEW}")


if __name__ == "__main__":
    main()
