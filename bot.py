from aiohttp import web
import os
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler

class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b'OK')

    def do_HEAD(self):
        self.send_response(200)
        self.end_headers()

def run_server():
    port = int(os.environ.get("PORT", 10000))
    server = HTTPServer(("0.0.0.0", port), Handler)
    server.serve_forever()

threading.Thread(target=run_server, daemon=True).start()

import asyncio
import sqlite3
from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart
from aiogram.types import (
    Message, ReplyKeyboardMarkup, KeyboardButton,
    InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery
)
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage

from admin import register_admin_handlers, notify_new_order

BOT_TOKEN = os.environ.get("BOT_TOKEN")
bot = Bot(token=BOT_TOKEN)
storage = MemoryStorage()
dp = Dispatcher(storage=storage)

# ─── FSM ────────────────────────────────────────────────────────────────────
class RegStates(StatesGroup):
    choosing_type       = State()   # физ или юр
    # Физ лицо
    waiting_name        = State()
    waiting_phone       = State()
    # Юр лицо
    waiting_company     = State()
    waiting_inn         = State()
    waiting_legal_addr  = State()
    waiting_actual_addr = State()
    waiting_ur_phone    = State()
    waiting_email       = State()

class OrderStates(StatesGroup):
    confirm_profile  = State()
    waiting_name     = State()
    waiting_phone    = State()
    waiting_address  = State()

