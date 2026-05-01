"""tasteabrew.ru — спарсить весь чай (68 продуктов).
Затем переименовать в TMA подкатегории tea_restoranica_* → tea_tasteabrew_*
и привязать фото.
"""
import urllib.request, re, json, time
from pathlib import Path

PRODUCTS = Path("/root/projects/ai-agents-rb/BOT_TG/tma_static/products.json")
PHOTOS = Path("/root/projects/ai-agents-rb/BOT_TG/tma_static/photos/products")
PHOTOS.mkdir(parents=True, exist_ok=True)

UA = {"User-Agent":"Mozilla/5.0 Chrome/120","Accept":"*/*","Referer":"https://www.google.com/"}


def get(url):
    return urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=15).read().decode('utf-8','replace')


def get_bytes(url):
    # Кодируем не-ASCII в URL
    from urllib.parse import quote
    parts = url.split("/", 3)
    if len(parts) >= 4:
        path = "/".join(parts[3:].__class__([parts[3]]))  # weird, fix
        # проще — quote на не-ascii path
    safe_url = quote(url, safe=":/?&=#%")
    return urllib.request.urlopen(urllib.request.Request(safe_url, headers=UA), timeout=15).read()


def parse_og(html, prop):
    m = re.search(rf'<meta[^>]+property="{prop}"[^>]+content="([^"]*)"', html)
    if not m:
        m = re.search(rf'<meta[^>]+content="([^"]*)"[^>]+property="{prop}"', html)
    return m.group(1) if m else ""


def harvest_tasteabrew():
    """Возвращает [{name, image, url, desc}, ...]"""
    sm = get("https://www.tasteabrew.ru/sitemap.xml")
    urls = re.findall(r"<loc>([^<]+/product/[^<]+)</loc>", sm)
    print(f"  → {len(urls)} продукт-страниц")
    items = []
    for i, u in enumerate(urls, 1):
        if i % 15 == 0:
            print(f"    {i}/{len(urls)}...")
        try:
            html = get(u)
            title = parse_og(html, "og:title")
            img = parse_og(html, "og:image")
            desc = parse_og(html, "og:description") or ""
            if not title or not img:
                continue
            items.append({"name": title.strip(), "image": img, "url": u, "desc": desc[:300]})
            time.sleep(0.2)
        except Exception as e:
            print(f"    ! {u}: {e}")
    return items


def kw(s):
    s = s.upper()
    s = re.sub(r"[ЁЕ]","Е",s)
    s = re.sub(r"[^\w\s]"," ",s)
    NOISE = {"ЧАЙ","TASTEABREW","RESTORANICA","TOGO","TO","GO","ICED",
             "ПАКЕТ","ПАКЕТИКОВ","ПАК","ПИРАМИДКА","ПИРАМИДОК","ПИР",
             "ФРУКТОВЫЙ","ФРУКТ","ТРАВЯНОЙ","ТРАВ","ЧЕРНЫЙ","ЧЕРН",
             "ЗЕЛЕНЫЙ","ЗЕЛЕН","БЕЛЫЙ","СТАКАНЧИК","СТАКАНЧИКА",
             "ЧАЙНИК","ЧАЙНИКА","ЧАШКА","ВКУСОМ","ВКУС","ИЗ","С",
             "Г","КГ","МЛ","Л","ШТ","ИМУННЫЙ","NEW","НОВ"}
    out = set()
    for w in s.split():
        if len(w) < 3 or w.isdigit(): continue
        out.add(w)
    return out - NOISE


def score(a, b):
    ka, kb = kw(a), kw(b)
    if not ka or not kb: return 0.0
    return len(ka & kb) / max(len(ka), len(kb))


# Detect format type из имени
def detect_format(name):
    n = name.lower()
    if "togo" in n or "to-go" in n or "to go" in n or "стаканчик" in n or "60 пакетик" in n:
        return "togo"
    if "iced" in n or "айс" in n or "ICEDTEA" in name:
        return "iced"
    if "пирамидк" in n or "pyra" in n:
        return "pyr"
    if "пакет" in n or " пак " in n or "30пак" in n:
        return "cup"
    return "loose"


