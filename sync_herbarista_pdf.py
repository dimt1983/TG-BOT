"""Парсит PDF-каталог Herbarista, извлекает (имя сиропа, изображение, описание)
и связывает с TMA-товарами линейки Herbarista.
"""
import fitz, json, re, os
from pathlib import Path

PDF = Path("/root/projects/ai-agents-rb/BOT_TG/каталог-онлайн Herbarista_compressed (30).pdf")
PRODUCTS_JSON = Path("/root/projects/ai-agents-rb/BOT_TG/tma_static/products.json")
PHOTOS_DIR = Path("/root/projects/ai-agents-rb/BOT_TG/tma_static/photos/products")
PHOTOS_DIR.mkdir(parents=True, exist_ok=True)

# Имя сиропа в каталоге Herbarista: "ENG NAME /" на одной строке, "РУС НАЗВАНИЕ" на следующей
# Используем поиск пар по строкам (не одной regex)


def find_syrup_names(text):
    """Возвращает список (eng_name, ru_name) найденных в тексте."""
    lines = [l.strip() for l in text.split("\n") if l.strip()]
    pairs = []
    for i, line in enumerate(lines):
        # ENG NAME заканчивается на "/" — английское название (заглавные буквы)
        if line.endswith("/") and re.match(r"^[A-Z][A-Z\s'.&\-]*$", line[:-1].strip()):
            eng = line[:-1].strip()
            # ru_name — следующая строка, заглавными русскими
            if i + 1 < len(lines):
                next_l = lines[i + 1]
                if re.match(r"^[А-ЯЁ][А-ЯЁ\s\-]*$", next_l):
                    pairs.append((eng, next_l))
    return pairs


def get_image_xrefs_with_bbox(page):
    """Возвращает [(bbox, xref), ...] для всех картинок страницы."""
    out = []
    for img in page.get_images(full=True):
        xref = img[0]
        try:
            for bbox in page.get_image_rects(xref):
                out.append(((bbox.x0, bbox.y0, bbox.x1, bbox.y1), xref))
        except Exception:
            pass
    return out


def extract_all_syrups(doc):
    """Парсит все страницы и возвращает уникальные сиропы с картинкой."""
    out = []
    for pn in range(len(doc)):
        page = doc[pn]
        text = page.get_text()
        pairs = find_syrup_names(text)
        if not pairs:
            continue

        # Получаем bbox для каждого имени (поиск по тексту с позициями)
        name_positions = []  # (rus_name, eng_name, bbox)
        # Ищем через page.search_for
        for eng, rus in pairs:
            rects_eng = page.search_for(eng + " /") or page.search_for(eng)
            rects_rus = page.search_for(rus)
            if not rects_eng and not rects_rus:
                continue
            # Берём bbox первого найденного
            r = (rects_eng or rects_rus)[0]
            name_positions.append((rus, eng, (r.x0, r.y0, r.x1, r.y1)))

        # Получаем все изображения с bbox
        images = get_image_xrefs_with_bbox(page)
        if not images:
            continue

        # Фильтруем: только большие картинки (продуктовые) — площадь > 10000
        big_images = [(b, x) for b, x in images if (b[2]-b[0]) * (b[3]-b[1]) > 10000]
        if not big_images:
            continue

        # Описание — текст после имени в плоском тексте
        all_lines = [l.strip() for l in text.split("\n") if l.strip()]
        for rus, eng, nbox in name_positions:
            # Для описания берём текст в plain — после имени до следующего имени или разделителя
            desc = ""
            try:
                idx = all_lines.index(rus)
                desc_parts = []
                for j in range(idx + 1, min(idx + 8, len(all_lines))):
                    nl = all_lines[j]
                    if nl.endswith("/") and re.match(r"^[A-Z][A-Z\s'.&\-]*$", nl[:-1].strip()):
                        break
                    if re.match(r"^[А-ЯЁ][А-ЯЁ\s\-]*$", nl):
                        break
                    if nl.startswith("#") or len(nl) < 5:
                        continue
                    desc_parts.append(nl)
                desc = " ".join(desc_parts)[:500]
            except ValueError:
                pass

            # Ближайшая картинка по евклидовой дистанции
            cx = (nbox[0] + nbox[2]) / 2
            cy = (nbox[1] + nbox[3]) / 2
            best_img, best_dist = None, 1e9
            for ibbox, xref in big_images:
                ix = (ibbox[0] + ibbox[2]) / 2
                iy = (ibbox[1] + ibbox[3]) / 2
                d = ((cx - ix) ** 2 + (cy - iy) ** 2) ** 0.5
                if d < best_dist:
                    best_dist, best_img = d, xref

            if not best_img:
                continue
            img_data = doc.extract_image(best_img)
            if not img_data or len(img_data.get("image", b"")) < 3000:
                continue

            out.append({
                "ru_name": rus, "eng_name": eng, "desc": desc,
                "image_bytes": img_data["image"], "image_ext": img_data.get("ext", "png"),
                "page": pn + 1,
            })

    # Дедуплицируем по ru_name
    seen = set()
    uniq = []
    for s in out:
        if s["ru_name"] in seen:
            continue
        seen.add(s["ru_name"])
        uniq.append(s)
    return uniq


