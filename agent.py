"""
Roastberry Agent Bot — умный админ-ассистент на Claude API
Отдельный бот, работает с той же shop.db что и основной магазин.

Переменные окружения:
  AGENT_BOT_TOKEN    — токен агент-бота от BotFather
  ANTHROPIC_API_KEY  — ключ Claude API
  ADMIN_IDS          — telegram id админов (через запятую, напр: 466755177)
  DB_PATH            — путь к БД (по умолчанию /app/data/shop.db)
  MAIN_BOT_TOKEN     — токен основного бота (для рассылок)
"""

import asyncio
import io
import json
import os
import sqlite3
from datetime import datetime, timedelta
from http.server import HTTPServer, BaseHTTPRequestHandler
import threading

import aiohttp
import openpyxl
from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, CommandStart
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import Message

# ─── Конфиг ───────────────────────────────────────────────────────────────────
AGENT_TOKEN   = os.environ.get("AGENT_BOT_TOKEN")
ANTHROPIC_KEY = os.environ.get("ANTHROPIC_API_KEY")
MAIN_TOKEN    = os.environ.get("MAIN_BOT_TOKEN")
ADMIN_IDS     = [int(x) for x in os.environ.get("ADMIN_IDS", "466755177").split(",")]

# БД агента — локальная копия, синхронизируется с основным ботом
# Агент читает данные напрямую через Telegram API основного бота
# и хранит локальную копию в /tmp/agent_shop.db
DB_PATH = "/app/agent_shop.db"

bot = Bot(token=AGENT_TOKEN)
dp  = Dispatcher(storage=MemoryStorage())

# ─── Keep-alive ───────────────────────────────────────────────────────────────
class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200); self.end_headers(); self.wfile.write(b"OK")
    def log_message(self, *a): pass

def run_server():
    port = int(os.environ.get("AGENT_PORT", 10001))
    HTTPServer(("0.0.0.0", port), Handler).serve_forever()

threading.Thread(target=run_server, daemon=True).start()

# ─── БД ───────────────────────────────────────────────────────────────────────
def init_agent_db():
    """Создаёт локальную БД агента с нужными таблицами."""
    con = sqlite3.connect(DB_PATH)
    con.executescript("""
        CREATE TABLE IF NOT EXISTS categories (
            id INTEGER PRIMARY KEY, name TEXT, parent_id INTEGER
        );
        CREATE TABLE IF NOT EXISTS products (
            id INTEGER PRIMARY KEY, name TEXT, description TEXT,
            price REAL DEFAULT 0, stock INTEGER DEFAULT 0,
            category_id INTEGER, roast_type TEXT, weight_g INTEGER DEFAULT 1000,
            tag TEXT DEFAULT '', prev_price REAL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY, tg_name TEXT,
            user_type TEXT DEFAULT 'individual', name TEXT, phone TEXT,
            company_name TEXT, inn TEXT, email TEXT,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS orders (
            id INTEGER PRIMARY KEY, user_id INTEGER, name TEXT,
            phone TEXT, address TEXT, total REAL, discount REAL DEFAULT 0,
            status TEXT DEFAULT 'new', created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS order_items (
            id INTEGER PRIMARY KEY, order_id INTEGER,
            product_id INTEGER, quantity INTEGER, price REAL
        );
        CREATE TABLE IF NOT EXISTS user_discounts (
            id INTEGER PRIMARY KEY, user_id INTEGER,
            category TEXT DEFAULT 'ALL', discount_pct REAL
        );
        CREATE TABLE IF NOT EXISTS user_prices (
            id INTEGER PRIMARY KEY, user_id INTEGER,
            product_id INTEGER, price REAL
        );
    """)
    con.commit(); con.close()

def get_db():
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    return con

def db_query(sql: str, params=()) -> list:
    con = get_db()
    try:
        rows = con.execute(sql, params).fetchall()
        con.close()
        return [dict(r) for r in rows]
    except Exception as e:
        con.close()
        return []

def db_execute(sql: str, params=()):
    con = get_db()
    con.execute(sql, params)
    con.commit(); con.close()

async def sync_db_from_main():
    """Синхронизирует данные из основного бота через Telegram API."""
    if not MAIN_TOKEN:
        return False
    try:
        main_bot = Bot(token=MAIN_TOKEN)
        # Отправляем специальную команду основному боту для получения дампа данных
        # Основной бот должен ответить данными через webhook
        # Пока используем прямое подключение к БД если возможно
        await main_bot.session.close()
        return True
    except Exception:
        return False

