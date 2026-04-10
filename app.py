"""
AUDUSD Bot v2 — OANDA webhook receiver for TradingView Pine Script alerts
─────────────────────────────────────────────────────────────────────────
All protections:
  ✓ One trade at a time  — no pyramiding ever
  ✓ Max 3 trades per rolling 12-hour window
  ✓ Session filter       — Asian / London / NY overlap only
  ✓ News protection      — blocks ForexFactory red/orange event windows
  ✓ Spike window         — no new trades 4:40-6:05 PM NY
                           background thread auto-removes SL from open trades
  ✓ Low volume filter    — skips midday dead zone and rollover period
  ✓ SL / TP / Trail      — parsed dynamically from Pine Script alert JSON
                           change them in Pine Script settings, bot uses those values

Required environment variables on Render:
  OANDA_ACCOUNT_ID
  OANDA_ACCESS_TOKEN
  OANDA_ENVIRONMENT   (practice | live)

Endpoints:
  POST https://your-app.onrender.com/webhook  ← TradingView alert webhook URL
  GET  https://your-app.onrender.com/health
  GET  https://your-app.onrender.com/status
  GET  https://your-app.onrender.com/trades
"""

import os
import time
import logging
import threading
from datetime import datetime

import oandapyV20
import oandapyV20.endpoints.orders  as orders
import oandapyV20.endpoints.trades  as trades_ep
import oandapyV20.endpoints.pricing as pricing

from flask import Flask, request, jsonify

# ─── Logging ──────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("audusd_bot_v2")

# ─── Config ───────────────────────────────────────────────────────────────────
ACCOUNT_ID   = os.environ.get("OANDA_ACCOUNT_ID",  "YOUR_ACCOUNT_ID")
ACCESS_TOKEN = os.environ.get("OANDA_ACCESS_TOKEN", "YOUR_ACCESS_TOKEN")
ENVIRONMENT  = os.environ.get("OANDA_ENVIRONMENT",  "practice")
INSTRUMENT   = "AUD_USD"
PIP          = 0.0001

client = oandapyV20.API(access_token=ACCESS_TOKEN, environment=ENVIRONMENT)
app    = Flask(__name__)

# ─── Trade frequency state ────────────────────────────────────────────────────
_trade_lock       = threading.Lock()   # one webhook processed at a time
_trade_timestamps = []                 # unix timestamps of placed trades
_MAX_TRADES_12H   = 3
_12H_SECONDS      = 12 * 3600


def can_trade_12h() -> bool:
    """True if fewer than 3 trades have been placed in the last 12 hours."""
    now = time.time()
    _trade_timestamps[:] = [t for t in _trade_timestamps if now - t < _12H_SECONDS]
    used = len(_trade_timestamps)
    log.info(f"12h trade count: {used}/{_MAX_TRADES_12H} — {_MAX_TRADES_12H - used} remaining")
    return used < _MAX_TRADES_12H


def record_trade():
    """Stamp a trade so the 12-hour counter knows about it."""
    _trade_timestamps.append(time.time())
    log.info(f"Trade stamped — {len(_trade_timestamps)}/{_MAX_TRADES_12H} in last 12h")


# ─── Time helper ──────────────────────────────────────────────────────────────
def ny_now():
    """Current datetime in New York timezone."""
    try:
        from zoneinfo import ZoneInfo       # Python 3.9+
        return datetime.now(ZoneInfo("America/New_York"))
    except ImportError:
        import pytz
        return datetime.now(pytz.timezone("America/New_York"))


# ─── Session filter ───────────────────────────────────────────────────────────
# Best AUDUSD liquidity windows (NY Eastern):
#   Asian   7 PM – 4 AM   Sydney + Tokyo — AUD home market
#   London  3 AM – 12 PM  heavy EUR/GBP traffic lifts AUD pairs
#   NY      8 AM – 12 PM  London/NY overlap — highest volume of day
#
# Hard exclusions:
#   12 PM – 3 PM  NY  midday dead zone
#    5 PM – 7 PM  NY  rollover period (also contains spike window)
#   Friday after 12 PM, Sunday before 5 PM

def in_session() -> bool:
    t    = ny_now()
    mins = t.hour * 60 + t.minute
    dow  = t.weekday()                     # 0=Mon … 4=Fri … 6=Sun

    asian  = mins >= 1140 or mins < 240    # 7 PM – 4 AM
    london = 180 <= mins < 720             # 3 AM – 12 PM
    ny_ov  = 480 <= mins < 720             # 8 AM – 12 PM

    if not (asian or london or ny_ov):
        return False

    dead_zone = 720 <= mins < 900          # 12 PM – 3 PM
    rollover  = 1020 <= mins < 1140        # 5 PM – 7 PM
    fri_close = dow == 4 and mins >= 720   # Friday after 12 PM
    sun_wait  = dow == 6 and mins < 1020   # Sunday before 5 PM

    if dead_zone or rollover or fri_close or sun_wait:
        return False

    return True


