content = open('src/trading/mt5_connector.py', 'r', encoding='utf-8').read()
old = '            else:\n                import MetaTrader5 as mt5\n                mt5.initialize(path=self.cfg.mt5_path, login=self.cfg.mt5_login, password=self.cfg.mt5_password.get_secret_value(), server=self.cfg.mt5_server)\n                self._is_initialized = True'
new = '            else:\n                import MetaTrader5 as _mt5_module\n                _mt5_module.initialize(path=self.cfg.mt5_path, login=self.cfg.mt5_login, password=self.cfg.mt5_password.get_secret_value(), server=self.cfg.mt5_server)\n                self._is_initialized = True'
if old in content:
    content = content.replace(old, new, 1)
    open('src/trading/mt5_connector.py', 'w', encoding='utf-8').write(content)
    print('OK - fix applied')
else:
    print('ERROR - pattern not found')
