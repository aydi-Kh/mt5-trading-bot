content = open('src/trading/mt5_connector.py', 'r', encoding='utf-8').read()
old = '    def _get_rates_range_logic(\n        self, symbol: str, timeframe: str, date_from: datetime, date_to: datetime\n    ) -> pd.DataFrame:\n        if not self._is_initialized:\n            self.initialize()'
new = '    def _get_rates_range_logic(\n        self, symbol: str, timeframe: str, date_from: datetime, date_to: datetime\n    ) -> pd.DataFrame:\n        if not self._is_initialized:\n            if self.cfg.mode != "backtest":\n                self.initialize()\n            else:\n                import MetaTrader5 as mt5\n                mt5.initialize(path=self.cfg.mt5_path, login=self.cfg.mt5_login, password=self.cfg.mt5_password.get_secret_value(), server=self.cfg.mt5_server)\n                self._is_initialized = True'
if old in content:
    content = content.replace(old, new, 1)
    open('src/trading/mt5_connector.py', 'w', encoding='utf-8').write(content)
    print('OK - fix applied')
else:
    print('ERROR - pattern not found')
