"""
sync_all_photos.py — единый скрипт синка фото со всех источников через sitemap + og:image:
- Botanika syrups (botanikastories.ru)
- Herbarista syrups (herbarista.store)
- Roastberry coffee (roastberry.coffee)
- Tasteabrew tea (tasteabrew.ru)
- GreenMilk milk (greenmilk.ru)

Алгоритм:
1. Тянем sitemap.xml у каждого
2. Фильтруем product-страницы по паттерну URL
3. На каждой странице берём og:title и og:image
4. Greedy fuzzy-match с TMA-товарами этой категории/бренда
5. Скачиваем фото, сохраняем в tma_static/photos/products/{tma_id}.jpg
6. Обновляем products.json
"""
import json
import re
import urllib.request
import urllib.error
from pathlib import Path

ROOT = Path(__file__).parent
PRODUCTS_JSON = ROOT / "tma_static" / "products.json"
PHOTOS_DIR = ROOT / "tma_static" / "photos" / "products"
PHOTOS_DIR.mkdir(parents=True, exist_ok=True)

UA = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8",
}


def http_get(url: str, timeout: int = 20) -> str | None:
    try:
        req = urllib.request.Request(url, headers=UA)
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read().decode("utf-8", "replace")
    except Exception as e:
        print(f"  ! GET {url}: {e}")
        return None


def http_get_bytes(url: str, timeout: int = 20) -> bytes | None:
    try:
        req = urllib.request.Request(url, headers=UA)
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read()
    except Exception as e:
        print(f"  ! GET {url}: {e}")
        return None


def get_sitemap_urls(sitemap_url: str) -> list[str]:
    """Возвращает все URL из sitemap.xml (рекурсивно если sitemapindex)."""
    body = http_get(sitemap_url)
    if not body:
        return []
    urls = []
    if "<sitemapindex" in body:
        # вложенные sitemap'ы
        for m in re.finditer(r"<loc>([^<]+)</loc>", body):
            sub = m.group(1).strip()
            urls.extend(get_sitemap_urls(sub))
    else:
        for m in re.finditer(r"<loc>([^<]+)</loc>", body):
            urls.append(m.group(1).strip())
    return urls


def parse_og(html: str) -> tuple[str | None, str | None, str | None]:
    """Возвращает (title, image, description) из og: meta-тегов."""
    def find_meta(prop):
        m = re.search(
            rf'<meta\s+(?:[^>]*?\bproperty="{prop}"[^>]*?\bcontent="([^"]*)"|[^>]*?\bcontent="([^"]*)"[^>]*?\bproperty="{prop}")',
            html,
        )
        return (m.group(1) or m.group(2)).strip() if m else None
    return (find_meta("og:title"), find_meta("og:image"), find_meta("og:description"))


# ============================================================
# Простой fuzzy-matcher (общий)
# ============================================================
NOISE = {"BOTANIKA","БОТАНИКА","ГЕРБАРИСТА","HERBARISTA","ROASTBERRY","TASTEABREW","NIKTEA","ALTHAUS",
         "GREEN","MILK","GREENMILK","РОЙБУШ","СИРОП","ЧАЙ","КОФЕ","КГ","Г","Л","МЛ","ШТ","УПАК",
         "ROASTERS","COFFEE","ЛИСТОВОЙ","ПАКЕТ","ПИРАМИДКИ","РАСС","ПОРЦ","КУПАЖ"}

