"""
tma_handler.py — интеграция Telegram Mini App с ботом.

Что делает:
1. Регистрирует команду /shop и кнопку «🛍️ Магазин» открывающие Mini App
2. Принимает данные корзины из TMA через F.web_app_data
3. Создаёт заказ в той же БД, что и обычный flow бота
4. Уведомляет админа через notify_new_order

Подключение к bot.py — одной строкой:
    from tma_handler import register_tma_handlers
    register_tma_handlers(dp, bot, get_db, notify_new_order)
"""
import json
import logging
import os
import re
from datetime import datetime, timezone
from typing import Callable

from aiogram import Dispatcher, Bot, F
from aiogram.filters import Command
from aiogram.types import (
    Message, WebAppInfo, KeyboardButton, ReplyKeyboardMarkup,
    InlineKeyboardMarkup, InlineKeyboardButton, MenuButtonWebApp,
    LabeledPrice, PreCheckoutQuery,
)

log = logging.getLogger(__name__)

# URL твоего Mini App. Должен быть HTTPS на продакшене.
# Для теста — обычная http-страница работает только через Telegram Desktop.
TMA_URL = os.environ.get("TMA_URL", "https://roastberry-tma.up.railway.app/")

# ID админа для уведомлений (может быть не задан — тогда notify_new_order возьмёт сам)
ADMIN_ID = int(os.environ.get("ADMIN_ID", "0") or 0)

# Кэш products.json для определения обжарки/категории по tma_id в TG-уведомлениях
_PRODUCTS_JSON = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                              "tma_static", "products.json")
_ROAST_CACHE = {"mtime": 0, "data": {}}


def _build_roast_map() -> dict:
    """Возвращает {tma_id: {category, roast}}. Кэш инвалидируется по mtime."""
    try:
        mt = os.path.getmtime(_PRODUCTS_JSON)
    except OSError:
        return _ROAST_CACHE["data"]
    if mt == _ROAST_CACHE["mtime"]:
        return _ROAST_CACHE["data"]
    try:
        with open(_PRODUCTS_JSON, encoding="utf-8") as f:
            j = json.load(f)
        prods = j if isinstance(j, list) else j.get("products", [])
        out = {}
        for p in prods:
            pid = p.get("id")
            if pid:
                out[pid] = {
                    "category": p.get("category") or "",
                    "roast":    p.get("roast") or "",
                }
        _ROAST_CACHE["mtime"] = mt
        _ROAST_CACHE["data"]  = out
    except Exception as e:
        log.warning(f"_build_roast_map failed: {e}")
    return _ROAST_CACHE["data"]

# Provider token для Telegram Payments (получается у платёжного провайдера через @BotFather)
# Тестовый токен ЮKassa: формат "1744374395:TEST:..."
# Боевой: получить в @BotFather → /mybots → твой бот → Payments → выбрать провайдера
PAYMENTS_PROVIDER_TOKEN = os.environ.get("PAYMENTS_PROVIDER_TOKEN", "")
PAYMENTS_CURRENCY = os.environ.get("PAYMENTS_CURRENCY", "RUB")
PAYMENTS_VAT_CODE = int(os.environ.get("PAYMENTS_VAT_CODE", "1") or 1)
PAYMENTS_TAX_SYSTEM_CODE = os.environ.get("PAYMENTS_TAX_SYSTEM_CODE", "").strip()
PAYMENTS_PAYMENT_MODE = os.environ.get("PAYMENTS_PAYMENT_MODE", "full_payment")
PAYMENTS_PAYMENT_SUBJECT = os.environ.get("PAYMENTS_PAYMENT_SUBJECT", "commodity")
PERSONAL_DATA_CONSENT_VERSION = os.environ.get(
    "PERSONAL_DATA_CONSENT_VERSION", "pdn-2026-06-30"
)


# ============================================================
# 1. Команды и кнопки для запуска Mini App
# ============================================================

def shop_keyboard() -> ReplyKeyboardMarkup:
    """Reply-клавиатура с кнопкой запуска TMA как WebApp."""
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(
            text="🛍️ Магазин (приложение)",
            web_app=WebAppInfo(url=TMA_URL),
        )]],
        resize_keyboard=True,
    )


def shop_inline_button() -> InlineKeyboardMarkup:
    """Inline-кнопка для прикрепления к любому сообщению."""
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(
            text="🛍️ Открыть магазин",
            web_app=WebAppInfo(url=TMA_URL),
        )
    ]])


# ============================================================
# 2. Преобразование данных из TMA → запись заказа в БД
# ============================================================

def find_product_id(con, item_name: str, fasovka: str):
    """Ищем товар в БД по имени + фасовке. Возвращаем id или None."""
    # имя в TMA — без явной указки 1кг/200г, фасовка отдельно. В БД имя обычно с фасовкой.
    # пробуем разные варианты
    candidates = [
        f"{item_name} {fasovka}",
        f"{item_name} - {fasovka}",
        item_name,
    ]
    # маленькая нормализация фасовки
    fas_g = fasovka.replace(" ", "").lower()
    if fas_g == "1кг":
        candidates.append(f"{item_name} 1 кг")
    elif fas_g.endswith("г"):
        candidates.append(f"{item_name} {fas_g[:-1]} г")

    for q in candidates:
        row = con.execute("SELECT id FROM products WHERE name = ? LIMIT 1", (q,)).fetchone()
        if row:
            return row[0] if not hasattr(row, "keys") else row["id"]
    # last resort — поиск по LIKE
    like = f"%{item_name}%{fas_g[:3]}%"
    row = con.execute("SELECT id FROM products WHERE name LIKE ? LIMIT 1", (like,)).fetchone()
    if row:
        return row[0] if not hasattr(row, "keys") else row["id"]
    return None


def _ensure_tma_id_column(con) -> None:
    """Идемпотентная миграция: добавляет order_items.tma_id и roast_choice если ещё нет."""
    cols = {r[1] for r in con.execute("PRAGMA table_info(order_items)").fetchall()}
    changed = False
    if "tma_id" not in cols:
        con.execute("ALTER TABLE order_items ADD COLUMN tma_id TEXT")
        changed = True
    if "roast_choice" not in cols:
        con.execute("ALTER TABLE order_items ADD COLUMN roast_choice TEXT")
        changed = True
    if changed:
        con.commit()


