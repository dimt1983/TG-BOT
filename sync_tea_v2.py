"""Парсит каталоги alephtrade.com по фасовкам и точно матчит с TMA-чаями."""
import urllib.request, re, json
from pathlib import Path

PRODUCTS_JSON = Path("/root/projects/ai-agents-rb/BOT_TG/tma_static/products.json")
PHOTOS_DIR = Path("/root/projects/ai-agents-rb/BOT_TG/tma_static/photos/products")
PHOTOS_DIR.mkdir(parents=True, exist_ok=True)

UA = {"User-Agent":"Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0",
      "Accept":"text/html,application/xhtml+xml","Accept-Language":"ru","Referer":"https://www.google.com/"}

# фасовка_код в TMA (loose/cup/pot/pyr) → URL alephtrade'a и название
SOURCES = {
    # ALTHAUS
    "tea_althaus_loose": "https://alephtrade.com/brands/tea/althaus/list_althaus",
    "tea_althaus_cup":   "https://alephtrade.com/brands/tea/althaus/paket2_althaus",
    "tea_althaus_pot":   "https://alephtrade.com/brands/tea/althaus/paket1_althaus",
    "tea_althaus_pyr":   "https://alephtrade.com/brands/tea/althaus/paket3_althaus",
    # NIKTEA
    "tea_niktea_loose": "https://alephtrade.com/brands/tea/niktea/list_niktea",
    "tea_niktea_cup":   "https://alephtrade.com/brands/tea/niktea/paket2_niktea",
    "tea_niktea_pot":   "https://alephtrade.com/brands/tea/niktea/paket1_niktea",
    "tea_niktea_top":   "https://alephtrade.com/brands/tea/niktea/top_selection",
}


def http_get(url):
    try:
        req = urllib.request.Request(url, headers=UA)
        raw = urllib.request.urlopen(req, timeout=20).read()
        # alephtrade в windows-1251
        try:
            return raw.decode('windows-1251', errors='replace')
        except Exception:
            return raw.decode('utf-8', errors='replace')
    except Exception as e:
        print(f"  ! {url}: {e}")
        return None


def http_get_bytes(url):
    try:
        req = urllib.request.Request(url, headers=UA)
        return urllib.request.urlopen(req, timeout=20).read()
    except Exception as e:
        print(f"  ! {url}: {e}")
        return None


def parse_catalog(html, base_url):
    """Парсит alephtrade-каталог. Структура (повторяющаяся):
       <img class="img_tee" src="/images/...">
       <p class="product-head"><span><b>ENG | РУС</b></span></p>
       <p class="product-short">Описание.</p>
    """
    if not html:
        return []
    items = []
    # Каждая карточка: img с классом img_tee + product-head + product-short
    block_re = re.compile(
        r'<img[^>]+class="img_tee"[^>]+src="(/images/[^"]+\.(?:jpg|png|jpeg))"[^>]*>.*?'
        r'<p[^>]*class="product-head"[^>]*>(.*?)</p>'
        r'(?:.*?<p[^>]*class="product-short"[^>]*>(.*?)</p>)?',
        re.I | re.S
    )
    tag_re = re.compile(r'<[^>]+>', re.S)
    for m in block_re.finditer(html):
        img_path = m.group(1)
        head = tag_re.sub('', m.group(2)).strip()
        short = tag_re.sub('', m.group(3) or '').strip()
        # head формата "ENG | РУС"
        if '|' in head:
            parts = head.split('|', 1)
            rus_name = parts[1].strip()
        else:
            rus_name = head.strip()
        if not rus_name or len(rus_name) < 3:
            continue
        items.append({
            "name": rus_name,
            "image": "https://alephtrade.com" + img_path,
            "desc": short[:300],
        })
    # Дедуп
    seen = set()
    uniq = []
    for it in items:
        if it["name"] in seen: continue
        seen.add(it["name"])
        uniq.append(it)
    return uniq


# Match
NOISE = {"ALTHAUS","NIKTEA","ЧАЙ","ЧЕРНЫЙ","ЧЁРНЫЙ","ЗЕЛЕНЫЙ","ЗЕЛЁНЫЙ","БЕЛЫЙ","ROOIBOS","РОЙБУШ",
         "ЛИСТОВОЙ","ПИРАМИДКИ","ПАКЕТ","ПАК","Г","КГ","ГР","ШТ","РАСС","ПОРЦ","КУПАЖ"}
