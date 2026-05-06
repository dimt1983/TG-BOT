"""Реструктуризация раздела «Кофе»:

  Кофе
  ├─ Эспрессо
  │   ├─ Моносорт   (моносорта где roast содержит E)
  │   ├─ Микролот   (E-микролоты, не Black/Борщ)
  │   └─ Смесь      (BE-смеси)
  ├─ Фильтр
  │   ├─ Моносорт   (F)
  │   ├─ Микролот   (F-микролоты, не Black/Борщ)
  │   └─ Смесь      (BF)
  ├─ Блэк            (микролоты_black_edition)
  ├─ Борщ            (микролоты_борщ_edition)
  ├─ Прочее
  └─ Дрипы / Капсулы (drip + nespresso)

Если roast = "EF" — товар появляется в обеих веточках (Эспрессо/Моносорт и Фильтр/Моносорт)
через поле `subcategories: [...]`. Аналогично blends с roast "BEF" (если такие появятся).
"""
import json
from pathlib import Path

PRODUCTS = Path("/root/projects/ai-agents-rb/BOT_TG/tma_static/products.json")

NEW_SUBS = [
    # L1
    {"id": "coffee_espresso",       "parent": "coffee",          "name": "☕ Эспрессо"},
    {"id": "coffee_filter",         "parent": "coffee",          "name": "💧 Фильтр"},
    {"id": "coffee_black",          "parent": "coffee",          "name": "🖤 Блэк"},
    {"id": "coffee_borshch",        "parent": "coffee",          "name": "🍅 Борщ"},
    {"id": "coffee_other",          "parent": "coffee",          "name": "📦 Прочее"},
    {"id": "coffee_drip_capsules",  "parent": "coffee",          "name": "💊 Дрипы / Капсулы"},
    # L2 под Эспрессо
    {"id": "coffee_espresso_mono",     "parent": "coffee_espresso", "name": "🌍 Моносорт"},
    {"id": "coffee_espresso_microlot", "parent": "coffee_espresso", "name": "✨ Микролот"},
    {"id": "coffee_espresso_blend",    "parent": "coffee_espresso", "name": "🎨 Смесь"},
    # L2 под Фильтр
    {"id": "coffee_filter_mono",     "parent": "coffee_filter",   "name": "🌍 Моносорт"},
    {"id": "coffee_filter_microlot", "parent": "coffee_filter",   "name": "✨ Микролот"},
    {"id": "coffee_filter_blend",    "parent": "coffee_filter",   "name": "🎨 Смесь"},
]


def assign(p: dict) -> list[str]:
    """Возвращает список subcategories ID. Идемпотентна: работает и
    с исходными значениями ('микролоты_black_edition' и т.п.) и с уже
    переписанными ('coffee_black' и т.п.) — чтобы повторный прогон
    не валил всё в coffee_other."""
    sub = p.get("subcategory", "")
    subs = p.get("subcategories") or [sub]
    roast = (p.get("roast") or "").upper()
    out = []

    is_black = sub == "микролоты_black_edition" or "coffee_black" in subs
    is_borshch = sub == "микролоты_борщ_edition" or "coffee_borshch" in subs

    if is_black:
        out = ["coffee_black"]
        if "E" in roast: out.append("coffee_espresso_microlot")
        if "F" in roast: out.append("coffee_filter_microlot")
        return out
    if is_borshch:
        out = ["coffee_borshch"]
        if "E" in roast: out.append("coffee_espresso_microlot")
        if "F" in roast: out.append("coffee_filter_microlot")
        return out

    if sub in ("drip", "nespresso") or "coffee_drip_capsules" in subs:
        return ["coffee_drip_capsules"]

    is_mono = (sub == "моносорта"
               or "coffee_espresso_mono" in subs
               or "coffee_filter_mono" in subs)
    if is_mono:
        if "E" in roast: out.append("coffee_espresso_mono")
        if "F" in roast: out.append("coffee_filter_mono")
        return out or ["coffee_other"]

    is_blend = (sub == "смеси"
                or "coffee_espresso_blend" in subs
                or "coffee_filter_blend" in subs)
    if is_blend:
        if "E" in roast: out.append("coffee_espresso_blend")
        if "F" in roast: out.append("coffee_filter_blend")
        return out or ["coffee_other"]

    return ["coffee_other"]


def main():
    data = json.loads(PRODUCTS.read_text(encoding="utf-8"))

    # 1. Заменяем все subcategories с parent=coffee на новые L1+L2
    data["subcategories"] = [
        s for s in data["subcategories"] if s.get("parent") != "coffee"
    ]
    data["subcategories"].extend(NEW_SUBS)

    # 2. Переписываем поле subcategory у coffee-товаров
    for p in data["products"]:
        if p.get("category") != "coffee":
            continue
        targets = assign(p)
        if len(targets) == 1:
            p["subcategory"] = targets[0]
            p.pop("subcategories", None)
        else:
            # multi-section: оставляем subcategory = первый, и subcategories = list
            p["subcategory"] = targets[0]
            p["subcategories"] = targets

    PRODUCTS.write_text(
        json.dumps(data, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    # 3. Отчёт
    print("=== Новая иерархия Кофе ===")
    for s in NEW_SUBS:
        ids = {s["id"] for s in NEW_SUBS}
        cnt_direct = sum(
            1 for p in data["products"]
            if p.get("category") == "coffee"
            and (p.get("subcategory") == s["id"]
                 or s["id"] in (p.get("subcategories") or []))
        )
        indent = "    " if s["parent"] != "coffee" else "  "
        print(f"{indent}[{s['id']:30}] {s['name']} = {cnt_direct}")


if __name__ == "__main__":
    main()