# ─── БД ─────────────────────────────────────────────────────────────────────
def init_db():
    con = sqlite3.connect("shop.db")
    cur = con.cursor()
    cur.executescript("""
        CREATE TABLE IF NOT EXISTS categories (
            id        INTEGER PRIMARY KEY AUTOINCREMENT,
            name      TEXT NOT NULL,
            parent_id INTEGER REFERENCES categories(id)
        );

        CREATE TABLE IF NOT EXISTS products (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            name        TEXT    NOT NULL,
            description TEXT,
            price       REAL    NOT NULL DEFAULT 0,
            stock       INTEGER NOT NULL DEFAULT 0,
            category_id INTEGER REFERENCES categories(id),
            photo_url   TEXT
        );

        CREATE TABLE IF NOT EXISTS users (
            user_id        INTEGER PRIMARY KEY,
            tg_name        TEXT,
            user_type      TEXT DEFAULT 'individual',
            name           TEXT,
            phone          TEXT,
            city           TEXT,
            address        TEXT,
            company_name   TEXT,
            inn            TEXT,
            legal_address  TEXT,
            actual_address TEXT,
            email          TEXT,
            created_at     DATETIME DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS cart (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id    INTEGER NOT NULL,
            product_id INTEGER NOT NULL,
            quantity   INTEGER NOT NULL DEFAULT 1,
            FOREIGN KEY (product_id) REFERENCES products(id)
        );

        CREATE TABLE IF NOT EXISTS orders (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id    INTEGER NOT NULL,
            name       TEXT,
            phone      TEXT,
            address    TEXT,
            total      REAL,
            status     TEXT DEFAULT 'new',
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS order_items (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            order_id   INTEGER NOT NULL,
            product_id INTEGER NOT NULL,
            quantity   INTEGER NOT NULL,
            price      REAL    NOT NULL,
            FOREIGN KEY (order_id)   REFERENCES orders(id),
            FOREIGN KEY (product_id) REFERENCES products(id)
        );
    """)

    # Миграция: добавляем новые колонки если их нет
    existing_columns = [row[1] for row in cur.execute("PRAGMA table_info(users)").fetchall()]
    new_columns = {
        "user_type":      "TEXT DEFAULT 'individual'",
        "company_name":   "TEXT",
        "inn":            "TEXT",
        "legal_address":  "TEXT",
        "actual_address": "TEXT",
        "email":          "TEXT",
    }
    for col_name, col_type in new_columns.items():
        if col_name not in existing_columns:
            cur.execute(f"ALTER TABLE users ADD COLUMN {col_name} {col_type}")

    cur.execute("SELECT COUNT(*) FROM categories")
    if cur.fetchone()[0] == 0:
        cur.execute("INSERT INTO categories (name, parent_id) VALUES ('ESPRESSO RBR', NULL)")
        esp_id = cur.lastrowid
        cur.execute("INSERT INTO categories (name, parent_id) VALUES ('FILTER RBR', NULL)")
        fil_id = cur.lastrowid
        cur.execute("INSERT INTO categories (name, parent_id) VALUES ('Микролоты', ?)", (esp_id,))
        esp_micro = cur.lastrowid
        cur.execute("INSERT INTO categories (name, parent_id) VALUES ('Моносорта', ?)", (esp_id,))
        esp_mono = cur.lastrowid
        cur.execute("INSERT INTO categories (name, parent_id) VALUES ('Микролоты', ?)", (fil_id,))
        fil_micro = cur.lastrowid
        cur.execute("INSERT INTO categories (name, parent_id) VALUES ('Моносорта', ?)", (fil_id,))
        fil_mono = cur.lastrowid

        for cat_id, products in [
            (esp_micro, [("Руанда Намяшике Хиллс натур. эспрессо - 1 кг", 5),
                         ("Эфиопия Челчеле гр.1 мытый эспрессо - 1 кг", 4)]),
            (esp_mono,  [("Бразилия СЕРРАДО Дарк темная обжарка эспрессо - 1 кг", 100),
                         ("Бразилия СЕРРАДО средняя обжарка эспрессо - 1 кг", 521),
                         ("Гватемала Уетенанго мытый - 1 кг эспрессо", 21),
                         ("Гондурас Сан Николас темная обжарка - 1 кг", 5),
                         ("Кения АБ мытый - 1 кг эспрессо", 3),
                         ("Китай Симао гр.1 Меллоу мытый эспрессо 1 кг", 10),
                         ("Колумбия Андино мытый эспрессо - 1 кг", 16),
                         ("Колумбия Супремо эспрессо - 1 кг", 5),
                         ("Перу SHG мытый эспрессо - 1 кг", 15),
                         ("Перу Монте Верде гр.1 мытый эспрессо - 1 кг", 10),
                         ("Перу Монте Верде гр.1 сухой эспрессо - 1 кг", 3),
                         ("Уганда Вугар мытый - эспрессо 1 кг", 10),
                         ("Уганда Рувензори сухой - эспрессо 1 кг", 21),
                         ("Эфиопия Yirgacheffe Gr 2 мытый эспрессо - 1 кг", 30),
                         ("Эфиопия Yirgacheffe Gr 4 сухой эспрессо - 1 кг", 136),
                         ("Эфиопия Сидамо Гр.2 мытый эспрессо - 1 кг", 3),
                         ("Эфиопия Сидамо Гр.4 сухой эспрессо - 1 кг", 12)]),
            (fil_micro, [("Колумбия Гонзало Кармана Катура сухой - 1 кг FILTER", 6),
                         ("Эфиопия Ададо сухой - 1 кг", 4),
                         ("Эфиопия Арича гр.1 сухой - 1 кг FILTER", 8),
                         ("Эфиопия Белоя сухой - 1 кг FILTER", 1),
                         ("Эфиопия Челелекту гр.1 мытый FILTER - 1 кг", 4),
                         ("Эфиопия Челчеле гр.1 сухой FILTER - 1 кг", 3)]),
            (fil_mono,  [("Бразилия Серрадо Желтый Бурбон сухой - 1 кг FILTER", 15),
                         ("Бразилия Фазенда Сертао сухой МОЛОТЫЙ - 1 кг FILTER", 11),
                         ("Гватемала Уеуетенанго мытый - 1 кг FILTER", 11),
                         ("Кения АА мытый - 1 кг FILTER", 12),
                         ("Кения Центральная Провинция АБ мытый - 1 кг FILTER", 4),
                         ("Китай Симао гр.1 Меллоу мытый 1 кг - FILTER", 4),
                         ("Колумбия Андино мытый - 1 кг FILTER", 6),
                         ("Перу Монте Верде гр.1 сухой - 1 кг FILTER", 5),
                         ("Руанда Мутетели 15+ мытый - 1 кг FILTER", 4),
                         ("Танзания АА мытый - 1 кг FILTER", 4),
                         ("Эфиопия Yirgacheffe Gr 2 мытый - 1 кг FILTER", 12),
                         ("Эфиопия Лиму гр.2 мытый 1 кг - FILTER", 9)]),
        ]:
            for name, stock in products:
                cur.execute(
                    "INSERT INTO products (name, stock, category_id, price) VALUES (?,?,?,?)",
                    (name, stock, cat_id, 1200.0)
                )
    con.commit()
    con.close()

