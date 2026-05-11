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
import time
from http.server import HTTPServer, ThreadingHTTPServer, BaseHTTPRequestHandler
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


def set_tma_api_handler(bot, get_db, notify_new_order, main_loop, bot_token,
                        get_discount=None, notify_external_order=None):
    """Регистрирует контекст для обработки API-запросов от TMA.
    Вызывается из bot.py после инициализации event-loop.

    get_discount / notify_external_order — нужны для POST /orders из bot.py
    (миграция из _SyncHandler). При None POST /orders отвечает 503."""
    global _TMA_CTX
    _TMA_CTX = {
        "bot": bot, "get_db": get_db, "notify_new_order": notify_new_order,
        "loop": main_loop, "bot_token": bot_token,
        "get_discount": get_discount,
        "notify_external_order": notify_external_order,
    }
    try:
        _ensure_chat_table()
    except Exception:
        pass


# ─── products.json index (для админ-обогащения позиций заказа) ──────────────
# Индексы: by_id (tma_id → meta), by_name (name_lower → meta), all (для fuzzy).
# tma_id — основной ключ при оформлении из TMA (точно различает E/F-обжарку
# у товаров с одинаковым именем). Имена в shop.db ≠ имена в products.json,
# поэтому fuzzy остаётся как fallback для старых заказов и заказов из David.
_PRODUCTS_INDEX = {"mtime": 0, "data": {"by_id": {}, "by_name": {}, "all": []}}

_NAME_NOISE = {
    "кофе", "упак", "г", "гр", "кг", "шт", "мл", "л",
    "roastberry", "rb", "rbr", "black", "borщ", "borsh", "be", "bf", "ef", "fc",
    "молотый", "мытый", "сухой", "натур", "наутер", "натуральный",
    "1", "200", "250", "500", "8",
    "дп", "фильтр", "пакетов", "под",
}

def _name_tokens(name: str) -> set:
    s = (name or "").lower()
    s = s.replace("ё", "е")
    out = set()
    cur = []
    for ch in s:
        if ch.isalnum():
            cur.append(ch)
        else:
            if cur:
                out.add("".join(cur))
                cur = []
    if cur:
        out.add("".join(cur))
    return {t for t in out if len(t) >= 3 and not t.isdigit() and t not in _NAME_NOISE}

def _products_index() -> dict:
    path = os.path.join(TMA_ROOT, "products.json")
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        return _PRODUCTS_INDEX["data"]
    if mtime == _PRODUCTS_INDEX["mtime"]:
        return _PRODUCTS_INDEX["data"]
    try:
        with open(path, encoding="utf-8") as f:
            j = json.load(f)
        prods = j if isinstance(j, list) else j.get("products", [])
        by_id    = {}
        by_name  = {}
        all_items = []
        for p in prods:
            name = (p.get("name") or "").strip()
            if not name:
                continue
            meta = {
                "category": p.get("category") or "",
                "roast":    p.get("roast") or "",
            }
            pid = p.get("id")
            if pid:
                by_id[pid] = meta
            by_name.setdefault(name.lower(), meta)
            tokens = _name_tokens(name)
            if tokens:
                all_items.append((tokens, meta))
        _PRODUCTS_INDEX["mtime"] = mtime
        _PRODUCTS_INDEX["data"]  = {"by_id": by_id, "by_name": by_name, "all": all_items}
    except Exception:
        pass
    return _PRODUCTS_INDEX["data"]

def _lookup_product_meta(product_name: str, tma_id: str = "") -> dict:
    idx = _products_index()
    if tma_id:
        hit = idx["by_id"].get(tma_id)
        if hit:
            return hit
    if not product_name:
        return {}
    nl = product_name.strip().lower()
    hit = idx["by_name"].get(nl)
    if hit:
        return hit
    # Fuzzy: token jaccard
    src = _name_tokens(product_name)
    if not src:
        return {}
    best_score = 0.0
    best_meta  = None
    for tokens, meta in idx["all"]:
        if not tokens:
            continue
        inter = len(src & tokens)
        if inter == 0:
            continue
        score = inter / max(len(src), len(tokens))
        if score > best_score:
            best_score = score
            best_meta  = meta
    if best_score >= 0.5 and best_meta:
        return best_meta
    return {}


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


