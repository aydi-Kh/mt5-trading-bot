content = open('train_lstm.py', 'r', encoding='utf-8').read()
old = 'criterion = nn.CrossEntropyLoss()'
new = 'class_counts = [labels.count(i) for i in range(3)]\ntotal = sum(class_counts)\nweights = torch.tensor([total/c for c in class_counts], dtype=torch.float32)\ncriterion = nn.CrossEntropyLoss(weight=weights)'
if old in content:
    content = content.replace(old, new, 1)
    open('train_lstm.py', 'w', encoding='utf-8').write(content)
    print('OK')
else:
    print('ERROR')
