import pandas as pd, sys, torch, numpy as np
sys.path.insert(0, '.')
from src.trading.backtester import BacktestEngine
from src.core.feature_engineering import FeatureEngineer
from src.trading.execution_filter import ExecutionFilter
from src.models.lstm_model import LSTMModel
from src.models.base_model import Signal
from src.core.constants import SignalDirection
from pathlib import Path

input_dim = int(open('models/trained/lstm_input_dim.txt').read())
base_model = LSTMModel(input_dim=input_dim, model_path=Path('models/trained/lstm_xauusd.pt'))

class FM:
    def __init__(self, m, thr):
        self._b = m
        self._t = thr
        self.model = m.model
    def predict(self, obs):
        r = self._b.predict(obs)
        if float(r.confidence) < self._t:
            return Signal(direction=SignalDirection.HOLD, confidence=0.33)
        return r

df = pd.read_parquet('data/historical/GOLD_M5_2024.parquet').set_index('time')['2024-03-01':'2024-04-15']
bypass = ['MACRO_EVENT','TREND_ANGLE','EMA_SEQUENCE','MOMENTUM','CONFIDENCE_THRESHOLD','SESSION_CLOSED']

for thr in [0.65, 0.75, 0.85]:
    fe = FeatureEngineer(base_timeframe='M5')
    ef = ExecutionFilter(bypass_layers=bypass)
    model = FM(base_model, thr)
    engine = BacktestEngine(symbol='GOLD', initial_balance=10000, feature_engineer=fe, execution_filter=ef)
    engine.run_walk_forward(df, model, train_window=200, test_window=50, step_size=20)
    n = len(engine.trades)
    if n:
        wins = sum(1 for t in engine.trades if t.pnl > 0)
        pnl = sum(t.pnl for t in engine.trades)
        dirs = [t.direction for t in engine.trades]
        print(f'conf={thr} trades={n} WR={wins/n*100:.1f}% PnL={pnl:.2f} BUY={dirs.count(1)} SELL={dirs.count(-1)}')
    else:
        print(f'conf={thr} 0 trades')
