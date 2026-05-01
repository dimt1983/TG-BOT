"""Применяет одну картинку «Чайные Братья» ко всем товарам подкатегории «Китайский чай».
Файл должен лежать в tma_static/assets/chinese_tea_logo.png (любой png/jpg).
"""
import json, shutil
from pathlib import Path

PRODUCTS = Path("/root/projects/ai-agents-rb/BOT_TG/tma_static/products.json")
ASSETS = Path("/root/projects/ai-agents-rb/BOT_TG/tma_static/assets")
PHOTOS = Path("/root/projects/ai-agents-rb/BOT_TG/tma_static/photos/products")

# Ищем источник
candidates = [ASSETS/"chinese_tea_logo.png", ASSETS/"chinese_tea_logo.jpg",
              ASSETS/"chinese_brothers.png", ASSETS/"chinese_brothers.jpg"]
src = next((c for c in candidates if c.exists()), None)
if not src:
    print(f"❌ Файл не найден. Положите логотип в один из путей:")
    for c in candidates: print(f"   {c}")
    raise SystemExit(1)
print(f"Источник: {src} ({src.stat().st_size} байт)")

# Конвертируем в jpg если png
try:
    from PIL import Image
    from io import BytesIO
    img = Image.open(src).convert("RGBA")
    # белый фон если есть прозрачность
    bg = Image.new("RGB", img.size, (245, 241, 236))
    bg.paste(img, mask=img.split()[3] if img.mode == "RGBA" else None)
    bg.thumbnail((900, 900))
    out_buf = BytesIO()
    bg.save(out_buf, "JPEG", quality=88, optimize=True)
    JPG_DATA = out_buf.getvalue()
    print(f"Конвертировано в JPG: {len(JPG_DATA)} байт")
except Exception as e:
    JPG_DATA = src.read_bytes()
    print(f"Использую как есть (без конвертации): {e}")

data = json.loads(PRODUCTS.read_text(encoding="utf-8"))
chinese = [p for p in data["products"] if p.get("subcategory","").startswith("tea_китайский_чай")]
print(f"\nКитайских чаёв: {len(chinese)}")

PHOTOS.mkdir(parents=True, exist_ok=True)
for p in chinese:
    dest = PHOTOS / f"{p['id']}.jpg"
    dest.write_bytes(JPG_DATA)
    p["photo"] = f"photos/products/{p['id']}.jpg"

PRODUCTS.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
print(f"Применено к {len(chinese)} товарам")
