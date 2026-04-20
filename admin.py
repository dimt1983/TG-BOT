"""
Админ-панель RBR Coffee Shop
Положи рядом с bot.py в одну папку.
Перед запуском замени ADMIN_IDS на свой Telegram ID (узнать: @userinfobot).
"""

import io
import sqlite3
import openpyxl
from aiogram import Dispatcher, F
from aiogram.filters import Command
from aiogram.types import Message, CallbackQuery
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup

# ─── Настройки ───────────────────────────────────────────────────────────────
ADMIN_IDS = [466755177]  # ← замени на свой Telegram ID

# ─── FSM ─────────────────────────────────────────────────────────────────────
class AdminStates(StatesGroup):
    waiting_xlsx        = State()
    waiting_new_name    = State()
    waiting_new_cat     = State()
    waiting_new_price   = State()
    waiting_new_stock   = State()
    waiting_new_photo   = State()
    waiting_edit_price  = State()
    waiting_edit_stock  = State()
    waiting_edit_photo  = State()

# ─── Хелперы ─────────────────────────────────────────────────────────────────
def get_db():
    con = sqlite3.connect("shop.db")
    con.row_factory = sqlite3.Row
    return con

def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS

def admin_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📥 Загрузить xlsx",         callback_data="adm_upload")],
        [InlineKeyboardButton(text="➕ Добавить товар",         callback_data="adm_add_product")],
        [InlineKeyboardButton(text="✏️ Редактировать товар",    callback_data="adm_edit_list")],
        [InlineKeyboardButton(text="❌ Удалить товар",          callback_data="adm_delete_list")],
        [InlineKeyboardButton(text="📋 Все заказы",            callback_data="adm_orders")],
        [InlineKeyboardButton(text="👥 Клиенты",               callback_data="adm_clients")],
        [InlineKeyboardButton(text="📊 Статистика",            callback_data="adm_stats")],
    ])