def _ensure_order_payment_columns(con) -> None:
    cols = {r[1] for r in con.execute("PRAGMA table_info(orders)").fetchall()}
    add = []
    if "payment_method" not in cols:
        add.append("ALTER TABLE orders ADD COLUMN payment_method TEXT")
    if "paid_at" not in cols:
        add.append("ALTER TABLE orders ADD COLUMN paid_at TEXT")
    for sql in add:
        con.execute(sql)
    if add:
        con.commit()


def _ensure_user_profile_columns(con) -> None:
    """Миграция users — поля для авто-подстановки в checkout. Идемпотентна."""
    cols = {r[1] for r in con.execute("PRAGMA table_info(users)").fetchall()}
    add = []
    if "company_name"   not in cols: add.append("ALTER TABLE users ADD COLUMN company_name TEXT")
    if "inn"            not in cols: add.append("ALTER TABLE users ADD COLUMN inn TEXT")
    if "legal_address"  not in cols: add.append("ALTER TABLE users ADD COLUMN legal_address TEXT")
    if "email"          not in cols: add.append("ALTER TABLE users ADD COLUMN email TEXT")
    if "user_type"      not in cols: add.append("ALTER TABLE users ADD COLUMN user_type TEXT")
    if "pd_consent_at"  not in cols: add.append("ALTER TABLE users ADD COLUMN pd_consent_at TEXT")
    if "pd_consent_version" not in cols: add.append("ALTER TABLE users ADD COLUMN pd_consent_version TEXT")
    for sql in add:
        con.execute(sql)
    if add:
        con.commit()


def _normalize_personal_data_consent(payload: dict) -> dict:
    consent = payload.get("consent") or {}
    accepted = bool(consent.get("accepted") or consent.get("signedAt"))
    if not accepted:
        raise ValueError("Подтвердите согласие на обработку персональных данных")
    signed_at = (consent.get("signedAt") or "").strip()
    if not signed_at:
        signed_at = datetime.now(timezone.utc).isoformat()
    version = (consent.get("version") or PERSONAL_DATA_CONSENT_VERSION).strip()
    normalized = {"accepted": True, "version": version, "signedAt": signed_at}
    payload["consent"] = normalized
    return normalized


def _upsert_user_profile(con, user_id: int, payload: dict) -> None:
    """После успешного заказа сохраняем имя/телефон/email/юр.данные в users
    под user_id. Поля без значения не затираются. Используется и для TG-юзеров,
    и для гостей (у них user_id отрицательный)."""
    _ensure_user_profile_columns(con)
    contact = payload.get("contact") or {}
    user_type = contact.get("type") or "individual"
    fields = {
        "name":          contact.get("name") or contact.get("recipient_name") or "",
        "phone":         contact.get("phone") or "",
        "address":       contact.get("address") or contact.get("delivery_address") or "",
        "user_type":     user_type,
    }
    if user_type == "company":
        fields["company_name"]  = contact.get("company") or ""
        fields["inn"]           = contact.get("inn") or ""
        fields["legal_address"] = contact.get("legal_address") or ""
        fields["email"]         = contact.get("email") or ""
    else:
        fields["email"]         = contact.get("email") or ""

    consent = payload.get("consent") or {}
    if consent.get("accepted"):
        fields["pd_consent_at"] = consent.get("signedAt") or ""
        fields["pd_consent_version"] = consent.get("version") or PERSONAL_DATA_CONSENT_VERSION

    # Только непустые значения — чтобы не затирать ранее сохранённое
    fields = {k: v for k, v in fields.items() if v}
    if not fields:
        return

    # UPSERT (SQLite 3.24+, имеется в Python 3.11+)
    row = con.execute(
        "SELECT 1 FROM users WHERE user_id=?", (user_id,)
    ).fetchone()
    if row:
        set_clause = ", ".join(f"{k}=?" for k in fields.keys())
        con.execute(
            f"UPDATE users SET {set_clause} WHERE user_id=?",
            (*fields.values(), user_id),
        )
    else:
        cols = ["user_id"] + list(fields.keys())
        placeholders = ",".join(["?"] * len(cols))
        con.execute(
            f"INSERT INTO users ({','.join(cols)}) VALUES ({placeholders})",
            (user_id, *fields.values()),
        )
    con.commit()


def _apply_personal_pricing(con, user_id: int, items: list) -> None:
    """Пересчитывает price у каждой позиции с учётом user_pricing-правил клиента.
    Меняет items in-place. Правила НЕ суммируются — берётся самое выгодное
    клиенту (минимальная цена). На позиции с user_pricing tier/volume-скидка
    больше НЕ применяется (item['_personal_priced'] = True).

    Не трогает tier-скидку: она применяется отдельно в _compute_order_discount()."""
    # Знак user_id роли не играет: у гостя и WhatsApp-клиента он
    # отрицательный, но правила в user_pricing заведены именно на этот id.
    # Раньше здесь стояло user_id <= 0, и назначенная таким карточкам
    # скидка молча не применялась.
    if not user_id or not items:
        return
    try:
        from tma_static_server import (
            _user_pricing_rules, _matching_pricing_rules, _apply_pricing_rules,
            _ensure_pricing_schema,
        )
    except Exception:
        return
    try:
        _ensure_pricing_schema()
        rules = _user_pricing_rules(con, user_id)
        if not rules:
            return
        prod_meta = {}
        try:
            with open(_PRODUCTS_JSON, encoding="utf-8") as f:
                j = json.load(f)
            for p in (j if isinstance(j, list) else j.get("products", [])):
                pid = p.get("id")
                if pid:
                    prod_meta[pid] = {
                        "category": p.get("category") or "",
                        "subcategory": p.get("subcategory") or "",
                    }
        except Exception:
            pass
        for it in items:
            tma_id = it.get("id") or ""
            meta = prod_meta.get(tma_id, {})
            category = meta.get("category") or it.get("category") or ""
            subcategory = meta.get("subcategory") or it.get("subcategory") or ""
            fasovka_size = it.get("fasovka") or ""
            applicable = _matching_pricing_rules(
                rules, tma_id=tma_id,
                category=category, subcategory=subcategory,
                fasovka_size=fasovka_size,
            )
            if not applicable:
                continue
            base = float(it.get("price") or 0)
            new_price = _apply_pricing_rules(base, applicable)
            if abs(new_price - base) > 0.01:
                it["price"] = new_price
                it["_personal_priced"] = True
                it["_personal_rule_ids"] = [r.get("id") for r in applicable]


    except Exception as e:
        log.warning(f"_apply_personal_pricing failed: {e}")


