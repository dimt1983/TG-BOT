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
    def log_message(self, format, *args):
        pass

def run_server():
    port = int(os.environ.get("PORT", 10000))
    server = HTTPServer(("0.0.0.0", port), Handler)
    server.serve_forever()

threading.Thread(target=run_server, daemon=True).start()

import asyncio
import sqlite3
from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart, Command
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

# ─── Система скидок ──────────────────────────────────────────────────────────
# Скидка считается по суммарному весу (в кг) всей корзины
DISCOUNT_TIERS = [
    (25, 0.20),   # от 25 кг — скидка 20%
    (10, 0.10),   # от 10 кг — скидка 10%
    (0,  0.00),   # до 10 кг — без скидки
]

def get_discount(total_kg: float) -> float:
    """Возвращает процент скидки (0.0–0.20) для данного веса корзины."""
    for threshold, pct in DISCOUNT_TIERS:
        if total_kg >= threshold:
            return pct
    return 0.0

# ─── FSM ─────────────────────────────────────────────────────────────────────
class RegStates(StatesGroup):
    choosing_type       = State()
    waiting_name        = State()
    waiting_phone       = State()
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

# ─── БД ──────────────────────────────────────────────────────────────────────
CATALOG = {
    # ── МОНОСОРТА ──────────────────────────────────────────────────────────
    "Моносорта": {
        "Бразилия Серрадо": {
            "roast": "E",
            "process": "Natural",
            "description": "Цитрусы, жёлтое яблоко, карамель, жареные орехи, тёмный шоколад. Q 81,5",
            "recipe_e": (
                "☕ *Эспрессо (Бразилия Серрадо):*\n"
                "• Помол: мелкий (18–20 делений)\n"
                "• Навеска: 18 г\n"
                "• Выход: 36–40 г\n"
                "• Время: 25–30 сек\n"
                "• Температура: 93–94°C\n\n"
                "_Раскрывает карамель и орехи, добавляет телу плотность._"
            ),
            "recipe_f": None,
            "price_1kg": 2015, "price_200g": 435,
        },
        "Бразилия Серрадо Дарк": {
            "roast": "E",
            "process": "Natural",
            "description": "Сухофрукты, нуга, жареные орехи, сливочная карамель, тёмный шоколад. Q 81,5",
            "recipe_e": (
                "☕ *Эспрессо (Серрадо Дарк):*\n"
                "• Помол: мелкий (16–18 делений)\n"
                "• Навеска: 18 г\n"
                "• Выход: 34–38 г\n"
                "• Время: 24–28 сек\n"
                "• Температура: 91–92°C\n\n"
                "_Тёмная обжарка — чуть короче время пролива, ниже температура._"
            ),
            "recipe_f": None,
            "price_1kg": 2015, "price_200g": 392,
        },
        "Бразилия Сан Рафаель": {
            "roast": "E",
            "process": "Natural",
            "description": "Сухофрукты, чернослив, грецкий орех, тёмный шоколад. Q 82,5",
            "recipe_e": (
                "☕ *Эспрессо (Сан Рафаель):*\n"
                "• Помол: мелкий (18–20 делений)\n"
                "• Навеска: 18 г\n"
                "• Выход: 36–40 г\n"
                "• Время: 26–30 сек\n"
                "• Температура: 93°C\n\n"
                "_Насыщенный профиль с сухофруктами отлично звучит в капучино._"
            ),
            "recipe_f": None,
            "price_1kg": 2070, "price_200g": 445,
        },
        "Уганда Вугар Элгон": {
            "roast": "E",
            "process": "Washed",
            "description": "Специи, грецкий орех, фундук, тёмный шоколад, крепкий чёрный чай.",
            "recipe_e": (
                "☕ *Эспрессо (Уганда Вугар):*\n"
                "• Помол: мелкий (18–20 делений)\n"
                "• Навеска: 18 г\n"
                "• Выход: 38–42 г\n"
                "• Время: 26–30 сек\n"
                "• Температура: 93–94°C\n\n"
                "_Орехи и специи дают насыщенное тело. Хорош в молоке._"
            ),
            "recipe_f": None,
            "price_1kg": 2070, "price_200g": 445,
        },
        "Танзания АА": {
            "roast": "F",
            "process": "Washed",
            "description": "Чернослив, чёрный чай, пряности, красное яблоко.",
            "recipe_e": None,
            "recipe_f": (
                "🫖 *Фильтр (Танзания АА):*\n"
                "• Метод: пуровер / кемекс\n"
                "• Помол: средний\n"
                "• Навеска: 15 г на 250 мл\n"
                "• Температура: 92–93°C\n"
                "• Время: 3–4 мин\n\n"
                "_Раскрывает сложный профиль с пряностями и сухофруктами._"
            ),
            "price_1kg": 2335, "price_200g": 495,
        },
        "Руанда Нямашеки Хиллс": {
            "roast": "EF",
            "process": "Natural",
            "description": "Сухофрукты, тёмные ягоды, цитрусы, чернослив, чёрный чай.",
            "recipe_e": (
                "☕ *Эспрессо (Руанда Нямашеки):*\n"
                "• Помол: мелкий (20–22 делений)\n"
                "• Навеска: 18 г\n"
                "• Выход: 36–40 г\n"
                "• Время: 26–30 сек\n"
                "• Температура: 92–93°C\n\n"
                "_Ягодная сладость хорошо балансируется плотным телом._"
            ),
            "recipe_f": (
                "🫖 *Фильтр (Руанда Нямашеки):*\n"
                "• Метод: аэропресс / пуровер\n"
                "• Помол: средний\n"
                "• Навеска: 15 г на 250 мл\n"
                "• Температура: 90–92°C\n"
                "• Время: 3–4 мин\n\n"
                "_Натуральная обработка даёт яркие ягодные ноты в фильтре._"
            ),
            "price_1kg": 2863, "price_200g": 655,
        },
        "Бразилия Серрадо Жёлтый Бурбон": {
            "roast": "F",
            "process": "Natural",
            "description": "Лимон, апельсин, сухофрукты, тёмный шоколад, какао. Q 84,25",
            "recipe_e": None,
            "recipe_f": (
                "🫖 *Фильтр (Жёлтый Бурбон):*\n"
                "• Метод: пуровер\n"
                "• Помол: средний\n"
                "• Навеска: 15 г на 250 мл\n"
                "• Температура: 92–93°C\n"
                "• Время: 3–3.5 мин\n\n"
                "_Цитрусовая яркость и шоколадная база — классика фильтра._"
            ),
            "price_1kg": 2120, "price_200g": 455,
        },
        "Гватемала Декаф": {
            "roast": "E",
            "process": "Decaf",
            "description": "Курага, чернослив, изюм, карамель, какао, сладкая выпечка, пряности. Q 83",
            "recipe_e": (
                "☕ *Эспрессо (Гватемала Декаф):*\n"
                "• Помол: мелкий (18–20 делений)\n"
                "• Навеска: 18 г\n"
                "• Выход: 36–42 г\n"
                "• Время: 26–30 сек\n"
                "• Температура: 93–94°C\n\n"
                "_Декаф без потери вкуса — пряности и карамель прекрасно раскрываются вечером._"
            ),
            "recipe_f": None,
            "price_1kg": 2440, "price_200g": 520,
        },
        "Гондурас Сан Николас": {
            "roast": "E",
            "process": "Washed",
            "description": "Красное яблоко, орехи, курага, тёмный шоколад. Q 82,5",
            "recipe_e": (
                "☕ *Эспрессо (Гондурас Сан Николас):*\n"
                "• Помол: мелкий (18–20 делений)\n"
                "• Навеска: 18 г\n"
                "• Выход: 36–40 г\n"
                "• Время: 25–30 сек\n"
                "• Температура: 93°C\n\n"
                "_Сбалансированный кофе с фруктовой кислотностью и шоколадным финишем._"
            ),
            "recipe_f": None,
            "price_1kg": 2175, "price_200g": 465,
        },
        "Руанда Мутетели": {
            "roast": "F",
            "process": "Washed",
            "description": "Чёрный чай, вишня, шиповник, слива, лимон.",
            "recipe_e": None,
            "recipe_f": (
                "🫖 *Фильтр (Руанда Мутетели):*\n"
                "• Метод: пуровер / кемекс\n"
                "• Помол: средний\n"
                "• Навеска: 15 г на 250 мл\n"
                "• Температура: 91–93°C\n"
                "• Время: 3–4 мин\n\n"
                "_Элегантный чайный профиль с ягодной кислинкой._"
            ),
            "price_1kg": 2230, "price_200g": 475,
        },
        "Перу гр.1": {
            "roast": "E",
            "process": "Washed",
            "description": "Молочный шоколад, сухофрукты, цитрусы, выпечка, карамель.",
            "recipe_e": (
                "☕ *Эспрессо (Перу гр.1):*\n"
                "• Помол: мелкий (18–20 делений)\n"
                "• Навеска: 18 г\n"
                "• Выход: 36–40 г\n"
                "• Время: 25–30 сек\n"
                "• Температура: 93°C\n\n"
                "_Мягкий, молочно-шоколадный — идеальная основа для латте._"
            ),
            "recipe_f": None,
            "price_1kg": 2230, "price_200g": 475,
        },
        "Перу Монте Верде": {
            "roast": "F",
            "process": "Natural",
            "description": "Молочный шоколад, йогурт, карамель, ром, ананас.",
            "recipe_e": None,
            "recipe_f": (
                "🫖 *Фильтр (Перу Монте Верде):*\n"
                "• Метод: аэропресс / кемекс\n"
                "• Помол: средний\n"
                "• Навеска: 15 г на 250 мл\n"
                "• Температура: 90–92°C\n"
                "• Время: 3–4 мин\n\n"
                "_Экзотическая натуральная обработка: ананас и ром в чашке._"
            ),
            "price_1kg": 2620, "price_200g": 555,
        },
        "Кения АА": {
            "roast": "F",
            "process": "Washed",
            "description": "Ягоды, специи, малиновый джем, карамель. Q 84,25",
            "recipe_e": None,
            "recipe_f": (
                "🫖 *Фильтр (Кения АА):*\n"
                "• Метод: пуровер\n"
                "• Помол: средний\n"
                "• Навеска: 15 г на 250 мл\n"
                "• Температура: 90–91°C\n"
                "• Время: 3–4 мин\n\n"
                "_Яркая ягодная кислотность — один из лучших фильтров из Африки._"
            ),
            "price_1kg": 2595, "price_200g": 550,
        },
        "Кения АБ Центральная провинция": {
            "roast": "EF",
            "process": "Washed",
            "description": "Вишня, цитрус, ревень, карамель. Q 85",
            "recipe_e": (
                "☕ *Эспрессо (Кения АБ):*\n"
                "• Помол: мелкий (20–22 делений)\n"
                "• Навеска: 18 г\n"
                "• Выход: 38–42 г\n"
                "• Время: 26–30 сек\n"
                "• Температура: 92–93°C\n\n"
                "_Необычная кислотность для эспрессо — интересный выбор для кофе с молоком._"
            ),
            "recipe_f": (
                "🫖 *Фильтр (Кения АБ):*\n"
                "• Метод: пуровер / сифон\n"
                "• Помол: средний\n"
                "• Навеска: 15 г на 250 мл\n"
                "• Температура: 90°C\n"
                "• Время: 3.5–4 мин\n\n"
                "_Раскрывает все грани вишнёво-цитрусового профиля Q 85._"
            ),
            "price_1kg": 2490, "price_200g": 530,
        },
        "Колумбия Андино": {
            "roast": "EF",
            "process": "Washed",
            "description": "Зелёное яблоко, красные ягоды, чёрная смородина, цитрусы, шиповник, абрикос. Q 83,75",
            "recipe_e": (
                "☕ *Эспрессо (Колумбия Андино):*\n"
                "• Помол: мелкий (20 делений)\n"
                "• Навеска: 18 г\n"
                "• Выход: 36–40 г\n"
                "• Время: 26–30 сек\n"
                "• Температура: 93°C\n\n"
                "_Яркая фруктовая кислотность, хороша в холодном эспрессо._"
            ),
            "recipe_f": (
                "🫖 *Фильтр (Колумбия Андино):*\n"
                "• Метод: пуровер\n"
                "• Помол: средний\n"
                "• Навеска: 15 г на 250 мл\n"
                "• Температура: 91–93°C\n"
                "• Время: 3–4 мин\n\n"
                "_Смородина и шиповник делают этот кофе очень интересным в фильтре._"
            ),
            "price_1kg": 2175, "price_200g": 465,
        },
        "Никарагуа Марагаджип": {
            "roast": "E",
            "process": "Washed",
            "description": "Табак, специи, цитрусы, чернослив.",
            "recipe_e": (
                "☕ *Эспрессо (Никарагуа Марагаджип):*\n"
                "• Помол: мелкий (18–20 делений)\n"
                "• Навеска: 18 г\n"
                "• Выход: 36–40 г\n"
                "• Время: 25–28 сек\n"
                "• Температура: 93–94°C\n\n"
                "_Крупное зерно сорта марагаджип даёт глубокий, смолистый вкус._"
            ),
            "recipe_f": None,
            "price_1kg": 2545, "price_200g": 540,
        },
        "Уганда Рувензор": {
            "roast": "E",
            "process": "Natural",
            "description": "Шоколад, орехи, тост, сухофрукты. Q 81,75",
            "recipe_e": (
                "☕ *Эспрессо (Уганда Рувензор):*\n"
                "• Помол: мелкий (18–20 делений)\n"
                "• Навеска: 18 г\n"
                "• Выход: 36–40 г\n"
                "• Время: 25–29 сек\n"
                "• Температура: 92–93°C\n\n"
                "_Плотный, шоколадный, с поджаренными нотами. Идеал для капучино._"
            ),
            "recipe_f": None,
            "price_1kg": 2015, "price_200g": 435,
        },
        "Эфиопия Лиму гр.2": {
            "roast": "F",
            "process": "Washed",
            "description": "Цитрусы, вишня, чёрный чай, шоколад. Q 84,5",
            "recipe_e": None,
            "recipe_f": (
                "🫖 *Фильтр (Эфиопия Лиму):*\n"
                "• Метод: пуровер / хемекс\n"
                "• Помол: средний\n"
                "• Навеска: 15 г на 250 мл\n"
                "• Температура: 90–92°C\n"
                "• Время: 3.5–4 мин\n\n"
                "_Чайная утончённость Лиму с цитрусовой яркостью._"
            ),
            "price_1kg": 2120, "price_200g": 455,
        },
        "Эфиопия Сидамо гр.2 Гуджи": {
            "roast": "EF",
            "process": "Washed",
            "description": "Зелёное яблоко, мандарин, грейпфрут, изюм, лимонный леденец. Q 84",
            "recipe_e": (
                "☕ *Эспрессо (Сидамо Гуджи):*\n"
                "• Помол: мелкий (20–22 делений)\n"
                "• Навеска: 18 г\n"
                "• Выход: 40–44 г\n"
                "• Время: 26–30 сек\n"
                "• Температура: 92°C\n\n"
                "_Цитрусовый и чистый — интересный выбор для тех, кто любит «яркий» эспрессо._"
            ),
            "recipe_f": (
                "🫖 *Фильтр (Сидамо Гуджи):*\n"
                "• Метод: пуровер\n"
                "• Помол: средний\n"
                "• Навеска: 15 г на 250 мл\n"
                "• Температура: 90°C\n"
                "• Время: 3–3.5 мин\n\n"
                "_Мандарин и грейпфрут в чашке — летний фильтр._"
            ),
            "price_1kg": 2120, "price_200g": 455,
        },
        "Эфиопия Арича гр.3": {
            "roast": "E",
            "process": "Natural",
            "description": "Персик, молочный шоколад, миндаль, жасмин, бергамот.",
            "recipe_e": (
                "☕ *Эспрессо (Эфиопия Арича гр.3):*\n"
                "• Помол: мелкий (20–22 делений)\n"
                "• Навеска: 18 г\n"
                "• Выход: 38–42 г\n"
                "• Время: 26–30 сек\n"
                "• Температура: 92–93°C\n\n"
                "_Цветочная нежность бергамота с персиком — необычный эспрессо._"
            ),
            "recipe_f": None,
            "price_1kg": 2175, "price_200g": 465,
        },
        "Эфиопия Milk": {
            "roast": "E",
            "process": "Natural",
            "description": "Тёмный шоколад, жареный орех, тёмный виноград, цитрусы.",
            "recipe_e": (
                "☕ *Эспрессо (Эфиопия Milk):*\n"
                "• Помол: мелкий (18–20 делений)\n"
                "• Навеска: 18 г\n"
                "• Выход: 36–40 г\n"
                "• Время: 25–29 сек\n"
                "• Температура: 93°C\n\n"
                "_Насыщенный шоколадно-ореховый эспрессо. Отличная основа для флэт-уайта._"
            ),
            "recipe_f": None,
            "price_1kg": 2015, "price_200g": 435,
        },
        "Эфиопия Иргачиф гр.4": {
            "roast": "E",
            "process": "Natural",
            "description": "Абрикос, горький шоколад, красные ягоды, цитрусовые.",
            "recipe_e": (
                "☕ *Эспрессо (Иргачиф гр.4):*\n"
                "• Помол: мелкий (20–22 делений)\n"
                "• Навеска: 18 г\n"
                "• Выход: 38–42 г\n"
                "• Время: 26–30 сек\n"
                "• Температура: 92–93°C\n\n"
                "_Яркий фруктово-ягодный эспрессо с горькошоколадным финишем._"
            ),
            "recipe_f": None,
            "price_1kg": 2015, "price_200g": 435,
        },
    },

    # ── МИКРОЛОТЫ Black Edition ──────────────────────────────────────────────
    "Микролоты Black Edition": {
        "Эфиопия Ададо": {
            "roast": "F",
            "process": "Natural",
            "description": "Чёрный чай, красное яблоко, миндаль, цитрусы.",
            "recipe_e": None,
            "recipe_f": (
                "🫖 *Фильтр (Эфиопия Ададо):*\n"
                "• Метод: пуровер / аэропресс\n"
                "• Помол: средний\n"
                "• Навеска: 15 г на 250 мл\n"
                "• Температура: 90–92°C\n"
                "• Время: 3.5–4 мин\n\n"
                "_Чайная сложность с нотами яблока и миндаля._"
            ),
            "price_1kg": 2390, "price_200g": 560,
        },
        "Эфиопия Белойя гр.2": {
            "roast": "F",
            "process": "Natural",
            "description": "Персик, вишня, красный апельсин, цветочный мёд.",
            "recipe_e": None,
            "recipe_f": (
                "🫖 *Фильтр (Эфиопия Белойя):*\n"
                "• Метод: пуровер / кемекс\n"
                "• Помол: средний\n"
                "• Навеска: 15 г на 250 мл\n"
                "• Температура: 90–91°C\n"
                "• Время: 3.5–4.5 мин\n\n"
                "_Мёд и персик — один из самых нежных эфиопских профилей._"
            ),
            "price_1kg": 2233, "price_200g": 525,
        },
        "Эфиопия Челелекту гр.1": {
            "roast": "F",
            "process": "Washed",
            "description": "Пряности, цветы, черёмуха, чёрный чай, тёмные ягоды.",
            "recipe_e": None,
            "recipe_f": (
                "🫖 *Фильтр (Эфиопия Челелекту):*\n"
                "• Метод: пуровер / сифон\n"
                "• Помол: средний-крупный\n"
                "• Навеска: 15 г на 250 мл\n"
                "• Температура: 90–91°C\n"
                "• Время: 4–5 мин\n\n"
                "_Сложный цветочно-пряный профиль. Требует чуть крупнее помол._"
            ),
            "price_1kg": 2863, "price_200g": 655,
        },
        "Колумбия Клаудиа Колменарес": {
            "roast": "F",
            "process": "Washed",
            "description": "Сладкая выпечка, сухофрукты, цветы, лайм, молочный шоколад. Q 86",
            "recipe_e": None,
            "recipe_f": (
                "🫖 *Фильтр (Колумбия Клаудиа):*\n"
                "• Метод: пуровер\n"
                "• Помол: средний\n"
                "• Навеска: 15 г на 250 мл\n"
                "• Температура: 91–92°C\n"
                "• Время: 3.5–4 мин\n\n"
                "_Q 86 — топовый скор. Сложный букет: цветы, лайм и шоколад._"
            ),
            "price_1kg": 3500, "price_200g": 780,
        },
        "Эфиопия Челчеле гр.1": {
            "roast": "F",
            "process": "Natural",
            "description": "Бергамот, тёмная слива, черника, специи, чёрный чай.",
            "recipe_e": None,
            "recipe_f": (
                "🫖 *Фильтр (Эфиопия Челчеле):*\n"
                "• Метод: кемекс / пуровер\n"
                "• Помол: средний\n"
                "• Навеска: 15 г на 250 мл\n"
                "• Температура: 90°C\n"
                "• Время: 4–5 мин\n\n"
                "_Черника и бергамот — парфюмный, сложный профиль микролота._"
            ),
            "price_1kg": 2863, "price_200g": 655,
        },
    },

    # ── МИКРОЛОТЫ Борщ Edition ───────────────────────────────────────────────
    "Микролоты Борщ Edition": {
        "Экваториальный блэнд": {
            "roast": "F",
            "process": "Natural/Anaerobic/Washed",
            "description": "Ягоды, красное вино, вишня в шоколаде, косточковые фрукты.",
            "recipe_e": None,
            "recipe_f": (
                "🫖 *Фильтр (Экваториальный блэнд):*\n"
                "• Метод: аэропресс / пуровер\n"
                "• Помол: средний\n"
                "• Навеска: 15 г на 250 мл\n"
                "• Температура: 90–91°C\n"
                "• Время: 3.5–4 мин\n\n"
                "_Три метода обработки в одном блэнде — вино, ягоды, глубина._"
            ),
            "price_1kg": 3020, "price_200g": 685,
        },
        "Эфиопия Арича гр.1": {
            "roast": "F",
            "process": "Natural",
            "description": "Абрикос, клубника, апельсин, красные ягоды, красное яблоко, чернослив.",
            "recipe_e": None,
            "recipe_f": (
                "🫖 *Фильтр (Эфиопия Арича гр.1):*\n"
                "• Метод: пуровер\n"
                "• Помол: средний\n"
                "• Навеска: 15 г на 250 мл\n"
                "• Температура: 90–91°C\n"
                "• Время: 4–4.5 мин\n\n"
                "_Клубника и абрикос — летний фруктовый профиль высшего класса._"
            ),
            "price_1kg": 3020, "price_200g": 685,
        },
        "Колумбия Хайро Арсила Натуральная": {
            "roast": "F",
            "process": "Natural",
            "description": "Красные ягоды, красные фрукты, травяной чай, тростниковый сахар.",
            "recipe_e": None,
            "recipe_f": (
                "🫖 *Фильтр (Колумбия Хайро Арсила):*\n"
                "• Метод: кемекс / пуровер\n"
                "• Помол: средний\n"
                "• Навеска: 15 г на 250 мл\n"
                "• Температура: 90–91°C\n"
                "• Время: 4–4.5 мин\n\n"
                "_Натуральная колумбия с ягодной сладостью и чайной нежностью._"
            ),
            "price_1kg": 2863, "price_200g": 655,
        },
        "Колумбия Гонзало Кармона": {
            "roast": "F",
            "process": "Natural",
            "description": "Шоколад с ликёром, тропические фрукты, красные ягоды, цитрусы. Q 85,25",
            "recipe_e": None,
            "recipe_f": (
                "🫖 *Фильтр (Колумбия Гонзало Кармона):*\n"
                "• Метод: аэропресс / пуровер\n"
                "• Помол: средний\n"
                "• Навеска: 15 г на 250 мл\n"
                "• Температура: 90°C\n"
                "• Время: 4–5 мин\n\n"
                "_Шоколад с ликёром и манго — один из самых ярких натуральных лотов._"
            ),
            "price_1kg": 3500, "price_200g": 780,
        },
    },

    # ── СМЕСИ ───────────────────────────────────────────────────────────────
    "Смеси": {
        "КЛАССИКА": {
            "roast": "BE",
            "process": "Brazil Natural + India Robusta",
            "description": "Лесной орех, какао, изюм.",
            "recipe_e": (
                "☕ *Эспрессо (КЛАССИКА):*\n"
                "• Помол: мелкий (16–18 делений)\n"
                "• Навеска: 18–20 г\n"
                "• Выход: 36–42 г\n"
                "• Время: 25–30 сек\n"
                "• Температура: 93–94°C\n\n"
                "_Классический итальянский эспрессо — плотный, с горчинкой._"
            ),
            "recipe_f": None,
            "price_1kg": 1975, "price_200g": 425,
        },
        "БИТТЕР": {
            "roast": "BE",
            "process": "Brazil Natural + India Robusta",
            "description": "Тёмный шоколад, какао, цитрусы.",
            "recipe_e": (
                "☕ *Эспрессо (БИТТЕР):*\n"
                "• Помол: мелкий (16–18 делений)\n"
                "• Навеска: 18–20 г\n"
                "• Выход: 34–38 г\n"
                "• Время: 24–28 сек\n"
                "• Температура: 92–93°C\n\n"
                "_Горький шоколад и какао — для ценителей насыщенной горчинки._"
            ),
            "recipe_f": None,
            "price_1kg": 1910, "price_200g": 410,
        },
        "ХАНИ": {
            "roast": "BE",
            "process": "Brazil Natural + Ethiopia Natural",
            "description": "Молочный шоколад, цветочный мёд, цитрусы.",
            "recipe_e": (
                "☕ *Эспрессо (ХАНИ):*\n"
                "• Помол: мелкий (18–20 делений)\n"
                "• Навеска: 18 г\n"
                "• Выход: 36–40 г\n"
                "• Время: 25–30 сек\n"
                "• Температура: 93°C\n\n"
                "_Мёд и молочный шоколад — нежный блэнд. Идеален в латте._"
            ),
            "recipe_f": None,
            "price_1kg": 2015, "price_200g": 435,
        },
        "ВЕНЕЦИЯ": {
            "roast": "BE",
            "process": "Brazil Natural + Ethiopia Natural",
            "description": "Горький шоколад, тёмная карамель, специи.",
            "recipe_e": (
                "☕ *Эспрессо (ВЕНЕЦИЯ):*\n"
                "• Помол: мелкий (18–20 делений)\n"
                "• Навеска: 18 г\n"
                "• Выход: 36–40 г\n"
                "• Время: 25–29 сек\n"
                "• Температура: 93°C\n\n"
                "_Пряный шоколадный блэнд с глубокой карамелью._"
            ),
            "recipe_f": None,
            "price_1kg": 2015, "price_200g": 435,
        },
        "ЛУНГО": {
            "roast": "BF",
            "process": "Brazil Natural + Ethiopia Natural",
            "description": "Цитрусы, орехи, молочный шоколад.",
            "recipe_e": None,
            "recipe_f": (
                "🫖 *Фильтр / Лунго (ЛУНГО):*\n"
                "• Метод: кемекс / пуровер / лунго в рожке\n"
                "• Помол: средний\n"
                "• Навеска: 15 г на 250 мл\n"
                "• Температура: 92–93°C\n"
                "• Время: 3.5–4 мин\n\n"
                "_Блэнд под длинный кофе — цитрусовый, с ореховой базой._"
            ),
            "price_1kg": 2015, "price_200g": 435,
        },
    },
}

