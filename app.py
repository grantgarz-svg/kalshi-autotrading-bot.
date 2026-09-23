
import streamlit as st
import requests
import time
import datetime as dt
import uuid
import base64
from decimal import Decimal
from urllib.parse import urlparse

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding


# ============================================================
# PAGE CONFIG
# ============================================================

st.set_page_config(
    page_title="Kalshi Scalper",
    page_icon="⚡",
    layout="wide",
)

st.title("⚡ Kalshi Scalper")
st.caption("Short-term Kalshi trading dashboard")


# ============================================================
# SESSION STATE
# ============================================================

if "running" not in st.session_state:
    st.session_state.running = False

if "logs" not in st.session_state:
    st.session_state.logs = []

if "daily_spent" not in st.session_state:
    st.session_state.daily_spent = Decimal("0")

if "paper_trades" not in st.session_state:
    st.session_state.paper_trades = []

if "active_ticker" not in st.session_state:
    st.session_state.active_ticker = None

if "last_price" not in st.session_state:
    st.session_state.last_price = None

if "last_trade_time" not in st.session_state:
    st.session_state.last_trade_time = None


# ============================================================
# API URLS
# ============================================================

DEMO_BASE_URL = "https://demo-api.kalshi.co/trade-api/v2"
LIVE_BASE_URL = "https://api.elections.kalshi.com/trade-api/v2"


# ============================================================
# LOGGING
# ============================================================

def log(message):
    timestamp = dt.datetime.now().strftime("%H:%M:%S")

    st.session_state.logs.append(
        f"[{timestamp}] {message}"
    )

    st.session_state.logs = (
        st.session_state.logs[-50:]
    )


# ============================================================
# PRIVATE KEY
# ============================================================

def load_private_key(private_key_text):

    return serialization.load_pem_private_key(
        private_key_text.encode("utf-8"),
        password=None,
    )


# ============================================================
# KALSHI AUTHENTICATION
# ============================================================

def create_signature(
    private_key,
    timestamp,
    method,
    path,
):

    message = (
        f"{timestamp}{method.upper()}{path}"
    ).encode("utf-8")

    signature = private_key.sign(
        message,
        padding.PSS(
            mgf=padding.MGF1(
                hashes.SHA256()
            ),
            salt_length=hashes.SHA256().digest_size,
        ),
        hashes.SHA256(),
    )

    return base64.b64encode(
        signature
    ).decode("utf-8")


def create_headers(
    key_id,
    private_key,
    method,
    url,
):

    timestamp = str(
        int(time.time() * 1000)
    )

    parsed = urlparse(url)

    signature = create_signature(
        private_key=private_key,
        timestamp=timestamp,
        method=method,
        path=parsed.path,
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
        key_id,
        private_key_text,
        demo=True,
    ):

        self.key_id = key_id.strip()

        self.private_key = (
            load_private_key(
                private_key_text
            )
        )

        if demo:
            self.base_url = DEMO_BASE_URL
        else:
            self.base_url = LIVE_BASE_URL

    def request(
        self,
        method,
        endpoint,
        params=None,
        body=None,
    ):

        url = self.base_url + endpoint

        headers = create_headers(
            key_id=self.key_id,
            private_key=self.private_key,
            method=method,
            url=url,
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
                f"API error {response.status_code}: "
                f"{response.text}"
            )

        return response.json()

    # --------------------------------------------------------
    # GET MARKETS
    # --------------------------------------------------------

    def get_markets(
        self,
        series_ticker,
    ):

        return self.request(
            "GET",
            "/markets",
            params={
                "series_ticker": series_ticker,
                "status": "open",
                "limit": 100,
            },
        )

    # --------------------------------------------------------
    # GET ORDER BOOK
    # --------------------------------------------------------

    def get_orderbook(
        self,
        ticker,
    ):

        return self.request(
            "GET",
            f"/markets/{ticker}/orderbook",
        )

    # --------------------------------------------------------
    # GET BALANCE
    # --------------------------------------------------------

    def get_balance(self):

        return self.request(
            "GET",
            "/portfolio/balance",
        )

    # --------------------------------------------------------
    # PLACE ORDER
    # --------------------------------------------------------

    def place_yes_order(
        self,
        ticker,
        contracts,
        price_cents,
    ):

        price = (
            Decimal(price_cents)
            / Decimal(100)
        )

        order = {
            "ticker": ticker,
            "client_order_id": str(
                uuid.uuid4()
            ),
            "side": "bid",
            "count": contracts,
            "price": str(price),
            "time_in_force": "immediate_or_cancel",
        }

        return self.request(
            "POST",
            "/portfolio/orders",
            body=order,
        )


