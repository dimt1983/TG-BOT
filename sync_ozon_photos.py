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
# offer_id у Ozon вида: RB-E-БРАЗИЛИЯ СЕРРАДО, 1 кг   |  RBR-F-КЕНИЯ АА, 1кг
# Префикс: RB|RBR + - + (E|F|BE|BF) + - + ИМЯ + , + ФАСОВКА

OFFER_RE = re.compile(r'^(RB[R]?)-([A-Z]{1,3})-\s*(.+?)\s*,\s*(.+)$')


def parse_offer_id(offer_id: str) -> dict | None:
    """Парсит RB-E-ИМЯ, фасовка → {prefix, type, name, fasovka}."""
    if not offer_id:
        return None
    m = OFFER_RE.match(offer_id.strip())
    if not m:
        return None
    return {
        "prefix": m.group(1),
        "type": m.group(2),       # E / F / BE / BF
        "name": m.group(3).strip(),
        "fasovka": m.group(4).strip(),
    }


def normalize_words(s: str) -> set[str]:
    s = s.upper()
    # ru → латинизированные эквиваленты
    repl = {
        "ИРГАЧЕФФЕ": "ИРГАЧИФ", "ИРГАЧИФФ": "ИРГАЧИФ", "YIRGACHEFFE": "ИРГАЧИФ",
        "BRAZIL": "БРАЗИЛИЯ", "COLOMBIA": "КОЛУМБИЯ", "KENYA": "КЕНИЯ",
        "ETHIOPIA": "ЭФИОПИЯ", "HONDURAS": "ГОНДУРАС",
        "УЭУЕТЭНАНГО": "УЕУЕТЕНАНГО", "УЭУЭТЕНАНГО": "УЕУЕТЕНАНГО", "УЕТЕНАНГО": "УЕУЕТЕНАНГО",
        "DECAFFEINATED": "ДЕКАФ", "DECAF": "ДЕКАФ",
    }
    s = re.sub(r'[^\w\s]+', ' ', s)
    out = set()
    for w in s.split():
        if len(w) < 2:
            continue
        out.add(repl.get(w, w))
    noise = {"СУХОЙ", "МЫТЫЙ", "ХАНИ", "ГР", "1", "2", "3", "4", "ОБЖАРКА",
             "ТЕМНАЯ", "СВЕТЛАЯ", "ROASTERS", "RB", "RBR",
             "АРАБИКА", "100", "СВЕЖЕОБЖАРЕННЫЙ", "ЗЕРНОВОЙ",
             "В", "ЗЕРНАХ", "КОФЕ", "УПАКОВКА", "СМЕСЬ"}
    return out - noise


def match_score(tma_name: str, ozon_name: str) -> float:
    a = normalize_words(tma_name)
    b = normalize_words(ozon_name)
    if not a or not b:
        return 0.0
    inter = len(a & b)
    return inter / max(len(a), len(b))


def parse_fasovka(s: str) -> str | None:
    """'1 кг' / '200 г' / '1кг' → нормализованный."""
    if not s:
        return None
    m = re.search(r'(\d+(?:[\.,]\d+)?)\s*(кг|г|шт|мл|л)', s.lower())
    if not m:
        return None
    v = float(m.group(1).replace(',', '.'))
    u = m.group(2)
    if u == 'кг':
        return f"{v:g} кг"
    if u == 'г':
        return f"{int(v)} г"
    return f"{v:g} {u}"


