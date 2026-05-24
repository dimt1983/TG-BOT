"""Поиск фото товаров через Wildberries Search API.

Для каждого TMA-товара без фото:
1. Делаем search.wb.ru/exactmatch?query=<название>
2. Берём первый product_id (nm_id)
3. Конструируем URL картинки: https://basket-NN.wbbasket.ru/vol<X>/part<Y>/<nm_id>/images/big/1.webp
4. Скачиваем, конвертируем в jpg
5. Сохраняем

WB-картинки лежат в одном из 22+ "корзин" (basket-01..basket-22) — нужно подобрать.
Для нового товара обычно basket = (nm_id // 100000 + 1) // 144 + 1 — но не всегда. Проще пробежать.
"""
import urllib.request, urllib.parse, json, re, time
from pathlib import Path
from io import BytesIO

PRODUCTS = Path("/root/projects/ai-agents-rb/BOT_TG/tma_static/products.json")
PHOTOS = Path("/root/projects/ai-agents-rb/BOT_TG/tma_static/photos/products")
PHOTOS.mkdir(parents=True, exist_ok=True)

UA = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120",
    "Accept": "*/*",
    "Origin": "https://www.wildberries.ru",
    "Referer": "https://www.wildberries.ru/",
}


def http_json(url):
    try:
        req = urllib.request.Request(url, headers=UA)
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.loads(r.read())
    except Exception as e:
        return None


def http_bytes(url):
    try:
        req = urllib.request.Request(url, headers=UA)
        with urllib.request.urlopen(req, timeout=15) as r:
            return r.read()
    except Exception:
        return None


_TRUSTED_HOSTS = (
    "basket-",  # wbbasket.ru — товарные карточки Wildberries
    ".wbbasket.ru",
    "ozone.ru",       # Ozon CDN
    "ya-marketing.ru",
)


def yandex_image_search(query: str) -> list[str]:
    """Только trusted-источники: wbbasket / ozon / другие маркетплейсы.
    Yandex картинки общего поиска часто отдают мусор (логотипы, иллюстрации, шарики)."""
    q = urllib.parse.quote(query)
    url = f"https://yandex.ru/images/search?text={q}"
    try:
        req = urllib.request.Request(url, headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120",
            "Accept": "text/html",
            "Accept-Language": "ru,en;q=0.9",
        })
        with urllib.request.urlopen(req, timeout=15) as r:
            html = r.read().decode("utf-8", "replace")
    except Exception:
        return []
    # ТОЛЬКО Wildberries-CDN и Ozon-CDN — это товарные карточки маркетплейсов
    wb = re.findall(r'(https://basket-\d+\.wbbasket\.ru/vol\d+/part\d+/\d+/images/big/\d+\.webp)', html)
    ozon = re.findall(r'(https://cdn\d?\.ozone\.ru/[^\s"\']+\.(?:jpg|jpeg|png|webp))', html)
    return wb + ozon


def wb_image_url(nm_id: int) -> str:
    """Формирует URL картинки на CDN WB."""
    short = nm_id // 100000
    # basket распределение — приблизительная формула
    if short <= 143: basket = "01"
    elif short <= 287: basket = "02"
    elif short <= 431: basket = "03"
    elif short <= 719: basket = "04"
    elif short <= 1007: basket = "05"
    elif short <= 1061: basket = "06"
    elif short <= 1115: basket = "07"
    elif short <= 1169: basket = "08"
    elif short <= 1313: basket = "09"
    elif short <= 1601: basket = "10"
    elif short <= 1655: basket = "11"
    elif short <= 1919: basket = "12"
    elif short <= 2045: basket = "13"
    elif short <= 2189: basket = "14"
    elif short <= 2405: basket = "15"
    elif short <= 2621: basket = "16"
    elif short <= 2837: basket = "17"
    elif short <= 3053: basket = "18"
    elif short <= 3269: basket = "19"
    elif short <= 3485: basket = "20"
    elif short <= 3701: basket = "21"
    elif short <= 3917: basket = "22"
    elif short <= 4133: basket = "23"
    elif short <= 4349: basket = "24"
    elif short <= 4565: basket = "25"
    elif short <= 4781: basket = "26"
    else: basket = "27"
    vol = nm_id // 100000
    part = nm_id // 1000
    return f"https://basket-{basket}.wbbasket.ru/vol{vol}/part{part}/{nm_id}/images/big/1.webp"


