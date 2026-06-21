content = open('train_lstm.py', 'r', encoding='utf-8').read()
old = 'class_counts = [labels.count(i) for i in range(3)]\ntotal = sum(class_counts)\nweights = torch.tensor([total/c for c in class_counts], dtype=torch.float32)\ncriterion = nn.CrossEntropyLoss(weight=weights)'
new = 'criterion = nn.CrossEntropyLoss()'
if old in content:
    content = content.replace(old, new, 1)

old2 = 'for epoch in range(20):'
new2 = 'for epoch in range(50):'
if old2 in content:
    content = content.replace(old2, new2, 1)

open('train_lstm.py', 'w', encoding='utf-8').write(content)
print('OK')
