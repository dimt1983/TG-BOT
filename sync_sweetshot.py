"""SweetShot фото с alephtrade.com → TMA."""
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
    NOISE = {"СИРОП","SWEETSHOT","СВИТШОТ","Л","МЛ","КГ","Г","СТЕКЛ","БУТ","ОБЪЕМ"}
    out = set()
    for w in s.split():
        if len(w) < 3 or w.isdigit(): continue
        out.add(w)
    return out - NOISE


def score(a, b):
    ka, kb = kw(a), kw(b)
    if not ka or not kb: return 0.0
    return len(ka & kb) / max(len(ka), len(kb))


def main():
    refs = parse_catalog("https://alephtrade.com/brands/syrups/sweetshot/sweetshot_1l")
    print(f"С alephtrade SweetShot 1L: {len(refs)}")

    data = json.loads(PRODUCTS.read_text(encoding="utf-8"))
    sweetshot = [p for p in data["products"] if p.get("subcategory") == "syr_sweetshot"]
    print(f"TMA SweetShot: {len(sweetshot)}")

    cache = {}
    matched = 0
    for p in sweetshot:
        best, best_s = None, 0.0
        for r in refs:
            s = score(p["name"], r["name"])
            if s > best_s:
                best_s, best = s, r
        if not best or best_s < 0.4:
            continue
        url = best["image"]
        if url not in cache:
            try:
                cache[url] = get_bytes(url)
            except Exception as e:
                print(f"  ! {url}: {e}")
                cache[url] = None
        if not cache[url]:
            continue
        dest = PHOTOS / f"{p['id']}.jpg"
        dest.write_bytes(cache[url])
        p["photo"] = f"photos/products/{p['id']}.jpg"
        if best["desc"]:
            p["description"] = best["desc"][:300]
        matched += 1
        print(f"  ✓ [{best_s:.2f}] {p['name'][:40]:<40} → {best['name']}")

    PRODUCTS.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\nИтого: {matched} из {len(sweetshot)}")


if __name__ == "__main__":
    main()