def find_best_match(tma_product: dict, ozon_items: list[dict], threshold: float = 0.4):
    """Возвращает (best_item, score, matched_fasovka)."""
    tma_name = tma_product["name"]
    tma_fasovkas = [parse_fasovka(f.get("size", "")) for f in tma_product.get("fasovka", [])]
    tma_fasovkas = [f for f in tma_fasovkas if f]

    best, score, fas = None, 0.0, None
    for o in ozon_items:
        # 1. Используем offer_id если получится распарсить
        parsed = parse_offer_id(o.get("offer_id", ""))
        if parsed:
            s = match_score(tma_name, parsed["name"])
            ofas = parse_fasovka(parsed["fasovka"])
            # бонус если фасовка совпадает с одной из TMA
            if ofas in tma_fasovkas:
                s += 0.2
        else:
            s = match_score(tma_name, o.get("name", ""))
            ofas = None
        if s > score:
            best, score, fas = o, s, ofas
    if score < threshold:
        return None, score, None
    return best, score, fas


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

    # Для матчинга нам нужны имена Ozon-товаров
    ozon_named = []
    for o in info:
        if not o:
            continue
        # Главное фото — это primary_image (то что в выдаче на маркетплейсе)
        primary = o.get("primary_image") or []
        if isinstance(primary, list):
            main_img = primary[0] if primary else None
        elif isinstance(primary, str):
            main_img = primary
        else:
            main_img = None
        # Если primary_image нет — берём первое из images
        if not main_img:
            images = o.get("images") or []
            if isinstance(images, dict):
                images = list(images.values()) if images else []
            for im in images:
                if isinstance(im, str):
                    main_img = im; break
                if isinstance(im, dict):
                    main_img = im.get("file_name") or im.get("file") or im.get("url")
                    if main_img: break
        ozon_named.append({
            "name": o.get("name", ""),
            "offer_id": o.get("offer_id", ""),
            "product_id": o.get("id") or o.get("product_id"),
            "first_img": main_img,
        })

    # === Greedy-матчинг: каждый Ozon-товар идёт только в ОДИН TMA-товар (с лучшим score) ===
    # 1. Считаем все возможные пары (tma, ozon, score) с фасовкой-бонусом
    # 2. Сортируем по score
    # 3. Жадно назначаем — пропуская уже занятые tma и ozon
    pairs = []
    for p in tma_products:
        if p["category"] != "coffee":
            continue
        for o in ozon_named:
            best_score, ofas = 0.0, None
            parsed = parse_offer_id(o.get("offer_id", ""))
            if parsed:
                s = match_score(p["name"], parsed["name"])
                of = parse_fasovka(parsed["fasovka"])
                tma_fas = [parse_fasovka(f.get("size","")) for f in p.get("fasovka",[])]
                tma_fas = [f for f in tma_fas if f]
                if of in tma_fas:
                    s += 0.3
                ofas = of
            else:
                s = match_score(p["name"], o.get("name",""))
            if s >= 0.5:
                pairs.append((s, p["id"], o["offer_id"], p, o, ofas))

    pairs.sort(reverse=True, key=lambda x: x[0])

    used_tma = set()
    used_ozon = set()
    for score, tma_id, ozon_offer, p, o, ofas in pairs:
        if tma_id in used_tma or ozon_offer in used_ozon:
            continue
        used_tma.add(tma_id)
        used_ozon.add(ozon_offer)

        matches.append({
            "tma_id": p["id"], "tma_name": p["name"],
            "ozon_offer": o["offer_id"], "ozon_pid": o["product_id"],
            "ozon_name": o["name"], "score": round(score, 2),
            "photo_url": o["first_img"], "matched_fasovka": ofas,
        })

        if o["first_img"]:
            dest = PHOTOS_DIR / f"{p['id']}.jpg"
            # Всегда перекачиваем — даже если файл существует (потому что URL мог измениться)
            if download_image(o["first_img"], dest):
                photo_count += 1
                p["photo"] = f"photos/products/{p['id']}.jpg"
                p["ozon_offer_id"] = o["offer_id"]

    skipped = sum(1 for p in tma_products if p["category"] == "coffee" and p["id"] not in used_tma)

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
    print(f"Матчей: {len(matches)}")
    print(f"Фото скачано: {photo_count}")
    print(f"Не сматчены: {skipped}")
    print(f"\nMapping → {MAPPING_CSV}")
    print(f"Если в mapping.csv видишь неправильные пары — поправь в боевой products.json вручную.")
    print(f"\nЛучшие матчи:")
    for m in sorted(matches, key=lambda x: -x["score"])[:10]:
        print(f"  [{m['score']:.2f}] {m['tma_name'][:45]:<45} → {m['ozon_name'][:45]}")


if __name__ == "__main__":
    main()
