"""
Запусти этот скрипт в той же папке где лежит shop.db
python set_stock.py
"""
import sqlite3

con = sqlite3.connect("shop.db")
cur = con.cursor()

cur.execute("UPDATE products SET stock = 10")
count = cur.rowcount
con.commit()
con.close()

print(f"✅ Готово! Обновлено {count} позиций — у каждой теперь остаток 10 шт.")
