"""Generiert NT-Logo PNGs für iOS Home-Screen, Android, etc.
Output: static/icons/*.png
"""
from PIL import Image, ImageDraw, ImageFont
import os

OUT = os.path.join(os.path.dirname(__file__), '..', 'static', 'icons')
os.makedirs(OUT, exist_ok=True)

# Versuche Inter zu laden, fallback auf Default
def find_font(size):
    candidates = [
        '/System/Library/Fonts/Supplemental/Arial Bold.ttf',
        '/System/Library/Fonts/Helvetica.ttc',
        '/Library/Fonts/Arial Bold.ttf',
        'C:/Windows/Fonts/arialbd.ttf',
    ]
    for p in candidates:
        if os.path.exists(p):
            try:
                return ImageFont.truetype(p, size)
            except Exception:
                pass
    return ImageFont.load_default()

def make_icon(size, filename):
    """Navy gradient bg + goldenes NT in der Mitte."""
    img = Image.new('RGB', (size, size), (15, 28, 63))
    draw = ImageDraw.Draw(img)

    # Vertikaler Gradient #0f1c3f → #1a2c5b
    top = (15, 28, 63)
    bot = (26, 44, 91)
    for y in range(size):
        t = y / size
        r = int(top[0] + (bot[0] - top[0]) * t)
        g = int(top[1] + (bot[1] - top[1]) * t)
        b = int(top[2] + (bot[2] - top[2]) * t)
        draw.line([(0, y), (size, y)], fill=(r, g, b))

    # Abgerundete Ecken (Maske)
    radius = int(size * 0.22)
    mask = Image.new('L', (size, size), 0)
    mdraw = ImageDraw.Draw(mask)
    mdraw.rounded_rectangle((0, 0, size, size), radius=radius, fill=255)
    out = Image.new('RGBA', (size, size), (0, 0, 0, 0))
    out.paste(img, (0, 0), mask)

    # NT Text in Gold (#d4a843)
    draw2 = ImageDraw.Draw(out)
    font_size = int(size * 0.42)
    font = find_font(font_size)

    text = 'NT'
    bbox = draw2.textbbox((0, 0), text, font=font)
    tw = bbox[2] - bbox[0]
    th = bbox[3] - bbox[1]
    x = (size - tw) // 2 - bbox[0]
    y = (size - th) // 2 - bbox[1]
    # Slight letter-spacing fake durch separate Zeichen
    draw2.text((x, y), text, font=font, fill=(212, 168, 67))

    out.save(os.path.join(OUT, filename), 'PNG')
    print(f'✓ {filename} ({size}x{size})')

# Standardgrößen für iOS, Android, Favicons
sizes = [
    (180, 'apple-touch-icon.png'),     # iOS Safari Home-Screen
    (192, 'icon-192.png'),              # Android / PWA
    (512, 'icon-512.png'),              # PWA splash
    (32, 'favicon-32.png'),             # Browser-Tab fallback
    (16, 'favicon-16.png'),             # Browser-Tab klein
]

for size, name in sizes:
    make_icon(size, name)

print('\nFertig! Files in static/icons/')
