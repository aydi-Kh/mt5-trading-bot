content = open('src/core/health.py', 'r', encoding='utf-8').read()
old = '    def check_models(self) -> ComponentStatus:'
new = '    def check_models(self) -> ComponentStatus:\n        if getattr(self.config, "mode", "") == "backtest":\n            return ComponentStatus(name="models", status=HealthStatus.HEALTHY, message="Backtest mode - model check skipped")'
if old in content:
    content = content.replace(old, new, 1)
    open('src/core/health.py', 'w', encoding='utf-8').write(content)
    print('OK - fix applied')
else:
    print('ERROR - pattern not found')