def get_db():
    con = sqlite3.connect("shop.db")
    con.row_factory = sqlite3.Row
    return con

def get_user(user_id: int):
    con = get_db()
    u = con.execute("SELECT * FROM users WHERE user_id = ?", (user_id,)).fetchone()
    con.close()
    return u

def save_user_individual(user_id: int, tg_name: str, name: str, phone: str):
    con = get_db()
    con.execute("""
        INSERT INTO users (user_id, tg_name, user_type, name, phone)
        VALUES (?,?,'individual',?,?)
        ON CONFLICT(user_id) DO UPDATE SET
            tg_name=excluded.tg_name, user_type='individual',
            name=excluded.name, phone=excluded.phone
    """, (user_id, tg_name, name, phone))
    con.commit()
    con.close()

def save_user_company(user_id: int, tg_name: str, company_name: str, inn: str,
                      legal_address: str, actual_address: str, phone: str, email: str):
    con = get_db()
    con.execute("""
        INSERT INTO users (user_id, tg_name, user_type, company_name, inn,
                           legal_address, actual_address, phone, email, name)
        VALUES (?,?,'company',?,?,?,?,?,?,?)
        ON CONFLICT(user_id) DO UPDATE SET
            tg_name=excluded.tg_name, user_type='company',
            company_name=excluded.company_name, inn=excluded.inn,
            legal_address=excluded.legal_address, actual_address=excluded.actual_address,
            phone=excluded.phone, email=excluded.email, name=excluded.company_name
    """, (user_id, tg_name, company_name, inn, legal_address, actual_address, phone, email, company_name))
    con.commit()
    con.close()

def update_user_address(user_id: int, address: str):
    con = get_db()
    con.execute("UPDATE users SET address = ? WHERE user_id = ?", (address, user_id))
    con.commit()
    con.close()

# ─── Клавиатуры ─────────────────────────────────────────────────────────────
main_keyboard = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="☕ Каталог"),   KeyboardButton(text="🛒 Корзина")],
        [KeyboardButton(text="📦 Остатки"),   KeyboardButton(text="📞 Контакты")],
        [KeyboardButton(text="👤 Мой профиль")],
    ],
    resize_keyboard=True
)

user_type_keyboard = InlineKeyboardMarkup(inline_keyboard=[
    [InlineKeyboardButton(text="👤 Физическое лицо", callback_data="reg_individual")],
    [InlineKeyboardButton(text="🏢 Юридическое лицо", callback_data="reg_company")],
])

def root_categories_keyboard():
    con = get_db()
    cats = con.execute("SELECT * FROM categories WHERE parent_id IS NULL").fetchall()
    con.close()
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=c["name"], callback_data=f"cat_{c['id']}")] for c in cats
    ])

def subcategories_keyboard(parent_id):
    con = get_db()
    cats = con.execute("SELECT * FROM categories WHERE parent_id = ?", (parent_id,)).fetchall()
    con.close()
    buttons = [[InlineKeyboardButton(text=c["name"], callback_data=f"subcat_{c['id']}")] for c in cats]
    buttons.append([InlineKeyboardButton(text="◀ Назад", callback_data="back_root")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def products_keyboard(category_id):
    con = get_db()
    products = con.execute(
        "SELECT * FROM products WHERE category_id = ? AND stock > 0 ORDER BY name", (category_id,)
    ).fetchall()
    parent = con.execute("SELECT parent_id FROM categories WHERE id = ?", (category_id,)).fetchone()
    con.close()
    buttons = [[InlineKeyboardButton(
        text=f"{p['name'][:45]} — {p['price']:.0f} ₽",
        callback_data=f"product_{p['id']}"
    )] for p in products]
    back_cb = f"cat_{parent['parent_id']}" if parent and parent["parent_id"] else "back_root"
    buttons.append([InlineKeyboardButton(text="◀ Назад", callback_data=back_cb)])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def product_keyboard(product_id, category_id):
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="➕ В корзину", callback_data=f"add_{product_id}"),
        InlineKeyboardButton(text="◀ Назад",      callback_data=f"subcat_{category_id}"),
    ]])

