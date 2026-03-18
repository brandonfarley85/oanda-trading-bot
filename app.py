"""
Farleys Pinpoint Velocity BOT
OANDA Webhook Server — UPDATED VERSION
- Auto 40 pip stop loss if not sent
- Auto 60 pip take profit if not sent
- Loosened spread to 30 pips
- Cooldown reduced to 5 seconds
"""

from flask import Flask, request, jsonify
import oandapyV20
import oandapyV20.endpoints.orders as orders
import oandapyV20.endpoints.positions as positions
import oandapyV20.endpoints.pricing as pricing
import oandapyV20.endpoints.accounts as accounts
import json
import logging
import time

# ===================================================
# LOGGING
# ===================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)

log = logging.getLogger(__name__)

# ===================================================
# FLASK APP
# ===================================================

app = Flask(__name__)

# ===================================================
# OANDA CREDENTIALS
# ===================================================

ACCOUNT_ID = "101-001-24987993-002"
ACCESS_TOKEN = "1a08faff98fd3fef165a4ee4ca69f245-43834b16d9b0baf270518ce1edc5f2dd"
ENVIRONMENT = "practice"

client = oandapyV20.API(
    access_token=ACCESS_TOKEN,
    environment=ENVIRONMENT
)

# ===================================================
# BOT SETTINGS
# ===================================================

COOLDOWN_SEC = 5        # FIX 1: was 10, reduced to 5
LAST_TRADE_TIME = 0
LAST_TRADE_ID = None

# ===================================================
# SYMBOL CONVERSION
# ===================================================

SYMBOL_MAP = {
    # Majors
    "EURUSD": "EUR_USD",
    "GBPUSD": "GBP_USD",
    "USDJPY": "USD_JPY",
    "USDCHF": "USD_CHF",
    "AUDUSD": "AUD_USD",
    "USDCAD": "USD_CAD",
    "NZDUSD": "NZD_USD",
    # Crosses
    "EURGBP": "EUR_GBP",
    "EURJPY": "EUR_JPY",
    "EURCAD": "EUR_CAD",
    "EURAUD": "EUR_AUD",
    "EURNZD": "EUR_NZD",
    "EURCHF": "EUR_CHF",
    "GBPJPY": "GBP_JPY",
    "GBPCAD": "GBP_CAD",
    "GBPAUD": "GBP_AUD",
    "GBPNZD": "GBP_NZD",
    "GBPCHF": "GBP_CHF",
    "AUDJPY": "AUD_JPY",
    "AUDCAD": "AUD_CAD",
    "AUDNZD": "AUD_NZD",
    "AUDCHF": "AUD_CHF",
    "NZDJPY": "NZD_JPY",
    "NZDCAD": "NZD_CAD",
    "NZDCHF": "NZD_CHF",
    "CADJPY": "CAD_JPY",
    "CADCHF": "CAD_CHF",
    "CHFJPY": "CHF_JPY",
    # Metals
    "XAUUSD": "XAU_USD",
    "XAGUSD": "XAG_USD",
}

def clean_symbol(raw):
    s = raw.upper().strip()
    s = s.replace("/", "").replace("-", "").replace("_", "")
    if ":" in s:
        s = s.split(":")[-1]
    return s

def format_symbol(raw):
    s = clean_symbol(raw)
    if s in SYMBOL_MAP:
        return SYMBOL_MAP[s]
    if len(s) == 6:
        return f"{s[:3]}_{s[3:]}"
    return s

# ===================================================
# DECIMAL RULES
# ===================================================

def decimal_places(symbol):
    if "JPY" in symbol:
        return 3
    if "XAU" in symbol:
        return 2
    return 5

# ===================================================
# GET PRICE
# ===================================================

def get_price(symbol, action):
    r = pricing.PricingInfo(ACCOUNT_ID, params={"instruments": symbol})
    resp = client.request(r)
    price_data = resp["prices"][0]
    if action == "buy":
        return float(price_data["closeoutAsk"])
    else:
        return float(price_data["closeoutBid"])

# ===================================================
# SPREAD PROTECTION
# ===================================================

def spread_too_large(symbol):
    r = pricing.PricingInfo(ACCOUNT_ID, params={"instruments": symbol})
    resp = client.request(r)
    p = resp["prices"][0]
    ask = float(p["closeoutAsk"])
    bid = float(p["closeoutBid"])
    spread = ask - bid
    if "JPY" in symbol:
        max_spread = 0.05       # 5 pips for JPY
    elif "XAU" in symbol:
        max_spread = 0.50       # 50 pips for Gold
    else:
        max_spread = 0.003      # FIX 2: was 0.002 (20 pips), now 0.003 (30 pips)
    if spread > max_spread:
        log.warning(f"Spread too large: {spread} > {max_spread} — trade blocked")
        return True
    return False

# ===================================================
# POSITION CHECK
# ===================================================

def position_exists(symbol):
    try:
        r = positions.PositionDetails(ACCOUNT_ID, instrument=symbol)
        resp = client.request(r)
        long_units = float(resp["position"]["long"]["units"])
        short_units = float(resp["position"]["short"]["units"])
        if long_units != 0 or short_units != 0:
            return True
    except:
        pass
    return False

# ===================================================
# CLOSE POSITION
# ===================================================

