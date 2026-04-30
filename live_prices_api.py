"""
live_prices_api.py — даёт TMA живые цены кофе из Bishop'овского чистового прайса.

Структура источника:
    /root/projects/ai-agents-rb/Прайсы/чистовики/Roastberry_Прайс_2026.xlsx
        Лист 'Прайс 2026': Тип | Название | Обжарка | 1кг | 10кг | 25кг | 200г | 10кг_200 | 25кг_200

Где используется:
    1. tma_static_server.py подключает endpoint /tma/api/prices
    2. TMA при загрузке делает fetch(`api/prices`) и переливает свежие цены
       поверх coffee-секции в products.json
    3. Чай / сиропы / молоко не трогаются

Кэширование: 60 секунд (mtime файла + время чтения).
"""
import json
import os
import re
import time
from pathlib import Path

# Поиск xlsx в нескольких местах: env > рядом с ботом (deploy) > синхронизированная папка
_HERE = Path(__file__).parent
_CANDIDATES = [
    Path(os.environ.get("BISHOP_PRICE_XLSX", "")) if os.environ.get("BISHOP_PRICE_XLSX") else None,
    _HERE / "tma_static" / "data" / "Roastberry_Прайс_2026.xlsx",
    Path("/root/projects/ai-agents-rb/Прайсы/чистовики/Roastberry_Прайс_2026.xlsx"),
]
PRICE_XLSX = next((p for p in _CANDIDATES if p and p.exists()), _CANDIDATES[1])

_CACHE = {"data": None, "ts": 0, "mtime": 0}
_CACHE_TTL = 60  # сек


def slugify(s: str) -> str:
    s = s.lower().strip()
    s = re.sub(r'[^\w\-]+', '_', s, flags=re.UNICODE)
    return s[:50]


def load_prices() -> dict:
    """Возвращает словарь:
    {
        "by_id": {"c-bittiер": {"price_1000": 1910, "price_200": 410, "discount_10kg": 1720, ...}},
        "by_name": {"БИТТЕР": {...}},
        "meta": {"course_usd": 88, "loaded_at": ts, "source": "..."}
    }
    """
    if not PRICE_XLSX.exists():
        return {"by_id": {}, "by_name": {}, "meta": {"error": "price file not found"}}

    mtime = PRICE_XLSX.stat().st_mtime
    now = time.time()

    if _CACHE["data"] and now - _CACHE["ts"] < _CACHE_TTL and _CACHE["mtime"] == mtime:
        return _CACHE["data"]

    try:
        import openpyxl
    except ImportError:
        return {"by_id": {}, "by_name": {}, "meta": {"error": "openpyxl not installed"}}

    wb = openpyxl.load_workbook(PRICE_XLSX, read_only=True, data_only=True)
    ws = wb[wb.sheetnames[0]]

    by_name = {}
    course = None
    rows_processed = 0

    for row in ws.iter_rows(min_row=1, values_only=True):
        if not row:
            continue
        cells = [str(c).strip() if c is not None else "" for c in row]

        # Курс
        if cells[0].lower().startswith("курс"):
            m = re.search(r'(\d+)', cells[0])
            if m:
                course = int(m.group(1))
            continue

        # Шапка
        if cells[0].lower() == "тип":
            continue

        # Это товарная строка?
        type_ = cells[0]
        name = cells[1] if len(cells) > 1 else ""
        if not name or not type_:
            continue
        # Пытаемся прочесть цены
        try:
            price_1kg = float(cells[3]) if len(cells) > 3 and cells[3] else None
            price_10kg = float(cells[4]) if len(cells) > 4 and cells[4] else None
            price_25kg = float(cells[5]) if len(cells) > 5 and cells[5] else None
            price_200 = float(cells[6]) if len(cells) > 6 and cells[6] else None
            price_200_10 = float(cells[7]) if len(cells) > 7 and cells[7] else None
            price_200_25 = float(cells[8]) if len(cells) > 8 and cells[8] else None
        except (ValueError, TypeError):
            continue

        if price_1kg is None and price_200 is None:
            continue

        roast = cells[2] if len(cells) > 2 else ""

        by_name[name.upper()] = {
            "type": type_,
            "name": name,
            "roast_tag": roast,
            "price_1000": price_1kg,
            "price_200": price_200,
            "discount_tiers": {
                "10kg_per_kg": price_10kg,
                "25kg_per_kg": price_25kg,
                "10kg_per_200g": price_200_10,
                "25kg_per_200g": price_200_25,
            },
        }
        rows_processed += 1

    data = {
        "by_id": {},
        "by_name": by_name,
        "meta": {
            "course_usd": course,
            "loaded_at": now,
            "rows": rows_processed,
            "source": str(PRICE_XLSX),
        },
    }

    _CACHE["data"] = data
    _CACHE["ts"] = now
    _CACHE["mtime"] = mtime
    return data