# ============================================================
# FIND ACTIVE MARKET
# ============================================================

def find_active_market(markets):

    if not markets:
        return None

    open_markets = [
        market
        for market in markets
        if market.get("status") in (
            None,
            "open",
        )
    ]

    if not open_markets:
        return None

    # Sort by close time when available.
    open_markets.sort(
        key=lambda x: (
            x.get("close_time")
            or x.get("expiration_time")
            or "9999"
        )
    )

    return open_markets[0]


# ============================================================
# FIND BEST ASK
# ============================================================

def get_best_ask(orderbook):

    book = orderbook.get(
        "orderbook",
        {},
    )

    # Try YES asks.
    yes_asks = book.get(
        "yes_asks"
    )

    if yes_asks:

        prices = []

        for level in yes_asks:

            if isinstance(
                level,
                (list, tuple),
            ):

                prices.append(
                    int(level[0])
                )

            elif isinstance(
                level,
                dict,
            ):

                if "price" in level:
                    prices.append(
                        int(level["price"])
                    )

        if prices:
            return min(prices)

    # Compatibility with older format.
    yes = book.get("yes")

    if yes:

        prices = []

        for level in yes:

            if isinstance(
                level,
                (list, tuple),
            ):

                prices.append(
                    int(level[0])
                )

            elif isinstance(
                level,
                dict,
            ):

                if "price" in level:
                    prices.append(
                        int(level["price"])
                    )

        if prices:
            return min(prices)

    return None


# ============================================================
# PAPER TRADE
# ============================================================

def record_paper_trade(
    ticker,
    price_cents,
    contracts,
):

    cost = (
        Decimal(contracts)
        *
        Decimal(price_cents)
        /
        Decimal(100)
    )

    st.session_state.daily_spent += cost

    st.session_state.paper_trades.append(
        {
            "Time": dt.datetime.now().strftime(
                "%H:%M:%S"
            ),
            "Ticker": ticker,
            "Side": "YES",
            "Contracts": contracts,
            "Price": f"{price_cents}¢",
            "Cost": f"${cost:.2f}",
        }
    )


# ============================================================
# SIDEBAR
# ============================================================

with st.sidebar:

    st.header("🔑 Account")

    st.info(
        "For safety, PAPER mode is selected by default."
    )

    trading_mode = st.radio(
        "Trading mode",
        [
            "PAPER TRADING",
            "LIVE TRADING",
        ],
    )

    demo_mode = st.checkbox(
        "Use Kalshi Demo",
        value=True,
    )

    st.divider()

    key_id = st.text_input(
        "Kalshi API Key ID",
        type="password",
    )

    # IMPORTANT:
    # Streamlit versions can reject type="password"
    # on st.text_area(), so we intentionally do NOT
    # use type="password" here.

    private_key_text = st.text_area(
        "Private Key PEM",
        height=220,
        placeholder=(
            "-----BEGIN PRIVATE KEY-----\n"
            "Paste your Kalshi private key here\n"
            "-----END PRIVATE KEY-----"
        ),
    )

    st.divider()

    st.header("📊 Market")

    series_ticker = st.text_input(
        "Market Series",
        value="KXBTC15M",
    )

    st.divider()

    st.header("💰 Risk Settings")

    bet_size = st.selectbox(
        "Maximum dollars per trade",
        [1, 5, 10, 25, 50, 100],
        index=2,
    )

    daily_cap = st.selectbox(
        "Daily spending limit",
        [25, 50, 100, 250, 500],
        index=2,
    )

    max_entry = st.slider(
        "Maximum entry price",
        1,
        99,
        75,
        format="%d¢",
    )

    cooldown = st.slider(
        "Cooldown between trades",
        10,
        300,
        60,
        step=10,
        format="%d seconds",
    )

    st.divider()

    st.header("🕐 Trading Hours")

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