# Drip и Nespresso хранятся отдельно (фасовка иная)
DRIP_PRODUCTS = [
    {"name": "Бразилия Сертао Жёлтый Бурбон", "description": "Цитрусы, сухофрукты, тёмный шоколад, какао.", "series": "CHOCOLATE", "price_pack": 670, "price_unit": 67},
    {"name": "Гватемала Уетенанго", "description": "Зелёное яблоко, вишня, сухофрукты, миндаль.", "series": "CHOCOLATE", "price_pack": 670, "price_unit": 67},
    {"name": "Гватемала Финка Медина", "description": "Изюм, красное яблоко, мёд.", "series": "CHOCOLATE", "price_pack": 770, "price_unit": 81},
    {"name": "Коста-Рика Тарразу Ла Пастора", "description": "Цитрусы, красные ягоды, чёрный чай, тёмный шоколад.", "series": "CHOCOLATE", "price_pack": 670, "price_unit": 67},
    {"name": "Колумбия Питалито Без Кофеина", "description": "Красное яблоко, тростниковый сахар, миндаль.", "series": "CHOCOLATE", "price_pack": 670, "price_unit": 67},
    {"name": "Эфиопия Иргачиф (Drip)", "description": "Абрикос, миндаль, цветы, чай с бергамотом.", "series": "FLOWER", "price_pack": 710, "price_unit": 72},
    {"name": "Кения АА (Drip)", "description": "Красные ягоды, специи, малиновый джем, карамель.", "series": "FLOWER", "price_pack": 730, "price_unit": 75},
    {"name": "Колумбия Клаудиа Колменарес (Drip)", "description": "Сладкая выпечка, сухофрукты, цветы, лайм.", "series": "FRUIT", "price_pack": 810, "price_unit": 85},
    {"name": "Эфиопия Бомбе", "description": "Красный апельсин, персик, карамель, красные яблоки.", "series": "FRUIT", "price_pack": 760, "price_unit": 78},
    {"name": "Кения Моунт С", "description": "Зелёное яблоко, красные ягоды, карамель.", "series": "FRUIT", "price_pack": 630, "price_unit": 62},
    {"name": "Колумбия Супремо (Drip)", "description": "Зелёное яблоко, красные ягоды, карамель.", "series": "FRUIT", "price_pack": 710, "price_unit": 72},
    {"name": "Колумбия Ла Кумбре", "description": "Вишня, сушёное яблоко, миндаль.", "series": "FRUIT", "price_pack": 710, "price_unit": 72},
    {"name": "Колумбия Гонзало Кармона (Drip)", "description": "Шоколад с ликёром, тропические фрукты, красные ягоды.", "series": "FUNKI", "price_pack": 820, "price_unit": 86},
]

