content = open('src/trading/execution_filter.py', 'r', encoding='utf-8').read()
old = '        # Layer 9: Confidence Threshold'
new = '        # Layer 9: Confidence Threshold\n        if "CONFIDENCE_THRESHOLD" in self.bypass_layers:\n            return ExecutionDecision(signal=signal, confidence_score=signal.confidence, blocked_by=None, trace=trace)'
if old in content:
    content = content.replace(old, new, 1)
    open('src/trading/execution_filter.py', 'w', encoding='utf-8').write(content)
    print('OK')
else:
    print('ERROR')
