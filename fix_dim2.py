content = open('main.py', 'r', encoding='utf-8').read()
old = '        lstm_path = args.model_dir / "lstm_xauusd.pt"\n        model = LSTMModel(model_path=lstm_path if lstm_path.exists() else None)  # type: ignore'
new = '        lstm_path = args.model_dir / "lstm_xauusd.pt"\n        lstm_dim_path = args.model_dir / "lstm_input_dim.txt"\n        lstm_input_dim = int(lstm_dim_path.read_text()) if lstm_dim_path.exists() else 140\n        model = LSTMModel(input_dim=lstm_input_dim, model_path=lstm_path if lstm_path.exists() else None)  # type: ignore'
if old in content:
    content = content.replace(old, new, 1)
    open('main.py', 'w', encoding='utf-8').write(content)
    print('OK')
else:
    print('ERROR')
