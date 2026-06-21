content = open("src/trading/backtester.py", "r", encoding="utf-8").read()
old = """                            # Reinit model if input_dim mismatch
                            if hasattr(model, "model") and model.model is not None and hasattr(model.model, "lstm"):
                                expected = model.model.lstm.input_size
                                actual = obs_input.shape[-1]
                                if expected != actual:
                                    from src.models.lstm_model import LSTMModel
                                    model.__class__ = LSTMModel
                                    model.__init__(input_dim=actual)"""
new = """                            # Reinit disabled - use pre-trained model as-is"""
if old in content:
    content = content.replace(old, new, 1)
    open("src/trading/backtester.py", "w", encoding="utf-8").write(content)
    print("OK - reinit disabled")
else:
    print("ERROR - pattern not found")
