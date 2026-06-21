content = open('src/trading/backtester.py', 'r', encoding='utf-8').read()

old = '''                    elif hasattr(model, "train") and hasattr(model, "model") and model.model is not None:
                        import torch
                        X = torch.tensor((train_slice - train_mean) / (train_std + 1e-8), dtype=torch.float32)
                        # Simple direction labels from price changes
                        prices = close_vals[start:test_start_idx]
                        labels = []
                        for k in range(len(prices)-1):
                            diff = prices[k+1] - prices[k]
                            labels.append(2 if diff > 0 else (0 if diff < 0 else 1))
                        labels.append(1)
                        y = torch.tensor(labels, dtype=torch.long)
                        dataset = list(zip(X.unsqueeze(1), y))
                        model.train(dataset, epochs=3)'''

new = '''                    # DISABLED: walk-forward retraining overwrites pre-trained model
                    # elif hasattr(model, "train") and hasattr(model, "model") and model.model is not None:
                    #     pass'''

if old in content:
    content = content.replace(old, new, 1)
    open('src/trading/backtester.py', 'w', encoding='utf-8').write(content)
    print('OK - retraining disabled')
else:
    print('ERROR - pattern not found')
