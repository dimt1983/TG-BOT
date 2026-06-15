"""Консьерж-клиенты: ghost-аккаунты, заводимые Бишопом, и их self-claim в приложение.

Самодостаточный модуль над shop.db (минимум правок в tma_static_server). Все функции
принимают sqlite3.Connection и сами коммитят. Защищены от расхождения схем (тест/прод)
через PRAGMA table_info — пишем только реально существующие колонки.

Терминология:
- ghost / concierge — клиент заведён Бишопом, отрицательный stable-id из телефона,
  tg_name='concierge'. Ещё не подтвердил себя в приложении.
- claim — клиент вошёл в апп под настоящим Telegram-id и «забрал» ghost: заказы и
  реквизиты переезжают на настоящий id, ghost удаляется.
"""
from __future__ import annotations

import hashlib
import secrets
import sqlite3
from datetime import datetime, timedelta, timezone

CONCIERGE_TG_NAME = "concierge"
CLAIM_TTL_DAYS = 7

# Поля профиля, которые переносим ghost→real и принимаем при заведении.
PROFILE_FIELDS = (
    "user_type", "name", "phone", "company_name",
    "inn", "legal_address", "actual_address", "email", "price_tier",
)


def _digits(phone: str) -> str:
    return "".join(ch for ch in str(phone or "") if ch.isdigit())


def stable_ghost_id(phone: str) -> int:
    """Стабильный отрицательный id из телефона — та же схема, что у гостевого входа
    (/tma/api/auth/guest). Одна и та же для заказа, гостя и claim → они сматчатся."""
    digits = _digits(phone)
    if not digits:
        raise ValueError("phone required for stable ghost id")
    n = int(hashlib.sha256(digits.encode()).hexdigest()[:12], 16)
    return -(n % (10 ** 9))


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _cols(con: sqlite3.Connection, table: str) -> set[str]:
    return {r[1] for r in con.execute(f"PRAGMA table_info({table})").fetchall()}


def _table_exists(con: sqlite3.Connection, table: str) -> bool:
    return con.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone() is not None


def ensure_schema(con: sqlite3.Connection) -> None:
    con.execute("""
        CREATE TABLE IF NOT EXISTS concierge_claims (
            token       TEXT PRIMARY KEY,
            ghost_id    INTEGER NOT NULL,
            phone       TEXT,
            created_at  TEXT NOT NULL,
            expires_at  TEXT NOT NULL,
            used_at     TEXT,
            claimed_by  INTEGER
        )
    """)
    con.commit()


def get_client(con: sqlite3.Connection, user_id: int) -> dict | None:
    con.row_factory = sqlite3.Row
    r = con.execute("SELECT * FROM users WHERE user_id=?", (user_id,)).fetchone()
    return dict(r) if r else None


def create_or_get_client(con: sqlite3.Connection, *, phone: str, name: str | None = None,
                         user_type: str = "individual", **profile) -> dict:
    """Идемпотентно по stable_ghost_id(phone). Возвращает {user_id, created}.
    Заводит ghost (tg_name='concierge') либо обновляет переданные непустые поля."""
    gid = stable_ghost_id(phone)
    ucols = _cols(con, "users")
    existing = con.execute("SELECT 1 FROM users WHERE user_id=?", (gid,)).fetchone()

    incoming = {"user_type": user_type, "name": name, "phone": phone, **profile}
    incoming = {k: str(v).strip() for k, v in incoming.items()
                if k in PROFILE_FIELDS and v is not None and str(v).strip()}

    if not existing:
        insert_cols = ["user_id"]
        insert_vals = [gid]
        if "tg_name" in ucols:
            insert_cols.append("tg_name")
            insert_vals.append(CONCIERGE_TG_NAME)
        for k, v in incoming.items():
            if k in ucols:
                insert_cols.append(k)
                insert_vals.append(v)
        placeholders = ",".join("?" * len(insert_cols))
        con.execute(
            f"INSERT INTO users ({','.join(insert_cols)}) VALUES ({placeholders})",
            insert_vals,
        )
        con.commit()
        return {"user_id": gid, "created": True}

    # уже есть — обновим только переданные непустые и существующие колонки
    upd = {k: v for k, v in incoming.items() if k in ucols and k != "phone"}
    if upd:
        set_clause = ",".join(f"{k}=?" for k in upd)
        con.execute(f"UPDATE users SET {set_clause} WHERE user_id=?",
                    (*upd.values(), gid))
        con.commit()
    return {"user_id": gid, "created": False}