# === Match ===
NOISE = {"СИРОП","ЭЛИКСИР","ELIXIR","ROYAL","КОРДИАЛ","CORDIAL","HERBARISTA","Л","МЛ","ШТ","КГ","Г","ОБЖИГА","ДВОЙНОГО"}
ALIASES = {
    "ВАНИЛЬ":"ВАНИЛЬ","ВАНИЛЬНАЯ":"ВАНИЛЬ","ВАНИЛЬНЫЙ":"ВАНИЛЬ","БУРБОНСКАЯ":"БУРБОН",
    "КАРАМЕЛЬ":"КАРАМЕЛЬ","СОЛЁНАЯ":"СОЛЁНАЯ","СОЛЕНАЯ":"СОЛЁНАЯ",
    "ШОКОЛАД":"ШОКОЛАД","ШОКОЛАДНЫЙ":"ШОКОЛАД","ШОКОЛАДНОЕ":"ШОКОЛАД",
    "КОКОС":"КОКОС","КОКОСОВЫЙ":"КОКОС","КОКОСОМ":"КОКОС",
    "МИНДАЛЬ":"МИНДАЛЬ","МИНДАЛЬНЫЙ":"МИНДАЛЬ","МИНДАЛЬНАЯ":"МИНДАЛЬ",
    "ОРЕХ":"ОРЕХ","ОРЕХОВЫЙ":"ОРЕХ","ОРЕХА":"ОРЕХ",
    "БАНАН":"БАНАН","БАНАНОВЫЙ":"БАНАН",
    "КЛУБНИКА":"КЛУБНИКА","КЛУБНИЧНАЯ":"КЛУБНИКА",
    "ЯБЛОКО":"ЯБЛОКО","ЯБЛОЧНЫЙ":"ЯБЛОКО",
    "АПЕЛЬСИН":"АПЕЛЬСИН",
    "ЛИМОН":"ЛИМОН","ЛАЙМ":"ЛАЙМ",
    "МАЛИНА":"МАЛИНА","МАЛИНОВЫЙ":"МАЛИНА","МАЛИНОЙ":"МАЛИНА",
    "ВИШНЯ":"ВИШНЯ","ВИШНЁВАЯ":"ВИШНЯ",
    "ИМБИРЬ":"ИМБИРЬ","ИМБИРНЫЙ":"ИМБИРЬ",
    "АНАНАС":"АНАНАС","МАНГО":"МАНГО","ПЕРСИК":"ПЕРСИК","ГРУША":"ГРУША","ДЫНЯ":"ДЫНЯ",
    "ТЫКВА":"ТЫКВА","ЕЖЕВИКА":"ЕЖЕВИКА","СМОРОДИНА":"СМОРОДИНА",
    "ЛАВАНДА":"ЛАВАНДА","МЯТА":"МЯТА","МЯТНЫЙ":"МЯТА","МЯТНАЯ":"МЯТА",
    "КОНОПЛЯНАЯ":"КОНОПЛЯНАЯ","ХАЛВА":"ХАЛВА",
    "АРАХИСОВОЕ":"АРАХИСОВОЕ","МАСЛО":"МАСЛО",
    "ИРИСКА":"ИРИСКА","СЛИВОЧНАЯ":"СЛИВКИ","СЛИВОЧНЫЙ":"СЛИВКИ",
    "ИРЛАНДСКИЙ":"ИРЛАНДСКИЙ","ИРИШ":"ИРЛАНДСКИЙ",
    "ФИСТАШКА":"ФИСТАШКА","ФИСТАШКОВЫЙ":"ФИСТАШКА",
    "БАБЛ":"БАБЛ","ГАМ":"ГАМ",
    "КЛЕНОВЫЙ":"КЛЕНОВЫЙ","КАШТАН":"КАШТАН",
    "СПЕЦИИ":"СПЕЦИИ","СПЕЦИЯМИ":"СПЕЦИИ","ПРЯНОСТИ":"СПЕЦИИ",
    "КЕДРОВЫЙ":"КЕДРОВЫЙ","ЕЛОВЫЙ":"ЕЛОВЫЙ","ХВОЯ":"ЕЛОВЫЙ",
    "ТАБАК":"ТАБАК","БАРБАРИС":"БАРБАРИС","МАКАДАМИЯ":"МАКАДАМИЯ",
    "СГУЩЕННОЕ":"СГУЩЕНКА","СГУЩЁННОЕ":"СГУЩЕНКА","СГУЩЕНКА":"СГУЩЕНКА",
    "КРАСНЫЙ":"КРАСНЫЙ","ТРОПИЧЕСКИЙ":"ТРОПИЧЕСКИЙ",
    "ЛИСТЬЯМИ":"ЛИСТЬЯ","ЛИСТЬЯ":"ЛИСТЬЯ","ЛИСТЬЕВ":"ЛИСТЬЯ",
    "ЛЕМОНГРАСС":"ЛЕМОНГРАСС","ЭВКАЛИПТ":"ЭВКАЛИПТ","ПОПКОРН":"ПОПКОРН","ТАРХУН":"ТАРХУН",
    "ЭСТРАГОН":"ТАРХУН","ВЕРБЕНА":"ВЕРБЕНА",
    "БАЗИЛИК":"БАЗИЛИК","РОЗА":"РОЗА","БУЗИНА":"БУЗИНА",
    "БОБЫ":"ТОНКА","ТОНКА":"ТОНКА","ОРХИДЕЯ":"ОРХИДЕЯ","ПАЧУЛИ":"ПАЧУЛИ","ФЕЙХОА":"ФЕЙХОА",
    "ЯГОДЫ":"ЯГОДЫ","СЕВЕРА":"СЕВЕРА","СИБИРСКИЕ":"СИБИРСКИЕ",
    "АНИС":"АНИС","БУРБОН":"БУРБОН","БУРБОНА":"БУРБОН",
    "РЕВЕНЬ":"РЕВЕНЬ","ПЕРСИКОВЫЙ":"ПЕРСИК",
    "БИФОРБА":"БУРБОН","ТРОПИК":"ТРОПИЧЕСКИЙ","ТРЮФЕЛЬ":"ТРЮФЕЛЬ",
    "АРОМА":"АРОМА","ЭЛИКСИРС":"ЭЛИКСИР",
}