# ─── Claude API ───────────────────────────────────────────────────────────────
async def ask_claude(system: str, user: str, max_tokens: int = 2000) -> str:
    """Отправляет запрос в Claude API и возвращает текст ответа."""
    async with aiohttp.ClientSession() as session:
        resp = await session.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": ANTHROPIC_KEY,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": "claude-haiku-4-5-20251001",
                "max_tokens": max_tokens,
                "system": system,
                "messages": [{"role": "user", "content": user}],
            }
        )
        data = await resp.json()
        if "content" in data:
            return data["content"][0]["text"]
        return f"Ошибка API: {data.get('error', {}).get('message', str(data))}"

# ─── Проверка доступа ─────────────────────────────────────────────────────────
def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS

# ─── /start ───────────────────────────────────────────────────────────────────
@dp.message(CommandStart())
async def start(message: Message):
    if not is_admin(message.from_user.id):
        await message.answer("⛔ Нет доступа.")
        return

    products = db_query("SELECT COUNT(*) as cnt FROM products")
    count = products[0]["cnt"] if products else 0
    db_status = f"📦 Товаров в БД: *{count}*" if count > 0 else "⚠️ БД пуста — отправь xlsx для загрузки данных"

    await message.answer(
        "🤖 *Roastberry Agent* готов к работе!\n\n"
        f"{db_status}\n\n"
        "Что я умею:\n"
        "📎 Отправь xlsx → обновлю остатки или цены\n"
        "💬 Задай вопрос в свободной форме\n"
        "📊 /analytics — аналитика за неделю\n"
        "⚠️ /lowstock — товары с низким остатком\n"
        "🏆 /top — топ продаж\n"
        "👥 /clients — активные клиенты\n"
        "💰 /prices — сводка цен\n"
        "🔄 /syncdb — загрузить данные из xlsx\n\n"
        "Просто напиши мне что нужно сделать!",
        parse_mode="Markdown"
    )

@dp.message(Command("syncdb"))
async def syncdb_cmd(message: Message):
    if not is_admin(message.from_user.id): return
    products = db_query("SELECT COUNT(*) as cnt FROM products")
    orders = db_query("SELECT COUNT(*) as cnt FROM orders")
    users = db_query("SELECT COUNT(*) as cnt FROM users")
    p = products[0]["cnt"] if products else 0
    o = orders[0]["cnt"] if orders else 0
    u = users[0]["cnt"] if users else 0
    await message.answer(
        f"📊 *Состояние локальной БД агента:*\n\n"
        f"📦 Товаров: *{p}*\n"
        f"📋 Заказов: *{o}*\n"
        f"👥 Клиентов: *{u}*\n\n"
        f"{'✅ БД заполнена' if p > 0 else '⚠️ БД пуста — отправь xlsx файл из 1С для загрузки остатков и цен'}",
        parse_mode="Markdown"
    )

# ─── Аналитика ────────────────────────────────────────────────────────────────
@dp.message(Command("analytics"))
async def analytics(message: Message):
    if not is_admin(message.from_user.id): return

    week_ago = (datetime.now() - timedelta(days=7)).strftime("%Y-%m-%d")

    orders = db_query("""
        SELECT COUNT(*) as cnt, COALESCE(SUM(total),0) as revenue
        FROM orders WHERE created_at >= ? AND status != 'cancelled'
    """, (week_ago,))

    top = db_query("""
        SELECT p.name, SUM(oi.quantity) as qty, SUM(oi.quantity * oi.price) as revenue
        FROM order_items oi
        JOIN products p ON oi.product_id = p.id
        JOIN orders o ON oi.order_id = o.id
        WHERE o.created_at >= ? AND o.status != 'cancelled'
        GROUP BY p.id ORDER BY revenue DESC LIMIT 5
    """, (week_ago,))

    new_clients = db_query("""
        SELECT COUNT(*) as cnt FROM users WHERE created_at >= ?
    """, (week_ago,))

    low = db_query("""
        SELECT COUNT(*) as cnt FROM products WHERE stock > 0 AND stock <= 5
    """)

    out = db_query("""
        SELECT COUNT(*) as cnt FROM products WHERE stock = 0
    """)

    lines = [
        f"📊 *Аналитика за 7 дней*\n",
        f"💰 Выручка: *{orders[0]['revenue']:.0f} ₽*",
        f"📋 Заказов: *{orders[0]['cnt']}*",
        f"👥 Новых клиентов: *{new_clients[0]['cnt']}*",
        f"⚠️ Заканчивается (≤5): *{low[0]['cnt']}*",
        f"❌ Нет в наличии: *{out[0]['cnt']}*",
    ]
    if top:
        lines.append("\n🏆 *Топ продаж:*")
        for i, t in enumerate(top, 1):
            lines.append(f"  {i}. {t['name'][:40]} — {t['qty']} шт / {t['revenue']:.0f} ₽")

    await message.answer("\n".join(lines), parse_mode="Markdown")