def _norm_words(s: str) -> set[str]:
    """Нормализованный набор слов для fuzzy-матчинга."""
    s = s.upper()
    # Убираем варианты записи / — есть строки "А/Б/В" в xlsx, разворачиваем
    s = s.replace("/", " ")
    # Убираем спецсимволы
    s = re.sub(r'[^\w\s]+', ' ', s)
    # Нормализация некоторых ключевых слов
    repl = {
        "ИРГАЧ": "ИРГАЧИФ", "ИРГАЧИФ": "ИРГАЧИФ", "ИРГАЧИФФ": "ИРГАЧИФ",
        "ГВАТЕМАЛЛА": "ГВАТЕМАЛА",
        "РУАНАДА": "РУАНДА", "РУАНДА": "РУАНДА",
        "МУТИТЕЛЛИ": "МУТЕТЕЛИ", "МУТЕТЕЛИ": "МУТЕТЕЛИ", "МУТИТЕЛИ": "МУТЕТЕЛИ",
        "БИЛОЯ": "БЕЛОЯ", "БЕЛОЯ": "БЕЛОЯ", "БЕЛОЙЯ": "БЕЛОЯ",
        "САН": "САН", "RAFAEL": "САН РАФАЕЛЬ", "РАФАЕЛЬ": "РАФАЕЛЬ",
        "BLACK": "BLACK", "ЭДИШН": "EDITION", "EDITION": "EDITION",
    }
    words = []
    for w in s.split():
        if len(w) < 3 and not w.isdigit():
            continue
        words.append(repl.get(w, w))
    # Удаляем шумовые слова
    noise = {"СУХОЙ", "МЫТЫЙ", "ХАНИ", "АНАЭРОБ", "ТОП", "ГР", "1", "2", "3", "4",
             "ЭДИШН", "EDITION", "ОБЖАРКА", "ТЕМНАЯ", "СВЕТЛАЯ", "ДАРК", "DARK"}
    return set(words) - noise


def _match_score(tma_name: str, ozon_name: str) -> float:
    a = _norm_words(tma_name)
    b = _norm_words(ozon_name)
    if not a or not b:
        return 0.0
    inter = len(a & b)
    return inter / max(len(a), len(b))


def merge_into_products(tma_products: list[dict]) -> tuple[list[dict], dict]:
    """Применяет цены к coffee-товарам TMA. Возвращает (обновлённые товары, статистика)."""
    prices = load_prices()
    by_name = prices["by_name"]
    matched = 0
    unmatched = []
    out = []

    for p in tma_products:
        if p.get("category") != "coffee":
            out.append(p)
            continue

        # exact match
        name = p["name"].upper().strip()
        match = by_name.get(name)
        if not match:
            short = name.split()[0] if name else ""
            match = by_name.get(short)
        if not match:
            # fuzzy match
            best_key, best_score = None, 0.0
            for k in by_name.keys():
                s = _match_score(name, k)
                if s > best_score:
                    best_score = s
                    best_key = k
            if best_score >= 0.5:
                match = by_name[best_key]

        if match:
            # Перепишем цены в fasovka
            new_fasovka = []
            for fa in p.get("fasovka", []):
                if "1 кг" in fa["size"] and match.get("price_1000"):
                    fa = dict(fa)
                    fa["price"] = int(match["price_1000"])
                elif "200 г" in fa["size"] and match.get("price_200"):
                    fa = dict(fa)
                    fa["price"] = int(match["price_200"])
                new_fasovka.append(fa)
            p = dict(p)
            p["fasovka"] = new_fasovka
            p["_price_source"] = "bishop"
            p["_discount_tiers"] = match.get("discount_tiers")
            matched += 1
        else:
            unmatched.append(p["name"])

        out.append(p)

    return out, {
        "matched": matched,
        "unmatched_count": len(unmatched),
        "unmatched": unmatched[:20],
        "meta": prices["meta"],
    }


# === Тест из CLI ===
if __name__ == "__main__":
    import sys
    products_path = Path(__file__).parent / "tma_static" / "products.json"
    if not products_path.exists():
        print(f"Не найден {products_path}")
        sys.exit(1)
    tma = json.loads(products_path.read_text(encoding="utf-8"))
    updated, stats = merge_into_products(tma["products"])
    print(f"Матч: {stats['matched']} / {sum(1 for p in tma['products'] if p.get('category') == 'coffee')}")
    print(f"Не сматчено: {stats['unmatched_count']}")
    if stats['unmatched']:
        print("Примеры:")
        for n in stats['unmatched'][:10]:
            print(f"  - {n}")
    print(f"\nМета: {stats['meta']}")

    # Покажем пример
    sample = next((p for p in updated if p.get("_price_source") == "bishop"), None)
    if sample:
        print(f"\nПример обновлённого:")
        print(f"  {sample['name']}")
        for fa in sample.get('fasovka', []):
            print(f"  → {fa['size']}: {fa['price']} ₽")
        if sample.get('_discount_tiers'):
            print(f"  Опт-цены: {sample['_discount_tiers']}")
