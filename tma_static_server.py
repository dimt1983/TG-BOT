"""
tma_static_server.py — раздача Mini App статики на том же порту что и healthcheck.

GET /         → healthcheck "OK"
GET /sync     → снимок БД (Bearer-токен)
GET /tma/*    → статика из ./tma_static/
GET /tma/api/my_orders      → история заказов пользователя
GET /tma/api/admin/check    → проверка прав администратора
GET /tma/api/admin/orders   → список заказов (только для admin)
GET /tma/api/admin/order/ID → детали заказа (только для admin)
GET /tma/api/admin/users    → список клиентов (только для admin)
GET /tma/api/admin/user/ID  → профиль клиента + его заказы (только для admin)
GET /tma/api/admin/products → каталог (прокси на VPS Shop Admin API)
GET /tma/api/chat/ORDER_ID  → сообщения чата поддержки
POST /tma/api/order                  → создать заказ
POST /tma/api/send_kp               → отправить КП аренды в личку через бот
POST /tma/api/admin/order/ID/status → сменить статус заказа (только для admin)
POST /tma/api/admin/product/ID       → правка товара (прокси PATCH на VPS)
POST /tma/api/admin/product/ID/photo → загрузка фото (прокси на VPS)
POST /tma/api/chat/ORDER_ID         → отправить сообщение в чат
"""
import os
import re
import json
import threading
import mimetypes
import sqlite3
import asyncio
import hmac
import hashlib
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import unquote, parse_qsl, urlparse, parse_qs, quote
import urllib.request
import urllib.error

PORT = int(os.environ.get("PORT", 10000))
TMA_ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "tma_static")
PRICE_SYNC_ENABLED = os.environ.get("BISHOP_PRICE_SYNC", "1") != "0"

DB_PATH = os.environ.get("DB_PATH", "/app/data/shop.db")
API_ORDERS_TOKEN = os.environ.get("API_ORDERS_TOKEN", "")

# Администраторы TMA: user_id из Telegram.
# Переопределяется через env TMA_ADMIN_IDS (через запятую).
_raw_admin = os.environ.get("TMA_ADMIN_IDS", "466755177")
ADMIN_IDS = {int(x.strip()) for x in _raw_admin.split(",") if x.strip().isdigit()}

# КП аренды — отправляется клиенту по нажатию кнопки в карусели.
# Сначала пробуем локальный файл из репо (BOT_TG/kp/), потом — VPS-расположение
# для случая когда бот живёт рядом с папкой Прайсы (старый сетап).
_BOT_DIR = os.path.dirname(os.path.abspath(__file__))
_KP_CANDIDATES = [
    os.path.join(_BOT_DIR, "kp", "Roastberry_КП_Аренда.pdf"),
    os.path.normpath(os.path.join(_BOT_DIR, "..", "Прайсы", "чистовики",
                                   "Roastberry_КП_Аренда.pdf")),
]
KP_PDF_PATH = next((p for p in _KP_CANDIDATES if os.path.isfile(p)),
                   _KP_CANDIDATES[0])

# Допустимые статусы заказа для смены через adminAPI
ORDER_STATUSES_VALID = {"confirmed", "shipped", "done", "cancelled"}

# Прокси на Shop Admin API (живёт на VPS рядом с Bishop'ом, пишет в products.json + git push).
SHOP_ADMIN_API_URL = os.environ.get("SHOP_ADMIN_API_URL", "").rstrip("/")
SHOP_ADMIN_TOKEN   = os.environ.get("SHOP_ADMIN_TOKEN", "")

# Контекст: заполняется из bot.py через set_tma_api_handler.
_TMA_CTX = None


def set_tma_api_handler(bot, get_db, notify_new_order, main_loop, bot_token):
    """Регистрирует контекст для обработки API-запросов от TMA.
    Вызывается из bot.py после инициализации event-loop."""
    global _TMA_CTX
    _TMA_CTX = {
        "bot": bot, "get_db": get_db, "notify_new_order": notify_new_order,
        "loop": main_loop, "bot_token": bot_token,
    }
    try:
        _ensure_chat_table()
    except Exception:
        pass


