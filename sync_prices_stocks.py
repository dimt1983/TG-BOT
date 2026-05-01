"""Синк цен (Базовый прайс) и остатков из 'Прайс и остатки.xlsx' в TMA-каталог.

Файл — выгрузка 1С с иерархией:
  outline=0: категория (МОЛОКО / СИРОПЫ / ЧАЙ)
  outline=1: бренд (BARLINE / BOTANIKA / ALTHAUS / ...)
  outline=2: товар
Колонки:
  E (5)  — Номенклатура
  O (15) — Цена (базовый прайс, RUB)
  Q (17) — Остаток
"""
import openpyxl, json, re
from pathlib import Path

XLSX = Path("/root/projects/ai-agents-rb/BOT_TG/Прайс и остатки.xlsx")
PRODUCTS = Path("/root/projects/ai-agents-rb/BOT_TG/tma_static/products.json")


def num(v):
    if v is None or v == "": return None
    try: return float(v)
    except: return None


def normalize(s):
    s = s.upper()
    s = re.sub(r"[ЁЕ]","Е",s)
    s = re.sub(r"[^\w\s]"," ",s)
    return s


def kw(s):
    s = normalize(s)
    NOISE = {"СИРОП","ТОППИНГ","СИРОПЫ","ЧАЙ","НАПИТОК","Б","А","Л","КГ","Г","ШТ","МЛ","Г",
             "BARLINE","BOTANIKA","ALTHAUS","NIKTEA","ROASTBERRY","RBR","HERBARISTA","SWEETSHOT",
             "GREEN","MILK","НА","СОЕВОЙ","ОСНОВЕ","КОРДИАЛ","BARBACKS","CLAVIS","ПАРФЮМ"}
    out = set()
    for w in s.split():
        if len(w) < 3: continue
        if w.isdigit(): continue
        out.add(w)
    return out - NOISE


def score(a, b):
    ka, kb = kw(a), kw(b)
    if not ka or not kb: return 0.0
    return len(ka & kb) / max(len(ka), len(kb))


def parse_xlsx():
    wb = openpyxl.load_workbook(XLSX, data_only=True)
    ws = wb[wb.sheetnames[0]]
    items = []
    for r in range(1, ws.max_row + 1):
        name = ws.cell(r, 5).value
        if not name: continue
        name = str(name).strip()
        row_obj = ws.row_dimensions.get(r)
        outline = row_obj.outline_level if row_obj else 0
        if outline < 2: continue  # только товары
        price = num(ws.cell(r, 15).value)
        stock = num(ws.cell(r, 17).value)
        if price is None: continue
        items.append({"name": name, "price": price, "stock": int(stock or 0)})
    return items


def main():
    items = parse_xlsx()
    print(f"Распарсено товаров из xlsx: {len(items)}")

    data = json.loads(PRODUCTS.read_text(encoding="utf-8"))
    products = data["products"]

    # Только не-кофе из TMA (кофе обновляется через Bishop xlsx live)
    targets = [p for p in products if p.get("category") != "coffee"]
    print(f"Целевых TMA-товаров (не-кофе): {len(targets)}")

    # Матчим: для каждого xlsx-item ищем лучший TMA target
    pairs = []
    for src in items:
        best, best_s = None, 0.0
        for t in targets:
            s = score(src["name"], t["name"])
            if s > best_s:
                best_s, best = s, t
        if best and best_s >= 0.5:
            pairs.append((best_s, src, best))
    pairs.sort(reverse=True, key=lambda x: x[0])

    used_tma = set()
    used_xlsx = set()
    matched = 0
    price_changes = 0
    stock_changes = 0
    for s, src, t in pairs:
        if t["id"] in used_tma: continue
        if src["name"] in used_xlsx: continue
        used_tma.add(t["id"])
        used_xlsx.add(src["name"])
        # Применяем: цена идёт в первую (или единственную) фасовку
        old_price = t["fasovka"][0]["price"] if t.get("fasovka") else None
        if old_price != src["price"]:
            t["fasovka"][0]["price"] = src["price"]
            price_changes += 1
        old_stock = t.get("stock", 0)
        if old_stock != src["stock"]:
            t["stock"] = src["stock"]
            stock_changes += 1
        matched += 1

    PRODUCTS.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"\n=== Итого ===")
    print(f"Сматчено: {matched} / xlsx={len(items)}, tma={len(targets)}")
    print(f"Цены обновлены: {price_changes}")
    print(f"Остатки обновлены: {stock_changes}")
    print(f"\nXLSX-товаров без матча в TMA: {len(items) - matched}")
    only_in_xlsx = [src['name'] for src in items if src['name'] not in used_xlsx]
    print(f"Примеры (первые 10):")
    for n in only_in_xlsx[:10]: print(f"  + {n[:75]}")
    print(f"\nTMA-товаров без матча в xlsx: {len(targets) - matched}")
    only_in_tma = [t['name'] for t in targets if t['id'] not in used_tma]
    for n in only_in_tma[:10]: print(f"  - {n[:75]}")


if __name__ == "__main__":
    main()