def cart_keyboard(items):
    buttons = [[InlineKeyboardButton(
        text=f"❌ {item['name'][:35]}", callback_data=f"remove_{item['cart_id']}"
    )] for item in items]
    buttons.append([InlineKeyboardButton(text="✅ Оформить заказ",  callback_data="checkout")])
    buttons.append([InlineKeyboardButton(text="🗑 Очистить корзину", callback_data="clear_cart")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

# ─── /start и регистрация ────────────────────────────────────────────────────
@dp.message(CommandStart())
async def start_handler(message: Message, state: FSMContext):
    user = get_user(message.from_user.id)
    if user:
        display_name = user['company_name'] if user['user_type'] == 'company' else user['name']
        await message.answer(
            f"👋 С возвращением, *{display_name}*! ☕",
            parse_mode="Markdown", reply_markup=main_keyboard
        )
    else:
        await message.answer(
            f"👋 Добро пожаловать в *RBR Coffee Shop*!\n\n"
            "Для начала выберите тип аккаунта:",
            parse_mode="Markdown",
            reply_markup=user_type_keyboard
        )
        await state.set_state(RegStates.choosing_type)

# --- Выбор типа ---
@dp.callback_query(F.data == "reg_individual", RegStates.choosing_type)
async def reg_choose_individual(callback: CallbackQuery, state: FSMContext):
    await state.update_data(user_type="individual")
    await callback.message.answer("👤 Введите ваше *имя*:", parse_mode="Markdown")
    await state.set_state(RegStates.waiting_name)
    await callback.answer()

@dp.callback_query(F.data == "reg_company", RegStates.choosing_type)
async def reg_choose_company(callback: CallbackQuery, state: FSMContext):
    await state.update_data(user_type="company")
    await callback.message.answer("🏢 Введите *наименование организации*:", parse_mode="Markdown")
    await state.set_state(RegStates.waiting_company)
    await callback.answer()

# --- Физ лицо ---
@dp.message(RegStates.waiting_name)
async def reg_name(message: Message, state: FSMContext):
    await state.update_data(name=message.text.strip())
    await message.answer("📱 Введите *номер телефона*:", parse_mode="Markdown")
    await state.set_state(RegStates.waiting_phone)

@dp.message(RegStates.waiting_phone)
async def reg_phone(message: Message, state: FSMContext):
    data = await state.get_data()

    # Если это регистрация физ лица
    if data.get("user_type") == "individual":
        save_user_individual(
            user_id=message.from_user.id,
            tg_name=message.from_user.username or "",
            name=data["name"],
            phone=message.text.strip()
        )
        await state.clear()
        await message.answer(
            f"✅ Отлично, *{data['name']}*! Регистрация завершена.\n\n"
            "Добро пожаловать в магазин ☕",
            parse_mode="Markdown", reply_markup=main_keyboard
        )
    # Если это регистрация юр лица (последний шаг — телефон)
    elif data.get("user_type") == "company":
        await state.update_data(phone=message.text.strip())
        await message.answer("📧 Введите *электронную почту*:", parse_mode="Markdown")
        await state.set_state(RegStates.waiting_email)

# --- Юр лицо ---
@dp.message(RegStates.waiting_company)
async def reg_company(message: Message, state: FSMContext):
    await state.update_data(company_name=message.text.strip())
    await message.answer("🔢 Введите *ИНН*:", parse_mode="Markdown")
    await state.set_state(RegStates.waiting_inn)

@dp.message(RegStates.waiting_inn)
async def reg_inn(message: Message, state: FSMContext):
    inn = message.text.strip()
    if not inn.isdigit() or len(inn) not in (10, 12):
        await message.answer("⚠️ ИНН должен содержать 10 или 12 цифр. Попробуйте ещё раз:")
        return
    await state.update_data(inn=inn)
    await message.answer("🏢 Введите *юридический адрес*:", parse_mode="Markdown")
    await state.set_state(RegStates.waiting_legal_addr)

@dp.message(RegStates.waiting_legal_addr)
async def reg_legal_addr(message: Message, state: FSMContext):
    await state.update_data(legal_address=message.text.strip())
    await message.answer("📍 Введите *фактический адрес*:", parse_mode="Markdown")
    await state.set_state(RegStates.waiting_actual_addr)

@dp.message(RegStates.waiting_actual_addr)
async def reg_actual_addr(message: Message, state: FSMContext):
    await state.update_data(actual_address=message.text.strip())
    await message.answer("📱 Введите *номер телефона*:", parse_mode="Markdown")
    await state.set_state(RegStates.waiting_ur_phone)

@dp.message(RegStates.waiting_ur_phone)
async def reg_ur_phone(message: Message, state: FSMContext):
    await state.update_data(phone=message.text.strip())
    await message.answer("📧 Введите *электронную почту*:", parse_mode="Markdown")
    await state.set_state(RegStates.waiting_email)

@dp.message(RegStates.waiting_email)
async def reg_email(message: Message, state: FSMContext):
    data = await state.get_data()
    save_user_company(
        user_id=message.from_user.id,
        tg_name=message.from_user.username or "",
        company_name=data["company_name"],
        inn=data["inn"],
        legal_address=data["legal_address"],
        actual_address=data["actual_address"],
        phone=data["phone"],
        email=message.text.strip()
    )
    await state.clear()
    await message.answer(
        f"✅ Организация *{data['company_name']}* зарегистрирована!\n\n"
        "Добро пожаловать в магазин ☕",
        parse_mode="Markdown", reply_markup=main_keyboard
    )

# ─── Профиль ─────────────────────────────────────────────────────────────────
@dp.message(F.text == "👤 Мой профиль")
async def profile_handler(message: Message):
    user = get_user(message.from_user.id)
    if not user:
        await message.answer("Сначала зарегистрируйся через /start")
        return
    con = get_db()
    orders_count = con.execute(
        "SELECT COUNT(*) FROM orders WHERE user_id = ?", (message.from_user.id,)
    ).fetchone()[0]
    total_spent = con.execute(
        "SELECT COALESCE(SUM(total),0) FROM orders WHERE user_id = ? AND status != 'cancelled'",
        (message.from_user.id,)
    ).fetchone()[0]
    con.close()

    if user["user_type"] == "company":
        text = (
            f"🏢 *Профиль организации*\n\n"
            f"🏢 Наименование: *{user['company_name']}*\n"
            f"🔢 ИНН: {user['inn']}\n"
            f"📍 Юр. адрес: {user['legal_address']}\n"
            f"📍 Факт. адрес: {user['actual_address']}\n"
            f"📱 Телефон: {user['phone']}\n"
            f"📧 Email: {user['email']}\n"
            f"🏠 Адрес доставки: {user['address'] or 'не указан'}\n\n"
            f"📋 Заказов: *{orders_count}*\n"
            f"💰 Потрачено: *{total_spent:.0f} ₽*"
        )
    else:
        text = (
            f"👤 *Мой профиль*\n\n"
            f"Имя: *{user['name']}*\n"
            f"📱 Телефон: {user['phone']}\n"
            f"🏠 Адрес доставки: {user['address'] or 'не указан'}\n\n"
            f"📋 Заказов: *{orders_count}*\n"
            f"💰 Потрачено: *{total_spent:.0f} ₽*"
        )

    await message.answer(
        text,
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✏️ Изменить данные", callback_data="edit_profile")]
        ])
    )

