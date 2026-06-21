import pandas as pd, sys, torch
import numpy as np
sys.path.insert(0, '.')
from src.trading.backtester import BacktestEngine
from src.core.feature_engineering import FeatureEngineer
from src.trading.execution_filter import ExecutionFilter
from src.models.lstm_model import LSTMModel
from src.core.schemas import TradeSignal
from pathlib import Path

input_dim = int(open('models/trained/lstm_input_dim.txt').read())
base_model = LSTMModel(input_dim=input_dim, model_path=Path('models/trained/lstm_xauusd.pt'))

class FilteredModel:
    def __init__(self, m, thr=0.65):
        self._lstm = m.model
        self._thr = thr
    def predict(self, obs):
        x = torch.tensor(obs, dtype=torch.float32)
        if x.dim()==1: x=x.unsqueeze(0).unsqueeze(0)
        elif x.dim()==2: x=x.unsqueeze(0)
        with torch.no_grad():
            out = self._lstm(x)
            probs = torch.softmax(out,dim=1).numpy()[0]
        pred = int(probs.argmax())
        conf = float(probs[pred])
        if conf < self._thr:
            pred = 1  # HOLD
            conf = 0.33
        direction = pred - 1  # 0->-1 SELL, 1->0 HOLD, 2->+1 BUY
        return TradeSignal(direction=direction, confidence=conf, symbol='GOLD', timeframe='M5')

scenarios = [
    (['MACRO_EVENT','TREND_ANGLE','EMA_SEQUENCE','MOMENTUM','CONFIDENCE_THRESHOLD'], 0.65, 'bypass4_conf65'),
    (['MACRO_EVENT','TREND_ANGLE','EMA_SEQUENCE','MOMENTUM','CONFIDENCE_THRESHOLD'], 0.75, 'bypass4_conf75'),
    (['MACRO_EVENT','TREND_ANGLE','EMA_SEQUENCE','MOMENTUM','CONFIDENCE_THRESHOLD'], 0.80, 'bypass4_conf80'),
    (['MACRO_EVENT','TREND_ANGLE','EMA_SEQUENCE','MOMENTUM','CONFIDENCE_THRESHOLD','SESSION_CLOSED'], 0.70, 'bypassALL_conf70'),
]

df = pd.read_parquet('data/historical/GOLD_M5_2024.parquet').set_index('time')
df_mar = df['2024-03-01':'2024-04-15']

for bypass, thr, label in scenarios:
    fe = FeatureEngineer(base_timeframe='M5')
    ef = ExecutionFilter(bypass_layers=bypass)
    model = FilteredModel(base_model, thr)
    engine = BacktestEngine(symbol='GOLD', initial_balance=10000, feature_engineer=fe, execution_filter=ef)
    report = engine.run_walk_forward(df_mar, model, train_window=200, test_window=50, step_size=20)
    n = len(engine.trades)
    if n > 0:
        wins = sum(1 for t in engine.trades if t.pnl > 0)
        pnl = sum(t.pnl for t in engine.trades)
        dirs = [t.direction for t in engine.trades]
        print(f'[{label}] trades={n} WR={wins/n*100:.1f}% PnL={pnl:.2f} BUY={dirs.count(1)} SELL={dirs.count(-1)}')
    else:
        print(f'[{label}] 0 trades')
