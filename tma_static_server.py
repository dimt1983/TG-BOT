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
import logging
import time
from http.server import HTTPServer, ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import unquote, parse_qsl, urlparse, parse_qs, quote
import urllib.request
import urllib.error
import customer_account
try:
    import concierge as cz
except Exception:  # concierge.py может отсутствовать в деплое — магазин обязан подняться
    cz = None

PORT = int(os.environ.get("PORT", 10000))
TMA_ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "tma_static")
TMA_ROOT_V2 = os.path.join(os.path.dirname(os.path.abspath(__file__)), "tma_static_v2")
ADMIN_ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "admin_static")
PRODUCT_REDIRECTS_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "catalog_sync",
    "product_redirects.json",
)
PRICE_SYNC_ENABLED = os.environ.get("BISHOP_PRICE_SYNC", "1") != "0"

DB_PATH = os.environ.get("DB_PATH", "/app/data/shop.db")
log = logging.getLogger(__name__)


def _api_orders_token() -> str:
    return os.environ.get("API_ORDERS_TOKEN", "")

# Администраторы TMA: user_id из Telegram.
# Унифицируем со списком админов бота (admin.ADMIN_IDS) — добавление одного юзера
# в одно место даёт ему сразу и уведомления о заказах в чате, и админ-вкладку
# в Mini App. Через env TMA_ADMIN_IDS можно дополнительно расширить.
_raw_admin = os.environ.get("TMA_ADMIN_IDS", "")
ADMIN_IDS = {int(x.strip()) for x in _raw_admin.split(",") if x.strip().isdigit()}
try:
    from admin import ADMIN_IDS as _BOT_ADMIN_IDS
    ADMIN_IDS.update(set(_BOT_ADMIN_IDS))
except Exception:
    if not ADMIN_IDS:
        ADMIN_IDS = {466755177}  # fallback на Дмитрия

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
    try:
        _ensure_pricing_schema()
    except Exception:
        pass
    try:
        con_fav = sqlite3.connect(DB_PATH)
        try:
            customer_account.ensure_favorites_table(con_fav)
        finally:
            con_fav.close()
    except Exception:
        pass


# ─── products.json index (для админ-обогащения позиций заказа) ──────────────
# Индексы: by_id (tma_id → meta), by_name (name_lower → meta), all (для fuzzy).
# tma_id — основной ключ при оформлении из TMA (точно различает E/F-обжарку
# у товаров с одинаковым именем). Имена в shop.db ≠ имена в products.json,
# поэтому fuzzy остаётся как fallback для старых заказов и заказов из David.
_PRODUCTS_INDEX = {"mtime": 0, "data": {"by_id": {}, "by_name": {}, "all": []}}
_PRODUCT_REDIRECTS_CACHE = {
    "path": None,
    "signature": None,
    "redirects": {},
}
_PRODUCT_REDIRECTS_LOCK = threading.Lock()

_NAME_NOISE = {
    "кофе", "упак", "г", "гр", "кг", "шт", "мл", "л",
    "roastberry", "rb", "rbr", "black", "borщ", "borsh", "be", "bf", "ef", "fc",
    "молотый", "мытый", "сухой", "натур", "наутер", "натуральный",
    "1", "200", "250", "500", "8",
    "дп", "фильтр", "пакетов", "под",
}

def _invalidate_product_redirects_cache() -> None:
    with _PRODUCT_REDIRECTS_LOCK:
        _PRODUCT_REDIRECTS_CACHE.update({
            "path": None,
            "signature": None,
            "redirects": {},
        })


def _load_product_redirects(path=None) -> dict[str, str]:
    redirect_path = os.path.abspath(os.fspath(path or PRODUCT_REDIRECTS_PATH))
    with _PRODUCT_REDIRECTS_LOCK:
        try:
            stat = os.stat(redirect_path)
            signature = (stat.st_ino, stat.st_mtime_ns, stat.st_size)
        except OSError as exc:
            log.error(
                "Failed to stat product redirects from %s: %s",
                redirect_path,
                exc,
            )
            if _PRODUCT_REDIRECTS_CACHE["path"] == redirect_path:
                return _PRODUCT_REDIRECTS_CACHE["redirects"]
            return {}
        if (
            _PRODUCT_REDIRECTS_CACHE["path"] == redirect_path
            and _PRODUCT_REDIRECTS_CACHE["signature"] == signature
        ):
            return _PRODUCT_REDIRECTS_CACHE["redirects"]
        try:
            with open(redirect_path, encoding="utf-8") as stream:
                redirects = json.load(stream)
            if not isinstance(redirects, dict):
                raise ValueError("expected object")
            for source, target in redirects.items():
                if (
                    not isinstance(source, str)
                    or not source
                    or source != source.strip()
                    or not isinstance(target, str)
                    or not target
                    or target != target.strip()
                ):
                    raise ValueError(
                        "ids must be non-empty strings without surrounding whitespace"
                    )
        except Exception as exc:
            log.error(
                "Failed to load product redirects from %s: %s",
                redirect_path,
                exc,
            )
            previous = (
                _PRODUCT_REDIRECTS_CACHE["redirects"]
                if _PRODUCT_REDIRECTS_CACHE["path"] == redirect_path
                else {}
            )
            _PRODUCT_REDIRECTS_CACHE.update({
                "path": redirect_path,
                "signature": signature,
                "redirects": previous,
            })
            return previous
        _PRODUCT_REDIRECTS_CACHE.update({
            "path": redirect_path,
            "signature": signature,
            "redirects": redirects,
        })
        return redirects


def resolve_product_id(
    product_id: str,
    redirects: dict[str, str],
    *,
    max_hops: int = 16,
) -> str:
    original = product_id
    if not isinstance(product_id, str) or not product_id or not isinstance(redirects, dict):
        return original
    if max_hops <= 0:
        return original
    current = product_id
    seen = {current}
    for _ in range(max_hops):
        target = redirects.get(current)
        if target is None:
            return current
        if not isinstance(target, str) or not target or target in seen:
            log.error("Invalid or cyclic product redirect for %s", original)
            return original
        seen.add(target)
        current = target
    if current in redirects:
        log.error("Product redirect hop limit exceeded for %s", original)
        return original
    return current


def _canonicalize_product_ids(
    product_ids,
    redirects: dict[str, str] | None = None,
) -> list[str]:
    redirect_map = _load_product_redirects() if redirects is None else redirects
    canonical_ids = []
    seen = set()
    for product_id in product_ids or []:
        canonical_id = resolve_product_id(str(product_id), redirect_map)
        if canonical_id not in seen:
            seen.add(canonical_id)
            canonical_ids.append(canonical_id)
    return canonical_ids


def _canonicalize_order_payload(
    payload: dict,
    redirects: dict[str, str] | None = None,
) -> tuple[dict, dict[str, str]]:
    redirect_map = _load_product_redirects() if redirects is None else redirects
    updated = dict(payload or {})
    items = updated.get("items")
    if not isinstance(items, list):
        return updated, {}
    updated_items = []
    migrated = {}
    for item in items:
        if not isinstance(item, dict):
            updated_items.append(item)
            continue
        updated_item = dict(item)
        product_id = updated_item.get("id")
        if isinstance(product_id, str) and product_id:
            canonical_id = resolve_product_id(product_id, redirect_map)
            updated_item["id"] = canonical_id
            if canonical_id != product_id:
                updated_item["canonical_product_id"] = canonical_id
                migrated[product_id] = canonical_id
        updated_items.append(updated_item)
    updated["items"] = updated_items
    return updated, migrated