@dp.callback_query(F.data == "edit_profile")
async def edit_profile(callback: CallbackQuery, state: FSMContext):
    await callback.message.answer(
        "Выберите тип аккаунта:",
        reply_markup=user_type_keyboard
    )
    await state.set_state(RegStates.choosing_type)
    await callback.answer()

# ─── Контакты ────────────────────────────────────────────────────────────────
@dp.message(F.text == "📞 Контакты")
async def contacts_handler(message: Message):
    await message.answer(
        "📞 *Наши контакты*\n\n📱 +7 (999) 123-45-67\n📧 shop@rbrcoffee.com\n🕐 Пн–Пт 9:00–18:00",
        parse_mode="Markdown"
    )

# ─── Каталог ─────────────────────────────────────────────────────────────────
@dp.message(F.text == "☕ Каталог")
async def catalog_handler(message: Message):
    await message.answer("☕ Выберите тип обжарки:", reply_markup=root_categories_keyboard())

@dp.callback_query(F.data == "back_root")
async def back_root(callback: CallbackQuery):
    await callback.message.answer("☕ Выберите тип обжарки:", reply_markup=root_categories_keyboard())
    await callback.answer()

@dp.callback_query(F.data.startswith("cat_"))
async def category_handler(callback: CallbackQuery):
    cat_id = int(callback.data.split("_")[1])
    con = get_db()
    cat = con.execute("SELECT * FROM categories WHERE id = ?", (cat_id,)).fetchone()
    con.close()
    await callback.message.answer(
        f"📂 *{cat['name']}* — выберите подкатегорию:",
        parse_mode="Markdown", reply_markup=subcategories_keyboard(cat_id)
    )
    await callback.answer()

