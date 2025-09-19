from datetime import datetime, timezone
from typing import Dict, Any, Tuple, Optional, List
import math

def _today_ddmmyy_ist() -> str:
    # Options expire at 5:30 PM IST; symbol uses ddmmyy format per Delta India guide
    # Use UTC now, convert to IST date for symbol tag
    # Simplified: IST = UTC+5:30; get IST date and format ddmmyy
    now_utc = datetime.now(timezone.utc)
    ist_offset = 5.5
    ist_seconds = int(ist_offset * 3600)
    ist_dt = now_utc.timestamp() + ist_seconds
    ist = datetime.fromtimestamp(ist_dt, tz=timezone.utc)  # naive approach but yields date roll
    return ist.strftime("%d%m%y")

def _pick_strikes(cmp_price: float, strikes: List[float]) -> Tuple[float, float]:
    up_target = cmp_price * 1.01
    dn_target = cmp_price * 0.99
    ce = min(strikes, key=lambda s: abs(s - up_target))
    pe = min(strikes, key=lambda s: abs(s - dn_target))
    return ce, pe

def _extract_btc_option_products(products: List[Dict[str, Any]], expiry_ddmmyy: str) -> Dict[str, Dict[str, Any]]:
    """
    Return dict: {symbol: product_obj} for C-BTC-<strike>-<ddmmyy> and P-BTC-<strike>-<ddmmyy>
    """
    out = {}
    for p in products:
        sym = p.get("symbol", "")
        if not sym or "BTC" not in sym:  # BTC options only
            continue
        # Expect sym like C-BTC-50000-210821 or P-BTC-48000-210821
        parts = sym.split("-")
        if len(parts) < 4:
            continue
        cp, underlying, strike_str, exp = parts[0], parts[1], parts[2], parts[3]
        if underlying != "BTC":
            continue
        if exp != expiry_ddmmyy:
            continue
        if cp not in ("C", "P"):
            continue
        try:
            strike = float(strike_str)
        except:
            continue
        out[sym] = p
    return out

def _parse_strike_from_symbol(symbol: str) -> Optional[float]:
    try:
        return float(symbol.split("-")[2])
    except:
        return None

def _current_option_premium(client, symbol: str) -> float:
    t = client.get_ticker_symbol(symbol)
    # pick last_price or close price from the ticker response
    # Delta docs mention latest price data within tickers
    last = t.get("result") or t  # support both plain/result wrappers
    # Normalize (the API commonly returns {'result': {'close': '...'}})
    obj = last.get("close") if isinstance(last, dict) and "close" in last else None
    if obj is not None:
        try:
            return float(obj)
        except:
            pass
    # Fallback to 'last_price' or 'mark_price' if present
    for k in ("last_price", "mark_price"):
        v = last.get(k) if isinstance(last, dict) else None
        if v is not None:
            try:
                return float(v)
            except:
                continue
    raise RuntimeError(f"Premium not found for {symbol}")

def _normalize_result(obj):
    return obj.get("result") if isinstance(obj, dict) and "result" in obj else obj

