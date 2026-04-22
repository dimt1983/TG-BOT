"""
Roastberry Agent Bot — умный админ-ассистент на Claude API.

Переменные окружения:
  AGENT_BOT_TOKEN    — токен агент-бота от BotFather
  ANTHROPIC_API_KEY  — ключ Claude API (необязательно — есть резервный режим)
  ADMIN_IDS          — telegram id админов через запятую (напр: 466755177)
  AGENT_PORT         — порт keep-alive сервера (по умолчанию 10001)
"""

import asyncio
import io
import json
import os
import sqlite3
import threading
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer

import aiohttp
import openpyxl
from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, CommandStart
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import Message

# ─── Конфиг ───────────────────────────────────────────────────────────────────
AGENT_TOKEN   = os.environ.get("AGENT_BOT_TOKEN")
ANTHROPIC_KEY = os.environ.get("ANTHROPIC_API_KEY")
ADMIN_IDS     = [int(x) for x in os.environ.get("ADMIN_IDS", "466755177").split(",")]
DB_PATH       = "/app/agent_shop.db"

bot = Bot(token=AGENT_TOKEN)
dp  = Dispatcher(storage=MemoryStorage())

# ─── Keep-alive сервер ────────────────────────────────────────────────────────
class _Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"OK")
    def log_message(self, *a):
        pass

def _run_server():
    port = int(os.environ.get("AGENT_PORT", 10001))
    HTTPServer(("0.0.0.0", port), _Handler).serve_forever()

threading.Thread(target=_run_server, daemon=True).start()

# ─── БД ───────────────────────────────────────────────────────────────────────
def init_db():
    con = sqlite3.connect(DB_PATH)
    con.execute(
        "CREATE TABLE IF NOT EXISTS categories"
        "(id INTEGER PRIMARY KEY, name TEXT, parent_id INTEGER)"
    )
    con.execute(
        "CREATE TABLE IF NOT EXISTS products"
        "(id INTEGER PRIMARY KEY, name TEXT, description TEXT,"
        " price REAL DEFAULT 0, stock INTEGER DEFAULT 0,"
        " category_id INTEGER, roast_type TEXT,"
        " weight_g INTEGER DEFAULT 1000,"
        " tag TEXT DEFAULT '', prev_price REAL DEFAULT 0)"
    )
    con.execute(
        "CREATE TABLE IF NOT EXISTS users"
        "(user_id INTEGER PRIMARY KEY, tg_name TEXT,"
        " user_type TEXT DEFAULT 'individual',"
        " name TEXT, phone TEXT, company_name TEXT,"
        " inn TEXT, email TEXT,"
        " created_at DATETIME DEFAULT CURRENT_TIMESTAMP)"
    )
    con.execute(
        "CREATE TABLE IF NOT EXISTS orders"
        "(id INTEGER PRIMARY KEY, user_id INTEGER,"
        " name TEXT, phone TEXT, address TEXT,"
        " total REAL, discount REAL DEFAULT 0,"
        " status TEXT DEFAULT 'new',"
        " created_at DATETIME DEFAULT CURRENT_TIMESTAMP)"
    )
    con.execute(
        "CREATE TABLE IF NOT EXISTS order_items"
        "(id INTEGER PRIMARY KEY, order_id INTEGER,"
        " product_id INTEGER, quantity INTEGER, price REAL)"
    )
    con.commit()
    con.close()


def get_db():
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    return con


def db_query(sql, params=()):
    try:
        con = get_db()
        rows = con.execute(sql, params).fetchall()
        con.close()
        return [dict(r) for r in rows]
    except Exception:
        return []


def is_admin(uid):
    return uid in ADMIN_IDS

# ─── Claude API ───────────────────────────────────────────────────────────────
async def ask_claude(system, user, max_tokens=2000):
    if not ANTHROPIC_KEY:
        return "ANTHROPIC_API_KEY не задан."
    async with aiohttp.ClientSession() as s:
        resp = await s.post(
            "https://api.proxyapi.ru/anthropic/v1/messages",
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
            },
        )
        data = await resp.json()
    if "content" in data:
        return data["content"][0]["text"]
    return "Ошибка API: " + data.get("error", {}).get("message", str(data))

# ─── Резервное сопоставление ──────────────────────────────────────────────────
def fuzzy_match(source, candidates):
    src = source.lower()
    for noise in ['"', "roastberry", " rbr", "be ", "bf ",
                  "упак.", "молотый", "байховый", "пакетир"]:
        src = src.replace(noise, " ")
    src_words = set(src.split())
    best_name, best_score = None, 0.0
    for cand in candidates:
        cand_l = cand.lower()
        cand_words = set(cand_l.split())
        common = src_words & cand_words
        if not common:
            continue
        score = len(common) / max(len(src_words), len(cand_words))
        if src in cand_l or cand_l in src:
            score += 0.3
        for kw in ["бразилия", "эфиопия", "кения", "колумбия", "классика",
                   "венеция", "биттер", "botanika", "herbarista", "althaus"]:
            if kw in src and kw in cand_l:
                score += 0.2
                break
        if score > best_score:
            best_score, best_name = score, cand
    return best_name, best_score


def detect_type(rows):
    return "both" if sum(1 for r in rows[:20] if len(r["nums"]) >= 2) > 10 else "stock"

