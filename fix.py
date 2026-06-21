content = open('src/trading/mt5_connector.py', 'r', encoding='utf-8').read()
old = '    def _initialize_logic(self) -> bool:\n        """Internal initialization logic wrapped by circuit breaker."""'
new = '    def _initialize_logic(self) -> bool:\n        """Internal initialization logic wrapped by circuit breaker."""\n        if self.cfg.mode == "backtest":\n            logger.info("backtest_mode_skipping_mt5_connection")\n            self._is_initialized = True\n            return True'
if old in content:
    content = content.replace(old, new, 1)
    open('src/trading/mt5_connector.py', 'w', encoding='utf-8').write(content)
    print('OK - fix applied')
else:
    print('ERROR - pattern not found')