NESPRESSO_PRODUCTS = [
    {"name": "Бразилия Серрадо (Nespresso)", "description": "Сухофрукты, тёмный шоколад.", "series": "CHOCOLATE", "price_pack": 450, "price_unit": 74},
    {"name": "Бразилия Пибери", "description": "Косточковые фрукты, шоколад, цитрусовые.", "series": "CHOCOLATE", "price_pack": 450, "price_unit": 74},
    {"name": "Колумбия (Nespresso)", "description": "Карамель, яблоко, кешью.", "series": "FRUIT", "price_pack": 460, "price_unit": 75},
    {"name": "Эфиопия Сидамо (Nespresso)", "description": "Персик, лимон, чёрный чай.", "series": "FLOWER", "price_pack": 500, "price_unit": 82},
]


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
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            name         TEXT    NOT NULL,
            description  TEXT,
            recipe_e     TEXT,
            recipe_f     TEXT,
            roast_type   TEXT,
            process      TEXT,
            weight_g     INTEGER NOT NULL DEFAULT 1000,
            price        REAL    NOT NULL DEFAULT 0,
            stock        INTEGER NOT NULL DEFAULT 0,
            category_id  INTEGER REFERENCES categories(id),
            photo_url    TEXT
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
            discount   REAL DEFAULT 0,
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

    # Миграция users
    existing_cols = [row[1] for row in cur.execute("PRAGMA table_info(users)").fetchall()]
    for col, typ in [
        ("user_type", "TEXT DEFAULT 'individual'"),
        ("company_name", "TEXT"), ("inn", "TEXT"),
        ("legal_address", "TEXT"), ("actual_address", "TEXT"), ("email", "TEXT"),
    ]:
        if col not in existing_cols:
            cur.execute(f"ALTER TABLE users ADD COLUMN {col} {typ}")

    # Миграция products
    prod_cols = [row[1] for row in cur.execute("PRAGMA table_info(products)").fetchall()]
    for col, typ in [
        ("description", "TEXT"), ("recipe_e", "TEXT"), ("recipe_f", "TEXT"),
        ("roast_type", "TEXT"), ("process", "TEXT"), ("weight_g", "INTEGER DEFAULT 1000"),
        ("photo_url", "TEXT"),
    ]:
        if col not in prod_cols:
            cur.execute(f"ALTER TABLE products ADD COLUMN {col} {typ}")

    # Наполняем каталог если пустой
    cur.execute("SELECT COUNT(*) FROM categories")
    if cur.fetchone()[0] == 0:
        _seed_catalog(cur)

    con.commit()
    con.close()