# ─── /start ───────────────────────────────────────────────────────────────────
@dp.message(CommandStart())
async def cmd_start(message: Message):
    if not is_admin(message.from_user.id):
        await message.answer("нет доступа")
        return
    cnt = db_query("SELECT COUNT(*) as c FROM products")
    n = cnt[0]["c"] if cnt else 0
    mode = "Claude API" if ANTHROPIC_KEY else "резервный режим (без Claude)"
    await message.answer(
        "*Roastberry Agent* готов!\n\n"
        f"Товаров в БД: *{n}*\n"
        f"Режим: {mode}\n\n"
        "Команды:\n"
        "Отправь xlsx — обновлю остатки/цены\n"
        "/analytics — аналитика\n"
        "/lowstock — низкий остаток\n"
        "/top — топ продаж\n"
        "/clients — клиенты\n"
        "/prices — цены\n"
        "/syncdb — статус БД\n\n"
        "Или задай вопрос в свободной форме!",
        parse_mode="Markdown",
    )

# ─── /syncdb ──────────────────────────────────────────────────────────────────
@dp.message(Command("syncdb"))
async def cmd_syncdb(message: Message):
    if not is_admin(message.from_user.id):
        return
    p = (db_query("SELECT COUNT(*) as c FROM products") or [{"c": 0}])[0]["c"]
    o = (db_query("SELECT COUNT(*) as c FROM orders") or [{"c": 0}])[0]["c"]
    u = (db_query("SELECT COUNT(*) as c FROM users") or [{"c": 0}])[0]["c"]
    await message.answer(
        f"*Состояние БД агента:*\n\nТоваров: *{p}*\nЗаказов: *{o}*\nКлиентов: *{u}*\n\n"
        + ("Отправь xlsx для загрузки данных." if p == 0 else "БД заполнена."),
        parse_mode="Markdown",
    )

# ─── /analytics ───────────────────────────────────────────────────────────────
@dp.message(Command("analytics"))
async def cmd_analytics(message: Message):
    if not is_admin(message.from_user.id):
        return
    week_ago = (datetime.now() - timedelta(days=7)).strftime("%Y-%m-%d")
    orders = db_query(
        "SELECT COUNT(*) as cnt, COALESCE(SUM(total),0) as rev FROM orders "
        "WHERE created_at>=? AND status!='cancelled'", (week_ago,)
    )
    top = db_query(
        "SELECT p.name, SUM(oi.quantity) as qty, SUM(oi.quantity*oi.price) as rev "
        "FROM order_items oi JOIN products p ON oi.product_id=p.id "
        "JOIN orders o ON oi.order_id=o.id "
        "WHERE o.created_at>=? AND o.status!='cancelled' "
        "GROUP BY p.id ORDER BY rev DESC LIMIT 5", (week_ago,)
    )
    nc = (db_query("SELECT COUNT(*) as c FROM users WHERE created_at>=?", (week_ago,)) or [{"c":0}])[0]["c"]
    low = (db_query("SELECT COUNT(*) as c FROM products WHERE stock>0 AND stock<=5") or [{"c":0}])[0]["c"]
    out = (db_query("SELECT COUNT(*) as c FROM products WHERE stock=0") or [{"c":0}])[0]["c"]
    rev = orders[0]["rev"] if orders else 0
    cnt = orders[0]["cnt"] if orders else 0
    lines = [
        "*Аналитика за 7 дней*\n",
        f"Выручка: *{rev:.0f} руб*",
        f"Заказов: *{cnt}*",
        f"Новых клиентов: *{nc}*",
        f"Заканчивается (<=5): *{low}*",
        f"Нет в наличии: *{out}*",
    ]
    if top:
        lines.append("\n*Топ продаж:*")
        for i, t in enumerate(top, 1):
            lines.append(f"  {i}. {t['name'][:40]} — {t['qty']} шт / {t['rev']:.0f} руб")
    await message.answer("\n".join(lines), parse_mode="Markdown")

# ─── /lowstock ────────────────────────────────────────────────────────────────
@dp.message(Command("lowstock"))
async def cmd_lowstock(message: Message):
    if not is_admin(message.from_user.id):
        return
    low  = db_query("SELECT name, stock FROM products WHERE stock<=5 AND stock>0 ORDER BY stock LIMIT 30")
    zero = db_query("SELECT name FROM products WHERE stock=0 ORDER BY name LIMIT 20")
    lines = ["*Заканчивается:*\n"]
    for p in low:
        lines.append(f"  {p['name'][:45]}: *{p['stock']}*")
    if not low:
        lines.append("  Всё в порядке!")
    if zero:
        lines.append(f"\n*Нет в наличии ({len(zero)}):*")
        for p in zero[:10]:
            lines.append(f"  - {p['name'][:45]}")
        if len(zero) > 10:
            lines.append(f"  ... и ещё {len(zero)-10}")
    text = "\n".join(lines)
    for i in range(0, len(text), 4000):
        await message.answer(text[i:i+4000], parse_mode="Markdown")