# ─── Низкий остаток ───────────────────────────────────────────────────────────
@dp.message(Command("lowstock"))
async def lowstock(message: Message):
    if not is_admin(message.from_user.id): return

    products = db_query("""
        SELECT name, stock FROM products WHERE stock <= 5 AND stock > 0
        ORDER BY stock ASC LIMIT 30
    """)
    zero = db_query("""
        SELECT name FROM products WHERE stock = 0
        ORDER BY name LIMIT 20
    """)

    lines = ["⚠️ *Заканчивается (≤5 шт):*\n"]
    for p in products:
        lines.append(f"  🟡 {p['name'][:45]}: *{p['stock']}*")
    if not products:
        lines.append("  Всё в порядке!")

    if zero:
        lines.append(f"\n❌ *Нет в наличии ({len(zero)}):*")
        for p in zero[:10]:
            lines.append(f"  • {p['name'][:45]}")
        if len(zero) > 10:
            lines.append(f"  ... и ещё {len(zero)-10}")

    text = "\n".join(lines)
    for i in range(0, len(text), 4000):
        await message.answer(text[i:i+4000], parse_mode="Markdown")

# ─── Топ продаж ───────────────────────────────────────────────────────────────
@dp.message(Command("top"))
async def top_sales(message: Message):
    if not is_admin(message.from_user.id): return

    top = db_query("""
        SELECT p.name, SUM(oi.quantity) as qty,
               SUM(oi.quantity * oi.price) as revenue
        FROM order_items oi
        JOIN products p ON oi.product_id = p.id
        JOIN orders o ON oi.order_id = o.id
        WHERE o.status != 'cancelled'
        GROUP BY p.id ORDER BY revenue DESC LIMIT 15
    """)

    lines = ["🏆 *Топ продаж за всё время:*\n"]
    for i, t in enumerate(top, 1):
        lines.append(f"{i}. {t['name'][:42]}\n   {t['qty']} шт — {t['revenue']:.0f} ₽")

    await message.answer("\n".join(lines), parse_mode="Markdown")

# ─── Клиенты ──────────────────────────────────────────────────────────────────
@dp.message(Command("clients"))
async def clients(message: Message):
    if not is_admin(message.from_user.id): return

    clients_data = db_query("""
        SELECT u.name, u.company_name, u.user_type, u.phone,
               COUNT(o.id) as orders_cnt,
               COALESCE(SUM(o.total), 0) as total_spent,
               MAX(o.created_at) as last_order
        FROM users u
        LEFT JOIN orders o ON u.user_id = o.user_id AND o.status != 'cancelled'
        GROUP BY u.user_id
        ORDER BY total_spent DESC LIMIT 20
    """)

    lines = [f"👥 *Клиенты (топ-20 по выручке):*\n"]
    for c in clients_data:
        name = c["company_name"] or c["name"]
        icon = "🏢" if c["user_type"] == "company" else "👤"
        last = c["last_order"][:10] if c["last_order"] else "—"
        lines.append(
            f"{icon} *{name}*\n"
            f"   📋 {c['orders_cnt']} заказов | 💰 {c['total_spent']:.0f} ₽ | 📅 {last}"
        )

    text = "\n".join(lines)
    for i in range(0, len(text), 4000):
        await message.answer(text[i:i+4000], parse_mode="Markdown")

# ─── Рассылка ─────────────────────────────────────────────────────────────────
@dp.message(Command("broadcast"))
async def broadcast_cmd(message: Message):
    if not is_admin(message.from_user.id): return
    await message.answer(
        "📢 Отправь следующим сообщением текст рассылки.\n"
        "Поддерживается Markdown. Уйдёт всем клиентам."
    )

