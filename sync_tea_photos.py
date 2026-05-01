"""
sync_tea_photos.py — скачивает фото и описания чая с alephtrade.com
и привязывает к нашим TMA-товарам ALTHAUS и NIKTEA.
"""
import json, re, urllib.request, urllib.error
from pathlib import Path

ROOT = Path(__file__).parent
PRODUCTS_JSON = ROOT / "tma_static" / "products.json"
PHOTOS_DIR = ROOT / "tma_static" / "photos" / "products"
PHOTOS_DIR.mkdir(parents=True, exist_ok=True)

# === ALTHAUS — данные с alephtrade.com/brands/tea/althaus/list_althaus ===
ALTHAUS = [
    ("Ассам Мэленг GFBOP", "https://alephtrade.com/images/assam_meleng_gfbop1100.jpg", "Чёрный листовой чай, Индия. 250г."),
    ("Голден Ассам Санкар FTGFOP", "https://alephtrade.com/images/golden_assam_sankar_ftgfop1105.jpg", "Чёрный листовой чай, Индия. 250г."),
    ("Дарджилинг Путтабонг FTGFOP", "https://alephtrade.com/images/darjeeling_puttabong_ftgfop_first_flush1200.jpg", "Чёрный листовой чай, Индия. Первый сбор. 250г."),
    ("Цейлон Канелия", "https://alephtrade.com/images/ceylon_op1_kanneliya1300.jpg", "Чёрный листовой чай OP1 Канелия. Шри-Ланка. 250г."),
    ("Английский Завтрак Эндрю", "https://alephtrade.com/images/english_breakfast_st_andrews1305.jpg", "Чёрный купаж английского завтрака. Листовой. 250г."),
    ("Пу Эр Ан-Бао", "https://alephtrade.com/images/pu_er_an_bao1400.jpg", "Чёрный ферментированный чай (тёмный). Листовой. 250г."),
    ("Улун Цзинь Хуан", "https://alephtrade.com/images/superior_oolong_jin_huang1405.jpg", "Полуферментированный улун Superior. 60г."),
    ("Лапсанг Сушонг", "https://alephtrade.com/images/lapsang_souchong_hong_cha1415.jpg", "Лапсанг Сушонг Хун-Ча — копчёный чёрный. 100г."),
    ("Тепло Дома", "https://alephtrade.com/images/Althaus/warm_and_cozy.jpg", "Чёрный с пряностями: кардамон, корица, шоколад, какао. 100г."),
    ("Императорский Грей", "https://alephtrade.com/images/imperial_earl_grey2005.jpg", "Чёрный с эфирами бергамота — Императорский Эрл Грей. 250г."),
    ("Голубой Грей", "https://alephtrade.com/images/blue_earl_grey2010.jpg", "Чёрный с лепестками василька и бергамотом. 250г."),
    ("Грей Премиум", "https://alephtrade.com/images/earl_grey_supreme2012.jpg", "Чёрный Эрл Грей Premium с бергамотом. 250г."),
    ("Земля Клубники Сливках", "https://alephtrade.com/images/strawberry_cream_ameli2020.jpg", "Чёрный с фруктами, клубникой и сливками. 250г."),
    ("Спелая Дикая Вишня", "https://alephtrade.com/images/sweet_wild_cherry2025.jpg", "Чёрный с кусочками вишни и листом вишни. 250г."),
    ("Пряный Пунш", "https://alephtrade.com/images/spice_punch2030.jpg", "Чёрный со специями: гвоздика, корица, имбирь. 250г."),
    ("Горные Травы", "https://alephtrade.com/images/mountain_herbs2050.jpg", "Чёрный с травами. 250г."),
    ("Чёрная Смородина", "https://alephtrade.com/images/black_currant_traditional2055.jpg", "Чёрный с листом и ароматом чёрной смородины. 250г."),
    ("Грей Карамель", "https://alephtrade.com/images/Althaus/golden-caramel-earl-grey-02.jpg", "Чёрный с карамелью и кусочками карамели. 200г."),
    ("Зелень Гималайская", "https://alephtrade.com/images/green_himalaijan3200.jpg", "Зелёный листовой. 250г."),
    ("Супериор Белый", "https://alephtrade.com/images/3400-superior-white-new.jpg", "Белый чай Superior. 70г."),
    ("Ройал Пай Му Тан", "https://alephtrade.com/images/royal_pai_mu_tan3410.jpg", "Royal Pai Mu Tan — белый. 65г."),
    ("Чжу-Ча", "https://alephtrade.com/images/gunpowder_zhu_cha3415.jpg", "Зелёный Gunpowder Чжу-Ча. 250г."),
    ("Лунг Чин", "https://alephtrade.com/images/lung_ching_light3425.jpg", "Зелёный Lung Ching Light. 100г."),
    ("Молочный Улун", "https://alephtrade.com/images/milk_oolong3435.jpg", "Зелёный молочный улун. 250г."),
    ("Сенча Сенпай", "https://alephtrade.com/images/sencha_senpai3600.jpg", "Зелёный Sencha Senpai. 250г."),
    ("Гиокуро Танабе", "https://alephtrade.com/images/gyokuro_tanabe3610.jpg", "Зелёный Gyokuro Tanabe. 100г."),
    ("Генмача Райсу", "https://alephtrade.com/images/genmacha_raisu3615.jpg", "Зелёный с обжаренным рисом и жасмином. 100г."),
    ("Зелёные Новости", "https://alephtrade.com/images/Althaus/green_is_the_new_grey.jpg", "Зелёный с ромашкой, жасмином, манго. 50г."),
    ("Ройал Жасмин", "https://alephtrade.com/images/royal_jasmine_chung_hao4000.jpg", "Зелёный Royal Jasmine Chung Hao. 250г."),
    ("Жасмин Тин Юань", "https://alephtrade.com/images/jasmine_ting_yuan4005.jpg", "Зелёный с цветами жасмина. 250г."),
    ("Жасмин Бай Инь", "https://alephtrade.com/images/jasmine_pearls_bai_yin4010.jpg", "Зелёные жасминовые жемчужины Бай Инь. 100г."),
    ("Касабланка Мята", "https://alephtrade.com/images/casablanca_mint4025.jpg", "Зелёный с мятой. 150г."),
    ("Зелёный Матинее", "https://alephtrade.com/images/grun_matinee4035.jpg.jpg", "Зелёный с жасмином, виноградным листом и лемонграссом. 250г."),
    ("Манон", "https://alephtrade.com/images/manon4040.jpg", "Зелёный с жасмином, виноградным листом, ванилью. 250г."),
    ("Полёт Дракона", "https://alephtrade.com/images/ginseng_flight_of_dragon4045.jpg", "Зелёный с женьшенем и жасмином. 200г."),
    ("Арабский Шейх", "https://alephtrade.com/images/arabischer_sheik4050.jpg", "Зелёный с жасмином и мятой. 200г."),
    ("Весна Зовёт", "https://alephtrade.com/images/Althaus/spring_is_calling.jpg", "Зелёный + белый, гибискус, роза, яблоко, апельсин. 100г."),
    ("Зимняя Чашка", "https://alephtrade.com/images/Althaus/winter_in_a_cup.jpg", "Ройбуш с клубникой, яблоком, вишней, корицей. 100г."),
    ("Костра Огонь", "https://alephtrade.com/images/Althaus/by_the_fire.jpg", "Ройбуш с яблоком, апельсином, корицей, гвоздикой, бадьяном. 100г."),
    ("Яблочный Крамбл", "https://alephtrade.com/images/Althaus/apple_crumble.jpg", "Зелёный + ройбуш, яблоко, апельсин, корица. 100г."),
    ("Красные Ягоды", "https://alephtrade.com/images/red_fruit_flash5000.jpg", "Ройбуш с гибискусом, клюквой, чёрной смородиной. 250г."),
    ("Манила Манго", "https://alephtrade.com/images/manila_mango5005.jpg", "Ройбуш с манго, ананасом, папайей. 250г."),
    ("Клубника Флип", "https://alephtrade.com/images/strawberry_flip5015.jpg", "Ройбуш с яблоком, апельсином, бананом, клубникой. 250г."),
    ("Дикая Вишня", "https://alephtrade.com/images/wildkirsche5020.jpg", "Ройбуш с яблоком, апельсином, кислой вишней. 250г."),
    ("Сицилийский Апельсин", "https://alephtrade.com/images/sicilian_orange5025.jpg", "Ройбуш с апельсином и севильским апельсином. 250г."),
    ("Голубой Ангел", "https://alephtrade.com/images/5030-blauer-engel.jpg", "Ройбуш с яблоком, апельсином, бананом, голубикой, клубникой. 250г."),
    ("Персидское Яблоко", "https://alephtrade.com/images/persischer_apfel5035.jpg", "Зелёный с яблочным листом. 250г."),
    ("Пляж Палм", "https://alephtrade.com/images/palm_beach5050.jpg", "Ройбуш с гибискусом, ананасом, манго. 250г."),
    ("Мультифит", "https://alephtrade.com/images/multifit5060.jpg", "Ройбуш с гибискусом, яблоком, апельсином, мятой — 10 компонентов. 250г."),
    ("Миндальный Пирог", "https://alephtrade.com/images/almond_pie5065.jpg", "Зелёный + ройбуш, яблоко, ваниль, миндаль. 200г."),
    ("Киви Колада", "https://alephtrade.com/images/kiwi_colada5070.jpg", "Ройбуш с киви, яблоком, апельсином, ананасом, бананом. 200г."),
    ("Белый Кокос", "https://alephtrade.com/images/coco_white_new.jpg", "Белый чай с манго, яблоком, бананом, апельсином, ананасом. 250г."),
    ("Сущность Фруктов", "https://alephtrade.com/images/essence_of_fruit_new.jpg", "Зелёный + ройбуш, яблоко, апельсин, манго, ананас. 250г."),
    ("Ванильная Звёздная Пыль", "https://alephtrade.com/images/Althaus/vanilla-stardust-fruechtetee-02.jpg", "Зелёный с ягодами, клубникой, ванилью. 200г."),
    ("Чай Бабочка", "https://alephtrade.com/images/Althaus/butterfly_garden.jpg", "Ройбуш с гибискусом, календулой, жасмином. 50г."),
    ("Яркий Новый День", "https://alephtrade.com/images/Althaus/bright_new_day.jpg", "Зелёный + белый, ромашка, манго, лемонграсс. 100г."),
    ("Щелкунчик Ройбуш", "https://alephtrade.com/images/Althaus/rooibos_nutcracker.jpg", "Ройбуш с миндалём, гвоздикой, корицей. 100г."),
    ("Имбирь Специи", "https://alephtrade.com/images/Althaus/ginger_and_spice.jpg", "Ройбуш с имбирём, мятой, гвоздикой, корицей, бадьяном. 100г."),
    ("Чашка Благополучия", "https://alephtrade.com/images/wellness_cup6000.jpg", "Ройбуш с травами: ромашка, мята, лемонграсс. 75г."),
    ("Долина Женьшеня", "https://alephtrade.com/images/ginseng_valley6005.jpg", "Белый с женьшенем, гибискусом, яблоком, апельсином. 250г."),
    ("Ромашковый Луг", "https://alephtrade.com/images/chamomile_meadow6010.jpg", "Цветки ромашки. 75г."),
    ("Баварская Мята", "https://alephtrade.com/images/bavarian_mint6015.jpg", "Перечная и обычная мята. 75г."),
    ("Французская Роза", "https://alephtrade.com/images/french_rose6020.jpg", "Бутоны розы. 125г."),
    ("Лемонграсс", "https://alephtrade.com/images/lemongrass6025.jpg", "Лемонграсс. 100г."),
    ("Японская Липа", "https://alephtrade.com/images/japanese_linden6030.jpg", "Цветки липы. 75г."),
    ("Имбирный Ветерок", "https://alephtrade.com/images/ginger_breeze6035.jpg", "Имбирь, гибискус, лемонграсс. 250г."),
    ("Лимон Мята", "https://alephtrade.com/images/lemon_mint6045.jpg", "Лемонграсс, мелисса, лимонная вербена. 150г."),
    ("Травяное Искушение", "https://alephtrade.com/images/herbal_temptation.jpg", "Ройбуш с гибискусом, мятой, лемонграссом, мелиссой. 175г."),
    ("Ройбуш Клубника Сливках", "https://alephtrade.com/images/rooibush_strawberry_cream6200.jpg", "Ройбуш с клубникой и сливками. 250г."),
    ("Ройбуш Крем Карамель", "https://alephtrade.com/images/rooibush_cream_caramel6210.jpg", "Ройбуш с карамелью и вишней. 250г."),
    ("Ройбуш Сладкий Апельсин", "https://alephtrade.com/images/rooibush_sweet_orange6215.jpg", "Ройбуш с севильским апельсином и гибискусом. 250г."),
]

