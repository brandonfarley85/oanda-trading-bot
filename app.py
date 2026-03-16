try:
    symbol = data["symbol"]
    action = data["action"]
    stop_price = float(data["stop"])
    size = float(data["size"])
except Exception as e:
    return {"error": str(e)}, 400