st.subheader("Dashboard")

col1, col2, col3, col4 = st.columns(4)

with col1:

    st.metric(
        "Mode",
        trading_mode,
    )

with col2:

    st.metric(
        "Daily Spent",
        f"${st.session_state.daily_spent:.2f}",
    )

with col3:

    if st.session_state.last_price:
        price = (
            f"{st.session_state.last_price}¢"
        )
    else:
        price = "--"

    st.metric(
        "Best Ask",
        price,
    )

with col4:

    st.metric(
        "Market",
        st.session_state.active_ticker
        or "--",
    )


# ============================================================
# START / STOP
# ============================================================

col1, col2 = st.columns(2)

with col1:

    if st.button(
        "▶ START AUTOTRADING",
        type="primary",
        use_container_width=True,
    ):

        if not key_id:

            st.error(
                "Enter your Kalshi API Key ID."
            )

        elif not private_key_text:

            st.error(
                "Enter your Kalshi private key."
            )

        else:

            st.session_state.running = True

            log(
                "Bot started."
            )

            st.rerun()


with col2:

    if st.button(
        "⏹ STOP",
        use_container_width=True,
    ):

        st.session_state.running = False

        log(
            "Bot stopped."
        )

        st.rerun()


# ============================================================
# BOT
# ============================================================

