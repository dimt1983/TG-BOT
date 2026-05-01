"""tasteabrew.ru через collection-страницы (per-group matching).

Парсит каждую коллекцию (To Go, Horeca-чайник, Iced, Pro листовой),
для каждого TMA-Tasteabrew товара ищет лучший матч в соответствующих
коллекциях и применяет фото с порогом 0.5.

Лучше sitemap-подхода тем, что imgs идут с product-preview карточек
(оптимизированный srcset на InSales CDN), и группировка по форм-фактору.
"""
import urllib.request, urllib.parse, re, json
from pathlib import Path
from io import BytesIO

PRODUCTS = Path("/root/projects/ai-agents-rb/BOT_TG/tma_static/products.json")
PHOTOS = Path("/root/projects/ai-agents-rb/BOT_TG/tma_static/photos/products")
PHOTOS.mkdir(parents=True, exist_ok=True)

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120",
      "Accept": "*/*", "Accept-Language": "ru,en;q=0.9"}

# collection_slug → TMA subcategory hint (приоритет при равном score)
COLLECTION_TO_SUB = {
    "goryachie-to-go-chai":             "tea_tasteabrew_togo",
    "avtorskiy-chay-tasteabrew-horeca": "tea_tasteabrew_loose",
    "holodnye-iced-chai":               "tea_tasteabrew_iced",
    "chay-tasteabrew-pro":              "tea_tasteabrew_loose",  # PRO 250gr leaf
}


def http_text(url: str) -> str:
    req = urllib.request.Request(url, headers=UA)
    return urllib.request.urlopen(req, timeout=20).read().decode("utf-8", "replace")


def http_bytes(url: str) -> bytes | None:
    safe = urllib.parse.quote(url, safe=":/?&=#%@")
    try:
        req = urllib.request.Request(safe, headers=UA)
        return urllib.request.urlopen(req, timeout=20).read()
    except Exception as e:
        print(f"    ! download fail: {e}")
        return None


def parse_collection(slug: str) -> list[dict]:
    """Возвращает [{name, image, sub_hint}, ...]"""
    url = f"https://www.tasteabrew.ru/collection/{slug}"
    # Retry up to 3 раз — DNS-флапы случаются
    for attempt in range(3):
        try:
            html = http_text(url)
            break
        except Exception as e:
            print(f"    ! retry {attempt+1}/3 ({slug}): {e}")
    else:
        return []
    pat = re.compile(r'data-product-id="(\d+)"[^>]*class="product-preview[^"]*"', re.S)
    matches = list(pat.finditer(html))
    items = []
    for i, m in enumerate(matches):
        start = m.start()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(html)
        block = html[start:end]
        title_m = re.search(r'<a[^>]+href="/product/[^"]+"[^>]*>\s*([^<]+?)\s*</a>', block)
        if not title_m:
            continue
        name = title_m.group(1).strip()
        low = name.lower()
        # Пропускаем только саму посуду (стекло/металлическая крышка),
        # но не «для стаканчика» (это форм-фактор чая Sito-350)
        if "чайник стекло" in low or "стакан двухслойный" in low or "стакан прозрач" in low:
            continue
        # Извлекаем именно оригинальную картинку (не InSales rs:fit вариант),
        # чтобы получить полное качество
        orig_m = re.search(
            r'data-srcset="https://static\.insales-cdn\.com/r/[^/]+/[^/]+/[^/]+/plain/(images/products/[^"@]+)',
            block,
        )
        if orig_m:
            img = "https://static.insales-cdn.com/" + orig_m.group(1)
        else:
            cdn_m = re.search(r'(https://static\.insales-cdn\.com/images/products/[^"\s@]+)', block)
            img = cdn_m.group(1) if cdn_m else None
        if not img:
            continue
        items.append({"name": name, "image": img, "sub_hint": COLLECTION_TO_SUB.get(slug)})
    return items


_NOISE_STEMS = {
    "ЧАЙ", "ЧАЙНЫ", "ЧАЙНИ", "TASTE", "RESTO", "RESTA", "PRO", "TOGO",
    "HOREC", "ICED", "АЙС",
    "СТАКА", "ЧЕРН", "ЗЕЛЕН", "БЕЛЫЙ", "ЖЕЛТЫ",
    "ПАК", "ПАКЕТ", "ШТКОР", "КОР",
    "ФРУКТ", "ТРАВЯ", "ТРАВ",
    "MULL", "WINE", "WITH", "MIX",
    "SITO", "ДЛЯ", "ВКУС", "НОВ", "ХИТ",
    "АССОР",
}