def run_short_strangle(client, underlying_symbol: str) -> str:
    """
    - Determine CMP for BTC via /v2/tickers/{symbol} (likely BTCUSDT).
    - Find BTC options with today's IST expiry ddmmyy.
    - Choose CE near +1% and PE near -1% strikes.
    - Place sell 1 lot per leg (market order with size=1 lot units matching product contract size).
    - Place stop orders with stop price = entry premium * 2 (loss at +1x premium over entry; as short, stop-buy triggers).
      Note: Using stop order body consistent with /v2/orders for stop entries.
    - Return a formatted Telegram message.
    """
    # 1) Spot CMP
    spot_tick = client.get_ticker_symbol(underlying_symbol)
    spot = _normalize_result(spot_tick)
    # try common fields
    cmp_price = None
    for k in ("close", "last_price", "mark_price"):
        v = spot.get(k) if isinstance(spot, dict) else None
        if v is not None:
            try:
                cmp_price = float(v); break
            except:
                pass
    if cmp_price is None:
        raise RuntimeError(f"CMP not found in ticker for {underlying_symbol}")

    # 2) Same-day expiry symbol tag
    expiry = _today_ddmmyy_ist()

    # 3) Products
    prods_raw = client.get_products()
    prods = _normalize_result(prods_raw)
    # Filter BTC options for expiry
    sym_to_prod = _extract_btc_option_products(prods, expiry)
    if not sym_to_prod:
        raise RuntimeError(f"No BTC options found for expiry {expiry}")

    # 4) Collect strikes and choose ±1%
    strikes = []
    for sym in sym_to_prod.keys():
        s = _parse_strike_from_symbol(sym)
        if s:
            strikes.append(s)
    strikes = sorted(list(set(strikes)))
    if not strikes:
        raise RuntimeError("No strikes parsed from option symbols")

    ce_strike, pe_strike = _pick_strikes(cmp_price, strikes)

    ce_symbol = f"C-BTC-{int(ce_strike)}-{expiry}"
    pe_symbol = f"P-BTC-{int(pe_strike)}-{expiry}"

    if ce_symbol not in sym_to_prod or pe_symbol not in sym_to_prod:
        raise RuntimeError(f"Selected symbols not available: {ce_symbol}, {pe_symbol}")

    ce_prod = sym_to_prod[ce_symbol]
    pe_prod = sym_to_prod[pe_symbol]

    ce_pid = ce_prod.get("id")
    pe_pid = pe_prod.get("id")
    if not ce_pid or not pe_pid:
        raise RuntimeError("Product ids missing for selected options")

    # 5) Fetch premiums for stop-loss definition
    ce_premium = _current_option_premium(client, ce_symbol)
    pe_premium = _current_option_premium(client, pe_symbol)

    # Entry: sell 1 lot each. Using market order where supported.
    # For shorts: side='sell' and size in contracts. Assuming 1 contract unit.
    orders = []

    ce_entry = client.place_order({
        "order_type": "market_order",
        "size": 1,
        "side": "sell",
        "product_id": ce_pid
    })
    orders.append(("CE entry", ce_entry))

    pe_entry = client.place_order({
        "order_type": "market_order",
        "size": 1,
        "side": "sell",
        "product_id": pe_pid
    })
    orders.append(("PE entry", pe_entry))

    # Stop-loss: stop-buy at price = entry premium * 2 (1x premium loss over credit)
    ce_stop_price = round(ce_premium * 2, 2)
    pe_stop_price = round(pe_premium * 2, 2)

    ce_stop = client.place_order({
        "order_type": "stop_order",
        "size": 1,
        "side": "buy",
        "product_id": ce_pid,
        "stop_price": str(ce_stop_price)
    })
    orders.append(("CE stop", ce_stop))

    pe_stop = client.place_order({
        "order_type": "stop_order",
        "size": 1,
        "side": "buy",
        "product_id": pe_pid,
        "stop_price": str(pe_stop_price)
    })
    orders.append(("PE stop", pe_stop))

    # Build report
    lines = [
        "<b>BTC Short Strangle Executed</b>",
        f"Expiry: {expiry}",
        f"Spot (CMP): {cmp_price}",
        f"CE: {ce_symbol} | premium≈{ce_premium} | SL={ce_stop_price}",
        f"PE: {pe_symbol} | premium≈{pe_premium} | SL={pe_stop_price}",
    ]
    # Append order ids/status
    for title, obj in orders:
        res = obj.get("result") if isinstance(obj, dict) and "result" in obj else obj
        oid = res.get("id") if isinstance(res, dict) else None
        state = res.get("state") if isinstance(res, dict) else None
        lines.append(f"{title}: id={oid}, state={state}")

    return "\n".join(lines)
