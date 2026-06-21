import pandas as pd, numpy as np, torch, sys
sys.path.insert(0, '.')
from src.core.feature_engineering import FeatureEngineer
from src.models.lstm_model import LSTMModel
from pathlib import Path

df = pd.read_parquet('data/historical/GOLD_M5_2024.parquet').set_index('time')
df = df['2024-03-01':'2024-03-07']
fe = FeatureEngineer(normalize=False)
df_feat = fe.compute_features(df, drop_ohlcv=False)
cols = [c for c in df_feat.columns if c not in ['open','high','low','close','tick_volume','real_volume','spread','atr']]
X = df_feat[cols].values
X = np.nan_to_num(X, nan=0.0)
input_dim = int(open('models/trained/lstm_input_dim.txt').read())
model = LSTMModel(input_dim=input_dim, model_path=Path('models/trained/lstm_xauusd.pt'))
model.model.eval()
X_t = torch.tensor(X, dtype=torch.float32).unsqueeze(1)
with torch.no_grad():
    out = model.model(X_t)
    preds = out.argmax(1).tolist()
    probs = torch.softmax(out, dim=1).numpy()
print(f'Predictions: SELL={preds.count(0)} HOLD={preds.count(1)} BUY={preds.count(2)}')
print(f'Avg confidence BUY: {probs[:,2].mean():.3f}')
print(f'Avg confidence SELL: {probs[:,0].mean():.3f}')
print(f'Max BUY confidence: {probs[:,2].max():.3f}')
