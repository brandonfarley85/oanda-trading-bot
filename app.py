"""
Farleys Pinpoint Velocity BOT
OANDA Webhook Server — VELOCITY STRATEGY VERSION
- Stop and TP always sent by Pine Script (structure-based)
- Size sent as lot size by Pine Script (0.01 minimum)
- Symbol arrives pre-formatted as EUR_USD style
- Spread protection built in
- Cooldown 5 seconds
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

ACCOUNT_ID  = "101-001-24987993-002"
ACCESS_TOKEN = "1a08faff98fd3fef165a4ee4ca69f245-43834b16d9b0baf270518ce1edc5f2dd"
ENVIRONMENT  = "practice"

client = oandapyV20.API(
    access_token=ACCESS_TOKEN,
    environment=ENVIRONMENT
)

# ===================================================
# BOT SETTINGS
# ===================================================

COOLDOWN_SEC    = 5
LAST_TRADE_TIME = 0
LAST_TRADE_ID   = None

# ===================================================
# SYMBOL CLEANUP
# — Pine Script sends EUR_USD format already
# — This just makes sure it's clean
# ===================================================

def format_symbol(raw):
    s = raw.upper().strip()
    # Already in EUR_USD format from Pine — just clean any junk
    s = s.replace("OANDA:", "").replace("/", "")
    # If it came in without underscore (e.g. EURUSD), add it
    if "_" not in s and len(s) == 6:
        s = s[:3] + "_" + s[3:]
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
        max_spread = 0.05
    elif "XAU" in symbol:
        max_spread = 0.50
    else:
        max_spread = 0.003
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
        long_units  = float(resp["position"]["long"]["units"])
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
# OPEN TRADE
# — Velocity Pine ALWAYS sends stop and tp
# — Size comes as lot size (e.g. 0.05) → convert to units
# ===================================================

def open_trade(symbol, action, stop_price, tp_price, lot_size):

    if spread_too_large(symbol):
        log.warning("Spread too large — trade blocked")
        return

    if position_exists(symbol):
        log.warning("Position already open — trade blocked")
        return

    dp = decimal_places(symbol)

    # Stop and TP always come from Pine for Velocity Bot
    sl = round(float(stop_price), dp)
    tp = round(float(tp_price), dp)

    # Convert lot size to OANDA units (1 lot = 100,000 units)
    if action == "buy":
        units = int(round(float(lot_size) * 100000))
    else:
        units = -int(round(float(lot_size) * 100000))

    if units == 0:
        log.error("Units calculated as 0 — trade blocked")
        return

    # Get current price for validation
    r_price = pricing.PricingInfo(ACCOUNT_ID, params={"instruments": symbol})
    resp_price = client.request(r_price)
    price_data = resp_price["prices"][0]
    if action == "buy":
        price = float(price_data["closeoutAsk"])
        if sl >= price:
            log.error(f"Invalid SL for BUY: sl={sl} >= price={price} — trade blocked")
            return
        if tp <= price:
            log.error(f"Invalid TP for BUY: tp={tp} <= price={price} — trade blocked")
            return
    else:
        price = float(price_data["closeoutBid"])
        if sl <= price:
            log.error(f"Invalid SL for SELL: sl={sl} <= price={price} — trade blocked")
            return
        if tp >= price:
            log.error(f"Invalid TP for SELL: tp={tp} >= price={price} — trade blocked")
            return

    order_data = {
        "order": {
            "instrument":    symbol,
            "units":         str(units),
            "type":          "MARKET",
            "positionFill":  "DEFAULT",
            "stopLossOnFill":   {"price": str(sl)},
            "takeProfitOnFill": {"price": str(tp)}
        }
    }

    log.info(f"SENDING ORDER: {json.dumps(order_data, indent=2)}")

    r = orders.OrderCreate(ACCOUNT_ID, data=order_data)
    resp = client.request(r)
    log.info(f"ORDER RESPONSE: {json.dumps(resp, indent=2)}")

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
    # Velocity Pine always sends stop and tp — both are required
    try:
        stop_price = float(data["stop"])
        tp_price   = float(data["tp"])
        lot_size   = float(data["size"])
    except Exception as e:
        log.error(f"Missing required field: {e}")
        return jsonify({"error": f"missing field: {e} — Velocity Bot requires stop, tp, and size"}), 400

    if stop_price == 0 or tp_price == 0:
        log.error("Stop or TP is 0 — trade blocked. Velocity Bot requires valid stop and tp.")
        return jsonify({"error": "stop and tp must be non-zero"}), 400

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
        open_trade(symbol, action, stop_price, tp_price, lot_size)
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
    return "Farleys Velocity Bot Running"

# ===================================================
# START SERVER
# ===================================================

if __name__ == "__main__":
    import os
    log.info("VELOCITY BOT STARTED")
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)












