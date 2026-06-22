import sys, time, numpy as np, pandas as pd, torch
sys.path.insert(0, '.')
from src.trading.mt5_bridge_connector import ping, get_price, open_trade, close_all
from src.core.feature_engineering import FeatureEngineer
from src.models.lstm_model import LSTMModel
from pathlib import Path
import MetaTrader5 as mt5

SYMBOL = 'GOLD#'
LOT = 0.01
ATR_SL = 2.0
ATR_TP = 4.0
CONF_THRESHOLD = 0.65
BARS = 300

print('Starting live bot...')
print('Bridge:', ping())

input_dim = int(open('models/trained/lstm_input_dim.txt').read())
model = LSTMModel(input_dim=input_dim, model_path=Path('models/trained/lstm_xauusd.pt'))
model.model.eval()
fe = FeatureEngineer(base_timeframe='M5')
print(f'Model loaded: {input_dim} features')

def get_bars():
    mt5.initialize()
    rates = mt5.copy_rates_from_pos(SYMBOL, mt5.TIMEFRAME_M5, 0, BARS)
    if rates is None:
        return None
    df = pd.DataFrame(rates)
    df['time'] = pd.to_datetime(df['time'], unit='s')
    df = df.set_index('time')
    return df

print('Bot running - Ctrl+C to stop')
active_trade = None

while True:
    try:
        df = get_bars()
        if df is None:
            print('No data')
            time.sleep(60)
            continue
        df_feat = fe.compute_features(df)
        X = df_feat.values
        X = np.nan_to_num(X, nan=0.0)
        if X.shape[1] > input_dim:
            X = X[:, :input_dim]
        mean = X.mean(axis=0)
        std = X.std(axis=0) + 1e-8
        X = (X - mean) / std
        obs = torch.tensor(X[-1:], dtype=torch.float32).unsqueeze(0)
        with torch.no_grad():
            out = model.model(obs)
            probs = torch.softmax(out, dim=1).numpy()[0]
        pred = int(probs.argmax())
        conf = float(probs[pred])
        direction = pred - 1
        price = get_price(SYMBOL)
        bid = float(price['bid'])
        ask = float(price['ask'])
        atr = float(df['high'].tail(14).max() - df['low'].tail(14).min()) / 14
        signals = ['SELL', 'HOLD', 'BUY']
        print(f'Signal: {signals[pred]} conf={conf:.2f} price={bid} atr={atr:.2f}')
        if conf >= CONF_THRESHOLD and direction != 0 and active_trade is None:
            sl = bid - direction * ATR_SL * atr
            tp = bid + direction * ATR_TP * atr
            result = open_trade(SYMBOL, direction, LOT, sl, tp)
            print(f'Trade opened: {result}')
            active_trade = result
        time.sleep(300)
    except KeyboardInterrupt:
        print('Stopping...')
        break
    except Exception as e:
        print('Error:', e)
        time.sleep(60)