ALIASES = {
    "ВАНИЛЬ":"ВАНИЛЬ","ВАНИЛЬНАЯ":"ВАНИЛЬ","ВАНИЛЬНЫЙ":"ВАНИЛЬ",
    "ОРЕХ":"ОРЕХ","ОРЕХОВЫЙ":"ОРЕХ","ОРЕХА":"ОРЕХ",
    "АПЕЛЬСИН":"АПЕЛЬСИН","ОРАНЖ":"АПЕЛЬСИН","ORANGE":"АПЕЛЬСИН",
    "КАРАМЕЛЬ":"КАРАМЕЛЬ","КАРАМЕЛИ":"КАРАМЕЛЬ",
    "ШОКОЛАД":"ШОКОЛАД","ШОКОЛАДНЫЙ":"ШОКОЛАД",
    "КОКОС":"КОКОС","КОКОСОВЫЙ":"КОКОС","COCONUT":"КОКОС",
    "МИНДАЛЬ":"МИНДАЛЬ","МИНДАЛЬНЫЙ":"МИНДАЛЬ","ALMOND":"МИНДАЛЬ",
    "ЛЕСНОЙ":"ЛЕСНОЙ","ОРЕШКОВЫЙ":"ОРЕХ","ФУНДУК":"ФУНДУК",
    "БАНАН":"БАНАН","БАНАНОВЫЙ":"БАНАН","BANANA":"БАНАН",
    "КЛУБНИКА":"КЛУБНИКА","КЛУБНИЧНАЯ":"КЛУБНИКА","STRAWBERRY":"КЛУБНИКА",
    "СОЯ":"СОЯ","СОЕВЫЙ":"СОЯ","SOY":"СОЯ",
    "ОВЁС":"ОВЕС","ОВЕС":"ОВЕС","OAT":"ОВЕС","ОВСЯНЫЙ":"ОВЕС",
    "МЯТА":"МЯТА","МЯТНЫЙ":"МЯТА","MINT":"МЯТА",
    "ЛАВАНДА":"ЛАВАНДА","ЛАВАНДОВЫЙ":"ЛАВАНДА",
    "ПЕРСИК":"ПЕРСИК","PEACH":"ПЕРСИК",
    "ВИШНЯ":"ВИШНЯ","ВИШНЁВАЯ":"ВИШНЯ","CHERRY":"ВИШНЯ",
    "ИМБИРЬ":"ИМБИРЬ","ИМБИРНЫЙ":"ИМБИРЬ","GINGER":"ИМБИРЬ",
    "МАЛИНА":"МАЛИНА","МАЛИНОВЫЙ":"МАЛИНА",
    "АРБУЗ":"АРБУЗ","WATERMELON":"АРБУЗ",
    "ДЫНЯ":"ДЫНЯ","MELON":"ДЫНЯ",
    "АНАНАС":"АНАНАС","PINEAPPLE":"АНАНАС",
    "АМАРЕТТО":"АМАРЕТТО","AMARETTO":"АМАРЕТТО",
    "АЙРИШ":"АЙРИШ","IRISH":"АЙРИШ",
    "БАЗИЛИК":"БАЗИЛИК","BASIL":"БАЗИЛИК",
    "ГРЕНАДИН":"ГРЕНАДИН","GRENADINE":"ГРЕНАДИН",
    "ЛИЧИ":"ЛИЧИ","LYCHEE":"ЛИЧИ","ЛЫЧИ":"ЛИЧИ",
    "МАНГО":"МАНГО","MANGO":"МАНГО",
    "МАРАКУЙЯ":"МАРАКУЙЯ","PASSION":"МАРАКУЙЯ",
    "ПОПКОРН":"ПОПКОРН","POPCORN":"ПОПКОРН",
    "ТАРХУН":"ТАРХУН",
    "ФИСТАШКА":"ФИСТАШКА","ФИСТАШКОВЫЙ":"ФИСТАШКА","PISTACHIO":"ФИСТАШКА",
    "ЕЖЕВИКА":"ЕЖЕВИКА","BLACKBERRY":"ЕЖЕВИКА",
    "ГРУША":"ГРУША","PEAR":"ГРУША",
    "ЯБЛОКО":"ЯБЛОКО","APPLE":"ЯБЛОКО","ЯБЛОЧНЫЙ":"ЯБЛОКО",
    "ЛИМОН":"ЛИМОН","ЛАЙМ":"ЛАЙМ","LIME":"ЛАЙМ","LEMON":"ЛИМОН",
    "БУРБОНСКАЯ":"БУРБОН","BOURBON":"БУРБОН","БУРБОНСКИЙ":"БУРБОН",
    "СЛИВОЧНАЯ":"СЛИВКИ","СЛИВКИ":"СЛИВКИ","CREAM":"СЛИВКИ",
    "ИРИСКА":"ИРИСКА","TOFFEE":"ИРИСКА",
    "СОЛЕНАЯ":"СОЛЁНАЯ","SALTED":"СОЛЁНАЯ","СОЛЁНАЯ":"СОЛЁНАЯ",
    "ТЫКВА":"ТЫКВА","PUMPKIN":"ТЫКВА",
}

def kw(s: str) -> set[str]:
    s = s.upper()
    s = re.sub(r"[ЁЕ]", "Е", s)
    s = re.sub(r"[^А-ЯA-Z0-9\s]+", " ", s)
    out = set()
    for w in s.split():
        if len(w) < 3:
            continue
        out.add(ALIASES.get(w, w))
    return out - NOISE


def score(a: str, b: str) -> float:
    ka = kw(a); kb = kw(b)
    if not ka or not kb:
        return 0.0
    return len(ka & kb) / max(len(ka), len(kb))


def is_product_url(u: str, hints: list[str]) -> bool:
    return any(h in u for h in hints)


