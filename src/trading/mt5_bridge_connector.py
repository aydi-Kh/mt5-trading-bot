import json, time, os

MT5_FILES = r'C:\Users\X1 CARBONE\AppData\Roaming\MetaQuotes\Terminal\D0E8209F77C8CF37AD8BF550E51FF075\MQL5\Files'
CMD_FILE = os.path.join(MT5_FILES, 'python_commands.json')
RES_FILE = os.path.join(MT5_FILES, 'python_results.json')

def send_command(cmd, timeout=10):
    with open(CMD_FILE, 'w') as f:
        json.dump(cmd, f)
    start = time.time()
    while time.time() - start < timeout:
        if os.path.exists(RES_FILE):
            with open(RES_FILE, 'r') as f:
                result = json.load(f)
            os.remove(RES_FILE)
            return result
        time.sleep(0.1)
    return {'error': 'timeout'}

def ping():
    return send_command({'action': 'ping'})

def get_price(symbol):
    return send_command({'action': 'get_price', 'symbol': symbol})

def open_trade(symbol, direction, lot, sl, tp):
    return send_command({'action': 'open_trade', 'symbol': symbol, 'direction': direction, 'lot': lot, 'sl': sl, 'tp': tp})

def close_all(symbol):
    return send_command({'action': 'close_all', 'symbol': symbol})

if __name__ == '__main__':
    print('Ping:', ping())
    print('Price:', get_price('GOLD#'))