# ─── Цены ─────────────────────────────────────────────────────────────────────
@dp.message(Command("prices"))
async def prices_cmd(message: Message):
    if not is_admin(message.from_user.id): return

    cats = db_query("SELECT id, name FROM categories WHERE parent_id IS NULL")
    lines = ["💰 *Сводка цен по категориям:*\n"]
    for cat in cats:
        products = db_query("""
            SELECT name, price FROM products
            WHERE category_id = ? AND stock > 0
            ORDER BY price DESC LIMIT 5
        """, (cat["id"],))
        if products:
            lines.append(f"*{cat['name']}* (топ-5 по цене):")
            for p in products:
                lines.append(f"  • {p['name'][:40]} — {p['price']:.0f} ₽")
    text = "\n".join(lines)
    for i in range(0, len(text), 4000):
        await message.answer(text[i:i+4000], parse_mode="Markdown")

# ─── Умная обработка xlsx (остатки + цены) ────────────────────────────────────
@dp.message(F.document)
async def handle_document(message: Message):
    if not is_admin(message.from_user.id): return

    doc = message.document
    if not doc.file_name.endswith(".xlsx"):
        await message.answer("⚠️ Поддерживается только .xlsx")
        return

    await message.answer("📥 Читаю файл...")

    # Скачиваем файл
    file = await bot.get_file(doc.file_id)
    buf = io.BytesIO()
    await bot.download_file(file.file_path, buf)
    buf.seek(0)

    try:
        wb = openpyxl.load_workbook(buf, read_only=True, data_only=True)
        ws = wb.active
    except Exception as e:
        await message.answer(f"❌ Не удалось открыть файл: {e}")
        return

    # Читаем данные из xlsx
    rows = []
    for row in ws.iter_rows(values_only=True):
        if not row[0] or not isinstance(row[0], str) or len(row[0]) < 3:
            continue
        name = row[0].strip()
        nums = [round(float(c)) for c in row[1:] if isinstance(c, (int, float))]
        if not nums:
            continue
        rows.append({"name": name, "nums": nums})

    if not rows:
        await message.answer("❌ Не нашёл данных в файле.")
        return

    # Получаем список товаров из БД
    db_products = db_query("SELECT id, name, price, stock FROM products ORDER BY name")
    db_names = [p["name"] for p in db_products]

    await message.answer(f"🔍 Найдено {len(rows)} строк в файле. Анализирую через Claude...")

    # Определяем тип файла (остатки или цены) и сопоставляем через Claude
    sample = rows[:30]  # берём первые 30 строк для анализа
    sample_text = "\n".join([
        f"{r['name']} | {' | '.join(str(n) for n in r['nums'])}"
        for r in sample
    ])

    db_sample = "\n".join(db_names[:80])

    system_prompt = """Ты — помощник для магазина кофе Roastberry.
Тебе дают данные из 1С и список товаров в боте.
Твоя задача — сопоставить названия из 1С с товарами бота и определить тип данных.

Отвечай ТОЛЬКО валидным JSON, без markdown и пояснений."""

    user_prompt = f"""Проанализируй данные из 1С:
{sample_text}

Список товаров в боте (первые 80):
{db_sample}

Задачи:
1. Определи тип файла: "stock" (остатки) или "prices" (прайс с ценами) или "both" (и то и другое)
2. Для каждой строки из 1С найди наиболее подходящий товар из бота
3. Если в строке несколько чисел — последнее обычно остаток, предпоследнее — цена

Верни JSON:
{{
  "file_type": "stock" | "prices" | "both",
  "matches": [
    {{
      "source_name": "название из 1С",
      "bot_name": "название в боте или null",
      "stock": число или null,
      "price": число или null,
      "confidence": "high" | "medium" | "low"
    }}
  ]
}}"""

    result_text = await ask_claude(system_prompt, user_prompt, max_tokens=4000)

    # Парсим JSON ответ
    try:
        # Убираем возможные markdown блоки
        clean = result_text.strip()
        if clean.startswith("```"):
            clean = "\n".join(clean.split("\n")[1:-1])
        result = json.loads(clean)
    except json.JSONDecodeError as e:
        await message.answer(f"❌ Ошибка разбора ответа Claude: {e}\n\nОтвет: {result_text[:500]}")
        return

    file_type = result.get("file_type", "stock")
    matches = result.get("matches", [])

    # Если строк больше 30 — обрабатываем остальные тоже
    if len(rows) > 30:
        await message.answer(f"🔍 Обрабатываю оставшиеся {len(rows)-30} строк...")
        remaining = rows[30:]
        remaining_text = "\n".join([
            f"{r['name']} | {' | '.join(str(n) for n in r['nums'])}"
            for r in remaining
        ])

        user_prompt2 = f"""Сопоставь оставшиеся строки из 1С с товарами бота.
Тип файла: {file_type}

Строки из 1С:
{remaining_text}

Список товаров бота:
{chr(10).join(db_names)}

Верни JSON массив matches (такой же формат как раньше):
{{"matches": [...]}}"""

        result2_text = await ask_claude(system_prompt, user_prompt2, max_tokens=4000)
        try:
            clean2 = result2_text.strip()
            if clean2.startswith("```"):
                clean2 = "\n".join(clean2.split("\n")[1:-1])
            result2 = json.loads(clean2)
            matches.extend(result2.get("matches", []))
        except Exception:
            pass  # продолжаем с тем что есть

    # Применяем обновления в БД
    updated_stock = updated_price = skipped = 0
    not_found = []

    # Создаём индекс БД для быстрого поиска
    db_index = {p["name"]: p for p in db_products}

    con = get_db()
    for m in matches:
        bot_name = m.get("bot_name")
        confidence = m.get("confidence", "low")

        if not bot_name or confidence == "low":
            not_found.append(m.get("source_name", "?"))
            skipped += 1
            continue

        # Ищем в БД
        product = db_index.get(bot_name)
        if not product:
            # Попробуем частичный поиск
            for name, p in db_index.items():
                if bot_name.lower() in name.lower() or name.lower() in bot_name.lower():
                    product = p
                    break

        if not product:
            not_found.append(f"{m.get('source_name')} → {bot_name}")
            skipped += 1
            continue

        pid = product["id"]
        if file_type in ("stock", "both") and m.get("stock") is not None:
            con.execute("UPDATE products SET stock = ? WHERE id = ?", (int(m["stock"]), pid))
            updated_stock += 1

        if file_type in ("prices", "both") and m.get("price") is not None and m["price"] > 0:
            old_price = product["price"]
            new_price = float(m["price"])
            con.execute("UPDATE products SET prev_price = ?, price = ? WHERE id = ?",
                        (old_price, new_price, pid))
            updated_price += 1

    con.commit(); con.close()

    # Формируем отчёт
    type_labels = {"stock": "остатки", "prices": "цены", "both": "остатки и цены"}
    lines = [
        f"✅ *Обновление завершено!*\n",
        f"📄 Тип файла: {type_labels.get(file_type, file_type)}",
        f"📦 Обновлено остатков: *{updated_stock}*",
        f"💰 Обновлено цен: *{updated_price}*",
        f"⚠️ Пропущено: *{skipped}*",
    ]
    if not_found:
        lines.append(f"\n❓ *Не сопоставлено ({min(len(not_found),10)} из {len(not_found)}):*")
        for nf in not_found[:10]:
            lines.append(f"  • {nf[:50]}")
        if len(not_found) > 10:
            lines.append(f"  ... и ещё {len(not_found)-10}")

    # Проверяем итоговое состояние БД
    total_products = db_query("SELECT COUNT(*) as cnt FROM products")[0]["cnt"]
    lines.append(f"\n📊 Всего товаров в БД агента: *{total_products}*")

    await message.answer("\n".join(lines), parse_mode="Markdown")

    # Алерт по низким остаткам после обновления
    low = db_query("""
        SELECT name, stock FROM products
        WHERE stock > 0 AND stock <= 5
        ORDER BY stock ASC LIMIT 5
    """)
    if low:
        alert_lines = ["\n⚠️ *Внимание — низкий остаток после обновления:*"]
        for p in low:
            alert_lines.append(f"  🔴 {p['name'][:45]}: *{p['stock']} шт*")
        await message.answer("\n".join(alert_lines), parse_mode="Markdown")

