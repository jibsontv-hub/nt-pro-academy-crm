"""
🎨 Pro Academy Icon-Generator

Generiert ALLE benötigten App-Icons:
  - pa-icon-192.png      (PWA Android)
  - pa-icon-512.png      (PWA Android großes Icon)
  - pa-apple-touch.png   (iOS Home-Screen 180×180)
  - pa-favicon-32.png    (Browser-Tab Favicon)
  - pa-logo.png          (großes Logo für Hero-Bereiche)

Strategie:
  1. Wenn /static/icons/pa-logo.png schon existiert (user-uploaded) → resize daraus
  2. Sonst: zeichne PA-Logo programmatisch (schwarz BG + P weiß + A gold)

Run:
  cd ~/nt-pro-academy-crm && python3 scripts/generate_pa_icons.py
"""
import os
import sys

try:
    from PIL import Image, ImageDraw, ImageFont
except ImportError:
    print("✗ PIL/Pillow nicht installiert. Installiere mit: pip3 install --user pillow")
    sys.exit(1)

ICONS_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'static', 'icons')
SOURCE_PNG = os.path.join(ICONS_DIR, 'pa-logo.png')
SOURCE_SVG = os.path.join(ICONS_DIR, 'pa-logo.svg')

# Brand-Farben (aus pa-logo.svg)
BLACK = (0, 0, 0, 255)
WHITE = (255, 255, 255, 255)
GOLD = (212, 168, 67, 255)


def draw_pa_logo(size, radius_factor=0.18):
    """Zeichnet PA-Logo programmatisch in beliebiger Größe.
    Schwarzer Background mit Rounded-Corners (rund auf großen Icons),
    P weiß links, A gold rechts."""
    img = Image.new('RGBA', (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    # Rounded Background
    radius = int(size * radius_factor)
    draw.rounded_rectangle([(0, 0), (size, size)], radius=radius, fill=BLACK)

    # P + A — schreibe als Text mittig
    try:
        # Versuche eine fette Font zu finden
        for fpath in ['/Library/Fonts/Arial Bold.ttf',
                      '/System/Library/Fonts/Helvetica.ttc',
                      '/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf',
                      '/Library/Fonts/Arial.ttf']:
            if os.path.exists(fpath):
                font = ImageFont.truetype(fpath, int(size * 0.55))
                break
        else:
            font = ImageFont.load_default()
    except Exception:
        font = ImageFont.load_default()

    # P weiß
    p_text = 'P'
    bbox = draw.textbbox((0, 0), p_text, font=font)
    p_w, p_h = bbox[2] - bbox[0], bbox[3] - bbox[1]
    p_x = int(size * 0.18) - bbox[0]
    p_y = int(size * 0.50) - p_h // 2 - bbox[1]
    draw.text((p_x, p_y), p_text, font=font, fill=WHITE)

    # A gold
    a_text = 'A'
    bbox_a = draw.textbbox((0, 0), a_text, font=font)
    a_x = int(size * 0.50) - bbox_a[0]
    a_y = int(size * 0.50) - (bbox_a[3] - bbox_a[1]) // 2 - bbox_a[1]
    draw.text((a_x, a_y), a_text, font=font, fill=GOLD)

    return img


def main():
    os.makedirs(ICONS_DIR, exist_ok=True)

    # 1. Source-Logo holen oder zeichnen
    if os.path.exists(SOURCE_PNG):
        print(f"✓ Source: {SOURCE_PNG} (user upload)")
        source = Image.open(SOURCE_PNG).convert('RGBA')
        # Crop auf Quadrat falls nötig
        w, h = source.size
        if w != h:
            sq = min(w, h)
            offset_x = (w - sq) // 2
            source = source.crop((offset_x, 0, offset_x + sq, sq))
            print(f"  → gecroppt auf {sq}×{sq}")
    else:
        print(f"✓ Source: programmatisch generiert (kein User-Upload)")
        source = draw_pa_logo(1024)
        # Auch das große Logo speichern
        big_path = os.path.join(ICONS_DIR, 'pa-logo.png')
        source.save(big_path, 'PNG', optimize=True)
        print(f"  ✓ pa-logo.png (1024×1024) gespeichert als Master")

    # 2. Alle Größen erzeugen
    targets = [
        ('pa-icon-192.png', 192),
        ('pa-icon-512.png', 512),
        ('pa-apple-touch.png', 180),
        ('pa-favicon-32.png', 32),
    ]

    for filename, size in targets:
        output = os.path.join(ICONS_DIR, filename)
        # Bei kleinen Sizes: programmatisch zeichnen ist schärfer als resize
        if size <= 192 and not os.path.exists(SOURCE_PNG):
            img = draw_pa_logo(size)
        else:
            img = source.resize((size, size), Image.LANCZOS)
        img.save(output, 'PNG', optimize=True)
        kb = os.path.getsize(output) / 1024
        print(f"  ✓ {filename:30s} {size}×{size}  ({kb:.1f} KB)")

    print(f"\n🎨 Alle Icons erzeugt in {ICONS_DIR}/")
    print(f"   Restart App: touch /var/www/proacademy-business_de_wsgi.py")


if __name__ == '__main__':
    main()
