import pandas as pd
import sys
sys.path.insert(0, '.')
from src.trading.backtester import BacktestEngine, BacktestTrade
from src.core.feature_engineering import FeatureEngineer
from src.trading.execution_filter import ExecutionFilter
from src.models.lstm_model import LSTMModel
from datetime import datetime
from pathlib import Path

df = pd.read_parquet('data/historical/GOLD_M5_2024.parquet').set_index('time')
df = df['2024-01-01':'2024-01-04']

fe = FeatureEngineer(base_timeframe='M5')
ef = ExecutionFilter(bypass_layers=['MACRO_EVENT'])
model = LSTMModel(input_dim=247, model_path=Path('models/trained/lstm_xauusd.pt'))

engine = BacktestEngine(symbol='GOLD', initial_balance=10000, feature_engineer=fe, execution_filter=ef)
report = engine.run_walk_forward(df, model, train_window=200, test_window=50, step_size=20)

print(f'Total trades: {len(engine.trades)}')
for t in engine.trades[:20]:
    print(f'{t.entry_time} | {t.direction:+d} | entry={t.entry_price:.2f} | exit={t.exit_price:.2f} | pnl={t.pnl:.2f}')
