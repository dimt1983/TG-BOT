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

# Provider token для Telegram Payments (получается у платёжного провайдера через @BotFather)
# Тестовый токен ЮKassa: формат "1744374395:TEST:..."
# Боевой: получить в @BotFather → /mybots → твой бот → Payments → выбрать провайдера
PAYMENTS_PROVIDER_TOKEN = os.environ.get("PAYMENTS_PROVIDER_TOKEN", "")
PAYMENTS_CURRENCY = os.environ.get("PAYMENTS_CURRENCY", "RUB")


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
    subtotal = sum(it["price"] * it["qty"] for it in items)
    total_kg = payload.get("totalKg") or sum(
        (parse_kg(it.get("fasovka", "")) or 0) * it["qty"] for it in items
    )
    discount_pct = 0.20 if total_kg >= 25 else 0.10 if total_kg >= 10 else 0
    discount_amt = round(subtotal * discount_pct)
    total = subtotal - discount_amt

    # Создаём шапку заказа
    cur = con.execute(
        """INSERT INTO orders (user_id, name, phone, address, total, discount, status)
           VALUES (?, ?, ?, ?, ?, ?, 'new')""",
        (
            user_id,
            contact.get("name") or "Клиент TMA",
            contact.get("phone") or "",
            contact.get("address") or "уточнить при подтверждении",
            total,
            discount_amt,
        ),
    )
    order_id = cur.lastrowid

    # Позиции
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
        con.execute(
            "INSERT INTO order_items (order_id, product_id, quantity, price) VALUES (?, ?, ?, ?)",
            (order_id, pid, it["qty"], it["price"]),
        )
        # Списание остатка (если есть колонка)
        try:
            con.execute("UPDATE products SET stock = stock - ? WHERE id = ?", (it["qty"], pid))
        except Exception:
            pass

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

    # Считаем итог
    subtotal = sum(it["price"] * it["qty"] for it in items)
    kg = sum((parse_kg(it.get("fasovka", "")) or 0) * it["qty"] for it in items)
    disc_pct = 0.20 if kg >= 25 else 0.10 if kg >= 10 else 0
    saved = round(subtotal * disc_pct)
    total = subtotal - saved

    # Сообщение покупателю
    lines = [f"✅ <b>Заказ №{order_id} оформлен!</b>\n"]
    for it in items[:15]:
        lines.append(
            f"• {it['name'][:48]}\n  <i>{it['qty']} × {it['price']} ₽ ({it.get('fasovka','')})</i>"
        )
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
        for it in items[:25]:
            adm_lines.append(f"• {it['name'][:50]} — {it.get('fasovka','')} × {it['qty']} = {it['price']*it['qty']:.0f} ₽")
        if len(items) > 25:
            adm_lines.append(f"... и ещё {len(items) - 25} позиций")
        adm_lines.append(f"\n⚖️ {kg:.2f} кг   💰 {subtotal:.0f} ₽")
        if saved:
            adm_lines.append(f"🎁 Скидка {int(disc_pct*100)}%: −{saved} ₽")
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

    return {"order_id": order_id, "total": total, "is_new_client": is_new_client}


# ============================================================
# 3. Telegram Payments: создание Invoice
# ============================================================

async def send_payment_invoice(bot: Bot, chat_id: int, order_id: int, items: list, total: float):
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
        await bot.send_invoice(
            chat_id=chat_id,
            title=f"Заказ Roastberry №{order_id}",
            description=f"{len(items)} позиций · доставка по согласованию",
            payload=f"order_{order_id}",
            provider_token=PAYMENTS_PROVIDER_TOKEN,
            currency=PAYMENTS_CURRENCY,
            prices=prices,
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

        # Считаем итог для ответа
        subtotal = sum(it["price"] * it["qty"] for it in items)
        kg = sum((parse_kg(it.get("fasovka", "")) or 0) * it["qty"] for it in items)
        disc_pct = 0.20 if kg >= 25 else 0.10 if kg >= 10 else 0
        saved = round(subtotal * disc_pct)
        total = subtotal - saved

        lines = [f"✅ <b>Заказ №{order_id} оформлен!</b>\n"]
        for it in items[:15]:
            lines.append(f"• {it['name'][:48]}\n  <i>{it['qty']} × {it['price']} ₽ ({it.get('fasovka','')})</i>")
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
            await send_payment_invoice(bot, message.chat.id, order_id, items, total)

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
            con.execute("UPDATE orders SET status = 'paid' WHERE id = ?", (order_id,))
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
