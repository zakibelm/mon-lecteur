"""Génère les icônes PNG de la PWA (192 et 512 px). Usage : python tools/make_icons.py"""
from pathlib import Path

from PIL import Image, ImageDraw

out = Path(__file__).resolve().parent.parent / "static"
for size in (192, 512):
    img = Image.new("RGB", (size, size), "#1f2937")
    d = ImageDraw.Draw(img)
    m = size // 5
    d.rounded_rectangle([m, m, size - m, size - m], radius=size // 12, outline="#60a5fa", width=size // 20)
    d.line([size // 2, m, size // 2, size - m], fill="#60a5fa", width=size // 24)   # reliure du livre
    img.save(out / f"icon-{size}.png")
print("Icônes générées dans", out)