def leaf_categories_keyboard(prefix: str):
    con = get_db()
    cats = con.execute("""
        SELECT c.id, c.name, p.name as parent_name
        FROM categories c JOIN categories p ON c.parent_id = p.id
    """).fetchall()
    con.close()
    buttons = [[InlineKeyboardButton(
        text=f"{c['parent_name']} / {c['name']}",
        callback_data=f"{prefix}{c['id']}"
    )] for c in cats]
    buttons.append([InlineKeyboardButton(text="◀ Отмена", callback_data="adm_back")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

# ─── /admin ───────────────────────────────────────────────────────────────────
async def admin_cmd(message: Message):
    if not is_admin(message.from_user.id):
        await message.answer("⛔ Нет доступа.")
        return
    await message.answer("🔧 *Панель администратора*", parse_mode="Markdown",
                         reply_markup=admin_keyboard())

async def adm_back(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.message.answer("🔧 *Панель администратора*", parse_mode="Markdown",
                                  reply_markup=admin_keyboard())
    await callback.answer()

# ─── Загрузка xlsx ───────────────────────────────────────────────────────────
async def adm_upload_start(callback: CallbackQuery, state: FSMContext):
    await callback.message.answer(
        "📥 Отправь xlsx-файл.\n\n"
        "*Формат колонок (порядок важен):*\n"
        "`Номенклатура | Цена | Остаток`\n\n"
        "Цену можно не указывать — тогда останется старая.\n"
        "Товары сопоставляются по названию.",
        parse_mode="Markdown"
    )
    await state.set_state(AdminStates.waiting_xlsx)
    await callback.answer()

async def adm_upload_file(message: Message, state: FSMContext, bot):
    if not message.document:
        await message.answer("⚠️ Пришли файл xlsx.")
        return
    if not message.document.file_name.endswith(".xlsx"):
        await message.answer("⚠️ Нужен файл .xlsx")
        return

    file = await bot.get_file(message.document.file_id)
    buf  = io.BytesIO()
    await bot.download_file(file.file_path, buf)
    buf.seek(0)

    try:
        wb = openpyxl.load_workbook(buf, read_only=True, data_only=True)
        ws = wb.active
    except Exception as e:
        await message.answer(f"❌ Не удалось открыть: {e}")
        await state.clear()
        return

    # Пытаемся найти колонки автоматически
    # Поддерживаем форматы:
    #   1) Номенклатура | ... | Остаток  (старый, из 1С)
    #   2) Номенклатура | Цена | Остаток (новый)
    updates = {}  # name -> {stock, price}
    for row in ws.iter_rows(values_only=True):
        if not row[0] or not isinstance(row[0], str) or len(row[0]) < 4:
            continue
        name = row[0].strip()
        # Собираем все числа из строки
        nums = [int(c) if isinstance(c, int) else round(float(c))
                for c in row[1:] if isinstance(c, (int, float))]
        if not nums:
            continue
        if len(nums) >= 2:
            # Предполагаем: предпоследнее = цена, последнее = остаток
            price = float(nums[-2])
            stock = int(nums[-1])
        else:
            price = None
            stock = int(nums[-1])
        updates[name] = {"stock": stock, "price": price}

    if not updates:
        await message.answer("❌ Не нашёл данных. Проверь формат.")
        await state.clear()
        return

    con = get_db()
    updated_stock = 0
    updated_price = 0
    skipped = []

    for name, data in updates.items():
        # Точное совпадение
        row = con.execute("SELECT id, price FROM products WHERE name = ?", (name,)).fetchone()
        if not row:
            # Частичное совпадение по первым 20 символам
            row = con.execute(
                "SELECT id, price FROM products WHERE name LIKE ?", (f"%{name[:20]}%",)
            ).fetchone()
        if row:
            con.execute("UPDATE products SET stock = ? WHERE id = ?", (data["stock"], row["id"]))
            updated_stock += 1
            if data["price"] is not None and data["price"] > 0:
                con.execute("UPDATE products SET price = ? WHERE id = ?", (data["price"], row["id"]))
                updated_price += 1
        else:
            skipped.append(name[:50])

    con.commit()
    con.close()

    text = f"✅ Остатки обновлены: *{updated_stock}* поз."
    if updated_price:
        text += f"\n💰 Цены обновлены: *{updated_price}* поз."
    if skipped:
        skip_list = "\n".join(f"  • {s}" for s in skipped[:15])
        if len(skipped) > 15:
            skip_list += f"\n  ... и ещё {len(skipped) - 15}"
        text += f"\n\n⚠️ Не найдено в БД ({len(skipped)}):\n{skip_list}"

    await message.answer(text, parse_mode="Markdown", reply_markup=admin_keyboard())
    await state.clear()

# ─── Добавить товар ───────────────────────────────────────────────────────────
async def adm_add_start(callback: CallbackQuery, state: FSMContext):
    await callback.message.answer("✏️ Введи *название* нового товара:", parse_mode="Markdown")
    await state.set_state(AdminStates.waiting_new_name)
    await callback.answer()

async def adm_add_name(message: Message, state: FSMContext):
    await state.update_data(name=message.text.strip())
    await message.answer("📂 Выбери категорию:", reply_markup=leaf_categories_keyboard("adm_newcat_"))

async def adm_add_cat(callback: CallbackQuery, state: FSMContext):
    cat_id = int(callback.data.replace("adm_newcat_", ""))
    await state.update_data(cat_id=cat_id)
    await callback.message.answer("💰 Введи *цену* (₽/кг):", parse_mode="Markdown")
    await state.set_state(AdminStates.waiting_new_price)
    await callback.answer()

async def adm_add_price(message: Message, state: FSMContext):
    try:
        price = float(message.text.replace(",", "."))
    except ValueError:
        await message.answer("⚠️ Введи число, например: 1200")
        return
    await state.update_data(price=price)
    await message.answer("📦 Введи *остаток* (кг):", parse_mode="Markdown")
    await state.set_state(AdminStates.waiting_new_stock)

async def adm_add_stock(message: Message, state: FSMContext):
    try:
        stock = int(message.text)
    except ValueError:
        await message.answer("⚠️ Введи целое число.")
        return
    await state.update_data(stock=stock)
    await message.answer(
        "🖼 Отправь *ссылку на фото* товара (URL)\nили напиши *-* чтобы пропустить:",
        parse_mode="Markdown"
    )
    await state.set_state(AdminStates.waiting_new_photo)

async def adm_add_photo(message: Message, state: FSMContext):
    photo_url = None if message.text.strip() == "-" else message.text.strip()
    data = await state.get_data()
    con = get_db()
    con.execute(
        "INSERT INTO products (name, price, stock, category_id, photo_url) VALUES (?,?,?,?,?)",
        (data["name"], data["price"], data["stock"], data["cat_id"], photo_url)
    )
    con.commit()
    con.close()
    await message.answer(
        f"✅ Товар добавлен:\n*{data['name']}*\n"
        f"Цена: {data['price']:.0f} ₽ | Остаток: {data['stock']} кг",
        parse_mode="Markdown", reply_markup=admin_keyboard()
    )
    await state.clear()

# ─── Редактировать товар ──────────────────────────────────────────────────────
async def adm_edit_list(callback: CallbackQuery):
    con = get_db()
    products = con.execute(
        "SELECT id, name, price, stock FROM products ORDER BY name LIMIT 50"
    ).fetchall()
    con.close()
    buttons = [[InlineKeyboardButton(
        text=f"{p['name'][:36]} | {p['price']:.0f}₽ | {p['stock']}кг",
        callback_data=f"adm_edit_{p['id']}"
    )] for p in products]
    buttons.append([InlineKeyboardButton(text="◀ Назад", callback_data="adm_back")])
    await callback.message.answer(
        "✏️ Выбери товар:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons)
    )
    await callback.answer()

async def adm_edit_product(callback: CallbackQuery, state: FSMContext):
    product_id = int(callback.data.replace("adm_edit_", ""))
    con = get_db()
    p = con.execute("SELECT * FROM products WHERE id = ?", (product_id,)).fetchone()
    con.close()
    await state.update_data(edit_product_id=product_id)
    photo_status = "✅ есть" if p["photo_url"] else "❌ нет"
    await callback.message.answer(
        f"📦 *{p['name']}*\n"
        f"Цена: {p['price']:.0f} ₽ | Остаток: {p['stock']} кг | Фото: {photo_status}\n\n"
        "Что изменить?",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="💰 Цену",    callback_data="adm_chg_price")],
            [InlineKeyboardButton(text="📦 Остаток", callback_data="adm_chg_stock")],
            [InlineKeyboardButton(text="🖼 Фото",    callback_data="adm_chg_photo")],
            [InlineKeyboardButton(text="◀ Назад",   callback_data="adm_back")],
        ])
    )
    await callback.answer()