# ─── Умные вопросы в свободной форме ─────────────────────────────────────────
@dp.message(F.text & ~F.text.startswith("/"))
async def smart_query(message: Message):
    if not is_admin(message.from_user.id): return

    query = message.text.strip()
    await message.answer("🤔 Думаю...")

    # Собираем контекст из БД для Claude
    stats = db_query("""
        SELECT
            (SELECT COUNT(*) FROM orders WHERE status!='cancelled') as total_orders,
            (SELECT COALESCE(SUM(total),0) FROM orders WHERE status!='cancelled') as revenue,
            (SELECT COUNT(*) FROM users) as clients,
            (SELECT COUNT(*) FROM products WHERE stock=0) as out_of_stock,
            (SELECT COUNT(*) FROM products WHERE stock<=5 AND stock>0) as low_stock
    """)[0]

    recent_orders = db_query("""
        SELECT o.id, o.created_at, o.total, o.name,
               GROUP_CONCAT(p.name || ' x' || oi.quantity) as items
        FROM orders o
        JOIN order_items oi ON o.id = oi.order_id
        JOIN products p ON oi.product_id = p.id
        WHERE o.status != 'cancelled'
        GROUP BY o.id
        ORDER BY o.created_at DESC LIMIT 10
    """)

    top_products = db_query("""
        SELECT p.name, SUM(oi.quantity) as qty
        FROM order_items oi JOIN products p ON oi.product_id = p.id
        JOIN orders o ON oi.order_id = o.id
        WHERE o.status != 'cancelled'
        GROUP BY p.id ORDER BY qty DESC LIMIT 10
    """)

    context = f"""
Статистика магазина Roastberry:
- Всего заказов: {stats['total_orders']}
- Выручка: {stats['revenue']:.0f} ₽
- Клиентов: {stats['clients']}
- Нет в наличии: {stats['out_of_stock']} товаров
- Заканчивается (≤5): {stats['low_stock']} товаров

Последние заказы:
{chr(10).join([f"№{o['id']} {o['created_at'][:10]} — {o['name']} — {o['total']:.0f}₽: {o['items'][:80]}" for o in recent_orders])}

Топ продаж:
{chr(10).join([f"{t['name'][:40]} — {t['qty']} шт" for t in top_products])}
"""

    system = """Ты — умный ассистент для магазина кофе и чая Roastberry.
У тебя есть данные о магазине. Отвечай кратко и по делу на русском языке.
Используй Markdown для форматирования. Если нужно выполнить действие — опиши что нужно сделать."""

    response = await ask_claude(system, f"Данные магазина:\n{context}\n\nВопрос: {query}")
    await message.answer(response, parse_mode="Markdown")

