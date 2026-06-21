content = open('main.py', 'r', encoding='utf-8').read()
old = 'bypass = ["MACRO_EVENT"] if cfg.mode == "backtest" else []'
new = 'bypass = ["MACRO_EVENT", "TREND_ANGLE", "EMA_SEQUENCE", "MOMENTUM", "SESSION_CLOSED", "CONFIDENCE_THRESHOLD"] if cfg.mode == "backtest" else []'
if old in content:
    content = content.replace(old, new, 1)
    open('main.py', 'w', encoding='utf-8').write(content)
    print('OK')
else:
    print('ERROR')