@dp.callback_query(F.data.startswith("subcat_"))
async def subcategory_handler(callback: CallbackQuery):
    subcat_id = int(callback.data.split("_")[1])
    con = get_db()
    subcat = con.execute("SELECT * FROM categories WHERE id = ?", (subcat_id,)).fetchone()
    count  = con.execute(
        "SELECT COUNT(*) FROM products WHERE category_id = ? AND stock > 0", (subcat_id,)
    ).fetchone()[0]
    con.close()
    if count == 0:
        await callback.answer("😔 В этой категории нет товаров в наличии.", show_alert=True)
        return
    await callback.message.answer(
        f"☕ *{subcat['name']}* — выберите товар:",
        parse_mode="Markdown", reply_markup=products_keyboard(subcat_id)
    )
    await callback.answer()

@dp.callback_query(F.data.startswith("product_"))
async def product_detail(callback: CallbackQuery):
    product_id = int(callback.data.split("_")[1])
    con = get_db()
    p = con.execute("SELECT * FROM products WHERE id = ?", (product_id,)).fetchone()
    con.close()
    if not p:
        await callback.answer("Товар не найден.")
        return
    emoji = "✅" if p["stock"] > 10 else ("⚠️" if p["stock"] > 0 else "❌")
    text = (
        f"☕ *{p['name']}*\n\n"
        f"💰 Цена: *{p['price']:.0f} ₽ / кг*\n"
        f"{emoji} В наличии: *{p['stock']} кг*"
    )
    kb = product_keyboard(product_id, p["category_id"])
    if p["photo_url"]:
        try:
            await callback.message.answer_photo(photo=p["photo_url"], caption=text,
                                                 parse_mode="Markdown", reply_markup=kb)
        except Exception:
            await callback.message.answer(text, parse_mode="Markdown", reply_markup=kb)
    else:
        await callback.message.answer(text, parse_mode="Markdown", reply_markup=kb)
    await callback.answer()

# ─── Корзина ─────────────────────────────────────────────────────────────────
@dp.callback_query(F.data.startswith("add_"))
async def add_to_cart(callback: CallbackQuery):
    product_id = int(callback.data.split("_")[1])
    user_id = callback.from_user.id
    con = get_db()
    existing = con.execute(
        "SELECT id FROM cart WHERE user_id = ? AND product_id = ?", (user_id, product_id)
    ).fetchone()
    if existing:
        con.execute("UPDATE cart SET quantity = quantity + 1 WHERE id = ?", (existing["id"],))
    else:
        con.execute("INSERT INTO cart (user_id, product_id, quantity) VALUES (?,?,1)",
                    (user_id, product_id))
    con.commit()
    con.close()
    await callback.answer("✅ Добавлено в корзину!")

