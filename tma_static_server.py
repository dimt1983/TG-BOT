"""
tma_static_server.py — раздача Mini App статики на том же порту что и healthcheck.

Заменяет run_server() в bot.py: вместо простого "OK" на /, отдаёт:
- GET /         → healthcheck "OK"
- GET /tma/*    → файлы из ./tma_static/

Использование (в bot.py заменить блок run_server):
    from tma_static_server import run_server
    threading.Thread(target=run_server, daemon=True).start()
"""
import os
import json
import threading
import mimetypes
import sqlite3
import time
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import unquote

PORT = int(os.environ.get("PORT", 10000))
TMA_ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "tma_static")
PRICE_SYNC_ENABLED = os.environ.get("BISHOP_PRICE_SYNC", "1") != "0"

# /sync — снимок БД для внешних агентов (Девид и т.д.).
# Доступ только с Authorization: Bearer <API_ORDERS_TOKEN>.
DB_PATH = os.environ.get("DB_PATH", "/app/data/shop.db")
API_ORDERS_TOKEN = os.environ.get("API_ORDERS_TOKEN", "")


def _check_bearer(handler) -> bool:
    """True, если в Authorization есть валидный Bearer-токен."""
    if not API_ORDERS_TOKEN:
        return False
    auth = handler.headers.get("Authorization", "")
    return auth == f"Bearer {API_ORDERS_TOKEN}"


def _read_sync_snapshot() -> dict:
    """Читает orders/users/products из shop.db, как _SyncHandler в bot.py."""
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


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        path = unquote(self.path.split("?", 1)[0])

        # Healthcheck
        if path == "/" or path == "":
            self._send(200, b"OK", "text/plain")
            return

        # /sync — публичный снимок БД для агентов, защищён Bearer-токеном
        if path == "/sync":
            if not _check_bearer(self):
                self._send(401, b'{"error":"unauthorized"}', "application/json")
                return
            try:
                data = _read_sync_snapshot()
                body = json.dumps(data, ensure_ascii=False).encode("utf-8")
                self._send(200, body, "application/json; charset=utf-8")
            except Exception as e:
                self._send(500, str(e).encode(), "text/plain")
            return

        # /tma/api/my_orders?tg_id=XXX — история заказов пользователя
        if path == "/tma/api/my_orders":
            from urllib.parse import urlparse, parse_qs
            qs = parse_qs(urlparse(self.path).query)
            tg_id = qs.get("tg_id", [""])[0]
            if not tg_id or not tg_id.isdigit():
                self._send(400, b'{"error":"tg_id required"}', "application/json")
                return
            try:
                con = sqlite3.connect(DB_PATH)
                con.row_factory = sqlite3.Row
                try:
                    orders = con.execute(
                        "SELECT id,total,total_kg,discount,status,comment,created_at "
                        "FROM orders WHERE user_id=? ORDER BY id DESC LIMIT 50",
                        (int(tg_id),)
                    ).fetchall()
                    out = []
                    for o in orders:
                        items = con.execute(
                            "SELECT product_name,fasovka,quantity,price "
                            "FROM order_items WHERE order_id=? ORDER BY id",
                            (o["id"],)
                        ).fetchall()
                        out.append({
                            "id": o["id"], "total": o["total"], "total_kg": o["total_kg"],
                            "discount": o["discount"], "status": o["status"],
                            "comment": o["comment"], "created_at": o["created_at"],
                            "items": [dict(i) for i in items],
                        })
                finally:
                    con.close()
                body = json.dumps({"orders": out}, ensure_ascii=False).encode("utf-8")
                self._send(200, body, "application/json; charset=utf-8",
                           {"Cache-Control": "no-cache"})
            except Exception as e:
                self._send(500, json.dumps({"error": str(e)}).encode(), "application/json")
            return

        # /tma/products.json — динамически с актуальными ценами от Bishop
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
                body = json.dumps(data, ensure_ascii=False).encode("utf-8")
                self._send(200, body, "application/json", {"Cache-Control": "no-cache, max-age=60"})
                return
            except Exception as e:
                # на случай чего фолбэк на статику
                pass

        # /tma/* — статика приложения
        if path.startswith("/tma/") or path == "/tma":
            rel = path[5:] if path.startswith("/tma/") else ""
            if rel == "" or rel.endswith("/"):
                rel = (rel + "index.html").lstrip("/")
            full_path = os.path.normpath(os.path.join(TMA_ROOT, rel))

            # Безопасность: не выходить за корень
            if not full_path.startswith(TMA_ROOT):
                self._send(403, b"Forbidden", "text/plain")
                return

            if not os.path.isfile(full_path):
                # SPA-fallback на index.html для непонятных путей
                full_path = os.path.join(TMA_ROOT, "index.html")
                if not os.path.isfile(full_path):
                    self._send(404, b"Not found", "text/plain")
                    return

            ctype, _ = mimetypes.guess_type(full_path)
            ctype = ctype or "application/octet-stream"
            try:
                with open(full_path, "rb") as f:
                    body = f.read()
                # Кеш для статики
                extra = {}
                if any(full_path.endswith(ext) for ext in [".jpg", ".png", ".webp", ".svg", ".ico"]):
                    # 1 час с принудительным revalidate — чтобы Telegram WebView
                    # на мобильных подтягивал свежие фото каталога.
                    extra["Cache-Control"] = "public, max-age=3600, must-revalidate"
                self._send(200, body, ctype, extra)
            except Exception as e:
                self._send(500, str(e).encode(), "text/plain")
            return

        # Всё остальное
        self._send(404, b"Not found", "text/plain")

    def do_HEAD(self):
        self.send_response(200)
        self.end_headers()

    def log_message(self, format, *args):
        pass

    def _send(self, status, body, ctype, extra_headers=None):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        # CORS — разрешим для Telegram WebApp
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