# ─── /top ─────────────────────────────────────────────────────────────────────
@dp.message(Command("top"))
async def cmd_top(message: Message):
    if not is_admin(message.from_user.id):
        return
    top = db_query(
        "SELECT p.name, SUM(oi.quantity) as qty, SUM(oi.quantity*oi.price) as rev "
        "FROM order_items oi JOIN products p ON oi.product_id=p.id "
        "JOIN orders o ON oi.order_id=o.id WHERE o.status!='cancelled' "
        "GROUP BY p.id ORDER BY rev DESC LIMIT 15"
    )
    lines = ["*Топ продаж:*\n"]
    for i, t in enumerate(top, 1):
        lines.append(f"{i}. {t['name'][:42]}\n   {t['qty']} шт — {t['rev']:.0f} руб")
    if not top:
        lines.append("Данных пока нет.")
    await message.answer("\n".join(lines), parse_mode="Markdown")

# ─── /clients ─────────────────────────────────────────────────────────────────
@dp.message(Command("clients"))
async def cmd_clients(message: Message):
    if not is_admin(message.from_user.id):
        return
    data = db_query(
        "SELECT u.name, u.company_name, u.user_type, "
        "COUNT(o.id) as cnt, COALESCE(SUM(o.total),0) as spent, "
        "MAX(o.created_at) as last "
        "FROM users u LEFT JOIN orders o ON u.user_id=o.user_id AND o.status!='cancelled' "
        "GROUP BY u.user_id ORDER BY spent DESC LIMIT 20"
    )
    lines = ["*Клиенты (топ-20):*\n"]
    for c in data:
        name = c["company_name"] or c["name"]
        icon = "org" if c["user_type"] == "company" else "usr"
        last = c["last"][:10] if c["last"] else "-"
        lines.append(f"[{icon}] *{name}*\n  {c['cnt']} заказов | {c['spent']:.0f} руб | {last}")
    if not data:
        lines.append("Клиентов пока нет.")
    text = "\n".join(lines)
    for i in range(0, len(text), 4000):
        await message.answer(text[i:i+4000], parse_mode="Markdown")

# ─── /prices ──────────────────────────────────────────────────────────────────
@dp.message(Command("prices"))
async def cmd_prices(message: Message):
    if not is_admin(message.from_user.id):
        return
    cats = db_query("SELECT id, name FROM categories WHERE parent_id IS NULL")
    lines = ["*Сводка цен:*\n"]
    for cat in cats:
        prods = db_query(
            "SELECT name, price FROM products WHERE category_id=? AND stock>0 "
            "ORDER BY price DESC LIMIT 5", (cat["id"],)
        )
        if prods:
            lines.append(f"*{cat['name']}:*")
            for p in prods:
                lines.append(f"  - {p['name'][:40]} — {p['price']:.0f} руб")
    text = "\n".join(lines)
    for i in range(0, len(text), 4000):
        await message.answer(text[i:i+4000], parse_mode="Markdown")

