content = open('src/trading/mt5_connector.py', 'r', encoding='utf-8').read()

fixes = [
    (
        '    def _get_account_info_logic(self) -> Dict[str, Any]:\n        if not self._is_initialized:',
        '    def _get_account_info_logic(self) -> Dict[str, Any]:\n        if self.cfg.mode == "backtest":\n            return {"balance": 10000.0, "equity": 10000.0, "margin": 0.0, "margin_free": 10000.0, "margin_level": 0.0, "profit": 0.0}\n        if not self._is_initialized:'
    ),
]

count = 0
for old, new in fixes:
    if old in content:
        content = content.replace(old, new, 1)
        count += 1

open('src/trading/mt5_connector.py', 'w', encoding='utf-8').write(content)
print(f'OK - {count}/1 fixes applied')
