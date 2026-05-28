"""Чистые функции личного кабинета клиента магазина.
Все функции принимают sqlite3.Connection (row_factory=Row) и не имеют
импорт-сайд-эффектов — легко тестируются отдельно от HTTP-сервера."""


def _digits(s) -> str:
    return "".join(ch for ch in (s or "") if str(ch).isdigit())


def ensure_favorites_table(con) -> None:
    con.execute("""CREATE TABLE IF NOT EXISTS favorites (
        user_id    INTEGER NOT NULL,
        tma_id     TEXT    NOT NULL,
        created_at TEXT    DEFAULT (datetime('now')),
        PRIMARY KEY (user_id, tma_id)
    )""")
    con.commit()


def resolve_customer_order_ids(con, uid: int) -> list:
    """id заказов клиента: по user_id ИЛИ по совпадению телефона (последние 10 цифр).
    Телефон берётся из users.phone по uid. Возвращает id по убыванию."""
    row = con.execute("SELECT phone FROM users WHERE user_id=?", (uid,)).fetchone()
    phone = _digits(row["phone"]) if row else ""
    tail = phone[-10:] if len(phone) >= 10 else ""
    ids = []
    # Полный скан orders + матч по телефону в Python: телефоны в БД в разных форматах,
    # индекс по сырому полю не поможет. Приемлемо для текущего объёма; при росте —
    # хранить нормализованный phone-хвост отдельной колонкой с индексом.
    for r in con.execute("SELECT id, user_id, phone FROM orders ORDER BY id DESC"):
        if r["user_id"] == uid:
            ids.append(r["id"])
        elif tail and _digits(r["phone"])[-10:] == tail:
            ids.append(r["id"])
    return ids


def set_user_type(con, uid: int, user_type: str) -> None:
    if user_type not in ("individual", "company"):
        raise ValueError("user_type must be 'individual' or 'company'")
    # Атомарно: создаём строку если её нет, затем гарантированно проставляем тип.
    con.execute("INSERT OR IGNORE INTO users (user_id, user_type) VALUES (?,?)", (uid, user_type))
    con.execute("UPDATE users SET user_type=? WHERE user_id=?", (user_type, uid))
    con.commit()


def get_purchased_tma_ids(con, uid: int) -> list:
    """Уникальные непустые tma_id из заказов клиента (по убыванию свежести)."""
    order_ids = resolve_customer_order_ids(con, uid)
    if not order_ids:
        return []
    qmarks = ",".join("?" * len(order_ids))
    seen, out = set(), []
    for r in con.execute(
        f"SELECT tma_id FROM order_items WHERE order_id IN ({qmarks}) ORDER BY id DESC",
        tuple(order_ids),
    ):
        t = r["tma_id"]
        if t and t not in seen:
            seen.add(t)
            out.append(t)
    return out


def get_favorites(con, uid: int) -> list:
    return [r["tma_id"] for r in con.execute(
        "SELECT tma_id FROM favorites WHERE user_id=? ORDER BY created_at DESC, tma_id",
        (uid,),
    )]


def toggle_favorite(con, uid: int, tma_id: str) -> list:
    tma_id = str(tma_id)
    exists = con.execute(
        "SELECT 1 FROM favorites WHERE user_id=? AND tma_id=?", (uid, tma_id)
    ).fetchone()
    if exists:
        con.execute("DELETE FROM favorites WHERE user_id=? AND tma_id=?", (uid, tma_id))
    else:
        con.execute("INSERT OR IGNORE INTO favorites (user_id, tma_id) VALUES (?,?)", (uid, tma_id))
    con.commit()
    return get_favorites(con, uid)


def merge_favorites(con, uid: int, tma_ids: list) -> list:
    for t in tma_ids or []:
        if t:
            con.execute(
                "INSERT OR IGNORE INTO favorites (user_id, tma_id) VALUES (?,?)",
                (uid, str(t)),
            )
    con.commit()
    return get_favorites(con, uid)