def issue_claim(con: sqlite3.Connection, ghost_id: int, *, ttl_days: int = CLAIM_TTL_DAYS,
                token: str | None = None) -> dict:
    """Одноразовый токен на claim ghost-клиента. Возвращает {token, ghost_id, expires_at}."""
    ensure_schema(con)
    row = con.execute("SELECT phone FROM users WHERE user_id=?", (ghost_id,)).fetchone()
    if not row:
        raise ValueError(f"ghost client {ghost_id} not found")
    phone = row[0] if not isinstance(row, sqlite3.Row) else row["phone"]
    token = token or secrets.token_urlsafe(24)
    expires = (datetime.now(timezone.utc) + timedelta(days=ttl_days)).isoformat()
    con.execute(
        "INSERT INTO concierge_claims (token, ghost_id, phone, created_at, expires_at) "
        "VALUES (?,?,?,?,?)",
        (token, ghost_id, phone, _now(), expires),
    )
    con.commit()
    return {"token": token, "ghost_id": ghost_id, "expires_at": expires}


def redeem_claim(con: sqlite3.Connection, token: str, real_user_id: int,
                 real_name: str | None = None) -> dict:
    """Слить ghost (из токена) в настоящий Telegram user_id.
    Возвращает {ok, merged_orders, real_user_id} либо {ok:False, error}."""
    ensure_schema(con)
    con.row_factory = sqlite3.Row
    claim = con.execute("SELECT * FROM concierge_claims WHERE token=?", (token,)).fetchone()
    if not claim:
        return {"ok": False, "error": "not_found"}
    if claim["used_at"]:
        return {"ok": False, "error": "already_used"}
    if claim["expires_at"] < _now():
        return {"ok": False, "error": "expired"}
    if real_user_id is None or int(real_user_id) <= 0:
        return {"ok": False, "error": "invalid_real_user"}
    real_user_id = int(real_user_id)
    ghost_id = int(claim["ghost_id"])
    if real_user_id == ghost_id:
        return {"ok": False, "error": "same_user"}

    ucols = _cols(con, "users")
    ghost = con.execute("SELECT * FROM users WHERE user_id=?", (ghost_id,)).fetchone()

    # настоящий пользователь: создать если нет
    real = con.execute("SELECT * FROM users WHERE user_id=?", (real_user_id,)).fetchone()
    if not real:
        cols = ["user_id"]
        vals = [real_user_id]
        if "tg_name" in ucols:
            cols.append("tg_name"); vals.append("tg")
        if real_name and "name" in ucols:
            cols.append("name"); vals.append(real_name)
        con.execute(f"INSERT INTO users ({','.join(cols)}) VALUES ({','.join('?'*len(cols))})", vals)
        real = con.execute("SELECT * FROM users WHERE user_id=?", (real_user_id,)).fetchone()

    # перенос профиля ghost→real только в пустые поля real
    if ghost:
        upd = {}
        for f in PROFILE_FIELDS:
            if f not in ucols:
                continue
            gval = ghost[f] if f in ghost.keys() else None
            rval = real[f] if f in real.keys() else None
            if gval and not rval:
                upd[f] = gval
        if upd:
            set_clause = ",".join(f"{k}=?" for k in upd)
            con.execute(f"UPDATE users SET {set_clause} WHERE user_id=?",
                        (*upd.values(), real_user_id))

    # заказы: перепривязать на настоящего
    merged_orders = 0
    if _table_exists(con, "orders"):
        cur = con.execute("UPDATE orders SET user_id=? WHERE user_id=?",
                          (real_user_id, ghost_id))
        merged_orders = cur.rowcount

    # избранное (опционально, если таблица есть)
    if _table_exists(con, "favorites"):
        fcols = _cols(con, "favorites")
        if "user_id" in fcols:
            try:
                con.execute("UPDATE OR IGNORE favorites SET user_id=? WHERE user_id=?",
                            (real_user_id, ghost_id))
                con.execute("DELETE FROM favorites WHERE user_id=?", (ghost_id,))
            except sqlite3.Error:
                pass

    # погасить ghost и токен
    con.execute("DELETE FROM users WHERE user_id=?", (ghost_id,))
    con.execute("UPDATE concierge_claims SET used_at=?, claimed_by=? WHERE token=?",
                (_now(), real_user_id, token))
    con.commit()
    return {"ok": True, "merged_orders": merged_orders, "real_user_id": real_user_id,
            "ghost_id": ghost_id}