@dp.message(F.text == "🛒 Корзина")
async def cart_handler(message: Message):
    await show_cart(message, message.from_user.id)

async def show_cart(message: Message, user_id: int):
    con = get_db()
    items = con.execute("""
        SELECT c.id as cart_id, p.name, p.price, c.quantity, (p.price * c.quantity) as subtotal
        FROM cart c JOIN products p ON c.product_id = p.id WHERE c.user_id = ?
    """, (user_id,)).fetchall()
    con.close()
    if not items:
        await message.answer("🛒 Ваша корзина пуста.")
        return
    lines = ["🛒 *Ваша корзина:*\n"]
    total = 0
    for item in items:
        lines.append(
            f"• {item['name'][:40]}\n"
            f"  {item['quantity']} кг × {item['price']:.0f} ₽ = {item['subtotal']:.0f} ₽"
        )
        total += item["subtotal"]
    lines.append(f"\n💰 *Итого: {total:.0f} ₽*")
    await message.answer("\n".join(lines), parse_mode="Markdown", reply_markup=cart_keyboard(items))

@dp.callback_query(F.data.startswith("remove_"))
async def remove_from_cart(callback: CallbackQuery):
    con = get_db()
    con.execute("DELETE FROM cart WHERE id = ?", (int(callback.data.split("_")[1]),))
    con.commit()
    con.close()
    await callback.answer("Товар удалён.")
    await show_cart(callback.message, callback.from_user.id)

@dp.callback_query(F.data == "clear_cart")
async def clear_cart(callback: CallbackQuery):
    con = get_db()
    con.execute("DELETE FROM cart WHERE user_id = ?", (callback.from_user.id,))
    con.commit()
    con.close()
    await callback.answer("Корзина очищена.")
    await callback.message.answer("🛒 Корзина очищена.")

# ─── Оформление заказа ───────────────────────────────────────────────────────
@dp.callback_query(F.data == "checkout")
async def checkout_start(callback: CallbackQuery, state: FSMContext):
    user = get_user(callback.from_user.id)
    if user and user["name"] and user["phone"]:
        display_name = user['company_name'] if user['user_type'] == 'company' else user['name']
        addr_hint = user["address"] or user.get("actual_address") or "не указан"
        await callback.message.answer(
            f"📋 *Использовать сохранённые данные?*\n\n"
            f"👤 {display_name}\n"
            f"📱 {user['phone']}\n"
            f"🏠 Адрес: {addr_hint}",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="✅ Да, использовать", callback_data="checkout_saved")],
                [InlineKeyboardButton(text="✏️ Ввести новые",    callback_data="checkout_new")],
            ])
        )
        await state.set_state(OrderStates.confirm_profile)
    else:
        await callback.message.answer("📝 Введите ваше *имя*:", parse_mode="Markdown")
        await state.set_state(OrderStates.waiting_name)
    await callback.answer()

@dp.callback_query(F.data == "checkout_saved", OrderStates.confirm_profile)
async def checkout_use_saved(callback: CallbackQuery, state: FSMContext):
    user = get_user(callback.from_user.id)
    display_name = user['company_name'] if user['user_type'] == 'company' else user['name']
    addr = user["address"] or user.get("actual_address")
    if addr:
        await state.update_data(name=display_name, phone=user["phone"], address=addr)
        await create_order(callback.message, callback.from_user.id, state)
    else:
        await state.update_data(name=display_name, phone=user["phone"])
        await callback.message.answer("🏠 Введите *адрес доставки*:", parse_mode="Markdown")
        await state.set_state(OrderStates.waiting_address)
    await callback.answer()

@dp.callback_query(F.data == "checkout_new", OrderStates.confirm_profile)
async def checkout_new_data(callback: CallbackQuery, state: FSMContext):
    await callback.message.answer("📝 Введите ваше *имя*:", parse_mode="Markdown")
    await state.set_state(OrderStates.waiting_name)
    await callback.answer()

@dp.message(OrderStates.waiting_name)
async def checkout_name(message: Message, state: FSMContext):
    await state.update_data(name=message.text.strip())
    await message.answer("📱 Введите *номер телефона*:", parse_mode="Markdown")
    await state.set_state(OrderStates.waiting_phone)

