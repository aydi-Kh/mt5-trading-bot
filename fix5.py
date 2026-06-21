content = open('src/trading/mt5_connector.py', 'r', encoding='utf-8').read()
old = '    def _get_symbol_properties_logic(self, symbol: str) -> Dict[str, Any]:\n        """Internal symbol properties retrieval logic."""\n        if not self._is_initialized:\n            self.initialize()'
new = '    def _get_symbol_properties_logic(self, symbol: str) -> Dict[str, Any]:\n        """Internal symbol properties retrieval logic."""\n        if self.cfg.mode == "backtest":\n            return {"name": symbol, "tradable": True, "spread": 20, "digits": 2, "point": 0.01, "trade_contract_size": 100.0}\n        if not self._is_initialized:\n            self.initialize()'
if old in content:
    content = content.replace(old, new, 1)
    open('src/trading/mt5_connector.py', 'w', encoding='utf-8').write(content)
    print('OK - fix applied')
else:
    print('ERROR - pattern not found')
