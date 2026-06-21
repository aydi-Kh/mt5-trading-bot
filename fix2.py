content = open('src/trading/mt5_connector.py', 'r', encoding='utf-8').read()
bad = '\n    # \u2500\u2500 BACKTEST MODE: skip live connection \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\n    if self.cfg.mode == "backtest":\n        logger.info("backtest_mode_skipping_mt5_connection")\n        self._is_initialized = True\n        return True\n    # \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500'
if bad in content:
    content = content.replace(bad, '')
    open('src/trading/mt5_connector.py', 'w', encoding='utf-8').write(content)
    print('OK - old block removed')
else:
    print('ERROR - pattern not found')
