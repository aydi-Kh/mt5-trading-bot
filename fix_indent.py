content = open('src/trading/execution_filter.py', 'r', encoding='utf-8').read()
old = '        else:\n            trend_passed, trend_metrics = self._check_trend_angle_with_metrics(\n            market_data,\n            signal.direction,\n            precomputed=metrics.get("trend_angle"),\n        )'
new = '        else:\n            trend_passed, trend_metrics = self._check_trend_angle_with_metrics(\n                market_data,\n                signal.direction,\n                precomputed=metrics.get("trend_angle"),\n            )'
if old in content:
    content = content.replace(old, new, 1)
    open('src/trading/execution_filter.py', 'w', encoding='utf-8').write(content)
    print('OK')
else:
    print('ERROR')