# === NIKTEA — данные с alephtrade.com/brands/tea/niktea/list_niktea ===
NIKTEA = [
    ("Высокогорный Цейлон", "https://alephtrade.com/images/100_highland_ceylon.png", "Чёрный чай, Шри-Ланка. 250г."),
    ("Ассам Суприм", "https://alephtrade.com/images/105_assam_supreme_black.png", "Чёрный чай, Индия. 250г."),
    ("Ассам Индиго", "https://alephtrade.com/images/loose_assam_indigo_retail_1.jpg", "Чёрный чай, Индия. 100г."),
    ("Юньнань Пуэр", "https://alephtrade.com/images/110_yunnan_puer.png", "Чёрный ферментированный пуэр, Китай. 250г."),
    ("Граф Бергамот", "https://alephtrade.com/images/200_earl_grey_special.png", "Чёрный с эфирами бергамота — Earl Grey Special. 250г."),
    ("Чай Граф", "https://alephtrade.com/images/loose_earlgrey_retail.jpg", "Earl Grey классический. 100г."),
    ("Горная Тимьян", "https://alephtrade.com/images/205_mountain_thymian.png", "Чёрный с чабрецом. 250г."),
    ("Шоколадный Микс", "https://alephtrade.com/images/210_chocolate_melange.png", "Чёрный с какао-бобами и фруктами. 250г."),
    ("Таёжная Ягода", "https://alephtrade.com/images/225_taiga_berries.jpg", "Чёрный с лесными ягодами. 250г."),
    ("Сенча Классик", "https://alephtrade.com/images/300_sencha_classic.png", "Зелёный Sencha Classic, Япония. 250г."),
    ("Генмайча", "https://alephtrade.com/images/305_genmaicha_green.png", "Зелёный с рисом и соевой пудрой. 250г."),
    ("Драконий Колодец", "https://alephtrade.com/images/310_dragon_well.png", "Зелёный Dragon Well, Китай. 250г."),
    ("Молочный Улун", "https://alephtrade.com/images/315_milk_oolong.png", "Зелёный молочный улун, Китай. 250г."),
    ("Цветочная Сенча", "https://alephtrade.com/images/435_flowery_sencha.jpg", "Зелёный с гибискусом и добавками. 150г."),
    ("Серебристый Жасмин", "https://alephtrade.com/images/400_silver_jasmine.png", "Зелёный с цветами жасмина. 250г."),
    ("Жемчужины Дракона", "https://alephtrade.com/images/405_dragon_pearls.png", "Зелёный с цветами жасмина — жемчужины. 250г."),
    ("Женьшеневый Улун", "https://alephtrade.com/images/410_ginseng_oolong.png", "Зелёный с женьшенем. 500г."),
    ("Марокканская Ночь", "https://alephtrade.com/images/420_marrakesh_night.png", "Зелёный с мятой. 500г."),
    ("Зелёный Жасмин", "https://alephtrade.com/images/loose_jasmine_emerald_retail.jpg", "Зелёный с цветами жасмина. 100г."),
    ("Золотая Лагуна", "https://alephtrade.com/images/500_golden_lagoon.png", "Белый с тропическими фруктами. 250г."),
    ("Пина Колада", "https://alephtrade.com/images/505_pina_colada.png", "Белый с кокосом и ананасом. 250г."),
    ("Краснополянский Микс", "https://alephtrade.com/images/515_krasnaya_polyana.jpg", "Травяной сбор с фруктами и мёдом. 200г."),
]