def _compute_order_discount(con, user_id: int, items: list) -> tuple[float, float, dict]:
    """Считает скидку на ОБЩИЕ позиции (без user_pricing). Скидки НЕ суммируются —
    берётся max(tier_pct, volume_pct).

    Возвращает (subtotal_personal, subtotal_general_after_discount, breakdown).
      - subtotal_personal: сумма позиций с user_pricing (цена уже снижена)
      - subtotal_general_after_discount: сумма позиций без user_pricing после max-скидки

    Логика: позиции с персональной скидкой уже получили выгодную цену — общая
    оптовая (tier/volume) к ним НЕ применяется. К остальным — max(tier, volume).
    Так клиент не получает «двойную» скидку."""
    tier_pct = 0.0
    tier_name = "standard"
    try:
        from tma_static_server import _user_tier, TIER_AUTO_PCT
        tier_name = _user_tier(con, user_id)
        tier_pct = TIER_AUTO_PCT.get(tier_name, 0.0)
    except Exception:
        pass

    subtotal_personal = 0.0
    subtotal_general = 0.0
    total_kg_general = 0.0
    for it in items:
        line = float(it.get("price") or 0) * float(it.get("qty") or 0)
        kg = (parse_kg(it.get("fasovka", "")) or 0) * float(it.get("qty") or 0)
        if it.get("_personal_priced"):
            subtotal_personal += line
        else:
            subtotal_general += line
            total_kg_general += kg

    volume_pct = 20.0 if total_kg_general >= 25 else (10.0 if total_kg_general >= 10 else 0.0)
    # НЕ суммируем — берём максимум (бизнес-правило: одна скидка на позицию).
    general_pct = max(tier_pct, volume_pct)
    subtotal_general_after = round(subtotal_general * (1 - general_pct / 100), 2)

    return subtotal_personal, subtotal_general_after, {
        "tier_pct": tier_pct,
        "tier_name": tier_name,
        "volume_pct": volume_pct,
        "general_pct": general_pct,
        "total_kg": total_kg_general,
        "subtotal_personal": subtotal_personal,
        "subtotal_general": subtotal_general,
        "subtotal_general_after": subtotal_general_after,
    }


def _absorb_phone_stub(con, user_id: int, phone: str | None) -> None:
    """Забрать заглушку с тем же номером в настоящую учётку.

    Человек, открывший магазин по обычной ссылке, попадает гостем и получает
    отрицательный id. Зайдя потом через Telegram, он заводил вторую карточку —
    так «Ирина» и «Ирина Громова» стали двумя клиентами с одним телефоном.
    Заказы, избранное, тариф и персональные правила переносим на настоящую.
    """
    try:
        if not phone or int(user_id) <= 0:
            return
        import concierge as cz
        stub = cz.find_stub_by_phone(con, phone, exclude_id=int(user_id))
        if stub is None:
            return
        res = cz.merge_stub_into_real(con, stub, int(user_id))
        if res.get("ok"):
            log.info("tma: склеил заглушку %s → %s (заказов %s, правил %s)",
                     stub, user_id, res.get("merged_orders"), res.get("moved_rules"))
    except Exception as e:                      # склейка не должна ронять заказ
        log.warning("tma: склейка заглушки не удалась: %s", e)


def create_order_from_tma(con, user_id: int, payload: dict) -> int:
    """
    Создаёт заказ из payload, который пришёл из TMA.
    Возвращает order_id.

    payload формат:
    {
      "type": "order",
      "items": [{"id":..,"name":..,"fasovka":..,"price":..,"qty":..,"category":..}],
      "subtotal": 1234, "discount_pct": 0.10, "saved": 123, "total": 1111,
      "totalKg": 12.5,
      "contact": {"name":"Иван","phone":"...","address":"..."}  // опционально
    }
    """
    items = payload.get("items", [])
    contact = payload.get("contact") or {}
    _normalize_personal_data_consent(payload)
    _ensure_order_payment_columns(con)
    _absorb_phone_stub(con, user_id, contact.get("phone"))
    no_price = []
    for it in items:
        if it.get("price") is None or it.get("on_request"):
            label = it.get("name") or "товар"
            fasovka = it.get("fasovka") or ""
            no_price.append(f"{label} ({fasovka})" if fasovka else label)
    if no_price:
        raise ValueError(
            "В корзине есть позиции «по запросу» (без цены): "
            + ", ".join(no_price[:5])
            + (f" и ещё {len(no_price)-5}" if len(no_price) > 5 else "")
            + ". Уберите их из корзины или напишите менеджеру — оформим вручную."
        )
    _apply_personal_pricing(con, user_id, items)
    subtotal = sum((it.get("price") or 0) * it["qty"] for it in items)
    sub_personal, sub_general_after, breakdown = _compute_order_discount(con, user_id, items)
    total = round(sub_personal + sub_general_after, 2)
    discount_amt = round(subtotal - total)
    payload["_discount_breakdown"] = breakdown
    payload["_final_total"] = total
    payload["_final_subtotal"] = subtotal

    # Создаём шапку заказа
    cur = con.execute(
        """INSERT INTO orders (user_id, name, phone, address, total, discount, status, payment_method)
           VALUES (?, ?, ?, ?, ?, ?, 'new', ?)""",
        (
            user_id,
            contact.get("name") or "Клиент TMA",
            contact.get("phone") or "",
            contact.get("address") or "уточнить при подтверждении",
            total,
            discount_amt,
            payload.get("payment_method") or "on_delivery",
        ),
    )
    order_id = cur.lastrowid

    # Позиции
    _ensure_tma_id_column(con)
    for it in items:
        pid = find_product_id(con, it["name"], it.get("fasovka", ""))
        if pid is None:
            # фоллбек: создаём «заглушку»-товар чтобы FK не падал
            cur2 = con.execute(
                "INSERT INTO products (name, description, price, stock) VALUES (?, ?, ?, 0)",
                (f"{it['name']} ({it.get('fasovka','')})", "Из Mini App", it["price"]),
            )
            pid = cur2.lastrowid
            log.warning(f"TMA: товар не найден в БД, создан заглушка id={pid}: {it['name']}")
        # tma_id (строковый id из products.json) — нужен админке чтобы точно
        # отличить E-обжарку от F-обжарки у товаров с одинаковым именем.
        tma_id = (it.get("id") or "")[:128]
        # roast_choice — выбор клиента для EF-карточек (один товар, два рецепта)
        roast_choice = (it.get("roast_choice") or "").strip().upper()[:2] or None
        con.execute(
            "INSERT INTO order_items (order_id, product_id, quantity, price, tma_id, roast_choice) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (order_id, pid, it["qty"], it["price"], tma_id or None, roast_choice),
        )
        # Списание остатка (если есть колонка)
        try:
            con.execute("UPDATE products SET stock = stock - ? WHERE id = ?", (it["qty"], pid))
        except Exception:
            pass

    # Сохраняем профиль для авто-подстановки в следующий заказ
    try:
        _upsert_user_profile(con, user_id, payload)
    except Exception as e:
        log.warning(f"upsert user profile failed: {e}")

    con.commit()
    return order_id