def close_position(symbol, side):
    try:
        if side == "buy":
            data = {"longUnits": "ALL"}
        else:
            data = {"shortUnits": "ALL"}
        r = positions.PositionClose(ACCOUNT_ID, instrument=symbol, data=data)
        resp = client.request(r)
        log.info(f"CLOSED {side} position on {symbol}: {json.dumps(resp, indent=2)}")
    except Exception as e:
        log.error(f"Failed to close position: {e}")

# ===================================================
# OPEN TRADE — auto SL/TP if not provided
# ===================================================

def open_trade(symbol, action, stop_price, tp_price, size):

    if spread_too_large(symbol):
        log.warning("Spread too large — trade blocked")
        return

    if position_exists(symbol):
        log.warning("Position already open — trade blocked")
        return

    PIP_SIZE = 0.01 if "JPY" in symbol else 0.0001
    STOP_PIPS = 40
    TP_PIPS = 60

    price = get_price(symbol, action)
    dp = decimal_places(symbol)

    if stop_price == 0:
        if action == "buy":
            sl = round(price - (STOP_PIPS * PIP_SIZE), dp)
            tp = round(price + (TP_PIPS * PIP_SIZE), dp)
        else:
            sl = round(price + (STOP_PIPS * PIP_SIZE), dp)
            tp = round(price - (TP_PIPS * PIP_SIZE), dp)
    else:
        sl = round(float(stop_price), dp)
        tp = round(float(tp_price), dp)

    if action == "buy":
        units = int(float(size) * 100000)
        if sl >= price:
            log.error(f"Invalid SL for BUY: sl={sl} price={price}")
            return
    else:
        units = -int(float(size) * 100000)
        if sl <= price:
            log.error(f"Invalid SL for SELL: sl={sl} price={price}")
            return

    if units == 0:
        log.error("Units calculated as 0 — trade blocked")
        return

    order_data = {
        "order": {
            "instrument": symbol,
            "units": str(units),
            "type": "MARKET",
            "positionFill": "DEFAULT",
            "stopLossOnFill": {"price": str(sl)},
            "takeProfitOnFill": {"price": str(tp)}
        }
    }

    log.info(f"SENDING ORDER: {json.dumps(order_data, indent=2)}")

    r = orders.OrderCreate(ACCOUNT_ID, data=order_data)
    resp = client.request(r)
    log.info(f"ORDER RESPONSE: {json.dumps(resp, indent=2)}")  # FIX 3: was missing closing )

# ===================================================
# WEBHOOK
# ===================================================

@app.route("/webhook", methods=["POST"])
def webhook():

    global LAST_TRADE_TIME, LAST_TRADE_ID

    raw = request.data.decode("utf-8")
    log.info(f"WEBHOOK RECEIVED: {raw}")

    if not raw:
        return jsonify({"error": "empty body"}), 400

    try:
        data = json.loads(raw)
    except Exception as e:
        log.error(f"JSON parse error: {e}")
        return jsonify({"error": "invalid json"}), 400

    log.info(f"PARSED DATA: {data}")

    try:
        symbol_raw = data["symbol"]
        action     = data["action"].lower()
    except Exception as e:
        return jsonify({"error": f"missing field: {e}"}), 400

    symbol = format_symbol(symbol_raw)
    log.info(f"SYMBOL: {symbol} | ACTION: {action}")

    # ===== HANDLE CLOSE =====
    if action == "close":
        side = data.get("side", "")
        close_position(symbol, side)
        return jsonify({"status": "close sent"})

    # ===== HANDLE BUY / SELL =====
    try:
        stop_price = float(data.get("stop", 0))
        tp_price   = float(data.get("tp", 0))
        size       = float(data["size"])   # size is still required in the alert
    except Exception as e:
        log.error(f"Missing trade field: {e}")
        return jsonify({"error": f"missing field: {e}"}), 400

    # Cooldown check
    trade_id = f"{symbol}_{action}_{int(time.time() / 30)}"
    if trade_id == LAST_TRADE_ID:
        log.warning("Duplicate trade ignored")
        return jsonify({"status": "duplicate ignored"})

    if time.time() - LAST_TRADE_TIME < COOLDOWN_SEC:
        log.warning("Cooldown active")
        return jsonify({"status": "cooldown active"})

    LAST_TRADE_ID   = trade_id
    LAST_TRADE_TIME = time.time()

    try:
        open_trade(symbol, action, stop_price, tp_price, size)
    except Exception as e:
        log.error(f"Trade error: {e}")
        return jsonify({"error": str(e)}), 500

    return jsonify({"status": "trade sent"})

# ===================================================
# HEALTH CHECK
# ===================================================

@app.route("/health")
def health():
    try:
        r = accounts.AccountSummary(ACCOUNT_ID)
        resp = client.request(r)
        equity = resp["account"]["NAV"]
        return jsonify({"status": "running", "equity": equity})
    except Exception as e:
        return jsonify({"status": "error", "detail": str(e)}), 500

# ===================================================
# ROOT PAGE
# ===================================================

@app.route("/")
def home():
    return "Farleys OANDA Bot Running"

# ===================================================
# START SERVER
# ===================================================

if __name__ == "__main__":
    import os
    log.info("BOT STARTED")
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)














