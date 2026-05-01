"""RBR TEA листовой → берём фото со страниц ALTHAUS листовой на alephtrade
по совпадению ключевых слов (Ассам, Эрл Грей, Молочный Улун, Жасмин, и т.п.)
"""
import urllib.request, re, json
from pathlib import Path

PRODUCTS = Path("/root/projects/ai-agents-rb/BOT_TG/tma_static/products.json")
PHOTOS = Path("/root/projects/ai-agents-rb/BOT_TG/tma_static/photos/products")
PHOTOS.mkdir(parents=True, exist_ok=True)

UA = {"User-Agent":"Mozilla/5.0","Accept":"*/*","Referer":"https://www.google.com/"}


def get(url):
    raw = urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=20).read()
    return raw.decode('windows-1251','replace')


def get_bytes(url):
    return urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=20).read()


def parse_catalog(url):
    html = get(url)
    block_re = re.compile(
        r'<img[^>]+class="img_tee"[^>]+src="(/images/[^"]+\.(?:jpg|png|jpeg))"[^>]*>.*?'
        r'<p[^>]*class="product-head"[^>]*>(.*?)</p>'
        r'(?:.*?<p[^>]*class="product-short"[^>]*>(.*?)</p>)?',
        re.I | re.S,
    )
    tag_re = re.compile(r'<[^>]+>', re.S)
    items = []
    for m in block_re.finditer(html):
        head = tag_re.sub('', m.group(2)).strip()
        short = tag_re.sub('', m.group(3) or '').strip()
        rus = head.split('|')[1].strip() if '|' in head else head.strip()
        items.append({"name": rus, "image": "https://alephtrade.com" + m.group(1), "desc": short})
    return items


def kw(s):
    s = s.upper()
    s = re.sub(r"[ЁЕ]","Е",s)
    s = re.sub(r"[^\w\s]"," ",s)
    NOISE = {"RBR","TEA","ЧАЙ","ALTHAUS","АЛЬТХАУС","250","100","200","Г","КГ","ГР",
             "ЛИСТОВОЙ","ЛИСТ","БАЙХОВЫЙ","БАЙХ","ПАКЕТ","ЧЕРНЫЙ","ЗЕЛЕНЫЙ","БЕЛЫЙ",
             "АРОМАТ","АРОМ","С","СО","ИЗ","ОТ","ВКУС","ВКУСОМ"}
    out = set()
    for w in s.split():
        if len(w) < 3 or w.isdigit(): continue
        out.add(w)
    return out - NOISE


# Алиасы — RBR TEA имена → ALTHAUS аналоги
ALIASES = {
    "АССАМ": "АССАМ", "ЭРЛ": "ГРЕЙ", "ГРЕЙ": "ГРЕЙ", "БЕРГАМОТ": "ГРЕЙ",
    "СЕНЧА": "СЕНЧА", "ЖАСМИН": "ЖАСМИН", "ЖАСМИНОВЫЙ": "ЖАСМИН",
    "УЛУН": "УЛУН", "МОЛОЧНЫЙ": "МОЛОЧНЫЙ",
    "ГЕНМАИЧА": "ГЕНМАЧА", "ГЕНМАЧА": "ГЕНМАЧА",
    "ВАНИЛЬ": "ВАНИЛЬ", "ВАНИЛЬНЫЙ": "ВАНИЛЬ",
    "КАРАМЕЛЬ": "КАРАМЕЛЬ", "СЛИВКИ": "СЛИВКИ",
    "ЯБЛОКО": "ЯБЛОКО", "КЛУБНИКА": "КЛУБНИКА",
    "ВИШНЯ": "ВИШНЯ", "ВИШНЕВЫЙ": "ВИШНЯ",
    "АПЕЛЬСИН": "АПЕЛЬСИН", "ОРАНЖ": "АПЕЛЬСИН",
    "ЛЕМОНГРАСС": "ЛЕМОНГРАСС", "ЛИМОН": "ЛИМОН",
    "МЯТА": "МЯТА", "МЯТНЫЙ": "МЯТА",
    "РОМАШКА": "РОМАШКА", "РОЙБУШ": "РОЙБУШ",
    "ИМБИРЬ": "ИМБИРЬ", "ШОКОЛАД": "ШОКОЛАД",
    "ЯГОДЫ": "ЯГОДЫ", "ЯГОДНЫЙ": "ЯГОДЫ",
}


def kw_aliased(s):
    return {ALIASES.get(w, w) for w in kw(s)}


def score(a, b):
    ka, kb = kw_aliased(a), kw_aliased(b)
    if not ka or not kb: return 0.0
    return len(ka & kb) / max(len(ka), len(kb))


def main():
    print("Парсю ALTHAUS листовой каталог...")
    althaus_items = parse_catalog("https://alephtrade.com/brands/tea/althaus/list_althaus")
    print(f"  Получено: {len(althaus_items)} ALTHAUS-листовых")

    data = json.loads(PRODUCTS.read_text(encoding="utf-8"))
    rbr = [p for p in data["products"] if p.get("subcategory") == "tea_rbr_tea_loose"]
    print(f"\nRBR TEA листовой в TMA: {len(rbr)}")

    cache = {}
    matched = 0
    for p in rbr:
        best, best_s = None, 0.0
        for r in althaus_items:
            s = score(p["name"], r["name"])
            if s > best_s:
                best_s, best = s, r
        if not best or best_s < 0.30:
            print(f"  ✗ {p['name'][:50]} (best score {best_s:.2f}: {best['name'][:40] if best else '—'})")
            continue
        url = best["image"]
        if url not in cache:
            try:
                cache[url] = get_bytes(url)
            except Exception as e:
                print(f"  ! {url}: {e}")
                cache[url] = None
                continue
        if not cache[url]: continue
        dest = PHOTOS / f"{p['id']}.jpg"
        dest.write_bytes(cache[url])
        p["photo"] = f"photos/products/{p['id']}.jpg"
        if best.get("desc"):
            p["description"] = best["desc"][:300]
        matched += 1
        print(f"  ✓ [{best_s:.2f}] {p['name'][:42]:<42} → {best['name'][:48]}")

    PRODUCTS.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n=== Итого: {matched} / {len(rbr)} ===")


if __name__ == "__main__":
    main()