def kw(s):
    s = s.upper()
    s = re.sub(r"[ЁЕ]", "Е", s)
    s = re.sub(r"[^А-ЯA-Z0-9\s]+", " ", s)
    out = set()
    for w in s.split():
        if len(w) < 3:
            continue
        out.add(ALIASES.get(w, w))
    return out - NOISE


def score(a, b):
    ka, kb = kw(a), kw(b)
    if not ka or not kb:
        return 0.0
    return len(ka & kb) / max(len(ka), len(kb))


def main():
    print("Парсю PDF...")
    doc = fitz.open(PDF)
    syrups = extract_all_syrups(doc)
    print(f"Извлечено сиропов: {len(syrups)}")
    for s in syrups[:8]:
        print(f"  стр{s['page']}: {s['ru_name']:<30} ({s['eng_name']})")

    # Обновляем TMA
    data = json.loads(PRODUCTS_JSON.read_text(encoding="utf-8"))
    products = data["products"]
    herbarista_prods = [p for p in products if p.get("subcategory") == "syr_herbarista"]
    print(f"\nTMA Herbarista: {len(herbarista_prods)}")

    matched = 0
    for p in herbarista_prods:
        best, best_s = None, 0.0
        for s in syrups:
            sc = score(p["name"], s["ru_name"])
            if sc > best_s:
                best_s, best = sc, s
        if not best or best_s < 0.4:
            continue
        # Сохраняем картинку
        ext = best["image_ext"]
        if ext not in ("png", "jpg", "jpeg"):
            ext = "png"
        dest = PHOTOS_DIR / f"{p['id']}.{ext}"
        # Если ext не jpg — конвертируем через PIL
        if ext != "jpg":
            try:
                from PIL import Image
                from io import BytesIO
                img = Image.open(BytesIO(best["image_bytes"])).convert("RGB")
                # уменьшим до 800x800
                img.thumbnail((800, 800))
                jpg_dest = PHOTOS_DIR / f"{p['id']}.jpg"
                img.save(jpg_dest, "JPEG", quality=85, optimize=True)
                dest = jpg_dest
            except Exception:
                dest.write_bytes(best["image_bytes"])
        else:
            dest.write_bytes(best["image_bytes"])

        p["photo"] = f"photos/products/{p['id']}.jpg"
        p["description"] = best["desc"][:500]
        p["_source"] = "herbarista_pdf"
        matched += 1
        print(f"  ✓ [{best_s:.2f}] {p['name'][:38]:<38} → {best['ru_name']}")

    PRODUCTS_JSON.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\nИтого Herbarista: {matched} / {len(herbarista_prods)}")


if __name__ == "__main__":
    main()
