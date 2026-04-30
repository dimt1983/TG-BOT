"""
sync_ozon_photos.py — берёт каталог TMA, матчит с товарами на Ozon по имени,
скачивает первое фото с Ozon и кладёт в tma_static/photos/products/.

Запуск (раз в сутки или вручную):
    OZON_CLIENT_ID=... OZON_API_KEY=... python3 sync_ozon_photos.py

Что делает:
1. POST /v3/product/list                — список всех ваших товаров (offer_id + product_id)
2. POST /v3/product/info/list           — детали с массивом images
3. Fuzzy-match по имени с products.json
4. Скачивает первое изображение, сохраняет в tma_static/photos/products/{id}.jpg
5. Обновляет products.json — добавляет поле "photo"
6. Создаёт mapping.csv для ручной коррекции
"""
import json
import os
import re
import sys
import urllib.request
import urllib.error
from pathlib import Path

OZON_CLIENT_ID = os.environ.get("OZON_CLIENT_ID", "65780639")
OZON_API_KEY = os.environ.get("OZON_API_KEY", "d8f0b4ff-def3-4fd4-b4b4-f2722a63c7a7")
OZON_API = "https://api-seller.ozon.ru"

ROOT = Path(__file__).parent
PRODUCTS_JSON = ROOT / "tma_static" / "products.json"
PHOTOS_DIR = ROOT / "tma_static" / "photos" / "products"
MAPPING_CSV = ROOT / "ozon_mapping.csv"