# ─── News protection ──────────────────────────────────────────────────────────
# Update this list each week from ForexFactory (red and orange folders only).
# Format: (start_HHMM, end_HHMM, "description")

NEWS_BLOCKS = [
    (815,  900,  "NFP / CPI / US Jobs (8:15-9:00 AM)"),
    (1345, 1415, "FOMC statement (1:45-2:15 PM)"),
    (15,   100,  "RBA Rate Decision (12:15-1:00 AM)"),
    (1915, 2000, "AUD Employment Change (7:15-8:00 PM)"),
    (745,  815,  "BOE / BOC / BOJ decisions (7:45-8:15 AM)"),
    (1255, 1310, "Fed Chair press conference (12:55-1:10 PM)"),
]


def in_news_block():
    """Return (True, reason) if current NY time falls inside a news window."""
    t    = ny_now()
    hhmm = t.hour * 100 + t.minute
    for start, end, desc in NEWS_BLOCKS:
        if start <= hhmm <= end:
            return True, desc
    return False, ""


# ─── Spike / rollover window ──────────────────────────────────────────────────
# 4:40 PM – 6:05 PM NY  =  broker rollover, wide spreads, price spikes.
# No new trades. Background thread removes SL from any open trade.

def in_spike_window() -> bool:
    t    = ny_now()
    mins = t.hour * 60 + t.minute
    return 1000 <= mins <= 1085            # 4:40 PM = 1000, 6:05 PM = 1085


# ─── OANDA helpers ────────────────────────────────────────────────────────────

def get_open_trade():
    """Return first open AUDUSD trade dict, or None if flat."""
    r = trades_ep.TradesList(ACCOUNT_ID, params={"instrument": INSTRUMENT})
    client.request(r)
    open_trades = r.response.get("trades", [])
    return open_trades[0] if open_trades else None


def get_price():
    """Return (bid, ask) for AUDUSD."""
    r = pricing.PricingInfo(ACCOUNT_ID, params={"instruments": INSTRUMENT})
    client.request(r)
    p = r.response["prices"][0]
    return float(p["bids"][0]["price"]), float(p["asks"][0]["price"])


def remove_stop_loss(trade: dict):
    """
    Move SL 500 pips away during spike window.
    OANDA does not support deleting SL — we push it unreachably far.
    """
    trade_id = trade["id"]
    bid, ask = get_price()
    units    = float(trade["currentUnits"])

    sl_safe = round(bid - 0.0500, 5) if units > 0 else round(ask + 0.0500, 5)

    data = {"stopLoss": {"timeInForce": "GTC", "price": str(sl_safe)}}
    r    = trades_ep.TradeCRCDO(ACCOUNT_ID, tradeID=trade_id, data=data)
    client.request(r)
    log.info(f"Spike window: SL moved to {sl_safe} on trade {trade_id}")


def place_order(action: str, sl_pips: int, tp_pips: int,
                trail_pips: int, units: int) -> dict:
    """
    Place a MARKET order.
    sl_pips / tp_pips / trail_pips come from the Pine Script webhook JSON
    so whatever you set in TradingView settings is what gets traded.
    """
    bid, ask = get_price()
    sl_dist  = sl_pips    * PIP
    tp_dist  = tp_pips    * PIP
    tr_dist  = trail_pips * PIP

    if action == "buy":
        entry = ask
        sl_px = round(entry - sl_dist, 5)
        tp_px = round(entry + tp_dist, 5)
        qty   = str(units)
    else:
        entry = bid
        sl_px = round(entry + sl_dist, 5)
        tp_px = round(entry - tp_dist, 5)
        qty   = str(-units)

    data = {
        "order": {
            "type":       "MARKET",
            "instrument": INSTRUMENT,
            "units":      qty,
            "stopLossOnFill": {
                "price":       str(sl_px),
                "timeInForce": "GTC",
            },
            "takeProfitOnFill": {
                "price":       str(tp_px),
                "timeInForce": "GTC",
            },
            "trailingStopLossOnFill": {
                "distance":    str(round(tr_dist, 5)),
                "timeInForce": "GTC",
            },
        }
    }

    r = orders.OrderCreate(ACCOUNT_ID, data=data)
    client.request(r)
    log.info(
        f"ORDER {action.upper()} {qty} {INSTRUMENT} | "
        f"Entry≈{entry:.5f} | SL={sl_px:.5f} | TP={tp_px:.5f} | Trail={tr_dist:.5f}"
    )
    return r.response


