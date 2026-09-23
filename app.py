import streamlit as st
import requests
import time
import datetime as dt
import uuid
import json
from pathlib import Path
from decimal import Decimal
from urllib.parse import urlparse
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding


# ============================================================
# STREAMLIT CONFIG
# ============================================================

st.set_page_config(
    page_title="Kalshi Scalper",
    page_icon="⚡",
    layout="wide",
)

st.title("⚡ Kalshi Scalper")
st.caption("Short-term Kalshi momentum trader — paper trading by default")


# ============================================================
# SESSION STATE
# ============================================================

DEFAULTS = {
    "running": False,
    "logs": [],
    "daily_spent": Decimal("0"),
    "paper_positions": {},
    "paper_trades": [],
    "last_trade_time": None,
    "active_ticker": None,
    "last_price": None,
    "last_signal": "Waiting",
}

for key, value in DEFAULTS.items():
    if key not in st.session_state:
        st.session_state[key] = value


# ============================================================
# API CONFIG
# ============================================================

# Current production API base.
PRODUCTION_BASE = "https://api.elections.kalshi.com/trade-api/v2"

# Demo/sandbox API.
DEMO_BASE = "https://demo-api.kalshi.co/trade-api/v2"


# ============================================================
# HELPERS
# ============================================================

def log(message: str):
    timestamp = dt.datetime.now().strftime("%H:%M:%S")
    st.session_state.logs.append(f"[{timestamp}] {message}")

    # Keep memory under control.
    st.session_state.logs = st.session_state.logs[-100:]


def get_private_key(pem_text: str):
    return serialization.load_pem_private_key(
        pem_text.encode("utf-8"),
        password=None,
    )


def sign_request(
    private_key,
    timestamp: str,
    method: str,
    path: str,
) -> str:

    message = f"{timestamp}{method}{path}".encode("utf-8")

    signature = private_key.sign(
        message,
        padding.PSS(
            mgf=padding.MGF1(hashes.SHA256()),
            salt_length=hashes.SHA256().digest_size,
        ),
        hashes.SHA256(),
    )

    import base64

    return base64.b64encode(signature).decode("utf-8")