def _stem(w: str) -> str:
    """Грубая нормализация: usable Russian stem (5 chars) + EN nouns lowercased upper."""
    # обрезаем хвостовые цифры/буквенные суффиксы 30ПАК → ПАК
    w = re.sub(r"^\d+", "", w)  # leading digits
    w = re.sub(r"\d+$", "", w)  # trailing digits
    if len(w) <= 4:
        return w
    return w[:5]


def kw(s: str) -> set[str]:
    s = s.upper()
    s = re.sub(r"[ЁЕ]", "Е", s)
    s = re.sub(r"[^\w\s]", " ", s)
    out = set()
    for w in s.split():
        if w.isdigit() or len(w) < 3:
            continue
        st = _stem(w)
        if len(st) < 3 or st in _NOISE_STEMS:
            continue
        out.add(st)
    return out


# RU↔EN/синонимы — теперь работаем со стемами (5 chars upper)
# Ключи и значения должны быть стемами
ALIASES = {
    "АПЕЛЬ": "АПЕЛЬ", "ORANG": "АПЕЛЬ",
    "МАНГО": "МАНГО", "MANGO": "МАНГО",
    "СМОРО": "СМОРО", "CURRA": "СМОРО",
    "МАСАЛ": "МАСАЛ", "MASAL": "МАСАЛ",
    "ИММУН": "ИММУН", "IMMUN": "ИММУН",
    "КЕДРО": "КЕДРО", "CEDAR": "КЕДРО",
    "ЧАГА": "ЧАГА", "ЧАГОЙ": "ЧАГА", "CHAGA": "ЧАГА",
    "ЯБЛОК": "ЯБЛОК", "ЯБЛОЧ": "ЯБЛОК", "APPLE": "ЯБЛОК",
    "МОЖЖЕ": "МОЖЖЕ", "JUNIP": "МОЖЖЕ",
    "САГАН": "САГАН", "ДАЙЛЯ": "ДАЙЛЯ", "ПУЭР": "ПУЭР",
    "КИВИ": "КИВИ", "KIWI": "КИВИ",
    "КЛУБН": "КЛУБН", "STRAW": "КЛУБН",
    "УЛУН": "УЛУН", "OOLON": "УЛУН",
    "МОЛОЧ": "МОЛОЧ", "MILK": "МОЛОЧ",
    "МАНДА": "МАНДА", "MANDA": "МАНДА",
    "МЯТНЫ": "МЯТА", "МЯТНА": "МЯТА", "МЯТА": "МЯТА", "МЯТОЙ": "МЯТА", "MINT": "МЯТА",
    "МАЛИН": "МАЛИН", "RASPB": "МАЛИН",
    "РОЙБУ": "РОЙБУ", "ROOIB": "РОЙБУ",
    "ШИПОВ": "ШИПОВ", "ROSE": "ШИПОВ",
    "ЧАБРЕ": "ЧАБРЕ", "THYME": "ЧАБРЕ",
    "БЕРГА": "БЕРГА", "BERGA": "БЕРГА",
    "КОРОЛ": "КОРОЛ", "ROYAL": "КОРОЛ",
    "ЯГОДЫ": "ЯГОДЫ", "ЯГОДН": "ЯГОДЫ", "BERRI": "ЯГОДЫ",
    "ЛЕСНЫ": "ЛЕСНЫ", "FORES": "ЛЕСНЫ",
    "ГЛИНТ": "ГЛИНТ",
    "ПРЯНЫ": "ПРЯНЫ", "SPICY": "ПРЯНЫ",
    "ГОЛУБ": "ГОЛУБ", "BLUE": "ГОЛУБ",
    "ЧЕРНА": "ЧЕРНА", "BLACK": "ЧЕРНА",
    "СЛАДК": "СЛАДК", "SWEET": "СЛАДК",
    "ВИШНЯ": "ВИШНЯ", "ВИШНЕ": "ВИШНЯ",
    "ШОКОЛ": "ШОКОЛ", "CHOCO": "ШОКОЛ",
    "ДЫНЯ": "ДЫНЯ", "ДЫНЕЙ": "ДЫНЯ",
    "АССАМ": "АССАМ", "ASSAM": "АССАМ",
    "СЕНЧА": "СЕНЧА",
    "ЦЕЙЛО": "ЦЕЙЛО", "ЦЕЙЛОН": "ЦЕЙЛО",
    "ХРИЗА": "ХРИЗА",
    "ИВАН": "ИВАН",
    "АРБУЗ": "АРБУЗ",
    "КЛЮКВ": "КЛЮКВ",
    "МАРАК": "МАРАК",
    "ТРОПИ": "ТРОПИ",
    "МОХИТ": "МОХИТ",
    "ЛИПА": "ЛИПА", "ЛИПОЙ": "ЛИПА",
    "ЖАСМИ": "ЖАСМИ",
    "МЕДОВ": "МЕДОВ",
    "ПРЕМИ": "ПРЕМИ",
    "ИМБИР": "ИМБИР", "GINGE": "ИМБИР",
    "ОБЛЕП": "ОБЛЕП",
    "ЗЕМЛЯ": "ЗЕМЛЯ",
    "АССОР": "АССОР",
    "ЭРЛБЕ": "БЕРГА",  # «Эрлберг» = эрл-грей бергамот
    "ИМУНН": "ИММУН",  # частая опечатка «Имунный» → ИММУН
}
def kw_aliased(s: str) -> set[str]:
    return {ALIASES.get(w, w) for w in kw(s)}