# === Утилиты ===
RU_NOISE = {
    "ALTHAUS","NIKTEA","ЧАЙ","ЧЕРНЫЙ","ЧЁРНЫЙ","БЛЭК","BLACK","ЗЕЛЁНЫЙ","ЗЕЛЕНЫЙ","GREEN",
    "БЕЛЫЙ","WHITE","ЛИСТОВОЙ","ПИРАМИДКИ","ПАКЕТ","ПАК","ПИРАМИДКА","ПИР","ARO","РАСС",
    "ПОРЦ","BLEND","КУПАЖ","ПИРАМ","ROOIBOS","РОЙБУШ","ВКУС","МИКС","ЧАШКА","КОЛЛ","МОЛТИ",
    "СУПРИМ","ВЫСОКОГОРНЫЙ","ВЫС","ИНД","КИТ","ШРИ","ЛАНКА","ПОДАР","Г","КГ","ШТ"
}


# алиасы орфографии — приводим к каноничной форме
ALIASES = {
    "МЭЛЕНГ": "МЕЛЕНГ", "МЕЛИНГ": "МЕЛЕНГ",
    "ТИНГ": "ТИН",
    "ГРЮН": "ЗЕЛЁНЫЙ_МАТИНЕ", "МАТИНЭ": "ЗЕЛЁНЫЙ_МАТИНЕ", "МАТИНЕЕ": "ЗЕЛЁНЫЙ_МАТИНЕ", "МАТИНЕ": "ЗЕЛЁНЫЙ_МАТИНЕ",
    "ГРЕЙ": "ГРЕЙ", "GREY": "ГРЕЙ", "ГРАФ": "ГРЕЙ",
    "ЭРЛ": "ГРЕЙ", "ИМПЕРАТОРСКИЙ": "ИМПЕРАТОРСКИЙ_ГРЕЙ", "ИМПЕРИАЛ": "ИМПЕРАТОРСКИЙ_ГРЕЙ",
    "РОЙАЛ": "РОЙАЛ", "ROYAL": "РОЙАЛ",
    "РЕД": "КРАСНЫЙ", "ФРУТ": "ФРУКТ", "ФРАШ": "ВСПЫШКА", "ФЛАШ": "ВСПЫШКА",
    "БЭРРИЗ": "ЯГОДЫ", "BERRIES": "ЯГОДЫ", "ЯГОДНЫЙ": "ЯГОДЫ",
    "УАЙЛД": "ДИКИЙ", "WILD": "ДИКИЙ", "ДИКАЯ": "ДИКИЙ",
    "ВАНИЛЬ": "ВАНИЛЬ", "ВАНИЛЬНАЯ": "ВАНИЛЬ", "ВАНИЛЬНЫЙ": "ВАНИЛЬ",
    "СЕНЧА": "СЕНЧА", "SENCHA": "СЕНЧА",
    "ЖАСМИН": "ЖАСМИН", "ЖАСМИНОВЫЙ": "ЖАСМИН",
    "ЛЕМОНГРАСС": "ЛЕМОНГРАСС", "ЛЕМОН": "ЛИМОН", "МИНТ": "МЯТА", "MINT": "МЯТА",
    "БЛУМ": "ЦВЕТЫ",
    "ДРАКОНИЙ": "ДРАКОН", "DRAGON": "ДРАКОН",
    "ВЫСОКОГОРНЫЙ": "ВЫСОКОГОРНЫЙ",
}