# ─── Обработка xlsx ───────────────────────────────────────────────────────────
@dp.message(F.document)
async def handle_xlsx(message: Message):
    if not is_admin(message.from_user.id):
        return
    doc = message.document
    if not doc.file_name.endswith(".xlsx"):
        await message.answer("Нужен файл .xlsx")
        return

    await message.answer("Читаю файл...")
    file = await bot.get_file(doc.file_id)
    buf = io.BytesIO()
    await bot.download_file(file.file_path, buf)
    buf.seek(0)

    try:
        wb = openpyxl.load_workbook(buf, read_only=True, data_only=True)
        ws = wb.active
    except Exception as e:
        await message.answer(f"Не удалось открыть: {e}")
        return

    skip = ["итого", "всего", "параметры", "период", "номенклатура", "количество", "ведомость"]
    rows = []
    for row in ws.iter_rows(values_only=True):
        if not row[0] or not isinstance(row[0], str) or len(row[0]) < 4:
            continue
        name = row[0].strip()
        if any(s in name.lower() for s in skip) and len(name) < 40:
            continue
        nums = [round(float(c)) for c in row[1:] if isinstance(c, (int, float))]
        if not nums:
            continue
        rows.append({"name": name, "nums": nums})

    if not rows:
        await message.answer("Данных не найдено.")
        return

    await message.answer(f"Найдено {len(rows)} строк. Сопоставляю...")

    db_products = db_query("SELECT id, name, price, stock FROM products ORDER BY name")
    if not db_products:
        await message.answer(
            "БД агента пуста. Сначала сделай /resetdb и /loadstock в основном боте."
        )
        return

    db_names = [p["name"] for p in db_products]
    db_index = {p["name"]: p for p in db_products}
    file_type = detect_type(rows)
    matches = []
    claude_ok = False

    if ANTHROPIC_KEY:
        await message.answer("Использую Claude для умного сопоставления...")
        try:
            for chunk_start in range(0, len(rows), 40):
                chunk = rows[chunk_start:chunk_start + 40]
                chunk_text = "\n".join(
                    f"{r['name']} | {' | '.join(str(n) for n in r['nums'])}"
                    for r in chunk
                )
                db_str = "\n".join(db_names[:120])
                system = (
                    "Помощник магазина кофе Roastberry. "
                    "Сопоставь названия из 1С с товарами бота. "
                    "Отвечай ТОЛЬКО валидным JSON без markdown."
                )
                prompt = (
                    "Данные из 1С:\n" + chunk_text + "\n\n"
                    "Товары бота:\n" + db_str + "\n\n"
                    "Верни JSON: "
                    '{"file_type":"stock","matches":[{"source_name":"...","bot_name":"...",'
                    '"stock":0,"price":null,"confidence":"high"}]}'
                )
                result_text = await ask_claude(system, prompt, max_tokens=3000)
                clean = result_text.strip()
                if clean.startswith("```"):
                    clean = "\n".join(clean.split("\n")[1:-1])
                parsed = json.loads(clean)
                if chunk_start == 0:
                    file_type = parsed.get("file_type", file_type)
                matches.extend(parsed.get("matches", []))
            claude_ok = True
        except Exception as e:
            await message.answer(f"Claude недоступен: {str(e)[:100]}\nПереключаюсь на резервный режим...")

    if not claude_ok:
        await message.answer("Резервный режим — сопоставляю по ключевым словам...")
        for r in rows:
            best, score = fuzzy_match(r["name"], db_names)
            stock = int(r["nums"][-1]) if r["nums"] else None
            price = float(r["nums"][-2]) if len(r["nums"]) >= 2 else None
            if score >= 0.35 and best:
                matches.append({
                    "source_name": r["name"],
                    "bot_name": best,
                    "stock": stock,
                    "price": price,
                    "confidence": "high" if score >= 0.6 else "medium",
                })
            else:
                matches.append({
                    "source_name": r["name"],
                    "bot_name": None,
                    "stock": None,
                    "price": None,
                    "confidence": "low",
                })

    upd_stock = upd_price = skipped = 0
    not_found = []
    con = get_db()
    for m in matches:
        bot_name = m.get("bot_name")
        conf = m.get("confidence", "low")
        if not bot_name or conf == "low":
            not_found.append(m.get("source_name", "?"))
            skipped += 1
            continue
        product = db_index.get(bot_name)
        if not product:
            for name, p in db_index.items():
                if bot_name.lower() in name.lower() or name.lower() in bot_name.lower():
                    product = p
                    break
        if not product:
            not_found.append(str(m.get("source_name", "")) + " -> " + bot_name)
            skipped += 1
            continue
        pid = product["id"]
        if file_type in ("stock", "both") and m.get("stock") is not None:
            con.execute("UPDATE products SET stock=? WHERE id=?", (int(m["stock"]), pid))
            upd_stock += 1
        if file_type in ("prices", "both") and m.get("price") and m["price"] > 0:
            con.execute("UPDATE products SET prev_price=?, price=? WHERE id=?",
                        (product["price"], float(m["price"]), pid))
            upd_price += 1
    con.commit()
    con.close()

    total_now = (db_query("SELECT COUNT(*) as c FROM products") or [{"c":0}])[0]["c"]
    type_labels = {"stock": "остатки", "prices": "цены", "both": "остатки и цены"}
    lines = [
        "Обновление завершено!\n",
        f"Тип: {type_labels.get(file_type, file_type)}",
        f"Остатков обновлено: *{upd_stock}*",
        f"Цен обновлено: *{upd_price}*",
        f"Пропущено: *{skipped}*",
        f"Всего товаров в БД: *{total_now}*",
    ]
    if not_found:
        lines.append(f"\nНе сопоставлено ({len(not_found)}):")
        for nf in not_found[:8]:
            lines.append(f"  - {nf[:50]}")
        if len(not_found) > 8:
            lines.append(f"  ... и ещё {len(not_found)-8}")
    await message.answer("\n".join(lines), parse_mode="Markdown")

    low = db_query("SELECT name, stock FROM products WHERE stock>0 AND stock<=3 ORDER BY stock LIMIT 5")
    if low:
        alert_lines = ["Критически низкий остаток:"]
        for p in low:
            alert_lines.append(f"  {p['name'][:45]}: *{p['stock']} шт*")
        await message.answer("\n".join(alert_lines), parse_mode="Markdown")

# ─── Умные вопросы ────────────────────────────────────────────────────────────
@dp.message(F.text & ~F.text.startswith("/"))
async def smart_query(message: Message):
    if not is_admin(message.from_user.id):
        return
    query = message.text.strip()
    await message.answer("Думаю...")

    stats = db_query(
        "SELECT "
        "(SELECT COUNT(*) FROM orders WHERE status!='cancelled') as total_orders,"
        "(SELECT COALESCE(SUM(total),0) FROM orders WHERE status!='cancelled') as revenue,"
        "(SELECT COUNT(*) FROM users) as clients,"
        "(SELECT COUNT(*) FROM products WHERE stock=0) as out_of_stock,"
        "(SELECT COUNT(*) FROM products WHERE stock<=5 AND stock>0) as low_stock"
    )
    s = stats[0] if stats else {}
    recent = db_query(
        "SELECT o.id, o.created_at, o.total, o.name, "
        "GROUP_CONCAT(p.name||' x'||oi.quantity) as items "
        "FROM orders o JOIN order_items oi ON o.id=oi.order_id "
        "JOIN products p ON oi.product_id=p.id "
        "WHERE o.status!='cancelled' GROUP BY o.id "
        "ORDER BY o.created_at DESC LIMIT 8"
    )
    top = db_query(
        "SELECT p.name, SUM(oi.quantity) as qty "
        "FROM order_items oi JOIN products p ON oi.product_id=p.id "
        "JOIN orders o ON oi.order_id=o.id WHERE o.status!='cancelled' "
        "GROUP BY p.id ORDER BY qty DESC LIMIT 8"
    )
    orders_str = " | ".join(
        str(o["id"]) + " " + str(o["created_at"])[:10] + " " + str(o["name"]) + " " + str(o["total"]) + "r"
        for o in recent
    )
    top_str = " | ".join(str(t["name"])[:30] + " " + str(t["qty"]) + "шт" for t in top)
    context = (
        "Магазин Roastberry. "
        "Заказов: " + str(s.get("total_orders", 0)) + ", "
        "выручка: " + str(round(s.get("revenue", 0))) + "р, "
        "клиентов: " + str(s.get("clients", 0)) + ", "
        "нет в наличии: " + str(s.get("out_of_stock", 0)) + ", "
        "мало (<=5): " + str(s.get("low_stock", 0)) + ". "
        "Последние заказы: " + orders_str + ". "
        "Топ продаж: " + top_str
    )
    system = (
        "Ты умный ассистент магазина кофе Roastberry. "
        "Отвечай кратко и по делу на русском, используй Markdown."
    )
    response = await ask_claude(system, "Данные: " + context + "\n\nВопрос: " + query)
    await message.answer(response, parse_mode="Markdown")

