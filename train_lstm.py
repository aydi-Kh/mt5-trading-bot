import pandas as pd
import numpy as np
import torch
import torch.nn as nn
from torch.utils.data import DataLoader, TensorDataset
from pathlib import Path
import sys
sys.path.insert(0, '.')
from src.core.feature_engineering import FeatureEngineer
from src.models.lstm_model import LSTMModel

print('Loading data...')
df = pd.read_parquet('data/historical/GOLD_M5_2024.parquet').set_index('time')
print(f'Loaded {len(df)} bars from {df.index.min()} to {df.index.max()}')

print('Computing features...')
fe = FeatureEngineer(normalize=False)
df_feat = fe.compute_features(df, drop_ohlcv=False)

cols_exclude = ['open','high','low','close','tick_volume','real_volume','spread','atr']
feat_cols = [c for c in df_feat.columns if c not in cols_exclude]
X = df_feat[feat_cols].values
close = df['close'].values[:len(X)]
print(f'Input dim: {len(feat_cols)}')

diffs = np.diff(close)
threshold = np.percentile(np.abs(diffs), 60)
labels = []
for d in diffs:
    if d > threshold: labels.append(2)
    elif d < -threshold: labels.append(0)
    else: labels.append(1)
labels.append(1)
print(f'Labels - SELL:{labels.count(0)} HOLD:{labels.count(1)} BUY:{labels.count(2)}')

X = np.nan_to_num(X, nan=0.0)
mean = X.mean(axis=0)
std = X.std(axis=0) + 1e-8
X = (X - mean) / std

split = int(len(X) * 0.8)
X_train = torch.tensor(X[:split], dtype=torch.float32).unsqueeze(1)
y_train = torch.tensor(labels[:split], dtype=torch.long)
X_test = torch.tensor(X[split:], dtype=torch.float32).unsqueeze(1)
y_test = torch.tensor(labels[split:], dtype=torch.long)

loader = DataLoader(TensorDataset(X_train, y_train), batch_size=256, shuffle=True)
input_dim = X.shape[1]

model = LSTMModel(input_dim=input_dim)
optimizer = torch.optim.Adam(model.model.parameters(), lr=0.001)
criterion = nn.CrossEntropyLoss()
model.model.train()

print('Training LSTM 50 epochs...')
for epoch in range(50):
    total_loss = 0
    correct = 0
    for xb, yb in loader:
        optimizer.zero_grad()
        out = model.model(xb)
        loss = criterion(out, yb)
        loss.backward()
        optimizer.step()
        total_loss += loss.item()
        correct += (out.argmax(1) == yb).sum().item()
    acc = correct / len(X_train)
    if (epoch+1) % 10 == 0:
        print(f'Epoch {epoch+1}/50 - Loss: {total_loss/len(loader):.4f} - Acc: {acc:.3f}')

model.model.eval()
with torch.no_grad():
    out = model.model(X_test)
    acc = (out.argmax(1) == y_test).float().mean()
    pred_classes = out.argmax(1).tolist()
    print(f'Test Accuracy: {acc:.3f}')
    print(f'Pred dist - SELL:{pred_classes.count(0)} HOLD:{pred_classes.count(1)} BUY:{pred_classes.count(2)}')

Path('models/trained').mkdir(parents=True, exist_ok=True)
torch.save(model.model.state_dict(), 'models/trained/lstm_xauusd.pt')
open('models/trained/lstm_input_dim.txt', 'w').write(str(input_dim))
print('Model saved!')