def close_trade(trade: dict):
    r = trades_ep.TradeClose(ACCOUNT_ID, tradeID=trade["id"])
    client.request(r)
    log.info(f"Closed trade {trade['id']}")


# ─── Webhook handler ──────────────────────────────────────────────────────────

@app.route("/webhook", methods=["POST"])
def webhook():
    """
    Receives Pine Script alert JSON from TradingView.

    Long:  {"symbol":"AUD_USD","action":"buy", "sl":30,"tp":80,"trail":30,"units":1000}
    Short: {"symbol":"AUD_USD","action":"sell","sl":30,"tp":80,"trail":30,"units":1000}
    Close: {"symbol":"AUD_USD","action":"close"}
    """
    with _trade_lock:
        try:
            payload = request.get_json(force=True, silent=True)
            if not payload:
                log.warning("Webhook: received empty or invalid JSON")
                return jsonify({"status": "error", "msg": "invalid json"}), 400

            log.info(f"Webhook payload: {payload}")

            action = payload.get("action", "").lower()
            symbol = payload.get("symbol", "")

            # Only handle AUDUSD
            if symbol not in ("AUD_USD", "AUDUSD", ""):
                return jsonify({"status": "skip", "msg": "wrong symbol"})

            # ── CLOSE ─────────────────────────────────────────────────────
            if action == "close":
                trade = get_open_trade()
                if trade:
                    close_trade(trade)
                    return jsonify({"status": "closed", "trade_id": trade["id"]})
                return jsonify({"status": "no open trade to close"})

            # ── BUY / SELL ────────────────────────────────────────────────
            if action not in ("buy", "sell"):
                return jsonify({"status": "skip", "msg": f"unknown action: {action}"})

            # Parse SL/TP/Trail/Units from Pine Script payload
            sl_pips    = int(payload.get("sl",    30))
            tp_pips    = int(payload.get("tp",    80))
            trail_pips = int(payload.get("trail", 30))
            units      = int(payload.get("units", 1000))

            # ══════════════════════════════════════════════════════════════
            # LAYER 1 — No pyramiding: one trade at a time only
            # ══════════════════════════════════════════════════════════════
            existing = get_open_trade()
            if existing:
                log.info(f"Already in trade {existing['id']} — no new entry")

                # While we're here: spike window check on existing trade
                if in_spike_window():
                    sl_order = existing.get("stopLossOrder")
                    if sl_order:
                        sl_price  = float(sl_order.get("price", 0))
                        bid, ask  = get_price()
                        mid       = (bid + ask) / 2
                        if abs(sl_price - mid) < 0.0200:   # SL still within 200 pips
                            log.info("Spike window: removing SL from open trade")
                            try:
                                remove_stop_loss(existing)
                            except Exception as e:
                                log.error(f"Could not remove SL: {e}")

                return jsonify({"status": "skip", "msg": "trade already open — no pyramiding"})

            # ══════════════════════════════════════════════════════════════
            # LAYER 2 — Max 3 trades per 12 hours
            # ══════════════════════════════════════════════════════════════
            if not can_trade_12h():
                log.info(f"12-hour cap reached ({_MAX_TRADES_12H} trades) — skipping")
                return jsonify({"status": "skip", "msg": f"12h limit: already {_MAX_TRADES_12H} trades"})

            # ══════════════════════════════════════════════════════════════
            # LAYER 3 — Spike / rollover window (4:40–6:05 PM NY)
            # ══════════════════════════════════════════════════════════════
            if in_spike_window():
                log.info("Spike window active — no new trades")
                return jsonify({"status": "skip", "msg": "spike window 4:40-6:05 PM NY"})

            # ══════════════════════════════════════════════════════════════
            # LAYER 4 — Session & volume filter
            # ══════════════════════════════════════════════════════════════
            if not in_session():
                t = ny_now()
                log.info(f"Off session or low volume ({t.strftime('%H:%M')} NY) — skipping")
                return jsonify({"status": "skip", "msg": "off session or low volume"})

            # ══════════════════════════════════════════════════════════════
            # LAYER 5 — News protection (ForexFactory red/orange events)
            # ══════════════════════════════════════════════════════════════
            is_news, news_reason = in_news_block()
            if is_news:
                log.info(f"News block: {news_reason} — skipping")
                return jsonify({"status": "skip", "msg": f"news block: {news_reason}"})

            # ══════════════════════════════════════════════════════════════
            # ALL CHECKS PASSED — place the trade
            # ══════════════════════════════════════════════════════════════
            log.info(
                f"All checks passed → {action.upper()} "
                f"SL={sl_pips}p TP={tp_pips}p Trail={trail_pips}p Units={units}"
            )
            result = place_order(action, sl_pips, tp_pips, trail_pips, units)
            record_trade()
            return jsonify({"status": "ok", "order": str(result)[:300]})

        except oandapyV20.exceptions.V20Error as e:
            log.error(f"OANDA API error: {e}")
            return jsonify({"status": "error", "msg": str(e)}), 500
        except Exception as e:
            log.error(f"Unexpected error: {e}", exc_info=True)
            return jsonify({"status": "error", "msg": str(e)}), 500