# ─── Авто-алерты ─────────────────────────────────────────────────────────────

# ─── /initdb — загрузка каталога в локальную БД агента ───────────────────────
CATALOG_SEED = [
    # (категория_верхнего_уровня, подкатегория, название_товара)
    # ── КОФЕ Моносорта 1 кг ──────────────────────────────────────────────────
    ("Кофе","Моносорта","Бразилия Серрадо 1 кг"),
    ("Кофе","Моносорта","Бразилия Серрадо Дарк 1 кг"),
    ("Кофе","Моносорта","Бразилия Сан Рафаель 1 кг"),
    ("Кофе","Моносорта","Бразилия Серрадо Желтый Бурбон 1 кг"),
    ("Кофе","Моносорта","Уганда Вугар Элгон 1 кг"),
    ("Кофе","Моносорта","Уганда Рувензори 1 кг"),
    ("Кофе","Моносорта","Кения АА 1 кг"),
    ("Кофе","Моносорта","Кения АБ Центральная провинция 1 кг"),
    ("Кофе","Моносорта","Колумбия Андино 1 кг"),
    ("Кофе","Моносорта","Гватемала Декаф 1 кг"),
    ("Кофе","Моносорта","Гватемала Уетенанго 1 кг"),
    ("Кофе","Моносорта","Гондурас Сан Николас 1 кг"),
    ("Кофе","Моносорта","Перу Монте Верде 1 кг"),
    ("Кофе","Моносорта","Перу SHG 1 кг"),
    ("Кофе","Моносорта","Руанда Мутетели 1 кг"),
    ("Кофе","Моносорта","Танзания АА 1 кг"),
    ("Кофе","Моносорта","Эфиопия Лиму гр.2 1 кг"),
    ("Кофе","Моносорта","Эфиопия Сидамо гр.2 1 кг"),
    ("Кофе","Моносорта","Эфиопия Иргачиф гр.2 1 кг"),
    ("Кофе","Моносорта","Эфиопия Иргачиф гр.4 1 кг"),
    ("Кофе","Моносорта","Эфиопия Milk 1 кг"),
    ("Кофе","Моносорта","Эфиопия Челчеле гр.1 1 кг"),
    ("Кофе","Моносорта","Китай Симао 1 кг"),
    # ── КОФЕ Моносорта 200 г ─────────────────────────────────────────────────
    ("Кофе","Моносорта","Бразилия Серрадо 200 г"),
    ("Кофе","Моносорта","Бразилия Серрадо Дарк 200 г"),
    ("Кофе","Моносорта","Бразилия Сан Рафаель 200 г"),
    ("Кофе","Моносорта","Бразилия Паже 200 г"),
    ("Кофе","Моносорта","Гватемала Декаф 200 г"),
    ("Кофе","Моносорта","Гватемала Уетенанго 200 г"),
    ("Кофе","Моносорта","Гондурас Сан Николас 200 г"),
    ("Кофе","Моносорта","Кения АА 200 г"),
    ("Кофе","Моносорта","Кения АБ Центральная провинция 200 г"),
    ("Кофе","Моносорта","Колумбия Андино 200 г"),
    ("Кофе","Моносорта","Перу SHG 200 г"),
    ("Кофе","Моносорта","Руанда Мутетели 200 г"),
    ("Кофе","Моносорта","Танзания АА 200 г"),
    ("Кофе","Моносорта","Уганда Рувензори 200 г"),
    ("Кофе","Моносорта","Эфиопия Иргачиф гр.2 200 г"),
    ("Кофе","Моносорта","Эфиопия Иргачиф гр.4 200 г"),
    ("Кофе","Моносорта","Эфиопия Лиму гр.2 200 г"),
    ("Кофе","Моносорта","Эфиопия Сидамо гр.2 200 г"),
    ("Кофе","Моносорта","Эфиопия Челчеле гр.1 200 г"),
    ("Кофе","Моносорта","Китай Симао 200 г"),
    # ── КОФЕ Смеси ───────────────────────────────────────────────────────────
    ("Кофе","Смеси","БИТТЕР 1 кг"), ("Кофе","Смеси","ВЕНЕЦИЯ 1 кг"),
    ("Кофе","Смеси","КЛАССИКА 1 кг"), ("Кофе","Смеси","ХАНИ 1 кг"),
    ("Кофе","Смеси","ЛУНГО 1 кг"), ("Кофе","Смеси","FRESH 1 кг"),
    ("Кофе","Смеси","БИТТЕР 200 г"), ("Кофе","Смеси","ВЕНЕЦИЯ 200 г"),
    ("Кофе","Смеси","КЛАССИКА 200 г"), ("Кофе","Смеси","ХАНИ 200 г"),
    ("Кофе","Смеси","ЛУНГО 200 г"),
    # ── КОФЕ Black Edition ───────────────────────────────────────────────────
    ("Кофе","Микролоты Black Edition","Эфиопия Ададо 1 кг"),
    ("Кофе","Микролоты Black Edition","Эфиопия Арича гр.1 1 кг"),
    ("Кофе","Микролоты Black Edition","Эфиопия Чelelекту гр.1 1 кг"),
    ("Кофе","Микролоты Black Edition","Эфиопия Белойя гр.2 1 кг"),
    ("Кофе","Микролоты Black Edition","Эфиопия Adadо 200 г"),
    ("Кофе","Микролоты Black Edition","Эфиопия Белойя гр.2 200 г"),
    ("Кофе","Микролоты Black Edition","Эфиопия Чelelекту гр.1 200 г"),
    ("Кофе","Микролоты Black Edition","Эфиопия Челчеле гр.1 200 г"),
    # ── КОФЕ Борщ Edition ────────────────────────────────────────────────────
    ("Кофе","Микролоты Борщ Edition","Колумбия Гонзало Кармона 1 кг"),
    ("Кофе","Микролоты Борщ Edition","Колумбия Гонзало Кармона 200 г"),
    ("Кофе","Микролоты Борщ Edition","Колумбия Хайро Арсила 200 г"),
    ("Кофе","Микролоты Борщ Edition","Экваториальный блэнд 200 г"),
    ("Кофе","Микролоты Борщ Edition","Эфиопия Арича гр.1 200 г"),
    # ── КОФЕ Drip ────────────────────────────────────────────────────────────
    ("Кофе","Drip","Гватемала Уетенанго (8 шт)"),
    ("Кофе","Drip","Коста-Рика Тарразу (8 шт)"),
    ("Кофе","Drip","Кения Моунт С (8 шт)"),
    ("Кофе","Drip","Колумбия Супремо (8 шт)"),
    ("Кофе","Drip","Колумбия Клаудиа Колменарес (8 шт)"),
    ("Кофе","Drip","Эфиопия Бомбе (8 шт)"),
    ("Кофе","Drip","Колумбия Питалито (8 шт)"),
    ("Кофе","Drip","Кения АА (Drip) (1 шт)"),
    ("Кофе","Drip","Коста-Рика Тарразу (1 шт)"),
    ("Кофе","Drip","Колумбия Клаудиа Колменарес (1 шт)"),
    ("Кофе","Drip","Гватемала Уетенанго (1 шт)"),
    ("Кофе","Drip","Эфиопия Бомбе (1 шт)"),
    ("Кофе","Drip","Эфиопия Иргачиф (1 шт)"),
    # ── Молоко ───────────────────────────────────────────────────────────────
    ("Молоко","Молоко","Green Milk Банановый напиток соевый 1л"),
    ("Молоко","Молоко","Green Milk Кокосовый напиток соевый 1л"),
    ("Молоко","Молоко","Green Milk Миндальный напиток соевый 1л"),
    ("Молоко","Молоко","Green Milk Соевый напиток 1л"),
    ("Молоко","Молоко","Green Milk Фундучный напиток соевый 1л"),
    # ── Сиропы BOTANIKA ──────────────────────────────────────────────────────
    ("Сиропы","BOTANIKA","Сироп BOTANIKA Арбуз 1л"),
    ("Сиропы","BOTANIKA","Сироп BOTANIKA Бабл Гам 1л"),
    ("Сиропы","BOTANIKA","Сироп BOTANIKA Банан Желтый 1л"),
    ("Сиропы","BOTANIKA","Сироп BOTANIKA Ваниль 1л"),
    ("Сиропы","BOTANIKA","Сироп BOTANIKA Вишня 1л"),
    ("Сиропы","BOTANIKA","Сироп BOTANIKA Гранат 1л"),
    ("Сиропы","BOTANIKA","Сироп BOTANIKA Дыня 1л"),
    ("Сиропы","BOTANIKA","Сироп BOTANIKA Дыня Зеленая 1л"),
    ("Сиропы","BOTANIKA","Сироп BOTANIKA Карамель 1л"),
    ("Сиропы","BOTANIKA","Сироп BOTANIKA Карамель Соленая 1л"),
    ("Сиропы","BOTANIKA","Сироп BOTANIKA Клубника 1л"),
    ("Сиропы","BOTANIKA","Сироп BOTANIKA Кокос 1л"),
    ("Сиропы","BOTANIKA","Сироп BOTANIKA Лаванда 1л"),
    ("Сиропы","BOTANIKA","Сироп BOTANIKA Малина 1л"),
    ("Сиропы","BOTANIKA","Сироп BOTANIKA Манго 1л"),
    ("Сиропы","BOTANIKA","Сироп BOTANIKA Мед 1л"),
    ("Сиропы","BOTANIKA","Сироп BOTANIKA Мята Зеленая 1л"),
    ("Сиропы","BOTANIKA","Сироп BOTANIKA Персик 1л"),
    ("Сиропы","BOTANIKA","Сироп BOTANIKA Тархун 1л"),
    ("Сиропы","BOTANIKA","Сироп BOTANIKA Шоколад 1л"),
    # ── Сиропы Herbarista ────────────────────────────────────────────────────
    ("Сиропы","Herbarista","Сироп Herbarista Бурбонская Ваниль 0.7л"),
    ("Сиропы","Herbarista","Сироп Herbarista Карамель Двойного Обжига 0.7л"),
    ("Сиропы","Herbarista","Сироп Herbarista Кокос с мякотью 0.7л"),
    ("Сиропы","Herbarista","Сироп Herbarista Лесной орех 0.7л"),
    ("Сиропы","Herbarista","Сироп Herbarista Малина с листьями 0.7л"),
    ("Сиропы","Herbarista","Сироп Herbarista Попкорн 0.7л"),
    ("Сиропы","Herbarista","Сироп Herbarista Фисташка 0.7л"),
    ("Сиропы","Herbarista","Сироп Herbarista Шоколадный трюфель 0.7л"),
    # ── Сиропы SweetShot ─────────────────────────────────────────────────────
    ("Сиропы","SweetShot","Сироп SweetShot Ваниль 1л"),
    ("Сиропы","SweetShot","Сироп SweetShot Карамель 1л"),
    ("Сиропы","SweetShot","Сироп SweetShot Клубника 1л"),
    ("Сиропы","SweetShot","Сироп SweetShot Личи 1л"),
    ("Сиропы","SweetShot","Сироп SweetShot Малина 1л"),
    ("Сиропы","SweetShot","Сироп SweetShot Манго 1л"),
    ("Сиропы","SweetShot","Сироп SweetShot Тархун 1л"),
    # ── Сиропы BARLINE ───────────────────────────────────────────────────────
    ("Сиропы","BARLINE","Сироп BARLINE Базилик 1л"),
    ("Сиропы","BARLINE","Сироп BARLINE Земляника 1л"),
    ("Сиропы","BARLINE","Сироп BARLINE Мандарин 1л"),
    ("Сиропы","BARLINE","Сироп BARLINE Сангрия 1л"),
    ("Сиропы","BARLINE","Топпинг BARLINE Карамель 1кг"),
    ("Сиропы","BARLINE","Топпинг BARLINE Шоколад 1кг"),
    # ── Чай ALTHAUS ──────────────────────────────────────────────────────────
    ("Чай","ALTHAUS","ALTHAUS Ассам Молти Кап 15х4г"),
    ("Чай","ALTHAUS","ALTHAUS Сенча Сенпай зел 15х4г"),
    ("Чай","ALTHAUS","ALTHAUS Молочный Улун зел 15х4г"),
    ("Чай","ALTHAUS","ALTHAUS Ройбуш Ванильная Карамель 15х4г"),
    ("Чай","ALTHAUS","ALTHAUS Ассам Меленг чёрн пакет 20х1.75г"),
    ("Чай","ALTHAUS","ALTHAUS Ройал Эрл Грей чёрн аром 20х1.75г"),
    ("Чай","ALTHAUS","ALTHAUS Сенча Сенпай зел пакет 20х1.75г"),
    ("Чай","ALTHAUS","ALTHAUS Ромашковый луг трав 20х1.75г"),
    ("Чай","ALTHAUS","ALTHAUS Чистая Мята трав 20х1.75г"),
    ("Чай","ALTHAUS","ALTHAUS Ассам Меленг чёрн лист 250г"),
    ("Чай","ALTHAUS","ALTHAUS Молочный Улун зел лист 250г"),
    ("Чай","ALTHAUS","ALTHAUS Горные Травы чёрн лист 250г"),
    ("Чай","ALTHAUS","ALTHAUS Японская Липа трав 75г"),
    # ── Чай NIKTEA ───────────────────────────────────────────────────────────
    ("Чай","NIKTEA","NIKTEA Кения Сапфир чёрн пакет 25х2г"),
    ("Чай","NIKTEA","NIKTEA Эрл Грей Ультрамарин чёрн аром 25х2г"),
    ("Чай","NIKTEA","NIKTEA Ориентал Блум зел пакет 25х2г"),
    ("Чай","NIKTEA","NIKTEA Молочный Улун зел аром 25х2г"),
    ("Чай","NIKTEA","NIKTEA Горный Чабрец чёрн пакет 25х2г"),
    ("Чай","NIKTEA","NIKTEA Эрл Грей пирамидки 15х2.7г"),
    ("Чай","NIKTEA","NIKTEA Молочный Улун пирамидки 15х2.7г"),
    ("Чай","NIKTEA","NIKTEA Королевский Завтрак для чайника 20х4г"),
    # ── Чай RBR TEA ──────────────────────────────────────────────────────────
    ("Чай","RBR TEA","RBR TEA Вишневый сад 250г"),
    ("Чай","RBR TEA","RBR TEA Гречишный чай 250г"),
    ("Чай","RBR TEA","RBR TEA Зеленый с жасмином 250г"),
    ("Чай","RBR TEA","RBR TEA Молочный улун 250г"),
    ("Чай","RBR TEA","RBR TEA Сенча 250г"),
    ("Чай","RBR TEA","RBR TEA Облепиховый сбор иван-чай 250г"),
    # ── Чай Restoranica ──────────────────────────────────────────────────────
    ("Чай","Restoranica","Restoranica TOGO Чайный Глинтвейн 30пак"),
    ("Чай","Restoranica","Restoranica TOGO Манго с апельсином 60пак"),
    ("Чай","Restoranica","Restoranica TOGO Медовое яблоко с имбирем 30пак"),
    ("Чай","Restoranica","Restoranica TOGO Иван-Чай с малиной 36пак"),
    ("Чай","Restoranica","Restoranica TOGO Малина с мятой 36пак"),
    ("Чай","Restoranica","Restoranica RESTA Апельсин Манго 30пак"),
    ("Чай","Restoranica","Restoranica RESTA Голубая масала 30пак"),
    ("Чай","Restoranica","Restoranica ICEDTEA Клубника и киви 50пак"),
    ("Чай","Restoranica","Restoranica ICEDTEA Малиновый мохито 50пак"),
    ("Чай","Restoranica","Restoranica ICEDTEA Манго и апельсин 50пак"),
]