def parse_kg(fasovka_str: str) -> float | None:
    import re
    m = re.search(r"(\d+(?:\.\d+)?)\s*(кг|г)", fasovka_str.lower())
    if not m:
        return None
    v = float(m.group(1))
    if m.group(2) == "г":
        v /= 1000
    return v


# ============================================================
# 2a. Inline-клавиатура «прогрессивных» кнопок статуса заказа
# ============================================================

def order_status_keyboard(order_id: int, status: str):
    """Кнопки прогрессивно зависят от текущего статуса заказа.
    new        → [✅ Подтвердить] [❌ Отменить]
    confirmed  → [📦 Выполнен]    [❌ Отменить]
    done|cancelled → None (никаких кнопок)
    """
    from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
    if status in ("done", "cancelled"):
        return None
    if status == "confirmed":
        rows = [[
            InlineKeyboardButton(text="📦 Выполнен",  callback_data=f"rb_ord_{order_id}_done"),
            InlineKeyboardButton(text="❌ Отменить", callback_data=f"rb_ord_{order_id}_cancelled"),
        ]]
    else:  # new и любой неизвестный
        rows = [[
            InlineKeyboardButton(text="✅ Подтвердить", callback_data=f"rb_ord_{order_id}_confirmed"),
            InlineKeyboardButton(text="❌ Отменить",   callback_data=f"rb_ord_{order_id}_cancelled"),
        ]]
    return InlineKeyboardMarkup(inline_keyboard=rows)


# ============================================================
# 2b. Полный пайплайн обработки заказа из TMA (HTTP-вариант)
# ============================================================

