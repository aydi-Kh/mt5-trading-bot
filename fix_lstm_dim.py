content = open('main.py', 'r', encoding='utf-8').read()
old = '        lstm_path = args.model_dir / "lstm_xauusd.pt"\n        model = LSTMModel(model_path=lstm_path if lstm_path.exists() else None)  # type: ignore'
new = '        lstm_path = args.model_dir / "lstm_xauusd.pt"\n        model = LSTMModel(input_dim=247, model_path=lstm_path if lstm_path.exists() else None)  # type: ignore'
if old in content:
    content = content.replace(old, new, 1)
    open('main.py', 'w', encoding='utf-8').write(content)
    print('OK')
else:
    print('ERROR')