async def adm_chg_price_start(callback: CallbackQuery, state: FSMContext):
    await callback.message.answer("💰 Введи новую цену (₽/кг):")
    await state.set_state(AdminStates.waiting_edit_price)
    await callback.answer()

async def adm_chg_price(message: Message, state: FSMContext):
    try:
        price = float(message.text.replace(",", "."))
    except ValueError:
        await message.answer("⚠️ Введи число.")
        return
    data = await state.get_data()
    con = get_db()
    con.execute("UPDATE products SET price = ? WHERE id = ?", (price, data["edit_product_id"]))
    con.commit()
    con.close()
    await message.answer(f"✅ Цена: *{price:.0f} ₽*", parse_mode="Markdown",
                         reply_markup=admin_keyboard())
    await state.clear()

async def adm_chg_stock_start(callback: CallbackQuery, state: FSMContext):
    await callback.message.answer("📦 Введи новый остаток (кг):")
    await state.set_state(AdminStates.waiting_edit_stock)
    await callback.answer()

async def adm_chg_stock(message: Message, state: FSMContext):
    try:
        stock = int(message.text)
    except ValueError:
        await message.answer("⚠️ Введи целое число.")
        return
    data = await state.get_data()
    con = get_db()
    con.execute("UPDATE products SET stock = ? WHERE id = ?", (stock, data["edit_product_id"]))
    con.commit()
    con.close()
    await message.answer(f"✅ Остаток: *{stock} кг*", parse_mode="Markdown",
                         reply_markup=admin_keyboard())
    await state.clear()