@dp.message(Command("initdb"))
async def cmd_initdb(message: Message):
    if not is_admin(message.from_user.id):
        return

    await message.answer("Загружаю каталог в БД агента...")
    con = get_db()

    # Очищаем и пересоздаём
    con.execute("DELETE FROM products")
    con.execute("DELETE FROM categories")

    # Создаём категории
    cat_ids = {}
    for top_name, sub_name, _ in CATALOG_SEED:
        if top_name not in cat_ids:
            cur = con.execute(
                "INSERT INTO categories (name, parent_id) VALUES (?,NULL)", (top_name,)
            )
            cat_ids[top_name] = cur.lastrowid
        key = top_name + "|" + sub_name
        if key not in cat_ids:
            cur = con.execute(
                "INSERT INTO categories (name, parent_id) VALUES (?,?)",
                (sub_name, cat_ids[top_name])
            )
            cat_ids[key] = cur.lastrowid

    # Вставляем товары
    for top_name, sub_name, prod_name in CATALOG_SEED:
        key = top_name + "|" + sub_name
        cat_id = cat_ids.get(key, cat_ids.get(top_name, 1))
        con.execute(
            "INSERT INTO products (name, stock, price, category_id) VALUES (?,0,0,?)",
            (prod_name, cat_id)
        )

    con.commit()
    con.close()

    cnt = (db_query("SELECT COUNT(*) as c FROM products") or [{"c":0}])[0]["c"]
    await message.answer(
        f"Каталог загружен!\n\nТоваров: *{cnt}*\n\n"
        "Теперь отправь xlsx файл из 1С — обновлю остатки и цены.",
        parse_mode="Markdown"
    )

