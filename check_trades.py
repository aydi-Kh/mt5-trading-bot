import sqlite3, pandas as pd
conn = sqlite3.connect('trades.db')
cur = conn.cursor()
cur.execute("SELECT name FROM sqlite_master WHERE type='table'")
print('Tables:', cur.fetchall())
try:
    df = pd.read_sql("SELECT * FROM trades ORDER BY entry_time DESC LIMIT 20", conn)
    print(df.to_string())
except Exception as e:
    print('Error:', e)
conn.close()
