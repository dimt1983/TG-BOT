"""Перераспределяет чай по подкатегориям 'бренд × формат фасовки'.
Например: 'tea_althaus' → 'tea_althaus_loose' (лист), 'tea_althaus_cup' (пакет на чашку), 'tea_althaus_pot' (на чайник), 'tea_althaus_pyr' (пирамидки)
"""
import json, re
from pathlib import Path

PRODUCTS_JSON = Path(__file__).parent / "tma_static" / "products.json"

FORMAT_LABELS = {
    "loose": "📄 Листовой",
    "cup": "🍵 На чашку",
    "pot": "🫖 На чайник",
    "pyr": "🔺 Пирамидки",
    "iced": "🧊 Iced",
    "togo": "📦 To Go",
    "other": "🍵 Другое",
}


def detect_format(name: str) -> str:
    n = name.lower()
    # Пирамидки — приоритет
    if "пирамидк" in n or "pyra" in n or re.search(r"\bpyr\b", n):
        return "pyr"
    # ICED tea — только как отдельное слово
    if re.search(r"\biced\b", n) or re.search(r"\bайс\b", n) or "ICEDTEA" in name:
        return "iced"
    # TOGO
    if "togo" in n or "to-go" in n or re.search(r"\bto go\b", n):
        return "togo"
    # Для чайника — обычно 15х4г, 15х3г, 20х4г
    m = re.search(r"(\d+)\s*[хx×]\s*(\d+(?:[\.,]\d+)?)\s*г", n)
    if m:
        gr = float(m.group(2).replace(",", "."))
        if gr >= 3.0:
            return "pot"
        return "cup"
    if "для чайника" in n:
        return "pot"
    if re.search(r"\bпакет\b", n) or re.search(r"\bпак\b", n):
        return "cup"
    return "loose"


def main():
    data = json.loads(PRODUCTS_JSON.read_text(encoding="utf-8"))

    # 1. Удалить старые подкатегории чая (и пересобрать)
    OLD_TEA_SUBS = {s["id"] for s in data["subcategories"] if s["parent"] == "tea"}
    print(f"Старые tea-подкатегории: {sorted(OLD_TEA_SUBS)}")

    # 2. Для каждого чая определить новую subcategory: <старая>_<format>
    new_subcat_to_label = {}  # id → name
    brand_count = {}

    for p in data["products"]:
        if p.get("category") != "tea":
            continue
        old_sub = p.get("subcategory", "")
        if not old_sub.startswith("tea_"):
            continue
        # снять старый суффикс формата если есть (для повторного запуска)
        for suf in ["_loose", "_cup", "_pot", "_pyr", "_iced", "_togo", "_other"]:
            if old_sub.endswith(suf):
                old_sub = old_sub[:-len(suf)]
                break
        # достаём бренд из subcategory tea_althaus → althaus
        brand = old_sub[4:]
        fmt = detect_format(p["name"])
        new_sub = f"{old_sub}_{fmt}"
        p["subcategory"] = new_sub
        # Лейбл подкатегории
        old_brand_label = next((s["name"] for s in data["subcategories"] if s["id"] == old_sub), brand.upper())
        # Из старого лейбла отрежем эмодзи и оставим бренд
        m = re.match(r"^([^\s]+)\s+(.+)$", old_brand_label)
        brand_text = m.group(2) if m else old_brand_label
        new_label = f"{brand_text} · {FORMAT_LABELS[fmt]}"
        new_subcat_to_label[new_sub] = new_label
        brand_count[new_sub] = brand_count.get(new_sub, 0) + 1

    # 3. Пересобрать список подкатегорий: убрать старые tea_*, добавить новые
    new_subs = [s for s in data["subcategories"] if s["parent"] != "tea"]
    for sid in sorted(new_subcat_to_label):
        new_subs.append({"id": sid, "parent": "tea", "name": new_subcat_to_label[sid]})

    data["subcategories"] = new_subs
    PRODUCTS_JSON.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"\nНовых подкатегорий чая: {len(new_subcat_to_label)}")
    for sid, lbl in sorted(new_subcat_to_label.items()):
        print(f"  {sid}: {lbl} ({brand_count[sid]} шт)")


if __name__ == "__main__":
    main()
