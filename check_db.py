import sqlite3
conn = sqlite3.connect('planning_ejp.db')
print(conn.execute("PRAGMA table_info(eglises);").fetchall())
