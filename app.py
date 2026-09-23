import streamlit as st
import requests
import time
import datetime as dt
import uuid
import base64
import json
import csv
import re
from decimal import Decimal, InvalidOperation
from urllib.parse import urlparse
from pathlib import Path

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding


# ============================================================
# PAGE CONFIGURATION
# ============================================================

st.set_page_config(
    page_title="Kalshi Scalper Pro",
    page_icon="⚡",
    layout="wide",
)

st.title("⚡ Kalshi Scalper Pro")
st.caption("Production-hardened paper/live Kalshi trading dashboard with multi-strike liquidity scanning")


# ============================================================
# CONSTANTS
# ============================================================

PROD_BASE_URL = "https://external-api.kalshi.com/trade-api/v2"
DEMO_BASE_URL = "https://external-api.demo.kalshi.co/trade-api/v2"

LOG_FILE = Path("kalshi_orders.csv")
ZERO = Decimal("0")

MONTH_MAP = {
    "JAN": 1, "FEB": 2, "MAR": 3, "APR": 4, "MAY": 5, "JUN": 6,
    "JUL": 7, "AUG": 8, "SEP": 9, "OCT": 10, "NOV": 11, "DEC": 12,
}


# ============================================================
# SESSION STATE INITIALIZATION
# ============================================================

DEFAULTS = {
    "running": False,
    "emergency_stop": False,
    "logs": [],
    "paper_trades": [],
    "last_trade_time": None,
    "last_price": None,
    "active_ticker": None,
    "last_order_id": None,
    "last_client_order_id": None,
    "last_fill_count": ZERO,
    "bot_positions": {},
}

for key, value in DEFAULTS.items():
    if key not in st.session_state:
        st.session_state[key] = value


# ============================================================
# HELPERS
# ============================================================

def D(value, default="0"):
    try:
        if value is None:
            return Decimal(default)
        return Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return Decimal(default)


def now_utc():
    return dt.datetime.now(dt.timezone.utc)


def log(message):
    stamp = now_utc().strftime("%Y-%m-%d %H:%M:%S UTC")
    line = f"[{stamp}] {message}"
    st.session_state.logs.append(line)
    st.session_state.logs = st.session_state.logs[-100:]


def append_order_log(row):
    fields = [
        "timestamp", "mode", "ticker", "outcome", "action", "book_side",
        "contracts", "price", "estimated_cost", "order_id", "client_order_id",
        "fill_count", "remaining_count", "status", "error",
    ]
    exists = LOG_FILE.exists()
    try:
        with LOG_FILE.open("a", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fields)
            if not exists:
                writer.writeheader()
            writer.writerow({k: row.get(k, "") for k in fields})
    except Exception as e:
        log(f"CSV Logging error: {e}")


def load_private_key(text):
    return serialization.load_pem_private_key(
        text.encode("utf-8"),
        password=None,
    )


# ============================================================
# AUTHENTICATION
# ============================================================

def create_signature(private_key, timestamp, method, path):
    message = f"{timestamp}{method.upper()}{path}".encode("utf-8")
    signature = private_key.sign(
        message,
        padding.PSS(
            mgf=padding.MGF1(hashes.SHA256()),
            salt_length=hashes.SHA256().digest_size,
        ),
        hashes.SHA256(),
    )
    return base64.b64encode(signature).decode("utf-8")


def create_headers(key_id, private_key, method, url):
    timestamp = str(int(time.time() * 1000))
    path = urlparse(url).path
    signature = create_signature(private_key, timestamp, method, path)
    return {
        "KALSHI-ACCESS-KEY": key_id,
        "KALSHI-ACCESS-SIGNATURE": signature,
        "KALSHI-ACCESS-TIMESTAMP": timestamp,
        "Content-Type": "application/json",
    }


# ============================================================
# KALSHI CLIENT
# ============================================================