def harvest_pages(sitemap_url: str, product_url_hints: list[str], max_pages: int = 200) -> list[dict]:
    """Идёт по sitemap, на каждой product-странице берёт og:title/image. Возвращает список dict."""
    print(f"  → sitemap {sitemap_url}")
    urls = get_sitemap_urls(sitemap_url)
    product_urls = [u for u in urls if is_product_url(u, product_url_hints)]
    print(f"  → product URL: {len(product_urls)}")
    out = []
    for i, u in enumerate(product_urls[:max_pages]):
        if i % 20 == 0 and i:
            print(f"    {i}/{min(len(product_urls), max_pages)}...")
        html = http_get(u)
        if not html:
            continue
        title, img, desc = parse_og(html)
        if title and img:
            out.append({"title": title, "image": img, "desc": desc or "", "url": u})
    return out


def match_and_attach(tma_subset: list[dict], refs: list[dict], thresh: float = 0.4) -> int:
    """Привязывает refs к tma_subset. Возвращает кол-во привязанных."""
    cache: dict[str, Path] = {}
    matched = 0
    for t in tma_subset:
        best, best_s = None, 0.0
        for r in refs:
            s = score(t["name"], r["title"])
            if s > best_s:
                best_s, best = s, r
        if not best or best_s < thresh:
            continue
        url = best["image"]
        if url not in cache:
            data = http_get_bytes(url)
            if not data or len(data) < 500:
                cache[url] = None
                continue
            tmp = PHOTOS_DIR / f"_ref_{abs(hash(url))}.jpg"
            tmp.write_bytes(data)
            cache[url] = tmp
        if not cache[url]:
            continue
        dest = PHOTOS_DIR / f"{t['id']}.jpg"
        dest.write_bytes(cache[url].read_bytes())
        t["photo"] = f"photos/products/{t['id']}.jpg"
        if best.get("desc"):
            t["description"] = best["desc"][:500]
        t["_source"] = best["url"]
        matched += 1
        print(f"    ✓ [{best_s:.2f}] {t['name'][:38]:<38} → {best['title'][:55]}")
    for tmp in cache.values():
        if tmp and tmp.exists():
            tmp.unlink()
    return matched


def main():
    data = json.loads(PRODUCTS_JSON.read_text(encoding="utf-8"))
    products = data["products"]

    SOURCES = [
        # (название, sitemap, product url hints, фильтр TMA)
        ("BOTANIKA", "https://botanikastories.ru/sitemap.xml",
         ["/product/"],
         lambda p: p.get("subcategory") == "syr_botanika"),
        ("Herbarista", "https://herbarista.store/sitemap.xml",
         ["/products/", "/product/"],
         lambda p: p.get("subcategory") == "syr_herbarista"),
        ("Roastberry.coffee", "https://roastberry.coffee/sitemap.xml",
         ["/coffee/", "/product/", "/shop/"],
         lambda p: p.get("category") == "coffee" and not p.get("photo")),
        ("Tasteabrew (Restoranica)", "https://www.tasteabrew.ru/sitemap.xml",
         ["/product/"],
         lambda p: p.get("subcategory") == "tea_rest_tea"),
    ]

    summary = []
    for name, sitemap, hints, filt in SOURCES:
        print(f"\n=== {name} ===")
        tma_subset = [p for p in products if filt(p)]
        if not tma_subset:
            print(f"  TMA-товаров: 0 — пропускаем")
            continue
        print(f"  TMA-товаров: {len(tma_subset)}")
        refs = harvest_pages(sitemap, hints, max_pages=120)
        print(f"  Каталог-страниц с og:image: {len(refs)}")
        n = match_and_attach(tma_subset, refs)
        summary.append((name, n, len(tma_subset)))

    # GreenMilk — нет sitemap, но мало товаров (5). Ищем product-страницы вручную:
    print(f"\n=== GreenMilk ===")
    greenmilk_refs = []
    # Перебираем известные slugs
    for slug in ["kokos","mindal","banan","klubnika","oves","funduk","vanil","soya"]:
        for path_pattern in [f"https://greenmilk.ru/catalog/product/{slug}/",
                             f"https://greenmilk.ru/product/{slug}/",
                             f"https://greenmilk.ru/{slug}/",
                             f"https://greenmilk.ru/catalog/{slug}/"]:
            html = http_get(path_pattern)
            if html:
                t, img, desc = parse_og(html)
                if t and img:
                    greenmilk_refs.append({"title": t, "image": img, "desc": desc or "", "url": path_pattern})
                    break
    milk_subset = [p for p in products if p.get("category") == "milk"]
    print(f"  TMA-товаров: {len(milk_subset)}, refs: {len(greenmilk_refs)}")
    if milk_subset and greenmilk_refs:
        n = match_and_attach(milk_subset, greenmilk_refs, thresh=0.3)
        summary.append(("GreenMilk", n, len(milk_subset)))

    PRODUCTS_JSON.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"\n=== ИТОГО ===")
    for name, n, tot in summary:
        print(f"  {name}: {n} / {tot}")


if __name__ == "__main__":
    main()
