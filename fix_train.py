content = open('src/trading/backtester.py', 'r', encoding='utf-8').read()
old = '                train_std[train_std == 0] = 1.0  # Avoid division by zero'
new = '                train_std[train_std == 0] = 1.0  # Avoid division by zero\n                # Train model on walk-forward window if it supports online training\n                try:\n                    if hasattr(model, "train_on_features"):\n                        model.train_on_features(train_slice, train_mean, train_std)\n                    elif hasattr(model, "train") and hasattr(model, "model") and model.model is not None:\n                        import torch\n                        X = torch.tensor((train_slice - train_mean) / (train_std + 1e-8), dtype=torch.float32)\n                        # Simple direction labels from price changes\n                        prices = close_vals[start:test_start_idx]\n                        labels = []\n                        for k in range(len(prices)-1):\n                            diff = prices[k+1] - prices[k]\n                            labels.append(2 if diff > 0 else (0 if diff < 0 else 1))\n                        labels.append(1)\n                        y = torch.tensor(labels, dtype=torch.long)\n                        dataset = list(zip(X.unsqueeze(1), y))\n                        model.train(dataset, epochs=3)\n                except Exception as _te:\n                    pass'
if old in content:
    content = content.replace(old, new, 1)
    open('src/trading/backtester.py', 'w', encoding='utf-8').write(content)
    print('OK - fix applied')
else:
    print('ERROR - pattern not found')