class KalshiClient:
    def __init__(self, key_id, private_key_text, demo=False):
        self.key_id = key_id.strip()
        self.private_key = load_private_key(private_key_text)
        self.base_url = DEMO_BASE_URL if demo else PROD_BASE_URL

    def request(self, method, endpoint, params=None, body=None):
        url = self.base_url + endpoint
        headers = create_headers(self.key_id, self.private_key, method, url)

        response = requests.request(
            method=method,
            url=url,
            headers=headers,
            params=params,
            json=body,
            timeout=15,
        )

        if not response.ok:
            try:
                detail = response.json()
            except Exception:
                detail = response.text
            raise RuntimeError(f"Kalshi API {response.status_code}: {detail}")

        if not response.content:
            return {}

        return response.json()

    def get_markets(self, series_ticker=None, limit=100):
        params = {"status": "open", "limit": limit}
        if series_ticker:
            params["series_ticker"] = series_ticker
        return self.request("GET", "/markets", params=params)

    def get_market(self, ticker):
        return self.request("GET", f"/markets/{ticker}")

    def get_orderbook(self, ticker):
        return self.request("GET", f"/markets/{ticker}/orderbook")

    def get_balance(self):
        return self.request("GET", "/portfolio/balance")

    def get_positions(self, ticker=None):
        params = {"limit": 1000}
        if ticker:
            params["ticker"] = ticker
        return self.request("GET", "/portfolio/positions", params=params)

    def get_fills(self, ticker=None, min_ts=None):
        params = {"limit": 1000}
        if ticker:
            params["ticker"] = ticker
        if min_ts is not None:
            params["min_ts"] = int(min_ts)
        return self.request("GET", "/portfolio/fills", params=params)

    def get_orders(self, ticker=None, status=None):
        params = {"limit": 100}
        if ticker:
            params["ticker"] = ticker
        if status:
            params["status"] = status
        return self.request("GET", "/portfolio/orders", params=params)

    def get_order(self, order_id):
        return self.request("GET", f"/portfolio/orders/{order_id}")

    def create_v2_order(self, ticker, client_order_id, book_side, contracts, price_dollars, reduce_only=False):
        body = {
            "ticker": ticker,
            "client_order_id": client_order_id,
            "side": book_side,
            "count": int(contracts),
            "price": int(D(price_dollars) * 100),
            "time_in_force": "immediate_or_cancel",
            "self_trade_prevention_type": "taker_at_cross",
            "reduce_only": bool(reduce_only),
        }
        return self.request("POST", "/portfolio/events/orders", body=body)

    def cancel_order(self, order_id):
        return self.request("DELETE", f"/portfolio/events/orders/{order_id}")


# ============================================================
# MARKET HELPERS & MULTI-STRIKE LIQUIDITY SCANNER
# ============================================================

def parse_time(value):
    if not value:
        return None
    if isinstance(value, (int, float)):
        return dt.datetime.fromtimestamp(float(value), tz=dt.timezone.utc)
    text = str(value)
    try:
        return dt.datetime.fromisoformat(text.replace("Z", "+00:00")).astimezone(dt.timezone.utc)
    except Exception:
        return None


def market_minutes_remaining(market):
    close = parse_time(market.get("close_time")) or parse_time(market.get("expiration_time"))
    if close is None:
        return None
    return (close - now_utc()).total_seconds() / 60


def get_live_ask_price(client, ticker, outcome_to_trade):
    try:
        res = client.get_orderbook(ticker)
        ob = res.get("orderbook", res)
        
        yes_levels = ob.get("yes", []) or ob.get("yes_bids", [])
        no_levels = ob.get("no", []) or ob.get("no_bids", [])
        
        def extract_best(levels):
            if not levels: return 0
            lvl = levels[0]
            if isinstance(lvl, dict): return int(lvl.get("price", 0))
            if isinstance(lvl, (list, tuple)): return int(lvl[0])
            return 0

        yes_bid = extract_best(yes_levels)
        no_bid = extract_best(no_levels)
        
        yes_ask = (100 - no_bid) if no_bid > 0 else 0
        no_ask = (100 - yes_bid) if yes_bid > 0 else 0

        return yes_ask if outcome_to_trade == "YES" else no_ask
    except Exception:
        return 0