SUBCAT_LABELS = {
    "tea_tasteabrew_loose": "Tasteabrew · 📄 Листовой",
    "tea_tasteabrew_togo": "Tasteabrew · 📦 To Go",
    "tea_tasteabrew_iced": "Tasteabrew · 🧊 Iced",
    "tea_tasteabrew_pyr": "Tasteabrew · 🔺 Пирамидки",
    "tea_tasteabrew_cup": "Tasteabrew · 🍵 На чашку",
    "tea_tasteabrew_pot": "Tasteabrew · 🫖 На чайник",
}


def main():
    print("Парсю tasteabrew.ru...")
    refs = harvest_tasteabrew()
    print(f"Получено: {len(refs)} продуктов")

    data = json.loads(PRODUCTS.read_text(encoding="utf-8"))
    products = data["products"]

    # 1. Переименовать TMA-товары: tea_restoranica_* → tea_tasteabrew_*
    rename_count = 0
    rename_name = 0
    for p in products:
        sub = p.get("subcategory", "")
        if sub.startswith("tea_restoranica_"):
            new_sub = sub.replace("tea_restoranica_", "tea_tasteabrew_")
            p["subcategory"] = new_sub
            rename_count += 1
        # Имя — заменяем "Restoranica" на "Tasteabrew" если есть
        if "Restoranica" in p.get("name", ""):
            p["name"] = p["name"].replace("Restoranica", "Tasteabrew")
            rename_name += 1
        # country тоже
        if p.get("country") == "Restoranica":
            p["country"] = "Tasteabrew"
    print(f"\nПереименовано подкатегорий: {rename_count}")
    print(f"Переименовано в имени: {rename_name}")

    # 2. Обновить subcategories
    new_subs = []
    for s in data["subcategories"]:
        if s["id"].startswith("tea_restoranica_"):
            new_id = s["id"].replace("tea_restoranica_", "tea_tasteabrew_")
            new_label = SUBCAT_LABELS.get(new_id, s["name"].replace("RESTORANICA", "Tasteabrew").replace("Restoranica", "Tasteabrew"))
            new_subs.append({"id": new_id, "parent": "tea", "name": new_label})
        else:
            new_subs.append(s)
    data["subcategories"] = new_subs

    # 3. Скачиваем фото и матчим с tasteabrew данными
    targets = [p for p in products if p.get("subcategory","").startswith("tea_tasteabrew_")]
    print(f"\nTMA Tasteabrew товаров: {len(targets)}")

    cache = {}  # url → bytes
    matched = 0
    for p in targets:
        best, best_s = None, 0.0
        for r in refs:
            s = score(p["name"], r["name"])
            if s > best_s:
                best_s, best = s, r
        if not best or best_s < 0.30:
            continue
        url = best["image"]
        if url not in cache:
            try:
                cache[url] = get_bytes(url)
            except Exception as e:
                print(f"  ! {url}: {e}")
                cache[url] = None
                continue
        if not cache[url]:
            continue
        # Конвертим в jpg
        try:
            from PIL import Image
            from io import BytesIO
            img = Image.open(BytesIO(cache[url])).convert("RGB")
            img.thumbnail((900, 900))
            out = BytesIO()
            img.save(out, "JPEG", quality=85, optimize=True)
            jpg = out.getvalue()
        except Exception:
            jpg = cache[url]
        dest = PHOTOS / f"{p['id']}.jpg"
        dest.write_bytes(jpg)
        p["photo"] = f"photos/products/{p['id']}.jpg"
        if best.get("desc"):
            p["description"] = best["desc"]
        matched += 1
        print(f"  ✓ [{best_s:.2f}] {p['name'][:42]:<42} → {best['name'][:50]}")

    PRODUCTS.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n=== Итого ===")
    print(f"Сматчено фото: {matched} / {len(targets)}")
    print(f"Tasteabrew продуктов в источнике: {len(refs)}")


if __name__ == "__main__":
    main()