async def adm_chg_photo_start(callback: CallbackQuery, state: FSMContext):
    await callback.message.answer(
        "🖼 Отправь новую *ссылку на фото* (URL)\nили *-* чтобы удалить фото:",
        parse_mode="Markdown"
    )
    await state.set_state(AdminStates.waiting_edit_photo)
    await callback.answer()

async def adm_chg_photo(message: Message, state: FSMContext):
    photo_url = None if message.text.strip() == "-" else message.text.strip()
    data = await state.get_data()
    con = get_db()
    con.execute("UPDATE products SET photo_url = ? WHERE id = ?", (photo_url, data["edit_product_id"]))
    con.commit()
    con.close()
    status = "✅ Фото обновлено." if photo_url else "✅ Фото удалено."
    await message.answer(status, reply_markup=admin_keyboard())
    await state.clear()

# ─── Удалить товар ────────────────────────────────────────────────────────────
async def adm_delete_list(callback: CallbackQuery):
    con = get_db()
    products = con.execute("SELECT id, name FROM products ORDER BY name LIMIT 50").fetchall()
    con.close()
    buttons = [[InlineKeyboardButton(
        text=f"❌ {p['name'][:45]}", callback_data=f"adm_del_{p['id']}"
    )] for p in products]
    buttons.append([InlineKeyboardButton(text="◀ Назад", callback_data="adm_back")])
    await callback.message.answer(
        "❌ Выбери товар для удаления:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons)
    )
    await callback.answer()

async def adm_delete_product(callback: CallbackQuery):
    product_id = int(callback.data.replace("adm_del_", ""))
    con = get_db()
    p = con.execute("SELECT name FROM products WHERE id = ?", (product_id,)).fetchone()
    if p:
        con.execute("DELETE FROM products WHERE id = ?", (product_id,))
        con.execute("DELETE FROM cart WHERE product_id = ?", (product_id,))
        con.commit()
        await callback.message.answer(f"✅ «{p['name']}» удалён.", reply_markup=admin_keyboard())
    con.close()
    await callback.answer()

# ─── Заказы ───────────────────────────────────────────────────────────────────
async def adm_orders(callback: CallbackQuery):
    con = get_db()
    orders = con.execute("SELECT * FROM orders ORDER BY created_at DESC LIMIT 20").fetchall()
    con.close()
    if not orders:
        await callback.message.answer("📋 Заказов пока нет.", reply_markup=admin_keyboard())
        await callback.answer()
        return
    status_emoji = {"new": "🆕", "confirmed": "✅", "done": "📦", "cancelled": "❌"}
    buttons = [[InlineKeyboardButton(
        text=f"{status_emoji.get(o['status'], '❓')} №{o['id']} | {o['total']:.0f}₽ | {o['name']} | {o['created_at'][:10]}",
        callback_data=f"adm_order_{o['id']}"
    )] for o in orders]
    buttons.append([InlineKeyboardButton(text="◀ Назад", callback_data="adm_back")])
    await callback.message.answer(
        "📋 *Последние заказы:*", parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons)
    )
    await callback.answer()

