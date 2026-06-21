content = open('src/trading/backtester.py', 'r', encoding='utf-8').read()
old = '                            obs_input = obs.reshape(1, 1, -1) if hasattr(obs, "reshape") else obs\n                            signal_obj = model.predict(obs_input)'
new = '                            obs_input = obs.reshape(1, 1, -1) if hasattr(obs, "reshape") else obs\n                            # Reinit model if input_dim mismatch\n                            if hasattr(model, "model") and model.model is not None and hasattr(model.model, "lstm"):\n                                expected = model.model.lstm.input_size\n                                actual = obs_input.shape[-1]\n                                if expected != actual:\n                                    from src.models.lstm_model import LSTMModel\n                                    model.__class__ = LSTMModel\n                                    model.__init__(input_dim=actual)\n                            signal_obj = model.predict(obs_input)'
if old in content:
    content = content.replace(old, new, 1)
    open('src/trading/backtester.py', 'w', encoding='utf-8').write(content)
    print('OK')
else:
    print('ERROR')
