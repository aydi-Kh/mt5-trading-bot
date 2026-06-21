content = open('train_lstm.py', 'r', encoding='utf-8').read()
old = '''labels = []
for i in range(len(close)-1):
    diff = close[i+1] - close[i]
    if diff > 0.5:
        labels.append(2)
    elif diff < -0.5:
        labels.append(0)
    else:
        labels.append(1)
labels.append(1)'''
new = '''import numpy as np as np2
diffs = np.diff(close)
threshold = np.percentile(np.abs(diffs), 60)
labels = []
for d in diffs:
    if d > threshold: labels.append(2)
    elif d < -threshold: labels.append(0)
    else: labels.append(1)
labels.append(1)
print(f"Label dist: SELL={labels.count(0)} HOLD={labels.count(1)} BUY={labels.count(2)}")'''
if old in content:
    content = content.replace(old, new, 1)
    open('train_lstm.py', 'w', encoding='utf-8').write(content)
    print('OK')
else:
    print('ERROR')