def _seed_catalog(cur):
    """Заполняет категории и товары из CATALOG, DRIP_PRODUCTS, NESPRESSO_PRODUCTS."""
    # Корневые категории
    section_ids = {}
    for section in ["Моносорта", "Микролоты Black Edition", "Микролоты Борщ Edition", "Смеси", "Drip", "Nespresso"]:
        cur.execute("INSERT INTO categories (name, parent_id) VALUES (?, NULL)", (section,))
        section_ids[section] = cur.lastrowid

    # Зерновой кофе из CATALOG
    for section_name, products in CATALOG.items():
        parent_id = section_ids[section_name]
        for prod_name, info in products.items():
            # 1 кг
            cur.execute("""
                INSERT INTO products (name, description, recipe_e, recipe_f, roast_type, process,
                                      weight_g, price, stock, category_id)
                VALUES (?,?,?,?,?,?,1000,?,0,?)
            """, (
                prod_name + " 1 кг",
                info["description"], info["recipe_e"], info["recipe_f"],
                info["roast"], info["process"],
                info["price_1kg"], parent_id
            ))
            # 200 г
            cur.execute("""
                INSERT INTO products (name, description, recipe_e, recipe_f, roast_type, process,
                                      weight_g, price, stock, category_id)
                VALUES (?,?,?,?,?,?,200,?,0,?)
            """, (
                prod_name + " 200 г",
                info["description"], info["recipe_e"], info["recipe_f"],
                info["roast"], info["process"],
                info["price_200g"], parent_id
            ))

    # Drip
    drip_parent = section_ids["Drip"]
    for p in DRIP_PRODUCTS:
        recipe = (
            f"💧 *Как заварить дрип-пакет:*\n"
            f"1. Вскипятите воду, остудите до 92–94°C\n"
            f"2. Вскройте дрип-пакет, раскройте ушки и установите на кружку\n"
            f"3. Медленно налейте 30 мл воды — дайте настояться 30 сек\n"
            f"4. Добавьте ещё 170–200 мл воды в несколько заходов\n"
            f"5. Дождитесь полного стекания (2–3 мин)\n\n"
            f"☕ Объём: 200–250 мл. Серия: {p['series']}"
        )
        # Упаковка 8 шт
        cur.execute("""
            INSERT INTO products (name, description, recipe_e, recipe_f, roast_type, process,
                                  weight_g, price, stock, category_id)
            VALUES (?,?,NULL,?,?,?,8,?,0,?)
        """, (
            p["name"] + " (8 шт)",
            p["description"], recipe, "Drip", p["series"],
            p["price_pack"], drip_parent
        ))
        # Поштучно
        cur.execute("""
            INSERT INTO products (name, description, recipe_e, recipe_f, roast_type, process,
                                  weight_g, price, stock, category_id)
            VALUES (?,?,NULL,?,?,?,1,?,0,?)
        """, (
            p["name"] + " (1 шт)",
            p["description"], recipe, "Drip", p["series"],
            p["price_unit"], drip_parent
        ))

    # Nespresso
    nesp_parent = section_ids["Nespresso"]
    for p in NESPRESSO_PRODUCTS:
        recipe = (
            f"💊 *Как использовать капсулу Nespresso:*\n"
            f"1. Вставьте капсулу в машину стандарта Nespresso Original\n"
            f"2. Выберите режим эспрессо (40 мл) или лунго (110 мл)\n"
            f"3. Наслаждайтесь! Капсула совместима со всеми машинами Nespresso\n\n"
            f"☕ Серия: {p['series']}"
        )
        # Упаковка 10 шт
        cur.execute("""
            INSERT INTO products (name, description, recipe_e, recipe_f, roast_type, process,
                                  weight_g, price, stock, category_id)
            VALUES (?,?,NULL,?,?,?,10,?,0,?)
        """, (
            p["name"] + " (10 шт)",
            p["description"], recipe, "Nespresso", p["series"],
            p["price_pack"], nesp_parent
        ))
        # Поштучно
        cur.execute("""
            INSERT INTO products (name, description, recipe_e, recipe_f, roast_type, process,
                                  weight_g, price, stock, category_id)
            VALUES (?,?,NULL,?,?,?,1,?,0,?)
        """, (
            p["name"] + " (1 шт)",
            p["description"], recipe, "Nespresso", p["series"],
            p["price_unit"], nesp_parent
        ))


def get_db():
    con = sqlite3.connect("shop.db")
    con.row_factory = sqlite3.Row
    return con

def get_user(user_id: int):
    con = get_db()
    u = con.execute("SELECT * FROM users WHERE user_id = ?", (user_id,)).fetchone()
    con.close()
    return u

def save_user_individual(user_id, tg_name, name, phone):
    con = get_db()
    con.execute("""
        INSERT INTO users (user_id, tg_name, user_type, name, phone)
        VALUES (?,?,'individual',?,?)
        ON CONFLICT(user_id) DO UPDATE SET
            tg_name=excluded.tg_name, user_type='individual',
            name=excluded.name, phone=excluded.phone
    """, (user_id, tg_name, name, phone))
    con.commit(); con.close()

def save_user_company(user_id, tg_name, company_name, inn, legal_address, actual_address, phone, email):
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
    con.commit(); con.close()

def update_user_address(user_id, address):
    con = get_db()
    con.execute("UPDATE users SET address = ? WHERE user_id = ?", (address, user_id))
    con.commit(); con.close()

# ─── Расчёт скидки корзины ───────────────────────────────────────────────────
def calc_cart_totals(user_id: int):
    """
    Возвращает (items, subtotal, discount_pct, discount_amt, total, total_kg)
    Для Drip/Nespresso вес не учитывается в кг (они не кг-товары).
    Для зерна weight_g=1000 → 1 кг, weight_g=200 → 0.2 кг.
    """
    con = get_db()
    items = con.execute("""
        SELECT c.id as cart_id, c.quantity, p.id as product_id,
               p.name, p.price, p.weight_g, p.roast_type
        FROM cart c JOIN products p ON c.product_id = p.id
        WHERE c.user_id = ?
    """, (user_id,)).fetchall()
    con.close()

    subtotal = 0.0
    total_kg = 0.0
    for it in items:
        subtotal += it["price"] * it["quantity"]
        # Считаем вес только для зернового кофе (не Drip/Nespresso)
        if it["roast_type"] not in ("Drip", "Nespresso"):
            total_kg += (it["weight_g"] / 1000.0) * it["quantity"]

    disc_pct = get_discount(total_kg)
    discount_amt = subtotal * disc_pct
    total = subtotal - discount_amt
    return items, subtotal, disc_pct, discount_amt, total, total_kg

# ─── Клавиатуры ──────────────────────────────────────────────────────────────
main_keyboard = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="☕ Каталог"),      KeyboardButton(text="🛒 Корзина")],
        [KeyboardButton(text="⭐ Избранное"),    KeyboardButton(text="📋 Мои заказы")],
        [KeyboardButton(text="📦 Остатки"),      KeyboardButton(text="📞 Контакты")],
        [KeyboardButton(text="👤 Мой профиль")],
    ],
    resize_keyboard=True
)

user_type_keyboard = InlineKeyboardMarkup(inline_keyboard=[
    [InlineKeyboardButton(text="👤 Физическое лицо", callback_data="reg_individual")],
    [InlineKeyboardButton(text="🏢 Юридическое лицо", callback_data="reg_company")],
])

ROAST_EMOJI = {"E": "☕", "F": "🫖", "EF": "☕🫖", "BE": "☕", "BF": "🫖", "Drip": "💧", "Nespresso": "💊"}

def root_categories_keyboard():
    con = get_db()
    cats = con.execute("SELECT * FROM categories WHERE parent_id IS NULL").fetchall()
    con.close()
    icons = {"Моносорта": "🌍", "Микролоты Black Edition": "⚫", "Микролоты Борщ Edition": "🟣",
             "Смеси": "🎨", "Drip": "💧", "Nespresso": "💊"}
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text=f"{icons.get(c['name'], '☕')} {c['name']}",
            callback_data=f"cat_{c['id']}"
        )] for c in cats
    ])

