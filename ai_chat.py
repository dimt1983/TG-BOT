"""
ai_chat.py — клиентский AI-чат для @roastberry-бота.

Ловит обычный текст пользователя (не команды, не FSM-состояния)
и отвечает через Claude Haiku 4.5 с tool-use:
- get_catalog       — каталог товаров из shop.db
- get_my_orders     — заказы текущего юзера
- get_order_details — детали заказа (с проверкой принадлежности)
- escalate_to_support — записать сообщение в chat_messages (читается из TMA)

Промпт-кэширование на system + tools.
"""
from __future__ import annotations

import json
import os
import sqlite3

from aiogram import Router, F
from aiogram.fsm.context import FSMContext
from aiogram.types import Message

DB_PATH = os.environ.get("DB_PATH", "/app/data/shop.db")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "").strip()

# Используем тот же ProxyAPI base URL что и Bishop/Девид
PROXY_BASE = "https://api.proxyapi.ru/anthropic"
MODEL = "claude-haiku-4-5-20251001"
MAX_TOKENS = 800
MAX_TOOL_LOOPS = 6

try:
    from anthropic import AsyncAnthropic
    CLAUDE = AsyncAnthropic(api_key=ANTHROPIC_API_KEY, base_url=PROXY_BASE) \
        if ANTHROPIC_API_KEY else None
except ImportError:
    CLAUDE = None


SYSTEM_PROMPT = [
    {
        "type": "text",
        "text": (
            "Ты — клиентский ассистент кофейного магазина Roastberry. "
            "Помогаешь покупателям: рассказываешь про каталог (кофе, чай, сиропы, молоко), "
            "статус их заказов, доставку и оплату. Отвечай на русском, кратко, по делу.\n\n"
            "Главные правила:\n"
            "• Покупки оформляются ТОЛЬКО через мини-приложение — у клиента в чате есть "
            "кнопка \"🛍 Открыть магазин\". Не обещай оформить заказ в чате — направляй в приложение.\n"
            "• Для статуса заказов используй get_my_orders или get_order_details.\n"
            "• Для вопросов про товары/цены/наличие — get_catalog.\n"
            "• Если клиент явно жалуется на конкретный заказ или просит связаться с менеджером — "
            "вызови escalate_to_support с order_id и текстом. Сообщение попадёт в чат поддержки "
            "в приложении (на экране заказа).\n"
            "• На политику, личное и прочее не по теме — мягко возвращай к магазину.\n\n"
            "О магазине:\n"
            "• Roastberry — обжарщик кофе из Перми, кофе свежей обжарки.\n"
            "• Доставка: самовывоз из Перми, курьер по Перми, СДЭК по России.\n"
            "• Скидки по весу: от 10 кг — 10%, от 25 кг — 20%.\n"
            "• Оплата: онлайн в приложении (карта/Telegram Pay) или наличные при получении."
        ),
        "cache_control": {"type": "ephemeral"},
    }
]

TOOLS = [
    {
        "name": "get_catalog",
        "description": (
            "Каталог товаров из БД магазина с ценой и остатком. "
            "Можно фильтровать по категории и подстроке в названии."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "category": {
                    "type": "string",
                    "enum": ["coffee", "tea", "syrup", "milk", "all"],
                    "description": "Категория, по умолчанию all.",
                },
                "search": {
                    "type": "string",
                    "description": "Подстрока в названии (опционально).",
                },
                "in_stock_only": {
                    "type": "boolean",
                    "description": "Только товары в наличии. По умолчанию true.",
                },
            },
        },
    },
    {
        "name": "get_my_orders",
        "description": (
            "Список заказов текущего пользователя со статусом. "
            "Использовать когда клиент спрашивает про СВОИ заказы (\"где мой заказ?\")."
        ),
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "get_order_details",
        "description": (
            "Детали конкретного заказа (позиции, сумма, статус, адрес). "
            "Возвращает данные только если заказ принадлежит этому клиенту."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "order_id": {"type": "integer"},
            },
            "required": ["order_id"],
        },
    },
    {
        "name": "escalate_to_support",
        "description": (
            "Записать сообщение в чат поддержки заказа (читается администратором "
            "в мини-приложении). Использовать ТОЛЬКО когда клиент явно жалуется на "
            "конкретный заказ или просит связаться с менеджером."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "order_id": {
                    "type": "integer",
                    "description": "ID заказа, к которому относится обращение.",
                },
                "message": {
                    "type": "string",
                    "description": "Текст обращения от имени клиента.",
                },
            },
            "required": ["order_id", "message"],
        },
    },
]
# Кэш на последнем tool — стабильный блок, экономит input-токены при повторах
TOOLS[-1]["cache_control"] = {"type": "ephemeral"}


def _db() -> sqlite3.Connection:
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    return con


# ─── Tool implementations ────────────────────────────────────────────────────

def _tool_get_catalog(category: str = "all", search: str | None = None,
                      in_stock_only: bool = True) -> list:
    cat_like = {
        "coffee": "%Кофе%",
        "tea":    "%Чай%",
        "syrup":  "%Сироп%",
        "milk":   "%Молок%",
    }.get(category)

    sql = (
        "SELECT p.name, p.price, p.stock, c.name AS category "
        "FROM products p "
        "LEFT JOIN categories c  ON c.id  = p.category_id "
        "LEFT JOIN categories cp ON cp.id = c.parent_id "
        "WHERE 1=1 "
    )
    params: list = []
    if cat_like:
        sql += "AND (c.name LIKE ? OR cp.name LIKE ?) "
        params += [cat_like, cat_like]
    if search:
        sql += "AND p.name LIKE ? "
        params.append(f"%{search}%")
    if in_stock_only:
        sql += "AND p.stock > 0 "
    sql += "ORDER BY p.name LIMIT 60"

    con = _db()
    try:
        rows = con.execute(sql, params).fetchall()
        return [dict(r) for r in rows]
    finally:
        con.close()


