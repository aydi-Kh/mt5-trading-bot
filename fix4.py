content = open('src/trading/mt5_connector.py', 'r', encoding='utf-8').read()
old = '    def _get_terminal_status_logic(self) -> Dict[str, Any]:\n        """Internal terminal status retrieval logic."""\n        if not self._is_initialized:\n            self.initialize()'
new = '    def _get_terminal_status_logic(self) -> Dict[str, Any]:\n        """Internal terminal status retrieval logic."""\n        if self.cfg.mode == "backtest":\n            return {"algo_trading": True, "trade_allowed": True, "connected": True}\n        if not self._is_initialized:\n            self.initialize()'
if old in content:
    content = content.replace(old, new, 1)
    open('src/trading/mt5_connector.py', 'w', encoding='utf-8').write(content)
    print('OK - fix applied')
else:
    print('ERROR - pattern not found')