if st.session_state.running:

    # --------------------------------------------------------
    # Check credentials
    # --------------------------------------------------------

    try:

        client = KalshiClient(
            key_id=key_id,
            private_key_text=private_key_text,
            demo=demo_mode,
        )

    except Exception as error:

        st.error(
            f"Private key error: {error}"
        )

        st.session_state.running = False

        st.stop()

    # --------------------------------------------------------
    # Trading hours
    # --------------------------------------------------------

    now = dt.datetime.now().time()

    if not (
        start_time
        <= now
        <= end_time
    ):

        st.warning(
            "Outside your trading hours."
        )

        time.sleep(5)

        st.rerun()

    # --------------------------------------------------------
    # Daily cap
    # --------------------------------------------------------

    if (
        st.session_state.daily_spent
        >= Decimal(daily_cap)
    ):

        st.error(
            "Daily spending limit reached."
        )

        st.session_state.running = False

        st.stop()

    # --------------------------------------------------------
    # Find market
    # --------------------------------------------------------

    try:

        response = client.get_markets(
            series_ticker
        )

        markets = response.get(
            "markets",
            [],
        )

        market = find_active_market(
            markets
        )

        if not market:

            log(
                f"No active market found "
                f"for {series_ticker}."
            )

            time.sleep(5)

            st.rerun()

        ticker = market.get(
            "ticker"
        )

        st.session_state.active_ticker = (
            ticker
        )

    except Exception as error:

        log(
            f"Market error: {error}"
        )

        time.sleep(5)

        st.rerun()

    # --------------------------------------------------------
    # Order book
    # --------------------------------------------------------

    try:

        orderbook = client.get_orderbook(
            ticker
        )

        best_ask = get_best_ask(
            orderbook
        )

        if best_ask is None:

            log(
                "No YES ask available."
            )

            time.sleep(3)

            st.rerun()

        st.session_state.last_price = (
            best_ask
        )

        log(
            f"{ticker} | YES ask = "
            f"{best_ask}¢"
        )

    except Exception as error:

        log(
            f"Orderbook error: {error}"
        )

        time.sleep(5)

        st.rerun()

    # --------------------------------------------------------
    # ENTRY SIGNAL
    # --------------------------------------------------------

    signal = (
        best_ask <= max_entry
    )

    # --------------------------------------------------------
    # COOLDOWN
    # --------------------------------------------------------

    if signal:

        last_trade = (
            st.session_state.last_trade_time
        )

        if last_trade:

            seconds_since_trade = (
                dt.datetime.now()
                - last_trade
            ).total_seconds()

            if seconds_since_trade < cooldown:

                signal = False

                log(
                    "Cooldown active."
                )

    # --------------------------------------------------------
    # TRADE
    # --------------------------------------------------------

    if signal:

        # Calculate contracts from dollar amount.
        #
        # Example:
        # $10 at 50¢ = 20 contracts.

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

        remaining = (
            Decimal(daily_cap)
            -
            st.session_state.daily_spent
        )

        if estimated_cost > remaining:

            contracts = int(
                remaining
                /
                (
                    Decimal(best_ask)
                    /
                    Decimal(100)
                )
            )

        if contracts > 0:

            # ================================================
            # PAPER
            # ================================================

            if trading_mode == "PAPER TRADING":

                record_paper_trade(
                    ticker=ticker,
                    price_cents=best_ask,
                    contracts=contracts,
                )

                log(
                    f"📝 PAPER BUY: "
                    f"{contracts} YES "
                    f"@ {best_ask}¢"
                )

            # ================================================
            # LIVE
            # ================================================

            else:

                # Extra safety check.
                if demo_mode:

                    log(
                        "⚠️ LIVE mode selected, "
                        "but Demo API is enabled."
                    )

                    try:

                        result = (
                            client.place_yes_order(
                                ticker=ticker,
                                contracts=contracts,
                                price_cents=best_ask,
                            )
                        )

                        log(
                            "DEMO order submitted."
                        )

                    except Exception as error:

                        log(
                            f"Order failed: {error}"
                        )

                else:

                    st.warning(
                        "LIVE TRADING IS ENABLED."
                    )

                    try:

                        result = (
                            client.place_yes_order(
                                ticker=ticker,
                                contracts=contracts,
                                price_cents=best_ask,
                            )
                        )

                        log(
                            f"🟢 LIVE ORDER SENT: "
                            f"{contracts} YES "
                            f"@ {best_ask}¢"
                        )

                    except Exception as error:

                        log(
                            f"❌ LIVE ORDER FAILED: "
                            f"{error}"
                        )

            st.session_state.last_trade_time = (
                dt.datetime.now()
            )

    # ========================================================
    # LOG
    # ========================================================

    st.subheader("📜 Bot Log")

    if st.session_state.logs:

        st.code(
            "\n".join(
                st.session_state.logs[-20:]
            )
        )

    else:

        st.info(
            "Waiting for bot activity..."
        )

    # ========================================================
    # PAPER TRADES
    # ========================================================

    if st.session_state.paper_trades:

        st.subheader(
            "📝 Paper Trades"
        )

        st.dataframe(
            st.session_state.paper_trades,
            use_container_width=True,
            hide_index=True,
        )

    # ========================================================
    # REFRESH
    # ========================================================

    time.sleep(3)

    st.rerun()

else:

    st.info(
        "Bot is stopped. "
        "Choose your settings and press "
        "'START AUTOTRADING'."
    )

    if st.session_state.logs:

        st.subheader(
            "📜 Bot Log"
        )

        st.code(
            "\n".join(
                st.session_state.logs[-20:]
            )
        )

    if st.session_state.paper_trades:

        st.subheader(
            "📝 Paper Trades"
        )

        st.dataframe(
            st.session_state.paper_trades,
            use_container_width=True,
            hide_index=True,
        )
```
