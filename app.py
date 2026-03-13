"""
Farleys Pinpoint Velocity BOT - OANDA Webhook Server v7
FIXED VERSION -- resolves all 400 BAD REQUEST errors

HOW TO RUN:
  1. pip install flask oandapyV20
  2. python oanda_bot.py
  3. ngrok http 5000
  4. TradingView alert:
       Webhook URL -> https://YOUR_NGROK_URL/webhook
       Message box -> LEAVE COMPLETELY BLANK
       Frequency   -> Once Per Bar Close

EXPECTED PAYLOADS FROM PINE SCRIPT:
  {"symbol":"EURUSD","action":"buy","stop_distance":0.00045}
  {"symbol":"EURUSD","action":"sell","stop_distance":0.00045}
  {"symbol":"EURUSD","action":"breakeven","side":"buy","entry":1.12345}
  {"symbol":"EURUSD","action":"breakeven","side":"sell","entry":1.12345}
  {"symbol":"EURUSD","action":"close_buy"}
  {"symbol":"EURUSD","action":"close_sell"}
"""

from flask import Flask, request, jsonify
import oandapyV20
import oandapyV20.endpoints.orders    as orders
import oandapyV20.endpoints.positions as positions
import oandapyV20.endpoints.pricing   as pricing
import oandapyV20.endpoints.accounts  as accounts
import oandapyV20.endpoints.trades    as trades
import json
import math
import logging
from datetime import datetime

# ===================================================
# ===== LOGGING
# ===================================================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("bot.log")
    ]
)
log = logging.getLogger(__name__)

app = Flask(__name__)

# ===================================================
# ===== CREDENTIALS
# ===================================================
ACCOUNT_ID     = "101-001-24987993-002"
ACCESS_TOKEN   = "1a08faff98fd3fef165a4ee4ca69f245-43834b16d9b0baf270518ce1edc5f2dd"
WEBHOOK_SECRET = ""
OANDA_ENV      = "practice"

client = oandapyV20.API(access_token=ACCESS_TOKEN, environment=OANDA_ENV)

# ===================================================
# ===== BOT SETTINGS
# ===================================================
FIXED_UNITS = 75_000
TP_PERCENT  = 3.0

# ===================================================
# ===== SYMBOL MAP
# ===================================================
SYMBOL_MAP = {
    "EURUSD": "EUR_USD", "GBPUSD": "GBP_USD", "USDJPY": "USD_JPY",
    "USDCHF": "USD_CHF", "AUDUSD": "AUD_USD", "USDCAD": "USD_CAD",
    "NZDUSD": "NZD_USD", "XAUUSD": "XAU_USD", "GBPJPY": "GBP_JPY",
    "EURJPY": "EUR_JPY", "EURGBP": "EUR_GBP", "AUDJPY": "AUD_JPY",
    "CADJPY": "CAD_JPY", "CHFJPY": "CHF_JPY", "EURAUD": "EUR_AUD",
    "EURCAD": "EUR_CAD", "GBPAUD": "GBP_AUD", "GBPCAD": "GBP_CAD",
    "AUDCAD": "AUD_CAD", "AUDNZD": "AUD_NZD", "NZDJPY": "NZD_JPY",
    "US30":   "US30_USD", "SPX500": "SPX500_USD",
    "NAS100": "NAS100_USD", "GER40": "DE40_EUR",
    "EUR_USD": "EUR_USD", "GBP_USD": "GBP_USD", "USD_JPY": "USD_JPY",
    "XAU_USD": "XAU_USD",
}


# ===================================================
# ===== HELPERS
# ===================================================

def clean_symbol(raw: str) -> str:
    """
    Strip exchange prefixes and normalise to plain ticker.
    TradingView can send: OANDA:EURUSD, FX:EURUSD, FX_IDC:EURUSD, EURUSD
    We strip everything up to and including the colon.
    """
    s = raw.upper().strip()
    if ":" in s:
        s = s.split(":")[-1]
    s = s.replace("/", "")
    s = s.replace("-", "")
    return s