def kw(s):
    s = s.upper()
    s = re.sub(r'[ЁЕ]', 'Е', s)  # ё → е для устойчивости
    s = re.sub(r'[^А-ЯA-Z0-9\s]+', ' ', s)
    out = set()
    for w in s.split():
        if len(w) < 3:
            continue
        w = ALIASES.get(w, w)
        out.add(w)
    return out - RU_NOISE


def score(tma_name, ref_name):
    a = kw(tma_name)
    b = kw(ref_name)
    if not a or not b:
        return 0.0
    inter = len(a & b)
    return inter / max(len(a), len(b))


def download(url, dest):
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=20) as r:
            data = r.read()
            if len(data) < 500:
                return False
            dest.write_bytes(data)
            return True
    except Exception as e:
        print(f"  ! {url}: {e}")
        return False


def main():
    if not PRODUCTS_JSON.exists():
        raise SystemExit(f"Не найден {PRODUCTS_JSON}")
    data = json.loads(PRODUCTS_JSON.read_text(encoding="utf-8"))
    products = data["products"]

    # Только товары с category == 'tea' и subcategory ALTHAUS / NIKTEA
    althaus_prods = [p for p in products if p.get("subcategory") == "tea_althaus"]
    niktea_prods = [p for p in products if p.get("subcategory") == "tea_niktea"]
    print(f"TMA: ALTHAUS = {len(althaus_prods)}, NIKTEA = {len(niktea_prods)}")
    print(f"Источник: ALTHAUS = {len(ALTHAUS)}, NIKTEA = {len(NIKTEA)}")

    def match_brand(tma_list, ref_list):
        # Для каждого TMA-товара берём САМЫЙ лучший ref (один ref может быть у нескольких TMA — это разные форматы одного сорта)
        # download cache: ref_url → локальный файл (чтобы не качать одно и то же дважды)
        ref_cache = {}
        matched = 0
        for t in tma_list:
            best, best_s = None, 0.0
            for ref in ref_list:
                rname, rurl, rdesc = ref
                s = score(t["name"], rname)
                if s > best_s:
                    best_s, best = s, ref
            if not best or best_s < 0.30:
                continue
            rname, rurl, rdesc = best
            # скачиваем один раз
            if rurl not in ref_cache:
                tmp = PHOTOS_DIR / f"_ref_{abs(hash(rurl))}.jpg"
                if download(rurl, tmp):
                    ref_cache[rurl] = tmp
                else:
                    ref_cache[rurl] = None
            src = ref_cache[rurl]
            if not src:
                continue
            # копируем в файл с tma_id (одна и та же картинка может быть у нескольких товаров)
            dest = PHOTOS_DIR / f"{t['id']}.jpg"
            dest.write_bytes(src.read_bytes())
            t["photo"] = f"photos/products/{t['id']}.jpg"
            t["description"] = rdesc
            t["_source"] = "alephtrade"
            matched += 1
            print(f"  ✓ [{best_s:.2f}] {t['name'][:40]:<40} → {rname}")
        # удаляем временные ref-файлы
        for tmp in ref_cache.values():
            if tmp and tmp.exists():
                tmp.unlink()
        return matched, len(tma_list) - matched

    print("\n=== ALTHAUS ===")
    a_match, a_skip = match_brand(althaus_prods, ALTHAUS)
    print(f"\n=== NIKTEA ===")
    n_match, n_skip = match_brand(niktea_prods, NIKTEA)

    PRODUCTS_JSON.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n=== Итого ===")
    print(f"ALTHAUS: {a_match} сматчено, {a_skip} без матча")
    print(f"NIKTEA: {n_match} сматчено, {n_skip} без матча")
    print(f"Сохранено в {PRODUCTS_JSON}")


if __name__ == "__main__":
    main()
