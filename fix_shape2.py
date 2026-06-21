content = open('src/trading/backtester.py', 'r', encoding='utf-8').read()
old = '                            signal_obj = model.predict(obs)'
new = '                            obs_input = obs.reshape(1, 1, -1) if hasattr(obs, "reshape") else obs\n                            signal_obj = model.predict(obs_input)'
if old in content:
    content = content.replace(old, new, 1)
    open('src/trading/backtester.py', 'w', encoding='utf-8').write(content)
    print('OK')
else:
    print('ERROR')
