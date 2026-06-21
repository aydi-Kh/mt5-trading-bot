import sqlite3, pandas as pd
conn = sqlite3.connect('trades.db')
df = pd.read_sql("SELECT * FROM trades LIMIT 5", conn)
print(df.columns.tolist())
print(df.head())
conn.close()
