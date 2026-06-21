content = open('src/trading/execution_filter.py', 'r', encoding='utf-8').read()

# Bypass TREND_ANGLE
old1 = '        # Layer 2: Trend Angle\n        trend_passed, trend_metrics = self._check_trend_angle_with_metrics('
new1 = '        # Layer 2: Trend Angle\n        if "TREND_ANGLE" in self.bypass_layers:\n            trend_passed, trend_metrics = True, {"slope": 0.0, "bypassed": True}\n        else:\n            trend_passed, trend_metrics = self._check_trend_angle_with_metrics('

# Bypass EMA_SEQUENCE
old2 = '        # Layer 3: EMA Sequence\n        ema_passed, ema_metrics = self._check_ema_sequence_with_metrics('
new2 = '        # Layer 3: EMA Sequence\n        if "EMA_SEQUENCE" in self.bypass_layers:\n            ema_passed, ema_metrics = True, {"bypassed": True}\n        else:\n            ema_passed, ema_metrics = self._check_ema_sequence_with_metrics('

# Bypass MOMENTUM
old3 = '        # Layer 4: Momentum (RSI)\n        momentum_passed, momentum_metrics = self._check_momentum_with_metrics('
new3 = '        # Layer 4: Momentum (RSI)\n        if "MOMENTUM" in self.bypass_layers:\n            momentum_passed, momentum_metrics = True, {"rsi": 50.0, "bypassed": True}\n        else:\n            momentum_passed, momentum_metrics = self._check_momentum_with_metrics('

# Bypass SESSION_CLOSED
old4 = '        # Layer 5: Session/Time\n        session_passed = self._check_session_time(timestamp)'
new4 = '        # Layer 5: Session/Time\n        if "SESSION_CLOSED" in self.bypass_layers:\n            session_passed = True\n        else:\n            session_passed = self._check_session_time(timestamp)'

count = 0
for old, new in [(old1,new1),(old2,new2),(old3,new3),(old4,new4)]:
    if old in content:
        content = content.replace(old, new, 1)
        count += 1
    else:
        print(f'MISS: {old[:50]}')

open('src/trading/execution_filter.py', 'w', encoding='utf-8').write(content)
print(f'OK - {count}/4 fixes applied')
