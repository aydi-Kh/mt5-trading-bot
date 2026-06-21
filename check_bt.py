content = open('src/trading/backtester.py', 'r', encoding='utf-8').read()
old = '''                    elif hasattr(model, "train") and hasattr(model, "model") and model.model is not None:'''
print('Found:', old in content)