# ─── DB helpers ──────────────────────────────────────────────────────────────

def _ensure_chat_table():
    con = sqlite3.connect(DB_PATH)
    try:
        con.execute("""CREATE TABLE IF NOT EXISTS chat_messages (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            order_id   INTEGER NOT NULL,
            user_id    INTEGER NOT NULL,
            user_name  TEXT,
            is_admin   INTEGER DEFAULT 0,
            text       TEXT NOT NULL,
            created_at TEXT DEFAULT (datetime('now', 'localtime'))
        )""")
        con.execute(
            "CREATE INDEX IF NOT EXISTS idx_chat_order ON chat_messages(order_id, id)"
        )
        con.commit()
    finally:
        con.close()


def _read_sync_snapshot() -> dict:
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    try:
        orders = con.execute(
            "SELECT id,user_id,name,phone,address,total,discount,status,created_at "
            "FROM orders ORDER BY id DESC LIMIT 200"
        ).fetchall()
        users = con.execute(
            "SELECT user_id,tg_name,user_type,name,phone,company_name,created_at "
            "FROM users ORDER BY user_id DESC LIMIT 500"
        ).fetchall()
        products = con.execute(
            "SELECT name,price,stock FROM products ORDER BY name"
        ).fetchall()
    finally:
        con.close()
    return {
        "orders":   [dict(r) for r in orders],
        "users":    [dict(r) for r in users],
        "products": [dict(r) for r in products],
    }


# ─── Auth helpers ─────────────────────────────────────────────────────────────