def format_symbol(raw: str) -> str:
    """Convert TradingView ticker -> OANDA instrument string."""
    s = clean_symbol(raw)
    if s in SYMBOL_MAP:
        return SYMBOL_MAP[s]
    if "_" in s:
        return s
    if len(s) == 6:
        return f"{s[:3]}_{s[3:]}"
    log.warning(f"Unknown symbol '{raw}' (cleaned='{s}') -- passing through unchanged")
    return s


def decimal_places(symbol: str) -> int:
    """Correct decimal precision for SL/TP price strings per instrument."""
    s = symbol.upper()
    if "JPY" in s:                                                       return 3
    if "XAU" in s or "GOLD" in s:                                        return 2
    if any(x in s for x in ["US30","SPX500","NAS100","DE40","UK100"]):   return 1
    return 5


def get_live_price(symbol: str, action: str) -> float:
    """
    Fetch live bid/ask from OANDA.
    BUY  -> Ask (closeoutAsk)
    SELL -> Bid (closeoutBid)
    """
    r    = pricing.PricingInfo(ACCOUNT_ID, params={"instruments": symbol})
    resp = client.request(r)
    pd   = resp["prices"][0]
    price = float(pd["closeoutAsk"]) if action == "buy" else float(pd["closeoutBid"])
    log.info(f"Live price {symbol} [{action}]: {price}")
    return price


def get_open_position(symbol: str):
    """
    Returns (side, [trade_ids]) or (None, []) if flat.
    Used to block double entries and find trades for SL modification.
    """
    try:
        r    = positions.PositionDetails(ACCOUNT_ID, instrument=symbol)
        resp = client.request(r)
        long_units  = float(resp["position"]["long"]["units"])
        short_units = float(resp["position"]["short"]["units"])
        if long_units > 0:
            ids = [t["tradeID"] for t in resp["position"]["long"].get("tradeIDs", [])]
            return "buy", ids
        if short_units < 0:
            ids = [t["tradeID"] for t in resp["position"]["short"].get("tradeIDs", [])]
            return "sell", ids
        return None, []
    except Exception as e:
        log.info(f"No open position for {symbol}: {e}")
        return None, []


def validate_stop_distance(value) -> float:
    """
    Parse and validate stop_distance.
    Raises ValueError if value is NaN, Infinity, zero, or non-numeric.
    """
    try:
        v = float(value)
    except (TypeError, ValueError):
        raise ValueError(f"stop_distance is not a number: {value!r}")

    if math.isnan(v):
        raise ValueError(
            "stop_distance is NaN -- ATR not yet calculated on this bar. "
            "Pine Script guard (not na(stopDist)) should prevent this."
        )
    if math.isinf(v):
        raise ValueError(f"stop_distance is Infinity -- invalid value")
    if v <= 0:
        raise ValueError(f"stop_distance must be > 0, got {v}")

    return v


# ===================================================
# ===== OPEN TRADE
# ===================================================

def open_trade(symbol: str, action: str, stop_distance: float):
    """
    Open a market order on OANDA.

    Units:       Fixed at 75,000 every trade
    Stop Loss:   Swing High/Low distance from Pine Script
    Take Profit: 3% from live entry price (bot calculates)
    """
    side, _ = get_open_position(symbol)
    if side is not None:
        log.warning(f"Skipping {action} -- already in {side} position for {symbol}")
        return

    price = get_live_price(symbol, action)
    dp    = decimal_places(symbol)

    tp_distance = price * (TP_PERCENT / 100)

    if action == "buy":
        signed_units = FIXED_UNITS
        sl_price     = round(price - stop_distance, dp)
        tp_price     = round(price + tp_distance,   dp)
    else:
        signed_units = -FIXED_UNITS
        sl_price     = round(price + stop_distance, dp)
        tp_price     = round(price - tp_distance,   dp)

    log.info(
        f"OPEN {action.upper()} | {symbol} | {signed_units:,} units | "
        f"Entry~{price} | SL={sl_price} (dist={stop_distance:.6f}) | "
        f"TP={tp_price} ({TP_PERCENT}% = {tp_distance:.6f})"
    )

    data = {
        "order": {
            "instrument":   symbol,
            "units":        str(signed_units),
            "type":         "MARKET",
            "positionFill": "DEFAULT",
            "stopLossOnFill":   {"price": str(sl_price)},
            "takeProfitOnFill": {"price": str(tp_price)},
        }
    }

    r    = orders.OrderCreate(ACCOUNT_ID, data=data)
    resp = client.request(r)
    log.info(f"Order placed: {json.dumps(resp, indent=2)}")


