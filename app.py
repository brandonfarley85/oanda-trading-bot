"""
Farleys Pinpoint Velocity BOT
OANDA Webhook Server — CLEAN STABLE VERSION
"""

from flask import Flask, request, jsonify
import oandapyV20
import oandapyV20.endpoints.orders as orders
import oandapyV20.endpoints.positions as positions
import oandapyV20.endpoints.pricing as pricing
import oandapyV20.endpoints.accounts as accounts
import json
import math
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

FIXED_UNITS = 75000
TP_PERCENT = 3.0
COOLDOWN_SEC = 10

LAST_TRADE_TIME = 0
LAST_TRADE_ID = None

# ===================================================
# SYMBOL CONVERSION
# ===================================================

SYMBOL_MAP = {
    "EURUSD":"EUR_USD",
    "GBPUSD":"GBP_USD",
    "USDJPY":"USD_JPY",
    "USDCHF":"USD_CHF",
    "AUDUSD":"AUD_USD",
    "USDCAD":"USD_CAD",
    "NZDUSD":"NZD_USD",
    "XAUUSD":"XAU_USD"
}

def clean_symbol(raw):
    s = raw.upper().strip()
    s = s.replace("/", "").replace("-", "")
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

    r = pricing.PricingInfo(
        ACCOUNT_ID,
        params={"instruments":symbol}
    )

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

    r = pricing.PricingInfo(
        ACCOUNT_ID,
        params={"instruments":symbol}
    )

    resp = client.request(r)
    p = resp["prices"][0]

    ask = float(p["closeoutAsk"])
    bid = float(p["closeoutBid"])

    spread = ask - bid

    if spread > ask * 0.001:
        return True

    return False

# ===================================================
# POSITION CHECK
# ===================================================

def position_exists(symbol):

    try:

        r = positions.PositionDetails(
            ACCOUNT_ID,
            instrument=symbol
        )

        resp = client.request(r)

        long_units = float(resp["position"]["long"]["units"])
        short_units = float(resp["position"]["short"]["units"])

        if long_units != 0 or short_units != 0:
            return True

    except:
        pass

    return False

# ===================================================
# STOP DISTANCE VALIDATION
# ===================================================

def validate_stop_distance(v):

    v = float(v)

    if math.isnan(v) or math.isinf(v) or v <= 0:
        raise ValueError("Invalid stop distance")

    if v > 0.02:
        raise ValueError("Stop distance too large")

    return v

# ===================================================
# OPEN TRADE
# ===================================================

def open_trade(symbol, action, stop_distance, size, tp):


    if spread_too_large(symbol):
        log.warning("Spread too large")
        return

    if position_exists(symbol):
        log.warning("Position already open")
        return

    price = get_price(symbol, action)
    dp = decimal_places(symbol)

    
    if action == "buy":

        units = int(float(size) * 100000)
        sl = round(price - stop_distance, dp)
        tp = round(price + tp_distance, dp)

    else:

        units = -int(float(size) * 100000)
        sl = round(price + stop_distance, dp)
        tp = round(price - tp_distance, dp)

    order_data = {
        "order": {
            "instrument": symbol,
            "units": str(units),
            "type": "MARKET",
            "positionFill": "DEFAULT",

            "stopLossOnFill": {
                "price": str(sl)
            },

            "takeProfitOnFill": {
                "price": str(tp)
            }
        }
    }

    r = orders.OrderCreate(
        ACCOUNT_ID,
        data=order_data
    )

    resp = client.request(r)

    log.info(json.dumps(resp, indent=2))

# ===================================================
# WEBHOOK
# ===================================================

@app.route("/webhook", methods=["POST"])
def webhook():

    global LAST_TRADE_TIME
    global LAST_TRADE_ID

    raw = request.data.decode("utf-8")

    if not raw:
        return jsonify({"error":"empty body"}),400

    try:
        data = json.loads(raw)
    except:
        return jsonify({"error":"invalid json"}),400

    symbol = format_symbol(data["symbol"])
    action = data["action"]

    trade_id = f"{symbol}_{action}_{int(time.time()/30)}"

    if trade_id == LAST_TRADE_ID:
        return jsonify({"status":"duplicate ignored"})

    if time.time() - LAST_TRADE_TIME < COOLDOWN_SEC:
        return jsonify({"status":"cooldown active"})

    LAST_TRADE_ID = trade_id
    LAST_TRADE_TIME = time.time()

    try:

        price = get_price(symbol, action)

stop_price = float(data["stop"])
stop_distance = abs(price - stop_price)

stop_distance = validate_stop_distance(stop_distance)

        open_trade(symbol, action, stop_distance, data["size"], data["tp"])

    except Exception as e:

        log.error(str(e))
        return jsonify({"error":str(e)}),500

    return jsonify({"status":"trade sent"})

# ===================================================
# HEALTH CHECK
# ===================================================

@app.route("/health")
def health():

    try:

        r = accounts.AccountSummary(ACCOUNT_ID)
        resp = client.request(r)

        equity = resp["account"]["NAV"]

        return jsonify({
            "status":"running",
            "equity":equity
        })

    except:
        return jsonify({"status":"error"}),500

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

    log.info("BOT STARTED")

    app.run(
        host="0.0.0.0",
        port=10000
    )


















