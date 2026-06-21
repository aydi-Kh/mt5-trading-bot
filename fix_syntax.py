content = open('src/trading/backtester.py', 'r', encoding='utf-8').read()
old = '                try:\n                    if False:  # Disabled - use pre-trained model only\n                    if hasattr(model, "train_on_features"):'
new = '                try:\n                    if hasattr(model, "train_on_features"):'
if old in content:
    content = content.replace(old, new, 1)
    open('src/trading/backtester.py', 'w', encoding='utf-8').write(content)
    print('OK')
else:
    print('ERROR')
