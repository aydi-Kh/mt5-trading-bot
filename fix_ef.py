content = open('src/trading/execution_filter.py', 'r', encoding='utf-8').read()

fixes = [
    (
        '    def __init__(\n        self,\n        max_drawdown: float = 0.12,\n        rsi_period: int = 14,\n        config: TradingConfig | None = None,\n        event_intelligence: Any | None = None,\n        monitor: Any | None = None,\n    ):',
        '    def __init__(\n        self,\n        max_drawdown: float = 0.12,\n        rsi_period: int = 14,\n        config: TradingConfig | None = None,\n        event_intelligence: Any | None = None,\n        monitor: Any | None = None,\n        bypass_layers: list[str] | None = None,\n    ):'
    ),
    (
        '        self.event_intelligence = event_intelligence\n        self.cfg = config\n        self.monitor = monitor',
        '        self.event_intelligence = event_intelligence\n        self.cfg = config\n        self.monitor = monitor\n        self.bypass_layers: list[str] = bypass_layers or []'
    ),
]

count = 0
for old, new in fixes:
    if old in content:
        content = content.replace(old, new, 1)
        count += 1

open('src/trading/execution_filter.py', 'w', encoding='utf-8').write(content)
print(f'OK - {count}/2 fixes applied')