ALIASES = {"МЭЛЕНГ":"МЕЛЕНГ","ТИНГ":"ТИН","ГРЕЙ":"ГРЕЙ","GREY":"ГРЕЙ","ГРАФ":"ГРЕЙ","ЭРЛ":"ГРЕЙ",
           "СЕНЧА":"СЕНЧА","ЖАСМИН":"ЖАСМИН","ЖАСМИНОВЫЙ":"ЖАСМИН","ВАНИЛЬ":"ВАНИЛЬ",
           "ИРГАЧЕФФЕ":"ИРГАЧИФ","ИРГАЧИФФ":"ИРГАЧИФ","YIRGACHEFFE":"ИРГАЧИФ",
           "ДРАКОНА":"ДРАКОН","ДРАКОНИЙ":"ДРАКОН","DRAGON":"ДРАКОН","ЖЕМЧУЖИНЫ":"ЖЕМЧУЖИНЫ",
           "ЦЕЙЛОН":"ЦЕЙЛОН","CEYLON":"ЦЕЙЛОН","ЯБЛОКО":"ЯБЛОКО","ЯБЛОЧНЫЙ":"ЯБЛОКО",
           "АССАМ":"АССАМ","ASSAM":"АССАМ","ВЫСОКОГОРНЫЙ":"ВЫСОКОГОРНЫЙ","КЕНИЯ":"КЕНИЯ",
           "СУПРИМ":"СУПРИМ","SUPREME":"СУПРИМ","МАЛИНА":"МАЛИНА","МАЛИНОВЫЙ":"МАЛИНА",
           "АПЕЛЬСИН":"АПЕЛЬСИН","ОРАНЖ":"АПЕЛЬСИН","КАРАМЕЛЬ":"КАРАМЕЛЬ","СОЛЕНАЯ":"СОЛЕНАЯ",
           "ВИШНЯ":"ВИШНЯ","ВИШНЁВАЯ":"ВИШНЯ","КЛУБНИКА":"КЛУБНИКА","КЛУБНИЧНАЯ":"КЛУБНИКА",
           "ИМБИРЬ":"ИМБИРЬ","ИМБИРНЫЙ":"ИМБИРЬ","РОМАШКА":"РОМАШКА","РОМАШКОВЫЙ":"РОМАШКА",
           "МЯТА":"МЯТА","МЯТНЫЙ":"МЯТА","ЛЕМОНГРАСС":"ЛЕМОНГРАСС",
           "УЛУН":"УЛУН","OOLONG":"УЛУН","МОЛОЧНЫЙ":"МОЛОЧНЫЙ",
           "БЕРГАМОТ":"БЕРГАМОТ","BERGAMOT":"БЕРГАМОТ",
           "СМОРОДИНА":"СМОРОДИНА","ЯГОДЫ":"ЯГОДЫ","BERRIES":"ЯГОДЫ"}


def kw(s):
    s = s.upper()
    s = re.sub(r"[ЁЕ]","Е",s)
    s = re.sub(r"[^\w\s]"," ",s)
    out = set()
    for w in s.split():
        if len(w) < 3: continue
        out.add(ALIASES.get(w,w))
    return out - NOISE


def score(a,b):
    ka,kb = kw(a), kw(b)
    if not ka or not kb: return 0.0
    return len(ka & kb) / max(len(ka), len(kb))


def main():
    data = json.loads(PRODUCTS_JSON.read_text(encoding="utf-8"))

    # Добавим подкатегорию tea_niktea_top если её нет
    has_top = any(s["id"] == "tea_niktea_top" for s in data["subcategories"])
    if not has_top:
        data["subcategories"].append({"id":"tea_niktea_top","parent":"tea","name":"NIKTEA · 🌟 Top Selection"})

    # Парсим все 8 каталогов
    catalog_data = {}  # subcat_id → list of {name,image,desc}
    for subcat, url in SOURCES.items():
        print(f"\n=== {subcat} ===")
        html = http_get(url)
        items = parse_catalog(html, url)
        catalog_data[subcat] = items
        print(f"  Распарсено: {len(items)}")
        for it in items[:5]:
            print(f"    - {it['name'][:50]:<50} {it['image'][:60]}")

    # Match
    print("\n\n=== Matching ===")
    matched_total = 0
    img_cache = {}
    for subcat, items in catalog_data.items():
        if not items:
            continue
        tma_items = [p for p in data["products"] if p.get("subcategory") == subcat]
        if subcat == "tea_niktea_top":
            # для top_selection — нет TMA-товаров пока, добавим из каталога?
            print(f"  {subcat}: TMA-товаров 0 (раздел новый)")
            # пропускаем — нам нужны существующие товары а не создание новых
            continue
        print(f"\n{subcat}: TMA={len(tma_items)}, ref={len(items)}")
        local_matched = 0
        for p in tma_items:
            best, best_s = None, 0.0
            for r in items:
                s = score(p["name"], r["name"])
                if s > best_s:
                    best_s, best = s, r
            if not best or best_s < 0.30:
                continue
            url = best["image"]
            if url not in img_cache:
                data_bytes = http_get_bytes(url)
                if not data_bytes or len(data_bytes) < 500:
                    img_cache[url] = None
                    continue
                tmp = PHOTOS_DIR / f"_ref_{abs(hash(url))}.jpg"
                tmp.write_bytes(data_bytes)
                img_cache[url] = tmp
            if not img_cache[url]:
                continue
            dest = PHOTOS_DIR / f"{p['id']}.jpg"
            dest.write_bytes(img_cache[url].read_bytes())
            p["photo"] = f"photos/products/{p['id']}.jpg"
            if best.get("desc"):
                p["description"] = best["desc"]
            p["_source"] = "alephtrade"
            local_matched += 1
            print(f"    ✓ [{best_s:.2f}] {p['name'][:38]:<38} → {best['name'][:40]}")
        matched_total += local_matched
        print(f"  {subcat}: {local_matched}/{len(tma_items)}")

    # Удаляем _ref_ временные
    for tmp in img_cache.values():
        if tmp and tmp.exists():
            tmp.unlink()

    PRODUCTS_JSON.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n=== Итого сматчено чаёв: {matched_total} ===")


if __name__ == "__main__":
    main()