# ─── Background spike monitor ─────────────────────────────────────────────────

def spike_monitor():
    """
    Runs every 30 seconds in a background thread.
    During the spike window (4:40–6:05 PM NY), automatically moves the SL
    on any open trade far away so broker price spikes can't trigger it.
    """
    log.info("Spike monitor: started")
    while True:
        try:
            if in_spike_window():
                trade = get_open_trade()
                if trade:
                    sl_order = trade.get("stopLossOrder")
                    if sl_order:
                        sl_price  = float(sl_order.get("price", 0))
                        bid, ask  = get_price()
                        mid       = (bid + ask) / 2
                        if abs(sl_price - mid) < 0.0200:
                            log.info("Spike monitor: auto-removing close SL")
                            remove_stop_loss(trade)
        except Exception as e:
            log.warning(f"Spike monitor non-fatal error: {e}")
        time.sleep(30)


# ─── Info endpoints ───────────────────────────────────────────────────────────

@app.route("/health", methods=["GET"])
def health():
    t        = ny_now()
    spike    = in_spike_window()
    sess     = in_session()
    news, nr = in_news_block()
    now_ts   = time.time()
    _trade_timestamps[:] = [ts for ts in _trade_timestamps if now_ts - ts < _12H_SECONDS]

    return jsonify({
        "status":         "ok",
        "ny_time":        t.strftime("%Y-%m-%d %H:%M:%S"),
        "in_session":     sess,
        "spike_window":   spike,
        "news_block":     news,
        "news_reason":    nr,
        "trades_12h":     len(_trade_timestamps),
        "max_trades_12h": _MAX_TRADES_12H,
        "can_trade_now":  sess and not spike and not news and can_trade_12h(),
        "environment":    ENVIRONMENT,
        "instrument":     INSTRUMENT,
    })


@app.route("/status", methods=["GET"])
def status():
    """Current open trade details."""
    try:
        trade = get_open_trade()
        if trade:
            return jsonify({
                "trade_id":      trade["id"],
                "units":         trade["currentUnits"],
                "open_price":    trade["price"],
                "unrealized_pl": trade.get("unrealizedPL", "n/a"),
                "stop_loss":     trade.get("stopLossOrder",          {}).get("price",    "none"),
                "take_profit":   trade.get("takeProfitOrder",         {}).get("price",    "none"),
                "trailing_stop": trade.get("trailingStopLossOrder",   {}).get("distance", "none"),
            })
        return jsonify({"status": "flat — no open trade"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/trades", methods=["GET"])
def trades_info():
    """Rolling 12-hour trade count."""
    now = time.time()
    _trade_timestamps[:] = [ts for ts in _trade_timestamps if now - ts < _12H_SECONDS]
    return jsonify({
        "trades_in_12h": len(_trade_timestamps),
        "max_allowed":   _MAX_TRADES_12H,
        "slots_left":    max(0, _MAX_TRADES_12H - len(_trade_timestamps)),
        "times":         [datetime.fromtimestamp(ts).strftime("%H:%M:%S") for ts in _trade_timestamps],
    })


# ─── Entry point ──────────────────────────────────────────────────────────────

if __name__ == "__main__":
    log.info("=" * 60)
    log.info("AUDUSD Bot v2")
    log.info(f"  Environment  : {ENVIRONMENT.upper()}")
    log.info(f"  Instrument   : {INSTRUMENT}")
    log.info(f"  Max trades   : {_MAX_TRADES_12H} per 12 hours")
    log.info(f"  Webhook      : POST /webhook")
    log.info(f"  Health check : GET  /health")
    log.info(f"  Trade status : GET  /status")
    log.info(f"  Trade count  : GET  /trades")
    log.info("=" * 60)

    # Start spike monitor background thread
    t = threading.Thread(target=spike_monitor, daemon=True)
    t.start()

    # Start Flask — Render injects PORT automatically
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)