def products_keyboard(category_id):
    con = get_db()
    products = con.execute(
        "SELECT * FROM products WHERE category_id = ? AND stock > 0 ORDER BY name",
        (category_id,)
    ).fetchall()
    cat = con.execute("SELECT * FROM categories WHERE id = ?", (category_id,)).fetchone()
    con.close()

    # Группируем по базовому названию (без " 1 кг" / " 200 г" / " (8 шт)" etc.)
    seen_base = {}
    for p in products:
        base = p["name"]
        for suffix in [" 1 кг", " 200 г", " (8 шт)", " (10 шт)", " (1 шт)"]:
            base = base.replace(suffix, "")
        if base not in seen_base:
            seen_base[base] = p  # первый вариант этого товара

    buttons = []
    for base, p in seen_base.items():
        roast = p["roast_type"] or ""
        emoji = ROAST_EMOJI.get(roast, "☕")
        buttons.append([InlineKeyboardButton(
            text=f"{emoji} {base[:45]}",
            callback_data=f"product_{p['id']}"
        )])
    buttons.append([InlineKeyboardButton(text="◀ Назад", callback_data="back_root")])
    return InlineKeyboardMarkup(inline_keyboard=buttons), len(products)

def product_card_keyboard(product_id, category_id, has_recipe_e, has_recipe_f):
    """Клавиатура карточки товара с кнопками фасовки, рецептов, фото."""
    con = get_db()
    p = con.execute("SELECT * FROM products WHERE id = ?", (product_id,)).fetchone()
    con.close()

    # Ищем альтернативные фасовки этого товара
    base_name = p["name"]
    for suffix in [" 1 кг", " 200 г", " (8 шт)", " (10 шт)", " (1 шт)"]:
        base_name = base_name.replace(suffix, "")

    con = get_db()
    variants = con.execute(
        "SELECT * FROM products WHERE name LIKE ? AND category_id = ? AND stock > 0 ORDER BY weight_g",
        (f"{base_name}%", category_id)
    ).fetchall()
    con.close()

    buttons = []

    # Кнопки фасовок
    if len(variants) > 1:
        size_row = []
        for v in variants:
            label = _variant_label(v)
            marker = "✅ " if v["id"] == product_id else ""
            size_row.append(InlineKeyboardButton(
                text=f"{marker}{label} — {v['price']:.0f} ₽",
                callback_data=f"product_{v['id']}"
            ))
        # По 2 кнопки в ряд
        for i in range(0, len(size_row), 2):
            buttons.append(size_row[i:i+2])

    # Кнопки действий
    action_row = [InlineKeyboardButton(text="➕ В корзину", callback_data=f"add_{product_id}")]
    if p["photo_url"]:
        action_row.append(InlineKeyboardButton(text="📸 Фото", callback_data=f"photo_{product_id}"))
    buttons.append(action_row)

    # Рецепты
    recipe_row = []
    if has_recipe_e:
        recipe_row.append(InlineKeyboardButton(text="☕ Рецепт эспрессо", callback_data=f"recipe_e_{product_id}"))
    if has_recipe_f:
        recipe_row.append(InlineKeyboardButton(text="🫖 Рецепт фильтр", callback_data=f"recipe_f_{product_id}"))
    if recipe_row:
        buttons.append(recipe_row)

    # Избранное и уведомление
    con2 = get_db()
    in_wish = con2.execute(
        "SELECT id FROM wishlist WHERE user_id = ? AND product_id = ?",
        (0, product_id)  # user_id подставляется в хендлере через отдельную функцию
    )
    con2.close()
    buttons.append([
        InlineKeyboardButton(text="⭐ В избранное", callback_data=f"wish_add_{product_id}"),
    ])
    buttons.append([InlineKeyboardButton(text="◀ Назад", callback_data=f"cat_{category_id}")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def _variant_label(v) -> str:
    if v["roast_type"] == "Drip":
        return f"{v['weight_g']} шт" if v["weight_g"] > 1 else "1 шт"
    if v["roast_type"] == "Nespresso":
        return f"{v['weight_g']} шт" if v["weight_g"] > 1 else "1 шт"
    return f"{v['weight_g']} г" if v["weight_g"] < 1000 else "1 кг"

def cart_keyboard(items):
    buttons = [[InlineKeyboardButton(
        text=f"❌ {it['name'][:35]}", callback_data=f"remove_{it['cart_id']}"
    )] for it in items]
    buttons.append([InlineKeyboardButton(text="🎁 Ввести промокод",    callback_data="enter_promo")])
    buttons.append([InlineKeyboardButton(text="✅ Оформить заказ",     callback_data="checkout")])
    buttons.append([InlineKeyboardButton(text="🗑 Очистить корзину",   callback_data="clear_cart")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

# ─── /start и регистрация ─────────────────────────────────────────────────────
@dp.message(CommandStart())
async def start_handler(message: Message, state: FSMContext):
    user = get_user(message.from_user.id)
    if user:
        display = user["company_name"] if user["user_type"] == "company" else user["name"]
        await message.answer(f"👋 С возвращением, *{display}*! ☕",
                             parse_mode="Markdown", reply_markup=main_keyboard)
    else:
        await message.answer(
            "👋 Добро пожаловать в *Roastberry Coffee*!\n\nВыберите тип аккаунта:",
            parse_mode="Markdown", reply_markup=user_type_keyboard
        )
        await state.set_state(RegStates.choosing_type)

@dp.callback_query(F.data == "reg_individual", RegStates.choosing_type)
async def reg_individual(callback: CallbackQuery, state: FSMContext):
    await state.update_data(user_type="individual")
    await callback.message.answer("👤 Введите ваше *имя*:", parse_mode="Markdown")
    await state.set_state(RegStates.waiting_name); await callback.answer()

@dp.callback_query(F.data == "reg_company", RegStates.choosing_type)
async def reg_company_cb(callback: CallbackQuery, state: FSMContext):
    await state.update_data(user_type="company")
    await callback.message.answer("🏢 Введите *наименование организации*:", parse_mode="Markdown")
    await state.set_state(RegStates.waiting_company); await callback.answer()

@dp.message(RegStates.waiting_name)
async def reg_name(message: Message, state: FSMContext):
    await state.update_data(name=message.text.strip())
    await message.answer("📱 Введите *номер телефона*:", parse_mode="Markdown")
    await state.set_state(RegStates.waiting_phone)

@dp.message(RegStates.waiting_phone)
async def reg_phone(message: Message, state: FSMContext):
    data = await state.get_data()
    if data.get("user_type") == "individual":
        save_user_individual(message.from_user.id, message.from_user.username or "",
                             data["name"], message.text.strip())
        await state.clear()
        await message.answer(f"✅ *{data['name']}*, регистрация завершена!\n\nДобро пожаловать ☕",
                             parse_mode="Markdown", reply_markup=main_keyboard)
    else:
        await state.update_data(phone=message.text.strip())
        await message.answer("📧 Введите *электронную почту*:", parse_mode="Markdown")
        await state.set_state(RegStates.waiting_email)

@dp.message(RegStates.waiting_company)
async def reg_company_name(message: Message, state: FSMContext):
    await state.update_data(company_name=message.text.strip())
    await message.answer("🔢 Введите *ИНН*:", parse_mode="Markdown")
    await state.set_state(RegStates.waiting_inn)

@dp.message(RegStates.waiting_inn)
async def reg_inn(message: Message, state: FSMContext):
    inn = message.text.strip()
    if not inn.isdigit() or len(inn) not in (10, 12):
        await message.answer("⚠️ ИНН должен содержать 10 или 12 цифр:"); return
    await state.update_data(inn=inn)
    await message.answer("🏢 Введите *юридический адрес*:", parse_mode="Markdown")
    await state.set_state(RegStates.waiting_legal_addr)

@dp.message(RegStates.waiting_legal_addr)
async def reg_legal(message: Message, state: FSMContext):
    await state.update_data(legal_address=message.text.strip())
    await message.answer("📍 Введите *фактический адрес*:", parse_mode="Markdown")
    await state.set_state(RegStates.waiting_actual_addr)

@dp.message(RegStates.waiting_actual_addr)
async def reg_actual(message: Message, state: FSMContext):
    await state.update_data(actual_address=message.text.strip())
    await message.answer("📱 Введите *телефон*:", parse_mode="Markdown")
    await state.set_state(RegStates.waiting_ur_phone)

@dp.message(RegStates.waiting_ur_phone)
async def reg_ur_phone(message: Message, state: FSMContext):
    await state.update_data(phone=message.text.strip())
    await message.answer("📧 Введите *электронную почту*:", parse_mode="Markdown")
    await state.set_state(RegStates.waiting_email)

@dp.message(RegStates.waiting_email)
async def reg_email(message: Message, state: FSMContext):
    data = await state.get_data()
    data["email"] = message.text.strip()
    save_user_company(
        message.from_user.id, message.from_user.username or "",
        data["company_name"], data["inn"], data["legal_address"],
        data["actual_address"], data["phone"], data["email"]
    )
    await state.clear()
    await message.answer(f"✅ *{data['company_name']}* — регистрация завершена! ☕",
                         parse_mode="Markdown", reply_markup=main_keyboard)

# ─── Профиль ─────────────────────────────────────────────────────────────────
@dp.message(F.text == "👤 Мой профиль")
async def profile_handler(message: Message):
    user = get_user(message.from_user.id)
    if not user:
        await message.answer("Вы не зарегистрированы. Нажмите /start"); return
    if user["user_type"] == "company":
        text = (f"🏢 *{user['company_name']}*\n"
                f"ИНН: {user['inn']}\n"
                f"📱 {user['phone']} | 📧 {user['email']}\n"
                f"Юр. адрес: {user['legal_address']}\n"
                f"Факт. адрес: {user['actual_address']}")
    else:
        text = (f"👤 *{user['name']}*\n"
                f"📱 {user['phone']}\n"
                f"🏠 {user['address'] or 'адрес не указан'}")
    await message.answer(text, parse_mode="Markdown")

# ─── Контакты ─────────────────────────────────────────────────────────────────
@dp.message(F.text == "📞 Контакты")
async def contacts_handler(message: Message):
    await message.answer(
        "📞 *Roastberry Coffee*\n\n"
        "🌐 roastberry.coffee\n"
        "📦 Доставка по всей России (СДЭК)\n\n"
        "💰 *Система скидок:*\n"
        "• от 10 кг зерна — скидка 10%\n"
        "• от 25 кг зерна — скидка 20%\n"
        "• от 100 кг — спецпрайс (свяжитесь с нами)",
        parse_mode="Markdown"
    )

# ─── Каталог ─────────────────────────────────────────────────────────────────
@dp.message(F.text == "☕ Каталог")
async def catalog_handler(message: Message):
    await message.answer("☕ *Каталог Roastberry*\n\nВыберите раздел:",
                         parse_mode="Markdown", reply_markup=root_categories_keyboard())

@dp.callback_query(F.data == "back_root")
async def back_root(callback: CallbackQuery):
    await callback.message.answer("☕ *Каталог Roastberry*\n\nВыберите раздел:",
                                  parse_mode="Markdown", reply_markup=root_categories_keyboard())
    await callback.answer()

@dp.callback_query(F.data.startswith("cat_"))
async def cat_handler(callback: CallbackQuery):
    cat_id = int(callback.data.split("_")[1])
    con = get_db()
    cat = con.execute("SELECT * FROM categories WHERE id = ?", (cat_id,)).fetchone()
    con.close()

    kb, count = products_keyboard(cat_id)
    if count == 0:
        await callback.message.answer(f"😔 В разделе «{cat['name']}» пока нет товаров в наличии.",
                                      reply_markup=root_categories_keyboard())
    else:
        desc = {
            "Моносорта": "Моносорта — кофе из одного региона и фермы.",
            "Микролоты Black Edition": "⚫ Премиальные микролоты — редкие партии с высоким Q-score.",
            "Микролоты Борщ Edition": "🟣 Эксклюзивная коллекция — необычные обработки и вкусовые профили.",
            "Смеси": "🎨 Авторские купажи — сбалансированные блэнды для эспрессо и фильтра.",
            "Drip": "💧 Дрип-пакеты — кофе в дорогу, без оборудования.",
            "Nespresso": "💊 Капсулы Nespresso Original — для машин Nespresso.",
        }.get(cat["name"], "")
        await callback.message.answer(
            f"*{cat['name']}*\n_{desc}_",
            parse_mode="Markdown", reply_markup=kb
        )
    await callback.answer()

# ─── Карточка товара ─────────────────────────────────────────────────────────
@dp.callback_query(F.data.startswith("product_"))
async def product_handler(callback: CallbackQuery):
    product_id = int(callback.data.split("_")[1])
    user_id = callback.from_user.id
    con = get_db()
    p = con.execute("SELECT * FROM products WHERE id = ?", (product_id,)).fetchone()
    if not p:
        con.close(); await callback.answer("Товар не найден."); return

    # Проверяем — заказывал ли клиент этот товар (или похожий по базовому имени)
    base_name = p["name"]
    for sfx in [" 1 кг", " 200 г", " (8 шт)", " (10 шт)", " (1 шт)"]:
        base_name = base_name.replace(sfx, "")
    ordered_before = con.execute("""
        SELECT COUNT(*) FROM order_items oi
        JOIN products pr ON oi.product_id = pr.id
        JOIN orders o ON oi.order_id = o.id
        WHERE o.user_id = ? AND pr.name LIKE ? AND o.status != 'cancelled'
    """, (user_id, f"{base_name}%")).fetchone()[0]
    con.close()

    roast_labels = {
        "E": "☕ Эспрессо", "F": "🫖 Фильтр", "EF": "☕🫖 Эспрессо и Фильтр",
        "BE": "☕ Блэнд эспрессо", "BF": "🫖 Блэнд фильтр",
        "Drip": "💧 Drip-пакет", "Nespresso": "💊 Nespresso"
    }
    roast_str = roast_labels.get(p["roast_type"], p["roast_type"] or "")
    weight_str = _variant_label(p)

    # Пометки
    tag = p["tag"] if "tag" in p.keys() else ""
    prev_price = p["prev_price"] if "prev_price" in p.keys() else 0
    badges = []
    if ordered_before:
        badges.append("🔁 Вы уже заказывали")
    tag_label = _tag_str(tag or "", prev_price or 0, p["price"]) if tag else ""
    if tag_label:
        badges.append(tag_label)

    text = ""
    if badges:
        text += " | ".join(badges) + "\n\n"
    text += f"*{p['name']}*\n\n"
    if p["description"]:
        text += f"🍫 {p['description']}\n\n"
    text += f"🔥 Обжарка: {roast_str}\n"
    if p["process"] and p["roast_type"] not in ("Drip", "Nespresso"):
        text += f"⚙️ Обработка: {p['process']}\n"
    text += f"📦 Фасовка: {weight_str}\n"
    text += f"💰 Цена: *{p['price']:.0f} ₽*"
    if p["stock"] == 0:
        text += "\n\n❌ *Нет в наличии*"
    elif p["stock"] <= 5:
        text += f"\n⚠️ Осталось: {p['stock']}"

    # Если нет в наличии — кнопка «уведомить»
    if p["stock"] == 0:
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔔 Уведомить когда появится",
                                  callback_data=f"notify_{product_id}")],
            [InlineKeyboardButton(text="⭐ В избранное", callback_data=f"wish_add_{product_id}")],
            [InlineKeyboardButton(text="◀ Назад", callback_data=f"cat_{p['category_id']}")],
        ])
    else:
        kb = product_card_keyboard(
            product_id, p["category_id"],
            has_recipe_e=bool(p["recipe_e"]),
            has_recipe_f=bool(p["recipe_f"])
        )

    if p["photo_url"]:
        await callback.message.answer_photo(photo=p["photo_url"], caption=text,
                                            parse_mode="Markdown", reply_markup=kb)
    else:
        await callback.message.answer(text, parse_mode="Markdown", reply_markup=kb)
    await callback.answer()