def _add_canonical_product_id_to_response(
    body: bytes,
    content_type: str,
    *,
    requested_id: str,
    canonical_id: str,
) -> bytes:
    if requested_id == canonical_id or "json" not in (content_type or "").lower():
        return body
    try:
        payload = json.loads(body.decode("utf-8"))
    except Exception:
        return body
    if not isinstance(payload, dict):
        return body
    payload["canonical_product_id"] = canonical_id
    return _j(payload)


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
    canonical_meta = {}
    if tma_id:
        canonical_id = resolve_product_id(tma_id, _load_product_redirects())
        canonical_meta = {"canonical_product_id": canonical_id}
        hit = idx["by_id"].get(canonical_id)
        if hit:
            return {**hit, **canonical_meta}
    if not product_name:
        return canonical_meta
    nl = product_name.strip().lower()
    hit = idx["by_name"].get(nl)
    if hit:
        return {**hit, **canonical_meta}
    # Fuzzy: token jaccard
    src = _name_tokens(product_name)
    if not src:
        return canonical_meta
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
        return {**best_meta, **canonical_meta}
    return canonical_meta


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


# Бьёт повторные webhook'и Девида (POST /orders). Гибрид:
#   - explicit idempotency_key из payload → key="ext:<token>", навечно
#   - auto: sha256(phone+items) + 120-сек bucket → key="auto:..." c TTL
# Окно auto-режима выбрано так, чтобы поймать ретраи сети, но не блокировать
# реальный повторный заказ через несколько минут.
_IDEM_WINDOW_SEC = 120

def _ensure_pricing_schema():
    """Миграция под спецусловия клиентов: users.price_tier + таблица user_pricing.

    price_tier:
      'standard'     — базовая колонка прайса (1кг / 200г)
      'discount_10'  — колонка 10кг / 10кг_200 (Bishop'овский опт-прайс)
      'discount_20'  — колонка 25кг / 25кг_200
      'stm'          — отдельный СТМ-прайс (источник у Bishop, пока stub)

    user_pricing (поверх tier):
      scope='category'  — на категорию (target_id='syrup'|'tea'|'milk'|...)
      scope='subcategory' — на subcategory из products.json
      scope='product'   — на конкретный товар (target_id=tma_id; fasovka опц.)
      Либо discount_pct (0..100), либо fixed_price. Не оба сразу.
    """
    con = sqlite3.connect(DB_PATH)
    try:
        cols = {r[1] for r in con.execute("PRAGMA table_info(users)").fetchall()}
        if "price_tier" not in cols:
            con.execute("ALTER TABLE users ADD COLUMN price_tier TEXT DEFAULT 'standard'")
        con.execute("""CREATE TABLE IF NOT EXISTS user_pricing (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id      INTEGER NOT NULL,
            scope        TEXT NOT NULL,
            target_id    TEXT,
            discount_pct REAL,
            fixed_price  REAL,
            fasovka      TEXT,
            created_at   TEXT DEFAULT (datetime('now')),
            UNIQUE(user_id, scope, target_id, fasovka)
        )""")
        con.execute(
            "CREATE INDEX IF NOT EXISTS idx_pricing_user ON user_pricing(user_id)"
        )
        con.commit()
    finally:
        con.close()


# ─── Personal pricing helpers ─────────────────────────────────────────────────
# Применяются при создании заказа: tier → колонка прайса (через live_prices_api),
# user_pricing → точечные правила (категория / подкатегория / товар).

# Маппинг tier → имя колонки в xlsx-прайсе (Bishop / live_prices_api).
# Если live_prices_api не вернул нужную колонку — fallback на standard.
TIER_PRICE_COLUMNS = {
    "standard":    "price",
    "discount_10": "price_10kg",
    "discount_20": "price_25kg",
    "stm":         "price_stm",
}


def _user_tier(con: sqlite3.Connection, user_id: int) -> str:
    try:
        row = con.execute(
            "SELECT price_tier FROM users WHERE user_id=?", (user_id,)
        ).fetchone()
        if row and row[0]:
            return str(row[0])
    except Exception:
        pass
    return "standard"


def _user_pricing_rules(con: sqlite3.Connection, user_id: int) -> list[dict]:
    try:
        rows = con.execute(
            "SELECT id, scope, target_id, discount_pct, fixed_price, fasovka, created_at "
            "FROM user_pricing WHERE user_id=? ORDER BY id DESC",
            (user_id,),
        ).fetchall()
        redirects = _load_product_redirects()
        rules = []
        for row in rows:
            rule = dict(row)
            if rule.get("scope") == "product" and rule.get("target_id"):
                canonical_id = resolve_product_id(rule["target_id"], redirects)
                rule["target_id"] = canonical_id
                rule["canonical_product_id"] = canonical_id
            rules.append(rule)
        return rules
    except Exception:
        return []


def _matching_pricing_rules(rules: list[dict], *, tma_id: str, category: str,
                             subcategory: str, fasovka_size: str) -> list[dict]:
    """Возвращает ВСЕ применимые правила (для суммирования процентов).
    Скидки на 'all' + 'category' + 'subcategory' + 'product' складываются.
    Если есть fixed_price — он перебивает (берём минимальный)."""
    out = []
    redirects = _load_product_redirects()
    canonical_tma_id = resolve_product_id((tma_id or "").strip(), redirects)
    for r in rules:
        scope = r.get("scope")
        if scope not in {"all", "category", "subcategory", "product"}:
            continue
        tgt = (r.get("target_id") or "").strip()
        if scope == "product":
            tgt = resolve_product_id(tgt, redirects).casefold()
            if tgt != (canonical_tma_id or "").casefold():
                continue
        else:
            tgt = tgt.lower()
        if scope == "subcategory" and tgt != (subcategory or "").lower():
            continue
        if scope == "category" and tgt != (category or "").lower():
            continue
        rule_fa = (r.get("fasovka") or "").strip().lower()
        if rule_fa and rule_fa != (fasovka_size or "").lower():
            continue
        out.append(r)
    return out


def _apply_pricing_rules(base_price: float, rules: list[dict]) -> float:
    """Применяет ОДНО — самое выгодное клиенту — правило из списка.
    Скидки НЕ суммируются: берём минимальную итоговую цену."""
    if not rules:
        return base_price
    candidates = [base_price]
    for r in rules:
        fixed = r.get("fixed_price")
        if fixed is not None:
            try:
                candidates.append(float(fixed))
            except (TypeError, ValueError):
                pass
        pct = r.get("discount_pct")
        if pct is not None:
            try:
                p = float(pct)
                if 0 <= p <= 100:
                    candidates.append(round(base_price * (1 - p / 100), 2))
            except (TypeError, ValueError):
                pass
    return min(candidates)


# Тариф клиента → % автоскидки на весь заказ.
# Применяется СЕРВЕРНО при создании заказа, поверх user_pricing.
# Live_prices_api для кофе показывает оптовые цены в каталоге — там скидка уже в цене.
# Здесь же гарантируем что и чай/сироп/прочее получат тот же тариф.
TIER_AUTO_PCT = {
    "standard":    0.0,
    "discount_10": 10.0,
    "discount_20": 20.0,
    # stm — отдельный прайс, не применяем generic %
}


# Обратная совместимость для tma_handler.py (которая импортирует старые имена).
def _match_pricing_rule(rules, **kwargs):
    matched = _matching_pricing_rules(rules, **kwargs)
    return matched[0] if matched else None


def _apply_pricing_rule(base_price, rule):
    return _apply_pricing_rules(base_price, [rule] if rule else [])


