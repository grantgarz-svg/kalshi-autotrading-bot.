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
# PAGE
# ============================================================

st.set_page_config(
    page_title="Kalshi Scalper",
    page_icon="⚡",
    layout="wide",
)

st.title("⚡ Kalshi Scalper")
st.caption("Paper/live Kalshi trading dashboard with risk controls")


# ============================================================
# CONSTANTS
# ============================================================

PROD_BASE_URL = "https://api.elections.kalshi.com/trade-api/v2"
DEMO_BASE_URL = "https://demo-api.kalshi.co/trade-api/v2"

LOG_FILE = Path("kalshi_orders.csv")

ZERO = Decimal("0")

MONTH_MAP = {
    "JAN": 1, "FEB": 2, "MAR": 3, "APR": 4, "MAY": 5, "JUN": 6,
    "JUL": 7, "AUG": 8, "SEP": 9, "OCT": 10, "NOV": 11, "DEC": 12,
}


# ============================================================
# SESSION STATE
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
        "timestamp",
        "mode",
        "ticker",
        "outcome",
        "action",
        "book_side",
        "contracts",
        "price",
        "estimated_cost",
        "order_id",
        "client_order_id",
        "fill_count",
        "remaining_count",
        "status",
        "error",
    ]

    exists = LOG_FILE.exists()

    with LOG_FILE.open("a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        if not exists:
            writer.writeheader()
        writer.writerow({k: row.get(k, "") for k in fields})


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

    signature = create_signature(
        private_key,
        timestamp,
        method,
        path,
    )

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
    """
    Uses the current Kalshi production/demo REST host and
    RSA-PSS request signing.

    Order writes use the V2 event-market order endpoint:
        POST /portfolio/events/orders

    V2 prediction orders use:
        side="bid" -> buy YES / sell NO
        side="ask" -> sell YES / buy NO
    """

    def __init__(self, key_id, private_key_text, demo=False):
        self.key_id = key_id.strip()
        self.private_key = load_private_key(private_key_text)
        self.base_url = DEMO_BASE_URL if demo else PROD_BASE_URL

    def request(self, method, endpoint, params=None, body=None):
        url = self.base_url + endpoint

        headers = create_headers(
            self.key_id,
            self.private_key,
            method,
            url,
        )

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

            raise RuntimeError(
                f"Kalshi API {response.status_code}: {detail}"
            )

        if not response.content:
            return {}

        return response.json()

    # ----------------------------
    # Market data
    # ----------------------------

    def get_markets(self, series_ticker):
        return self.request(
            "GET",
            "/markets",
            params={
                "series_ticker": series_ticker,
                "status": "open",
                "limit": 100,
            },
        )

    def get_market(self, ticker):
        return self.request(
            "GET",
            f"/markets/{ticker}",
        )

    def get_orderbook(self, ticker):
        return self.request(
            "GET",
            f"/markets/{ticker}/orderbook",
        )

    # ----------------------------
    # Portfolio
    # ----------------------------

    def get_balance(self):
        return self.request(
            "GET",
            "/portfolio/balance",
        )

    def get_positions(self, ticker=None):
        params = {"limit": 1000}
        if ticker:
            params["ticker"] = ticker

        return self.request(
            "GET",
            "/portfolio/positions",
            params=params,
        )

    def get_fills(self, ticker=None, min_ts=None):
        params = {"limit": 1000}

        if ticker:
            params["ticker"] = ticker

        if min_ts is not None:
            params["min_ts"] = int(min_ts)

        return self.request(
            "GET",
            "/portfolio/fills",
            params=params,
        )

    def get_orders(self, ticker=None, status=None):
        params = {"limit": 100}

        if ticker:
            params["ticker"] = ticker

        if status:
            params["status"] = status

        return self.request(
            "GET",
            "/portfolio/orders",
            params=params,
        )

    def get_order(self, order_id):
        return self.request(
            "GET",
            f"/portfolio/orders/{order_id}",
        )

    # ----------------------------
    # V2 order writing
    # ----------------------------

    def create_v2_order(
        self,
        ticker,
        client_order_id,
        book_side,
        contracts,
        price_dollars,
        reduce_only=False,
    ):
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

        return self.request(
            "POST",
            "/portfolio/events/orders",
            body=body,
        )

    def cancel_order(self, order_id):
        return self.request(
            "DELETE",
            f"/portfolio/events/orders/{order_id}",
        )


# ============================================================
# MARKET HELPERS & TICKER EXPIRATION PARSER
# ============================================================

def parse_ticker_expiry(ticker):
    """
    Directly extracts expiry from tickers like 'KXBTC15M-26SEP222315-15'
    Pattern: YY + MMM + DD + HHMM (e.g., 26SEP222315 -> 2026-09-22 23:15 UTC)
    """
    if not ticker:
        return None

    parts = str(ticker).split("-")
    if len(parts) >= 2:
        date_str = parts[1].upper()
        match = re.match(r"^(\d{2})([A-Z]{3})(\d{2})(\d{2})(\d{2})$", date_str)
        if match:
            yy, mmm, dd, hh, mm = match.groups()
            month = MONTH_MAP.get(mmm)
            if month:
                try:
                    return dt.datetime(
                        year=2000 + int(yy),
                        month=month,
                        day=int(dd),
                        hour=int(hh),
                        minute=int(mm),
                        tzinfo=dt.timezone.utc,
                    )
                except Exception:
                    pass
    return None


def parse_time(value):
    if not value:
        return None

    if isinstance(value, (int, float)):
        return dt.datetime.fromtimestamp(
            float(value),
            tz=dt.timezone.utc,
        )

    text = str(value)

    try:
        return dt.datetime.fromisoformat(
            text.replace("Z", "+00:00")
        ).astimezone(dt.timezone.utc)
    except Exception:
        return None


def market_minutes_remaining(market):
    ticker = market.get("ticker", "")
    # Primary: Parse the exact target time directly from the contract ticker
    close = parse_ticker_expiry(ticker)

    # Fallback: Use payload expiration timestamps if ticker format fails
    if close is None:
        close = (
            parse_time(market.get("close_time"))
            or parse_time(market.get("expiration_time"))
        )

    if close is None:
        return None

    return (close - now_utc()).total_seconds() / 60


def find_active_market(markets):
    open_markets = [
        m for m in markets
        if m.get("status") in (None, "open", "active")
    ]

    valid_markets = []
    for m in open_markets:
        mins = market_minutes_remaining(m)
        # Drop contracts whose trading window has already passed
        if mins is not None and mins > 0:
            valid_markets.append(m)

    if not valid_markets:
        return None

    # 1. Sort by closest closing time to get the active 15-minute window
    valid_markets.sort(key=lambda m: market_minutes_remaining(m))
    target_mins = market_minutes_remaining(valid_markets[0])

    # 2. Isolate all strikes for this specific window
    current_window = [
        m for m in valid_markets
        if abs(market_minutes_remaining(m) - target_mins) < 1.0
    ]

    # 3. Target the At-The-Money (ATM) strike with tight spreads and active liquidity
    def atm_score(m):
        bid = m.get("yes_bid")
        ask = m.get("yes_ask")

        # Heavily penalize empty order books
        if bid is None or ask is None or bid == 0 or ask >= 100:
            return 9999

        midpoint = (bid + ask) / 2
        spread = ask - bid
        return abs(midpoint - 50) + spread

    current_window.sort(key=atm_score)

    return current_window[0]


def get_market_prices(market):
    yes_bid = market.get("yes_bid")
    yes_ask = market.get("yes_ask")
    no_bid = market.get("no_bid")
    no_ask = market.get("no_ask")

    if yes_ask is None and no_bid is not None:
        yes_ask = 100 - int(no_bid)

    if no_ask is None and yes_bid is not None:
        no_ask = 100 - int(yes_bid)

    return {
        "yes_bid": int(yes_bid) if yes_bid is not None else None,
        "yes_ask": int(yes_ask) if yes_ask is not None else None,
        "no_bid": int(no_bid) if no_bid is not None else None,
        "no_ask": int(no_ask) if no_ask is not None else None,
    }


# ============================================================
# V2 OUTCOME CONVERSION
# ============================================================

def outcome_to_book_side(outcome, action):
    """
    V2 single-book architecture:
    YES: buy -> bid, sell -> ask
    NO:  buy -> ask, sell -> bid
    """
    if outcome == "YES":
        return "bid" if action == "BUY" else "ask"

    return "ask" if action == "BUY" else "bid"


def outcome_price_to_book_price(outcome, action, outcome_cents):
    p = D(outcome_cents) / D(100)

    if outcome == "YES":
        return p

    return D(1) - p


# ============================================================
# POSITIONS
# ============================================================

def extract_position(positions_response, ticker):
    rows = positions_response.get("market_positions", [])

    for row in rows:
        row_ticker = (
            row.get("ticker")
            or row.get("market_ticker")
        )

        if row_ticker != ticker:
            continue

        raw = (
            row.get("position_fp")
            if row.get("position_fp") is not None
            else row.get("position")
        )

        position = D(raw)

        if position > 0:
            return {
                "ticker": ticker,
                "outcome": "YES",
                "contracts": position,
            }

        if position < 0:
            return {
                "ticker": ticker,
                "outcome": "NO",
                "contracts": abs(position),
            }

        return {
            "ticker": ticker,
            "outcome": None,
            "contracts": ZERO,
        }

    return {
        "ticker": ticker,
        "outcome": None,
        "contracts": ZERO,
    }


# ============================================================
# FILL / ENTRY PRICE HELPERS
# ============================================================

def fill_outcome(fill):
    outcome = (
        fill.get("outcome_side")
        or fill.get("side")
        or fill.get("outcome")
    )

    if outcome:
        return str(outcome).upper()

    book_side = str(fill.get("book_side", "")).lower()

    if book_side == "bid":
        return "YES"

    if book_side == "ask":
        return "NO"

    return None


def fill_action(fill):
    action = fill.get("action")

    if action:
        return str(action).upper()

    return None


def fill_price(fill, outcome):
    if outcome == "YES":
        raw = (
            fill.get("yes_price_dollars")
            or fill.get("yes_price")
        )
    else:
        raw = (
            fill.get("no_price_dollars")
            or fill.get("no_price")
        )

    value = D(raw)

    if value > 1:
        value = value / D(100)

    return value


def fill_count(fill):
    raw = (
        fill.get("count_fp")
        or fill.get("count")
        or fill.get("filled_count_fp")
        or fill.get("filled_count")
    )

    return D(raw)


def reconstruct_average_entry(fills, ticker, outcome):
    relevant = []

    for fill in fills:
        if (
            fill.get("ticker")
            and fill.get("ticker") != ticker
        ):
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

        created = (
            fill.get("created_time")
            or fill.get("ts")
            or ""
        )

        relevant.append(
            (str(created), action, qty, price)
        )

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

    total_cost = sum(
        (lot_qty * lot_price for lot_qty, lot_price in lots),
        ZERO,
    )

    return total_cost / total_qty


# ============================================================
# DAILY SPENDING
# ============================================================

def utc_day_start_timestamp():
    today = now_utc().replace(
        hour=0,
        minute=0,
        second=0,
        microsecond=0,
    )
    return int(today.timestamp())


def calculate_daily_spend(client):
    fills_response = client.get_fills(
        min_ts=utc_day_start_timestamp()
    )

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
        fee = D(fill.get("fee_cost"))

        total += qty * price
        total += fee

    return total


# ============================================================
# ORDER ENGINE
# ============================================================

def make_client_order_id():
    return f"ksbot-{uuid.uuid4().hex}"


def calculate_contracts(max_dollars, price_cents, remaining_daily):
    if price_cents <= 0:
        return 0

    price = D(price_cents) / D(100)

    allowed = min(
        D(max_dollars),
        D(remaining_daily),
    )

    if allowed <= 0:
        return 0

    return int(allowed / price)


def submit_trade(
    client,
    mode,
    ticker,
    outcome,
    action,
    contracts,
    outcome_cents,
    reduce_only=False,
):
    book_side = outcome_to_book_side(
        outcome,
        action,
    )

    book_price = outcome_price_to_book_price(
        outcome,
        action,
        outcome_cents,
    )

    estimated_cost = D(contracts) * (
        D(outcome_cents) / D(100)
    )

    client_order_id = make_client_order_id()

    st.session_state.last_client_order_id = client_order_id

    # Paper mode
    if mode == "PAPER TRADING":
        log(
            f"📝 PAPER {action} {contracts} {outcome} "
            f"{ticker} @ {outcome_cents}¢"
        )

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
            "timestamp": now_utc().isoformat(),
            "mode": mode,
            "ticker": ticker,
            "outcome": outcome,
            "action": action,
            "book_side": book_side,
            "contracts": contracts,
            "price": f"{D(outcome_cents)/D(100):.4f}",
            "estimated_cost": f"{estimated_cost:.4f}",
            "order_id": "PAPER",
            "client_order_id": client_order_id,
            "fill_count": contracts,
            "remaining_count": 0,
            "status": "paper_fill",
            "error": "",
        })

        return {
            "order_id": "PAPER",
            "client_order_id": client_order_id,
            "fill_count": D(contracts),
            "remaining_count": ZERO,
            "status": "paper_fill",
        }

    # Live order
    try:
        response = client.create_v2_order(
            ticker=ticker,
            client_order_id=client_order_id,
            book_side=book_side,
            contracts=contracts,
            price_dollars=book_price,
            reduce_only=reduce_only,
        )

        order = response.get("order", response)

        order_id = order.get("order_id")
        fill_count_value = D(
            order.get("fill_count")
            or order.get("fill_count_fp")
        )

        remaining = D(
            order.get("remaining_count")
            or order.get("remaining_count_fp")
        )

        status = order.get("status", "submitted")

        st.session_state.last_order_id = order_id
        st.session_state.last_fill_count = fill_count_value

        append_order_log({
            "timestamp": now_utc().isoformat(),
            "mode": mode,
            "ticker": ticker,
            "outcome": outcome,
            "action": action,
            "book_side": book_side,
            "contracts": contracts,
            "price": f"{book_price:.4f}",
            "estimated_cost": f"{estimated_cost:.4f}",
            "order_id": order_id or "",
            "client_order_id": client_order_id,
            "fill_count": f"{fill_count_value:.2f}",
            "remaining_count": f"{remaining:.2f}",
            "status": status,
            "error": "",
        })

        log(
            f"🟢 LIVE ORDER {action}: "
            f"{contracts} {outcome} {ticker} "
            f"@ {outcome_cents}¢ | "
            f"filled={fill_count_value} | "
            f"order={order_id}"
        )

        return {
            "order_id": order_id,
            "client_order_id": client_order_id,
            "fill_count": fill_count_value,
            "remaining_count": remaining,
            "status": status,
        }

    except Exception as error:
        append_order_log({
            "timestamp": now_utc().isoformat(),
            "mode": mode,
            "ticker": ticker,
            "outcome": outcome,
            "action": action,
            "book_side": book_side,
            "contracts": contracts,
            "price": f"{book_price:.4f}",
            "estimated_cost": f"{estimated_cost:.4f}",
            "order_id": "",
            "client_order_id": client_order_id,
            "fill_count": "",
            "remaining_count": "",
            "status": "ERROR",
            "error": str(error),
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

            filled = D(
                order.get("fill_count")
                or order.get("fill_count_fp")
            )

            remaining = D(
                order.get("remaining_count")
                or order.get("remaining_count_fp")
            )

            status = order.get("status", "unknown")

            if filled > 0 or status in (
                "canceled",
                "executed",
                "filled",
            ):
                return filled, remaining, status

        except Exception:
            pass

        time.sleep(0.5)

    if latest:
        return (
            D(
                latest.get("fill_count")
                or latest.get("fill_count_fp")
            ),
            D(
                latest.get("remaining_count")
                or latest.get("remaining_count_fp")
            ),
            latest.get("status", "unknown"),
        )

    return ZERO, ZERO, "unknown"


# ============================================================
# RISK / EXIT ENGINE
# ============================================================

def manage_position(
    client,
    mode,
    market,
    position,
    fills,
    take_profit_pct,
    stop_loss_pct,
):
    if position["contracts"] <= 0:
        return False

    ticker = position["ticker"]
    outcome = position["outcome"]
    contracts = int(position["contracts"])

    if outcome not in ("YES", "NO"):
        return False

    prices = get_market_prices(market)

    if outcome == "YES":
        current_bid = prices["yes_bid"]
    else:
        current_bid = prices["no_bid"]

    if current_bid is None or current_bid <= 0:
        return False

    entry = reconstruct_average_entry(
        fills,
        ticker,
        outcome,
    )

    if entry is None:
        log(
            f"Position {ticker} {outcome} has no "
            f"reconstructed entry price; TP/SL skipped."
        )
        return False

    current = D(current_bid) / D(100)

    tp_price = entry * (
        D(1) + D(take_profit_pct) / D(100)
    )

    sl_price = entry * (
        D(1) - D(stop_loss_pct) / D(100)
    )

    reason = None

    if take_profit_pct > 0 and current >= tp_price:
        reason = "TAKE PROFIT"

    if stop_loss_pct > 0 and current <= sl_price:
        reason = "STOP LOSS"

    if reason is None:
        return False

    log(
        f"🚨 {reason}: {ticker} {outcome} "
        f"entry=${entry:.4f}, bid=${current:.4f}"
    )

    result = submit_trade(
        client=client,
        mode=mode,
        ticker=ticker,
        outcome=outcome,
        action="SELL",
        contracts=contracts,
        outcome_cents=current_bid,
        reduce_only=True,
    )

    if mode == "LIVE TRADING" and result.get("order_id"):
        filled, remaining, status = confirm_fill(
            client,
            result["order_id"],
        )

        log(
            f"{reason} exit confirmation: "
            f"filled={filled}, remaining={remaining}, "
            f"status={status}"
        )

    return True


# ============================================================
# SIDEBAR
# ============================================================

with st.sidebar:
    st.header("🔑 Account")

    trading_mode = st.radio(
        "Trading mode",
        ["PAPER TRADING", "LIVE TRADING"],
        index=0,
    )

    demo_mode = st.checkbox(
        "Use Kalshi Demo API",
        value=True,
        help="Keep this checked while testing. Uncheck for Kalshi production.",
    )

    key_id_default = ""
    private_key_default = ""

    try:
        key_id_default = st.secrets.get(
            "KALSHI_API_KEY_ID",
            "",
        )
        private_key_default = st.secrets.get(
            "KALSHI_PRIVATE_KEY",
            "",
        )
    except Exception:
        pass

    key_id = st.text_input(
        "Kalshi API Key ID",
        value=key_id_default,
        type="password",
    )

    private_key_text = st.text_area(
        "Private Key PEM",
        value=private_key_default,
        height=180,
        help=(
            "Prefer Streamlit Secrets instead of pasting your "
            "private key into the app."
        ),
    )

    st.divider()

    st.header("📊 Market")

    series_ticker = st.text_input(
        "Market Series",
        value="KXBTC15M",
    )

    display_outcome = st.selectbox(
        "Entry outcome",
        ["UP", "DOWN"],
    )

    # Map UI display to single-book backend format
    outcome_to_trade = "YES" if display_outcome == "UP" else "NO"

    st.divider()

    st.header("💰 Risk Settings")

    max_dollars_trade = st.number_input(
        "Maximum dollars per trade",
        min_value=0.01,
        max_value=10000.00,
        value=10.00,
        step=1.00,
    )

    daily_cap = st.number_input(
        "Daily spending cap",
        min_value=0.01,
        max_value=100000.00,
        value=100.00,
        step=5.00,
    )

    max_entry = st.slider(
        "Maximum entry price",
        min_value=1,
        max_value=99,
        value=75,
        format="%d¢",
    )

    cooldown = st.slider(
        "Cooldown between entries",
        min_value=10,
        max_value=1800,
        value=60,
        step=10,
        format="%d seconds",
    )

    min_minutes_to_expiry = st.number_input(
        "Do not enter if expiration is closer than",
        min_value=0.0,
        max_value=120.0,
        value=2.0,
        step=0.5,
    )

    take_profit_pct = st.number_input(
        "Take profit %",
        min_value=0.0,
        max_value=500.0,
        value=20.0,
        step=1.0,
    )

    stop_loss_pct = st.number_input(
        "Stop loss %",
        min_value=0.0,
        max_value=99.0,
        value=25.0,
        step=1.0,
    )

    st.divider()

    st.header("🕐 Trading Hours")

    c1, c2 = st.columns(2)

    with c1:
        start_time = st.time_input(
            "Start",
            dt.time(0, 0),
        )

    with c2:
        end_time = st.time_input(
            "End",
            dt.time(23, 59),
        )

    st.divider()

    manage_existing = st.checkbox(
        "Manage existing position for TP/SL",
        value=False,
        help=(
            "If disabled, TP/SL is only used for positions "
            "created while this bot is running."
        ),
    )


# ============================================================
# SAFETY GATE
# ============================================================

live_confirmed = False

if trading_mode == "LIVE TRADING":
    st.warning(
        "⚠️ LIVE TRADING CAN PLACE REAL MONEY ORDERS."
    )

    confirmation = st.text_input(
        "Type LIVE I UNDERSTAND to enable live orders",
        type="password",
    )

    live_confirmed = (
        confirmation.strip() == "LIVE I UNDERSTAND"
    )

    if not live_confirmed:
        st.error(
            "Live orders are locked until the confirmation phrase is entered."
        )


# ============================================================
# DASHBOARD
# ============================================================

st.subheader("Dashboard")

c1, c2, c3, c4, c5 = st.columns(5)

with c1:
    st.metric(
        "Mode",
        trading_mode,
    )

with c2:
    st.metric(
        "Daily Spent",
        "$--",
    )

with c3:
    st.metric(
        "Market",
        st.session_state.active_ticker or "--",
    )

with c4:
    price_display = (
        f"{st.session_state.last_price}¢"
        if st.session_state.last_price
        else "--"
    )
    st.metric(
        "Entry Price",
        price_display,
    )

with c5:
    st.metric(
        "Last Fill",
        f"{st.session_state.last_fill_count:.2f}",
    )


# ============================================================
# START / STOP
# ============================================================

c1, c2, c3 = st.columns(3)

with c1:
    if st.button(
        "▶ START AUTOTRADING",
        type="primary",
        use_container_width=True,
    ):
        if not key_id:
            st.error("Enter your Kalshi API Key ID.")

        elif not private_key_text:
            st.error(
                "Provide the private key through Streamlit Secrets "
                "or the field above."
            )

        elif trading_mode == "LIVE TRADING" and not live_confirmed:
            st.error("Live confirmation is required.")

        else:
            st.session_state.running = True
            st.session_state.emergency_stop = False
            log(
                f"Bot started in {trading_mode}. "
                f"Demo API={demo_mode}."
            )
            st.rerun()

with c2:
    if st.button(
        "⏹ STOP",
        use_container_width=True,
    ):
        st.session_state.running = False
        log("Bot stopped.")
        st.rerun()

with c3:
    if st.button(
        "🛑 EMERGENCY STOP",
        type="secondary",
        use_container_width=True,
    ):
        st.session_state.running = False
        st.session_state.emergency_stop = True
        log("🛑 EMERGENCY STOP ACTIVATED.")
        st.rerun()


if st.session_state.emergency_stop:
    st.error(
        "🛑 EMERGENCY STOP IS ACTIVE. "
        "Press START only after reviewing your settings."
    )


# ============================================================
# BOT LOOP
# ============================================================

if st.session_state.running:

    # --------------------------------------------------------
    # Safety
    # --------------------------------------------------------

    if st.session_state.emergency_stop:
        st.session_state.running = False
        st.stop()

    # --------------------------------------------------------
    # Credentials
    # --------------------------------------------------------

    try:
        client = KalshiClient(
            key_id=key_id,
            private_key_text=private_key_text,
            demo=demo_mode,
        )
    except Exception as error:
        st.error(f"Private key error: {error}")
        st.session_state.running = False
        st.stop()

    # --------------------------------------------------------
    # Live mode must never use Demo API
    # --------------------------------------------------------

    if trading_mode == "LIVE TRADING" and demo_mode:
        st.error(
            "LIVE TRADING is selected while Demo API is enabled. "
            "Disable 'Use Kalshi Demo API' before live orders."
        )
        st.session_state.running = False
        st.stop()

    # --------------------------------------------------------
    # Trading hours
    # --------------------------------------------------------

    current_time = dt.datetime.now().time()

    if not (start_time <= current_time <= end_time):
        log("Outside trading hours.")
        time.sleep(5)
        st.rerun()

    # --------------------------------------------------------
    # Daily spend
    # --------------------------------------------------------

    try:
        daily_spent = (
            calculate_daily_spend(client)
            if trading_mode == "LIVE TRADING"
            else sum(
                (
                    D(str(row["Cost"]).replace("$", ""))
                    for row in st.session_state.paper_trades
                ),
                ZERO,
            )
        )

        c1.metric(
            "Daily Spent",
            f"${daily_spent:.2f}",
        )

    except Exception as error:
        log(f"Daily spend error: {error}")
        daily_spent = ZERO

    if daily_spent >= D(daily_cap):
        st.error("Daily spending cap reached.")
        log(f"Daily cap reached: ${daily_spent:.2f}")
        st.session_state.running = False
        st.stop()

    # --------------------------------------------------------
    # Find market
    # --------------------------------------------------------

    try:
        markets_response = client.get_markets(series_ticker)

        market = find_active_market(
            markets_response.get("markets", [])
        )

        if not market:
            log(f"No open market for {series_ticker}.")
            time.sleep(5)
            st.rerun()

        ticker = market.get("ticker")

        if not ticker:
            raise RuntimeError(
                "Kalshi returned an open market without a ticker."
            )

        st.session_state.active_ticker = ticker

    except Exception as error:
        log(f"Market error: {error}")
        time.sleep(5)
        st.rerun()

    # --------------------------------------------------------
    # Expiration protection
    # --------------------------------------------------------

    minutes_left = market_minutes_remaining(market)

    if (
        minutes_left is not None
        and minutes_left <= float(min_minutes_to_expiry)
    ):
        log(
            f"{ticker}: entry blocked because "
            f"only {minutes_left:.2f} minutes remain."
        )
        time.sleep(5)
        st.rerun()

    # --------------------------------------------------------
    # Current prices
    # --------------------------------------------------------

    try:
        prices = get_market_prices(market)

        entry_price = (
            prices["yes_ask"]
            if outcome_to_trade == "YES"
            else prices["no_ask"]
        )

        if entry_price is None:
            detail = client.get_market(ticker)
            prices = get_market_prices(
                detail.get("market", detail)
            )

            entry_price = (
                prices["yes_ask"]
                if outcome_to_trade == "YES"
                else prices["no_ask"]
            )

        if entry_price is None:
            log(f"{ticker}: no {outcome_to_trade} ask.")
            time.sleep(3)
            st.rerun()

        st.session_state.last_price = entry_price

    except Exception as error:
        log(f"Price error: {error}")
        time.sleep(5)
        st.rerun()

    # --------------------------------------------------------
    # Position tracking
    # --------------------------------------------------------

    try:
        position_response = client.get_positions(ticker=ticker)
        position = extract_position(position_response, ticker)

        if position["contracts"] > 0:
            log(
                f"POSITION {ticker}: "
                f"{position['contracts']:.2f} "
                f"{position['outcome']}"
            )

        fills_response = client.get_fills(ticker=ticker)
        fills = fills_response.get("fills", [])

    except Exception as error:
        log(f"Position/fill error: {error}")
        time.sleep(5)
        st.rerun()

    # --------------------------------------------------------
    # Take profit / stop loss
    # --------------------------------------------------------

    if (
        position["contracts"] > 0
        and (
            manage_existing
            or ticker in st.session_state.bot_positions
        )
    ):
        try:
            exited = manage_position(
                client=client,
                mode=trading_mode,
                market=market,
                position=position,
                fills=fills,
                take_profit_pct=take_profit_pct,
                stop_loss_pct=stop_loss_pct,
            )

            if exited:
                st.session_state.last_trade_time = now_utc()
                time.sleep(2)
                st.rerun()

        except Exception as error:
            log(f"TP/SL error: {error}")

    # --------------------------------------------------------
    # Duplicate-order protection
    # --------------------------------------------------------

    already_in_target_position = (
        position["contracts"] > 0
        and position["outcome"] == outcome_to_trade
    )

    if already_in_target_position:
        log(
            f"Duplicate protection: already holding "
            f"{position['contracts']:.2f} {outcome_to_trade} "
            f"in {ticker}."
        )
        time.sleep(3)
        st.rerun()

    # --------------------------------------------------------
    # Cooldown
    # --------------------------------------------------------

    signal = entry_price <= max_entry

    if signal and st.session_state.last_trade_time:
        elapsed = (
            now_utc() - st.session_state.last_trade_time
        ).total_seconds()

        if elapsed < cooldown:
            signal = False

    # --------------------------------------------------------
    # Entry
    # --------------------------------------------------------

    if signal:
        remaining_daily = D(daily_cap) - daily_spent

        contracts = calculate_contracts(
            max_dollars=D(max_dollars_trade),
            price_cents=entry_price,
            remaining_daily=remaining_daily,
        )

        if contracts <= 0:
            log(
                "Entry blocked: daily cap or trade limit "
                "does not allow even one contract."
            )

        else:
            estimated_cost = (
                D(contracts) * D(entry_price) / D(100)
            )

            log(
                f"ENTRY SIGNAL: BUY {contracts} "
                f"{outcome_to_trade} {ticker} "
                f"@ {entry_price}¢ "
                f"(~${estimated_cost:.2f})"
            )

            try:
                result = submit_trade(
                    client=client,
                    mode=trading_mode,
                    ticker=ticker,
                    outcome=outcome_to_trade,
                    action="BUY",
                    contracts=contracts,
                    outcome_cents=entry_price,
                    reduce_only=False,
                )

                st.session_state.last_trade_time = now_utc()

                if trading_mode == "LIVE TRADING":
                    filled, remaining, status = confirm_fill(
                        client,
                        result.get("order_id"),
                    )

                    st.session_state.last_fill_count = filled

                    log(
                        f"FILL CONFIRMATION: "
                        f"filled={filled}, "
                        f"remaining={remaining}, "
                        f"status={status}"
                    )

                    if filled > 0:
                        st.session_state.bot_positions[ticker] = {
                            "outcome": outcome_to_trade,
                            "contracts": str(filled),
                        }

                else:
                    st.session_state.bot_positions[ticker] = {
                        "outcome": outcome_to_trade,
                        "contracts": str(contracts),
                    }

            except Exception:
                pass

    else:
        log(
            f"{ticker}: {outcome_to_trade} ask "
            f"{entry_price}¢ > max {max_entry}¢ "
            f"or cooldown/position protection active."
        )

    # --------------------------------------------------------
    # Bot log
    # --------------------------------------------------------

    st.subheader("📜 Bot Log")

    if st.session_state.logs:
        st.code("\n".join(st.session_state.logs[-30:]))
    else:
        st.info("Waiting for bot activity...")

    # --------------------------------------------------------
    # Paper trades
    # --------------------------------------------------------

    if st.session_state.paper_trades:
        st.subheader("📝 Paper Trades")

        st.dataframe(
            st.session_state.paper_trades,
            use_container_width=True,
            hide_index=True,
        )

    # --------------------------------------------------------
    # Refresh
    # --------------------------------------------------------

    time.sleep(3)
    st.rerun()


# ============================================================
# STOPPED SCREEN
# ============================================================

else:
    st.info(
        "Bot is stopped. Choose your settings and press 'START AUTOTRADING'."
    )

    if st.session_state.logs:
        st.subheader("📜 Bot Log")
        st.code("\n".join(st.session_state.logs[-30:]))

    if st.session_state.paper_trades:
        st.subheader("📝 Paper Trades")

        st.dataframe(
            st.session_state.paper_trades,
            use_container_width=True,
            hide_index=True,
        )

    if LOG_FILE.exists():
        st.download_button(
            "⬇️ Download order log",
            data=LOG_FILE.read_bytes(),
            file_name="kalshi_orders.csv",
            mime="text/csv",
        )