# ─── Рецепты ─────────────────────────────────────────────────────────────────
@dp.callback_query(F.data.startswith("recipe_e_"))
async def recipe_e_handler(callback: CallbackQuery):
    product_id = int(callback.data.split("_")[2])
    con = get_db()
    p = con.execute("SELECT recipe_e, name FROM products WHERE id = ?", (product_id,)).fetchone()
    con.close()
    if p and p["recipe_e"]:
        await callback.message.answer(p["recipe_e"], parse_mode="Markdown")
    await callback.answer()

@dp.callback_query(F.data.startswith("recipe_f_"))
async def recipe_f_handler(callback: CallbackQuery):
    product_id = int(callback.data.split("_")[2])
    con = get_db()
    p = con.execute("SELECT recipe_f, name FROM products WHERE id = ?", (product_id,)).fetchone()
    con.close()
    if p and p["recipe_f"]:
        await callback.message.answer(p["recipe_f"], parse_mode="Markdown")
    await callback.answer()

@dp.callback_query(F.data.startswith("photo_"))
async def photo_handler(callback: CallbackQuery):
    product_id = int(callback.data.split("_")[1])
    con = get_db()
    p = con.execute("SELECT photo_url, name FROM products WHERE id = ?", (product_id,)).fetchone()
    con.close()
    if p and p["photo_url"]:
        await callback.message.answer_photo(photo=p["photo_url"], caption=f"📸 {p['name']}")
    else:
        await callback.answer("Фото пока не добавлено.", show_alert=False)
    await callback.answer()

# ─── Корзина ─────────────────────────────────────────────────────────────────
@dp.callback_query(F.data.startswith("add_"))
async def add_to_cart(callback: CallbackQuery):
    product_id = int(callback.data.split("_")[1])
    user_id = callback.from_user.id
    con = get_db()
    p = con.execute("SELECT * FROM products WHERE id = ?", (product_id,)).fetchone()
    if not p or p["stock"] <= 0:
        await callback.answer("😔 Товара нет в наличии.", show_alert=True)
        con.close(); return
    existing = con.execute(
        "SELECT * FROM cart WHERE user_id = ? AND product_id = ?", (user_id, product_id)
    ).fetchone()
    if existing:
        con.execute("UPDATE cart SET quantity = quantity + 1 WHERE id = ?", (existing["id"],))
    else:
        con.execute("INSERT INTO cart (user_id, product_id, quantity) VALUES (?,?,1)",
                    (user_id, product_id))
    con.commit(); con.close()
    await callback.answer(f"✅ «{p['name'][:30]}» добавлен в корзину!")

@dp.message(F.text == "🛒 Корзина")
async def cart_btn(message: Message):
    await show_cart(message, message.from_user.id)

async def show_cart(message: Message, user_id: int):
    items, subtotal, disc_pct, discount_amt, total, total_kg = calc_cart_totals(user_id)
    if not items:
        await message.answer("🛒 Ваша корзина пуста."); return

    lines = ["🛒 *Ваша корзина:*\n"]
    for it in items:
        unit = _variant_label(it) if False else ""
        lines.append(f"• {it['name'][:40]}\n  {it['quantity']} × {it['price']:.0f} ₽ = {it['price']*it['quantity']:.0f} ₽")

    lines.append(f"\n💰 Сумма: {subtotal:.0f} ₽")
    if disc_pct > 0:
        lines.append(f"📦 Зерно в корзине: {total_kg:.1f} кг")
        lines.append(f"🎁 Скидка {int(disc_pct*100)}%: −{discount_amt:.0f} ₽")
        lines.append(f"✅ *Итого: {total:.0f} ₽*")
    else:
        if total_kg > 0:
            need = 10 - total_kg
            lines.append(f"\n💡 До скидки 10% ещё *{need:.1f} кг* зерна")
        lines.append(f"\n💰 *Итого: {total:.0f} ₽*")

    await message.answer("\n".join(lines), parse_mode="Markdown", reply_markup=cart_keyboard(items))

@dp.callback_query(F.data.startswith("remove_"))
async def remove_from_cart(callback: CallbackQuery):
    con = get_db()
    con.execute("DELETE FROM cart WHERE id = ?", (int(callback.data.split("_")[1]),))
    con.commit(); con.close()
    await callback.answer("Товар удалён.")
    await show_cart(callback.message, callback.from_user.id)

@dp.callback_query(F.data == "clear_cart")
async def clear_cart(callback: CallbackQuery):
    con = get_db()
    con.execute("DELETE FROM cart WHERE user_id = ?", (callback.from_user.id,))
    con.commit(); con.close()
    await callback.answer("Корзина очищена.")
    await callback.message.answer("🛒 Корзина очищена.")

# ─── Оформление заказа ───────────────────────────────────────────────────────
def _user_display(user) -> str:
    return user["company_name"] if user["user_type"] == "company" else user["name"]

def _user_addr(user) -> str:
    """Возвращает адрес из профиля — для юрлица actual_address, для физлица address."""
    addr = None
    try:
        addr = user["address"]
    except Exception:
        pass
    if not addr:
        try:
            addr = user["actual_address"]
        except Exception:
            pass
    return addr or ""