async def process_tma_order(
    payload: dict,
    user_id: int,
    user_full_name: str,
    get_db,
    bot: Bot,
    notify_new_order=None,
) -> dict:
    """Создаёт заказ из payload (формат как в web_app_data), пишет в БД,
    шлёт подтверждение пользователю и уведомления админам.

    Возвращает {"order_id": int, "total": float, "is_new_client": bool}.
    Бросает ValueError если payload некорректный.
    """
    if payload.get("type") != "order":
        raise ValueError("type != 'order'")
    items = payload.get("items", [])
    if not items:
        raise ValueError("Корзина пуста")

    # Новый ли клиент
    is_new_client = False
    try:
        con = get_db()
        prior = con.execute(
            "SELECT 1 FROM orders WHERE user_id=? LIMIT 1", (user_id,)
        ).fetchone()
        is_new_client = not prior
        con.close()
    except Exception:
        pass

    # Создаём заказ
    con = get_db()
    order_id = create_order_from_tma(con, user_id, payload)
    con.close()

    bd = payload.get("_discount_breakdown") or {}
    subtotal = payload.get("_final_subtotal") or sum((it.get("price") or 0) * it["qty"] for it in items)
    total = payload.get("_final_total") or subtotal
    saved = round(subtotal - total)
    sub_personal = bd.get("subtotal_personal") or 0
    kg = bd.get("total_kg") or sum((parse_kg(it.get("fasovka", "")) or 0) * it["qty"] for it in items)
    tier_pct = bd.get("tier_pct") or 0
    vol_pct = bd.get("volume_pct") or 0
    general_pct = bd.get("general_pct") or 0
    tier_name = bd.get("tier_name") or "standard"
    TIER_LABEL = {"standard": "Стандартный", "discount_10": "Опт −10%",
                  "discount_20": "Опт −20%", "stm": "СТМ"}

    lines = [f"✅ <b>Заказ №{order_id} оформлен!</b>\n"]
    for it in items[:15]:
        mark = " ⭐" if it.get("_personal_priced") else ""
        lines.append(
            f"• {it['name'][:48]}{mark}\n  <i>{it['qty']} × {it.get('price') or 0} ₽ ({it.get('fasovka','')})</i>"
        )
    if len(items) > 15:
        lines.append(f"... и ещё {len(items) - 15} позиций")
    lines.append(f"\n💰 Сумма: {subtotal:.0f} ₽")
    if sub_personal:
        lines.append(f"⭐ С вашими персональными ценами: {sub_personal:.0f} ₽")
    lines.append(f"⚖️ Вес: {kg:.2f} кг")
    if general_pct:
        why = f"тариф «{TIER_LABEL.get(tier_name, tier_name)}»" if tier_pct >= vol_pct else f"за объём ({kg:.1f} кг)"
        lines.append(f"📦 Скидка на остальное −{general_pct:.0f}% ({why})")
    if saved:
        lines.append(f"🎁 <b>Экономия: −{saved} ₽</b>")
    lines.append(f"\n💳 <b>К оплате: {total:.0f} ₽</b>")
    lines.append(
        "\n📦 <b>Сроки</b>\n"
        "  • Сборка и обжарка: 1–2 рабочих дня\n"
        "  • Пермь — курьер/самовывоз: 1–2 дня\n"
        "  • Регионы СДЭК: 3–7 рабочих дней"
    )
    lines.append("\n📞 Менеджер свяжется для подтверждения деталей доставки.")
    try:
        await bot.send_message(user_id, "\n".join(lines), parse_mode="HTML")
    except Exception as e:
        log.warning(f"TMA-HTTP: не удалось отправить подтверждение клиенту {user_id}: {e}")

    # Уведомление админам — полное, со всем составом заказа + кнопки действий
    try:
        from admin import ADMIN_IDS
        contact = payload.get("contact") or {}
        pay = payload.get("payment_method") or "—"
        delivery = (contact.get("delivery") or {})
        d_method = delivery.get("method") or ""
        d_label = {"pickup": "🏬 Самовывоз", "courier": "🚗 Курьер по Перми", "cdek": "📦 СДЭК"}.get(d_method, "🚚 Доставка")
        adm_lines = [
            f"🔔 <b>Новый заказ №{order_id}</b>",
            f"👤 {user_full_name} (id <code>{user_id}</code>)",
            f"📞 {contact.get('phone','—')}",
            f"\n<b>{d_label}</b>",
        ]
        if d_method == "pickup":
            adm_lines.append("📍 Пермь, ул. Карпинского, 14")
        elif d_method == "courier":
            adm_lines.append(f"📍 Пермь — {delivery.get('address','—')}")
            if delivery.get("time"):
                adm_lines.append(f"⏰ {delivery['time']}")
        elif d_method == "cdek":
            adm_lines.append(f"🌍 {delivery.get('city','—')} — {delivery.get('address','—')}")
            if delivery.get("recipient_name") or delivery.get("recipient_phone"):
                adm_lines.append(f"📦 Получатель: {delivery.get('recipient_name','—')} · {delivery.get('recipient_phone','—')}")
        else:
            adm_lines.append(f"📍 {contact.get('address','—')}")
        if contact.get("type") == "company":
            comp = contact.get("company") or contact.get("company_name") or "—"
            adm_lines.append(f"\n🏢 {comp} · ИНН {contact.get('inn','—')}")
            if contact.get("legal_address"):
                adm_lines.append(f"📋 Юр.адрес: {contact['legal_address']}")
            if contact.get("email"):
                adm_lines.append(f"✉️ {contact['email']}")
        if contact.get("comment"):
            adm_lines.append(f"💬 {contact['comment']}")
        adm_lines.append("\n📦 <b>Состав:</b>")
        # Подтягиваем roast/category из products.json чтобы показать бейдж E/F
        # у кофе и группировать визуально.
        _roast_map = _build_roast_map()

        def _roast_badge(item):
            tid = item.get("id")
            meta = _roast_map.get(tid) or {}
            r = (meta.get("roast") or "").strip().upper()
            if not r: return ""
            choice = (item.get("roast_choice") or "").strip().upper()
            if r in ("EF", "E F") and choice in ("E", "F"):
                return "🔴E " if choice == "E" else "🟢F "
            if r in ("E", "BE"):       return "🔴E "
            if r in ("F", "BF"):       return "🟢F "
            if r in ("EF", "E F"):     return "🔴E🟢F "
            if r.startswith("ДРИП"):   return "💧 "
            if r.startswith("NESPRESSO"): return "Ⓝ "
            return ""

        # Сортируем визуально: кофе → курсы → чай → сиропы → молоко → прочее
        cat_order = {"coffee": 0, "consulting": 1, "tea": 2, "syrup": 3, "milk": 4}
        def _cat_key(it):
            tid = it.get("id")
            cat = (_roast_map.get(tid) or {}).get("category") or it.get("category", "")
            return cat_order.get(cat, 99)
        items_sorted = sorted(items[:25], key=_cat_key)

        for it in items_sorted:
            badge = _roast_badge(it)
            adm_lines.append(
                f"• {badge}{it['name'][:50]} — {it.get('fasovka','')} × {it['qty']} = {(it.get('price') or 0)*it['qty']:.0f} ₽"
            )
        if len(items) > 25:
            adm_lines.append(f"... и ещё {len(items) - 25} позиций")
        adm_lines.append(f"\n⚖️ {kg:.2f} кг   💰 {subtotal:.0f} ₽")
        if sub_personal:
            adm_lines.append(f"⭐ Персональные цены: {sub_personal:.0f} ₽")
        if general_pct:
            adm_lines.append(f"📦 Общая скидка −{general_pct:.0f}% (max от tier={tier_pct:.0f}% / volume={vol_pct:.0f}%)")
        if saved:
            adm_lines.append(f"🎁 <b>Экономия клиента: −{saved} ₽</b>")
        adm_lines.append(f"💳 <b>К оплате: {total:.0f} ₽</b> · оплата: {pay}")
        adm_text = "\n".join(adm_lines)
        kb = order_status_keyboard(order_id, "new")
        for aid in ADMIN_IDS:
            try:
                await bot.send_message(aid, adm_text, parse_mode="HTML", reply_markup=kb)
            except Exception as e:
                log.warning(f"TMA-HTTP: не уведомили админа {aid}: {e}")
    except Exception as e:
        log.warning(f"TMA-HTTP: ошибка уведомления админам: {e}")

    # Уведомление о новом клиенте
    if is_new_client:
        try:
            from admin import ADMIN_IDS
            contact = payload.get("contact") or {}
            consent = payload.get("consent") or {}
            consent_str = (
                f"v{consent.get('version','?')} от {consent.get('signedAt','—')[:19]}"
                if consent else "не подписано"
            )
            txt = (
                f"🆕 <b>Новая регистрация клиента</b>\n\n"
                f"👤 {user_full_name}\n"
                f"🆔 tg_id: <code>{user_id}</code>\n"
                f"📞 {contact.get('phone','—')}\n"
                f"🏠 {contact.get('address','—')}\n"
                f"📜 Согласие на обработку ПД: {consent_str}\n\n"
                f"💼 Тип: {contact.get('type','individual')}\n"
                f"🛒 Первый заказ: №{order_id} на {total:.0f} ₽"
            )
            for aid in ADMIN_IDS:
                try:
                    await bot.send_message(aid, txt, parse_mode="HTML")
                except Exception as e:
                    log.warning(f"TMA-HTTP: не уведомили {aid} о новом клиенте: {e}")
        except Exception as e:
            log.warning(f"TMA-HTTP: ошибка блока «новый клиент»: {e}")

    invoice_link = None
    if payload.get("payment_method") == "online":
        if payload.get("invoice_delivery") == "link":
            invoice_link = await create_payment_invoice_link(
                bot, order_id, items, total, payload.get("contact") or {}
            )
        if not invoice_link:
            await send_payment_invoice(bot, user_id, order_id, items, total, payload.get("contact") or {})

    return {
        "order_id": order_id,
        "total": total,
        "is_new_client": is_new_client,
        "invoice_link": invoice_link,
    }