_SYNC_CACHE = {"data": None, "ts": 0.0}
_SYNC_CACHE_TTL = 30.0  # секунд

def _read_sync_snapshot() -> dict:
    import time
    now = time.monotonic()
    if _SYNC_CACHE["data"] is not None and (now - _SYNC_CACHE["ts"]) < _SYNC_CACHE_TTL:
        return _SYNC_CACHE["data"]
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
    snapshot = {
        "orders":   [dict(r) for r in orders],
        "users":    [dict(r) for r in users],
        "products": [dict(r) for r in products],
    }
    _SYNC_CACHE["data"] = snapshot
    _SYNC_CACHE["ts"] = now
    return snapshot


def _invalidate_sync_cache():
    """Сбрасывает кеш /sync — вызывается после изменений данных (новый заказ,
    обновление stock/price), чтобы внешние агенты сразу видели актуальное."""
    _SYNC_CACHE["data"] = None
    _SYNC_CACHE["ts"] = 0.0


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


def verify_tg_login_widget(payload: dict, bot_token: str) -> dict | None:
    """Проверяет данные от Telegram Login Widget (виджет на сайте, без Mini App).
    Формат отличается от initData: hash считается по data_check_string из
    отсортированных по ключу `key=value` (разделитель \\n), secret = sha256(bot_token).
    Срок жизни — 1 час, иначе считаем устаревшим."""
    if not payload or not bot_token:
        return None
    try:
        received_hash = payload.pop("hash", "")
        if not received_hash:
            return None
        auth_date = int(payload.get("auth_date", 0))
        if time.time() - auth_date > 3600:
            return None
        data_check = "\n".join(
            f"{k}={payload[k]}" for k in sorted(payload.keys())
            if payload[k] is not None and payload[k] != ""
        )
        secret = hashlib.sha256(bot_token.encode()).digest()
        calc = hmac.new(secret, data_check.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(calc, received_hash):
            return None
        return {
            "id":         int(payload.get("id", 0)),
            "first_name": payload.get("first_name", ""),
            "last_name":  payload.get("last_name", ""),
            "username":   payload.get("username", ""),
            "photo_url":  payload.get("photo_url", ""),
        }
    except Exception:
        return None


# ─── JWT (для browser-mode, без Telegram-инициализации) ──────────────────────
_JWT_SECRET_FALLBACK = "rb-tma-jwt-secret-change-me"

def _jwt_secret() -> str:
    """Берём JWT-секрет из env или из bot_token (он уже секретный)."""
    s = os.environ.get("JWT_SECRET")
    if s:
        return s
    if _TMA_CTX and _TMA_CTX.get("bot_token"):
        return "jwt:" + _TMA_CTX["bot_token"]
    return _JWT_SECRET_FALLBACK


def _b64url_enc(b: bytes) -> str:
    import base64
    return base64.urlsafe_b64encode(b).decode("ascii").rstrip("=")


def _b64url_dec(s: str) -> bytes:
    import base64
    pad = "=" * (-len(s) % 4)
    return base64.urlsafe_b64decode(s + pad)


def jwt_sign(payload: dict, ttl_seconds: int = 30 * 24 * 3600) -> str:
    """Минимальная HS256 реализация без внешних либ.
    Payload содержит user_id, exp; срок жизни — 30 дней по умолчанию."""
    header = {"alg": "HS256", "typ": "JWT"}
    payload = dict(payload)
    payload["exp"] = int(time.time()) + ttl_seconds
    h = _b64url_enc(json.dumps(header, separators=(",", ":")).encode())
    p = _b64url_enc(json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode())
    sig = hmac.new(_jwt_secret().encode(), f"{h}.{p}".encode(), hashlib.sha256).digest()
    return f"{h}.{p}.{_b64url_enc(sig)}"


def jwt_verify(token: str) -> dict | None:
    try:
        h, p, s = token.split(".")
        sig = hmac.new(_jwt_secret().encode(), f"{h}.{p}".encode(), hashlib.sha256).digest()
        if not hmac.compare_digest(_b64url_enc(sig), s):
            return None
        payload = json.loads(_b64url_dec(p))
        if int(payload.get("exp", 0)) < time.time():
            return None
        return payload
    except Exception:
        return None


def _get_request_user(handler) -> dict | None:
    """Верифицирует пользователя по одному из источников:
    1) X-Tma-InitData (Telegram Mini App, основной канал)
    2) Authorization: Bearer <JWT> (browser-mode после Telegram Login Widget)
    Возвращает user-dict {id, first_name, ...} или None."""
    if _TMA_CTX is None:
        return None
    # Mini App
    user = verify_tma_init_data(
        handler.headers.get("X-Tma-InitData", ""),
        _TMA_CTX["bot_token"],
    )
    if user:
        return user
    # JWT (browser-mode)
    auth = handler.headers.get("Authorization", "")
    if auth.startswith("Bearer "):
        token = auth[7:].strip()
        # Не путаем с API_ORDERS_TOKEN (это серверный токен для David'а)
        if token and token != API_ORDERS_TOKEN:
            payload = jwt_verify(token)
            if payload and payload.get("id"):
                return {
                    "id":         int(payload["id"]),
                    "first_name": payload.get("first_name", ""),
                    "last_name":  payload.get("last_name", ""),
                    "username":   payload.get("username", ""),
                    "photo_url":  payload.get("photo_url", ""),
                }
    return None


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
                            it_cols = {r[1] for r in con.execute(
                                "PRAGMA table_info(order_items)").fetchall()}
                            tma_id_sql = "oi.tma_id" if "tma_id" in it_cols else "'' as tma_id"
                            items = con.execute(
                                f"SELECT oi.quantity, oi.price, p.name as product_name, "
                                f"{tma_id_sql} "
                                f"FROM order_items oi "
                                f"LEFT JOIN products p ON oi.product_id=p.id "
                                f"WHERE oi.order_id=? ORDER BY oi.id", (oid,)
                            ).fetchall()
                            items_out = []
                            for i in items:
                                d = dict(i)
                                meta = _lookup_product_meta(
                                    d.get("product_name") or "",
                                    tma_id=d.get("tma_id") or "",
                                )
                                d["category"] = meta.get("category", "")
                                d["roast"]    = meta.get("roast", "")
                                items_out.append(d)
                            self._send(200,
                                _j({**dict(o), "items": items_out}),
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

        # ── Внешние интеграции (Bearer): создание заказа, обновление остатков ─
        # Перенесено из bot.py:_SyncHandler, чтобы один HTTP-сервер обслуживал
        # и публичный API, и TMA Mini App (не было EADDRINUSE на :8081).
        if path == "/orders":
            if not _check_bearer(self):
                self._send(401, _j({"error": "unauthorized"}),
                           "application/json; charset=utf-8")
                return
            self._handle_external_order()
            return
        if path == "/update_stock" or path == "/update_prices":
            if not _check_bearer(self):
                self._send(401, _j({"error": "unauthorized"}),
                           "application/json; charset=utf-8")
                return
            self._handle_update_stock()
            return

        # ── Telegram Login Widget → JWT (browser-mode авторизация) ───────────
        if path == "/tma/api/auth/telegram":
            if _TMA_CTX is None:
                self._send(503, _j({"error": "service unavailable"}),
                           "application/json; charset=utf-8")
                return
            payload = self._read_body() or {}
            user = verify_tg_login_widget(dict(payload), _TMA_CTX["bot_token"])
            if not user or not user.get("id"):
                self._send(401, _j({"error": "Подпись виджета невалидна или устарела"}),
                           "application/json; charset=utf-8")
                return
            token = jwt_sign(user)
            self._send(200, _j({"token": token, "user": user}),
                       "application/json; charset=utf-8", {"Cache-Control": "no-store"})
            return

        # ── Гостевой логин (для клиентов без Telegram) → JWT ─────────────────
        # Принимает {name, phone, email?}, валидирует телефон, выдаёт JWT с
        # отрицательным guest_id (стабильный hash от phone, чтобы повторные
        # заказы с того же номера привязывались к одному клиенту).
        if path == "/tma/api/auth/guest":
            payload = self._read_body() or {}
            name  = str(payload.get("name", "")).strip()[:80]
            phone = re.sub(r"[^\d+]", "", str(payload.get("phone", "")))
            email = str(payload.get("email", "")).strip()[:120]
            if not name:
                self._send(400, _j({"error": "Введите имя"}),
                           "application/json; charset=utf-8")
                return
            # Минимальная валидация телефона: 10–15 цифр (учёт + и кода страны)
            digits = re.sub(r"\D", "", phone)
            if len(digits) < 10 or len(digits) > 15:
                self._send(400, _j({"error": "Введите корректный телефон"}),
                           "application/json; charset=utf-8")
                return
            # Стабильный отрицательный guest_id из телефона. Минусом отделяем
            # от Telegram user_id (всегда положительный) — потом в админке
            # видно по знаку, гость это или TG-клиент.
            guest_int = int(hashlib.sha256(digits.encode()).hexdigest()[:12], 16)
            guest_id  = -(guest_int % (10**9))  # держим в int32-range
            user = {
                "id":         guest_id,
                "first_name": name,
                "last_name":  "",
                "username":   "",
                "photo_url":  "",
                "phone":      digits,
                "email":      email,
                "is_guest":   True,
            }
            token = jwt_sign(user)
            self._send(200, _j({"token": token, "user": user}),
                       "application/json; charset=utf-8", {"Cache-Control": "no-store"})
            return

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
        _invalidate_sync_cache()
        self._send(200, _j({"ok": True, **result}), "application/json; charset=utf-8")

    def _read_body(self) -> dict:
        try:
            length = int(self.headers.get("Content-Length", "0"))
            raw = self.rfile.read(length) if length > 0 else b""
            return json.loads(raw.decode("utf-8")) if raw else {}
        except Exception:
            return {}

    # ── Внешний POST /update_stock|/update_prices (из bot.py:_SyncHandler) ──
    def _handle_update_stock(self):
        payload = self._read_body()
        items = payload.get("items") or []
        fuzzy = bool(payload.get("fuzzy", True))
        if not isinstance(items, list) or not items:
            self._send(400, _j({"error": "items required"}),
                       "application/json; charset=utf-8")
            return
        try:
            con = sqlite3.connect(DB_PATH)
            con.row_factory = sqlite3.Row
            updated_stock = 0
            updated_price = 0
            not_found = []
            for it in items:
                name = str(it.get("product_name", "")).strip()
                if not name:
                    continue
                stock = it.get("stock")
                price = it.get("price")
                row = con.execute(
                    "SELECT id, name FROM products WHERE name = ?", (name,)
                ).fetchone()
                if row is None and fuzzy:
                    rows = con.execute(
                        "SELECT id, name FROM products WHERE name LIKE ?",
                        (f"%{name}%",),
                    ).fetchall()
                    if len(rows) == 1:
                        row = rows[0]
                if row is None:
                    not_found.append(name)
                    continue
                pid = row["id"]
                if stock is not None:
                    try:
                        con.execute(
                            "UPDATE products SET stock = ? WHERE id = ?",
                            (int(float(stock)), pid),
                        )
                        updated_stock += 1
                    except (TypeError, ValueError):
                        pass
                if price is not None:
                    try:
                        price_f = float(price)
                        if price_f > 0:
                            con.execute(
                                "UPDATE products SET prev_price = price, price = ? "
                                "WHERE id = ?",
                                (price_f, pid),
                            )
                            updated_price += 1
                    except (TypeError, ValueError):
                        pass
            con.commit()
            con.close()
            _invalidate_sync_cache()
            self._send(200, _j({
                "status": "ok",
                "updated_stock": updated_stock,
                "updated_price": updated_price,
                "not_found_count": len(not_found),
                "not_found_sample": not_found[:20],
            }), "application/json; charset=utf-8")
        except Exception as e:
            self._send(500, _j({"error": str(e)}),
                       "application/json; charset=utf-8")

    # ── Внешний POST /orders (Девид / wahelp-agent / 1С — в будущем) ────────
    def _handle_external_order(self):
        if _TMA_CTX is None:
            self._send(503, _j({"error": "TMA context not initialized"}),
                       "application/json; charset=utf-8")
            return
        payload = self._read_body()
        phone = str(payload.get("client_phone", "")).strip()
        items = payload.get("items") or []
        if not phone or not items:
            self._send(400, _j({"error": "client_phone and items required"}),
                       "application/json; charset=utf-8")
            return
        try:
            con = sqlite3.connect(DB_PATH)
            con.row_factory = sqlite3.Row
            try:
                norm_phone = "".join(ch for ch in phone if ch.isdigit())
                user = None
                if norm_phone:
                    rows = con.execute(
                        "SELECT user_id, name, phone, company_name FROM users"
                    ).fetchall()
                    for r in rows:
                        r_norm = "".join(ch for ch in (r["phone"] or "") if ch.isdigit())
                        if r_norm and r_norm.endswith(norm_phone[-10:]):
                            user = r
                            break
                if user is None:
                    fallback_name = payload.get("client_name") or "WhatsApp клиент"
                    new_uid = -(abs(hash(norm_phone)) % (10**9))
                    con.execute(
                        "INSERT OR IGNORE INTO users "
                        "(user_id, tg_name, user_type, name, phone) "
                        "VALUES (?,?,?,?,?)",
                        (new_uid, "whatsapp", "company", fallback_name, phone),
                    )
                    user_id = new_uid
                    client_name = fallback_name
                else:
                    user_id = user["user_id"]
                    client_name = user["company_name"] or user["name"] or "Клиент"

                total = 0.0
                resolved_items = []
                missing = []
                for it in items:
                    name = str(it.get("product_name", "")).strip()
                    try:
                        qty = float(it.get("quantity", 0) or 0)
                    except (TypeError, ValueError):
                        qty = 0
                    if not name or qty <= 0:
                        continue
                    prod = con.execute(
                        "SELECT id, name, price FROM products WHERE name = ?", (name,)
                    ).fetchone()
                    if prod is None:
                        missing.append(name)
                        continue
                    line_price = (prod["price"] or 0) * qty
                    total += line_price
                    resolved_items.append({
                        "product_id": prod["id"],
                        "product_name": prod["name"],
                        "price": prod["price"] or 0,
                        "quantity": qty,
                    })
                if missing:
                    self._send(400, _j({
                        "error": "some products not found", "missing": missing,
                    }), "application/json; charset=utf-8")
                    return
                if not resolved_items:
                    self._send(400, _j({"error": "no valid items"}),
                               "application/json; charset=utf-8")
                    return

                address = str(payload.get("address", "")).strip()
                comment = str(payload.get("comment", "")).strip()
                full_address = address
                if comment:
                    full_address = f"{address} | {comment}".strip(" |")

                total_kg = 0.0
                for ri in resolved_items:
                    nl = ri["product_name"].lower()
                    if "1 кг" in nl or "1кг" in nl:
                        total_kg += ri["quantity"]
                    elif "200 г" in nl or "200г" in nl:
                        total_kg += ri["quantity"] * 0.2
                gd = _TMA_CTX.get("get_discount")
                discount_pct = gd(total_kg) if gd else 0.0
                total_after = total * (1 - discount_pct)

                cur = con.execute(
                    "INSERT INTO orders "
                    "(user_id, name, phone, address, total, discount, status) "
                    "VALUES (?,?,?,?,?,?,?)",
                    (user_id, client_name, phone, full_address,
                     total_after, discount_pct, "new"),
                )
                order_id = cur.lastrowid
                for ri in resolved_items:
                    con.execute(
                        "INSERT INTO order_items "
                        "(order_id, product_id, quantity, price) "
                        "VALUES (?,?,?,?)",
                        (order_id, ri["product_id"], ri["quantity"], ri["price"]),
                    )
                con.commit()
            finally:
                con.close()

            _invalidate_sync_cache()

            source = payload.get("source", "WhatsApp")
            notify = _TMA_CTX.get("notify_external_order")
            loop = _TMA_CTX.get("loop")
            if notify and loop:
                try:
                    asyncio.run_coroutine_threadsafe(
                        notify(order_id, client_name, phone, full_address,
                               total_after, discount_pct, resolved_items, source),
                        loop,
                    )
                except Exception as e:
                    print(f"Notify error: {e}")

            self._send(200, _j({
                "status": "ok",
                "order_id": order_id,
                "total": total_after,
                "discount_pct": discount_pct,
                "items_count": len(resolved_items),
            }), "application/json; charset=utf-8")
        except Exception as e:
            self._send(500, _j({"error": str(e)}),
                       "application/json; charset=utf-8")

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
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    print(f"[TMA-static] Сервер на порту {PORT}, корень {TMA_ROOT}")
    server.serve_forever()


if __name__ == "__main__":
    run_server()