async def adm_order_detail(callback: CallbackQuery):
    order_id = int(callback.data.replace("adm_order_", ""))
    con = get_db()
    o = con.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()
    items = con.execute("""
        SELECT oi.quantity, oi.price, p.name
        FROM order_items oi JOIN products p ON oi.product_id = p.id
        WHERE oi.order_id = ?
    """, (order_id,)).fetchall()
    con.close()
    lines = [f"📋 *Заказ №{o['id']}* [{o['status']}]\n",
             f"👤 {o['name']}  📱 {o['phone']}",
             f"🏠 {o['address']}",
             f"📅 {o['created_at']}\n"]
    for item in items:
        lines.append(f"• {item['name'][:40]}\n  {item['quantity']} кг × {item['price']:.0f} ₽")
    lines.append(f"\n💰 Итого: *{o['total']:.0f} ₽*")
    await callback.message.answer(
        "\n".join(lines), parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✅ Подтвердить", callback_data=f"adm_status_{order_id}_confirmed")],
            [InlineKeyboardButton(text="📦 Выполнен",   callback_data=f"adm_status_{order_id}_done")],
            [InlineKeyboardButton(text="❌ Отменить",   callback_data=f"adm_status_{order_id}_cancelled")],
            [InlineKeyboardButton(text="◀ К заказам",  callback_data="adm_orders")],
        ])
    )
    await callback.answer()

async def adm_set_status(callback: CallbackQuery, bot):
    rest = callback.data[len("adm_status_"):]
    order_id_str, new_status = rest.rsplit("_", 1)
    order_id = int(order_id_str)
    con = get_db()
    con.execute("UPDATE orders SET status = ? WHERE id = ?", (new_status, order_id))
    o = con.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()
    con.commit()
    con.close()
    status_text = {"confirmed": "✅ Подтверждён", "done": "📦 Выполнен",
                   "cancelled": "❌ Отменён"}.get(new_status, new_status)
    await callback.message.answer(f"Заказ №{order_id} → {status_text}", reply_markup=admin_keyboard())
    try:
        await bot.send_message(o["user_id"],
            f"📦 *Статус вашего заказа №{order_id}*\n\n{status_text}", parse_mode="Markdown")
    except Exception:
        pass
    await callback.answer()

# ─── Клиенты ──────────────────────────────────────────────────────────────────
async def adm_clients(callback: CallbackQuery):
    con = get_db()
    clients = con.execute("""
        SELECT u.name, u.phone, u.city,
               COUNT(o.id) as orders_count,
               COALESCE(SUM(o.total), 0) as total_spent
        FROM users u
        LEFT JOIN orders o ON u.user_id = o.user_id AND o.status != 'cancelled'
        GROUP BY u.user_id
        ORDER BY total_spent DESC
        LIMIT 30
    """).fetchall()
    con.close()
    if not clients:
        await callback.message.answer("👥 Клиентов пока нет.", reply_markup=admin_keyboard())
        await callback.answer()
        return
    lines = [f"👥 *Клиенты ({len(clients)}):*\n"]
    for c in clients:
        lines.append(
            f"👤 *{c['name']}* | 📱 {c['phone']}"
            + (f" | 🏠 {c['city']}" if c['city'] else "")
            + f"\n   Заказов: {c['orders_count']} | Сумма: {c['total_spent']:.0f} ₽"
        )
    await callback.message.answer("\n".join(lines), parse_mode="Markdown",
                                  reply_markup=admin_keyboard())
    await callback.answer()