def find_active_market_with_liquidity(client, series_ticker, min_expiry_mins=0, outcome_to_trade="YES"):
    markets = []
    try:
        res = client.get_markets(series_ticker=series_ticker)
        markets = res.get("markets", [])
    except Exception:
        pass

    if not markets:
        try:
            res = client.get_markets(limit=200)
            all_markets = res.get("markets", [])
            markets = [m for m in all_markets if series_ticker.upper() in m.get("ticker", "").upper()]
        except Exception:
            pass

    open_markets = [m for m in markets if m.get("status") in (None, "open", "active")]
    valid_markets = [m for m in open_markets if market_minutes_remaining(m) is not None and market_minutes_remaining(m) > min_expiry_mins]

    if not valid_markets:
        return None, 0

    windows = {}
    for m in valid_markets:
        mins = market_minutes_remaining(m)
        window_key = round(mins)
        if window_key not in windows:
            windows[window_key] = []
        windows[window_key].append(m)

    sorted_windows = sorted(windows.keys())
    
    for w_key in sorted_windows:
        current_window = windows[w_key]
        current_window.sort(key=lambda x: x.get("ticker", ""))
        
        for m in current_window:
            t = m.get("ticker")
            ask = get_live_ask_price(client, t, outcome_to_trade)
            if 0 < ask < 100:
                return m, ask

    if sorted_windows:
        fallback_window = windows[sorted_windows[0]]
        if fallback_window:
            fallback_market = fallback_window[len(fallback_window) // 2]
            t = fallback_market.get("ticker")
            ask = get_live_ask_price(client, t, outcome_to_trade)
            return fallback_market, ask

    return None, 0


def outcome_to_book_side(outcome, action):
    if outcome == "YES":
        return "bid" if action == "BUY" else "ask"
    return "ask" if action == "BUY" else "bid"


def outcome_price_to_book_price(outcome, action, outcome_cents):
    p = D(outcome_cents) / D(100)
    if outcome == "YES":
        return p
    return D(1) - p


def extract_position(positions_response, ticker):
    rows = positions_response.get("market_positions", [])
    for row in rows:
        row_ticker = row.get("ticker") or row.get("market_ticker")
        if row_ticker != ticker:
            continue
        raw = row.get("position_fp") if row.get("position_fp") is not None else row.get("position")
        position = D(raw)
        if position > 0:
            return {"ticker": ticker, "outcome": "YES", "contracts": position}
        if position < 0:
            return {"ticker": ticker, "outcome": "NO", "contracts": abs(position)}
        return {"ticker": ticker, "outcome": None, "contracts": ZERO}
    return {"ticker": ticker, "outcome": None, "contracts": ZERO}


def fill_outcome(fill):
    outcome = fill.get("outcome_side") or fill.get("side") or fill.get("outcome")
    if outcome:
        return str(outcome).upper()
    book_side = str(fill.get("book_side", "")).lower()
    if book_side == "bid": return "YES"
    if book_side == "ask": return "NO"
    return None


def fill_action(fill):
    action = fill.get("action")
    return str(action).upper() if action else None


def fill_price(fill, outcome):
    raw = fill.get("yes_price_dollars") or fill.get("yes_price") if outcome == "YES" else fill.get("no_price_dollars") or fill.get("no_price")
    value = D(raw)
    if value > 1:
        value = value / D(100)
    return value


def fill_count(fill):
    raw = fill.get("count_fp") or fill.get("count") or fill.get("filled_count_fp") or fill.get("filled_count")
    return D(raw)


def reconstruct_average_entry(fills, ticker, outcome):
    relevant = []
    for fill in fills:
        if fill.get("ticker") and fill.get("ticker") != ticker:
            continue
        f_outcome = fill_outcome(fill)
        if f_outcome != outcome:
            continue
        action = fill_action(fill)
        if action not in ("BUY", "SELL"):
            continue
        qty = fill_count(fill)
        price = fill_price(fill, outcome)
        if qty <= 0 or price <= 0:
            continue
        created = fill.get("created_time") or fill.get("ts") or ""
        relevant.append((str(created), action, qty, price))

    relevant.sort(key=lambda x: x[0])
    lots = []
    for _, action, qty, price in relevant:
        if action == "BUY":
            lots.append([qty, price])
            continue
        remaining = qty
        while remaining > 0 and lots:
            lot_qty, lot_price = lots[0]
            take = min(remaining, lot_qty)
            lot_qty -= take
            remaining -= take
            if lot_qty <= 0:
                lots.pop(0)
            else:
                lots[0][0] = lot_qty

    total_qty = sum((lot[0] for lot in lots), ZERO)
    if total_qty <= 0:
        return None
    total_cost = sum((lot_qty * lot_price for lot_qty, lot_price in lots), ZERO)
    return total_cost / total_qty


def utc_day_start_timestamp():
    today = now_utc().replace(hour=0, minute=0, second=0, microsecond=0)
    return int(today.timestamp())


def calculate_daily_spend(client):
    fills_response = client.get_fills(min_ts=utc_day_start_timestamp())
    fills = fills_response.get("fills", [])
    total = ZERO
    for fill in fills:
        if str(fill.get("action", "")).lower() != "buy":
            continue
        outcome = fill_outcome(fill)
        if outcome not in ("YES", "NO"):
            continue
        qty = fill_count(fill)
        price = fill_price(fill, outcome)
        fee = D(fill.get("fee_cost", 0))
        total += (qty * price) + fee
    return total


def make_client_order_id():
    return f"ksbot-{uuid.uuid4().hex}"


def calculate_contracts(max_dollars, price_cents, remaining_daily):
    if price_cents <= 0:
        return 0
    price = D(price_cents) / D(100)
    allowed = min(D(max_dollars), D(remaining_daily))
    if allowed <= 0:
        return 0
    return int(allowed / price)


def submit_trade(client, mode, ticker, outcome, action, contracts, outcome_cents, reduce_only=False):
    book_side = outcome_to_book_side(outcome, action)
    book_price = outcome_price_to_book_price(outcome, action, outcome_cents)
    estimated_cost = D(contracts) * (D(outcome_cents) / D(100))
    client_order_id = make_client_order_id()
    
    st.session_state.last_client_order_id = client_order_id

    if mode == "PAPER TRADING":
        log(f"📝 PAPER {action} {contracts} {outcome} {ticker} @ {outcome_cents}¢")
        st.session_state.paper_trades.append({
            "Time": now_utc().strftime("%H:%M:%S"),
            "Ticker": ticker,
            "Outcome": outcome,
            "Action": action,
            "Contracts": contracts,
            "Price": f"{outcome_cents}¢",
            "Cost": f"${estimated_cost:.2f}",
        })
        append_order_log({
            "timestamp": now_utc().isoformat(), "mode": mode, "ticker": ticker,
            "outcome": outcome, "action": action, "book_side": book_side,
            "contracts": contracts, "price": f"{D(outcome_cents)/D(100):.4f}",
            "estimated_cost": f"{estimated_cost:.4f}", "order_id": "PAPER",
            "client_order_id": client_order_id, "fill_count": contracts,
            "remaining_count": 0, "status": "paper_fill", "error": "",
        })
        return {
            "order_id": "PAPER", "client_order_id": client_order_id,
            "fill_count": D(contracts), "remaining_count": ZERO, "status": "paper_fill",
        }

    try:
        response = client.create_v2_order(
            ticker=ticker, client_order_id=client_order_id, book_side=book_side,
            contracts=contracts, price_dollars=book_price, reduce_only=reduce_only,
        )
        order = response.get("order", response)
        order_id = order.get("order_id")
        fill_count_value = D(order.get("fill_count") or order.get("fill_count_fp"))
        remaining = D(order.get("remaining_count") or order.get("remaining_count_fp"))
        status = order.get("status", "submitted")

        st.session_state.last_order_id = order_id
        st.session_state.last_fill_count = fill_count_value

        append_order_log({
            "timestamp": now_utc().isoformat(), "mode": mode, "ticker": ticker,
            "outcome": outcome, "action": action, "book_side": book_side,
            "contracts": contracts, "price": f"{book_price:.4f}",
            "estimated_cost": f"{estimated_cost:.4f}", "order_id": order_id or "",
            "client_order_id": client_order_id, "fill_count": f"{fill_count_value:.2f}",
            "remaining_count": f"{remaining:.2f}", "status": status, "error": "",
        })
        log(f"🟢 LIVE ORDER {action}: {contracts} {outcome} {ticker} @ {outcome_cents}¢ | filled={fill_count_value}")
        return {
            "order_id": order_id, "client_order_id": client_order_id,
            "fill_count": fill_count_value, "remaining_count": remaining, "status": status,
        }
    except Exception as error:
        append_order_log({
            "timestamp": now_utc().isoformat(), "mode": mode, "ticker": ticker,
            "outcome": outcome, "action": action, "book_side": book_side,
            "contracts": contracts, "price": f"{book_price:.4f}",
            "estimated_cost": f"{estimated_cost:.4f}", "order_id": "",
            "client_order_id": client_order_id, "fill_count": "",
            "remaining_count": "", "status": "ERROR", "error": str(error),
        })
        log(f"❌ LIVE ORDER FAILED: {error}")
        raise


def confirm_fill(client, order_id, timeout_seconds=4):
    if not order_id:
        return ZERO, ZERO, "unknown"
    deadline = time.time() + timeout_seconds
    latest = None
    while time.time() < deadline:
        try:
            response = client.get_order(order_id)
            order = response.get("order", response)
            latest = order
            filled = D(order.get("fill_count") or order.get("fill_count_fp"))
            remaining = D(order.get("remaining_count") or order.get("remaining_count_fp"))
            status = order.get("status", "unknown")
            if filled > 0 or status in ("canceled", "executed", "filled"):
                return filled, remaining, status
        except Exception:
            pass
        time.sleep(0.5)

    if latest:
        return (
            D(latest.get("fill_count") or latest.get("fill_count_fp")),
            D(latest.get("remaining_count") or latest.get("remaining_count_fp")),
            latest.get("status", "unknown"),
        )
    return ZERO, ZERO, "unknown"


def manage_position(client, mode, position, fills, take_profit_pct, stop_loss_pct):
    if position["contracts"] <= 0:
        return False

    ticker = position["ticker"]
    outcome = position["outcome"]
    contracts = int(position["contracts"])
    if outcome not in ("YES", "NO"):
        return False

    try:
        res = client.get_orderbook(ticker)
        ob = res.get("orderbook", res)
        yes_levels = ob.get("yes", []) or ob.get("yes_bids", [])
        no_levels = ob.get("no", []) or ob.get("no_bids", [])
        
        def extract_best(levels):
            if not levels: return 0
            lvl = levels[0]
            if isinstance(lvl, dict): return int(lvl.get("price", 0))
            if isinstance(lvl, (list, tuple)): return int(lvl[0])
            return 0
            
        yes_bid = extract_best(yes_levels)
        no_bid = extract_best(no_levels)
        current_bid = yes_bid if outcome == "YES" else no_bid
        
        if current_bid <= 0:
            return False
    except Exception as error:
        log(f"TP/SL price fetch error: {error}")
        return False

    entry = reconstruct_average_entry(fills, ticker, outcome)
    if entry is None:
        return False

    current = D(current_bid) / D(100)
    tp_price = entry * (D(1) + D(take_profit_pct) / D(100))
    sl_price = entry * (D(1) - D(stop_loss_pct) / D(100))

    reason = None
    if take_profit_pct > 0 and current >= tp_price:
        reason = "TAKE PROFIT"
    if stop_loss_pct > 0 and current <= sl_price:
        reason = "STOP LOSS"

    if reason is None:
        return False

    log(f"🚨 {reason}: {ticker} {outcome} entry=${entry:.4f}, bid=${current:.4f}")
    result = submit_trade(
        client=client, mode=mode, ticker=ticker, outcome=outcome,
        action="SELL", contracts=contracts, outcome_cents=current_bid, reduce_only=True,
    )

    if mode == "LIVE TRADING" and result.get("order_id"):
        filled, remaining, status = confirm_fill(client, result["order_id"])
        log(f"{reason} exit confirmation: filled={filled}, remaining={remaining}, status={status}")

    return True


# ============================================================
# SIDEBAR CONTROLS
# ============================================================

with st.sidebar:
    st.header("🔑 Account")
    trading_mode = st.radio("Trading mode", ["PAPER TRADING", "LIVE TRADING"], index=0)
    demo_mode = st.checkbox("Use Kalshi Demo API", value=True)

    key_id_default = st.secrets.get("KALSHI_API_KEY_ID", "") if "KALSHI_API_KEY_ID" in st.secrets else ""
    private_key_default = st.secrets.get("KALSHI_PRIVATE_KEY", "") if "KALSHI_PRIVATE_KEY" in st.secrets else ""

    key_id = st.text_input("Kalshi API Key ID", value=key_id_default, type="password")
    private_key_text = st.text_area("Private Key PEM", value=private_key_default, height=180)

    st.divider()
    st.header("📊 Market")
    series_ticker = st.text_input("Market Series", value="KXBTC15M")
    display_outcome = st.selectbox("Entry outcome", ["UP", "DOWN"])
    outcome_to_trade = "YES" if display_outcome == "UP" else "NO"

    st.divider()
    st.header("💰 Risk Settings")
    max_dollars_trade = st.number_input("Maximum dollars per trade", min_value=0.01, max_value=10000.00, value=10.00, step=1.00)
    daily_cap = st.number_input("Daily spending cap", min_value=0.01, max_value=100000.00, value=100.00, step=5.00)
    max_entry = st.slider("Maximum entry price", min_value=1, max_value=99, value=75, format="%d¢")
    cooldown = st.slider("Cooldown between entries", min_value=10, max_value=1800, value=60, step=10, format="%d seconds")
    min_minutes_to_expiry = st.number_input("Do not enter if expiration is closer than", min_value=0.0, max_value=120.0, value=2.0, step=0.5)
    take_profit_pct = st.number_input("Take profit %", min_value=0.0, max_value=500.0, value=20.0, step=1.0)
    stop_loss_pct = st.number_input("Stop loss %", min_value=0.0, max_value=99.0, value=25.0, step=1.0)

    st.divider()
    st.header("🕐 Trading Hours")
    c1, c2 = st.columns(2)
    with c1:
        start_time = st.time_input("Start", dt.time(0, 0))
    with c2:
        end_time = st.time_input("End", dt.time(23, 59))

    st.divider()
    manage_existing = st.checkbox("Manage existing position for TP/SL", value=False)


# ============================================================
# SAFETY GATE
# ============================================================

live_confirmed = False
if trading_mode == "LIVE TRADING":
    st.warning("⚠️ LIVE TRADING CAN PLACE REAL MONEY ORDERS.")
    confirmation = st.text_input("Type LIVE I UNDERSTAND to enable live orders", type="password")
    live_confirmed = (confirmation.strip() == "LIVE I UNDERSTAND")
    if not live_confirmed:
        st.error("Live orders are locked until confirmation phrase is entered.")


# ============================================================
# START / STOP ACTIONS
# ============================================================

c1, c2, c3 = st.columns(3)
with c1:
    if st.button("▶ START AUTOTRADING", type="primary", use_container_width=True):
        if not key_id:
            st.error("Enter your Kalshi API Key ID.")
        elif not private_key_text:
            st.error("Provide the private key.")
        elif trading_mode == "LIVE TRADING" and not live_confirmed:
            st.error("Live confirmation required.")
        else:
            st.session_state.running = True
            st.session_state.emergency_stop = False
            log(f"Bot started in {trading_mode}. Demo API={demo_mode}.")
            st.rerun()

with c2:
    if st.button("⏹ STOP", use_container_width=True):
        st.session_state.running = False
        log("Bot stopped.")
        st.rerun()

with c3:
    if st.button("🛑 EMERGENCY STOP", type="secondary", use_container_width=True):
        st.session_state.running = False
        st.session_state.emergency_stop = True
        log("🛑 EMERGENCY STOP ACTIVATED.")
        st.rerun()

if st.session_state.emergency_stop:
    st.error("🛑 EMERGENCY STOP IS ACTIVE. Press START only after reviewing settings.")


# ============================================================
# BOT ENGINE LOOP (Non-blocking via Streamlit Fragment)
# ============================================================

def run_bot_cycle(client, daily_spent):
    if st.session_state.emergency_stop:
        st.session_state.running = False
        st.rerun()
        return

    if trading_mode == "LIVE TRADING" and demo_mode:
        st.error("LIVE TRADING selected while Demo API is enabled.")
        st.session_state.running = False
        st.rerun()
        return

    current_time = dt.datetime.now().time()
    if not (start_time <= current_time <= end_time):
        return

    if daily_spent >= D(daily_cap):
        st.error("Daily spending cap reached.")
        log(f"Daily cap reached: ${daily_spent:.2f}")
        st.session_state.running = False
        st.rerun()
        return

    try:
        market, entry_price = find_active_market_with_liquidity(
            client, series_ticker, min_expiry_mins=float(min_minutes_to_expiry), outcome_to_trade=outcome_to_trade
        )

        if not market or entry_price <= 0 or entry_price >= 100:
            return

        ticker = market.get("ticker")
        st.session_state.active_ticker = ticker
        st.session_state.last_price = entry_price
    except Exception as error:
        log(f"Market discovery error: {error}")
        return

    try:
        position_response = client.get_positions(ticker=ticker)
        position = extract_position(position_response, ticker)
        fills_response = client.get_fills(ticker=ticker)
        fills = fills_response.get("fills", [])
    except Exception as error:
        log(f"Position/fill error: {error}")
        return

    if position["contracts"] > 0 and (manage_existing or ticker in st.session_state.bot_positions):
        try:
            exited = manage_position(client, trading_mode, position, fills, take_profit_pct, stop_loss_pct)
            if exited:
                st.session_state.last_trade_time = now_utc()
                return
        except Exception as error:
            log(f"TP/SL error: {error}")

    already_in_target_position = (position["contracts"] > 0 and position["outcome"] == outcome_to_trade)
    if already_in_target_position:
        return

    signal = entry_price <= max_entry
    if signal and st.session_state.last_trade_time:
        elapsed = (now_utc() - st.session_state.last_trade_time).total_seconds()
        if elapsed < cooldown:
            signal = False

    if signal:
        remaining_daily = D(daily_cap) - daily_spent
        contracts = calculate_contracts(D(max_dollars_trade), entry_price, remaining_daily)

        if contracts <= 0:
            log("Entry blocked: daily cap or trade limit does not allow even one contract.")
        else:
            estimated_cost = D(contracts) * D(entry_price) / D(100)
            log(f"ENTRY SIGNAL: BUY {contracts} {outcome_to_trade} {ticker} @ {entry_price}¢ (~${estimated_cost:.2f})")

            try:
                result = submit_trade(
                    client=client, mode=trading_mode, ticker=ticker, outcome=outcome_to_trade,
                    action="BUY", contracts=contracts, outcome_cents=entry_price, reduce_only=False,
                )
                st.session_state.last_trade_time = now_utc()

                if trading_mode == "LIVE TRADING":
                    filled, remaining, status = confirm_fill(client, result.get("order_id"))
                    st.session_state.last_fill_count = filled
                    log(f"FILL CONFIRMATION: filled={filled}, remaining={remaining}, status={status}")
                    if filled > 0:
                        st.session_state.bot_positions[ticker] = {"outcome": outcome_to_trade, "contracts": str(filled)}
                else:
                    st.session_state.bot_positions[ticker] = {"outcome": outcome_to_trade, "contracts": str(contracts)}
            except Exception as e:
                log(f"Order submission error exception caught: {e}")


@st.fragment(run_every=3)
def render_dashboard_and_tick():
    st.subheader("Dashboard")
    
    client = None
    daily_spent = ZERO
    
    if key_id and private_key_text:
        try:
            client = KalshiClient(key_id=key_id, private_key_text=private_key_text, demo=demo_mode)
        except Exception as error:
            if st.session_state.running:
                st.error(f"Private key error: {error}")
                st.session_state.running = False
                st.rerun()

    if client and trading_mode == "LIVE TRADING":
        try:
            daily_spent = calculate_daily_spend(client)
        except Exception:
            pass
    elif trading_mode == "PAPER TRADING":
        daily_spent = sum((D(str(row["Cost"]).replace("$", "")) for row in st.session_state.paper_trades), ZERO)

    c_dash1, c_dash2, c_dash3, c_dash4, c_dash5 = st.columns(5)
    with c_dash1:
        st.metric("Mode", trading_mode)
    with c_dash2:
        st.metric("Daily Spent", f"${daily_spent:.2f}")
    with c_dash3:
        st.metric("Market", st.session_state.active_ticker or "--")
    with c_dash4:
        price_display = f"{st.session_state.last_price}¢" if st.session_state.last_price else "--"
        st.metric("Entry Price", price_display)
    with c_dash5:
        st.metric("Last Fill", f"{st.session_state.last_fill_count:.2f}")

    if st.session_state.running and client:
        run_bot_cycle(client, daily_spent)
    elif not st.session_state.running:
        st.info("Bot is stopped. Choose your settings and press 'START AUTOTRADING'.")

    st.subheader("📜 Bot Log")
    if st.session_state.logs:
        st.code("\n".join(st.session_state.logs[-30:]))
    else:
        st.info("Waiting for bot activity...")

    if st.session_state.paper_trades:
        st.subheader("📝 Paper Trades")
        st.dataframe(st.session_state.paper_trades, use_container_width=True, hide_index=True)

    if not st.session_state.running and LOG_FILE.exists():
        st.download_button(
            "⬇️ Download order log",
            data=LOG_FILE.read_bytes(),
            file_name="kalshi_orders.csv",
            mime="text/csv",
        )

render_dashboard_and_tick()