def verify_tma_init_data(init_data: str, bot_token: str) -> dict | None:
    """Проверяет HMAC-подпись Telegram WebApp initData.
    Возвращает user-dict или None при невалидных данных."""
    if not init_data or not bot_token:
        return None
    try:
        parsed = dict(parse_qsl(init_data, keep_blank_values=True))
        received_hash = parsed.pop("hash", "")
        if not received_hash:
            return None
        data_check = "\n".join(f"{k}={parsed[k]}" for k in sorted(parsed.keys()))
        secret = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
        calc = hmac.new(secret, data_check.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(calc, received_hash):
            return None
        user_json = parsed.get("user", "")
        return json.loads(user_json) if user_json else None
    except Exception:
        return None


def _check_bearer(handler) -> bool:
    if not API_ORDERS_TOKEN:
        return False
    return handler.headers.get("Authorization", "") == f"Bearer {API_ORDERS_TOKEN}"


def _get_request_user(handler) -> dict | None:
    """Верифицирует initData из заголовка и возвращает user-dict или None."""
    if _TMA_CTX is None:
        return None
    return verify_tma_init_data(
        handler.headers.get("X-Tma-InitData", ""),
        _TMA_CTX["bot_token"]
    )


def _j(data: dict) -> bytes:
    return json.dumps(data, ensure_ascii=False).encode("utf-8")


def _proxy_shop_admin(method: str, sub_path: str, body: bytes = b"",
                      content_type: str = "application/json") -> tuple[int, bytes, str]:
    """Форвардит запрос в VPS Shop Admin API с Bearer-токеном.
    Возвращает (status, body_bytes, content_type)."""
    if not SHOP_ADMIN_API_URL or not SHOP_ADMIN_TOKEN:
        return 503, _j({"ok": False, "error": "SHOP_ADMIN_API_URL/SHOP_ADMIN_TOKEN not configured"}), "application/json; charset=utf-8"
    url = f"{SHOP_ADMIN_API_URL}{sub_path}"
    req = urllib.request.Request(url, data=body or None, method=method)
    req.add_header("Authorization", f"Bearer {SHOP_ADMIN_TOKEN}")
    if body:
        req.add_header("Content-Type", content_type)
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            return r.status, r.read(), r.headers.get("Content-Type", "application/json")
    except urllib.error.HTTPError as e:
        return e.code, (e.read() or b""), e.headers.get("Content-Type", "application/json") if e.headers else "application/json"
    except urllib.error.URLError as e:
        return 502, _j({"ok": False, "error": f"VPS unreachable: {e.reason}"}), "application/json; charset=utf-8"
    except Exception as e:
        return 500, _j({"ok": False, "error": f"proxy error: {e}"}), "application/json; charset=utf-8"


# ─── HTTP Handler ─────────────────────────────────────────────────────────────

class Handler(BaseHTTPRequestHandler):

    def do_GET(self):
        path = unquote(self.path.split("?", 1)[0])

        # ── Healthcheck ──────────────────────────────────────────────────────
        if path in ("/", ""):
            self._send(200, b"OK", "text/plain")
            return

        # ── /sync (Bearer) ───────────────────────────────────────────────────
        if path == "/sync":
            if not _check_bearer(self):
                self._send(401, b'{"error":"unauthorized"}', "application/json")
                return
            try:
                body = _j(_read_sync_snapshot())
                self._send(200, body, "application/json; charset=utf-8")
            except Exception as e:
                self._send(500, str(e).encode(), "text/plain")
            return

        # ── /tma/api/my_orders ───────────────────────────────────────────────
        if path == "/tma/api/my_orders":
            qs = parse_qs(urlparse(self.path).query)
            tg_id = qs.get("tg_id", [""])[0]
            if not tg_id or not tg_id.isdigit():
                self._send(400, b'{"error":"tg_id required"}', "application/json")
                return
            try:
                con = sqlite3.connect(DB_PATH)
                con.row_factory = sqlite3.Row
                try:
                    ord_cols = {r[1] for r in con.execute("PRAGMA table_info(orders)").fetchall()}
                    it_cols  = {r[1] for r in con.execute("PRAGMA table_info(order_items)").fetchall()}
                    extra = [c for c in ("total_kg", "discount", "comment") if c in ord_cols]
                    sel = ",".join(["id", "total", "status", "created_at"] + extra)
                    orders = con.execute(
                        f"SELECT {sel} FROM orders WHERE user_id=? ORDER BY id DESC LIMIT 50",
                        (int(tg_id),)
                    ).fetchall()
                    name_expr    = "oi.product_name" if "product_name" in it_cols else "p.name as product_name"
                    fasovka_expr = "oi.fasovka"       if "fasovka"       in it_cols else "NULL as fasovka"
                    out = []
                    for o in orders:
                        items = con.execute(
                            f"SELECT oi.quantity, oi.price, {name_expr}, {fasovka_expr} "
                            f"FROM order_items oi LEFT JOIN products p ON oi.product_id=p.id "
                            f"WHERE oi.order_id=? ORDER BY oi.id",
                            (o["id"],)
                        ).fetchall()
                        out.append({**dict(o), "items": [dict(i) for i in items]})
                finally:
                    con.close()
                self._send(200, _j({"orders": out}), "application/json; charset=utf-8",
                           {"Cache-Control": "no-cache"})
            except Exception as e:
                self._send(500, _j({"error": str(e)}), "application/json")
            return

        # ── /tma/api/admin/check ─────────────────────────────────────────────
        if path == "/tma/api/admin/check":
            user = _get_request_user(self)
            is_adm = bool(user and user.get("id") in ADMIN_IDS)
            self._send(200, _j({"is_admin": is_adm, "user_id": user.get("id") if user else None}),
                       "application/json; charset=utf-8", {"Cache-Control": "no-cache"})
            return

        # ── /tma/api/admin/products → прокси в VPS Shop Admin API ────────────
        if path == "/tma/api/admin/products":
            user = _get_request_user(self)
            if not (user and user.get("id") in ADMIN_IDS):
                self._send(403, _j({"error": "Доступ запрещён"}),
                           "application/json; charset=utf-8")
                return
            status, rbody, rctype = _proxy_shop_admin("GET", "/admin/products")
            self._send(status, rbody, rctype, {"Cache-Control": "no-cache"})
            return

        # ── /tma/api/admin/* ─────────────────────────────────────────────────
        m_admin = re.match(
            r"^/tma/api/admin/(orders|users|order/(\d+)|user/(\d+))$", path
        )
        if m_admin:
            user = _get_request_user(self)
            if not (user and user.get("id") in ADMIN_IDS):
                self._send(403, _j({"error": "Доступ запрещён"}),
                           "application/json; charset=utf-8")
                return
            seg = m_admin.group(1)
            try:
                con = sqlite3.connect(DB_PATH)
                con.row_factory = sqlite3.Row
                try:
                    if seg == "orders":
                        qs     = parse_qs(urlparse(self.path).query)
                        limit  = min(int(qs.get("limit",  ["50"])[0]), 200)
                        offset = int(qs.get("offset", ["0"])[0])
                        sf     = qs.get("status", [""])[0]
                        where  = "WHERE o.status=?" if sf else ""
                        params = ([sf] if sf else []) + [limit, offset]
                        rows = con.execute(
                            f"SELECT o.id, o.user_id, o.name, o.phone, o.address, "
                            f"o.total, o.status, o.created_at, u.tg_name, "
                            f"COUNT(oi.id) as items_count "
                            f"FROM orders o "
                            f"LEFT JOIN users u ON o.user_id=u.user_id "
                            f"LEFT JOIN order_items oi ON oi.order_id=o.id "
                            f"{where} GROUP BY o.id ORDER BY o.id DESC LIMIT ? OFFSET ?",
                            params
                        ).fetchall()
                        counts = {}
                        for r in con.execute(
                            "SELECT status, COUNT(*) as n FROM orders GROUP BY status"
                        ):
                            counts[r["status"]] = r["n"]
                        self._send(200,
                            _j({"orders": [dict(r) for r in rows], "counts": counts}),
                            "application/json; charset=utf-8", {"Cache-Control": "no-cache"})

                    elif seg == "users":
                        rows = con.execute(
                            "SELECT u.user_id, u.tg_name, u.name, u.phone, u.city, "
                            "u.created_at, COUNT(o.id) as order_count, "
                            "MAX(o.created_at) as last_order_at, "
                            "COALESCE(SUM(o.total),0) as total_spent "
                            "FROM users u LEFT JOIN orders o ON o.user_id=u.user_id "
                            "GROUP BY u.user_id "
                            "ORDER BY last_order_at DESC, u.created_at DESC LIMIT 200"
                        ).fetchall()
                        self._send(200, _j({"users": [dict(r) for r in rows]}),
                                   "application/json; charset=utf-8", {"Cache-Control": "no-cache"})

                    elif m_admin.group(2):          # order/ID
                        oid = int(m_admin.group(2))
                        o = con.execute(
                            "SELECT o.id, o.user_id, o.name, o.phone, o.address, "
                            "o.total, o.status, o.created_at, u.tg_name, u.city "
                            "FROM orders o LEFT JOIN users u ON o.user_id=u.user_id "
                            "WHERE o.id=?", (oid,)
                        ).fetchone()
                        if not o:
                            self._send(404, _j({"error": "Заказ не найден"}),
                                       "application/json; charset=utf-8")
                        else:
                            items = con.execute(
                                "SELECT oi.quantity, oi.price, p.name as product_name "
                                "FROM order_items oi "
                                "LEFT JOIN products p ON oi.product_id=p.id "
                                "WHERE oi.order_id=? ORDER BY oi.id", (oid,)
                            ).fetchall()
                            self._send(200,
                                _j({**dict(o), "items": [dict(i) for i in items]}),
                                "application/json; charset=utf-8", {"Cache-Control": "no-cache"})

                    elif m_admin.group(3):          # user/ID
                        uid = int(m_admin.group(3))
                        u = con.execute(
                            "SELECT * FROM users WHERE user_id=?", (uid,)
                        ).fetchone()
                        if not u:
                            self._send(404, _j({"error": "Клиент не найден"}),
                                       "application/json; charset=utf-8")
                        else:
                            orders = con.execute(
                                "SELECT id, total, status, created_at FROM orders "
                                "WHERE user_id=? ORDER BY id DESC LIMIT 30", (uid,)
                            ).fetchall()
                            self._send(200,
                                _j({**dict(u), "orders": [dict(o) for o in orders]}),
                                "application/json; charset=utf-8", {"Cache-Control": "no-cache"})
                finally:
                    con.close()
            except Exception as e:
                self._send(500, _j({"error": str(e)}), "application/json; charset=utf-8")
            return

        # ── /tma/api/chat/ORDER_ID (GET messages) ────────────────────────────
        m_chat = re.match(r"^/tma/api/chat/(\d+)$", path)
        if m_chat:
            user = _get_request_user(self)
            if not user or not user.get("id"):
                self._send(401, _j({"error": "unauthorized"}),
                           "application/json; charset=utf-8")
                return
            order_id = int(m_chat.group(1))
            user_id  = user.get("id")
            is_adm   = user_id in ADMIN_IDS
            qs       = parse_qs(urlparse(self.path).query)
            since_id = int(qs.get("since", ["0"])[0])
            try:
                _ensure_chat_table()
                con = sqlite3.connect(DB_PATH)
                con.row_factory = sqlite3.Row
                try:
                    if not is_adm:
                        o = con.execute(
                            "SELECT user_id FROM orders WHERE id=?", (order_id,)
                        ).fetchone()
                        if not o or o["user_id"] != user_id:
                            self._send(403, _j({"error": "Нет доступа"}),
                                       "application/json; charset=utf-8")
                            return
                    msgs = con.execute(
                        "SELECT id, user_id, user_name, is_admin, text, created_at "
                        "FROM chat_messages WHERE order_id=? AND id>? ORDER BY id LIMIT 50",
                        (order_id, since_id)
                    ).fetchall()
                finally:
                    con.close()
                self._send(200, _j({"messages": [dict(m) for m in msgs]}),
                           "application/json; charset=utf-8", {"Cache-Control": "no-cache"})
            except Exception as e:
                self._send(500, _j({"error": str(e)}), "application/json; charset=utf-8")
            return

        # ── /tma/products.json (с живыми ценами от Bishop) ───────────────────
        if path == "/tma/products.json" and PRICE_SYNC_ENABLED:
            try:
                with open(os.path.join(TMA_ROOT, "products.json"), encoding="utf-8") as f:
                    data = json.load(f)
                try:
                    from live_prices_api import merge_into_products
                    updated, stats = merge_into_products(data["products"])
                    data["products"] = updated
                    data["_price_sync"] = {
                        "matched": stats["matched"],
                        "source": "bishop",
                        "loaded_at": stats["meta"].get("loaded_at"),
                    }
                except Exception as e:
                    data["_price_sync"] = {"error": str(e)}
                body = _j(data)
                self._send(200, body, "application/json", {"Cache-Control": "no-cache, max-age=60"})
                return
            except Exception:
                pass  # fallback на статику

        # ── /tma/* статика ───────────────────────────────────────────────────
        if path.startswith("/tma/") or path == "/tma":
            rel = path[5:] if path.startswith("/tma/") else ""
            if rel == "" or rel.endswith("/"):
                rel = (rel + "index.html").lstrip("/")
            full_path = os.path.normpath(os.path.join(TMA_ROOT, rel))
            if not full_path.startswith(TMA_ROOT):
                self._send(403, b"Forbidden", "text/plain")
                return
            if not os.path.isfile(full_path):
                full_path = os.path.join(TMA_ROOT, "index.html")
                if not os.path.isfile(full_path):
                    self._send(404, b"Not found", "text/plain")
                    return
            ctype, _ = mimetypes.guess_type(full_path)
            ctype = ctype or "application/octet-stream"
            try:
                with open(full_path, "rb") as f:
                    body = f.read()
                extra = {}
                if any(full_path.endswith(e) for e in (".jpg", ".png", ".webp", ".svg", ".ico")):
                    extra["Cache-Control"] = "public, max-age=3600, must-revalidate"
                self._send(200, body, ctype, extra)
            except Exception as e:
                self._send(500, str(e).encode(), "text/plain")
            return

        self._send(404, b"Not found", "text/plain")

    def do_HEAD(self):
        self.send_response(200)
        self.end_headers()

    def do_POST(self):
        path = unquote(self.path.split("?", 1)[0])

        # ── Создать заказ ────────────────────────────────────────────────────
        if path == "/tma/api/order":
            self._handle_tma_order()
            return

        # ── Отправить КП аренды в личку ──────────────────────────────────────
        if path == "/tma/api/send_kp":
            user = _get_request_user(self)
            if not user or not user.get("id"):
                self._send(401, _j({"error": "Требуется авторизация через Telegram"}),
                           "application/json; charset=utf-8")
                return
            if _TMA_CTX is None:
                self._send(503, _j({"error": "Сервис временно недоступен"}),
                           "application/json; charset=utf-8")
                return
            if not os.path.isfile(KP_PDF_PATH):
                self._send(404, _j({"error": "Файл КП не найден"}),
                           "application/json; charset=utf-8")
                return
            user_id = int(user["id"])
            async def _send_kp():
                from aiogram.types import FSInputFile
                doc = FSInputFile(KP_PDF_PATH, filename="Roastberry_КП_Аренда.pdf")
                await _TMA_CTX["bot"].send_document(
                    chat_id=user_id, document=doc,
                    caption="Наше КП по аренде оборудования ☕\n\nЕсть вопросы — пишите, поможем!"
                )
            try:
                fut = asyncio.run_coroutine_threadsafe(_send_kp(), _TMA_CTX["loop"])
                fut.result(timeout=10)
                self._send(200, _j({"ok": True}), "application/json; charset=utf-8")
            except Exception as e:
                self._send(500, _j({"ok": False, "error": str(e)}),
                           "application/json; charset=utf-8")
            return

        # ── Загрузка фото товара (admin) → прокси на VPS ───────────────────
        m_photo = re.match(r"^/tma/api/admin/product/([^/]+)/photo$", path)
        if m_photo:
            user = _get_request_user(self)
            if not (user and user.get("id") in ADMIN_IDS):
                self._send(403, _j({"error": "Доступ запрещён"}),
                           "application/json; charset=utf-8")
                return
            ctype_in = self.headers.get("Content-Type", "application/octet-stream")
            try:
                clen = int(self.headers.get("Content-Length", "0") or "0")
            except ValueError:
                clen = 0
            if clen <= 0 or clen > 6 * 1024 * 1024:
                self._send(400, _j({"ok": False, "error": "invalid Content-Length"}),
                           "application/json; charset=utf-8")
                return
            raw = self.rfile.read(clen)
            tma_id = unquote(m_photo.group(1))
            status, rbody, rctype = _proxy_shop_admin(
                "POST", f"/admin/product/{quote(tma_id, safe='')}/photo",
                body=raw, content_type=ctype_in,
            )
            self._send(status, rbody, rctype)
            return

        # ── Правка товара (admin) → прокси на VPS как PATCH ────────────────
        m_prod = re.match(r"^/tma/api/admin/product/([^/]+)$", path)
        if m_prod:
            user = _get_request_user(self)
            if not (user and user.get("id") in ADMIN_IDS):
                self._send(403, _j({"error": "Доступ запрещён"}),
                           "application/json; charset=utf-8")
                return
            try:
                clen = int(self.headers.get("Content-Length", "0") or "0")
            except ValueError:
                clen = 0
            raw = self.rfile.read(clen) if clen > 0 else b""
            tma_id = unquote(m_prod.group(1))
            status, rbody, rctype = _proxy_shop_admin(
                "PATCH", f"/admin/product/{quote(tma_id, safe='')}",
                body=raw, content_type="application/json",
            )
            self._send(status, rbody, rctype)
            return

        # ── Сменить статус заказа (admin) ─────────────────────────────────────
        m_status = re.match(r"^/tma/api/admin/order/(\d+)/status$", path)
        if m_status:
            user = _get_request_user(self)
            if not (user and user.get("id") in ADMIN_IDS):
                self._send(403, _j({"error": "Доступ запрещён"}),
                           "application/json; charset=utf-8")
                return
            order_id   = int(m_status.group(1))
            payload    = self._read_body()
            new_status = payload.get("status", "")
            if new_status not in ORDER_STATUSES_VALID:
                self._send(400,
                    _j({"error": f"Статус должен быть одним из {sorted(ORDER_STATUSES_VALID)}"}),
                    "application/json; charset=utf-8")
                return
            try:
                con = sqlite3.connect(DB_PATH)
                con.row_factory = sqlite3.Row
                try:
                    o = con.execute(
                        "SELECT id, user_id FROM orders WHERE id=?", (order_id,)
                    ).fetchone()
                    if not o:
                        self._send(404, _j({"error": "Заказ не найден"}),
                                   "application/json; charset=utf-8")
                        return
                    con.execute("UPDATE orders SET status=? WHERE id=?",
                                (new_status, order_id))
                    con.commit()
                    client_uid = o["user_id"]
                finally:
                    con.close()
            except Exception as e:
                self._send(500, _j({"error": str(e)}), "application/json; charset=utf-8")
                return
            # Уведомить клиента
            client_msgs = {
                "confirmed": (
                    f"✅ <b>Заказ №{order_id} подтверждён</b>\n\n"
                    f"Взяли в работу. Кофе будет готов через 1–2 рабочих дня."
                ),
                "shipped": (
                    f"📦 <b>Заказ №{order_id} передан в доставку</b>\n\n"
                    f"Ожидайте уведомление от курьерской службы."
                ),
                "done": (
                    f"🎉 <b>Заказ №{order_id} выполнен</b>\n\n"
                    f"Спасибо что выбрали Roastberry! Если есть вопросы — пишите."
                ),
                "cancelled": (
                    f"❌ <b>Заказ №{order_id} отменён</b>\n\n"
                    f"Если это ошибка — напишите нам."
                ),
            }
            if _TMA_CTX and client_uid:
                msg_text = client_msgs.get(new_status, f"Статус заказа №{order_id} изменён.")
                async def _notify_client():
                    try:
                        await _TMA_CTX["bot"].send_message(
                            client_uid, msg_text, parse_mode="HTML"
                        )
                    except Exception:
                        pass
                asyncio.run_coroutine_threadsafe(_notify_client(), _TMA_CTX["loop"])
            self._send(200, _j({"ok": True, "status": new_status}),
                       "application/json; charset=utf-8")
            return

        # ── Отправить сообщение в чат поддержки ──────────────────────────────
        m_chat = re.match(r"^/tma/api/chat/(\d+)$", path)
        if m_chat:
            user = _get_request_user(self)
            if not user or not user.get("id"):
                self._send(401, _j({"error": "unauthorized"}),
                           "application/json; charset=utf-8")
                return
            order_id  = int(m_chat.group(1))
            user_id   = user.get("id")
            is_adm    = user_id in ADMIN_IDS
            full_name = " ".join(
                filter(None, [user.get("first_name"), user.get("last_name")])
            ) or user.get("username") or "Клиент"
            payload   = self._read_body()
            text      = (payload.get("text") or "").strip()
            if not text:
                self._send(400, _j({"error": "text required"}),
                           "application/json; charset=utf-8")
                return
            if len(text) > 2000:
                self._send(400, _j({"error": "too long"}), "application/json; charset=utf-8")
                return
            try:
                _ensure_chat_table()
                con = sqlite3.connect(DB_PATH)
                con.row_factory = sqlite3.Row
                try:
                    if not is_adm:
                        o = con.execute(
                            "SELECT user_id FROM orders WHERE id=?", (order_id,)
                        ).fetchone()
                        if not o or o["user_id"] != user_id:
                            self._send(403, _j({"error": "Нет доступа"}),
                                       "application/json; charset=utf-8")
                            return
                    con.execute(
                        "INSERT INTO chat_messages "
                        "(order_id, user_id, user_name, is_admin, text) VALUES (?,?,?,?,?)",
                        (order_id, user_id, full_name, 1 if is_adm else 0, text)
                    )
                    con.commit()
                    msg_id = con.execute("SELECT last_insert_rowid()").fetchone()[0]
                    notify_uid = None
                    if is_adm:
                        row = con.execute(
                            "SELECT user_id FROM orders WHERE id=?", (order_id,)
                        ).fetchone()
                        if row:
                            notify_uid = row["user_id"]
                    else:
                        notify_uid = list(ADMIN_IDS)[0] if ADMIN_IDS else None
                finally:
                    con.close()
                # Уведомить другую сторону через бот (non-blocking)
                if _TMA_CTX and notify_uid:
                    if is_adm:
                        note = f"💬 Ответ поддержки по заказу №{order_id}:\n\n{text}"
                    else:
                        note = f"💬 {full_name} / заказ №{order_id}:\n\n{text}"
                    async def _notify_chat():
                        try:
                            await _TMA_CTX["bot"].send_message(notify_uid, note)
                        except Exception:
                            pass
                    asyncio.run_coroutine_threadsafe(_notify_chat(), _TMA_CTX["loop"])
                self._send(200, _j({"ok": True, "id": msg_id}),
                           "application/json; charset=utf-8")
            except Exception as e:
                self._send(500, _j({"error": str(e)}), "application/json; charset=utf-8")
            return

        self._send(404, b"Not found", "text/plain")

    def _handle_tma_order(self):
        if _TMA_CTX is None:
            self._send(503, _j({"ok": False, "error": "API не инициализирован"}),
                       "application/json")
            return
        init_data = self.headers.get("X-Tma-InitData", "")
        user = verify_tma_init_data(init_data, _TMA_CTX["bot_token"])
        if not user or not user.get("id"):
            self._send(401, _j({"ok": False,
                                "error": "Откройте магазин из Telegram (initData невалидна)"}),
                       "application/json; charset=utf-8")
            return
        payload = self._read_body()
        full_name = (
            " ".join(filter(None, [user.get("first_name"), user.get("last_name")]))
            or user.get("username") or "TMA-клиент"
        )
        try:
            from tma_handler import process_tma_order
            fut = asyncio.run_coroutine_threadsafe(
                process_tma_order(
                    payload, int(user["id"]), full_name,
                    _TMA_CTX["get_db"], _TMA_CTX["bot"], _TMA_CTX["notify_new_order"]
                ),
                _TMA_CTX["loop"]
            )
            result = fut.result(timeout=20)
        except ValueError as e:
            self._send(400, _j({"ok": False, "error": str(e)}),
                       "application/json; charset=utf-8")
            return
        except Exception as e:
            self._send(500, _j({"ok": False, "error": f"server error: {e}"}),
                       "application/json; charset=utf-8")
            return
        self._send(200, _j({"ok": True, **result}), "application/json; charset=utf-8")

    def _read_body(self) -> dict:
        try:
            length = int(self.headers.get("Content-Length", "0"))
            raw = self.rfile.read(length) if length > 0 else b""
            return json.loads(raw.decode("utf-8")) if raw else {}
        except Exception:
            return {}

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "POST, GET, OPTIONS")
        self.send_header(
            "Access-Control-Allow-Headers",
            "Content-Type, X-Tma-InitData, Authorization"
        )
        self.end_headers()

    def log_message(self, format, *args):
        pass

    def _send(self, status, body, ctype, extra_headers=None):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        for k, v in (extra_headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)


def run_server():
    server = HTTPServer(("0.0.0.0", PORT), Handler)
    print(f"[TMA-static] Сервер на порту {PORT}, корень {TMA_ROOT}")
    server.serve_forever()


if __name__ == "__main__":
    run_server()
