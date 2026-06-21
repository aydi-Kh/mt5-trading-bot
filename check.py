content = open('main.py', 'r', encoding='utf-8').read()
old = 'bypass = ["SESSION_CLOSED","MACRO_EVENT"] if cfg.mode == "backtest" else []'
new = 'bypass = ["SESSION_CLOSED","MACRO_EVENT"] if cfg.mode == "backtest" else []'
print('Already correct' if old in content else 'Need fix')
