import pandas as pd, sys, torch
import numpy as np
sys.path.insert(0, '.')
from src.trading.backtester import BacktestEngine
from src.core.feature_engineering import FeatureEngineer
from src.trading.execution_filter import ExecutionFilter
from src.models.lstm_model import LSTMModel
from pathlib import Path

input_dim = int(open('models/trained/lstm_input_dim.txt').read())
base_model = LSTMModel(input_dim=input_dim, model_path=Path('models/trained/lstm_xauusd.pt'))

# Test direct predict
import numpy as np
obs = np.random.randn(1, 1, input_dim).astype(np.float32)
result = base_model.predict(obs)
print(f'predict() returns: {type(result)} = {result}')
print(f'has .direction: {hasattr(result, "direction")}')
print(f'has .confidence: {hasattr(result, "confidence")}')