def score(a: str, b: str) -> float:
    ka, kb = kw_aliased(a), kw_aliased(b)
    if not ka or not kb:
        return 0.0
    return len(ka & kb) / max(len(ka), len(kb))


def webp_to_jpg(buf: bytes) -> bytes | None:
    try:
        from PIL import Image
        img = Image.open(BytesIO(buf)).convert("RGB")
        img.thumbnail((900, 900))
        out = BytesIO()
        img.save(out, "JPEG", quality=88, optimize=True)
        return out.getvalue()
    except Exception as e:
        print(f"    ! convert: {e}")
        return None


def main():
    print("=== Парсю коллекции tasteabrew.ru ===")
    pool = []
    for slug in COLLECTION_TO_SUB:
        items = parse_collection(slug)
        print(f"  {slug:38} → {len(items)}")
        pool.extend(items)
    print(f"Всего источников: {len(pool)}\n")

    data = json.loads(PRODUCTS.read_text(encoding="utf-8"))
    targets = [p for p in data["products"]
               if p.get("subcategory", "").startswith("tea_tasteabrew_")]
    print(f"TMA-Tasteabrew товаров: {len(targets)}\n")

    cache = {}
    matched, replaced, skipped = 0, 0, 0
    for p in targets:
        # Скоринг по всему пулу + бонус +0.05 за совпадение sub_hint,
        # но только когда есть реальное пересечение ключевых слов
        best, best_s = None, 0.0
        for r in pool:
            s = score(p["name"], r["name"])
            if s > 0 and r.get("sub_hint") == p.get("subcategory"):
                s += 0.05
            if s > best_s:
                best_s, best = s, r
        if not best or best_s < 0.55:
            print(f"  ✗ [{best_s:.2f}] {p['name'][:55]}  | best: {best['name'][:45] if best else '—'}")
            skipped += 1
            continue
        url = best["image"]
        if url not in cache:
            raw = http_bytes(url)
            cache[url] = webp_to_jpg(raw) if raw else None
        if not cache[url]:
            skipped += 1
            continue
        had_photo = bool(p.get("photo"))
        dest = PHOTOS / f"{p['id']}.jpg"
        dest.write_bytes(cache[url])
        p["photo"] = f"photos/products/{p['id']}.jpg"
        if had_photo:
            replaced += 1
        else:
            matched += 1
        print(f"  ✓ [{best_s:.2f}] {p['name'][:55]:<55} → {best['name'][:45]}")

    PRODUCTS.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n=== Итого ===")
    print(f"Новых фото:       {matched}")
    print(f"Перепривязано:    {replaced}")
    print(f"Без матча (<0.5): {skipped}")


if __name__ == "__main__":
    main()