def make_headers(
    key_id: str,
    private_key,
    method: str,
    full_url: str,
):
    timestamp = str(int(time.time() * 1000))

    parsed = urlparse(full_url)

    signature = sign_request(
        private_key,
        timestamp,
        method.upper(),
        parsed.path,
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

    def __init__(
        self,
        key_id: str,
        private_key_text: str,
        demo: bool = True,
    ):

        self.key_id = key_id.strip()
        self.private_key = get_private_key(private_key_text)
        self.base_url = DEMO_BASE if demo else PRODUCTION_BASE

    def request(
        self,
        method: str,
        endpoint: str,
        params=None,
        body=None,
    ):

        url = self.base_url + endpoint

        headers = make_headers(
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
            timeout=10,
        )

        if not response.ok:
            raise RuntimeError(
                f"Kalshi API {response.status_code}: "
                f"{response.text}"
            )

        return response.json()

    # --------------------------------------------------------
    # MARKETS
    # --------------------------------------------------------

    def get_markets(self, series_ticker: str):

        return self.request(
            "GET",
            "/markets",
            params={
                "series_ticker": series_ticker,
                "status": "open",
                "limit": 100,
            },
        )

    def get_market(self, ticker: str):

        return self.request(
            "GET",
            f"/markets/{ticker}",
        )

    def get_orderbook(self, ticker: str):

        return self.request(
            "GET",
            f"/markets/{ticker}/orderbook",
        )

    # --------------------------------------------------------
    # ACCOUNT
    # --------------------------------------------------------

    def get_balance(self):

        return self.request(
            "GET",
            "/portfolio/balance",
        )

    # --------------------------------------------------------
    # ORDER
    # --------------------------------------------------------

    def place_yes_order(
        self,
        ticker: str,
        contracts: int,
        price_cents: int,
    ):

        price = Decimal(price_cents) / Decimal(100)

        order = {
            "ticker": ticker,
            "client_order_id": str(uuid.uuid4()),
            "side": "bid",
            "count": str(contracts),
            "price": str(price),
            "time_in_force": "immediate_or_cancel",
            "self_trade_prevention_type": "taker_at_cross",
        }

        return self.request(
            "POST",
            "/portfolio/events/orders",
            body=order,
        )


# ============================================================
# MARKET DISCOVERY
# ============================================================

def choose_active_market(markets):

    if not markets:
        return None

    # Prefer markets that are actually open.
    open_markets = [
        m for m in markets
        if m.get("status") == "open"
    ]

    if not open_markets:
        open_markets = markets

    # Try to select the market expiring soonest.
    def close_time(m):

        value = (
            m.get("close_time")
            or m.get("expiration_time")
            or m.get("expected_expiration_time")
        )

        if not value:
            return "9999-12-31"

        return value

    open_markets.sort(
        key=close_time
    )

    return open_markets[0]


# ============================================================
# ORDERBOOK PARSER
# ============================================================

def get_best_yes_ask(orderbook):

    book = orderbook.get("orderbook", {})

    # Current API representations can vary by endpoint/version.
    # Try the common structures safely.

    yes_asks = book.get("yes_asks")

    if yes_asks:
        prices = []

        for level in yes_asks:
            if isinstance(level, (list, tuple)):
                prices.append(int(level[0]))
            elif isinstance(level, dict):
                if "price" in level:
                    prices.append(int(level["price"]))

        if prices:
            return min(prices)

    # Some older responses expose "yes".
    yes = book.get("yes")

    if yes:
        prices = []

        for level in yes:
            if isinstance(level, (list, tuple)):
                prices.append(int(level[0]))
            elif isinstance(level, dict):
                if "price" in level:
                    prices.append(int(level["price"]))

        if prices:
            return min(prices)

    return None


# ============================================================
# POSITION / P&L
# ============================================================

def record_paper_trade(
    ticker,
    price_cents,
    contracts,
):

    cost = (
        Decimal(price_cents)
        / Decimal(100)
        * Decimal(contracts)
    )

    st.session_state.daily_spent += cost

    st.session_state.paper_trades.append({
        "time": dt.datetime.now().strftime("%H:%M:%S"),
        "ticker": ticker,
        "price": price_cents,
        "contracts": contracts,
        "cost": float(cost),
    })


# ============================================================
# SIDEBAR
# ============================================================

with st.sidebar:

    st.header("⚙️ Bot Configuration")

    mode = st.radio(
        "Trading mode",
        [
            "PAPER — no real orders",
            "LIVE — real money",
        ],
        index=0,
    )

    demo_environment = st.checkbox(
        "Use Kalshi Demo/Sandbox",
        value=True,
        help="Keep this enabled while testing.",
    )

    st.divider()

    st.subheader("🔐 Credentials")

    key_id = st.text_input(
        "Kalshi API Key ID",
        type="password",
    )

    private_key_text = st.text_area(
        "Private Key PEM",
        type="password",
        height=180,
        help="Never share this key with anyone.",
    )

    st.divider()

    st.subheader("📊 Market")

    series_ticker = st.text_input(
        "Market Series",
        value="KXBTC15M",
    )

    st.divider()

    st.subheader("💰 Risk Controls")

    bet_size = st.selectbox(
        "Dollar amount per trade",
        [1, 5, 10, 25, 50, 100],
        index=2,
    )

    daily_cap = st.selectbox(
        "Maximum daily spending",
        [25, 50, 100, 250, 500],
        index=2,
    )

    max_entry = st.slider(
        "Maximum entry price",
        min_value=1,
        max_value=99,
        value=75,
        format="%d¢",
    )

    take_profit = st.slider(
        "Take profit",
        min_value=5,
        max_value=90,
        value=20,
        format="+%d%%",
    )

    stop_loss = st.slider(
        "Stop loss",
        min_value=5,
        max_value=90,
        value=25,
        format="-%d%%",
    )

    cooldown = st.slider(
        "Trade cooldown",
        min_value=10,
        max_value=300,
        value=60,
        step=10,
        format="%d seconds",
    )

    min_signal_move = st.slider(
        "Minimum momentum move",
        min_value=1,
        max_value=20,
        value=3,
        format="%d¢",
    )

    st.divider()

    col1, col2 = st.columns(2)

    with col1:
        start_time = st.time_input(
            "Start",
            dt.time(0, 0),
        )

    with col2:
        end_time = st.time_input(
            "End",
            dt.time(23, 59),
        )


# ============================================================
# DASHBOARD
# ============================================================

col1, col2, col3, col4 = st.columns(4)

with col1:
    st.metric(
        "Mode",
        "PAPER" if mode.startswith("PAPER") else "LIVE",
    )

with col2:
    st.metric(
        "Daily Spent",
        f"${st.session_state.daily_spent:.2f}",
    )

with col3:
    price_display = (
        f"{st.session_state.last_price}¢"
        if st.session_state.last_price
        else "--"
    )

    st.metric(
        "Best Ask",
        price_display,
    )

with col4:
    st.metric(
        "Market",
        st.session_state.active_ticker or "--",
    )


# ============================================================
# CONTROL BUTTONS
# ============================================================

col1, col2, col3 = st.columns(3)

with col1:

    if st.button(
        "▶ START BOT",
        type="primary",
        use_container_width=True,
    ):
        st.session_state.running = True
        log("Bot started.")

with col2:

    if st.button(
        "⏹ STOP BOT",
        use_container_width=True,
    ):
        st.session_state.running = False
        log("Bot stopped.")

with col3:

    if st.button(
        "🗑 RESET SESSION",
        use_container_width=True,
    ):
        st.session_state.running = False
        st.session_state.logs = []
        st.session_state.daily_spent = Decimal("0")
        st.session_state.paper_positions = {}
        st.session_state.paper_trades = []
        st.session_state.active_ticker = None
        st.session_state.last_price = None
        st.session_state.last_signal = "Waiting"

        st.rerun()


# ============================================================
# BOT ENGINE
# ============================================================

if st.session_state.running:

    # --------------------------------------------------------
    # Validate credentials
    # --------------------------------------------------------

    if not key_id or not private_key_text:

        st.error(
            "Enter your Kalshi API credentials before starting."
        )

        st.session_state.running = False
        st.stop()

    # --------------------------------------------------------
    # Trading hours
    # --------------------------------------------------------

    now = dt.datetime.now().time()

    if not (start_time <= now <= end_time):

        st.warning(
            f"Outside trading hours. "
            f"Trading window: {start_time.strftime('%H:%M')} "
            f"to {end_time.strftime('%H:%M')}"
        )

        time.sleep(5)
        st.rerun()

    # --------------------------------------------------------
    # Daily cap
    # --------------------------------------------------------

    if st.session_state.daily_spent >= Decimal(daily_cap):

        st.error(
            f"Daily cap reached: "
            f"${st.session_state.daily_spent:.2f} / "
            f"${daily_cap:.2f}"
        )

        st.session_state.running = False
        st.stop()

    # --------------------------------------------------------
    # Create client
    # --------------------------------------------------------

    try:

        client = KalshiClient(
            key_id=key_id,
            private_key_text=private_key_text,
            demo=demo_environment,
        )

    except Exception as e:

        st.error(
            f"Could not load private key: {e}"
        )

        st.session_state.running = False
        st.stop()

    # --------------------------------------------------------
    # Find active market
    # --------------------------------------------------------

    try:

        response = client.get_markets(
            series_ticker
        )

        markets = response.get(
            "markets",
            [],
        )

        market = choose_active_market(
            markets
        )

        if not market:

            log(
                f"No open markets found for "
                f"{series_ticker}"
            )

            time.sleep(5)
            st.rerun()

        ticker = market["ticker"]

        if (
            st.session_state.active_ticker
            != ticker
        ):

            st.session_state.active_ticker = ticker

            log(
                f"Switched to market: {ticker}"
            )

    except Exception as e:

        log(
            f"Market discovery error: {e}"
        )

        time.sleep(5)
        st.rerun()

    # --------------------------------------------------------
    # Get orderbook
    # --------------------------------------------------------

    try:

        book = client.get_orderbook(
            ticker
        )

        best_ask = get_best_yes_ask(
            book
        )

        if best_ask is None:

            log(
                "No usable ask found."
            )

            time.sleep(3)
            st.rerun()

        st.session_state.last_price = best_ask

        log(
            f"{ticker} | YES ask: "
            f"{best_ask}¢"
        )

    except Exception as e:

        log(
            f"Orderbook error: {e}"
        )

        time.sleep(5)
        st.rerun()

    # ========================================================
    # MOMENTUM SIGNAL
    # ========================================================

    previous_price = (
        st.session_state.last_price
    )

    signal = False

    # Basic entry rule:
    #
    # 1. Price <= maximum entry
    # 2. Price is not zero
    # 3. We haven't traded recently
    # 4. Daily cap remains available
    #
    # This is deliberately conservative.
    #
    # A production strategy should use a proper price history
    # rather than treating a single snapshot as momentum.

    if best_ask <= max_entry:

        signal = True

        st.session_state.last_signal = (
            f"ENTRY: {best_ask}¢ <= {max_entry}¢"
        )

    else:

        st.session_state.last_signal = (
            f"WAIT: {best_ask}¢ > {max_entry}¢"
        )

    # --------------------------------------------------------
    # Cooldown
    # --------------------------------------------------------

    if signal:

        last_trade = (
            st.session_state.last_trade_time
        )

        if last_trade:

            elapsed = (
                dt.datetime.now()
                - last_trade
            ).total_seconds()

            if elapsed < cooldown:

                signal = False

                log(
                    f"Cooldown active "
                    f"({int(cooldown - elapsed)}s)"
                )

    # ========================================================
    # EXECUTE TRADE
    # ========================================================

    if signal:

        # Contracts are determined by dollar risk.
        #
        # Example:
        # $10 at 50¢ = 20 contracts.
        #
        contracts = max(
            1,
            int(
                Decimal(bet_size)
                /
                (
                    Decimal(best_ask)
                    /
                    Decimal(100)
                )
            ),
        )

        estimated_cost = (
            Decimal(contracts)
            *
            Decimal(best_ask)
            /
            Decimal(100)
        )

        # Never exceed daily cap.
        remaining_cap = (
            Decimal(daily_cap)
            -
            st.session_state.daily_spent
        )

        if estimated_cost > remaining_cap:

            contracts = int(
                remaining_cap
                /
                (
                    Decimal(best_ask)
                    /
                    Decimal(100)
                )
            )

            if contracts <= 0:

                log(
                    "Daily cap does not allow "
                    "another contract."
                )

                st.session_state.running = False
                st.stop()

            estimated_cost = (
                Decimal(contracts)
                *
                Decimal(best_ask)
                /
                Decimal(100)
            )

        # ----------------------------------------------------
        # PAPER MODE
        # ----------------------------------------------------

        if mode.startswith("PAPER"):

            record_paper_trade(
                ticker=ticker,
                price_cents=best_ask,
                contracts=contracts,
            )

            log(
                f"📝 PAPER BUY: "
                f"{contracts} YES @ "
                f"{best_ask}¢ "
                f"(${estimated_cost:.2f})"
            )

        # ----------------------------------------------------
        # LIVE MODE
        # ----------------------------------------------------

        else:

            if demo_environment:

                log(
                    "⚠️ LIVE mode selected, "
                    "but Kalshi DEMO environment "
                    "is enabled."
                )

            else:

                try:

                    result = client.place_yes_order(
                        ticker=ticker,
                        contracts=contracts,
                        price_cents=best_ask,
                    )

                    log(
                        f"🟢 LIVE ORDER SENT: "
                        f"{contracts} YES @ "
                        f"{best_ask}¢"
                    )

                    log(
                        f"Order response: "
                        f"{json.dumps(result)[:500]}"
                    )

                except Exception as e:

                    log(
                        f"❌ ORDER FAILED: {e}"
                    )

                    # Important:
                    # Do not count a failed order
                    # against spending.

                    time.sleep(5)
                    st.rerun()

        # ----------------------------------------------------
        # Record trade
        # ----------------------------------------------------

        st.session_state.last_trade_time = (
            dt.datetime.now()
        )

    # ========================================================
    # DISPLAY LOG
    # ========================================================

    st.subheader("📜 Bot Log")

    if st.session_state.logs:

        st.code(
            "\n".join(
                st.session_state.logs[-20:]
            )
        )

    else:

        st.info("Waiting for bot activity...")

    # ========================================================
    # PAPER TRADE TABLE
    # ========================================================

    if st.session_state.paper_trades:

        st.subheader("📝 Paper Trades")

        st.dataframe(
            st.session_state.paper_trades,
            use_container_width=True,
        )

    # --------------------------------------------------------
    # LOOP
    # --------------------------------------------------------

    time.sleep(3)

    st.rerun()

else:

    st.info(
        "Bot is stopped. Start it when you're ready."
    )

    if st.session_state.logs:

        st.subheader("📜 Bot Log")

        st.code(
            "\n".join(
                st.session_state.logs[-20:]
            )
        )

    if st.session_state.paper_trades:

        st.subheader("📝 Paper Trades")

        st.dataframe(
            st.session_state.paper_trades,
            use_container_width=True,
        )