@dp.message(OrderStates.waiting_phone)
async def checkout_phone(message: Message, state: FSMContext):
    await state.update_data(phone=message.text.strip())
    await message.answer("🏠 Введите *адрес доставки*:", parse_mode="Markdown")
    await state.set_state(OrderStates.waiting_address)

@dp.message(OrderStates.waiting_address)
async def checkout_address_handler(message: Message, state: FSMContext):
    await state.update_data(address=message.text.strip())
    await create_order(message, message.from_user.id, state)

async def create_order(message: Message, user_id: int, state: FSMContext):
    data = await state.get_data()
    con = get_db()
    items = con.execute("""
        SELECT c.product_id, p.name, p.price, c.quantity, (p.price * c.quantity) as subtotal
        FROM cart c JOIN products p ON c.product_id = p.id WHERE c.user_id = ?
    """, (user_id,)).fetchall()

    if not items:
        await message.answer("Корзина пуста.")
        await state.clear()
        con.close()
        return

    total = sum(i["subtotal"] for i in items)
    cur = con.execute(
        "INSERT INTO orders (user_id, name, phone, address, total) VALUES (?,?,?,?,?)",
        (user_id, data["name"], data["phone"], data["address"], total)
    )
    order_id = cur.lastrowid

    for item in items:
        con.execute(
            "INSERT INTO order_items (order_id, product_id, quantity, price) VALUES (?,?,?,?)",
            (order_id, item["product_id"], item["quantity"], item["price"])
        )
        con.execute("UPDATE products SET stock = stock - ? WHERE id = ?",
                    (item["quantity"], item["product_id"]))

    con.execute("DELETE FROM cart WHERE user_id = ?", (user_id,))
    con.commit()
    con.close()

    update_user_address(user_id, data["address"])

    lines = [f"✅ *Заказ №{order_id} оформлен!*\n"]
    for item in items:
        lines.append(f"• {item['name'][:40]}\n  {item['quantity']} кг = {item['subtotal']:.0f} ₽")
    lines.append(f"\n💰 Итого: *{total:.0f} ₽*")
    lines.append(f"👤 {data['name']}  📱 {data['phone']}\n🏠 {data['address']}")
    lines.append("\n📦 Мы свяжемся с вами для подтверждения!")

    await message.answer("\n".join(lines), parse_mode="Markdown", reply_markup=main_keyboard)
    await state.clear()
    await notify_new_order(bot, order_id, data["name"], total)

# ─── Остатки ─────────────────────────────────────────────────────────────────
@dp.message(F.text == "📦 Остатки")
async def stock_handler(message: Message):
    con = get_db()
    root_cats = con.execute("SELECT * FROM categories WHERE parent_id IS NULL").fetchall()
    lines = ["📦 *Остатки по складу:*\n"]
    for root in root_cats:
        total_root = con.execute("""
            SELECT COALESCE(SUM(p.stock), 0) FROM products p
            JOIN categories c ON p.category_id = c.id WHERE c.parent_id = ?
        """, (root["id"],)).fetchone()[0]
        lines.append(f"*{root['name']}* — итого {total_root} кг")
        for sub in con.execute(
            "SELECT * FROM categories WHERE parent_id = ?", (root["id"],)
        ).fetchall():
            lines.append(f"\n  _{sub['name']}_")
            for p in con.execute(
                "SELECT name, stock FROM products WHERE category_id = ? ORDER BY name", (sub["id"],)
            ).fetchall():
                e = "✅" if p["stock"] > 10 else ("⚠️" if p["stock"] > 0 else "❌")
                lines.append(f"  {e} {p['name'][:45]}: *{p['stock']} кг*")
        lines.append("")
    con.close()
    text = "\n".join(lines)
    for i in range(0, len(text), 4000):
        await message.answer(text[i:i+4000], parse_mode="Markdown")

# ─── Запуск ──────────────────────────────────────────────────────────────────
async def main():
    init_db()
    register_admin_handlers(dp, bot)
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
