import pandas as pd, sys
sys.path.insert(0, '.')
from src.trading.backtester import BacktestEngine
from src.core.feature_engineering import FeatureEngineer
from src.trading.execution_filter import ExecutionFilter
from src.models.lstm_model import LSTMModel
from pathlib import Path

df = pd.read_parquet('data/historical/GOLD_M5_2024.parquet').set_index('time')
df = df['2024-03-01':'2024-03-15']  # 2 semaines seulement
fe = FeatureEngineer(base_timeframe='M5')
ef = ExecutionFilter(bypass_layers=['MACRO_EVENT','TREND_ANGLE','EMA_SEQUENCE','MOMENTUM','CONFIDENCE_THRESHOLD'])
input_dim = int(open('models/trained/lstm_input_dim.txt').read())
model = LSTMModel(input_dim=input_dim, model_path=Path('models/trained/lstm_xauusd.pt'))
engine = BacktestEngine(symbol='GOLD', initial_balance=10000, feature_engineer=fe, execution_filter=ef)
report = engine.run_walk_forward(df, model, train_window=200, test_window=50, step_size=20)
print(f'Total trades: {len(engine.trades)}')
if engine.trades:
    wins = sum(1 for t in engine.trades if t.pnl > 0)
    dirs = [t.direction for t in engine.trades]
    print(f'BUY: {dirs.count(1)} SELL: {dirs.count(-1)}')
    print(f'Win Rate: {wins/len(engine.trades)*100:.1f}%')
    for t in engine.trades[:10]:
        print(f'{t.entry_time} | {t.direction:+d} | pnl={t.pnl:.2f}')