# ─── Статистика ───────────────────────────────────────────────────────────────
async def adm_stats(callback: CallbackQuery):
    con = get_db()
    total_orders   = con.execute("SELECT COUNT(*) FROM orders").fetchone()[0]
    total_revenue  = con.execute(
        "SELECT COALESCE(SUM(total),0) FROM orders WHERE status != 'cancelled'"
    ).fetchone()[0]
    new_orders     = con.execute("SELECT COUNT(*) FROM orders WHERE status='new'").fetchone()[0]
    total_products = con.execute("SELECT COUNT(*) FROM products").fetchone()[0]
    total_clients  = con.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    low_stock      = con.execute(
        "SELECT COUNT(*) FROM products WHERE stock <= 5 AND stock > 0"
    ).fetchone()[0]
    out_of_stock   = con.execute("SELECT COUNT(*) FROM products WHERE stock = 0").fetchone()[0]
    top = con.execute("""
        SELECT p.name, SUM(oi.quantity) as sold
        FROM order_items oi JOIN products p ON oi.product_id = p.id
        GROUP BY p.id ORDER BY sold DESC LIMIT 5
    """).fetchall()
    con.close()
    lines = [
        "📊 *Статистика магазина*\n",
        f"👥 Клиентов: *{total_clients}*",
        f"📋 Заказов всего: *{total_orders}*  |  🆕 Новых: *{new_orders}*",
        f"💰 Выручка: *{total_revenue:.0f} ₽*\n",
        f"📦 Товаров в каталоге: *{total_products}*",
        f"⚠️ Заканчивается (≤5 кг): *{low_stock}*",
        f"❌ Нет в наличии: *{out_of_stock}*",
    ]
    if top:
        lines.append("\n🏆 *Топ продаж:*")
        for i, t in enumerate(top, 1):
            lines.append(f"  {i}. {t['name'][:40]} — {t['sold']} кг")
    await callback.message.answer("\n".join(lines), parse_mode="Markdown",
                                  reply_markup=admin_keyboard())
    await callback.answer()

# ─── Уведомление о новом заказе ──────────────────────────────────────────────
async def notify_new_order(bot, order_id: int, user_name: str, total: float):
    for admin_id in ADMIN_IDS:
        try:
            await bot.send_message(
                admin_id,
                f"🔔 *Новый заказ №{order_id}!*\n👤 {user_name}\n💰 {total:.0f} ₽\n\n/admin → Все заказы",
                parse_mode="Markdown"
            )
        except Exception:
            pass

# ─── Регистрация хендлеров ────────────────────────────────────────────────────
def register_admin_handlers(dp: Dispatcher, bot):
    dp.message.register(admin_cmd, Command("admin"))
    dp.callback_query.register(adm_back,   F.data == "adm_back")
    dp.callback_query.register(adm_stats,  F.data == "adm_stats")
    dp.callback_query.register(adm_clients, F.data == "adm_clients")
    dp.callback_query.register(adm_orders, F.data == "adm_orders")
    dp.callback_query.register(adm_order_detail,
        F.data.startswith("adm_order_") & ~F.data.startswith("adm_orders"))
    dp.callback_query.register(lambda c: adm_set_status(c, bot), F.data.startswith("adm_status_"))

    dp.callback_query.register(adm_upload_start, F.data == "adm_upload")
    dp.message.register(lambda m, s: adm_upload_file(m, s, bot), AdminStates.waiting_xlsx)

    dp.callback_query.register(adm_add_start, F.data == "adm_add_product")
    dp.message.register(adm_add_name,  AdminStates.waiting_new_name)
    dp.callback_query.register(adm_add_cat, F.data.startswith("adm_newcat_"))
    dp.message.register(adm_add_price, AdminStates.waiting_new_price)
    dp.message.register(adm_add_stock, AdminStates.waiting_new_stock)
    dp.message.register(adm_add_photo, AdminStates.waiting_new_photo)

    dp.callback_query.register(adm_edit_list,    F.data == "adm_edit_list")
    dp.callback_query.register(adm_edit_product, F.data.startswith("adm_edit_"))
    dp.callback_query.register(adm_chg_price_start, F.data == "adm_chg_price")
    dp.message.register(adm_chg_price, AdminStates.waiting_edit_price)
    dp.callback_query.register(adm_chg_stock_start, F.data == "adm_chg_stock")
    dp.message.register(adm_chg_stock, AdminStates.waiting_edit_stock)
    dp.callback_query.register(adm_chg_photo_start, F.data == "adm_chg_photo")
    dp.message.register(adm_chg_photo, AdminStates.waiting_edit_photo)

    dp.callback_query.register(adm_delete_list,    F.data == "adm_delete_list")
    dp.callback_query.register(adm_delete_product, F.data.startswith("adm_del_"))
