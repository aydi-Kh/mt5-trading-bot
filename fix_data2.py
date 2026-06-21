content = open('train_lstm.py', 'r', encoding='utf-8').read()
old = "df = pd.read_parquet('data/historical/GOLD_M5_10years.parquet')"
new = "df = pd.read_parquet('data/historical/GOLD_M5_2024.parquet')"
if old in content:
    content = content.replace(old, new, 1)
    open('train_lstm.py', 'w', encoding='utf-8').write(content)
    print('OK')
else:
    print('ERROR')
