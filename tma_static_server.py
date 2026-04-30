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
import threading
import mimetypes
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import unquote

PORT = int(os.environ.get("PORT", 10000))
TMA_ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "tma_static")


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        path = unquote(self.path.split("?", 1)[0])

        # Healthcheck
        if path == "/" or path == "":
            self._send(200, b"OK", "text/plain")
            return

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
                    extra["Cache-Control"] = "public, max-age=86400"
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