# ============================================================
# 3. Telegram Payments: создание Invoice
# ============================================================

def _receipt_phone(phone: str) -> str:
    digits = re.sub(r"\D", "", phone or "")
    if len(digits) == 11 and digits.startswith("8"):
        digits = "7" + digits[1:]
    return digits


def _receipt_item(item: dict) -> dict:
    qty = float(item.get("qty") or 1)
    price = float(item.get("price") or 0)
    name = item.get("name") or "Товар Roastberry"
    fasovka = item.get("fasovka") or ""
    description = f"{name} {fasovka}".strip()[:128]
    return {
        "description": description,
        "quantity": f"{qty:.3f}".rstrip("0").rstrip("."),
        "amount": {
            "value": f"{price:.2f}",
            "currency": PAYMENTS_CURRENCY,
        },
        "vat_code": PAYMENTS_VAT_CODE,
        "payment_mode": PAYMENTS_PAYMENT_MODE,
        "payment_subject": PAYMENTS_PAYMENT_SUBJECT,
    }


def _build_yookassa_provider_data(items: list, total: float, contact: dict | None = None) -> str:
    receipt_items = []
    for item in items[:100]:
        if item.get("price") is None or item.get("on_request"):
            continue
        if float(item.get("price") or 0) <= 0:
            continue
        receipt_items.append(_receipt_item(item))

    receipt_sum = round(
        sum(float(i["amount"]["value"]) * float(i["quantity"]) for i in receipt_items),
        2,
    )
    total = round(float(total or 0), 2)
    if receipt_items and abs(receipt_sum - total) > 0.01:
        receipt_items = [{
            "description": f"Заказ Roastberry №{contact.get('order_id') if contact else ''}".strip()[:128],
            "quantity": "1",
            "amount": {
                "value": f"{total:.2f}",
                "currency": PAYMENTS_CURRENCY,
            },
            "vat_code": PAYMENTS_VAT_CODE,
            "payment_mode": PAYMENTS_PAYMENT_MODE,
            "payment_subject": PAYMENTS_PAYMENT_SUBJECT,
        }]

    receipt = {"items": receipt_items}
    if PAYMENTS_TAX_SYSTEM_CODE:
        receipt["tax_system_code"] = int(PAYMENTS_TAX_SYSTEM_CODE)
    contact = contact or {}
    customer = {}
    email = (contact.get("email") or "").strip()
    phone = _receipt_phone(contact.get("phone") or "")
    if email:
        customer["email"] = email
    elif phone:
        customer["phone"] = phone
    if customer:
        receipt["customer"] = customer
    return json.dumps({"receipt": receipt}, ensure_ascii=False, separators=(",", ":"))


async def send_payment_invoice(
    bot: Bot,
    chat_id: int,
    order_id: int,
    items: list,
    total: float,
    contact: dict | None = None,
):
    """
    Отправляет инвойс через Telegram Payments.
    Если PAYMENTS_PROVIDER_TOKEN не задан — присылает заглушку с инструкцией.
    """
    if not PAYMENTS_PROVIDER_TOKEN:
        await bot.send_message(
            chat_id,
            f"💳 <b>Онлайн-оплата</b>\n\n"
            f"Заказ №{order_id} принят.\n"
            f"Платёжный провайдер пока не настроен (нужен токен в env <code>PAYMENTS_PROVIDER_TOKEN</code>).\n"
            f"Менеджер свяжется и пришлёт ссылку на оплату вручную.\n\n"
            f"💰 К оплате: <b>{total:.0f} ₽</b>",
            parse_mode="HTML",
        )
        return

    # Формируем prices: тут разрешено указывать максимум 100 элементов
    # Простейший вариант — одна строка "Заказ №X"
    prices = [LabeledPrice(label=f"Заказ Roastberry №{order_id}", amount=int(total * 100))]
    # Можно детализировать по позициям (если total <= sum of items)
    # detailed = [LabeledPrice(label=it["name"][:32], amount=int(it["price"] * it["qty"] * 100))
    #             for it in items[:99]]
    # if abs(sum(p.amount for p in detailed) - prices[0].amount) < 100:
    #     prices = detailed

    try:
        receipt_contact = dict(contact or {})
        receipt_contact["order_id"] = order_id
        await bot.send_invoice(
            chat_id=chat_id,
            title=f"Заказ Roastberry №{order_id}",
            description=f"{len(items)} позиций · доставка по согласованию",
            payload=f"order_{order_id}",
            provider_token=PAYMENTS_PROVIDER_TOKEN,
            currency=PAYMENTS_CURRENCY,
            prices=prices,
            provider_data=_build_yookassa_provider_data(items, total, receipt_contact),
            need_name=False,
            need_phone_number=False,
            need_email=False,
            need_shipping_address=False,
            is_flexible=False,
            start_parameter=f"pay_{order_id}",
        )
    except Exception as e:
        log.exception("TMA: ошибка отправки инвойса")
        await bot.send_message(chat_id, f"⚠️ Не удалось отправить счёт: {e}")