@dp.callback_query(F.data == "checkout")
async def checkout_start(callback: CallbackQuery, state: FSMContext):
    user = get_user(callback.from_user.id)
    if user and user["name"] and user["phone"]:
        display = _user_display(user)
        addr = _user_addr(user)
        await callback.message.answer(
            f"📋 *Использовать сохранённые данные?*\n\n"
            f"👤 {display}\n📱 {user['phone']}\n🏠 {addr or 'адрес не указан'}",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="✅ Да, использовать", callback_data="checkout_saved")],
                [InlineKeyboardButton(text="✏️ Ввести новые данные", callback_data="checkout_new")],
            ])
        )
        await state.set_state(OrderStates.confirm_profile)
    else:
        await callback.message.answer("📝 Введите ваше *имя*:", parse_mode="Markdown")
        await state.set_state(OrderStates.waiting_name)
    await callback.answer()

@dp.callback_query(F.data == "checkout_saved", OrderStates.confirm_profile)
async def checkout_saved(callback: CallbackQuery, state: FSMContext):
    user = get_user(callback.from_user.id)
    display = _user_display(user)
    addr = _user_addr(user)
    await state.update_data(name=display, phone=user["phone"], address=addr)
    if addr:
        await create_order(callback.message, callback.from_user.id, state)
    else:
        await callback.message.answer("🏠 Введите *адрес доставки*:", parse_mode="Markdown")
        await state.set_state(OrderStates.waiting_address)
    await callback.answer()

@dp.callback_query(F.data == "checkout_new", OrderStates.confirm_profile)
async def checkout_new(callback: CallbackQuery, state: FSMContext):
    await callback.message.answer("📝 Введите ваше *имя*:", parse_mode="Markdown")
    await state.set_state(OrderStates.waiting_name); await callback.answer()

@dp.message(OrderStates.waiting_name)
async def order_name(message: Message, state: FSMContext):
    await state.update_data(name=message.text.strip())
    await message.answer("📱 Введите *телефон*:", parse_mode="Markdown")
    await state.set_state(OrderStates.waiting_phone)

@dp.message(OrderStates.waiting_phone)
async def order_phone(message: Message, state: FSMContext):
    await state.update_data(phone=message.text.strip())
    await message.answer("🏠 Введите *адрес доставки*:", parse_mode="Markdown")
    await state.set_state(OrderStates.waiting_address)

@dp.message(OrderStates.waiting_address)
async def order_address(message: Message, state: FSMContext):
    await state.update_data(address=message.text.strip())
    await create_order(message, message.from_user.id, state)

async def create_order(message: Message, user_id: int, state: FSMContext):
    data = await state.get_data()
    items, subtotal, disc_pct, discount_amt, total, total_kg = calc_cart_totals(user_id)
    if not items:
        await message.answer("Корзина пуста."); await state.clear(); return

    # Промокод поверх объёмной скидки
    promo_code = data.get("promo_code", "")
    promo_discount_pct = data.get("promo_discount", 0) / 100 if data.get("promo_discount") else 0
    if promo_discount_pct > 0:
        promo_amt = total * promo_discount_pct
        total = total - promo_amt
        discount_amt += promo_amt
    else:
        promo_amt = 0

    con = get_db()
    cur = con.execute(
        "INSERT INTO orders (user_id, name, phone, address, total, discount, promo_code) VALUES (?,?,?,?,?,?,?)",
        (user_id, data["name"], data["phone"], data["address"], total, discount_amt, promo_code)
    )
    order_id = cur.lastrowid
    for it in items:
        price_with_disc = it["price"] * (1 - disc_pct) * (1 - promo_discount_pct)
        con.execute(
            "INSERT INTO order_items (order_id, product_id, quantity, price) VALUES (?,?,?,?)",
            (order_id, it["product_id"], it["quantity"], price_with_disc)
        )
        con.execute("UPDATE products SET stock = stock - ? WHERE id = ?",
                    (it["quantity"], it["product_id"]))

    # Списываем использование промокода
    if promo_code:
        con.execute("""
            UPDATE promo_codes SET uses_left = uses_left - 1
            WHERE code = ? AND uses_left > 0 AND uses_left < 999999
        """, (promo_code,))

    con.execute("DELETE FROM cart WHERE user_id = ?", (user_id,))
    con.commit(); con.close()
    update_user_address(user_id, data["address"])

    lines = [f"✅ *Заказ №{order_id} оформлен!*\n"]
    for it in items:
        lines.append(f"• {it['name'][:40]}\n  {it['quantity']} × {it['price']:.0f} ₽")
    lines.append(f"\n💰 Сумма: {subtotal:.0f} ₽")
    if disc_pct > 0:
        lines.append(f"📦 Оптовая скидка {int(disc_pct*100)}%: −{subtotal * disc_pct:.0f} ₽")
    if promo_amt > 0:
        lines.append(f"🎁 Промокод {promo_code} ({data['promo_discount']:.0f}%): −{promo_amt:.0f} ₽")
    if discount_amt > 0:
        lines.append(f"✅ *Итого: {total:.0f} ₽*")
    else:
        lines.append(f"💰 *Итого: {total:.0f} ₽*")
    lines.append(f"\n👤 {data['name']}  📱 {data['phone']}\n🏠 {data['address']}")
    lines.append("\n📦 Мы свяжемся с вами для подтверждения!")

    await message.answer("\n".join(lines), parse_mode="Markdown", reply_markup=main_keyboard)
    await state.clear()
    await notify_new_order(bot, order_id, data["name"], total)

# ─── Остатки ─────────────────────────────────────────────────────────────────
@dp.message(F.text == "📦 Остатки")
async def stock_handler(message: Message):
    con = get_db()
    cats = con.execute("SELECT * FROM categories WHERE parent_id IS NULL").fetchall()
    lines = ["📦 *Остатки по складу:*\n"]
    for cat in cats:
        products = con.execute(
            "SELECT name, stock FROM products WHERE category_id = ? ORDER BY name", (cat["id"],)
        ).fetchall()
        total = sum(p["stock"] for p in products)
        lines.append(f"*{cat['name']}* — {total} ед.")
        for p in products:
            e = "✅" if p["stock"] > 10 else ("⚠️" if p["stock"] > 0 else "❌")
            lines.append(f"  {e} {p['name'][:45]}: *{p['stock']}*")
        lines.append("")
    con.close()
    text = "\n".join(lines)
    for i in range(0, len(text), 4000):
        await message.answer(text[i:i+4000], parse_mode="Markdown")


# ─── Пометки товаров — теги ───────────────────────────────────────────────────
TAG_LABELS = {"NEW": "🆕 Новинка", "EXPECTED": "⏳ Ожидается", "SALE": "📉 Снижена цена"}

def _tag_str(tag: str, prev_price: float = 0, price: float = 0) -> str:
    if tag == "SALE" and prev_price and prev_price != price:
        return f"📉 Цена снижена (было {prev_price:.0f} ₽)"
    return TAG_LABELS.get(tag, "")

# ─── Избранное ────────────────────────────────────────────────────────────────
@dp.message(F.text == "⭐ Избранное")
async def wishlist_handler(message: Message):
    user_id = message.from_user.id
    con = get_db()
    items = con.execute("""
        SELECT p.id, p.name, p.price, p.stock, p.tag, p.prev_price
        FROM wishlist w JOIN products p ON w.product_id = p.id
        WHERE w.user_id = ?
    """, (user_id,)).fetchall()
    con.close()
    if not items:
        await message.answer("⭐ Ваш список избранного пуст.\n\nДобавляйте товары кнопкой ⭐ В избранное в карточке товара.")
        return
    buttons = []
    for it in items:
        status = "✅" if it["stock"] > 0 else "❌"
        tag = _tag_str(it["tag"] or "", it["prev_price"] or 0, it["price"])
        tag_str = f" {tag}" if tag else ""
        buttons.append([InlineKeyboardButton(
            text=f"{status} {it['name'][:35]} — {it['price']:.0f} ₽{tag_str}",
            callback_data=f"product_{it['id']}"
        )])
        buttons.append([InlineKeyboardButton(
            text=f"❌ Убрать из избранного",
            callback_data=f"wish_del_{it['id']}"
        )])
    await message.answer("⭐ *Избранное:*", parse_mode="Markdown",
                         reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))

@dp.callback_query(F.data.startswith("wish_add_"))
async def wish_add(callback: CallbackQuery):
    product_id = int(callback.data.split("_")[2])
    user_id = callback.from_user.id
    con = get_db()
    try:
        con.execute("INSERT OR IGNORE INTO wishlist (user_id, product_id) VALUES (?,?)",
                    (user_id, product_id))
        con.commit()
        await callback.answer("⭐ Добавлено в избранное!")
    except Exception:
        await callback.answer("Уже в избранном.")
    con.close()

@dp.callback_query(F.data.startswith("wish_del_"))
async def wish_del(callback: CallbackQuery):
    product_id = int(callback.data.split("_")[2])
    con = get_db()
    con.execute("DELETE FROM wishlist WHERE user_id = ? AND product_id = ?",
                (callback.from_user.id, product_id))
    con.commit(); con.close()
    await callback.answer("Убрано из избранного.")
    await wishlist_handler(callback.message)

# ─── Уведомить когда появится ─────────────────────────────────────────────────
@dp.callback_query(F.data.startswith("notify_"))
async def notify_available(callback: CallbackQuery):
    product_id = int(callback.data.split("_")[1])
    user_id = callback.from_user.id
    con = get_db()
    con.execute("INSERT OR IGNORE INTO notify_when_available (user_id, product_id) VALUES (?,?)",
                (user_id, product_id))
    con.commit(); con.close()
    await callback.answer("🔔 Уведомим когда появится!", show_alert=True)

# ─── История заказов ──────────────────────────────────────────────────────────
@dp.message(F.text == "📋 Мои заказы")
async def my_orders_handler(message: Message):
    user_id = message.from_user.id
    con = get_db()
    orders = con.execute("""
        SELECT id, created_at, total, status FROM orders
        WHERE user_id = ? ORDER BY created_at DESC LIMIT 15
    """, (user_id,)).fetchall()
    con.close()
    if not orders:
        await message.answer("📋 У вас пока нет заказов."); return
    status_e = {"new": "🆕", "confirmed": "✅", "done": "📦", "cancelled": "❌"}
    buttons = [[InlineKeyboardButton(
        text=f"{status_e.get(o['status'],'?')} №{o['id']} от {o['created_at'][:10]} — {o['total']:.0f} ₽",
        callback_data=f"myorder_{o['id']}"
    )] for o in orders]
    await message.answer("📋 *Ваши заказы:*", parse_mode="Markdown",
                         reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))

