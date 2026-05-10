"""
🎨 Pro Academy Icon-Generator

Najib lädt 1 File hoch:  static/icons/pa-logo.png
Dieses Script erzeugt daraus AUTOMATISCH:
  - pa-icon-192.png      (PWA Android)
  - pa-icon-512.png      (PWA Android großes Icon)
  - pa-apple-touch.png   (iOS Home-Screen 180×180)
  - pa-favicon-32.png    (Browser-Tab Favicon)

Run auf PA:
  cd ~/nt-pro-academy-crm && python3 scripts/generate_pa_icons.py
"""
import os
import sys

try:
    from PIL import Image
except ImportError:
    print("✗ PIL/Pillow nicht installiert. Installiere mit: pip3 install --user pillow")
    sys.exit(1)

ICONS_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'static', 'icons')
SOURCE = os.path.join(ICONS_DIR, 'pa-logo.png')

if not os.path.exists(SOURCE):
    print(f"✗ Source nicht gefunden: {SOURCE}")
    print(f"  → Lade pa-logo.png in {ICONS_DIR}/ hoch und führe das Script erneut aus")
    sys.exit(1)

img = Image.open(SOURCE).convert('RGBA')
w, h = img.size
print(f"✓ Source geladen: {w}x{h} px")

# Falls das Bild Tagline/Text hat, croppen wir den oberen quadratischen Bereich
# (das PA-Symbol ist typischerweise im oberen 60-70% des Bildes)
square_size = min(w, h)
if h > w:
    # Vertikal: Crop oberen quadratischen Bereich
    crop_box = (0, 0, w, w)
    img_square = img.crop(crop_box)
elif w > h:
    # Horizontal: Crop mittig
    offset = (w - h) // 2
    img_square = img.crop((offset, 0, offset + h, h))
else:
    img_square = img

print(f"✓ Quadratisch gecroppt: {img_square.size[0]}x{img_square.size[0]} px")

targets = [
    ('pa-icon-192.png', 192),
    ('pa-icon-512.png', 512),
    ('pa-apple-touch.png', 180),
    ('pa-favicon-32.png', 32),
]

for filename, size in targets:
    output = os.path.join(ICONS_DIR, filename)
    resized = img_square.resize((size, size), Image.LANCZOS)
    resized.save(output, 'PNG', optimize=True)
    fsize_kb = os.path.getsize(output) / 1024
    print(f"  ✓ {filename:30s} {size}×{size}  ({fsize_kb:.1f} KB)")

print(f"\n🎨 Alle 4 Icons erzeugt in {ICONS_DIR}/")
print(f"   Restart der Web-App: touch /var/www/proacademy-business_de_wsgi.py")