async def check_alerts():
    while True:
        await asyncio.sleep(3600)
        try:
            low = db_query(
                "SELECT name, stock FROM products WHERE stock>0 AND stock<=3 ORDER BY stock LIMIT 10"
            )
            if low:
                lines = ["Алерт: критически низкий остаток!\n"]
                for p in low:
                    lines.append(f"  {p['name'][:45]}: *{p['stock']} шт*")
                text = "\n".join(lines)
                for admin_id in ADMIN_IDS:
                    try:
                        await bot.send_message(admin_id, text, parse_mode="Markdown")
                    except Exception:
                        pass
        except Exception:
            pass

# ─── Запуск ───────────────────────────────────────────────────────────────────
async def main():
    init_db()
    # Автоматически загружаем каталог если БД пуста
    cnt = (db_query("SELECT COUNT(*) as c FROM products") or [{"c":0}])[0]["c"]
    if cnt == 0:
        # Загружаем каталог автоматически
        con = get_db()
        con.execute("DELETE FROM products")
        con.execute("DELETE FROM categories")
        cat_ids = {}
        for top_name, sub_name, prod_name in CATALOG_SEED:
            if top_name not in cat_ids:
                cur = con.execute(
                    "INSERT INTO categories (name, parent_id) VALUES (?,NULL)", (top_name,)
                )
                cat_ids[top_name] = cur.lastrowid
            key = top_name + "|" + sub_name
            if key not in cat_ids:
                cur = con.execute(
                    "INSERT INTO categories (name, parent_id) VALUES (?,?)",
                    (sub_name, cat_ids[top_name])
                )
                cat_ids[key] = cur.lastrowid
            cat_id = cat_ids.get(key, cat_ids.get(top_name, 1))
            con.execute(
                "INSERT INTO products (name, stock, price, category_id) VALUES (?,0,0,?)",
                (prod_name, cat_id)
            )
        con.commit()
        con.close()
        cnt = (db_query("SELECT COUNT(*) as c FROM products") or [{"c":0}])[0]["c"]

    mode = "Claude API (proxyapi.ru)" if ANTHROPIC_KEY else "резервный режим"
    for admin_id in ADMIN_IDS:
        try:
            await bot.send_message(
                admin_id,
                f"*Roastberry Agent* запущен!\nТоваров: *{cnt}* | Режим: {mode}",
                parse_mode="Markdown",
            )
        except Exception:
            pass
    asyncio.create_task(check_alerts())
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