def _ensure_idempotency_table():
    con = sqlite3.connect(DB_PATH)
    try:
        con.execute("""CREATE TABLE IF NOT EXISTS external_idempotency (
            key        TEXT PRIMARY KEY,
            order_id   INTEGER NOT NULL,
            created_at TEXT DEFAULT (datetime('now'))
        )""")
        con.commit()
    finally:
        con.close()


def _items_hash(items: list) -> str:
    normalized = sorted(
        (str(it.get("product_name", "")).strip().lower(),
         float(it.get("quantity", 0) or 0))
        for it in items if it.get("product_name")
    )
    blob = json.dumps(normalized, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(blob.encode()).hexdigest()[:16]


def _idem_keys(payload: dict) -> list[str]:
    """Список ключей для проверки. Первый — для записи нового заказа."""
    explicit = str(payload.get("idempotency_key", "")).strip()
    if explicit:
        return [f"ext:{explicit[:128]}"]
    phone = re.sub(r"\D", "", str(payload.get("client_phone", "")))
    if not phone:
        return []
    ih = _items_hash(payload.get("items") or [])
    bucket = int(time.time() // _IDEM_WINDOW_SEC)
    # Проверяем текущее окно + предыдущее (чтобы не промахнуться на границе).
    return [f"auto:{phone}:{ih}:{bucket}", f"auto:{phone}:{ih}:{bucket - 1}"]


def _idem_lookup(con: sqlite3.Connection, keys: list[str]) -> int | None:
    if not keys:
        return None
    placeholders = ",".join("?" * len(keys))
    row = con.execute(
        f"SELECT order_id FROM external_idempotency WHERE key IN ({placeholders}) "
        f"ORDER BY created_at DESC LIMIT 1",
        keys,
    ).fetchone()
    return row[0] if row else None


def _idem_remember(con: sqlite3.Connection, key: str, order_id: int):
    try:
        con.execute(
            "INSERT OR IGNORE INTO external_idempotency (key, order_id) VALUES (?, ?)",
            (key, order_id),
        )
    except Exception:
        pass


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
    token = _api_orders_token()
    if not token:
        return False
    return handler.headers.get("Authorization", "") == f"Bearer {token}"


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
        if token and token != _api_orders_token():
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


def _ensure_order_payment_columns(con: sqlite3.Connection) -> None:
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
        query = parse_qs(urlparse(self.path).query, keep_blank_values=True)

        # ── Healthcheck ──────────────────────────────────────────────────────
        if path in ("/", ""):
            self._send(200, b"OK", "text/plain")
            return

        # ── /admin/* — десктоп-админка (заказы, клиенты, каталог) ────────────
        # Отдельная браузерная страница вне Telegram. Авторизация — Telegram
        # Login Widget → JWT (тот же канал, что /tma/api/auth/telegram).
        if path == "/admin" or path.startswith("/admin/"):
            rel = path[len("/admin/"):] if path.startswith("/admin/") else ""
            if rel == "" or rel.endswith("/"):
                rel = (rel + "index.html").lstrip("/")
            full_path = os.path.normpath(os.path.join(ADMIN_ROOT, rel))
            if not full_path.startswith(ADMIN_ROOT):
                self._send(403, b"Forbidden", "text/plain")
                return
            if not os.path.isfile(full_path):
                full_path = os.path.join(ADMIN_ROOT, "index.html")
                if not os.path.isfile(full_path):
                    self._send(404, b"Not found", "text/plain")
                    return
            ctype, _ = mimetypes.guess_type(full_path)
            ctype = ctype or "application/octet-stream"
            try:
                with open(full_path, "rb") as f:
                    body = f.read()
                extra = {}
                if full_path.endswith((".html", ".js", ".webmanifest")):
                    extra["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
                    extra["Pragma"] = "no-cache"
                    extra["Expires"] = "0"
                elif any(full_path.endswith(e) for e in (".jpg", ".png", ".webp", ".svg", ".ico", ".css")):
                    extra["Cache-Control"] = "public, max-age=3600, must-revalidate"
                self._send(200, body, ctype, extra)
            except Exception as e:
                self._send(500, str(e).encode(), "text/plain")
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

        # ── /tma/api/user/me — профиль текущего пользователя (для автозаполнения) ─
        if path == "/tma/api/user/me":
            user = _get_request_user(self)
            if not user or not user.get("id"):
                self._send(401, _j({"error": "unauthorized"}),
                           "application/json; charset=utf-8")
                return
            try:
                con = sqlite3.connect(DB_PATH)
                con.row_factory = sqlite3.Row
                try:
                    u_cols = {r[1] for r in con.execute("PRAGMA table_info(users)").fetchall()}
                    cols = [c for c in ("name", "phone", "email", "address", "city",
                                        "company_name", "inn", "legal_address", "user_type",
                                        "pd_consent_at", "pd_consent_version")
                            if c in u_cols]
                    sel = ", ".join(cols) if cols else "user_id"
                    row = con.execute(
                        f"SELECT {sel} FROM users WHERE user_id=?", (user["id"],)
                    ).fetchone()
                    out = dict(row) if row else {}
                finally:
                    con.close()
                # Подмешаем то что в JWT/initData (имя, email-from-guest)
                if not out.get("name"):
                    out["name"] = user.get("first_name", "")
                if not out.get("phone") and user.get("phone"):
                    out["phone"] = user["phone"]
                if not out.get("email") and user.get("email"):
                    out["email"] = user["email"]
                self._send(200, _j(out),
                           "application/json; charset=utf-8", {"Cache-Control": "no-store"})
            except Exception as e:
                self._send(500, _j({"error": str(e)}), "application/json; charset=utf-8")
            return

        # ── /tma/api/my_orders ───────────────────────────────────────────────
        if path == "/tma/api/my_orders":
            user = _get_request_user(self)
            if not user or not user.get("id"):
                self._send(401, _j({"error": "auth required"}), "application/json; charset=utf-8")
                return
            uid = int(user["id"])
            try:
                con = sqlite3.connect(DB_PATH)
                con.row_factory = sqlite3.Row
                try:
                    ord_cols = {r[1] for r in con.execute("PRAGMA table_info(orders)")}
                    it_cols  = {r[1] for r in con.execute("PRAGMA table_info(order_items)")}
                    order_ids = customer_account.resolve_customer_order_ids(con, uid)[:50]
                    extra = [c for c in (
                        "total_kg", "discount", "comment", "payment_method", "paid_at"
                    ) if c in ord_cols]
                    sel = ",".join(["id", "total", "status", "created_at"] + extra)
                    name_expr    = "oi.product_name" if "product_name" in it_cols else "p.name as product_name"
                    fasovka_expr = "oi.fasovka"       if "fasovka"       in it_cols else "NULL as fasovka"
                    tma_id_expr  = "oi.tma_id"        if "tma_id"        in it_cols else "'' as tma_id"
                    redirects = _load_product_redirects()
                    out = []
                    for oid in order_ids:
                        o = con.execute(f"SELECT {sel} FROM orders WHERE id=?", (oid,)).fetchone()
                        if not o:
                            continue
                        items = con.execute(
                            f"SELECT oi.quantity, oi.price, {name_expr}, {fasovka_expr}, {tma_id_expr} "
                            f"FROM order_items oi LEFT JOIN products p ON oi.product_id=p.id "
                            f"WHERE oi.order_id=? ORDER BY oi.id",
                            (oid,),
                        ).fetchall()
                        items_out = []
                        for item in items:
                            item_out = dict(item)
                            original_id = item_out.get("tma_id") or ""
                            if original_id:
                                canonical_id = resolve_product_id(original_id, redirects)
                                item_out["tma_id"] = canonical_id
                                item_out["canonical_product_id"] = canonical_id
                            items_out.append(item_out)
                        out.append({**dict(o), "items": items_out})
                finally:
                    con.close()
                self._send(200, _j({"orders": out}), "application/json; charset=utf-8",
                           {"Cache-Control": "no-store"})
            except Exception as e:
                self._send(500, _j({"error": str(e)}), "application/json")
            return

        if path == "/tma/api/my_products":
            user = _get_request_user(self)
            if not user or not user.get("id"):
                self._send(401, _j({"error": "auth required"}), "application/json; charset=utf-8")
                return
            try:
                con = sqlite3.connect(DB_PATH)
                con.row_factory = sqlite3.Row
                try:
                    tma_ids = customer_account.get_purchased_tma_ids(con, int(user["id"]))
                finally:
                    con.close()
                tma_ids = _canonicalize_product_ids(tma_ids)
                self._send(200, _j({"tma_ids": tma_ids}), "application/json; charset=utf-8",
                           {"Cache-Control": "no-store"})
            except Exception as e:
                self._send(500, _j({"error": str(e)}), "application/json; charset=utf-8")
            return

        if path == "/tma/api/favorites":
            user = _get_request_user(self)
            if not user or not user.get("id"):
                self._send(401, _j({"error": "auth required"}), "application/json; charset=utf-8")
                return
            try:
                con = sqlite3.connect(DB_PATH)
                con.row_factory = sqlite3.Row
                try:
                    customer_account.ensure_favorites_table(con)
                    ids = customer_account.get_favorites(con, int(user["id"]))
                finally:
                    con.close()
                ids = _canonicalize_product_ids(ids)
                self._send(200, _j({"tma_ids": ids}), "application/json; charset=utf-8",
                           {"Cache-Control": "no-store"})
            except Exception as e:
                self._send(500, _j({"error": str(e)}), "application/json; charset=utf-8")
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

        # ── /tma/api/admin/user/<id>/pricing — список правил клиента ─────────
        m_pr_get = re.match(r"^/tma/api/admin/user/(-?\d+)/pricing$", path)
        if m_pr_get:
            user = _get_request_user(self)
            if not (user and user.get("id") in ADMIN_IDS):
                self._send(403, _j({"error": "Доступ запрещён"}),
                           "application/json; charset=utf-8")
                return
            uid = int(m_pr_get.group(1))
            try:
                _ensure_pricing_schema()
                con = sqlite3.connect(DB_PATH)
                con.row_factory = sqlite3.Row
                try:
                    rules = _user_pricing_rules(con, uid)
                finally:
                    con.close()
                self._send(200, _j({"rules": rules}),
                           "application/json; charset=utf-8", {"Cache-Control": "no-cache"})
            except Exception as e:
                self._send(500, _j({"error": str(e)}), "application/json; charset=utf-8")
            return

        # ── /tma/api/admin/catalog_meta — категории + подкатегории + товары
        #     для select-фильтров в редакторах. Без полной products.json (легче).
        if path == "/tma/api/admin/catalog_meta":
            user = _get_request_user(self)
            if not (user and user.get("id") in ADMIN_IDS):
                self._send(403, _j({"error": "Доступ запрещён"}),
                           "application/json; charset=utf-8")
                return
            try:
                with open(os.path.join(TMA_ROOT, "products.json"), encoding="utf-8") as f:
                    pdata = json.load(f)
                prods = pdata if isinstance(pdata, list) else (pdata.get("products") or [])
                cats = sorted({(p.get("category") or "").strip()
                               for p in prods if p.get("category")})
                subs_by_cat: dict[str, set] = {}
                fasovkas: set = set()
                for p in prods:
                    c = (p.get("category") or "").strip()
                    s = (p.get("subcategory") or "").strip()
                    if c and s:
                        subs_by_cat.setdefault(c, set()).add(s)
                    for fa in (p.get("fasovka") or []):
                        fz = (fa.get("size") or "").strip()
                        if fz:
                            fasovkas.add(fz)
                self._send(200, _j({
                    "categories": cats,
                    "subcategories": {k: sorted(v) for k, v in subs_by_cat.items()},
                    "fasovkas": sorted(fasovkas),
                    "products_lite": [
                        {"id": p.get("id"), "name": p.get("name"),
                         "category": p.get("category"), "subcategory": p.get("subcategory")}
                        for p in prods if p.get("id") and p.get("name")
                    ],
                }), "application/json; charset=utf-8", {"Cache-Control": "no-cache, max-age=60"})
            except Exception as e:
                self._send(500, _j({"error": str(e)}), "application/json; charset=utf-8")
            return

        # ── /tma/api/admin/quarantine → прокси на VPS ─────────────────────────
        if path == "/tma/api/admin/quarantine":
            user = _get_request_user(self)
            if not (user and user.get("id") in ADMIN_IDS):
                self._send(403, _j({"error": "Доступ запрещён"}),
                           "application/json; charset=utf-8")
                return
            status, rbody, rctype = _proxy_shop_admin("GET", "/admin/quarantine")
            self._send(status, rbody, rctype, {"Cache-Control": "no-cache"})
            return

        # ── /tma/api/admin/pending → прокси на VPS ────────────────────────────
        if path == "/tma/api/admin/pending":
            user = _get_request_user(self)
            if not (user and user.get("id") in ADMIN_IDS):
                self._send(403, _j({"error": "Доступ запрещён"}),
                           "application/json; charset=utf-8")
                return
            status, rbody, rctype = _proxy_shop_admin("GET", "/admin/pending")
            self._send(status, rbody, rctype, {"Cache-Control": "no-cache"})
            return

        # ── /tma/api/admin/* ─────────────────────────────────────────────────
        m_admin = re.match(
            r"^/tma/api/admin/(orders|users|order/(\d+)|user/(-?\d+))$", path
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
                                if d.get("tma_id"):
                                    canonical_id = meta.get("canonical_product_id") or d["tma_id"]
                                    d["tma_id"] = canonical_id
                                    d["canonical_product_id"] = canonical_id
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

        # Immutable publication-verification view.  This deliberately returns
        # the exact file bytes; the normal route below remains dynamically
        # enriched for shop clients.
        canonical_values = query.get("canonical")
        if path == "/tma/products.json" and canonical_values is not None \
                and (not canonical_values or any(value != "1" for value in canonical_values)):
            self._send(400, b'Invalid canonical query', "text/plain",
                       {"Cache-Control": "no-store"})
            return
        if path == "/tma/products.json" and canonical_values \
                and all(value == "1" for value in canonical_values):
            try:
                with open(os.path.join(TMA_ROOT, "products.json"), "rb") as f:
                    body = f.read()
                self._send(
                    200, body, "application/json; charset=utf-8",
                    {"Cache-Control": "no-store"},
                )
            except Exception as e:
                self._send(500, str(e).encode(), "text/plain")
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

        # ── /tma/v1/* статика (старый дизайн на отдельном префиксе) ──────────
        if path.startswith("/tma/v1/") or path == "/tma/v1":
            rel = path[8:] if path.startswith("/tma/v1/") else ""
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

        # ── /tma/v2/* — алиас на новый дизайн (для обратной совместимости старых ссылок)
        if path.startswith("/tma/v2/") or path == "/tma/v2":
            rel = path[8:] if path.startswith("/tma/v2/") else ""
            if rel == "" or rel.endswith("/"):
                rel = (rel + "index.html").lstrip("/")
            full_path = os.path.normpath(os.path.join(TMA_ROOT_V2, rel))
            if not full_path.startswith(TMA_ROOT_V2):
                self._send(403, b"Forbidden", "text/plain")
                return
            if not os.path.isfile(full_path):
                full_path = os.path.normpath(os.path.join(TMA_ROOT, rel))
                if not full_path.startswith(TMA_ROOT) or not os.path.isfile(full_path):
                    full_path = os.path.join(TMA_ROOT_V2, "index.html")
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
                elif full_path.endswith((".html", "sw.js", ".webmanifest")):
                    extra["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
                    extra["Pragma"] = "no-cache"
                    extra["Expires"] = "0"
                self._send(200, body, ctype, extra)
            except Exception as e:
                self._send(500, str(e).encode(), "text/plain")
            return

        # ── /tma/* — статика: сначала ищем в v2 (новый дизайн = default),
        #            если нет — фолбэк в v1 (общие assets, photos, products.json)
        if path.startswith("/tma/") or path == "/tma":
            rel = path[5:] if path.startswith("/tma/") else ""
            if rel == "" or rel.endswith("/"):
                rel = (rel + "index.html").lstrip("/")
            full_path = os.path.normpath(os.path.join(TMA_ROOT_V2, rel))
            if not (full_path.startswith(TMA_ROOT_V2) and os.path.isfile(full_path)):
                full_path = os.path.normpath(os.path.join(TMA_ROOT, rel))
                if not full_path.startswith(TMA_ROOT):
                    self._send(403, b"Forbidden", "text/plain")
                    return
                if not os.path.isfile(full_path):
                    full_path = os.path.join(TMA_ROOT_V2, "index.html")
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
                elif full_path.endswith((".html", "sw.js", ".webmanifest")):
                    extra["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
                    extra["Pragma"] = "no-cache"
                    extra["Expires"] = "0"
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

        # ── Concierge (Bearer, для Бишопа): ghost-клиент + claim-ссылка ──────
        if path == "/admin/concierge/client":
            if cz is None:
                self._send(503, _j({"error": "concierge unavailable"}), "application/json; charset=utf-8")
                return
            if not _check_bearer(self):
                self._send(401, _j({"error": "unauthorized"}), "application/json; charset=utf-8")
                return
            body = self._read_body() or {}
            phone = str(body.get("phone") or body.get("client_phone") or "").strip()
            if not phone:
                self._send(400, _j({"error": "phone required"}), "application/json; charset=utf-8")
                return
            try:
                con = sqlite3.connect(DB_PATH)
                try:
                    cz.ensure_schema(con)
                    res = cz.create_or_get_client(
                        con, phone=phone,
                        name=body.get("name") or body.get("client_name"),
                        user_type=(body.get("user_type") or "individual"),
                        company_name=body.get("company_name"), inn=body.get("inn"),
                        legal_address=body.get("legal_address"),
                        actual_address=body.get("actual_address"),
                        email=body.get("email"),
                    )
                finally:
                    con.close()
                _invalidate_sync_cache()
                self._send(200, _j({"ok": True, **res}), "application/json; charset=utf-8")
            except Exception as e:
                self._send(500, _j({"error": str(e)}), "application/json; charset=utf-8")
            return

        if path == "/admin/concierge/claim_link":
            if cz is None:
                self._send(503, _j({"error": "concierge unavailable"}), "application/json; charset=utf-8")
                return
            if not _check_bearer(self):
                self._send(401, _j({"error": "unauthorized"}), "application/json; charset=utf-8")
                return
            body = self._read_body() or {}
            try:
                uid = int(body.get("user_id"))
            except (TypeError, ValueError):
                self._send(400, _j({"error": "user_id required"}), "application/json; charset=utf-8")
                return
            try:
                con = sqlite3.connect(DB_PATH)
                try:
                    out = cz.issue_claim(con, uid)
                finally:
                    con.close()
                base = os.environ.get("APP_PUBLIC_URL", "").rstrip("/")
                out["url"] = f"{base}/tma/v2/?claim={out['token']}"
                self._send(200, _j({"ok": True, **out}), "application/json; charset=utf-8")
            except ValueError as e:
                self._send(404, _j({"error": str(e)}), "application/json; charset=utf-8")
            except Exception as e:
                self._send(500, _j({"error": str(e)}), "application/json; charset=utf-8")
            return

        # ── Claim: клиент «забирает» ghost (нужна TG-авторизация клиента) ────
        if path == "/tma/api/claim":
            if cz is None:
                self._send(503, _j({"error": "concierge unavailable"}), "application/json; charset=utf-8")
                return
            user = _get_request_user(self)
            if not user or not user.get("id") or int(user["id"]) <= 0:
                self._send(401, _j({"error": "Требуется вход через Telegram"}),
                           "application/json; charset=utf-8")
                return
            body = self._read_body() or {}
            token = str(body.get("token") or "").strip()
            if not token:
                self._send(400, _j({"error": "token required"}), "application/json; charset=utf-8")
                return
            try:
                con = sqlite3.connect(DB_PATH)
                try:
                    real_name = " ".join(filter(None, [user.get("first_name"), user.get("last_name")])) or None
                    res = cz.redeem_claim(con, token, int(user["id"]), real_name=real_name)
                finally:
                    con.close()
                if not res.get("ok"):
                    code = {"not_found": 404, "already_used": 409, "expired": 410}.get(res.get("error"), 400)
                    self._send(code, _j(res), "application/json; charset=utf-8")
                    return
                _invalidate_sync_cache()
                self._send(200, _j(res), "application/json; charset=utf-8")
            except Exception as e:
                self._send(500, _j({"error": str(e)}), "application/json; charset=utf-8")
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

        # ── Авторизация из Panel (внутренний токен) → JWT ───────────────────
        if path == "/tma/api/auth/panel_token":
            auth_hdr = self.headers.get("Authorization", "")
            auth_token = auth_hdr[7:].strip() if auth_hdr.startswith("Bearer ") else ""
            panel_tokens = {
                tok.strip()
                for tok in (
                    os.environ.get("PANEL_INTERNAL_TOKEN", ""),
                    os.environ.get("SHOP_ADMIN_TOKEN", ""),
                    os.environ.get("SHOP_INTERNAL_TOKEN", ""),
                )
                if tok.strip()
            }
            if not panel_tokens or auth_token not in panel_tokens:
                self._send(403, _j({"error": "forbidden"}),
                           "application/json; charset=utf-8")
                return
            payload = self._read_body() or {}
            tg_id = int(payload.get("tg_id", 0))
            name  = str(payload.get("name", "")).strip()[:80] or "Admin"
            if not tg_id:
                self._send(400, _j({"error": "tg_id required"}),
                           "application/json; charset=utf-8")
                return
            user = {"id": tg_id, "first_name": name, "last_name": "",
                    "username": "", "photo_url": ""}
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

        m_pay = re.match(r"^/tma/api/order/(\d+)/pay$", path)
        if m_pay:
            self._handle_tma_order_pay(int(m_pay.group(1)))
            return

        if path == "/tma/api/user/type":
            user = _get_request_user(self)
            if not user or not user.get("id"):
                self._send(401, _j({"error": "auth required"}), "application/json; charset=utf-8")
                return
            body = self._read_body() or {}
            try:
                con = sqlite3.connect(DB_PATH)
                try:
                    customer_account.set_user_type(con, int(user["id"]),
                                                   str(body.get("user_type", "")))
                finally:
                    con.close()
                self._send(200, _j({"ok": True}), "application/json; charset=utf-8")
            except ValueError as e:
                self._send(400, _j({"error": str(e)}), "application/json; charset=utf-8")
            except Exception as e:
                self._send(500, _j({"error": str(e)}), "application/json; charset=utf-8")
            return

        if path == "/tma/api/favorites/toggle":
            user = _get_request_user(self)
            if not user or not user.get("id"):
                self._send(401, _j({"error": "auth required"}), "application/json; charset=utf-8")
                return
            body = self._read_body() or {}
            tma_id = str(body.get("tma_id", "")).strip()
            if not tma_id:
                self._send(400, _j({"error": "tma_id required"}), "application/json; charset=utf-8")
                return
            redirects = _load_product_redirects()
            canonical_id = resolve_product_id(tma_id, redirects)
            try:
                con = sqlite3.connect(DB_PATH)
                con.row_factory = sqlite3.Row
                try:
                    customer_account.ensure_favorites_table(con)
                    user_id = int(user["id"])
                    existing_ids = customer_account.get_favorites(con, user_id)
                    aliases = [
                        existing_id
                        for existing_id in existing_ids
                        if resolve_product_id(existing_id, redirects) == canonical_id
                    ]
                    if aliases:
                        con.executemany(
                            "DELETE FROM favorites WHERE user_id=? AND tma_id=?",
                            [(user_id, alias) for alias in aliases],
                        )
                        con.commit()
                        ids = customer_account.get_favorites(con, user_id)
                    else:
                        ids = customer_account.toggle_favorite(
                            con,
                            user_id,
                            canonical_id,
                        )
                finally:
                    con.close()
                ids = _canonicalize_product_ids(ids)
                self._send(
                    200,
                    _j({
                        "tma_ids": ids,
                        "canonical_product_id": canonical_id,
                    }),
                    "application/json; charset=utf-8",
                )
            except Exception as e:
                self._send(500, _j({"error": str(e)}), "application/json; charset=utf-8")
            return

        if path == "/tma/api/favorites/merge":
            user = _get_request_user(self)
            if not user or not user.get("id"):
                self._send(401, _j({"error": "auth required"}), "application/json; charset=utf-8")
                return
            body = self._read_body() or {}
            tma_ids = body.get("tma_ids") or []
            if not isinstance(tma_ids, list):
                self._send(400, _j({"error": "tma_ids must be a list"}), "application/json; charset=utf-8")
                return
            canonical_ids = _canonicalize_product_ids(tma_ids)
            try:
                con = sqlite3.connect(DB_PATH)
                con.row_factory = sqlite3.Row
                try:
                    customer_account.ensure_favorites_table(con)
                    ids = customer_account.merge_favorites(
                        con,
                        int(user["id"]),
                        canonical_ids,
                    )
                finally:
                    con.close()
                ids = _canonicalize_product_ids(ids)
                self._send(200, _j({"tma_ids": ids}), "application/json; charset=utf-8")
            except Exception as e:
                self._send(500, _j({"error": str(e)}), "application/json; charset=utf-8")
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
            requested_id = unquote(m_photo.group(1))
            tma_id = resolve_product_id(requested_id, _load_product_redirects())
            status, rbody, rctype = _proxy_shop_admin(
                "POST", f"/admin/product/{quote(tma_id, safe='')}/photo",
                body=raw, content_type=ctype_in,
            )
            rbody = _add_canonical_product_id_to_response(
                rbody,
                rctype,
                requested_id=requested_id,
                canonical_id=tma_id,
            )
            self._send(status, rbody, rctype)
            return

        # ── Удалить товар (admin) → прокси на VPS ──────────────────────────
        m_del = re.match(r"^/tma/api/admin/product/([^/]+)/delete$", path)
        if m_del:
            user = _get_request_user(self)
            if not (user and user.get("id") in ADMIN_IDS):
                self._send(403, _j({"error": "Доступ запрещён"}),
                           "application/json; charset=utf-8")
                return
            requested_id = unquote(m_del.group(1))
            tma_id = resolve_product_id(requested_id, _load_product_redirects())
            status, rbody, rctype = _proxy_shop_admin(
                "POST", f"/admin/product/{quote(tma_id, safe='')}/delete",
                body=b"", content_type="application/json",
            )
            rbody = _add_canonical_product_id_to_response(
                rbody,
                rctype,
                requested_id=requested_id,
                canonical_id=tma_id,
            )
            self._send(status, rbody, rctype)
            return

        # ── Создать карточку из pending (admin) → прокси на VPS ─────────────
        if path == "/tma/api/admin/pending/create":
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
            status, rbody, rctype = _proxy_shop_admin(
                "POST", "/admin/pending/create",
                body=raw, content_type="application/json",
            )
            self._send(status, rbody, rctype)
            return

        # ── Убрать позицию из pending без создания (admin) ──────────────────
        if path == "/tma/api/admin/pending/discard":
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
            status, rbody, rctype = _proxy_shop_admin(
                "POST", "/admin/pending/discard",
                body=raw, content_type="application/json",
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
            requested_id = unquote(m_prod.group(1))
            tma_id = resolve_product_id(requested_id, _load_product_redirects())
            status, rbody, rctype = _proxy_shop_admin(
                "PATCH", f"/admin/product/{quote(tma_id, safe='')}",
                body=raw, content_type="application/json",
            )
            rbody = _add_canonical_product_id_to_response(
                rbody,
                rctype,
                requested_id=requested_id,
                canonical_id=tma_id,
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

        # ── POST /tma/api/admin/user/<id> — апдейт профиля (реквизиты + tier) ─
        m_user_upd = re.match(r"^/tma/api/admin/user/(-?\d+)$", path)
        if m_user_upd:
            user = _get_request_user(self)
            if not (user and user.get("id") in ADMIN_IDS):
                self._send(403, _j({"error": "Доступ запрещён"}),
                           "application/json; charset=utf-8")
                return
            uid = int(m_user_upd.group(1))
            payload = self._read_body()
            ALLOWED_FIELDS = {
                "user_type", "name", "phone", "email",
                "company_name", "inn", "legal_address", "actual_address",
                "price_tier",
            }
            VALID_TIERS = {"standard", "discount_10", "discount_20", "stm"}
            updates = {}
            for k, v in (payload or {}).items():
                if k not in ALLOWED_FIELDS:
                    continue
                if v is None:
                    continue
                sv = str(v).strip()
                if k == "price_tier" and sv and sv not in VALID_TIERS:
                    self._send(400, _j({"error": f"price_tier must be one of {sorted(VALID_TIERS)}"}),
                               "application/json; charset=utf-8")
                    return
                updates[k] = sv
            if not updates:
                self._send(400, _j({"error": "no fields to update"}),
                           "application/json; charset=utf-8")
                return
            try:
                _ensure_pricing_schema()
                con = sqlite3.connect(DB_PATH)
                con.row_factory = sqlite3.Row
                try:
                    row = con.execute("SELECT 1 FROM users WHERE user_id=?", (uid,)).fetchone()
                    if not row:
                        self._send(404, _j({"error": "Клиент не найден"}),
                                   "application/json; charset=utf-8")
                        return
                    set_clause = ", ".join(f"{k}=?" for k in updates.keys())
                    con.execute(
                        f"UPDATE users SET {set_clause} WHERE user_id=?",
                        (*updates.values(), uid),
                    )
                    con.commit()
                    u = con.execute("SELECT * FROM users WHERE user_id=?", (uid,)).fetchone()
                finally:
                    con.close()
                self._send(200, _j({"ok": True, "user": dict(u)}),
                           "application/json; charset=utf-8")
            except Exception as e:
                self._send(500, _j({"error": str(e)}), "application/json; charset=utf-8")
            return

        # ── POST /tma/api/admin/user/<id>/pricing — добавить персональное правило ─
        m_pr_add = re.match(r"^/tma/api/admin/user/(-?\d+)/pricing$", path)
        if m_pr_add:
            user = _get_request_user(self)
            if not (user and user.get("id") in ADMIN_IDS):
                self._send(403, _j({"error": "Доступ запрещён"}),
                           "application/json; charset=utf-8")
                return
            uid = int(m_pr_add.group(1))
            payload = self._read_body() or {}
            scope = (payload.get("scope") or "").strip().lower()
            target_id = (payload.get("target_id") or "").strip()
            fasovka = (payload.get("fasovka") or "").strip() or None
            if scope not in {"product", "subcategory", "category", "all"}:
                self._send(400, _j({"error": "scope must be product|subcategory|category|all"}),
                           "application/json; charset=utf-8")
                return
            if scope == "all":
                target_id = "*"
            elif not target_id:
                self._send(400, _j({"error": "target_id required"}),
                           "application/json; charset=utf-8")
                return
            elif scope == "product":
                target_id = resolve_product_id(
                    target_id,
                    _load_product_redirects(),
                )
            discount_pct = payload.get("discount_pct")
            fixed_price = payload.get("fixed_price")
            if (discount_pct is None) == (fixed_price is None):
                self._send(400, _j({"error": "either discount_pct or fixed_price (not both)"}),
                           "application/json; charset=utf-8")
                return
            if discount_pct is not None:
                try:
                    discount_pct = float(discount_pct)
                except (TypeError, ValueError):
                    self._send(400, _j({"error": "discount_pct must be number"}),
                               "application/json; charset=utf-8")
                    return
                if not (0 <= discount_pct <= 100):
                    self._send(400, _j({"error": "discount_pct in 0..100"}),
                               "application/json; charset=utf-8")
                    return
                fixed_price = None
            else:
                try:
                    fixed_price = float(fixed_price)
                except (TypeError, ValueError):
                    self._send(400, _j({"error": "fixed_price must be number"}),
                               "application/json; charset=utf-8")
                    return
                if fixed_price < 0:
                    self._send(400, _j({"error": "fixed_price >= 0"}),
                               "application/json; charset=utf-8")
                    return
                discount_pct = None
            try:
                _ensure_pricing_schema()
                con = sqlite3.connect(DB_PATH)
                try:
                    u = con.execute("SELECT 1 FROM users WHERE user_id=?", (uid,)).fetchone()
                    if not u:
                        self._send(404, _j({"error": "Клиент не найден"}),
                                   "application/json; charset=utf-8")
                        return
                    # Upsert: при совпадении (user_id, scope, target_id, fasovka) — обновляем.
                    con.execute(
                        "INSERT INTO user_pricing "
                        "(user_id, scope, target_id, discount_pct, fixed_price, fasovka) "
                        "VALUES (?,?,?,?,?,?) "
                        "ON CONFLICT(user_id, scope, target_id, fasovka) DO UPDATE SET "
                        "discount_pct=excluded.discount_pct, fixed_price=excluded.fixed_price",
                        (uid, scope, target_id, discount_pct, fixed_price, fasovka),
                    )
                    con.commit()
                    rule_id = con.execute("SELECT last_insert_rowid()").fetchone()[0]
                finally:
                    con.close()
                response = {"ok": True, "id": rule_id}
                if scope == "product":
                    response["canonical_product_id"] = target_id
                self._send(200, _j(response),
                           "application/json; charset=utf-8")
            except Exception as e:
                self._send(500, _j({"error": str(e)}), "application/json; charset=utf-8")
            return

        # ── POST /tma/api/admin/pricing/<rule_id>/delete ─────────────────────
        m_pr_del = re.match(r"^/tma/api/admin/pricing/(\d+)/delete$", path)
        if m_pr_del:
            user = _get_request_user(self)
            if not (user and user.get("id") in ADMIN_IDS):
                self._send(403, _j({"error": "Доступ запрещён"}),
                           "application/json; charset=utf-8")
                return
            rid = int(m_pr_del.group(1))
            try:
                _ensure_pricing_schema()
                con = sqlite3.connect(DB_PATH)
                try:
                    con.execute("DELETE FROM user_pricing WHERE id=?", (rid,))
                    con.commit()
                finally:
                    con.close()
                self._send(200, _j({"ok": True}), "application/json; charset=utf-8")
            except Exception as e:
                self._send(500, _j({"error": str(e)}), "application/json; charset=utf-8")
            return

        # ── POST /tma/api/admin/dm/<id> — DM админа клиенту (без привязки к заказу) ─
        m_dm = re.match(r"^/tma/api/admin/dm/(\d+)$", path)
        if m_dm:
            user = _get_request_user(self)
            if not (user and user.get("id") in ADMIN_IDS):
                self._send(403, _j({"error": "Доступ запрещён"}),
                           "application/json; charset=utf-8")
                return
            target_uid = int(m_dm.group(1))
            payload = self._read_body()
            text = (payload.get("text") or "").strip()
            if not text:
                self._send(400, _j({"error": "text required"}),
                           "application/json; charset=utf-8")
                return
            if len(text) > 2000:
                self._send(400, _j({"error": "too long"}),
                           "application/json; charset=utf-8")
                return
            admin_full = " ".join(filter(None, [user.get("first_name"), user.get("last_name")])) \
                or user.get("username") or "Поддержка"
            if _TMA_CTX is None:
                self._send(503, _j({"error": "API не инициализирован"}),
                           "application/json; charset=utf-8")
                return
            note = f"💬 <b>Сообщение от {admin_full}:</b>\n\n{text}"
            # Пишем в chat_messages для истории (order_id=0 — DM вне заказа)
            try:
                _ensure_chat_table()
                con = sqlite3.connect(DB_PATH)
                try:
                    con.execute(
                        "INSERT INTO chat_messages "
                        "(order_id, user_id, user_name, is_admin, text) VALUES (0,?,?,1,?)",
                        (target_uid, admin_full, text),
                    )
                    con.commit()
                finally:
                    con.close()
            except Exception:
                pass
            # Шлём в Telegram. Ловим ошибки доставки.
            send_result = {"ok": True}
            async def _send_dm():
                try:
                    await _TMA_CTX["bot"].send_message(target_uid, note, parse_mode="HTML")
                except Exception as e:
                    send_result["ok"] = False
                    send_result["error"] = str(e)
            try:
                fut = asyncio.run_coroutine_threadsafe(_send_dm(), _TMA_CTX["loop"])
                fut.result(timeout=10)
            except Exception as e:
                send_result = {"ok": False, "error": str(e)}
            if not send_result["ok"]:
                err = send_result.get("error", "")
                hint = "Клиент заблокировал бота или ещё не начинал диалог" if "blocked" in err.lower() or "chat not found" in err.lower() else err
                self._send(502, _j({"ok": False, "error": hint}),
                           "application/json; charset=utf-8")
                return
            self._send(200, _j({"ok": True}), "application/json; charset=utf-8")
            return

        self._send(404, b"Not found", "text/plain")

    def _handle_tma_order_pay(self, order_id: int):
        if _TMA_CTX is None:
            self._send(503, _j({"ok": False, "error": "API не инициализирован"}),
                       "application/json; charset=utf-8")
            return
        user = _get_request_user(self)
        if not user or not user.get("id"):
            self._send(401, _j({"ok": False, "error": "Требуется авторизация"}),
                       "application/json; charset=utf-8")
            return
        uid = int(user["id"])
        try:
            con = sqlite3.connect(DB_PATH)
            con.row_factory = sqlite3.Row
            try:
                _ensure_order_payment_columns(con)
                order_ids = {int(x) for x in customer_account.resolve_customer_order_ids(con, uid)[:200]}
                if int(order_id) not in order_ids:
                    self._send(403, _j({"ok": False, "error": "Заказ не найден в вашем профиле"}),
                               "application/json; charset=utf-8")
                    return
                order = con.execute(
                    "SELECT id, user_id, total, status, payment_method, paid_at, phone FROM orders WHERE id=?",
                    (order_id,),
                ).fetchone()
                if not order:
                    self._send(404, _j({"ok": False, "error": "Заказ не найден"}),
                               "application/json; charset=utf-8")
                    return
                if (order["status"] or "").lower() == "paid" or order["paid_at"]:
                    self._send(200, _j({
                        "ok": True, "already_paid": True,
                        "order_id": order_id, "total": order["total"],
                    }), "application/json; charset=utf-8")
                    return
                it_cols = {r[1] for r in con.execute("PRAGMA table_info(order_items)").fetchall()}
                name_expr = "oi.product_name" if "product_name" in it_cols else "p.name as product_name"
                fasovka_expr = "oi.fasovka" if "fasovka" in it_cols else "NULL as fasovka"
                rows = con.execute(
                    f"SELECT oi.quantity, oi.price, {name_expr}, {fasovka_expr} "
                    f"FROM order_items oi LEFT JOIN products p ON oi.product_id=p.id "
                    f"WHERE oi.order_id=? ORDER BY oi.id",
                    (order_id,),
                ).fetchall()
                items = [{
                    "name": r["product_name"] or "Товар Roastberry",
                    "fasovka": r["fasovka"] or "",
                    "qty": int(r["quantity"] or 0),
                    "price": float(r["price"] or 0),
                } for r in rows if int(r["quantity"] or 0) > 0]
                if not items:
                    self._send(400, _j({"ok": False, "error": "В заказе нет позиций для оплаты"}),
                               "application/json; charset=utf-8")
                    return
                total = float(order["total"] or sum((i["price"] or 0) * i["qty"] for i in items))
                receipt_contact = {
                    "order_id": order_id,
                    "phone": order["phone"] or "",
                    "email": "",
                }
                u_cols = {r[1] for r in con.execute("PRAGMA table_info(users)").fetchall()}
                user_key = "user_id" if "user_id" in u_cols else ("id" if "id" in u_cols else "")
                if user_key:
                    wanted = [c for c in ("phone", "email") if c in u_cols]
                    if wanted:
                        profile = con.execute(
                            f"SELECT {','.join(wanted)} FROM users WHERE {user_key}=?",
                            (order["user_id"],),
                        ).fetchone()
                        if profile:
                            if not receipt_contact["phone"] and "phone" in wanted:
                                receipt_contact["phone"] = profile["phone"] or ""
                            if "email" in wanted:
                                receipt_contact["email"] = profile["email"] or ""
                con.execute(
                    "UPDATE orders SET payment_method=? WHERE id=?",
                    ("online", order_id),
                )
                con.commit()
            finally:
                con.close()

            from tma_handler import create_payment_invoice_link
            fut = asyncio.run_coroutine_threadsafe(
                create_payment_invoice_link(
                    _TMA_CTX["bot"], order_id, items, total, receipt_contact,
                ),
                _TMA_CTX["loop"],
            )
            invoice_link = fut.result(timeout=20)
            if not invoice_link:
                self._send(500, _j({"ok": False, "error": "Не удалось создать ссылку оплаты"}),
                           "application/json; charset=utf-8")
                return
            _invalidate_sync_cache()
            self._send(200, _j({
                "ok": True, "order_id": order_id, "total": total,
                "invoice_link": invoice_link,
            }), "application/json; charset=utf-8")
        except Exception as e:
            self._send(500, _j({"ok": False, "error": f"server error: {e}"}),
                       "application/json; charset=utf-8")

    def _handle_tma_order(self):
        if _TMA_CTX is None:
            self._send(503, _j({"ok": False, "error": "API не инициализирован"}),
                       "application/json")
            return
        user = _get_request_user(self)
        if not user or not user.get("id"):
            self._send(401, _j({"ok": False,
                                "error": "Требуется авторизация (Telegram или гостевой вход)"}),
                       "application/json; charset=utf-8")
            return
        payload, migrated_product_ids = _canonicalize_order_payload(
            self._read_body()
        )
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
        response = {"ok": True, **result}
        if migrated_product_ids:
            response["canonical_product_ids"] = migrated_product_ids
        self._send(200, _j(response), "application/json; charset=utf-8")

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

        # Idempotency: ловим повторный webhook (сеть, ретрай Девида).
        _ensure_idempotency_table()
        idem_keys = _idem_keys(payload)
        if idem_keys:
            try:
                con_idem = sqlite3.connect(DB_PATH)
                con_idem.row_factory = sqlite3.Row
                try:
                    dup_oid = _idem_lookup(con_idem, idem_keys)
                    if dup_oid:
                        existing = con_idem.execute(
                            "SELECT id, total, discount, "
                            "(SELECT COUNT(*) FROM order_items WHERE order_id=orders.id) AS items_count "
                            "FROM orders WHERE id=?", (dup_oid,)
                        ).fetchone()
                        if existing:
                            self._send(200, _j({
                                "status": "ok",
                                "order_id": existing["id"],
                                "total": existing["total"],
                                "discount_pct": existing["discount"],
                                "items_count": existing["items_count"],
                                "duplicate": True,
                            }), "application/json; charset=utf-8")
                            return
                finally:
                    con_idem.close()
            except Exception as e:
                print(f"[idempotency] lookup failed: {e}")

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
                    # Стабильный id из телефона (sha256) — та же схема, что у гостевого
                    # входа и concierge, чтобы заказ/гость/claim сматчились. Старый hash()
                    # солился на процесс → плейсхолдеры не совпадали между рестартами.
                    if cz is not None and norm_phone:
                        new_uid = cz.stable_ghost_id(norm_phone)
                    else:
                        new_uid = -(abs(hash(norm_phone or phone)) % (10**9))
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

                # Универсальный парс веса из имени товара: "1 кг", "200 г", "250 г" и т.д.
                # Не только кофе — чай / прочее весомое тоже идёт в общий вес.
                weight_re = re.compile(r"(\d+(?:[\.,]\d+)?)\s*(кг|г)\b", re.IGNORECASE)
                total_kg = 0.0
                for ri in resolved_items:
                    m = weight_re.search(ri["product_name"].lower())
                    if m:
                        v = float(m.group(1).replace(",", "."))
                        if m.group(2).lower() == "г":
                            v /= 1000
                        total_kg += v * ri["quantity"]
                # Применяем tier клиента (если есть в БД).
                _ensure_pricing_schema()
                tier_name = _user_tier(con, user_id)
                tier_pct = TIER_AUTO_PCT.get(tier_name, 0.0)
                # Auto-volume скидка (старая логика, через get_discount от bot.py).
                gd = _TMA_CTX.get("get_discount")
                auto_pct = float(gd(total_kg)) * 100 if gd else 0.0
                # НЕ суммируем — берём max (бизнес-правило: одна скидка на заказ).
                final_pct = max(tier_pct, auto_pct)
                discount_pct = final_pct / 100.0
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
                if idem_keys:
                    _idem_remember(con, idem_keys[0], order_id)
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
