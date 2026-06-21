content = open('src/models/lstm_model.py', 'r', encoding='utf-8').read()
old = '                signal_obj = model.predict(obs)'
new = '                signal_obj = model.predict(obs.reshape(1, 1, -1) if hasattr(obs, "reshape") else obs)'
if old in content:
    content = content.replace(old, new, 1)
    open('src/models/lstm_model.py', 'w', encoding='utf-8').write(content)
    print('OK')
else:
    print('ERROR')
