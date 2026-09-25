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
    page_title="Vortex Scalper Pro",
    page_icon="🌪️",
    layout="wide",
)

st.title("🌪️ Vortex Scalper Pro - ROI Exits & Bulletproof Stop-Loss")
st.caption("High-frequency Kalshi trading dashboard with exact return percentage tracking and error-logged stop-losses")


# ============================================================
# CONSTANTS
# ============================================================

PROD_BASE_URL = "https://external-api.kalshi.com/trade-api/v2"
DEMO_BASE_URL = "https://external-api.demo.kalshi.co/trade-api/v2"

LOG_FILE = Path("kalshi_orders.csv")
ZERO = Decimal("0")


# ============================================================
# SESSION STATE INITIALIZATION
# ============================================================

DEFAULTS = {
    "running": False,
    "emergency_stop": False,
    "logs": [],
    "trade_logs": [],
    "paper_trades": [],
    "paper_positions": {},      
    "paper_realized_pnl": ZERO, 
    "starting_live_balance": None,
    "last_trade_time": None,
    "active_ticker": None,
    "up_ask_display": "--",
    "down_ask_display": "--",
    "last_order_id": None,
    "last_client_order_id": None,
    "last_fill_count": ZERO,
    "bot_positions": {},
    "blacklisted_tickers": set(),
    "last_status_log": None,
    "price_history": [],
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
    stamp = now_utc().strftime("%H:%M:%S")
    line = f"[{stamp}] {message}"
    st.session_state.logs.append(line)
    st.session_state.logs = st.session_state.logs[-30:]

def log_once(msg_key, message):
    if st.session_state.get("last_status_log") != msg_key:
        log(message)
        st.session_state["last_status_log"] = msg_key

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
    except Exception:
        pass


def load_private_key(text):
    return serialization.load_pem_private_key(
        text.encode("utf-8"),
        password=None,
    )


# ============================================================
# AUTHENTICATION & CLIENT
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
            timeout=10,
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

    def get_markets(self, series_ticker=None, limit=50):
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

    def intra_exchange_transfer(self, amount_cents, source_shard=0, dest_shard=2):
        body = {
            "amount": int(amount_cents),
            "source_exchange_shard": int(source_shard),
            "destination_exchange_shard": int(dest_shard),
            "source_subaccount": 0,
            "destination_subaccount": 0
        }
        return self.request("POST", "/portfolio/intra_exchange_instance_transfer", body=body)

    def get_positions(self, ticker=None):
        params = {"limit": 1000}
        if ticker:
            params["ticker"] = ticker
        return self.request("GET", "/portfolio/positions", params=params)

    def get_fills(self, ticker=None, min_ts=None):
        params = {"limit": 500}
        if ticker:
            params["ticker"] = ticker
        if min_ts is not None:
            params["min_ts"] = int(min_ts)
        return self.request("GET", "/portfolio/fills", params=params)

    def get_order(self, order_id):
        return self.request("GET", f"/portfolio/orders/{order_id}")

    def create_v2_order(self, ticker, client_order_id, book_side, contracts, price_dollars, reduce_only=False):
        formatted_price = f"{float(price_dollars):.4f}"
        body = {
            "ticker": ticker,
            "client_order_id": client_order_id,
            "side": book_side,
            "count": str(int(contracts)),
            "price": formatted_price,
            "time_in_force": "immediate_or_cancel",
            "self_trade_prevention_type": "taker_at_cross",
            "reduce_only": bool(reduce_only),
        }
        return self.request("POST", "/portfolio/events/orders", body=body)


# ============================================================
# MARKET & MEMORY HELPERS
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


def get_both_prices(client, ticker):
    try:
        res = client.get_orderbook(ticker)
        ob = res.get("orderbook_fp") or res.get("orderbook") or res
        yes_levels = ob.get("yes_dollars") or ob.get("yes") or []
        no_levels = ob.get("no_dollars") or ob.get("no") or []
        
        def extract_best_bid(levels):
            if not levels: return 0
            lvl = levels[-1]
            if isinstance(lvl, (list, tuple)): return int(float(lvl[0]) * 100)
            if isinstance(lvl, dict): return int(float(lvl.get("price", 0)) * 100)
            return 0

        yes_bid = extract_best_bid(yes_levels)
        no_bid = extract_best_bid(no_levels)
        
        yes_ask = (100 - no_bid) if no_bid > 0 else 0
        no_ask = (100 - yes_bid) if yes_bid > 0 else 0

        return {"YES": {"ask": yes_ask, "bid": yes_bid}, "NO": {"ask": no_ask, "bid": no_bid}}
    except Exception:
        return None


def evaluate_historical_memory(price_history, intended_outcome):
    if len(price_history) < 5:
        return True
    recent = price_history[-10:]
    momentum = recent[-1] - recent[0]
    if intended_outcome == "YES":
        return momentum >= -3
    else:
        return momentum <= 3


def find_active_market_with_liquidity(client, series_ticker, min_expiry_mins=0, outcome_mode="YES", min_up=15, max_up=65, min_down=15, max_down=65, use_memory_filter=True):
    try:
        res = client.get_markets(series_ticker=series_ticker, limit=30)
        markets = res.get("markets", [])
    except Exception:
        return None, None, 0

    open_markets = [m for m in markets if str(m.get("status", "")).lower() not in ("closed", "settled", "finalized")]
    valid_markets = [m for m in open_markets if market_minutes_remaining(m) is not None and market_minutes_remaining(m) > min_expiry_mins]

    if not valid_markets:
        valid_markets = [m for m in open_markets if market_minutes_remaining(m) is not None and market_minutes_remaining(m) > 0]

    if not valid_markets:
        return None, None, 0

    for m in valid_markets:
        t = m.get("ticker")
        prices = get_both_prices(client, t)
        if not prices:
            continue
        
        ask_up = prices["YES"]["ask"]
        ask_down = prices["NO"]["ask"]
        
        st.session_state.active_ticker = t
        st.session_state.up_ask_display = f"{ask_up}¢" if ask_up > 0 else "--"
        st.session_state.down_ask_display = f"{ask_down}¢" if ask_down > 0 else "--"

        st.session_state.price_history.append(ask_up)
        st.session_state.price_history = st.session_state.price_history[-100:]

        if outcome_mode in ["YES", "BOTH"]:
            if min_up <= ask_up <= max_up:
                if not use_memory_filter or evaluate_historical_memory(st.session_state.price_history, "YES"):
                    return m, "YES", ask_up

        if outcome_mode in ["NO", "BOTH"]:
            if min_down <= ask_down <= max_down:
                if not use_memory_filter or evaluate_historical_memory(st.session_state.price_history, "NO"):
                    return m, "NO", ask_down

        time.sleep(0.02)

    return None, None, 0


def outcome_to_book_side(outcome, action):
    if outcome == "YES":
        return "bid" if action == "BUY" else "ask"
    return "ask" if action == "BUY" else "bid"


def outcome_price_to_book_price(outcome, action, outcome_cents):
    p = D(outcome_cents) / D(100)
    if outcome == "YES":
        return p
    return D(1) - p


def extract_position(positions_response, ticker, outcome_target=None):
    rows = positions_response.get("market_positions", []) or positions_response.get("positions", []) or []
    for row in rows:
        row_ticker = row.get("ticker") or row.get("market_ticker") or row.get("event_ticker")
        if row_ticker and ticker and row_ticker != ticker:
            if not ticker.startswith(row_ticker) and not row_ticker.startswith(ticker):
                continue
                
        raw = row.get("position_fp") if row.get("position_fp") is not None else row.get("position")
        if raw is None:
            raw = row.get("count") or row.get("balance") or 0
        position = D(raw)
        
        row_outcome = str(row.get("outcome", row.get("side", ""))).upper()
        
        if position > 0:
            oc = "YES" if row_outcome not in ("NO", "DOWN") else "NO"
            if outcome_target and outcome_target != oc: continue
            return {"ticker": row_ticker or ticker, "outcome": oc, "contracts": position}
        if position < 0:
            oc = "NO" if row_outcome not in ("YES", "UP") else "YES"
            if outcome_target and outcome_target != oc: continue
            return {"ticker": row_ticker or ticker, "outcome": oc, "contracts": abs(position)}
            
    return {"ticker": ticker, "outcome": None, "contracts": ZERO}


def fill_outcome(fill):
    action = str(fill.get("action", "")).upper()
    for key in ["outcome_side", "outcome", "side_target"]:
        if fill.get(key):
            val = str(fill[key]).upper()
            if val in ("YES", "NO"): return val
            
    side = str(fill.get("side", "")).upper()
    if side in ("YES", "NO"): return side
    
    if action == "BUY":
        if side == "BID": return "YES"
        if side == "ASK": return "NO"
    elif action == "SELL":
        if side == "ASK": return "YES"
        if side == "BID": return "NO"
        
    return None


def fill_price(fill, outcome):
    keys_to_try = []
    if outcome == "YES":
        keys_to_try = ["yes_price_dollars", "yes_price", "price_dollars", "price"]
    else:
        keys_to_try = ["no_price_dollars", "no_price", "price_dollars", "price"]
        
    for k in keys_to_try:
        if fill.get(k) is not None:
            val = D(fill.get(k))
            if val > 1: return val / D(100)
            return val
            
    return ZERO


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
        action = str(fill.get("action", "")).upper()
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
    return f"vortex-{uuid.uuid4().hex}"


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
        contracts_d = D(contracts)
        price_d = D(outcome_cents) / D(100)
        
        pos_key = f"{ticker}_{outcome}"
        if pos_key not in st.session_state.paper_positions:
            st.session_state.paper_positions[pos_key] = {"contracts": ZERO, "avg_cost": ZERO, "ticker": ticker, "outcome": outcome}
            
        pos = st.session_state.paper_positions[pos_key]

        if action == "BUY":
            total_current_cost = pos["contracts"] * pos["avg_cost"]
            new_cost = contracts_d * price_d
            pos["contracts"] += contracts_d
            pos["avg_cost"] = (total_current_cost + new_cost) / pos["contracts"]
        elif action == "SELL":
            realized_profit = (price_d - pos["avg_cost"]) * contracts_d
            st.session_state.paper_realized_pnl += realized_profit
            pos["contracts"] -= contracts_d
            if pos["contracts"] <= 0:
                pos["contracts"] = ZERO
                pos["avg_cost"] = ZERO

        st.session_state.paper_positions[pos_key] = pos

        log(f"📝 PAPER {action} {contracts} {outcome} {ticker} @ {outcome_cents}¢")
        trade_entry = {
            "Time": now_utc().strftime("%H:%M:%S"),
            "Mode": mode,
            "Action": action,
            "Outcome": outcome,
            "Ticker": ticker,
            "Contracts": contracts,
            "Price": f"{outcome_cents}¢",
            "Cost/Value": f"${estimated_cost:.2f}",
            "Status": "Filled"
        }
        st.session_state.trade_logs.append(trade_entry)
        st.session_state.paper_trades.append(trade_entry)
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
        log(f"🟢 LIVE ORDER {action}: {contracts} {outcome} {ticker} @ {outcome_cents}¢")
        
        st.session_state.trade_logs.append({
            "Time": now_utc().strftime("%H:%M:%S"),
            "Mode": mode,
            "Action": action,
            "Outcome": outcome,
            "Ticker": ticker,
            "Contracts": contracts,
            "Price": f"{outcome_cents}¢",
            "Cost/Value": f"${estimated_cost:.2f}",
            "Status": status
        })

        return {
            "order_id": order_id, "client_order_id": client_order_id,
            "fill_count": fill_count_value, "remaining_count": remaining, "status": status,
        }
    except Exception as error:
        log(f"❌ LIVE ORDER FAILED: {error}")
        raise


def manage_position(client, mode, position, avg_entry, take_profit_pct, stop_loss_pct, enable_panic, panic_mins):
    if position["contracts"] <= 0:
        return False

    ticker = position["ticker"]
    outcome = position["outcome"]
    contracts = int(position["contracts"])
    if outcome not in ("YES", "NO"):
        return False

    try:
        market_data = client.get_market(ticker)
        market = market_data.get("market", market_data)
        mins_left = market_minutes_remaining(market)

        prices = get_both_prices(client, ticker)
        if not prices: 
            log(f"⚠️ Warning: Could not fetch prices for {ticker} during exit check.")
            return False
        current_bid = prices[outcome]["bid"]
        if current_bid <= 0:
            return False
    except Exception as e:
        log(f"⚠️ Exit check API error for {ticker}: {e}")
        return False

    current = D(current_bid) / D(100)

    if avg_entry is None or avg_entry <= 0:
        avg_entry = current

    roi_pct = ((current - avg_entry) / avg_entry) * D(100)

    reason = None
    if take_profit_pct > 0 and roi_pct >= D(take_profit_pct):
        reason = f"TAKE PROFIT (+{roi_pct:.2f}%)"
    elif stop_loss_pct > 0 and roi_pct <= -D(stop_loss_pct):
        reason = f"STOP LOSS ({roi_pct:.2f}%)"
    elif enable_panic and mins_left is not None and mins_left <= panic_mins and current < avg_entry:
        reason = f"TIME PANIC EXIT ({roi_pct:.2f}%)"

    if reason is None:
        return False

    log(f"🌪️ VORTEX {reason}: {ticker} {outcome} entry=${avg_entry:.4f}, bid=${current:.4f}")
    submit_trade(
        client=client, mode=mode, ticker=ticker, outcome=outcome,
        action="SELL", contracts=contracts, outcome_cents=current_bid, reduce_only=True,
    )

    if reason.startswith(("STOP LOSS", "TIME PANIC EXIT", "TAKE PROFIT")):
        st.session_state.blacklisted_tickers.add(f"{ticker}_{outcome}")
        st.session_state["last_status_log"] = None 

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
    st.header("📊 Market & Memory")
    series_ticker = st.text_input("Market Series", value="KXBTC15M")
    display_outcome = st.selectbox("Entry outcome", ["UP", "DOWN", "BOTH (UP & DOWN)"])
    if display_outcome == "UP":
        outcome_mode = "YES"
    elif display_outcome == "DOWN":
        outcome_mode = "NO"
    else:
        outcome_mode = "BOTH"
        
    use_memory_filter = st.checkbox("🧠 Enable Historical Memory Filter", value=True)

    st.divider()
    st.header("💰 Risk & Panic Exit")
    max_dollars_trade = st.number_input("Maximum dollars per trade", min_value=0.01, max_value=10000.00, value=1.00, step=0.50)
    daily_cap = st.number_input("Daily spending cap", min_value=0.01, max_value=100000.00, value=100.00, step=5.00)
    
    min_entry_up, max_entry_up = 15, 65
    min_entry_down, max_entry_down = 15, 65

    if display_outcome in ["UP", "BOTH (UP & DOWN)"]:
        st.subheader("🟢 UP (YES) Entry Limits")
        c1, c2 = st.columns(2)
        with c1:
            min_entry_up = st.slider("Min UP", 1, 99, 15, format="%d¢", key="min_up")
        with c2:
            max_entry_up = st.slider("Max UP", 1, 99, 65, format="%d¢", key="max_up")

    if display_outcome in ["DOWN", "BOTH (UP & DOWN)"]:
        st.subheader("🔴 DOWN (NO) Entry Limits")
        c3, c4 = st.columns(2)
        with c3:
            min_entry_down = st.slider("Min DOWN", 1, 99, 15, format="%d¢", key="min_down")
        with c4:
            max_entry_down = st.slider("Max DOWN", 1, 99, 65, format="%d¢", key="max_down")
        
    max_spread = st.slider("Max Bid/Ask Spread", min_value=1, max_value=50, value=5, format="%d¢")
    cooldown = st.slider("Cooldown between entries", min_value=10, max_value=1800, value=45, step=5, format="%d seconds")
    min_minutes_to_expiry = st.number_input("Do not enter if expiration is closer than", min_value=0.0, max_value=120.0, value=2.0, step=0.5)
    
    take_profit_pct = st.number_input("Take profit % (ROI)", min_value=0.0, max_value=500.0, value=8.0, step=1.0)
    stop_loss_pct = st.number_input("Stop loss % (ROI)", min_value=0.0, max_value=99.0, value=15.0, step=1.0)

    st.divider()
    st.subheader("🚨 Time-Based Panic Exit")
    enable_panic_exit = st.checkbox("Enable Panic Exit before expiry", value=True)
    panic_minutes_threshold = st.number_input("Panic exit if minutes left <=", min_value=0.5, max_value=5.0, value=2.0, step=0.5)

    st.divider()
    st.header("🕐 Trading Hours")
    c1, c2 = st.columns(2)
    with c1:
        start_time = st.time_input("Start", dt.time(0, 0))
    with c2:
        end_time = st.time_input("End", dt.time(23, 59))


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
    if st.button("▶ START VORTEX", type="primary", use_container_width=True):
        if not key_id:
            st.error("Enter your Kalshi API Key ID.")
        elif not private_key_text:
            st.error("Provide the private key.")
        elif trading_mode == "LIVE TRADING" and not live_confirmed:
            st.error("Live confirmation required.")
        else:
            if trading_mode == "LIVE TRADING":
                try:
                    auto_client = KalshiClient(key_id, private_key_text, demo_mode)
                    bal_data = auto_client.get_balance()
                    total_cents = int(bal_data.get("balance", 0))
                    st.session_state.starting_live_balance = D(total_cents) / D(100)
                    
                    if "KXBTC" in series_ticker.upper() or "CRYPTO" in series_ticker.upper():
                        target_shard = 2
                        if total_cents > 0:
                            auto_client.intra_exchange_transfer(amount_cents=total_cents, source_shard=0, dest_shard=target_shard)
                            log(f"🌪️ Transferred {total_cents} cents to Crypto Shard {target_shard}!")
                    time.sleep(1)
                except Exception as e:
                    pass

            st.session_state.running = True
            st.session_state.emergency_stop = False
            log(f"Vortex Scalper Pro started ({trading_mode}).")
            st.rerun()

with c2:
    if st.button("⏹ STOP", use_container_width=True):
        st.session_state.running = False
        log("Vortex stopped.")
        st.rerun()

with c3:
    if st.button("🛑 EMERGENCY STOP", type="secondary", use_container_width=True):
        st.session_state.running = False
        st.session_state.emergency_stop = True
        log("🛑 EMERGENCY STOP ACTIVATED.")
        st.rerun()

if st.session_state.emergency_stop:
    st.error("🛑 EMERGENCY STOP IS ACTIVE.")


# ============================================================
# BOT ENGINE LOOP
# ============================================================

def run_bot_cycle(client, daily_spent):
    if st.session_state.emergency_stop:
        st.session_state.running = False
        st.rerun()
        return

    if trading_mode == "LIVE TRADING" and demo_mode:
        st.session_state.running = False
        st.rerun()
        return

    current_time = dt.datetime.now().time()
    if not (start_time <= current_time <= end_time):
        return

    if daily_spent >= D(daily_cap):
        st.session_state.running = False
        st.rerun()
        return

    try:
        market, found_outcome, entry_price = find_active_market_with_liquidity(
            client, series_ticker, min_expiry_mins=float(min_minutes_to_expiry), 
            outcome_mode=outcome_mode, 
            min_up=min_entry_up, max_up=max_entry_up, 
            min_down=min_entry_down, max_down=max_entry_down,
            use_memory_filter=use_memory_filter
        )

        if not market or entry_price <= 0 or entry_price >= 100:
            return

        ticker = market.get("ticker")
    except Exception:
        return

    pos_key = f"{ticker}_{found_outcome}"
    if pos_key in st.session_state.blacklisted_tickers:
        return

    position = {"contracts": ZERO}
    avg_entry = None

    if trading_mode == "LIVE TRADING":
        try:
            position_response = client.get_positions(ticker=ticker)
            position = extract_position(position_response, ticker, outcome_target=found_outcome)
            fills_response = client.get_fills(ticker=ticker)
            fills = fills_response.get("fills", [])
            avg_entry = reconstruct_average_entry(fills, ticker, found_outcome)
        except Exception:
            return
    else:
        p_pos = st.session_state.paper_positions.get(pos_key, {"contracts": ZERO, "avg_cost": ZERO, "outcome": found_outcome})
        position = {"ticker": ticker, "outcome": found_outcome, "contracts": p_pos["contracts"]}
        avg_entry = p_pos["avg_cost"] if p_pos["contracts"] > 0 else None

    if position["contracts"] > 0:
        try:
            exited = manage_position(
                client, trading_mode, position, avg_entry, 
                take_profit_pct, stop_loss_pct, 
                enable_panic_exit, panic_minutes_threshold
            )
            if exited:
                st.session_state.last_trade_time = now_utc()
                return
        except Exception:
            return

    if position["contracts"] > 0:
        return

    signal = True
    prices = get_both_prices(client, ticker)
    if not prices: return
    live_bid = prices[found_outcome]["bid"]
    spread = entry_price - live_bid

    cur_min = min_entry_up if found_outcome == "YES" else min_entry_down
    cur_max = max_entry_up if found_outcome == "YES" else max_entry_up

    if not (cur_min <= entry_price <= cur_max):
        signal = False
    elif spread > max_spread:
        signal = False
    elif live_bid <= (entry_price * (1 - stop_loss_pct / 100)):
        signal = False
    elif st.session_state.last_trade_time:
        elapsed = (now_utc() - st.session_state.last_trade_time).total_seconds()
        if elapsed < cooldown:
            signal = False

    if signal:
        st.session_state["last_status_log"] = None
        remaining_daily = D(daily_cap) - daily_spent
        contracts = calculate_contracts(D(max_dollars_trade), entry_price, remaining_daily)

        if contracts > 0:
            log(f"🌪️ VORTEX SIGNAL: BUY {contracts} {found_outcome} {ticker} @ {entry_price}¢")
            try:
                submit_trade(
                    client=client, mode=trading_mode, ticker=ticker, outcome=found_outcome,
                    action="BUY", contracts=contracts, outcome_cents=entry_price, reduce_only=False,
                )
                st.session_state.last_trade_time = now_utc()
            except Exception:
                pass


# ============================================================
# TURBO-OPTIMIZED 1-SECOND MONITORING LOOP
# ============================================================

@st.fragment(run_every=1)
def render_dashboard_and_tick():
    st.subheader("🌪️ Vortex Dashboard & Scanner")
    
    client = None
    daily_spent = ZERO
    live_bal = ZERO
    live_pnl = ZERO
    
    if key_id and private_key_text:
        try:
            client = KalshiClient(key_id=key_id, private_key_text=private_key_text, demo=demo_mode)
        except Exception as error:
            if st.session_state.running:
                st.error(f"Private key error: {error}")
                st.session_state.running = False
                st.rerun()

    if client:
        try:
            bal_data = client.get_balance()
            live_bal = D(bal_data.get("balance", 0)) / D(100)
            if st.session_state.starting_live_balance is None:
                st.session_state.starting_live_balance = live_bal
            live_pnl = live_bal - st.session_state.starting_live_balance
        except Exception:
            pass

        try:
            res = client.get_markets(series_ticker=series_ticker, limit=10)
            markets = res.get("markets", [])
            open_markets = [m for m in markets if str(m.get("status", "")).lower() not in ("closed", "settled", "finalized")]
            if open_markets:
                open_markets.sort(key=lambda x: x.get("ticker", ""))
                t_sample = open_markets[0].get("ticker")
                p_sample = get_both_prices(client, t_sample)
                if p_sample:
                    st.session_state.active_ticker = t_sample
                    st.session_state.up_ask_display = f"{p_sample['YES']['ask']}¢" if p_sample['YES']['ask'] > 0 else "--"
                    st.session_state.down_ask_display = f"{p_sample['NO']['ask']}¢" if p_sample['NO']['ask'] > 0 else "--"
        except Exception:
            pass

    if client and trading_mode == "LIVE TRADING":
        try:
            daily_spent = calculate_daily_spend(client)
        except Exception:
            pass
    elif trading_mode == "PAPER TRADING":
        daily_spent = sum((D(str(row.get("Cost/Value", row.get("Cost", "0"))).replace("$", "")) for row in st.session_state.paper_trades if row.get("Action") == "BUY"), ZERO)

    paper_unrealized = ZERO
    if client and trading_mode == "PAPER TRADING":
        for pk, p_data in st.session_state.paper_positions.items():
            if p_data["contracts"] > 0:
                prices = get_both_prices(client, p_data["ticker"])
                if prices:
                    current_bid_cents = prices[p_data["outcome"]]["bid"]
                    if current_bid_cents > 0:
                        current_val = D(current_bid_cents) / D(100)
                        paper_unrealized += (current_val - p_data["avg_cost"]) * p_data["contracts"]

    c_dash1, c_dash2, c_dash3, c_dash4, c_dash5, c_dash6 = st.columns(6)
    with c_dash1:
        st.metric("Mode", trading_mode)
    with c_dash2:
        if trading_mode == "PAPER TRADING":
            st.metric("Realized P/L", f"${st.session_state.paper_realized_pnl:.2f}")
        else:
            st.metric("Live P/L", f"${live_pnl:.2f}")
    with c_dash3:
        if trading_mode == "PAPER TRADING":
            st.metric("Unrealized P/L", f"${paper_unrealized:.2f}")
        else:
            st.metric("Live Balance", f"${live_bal:.2f}")
    with c_dash4:
        st.metric("Active Market", st.session_state.active_ticker or "--")
    with c_dash5:
        st.metric("🟢 UP Ask", st.session_state.up_ask_display)
    with c_dash6:
        st.metric("🔴 DOWN Ask", st.session_state.down_ask_display)

    if st.session_state.running and client:
        run_bot_cycle(client, daily_spent)
    elif not st.session_state.running:
        st.info("Vortex is stopped. Choose your settings and press 'START VORTEX'.")

    if len(st.session_state.price_history) > 1:
        st.subheader("📈 Smooth Historical Price Momentum (Memory Buffer)")
        st.line_chart(st.session_state.price_history, height=200)

    if trading_mode == "PAPER TRADING" and any(p["contracts"] > 0 for p in st.session_state.paper_positions.values()):
        st.subheader("💼 Active Paper Positions")
        active_pos_list = []
        for pk, p_data in st.session_state.paper_positions.items():
            if p_data["contracts"] > 0:
                active_pos_list.append({
                    "Ticker": p_data["ticker"],
                    "Outcome": p_data["outcome"],
                    "Contracts": p_data["contracts"],
                    "Avg Entry": f"${p_data['avg_cost']:.4f}",
                    "Total Cost": f"${(p_data['contracts'] * p_data['avg_cost']):.2f}"
                })
        st.dataframe(active_pos_list, use_container_width=True, hide_index=True)

    st.subheader("🛒 Execution Log")
    if st.session_state.trade_logs:
        st.dataframe(st.session_state.trade_logs[-10:], use_container_width=True, hide_index=True)
    else:
        st.info("No trades executed yet.")

    st.subheader("📜 Vortex Log (Turbo Buffer)")
    if st.session_state.logs:
        st.code("\n".join(st.session_state.logs[-15:]))
    else:
        st.info("Waiting for Vortex activity...")

render_dashboard_and_tick()
