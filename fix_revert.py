content = open('main.py', 'r', encoding='utf-8').read()
old = '        model = LSTMModel(input_dim=247, model_path=lstm_path if lstm_path.exists() else None)  # type: ignore'
new = '        model = LSTMModel(model_path=lstm_path if lstm_path.exists() else None)  # type: ignore'
if old in content:
    content = content.replace(old, new, 1)
    open('main.py', 'w', encoding='utf-8').write(content)
    print('OK')
else:
    print('Already correct')
