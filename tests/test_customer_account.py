import importlib.util
import sqlite3
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("customer_account", ROOT / "customer_account.py")
ca = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ca)


def make_db():
    con = sqlite3.connect(":memory:")
    con.row_factory = sqlite3.Row
    con.executescript("""
        CREATE TABLE users(user_id INTEGER PRIMARY KEY, tg_name TEXT, user_type TEXT,
            name TEXT, phone TEXT, company_name TEXT);
        CREATE TABLE orders(id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER,
            name TEXT, phone TEXT, total REAL, status TEXT DEFAULT 'new',
            created_at TEXT DEFAULT (datetime('now')));
        CREATE TABLE order_items(id INTEGER PRIMARY KEY AUTOINCREMENT, order_id INTEGER,
            product_id INTEGER, product_name TEXT, fasovka INTEGER, quantity INTEGER,
            price REAL, tma_id TEXT);
    """)
    return con


class FavoritesTableTest(unittest.TestCase):
    def test_ensure_favorites_table_idempotent(self):
        con = make_db()
        ca.ensure_favorites_table(con)
        ca.ensure_favorites_table(con)
        cols = {r[1] for r in con.execute("PRAGMA table_info(favorites)")}
        self.assertEqual(cols, {"user_id", "tma_id", "created_at"})


class ResolveOrdersTest(unittest.TestCase):
    def test_orders_by_user_id_only(self):
        con = make_db()
        con.execute("INSERT INTO users(user_id, phone) VALUES (10, '')")
        con.execute("INSERT INTO orders(id, user_id, phone) VALUES (1, 10, '')")
        con.execute("INSERT INTO orders(id, user_id, phone) VALUES (2, 99, '')")
        self.assertEqual(ca.resolve_customer_order_ids(con, 10), [1])

    def test_orders_merge_by_phone(self):
        con = make_db()
        con.execute("INSERT INTO users(user_id, phone) VALUES (10, '+7 (912) 345-67-89')")
        con.execute("INSERT INTO orders(id, user_id, phone) VALUES (1, 10, '')")
        con.execute("INSERT INTO orders(id, user_id, phone) VALUES (2, -555, '89123456789')")
        con.execute("INSERT INTO orders(id, user_id, phone) VALUES (3, -777, '79990001122')")
        self.assertEqual(set(ca.resolve_customer_order_ids(con, 10)), {1, 2})

    def test_no_phone_returns_user_id_only(self):
        con = make_db()
        con.execute("INSERT INTO users(user_id, phone) VALUES (10, '')")
        con.execute("INSERT INTO orders(id, user_id, phone) VALUES (1, 10, '')")
        con.execute("INSERT INTO orders(id, user_id, phone) VALUES (2, -555, '89123456789')")
        self.assertEqual(ca.resolve_customer_order_ids(con, 10), [1])


class UserTypeTest(unittest.TestCase):
    def test_set_type_updates_existing(self):
        con = make_db()
        con.execute("INSERT INTO users(user_id, user_type) VALUES (10, 'individual')")
        ca.set_user_type(con, 10, "company")
        row = con.execute("SELECT user_type FROM users WHERE user_id=10").fetchone()
        self.assertEqual(row["user_type"], "company")

    def test_set_type_creates_row_if_absent(self):
        con = make_db()
        ca.set_user_type(con, 42, "company")
        row = con.execute("SELECT user_type FROM users WHERE user_id=42").fetchone()
        self.assertEqual(row["user_type"], "company")

    def test_set_type_rejects_bad_value(self):
        con = make_db()
        with self.assertRaises(ValueError):
            ca.set_user_type(con, 10, "ooo")


class PurchasedTest(unittest.TestCase):
    def test_returns_distinct_tma_ids(self):
        con = make_db()
        con.execute("INSERT INTO users(user_id, phone) VALUES (10, '')")
        con.execute("INSERT INTO orders(id, user_id, phone) VALUES (1, 10, '')")
        con.execute("INSERT INTO orders(id, user_id, phone) VALUES (2, 10, '')")
        con.execute("INSERT INTO order_items(order_id, tma_id, product_name) VALUES (1, 'esp-1', 'A')")
        con.execute("INSERT INTO order_items(order_id, tma_id, product_name) VALUES (2, 'esp-1', 'A')")
        con.execute("INSERT INTO order_items(order_id, tma_id, product_name) VALUES (2, 'flt-9', 'B')")
        self.assertEqual(set(ca.get_purchased_tma_ids(con, 10)), {"esp-1", "flt-9"})

    def test_ignores_null_tma_id(self):
        con = make_db()
        con.execute("INSERT INTO users(user_id, phone) VALUES (10, '')")
        con.execute("INSERT INTO orders(id, user_id, phone) VALUES (1, 10, '')")
        con.execute("INSERT INTO order_items(order_id, tma_id, product_name) VALUES (1, NULL, 'Old')")
        self.assertEqual(ca.get_purchased_tma_ids(con, 10), [])


class FavoritesTest(unittest.TestCase):
    def setUp(self):
        self.con = make_db()
        ca.ensure_favorites_table(self.con)

    def test_toggle_adds_then_removes(self):
        self.assertEqual(ca.toggle_favorite(self.con, 10, "esp-1"), ["esp-1"])
        self.assertEqual(ca.toggle_favorite(self.con, 10, "esp-1"), [])

    def test_favorites_isolated_per_user(self):
        ca.toggle_favorite(self.con, 10, "esp-1")
        ca.toggle_favorite(self.con, 20, "flt-9")
        self.assertEqual(ca.get_favorites(self.con, 10), ["esp-1"])
        self.assertEqual(ca.get_favorites(self.con, 20), ["flt-9"])

    def test_merge_is_add_only(self):
        ca.toggle_favorite(self.con, 10, "esp-1")
        result = ca.merge_favorites(self.con, 10, ["esp-1", "flt-9", "blk-3"])
        self.assertEqual(set(result), {"esp-1", "flt-9", "blk-3"})
        result2 = ca.merge_favorites(self.con, 10, ["flt-9"])
        self.assertEqual(set(result2), {"esp-1", "flt-9", "blk-3"})


if __name__ == "__main__":
    unittest.main()