# ===================================================
# ===== BREAKEVEN -- MOVES SL ONLY, NEVER EXITS
# ===================================================

def move_to_breakeven(symbol: str, side: str, entry_price: float):
    """
    Moves SL to entry price only.
    TP is left completely untouched.
    Trade stays 100% open.
    """
    pos_side, trade_ids = get_open_position(symbol)

    if pos_side is None:
        log.warning(f"Breakeven skipped -- no open position for {symbol}")
        return

    if pos_side != side:
        log.warning(f"Breakeven skipped -- side mismatch: wanted {side}, have {pos_side}")
        return

    dp       = decimal_places(symbol)
    be_price = round(entry_price, dp)

    log.info(
        f"BREAKEVEN | {symbol} {side.upper()} | "
        f"Moving SL -> {be_price} | Trade stays OPEN | TP untouched | "
        f"Trade IDs: {trade_ids}"
    )

    for trade_id in trade_ids:
        try:
            data = {
                "stopLoss": {
                    "price":       str(be_price),
                    "timeInForce": "GTC"
                }
            }
            r    = trades.TradeCRCDO(ACCOUNT_ID, tradeID=trade_id, data=data)
            resp = client.request(r)
            log.info(f"SL moved to {be_price} on trade {trade_id} -- trade fully open")
        except Exception as e:
            log.error(f"Failed to update SL on trade {trade_id}: {e}")


# ===================================================
# ===== EMERGENCY CLOSE (safety net only)
# ===================================================

def close_position(symbol: str, side: str):
    """
    Safety net -- fires if Pine detects trade closed unexpectedly.
    Under normal operation OANDA's SL/TP handles all exits.
    """
    data = {"longUnits": "ALL"} if side == "buy" else {"shortUnits": "ALL"}
    log.info(f"SAFETY NET CLOSE | {symbol} {side.upper()}")
    try:
        r    = positions.PositionClose(ACCOUNT_ID, instrument=symbol, data=data)
        resp = client.request(r)
        log.info(f"Closed: {json.dumps(resp, indent=2)}")
    except Exception as e:
        log.warning(f"Close note for {symbol} {side} (likely already closed by SL/TP): {e}")


# ===================================================
# ===== WEBHOOK ENDPOINT
# ===================================================

