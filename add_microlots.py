"""Добавляет в TMA-каталог оптовые 1 кг-микролоты из 'остатки.xlsx'.

Только НОВЫЕ позиции, которых ещё нет в TMA:
  Под фильтр: Бразилия SL28 Хани, Судан Руме, Фазенда Лагуинья
  Под эспрессо: Эфиопия Челчеле мытый эспрессо

Существующие Black/Борщ Edition (Ададо, Челелекту, Челчеле сухой,
Гонзало Кармона) НЕ дублируем — у них в карточке уже есть 1 кг.
"""
import json
from pathlib import Path

PRODUCTS = Path("/root/projects/ai-agents-rb/BOT_TG/tma_static/products.json")

NEW_ITEMS = [
    {
        "id": "c-mlot-brazil-sl28-hani-anaerob",
        "name": "Бразилия SL28 Хани анаэробная 1кг",
        "category": "coffee",
        "subcategory": "coffee_filter_microlot",
        "country": "Бразилия",
        "roast": "F",
        "process": "Хани, анаэробная",
        "tags": ["Микролот"],
        "stock": 2,
        "fasovka": [{"size": "1 кг", "price": 3200}],
        "description": "Редкий сорт SL28 в Бразилии, ферментирован в honey-обработке "
                       "с анаэробной фазой. Сложный фруктовый профиль с винными нотами.",
    },
    {
        "id": "c-mlot-brazil-sudan-rume-anaerob",
        "name": "Бразилия Судан Руме анаэробная 1кг",
        "category": "coffee",
        "subcategory": "coffee_filter_microlot",
        "country": "Бразилия",
        "roast": "F",
        "process": "Натуральный, анаэробный",
        "altitude": "1380–1430 м",
        "tags": ["Микролот"],
        "stock": 6,
        "fasovka": [{"size": "1 кг", "price": 3500}],
        "description": "Карму-ди-Минас, фазенда Сантуарио Сулл. Разновидность Судан Руме. "
                       "В аромате — миндаль, фундук, цукаты, молочный шоколад. Во вкусе — "
                       "цветочный мёд, цукаты из цитрусов, молочный шоколад с орехами.",
    },
    {
        "id": "c-mlot-brazil-fazenda-laguinha",
        "name": "Бразилия Фазенда Лагуинья мундо анаэробная 1кг",
        "category": "coffee",
        "subcategory": "coffee_filter_microlot",
        "country": "Бразилия",
        "roast": "F",
        "process": "Натуральный, анаэробный",
        "altitude": "1000–1200 м",
        "tags": ["Микролот"],
        "stock": 4,
        "fasovka": [{"size": "1 кг", "price": 3300}],
        "description": "Карму-да-Кашуэра, фазенда Лагуинья. Разновидность Катуаи. "
                       "В аромате — сухофрукты, цитрусовые, сливочная карамель. "
                       "Во вкусе — сливочная карамель, ликёр, ваниль, шоколад с орехами.",
    },
    {
        "id": "c-mlot-efiopia-chelchele-washed-espresso",
        "name": "Эфиопия Челчеле гр.1 мытый эспрессо 1кг",
        "category": "coffee",
        "subcategory": "coffee_espresso_microlot",
        "country": "Эфиопия",
        "roast": "E",
        "process": "Мытый",
        "altitude": "1900–2100 м",
        "tags": ["Микролот"],
        "stock": 4,
        "fasovka": [{"size": "1 кг", "price": 2863}],
        "description": "Регион Гедео. Эспрессо-обжарка мытой обработки — отличается "
                       "от фильтр-варианта Челчеле (тот сухой натуральный). Чистый "
                       "цитрусовый кислый профиль, цветочный аромат.",
    },
]


def main():
    data = json.loads(PRODUCTS.read_text(encoding="utf-8"))
    existing_ids = {p["id"] for p in data["products"]}
    added = 0
    for item in NEW_ITEMS:
        if item["id"] in existing_ids:
            print(f"  ⊘ уже в каталоге: {item['id']}")
            continue
        data["products"].append(item)
        added += 1
        print(f"  ✓ {item['id']:50}  → {item['subcategory']}")
    PRODUCTS.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n=== Итого: добавлено {added} новых микролотов ===")
    # Текущее распределение
    print("\nТеперь в Микролот-папках:")
    for sub_id in ("coffee_filter_microlot", "coffee_espresso_microlot"):
        items = [p["name"] for p in data["products"]
                 if p.get("subcategory") == sub_id]
        print(f"  {sub_id}: {len(items)}")
        for n in items:
            print(f"    • {n}")


if __name__ == "__main__":
    main()