def fetch_image(nm_id: int) -> bytes | None:
    """Пробует разные basket-серверы (формула не на 100% точная)."""
    short = nm_id // 100000
    primary_basket_idx = None
    primary_url = wb_image_url(nm_id)
    data = http_bytes(primary_url)
    if data and len(data) > 1000:
        return data
    # Пробуем все basket-NN до 27
    for basket in range(1, 28):
        url = f"https://basket-{basket:02d}.wbbasket.ru/vol{short}/part{nm_id//1000}/{nm_id}/images/big/1.webp"
        if url == primary_url:
            continue
        data = http_bytes(url)
        if data and len(data) > 1000:
            return data
    return None


def webp_to_jpg(webp_bytes: bytes) -> bytes | None:
    try:
        from PIL import Image
        img = Image.open(BytesIO(webp_bytes)).convert("RGB")
        img.thumbnail((900, 900))
        out = BytesIO()
        img.save(out, "JPEG", quality=85, optimize=True)
        return out.getvalue()
    except Exception:
        return None


def find_photo(query: str) -> tuple[bytes, str] | None:
    """Yandex.Images → первая годная картинка → JPEG."""
    urls = yandex_image_search(query)
    for u in urls[:5]:
        data = http_bytes(u)
        if not data or len(data) < 1500:
            continue
        # Если webp — конвертируем
        if u.endswith(".webp"):
            jpg = webp_to_jpg(data)
            if jpg:
                return jpg, u
        else:
            # Уже JPG/PNG — приводим к JPG через PIL для нормализации
            try:
                from PIL import Image
                img = Image.open(BytesIO(data)).convert("RGB")
                img.thumbnail((900, 900))
                out = BytesIO()
                img.save(out, "JPEG", quality=85, optimize=True)
                return out.getvalue(), u
            except Exception:
                continue
    return None


def query_for(p: dict) -> str:
    """Строит поисковый запрос для товара."""
    name = p["name"]
    # Чай — просто как есть, бренд уже в имени
    return name


def main(force_revalidate: bool = False):
    """Если force_revalidate=True — переобрабатывает ВСЕ товары (включая уже с фото)
    через trusted-источники. Несматченные обнуляются."""
    data = json.loads(PRODUCTS.read_text(encoding="utf-8"))
    if force_revalidate:
        targets = [p for p in data["products"]
                   if p.get("category") in ("tea", "syrup", "milk")
                   and "Помпа" not in p.get("name", "")]
    else:
        targets = [p for p in data["products"]
                   if not p.get("photo") and p.get("category") in ("tea", "syrup", "milk")
                   and "Помпа" not in p.get("name", "")]
    print(f"Целевых товаров: {len(targets)} (revalidate={force_revalidate})")

    matched = 0
    cleared = 0
    for i, p in enumerate(targets, 1):
        if i % 30 == 0:
            print(f"  [{i}/{len(targets)}] matched={matched}, cleared={cleared}")
        q = query_for(p)
        result = find_photo(q)
        if not result:
            # Не нашли в trusted — удаляем старое фото если было
            if p.get("photo"):
                old = PHOTOS / f"{p['id']}.jpg"
                if old.exists(): old.unlink()
                p["photo"] = None
                cleared += 1
            time.sleep(0.4)
            continue
        jpg_bytes, src_url = result
        dest = PHOTOS / f"{p['id']}.jpg"
        dest.write_bytes(jpg_bytes)
        p["photo"] = f"photos/products/{p['id']}.jpg"
        matched += 1
        if matched % 20 == 0:
            PRODUCTS.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        time.sleep(0.25)

    PRODUCTS.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"\n=== Итого ===")
    print(f"Сматчено через WB/Ozon: {matched}")
    print(f"Удалено сомнительных старых фото: {cleared}")
    print(f"Остались без фото: {len(targets) - matched}")


if __name__ == "__main__":
    import sys
    main(force_revalidate=("--revalidate" in sys.argv))


if __name__ == "__main__":
    main()