async def create_payment_invoice_link(
    bot: Bot,
    order_id: int,
    items: list,
    total: float,
    contact: dict | None = None,
) -> str | None:
    """Создаёт invoice link для Telegram WebApp.openInvoice()."""
    if not PAYMENTS_PROVIDER_TOKEN:
        return None

    prices = [LabeledPrice(label=f"Заказ Roastberry №{order_id}", amount=int(total * 100))]
    receipt_contact = dict(contact or {})
    receipt_contact["order_id"] = order_id
    try:
        return await bot.create_invoice_link(
            title=f"Заказ Roastberry №{order_id}",
            description=f"{len(items)} позиций · доставка по согласованию",
            payload=f"order_{order_id}",
            provider_token=PAYMENTS_PROVIDER_TOKEN,
            currency=PAYMENTS_CURRENCY,
            prices=prices,
            provider_data=_build_yookassa_provider_data(items, total, receipt_contact),
            need_name=False,
            need_phone_number=False,
            need_email=False,
            need_shipping_address=False,
            is_flexible=False,
        )
    except Exception:
        log.exception("TMA: ошибка создания invoice link")
        return None


# ============================================================
# 4. Регистрация хендлеров
# ============================================================

def register_tma_handlers(
    dp: Dispatcher,
    bot: Bot,
    get_db: Callable,
    notify_new_order: Callable | None = None,
):
    """
    Подключает к существующему боту:
    - команду /shop (открывает Mini App)
    - обработчик F.web_app_data (принимает заказ из TMA)
    - кнопку MenuButton → запуск Mini App из меню рядом с полем ввода
    """

    # ── /shop — отправляем сообщение с кнопкой открытия Mini App ──────────────
    @dp.message(Command("shop"))
    async def cmd_shop(message: Message):
        await message.answer(
            "🛍️ <b>Магазин Roastberry</b>\n\n"
            "Открой удобный каталог в приложении: фото, описания, рецепты, корзина "
            "и автоматический подсчёт скидки от веса.",
            reply_markup=shop_keyboard(),
            parse_mode="HTML",
        )
        # И inline-кнопкой тоже
        await message.answer(
            "Или открой здесь:",
            reply_markup=shop_inline_button(),
        )

    # ── Получение данных из TMA через web_app_data ────────────────────────────
    @dp.message(F.web_app_data)
    async def handle_webapp_data(message: Message):
        raw = message.web_app_data.data
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            await message.answer("❌ Получены некорректные данные из приложения.")
            return

        if payload.get("type") != "order":
            await message.answer(f"📨 Получены данные: {raw[:200]}")
            return

        items = payload.get("items", [])
        if not items:
            await message.answer("⚠️ Корзина пуста.")
            return

        # Определяем, новый ли это клиент (по отсутствию прежних заказов)
        is_new_client = False
        try:
            con = get_db()
            prior = con.execute(
                "SELECT 1 FROM orders WHERE user_id = ? LIMIT 1",
                (message.from_user.id,),
            ).fetchone()
            is_new_client = not prior
            con.close()
        except Exception:
            pass

        # Создаём заказ
        try:
            con = get_db()
            order_id = create_order_from_tma(con, message.from_user.id, payload)
            con.close()
        except Exception as e:
            log.exception("TMA: ошибка создания заказа")
            await message.answer(f"❌ Ошибка при оформлении заказа: {e}")
            return

        # Считаем итог для ответа — берём из payload._discount_breakdown который
        # положил туда create_order_from_tma (с tier + user_pricing + auto).
        bd = payload.get("_discount_breakdown") or {}
        subtotal = payload.get("_final_subtotal") or sum((it.get("price") or 0) * it["qty"] for it in items)
        total = payload.get("_final_total") or subtotal
        saved = round(subtotal - total)
        kg = bd.get("total_kg") or sum((parse_kg(it.get("fasovka", "")) or 0) * it["qty"] for it in items)
        disc_pct = (bd.get("final_pct") or 0) / 100.0

        lines = [f"✅ <b>Заказ №{order_id} оформлен!</b>\n"]
        for it in items[:15]:
            lines.append(f"• {it['name'][:48]}\n  <i>{it['qty']} × {it.get('price') or 0} ₽ ({it.get('fasovka','')})</i>")
        if len(items) > 15:
            lines.append(f"... и ещё {len(items) - 15} позиций")
        lines.append(f"\n💰 Сумма: {subtotal:.0f} ₽")
        lines.append(f"⚖️ Вес кофе: {kg:.2f} кг")
        if saved:
            lines.append(f"🎁 Скидка {int(disc_pct*100)}%: −{saved} ₽")
        lines.append(f"\n💳 <b>К оплате: {total:.0f} ₽</b>")
        lines.append(
            "\n📦 <b>Сроки</b>\n"
            "  • Сборка и обжарка: 1–2 рабочих дня\n"
            "  • Пермь — курьер/самовывоз: 1–2 дня\n"
            "  • Регионы СДЭК: 3–7 рабочих дней"
        )
        lines.append("\n📞 Менеджер свяжется для подтверждения деталей доставки.")
        await message.answer("\n".join(lines), parse_mode="HTML")

        # Уведомление админам — новый заказ
        if notify_new_order:
            try:
                user_name = message.from_user.full_name or "TMA-клиент"
                await notify_new_order(bot, order_id, user_name, total)
            except Exception as e:
                log.warning(f"TMA: не удалось уведомить админа о заказе: {e}")

        # Уведомление админам — новый клиент (первый заказ)
        if is_new_client:
            try:
                from admin import ADMIN_IDS
                contact = payload.get("contact") or {}
                consent = payload.get("consent") or {}
                u = message.from_user
                uname = f"@{u.username}" if u.username else "—"
                consent_str = (
                    f"v{consent.get('version','?')} от {consent.get('signedAt','—')[:19]}"
                    if consent else "не подписано"
                )
                txt = (
                    f"🆕 <b>Новая регистрация клиента</b>\n\n"
                    f"👤 {u.full_name} ({uname})\n"
                    f"🆔 tg_id: <code>{u.id}</code>\n"
                    f"📞 {contact.get('phone','—')}\n"
                    f"🏠 {contact.get('address','—')}\n"
                    f"📜 Согласие на обработку ПД: {consent_str}\n\n"
                    f"💼 Тип: {contact.get('type','individual')}\n"
                    f"🛒 Первый заказ: №{order_id} на {total:.0f} ₽"
                )
                for aid in ADMIN_IDS:
                    try:
                        await bot.send_message(aid, txt, parse_mode="HTML")
                    except Exception as e:
                        log.warning(f"TMA: не уведомили {aid} о новом клиенте: {e}")
            except Exception as e:
                log.warning(f"TMA: ошибка уведомления о новом клиенте: {e}")

        # Если выбрана онлайн-оплата — присылаем Telegram Invoice
        if payload.get("payment_method") == "online":
            await send_payment_invoice(
                bot,
                message.chat.id,
                order_id,
                items,
                total,
                payload.get("contact") or {},
            )

    # ── Telegram Payments: pre-checkout (обязательно отвечать в течение 10 сек) ─
    @dp.pre_checkout_query()
    async def pre_checkout(query: PreCheckoutQuery):
        # Простейшая проверка: всегда соглашаемся, если payload корректный
        await bot.answer_pre_checkout_query(query.id, ok=True)

    # ── Telegram Payments: успешная оплата ────────────────────────────────────
    @dp.message(F.successful_payment)
    async def on_successful_payment(message: Message):
        sp = message.successful_payment
        # payload — это invoice_payload, который мы отдаём через order_id
        order_id = sp.invoice_payload.replace("order_", "") if sp.invoice_payload else "?"
        try:
            con = get_db()
            _ensure_order_payment_columns(con)
            paid_at = datetime.now(timezone.utc).isoformat()
            con.execute(
                "UPDATE orders SET status = 'paid', paid_at = ? WHERE id = ?",
                (paid_at, order_id),
            )
            con.commit(); con.close()
        except Exception:
            pass
        await message.answer(
            f"✅ <b>Оплата получена</b>\n\n"
            f"Заказ №{order_id} оплачен на сумму {sp.total_amount/100:.0f} {sp.currency}.\n"
            f"Передаём заказ на сборку 📦",
            parse_mode="HTML",
        )

    # ── Установить MenuButton (кнопка слева от поля ввода) ────────────────────
    async def setup_menu_button():
        try:
            await bot.set_chat_menu_button(
                menu_button=MenuButtonWebApp(
                    text="🛍️ Магазин",
                    web_app=WebAppInfo(url=TMA_URL),
                )
            )
            log.info(f"TMA menu button установлен: {TMA_URL}")
        except Exception as e:
            log.warning(f"TMA: не удалось установить menu button: {e}")

    # Запустим установку при старте поллинга
    @dp.startup()
    async def _on_startup():
        await setup_menu_button()

    # ── Кнопки статуса заказа в админ-уведомлении (rb_ord_*) ──────────────────
    @dp.callback_query(F.data.startswith("rb_ord_"))
    async def rb_order_status(callback):
        try:
            from admin import ADMIN_IDS
        except Exception:
            ADMIN_IDS = []
        if callback.from_user.id not in ADMIN_IDS:
            await callback.answer("Только для админов", show_alert=True)
            return
        # формат: rb_ord_{order_id}_{status}
        rest = callback.data[len("rb_ord_"):]
        try:
            order_id_str, new_status = rest.rsplit("_", 1)
            order_id = int(order_id_str)
        except Exception:
            await callback.answer("Битый callback", show_alert=True)
            return
        labels = {"confirmed": "✅ Подтверждён", "done": "📦 Выполнен", "cancelled": "❌ Отменён"}
        status_text = labels.get(new_status, new_status)
        # 1) UPDATE в БД
        try:
            con = get_db()
            con.execute("UPDATE orders SET status=? WHERE id=?", (new_status, order_id))
            o = con.execute("SELECT user_id FROM orders WHERE id=?", (order_id,)).fetchone()
            con.commit(); con.close()
        except Exception as e:
            await callback.answer(f"Ошибка БД: {e}", show_alert=True)
            return
        client_uid = o["user_id"] if o else None
        # 2) Перерисовываем сообщение: дописываем статус и обновляем клавиатуру
        new_kb = order_status_keyboard(order_id, new_status)
        try:
            original = callback.message.html_text or callback.message.text or ""
            # Если в тексте уже есть строка «→ ...» (повторное нажатие) — заменим
            base = original.split("\n\n→ ")[0]
            await callback.message.edit_text(
                f"{base}\n\n<b>→ {status_text}</b>",
                parse_mode="HTML", reply_markup=new_kb,
            )
        except Exception:
            try: await callback.message.edit_reply_markup(reply_markup=new_kb)
            except Exception: pass
        # 3) Сообщаем клиенту
        client_msgs = {
            "confirmed": (
                f"✅ <b>Заказ №{order_id} подтверждён</b>\n\n"
                f"Взяли в работу. Свежеобжаренный кофе будет готов через 1–2 рабочих дня.\n\n"
                f"📦 <b>Сроки доставки</b>\n"
                f"• Пермь — курьер/самовывоз: 1–2 дня\n"
                f"• Регионы СДЭК: 3–7 рабочих дней"
            ),
            "done": (
                f"🎉 <b>Заказ №{order_id} выполнен</b>\n\n"
                f"Спасибо что выбрали Roastberry! Если есть вопросы — пишите этим же сообщением."
            ),
            "cancelled": (
                f"❌ <b>Заказ №{order_id} отменён</b>\n\n"
                f"Если это ошибка — напишите нам. При оплате вернём средства в течение 3 рабочих дней."
            ),
        }
        msg = client_msgs.get(new_status, f"📦 <b>Заказ №{order_id}</b>\n\nСтатус: {status_text}")
        if client_uid:
            try:
                await bot.send_message(client_uid, msg, parse_mode="HTML")
            except Exception as e:
                log.warning(f"rb_ord: не доставлено клиенту {client_uid}: {e}")
        await callback.answer(status_text)

    log.info(f"TMA handlers зарегистрированы. URL: {TMA_URL}")