def _tool_get_my_orders(user_id: int) -> list:
    con = _db()
    try:
        rows = con.execute(
            "SELECT id, total, status, created_at "
            "FROM orders WHERE user_id = ? ORDER BY id DESC LIMIT 15",
            (user_id,),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        con.close()


def _tool_get_order_details(user_id: int, order_id: int) -> dict:
    con = _db()
    try:
        o = con.execute(
            "SELECT id, user_id, name, phone, address, total, status, created_at "
            "FROM orders WHERE id = ?",
            (order_id,),
        ).fetchone()
        if not o:
            return {"error": "Заказ не найден"}
        if o["user_id"] != user_id:
            return {"error": "Это не ваш заказ"}
        items = con.execute(
            "SELECT oi.quantity, oi.price, p.name AS product_name "
            "FROM order_items oi "
            "LEFT JOIN products p ON p.id = oi.product_id "
            "WHERE oi.order_id = ? ORDER BY oi.id",
            (order_id,),
        ).fetchall()
        return {**dict(o), "items": [dict(i) for i in items]}
    finally:
        con.close()


def _tool_escalate(user_id: int, user_name: str, order_id: int,
                   message: str) -> dict:
    con = _db()
    try:
        # На случай если таблицы ещё нет — создаём (та же что у TMA-чата)
        con.execute("""
            CREATE TABLE IF NOT EXISTS chat_messages (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                order_id   INTEGER NOT NULL,
                user_id    INTEGER NOT NULL,
                user_name  TEXT,
                is_admin   INTEGER DEFAULT 0,
                text       TEXT NOT NULL,
                created_at TEXT DEFAULT (datetime('now', 'localtime'))
            )
        """)
        o = con.execute(
            "SELECT user_id FROM orders WHERE id = ?", (order_id,)
        ).fetchone()
        if not o:
            return {"error": "Заказ не найден"}
        if o["user_id"] != user_id:
            return {"error": "Это не ваш заказ"}
        con.execute(
            "INSERT INTO chat_messages "
            "(order_id, user_id, user_name, is_admin, text) "
            "VALUES (?, ?, ?, 0, ?)",
            (order_id, user_id, user_name, message),
        )
        con.commit()
        return {"ok": True, "saved": True, "order_id": order_id}
    finally:
        con.close()


# ─── Agent loop ──────────────────────────────────────────────────────────────

async def run_agent(user_id: int, user_name: str, user_text: str) -> str:
    if CLAUDE is None:
        return "AI-чат временно недоступен (не настроен ANTHROPIC_API_KEY)."

    messages = [{"role": "user", "content": user_text}]

    for _ in range(MAX_TOOL_LOOPS):
        try:
            resp = await CLAUDE.messages.create(
                model=MODEL,
                max_tokens=MAX_TOKENS,
                system=SYSTEM_PROMPT,
                tools=TOOLS,
                messages=messages,
            )
        except Exception as e:
            return f"⚠️ Ошибка AI: {e}"

        if resp.stop_reason == "end_turn":
            text_parts = [b.text for b in resp.content if b.type == "text"]
            answer = "\n".join(text_parts).strip()
            return answer or "Не понял вопрос. Попробуйте сформулировать иначе."

        if resp.stop_reason != "tool_use":
            return "Не понял вопрос. Попробуйте сформулировать иначе."

        # Накапливаем ответ ассистента и собираем результаты тулов
        messages.append({"role": "assistant", "content": resp.content})
        tool_results = []
        for block in resp.content:
            if block.type != "tool_use":
                continue
            args = block.input or {}
            try:
                if block.name == "get_catalog":
                    result = _tool_get_catalog(
                        category=args.get("category", "all"),
                        search=args.get("search"),
                        in_stock_only=args.get("in_stock_only", True),
                    )
                elif block.name == "get_my_orders":
                    result = _tool_get_my_orders(user_id)
                elif block.name == "get_order_details":
                    result = _tool_get_order_details(
                        user_id, int(args.get("order_id", 0))
                    )
                elif block.name == "escalate_to_support":
                    result = _tool_escalate(
                        user_id, user_name,
                        int(args.get("order_id", 0)),
                        str(args.get("message", "")).strip(),
                    )
                else:
                    result = {"error": f"unknown tool {block.name}"}
            except Exception as e:
                result = {"error": str(e)}
            tool_results.append({
                "type": "tool_result",
                "tool_use_id": block.id,
                "content": json.dumps(result, ensure_ascii=False),
            })
        messages.append({"role": "user", "content": tool_results})

    return "Слишком много вызовов. Попробуйте сформулировать вопрос проще."


# ─── Router ──────────────────────────────────────────────────────────────────
router = Router()


@router.message(F.text & ~F.text.startswith("/"))
async def chat_handler(message: Message, state: FSMContext) -> None:
    """Любой обычный текст вне команд и FSM-состояний → AI-чат."""
    # Если юзер находится в активном FSM (регистрация, фидбек) — пропускаем,
    # пусть штатные хэндлеры обработают
    if await state.get_state() is not None:
        return

    text = (message.text or "").strip()
    if not text:
        return
    if len(text) > 500:
        await message.answer("Сообщение длинновато. Попробуйте покороче — отвечу точнее.")
        return

    user_id = message.from_user.id
    full_name = " ".join(filter(None, [
        message.from_user.first_name, message.from_user.last_name
    ])) or (message.from_user.username or "Клиент")

    try:
        await message.bot.send_chat_action(message.chat.id, "typing")
    except Exception:
        pass

    answer = await run_agent(user_id, full_name, text)
    await message.answer(answer)
