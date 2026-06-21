content = open('main.py', 'r', encoding='utf-8').read()
old = 'execution_filter = ExecutionFilter('
new = 'bypass = ["TREND_ANGLE","EMA_SEQUENCE","MOMENTUM","SESSION_CLOSED"] if cfg.mode == "backtest" else []\n    execution_filter = ExecutionFilter(bypass_layers=bypass,'
if old in content:
    content = content.replace(old, new, 1)
    open('main.py', 'w', encoding='utf-8').write(content)
    print('OK')
else:
    print('ERROR - pattern not found')
