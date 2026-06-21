content = open('src/trading/execution_filter.py', 'r', encoding='utf-8').read()
old = '        else:\n            ema_passed, ema_metrics = self._check_ema_sequence_with_metrics(\n            market_data,\n            signal.direction,\n            precomputed=metrics.get("ema_sequence"),\n        )'
new = '        else:\n            ema_passed, ema_metrics = self._check_ema_sequence_with_metrics(\n                market_data,\n                signal.direction,\n                precomputed=metrics.get("ema_sequence"),\n            )'
if old in content:
    content = content.replace(old, new, 1)
    open('src/trading/execution_filter.py', 'w', encoding='utf-8').write(content)
    print('OK')
else:
    print('ERROR')
