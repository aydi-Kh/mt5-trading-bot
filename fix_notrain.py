content = open('src/trading/backtester.py', 'r', encoding='utf-8').read()
old = '                # Train model on walk-forward window if it supports online training\n                try:'
new = '                # Train model on walk-forward window if it supports online training\n                try:\n                    if False:  # Disabled - use pre-trained model only'
if old in content:
    content = content.replace(old, new, 1)
    open('src/trading/backtester.py', 'w', encoding='utf-8').write(content)
    print('OK')
else:
    print('ERROR')