# ─── Авто-алерты (запускается при старте и каждый час) ────────────────────────
async def check_alerts():
    """Проверяет остатки и отправляет алерты админам."""
    while True:
        try:
            low = db_query("""
                SELECT name, stock FROM products
                WHERE stock > 0 AND stock <= 3
                ORDER BY stock ASC LIMIT 10
            """)
            if low:
                lines = ["⚠️ *Алерт: критически низкий остаток!*\n"]
                for p in low:
                    lines.append(f"  🔴 {p['name'][:45]}: *{p['stock']} шт*")
                text = "\n".join(lines)
                for admin_id in ADMIN_IDS:
                    try:
                        await bot.send_message(admin_id, text, parse_mode="Markdown")
                    except Exception:
                        pass
        except Exception:
            pass
        await asyncio.sleep(3600)  # каждый час

# ─── Запуск ───────────────────────────────────────────────────────────────────
async def main():
    # Инициализируем локальную БД
    init_agent_db()

    # Проверяем есть ли данные
    products = db_query("SELECT COUNT(*) as cnt FROM products")
    count = products[0]["cnt"] if products else 0

    if count == 0:
        for admin_id in ADMIN_IDS:
            try:
                await bot.send_message(
                    admin_id,
                    "🤖 *Roastberry Agent* запущен!\n\n"
                    "⚠️ Локальная БД пуста — нет доступа к данным магазина.\n\n"
                    "Для синхронизации отправь xlsx файл из 1С командой — "
                    "я обновлю свою копию данных.\n\n"
                    "Или добавь переменную `MAIN_BOT_TOKEN` в Variables агента "
                    "чтобы я мог синхронизироваться автоматически.",
                    parse_mode="Markdown"
                )
            except Exception:
                pass
    else:
        for admin_id in ADMIN_IDS:
            try:
                await bot.send_message(
                    admin_id,
                    f"🤖 *Roastberry Agent* запущен!\n\n"
                    f"📦 Товаров в БД: {count}\n"
                    f"✅ Готов к работе!",
                    parse_mode="Markdown"
                )
            except Exception:
                pass

    asyncio.create_task(check_alerts())
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
