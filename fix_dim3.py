content = open('train_lstm.py', 'r', encoding='utf-8').read()
old = "print('Model saved!')"
new = "open('models/trained/lstm_input_dim.txt', 'w').write(str(input_dim))\nprint('Model saved!')"
if old in content:
    content = content.replace(old, new, 1)
    open('train_lstm.py', 'w', encoding='utf-8').write(content)
    print('OK')
else:
    print('ERROR')