def ozon_post(path: str, payload: dict) -> dict:
    body = json.dumps(payload).encode()
    req = urllib.request.Request(
        OZON_API + path,
        data=body,
        headers={
            "Client-Id": OZON_CLIENT_ID,
            "Api-Key": OZON_API_KEY,
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        print(f"  HTTP {e.code} for {path}: {e.read()[:200]}")
        return {}


def fetch_all_products() -> list[dict]:
    """Вытащить все ваши товары с Ozon (offer_id + product_id + name)."""
    items = []
    last_id = ""
    while True:
        r = ozon_post(
            "/v3/product/list",
            {"filter": {"visibility": "ALL"}, "last_id": last_id, "limit": 1000},
        )
        chunk = r.get("result", {}).get("items") or []
        items.extend(chunk)
        last_id = r.get("result", {}).get("last_id") or ""
        if not chunk or not last_id:
            break
    return items


def fetch_info_batch(product_ids: list[int]) -> list[dict]:
    """Детали (включая images) для пачки product_id (макс 1000 за раз)."""
    out = []
    for i in range(0, len(product_ids), 1000):
        chunk = product_ids[i : i + 1000]
        r = ozon_post("/v3/product/info/list", {"product_id": chunk})
        out.extend(r.get("result", {}).get("items") or r.get("items") or [])
    return out


# ========== Матчинг ==========
def normalize(s: str) -> str:
    s = s.lower()
    # удалим знаки препинания, кавычки, лишние пробелы
    s = re.sub(r'[^\w\s]', ' ', s)
    s = re.sub(r'\s+', ' ', s).strip()
    return s


def name_keywords(s: str) -> set[str]:
    """Ключевые слова для матчинга (страна / регион / тип обработки / вес)."""
    n = normalize(s)
    words = [w for w in n.split() if len(w) >= 3]
    return set(words)


def match_score(tma_name: str, ozon_name: str) -> float:
    """Простой Jaccard-индекс ключевых слов."""
    a = name_keywords(tma_name)
    b = name_keywords(ozon_name)
    if not a or not b:
        return 0.0
    inter = len(a & b)
    union = len(a | b)
    return inter / union if union else 0.0


def find_best_match(tma_product: dict, ozon_items: list[dict]) -> tuple[dict | None, float]:
    best, score = None, 0.0
    for o in ozon_items:
        oname = o.get("name", "") or ""
        s = match_score(tma_product["name"], oname)
        if s > score:
            best, score = o, s
    return best, score


# ========== Загрузка картинок ==========
def download_image(url: str, dest: Path) -> bool:
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=20) as r:
            data = r.read()
            dest.write_bytes(data)
            return True
    except Exception as e:
        print(f"  ! download {url}: {e}")
        return False


# ========== Main ==========
def main():
    if not PRODUCTS_JSON.exists():
        print(f"Не найден {PRODUCTS_JSON}")
        sys.exit(1)

    PHOTOS_DIR.mkdir(parents=True, exist_ok=True)

    print(f"📦 Получаю список товаров с Ozon...")
    items = fetch_all_products()
    print(f"   найдено {len(items)} товаров на Ozon")
    if not items:
        print("Пусто — проверьте OZON_CLIENT_ID/OZON_API_KEY и видимость товаров.")
        return

    print(f"\n📥 Получаю детали с фотографиями...")
    pids = [it["product_id"] for it in items if it.get("product_id")]
    info = fetch_info_batch(pids)
    print(f"   получено {len(info)} карточек")

    by_id = {x.get("id") or x.get("product_id"): x for x in info if x}

    # Загружаем TMA-каталог
    with open(PRODUCTS_JSON, encoding="utf-8") as f:
        tma_data = json.load(f)
    tma_products = tma_data["products"]
    print(f"\n🔄 TMA-товаров: {len(tma_products)}, начинаю матчинг...")

    matches = []
    photo_count = 0
    skipped = 0
    threshold = 0.30  # минимум совпадения, ниже — считаем что не нашли

    # Для матчинга нам нужны имена Ozon-товаров
    ozon_named = []
    for o in info:
        if not o:
            continue
        name = o.get("name", "")
        offer = o.get("offer_id", "")
        product_id = o.get("id") or o.get("product_id")
        images = o.get("images") or []
        # У Ozon картинки могут быть в разных форматах
        if isinstance(images, dict):
            images = list(images.values()) if images else []
        first_img = None
        for im in images:
            if isinstance(im, str):
                first_img = im
                break
            if isinstance(im, dict):
                first_img = im.get("file_name") or im.get("file") or im.get("url")
                if first_img:
                    break
        ozon_named.append({
            "name": name,
            "offer_id": offer,
            "product_id": product_id,
            "first_img": first_img,
            "all_images": images,
        })

    for p in tma_products:
        if p["category"] != "coffee" and p["category"] != "tea":
            # Кофе и чай — приоритет. Сиропы/молоко — потом
            continue

        best, score = find_best_match(p, ozon_named)
        if not best or score < threshold:
            skipped += 1
            continue

        matches.append({
            "tma_id": p["id"],
            "tma_name": p["name"],
            "ozon_offer": best["offer_id"],
            "ozon_pid": best["product_id"],
            "ozon_name": best["name"],
            "score": round(score, 2),
            "photo_url": best["first_img"],
        })

        # Скачивание фото
        if best["first_img"]:
            dest = PHOTOS_DIR / f"{p['id']}.jpg"
            if not dest.exists() or dest.stat().st_size < 1000:
                if download_image(best["first_img"], dest):
                    photo_count += 1
                    p["photo"] = f"photos/products/{p['id']}.jpg"
                    p["ozon_offer_id"] = best["offer_id"]
            else:
                p["photo"] = f"photos/products/{p['id']}.jpg"
                p["ozon_offer_id"] = best["offer_id"]

    # Сохраняем mapping.csv для проверки
    import csv
    with open(MAPPING_CSV, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f, delimiter=";")
        w.writerow(["tma_id", "score", "tma_name", "ozon_offer", "ozon_name", "photo_url"])
        for m in sorted(matches, key=lambda x: -x["score"]):
            w.writerow([
                m["tma_id"], m["score"], m["tma_name"],
                m["ozon_offer"], m["ozon_name"], m["photo_url"] or "",
            ])

    # Обновляем products.json
    with open(PRODUCTS_JSON, "w", encoding="utf-8") as f:
        json.dump(tma_data, f, ensure_ascii=False, indent=2)

    # Отчёт
    print(f"\n=== Результат ===")
    print(f"Матчей выше {threshold}: {len(matches)}")
    print(f"Фото скачано: {photo_count}")
    print(f"Не сматчены: {skipped}")
    print(f"\nMapping → {MAPPING_CSV}")
    print(f"Если в mapping.csv видишь неправильные пары — поправь в боевой products.json вручную.")
    print(f"\nЛучшие матчи:")
    for m in sorted(matches, key=lambda x: -x["score"])[:10]:
        print(f"  [{m['score']:.2f}] {m['tma_name'][:45]:<45} → {m['ozon_name'][:45]}")


if __name__ == "__main__":
    main()