@app.route("/webhook", methods=["POST"])
def webhook():
    """
    Receives TradingView alerts from Pine Script.

    TradingView setup:
      Message box -> LEAVE COMPLETELY BLANK (Pine fills it)
      Frequency   -> Once Per Bar Close
      URL         -> https://YOUR_NGROK_URL/webhook
    """

    if WEBHOOK_SECRET:
        if request.headers.get("X-Webhook-Secret", "") != WEBHOOK_SECRET:
            log.warning("Unauthorized webhook attempt")
            return jsonify({"status": "unauthorized"}), 403

    raw_body = request.data.decode("utf-8", errors="replace").strip()
    log.info(f"RAW BODY: {raw_body!r}")

    if not raw_body:
        log.error("Empty request body")
        return jsonify({
            "status":  "error",
            "message": "Empty body. Make sure TradingView alert message box is BLANK."
        }), 400

    try:
        data = json.loads(raw_body)
    except json.JSONDecodeError as e:
        log.error(f"JSON parse error: {e} | Raw body: {raw_body!r}")
        return jsonify({
            "status":   "error",
            "message":  f"Invalid JSON: {e}",
            "raw_body": raw_body
        }), 400

    log.info(f"PARSED: {data}")

    raw_symbol = data.get("symbol")
    action     = data.get("action")

    if not raw_symbol or not action:
        log.error(f"Missing symbol or action in: {data}")
        return jsonify({
            "status":  "error",
            "message": "Missing 'symbol' or 'action' field",
            "received": data
        }), 400

    symbol = format_symbol(raw_symbol)
    log.info(f"Symbol: '{raw_symbol}' -> '{symbol}' | Action: '{action}'")

    try:
        if action in ("buy", "sell"):
            raw_sd = data.get("stop_distance")
            if raw_sd is None:
                return jsonify({
                    "status":  "error",
                    "message": "Missing 'stop_distance' for buy/sell action"
                }), 400

            stop_distance = validate_stop_distance(raw_sd)
            open_trade(symbol, action, stop_distance)

        elif action == "breakeven":
            side        = data.get("side")
            entry_price = data.get("entry")
            if not side or entry_price is None:
                return jsonify({
                    "status":  "error",
                    "message": "Breakeven requires 'side' and 'entry' fields"
                }), 400
            move_to_breakeven(symbol, side, float(entry_price))

        elif action == "close_buy":
            close_position(symbol, "buy")

        elif action == "close_sell":
            close_position(symbol, "sell")

        else:
            log.warning(f"Unknown action: '{action}'")
            return jsonify({
                "status":  "error",
                "message": f"Unknown action: '{action}'"
            }), 400

    except ValueError as e:
        log.error(f"Validation error: {e}")
        return jsonify({
            "status":  "error",
            "message": str(e)
        }), 400

    except KeyError as e:
        log.error(f"Missing field in payload: {e}")
        return jsonify({
            "status":  "error",
            "message": f"Missing field: {e}"
        }), 400

    except Exception as e:
        log.error(f"Unexpected error processing {action} for {symbol}: {e}", exc_info=True)
        return jsonify({
            "status":  "error",
            "message": str(e)
        }), 500

    return jsonify({
        "status":    "ok",
        "symbol":    symbol,
        "action":    action,
        "timestamp": datetime.utcnow().isoformat()
    })


# ===================================================
# ===== HEALTH CHECK
# ===================================================

@app.route("/health", methods=["GET"])
def health():
    try:
        r      = accounts.AccountSummary(ACCOUNT_ID)
        resp   = client.request(r)
        equity = float(resp["account"]["NAV"])
        ok     = True
    except Exception:
        equity = None
        ok     = False

    return jsonify({
        "status":      "running",
        "account_ok":  ok,
        "equity":      equity,
        "fixed_units": FIXED_UNITS,
        "tp_percent":  TP_PERCENT,
        "environment": OANDA_ENV,
        "account_id":  ACCOUNT_ID,
        "timestamp":   datetime.utcnow().isoformat()
    })


# ===================================================
# ===== STARTUP
# ===================================================

if __name__ == "__main__":
    log.info("=" * 60)
    log.info("  Farleys Pinpoint Velocity BOT -- v7 (FIXED)")
    log.info("=" * 60)
    log.info(f"  Environment  : {OANDA_ENV.upper()}")
    log.info(f"  Account      : {ACCOUNT_ID}")
    log.info(f"  Lot size     : 0.75 lots ({FIXED_UNITS:,} units) -- FIXED every trade")
    log.info(f"  Stop Loss    : Swing High/Low distance from Pine Script")
    log.info(f"  Take Profit  : {TP_PERCENT}% from live entry price")
    log.info(f"  Breakeven    : Fires at 1.5% profit -- moves SL to entry only")
    log.info(f"  Early exits  : NONE -- only OANDA SL/TP closes trades")
    log.info("=" * 60)
    log.info("  Alert setup  : Message box BLANK | Once Per Bar Close")
    log.info("=" * 60)
    app.run(host="0.0.0.0", port=5000, debug=False)