import pandas as pd, sys, torch, itertools
import numpy as np
sys.path.insert(0, '.')
from src.trading.backtester import BacktestEngine
from src.core.feature_engineering import FeatureEngineer
from src.trading.execution_filter import ExecutionFilter
from src.models.lstm_model import LSTMModel
from pathlib import Path

df = pd.read_parquet('data/historical/GOLD_M5_2024.parquet').set_index('time')
input_dim = int(open('models/trained/lstm_input_dim.txt').read())
base_model = LSTMModel(input_dim=input_dim, model_path=Path('models/trained/lstm_xauusd.pt'))

ALL_FILTERS = ['TREND_ANGLE','EMA_SEQUENCE','MOMENTUM','CONFIDENCE_THRESHOLD','SESSION_CLOSED']

scenarios = [
    # (bypass_filters, period, conf_threshold, label)
    (['TREND_ANGLE','EMA_SEQUENCE'], '2024-03-01:2024-04-15', 0.55, 'bypass_TA+EMA'),
    (['TREND_ANGLE','EMA_SEQUENCE','MOMENTUM'], '2024-03-01:2024-04-15', 0.55, 'bypass_TA+EMA+MOM'),
    (['TREND_ANGLE','EMA_SEQUENCE','MOMENTUM'], '2024-03-01:2024-04-15', 0.70, 'bypass_3+conf70'),
    (['TREND_ANGLE','EMA_SEQUENCE','MOMENTUM'], '2024-03-01:2024-04-15', 0.80, 'bypass_3+conf80'),
    (['TREND_ANGLE','EMA_SEQUENCE','MOMENTUM','CONFIDENCE_THRESHOLD'], '2024-03-01:2024-04-15', 0.65, 'bypass_4+conf65'),
    (['TREND_ANGLE','EMA_SEQUENCE','MOMENTUM','CONFIDENCE_THRESHOLD'], '2024-01-01:2024-01-04', 0.55, 'jan_bypass4'),
    (['TREND_ANGLE','EMA_SEQUENCE','MOMENTUM','CONFIDENCE_THRESHOLD'], '2024-06-01:2024-07-15', 0.65, 'jun_bypass4'),
    (['TREND_ANGLE','EMA_SEQUENCE','MOMENTUM','CONFIDENCE_THRESHOLD','SESSION_CLOSED'], '2024-03-01:2024-04-15', 0.75, 'all_bypass+conf75'),
]

results = []
for bypass, period, conf_thr, label in scenarios:
    try:
        start, end = period.split(':')
        df_p = df[start:end]
        if len(df_p) < 300:
            continue

        class FilteredModel:
            def __init__(self, m, thr):
                self.model = m.model
                self._thr = thr
            def predict(self, obs):
                x = torch.tensor(obs, dtype=torch.float32)
                if x.dim()==1: x=x.unsqueeze(0).unsqueeze(0)
                elif x.dim()==2: x=x.unsqueeze(0)
                with torch.no_grad():
                    out = self.model(x)
                    probs = torch.softmax(out,dim=1).numpy()[0]
                pred = int(probs.argmax())
                conf = float(probs[pred])
                if conf < self._thr:
                    return 1, 0.33
                return pred, conf

        fe = FeatureEngineer(base_timeframe='M5')
        ef = ExecutionFilter(bypass_layers=['MACRO_EVENT'] + bypass)
        model = FilteredModel(base_model, conf_thr)
        engine = BacktestEngine(symbol='GOLD', initial_balance=10000, feature_engineer=fe, execution_filter=ef)
        report = engine.run_walk_forward(df_p, model, train_window=200, test_window=50, step_size=20)

        n = len(engine.trades)
        if n > 0:
            wins = sum(1 for t in engine.trades if t.pnl > 0)
            total_pnl = sum(t.pnl for t in engine.trades)
            wr = wins/n*100
            dirs = [t.direction for t in engine.trades]
            results.append((label, period, n, wr, total_pnl, dirs.count(1), dirs.count(-1)))
            print(f'[{label}] trades={n} WR={wr:.1f}% PnL={total_pnl:.2f} BUY={dirs.count(1)} SELL={dirs.count(-1)}')
        else:
            print(f'[{label}] 0 trades')
    except Exception as e:
        print(f'[{label}] ERROR: {e}')

print('\n=== BEST SCENARIOS ===')
results.sort(key=lambda x: x[4], reverse=True)
for r in results[:5]:
    print(f'{r[0]} | {r[1]} | trades={r[2]} | WR={r[3]:.1f}% | PnL={r[4]:.2f}')