@dp.callback_query(F.data.startswith("myorder_"))
async def my_order_detail(callback: CallbackQuery):
    order_id = int(callback.data.split("_")[1])
    user_id = callback.from_user.id
    con = get_db()
    o = con.execute("SELECT * FROM orders WHERE id = ? AND user_id = ?",
                    (order_id, user_id)).fetchone()
    if not o:
        await callback.answer("Заказ не найден."); return
    items = con.execute("""
        SELECT oi.quantity, oi.price, p.id as product_id, p.name, p.stock
        FROM order_items oi JOIN products p ON oi.product_id = p.id
        WHERE oi.order_id = ?
    """, (order_id,)).fetchall()
    con.close()
    status_e = {"new": "🆕 Новый", "confirmed": "✅ Подтверждён",
                "done": "📦 Выполнен", "cancelled": "❌ Отменён"}
    lines = [f"📋 *Заказ №{o['id']}*  {status_e.get(o['status'],'')}",
             f"📅 {o['created_at'][:16]}", ""]
    for it in items:
        lines.append(f"• {it['name'][:40]}")
        lines.append(f"  {it['quantity']} × {it['price']:.0f} ₽ = {it['quantity']*it['price']:.0f} ₽")
    lines.append(f"\n💰 Итого: *{o['total']:.0f} ₽*")
    if o["discount"]:
        lines.append(f"🎁 Скидка: {o['discount']:.0f} ₽")
    await callback.message.answer(
        "\n".join(lines), parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔄 Повторить заказ", callback_data=f"repeat_{order_id}")],
        ])
    )
    await callback.answer()

@dp.callback_query(F.data.startswith("repeat_"))
async def repeat_order(callback: CallbackQuery):
    order_id = int(callback.data.split("_")[1])
    user_id = callback.from_user.id
    con = get_db()
    items = con.execute("""
        SELECT oi.product_id, oi.quantity, p.name, p.stock
        FROM order_items oi JOIN products p ON oi.product_id = p.id
        WHERE oi.order_id = ? AND p.stock > 0
    """, (order_id,)).fetchall()
    if not items:
        await callback.answer("😔 Все товары из этого заказа сейчас не в наличии.", show_alert=True)
        con.close(); return
    # Очищаем корзину и добавляем товары
    con.execute("DELETE FROM cart WHERE user_id = ?", (user_id,))
    added, skipped = [], []
    for it in items:
        con.execute("INSERT INTO cart (user_id, product_id, quantity) VALUES (?,?,?)",
                    (user_id, it["product_id"], it["quantity"]))
        added.append(it["name"])
    con.commit(); con.close()
    await callback.answer(f"✅ {len(added)} товаров добавлено в корзину!")
    await show_cart(callback.message, user_id)

# ─── Промокод при оформлении ──────────────────────────────────────────────────
class PromoState(StatesGroup):
    waiting_promo = State()

@dp.callback_query(F.data == "enter_promo")
async def enter_promo_start(callback: CallbackQuery, state: FSMContext):
    await callback.message.answer("🎁 Введите промокод:")
    await state.set_state(PromoState.waiting_promo); await callback.answer()

@dp.message(PromoState.waiting_promo)
async def enter_promo_code(message: Message, state: FSMContext):
    code = message.text.strip().upper()
    user_id = message.from_user.id
    con = get_db()
    promo = con.execute("""
        SELECT * FROM promo_codes
        WHERE code = ? AND is_active = 1 AND uses_left > 0
        AND (user_id IS NULL OR user_id = ?)
    """, (code, user_id)).fetchone()
    con.close()
    if not promo:
        await message.answer("❌ Промокод не найден или уже использован.")
        await state.clear(); return
    await state.update_data(promo_code=code, promo_discount=promo["discount"])
    await state.set_state(OrderStates.confirm_profile)
    await message.answer(
        f"✅ Промокод *{code}* применён — скидка *{promo['discount']:.0f}%*\n\nТеперь оформите заказ.",
        parse_mode="Markdown"
    )
    await state.clear()

# ─── Загрузка остатков из 1С ─────────────────────────────────────────────────
STOCK_MAP = [
    # Моносорта 1 кг
    ('Бразилия Серрадо Дарк% 1 кг',              109),
    ('Бразилия Серрадо 1 кг',                    631),
    ('Бразилия Сан Рафаель% 1 кг',               104),
    ('Бразилия Серрадо Жёлтый Бурбон% 1 кг',     22),
    ('Уганда Вугар Элгон% 1 кг',                  10),
    ('Уганда Рувензор% 1 кг',                     21),
    ('Кения АА% 1 кг',                            17),
    ('Кения АБ Центральная провинция% 1 кг',        1),
    ('Колумбия Андино% 1 кг',                     18),
    ('Гватемала Декаф% 1 кг',                      6),
    ('Гондурас Сан Николас% 1 кг',                16),
    ('Перу Монте Верде% 1 кг',                     4),
    ('Руанда Мутетели% 1 кг',                      2),
    ('Танзания АА% 1 кг',                          3),
    ('Эфиопия Лиму гр.2% 1 кг',                  14),
    ('Эфиопия Сидамо гр.2 Гуджи% 1 кг',           3),
    ('Эфиопия Иргачиф гр.4% 1 кг',               17),
    ('Эфиопия Milk% 1 кг',                        17),
    ('Эфиопия Челчеле гр.1% 1 кг',                4),
    # Смеси 1 кг
    ('БИТТЕР% 1 кг',                               7),
    ('ВЕНЕЦИЯ% 1 кг',                             16),
    ('КЛАССИКА% 1 кг',                            93),
    ('ХАНИ% 1 кг',                                24),
    ('ЛУНГО% 1 кг',                                9),
    # Black Edition 1 кг
    ('Эфиопия Adadо% 1 кг',                        4),
    ('Эфиопия Арича гр.1% 1 кг',                   5),
    ('Эфиопия Чelelекту гр.1% 1 кг',               4),
    # Борщ Edition 1 кг
    ('Колумбия Гонзало Кармона% 1 кг',             5),
    # Моносорта 200 г
    ('Бразилия Серрадо 200 г',                    19),
    ('Бразилия Серрадо Дарк% 200 г',              12),
    ('Бразилия Сан Рафаель% 200 г',               25),
    ('Гватемала Декаф% 200 г',                     6),
    ('Гондурас Сан Николас% 200 г',               24),
    ('Кения АА% 200 г',                           11),
    ('Кения АБ Центральная% 200 г',                9),
    ('Колумбия Андино% 200 г',                    24),
    ('Руанда Мутетели% 200 г',                    15),
    ('Танзания АА% 200 г',                        13),
    ('Уганда Рувензор% 200 г',                     4),
    ('Эфиопия Иргачиф гр.4% 200 г',              12),
    ('Эфиопия Лиму гр.2% 200 г',                  8),
    ('Эфиопия Сидамо гр.2 Гуджи% 200 г',          3),
    ('Эфиопия Челчеле гр.1% 200 г',               7),
    # Смеси 200 г
    ('БИТТЕР% 200 г',                             11),
    ('ВЕНЕЦИЯ% 200 г',                             9),
    ('КЛАССИКА% 200 г',                           33),
    ('ХАНИ% 200 г',                               18),
    ('ЛУНГО% 200 г',                               9),
    # Black Edition 200 г
    ('Эфиопия Adadо% 200 г',                      13),
    ('Эфиопия Белойя гр.2% 200 г',                 7),
    ('Эфиопия Чelelекту гр.1% 200 г',             10),
    ('Эфиопия Челчеле гр.1% 200 г',               7),
    # Борщ Edition 200 г
    ('Колумбия Гонзало Кармона% 200 г',           12),
    ('Колумбия Хайро Арсила% 200 г',              16),
    ('Экваториальный блэнд% 200 г',                3),
    ('Эфиопия Арича гр.1% 200 г',                 11),
    # Drip 8 шт
    ('Колумбия Питалито% (8 шт)',                   4),
    ('Гватемала Уетенанго% (8 шт)',                71),
    ('Коста-Рика Тарразу% (8 шт)',                 76),
    ('Кения Моунт С% (8 шт)',                      13),
    ('Колумбия Супремо% (8 шт)',                    7),
    ('Колумбия Клаудиа Колменарес% (8 шт)',        95),
    ('Эфиопия Бомбе% (8 шт)',                      74),
    # Drip 1 шт
    ('Кения АА (Drip)% (1 шт)',                   446),
    ('Коста-Рика Тарразу% (1 шт)',                 27),
    ('Колумбия Клаудиа Колменарес% (1 шт)',        29),
    ('Гватемала Уетенанго% (1 шт)',                31),
    ('Эфиопия Бомбе% (1 шт)',                      33),
    ('Эфиопия Иргачиф% (1 шт)',                   292),
]

@dp.message(Command("loadstock"))
async def load_stock_handler(message: Message):
    from admin import ADMIN_IDS
    if message.from_user.id not in ADMIN_IDS:
        return
    con = get_db()
    updated, not_found = [], []
    for pattern, stock in STOCK_MAP:
        rows = con.execute(
            "SELECT id, name FROM products WHERE name LIKE ?", (pattern,)
        ).fetchall()
        if rows:
            for row in rows:
                con.execute("UPDATE products SET stock = ? WHERE id = ?", (stock, row["id"]))
                updated.append(row["name"])
        else:
            not_found.append(pattern.replace('%', ''))
    con.commit(); con.close()

    text = f"✅ Остатки загружены: *{len(updated)}* позиций"
    if not_found:
        text += f"\n\n❌ Не найдено ({len(not_found)}):\n"
        text += "\n".join(f"• {n}" for n in not_found)
    await message.answer(text, parse_mode="Markdown")

# ─── Сброс ───────────────────────────────────────────────────────────────────
@dp.message(Command("reset"))
async def reset_handler(message: Message, state: FSMContext):
    con = get_db()
    con.execute("DELETE FROM users WHERE user_id = ?", (message.from_user.id,))
    con.commit(); con.close()
    await state.clear()
    await message.answer("🗑 Регистрация удалена. Нажми /start чтобы начать заново.")

# ─── Запуск ───────────────────────────────────────────────────────────────────
async def main():
    init_db()
    register_admin_handlers(dp, bot)
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
